"""Mixed-length truck loading optimizer — Gurobi/gurobipy implementation.

Same trailer usable length and modelling logic as the HiGHS reference model.
Lot size along the truck is NOT a fixed grid: each lot length equals the
assigned pallet's length from ``palletLengthDict``.

Each truck has two rows (left/right).  Layer 2 sits on layer 1 in the same
lot and must use the same length.  Sum of lot lengths on a row <= usable L.
Axle split uses lot x-centre along L, not A/B/C/D pad groups.

Solver back-end: Gurobi via gurobipy (replaces scipy.optimize.milp / HiGHS).
"""

from __future__ import annotations

import math
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

import gurobipy as gp
from gurobipy import GRB


# ---------------------------------------------------------------------------
# Utility helpers  (identical logic to the original)
# ---------------------------------------------------------------------------

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
    return x if x > 0 else None


def _yes(val: Any) -> bool:
    return str(val).strip().lower() in {"yes", "true", "1"}


def _require_finite_map(ip: dict, key: str, ids: Sequence[Any]) -> Dict[Any, float]:
    """Validate that every pallet id has a finite numeric entry under ``key``."""
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
    """Return validated pallet-length dict (no defaults — every entry must be present)."""
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
    """Determine usable trailer length; explicit CSV value takes precedence."""
    for key in (
        "usableLength", "usable_length",
        "containerLength", "container_length",
        "trailerLength", "trailer_length",
    ):
        x = _positive(ip.get(key))
        if x is not None:
            return x
    left = list(dict.fromkeys(ip.get("leftPositionList") or []))
    n_along = len(left) if left else max(1, int(ip.get("max_plt_n") or 2) // 2)
    return float(n_along) * max(lengths.values())


# ---------------------------------------------------------------------------
# Optimizer class
# ---------------------------------------------------------------------------

class ContainerLoadingOptimizerMixedLength:
    """Gurobi MIP: endogenous 1-D lots on two rows, pallet lengths from the CSV.

    Usage::

        with gp.Env(empty=True) as env:
            env.setParam("OutputFlag", 1)
            env.setParam("TimeLimit", 600)
            env.start()

            with ContainerLoadingOptimizerMixedLength(env, inputParams) as opt:
                opt.build_model()
                result = opt.solve()
                instructions = opt.generate_loading_instructions()
    """

    # ------------------------------------------------------------------
    # Construction / context manager
    # ------------------------------------------------------------------

    def __init__(
        self,
        env: gp.Env,
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

        self.env = env
        self.inputParams = inputParams
        self.run_ocean = run_ocean
        self.time_limit = time_limit
        self.gap = gap
        self.sku_penalty_factor = sku_penalty_factor
        self.vdr_stacking_rules = vdr_stacking_rules

        # Flags set during _set_flags()
        self.enforceAxleWeightConstraints = False
        self.enforceWeightBalancing = False
        self.enforceCompactPacking = False

        # Computed geometry
        self._lengths: Dict[Any, float] = {}
        self._L: float = 0.0
        self._kmax: int = 0
        self._split: float = 0.0
        self._door_zone: float = 0.0
        self._sides: Tuple[str, ...] = ("left", "right")
        self._k_id: Tuple[int, ...] = ()
        self._max_front_wt: Optional[float] = None
        self._max_rear_wt: Optional[float] = None

        # Gurobi model (created in build_model)
        self.model: Optional[gp.Model] = None

        # Variable tupledict references (populated in _create_variables)
        self._y_full: gp.tupledict = gp.tupledict()
        self._y_prtl: gp.tupledict = gp.tupledict()
        self._occ: gp.tupledict = gp.tupledict()
        self._occ2: gp.tupledict = gp.tupledict()
        self._col_len: gp.tupledict = gp.tupledict()
        self._col_wt: gp.tupledict = gp.tupledict()
        self._x_end: gp.tupledict = gp.tupledict()
        self._is_front: gp.tupledict = gp.tupledict()
        self._is_door: gp.tupledict = gp.tupledict()
        self._w_front: gp.tupledict = gp.tupledict()
        self._stk_ht: gp.tupledict = gp.tupledict()
        self._y_prior_full: gp.tupledict = gp.tupledict()
        self._y_prior_prtl: gp.tupledict = gp.tupledict()
        self._mtl_plt: gp.tupledict = gp.tupledict()
        self._y_mtl_plt: gp.tupledict = gp.tupledict()
        self._mtl_n: gp.tupledict = gp.tupledict()
        self._exceed_mtl_n: Optional[gp.Var] = None

    def __enter__(self) -> "ContainerLoadingOptimizerMixedLength":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        if self.model is not None:
            self.model.dispose()
        return False

    # ------------------------------------------------------------------
    # Container priority helper
    # ------------------------------------------------------------------

    def _container_priority(self, container_id: Any) -> float:
        raw = self.inputParams.get("container_priority") or {}
        if container_id in raw:
            return float(raw[container_id])
        if isinstance(container_id, (int, float)) and int(container_id) in raw:
            return float(raw[int(container_id)])
        return 1.0

    # ------------------------------------------------------------------
    # Flag / geometry pre-computation
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def build_model(self) -> None:
        """Build the complete MIP model (variables + constraints + objective)."""
        print("=" * 70)
        print("BUILDING MIXED-LENGTH MODEL  [Gurobi / gurobipy]")
        print("=" * 70)
        self._set_flags()

        self.model = gp.Model("ContainerLoadingMixedLength", env=self.env)
        self.model.Params.TimeLimit = self.time_limit
        self.model.Params.MIPGap = self.gap

        print("\n[1/3] Registering variables ...")
        self._create_variables()

        print("\n[2/3] Constraints ...")
        self._add_constraints()

        print("\n[3/3] Objective ...")
        self._set_objective()

        self.model.update()
        print("\nMODEL BUILD COMPLETE")
        print(f"  Variables   : {self.model.NumVars}")
        print(f"  Constraints : {self.model.NumConstrs}")
        print("=" * 70 + "\n")

    def solve(self) -> dict:
        """Optimise the model and return a result dictionary."""
        if self.model is None:
            raise RuntimeError("Call build_model() before solve().")

        print("\n" + "=" * 70)
        print("SOLVING MIXED-LENGTH  [Gurobi]")
        print(f"  Time limit : {self.time_limit} s   gap={self.gap}")
        print("=" * 70)

        t0 = time.perf_counter()
        self.model.optimize()
        runtime = time.perf_counter() - t0

        status = self.model.Status
        is_optimal = status == GRB.OPTIMAL
        has_solution = self.model.SolCount > 0

        obj = None
        if has_solution:
            obj = float(self.model.ObjVal)

        status_map = {
            GRB.OPTIMAL: "OPTIMAL",
            GRB.TIME_LIMIT: "TIME/NODE LIMIT",
            GRB.INFEASIBLE: "INFEASIBLE",
            GRB.UNBOUNDED: "UNBOUNDED",
            GRB.INF_OR_UNBD: "INF_OR_UNBD",
        }
        msg = status_map.get(status, f"STATUS_{status}")

        print(f"\nStatus  : {msg}")
        if has_solution:
            print(f"Objective (max fill): {obj:.4f}")
        print(f"Runtime : {runtime:.2f} s")

        return {
            "status": status,
            "is_optimal": is_optimal,
            "objective_value": obj,
            "mip_gap": self.model.MIPGap if has_solution else None,
            "runtime": runtime,
            "message": msg,
            "usable_length": self._L,
            "kmax": self._kmax,
        }

    def generate_loading_instructions(self) -> List[dict]:
        """Return a list of placement dicts for each loaded pallet.

        Each dict contains the pallet assignment details needed to produce
        loading instructions.  (Returns plain dicts to avoid dependency on
        the internal LoadObj class.)
        """
        if self.model is None or self.model.SolCount == 0:
            print("Cannot generate instructions — no solution.")
            return []

        ip = self.inputParams
        lyr = list(ip["layerList"])
        full = ip["fullPalletList"] or []
        prtl = ip["partialPalletList"] or []
        prtl_frac = ip.get("partialFractionValueForPartialPallet") or {}
        instr: List[dict] = []

        for v in ip["containerList"]:
            for s in self._sides:
                for k in self._k_id:
                    for l in lyr:
                        for plt in full:
                            if (v, l, s, k, plt) in self._y_full:
                                if self._y_full[v, l, s, k, plt].X >= 0.9:
                                    instr.append(self._make_instruction(
                                        v, l, s, k, plt, is_partial=False))
                        for plt in prtl:
                            if (v, l, s, k, plt) in self._y_prtl:
                                if self._y_prtl[v, l, s, k, plt].X >= 0.9:
                                    instr.append(self._make_instruction(
                                        v, l, s, k, plt, is_partial=True))

        instr.sort(key=lambda o: (str(o["container"]), o["location"], o["sequence"], o["layer"]))
        print(f"Generated {len(instr)} mixed-length loading instructions.")
        return instr

    def lot_rows(self) -> List[dict]:
        """Per-lot geometry for the results CSV / diagnostics."""
        if self.model is None or self.model.SolCount == 0:
            return []
        rows = []
        for v in self.inputParams["containerList"]:
            for s in self._sides:
                for k in self._k_id:
                    if (v, s, k) not in self._occ:
                        continue
                    if self._occ[v, s, k].X < 0.5:
                        continue
                    col_len = float(self._col_len[v, s, k].X)
                    x_end = float(self._x_end[v, s, k].X)
                    rows.append({
                        "Container": v,
                        "Side": s,
                        "Lot": k,
                        "LotLength": col_len,
                        "X_end": x_end,
                        "X_center": x_end - 0.5 * col_len,
                        "Is_front": int(self._is_front[v, s, k].X >= 0.5),
                    })
        return rows

    # ------------------------------------------------------------------
    # Variable creation
    # ------------------------------------------------------------------

    def _create_variables(self) -> None:
        m = self.model
        ip = self.inputParams
        veh = ip["containerList"]
        lyr = ip["layerList"]
        full = ip["fullPalletList"] or []
        prtl = ip["partialPalletList"] or []
        sides = self._sides
        k_id = self._k_id
        L = self._L
        max_wt = _as_float(ip.get("max_wt"), L * 1000.0) or 1e6
        max_ht = _as_float(ip.get("containerHeight"), 9.0)

        # --- Pallet assignment variables ---
        # y_full[v, l, s, k, plt] = 1 if full pallet `plt` is in (container v,
        #   layer l, side s, lot-slot k)
        if full:
            keys_full = [
                (v, l, s, k, plt)
                for v in veh for l in lyr for s in sides for k in k_id for plt in full
            ]
            self._y_full = m.addVars(keys_full, vtype=GRB.BINARY, name="y_full")

        # y_prtl[v, l, s, k, plt] = 1 if partial pallet `plt` is in that slot
        if prtl:
            keys_prtl = [
                (v, l, s, k, plt)
                for v in veh for l in lyr for s in sides for k in k_id for plt in prtl
            ]
            self._y_prtl = m.addVars(keys_prtl, vtype=GRB.BINARY, name="y_prtl")

        # --- Lot-level aggregate variables (per container / side / slot) ---
        lot_keys = [(v, s, k) for v in veh for s in sides for k in k_id]

        # occ[v,s,k]    = 1 iff layer-1 of lot k on side s of container v is occupied
        self._occ = m.addVars(lot_keys, vtype=GRB.BINARY, name="occ")

        # occ2[v,s,k]   = 1 iff layer-2 of lot k is occupied
        self._occ2 = m.addVars(lot_keys, vtype=GRB.BINARY, name="occ2")

        # col_len[v,s,k] = physical length of lot k (= pallet length when occupied)
        self._col_len = m.addVars(lot_keys, lb=0.0, ub=L, name="col_len")

        # col_wt[v,s,k]  = total weight of the column (both layers)
        self._col_wt = m.addVars(lot_keys, lb=0.0, ub=max_wt, name="col_wt")

        # x_end[v,s,k]   = cumulative end position of lot k along the row
        self._x_end = m.addVars(lot_keys, lb=0.0, ub=L, name="x_end")

        # is_front[v,s,k] = 1 iff lot centre x < axle-split point
        self._is_front = m.addVars(lot_keys, vtype=GRB.BINARY, name="is_front")

        # is_door[v,s,k]  = 1 iff lot centre x is within the door zone
        self._is_door = m.addVars(lot_keys, vtype=GRB.BINARY, name="is_door")

        # w_front[v,s,k]  = col_wt when is_front=1, else 0  (axle weight extraction)
        self._w_front = m.addVars(lot_keys, lb=0.0, ub=max_wt, name="w_front")

        # stk_ht[v,s,k]   = total stacked height of the column
        self._stk_ht = m.addVars(lot_keys, lb=0.0, ub=max_ht, name="stk_ht")

        # --- Prior-load variables ---
        max_prior_full = ip.get("max_prior_full") or {}
        max_prior_prtl = ip.get("max_prior_prtl") or {}

        # y_prior_full[plt] tracks how many pre-committed full pallets are utilised
        if full:
            self._y_prior_full = m.addVars(
                full,
                lb=0.0,
                ub={plt: float(max_prior_full.get(plt, 0) or 0) for plt in full},
                name="y_prior_full",
            )
        if prtl:
            self._y_prior_prtl = m.addVars(
                prtl,
                lb=0.0,
                ub={plt: float(max_prior_prtl.get(plt, 0) or 0) for plt in prtl},
                name="y_prior_prtl",
            )

        # --- SKU / material tracking variables ---
        mtl_id = ip.get("materialList") or []
        if mtl_id:
            mtl_keys = [(v, mtl) for v in veh for mtl in mtl_id]
            # mtl_plt[v, mtl] = total pallets of material `mtl` on container v
            self._mtl_plt = m.addVars(mtl_keys, vtype=GRB.INTEGER, lb=0.0, name="mtl_plt")
            # y_mtl_plt[v, mtl] = 1 iff any pallet of material `mtl` is on container v
            self._y_mtl_plt = m.addVars(mtl_keys, vtype=GRB.BINARY, name="y_mtl_plt")
            # mtl_n[v] = number of distinct materials on container v
            self._mtl_n = m.addVars(veh, vtype=GRB.INTEGER, lb=0.0, name="mtl_n")

        # exceed_mtl_n = max(0, max_v(mtl_n[v]) - unique_sku_n)  (slack variable)
        self._exceed_mtl_n = m.addVar(vtype=GRB.INTEGER, lb=0.0, name="exceed_mtl_n")

        print(f"  Registered {m.NumVars} variables.")

    # ------------------------------------------------------------------
    # Constraint building
    # ------------------------------------------------------------------

    def _add_constraints(self) -> None:
        m = self.model
        ip = self.inputParams
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

        # --- Convenience local references ---
        y_full = self._y_full
        y_prtl = self._y_prtl
        occ = self._occ
        occ2 = self._occ2
        col_len = self._col_len
        col_wt = self._col_wt
        x_end = self._x_end
        is_front = self._is_front
        is_door = self._is_door
        w_front = self._w_front
        stk_ht = self._stk_ht

        # Helper: sum of all assignment variables in one (v, l, s, k) cell
        def cell_y_expr(v, l, s, k):
            return (
                gp.quicksum(y_full[v, l, s, k, plt] for plt in full if (v, l, s, k, plt) in y_full)
                + gp.quicksum(y_prtl[v, l, s, k, plt] for plt in prtl if (v, l, s, k, plt) in y_prtl)
            )

        # Helper: sum weighted by pallet length
        def cell_len_expr(v, l, s, k):
            return (
                gp.quicksum(lengths[plt] * y_full[v, l, s, k, plt]
                            for plt in full if (v, l, s, k, plt) in y_full)
                + gp.quicksum(lengths[plt] * y_prtl[v, l, s, k, plt]
                              for plt in prtl if (v, l, s, k, plt) in y_prtl)
            )

        # Helper: sum weighted by pallet weight
        def cell_wt_expr(v, l, s, k):
            return (
                gp.quicksum(_as_float(wt.get(plt), 0.0) * y_full[v, l, s, k, plt]
                            for plt in full if (v, l, s, k, plt) in y_full)
                + gp.quicksum(_as_float(wt.get(plt), 0.0) * y_prtl[v, l, s, k, plt]
                              for plt in prtl if (v, l, s, k, plt) in y_prtl)
            )

        # Helper: sum weighted by pallet height
        def cell_ht_expr(v, l, s, k):
            return (
                gp.quicksum(_as_float(ht.get(plt), 0.0) * y_full[v, l, s, k, plt]
                            for plt in full if (v, l, s, k, plt) in y_full)
                + gp.quicksum(_as_float(ht.get(plt), 0.0) * y_prtl[v, l, s, k, plt]
                              for plt in prtl if (v, l, s, k, plt) in y_prtl)
            )

        # ----------------------------------------------------------------
        # C1: Demand cap — each pallet placed at most demand[plt] times
        # ----------------------------------------------------------------
        for plt in full:
            m.addConstr(
                gp.quicksum(
                    y_full[v, l, s, k, plt]
                    for v in veh for l in lyr for s in sides for k in k_id
                    if (v, l, s, k, plt) in y_full
                ) <= float(full_demand.get(plt, 0) or 0),
                name=f"demand_full[{plt}]",
            )
        for plt in prtl:
            m.addConstr(
                gp.quicksum(
                    y_prtl[v, l, s, k, plt]
                    for v in veh for l in lyr for s in sides for k in k_id
                    if (v, l, s, k, plt) in y_prtl
                ) <= float(prtl_demand.get(plt, 0) or 0),
                name=f"demand_prtl[{plt}]",
            )

        for v in veh:
            for s in sides:
                for k in k_id:

                    # --------------------------------------------------------
                    # C2: At most one pallet per (layer, lot)
                    # --------------------------------------------------------
                    for l in lyr:
                        m.addConstr(
                            cell_y_expr(v, l, s, k) <= 1.0,
                            name=f"one_per_cell[{v},{l},{s},{k}]",
                        )

                    # --------------------------------------------------------
                    # C3: Occupancy = sum of layer-1 assignments
                    # --------------------------------------------------------
                    m.addConstr(
                        cell_y_expr(v, l1, s, k) == occ[v, s, k],
                        name=f"occ_def[{v},{s},{k}]",
                    )

                    # --------------------------------------------------------
                    # C4: Column length = layer-1 pallet length
                    # --------------------------------------------------------
                    m.addConstr(
                        cell_len_expr(v, l1, s, k) == col_len[v, s, k],
                        name=f"col_len_def[{v},{s},{k}]",
                    )

                    # --------------------------------------------------------
                    # C5: Column weight and height = sum over all layers
                    # --------------------------------------------------------
                    m.addConstr(
                        gp.quicksum(cell_wt_expr(v, l, s, k) for l in lyr) == col_wt[v, s, k],
                        name=f"col_wt_def[{v},{s},{k}]",
                    )
                    m.addConstr(
                        gp.quicksum(cell_ht_expr(v, l, s, k) for l in lyr) == stk_ht[v, s, k],
                        name=f"stk_ht_def[{v},{s},{k}]",
                    )

                    if l2 is not None:
                        # --------------------------------------------------------
                        # C6a: Layer-2 occupancy linked to layer-2 sum
                        # --------------------------------------------------------
                        m.addConstr(
                            cell_y_expr(v, l2, s, k) == occ2[v, s, k],
                            name=f"occ2_def[{v},{s},{k}]",
                        )

                        # C6b: Layer 2 requires layer 1 (occ2 <= occ)
                        m.addConstr(
                            occ2[v, s, k] <= occ[v, s, k],
                            name=f"lyr2_needs_lyr1[{v},{s},{k}]",
                        )

                        # C6c: Layer-2 length <= col_len  (upper bound)
                        m.addConstr(
                            cell_len_expr(v, l2, s, k) <= col_len[v, s, k],
                            name=f"len2_le_col[{v},{s},{k}]",
                        )

                        # C6d: Layer-2 length >= col_len - L*(1-occ2)  (lower bound when occupied)
                        m.addConstr(
                            cell_len_expr(v, l2, s, k) >= col_len[v, s, k] - L * (1 - occ2[v, s, k]),
                            name=f"len2_ge_col[{v},{s},{k}]",
                        )

                        # --------------------------------------------------------
                        # C7: No stacking on partial layer-1 pallet
                        # --------------------------------------------------------
                        for plt in prtl:
                            if (v, l1, s, k, plt) in y_prtl:
                                m.addConstr(
                                    y_prtl[v, l1, s, k, plt] + occ2[v, s, k] <= 1.0,
                                    name=f"no_stack_on_partial[{v},{s},{k},{plt}]",
                                )

                    # --------------------------------------------------------
                    # C19: Compact packing (optional) — fill from nose
                    # --------------------------------------------------------
                    if self.enforceCompactPacking and k > 1:
                        m.addConstr(
                            occ[v, s, k] <= occ[v, s, k - 1],
                            name=f"compact[{v},{s},{k}]",
                        )

                    # --------------------------------------------------------
                    # C9: Cumulative position x_end
                    # --------------------------------------------------------
                    if k == 1:
                        m.addConstr(
                            x_end[v, s, k] == col_len[v, s, k],
                            name=f"x_end_def[{v},{s},{k}]",
                        )
                    else:
                        m.addConstr(
                            x_end[v, s, k] == x_end[v, s, k - 1] + col_len[v, s, k],
                            name=f"x_end_def[{v},{s},{k}]",
                        )

                    # --------------------------------------------------------
                    # C10: Front / rear classification
                    # x_centre = x_end - 0.5 * col_len
                    # is_front=1  <=>  x_centre < split
                    # Big-M linearisation with M = L:
                    #   x_centre - split <= L*(1 - is_front)
                    #   split - x_centre <= L*is_front
                    # --------------------------------------------------------
                    m.addConstr(
                        x_end[v, s, k] - 0.5 * col_len[v, s, k] - split <= L * (1 - is_front[v, s, k]),
                        name=f"is_front_ub[{v},{s},{k}]",
                    )
                    m.addConstr(
                        split - (x_end[v, s, k] - 0.5 * col_len[v, s, k]) <= L * is_front[v, s, k],
                        name=f"is_front_lb[{v},{s},{k}]",
                    )

                    # --------------------------------------------------------
                    # C11: Door zone classification
                    # is_door=1  <=>  x_centre >= door_from
                    # Big-M:
                    #   x_centre - door_from >= -L*(1 - is_door)    [is_door=1 forces x_centre >= door_from]
                    #   x_centre - door_from <= L*is_door            [is_door=0 forces x_centre <= door_from]
                    # --------------------------------------------------------
                    m.addConstr(
                        x_end[v, s, k] - 0.5 * col_len[v, s, k] - door_from >= -L * (1 - is_door[v, s, k]),
                        name=f"is_door_lb[{v},{s},{k}]",
                    )
                    m.addConstr(
                        x_end[v, s, k] - 0.5 * col_len[v, s, k] - door_from <= L * is_door[v, s, k],
                        name=f"is_door_ub[{v},{s},{k}]",
                    )

                    # --------------------------------------------------------
                    # C12: Front-weight extraction via big-M product linearisation
                    # w_front = col_wt * is_front  (linearised):
                    #   w_front <= col_wt
                    #   w_front <= max_wt * is_front
                    #   w_front >= col_wt - max_wt*(1 - is_front)
                    # --------------------------------------------------------
                    m.addConstr(
                        w_front[v, s, k] <= col_wt[v, s, k],
                        name=f"wfront_le_wt[{v},{s},{k}]",
                    )
                    m.addConstr(
                        w_front[v, s, k] <= max_wt * is_front[v, s, k],
                        name=f"wfront_le_M_isf[{v},{s},{k}]",
                    )
                    m.addConstr(
                        w_front[v, s, k] >= col_wt[v, s, k] - max_wt * (1 - is_front[v, s, k]),
                        name=f"wfront_ge_wt_M[{v},{s},{k}]",
                    )

                    # --------------------------------------------------------
                    # C17: Height limit per lot
                    # stk_ht <= cap_h - (cap_h - door_cap)*is_door
                    # --------------------------------------------------------
                    cap_h = max_ht - allow_ht
                    door_cap = door_ht - allow_ht
                    m.addConstr(
                        stk_ht[v, s, k] + (cap_h - door_cap) * is_door[v, s, k] <= cap_h,
                        name=f"height_cap[{v},{s},{k}]",
                    )

                    # --------------------------------------------------------
                    # C18: Crushing prevention — layer-1 weight >= layer-2 weight
                    # --------------------------------------------------------
                    if l2 is not None:
                        m.addConstr(
                            cell_wt_expr(v, l1, s, k) >= cell_wt_expr(v, l2, s, k),
                            name=f"no_crush[{v},{s},{k}]",
                        )

                # --------------------------------------------------------
                # C8: Row length capacity
                # --------------------------------------------------------
                m.addConstr(
                    gp.quicksum(col_len[v, s, k] for k in k_id) <= L,
                    name=f"row_length[{v},{s}]",
                )

            # ----------------------------------------------------------------
            # C13: Total vehicle weight cap
            # ----------------------------------------------------------------
            if max_wt > 0:
                m.addConstr(
                    gp.quicksum(col_wt[v, s, k] for s in sides for k in k_id) <= max_wt,
                    name=f"truck_wt[{v}]",
                )

            # ----------------------------------------------------------------
            # C14: Axle weight limits (optional)
            # ----------------------------------------------------------------
            if self.enforceAxleWeightConstraints:
                m.addConstr(
                    gp.quicksum(w_front[v, s, k] for s in sides for k in k_id) <= self._max_front_wt,
                    name=f"axle_front[{v}]",
                )
                m.addConstr(
                    gp.quicksum(col_wt[v, s, k] - w_front[v, s, k] for s in sides for k in k_id)
                    <= self._max_rear_wt,
                    name=f"axle_rear[{v}]",
                )

            # ----------------------------------------------------------------
            # C15: Side-to-side weight balance (optional)
            # ----------------------------------------------------------------
            if self.enforceWeightBalancing and ss_tol > 0:
                lw = gp.quicksum(col_wt[v, "left", k] for k in k_id)
                rw = gp.quicksum(col_wt[v, "right", k] for k in k_id)
                m.addConstr(lw <= (1 + ss_tol) * rw, name=f"ss_bal_ub[{v}]")
                m.addConstr(lw >= (1 - ss_tol) * rw, name=f"ss_bal_lb[{v}]")

            # ----------------------------------------------------------------
            # C16: Front-to-back weight balance (optional)
            # ----------------------------------------------------------------
            if self.enforceWeightBalancing and fb_tol > 0:
                fw = gp.quicksum(w_front[v, s, k] for s in sides for k in k_id)
                tw = gp.quicksum(col_wt[v, s, k] for s in sides for k in k_id)
                rw_expr = tw - fw
                m.addConstr(fw <= (1 + fb_tol) * rw_expr, name=f"fb_bal_ub[{v}]")
                m.addConstr(fw >= (1 - fb_tol) * rw_expr, name=f"fb_bal_lb[{v}]")

        # ----------------------------------------------------------------
        # C20: SKU / material tracking
        # ----------------------------------------------------------------
        if mtl_id:
            for v in veh:
                for mtl in mtl_id:
                    # mtl_plt[v,mtl] = total pallets of that material on v
                    placed = (
                        gp.quicksum(
                            y_full[v, l, s, k, plt]
                            for plt in full if plt_mtl.get(plt) == mtl
                            for l in lyr for s in sides for k in k_id
                            if (v, l, s, k, plt) in y_full
                        )
                        + gp.quicksum(
                            y_prtl[v, l, s, k, plt]
                            for plt in prtl if plt_mtl.get(plt) == mtl
                            for l in lyr for s in sides for k in k_id
                            if (v, l, s, k, plt) in y_prtl
                        )
                    )
                    m.addConstr(
                        self._mtl_plt[v, mtl] == placed,
                        name=f"mtl_plt_def[{v},{mtl}]",
                    )
                    # y_mtl_plt[v,mtl] = 1 iff mtl_plt > 0  (big-M indicator)
                    m.addConstr(
                        self._mtl_plt[v, mtl] <= sku_scale * self._y_mtl_plt[v, mtl],
                        name=f"y_mtl_plt_def[{v},{mtl}]",
                    )

                # mtl_n[v] = number of active materials
                m.addConstr(
                    self._mtl_n[v] == gp.quicksum(self._y_mtl_plt[v, mtl] for mtl in mtl_id),
                    name=f"mtl_n_def[{v}]",
                )

            # exceed_mtl_n >= mtl_n[v] - unique_sku  for all v
            for v in veh:
                m.addConstr(
                    self._exceed_mtl_n >= self._mtl_n[v] - unique_sku,
                    name=f"exceed_mtl[{v}]",
                )

        # ----------------------------------------------------------------
        # C21: Prior-load accounting
        # ----------------------------------------------------------------
        for plt in full:
            if plt in self._y_prior_full:
                total_placed = gp.quicksum(
                    y_full[v, l, s, k, plt]
                    for v in veh for l in lyr for s in sides for k in k_id
                    if (v, l, s, k, plt) in y_full
                )
                m.addConstr(
                    self._y_prior_full[plt] <= total_placed,
                    name=f"prior_full[{plt}]",
                )
        for plt in prtl:
            if plt in self._y_prior_prtl:
                total_placed = gp.quicksum(
                    y_prtl[v, l, s, k, plt]
                    for v in veh for l in lyr for s in sides for k in k_id
                    if (v, l, s, k, plt) in y_prtl
                )
                m.addConstr(
                    self._y_prior_prtl[plt] <= total_placed,
                    name=f"prior_prtl[{plt}]",
                )

        # ----------------------------------------------------------------
        # C22: Vendor stacking rules (optional)
        # ----------------------------------------------------------------
        if self.vdr_stacking_rules and l2 is not None:
            g2 = ip.get("stackFactorg2Pallet") or {}
            g3 = ip.get("stackFactorg3Pallet") or {}
            g4 = ip.get("stackFactorg4Pallet") or ip.get("stackFactorq4Pallet") or {}
            for v in veh:
                for s in sides:
                    for k in k_id:
                        for plt in full:
                            # g2 or g4: forbidden from layer 2
                            if g2.get(plt, 0) == 1 or g4.get(plt, 0) == 1:
                                if (v, l2, s, k, plt) in y_full:
                                    m.addConstr(
                                        y_full[v, l2, s, k, plt] == 0,
                                        name=f"vdr_g2_full[{v},{s},{k},{plt}]",
                                    )
                            # g3 or g4: cannot be on layer 1 when layer 2 is occupied
                            if g3.get(plt, 0) == 1 or g4.get(plt, 0) == 1:
                                if (v, l1, s, k, plt) in y_full:
                                    m.addConstr(
                                        y_full[v, l1, s, k, plt] + occ2[v, s, k] <= 1.0,
                                        name=f"vdr_g3_full[{v},{s},{k},{plt}]",
                                    )
                        for plt in prtl:
                            if g2.get(plt, 0) == 1 or g4.get(plt, 0) == 1:
                                if (v, l2, s, k, plt) in y_prtl:
                                    m.addConstr(
                                        y_prtl[v, l2, s, k, plt] == 0,
                                        name=f"vdr_g2_prtl[{v},{s},{k},{plt}]",
                                    )
                            if g3.get(plt, 0) == 1 or g4.get(plt, 0) == 1:
                                if (v, l1, s, k, plt) in y_prtl:
                                    m.addConstr(
                                        y_prtl[v, l1, s, k, plt] + occ2[v, s, k] <= 1.0,
                                        name=f"vdr_g3_prtl[{v},{s},{k},{plt}]",
                                    )

        print(f"  Constraints: {m.NumConstrs}")

    # ------------------------------------------------------------------
    # Objective
    # ------------------------------------------------------------------

    def _set_objective(self) -> None:
        m = self.model
        ip = self.inputParams
        veh = ip["containerList"]
        lyr = ip["layerList"]
        full = ip["fullPalletList"] or []
        prtl = ip["partialPalletList"] or []
        plt_mtl = ip.get("palletMaterialDict") or {}
        incentive = float(ip.get("priorityIncentive") or 1.0)
        sku_penalty = float(ip.get("sku_penalty") or 0.0)
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

        obj_expr = gp.LinExpr()

        # Placement rewards
        for c in veh:
            cp = self._container_priority(c)
            for l in lyr:
                for s in self._sides:
                    for k in self._k_id:
                        for plt in full:
                            if (c, l, s, k, plt) in self._y_full:
                                base = cp * incentive if _is_high(plt) else cp
                                obj_expr.add(self._y_full[c, l, s, k, plt], base)
                        for plt in prtl:
                            if (c, l, s, k, plt) in self._y_prtl:
                                base = cp * incentive if _is_high(plt) else cp
                                obj_expr.add(self._y_prtl[c, l, s, k, plt], base * _frac(plt))

        # Prior-load rewards
        for plt in full:
            if plt in self._y_prior_full:
                obj_expr.add(self._y_prior_full[plt], incentive)
        for plt in prtl:
            if plt in self._y_prior_prtl:
                obj_expr.add(self._y_prior_prtl[plt], incentive)

        # SKU diversity penalty
        if self._exceed_mtl_n is not None:
            obj_expr.add(self._exceed_mtl_n, -(sku_penalty * self.sku_penalty_factor))

        # Maximise fill (Gurobi maximises)
        m.setObjective(obj_expr, GRB.MAXIMIZE)
        print("  Objective: maximise weighted fill (lengths from palletLengthDict).")

    # ------------------------------------------------------------------
    # Loading instruction helper
    # ------------------------------------------------------------------

    def _make_instruction(self, v, l, s, k, plt, is_partial: bool) -> dict:
        """Build a placement record for a single assigned pallet."""
        ip = self.inputParams
        raw_h = _as_float((ip.get("palletHeightDict") or {}).get(plt, 0), 0.0)
        plt_len = self._lengths[plt]
        cube = (
            _as_float((ip.get("palletWidthDict") or {}).get(plt, 40.0), 40.0) / 12.0
            * plt_len / (12.0 if plt_len > 5 else 1.0)
            * (raw_h / 12.0 if raw_h > 5 else raw_h)
        )
        n_cases = int((ip.get("palletNumCasesDict") or {}).get(plt, 0))
        frac = _as_float((ip.get("partialFractionValueForPartialPallet") or {}).get(plt, 1.0), 1.0)
        return {
            "marketID": ip.get("marketID", ""),
            "palletID": plt,
            "container": v,
            "layer": l,
            "position": k,
            "sequence": k,
            "material": (ip.get("palletMaterialDict") or {}).get(plt, ""),
            "batch": (ip.get("palletBatchDict") or {}).get(plt, ""),
            "bin_loc": s,
            "location": s,
            "weight": (ip.get("palletWeightDict") or {}).get(plt, 0),
            "height": raw_h,
            "cube": cube,
            "cases": n_cases * (frac if is_partial and frac > 0 else 1.0),
            "total_cases": n_cases,
            "is_partial": is_partial,
            "pallet_type": "Partial" if is_partial else "Full",
            "stacking_group": (ip.get("palletStackingGroupDict") or {}).get(plt, "G1"),
            "shipTo": (ip.get("palletPlantDict") or {}).get(plt, ""),
            "description": s,
        }
