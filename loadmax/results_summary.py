"""
Container results summary in the same layout as the market loading UI.

Columns:
  Market Code, Brand Group, Date Time, Container,
  Full Pallet, Partial Pallet,
  Utilization (Pallets), Utilization (Volume), Utilization (Weight)
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Optional

from loadmax.data_reader import CapacityPlan, resolve_capacity_classes


SUMMARY_COLUMNS = [
    "Market Code",
    "Brand Group",
    "Date Time",
    "Container",
    "Full Pallet",
    "Partial Pallet",
    "Utilization (Pallets)",
    "Utilization (Volume)",
    "Utilization (Weight)",
    "Cargo Weight: Front",
    "Cargo Weight: Rear",
    "Front Limit",
    "Rear Limit",
]

# Added only when a pallet-cap rule is active, so a run with no new keys
# keeps the original summary columns.
CAPACITY_COLUMNS = [
    "Pallet Count",
    "Pallet Cap",
    "Class R Pallets",
    "Class T Pallets",
    "Class Mix",
]


def _format_date(val: Any) -> str:
    if val is None or val == "":
        return ""
    if isinstance(val, datetime):
        return val.date().isoformat()
    if isinstance(val, date):
        return val.isoformat()
    s = str(val).strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}", s):
        return s[:10]
    m = re.match(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{4})$", s)
    if m:
        a, b, y = m.groups()
        day, month = int(a), int(b)
        # Dumped files use D/M/Y (e.g. 13/05/2024).
        if month > 12 and day <= 12:
            day, month = month, day
        return f"{y}-{month:02d}-{day:02d}"
    return s


def _instruction_date(instr: Any, input_params: dict) -> str:
    batch_dates = input_params.get("palletDelivDateDict") or {}
    bin_dates = input_params.get("palletBinLocDict") or {}
    raw = getattr(instr, "batch_date", None) or getattr(instr, "bin_loc", None)
    if not raw:
        # Fall back to the first pallet on this container's known date.
        raw = next(iter(batch_dates.values()), None) or next(
            iter(bin_dates.values()), None
        )
    return _format_date(raw)


def _blank_if_zero(n: int) -> Any:
    return "" if n == 0 else n


def _bal_coeff(bal: dict, position: Any, keys: tuple) -> float:
    total = 0.0
    for key in keys:
        val = bal.get((position, key), 0.0)
        try:
            total += float(val or 0.0)
        except (TypeError, ValueError):
            pass
    return total


def _cargo_front_rear(items: List[Any], input_params: dict) -> tuple:
    """
    Axle cargo weights, matching the Gurobi / UI formula:
      front = sum((A+C) * weight) over front positions
      rear  = sum((B+D) * weight) over rear positions
    Split positions (typically 13/14) contribute half to each axle.
    """
    front_id = set(input_params.get("frontPositionList") or [])
    rear_id = set(input_params.get("rearPositionList") or [])
    bal = input_params.get("positionGroupBalanceCoefficientDict") or {}

    front = 0.0
    rear = 0.0
    for instr in items:
        pos = instr.position
        wt = float(instr.weight or 0)
        if pos in front_id:
            coeff = _bal_coeff(bal, pos, ("A", "C"))
            front += wt * (coeff if bal else 1.0)
        if pos in rear_id:
            coeff = _bal_coeff(bal, pos, ("B", "D"))
            rear += wt * (coeff if bal else 1.0)
    return front, rear


def _pallet_id(instr: Any) -> Any:
    if hasattr(instr, "palletID"):
        return instr.palletID
    return getattr(instr, "pallet_id", None)


def _summary_columns(rows: List[Dict[str, Any]]) -> List[str]:
    cols = list(SUMMARY_COLUMNS)
    if rows and "Pallet Count" in rows[0]:
        cols.extend(CAPACITY_COLUMNS)
    return cols


def validate_truck_capacity(
    input_params: dict,
    instructions: Iterable[Any],
    plan: Optional[CapacityPlan] = None,
) -> None:
    """Raise if a truck exceeds its cap. Warn when Class R and Class T share a truck."""
    plan = plan or resolve_capacity_classes(input_params)
    if not plan.report:
        return
    instr_list = list(instructions)
    by_container: Dict[Any, List[Any]] = defaultdict(list)
    for instr in instr_list:
        by_container[instr.container].append(instr)

    loaded_r = 0
    trucks_r = 0
    for container, items in by_container.items():
        r_n = sum(1 for item in items if _pallet_id(item) in plan.class_r_ids)
        t_n = sum(1 for item in items if _pallet_id(item) in plan.class_t_ids)
        count = len(items)
        has_r = r_n > 0
        has_t = t_n > 0
        cap = plan.applicable_cap(container, has_r, has_t)
        if count > cap:
            raise ValueError(
                f"Container {container} holds {count} pallets, above its cap of {cap} "
                f"(Class R {r_n}, Class T {t_n})."
            )
        if has_r and has_t:
            print(
                f"WARNING: Container {container} holds both Class R and Class T pallets."
            )
        if has_r:
            trucks_r += 1
            loaded_r += r_n

    if plan.class_r_active:
        minimum = math.ceil(loaded_r / plan.cap_r) if plan.cap_r else 0
        print(f"Class R trucks: {trucks_r} (minimum {minimum}).")


def build_container_results_summary(
    input_params: dict,
    instructions: Iterable[Any],
) -> List[Dict[str, Any]]:
    """One row per loaded container, matching the UI screenshot."""
    instr_list = list(instructions)
    if not instr_list:
        return []

    max_plt_n = int(input_params.get("max_plt_n") or 0)
    max_lyr_n = int(input_params.get("max_lyr_n") or 1)
    n_positions = len(input_params.get("positionList") or [])
    slot_cap = max_plt_n * max_lyr_n if max_plt_n and max_lyr_n else n_positions
    container_h = float(input_params.get("containerHeight") or 0)
    max_wt = float(input_params.get("max_wt") or 0)
    market = input_params.get("marketID", "")
    brand_group = input_params.get("brandGroup", "")

    by_container: Dict[Any, List[Any]] = defaultdict(list)
    for instr in instr_list:
        by_container[instr.container].append(instr)

    plan = resolve_capacity_classes(input_params)
    rows: List[Dict[str, Any]] = []
    for container in sorted(by_container, key=lambda c: (str(type(c)), c)):
        items = by_container[container]
        n_full = sum(1 for i in items if not i.is_partial)
        n_prtl = sum(1 for i in items if i.is_partial)
        total_wt = sum(float(i.weight or 0) for i in items)

        stack_h = defaultdict(float)
        for i in items:
            stack_h[i.position] += float(i.height or 0)

        util_plt = (n_full + n_prtl) / slot_cap * 100 if slot_cap else 0.0
        # Original Pyomo UI volume: full height on (max_plt_n-2) floor
        # spots, door height on the two door positions.
        door_h = float(input_params.get("containerHeightAtDoor") or container_h or 0)
        n_floor = max_plt_n if max_plt_n else n_positions
        if container_h and n_floor >= 2:
            denom_h = container_h * (n_floor - 2) + 2.0 * door_h
        else:
            denom_h = (n_positions or slot_cap) * container_h if container_h else 0.0
        util_vol = (sum(stack_h.values()) / denom_h * 100) if denom_h else 0.0
        wt_cap = max_wt
        if plan.use_per_truck_wt:
            wt_cap = float(plan.max_wt_by_truck.get(container, max_wt) or 0)
        util_wt = (total_wt / wt_cap * 100) if wt_cap else 0.0

        dates = [_instruction_date(i, input_params) for i in items]
        dates = [d for d in dates if d]
        ship_date = min(dates) if dates else ""

        cargo_front, cargo_rear = _cargo_front_rear(items, input_params)
        front_limit = input_params.get("max_front_wt")
        rear_limit = input_params.get("max_rear_wt")
        try:
            front_limit = float(front_limit) if front_limit not in (None, "", [], {}) else ""
        except (TypeError, ValueError):
            front_limit = ""
        try:
            rear_limit = float(rear_limit) if rear_limit not in (None, "", [], {}) else ""
        except (TypeError, ValueError):
            rear_limit = ""

        row = {
            "Market Code": market,
            "Brand Group": brand_group,
            "Date Time": ship_date,
            "Container": container,
            "Full Pallet": n_full,
            "Partial Pallet": _blank_if_zero(n_prtl),
            "Utilization (Pallets)": round(util_plt, 1),
            "Utilization (Volume)": round(util_vol, 1),
            "Utilization (Weight)": round(util_wt, 1),
            "Cargo Weight: Front": round(cargo_front),
            "Cargo Weight: Rear": round(cargo_rear),
            "Front Limit": int(front_limit) if front_limit != "" else "",
            "Rear Limit": int(rear_limit) if rear_limit != "" else "",
        }
        if plan.report:
            r_n = sum(1 for item in items if _pallet_id(item) in plan.class_r_ids)
            t_n = sum(1 for item in items if _pallet_id(item) in plan.class_t_ids)
            has_r = r_n > 0
            has_t = t_n > 0
            row["Pallet Count"] = n_full + n_prtl
            row["Pallet Cap"] = plan.applicable_cap(container, has_r, has_t)
            row["Class R Pallets"] = r_n
            row["Class T Pallets"] = t_n
            row["Class Mix"] = "Yes" if has_r and has_t else "No"
        rows.append(row)
    if plan.report:
        validate_truck_capacity(input_params, instr_list, plan)
    return rows


def print_container_results_summary(rows: List[Dict[str, Any]]) -> None:
    from prettytable import PrettyTable

    print("\n" + "=" * 80)
    print("RESULTS SUMMARY")
    print("=" * 80)
    columns = _summary_columns(rows)
    table = PrettyTable()
    table.field_names = columns
    table.align = "r"
    table.align["Market Code"] = "l"
    table.align["Brand Group"] = "l"
    table.align["Date Time"] = "l"
    if "Class Mix" in columns:
        table.align["Class Mix"] = "l"
    if not rows:
        print("  (no loaded containers)")
        return
    for row in rows:
        table.add_row([row[c] for c in columns])
    print(table)
    print("=" * 80)


def write_container_results_summary(rows: List[Dict[str, Any]], path) -> None:
    import pandas as pd

    pd.DataFrame(rows, columns=_summary_columns(rows)).to_csv(path, index=False)
