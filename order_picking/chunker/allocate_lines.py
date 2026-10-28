"""Turn demand rows into pick lines at real stock locations.

The rule is fixed and has no MIP. Whole pallets come from bulk stock. Whole
cases come from pick-face stock, then from bulk, and keep that row's mode.
Units below one case use an already-opened case, or open the smallest
pick-face case that covers them. Inside a tier the earliest expiry wins,
then the location nearest the dock, then the location id.

This module does not read ``wh_inventory.csv``. The caller supplies
``stock.csv`` and ``sku_master.csv``.
"""

from __future__ import annotations

import argparse
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import pandas as pd

_ROOT = Path(__file__).resolve().parents[1]
_SOURCE = str(_ROOT / "source")
if _SOURCE not in sys.path:
    sys.path.insert(0, _SOURCE)

from chunk_loads import _leg  # noqa: E402
from order_picking_optimizer import _build_lookup, _is_missing, _is_truthy_flag  # noqa: E402

_EPS = 1e-9
_LOAD_COLUMNS = (
    "truck_id",
    "depot_id",
    "destination_id",
    "departure_time",
    "source_order_id",
    "item_id",
    "sku_id",
    "location_id",
    "quantity",
    "weight",
    "volume",
    "pick_time",
    "must_ship",
    "account_tier",
    "pick_mode",
    "batch_id",
    "pallet_id",
)


@dataclass
class _Stock:
    sku_id: str
    location_id: str
    batch_id: str
    pallet_id: str
    qty: float
    expiry_key: tuple
    location_type: str


@dataclass
class _Opened:
    location_id: str
    batch_id: str
    pallet_id: str
    qty: float
    expiry_key: tuple
    location_type: str


def allocate_lines(
    demand: pd.DataFrame,
    stock: pd.DataFrame,
    sku_master: pd.DataFrame,
    travel,
    out_dir: Path,
    allow_short: bool = False,
    depot: Optional[str] = None,
    demand_label: str = "demand",
    stock_label: str = "stock",
    master_label: str = "sku master",
) -> int:
    """Write the load table. Return 1 when a SKU is missing or stock is short."""
    demands = _read_demand(demand, demand_label)
    if depot:
        for line in demands:
            line["travel_depot"] = str(depot)
    stock_rows = _read_stock(stock, stock_label)
    master = _read_master(sku_master, master_label)
    lookup = _build_lookup(travel)
    demands.sort(key=lambda item: item["sort_key"])
    loads: List[dict] = []
    shorts: List[dict] = []
    rejected: List[dict] = []
    opened: List[_Opened] = []
    serial = 0
    requested = 0.0
    pallet_units = 0.0
    case_units = 0.0
    opened_cases = 0
    walking = 0.0

    def emit(line: dict, row_location: str, batch_id: str, pallet_id: str, qty: float, mode: str, sku: dict) -> None:
        nonlocal serial, walking
        serial += 1
        origin = line["travel_depot"]
        walking += _leg(lookup, origin, row_location)
        loads.append(
            {
                "truck_id": line["truck_id"],
                "depot_id": line["depot_id"],
                "destination_id": line["destination_id"],
                "departure_time": line["departure_time"],
                "source_order_id": line["source_order_id"],
                "item_id": f"I{serial}",
                "sku_id": line["sku_id"],
                "location_id": row_location,
                "quantity": qty,
                "weight": qty * sku["unit_weight_kg"],
                "volume": qty * sku["unit_volume_m3"],
                "pick_time": "",
                "must_ship": line["must_ship_text"],
                "account_tier": line["account_tier"],
                "pick_mode": mode,
                "batch_id": batch_id,
                "pallet_id": pallet_id,
            }
        )

    for line in demands:
        sku = master.get(line["sku_id"])
        if sku is None:
            rejected.append(
                {
                    "sku_id": line["sku_id"],
                    "source_row": line["source_row"],
                    "reason": "missing_sku",
                }
            )
            continue
        pool = [row for row in stock_rows if row.sku_id == line["sku_id"]]
        requested += line["quantity"]
        remaining = line["quantity"]
        pallet_qty = sku["pallet_qty"]
        case_qty = sku["case_qty"]
        while pallet_qty is not None and remaining >= pallet_qty - _EPS:
            choice = _best(
                [row for row in pool if row.location_type == "bulk" and row.qty >= pallet_qty - _EPS],
                line["travel_depot"],
                lookup,
            )
            if choice is None:
                break
            choice.qty -= pallet_qty
            remaining -= pallet_qty
            pallet_units += pallet_qty
            emit(line, choice.location_id, choice.batch_id, choice.pallet_id, pallet_qty, "bulk", sku)
        cases_wanted = math.floor((remaining + _EPS) / case_qty)
        taken_cases = 0
        for location_type in ("pickface", "bulk"):
            while taken_cases < cases_wanted and remaining >= case_qty - _EPS:
                choice = _best(
                    [
                        row
                        for row in pool
                        if row.location_type == location_type and row.qty >= case_qty - _EPS
                    ],
                    line["travel_depot"],
                    lookup,
                )
                if choice is None:
                    break
                choice.qty -= case_qty
                remaining -= case_qty
                taken_cases += 1
                case_units += case_qty
                emit(line, choice.location_id, choice.batch_id, choice.pallet_id, case_qty, choice.location_type, sku)
        residual = remaining - math.floor((remaining + _EPS) / case_qty) * case_qty
        unfilled_cases = remaining - residual
        if residual > _EPS:
            used, opened_now = _take_residual(pool, opened, residual, case_qty, line["travel_depot"], lookup)
            if used is not None:
                remaining -= residual
                if opened_now:
                    opened_cases += 1
                emit(line, used.location_id, used.batch_id, used.pallet_id, residual, "pickface", sku)
        if remaining > _EPS or unfilled_cases > _EPS:
            allocated = line["quantity"] - remaining
            shorts.append(
                {
                    "sku_id": line["sku_id"],
                    "requested": line["quantity"],
                    "allocated": allocated,
                    "short": line["quantity"] - allocated,
                }
            )
    folder = Path(out_dir)
    folder.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(loads, columns=list(_LOAD_COLUMNS)).to_csv(folder / "truck_loads.csv", index=False)
    if shorts:
        pd.DataFrame(shorts, columns=["sku_id", "requested", "allocated", "short"]).to_csv(
            folder / "short_stock.csv", index=False
        )
    if rejected:
        pd.DataFrame(rejected, columns=["sku_id", "source_row", "reason"]).to_csv(
            folder / "allocation_rejected.csv", index=False
        )
    base = requested if requested > _EPS else 1.0
    summary = {
        "full_pallet_rate": pallet_units / base,
        "full_case_rate": case_units / base,
        "opened_case_count": opened_cases,
        "walking_minutes": walking,
    }
    pd.DataFrame([summary]).to_csv(folder / "allocation_summary.csv", index=False)
    print(
        f"Full-pallet rate {summary['full_pallet_rate']:.3f}, "
        f"full-case rate {summary['full_case_rate']:.3f}, "
        f"opened cases {opened_cases}, walking {walking:.3f} minutes."
    )
    if rejected:
        print(f"{len(rejected)} demand row(s) have no SKU master. See allocation_rejected.csv.")
        return 1
    if shorts and not allow_short:
        print(f"{len(shorts)} demand row(s) are short. See short_stock.csv.")
        return 1
    return 0


def _take_residual(
    pool: Sequence[_Stock],
    opened: List[_Opened],
    residual: float,
    case_qty: float,
    depot_id: str,
    lookup: dict,
) -> Tuple[Optional[_Opened], bool]:
    """Use an opened case, or open the smallest pick-face case that covers the residual."""
    ready = [case for case in opened if case.qty >= residual - _EPS]
    if ready:
        choice = min(ready, key=lambda case: _rank(case.expiry_key, depot_id, case.location_id, lookup))
        choice.qty -= residual
        return choice, False
    covering = [
        row
        for row in pool
        if row.location_type == "pickface" and row.qty >= residual - _EPS
    ]
    if not covering:
        return None, False
    choice = min(
        covering,
        key=lambda row: (row.qty, _rank(row.expiry_key, depot_id, row.location_id, lookup)),
    )
    take = min(case_qty, choice.qty)
    if take + _EPS < residual:
        return None, False
    choice.qty -= take
    held = _Opened(
        location_id=choice.location_id,
        batch_id=choice.batch_id,
        pallet_id=choice.pallet_id,
        qty=take - residual,
        expiry_key=choice.expiry_key,
        location_type="pickface",
    )
    if held.qty > _EPS:
        opened.append(held)
    return held, True


def _best(rows: Sequence[_Stock], depot_id: str, lookup: dict) -> Optional[_Stock]:
    if not rows:
        return None
    return min(rows, key=lambda row: _rank(row.expiry_key, depot_id, row.location_id, lookup))


def _rank(expiry_key: tuple, depot_id: str, location_id: str, lookup: dict) -> tuple:
    return (expiry_key, _leg(lookup, depot_id, location_id), location_id)


def _read_demand(frame: pd.DataFrame, label: str) -> List[dict]:
    columns = {str(name).strip() for name in frame.columns}
    frame = frame.copy()
    frame.columns = [str(name).strip() for name in frame.columns]
    missing = [name for name in ("sku_id", "quantity", "depot_id") if name not in columns]
    if missing:
        raise ValueError(f"{label} is missing column(s): {', '.join(missing)}.")
    if "group_id" not in columns and "truck_id" not in columns:
        raise ValueError(f"{label} needs column truck_id or group_id.")
    rows = []
    for offset, raw in enumerate(frame.to_dict(orient="records"), start=2):
        row_label = f"{label} row {offset}"
        sku_id = _text(raw.get("sku_id"), row_label, "sku_id")
        quantity = _positive(raw.get("quantity"), row_label, "quantity")
        depot_id = _text(raw.get("depot_id"), row_label, "depot_id")
        truck = _optional_text(raw.get("truck_id"))
        group = _optional_text(raw.get("group_id"))
        if truck == "" and group == "":
            raise ValueError(f"{row_label} needs a truck_id or a group_id.")
        departure = raw.get("departure_time")
        departure_missing = _is_missing(departure) or str(departure).strip() == ""
        departure_sort: Tuple = (1, 0.0)
        if not departure_missing:
            departure_sort = (0, _departure_value(departure))
        must = bool(_is_truthy_flag(raw.get("must_ship")))
        rows.append(
            {
                "sku_id": sku_id,
                "quantity": quantity,
                "depot_id": depot_id,
                "travel_depot": depot_id,
                "truck_id": truck or group,
                "destination_id": _optional_text(raw.get("destination_id")),
                "departure_time": "" if departure_missing else departure,
                "source_order_id": _optional_text(raw.get("source_order_id")),
                "must_ship_text": _optional_text(raw.get("must_ship")),
                "account_tier": _optional_text(raw.get("account_tier")),
                "source_row": offset,
                "sort_key": (departure_sort, 0 if must else 1, offset),
            }
        )
    if not rows:
        raise ValueError(f"{label} has no demand rows.")
    return rows


def _read_stock(frame: pd.DataFrame, label: str) -> List[_Stock]:
    frame = frame.copy()
    frame.columns = [str(name).strip() for name in frame.columns]
    required = ("sku_id", "location_id", "batch_id", "pallet_id", "qty", "location_type")
    missing = [name for name in required if name not in frame.columns]
    if missing:
        raise ValueError(f"{label} is missing column(s): {', '.join(missing)}.")
    rows: List[_Stock] = []
    for offset, raw in enumerate(frame.to_dict(orient="records"), start=2):
        row_label = f"{label} row {offset}"
        location_type = _text(raw.get("location_type"), row_label, "location_type").lower()
        if location_type not in {"bulk", "pickface"}:
            raise ValueError(f"{row_label} column location_type must be bulk or pickface.")
        rows.append(
            _Stock(
                sku_id=_text(raw.get("sku_id"), row_label, "sku_id"),
                location_id=_text(raw.get("location_id"), row_label, "location_id"),
                batch_id=_optional_text(raw.get("batch_id")),
                pallet_id=_optional_text(raw.get("pallet_id")),
                qty=_nonnegative(raw.get("qty"), row_label, "qty"),
                expiry_key=_expiry_key(raw.get("expiry") if "expiry" in frame.columns else None),
                location_type=location_type,
            )
        )
    return rows


def _read_master(frame: pd.DataFrame, label: str) -> Dict[str, dict]:
    frame = frame.copy()
    frame.columns = [str(name).strip() for name in frame.columns]
    required = ("sku_id", "unit_weight_kg", "unit_volume_m3")
    missing = [name for name in required if name not in frame.columns]
    if missing:
        raise ValueError(f"{label} is missing column(s): {', '.join(missing)}.")
    found: Dict[str, dict] = {}
    for offset, raw in enumerate(frame.to_dict(orient="records"), start=2):
        row_label = f"{label} row {offset}"
        sku_id = _text(raw.get("sku_id"), row_label, "sku_id")
        if sku_id in found:
            raise ValueError(f"{row_label} repeats sku_id {sku_id}.")
        case_qty = 1.0
        if "case_qty" in frame.columns and not _blank(raw.get("case_qty")):
            case_qty = _positive(raw.get("case_qty"), row_label, "case_qty")
        pallet_qty = None
        if "pallet_qty" in frame.columns and not _blank(raw.get("pallet_qty")):
            pallet_qty = _positive(raw.get("pallet_qty"), row_label, "pallet_qty")
        found[sku_id] = {
            "unit_weight_kg": _positive(raw.get("unit_weight_kg"), row_label, "unit_weight_kg"),
            "unit_volume_m3": _positive(raw.get("unit_volume_m3"), row_label, "unit_volume_m3"),
            "case_qty": case_qty,
            "pallet_qty": pallet_qty,
        }
    if not found:
        raise ValueError(f"{label} has no SKU rows.")
    return found


def _expiry_key(value: object) -> tuple:
    if _blank(value):
        return (1, "")
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return (0, str(value).strip())
    if not math.isfinite(number):
        return (1, "")
    return (0, number)


def _departure_value(value: object) -> float:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(number):
        return 0.0
    return number


def _blank(value: object) -> bool:
    if _is_missing(value):
        return True
    text = str(value).strip()
    return text == "" or text.lower() == "nan"


def _optional_text(value: object) -> str:
    if _blank(value):
        return ""
    return str(value).strip()


def _text(value: object, label: str, column: str) -> str:
    if _blank(value):
        raise ValueError(f"{label} column {column} is blank.")
    return str(value).strip()


def _positive(value: object, label: str, column: str) -> float:
    number = _number(value, label, column)
    if number <= 0:
        raise ValueError(f"{label} column {column} must be a finite number > 0.")
    return number


def _nonnegative(value: object, label: str, column: str) -> float:
    number = _number(value, label, column)
    if number < 0:
        raise ValueError(f"{label} column {column} must be a finite number >= 0.")
    return number


def _number(value: object, label: str, column: str) -> float:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} column {column} must be a number.") from exc
    if not math.isfinite(number):
        raise ValueError(f"{label} column {column} must be a finite number.")
    return number


def main(argv: Optional[Sequence[str]] = None) -> None:
    """Allocate demand onto stock and exit non-zero when rows are refused."""
    parser = argparse.ArgumentParser(description="Place demand on stock locations. No MIP.")
    parser.add_argument("--demand", type=Path, required=True)
    parser.add_argument("--stock", type=Path, required=True)
    parser.add_argument("--sku-master", type=Path, required=True)
    parser.add_argument("--travel", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--depot", default=None, help="Travel origin when a demand row has no depot match in the matrix.")
    parser.add_argument("--allow-short", action="store_true")
    args = parser.parse_args(list(argv) if argv is not None else None)
    code = allocate_lines(
        pd.read_csv(args.demand),
        pd.read_csv(args.stock),
        pd.read_csv(args.sku_master),
        pd.read_csv(args.travel),
        args.out,
        allow_short=args.allow_short,
        depot=args.depot,
        demand_label=str(args.demand),
        stock_label=str(args.stock),
        master_label=str(args.sku_master),
    )
    if code:
        sys.exit(code)


if __name__ == "__main__":
    main()
