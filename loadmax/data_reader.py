"""
data_reader.py
==============
Read dumped inputParams CSVs (data/im_input_1.csv style) for the optimizer.
"""

import ast
import csv
import math
import re
from pathlib import Path


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


if __name__ == "__main__":
    inputParams = read_im_input_csv("data/im_input_1.csv")
    print("\nSample Input Parameters:")
    print(f"  Market ID: {inputParams['marketID']}")
    print(f"  Containers: {len(inputParams['containerList'])}")
    print(f"  Full Pallets: {len(inputParams['fullPalletList'])}")
    print(f"  Partial Pallets: {len(inputParams['partialPalletList'])}")
    print(f"  Unique SKUs: {inputParams['unique_sku_n']}")
    print(f"  Max Weight: {inputParams['max_wt']}")
