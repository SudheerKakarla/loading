"""Mixed-length truck loading (additional model, not a replacement).

Same trailer usable length as the pad map. Lot size along the truck is NOT a
fixed 20/21/22/30 grid: each lot is the assigned pallet's length from
``palletLengthDict`` (inches or metres — whatever the CSV already uses).
1.0 m vs 1.2 m is not hardcoded.

Each truck has two rows (left/right). Layer 2 sits on layer 1 in the same
lot and must use the same length. Sum of lot lengths on a row <= usable L.
Axle split uses lot x-center along L, not A/B/C/D pad groups.
"""

from __future__ import annotations

import math
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
from scipy.optimize import Bounds, milp

from loadmax.container_loading_highs import ConstraintBuilder, VariableRegistry
from loadmax.models import Load_Obj as LoadObj


def _as_float(val: Any, default: float = 0.0) -> float:
    try:
        x = float(val)
    except (TypeError, ValueError):
        return default
    return x if math.isfinite(x) else default


def _positive(val: Any) -> Optional[float]:
    if val is None or val == "" or val is False:
        return None
    if isinstance(val, (list, tuple, dict, set)):
        return None
    x = _as_float(val, 0.0)
    if x <= 0:
        return None
    return x


def _yes(val: Any) -> bool:
    return str(val).strip().lower() in {"yes", "true", "1"}


def _require_finite_map(ip: dict, key: str, ids: Sequence[Any]) -> Dict[Any, float]:
    raw = ip.get(key) or {}
    out: Dict[Any, float] = {}
    missing = []
    for plt in ids:
        if plt not in raw or raw[plt] in (None, ""):
            missing.append(plt)
            continue
        x = _as_float(raw[plt], float("nan"))
        if not math.isfinite(x):
            missing.append(plt)
            continue
        out[plt] = x
    if missing:
        raise ValueError(
            f"{key} missing/invalid for: "
            + ", ".join(str(p) for p in missing[:12])
            + ("..." if len(missing) > 12 else "")
        )
    return out


def pallet_length_map(ip: dict) -> Dict[Any, float]:
    """Lengths from palletLengthDict. No default 1.0 / 1.2."""
    ids = list(ip.get("fullPalletList") or []) + list(ip.get("partialPalletList") or [])
    out = _require_finite_map(ip, "palletLengthDict", ids)
    bad = [plt for plt, x in out.items() if x <= 0]
    if bad:
        raise ValueError(
            "palletLengthDict must be positive for: "
            + ", ".join(str(p) for p in bad[:12])
            + ("..." if len(bad) > 12 else "")
        )
    return out


def usable_length(ip: dict, lengths: Dict[Any, float]) -> float:
    """Same usable L as the old pad row: n_along * typical lot, unless CSV sets it."""
    for key in (
        "usableLength",
        "usable_length",
        "containerLength",
        "container_length",
        "trailerLength",
        "trailer_length",
    ):
        x = _positive(ip.get(key))
        if x is not None:
            return x
    left = list(dict.fromkeys(ip.get("leftPositionList") or []))
    n_along = len(left) if left else max(1, int(ip.get("max_plt_n") or 2) // 2)
    vals = list(lengths.values())
    typical = max(vals)
    return float(n_along) * typical


class ContainerLoadingOptimizerMixedLength:
    """HiGHS MIP: endogenous 1-D lots on two rows, lengths from the CSV."""

    def __init__(
        self,
        inputParams: dict,
        run_ocean: bool = False,
        time_limit: float = 600.0,
        gap: float = 0.01,
        sku_penalty_factor: float = 1.0,
        vdr_stacking_rules: bool = False,
    ) -> None:
        if not 0.0 <= gap < 1.0:
            raise ValueError(f"gap must be in [0, 1), got {gap}")
        if time_limit <= 0:
            raise ValueError(f"time_limit must be positive, got {time_limit}")
        self.inputParams = inputParams
        self.run_ocean = run_ocean
        self.time_limit = time_limit
        self.gap = gap
        self.sku_penalty_factor = sku_penalty_factor
        self.vdr_stacking_rules = vdr_stacking_rules
        self.enforceAxleWeightConstraints = False
        self.enforceWeightBalancing = False
        self.enforceCompactPacking = False
        self._reg: Optional[VariableRegistry] = None
        self._cb: Optional[ConstraintBuilder] = None
        self._result = None
        self._lengths: Dict[Any, float] = {}
        self._L = 0.0
        self._kmax = 0
        self._split = 0.0
        self._door_zone = 0.0
        self._sides = ("left", "right")
        self._k_id: Tuple[int, ...] = ()
        self._max_front_wt: Optional[float] = None
        self._max_rear_wt: Optional[float] = None

    def _container_priority(self, container_id: Any) -> float:
        raw = self.inputParams.get("container_priority") or {}
        if container_id in raw:
            return float(raw[container_id])
        if isinstance(container_id, (int, float)):
            if int(container_id) in raw:
                return float(raw[int(container_id)])
        return 1.0

    def _set_flags(self) -> None:
        ip = self.inputParams
        self._lengths = pallet_length_map(ip)
        plt_ids = list(self._lengths)
        _require_finite_map(ip, "palletWeightDict", plt_ids)
        _require_finite_map(ip, "palletHeightDict", plt_ids)
        self._L = usable_length(ip, self._lengths)
        min_len = min(self._lengths.values())
        self._kmax = max(1, int(self._L // min_len))
        self._k_id = tuple(range(1, self._kmax + 1))
        left = list(dict.fromkeys(ip.get("leftPositionList") or []))
        front = set(ip.get("frontPositionList") or [])
        n_left = len(left) or self._kmax
        n_front_left = len([p for p in left if p in front]) or max(1, n_left // 2)
        self._split = self._L * (n_front_left / n_left)
        self._door_zone = 2.0 * min_len
        self._max_front_wt = _positive(ip.get("max_front_wt"))
        self._max_rear_wt = _positive(ip.get("max_rear_wt"))
        if self.run_ocean:
            self.enforceWeightBalancing = False
            self.enforceAxleWeightConstraints = False
            self.enforceCompactPacking = False
        else:
            self.enforceWeightBalancing = True
            self.enforceAxleWeightConstraints = (
                self._max_front_wt is not None and self._max_rear_wt is not None
            )
            self.enforceCompactPacking = _yes(ip.get("enforce_compact_packing", "No"))
        print(f"  Usable length L : {self._L:g} (same units as palletLengthDict)")
        print(f"  Distinct lengths: {sorted(set(self._lengths.values()))}")
        print(f"  Lots per row    : up to {self._kmax} (L / min length)")
        print(f"  Axle split x    : {self._split:g}")
        print(f"  Compact packing : {self.enforceCompactPacking}")
        print(f"  Axle limits     : {self.enforceAxleWeightConstraints}")

    def build_model(self) -> None:
        print("=" * 70)
        print("BUILDING MIXED-LENGTH MODEL  [HiGHS / scipy.optimize.milp]")
        print("=" * 70)
        self._set_flags()
        print("\n[1/3] Registering variables ...")
        self._reg = VariableRegistry()
        self._register_variables()
        print("\n[2/3] Constraints ...")
        self._cb = ConstraintBuilder(len(self._reg))
        self._add_constraints()
        print("\n[3/3] Objective ...")
        self._build_objective()
        print("\nMODEL BUILD COMPLETE")
        print(f"  Variables   : {len(self._reg)}")
        print(f"  Constraints : {len(self._cb._rows)}")
        print("=" * 70 + "\n")

    def _register_variables(self) -> None:
        ip = self.inputParams
        reg = self._reg
        veh = ip["containerList"]
        lyr = ip["layerList"]
        full = ip["fullPalletList"] or []
        prtl = ip["partialPalletList"] or []
        sides = self._sides
        k_id = self._k_id
        L = self._L
        max_wt = _as_float(ip.get("max_wt"), L * 1000.0) or 1e6
        max_ht = _as_float(ip.get("containerHeight"), 9.0)

        if full:
            reg.add("y_full", veh, lyr, sides, k_id, full, integrality=1, lb=0, ub=1)
        if prtl:
            reg.add("y_prtl", veh, lyr, sides, k_id, prtl, integrality=1, lb=0, ub=1)
        reg.add("occ", veh, sides, k_id, integrality=1, lb=0, ub=1)
        reg.add("occ2", veh, sides, k_id, integrality=1, lb=0, ub=1)
        reg.add("col_len", veh, sides, k_id, integrality=0, lb=0, ub=L)
        reg.add("col_wt", veh, sides, k_id, integrality=0, lb=0, ub=max_wt)
        reg.add("x_end", veh, sides, k_id, integrality=0, lb=0, ub=L)
        reg.add("is_front", veh, sides, k_id, integrality=1, lb=0, ub=1)
        reg.add("is_door", veh, sides, k_id, integrality=1, lb=0, ub=1)
        reg.add("w_front", veh, sides, k_id, integrality=0, lb=0, ub=max_wt)
        reg.add("stk_ht", veh, sides, k_id, integrality=0, lb=0, ub=max_ht)

        max_prior_full = ip.get("max_prior_full") or {}
        max_prior_prtl = ip.get("max_prior_prtl") or {}
        for plt in full:
            reg.add_single(
                "y_prior_full", plt, integrality=0, lb=0,
                ub=float(max_prior_full.get(plt, 0) or 0),
            )
        for plt in prtl:
            reg.add_single(
                "y_prior_prtl", plt, integrality=0, lb=0,
                ub=float(max_prior_prtl.get(plt, 0) or 0),
            )

        mtl_id = ip.get("materialList") or []
        if mtl_id:
            reg.add("mtl_plt", veh, mtl_id, integrality=1, lb=0)
            reg.add("y_mtl_plt", veh, mtl_id, integrality=1, lb=0, ub=1)
            reg.add("mtl_n", veh, integrality=1, lb=0)
        reg.add_single("exceed_mtl_n", integrality=1, lb=0)
        print(f"  Registered {len(reg)} variables.")

    def _add_constraints(self) -> None:
        ip = self.inputParams
        reg = self._reg
        cb = self._cb
        veh = ip["containerList"]
        lyr = list(ip["layerList"])
        l1 = lyr[0]
        l2 = lyr[1] if len(lyr) > 1 else None
        full = ip["fullPalletList"] or []
        prtl = ip["partialPalletList"] or []
        sides = self._sides
        k_id = self._k_id
        L = self._L
        lengths = self._lengths
        wt = ip["palletWeightDict"]
        ht = ip["palletHeightDict"]
        max_wt = _as_float(ip.get("max_wt"), 0.0)
        max_ht = _as_float(ip.get("containerHeight"), 9.0)
        door_ht = _as_float(ip.get("containerHeightAtDoor"), max_ht)
        allow_ht = _as_float(ip.get("load_allow_ht"), 0.0)
        full_demand = ip.get("fullPalletDemandByMaterialBatch") or {}
        prtl_demand = ip.get("partialPalletDemandByMaterialBatch") or {}
        plt_mtl = ip.get("palletMaterialDict") or {}
        mtl_id = ip.get("materialList") or []
        fb_tol = _as_float(ip.get("front_to_back_tolerance"), 0.0)
        ss_tol = _as_float(ip.get("side_to_side_tolerance"), 0.0)
        unique_sku = int(ip.get("unique_sku_n") or 0)
        max_lyr_n = int(ip.get("max_lyr_n") or len(lyr))
        max_plt_n = int(ip.get("max_plt_n") or (2 * self._kmax))
        sku_scale = float(max(1, max_lyr_n * max_plt_n))
        split = self._split
        door_from = max(0.0, L - self._door_zone)

        def cell_y(v, l, s, k):
            coeffs: Dict[int, float] = {}
            for plt in full:
                coeffs[reg["y_full", v, l, s, k, plt]] = 1.0
            for plt in prtl:
                coeffs[reg["y_prtl", v, l, s, k, plt]] = 1.0
            return coeffs

        def cell_len(v, l, s, k):
            coeffs: Dict[int, float] = {}
            for plt in full:
                coeffs[reg["y_full", v, l, s, k, plt]] = lengths[plt]
            for plt in prtl:
                coeffs[reg["y_prtl", v, l, s, k, plt]] = lengths[plt]
            return coeffs

        def cell_wt(v, l, s, k):
            coeffs: Dict[int, float] = {}
            for plt in full:
                coeffs[reg["y_full", v, l, s, k, plt]] = _as_float(wt.get(plt), 0.0)
            for plt in prtl:
                coeffs[reg["y_prtl", v, l, s, k, plt]] = _as_float(wt.get(plt), 0.0)
            return coeffs

        def cell_ht(v, l, s, k):
            coeffs: Dict[int, float] = {}
            for plt in full:
                coeffs[reg["y_full", v, l, s, k, plt]] = _as_float(ht.get(plt), 0.0)
            for plt in prtl:
                coeffs[reg["y_prtl", v, l, s, k, plt]] = _as_float(ht.get(plt), 0.0)
            return coeffs

        # Demand
        for plt in full:
            coeffs = {
                reg["y_full", v, l, s, k, plt]: 1.0
                for v in veh for l in lyr for s in sides for k in k_id
            }
            cb.add_le(coeffs, float(full_demand.get(plt, 0) or 0))
        for plt in prtl:
            coeffs = {
                reg["y_prtl", v, l, s, k, plt]: 1.0
                for v in veh for l in lyr for s in sides for k in k_id
            }
            cb.add_le(coeffs, float(prtl_demand.get(plt, 0) or 0))

        for v in veh:
            for s in sides:
                for k in k_id:
                    # at most one pallet per (layer, lot)
                    for l in lyr:
                        cb.add_le(cell_y(v, l, s, k), 1.0)

                    occ = reg["occ", v, s, k]
                    occ2 = reg["occ2", v, s, k]
                    col_len = reg["col_len", v, s, k]
                    col_wt = reg["col_wt", v, s, k]
                    stk = reg["stk_ht", v, s, k]

                    y1 = cell_y(v, l1, s, k)
                    cb.add_eq({**y1, occ: -1.0}, 0.0)

                    len1 = cell_len(v, l1, s, k)
                    cb.add_eq({**len1, col_len: -1.0}, 0.0)

                    wt_all: Dict[int, float] = {col_wt: 1.0}
                    ht_all: Dict[int, float] = {stk: 1.0}
                    for l in lyr:
                        for idx, coef in cell_wt(v, l, s, k).items():
                            wt_all[idx] = wt_all.get(idx, 0.0) - coef
                        for idx, coef in cell_ht(v, l, s, k).items():
                            ht_all[idx] = ht_all.get(idx, 0.0) - coef
                    cb.add_eq(wt_all, 0.0)
                    cb.add_eq(ht_all, 0.0)

                    if l2 is not None:
                        y2 = cell_y(v, l2, s, k)
                        cb.add_eq({**y2, occ2: -1.0}, 0.0)
                        cb.add_le({occ2: 1.0, occ: -1.0}, 0.0)
                        # layer 2 length = col_len when occupied; unconstrained when empty
                        # len2 <= col_len
                        # len2 >= col_len - L * (1 - occ2)
                        len2 = cell_len(v, l2, s, k)
                        cb.add_le({**len2, col_len: -1.0}, 0.0)
                        cb.add_ge({**len2, col_len: -1.0, occ2: -L}, -L)
                        # no full/partial on a partial
                        for plt in prtl:
                            y_pr = reg["y_prtl", v, l1, s, k, plt]
                            cb.add_le({y_pr: 1.0, occ2: 1.0}, 1.0)

                    # compact: fill from nose
                    if self.enforceCompactPacking and k > 1:
                        cb.add_le(
                            {occ: 1.0, reg["occ", v, s, k - 1]: -1.0},
                            0.0,
                        )

                    # x_end
                    xk = reg["x_end", v, s, k]
                    if k == 1:
                        cb.add_eq({xk: 1.0, col_len: -1.0}, 0.0)
                    else:
                        cb.add_eq(
                            {xk: 1.0, reg["x_end", v, s, k - 1]: -1.0, col_len: -1.0},
                            0.0,
                        )

                    # is_front: x_center = x_end - 0.5 col_len
                    isf = reg["is_front", v, s, k]
                    # x_center - split <= L (1 - is_front)
                    cb.add_le(
                        {xk: 1.0, col_len: -0.5, isf: L},
                        split + L,
                    )
                    # split - x_center <= L * is_front
                    cb.add_le(
                        {xk: -1.0, col_len: 0.5, isf: -L},
                        -split,
                    )

                    isd = reg["is_door", v, s, k]
                    # is_door=1 => x_center >= door_from
                    cb.add_ge(
                        {xk: 1.0, col_len: -0.5, isd: -L},
                        door_from - L,
                    )
                    # is_door=0 => x_center <= door_from
                    cb.add_le(
                        {xk: 1.0, col_len: -0.5, isd: -L},
                        door_from,
                    )

                    wf = reg["w_front", v, s, k]
                    cb.add_le({wf: 1.0, col_wt: -1.0}, 0.0)
                    cb.add_le({wf: 1.0, isf: -max_wt}, 0.0)
                    cb.add_ge({wf: 1.0, col_wt: -1.0, isf: -max_wt}, -max_wt)

                    cap_h = max_ht - allow_ht
                    door_cap = door_ht - allow_ht
                    # stk_ht <= cap_h - (cap_h - door_cap)*is_door
                    cb.add_le({stk: 1.0, isd: (cap_h - door_cap)}, cap_h)

                    # crushing: layer1 wt >= layer2 wt
                    if l2 is not None:
                        w1 = cell_wt(v, l1, s, k)
                        w2 = cell_wt(v, l2, s, k)
                        cr = dict(w1)
                        for idx, coef in w2.items():
                            cr[idx] = cr.get(idx, 0.0) - coef
                        cb.add_ge(cr, 0.0)

                # row length
                cb.add_le(
                    {reg["col_len", v, s, k]: 1.0 for k in k_id},
                    L,
                )

            # truck weight
            if max_wt > 0:
                tw: Dict[int, float] = {}
                for s in sides:
                    for k in k_id:
                        tw[reg["col_wt", v, s, k]] = 1.0
                cb.add_le(tw, max_wt)

            if self.enforceAxleWeightConstraints:
                front: Dict[int, float] = {}
                tot: Dict[int, float] = {}
                for s in sides:
                    for k in k_id:
                        front[reg["w_front", v, s, k]] = 1.0
                        tot[reg["col_wt", v, s, k]] = 1.0
                cb.add_le(front, self._max_front_wt)
                rear = dict(tot)
                for idx, coef in front.items():
                    rear[idx] = rear.get(idx, 0.0) - coef
                cb.add_le(rear, self._max_rear_wt)

            if self.enforceWeightBalancing and ss_tol > 0:
                left_w = {reg["col_wt", v, "left", k]: 1.0 for k in k_id}
                right_w = {reg["col_wt", v, "right", k]: 1.0 for k in k_id}
                m1 = dict(left_w)
                for idx, coef in right_w.items():
                    m1[idx] = m1.get(idx, 0.0) - (1 + ss_tol) * coef
                cb.add_le(m1, 0.0)
                m2 = dict(left_w)
                for idx, coef in right_w.items():
                    m2[idx] = m2.get(idx, 0.0) - (1 - ss_tol) * coef
                cb.add_ge(m2, 0.0)

            if self.enforceWeightBalancing and fb_tol > 0:
                fw = {reg["w_front", v, s, k]: 1.0 for s in sides for k in k_id}
                tw = {reg["col_wt", v, s, k]: 1.0 for s in sides for k in k_id}
                rw = dict(tw)
                for idx, coef in fw.items():
                    rw[idx] = rw.get(idx, 0.0) - coef
                # front <= (1+tol)*rear
                a = dict(fw)
                for idx, coef in rw.items():
                    a[idx] = a.get(idx, 0.0) - (1 + fb_tol) * coef
                cb.add_le(a, 0.0)
                b = dict(fw)
                for idx, coef in rw.items():
                    b[idx] = b.get(idx, 0.0) - (1 - fb_tol) * coef
                cb.add_ge(b, 0.0)

        # SKU tracking
        if mtl_id:
            for v in veh:
                for mtl in mtl_id:
                    mc: Dict[int, float] = {reg["mtl_plt", v, mtl]: 1.0}
                    for plt in full:
                        if plt_mtl.get(plt) == mtl:
                            for l in lyr:
                                for s in sides:
                                    for k in k_id:
                                        mc[reg["y_full", v, l, s, k, plt]] = -1.0
                    for plt in prtl:
                        if plt_mtl.get(plt) == mtl:
                            for l in lyr:
                                for s in sides:
                                    for k in k_id:
                                        mc[reg["y_prtl", v, l, s, k, plt]] = -1.0
                    cb.add_eq(mc, 0.0)
                    cb.add_le(
                        {reg["mtl_plt", v, mtl]: 1.0, reg["y_mtl_plt", v, mtl]: -sku_scale},
                        0.0,
                    )
                mn: Dict[int, float] = {reg["mtl_n", v]: 1.0}
                for mtl in mtl_id:
                    mn[reg["y_mtl_plt", v, mtl]] = -1.0
                cb.add_eq(mn, 0.0)
            ex: Dict[int, float] = {reg["exceed_mtl_n"]: 1.0}
            for v in veh:
                ex[reg["mtl_n", v]] = -1.0
            cb.add_ge(ex, -float(unique_sku))

        for plt in full:
            coeffs = {reg["y_prior_full", plt]: 1.0}
            for v in veh:
                for l in lyr:
                    for s in sides:
                        for k in k_id:
                            coeffs[reg["y_full", v, l, s, k, plt]] = -1.0
            cb.add_le(coeffs, 0.0)
        for plt in prtl:
            coeffs = {reg["y_prior_prtl", plt]: 1.0}
            for v in veh:
                for l in lyr:
                    for s in sides:
                        for k in k_id:
                            coeffs[reg["y_prtl", v, l, s, k, plt]] = -1.0
            cb.add_le(coeffs, 0.0)

        if self.vdr_stacking_rules and l2 is not None:
            g2 = ip.get("stackFactorg2Pallet") or {}
            g3 = ip.get("stackFactorg3Pallet") or {}
            g4 = ip.get("stackFactorg4Pallet") or ip.get("stackFactorq4Pallet") or {}
            for v in veh:
                for s in sides:
                    for k in k_id:
                        for plt in full:
                            if g2.get(plt, 0) == 1 or g4.get(plt, 0) == 1:
                                cb.add_eq({reg["y_full", v, l2, s, k, plt]: 1.0}, 0.0)
                            if g3.get(plt, 0) == 1 or g4.get(plt, 0) == 1:
                                cb.add_le(
                                    {reg["y_full", v, l1, s, k, plt]: 1.0, reg["occ2", v, s, k]: 1.0},
                                    1.0,
                                )
                        for plt in prtl:
                            if g2.get(plt, 0) == 1 or g4.get(plt, 0) == 1:
                                cb.add_eq({reg["y_prtl", v, l2, s, k, plt]: 1.0}, 0.0)
                            if g3.get(plt, 0) == 1 or g4.get(plt, 0) == 1:
                                cb.add_le(
                                    {reg["y_prtl", v, l1, s, k, plt]: 1.0, reg["occ2", v, s, k]: 1.0},
                                    1.0,
                                )

        print(f"  Constraints: {len(cb._rows)}")

    def _build_objective(self) -> None:
        ip = self.inputParams
        reg = self._reg
        veh = ip["containerList"]
        lyr = ip["layerList"]
        full = ip["fullPalletList"] or []
        prtl = ip["partialPalletList"] or []
        plt_mtl = ip.get("palletMaterialDict") or {}
        incentive = ip.get("priorityIncentive", 1.0) or 1.0
        sku_penalty = ip.get("sku_penalty", 0.0) or 0.0
        sku_map = ip.get("skuPriorityIncentive") or {}
        prtl_frac = ip.get("partialFractionValueForPartialPallet") or {}

        def _frac(plt: Any) -> float:
            f = _as_float(prtl_frac.get(plt, 1.0), 1.0)
            return f if f > 0 else 1.0

        def _is_high(plt: Any) -> bool:
            if plt in sku_map:
                return True
            mtl = plt_mtl.get(plt)
            if mtl in sku_map or str(mtl) in sku_map:
                return True
            try:
                return int(mtl) in sku_map
            except (TypeError, ValueError):
                return False

        for c in veh:
            cp = self._container_priority(c)
            for l in lyr:
                for s in self._sides:
                    for k in self._k_id:
                        for plt in full:
                            base = cp * incentive if _is_high(plt) else cp
                            reg.set_obj(("y_full", c, l, s, k, plt), -base)
                        for plt in prtl:
                            base = cp * incentive if _is_high(plt) else cp
                            reg.set_obj(("y_prtl", c, l, s, k, plt), -base * _frac(plt))
        for plt in full:
            reg.set_obj(("y_prior_full", plt), -incentive)
        for plt in prtl:
            reg.set_obj(("y_prior_prtl", plt), -incentive)
        if reg.get(("exceed_mtl_n",)) is not None:
            reg.set_obj(("exceed_mtl_n",), sku_penalty * self.sku_penalty_factor)
        print("  Objective: Pyomo 4-term fill (lengths from palletLengthDict).")

    def solve(self) -> dict:
        if self._reg is None:
            raise RuntimeError("Call build_model() before solve().")
        print("\n" + "=" * 70)
        print("SOLVING MIXED-LENGTH  [HiGHS via scipy.optimize.milp]")
        print(f"  Time limit : {self.time_limit} s   gap={self.gap}")
        print("=" * 70)
        t0 = time.perf_counter()
        res = milp(
            c=self._reg.obj_array(),
            integrality=self._reg.integrality_array(),
            bounds=Bounds(lb=self._reg.lb_array(), ub=self._reg.ub_array()),
            constraints=self._cb.to_linear_constraint(),
            options={
                "disp": True,
                "time_limit": self.time_limit,
                "mip_rel_gap": self.gap,
                "presolve": True,
            },
        )
        runtime = time.perf_counter() - t0
        self._result = res
        status_map = {
            0: "OPTIMAL", 1: "TIME/NODE LIMIT",
            2: "INFEASIBLE", 3: "UNBOUNDED", 4: "OTHER",
        }
        has = res.x is not None and res.fun is not None
        obj = None if not has else float(-res.fun)
        print(f"\nStatus  : {status_map.get(res.status, str(res.status))}")
        print(f"Message : {res.message}")
        if has:
            print(f"Objective (max fill): {obj}")
        print(f"Runtime : {runtime:.2f} s")
        return {
            "status": res.status,
            "is_optimal": res.status == 0,
            "objective_value": obj,
            "mip_gap": getattr(res, "mip_gap", None),
            "runtime": runtime,
            "message": status_map.get(res.status, str(res.status)),
            "x": res.x,
            "usable_length": self._L,
            "kmax": self._kmax,
        }

    def generate_loading_instructions(self) -> List[LoadObj]:
        if self._result is None or self._result.x is None:
            print("Cannot generate instructions — no solution.")
            return []
        x = self._result.x
        reg = self._reg
        ip = self.inputParams
        lyr = list(ip["layerList"])
        full = ip["fullPalletList"] or []
        prtl = ip["partialPalletList"] or []
        instr: List[LoadObj] = []

        def _make(v, l, s, k, plt, is_partial: bool) -> LoadObj:
            obj = LoadObj()
            obj.marketID = ip.get("marketID", "")
            obj.palletID = plt
            obj.container = v
            obj.layer = l
            obj.position = k if s == "left" else k
            obj.sequence = k
            obj.material = (ip.get("palletMaterialDict") or {}).get(plt, "")
            obj.batch = (ip.get("palletBatchDict") or {}).get(plt, "")
            obj.bin_loc = s
            obj.location = s
            obj.weight = (ip.get("palletWeightDict") or {}).get(plt, 0)
            obj.height = (ip.get("palletHeightDict") or {}).get(plt, 0)
            raw_h = _as_float(obj.height, 0.0)
            obj.cube = (
                _as_float((ip.get("palletWidthDict") or {}).get(plt, 40.0), 40.0) / 12.0
                * self._lengths[plt] / (12.0 if self._lengths[plt] > 5 else 1.0)
                * (raw_h / 12.0 if raw_h > 5 else raw_h)
            )
            n_cases = (ip.get("palletNumCasesDict") or {}).get(plt, 0)
            if is_partial:
                frac = _as_float(
                    (ip.get("partialFractionValueForPartialPallet") or {}).get(plt, 1.0),
                    1.0,
                )
                obj.cases = n_cases * (frac if frac > 0 else 1.0)
                obj.is_partial = True
                obj.pallet_type = "Partial"
            else:
                obj.cases = n_cases
                obj.is_partial = False
                obj.pallet_type = "Full"
            obj.total_cases = n_cases
            obj.stacking_group = (ip.get("palletStackingGroupDict") or {}).get(plt, "G1")
            obj.shipTo = (ip.get("palletPlantDict") or {}).get(plt, "")
            obj.description = s
            return obj

        for v in ip["containerList"]:
            for s in self._sides:
                for k in self._k_id:
                    for l in lyr:
                        for plt in full:
                            idx = reg.get(("y_full", v, l, s, k, plt))
                            if idx is not None and x[idx] >= 0.9:
                                instr.append(_make(v, l, s, k, plt, False))
                        for plt in prtl:
                            idx = reg.get(("y_prtl", v, l, s, k, plt))
                            if idx is not None and x[idx] >= 0.9:
                                instr.append(_make(v, l, s, k, plt, True))

        instr.sort(key=lambda o: (str(o.container), o.location, o.sequence, o.layer))
        print(f"Generated {len(instr)} mixed-length loading instructions.")
        return instr

    def lot_rows(self) -> List[dict]:
        """Per-lot geometry for the results CSV."""
        if self._result is None or self._result.x is None or self._reg is None:
            return []
        x = self._result.x
        reg = self._reg
        rows = []
        for v in self.inputParams["containerList"]:
            for s in self._sides:
                for k in self._k_id:
                    occ_i = reg.get(("occ", v, s, k))
                    if occ_i is None or x[occ_i] < 0.5:
                        continue
                    col_len = float(x[reg["col_len", v, s, k]])
                    x_end = float(x[reg["x_end", v, s, k]])
                    rows.append({
                        "Container": v,
                        "Side": s,
                        "Lot": k,
                        "LotLength": col_len,
                        "X_end": x_end,
                        "X_center": x_end - 0.5 * col_len,
                        "Is_front": int(x[reg["is_front", v, s, k]] >= 0.5),
                    })
        return rows
