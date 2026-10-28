"""
data_reader.py
==============
Read dumped inputParams CSVs (data/im_input_1.csv style) for the optimizer.

Optional truck-cap keys (all omitted means today's model):

- ``palletReducedCapClassDict`` / ``reducedCapMaterialList`` mark Class R.
- ``reducedCapMaxPallets`` is the pallet cap of a truck that holds any Class R pallet.
- ``tempControlledMaxPallets`` is the optional cap of a truck that holds any
  pallet flagged in ``palletRefrigeratedDict``.
- ``containerMaxPallets`` and ``containerMaxWt`` override the nominal pallet
  count and the total weight cap per truck.
"""

from __future__ import annotations

import ast
import csv
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional


def read_im_input_csv(path):
    """
    Read a dumped inputParams CSV (data/im_input_1.csv style).

    File format: one key,value row per parameter. Values are Python literals
    (lists, dicts, numbers, strings).
    """
    path = Path(path)
    print("=" * 70)
    print(f"READING INPUT PARAMS CSV: {path}")
    print("=" * 70)

    params = {}
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        for row in reader:
            if not row or not str(row[0]).strip():
                continue
            key = str(row[0]).strip()
            raw = row[1] if len(row) > 1 else ""
            params[key] = _parse_im_value(raw)

    params = _normalize_im_input(params)

    n_full = len(params.get("fullPalletList") or [])
    n_prtl = len(params.get("partialPalletList") or [])
    n_veh = len(params.get("containerList") or [])
    print(f"  Market      : {params.get('marketID')}")
    print(f"  Containers  : {n_veh}  {params.get('containerList')}")
    print(f"  Full pallets: {n_full}")
    print(f"  Partials    : {n_prtl}")
    print(f"  Positions   : {len(params.get('positionList') or [])}")
    print(f"  Layers      : {params.get('layerList')}")
    print(f"  Max weight  : {params.get('max_wt')}")
    print(f"  Front axle  : {params.get('max_front_wt')}")
    print(f"  Rear axle   : {params.get('max_rear_wt')}")
    cap_r = params.get("reducedCapMaxPallets")
    cap_t = params.get("tempControlledMaxPallets")
    if isinstance(cap_r, int) and cap_r > 0:
        print(f"  Class R cap : {cap_r}")
    if isinstance(cap_t, int) and cap_t > 0:
        print(f"  Class T cap : {cap_t}")
    if params.get("containerMaxPallets"):
        print(f"  Truck caps  : {params.get('containerMaxPallets')}")
    if params.get("containerMaxWt"):
        print(f"  Truck weights: {params.get('containerMaxWt')}")
    print("=" * 70)
    return params


def _parse_im_value(raw):
    if raw is None:
        return None
    s = str(raw).strip()
    if s == "":
        return None
    s = re.sub(
        r"datetime\.date\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)",
        r"'\1-\2-\3'",
        s,
    )
    s = re.sub(r"\bnan\b", "None", s, flags=re.IGNORECASE)
    try:
        return ast.literal_eval(s)
    except (ValueError, SyntaxError):
        return s


def _normalize_im_input(params):
    """Clean dumped input so the HiGHS/Gurobi models can consume it."""

    def _as_list(val):
        if val is None:
            return []
        if isinstance(val, (list, tuple)):
            out = []
            seen = set()
            for x in val:
                if x in seen:
                    continue
                seen.add(x)
                out.append(x)
            return out
        return [val]

    def _maybe_int(x):
        if isinstance(x, float) and x.is_integer():
            return int(x)
        return x

    for list_key in (
        "containerList",
        "effectiveContainerList",
        "layerList",
        "positionList",
        "frontPositionList",
        "rearPositionList",
        "leftPositionList",
        "rightPositionList",
        "sectionList",
        "fullPalletList",
        "partialPalletList",
        "materialList",
        "door_posn",
    ):
        if list_key in params:
            params[list_key] = [_maybe_int(x) for x in _as_list(params[list_key])]

    for dict_key in (
        "container_priority",
        "palletWeightDict",
        "palletHeightDict",
        "palletLengthDict",
        "palletWidthDict",
        "palletMaterialDict",
        "palletBatchDict",
        "palletNumCasesDict",
        "palletBinLocDict",
        "palletBatchDateDict",
        "palletPlantDict",
        "palletLocationDict",
        "palletStackingGroupDict",
        "palletBrandDict",
        "palletDescriptionDict",
        "fullPalletDemandByMaterialBatch",
        "partialPalletDemandByMaterialBatch",
        "partialFractionValueForPartialPallet",
        "max_prior_full",
        "max_prior_prtl",
        "stackFactorPallet",
        "stackFactorg2Pallet",
        "stackFactorg3Pallet",
        "stackFactorg4Pallet",
        "stackFactorq4Pallet",
        "skuPriorityIncentive",
        "positionSequenceDict",
        "positionGroupDict",
        "positionGroupBalanceCoefficientDict",
        "plantPriorityDict",
        "palletPriorityValue_numAvailableContainers",
        "palletProfitDict",
        "crushingLimitByMaterial",
        "fullPalletFootPrintArea",
        "partialPalletFootPrintArea",
        "palletRefrigeratedDict",
    ):
        if dict_key in params:
            cleaned = {}
            raw = params[dict_key]
            if isinstance(raw, dict):
                for k, v in raw.items():
                    cleaned[_maybe_int(k) if not isinstance(k, tuple) else k] = (
                        "" if v is None else v
                    )
            params[dict_key] = cleaned

    if not params.get("stackFactorg4Pallet") and params.get("stackFactorq4Pallet"):
        params["stackFactorg4Pallet"] = params["stackFactorq4Pallet"]

    if "palletDescriptionDict" not in params:
        params["palletDescriptionDict"] = {}
    if "skuPriorityIncentive" not in params:
        params["skuPriorityIncentive"] = {}
    if "plantPriorityDict" not in params:
        params["plantPriorityDict"] = {}

    for wt_key in ("max_front_wt", "max_rear_wt"):
        val = params.get(wt_key)
        if val in ([], {}, "", None) or isinstance(val, (list, dict)):
            params[wt_key] = None

    cp = {}
    for k, v in (params.get("container_priority") or {}).items():
        cp[_maybe_int(k)] = v
    params["container_priority"] = cp
    for c in params.get("containerList") or []:
        params["container_priority"].setdefault(c, 0.5 if c == 0 else 1.0)

    if "max_prior_full" not in params:
        params["max_prior_full"] = {p: 0 for p in params.get("fullPalletList") or []}
    if "max_prior_prtl" not in params:
        params["max_prior_prtl"] = {p: 0 for p in params.get("partialPalletList") or []}

    params = bind_sku_priority_to_pallets(params)
    resolve_capacity_classes(params)
    return params


def bind_sku_priority_to_pallets(params):
    """
    skuPriorityIncentive in dumped files is often keyed by material.
    HiGHS looks it up by pallet id. Copy material ranks onto each pallet
    via palletMaterialDict. Also prefer pallet_sku_priority_incentive as
    the fill reward so priority-1 SKUs load first.
    """
    all_plts = list(params.get("fullPalletList") or []) + list(
        params.get("partialPalletList") or []
    )
    sku_map = dict(params.get("skuPriorityIncentive") or {})
    plt_mtl = params.get("palletMaterialDict") or {}
    pallet_rank = params.get("palletPriorityDict") or {}
    pallet_keys = set(all_plts)

    def _as_number(val, default=1.0):
        if val is None or val == "":
            return default
        if isinstance(val, str):
            if val.strip().upper() == "YES":
                return 1.0
            try:
                return float(val)
            except ValueError:
                return default
        try:
            x = float(val)
        except (TypeError, ValueError):
            return default
        return x if math.isfinite(x) else default

    expanded = {}
    map_hits_pallets = any(k in pallet_keys for k in sku_map)
    if sku_map and not map_hits_pallets:
        for plt in all_plts:
            mtl = plt_mtl.get(plt)
            if mtl in sku_map:
                expanded[plt] = _as_number(sku_map[mtl], 1.0)
            elif pallet_rank.get(plt) is not None:
                expanded[plt] = _as_number(pallet_rank[plt], 1.0)
        if expanded:
            params["skuPriorityIncentive"] = expanded
    elif sku_map:
        params["skuPriorityIncentive"] = {
            k: _as_number(v, 1.0) for k, v in sku_map.items()
        }

    fill = dict(params.get("palletProfitDict") or {})
    sku_inc = params.get("pallet_sku_priority_incentive") or {}
    for plt, val in sku_inc.items():
        fill[plt] = _as_number(val, fill.get(plt, 1.0))
    # Higher photographed incentive = load first. If missing, invert
    # palletPriorityDict (1 is most urgent).
    for plt in all_plts:
        if plt in fill:
            continue
        rank = pallet_rank.get(plt)
        if rank is not None:
            r = _as_number(rank, 1.0)
            fill[plt] = 1.0 / r if r else 1.0
    if fill:
        params["palletProfitDict"] = fill
    return params


_YES = {"Yes", "yes", "YES"}


def _canon_id(value: Any) -> Any:
    """Match container_priority key handling: 1, 1.0, and '1' are one id."""
    if isinstance(value, bool):
        return value
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if text == "":
            return text
        try:
            number = float(text)
        except ValueError:
            return text
        if math.isfinite(number) and number.is_integer():
            return int(number)
        return text
    return value


def _is_yes_flag(value: Any) -> bool:
    return value is True or (isinstance(value, str) and value.strip() in _YES)


def _as_finite(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _optional_positive_int(value: Any, name: str) -> Optional[int]:
    """Positive integer cap, or None when the feature is off.

    Absent, blank, zero, and negative values turn the feature off.
    A non-integer is an error.
    """
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer, got {value!r}")
    number = value
    if isinstance(number, str):
        text = number.strip()
        if text == "":
            return None
        try:
            number = float(text)
        except ValueError as exc:
            raise ValueError(f"{name} must be an integer, got {value!r}") from exc
    if isinstance(number, float):
        if not math.isfinite(number) or not number.is_integer():
            raise ValueError(f"{name} must be an integer, got {value!r}")
        number = int(number)
    if not isinstance(number, int):
        raise ValueError(f"{name} must be an integer, got {value!r}")
    if number <= 0:
        return None
    return number


def _require_positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a positive integer, got {value!r}")
    number = value
    if isinstance(number, str):
        text = number.strip()
        try:
            number = float(text)
        except ValueError as exc:
            raise ValueError(f"{name} must be a positive integer, got {value!r}") from exc
    if isinstance(number, float):
        if not math.isfinite(number) or not number.is_integer():
            raise ValueError(f"{name} must be a positive integer, got {value!r}")
        number = int(number)
    if not isinstance(number, int) or number <= 0:
        raise ValueError(f"{name} must be a positive integer, got {value!r}")
    return number


def _require_positive_number(value: Any, name: str) -> float:
    if isinstance(value, bool) or value is None or value == "":
        raise ValueError(f"{name} must be a positive number, got {value!r}")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a positive number, got {value!r}") from exc
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{name} must be a positive number, got {value!r}")
    return number


def _non_negative_weight(value: Any, default: float, name: str) -> float:
    if value is None or value == "":
        return default
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a non-negative number, got {value!r}") from exc
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{name} must be a non-negative number, got {value!r}")
    return number


def _index_ids(ids: Any, label: str) -> Dict[Any, Any]:
    mapping: Dict[Any, Any] = {}
    for item in ids or []:
        key = _canon_id(item)
        if key in mapping and mapping[key] != item:
            raise ValueError(f"{label} has duplicate id {item!r} after normalisation")
        mapping[key] = item
    return mapping


def _lookup_demand(table: dict, plt: Any, label: str) -> int:
    raw = None
    if plt in table:
        raw = table[plt]
    else:
        key = _canon_id(plt)
        for cand, val in table.items():
            if _canon_id(cand) == key:
                raw = val
                break
    if raw is None or raw == "":
        raise ValueError(f"pallet {plt!r} is missing {label}")
    if isinstance(raw, bool):
        raise ValueError(f"pallet {plt!r} {label} must be an integer, got {raw!r}")
    number = raw
    if isinstance(number, str):
        try:
            number = float(number.strip())
        except ValueError as exc:
            raise ValueError(
                f"pallet {plt!r} {label} must be an integer, got {raw!r}"
            ) from exc
    if isinstance(number, float):
        if not math.isfinite(number) or not number.is_integer():
            raise ValueError(f"pallet {plt!r} {label} must be an integer, got {raw!r}")
        number = int(number)
    if not isinstance(number, int) or number < 0:
        raise ValueError(f"pallet {plt!r} {label} must be a non-negative integer, got {raw!r}")
    return number


def _priority_of(params: dict, container_id: Any) -> float:
    raw = params.get("container_priority") or {}
    if container_id in raw:
        return _as_finite(raw[container_id], 1.0)
    key = _canon_id(container_id)
    for cand, val in raw.items():
        if _canon_id(cand) == key:
            return _as_finite(val, 1.0)
    return 1.0


@dataclass
class CapacityPlan:
    """Resolved per-truck caps and class rules. Inactive fields leave the model unchanged."""

    nominal_cap: Dict[Any, int]
    use_nominal_cap: bool
    max_wt_by_truck: Dict[Any, float]
    use_per_truck_wt: bool
    class_r_ids: set
    class_t_ids: set
    cap_r: Optional[int]
    cap_t: Optional[int]
    consolidation_weight: float
    mix_weight: float
    mode: str
    lambda_r: float
    mu_mix: float
    demand_r: int
    demand_t: int
    default_n: int

    @property
    def class_r_active(self) -> bool:
        return self.cap_r is not None

    @property
    def class_t_cap_active(self) -> bool:
        return self.cap_t is not None

    @property
    def mix_active(self) -> bool:
        return self.class_r_active and self.mu_mix > 0

    @property
    def hard_mode(self) -> bool:
        return self.mode == "hard"

    @property
    def report(self) -> bool:
        return self.use_nominal_cap or self.class_r_active or self.class_t_cap_active

    @property
    def has_rule(self) -> bool:
        return self.report or self.use_per_truck_wt

    @property
    def truck_limit(self) -> int:
        if not self.cap_r:
            return 0
        return math.ceil(self.demand_r / self.cap_r)

    def applicable_cap(self, vehicle: Any, has_r: bool, has_t: bool) -> int:
        if vehicle in self.nominal_cap:
            cap = self.nominal_cap[vehicle]
        else:
            key = _canon_id(vehicle)
            match = next(
                (item for item in self.nominal_cap if _canon_id(item) == key),
                None,
            )
            if match is None:
                raise ValueError(f"container {vehicle!r} is not in containerList")
            cap = self.nominal_cap[match]
        if has_r and self.cap_r is not None:
            cap = min(cap, self.cap_r)
        if has_t and self.cap_t is not None:
            cap = min(cap, self.cap_t)
        return cap


def capacity_infeasible_message(plan: CapacityPlan) -> str:
    """Explain an infeasible cap solve. There is no silent fallback."""
    if plan.hard_mode and plan.class_r_active:
        return (
            "INFEASIBLE: hard Class R consolidation allows at most "
            f"{plan.truck_limit} truck(s), and no load meets that limit together with "
            "the pallet cap, weight, stacking, or compact packing rules. "
            "No fallback was applied."
        )
    return (
        "INFEASIBLE: no load satisfies the truck pallet or weight cap together with "
        "the other loading rules. Compact packing was not relaxed."
    )


def resolve_capacity_classes(params: dict) -> CapacityPlan:
    """Normalise optional truck-cap keys and return the plan.

    Absent or non-positive ``reducedCapMaxPallets`` / ``tempControlledMaxPallets``
    leave that rule off. Omitted weights use 0.5 and 0.25. Omitted mode is soft.
    Nominal pallet cap defaults to ``len(layerList) * len(positionList)``.
    Calling this twice is safe.
    """
    full = list(params.get("fullPalletList") or [])
    prtl = list(params.get("partialPalletList") or [])
    containers = list(params.get("containerList") or [])
    layers = list(params.get("layerList") or [])
    positions = list(params.get("positionList") or [])
    default_n = len(layers) * len(positions)
    pallet_index = _index_ids(full + prtl, "pallet list")
    container_index = _index_ids(containers, "containerList")

    def _need_grid(name: str) -> None:
        if not containers or default_n <= 0:
            raise ValueError(
                f"{name} requires containerList, layerList, and positionList"
            )

    cap_r = None
    if "reducedCapMaxPallets" in params:
        cap_r = _optional_positive_int(
            params.get("reducedCapMaxPallets"), "reducedCapMaxPallets"
        )
        params["reducedCapMaxPallets"] = cap_r

    cap_t = None
    if "tempControlledMaxPallets" in params:
        cap_t = _optional_positive_int(
            params.get("tempControlledMaxPallets"), "tempControlledMaxPallets"
        )
        params["tempControlledMaxPallets"] = cap_t

    class_r: set = set()
    dict_given = (
        "palletReducedCapClassDict" in params
        and params.get("palletReducedCapClassDict") not in (None, "")
    )
    list_given = (
        "reducedCapMaterialList" in params
        and params.get("reducedCapMaterialList") not in (None, "")
    )
    if dict_given:
        raw_flags = params.get("palletReducedCapClassDict")
        if not isinstance(raw_flags, dict):
            raise ValueError(
                "palletReducedCapClassDict must be a dict of pallet id to Yes/blank, "
                f"got {type(raw_flags).__name__}"
            )
        for key, val in raw_flags.items():
            canon = _canon_id(key)
            if canon not in pallet_index:
                raise ValueError(
                    f"palletReducedCapClassDict has unknown pallet id {key!r}"
                )
            if _is_yes_flag(val):
                class_r.add(pallet_index[canon])
    if list_given:
        raw_list = params.get("reducedCapMaterialList")
        if isinstance(raw_list, tuple):
            raw_list = list(raw_list)
        if not isinstance(raw_list, list):
            raise ValueError(
                "reducedCapMaterialList must be a list of material ids, "
                f"got {type(raw_list).__name__}"
            )
        materials = params.get("palletMaterialDict") or {}
        by_material: Dict[Any, list] = {}
        for plt in full + prtl:
            mtl = materials.get(plt)
            if mtl is None or mtl == "":
                continue
            by_material.setdefault(_canon_id(mtl), []).append(plt)
        unknown = []
        for mtl in raw_list:
            hits = by_material.get(_canon_id(mtl), [])
            if not hits:
                unknown.append(mtl)
            class_r.update(hits)
        if unknown:
            raise ValueError(
                "reducedCapMaterialList entry "
                + ", ".join(repr(item) for item in unknown)
                + " matches no pallet in palletMaterialDict"
            )
    if dict_given or list_given:
        params["palletReducedCapClassDict"] = {plt: "Yes" for plt in sorted(class_r, key=str)}

    class_t: set = set()
    refrig = params.get("palletRefrigeratedDict") or {}
    if isinstance(refrig, dict):
        for key, val in refrig.items():
            canon = _canon_id(key)
            if canon in pallet_index and _is_yes_flag(val):
                class_t.add(pallet_index[canon])

    use_nominal = (
        "containerMaxPallets" in params and params.get("containerMaxPallets") not in (None, "")
    )
    nominal: Dict[Any, int] = {}
    if use_nominal:
        _need_grid("containerMaxPallets")
        raw_caps = params.get("containerMaxPallets")
        if not isinstance(raw_caps, dict):
            raise ValueError(
                "containerMaxPallets must be a dict of container id to pallet cap, "
                f"got {type(raw_caps).__name__}"
            )
        supplied: Dict[Any, int] = {}
        for key, val in raw_caps.items():
            canon = _canon_id(key)
            if canon not in container_index:
                raise ValueError(f"containerMaxPallets has unknown container id {key!r}")
            cid = container_index[canon]
            if cid in supplied:
                raise ValueError(
                    f"containerMaxPallets has duplicate container id {key!r}"
                )
            supplied[cid] = _require_positive_int(
                val, f"containerMaxPallets for container {cid}"
            )
        for cid in containers:
            nominal[cid] = supplied.get(cid, default_n)
        params["containerMaxPallets"] = dict(nominal)
    else:
        for cid in containers:
            nominal[cid] = default_n

    if cap_r is not None:
        _need_grid("reducedCapMaxPallets")
        for cid, n_v in nominal.items():
            if cap_r > n_v:
                raise ValueError(
                    f"reducedCapMaxPallets ({cap_r}) is above nominal pallet cap "
                    f"{n_v} for container {cid}"
                )
    if cap_t is not None:
        _need_grid("tempControlledMaxPallets")
        for cid, n_v in nominal.items():
            if cap_t > n_v:
                raise ValueError(
                    f"tempControlledMaxPallets ({cap_t}) is above nominal pallet cap "
                    f"{n_v} for container {cid}"
                )

    global_wt = params.get("max_wt")
    try:
        global_wt_num = float(global_wt)
        if not math.isfinite(global_wt_num):
            global_wt_num = 0.0
    except (TypeError, ValueError):
        global_wt_num = 0.0

    use_wt = "containerMaxWt" in params and params.get("containerMaxWt") not in (None, "")
    max_wt: Dict[Any, float] = {}
    if use_wt:
        raw_wt = params.get("containerMaxWt")
        if not isinstance(raw_wt, dict):
            raise ValueError(
                "containerMaxWt must be a dict of container id to weight cap, "
                f"got {type(raw_wt).__name__}"
            )
        if not containers:
            raise ValueError("containerMaxWt requires containerList")
        supplied_wt: Dict[Any, float] = {}
        for key, val in raw_wt.items():
            canon = _canon_id(key)
            if canon not in container_index:
                raise ValueError(f"containerMaxWt has unknown container id {key!r}")
            cid = container_index[canon]
            if cid in supplied_wt:
                raise ValueError(f"containerMaxWt has duplicate container id {key!r}")
            supplied_wt[cid] = _require_positive_number(
                val, f"containerMaxWt for container {cid}"
            )
        for cid in containers:
            if cid in supplied_wt:
                max_wt[cid] = supplied_wt[cid]
                continue
            if global_wt_num <= 0:
                raise ValueError(
                    f"container {cid} is missing from containerMaxWt and max_wt "
                    "is not a positive number"
                )
            max_wt[cid] = global_wt_num
        params["containerMaxWt"] = dict(max_wt)
    else:
        for cid in containers:
            max_wt[cid] = global_wt_num

    consolidation_weight = _non_negative_weight(
        params.get("reducedCapConsolidationWeight"),
        0.5,
        "reducedCapConsolidationWeight",
    )
    mix_weight = _non_negative_weight(
        params.get("reducedCapTempMixWeight"),
        0.25,
        "reducedCapTempMixWeight",
    )
    if "reducedCapConsolidationWeight" in params:
        params["reducedCapConsolidationWeight"] = consolidation_weight
    if "reducedCapTempMixWeight" in params:
        params["reducedCapTempMixWeight"] = mix_weight

    mode = "soft"
    if (
        "reducedCapConsolidationMode" in params
        and params.get("reducedCapConsolidationMode") not in (None, "")
    ):
        raw_mode = str(params.get("reducedCapConsolidationMode")).strip().lower()
        if raw_mode not in {"soft", "hard"}:
            raise ValueError(
                "reducedCapConsolidationMode must be 'soft' or 'hard', "
                f"got {params.get('reducedCapConsolidationMode')!r}"
            )
        mode = raw_mode
        params["reducedCapConsolidationMode"] = mode
    if mode == "hard" and cap_r is None:
        raise ValueError(
            "reducedCapConsolidationMode is 'hard' but reducedCapMaxPallets "
            "is not a positive integer"
        )

    full_demand = params.get("fullPalletDemandByMaterialBatch") or {}
    prtl_demand = params.get("partialPalletDemandByMaterialBatch") or {}
    demand_r = 0
    demand_t = 0
    full_set = set(full)
    prtl_set = set(prtl)

    def _units(plt: Any) -> int:
        total = 0
        if plt in full_set:
            total += _lookup_demand(full_demand, plt, "fullPalletDemandByMaterialBatch")
        if plt in prtl_set:
            total += _lookup_demand(
                prtl_demand, plt, "partialPalletDemandByMaterialBatch"
            )
        return total

    if cap_r is not None:
        demand_r = sum(_units(plt) for plt in class_r)
    if cap_r is not None or cap_t is not None:
        demand_t = sum(_units(plt) for plt in class_t)

    max_cp = max((_priority_of(params, cid) for cid in containers), default=1.0)
    if max_cp < 0:
        max_cp = 0.0
    lambda_r = consolidation_weight * max_cp if cap_r is not None else 0.0
    mu_mix = mix_weight * max_cp if cap_r is not None else 0.0

    return CapacityPlan(
        nominal_cap=nominal,
        use_nominal_cap=use_nominal,
        max_wt_by_truck=max_wt,
        use_per_truck_wt=use_wt,
        class_r_ids=set(class_r),
        class_t_ids=set(class_t),
        cap_r=cap_r,
        cap_t=cap_t,
        consolidation_weight=consolidation_weight,
        mix_weight=mix_weight,
        mode=mode if cap_r is not None else "soft",
        lambda_r=lambda_r,
        mu_mix=mu_mix,
        demand_r=demand_r,
        demand_t=demand_t,
        default_n=default_n,
    )


if __name__ == "__main__":
    inputParams = read_im_input_csv("data/im_input_1.csv")
    print("\nSample Input Parameters:")
    print(f"  Market ID: {inputParams['marketID']}")
    print(f"  Containers: {len(inputParams['containerList'])}")
    print(f"  Full Pallets: {len(inputParams['fullPalletList'])}")
    print(f"  Partial Pallets: {len(inputParams['partialPalletList'])}")
    print(f"  Unique SKUs: {inputParams['unique_sku_n']}")
    print(f"  Max Weight: {inputParams['max_wt']}")
