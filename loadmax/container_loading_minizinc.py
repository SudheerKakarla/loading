"""Truck loading via MiniZinc (does not replace HiGHS).

Calls the local MiniZinc 2.10 driver with the bundled HiGHS MIP backend.
Slot model matches container_loading_highs.py: one pallet per
(container, layer, position).
"""

from __future__ import annotations

import csv
import json
import math
import os
import shutil
import subprocess
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from loadmax.models import Load_Obj as LoadObj

MODEL_PATH = Path(__file__).resolve().parent / "minizinc" / "truck_loading.mzn"

DEFAULT_MINIZINC_CANDIDATES = (
    Path(os.environ["MINIZINC"]) if os.environ.get("MINIZINC") else None,
    Path(r"C:\Users\sudhe\AppData\Local\Programs\MiniZinc\minizinc.exe"),
    Path(r"C:\Program Files\MiniZinc\minizinc.exe"),
    Path(r"C:\Program Files (x86)\MiniZinc\minizinc.exe"),
)


def find_minizinc() -> Path:
    """Resolve minizinc.exe: $MINIZINC, PATH, then the usual Windows install."""
    for cand in DEFAULT_MINIZINC_CANDIDATES:
        if cand is not None and cand.is_file():
            return cand
    which = shutil.which("minizinc")
    if which:
        return Path(which)
    raise FileNotFoundError(
        "MiniZinc not found. Install it or set MINIZINC to minizinc.exe "
        r"(expected C:\Users\sudhe\AppData\Local\Programs\MiniZinc\minizinc.exe)."
    )


def _positive_limit(val: Any) -> Optional[float]:
    if val is None or val == "" or val is False:
        return None
    if isinstance(val, (list, tuple, dict, set)):
        return None
    try:
        x = float(val)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(x) or x <= 0:
        return None
    return x


def _as_float(val: Any, default: float = 0.0) -> float:
    try:
        x = float(val)
    except (TypeError, ValueError):
        return default
    return x if math.isfinite(x) else default


def _as_int(val: Any, default: int = 0) -> int:
    try:
        x = int(float(val))
    except (TypeError, ValueError):
        return default
    return x


def _yes(val: Any) -> bool:
    return str(val).strip().lower() in {"yes", "true", "1"}


def _container_priority(ip: dict, container_id: Any) -> float:
    raw = ip.get("container_priority") or {}
    if container_id in raw:
        return float(raw[container_id])
    if isinstance(container_id, (int, float)):
        if int(container_id) in raw:
            return float(raw[int(container_id)])
        if float(container_id) in raw:
            return float(raw[float(container_id)])
    return 1.0


def _is_high_priority_sku(ip: dict, plt: Any) -> bool:
    sku_map = ip.get("skuPriorityIncentive") or {}
    plt_mtl = ip.get("palletMaterialDict") or {}
    if plt in sku_map:
        return True
    mtl = plt_mtl.get(plt)
    if mtl in sku_map or str(mtl) in sku_map:
        return True
    try:
        return int(mtl) in sku_map
    except (TypeError, ValueError):
        return False


def _frac(ip: dict, plt: Any) -> float:
    raw = (ip.get("partialFractionValueForPartialPallet") or {}).get(plt, 1.0)
    f = _as_float(raw, 1.0)
    if f <= 0:
        return 1.0
    return f


def _bal_coeff(bal: dict, position: Any, keys: Tuple[str, ...]) -> float:
    total = 0.0
    for key in keys:
        val = bal.get((position, key), 0.0)
        total += _as_float(val, 0.0)
    return total


def _dzn_bool(val: bool) -> str:
    return "true" if val else "false"


def _dzn_float(val: float) -> str:
    x = float(val)
    if not math.isfinite(x):
        raise ValueError(f"non-finite MiniZinc float: {val!r}")
    return repr(x)


def _dzn_int(val: int) -> str:
    return str(int(val))


def _dzn_set_int(vals: Iterable[Any]) -> str:
    nums = sorted({_as_int(v) for v in vals})
    if not nums:
        return "{}"
    return "{" + ", ".join(str(n) for n in nums) + "}"


def _dzn_list(values: Sequence[str]) -> str:
    return "[" + ", ".join(values) + "]"


def _require_number(table: dict, plt: Any, name: str) -> float:
    if plt not in table:
        raise ValueError(f"pallet {plt} missing {name}")
    val = table[plt]
    if val is None or val == "":
        raise ValueError(f"pallet {plt} missing {name}")
    try:
        x = float(val)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"pallet {plt} {name}={val!r} is not a number") from exc
    if not math.isfinite(x):
        raise ValueError(f"pallet {plt} {name}={val!r} is not finite")
    return x


def _require_material(table: dict, plt: Any) -> Any:
    if plt not in table or table[plt] is None or table[plt] == "":
        raise ValueError(f"pallet {plt} missing palletMaterialDict entry")
    return table[plt]


def _same_id(a: Any, b: Any) -> bool:
    if a == b:
        return True
    try:
        return float(a) == float(b)
    except (TypeError, ValueError):
        return str(a).strip() == str(b).strip()


def _canon_member(value: Any, known: Sequence[Any]) -> Any:
    for item in known:
        if _same_id(item, value):
            return item
    return value


def _close(a: Any, b: Any, tol: float = 1e-4) -> bool:
    try:
        return abs(float(a) - float(b)) <= tol
    except (TypeError, ValueError):
        return False


def _is_partial_cell(val: Any) -> bool:
    return str(val).strip().lower() in {"true", "1", "yes"}


def match_instruction_pallet(
    ip: dict,
    row: dict,
    remaining: Dict[Any, int],
) -> Tuple[Any, bool]:
    """Map a loading-instruction row to a pallet id.

    Prefer PalletID when present. Otherwise greedy match on
    material / batch / weight / height / partial, using remaining demand.
    """
    is_partial = _is_partial_cell(row.get("Is_Partial"))
    pool = list(ip.get("partialPalletList") or [] if is_partial else ip.get("fullPalletList") or [])
    explicit = str(row.get("PalletID") or row.get("palletID") or "").strip()
    if explicit:
        plt = _canon_member(explicit, pool)
        if plt not in pool:
            raise ValueError(f"PalletID {explicit!r} is not in the {'partial' if is_partial else 'full'} list")
        if remaining.get(plt, 0) <= 0:
            raise ValueError(f"PalletID {explicit!r} exceeds demand in warm-start CSV")
        remaining[plt] -= 1
        return plt, is_partial

    mtl_dict = ip.get("palletMaterialDict") or {}
    batch_dict = ip.get("palletBatchDict") or {}
    wt_dict = ip.get("palletWeightDict") or {}
    ht_dict = ip.get("palletHeightDict") or {}
    row_mtl = row.get("Material")
    row_batch = str(row.get("Batch") or "").strip()
    candidates = []
    for plt in pool:
        if remaining.get(plt, 0) <= 0:
            continue
        if not _same_id(mtl_dict.get(plt), row_mtl):
            continue
        plt_batch = str(batch_dict.get(plt) or "").strip()
        if row_batch and plt_batch and row_batch != plt_batch:
            continue
        if row.get("Weight") not in (None, "") and not _close(wt_dict.get(plt), row["Weight"]):
            continue
        if row.get("Height") not in (None, "") and not _close(ht_dict.get(plt), row["Height"]):
            continue
        candidates.append(plt)
    if not candidates:
        raise ValueError(
            f"no pallet match for container={row.get('Container')} "
            f"pos={row.get('Position')} layer={row.get('Layer')} "
            f"material={row_mtl} partial={is_partial}"
        )
    plt = candidates[0]
    remaining[plt] -= 1
    return plt, is_partial


def slots_from_instruction_csv(path: Path, ip: dict) -> List[Tuple[Any, Any, Any, Any, bool]]:
    """Read a HiGHS/MiniZinc loading-instructions CSV into slot tuples."""
    full = list(ip.get("fullPalletList") or [])
    prtl = list(ip.get("partialPalletList") or [])
    demand_full = ip.get("fullPalletDemandByMaterialBatch") or {}
    demand_prtl = ip.get("partialPalletDemandByMaterialBatch") or {}
    remaining = {plt: max(0, _as_int(demand_full.get(plt), 0)) for plt in full}
    remaining.update({plt: max(0, _as_int(demand_prtl.get(plt), 0)) for plt in prtl})
    veh = list(ip["containerList"])
    lyr = list(ip["layerList"])
    pos = list(ip["positionList"])
    assigned: List[Tuple[Any, Any, Any, Any, bool]] = []
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            c = _canon_member(row["Container"], veh)
            l = _canon_member(row["Layer"], lyr)
            p = _canon_member(row["Position"], pos)
            plt, is_partial = match_instruction_pallet(ip, row, remaining)
            assigned.append((c, l, p, plt, is_partial))
    return assigned


INSTRUCTION_COLUMNS = [
    "Container",
    "Sequence",
    "Position",
    "Layer",
    "PalletID",
    "Material",
    "Batch",
    "Cases",
    "Weight",
    "Height",
    "Is_Partial",
    "Stacking_Group",
    "Warehouse_Location",
    "Destination",
]


def backfill_pallet_id_column(path: Path, ip: dict) -> int:
    """Write PalletID (plt_*) onto an existing loading-instructions CSV.

    Uses the solver pallet id from the dump, not a dummy. Rows that already
    have a valid PalletID are left as-is (demand is still counted).
    """
    assigned = slots_from_instruction_csv(path, ip)
    with path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if len(rows) != len(assigned):
        raise ValueError(
            f"{path} has {len(rows)} rows but matched {len(assigned)} pallet ids"
        )
    out_rows = []
    for row, (_c, _l, _p, plt, _partial) in zip(rows, assigned):
        row = dict(row)
        row["PalletID"] = plt
        out_rows.append({col: row.get(col, "") for col in INSTRUCTION_COLUMNS})
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=INSTRUCTION_COLUMNS)
        writer.writeheader()
        writer.writerows(out_rows)
    return len(out_rows)


def reconstruct_objective(ip: dict, assigned: Sequence[Tuple[Any, Any, Any, Any, bool]]) -> float:
    """Pyomo 4-term fill from an incumbent: fill + incentive * y_prior - SKU penalty."""
    incentive = _as_float(ip.get("priorityIncentive"), 1.0) or 1.0
    sku_penalty = _as_float(ip.get("sku_penalty"), 0.0)
    sku_factor = _as_float(ip.get("sku_penalty_factor"), 0.0)
    max_prior_full = ip.get("max_prior_full") or {}
    max_prior_prtl = ip.get("max_prior_prtl") or {}
    plt_mtl = ip.get("palletMaterialDict") or {}
    unique_sku = _as_int(ip.get("unique_sku_n"), 0)

    fill = 0.0
    counts: Dict[Tuple[str, Any], int] = defaultdict(int)
    skus: Dict[Any, set] = defaultdict(set)
    for c, _l, _p, plt, is_partial in assigned:
        cp = _container_priority(ip, c)
        base = cp * incentive if _is_high_priority_sku(ip, plt) else cp
        fill += base * (_frac(ip, plt) if is_partial else 1.0)
        kind = "prtl" if is_partial else "full"
        counts[(kind, plt)] += 1
        mtl = plt_mtl.get(plt)
        if mtl is not None and mtl != "":
            skus[c].add(mtl)

    prior = 0.0
    for (kind, plt), n in counts.items():
        cap = max_prior_prtl if kind == "prtl" else max_prior_full
        prior += min(float(n), _as_float(cap.get(plt), 0.0))

    exceed = max(0, sum(len(s) for s in skus.values()) - unique_sku)
    return fill + incentive * prior - sku_penalty * sku_factor * exceed


def _mip_gap(objective: Optional[float], bound: Optional[float], *, maximize: bool = True) -> Optional[float]:
    if objective is None or bound is None:
        return None
    denom = max(abs(objective), 1e-12)
    if maximize:
        return max(0.0, (bound - objective) / denom)
    return max(0.0, (objective - bound) / denom)


def _looks_number(val: Any) -> bool:
    try:
        return math.isfinite(float(val))
    except (TypeError, ValueError):
        return False


def write_dzn(
    ip: dict,
    path: Path,
    *,
    run_ocean: bool = False,
    vdr_stacking_rules: bool = False,
    sku_penalty_factor: float = 1.0,
    warm_slots: Optional[Sequence[Tuple[Any, Any, Any, Any, bool]]] = None,
) -> Dict[str, Any]:
    """Write MiniZinc data. Returns index maps used to decode the solution."""
    veh = list(ip["containerList"])
    lyr = list(ip["layerList"])
    pos = list(ip["positionList"])
    full = list(ip["fullPalletList"] or [])
    prtl = list(ip["partialPalletList"] or [])
    materials = list(ip.get("materialList") or [])
    for plt in full + prtl:
        m = (ip.get("palletMaterialDict") or {}).get(plt)
        if m is not None and m not in materials:
            materials.append(m)

    veh_ix = {v: i + 1 for i, v in enumerate(veh)}
    lyr_ix = {v: i + 1 for i, v in enumerate(lyr)}
    pos_ix = {v: i + 1 for i, v in enumerate(pos)}
    full_ix = {v: i + 1 for i, v in enumerate(full)}
    prtl_ix = {v: i + 1 for i, v in enumerate(prtl)}
    mtl_ix = {v: i + 1 for i, v in enumerate(materials)}

    n_veh, n_lyr, n_pos = len(veh), len(lyr), len(pos)
    n_full, n_prtl, n_mtl = len(full), len(prtl), len(materials)
    if n_veh == 0 or n_lyr == 0 or n_pos == 0:
        raise ValueError("containerList, layerList, and positionList must be non-empty")

    wt = ip.get("palletWeightDict") or {}
    ht = ip.get("palletHeightDict") or {}
    plt_mtl = ip.get("palletMaterialDict") or {}
    demand_full = ip.get("fullPalletDemandByMaterialBatch") or {}
    demand_prtl = ip.get("partialPalletDemandByMaterialBatch") or {}
    max_prior_full = ip.get("max_prior_full") or {}
    max_prior_prtl = ip.get("max_prior_prtl") or {}
    g2 = ip.get("stackFactorg2Pallet") or {}
    g3 = ip.get("stackFactorg3Pallet") or {}
    g4 = ip.get("stackFactorg4Pallet") or ip.get("stackFactorq4Pallet") or {}
    bal = ip.get("positionGroupBalanceCoefficientDict") or {}
    front_id = set(ip.get("frontPositionList") or [])
    rear_id = set(ip.get("rearPositionList") or [])
    left_id = set(ip.get("leftPositionList") or [])
    right_id = set(ip.get("rightPositionList") or [])
    door = ip.get("door_posn") or []
    incentive = _as_float(ip.get("priorityIncentive"), 1.0) or 1.0
    sku_penalty = _as_float(ip.get("sku_penalty"), 0.0)
    max_front = _positive_limit(ip.get("max_front_wt"))
    max_rear = _positive_limit(ip.get("max_rear_wt"))
    fb_tol = _as_float(ip.get("front_to_back_tolerance"), 0.0)
    ss_tol = _as_float(ip.get("side_to_side_tolerance"), 0.0)
    max_plt_n_raw = _as_int(ip.get("max_plt_n"), n_pos)
    max_lyr_n = _as_int(ip.get("max_lyr_n"), n_lyr)
    first_raw = _as_int(ip.get("first_plt_n"), 0)
    # Model POS is 1..nPos in positionList order; map layout ids into that index.
    first_plt_n = pos_ix.get(first_raw, first_raw)
    max_plt_n = pos_ix.get(max_plt_n_raw, n_pos)
    unique_sku = _as_int(ip.get("unique_sku_n"), n_mtl)
    sku_scale = max(1, max_lyr_n * max_plt_n)

    use_axles = (not run_ocean) and max_front is not None and max_rear is not None
    use_compact = (not run_ocean) and _yes(ip.get("enforce_compact_packing", "No"))
    use_balance_fb = (not run_ocean) and fb_tol > 0
    use_balance_ss = (not run_ocean) and ss_tol > 0
    use_vdr = bool(vdr_stacking_rules)

    def _mtl_of(plt: Any) -> int:
        m = _require_material(plt_mtl, plt)
        ix = mtl_ix.get(m)
        if not ix:
            raise ValueError(f"pallet {plt} material {m!r} is not in materialList")
        return int(ix)

    def _flag(table: dict, plt: Any) -> int:
        return 1 if table.get(plt, 0) == 1 else 0

    def _demand(table: dict, plt: Any) -> int:
        x = _require_number(table, plt, "demand")
        if x < 0:
            raise ValueError(f"pallet {plt} demand={x} is negative")
        return int(x)

    weight_full = [_require_number(wt, p, "weight") for p in full]
    weight_prtl = [_require_number(wt, p, "weight") for p in prtl]
    height_full = [_require_number(ht, p, "height") for p in full]
    height_prtl = [_require_number(ht, p, "height") for p in prtl]
    mtl_full = [_mtl_of(p) for p in full]
    mtl_prtl = [_mtl_of(p) for p in prtl]
    dem_full = [_demand(demand_full, p) for p in full]
    dem_prtl = [_demand(demand_prtl, p) for p in prtl]
    prior_full = [_as_float(max_prior_full.get(p), 0.0) for p in full]
    prior_prtl = [_as_float(max_prior_prtl.get(p), 0.0) for p in prtl]

    fill_full: List[str] = []
    fill_prtl: List[str] = []
    for v in veh:
        cp = _container_priority(ip, v)
        for p in full:
            base = cp * incentive if _is_high_priority_sku(ip, p) else cp
            fill_full.append(_dzn_float(base))
        for p in prtl:
            base = cp * incentive if _is_high_priority_sku(ip, p) else cp
            fill_prtl.append(_dzn_float(base * _frac(ip, p)))

    front_coeff = [_bal_coeff(bal, p, ("A", "C")) if p in front_id else 0.0 for p in pos]
    rear_coeff = [_bal_coeff(bal, p, ("B", "D")) if p in rear_id else 0.0 for p in pos]
    door_model = {pos_ix[p] for p in door if p in pos_ix}

    lines = [
        f"nVeh = {n_veh};",
        f"nLyr = {n_lyr};",
        f"nPos = {n_pos};",
        f"nFull = {n_full};",
        f"nPrtl = {n_prtl};",
        f"nMtl = {max(n_mtl, 0)};",
        f"lyr_ord = {_dzn_list([_dzn_int(i + 1) for i in range(n_lyr)])};",
        f"first_plt_n = {first_plt_n};",
        f"max_plt_n = {max_plt_n};",
        f"max_lyr_n = {max_lyr_n};",
        f"unique_sku_n = {unique_sku};",
        f"sku_scale = {sku_scale};",
        f"max_wt = {_dzn_float(_as_float(ip.get('max_wt'), 0.0))};",
        f"max_front_wt = {_dzn_float(max_front or 0.0)};",
        f"max_rear_wt = {_dzn_float(max_rear or 0.0)};",
        f"max_ht = {_dzn_float(_as_float(ip.get('containerHeight'), 0.0))};",
        f"door_ht = {_dzn_float(_as_float(ip.get('containerHeightAtDoor'), 0.0))};",
        f"allow_ht = {_dzn_float(_as_float(ip.get('load_allow_ht'), 0.0))};",
        f"fb_tol = {_dzn_float(fb_tol)};",
        f"ss_tol = {_dzn_float(ss_tol)};",
        f"incentive = {_dzn_float(incentive)};",
        f"sku_pen_coeff = {_dzn_float(sku_penalty * float(sku_penalty_factor))};",
        f"use_axles = {_dzn_bool(use_axles)};",
        f"use_compact = {_dzn_bool(use_compact)};",
        f"use_balance_fb = {_dzn_bool(use_balance_fb)};",
        f"use_balance_ss = {_dzn_bool(use_balance_ss)};",
        f"use_crushing = {_dzn_bool(True)};",
        f"use_vdr = {_dzn_bool(use_vdr)};",
        f"door_posn = {_dzn_set_int(door_model)};",
        f"front_coeff = {_dzn_list(_dzn_float(x) for x in front_coeff)};",
        f"rear_coeff = {_dzn_list(_dzn_float(x) for x in rear_coeff)};",
        f"is_front = {_dzn_list('1' if p in front_id else '0' for p in pos)};",
        f"is_rear = {_dzn_list('1' if p in rear_id else '0' for p in pos)};",
        f"is_left = {_dzn_list('1' if p in left_id else '0' for p in pos)};",
        f"is_right = {_dzn_list('1' if p in right_id else '0' for p in pos)};",
        f"weight_full = {_dzn_list(_dzn_float(x) for x in weight_full)};",
        f"weight_prtl = {_dzn_list(_dzn_float(x) for x in weight_prtl)};",
        f"height_full = {_dzn_list(_dzn_float(x) for x in height_full)};",
        f"height_prtl = {_dzn_list(_dzn_float(x) for x in height_prtl)};",
        f"mtl_of_full = {_dzn_list(_dzn_int(x) for x in mtl_full)};",
        f"mtl_of_prtl = {_dzn_list(_dzn_int(x) for x in mtl_prtl)};",
        f"demand_full = {_dzn_list(_dzn_int(x) for x in dem_full)};",
        f"demand_prtl = {_dzn_list(_dzn_int(x) for x in dem_prtl)};",
        f"max_prior_full = {_dzn_list(_dzn_float(x) for x in prior_full)};",
        f"max_prior_prtl = {_dzn_list(_dzn_float(x) for x in prior_prtl)};",
        f"fill_full = array2d(VEH, FULL, {_dzn_list(fill_full)});",
        f"fill_prtl = array2d(VEH, PRTL, {_dzn_list(fill_prtl)});",
        f"g2_full = {_dzn_list([_dzn_int(_flag(g2, p)) for p in full])};",
        f"g2_prtl = {_dzn_list([_dzn_int(_flag(g2, p)) for p in prtl])};",
        f"g3_full = {_dzn_list([_dzn_int(_flag(g3, p)) for p in full])};",
        f"g3_prtl = {_dzn_list([_dzn_int(_flag(g3, p)) for p in prtl])};",
        f"g4_full = {_dzn_list([_dzn_int(_flag(g4, p)) for p in full])};",
        f"g4_prtl = {_dzn_list([_dzn_int(_flag(g4, p)) for p in prtl])};",
    ]

    n_warm = 0
    cell = n_veh * n_lyr * n_pos
    warm_full = [0] * (cell * n_full)
    warm_prtl = [0] * (cell * n_prtl)
    warm_slot_full = [0] * cell
    warm_slot_prtl = [0] * cell
    warm_occ = [0] * cell
    warm_wt = [0.0] * cell
    warm_ht = [0.0] * cell

    def _cell_index(vi: int, li: int, pi: int) -> int:
        return ((vi - 1) * n_lyr * n_pos + (li - 1) * n_pos + (pi - 1))

    if warm_slots:
        for c, l, p, plt, is_partial in warm_slots:
            vi = veh_ix.get(c)
            li = lyr_ix.get(l)
            pi = pos_ix.get(p)
            if vi is None or li is None or pi is None:
                raise ValueError(f"warm-start slot ({c},{l},{p}) is not in the model index")
            ci = _cell_index(vi, li, pi)
            if is_partial:
                fi = prtl_ix.get(plt)
                if fi is None:
                    raise ValueError(f"warm-start partial pallet {plt} is not in partialPalletList")
                idx = ci * n_prtl + (fi - 1)
                warm_prtl[idx] = 1
                warm_slot_prtl[ci] = fi
                warm_wt[ci] = weight_prtl[fi - 1]
                warm_ht[ci] = height_prtl[fi - 1]
            else:
                fi = full_ix.get(plt)
                if fi is None:
                    raise ValueError(f"warm-start full pallet {plt} is not in fullPalletList")
                idx = ci * n_full + (fi - 1)
                warm_full[idx] = 1
                warm_slot_full[ci] = fi
                warm_wt[ci] = weight_full[fi - 1]
                warm_ht[ci] = height_full[fi - 1]
            warm_occ[ci] = 1
            n_warm += 1
    lines.append(f"use_warm_start = {_dzn_bool(bool(warm_slots))};")
    lines.append(
        f"warm_y_full = array4d(VEH, LYR, POS, FULL, {_dzn_list(_dzn_int(x) for x in warm_full)});"
    )
    lines.append(
        f"warm_y_prtl = array4d(VEH, LYR, POS, PRTL, {_dzn_list(_dzn_int(x) for x in warm_prtl)});"
    )
    lines.append(
        f"warm_slot_full = array3d(VEH, LYR, POS, {_dzn_list(_dzn_int(x) for x in warm_slot_full)});"
    )
    lines.append(
        f"warm_slot_prtl = array3d(VEH, LYR, POS, {_dzn_list(_dzn_int(x) for x in warm_slot_prtl)});"
    )
    lines.append(
        f"warm_occ = array3d(VEH, LYR, POS, {_dzn_list(_dzn_int(x) for x in warm_occ)});"
    )
    lines.append(
        f"warm_wt = array3d(VEH, LYR, POS, {_dzn_list(_dzn_float(x) for x in warm_wt)});"
    )
    lines.append(
        f"warm_ht = array3d(VEH, LYR, POS, {_dzn_list(_dzn_float(x) for x in warm_ht)});"
    )

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {
        "veh": veh,
        "lyr": lyr,
        "pos": pos,
        "full": full,
        "prtl": prtl,
        "veh_ix": veh_ix,
        "lyr_ix": lyr_ix,
        "pos_ix": pos_ix,
        "full_ix": full_ix,
        "prtl_ix": prtl_ix,
        "use_axles": use_axles,
        "use_compact": use_compact,
        "use_balance_fb": use_balance_fb,
        "use_balance_ss": use_balance_ss,
        "use_vdr": use_vdr,
        "n_warm": n_warm,
    }


def _minizinc_version(exe: Path) -> str:
    try:
        proc = subprocess.run(
            [str(exe), "--version"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise RuntimeError(f"failed to run {exe}: {exc}") from exc
    return (proc.stdout or proc.stderr or "").strip().splitlines()[0] if proc.stdout or proc.stderr else ""


def _first_stat(stats: dict, names: Sequence[str]) -> Optional[float]:
    for name in names:
        val = stats.get(name)
        if _looks_number(val):
            return float(val)
    return None


def _parse_json_stream(text: str) -> dict:
    """Parse MiniZinc --json-stream. Keep status, objective, bound, and gap separate."""
    solution: Optional[dict] = None
    stats: dict = {}
    status: Optional[str] = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = msg.get("type")
        if kind == "solution":
            solution = msg.get("output") or msg.get("sections") or msg
        elif kind == "status":
            raw_status = msg.get("status")
            if raw_status:
                status = str(raw_status)
        elif kind == "statistics":
            block = msg.get("statistics")
            if isinstance(block, dict):
                stats.update(block)
        elif kind == "error":
            raise RuntimeError(msg.get("message") or text)
    vars_out = _solution_vars(solution or {})
    obj = None
    if vars_out:
        if _looks_number(vars_out.get("obj")):
            obj = float(vars_out["obj"])
        elif _looks_number(vars_out.get("_objective")):
            obj = float(vars_out["_objective"])
    if obj is None:
        obj = _first_stat(stats, ("objective", "obj", "best", "primalBound"))
    bound = _first_stat(
        stats,
        ("objectiveBound", "bound", "dualBound", "bestBound"),
    )
    gap = _first_stat(stats, ("gap", "mipGap", "relGap"))
    if gap is None:
        gap = _mip_gap(obj, bound, maximize=True)
    return {
        "status": status or "UNKNOWN",
        "feasible": solution is not None,
        "output": solution,
        "statistics": stats,
        "objective": obj,
        "bound": bound,
        "gap": gap,
    }


def _cube_index(arr: Any, i: int, j: int, k: int) -> int:
    """Read a MiniZinc JSON array[1..n1,1..n2,1..n3] as 0-based nested lists."""
    if not isinstance(arr, list):
        raise TypeError(f"expected nested list, got {type(arr)!r}")
    row = arr[i]
    if isinstance(row, list) and row and isinstance(row[0], list):
        return int(row[j][k])
    # flattened row-major
    n2 = None
    n3 = None
    if arr and isinstance(arr[0], list):
        n2 = len(arr[0])
        n3 = 1
        return int(arr[i][j * n3 + k] if n2 else arr[i][j])
    raise TypeError("slot array is not a 3D JSON list")


def _solution_vars(output: dict) -> dict:
    if not output:
        return {}
    if "slot_full" in output:
        return output
    inner = output.get("json")
    if isinstance(inner, str):
        try:
            inner = json.loads(inner)
        except json.JSONDecodeError:
            inner = None
    if isinstance(inner, dict) and "slot_full" in inner:
        return inner
    return output


def slots_from_output(output: dict, maps: dict) -> List[Tuple[Any, Any, Any, Any, bool]]:
    """Decode (container, layer, position, pallet, is_partial) tuples."""
    output = _solution_vars(output)
    slot_full = output.get("slot_full")
    slot_prtl = output.get("slot_prtl")
    if slot_full is None or slot_prtl is None:
        raise ValueError(f"MiniZinc solution missing slot_full/slot_prtl: {list(output)}")
    veh = maps["veh"]
    lyr = maps["lyr"]
    pos = maps["pos"]
    full = maps["full"]
    prtl = maps["prtl"]
    assigned: List[Tuple[Any, Any, Any, Any, bool]] = []
    for i, v in enumerate(veh):
        for j, l in enumerate(lyr):
            for k, p in enumerate(pos):
                fi = _cube_index(slot_full, i, j, k)
                ri = _cube_index(slot_prtl, i, j, k)
                if fi > 0:
                    assigned.append((v, l, p, full[fi - 1], False))
                elif ri > 0:
                    assigned.append((v, l, p, prtl[ri - 1], True))
    return assigned


def instructions_from_slots(ip: dict, assigned: Sequence[Tuple[Any, Any, Any, Any, bool]]) -> List[LoadObj]:
    instr: List[LoadObj] = []
    mtl_count: Dict[Any, int] = {}
    seq_dict = ip.get("positionSequenceDict") or {}

    for c, l, p, plt, is_partial in assigned:
        obj = LoadObj()
        obj.marketID = ip.get("marketID", "")
        obj.palletID = plt
        obj.container = c
        obj.layer = l
        obj.position = p
        obj.sequence = seq_dict.get(p, p)
        obj.material = (ip.get("palletMaterialDict") or {}).get(plt, "")
        obj.brand = (ip.get("palletBrandDict") or {}).get(plt, "")
        obj.description = (ip.get("palletDescriptionDict") or {}).get(plt, "")
        obj.bin_loc = (ip.get("palletBinLocDict") or {}).get(plt, "")
        obj.batch = (ip.get("palletBatchDict") or {}).get(plt, "")
        obj.batch_date = (ip.get("palletBatchDateDict") or {}).get(plt, "")
        raw_weight = (ip.get("palletWeightDict") or {}).get(plt, 0)
        raw_height = (ip.get("palletHeightDict") or {}).get(plt, 0)
        pallet_w_ft = (ip.get("palletWidthDict") or {}).get(plt, 40.0) / 12.0
        pallet_d_ft = (ip.get("palletLengthDict") or {}).get(plt, 48.0) / 12.0
        obj.weight = raw_weight
        obj.height = raw_height
        obj.cube = pallet_w_ft * pallet_d_ft * (float(raw_height or 0) / 12.0)
        n_cases = (ip.get("palletNumCasesDict") or {}).get(plt, 0)
        if is_partial:
            obj.cases = n_cases * _frac(ip, plt)
            obj.is_partial = True
            obj.pallet_type = "Partial"
        else:
            obj.cases = n_cases
            obj.is_partial = False
            obj.pallet_type = "Full"
        obj.total_cases = n_cases
        obj.stacking_group = (ip.get("palletStackingGroupDict") or {}).get(plt, "G1")
        obj.location = (ip.get("palletLocationDict") or {}).get(plt, "")
        obj.shipTo = (ip.get("palletPlantDict") or {}).get(plt, "")
        mtl_count[obj.material] = mtl_count.get(obj.material, 0) + 1
        instr.append(obj)

    instr.sort(key=lambda o: (str(o.container), o.sequence if o.sequence is not None else 0))
    prev_c, seq = None, 0
    for obj in instr:
        if obj.container != prev_c:
            seq = 1
            prev_c = obj.container
        else:
            seq += 1
        obj.sequence = seq
        obj.material_count = mtl_count.get(obj.material, 0)
    return instr


class ContainerLoadingOptimizerMiniZinc:
    """MiniZinc/HiGHS truck loader. Parallel to ContainerLoadingOptimizerHiGHS."""

    def __init__(
        self,
        inputParams: dict,
        run_ocean: bool = False,
        time_limit: float = 600.0,
        gap: float = 0.01,
        sku_penalty_factor: float = 1.0,
        vdr_stacking_rules: bool = False,
        minizinc_exe: Optional[Path] = None,
        solver: str = "highs",
        work_dir: Optional[Path] = None,
        threads: int = 0,
        warm_slots: Optional[Sequence[Tuple[Any, Any, Any, Any, bool]]] = None,
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
        self.minizinc_exe = Path(minizinc_exe) if minizinc_exe else find_minizinc()
        self.solver = solver
        self.work_dir = Path(work_dir) if work_dir else None
        self.threads = threads
        self.warm_slots = list(warm_slots) if warm_slots else None
        self._maps: Optional[dict] = None
        self._dzn_path: Optional[Path] = None
        self._raw_output = ""
        self.solve_result: Dict[str, Any] = {}
        self.assigned: List[Tuple[Any, Any, Any, Any, bool]] = []
        self.enforceAxleWeightConstraints = False
        self.enforceWeightBalancing = False

    def build_model(self) -> Path:
        if not MODEL_PATH.is_file():
            raise FileNotFoundError(f"missing MiniZinc model: {MODEL_PATH}")
        parent = self.work_dir or Path(tempfile.mkdtemp(prefix="loadmax_mzn_"))
        parent.mkdir(parents=True, exist_ok=True)
        self._dzn_path = parent / "truck_loading.dzn"
        self._maps = write_dzn(
            self.inputParams,
            self._dzn_path,
            run_ocean=self.run_ocean,
            vdr_stacking_rules=self.vdr_stacking_rules,
            sku_penalty_factor=self.sku_penalty_factor,
            warm_slots=self.warm_slots,
        )
        self.enforceAxleWeightConstraints = bool(self._maps["use_axles"])
        self.enforceWeightBalancing = bool(
            self._maps["use_balance_fb"] or self._maps["use_balance_ss"]
        )
        print(f"  MiniZinc : {self.minizinc_exe}")
        print(f"  Version  : {_minizinc_version(self.minizinc_exe)}")
        print(f"  Solver   : {self.solver}")
        print(f"  Model    : {MODEL_PATH}")
        print(f"  Data     : {self._dzn_path}")
        print(f"  Axles    : {self.enforceAxleWeightConstraints}")
        print(f"  Compact  : {self._maps['use_compact']}")
        print(f"  L/R bal  : {self._maps['use_balance_ss']}")
        print(f"  F/B bal  : {self._maps['use_balance_fb']}")
        n_warm = self._maps.get("n_warm") or 0
        print(f"  Warm start: {n_warm} slots" if n_warm else "  Warm start: off")
        return self._dzn_path

    def solve(self) -> dict:
        if self._dzn_path is None or self._maps is None:
            self.build_model()
        time_ms = int(self.time_limit * 1000)
        compile_budget_ms = 120_000
        cmd = [
            str(self.minizinc_exe),
            "--solver",
            self.solver,
            "--json-stream",
            "--output-mode",
            "json",
            "--output-objective",
            "--statistics",
            "--time-limit",
            str(time_ms + compile_budget_ms),
            "--solver-time-limit",
            str(time_ms),
            "--relGap",
            str(self.gap),
        ]
        if self.threads:
            cmd.extend(["-p", str(self.threads)])
        cmd.extend([str(MODEL_PATH), str(self._dzn_path)])
        print("  Running :", " ".join(cmd))
        proc = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
        )
        self._raw_output = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
        if proc.returncode not in (0, 1):
            raise RuntimeError(
                f"MiniZinc exited {proc.returncode}\n{self._raw_output[-4000:]}"
            )
        report = _parse_json_stream((proc.stdout or "") + "\n" + (proc.stderr or ""))
        output = report["output"]
        if output is None:
            raise RuntimeError(
                f"MiniZinc returned no solution (status={report['status']})\n"
                f"{self._raw_output[-4000:]}"
            )
        vars_out = _solution_vars(output)
        self.assigned = slots_from_output(vars_out, self._maps)
        obj = report["objective"]
        bound = report["bound"]
        gap = report["gap"]
        status = report["status"]
        reconstructed = reconstruct_objective(self.inputParams, self.assigned)
        self.solve_result = {
            "status": status,
            "message": status,
            "feasible": report["feasible"],
            "objective_value": obj,
            "objective_bound": bound,
            "gap": gap,
            "reconstructed_objective": reconstructed,
            "statistics": report["statistics"],
            "n_assigned": len(self.assigned),
            "returncode": proc.returncode,
            "warm_started": bool(self.warm_slots),
        }
        print(f"  Status   : {status}")
        print(f"  Feasible : {report['feasible']}")
        print(f"  Objective: {obj}")
        print(f"  Bound    : {bound}")
        print(f"  Gap      : {gap}")
        print(f"  Reconstructed obj: {reconstructed}")
        print(f"  Assigned : {len(self.assigned)}")
        return self.solve_result

    def generate_loading_instructions(self) -> List[LoadObj]:
        instr = instructions_from_slots(self.inputParams, self.assigned)
        print(f"Generated {len(instr)} loading instructions (MiniZinc).")
        return instr
