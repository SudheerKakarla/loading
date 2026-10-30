"""Cost rules for one truck and for a shipment's own fleet. No file I/O.

Section 6.2 is one ship-to on one truck. Section 6.3 is the multi-drop rule:
the truck price is the max trip rate plus handling, and an all-LTL trip takes
the cheaper of that truck price and the case price. Section 6.4 prices a
shipment alone, possibly on several trucks.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Protocol, Sequence

from baseline_cost import (
    FleetMix,
    RateCard,
    TruckType,
    case_rate_of,
    cheapest_fleet,
    offers_for,
)
from config import TripConfig


class _Member(Protocol):
    ship_to: str
    service: str
    cases: float


@dataclass(frozen=True)
class SoloQuote:
    cost: float
    price_basis: str
    case_cost: float | None
    fleet_cost: float | None
    fleet: FleetMix | None


def solo_quote(
    service: str,
    cases: float,
    weight_kg: float,
    volume_m3: float,
    rate: RateCard,
    trucks: Sequence[TruckType],
) -> SoloQuote | None:
    """FTL pays the cheapest fleet. LTL pays the cheaper of case and fleet.

    Equal case and fleet costs stay on the truck, so a real truck is not hidden
    by a tie. A case-priced quote still carries the fleet it declined.
    """
    fleet = cheapest_fleet(weight_kg, volume_m3, offers_for(rate, trucks))
    fleet_cost = None if fleet is None else float(fleet.cost)
    rate_per_case = case_rate_of(rate)
    case_cost = None if rate_per_case is None else float(rate_per_case) * float(cases)
    if str(service).upper() == "LTL" and case_cost is not None and (
        fleet_cost is None or case_cost < fleet_cost - 1e-9
    ):
        return SoloQuote(float(case_cost), "case", case_cost, fleet_cost, fleet)
    if fleet is None or fleet_cost is None:
        return None
    return SoloQuote(fleet_cost, "truck", case_cost, fleet_cost, fleet)


def solo_cost(
    shipment,
    rate: RateCard,
    trucks: Sequence[TruckType],
    cfg: TripConfig | None = None,
) -> SoloQuote | None:
    """Public wrapper around ``solo_quote``. ``cfg`` is unused: solo price has no date."""
    del cfg
    return solo_quote(
        shipment.service,
        shipment.cases,
        shipment.weight_kg,
        shipment.volume_m3,
        rate,
        trucks,
    )


def trip_cost(
    members: Sequence[_Member],
    truck_type: str,
    rates: Mapping[str, RateCard],
    cfg: TripConfig,
) -> tuple[float, str] | None:
    """Price one truck carrying these shipments.

    Returns None when any distinct ship-to does not offer ``truck_type``.
    Handling is summed across ship-tos under ``per_drop``, or the max handling
    under ``per_truck``. Two shipments for the same ship-to are one drop.
    """
    if not members:
        raise ValueError("trip_cost needs at least one shipment.")
    trip_rates: list[float] = []
    handlings: list[float] = []
    seen: set[str] = set()
    for member in members:
        rate = rates[member.ship_to]
        if truck_type not in rate.trip:
            return None
        if member.ship_to in seen:
            continue
        seen.add(member.ship_to)
        trip_rates.append(float(rate.trip[truck_type]))
        handlings.append(float(rate.handling.get(truck_type, 0.0)))
    truck_cost = max(trip_rates) + (_handling_charge(handlings, cfg))
    if any(str(member.service).upper() != "LTL" for member in members):
        return truck_cost, "truck"
    case_total = 0.0
    for member in members:
        per_case = case_rate_of(rates[member.ship_to])
        if per_case is None:
            return truck_cost, "truck"
        case_total += float(per_case) * float(member.cases)
    if case_total < truck_cost - 1e-9:
        return case_total, "case"
    return truck_cost, "truck"


def _handling_charge(handlings: Sequence[float], cfg: TripConfig) -> float:
    if not handlings:
        return 0.0
    if cfg.handling_basis == "per_truck":
        return max(handlings)
    return sum(handlings)
