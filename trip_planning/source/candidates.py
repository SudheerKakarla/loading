"""Legal bookings: who may share a truck, on which days, and at what price.

A shipment whose release is after its departure deadline is past due and gets
no candidate. A nonempty window whose dates are all closed is calendar-unavailable.
Without a fleet cap, one solo uses the latest open date and a shared booking is
kept only when it is strictly cheaper than the solos. With a cap, every open
date and every nondominated truck-count alternative is kept.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from itertools import combinations
from typing import Mapping, Sequence

import pandas as pd

from baseline_cost import (
    FleetMix,
    RateCard,
    TruckType,
    fleet_frontier,
    format_truck_mix,
    offers_for,
    parse_rates,
    parse_trucks,
)
from config import TRUCK_TYPES, TripConfig, norm_id
from pricing import solo_cost, trip_cost
from retrospective import PlanningCalendar, empty_calendar, iter_dates
from shipments import Shipment, to_shipments

_COST_TOL = 1e-6


@dataclass(frozen=True)
class Candidate:
    """One column of the set-partitioning model."""

    candidate_id: str
    member_ids: tuple[str, ...]
    cost: float
    ship_date: date
    early_days: int
    upgrade: int
    truck_type: str
    n_trucks: int
    price_basis: str
    counts: tuple[tuple[str, int], ...]
    cases: float
    weight_kg: float
    volume_m3: float
    capacity_kg: float
    capacity_m3: float
    is_solo: bool
    pricing_status: str = "optimal"
    pricing_gap: float = 0.0


@dataclass(frozen=True)
class CandidateSet:
    candidates: tuple[Candidate, ...]
    shipments: tuple[Shipment, ...]
    past_due_ids: tuple[str, ...]
    calendar_unavailable_ids: tuple[str, ...] = ()
    solo_baseline: tuple[tuple[str, float], ...] = ()
    calendar: PlanningCalendar | None = None


def generate_candidates(
    shipments: pd.DataFrame,
    rates: pd.DataFrame | Mapping[str, RateCard],
    trucks: pd.DataFrame | Sequence[TruckType],
    cfg: TripConfig,
    overrides: pd.DataFrame | None = None,
    *,
    planning_calendar: PlanningCalendar | None = None,
) -> CandidateSet:
    """Build solo candidates and the shared bookings the settings allow."""
    shipment_list = to_shipments(shipments)
    rate_cards = parse_rates(rates) if isinstance(rates, pd.DataFrame) else dict(rates)
    truck_types = parse_trucks(trucks) if isinstance(trucks, pd.DataFrame) else list(trucks)
    override_rules = parse_overrides(overrides)
    calendar = planning_calendar if planning_calendar is not None else empty_calendar()
    candidates: list[Candidate] = []
    past_due: list[str] = []
    unavailable: list[str] = []
    dateable: list[Shipment] = []
    solo_baseline: list[tuple[str, float]] = []

    for shipment in shipment_list:
        rate = rate_cards.get(shipment.ship_to)
        if rate is None:
            raise ValueError(f"Shipment {shipment.shipment_id} has no rate card.")
        quote = solo_cost(shipment, rate, truck_types, cfg)
        if quote is None:
            raise ValueError(
                f"Shipment {shipment.shipment_id} has no price. "
                "It should have been excluded before candidate generation."
            )
        if _member_window(shipment, cfg) is None:
            past_due.append(shipment.shipment_id)
            continue
        solo_baseline.append((shipment.shipment_id, float(quote.cost)))
        open_dates = open_departure_dates((shipment,), cfg, calendar)
        if not open_dates:
            unavailable.append(shipment.shipment_id)
            continue
        dateable.append(shipment)
        candidates.extend(_solo_candidates(shipment, quote, rate, truck_types, open_dates, cfg))

    solo_costs = dict(solo_baseline)
    for pool in _components(dateable, cfg, override_rules):
        candidates.extend(
            _consolidated(pool, rate_cards, truck_types, cfg, override_rules, solo_costs, calendar)
        )
    candidates.sort(key=lambda candidate: candidate.candidate_id)
    return CandidateSet(
        tuple(candidates),
        tuple(shipment_list),
        tuple(past_due),
        tuple(unavailable),
        tuple(solo_baseline),
        calendar,
    )


def feasible_ship_date(members: Sequence[Shipment], cfg: TripConfig) -> date | None:
    """Latest common departure inside every member's release, transit, and early window."""
    window = _intersect_windows(members, cfg)
    if window is None:
        return None
    return window[1]


def open_departure_dates(
    members: Sequence[Shipment],
    cfg: TripConfig,
    calendar: PlanningCalendar,
) -> list[date]:
    """Departure dates that meet every member's window, dispatch, and receiving rule."""
    window = _intersect_windows(members, cfg)
    if window is None:
        return []
    open_dates = []
    for day in iter_dates(window[0], window[1]):
        if not calendar.dispatch_open(day):
            continue
        arrivals_open = True
        for member in members:
            arrival = day + timedelta(days=cfg.transit_for(member.province))
            if not calendar.receiving_open(member.ship_to, arrival):
                arrivals_open = False
                break
        if arrivals_open:
            open_dates.append(day)
    return open_dates


def early_days_of(members: Sequence[Shipment], ship_date: date, cfg: TripConfig) -> int:
    total = 0
    for member in members:
        ready = member.rdd - timedelta(days=cfg.transit_for(member.province))
        total += (ready - ship_date).days
    return total


def pair_compatible(
    left: Shipment,
    right: Shipment,
    cfg: TripConfig,
    overrides: Mapping[frozenset[str], str],
) -> bool:
    """Symmetric rule. Sharing denial and ``solo_only`` beat an allow override."""
    if left.shipment_id == right.shipment_id:
        return False
    if left.ship_to in cfg.solo_only or right.ship_to in cfg.solo_only:
        return False
    if not left.allow_sharing or not right.allow_sharing:
        return False
    if left.ship_to != right.ship_to:
        rule = overrides.get(frozenset((left.ship_to, right.ship_to)))
        if rule == "deny":
            return False
        if rule == "allow":
            return True
        return _mode_match(left, right, cfg)
    return True


def parse_overrides(frame: pd.DataFrame | None) -> dict[frozenset[str], str]:
    if frame is None or frame.empty:
        return {}
    columns = {str(column).strip().lower(): column for column in frame.columns}
    missing = [name for name in ("ship_to_a", "ship_to_b", "rule") if name not in columns]
    if missing:
        raise ValueError("consolidation_overrides.csv is missing columns: " + ", ".join(missing))
    rules: dict[frozenset[str], str] = {}
    for record in frame.to_dict("records"):
        left = norm_id(record[columns["ship_to_a"]])
        right = norm_id(record[columns["ship_to_b"]])
        rule = str(record[columns["rule"]]).strip().lower()
        if not left or not right or left == right:
            raise ValueError("Each consolidation override must name two different ship-tos.")
        if rule not in {"allow", "deny"}:
            raise ValueError(f"Override rule for {left} and {right} must be allow or deny.")
        key = frozenset((left, right))
        if key in rules and rules[key] != rule:
            raise ValueError(f"Ship-tos {left} and {right} have both allow and deny.")
        rules[key] = rule
    return rules


def _solo_candidates(shipment, quote, rate, trucks, open_dates, cfg: TripConfig) -> list[Candidate]:
    dates = open_dates if cfg.max_trucks is not None else open_dates[-1:]
    alternatives = _solo_alternatives(shipment, quote, rate, trucks, cfg)
    found = []
    for basis, cost, fleet, status, gap in alternatives:
        for day in dates:
            found.append(_solo_candidate(shipment, basis, cost, fleet, status, gap, day, cfg))
    return found


def _solo_alternatives(shipment: Shipment, quote, rate: RateCard, trucks, cfg: TripConfig) -> list[tuple]:
    if cfg.max_trucks is None:
        fleet = quote.fleet if quote.price_basis == "truck" else None
        return [(quote.price_basis, float(quote.cost), fleet, quote.fleet_status, quote.fleet_gap)]
    found = []
    if shipment.allow_truck:
        for mix in fleet_frontier(shipment.weight_kg, shipment.volume_m3, offers_for(rate, trucks), cfg.max_trucks):
            found.append(("truck", float(mix.cost), mix, mix.status, mix.gap))
    if shipment.service == "LTL" and shipment.allow_case and quote.case_cost is not None:
        found.append(("case", float(quote.case_cost), None, "optimal", 0.0))
    return found


def _solo_candidate(
    shipment: Shipment,
    basis: str,
    cost: float,
    fleet: FleetMix | None,
    status: str,
    gap: float,
    ship_date: date,
    cfg: TripConfig,
) -> Candidate:
    if basis == "truck" and fleet is not None:
        counts = fleet.counts
        capacity_kg = fleet.capacity_kg
        capacity_m3 = fleet.capacity_m3
        n_trucks = fleet.n_trucks
    else:
        counts = ()
        capacity_kg = 0.0
        capacity_m3 = 0.0
        n_trucks = 0
    truck_type = format_truck_mix(counts, basis)
    return Candidate(
        candidate_id=(
            f"solo:{shipment.shipment_id}:{ship_date.isoformat()}:{truck_type}:{n_trucks}:{basis}"
        ),
        member_ids=(shipment.shipment_id,),
        cost=float(cost),
        ship_date=ship_date,
        early_days=early_days_of((shipment,), ship_date, cfg),
        upgrade=0,
        truck_type=truck_type,
        n_trucks=n_trucks,
        price_basis=basis,
        counts=counts,
        cases=shipment.cases,
        weight_kg=shipment.weight_kg,
        volume_m3=shipment.volume_m3,
        capacity_kg=capacity_kg,
        capacity_m3=capacity_m3,
        is_solo=True,
        pricing_status=status or "optimal",
        pricing_gap=float(gap),
    )


def _consolidated(
    pool: Sequence[Shipment],
    rates: Mapping[str, RateCard],
    trucks: Sequence[TruckType],
    cfg: TripConfig,
    overrides: Mapping[frozenset[str], str],
    solo_costs: Mapping[str, float],
    calendar: PlanningCalendar,
) -> list[Candidate]:
    found: list[Candidate] = []
    ordered = sorted(pool, key=lambda shipment: (_deadline(shipment, cfg), shipment.shipment_id))
    max_extra = cfg.max_shipments_per_trip - 1
    for index, anchor in enumerate(ordered):
        anchor_deadline = _deadline(anchor, cfg)
        rest = []
        for other in ordered[index + 1 :]:
            if (_deadline(other, cfg) - anchor_deadline).days > cfg.max_early_days:
                break
            rest.append(other)
        for size in range(1, max_extra + 1):
            if size > len(rest):
                break
            for extra in combinations(rest, size):
                combo = (anchor,) + extra
                if not _all_compatible(combo, cfg, overrides):
                    continue
                found.extend(_try_combo(combo, rates, trucks, cfg, solo_costs, calendar))
    return found


def _try_combo(
    combo: Sequence[Shipment],
    rates: Mapping[str, RateCard],
    trucks: Sequence[TruckType],
    cfg: TripConfig,
    solo_costs: Mapping[str, float],
    calendar: PlanningCalendar,
) -> list[Candidate]:
    drops = {shipment.ship_to for shipment in combo}
    if len(combo) > cfg.max_shipments_per_trip or len(drops) > cfg.drop_cap:
        return []
    open_dates = open_departure_dates(combo, cfg, calendar)
    if not open_dates:
        return []
    if cfg.max_trucks is None:
        open_dates = open_dates[-1:]
    weight = sum(shipment.weight_kg for shipment in combo)
    volume = sum(shipment.volume_m3 for shipment in combo)
    best: tuple[float, str, int, TruckType] | None = None
    for truck in trucks:
        if not truck.has_capacity:
            continue
        if weight > float(truck.capacity_kg) + _COST_TOL or volume > float(truck.capacity_m3) + _COST_TOL:
            continue
        priced = trip_cost(combo, truck.truck_type, rates, cfg)
        if priced is None:
            continue
        cost, basis = priced
        rank = TRUCK_TYPES.index(truck.truck_type) if truck.truck_type in TRUCK_TYPES else len(TRUCK_TYPES)
        if best is None or cost < best[0] - 1e-9 or (abs(cost - best[0]) <= 1e-9 and rank < best[2]):
            best = (cost, basis, rank, truck)
    if best is None:
        return []
    cost, basis, _rank, truck = best
    if cfg.max_trucks is None:
        solo_sum = sum(solo_costs[shipment.shipment_id] for shipment in combo)
        if cost >= solo_sum - _COST_TOL:
            return []
    member_ids = tuple(sorted(shipment.shipment_id for shipment in combo))
    upgrade = 0
    if basis == "truck" and len(combo) >= 2:
        upgrade = sum(1 for shipment in combo if shipment.service == "LTL")
    found = []
    for ship_date in open_dates:
        found.append(
            Candidate(
                candidate_id=f"c:{'|'.join(member_ids)}:{truck.truck_type}:{ship_date.isoformat()}",
                member_ids=member_ids,
                cost=float(cost),
                ship_date=ship_date,
                early_days=early_days_of(combo, ship_date, cfg),
                upgrade=upgrade,
                truck_type=truck.truck_type,
                n_trucks=1,
                price_basis=basis,
                counts=((truck.truck_type, 1),),
                cases=sum(shipment.cases for shipment in combo),
                weight_kg=weight,
                volume_m3=volume,
                capacity_kg=float(truck.capacity_kg),
                capacity_m3=float(truck.capacity_m3),
                is_solo=False,
            )
        )
    return found


def _all_compatible(
    combo: Sequence[Shipment],
    cfg: TripConfig,
    overrides: Mapping[frozenset[str], str],
) -> bool:
    for left, right in combinations(combo, 2):
        if not pair_compatible(left, right, cfg, overrides):
            return False
    return True


def _components(
    shipments: Sequence[Shipment],
    cfg: TripConfig,
    overrides: Mapping[frozenset[str], str],
) -> list[list[Shipment]]:
    parent = {shipment.shipment_id: shipment.shipment_id for shipment in shipments}
    by_id = {shipment.shipment_id: shipment for shipment in shipments}

    def find(shipment_id: str) -> str:
        while parent[shipment_id] != shipment_id:
            parent[shipment_id] = parent[parent[shipment_id]]
            shipment_id = parent[shipment_id]
        return shipment_id

    def union(left: str, right: str) -> None:
        root_left, root_right = find(left), find(right)
        if root_left != root_right:
            parent[root_right] = root_left

    ids = list(by_id)
    for index, left in enumerate(ids):
        for right in ids[index + 1 :]:
            if pair_compatible(by_id[left], by_id[right], cfg, overrides):
                union(left, right)
    groups: dict[str, list[Shipment]] = defaultdict(list)
    for shipment_id in ids:
        groups[find(shipment_id)].append(by_id[shipment_id])
    return list(groups.values())


def _mode_match(left: Shipment, right: Shipment, cfg: TripConfig) -> bool:
    same_province = bool(left.province) and left.province == right.province
    same_group = bool(left.customer_group) and left.customer_group == right.customer_group
    if cfg.compat_mode == "any":
        return True
    if cfg.compat_mode == "none":
        return False
    if cfg.compat_mode == "same_province":
        return same_province
    if cfg.compat_mode == "same_group":
        return same_group
    if cfg.compat_mode == "same_province_or_group":
        return same_province or same_group
    raise ValueError(f"Unknown compat_mode {cfg.compat_mode}.")


def _deadline(shipment: Shipment, cfg: TripConfig) -> date:
    return shipment.rdd - timedelta(days=cfg.transit_for(shipment.province))


def _member_window(shipment: Shipment, cfg: TripConfig) -> tuple[date, date] | None:
    deadline = _deadline(shipment, cfg)
    earliest = max(shipment.release_date, deadline - timedelta(days=cfg.max_early_days))
    if earliest > deadline:
        return None
    return earliest, deadline


def _intersect_windows(members: Sequence[Shipment], cfg: TripConfig) -> tuple[date, date] | None:
    if not members:
        return None
    start = date.min
    end = date.max
    for member in members:
        window = _member_window(member, cfg)
        if window is None:
            return None
        start = max(start, window[0])
        end = min(end, window[1])
        if start > end:
            return None
    return start, end
