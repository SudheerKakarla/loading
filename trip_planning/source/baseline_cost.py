"""Cheapest truck mix for one shipment, and the cheaper-of-two baseline.

Loads are divisible: a shipment may use several trucks. A trip rate of 0 or
blank means that truck is not offered. Blank handling counts as 0.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping, Sequence

import numpy as np
import pandas as pd
from scipy.optimize import Bounds, LinearConstraint, milp
from scipy.sparse import csr_matrix

from config import TRUCK_TYPES, TripConfig, norm_id

_LOAD_TOL = 1e-6
_COST_TOL = 1e-6
_ENUM_LIMIT = 2_000_000


@dataclass(frozen=True)
class TruckType:
    truck_type: str
    capacity_kg: float | None
    capacity_m3: float | None

    @property
    def has_capacity(self) -> bool:
        return (
            self.capacity_kg is not None
            and self.capacity_m3 is not None
            and self.capacity_kg > 0
            and self.capacity_m3 > 0
            and math.isfinite(self.capacity_kg)
            and math.isfinite(self.capacity_m3)
        )


@dataclass(frozen=True)
class TruckOffer:
    """One truck type a ship-to can buy, with trip rate plus handling."""

    truck_type: str
    capacity_kg: float
    capacity_m3: float
    unit_cost: float


@dataclass(frozen=True)
class RateCard:
    ship_to: str
    trip: Mapping[str, float]
    handling: Mapping[str, float]
    ltl_mt: float | None
    ltl_gt: float | None
    multidrop: bool = False
    service: str | None = None


@dataclass(frozen=True)
class FleetMix:
    cost: float
    counts: tuple[tuple[str, int], ...]
    capacity_kg: float
    capacity_m3: float
    n_trucks: int

    def count_of(self, truck_type: str) -> int:
        return sum(n for name, n in self.counts if name == truck_type)


def _rank(truck_type: str) -> tuple[int, str]:
    try:
        return (TRUCK_TYPES.index(truck_type), truck_type)
    except ValueError:
        return (len(TRUCK_TYPES), truck_type)


def _alone_count(weight: float, volume: float, offer: TruckOffer) -> int:
    need_kg = 0 if weight <= _LOAD_TOL else math.ceil((weight - _LOAD_TOL) / offer.capacity_kg)
    need_m3 = 0 if volume <= _LOAD_TOL else math.ceil((volume - _LOAD_TOL) / offer.capacity_m3)
    return max(need_kg, need_m3)


def cheapest_fleet(
    weight_kg: float,
    volume_m3: float,
    offers: Sequence[TruckOffer],
) -> FleetMix | None:
    """Minimum-cost multiset of trucks that covers both kg and m3.

    Returns an empty mix of cost 0 when both loads are 0. Returns None when
    no offered truck has capacity. Ties prefer fewer trucks, then more of the
    smaller truck type.
    """
    weight = float(weight_kg)
    volume = float(volume_m3)
    if weight < -_LOAD_TOL or volume < -_LOAD_TOL:
        raise ValueError("Load weight and volume must be >= 0.")
    weight = max(0.0, weight)
    volume = max(0.0, volume)
    usable = sorted(
        (
            offer
            for offer in offers
            if offer.capacity_kg > 0
            and offer.capacity_m3 > 0
            and math.isfinite(offer.unit_cost)
            and offer.unit_cost >= 0
        ),
        key=lambda offer: _rank(offer.truck_type),
    )
    if weight <= _LOAD_TOL and volume <= _LOAD_TOL:
        return FleetMix(0.0, (), 0.0, 0.0, 0)
    if not usable:
        return None

    bounds = [_alone_count(weight, volume, offer) for offer in usable]
    product = 1
    for bound in bounds:
        product *= bound + 1
        if product > _ENUM_LIMIT:
            return _fleet_milp(weight, volume, usable, bounds)
    return _fleet_enumerate(weight, volume, usable, bounds)


def _mix_from_counts(
    cost: float,
    counts: Sequence[int],
    usable: Sequence[TruckOffer],
) -> FleetMix:
    pairs: list[tuple[str, int]] = []
    cap_kg = 0.0
    cap_m3 = 0.0
    n_trucks = 0
    for offer, count in zip(usable, counts):
        if count <= 0:
            continue
        pairs.append((offer.truck_type, int(count)))
        cap_kg += count * offer.capacity_kg
        cap_m3 += count * offer.capacity_m3
        n_trucks += int(count)
    return FleetMix(float(cost), tuple(pairs), cap_kg, cap_m3, n_trucks)


def _better_mix(cost: float, counts: tuple[int, ...], best: tuple[float, tuple[int, ...]] | None) -> bool:
    if best is None:
        return True
    best_cost, best_counts = best
    if cost < best_cost - _COST_TOL:
        return True
    if abs(cost - best_cost) > _COST_TOL:
        return False
    if sum(counts) != sum(best_counts):
        return sum(counts) < sum(best_counts)
    return tuple(-count for count in counts) < tuple(-count for count in best_counts)


def _fleet_enumerate(
    weight: float,
    volume: float,
    usable: Sequence[TruckOffer],
    bounds: Sequence[int],
) -> FleetMix | None:
    best: tuple[float, tuple[int, ...]] | None = None

    def consider(cost: float, partial: Sequence[int]) -> None:
        nonlocal best
        full = tuple(partial) + (0,) * (len(usable) - len(partial))
        if _better_mix(cost, full, best):
            best = (cost, full)

    for index, offer in enumerate(usable):
        count = bounds[index]
        if count > 0:
            seed = [0] * index + [count]
            consider(count * offer.unit_cost, seed)

    def rec(index: int, got_kg: float, got_m3: float, cost: float, partial: list[int]) -> None:
        if got_kg + _LOAD_TOL >= weight and got_m3 + _LOAD_TOL >= volume:
            consider(cost, partial)
            return
        if index == len(usable) or (best is not None and cost > best[0] + _COST_TOL):
            return
        max_kg = got_kg
        max_m3 = got_m3
        for later in range(index, len(usable)):
            max_kg += bounds[later] * usable[later].capacity_kg
            max_m3 += bounds[later] * usable[later].capacity_m3
        if max_kg + _LOAD_TOL < weight or max_m3 + _LOAD_TOL < volume:
            return
        offer = usable[index]
        for count in range(0, bounds[index] + 1):
            next_cost = cost + count * offer.unit_cost
            if best is not None and next_cost > best[0] + _COST_TOL:
                break
            rec(index + 1, got_kg + count * offer.capacity_kg, got_m3 + count * offer.capacity_m3, next_cost, partial + [count])

    rec(0, 0.0, 0.0, 0.0, [])
    if best is None:
        return None
    return _mix_from_counts(best[0], best[1], usable)


def _fleet_milp(
    weight: float,
    volume: float,
    usable: Sequence[TruckOffer],
    bounds: Sequence[int],
) -> FleetMix | None:
    width = len(usable)
    cap_kg = np.array([offer.capacity_kg for offer in usable], dtype=float)
    cap_m3 = np.array([offer.capacity_m3 for offer in usable], dtype=float)
    matrix = csr_matrix(np.vstack([cap_kg, cap_m3]))
    result = milp(
        c=np.array([offer.unit_cost for offer in usable], dtype=float),
        integrality=np.ones(width, dtype=int),
        bounds=Bounds(np.zeros(width), np.array(bounds, dtype=float)),
        constraints=LinearConstraint(matrix, [weight, volume], [np.inf, np.inf]),
        options={"disp": False, "mip_rel_gap": 0.0, "time_limit": 30.0, "presolve": True},
    )
    if result.x is None or int(result.status) not in {0, 1}:
        return None
    counts = [int(round(float(value))) for value in result.x]
    cost = sum(count * offer.unit_cost for count, offer in zip(counts, usable))
    return _mix_from_counts(cost, counts, usable)


def column_map(frame: pd.DataFrame) -> dict[str, str]:
    return {str(column).strip().lower(): column for column in frame.columns}


def _cell(record: Mapping, columns: Mapping[str, str], name: str):
    source = columns.get(name)
    if source is None:
        return None
    return record[source]


def parse_number(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        if text.lower() in {"", "nan", "none", "<na>"}:
            return None
        value = text
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"Expected a finite number, got {value!r}.")
    return number


def _trip_rate(value: object, ship_to: str, truck_type: str) -> float | None:
    """A blank or zero trip rate means the truck is not offered."""
    number = parse_number(value)
    if number is None or abs(number) <= _COST_TOL:
        return None
    if number < 0:
        raise ValueError(f"Ship-to {ship_to} has a negative {truck_type} trip rate.")
    return number


def _handling(value: object, ship_to: str, truck_type: str) -> float:
    number = parse_number(value)
    if number is None:
        return 0.0
    if number < 0:
        raise ValueError(f"Ship-to {ship_to} has a negative {truck_type} handling charge.")
    return number


def _flag(value: object) -> bool:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return False
    text = str(value).strip().lower()
    return text in {"1", "true", "yes", "y"}


def parse_trucks(frame: pd.DataFrame) -> list[TruckType]:
    columns = column_map(frame)
    missing = [name for name in ("truck_type", "capacity_kg", "capacity_m3") if name not in columns]
    if missing:
        raise ValueError("trucks.csv is missing columns: " + ", ".join(missing))
    trucks: list[TruckType] = []
    seen: set[str] = set()
    for record in frame.to_dict("records"):
        truck_type = norm_id(_cell(record, columns, "truck_type")).upper()
        if not truck_type:
            continue
        if truck_type in seen:
            raise ValueError(f"Duplicate truck type {truck_type}.")
        seen.add(truck_type)
        trucks.append(
            TruckType(
                truck_type,
                parse_number(_cell(record, columns, "capacity_kg")),
                parse_number(_cell(record, columns, "capacity_m3")),
            )
        )
    if not trucks:
        raise ValueError("trucks.csv has no truck types.")
    return trucks


def parse_rates(frame: pd.DataFrame) -> dict[str, RateCard]:
    columns = column_map(frame)
    if "ship_to" not in columns:
        raise ValueError("rates.csv is missing column ship_to.")
    cards: dict[str, RateCard] = {}
    for record in frame.to_dict("records"):
        ship_to = norm_id(_cell(record, columns, "ship_to"))
        if not ship_to:
            continue
        if ship_to in cards:
            raise ValueError(f"Duplicate rate row for ship-to {ship_to}.")
        trip = {}
        handling = {}
        for truck_type in TRUCK_TYPES:
            rate = _trip_rate(_cell(record, columns, f"trip_{truck_type.lower()}"), ship_to, truck_type)
            if rate is not None:
                trip[truck_type] = rate
            handling[truck_type] = _handling(
                _cell(record, columns, f"handling_{truck_type.lower()}"),
                ship_to,
                truck_type,
            )
        service_raw = _cell(record, columns, "service")
        service = None if service_raw is None else str(service_raw).strip().upper() or None
        cards[ship_to] = RateCard(
            ship_to=ship_to,
            trip=trip,
            handling=handling,
            ltl_mt=parse_number(_cell(record, columns, "ltl_mt")),
            ltl_gt=parse_number(_cell(record, columns, "ltl_gt")),
            multidrop=_flag(_cell(record, columns, "multidrop")),
            service=service,
        )
    return cards


def offers_for(rate: RateCard, trucks: Sequence[TruckType]) -> list[TruckOffer]:
    offers = []
    for truck in trucks:
        if not truck.has_capacity or truck.truck_type not in rate.trip:
            continue
        offers.append(
            TruckOffer(
                truck.truck_type,
                float(truck.capacity_kg),
                float(truck.capacity_m3),
                float(rate.trip[truck.truck_type]) + float(rate.handling.get(truck.truck_type, 0.0)),
            )
        )
    return offers


def case_rate_of(rate: RateCard) -> float | None:
    """MT wins when both case-rate columns are filled."""
    if rate.ltl_mt is not None:
        return float(rate.ltl_mt)
    if rate.ltl_gt is not None:
        return float(rate.ltl_gt)
    return None


def format_truck_mix(counts: Sequence[tuple[str, int]], price_basis: str) -> str:
    parts = []
    for truck_type, count in counts:
        if count <= 0:
            continue
        parts.append(truck_type if count == 1 else f"{count}x{truck_type}")
    if parts:
        return "+".join(parts)
    if price_basis == "case":
        return "case"
    return ""


def build_baseline(
    shipments: pd.DataFrame,
    rates: Mapping[str, RateCard],
    trucks: Sequence[TruckType],
) -> tuple[pd.DataFrame, dict[str, float | int | str]]:
    """Price each shipment both as trucks and as LTL cases.

    ``all_trips_cost`` sends every shipment as its cheapest fleet.
    ``ltl_case_mix_cost`` sends FTL as trucks and LTL as cases.
    ``cheaper_cost`` is FTL as trucks and LTL as the cheaper of the two.
    Truck counts on the summary are the all-trips fleet.
    """
    from pricing import solo_quote

    detail_rows = []
    all_trips = 0.0
    case_mix = 0.0
    cheaper = 0.0
    cheaper_ftl = 0.0
    cheaper_ltl = 0.0
    all_kg = 0.0
    all_m3 = 0.0
    all_cap_kg = 0.0
    all_cap_m3 = 0.0
    truck_counts = {truck_type: 0 for truck_type in TRUCK_TYPES}
    cheaper_counts = {truck_type: 0 for truck_type in TRUCK_TYPES}
    ltl_case_below = 0
    ltl_case_above = 0
    ltl_case_equal = 0
    above_cases: list[float] = []
    unpriced = 0

    for record in shipments.to_dict("records"):
        ship_to = norm_id(record["ship_to"])
        rate = rates.get(ship_to)
        service = str(record.get("service", "")).strip().upper()
        cases = float(record["cases"])
        weight = float(record["weight_kg"])
        volume = float(record["volume_m3"])
        quote = None if rate is None else solo_quote(service, cases, weight, volume, rate, trucks)
        if quote is None:
            unpriced += 1
            detail_rows.append(_baseline_row(record, None))
            continue
        detail_rows.append(_baseline_row(record, quote))
        if quote.fleet_cost is not None and quote.fleet is not None:
            all_trips += quote.fleet_cost
            all_kg += weight
            all_m3 += volume
            all_cap_kg += quote.fleet.capacity_kg
            all_cap_m3 += quote.fleet.capacity_m3
            for truck_type, count in quote.fleet.counts:
                truck_counts[truck_type] = truck_counts.get(truck_type, 0) + count
        if service == "LTL" and quote.case_cost is not None:
            case_mix += quote.case_cost
        elif quote.fleet_cost is not None:
            case_mix += quote.fleet_cost
        cheaper += quote.cost
        if service == "LTL":
            cheaper_ltl += quote.cost
        else:
            cheaper_ftl += quote.cost
        if quote.price_basis == "truck" and quote.fleet is not None:
            for truck_type, count in quote.fleet.counts:
                cheaper_counts[truck_type] = cheaper_counts.get(truck_type, 0) + count
        if service == "LTL" and quote.case_cost is not None and quote.fleet_cost is not None:
            if quote.case_cost < quote.fleet_cost - _COST_TOL:
                ltl_case_below += 1
            elif quote.case_cost > quote.fleet_cost + _COST_TOL:
                ltl_case_above += 1
                above_cases.append(cases)
            else:
                ltl_case_equal += 1

    above_cases.sort()
    median_above = above_cases[len(above_cases) // 2] if above_cases else 0
    summary: dict[str, float | int | str] = {
        "shipments": len(detail_rows),
        "all_trips_cost": all_trips,
        "ltl_case_mix_cost": case_mix,
        "cheaper_cost": cheaper,
        "cheaper_ftl_cost": cheaper_ftl,
        "cheaper_ltl_cost": cheaper_ltl,
        "all_trips_trucks": sum(truck_counts.values()),
        "all_trips_util_kg": (all_kg / all_cap_kg) if all_cap_kg else 0.0,
        "all_trips_util_m3": (all_m3 / all_cap_m3) if all_cap_m3 else 0.0,
        "ltl_case_below_truck": ltl_case_below,
        "ltl_case_above_truck": ltl_case_above,
        "ltl_case_equal_truck": ltl_case_equal,
        "ltl_case_above_median_cases": median_above,
        "unpriced_shipments": unpriced,
    }
    for truck_type, count in truck_counts.items():
        summary[f"all_trips_{truck_type}"] = count
    for truck_type, count in cheaper_counts.items():
        summary[f"cheaper_{truck_type}"] = count
    return pd.DataFrame(detail_rows), summary


def _baseline_row(record: Mapping, quote) -> dict:
    fleet = None if quote is None else quote.fleet
    row = {
        "shipment_id": record.get("shipment_id", ""),
        "ship_to": norm_id(record.get("ship_to")),
        "rdd": record.get("rdd", ""),
        "cases": record.get("cases", ""),
        "weight_kg": record.get("weight_kg", ""),
        "volume_m3": record.get("volume_m3", ""),
        "service": record.get("service", ""),
        "case_cost": None if quote is None else quote.case_cost,
        "fleet_cost": None if quote is None else quote.fleet_cost,
        "baseline_cost": None if quote is None else quote.cost,
        "basis": None if quote is None else quote.price_basis,
        "n_trucks": 0 if fleet is None else fleet.n_trucks,
        "truck_mix": "" if quote is None else format_truck_mix(
            () if fleet is None else fleet.counts,
            quote.price_basis,
        ),
    }
    for truck_type in TRUCK_TYPES:
        row[f"n_{truck_type}"] = 0 if fleet is None else fleet.count_of(truck_type)
    return row


def summary_frame(summary: Mapping[str, float | int | str]) -> pd.DataFrame:
    return pd.DataFrame(
        [{"metric": key, "value": _format_metric(value)} for key, value in summary.items()]
    )


def _format_metric(value: object) -> str:
    if isinstance(value, float):
        if math.isfinite(value) and abs(value - round(value)) < 1e-6:
            return str(int(round(value)))
        return f"{value:.6f}".rstrip("0").rstrip(".")
    return str(value)


def write_baseline(shipments: pd.DataFrame, rates, trucks, out_dir) -> dict:
    from pathlib import Path

    detail, summary = build_baseline(shipments, rates, trucks)
    destination = Path(out_dir)
    destination.mkdir(parents=True, exist_ok=True)
    detail.to_csv(destination / "baseline_shipments.csv", index=False)
    summary_frame(summary).to_csv(destination / "baseline_summary.csv", index=False)
    return summary


def main(argv: list[str] | None = None) -> int:
    import argparse
    from pathlib import Path

    from shipments import build_shipments, read_inputs

    parser = argparse.ArgumentParser(description="Cheaper-of-two baseline for trip-planning inputs.")
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    tables = read_inputs(args.inputs)
    built = build_shipments(tables.orders, tables.customers, tables.rates, tables.trucks, TripConfig())
    summary = write_baseline(built.shipments, parse_rates(tables.rates), parse_trucks(tables.trucks), args.out)
    print(f"Cheaper-of-two baseline: {summary['cheaper_cost']} THB")
    print(f"All trips per truck: {summary['all_trips_cost']} THB")
    print(f"Wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
