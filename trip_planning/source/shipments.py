"""Group orders into shipments (one ship-to, one RDD) and set aside bad rows.

Data-quality exclusions never reach the MIP. Priority, first match wins:

1. ``no_master_customer`` — ship-to is blank or missing from the customer master.
2. ``zero_quantity`` — cases are 0.
3. ``no_rate`` — ship-to has no rate row.
4. ``truck_type_not_offered`` — no usable truck, and no LTL case rate either.

Past-due shipments stay in the shipment table. The MIP leaves them unplanned.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import NamedTuple

import pandas as pd

from baseline_cost import (
    TruckType,
    case_rate_of,
    column_map,
    offers_for,
    parse_number,
    parse_rates,
    parse_trucks,
)
from config import TripConfig, norm_id

_QTY_TOL = 1e-9


class ShipmentBuild(NamedTuple):
    """``lines`` are the orders that passed the data-quality filters, one row each."""

    shipments: pd.DataFrame
    exclusions: pd.DataFrame
    lines: pd.DataFrame
    input_cases: float
    input_kg: float
    input_m3: float
    order_ids: tuple[str, ...]


@dataclass(frozen=True)
class InputTables:
    orders: pd.DataFrame
    customers: pd.DataFrame
    rates: pd.DataFrame
    trucks: pd.DataFrame
    overrides: pd.DataFrame | None = None


@dataclass(frozen=True)
class Shipment:
    shipment_id: str
    ship_to: str
    rdd: date
    cases: float
    weight_kg: float
    volume_m3: float
    release_date: date
    service: str
    province: str
    customer_group: str
    order_ids: tuple[str, ...]


def read_inputs(path: Path) -> InputTables:
    """Read the four contract CSVs, plus overrides when that file is present."""
    folder = Path(path)
    required = ("orders.csv", "customers.csv", "rates.csv", "trucks.csv")
    missing = [name for name in required if not (folder / name).is_file()]
    if missing:
        raise FileNotFoundError(
            f"Missing {', '.join(missing)} in {folder}. "
            "The planner needs orders.csv, customers.csv, rates.csv, and trucks.csv."
        )
    orders = _read_csv(folder / "orders.csv")
    customers = _read_csv(folder / "customers.csv")
    rates = _read_csv(folder / "rates.csv")
    trucks = _read_csv(folder / "trucks.csv")
    override_path = folder / "consolidation_overrides.csv"
    overrides = _read_csv(override_path) if override_path.is_file() else None
    return InputTables(orders, customers, rates, trucks, overrides)


def build_shipments(
    orders: pd.DataFrame,
    customers: pd.DataFrame,
    rates: pd.DataFrame,
    trucks: pd.DataFrame,
    cfg: TripConfig | None = None,
) -> ShipmentBuild:
    """Group by ship-to and RDD. ``cfg`` does not change exclusions; dates are applied later."""
    if cfg is None:
        cfg = TripConfig()
    if not isinstance(cfg, TripConfig):
        raise TypeError("cfg must be a TripConfig.")

    order_rows = _order_rows(orders)
    customer_rows = _customer_rows(customers)
    rate_cards = parse_rates(rates)
    truck_types = parse_trucks(trucks)
    customers_by_id = {row["ship_to"]: row for row in customer_rows}

    exclusions: list[dict] = []
    kept: list[dict] = []
    input_cases = 0.0
    input_kg = 0.0
    input_m3 = 0.0
    seen_orders: set[str] = set()
    for row in order_rows:
        if row["order_id"] in seen_orders:
            raise ValueError(f"Duplicate order_id {row['order_id']}.")
        seen_orders.add(row["order_id"])
        input_cases += row["cases"]
        input_kg += row["weight_kg"]
        input_m3 += row["volume_m3"]
        reason = _exclusion_reason(row, customers_by_id, rate_cards, truck_types)
        if reason is not None:
            exclusions.append(
                {
                    "order_id": row["order_id"],
                    "shipment_id": "",
                    "category": "data_quality",
                    "reason": reason,
                    "cases": row["cases"],
                    "kg": row["weight_kg"],
                    "m3": row["volume_m3"],
                }
            )
            continue
        customer = customers_by_id[row["ship_to"]]
        rate = rate_cards[row["ship_to"]]
        row["service"] = resolve_service(customer["service"], rate.service)
        row["province"] = customer["province"]
        row["group"] = customer["group"]
        row["doc_date"] = _require_date(row["doc_date"], row["order_id"], "doc_date")
        row["rdd"] = _require_date(row["rdd"], row["order_id"], "rdd")
        kept.append(row)

    grouped: dict[tuple[str, date], list[dict]] = {}
    for row in kept:
        grouped.setdefault((row["ship_to"], row["rdd"]), []).append(row)

    shipment_rows = []
    lines = []
    for (ship_to, rdd), members in sorted(grouped.items(), key=lambda item: (item[0][0], item[0][1])):
        members = sorted(members, key=lambda item: item["order_id"])
        shipment_id = f"shp_{ship_to}_{rdd.isoformat()}"
        order_ids = [member["order_id"] for member in members]
        shipment_rows.append(
            {
                "shipment_id": shipment_id,
                "ship_to": ship_to,
                "rdd": rdd,
                "cases": sum(member["cases"] for member in members),
                "weight_kg": sum(member["weight_kg"] for member in members),
                "volume_m3": sum(member["volume_m3"] for member in members),
                "release_date": max(member["doc_date"] for member in members),
                "service": members[0]["service"],
                "province": members[0]["province"],
                "group": members[0]["group"],
                "order_ids": ";".join(order_ids),
            }
        )
        for member in members:
            lines.append(
                {
                    "order_id": member["order_id"],
                    "shipment_id": shipment_id,
                    "ship_to": ship_to,
                    "doc_date": member["doc_date"],
                    "rdd": rdd,
                    "cases": member["cases"],
                    "weight_kg": member["weight_kg"],
                    "volume_m3": member["volume_m3"],
                }
            )

    return ShipmentBuild(
        shipments=_frame(shipment_rows, _SHIPMENT_COLUMNS),
        exclusions=_frame(exclusions, _EXCLUSION_COLUMNS),
        lines=_frame(lines, _LINE_COLUMNS),
        input_cases=input_cases,
        input_kg=input_kg,
        input_m3=input_m3,
        order_ids=tuple(sorted(seen_orders)),
    )


def build_flags(
    orders: pd.DataFrame,
    customers: pd.DataFrame,
    rates: pd.DataFrame,
    trucks: pd.DataFrame,
) -> pd.DataFrame:
    """Warnings that do not, by themselves, drop an order from the plan."""
    flags: list[dict] = []
    rate_cards = parse_rates(rates)
    truck_types = parse_trucks(trucks)
    customers_by_id = {row["ship_to"]: row for row in _customer_rows(customers)}

    for row in _order_rows(orders):
        customer = customers_by_id.get(row["ship_to"])
        if customer is None or customer["sla_days"] is None:
            continue
        doc_date = _optional_date(row["doc_date"])
        rdd = _optional_date(row["rdd"])
        if doc_date is None or rdd is None:
            continue
        lead = (rdd - doc_date).days
        if lead < customer["sla_days"]:
            flags.append(
                {
                    "flag": "rdd_tighter_than_sla",
                    "key": row["order_id"],
                    "detail": f"lead {lead}d < sla {customer['sla_days']}d",
                }
            )

    for ship_to, customer in sorted(customers_by_id.items()):
        rate = rate_cards.get(ship_to)
        customer_service = _clean_service(customer["service"])
        rate_service = "" if rate is None or rate.service is None else _clean_service(rate.service)
        if customer_service and rate_service and customer_service != rate_service:
            flags.append(
                {
                    "flag": "service_conflict",
                    "key": ship_to,
                    "detail": (
                        f"customer {customer_service} vs rate card {rate_service}; "
                        "customer service is used"
                    ),
                }
            )
        if not customer_service and not rate_service:
            flags.append(
                {
                    "flag": "service_missing",
                    "key": ship_to,
                    "detail": "no service on the customer or the rate card; priced as FTL",
                }
            )

    for rate in rate_cards.values():
        if rate.multidrop:
            flags.append(
                {
                    "flag": "multidrop_priced_as_max_trip",
                    "key": rate.ship_to,
                    "detail": "multidrop remark; trip price is the max trip rate",
                }
            )

    flagged_blank: set[str] = set()
    for truck in truck_types:
        if truck.has_capacity:
            continue
        flagged_blank.add(truck.truck_type)
        flags.append(
            {
                "flag": "truck_without_capacity",
                "key": truck.truck_type,
                "detail": "capacity_kg or capacity_m3 is blank; truck type is not used",
            }
        )
    for truck_type in _rated_truck_types(rate_cards):
        if truck_type in flagged_blank:
            continue
        known = {truck.truck_type for truck in truck_types}
        if truck_type not in known:
            flags.append(
                {
                    "flag": "truck_without_capacity",
                    "key": truck_type,
                    "detail": "a trip rate exists and trucks.csv has no capacity for this type",
                }
            )
    for truck_type, detail in _suspicious_capacity(truck_types):
        flags.append({"flag": "capacity_suspicious", "key": truck_type, "detail": detail})

    return _frame(flags, ["flag", "key", "detail"]).sort_values(
        ["flag", "key"], kind="mergesort"
    ).reset_index(drop=True)


def to_shipments(frame: pd.DataFrame) -> list[Shipment]:
    shipments = []
    for record in frame.to_dict("records"):
        order_ids = tuple(
            norm_id(part) for part in str(record.get("order_ids", "")).split(";") if norm_id(part)
        )
        shipments.append(
            Shipment(
                shipment_id=norm_id(record["shipment_id"]),
                ship_to=norm_id(record["ship_to"]),
                rdd=_require_date(record["rdd"], record.get("shipment_id", ""), "rdd"),
                cases=float(record["cases"]),
                weight_kg=float(record["weight_kg"]),
                volume_m3=float(record["volume_m3"]),
                release_date=_require_date(
                    record["release_date"], record.get("shipment_id", ""), "release_date"
                ),
                service=resolve_service(record.get("service"), None),
                province="" if record.get("province") is None else str(record["province"]).strip(),
                customer_group="" if record.get("group") is None else str(record["group"]).strip(),
                order_ids=order_ids,
            )
        )
    return shipments


def resolve_service(customer_service: object, rate_service: object) -> str:
    """Customer master wins. A blank or unknown service is priced as FTL."""
    chosen = _clean_service(customer_service) or _clean_service(rate_service)
    if chosen in {"FTL", "LTL"}:
        return chosen
    return "FTL"


def _exclusion_reason(row: dict, customers: dict, rates: dict, trucks: list[TruckType]) -> str | None:
    ship_to = row["ship_to"]
    if not ship_to or ship_to not in customers:
        return "no_master_customer"
    if row["cases"] <= _QTY_TOL:
        return "zero_quantity"
    rate = rates.get(ship_to)
    if rate is None:
        return "no_rate"
    service = resolve_service(customers[ship_to]["service"], rate.service)
    has_case = service == "LTL" and case_rate_of(rate) is not None
    if not offers_for(rate, trucks) and not has_case:
        return "truck_type_not_offered"
    return None


def _suspicious_capacity(trucks: list[TruckType]) -> list[tuple[str, str]]:
    """Flag a truck whose m3 per kg is at least 1.5 times another truck's ratio."""
    ratios = [
        (truck.truck_type, float(truck.capacity_m3) / float(truck.capacity_kg))
        for truck in trucks
        if truck.has_capacity
    ]
    found = []
    for truck_type, ratio in ratios:
        comparisons = []
        for other, other_ratio in ratios:
            if other == truck_type or other_ratio <= 0:
                continue
            if ratio >= 1.5 * other_ratio:
                comparisons.append(f"{ratio:.6f} m3/kg is {ratio / other_ratio:.2f}x {other}")
        if comparisons:
            found.append((truck_type, "; ".join(comparisons)))
    return found


def _rated_truck_types(rate_cards: dict) -> set[str]:
    found: set[str] = set()
    for rate in rate_cards.values():
        found.update(rate.trip)
    return found


def _order_rows(frame: pd.DataFrame) -> list[dict]:
    rows = _take(frame, ["order_id", "ship_to", "doc_date", "rdd", "cases", "weight_kg", "volume_m3"], "orders")
    cleaned = []
    for row in rows:
        order_id = norm_id(row["order_id"])
        if not order_id:
            raise ValueError("orders has a row with a blank order_id.")
        cleaned.append(
            {
                "order_id": order_id,
                "ship_to": norm_id(row["ship_to"]),
                "doc_date": row["doc_date"],
                "rdd": row["rdd"],
                "cases": _require_number(row["cases"], order_id, "cases"),
                "weight_kg": _require_number(row["weight_kg"], order_id, "weight_kg"),
                "volume_m3": _require_number(row["volume_m3"], order_id, "volume_m3"),
            }
        )
    return cleaned


def _customer_rows(frame: pd.DataFrame) -> list[dict]:
    rows = _take(
        frame,
        ["ship_to", "province", "group", "service", "sla_days", "name"],
        "customers",
    )
    cleaned = []
    seen: set[str] = set()
    for row in rows:
        ship_to = norm_id(row["ship_to"])
        if not ship_to:
            raise ValueError("customers has a row with a blank ship_to.")
        if ship_to in seen:
            raise ValueError(f"Duplicate customer {ship_to}.")
        seen.add(ship_to)
        sla = parse_number(row["sla_days"])
        cleaned.append(
            {
                "ship_to": ship_to,
                "province": "" if row["province"] is None else str(row["province"]).strip(),
                "group": "" if row["group"] is None else str(row["group"]).strip(),
                "service": "" if row["service"] is None else str(row["service"]).strip(),
                "sla_days": None if sla is None else int(sla),
                "name": "" if row["name"] is None else str(row["name"]).strip(),
            }
        )
    return cleaned


def _take(frame: pd.DataFrame, required: list[str], label: str) -> list[dict]:
    columns = column_map(frame)
    missing = [name for name in required if name not in columns]
    if missing:
        raise ValueError(f"{label} is missing columns: " + ", ".join(missing))
    taken = []
    for record in frame.to_dict("records"):
        taken.append({name: record[columns[name]] for name in required})
    return taken


def _require_number(value: object, order_id: str, field: str) -> float:
    number = parse_number(value)
    if number is None:
        raise ValueError(f"Order {order_id} is missing {field}.")
    if number < 0:
        raise ValueError(f"Order {order_id} has negative {field}.")
    return number


def _require_date(value: object, owner: object, field: str) -> date:
    if value is None or str(value).strip().lower() in {"", "nan", "none", "<na>", "nat"}:
        raise ValueError(f"{owner} is missing {field}.")
    parsed = _optional_date(value)
    if parsed is None:
        raise ValueError(f"{owner} has an invalid {field}: {value!r}.")
    return parsed


def _optional_date(value: object) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if text.lower() in {"", "nan", "none", "<na>", "nat"}:
        return None
    if "T" in text:
        text = text.split("T", 1)[0]
    if " " in text:
        text = text.split(" ", 1)[0]
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _clean_service(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip().upper()


def _read_csv(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, dtype=str, keep_default_na=False, encoding="utf-8-sig")


def _frame(rows: list[dict], columns: list[str]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=columns)
    return pd.DataFrame(rows, columns=columns)


_SHIPMENT_COLUMNS = [
    "shipment_id",
    "ship_to",
    "rdd",
    "cases",
    "weight_kg",
    "volume_m3",
    "release_date",
    "service",
    "province",
    "group",
    "order_ids",
]
_EXCLUSION_COLUMNS = ["order_id", "shipment_id", "category", "reason", "cases", "kg", "m3"]
_LINE_COLUMNS = [
    "order_id",
    "shipment_id",
    "ship_to",
    "doc_date",
    "rdd",
    "cases",
    "weight_kg",
    "volume_m3",
]
