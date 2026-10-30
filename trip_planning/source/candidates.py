"""List the trips a shipment set can make: who may share a truck, on which day, within capacity.

A shipment that cannot leave on or before its delivery date is past due and gets
no candidate. A consolidated trip is kept only when it is strictly cheaper than
shipping each member alone, and only the cheapest truck type is kept for that set.
A solo that needs several trucks is one candidate; those trucks are not offered
to other shipments.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from itertools import combinations
from typing import Mapping, Sequence

import pandas as pd

from baseline_cost import RateCard, TruckType, format_truck_mix, parse_rates, parse_trucks
from config import TRUCK_TYPES, TripConfig, norm_id
from pricing import solo_cost, trip_cost
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


@dataclass(frozen=True)
class CandidateSet:
    candidates: tuple[Candidate, ...]
    shipments: tuple[Shipment, ...]
    past_due_ids: tuple[str, ...]


def generate_candidates(
    shipments: pd.DataFrame,
    rates: pd.DataFrame | Mapping[str, RateCard],
    trucks: pd.DataFrame | Sequence[TruckType],
    cfg: TripConfig,
    overrides: pd.DataFrame | None = None,
) -> CandidateSet:
    """Build solo candidates and consolidated trips that beat the sum of solos."""
    shipment_list = to_shipments(shipments)
    rate_cards = parse_rates(rates) if isinstance(rates, pd.DataFrame) else dict(rates)
    truck_types = parse_trucks(trucks) if isinstance(trucks, pd.DataFrame) else list(trucks)
    override_rules = parse_overrides(overrides)
    candidates: list[Candidate] = []
    past_due: list[str] = []
    dateable: list[Shipment] = []

    for shipment in shipment_list:
        rate = rate_cards.get(shipment.ship_to)
        if rate is None:
            raise ValueError(f"Shipment {shipment.shipment_id} has no rate card.")
        quote = solo_cost(shipment, rate, truck_types, cfg)
        if quote is None:
            raise ValueError(
                f"Shipment {shipment.shipment_id} has no price. "
                "It should have been excluded as truck_type_not_offered."
            )
        ship_date = feasible_ship_date((shipment,), cfg)
        if ship_date is None:
            past_due.append(shipment.shipment_id)
            continue
        dateable.append(shipment)
        candidates.append(_solo_candidate(shipment, quote, ship_date, cfg))

    solo_costs = {candidate.member_ids[0]: candidate.cost for candidate in candidates if candidate.is_solo}
    for pool in _components(dateable, cfg, override_rules):
        candidates.extend(
            _consolidated(pool, rate_cards, truck_types, cfg, override_rules, solo_costs)
        )
    candidates.sort(key=lambda candidate: candidate.candidate_id)
    return CandidateSet(tuple(candidates), tuple(shipment_list), tuple(past_due))


def feasible_ship_date(members: Sequence[Shipment], cfg: TripConfig) -> date | None:
    """The only legal ship date, or None when that date misses a release or an RDD.

    Ship date is the earliest member delivery date minus transit. The RDD spread
    (max RDD minus min RDD) may not exceed ``max_early_days``.
    """
    if not members:
        return None
    departures = [
        member.rdd - timedelta(days=cfg.transit_for(member.province)) for member in members
    ]
    ship_date = min(departures)
    if ship_date < max(member.release_date for member in members):
        return None
    spread = (max(member.rdd for member in members) - min(member.rdd for member in members)).days
    if spread > cfg.max_early_days:
        return None
    for member in members:
        arrival = ship_date + timedelta(days=cfg.transit_for(member.province))
        if arrival > member.rdd:
            return None
    return ship_date


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
    """Symmetric rule. ``solo_only`` ship-tos never share, not even with themselves."""
    if left.shipment_id == right.shipment_id:
        return False
    if left.ship_to in cfg.solo_only or right.ship_to in cfg.solo_only:
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


def _solo_candidate(shipment: Shipment, quote, ship_date: date, cfg: TripConfig) -> Candidate:
    fleet = quote.fleet
    if quote.price_basis == "truck" and fleet is not None:
        counts = fleet.counts
        capacity_kg = fleet.capacity_kg
        capacity_m3 = fleet.capacity_m3
        n_trucks = fleet.n_trucks
    else:
        counts = ()
        capacity_kg = 0.0
        capacity_m3 = 0.0
        n_trucks = 0
    return Candidate(
        candidate_id=f"solo:{shipment.shipment_id}",
        member_ids=(shipment.shipment_id,),
        cost=float(quote.cost),
        ship_date=ship_date,
        early_days=early_days_of((shipment,), ship_date, cfg),
        upgrade=0,
        truck_type=format_truck_mix(counts, quote.price_basis),
        n_trucks=n_trucks,
        price_basis=quote.price_basis,
        counts=counts,
        cases=shipment.cases,
        weight_kg=shipment.weight_kg,
        volume_m3=shipment.volume_m3,
        capacity_kg=capacity_kg,
        capacity_m3=capacity_m3,
        is_solo=True,
    )


def _consolidated(
    pool: Sequence[Shipment],
    rates: Mapping[str, RateCard],
    trucks: Sequence[TruckType],
    cfg: TripConfig,
    overrides: Mapping[frozenset[str], str],
    solo_costs: Mapping[str, float],
) -> list[Candidate]:
    found: list[Candidate] = []
    ordered = sorted(pool, key=lambda shipment: (shipment.rdd, shipment.shipment_id))
    max_extra = cfg.max_shipments_per_trip - 1
    for index, anchor in enumerate(ordered):
        rest = []
        for other in ordered[index + 1 :]:
            if (other.rdd - anchor.rdd).days > cfg.max_early_days:
                break
            rest.append(other)
        for size in range(1, max_extra + 1):
            if size > len(rest):
                break
            for extra in combinations(rest, size):
                combo = (anchor,) + extra
                if not _all_compatible(combo, cfg, overrides):
                    continue
                candidate = _try_combo(combo, rates, trucks, cfg, solo_costs)
                if candidate is not None:
                    found.append(candidate)
    return found


def _try_combo(
    combo: Sequence[Shipment],
    rates: Mapping[str, RateCard],
    trucks: Sequence[TruckType],
    cfg: TripConfig,
    solo_costs: Mapping[str, float],
) -> Candidate | None:
    drops = {shipment.ship_to for shipment in combo}
    if len(combo) > cfg.max_shipments_per_trip or len(drops) > cfg.drop_cap:
        return None
    ship_date = feasible_ship_date(combo, cfg)
    if ship_date is None:
        return None
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
        return None
    cost, basis, _rank, truck = best
    solo_sum = sum(solo_costs[shipment.shipment_id] for shipment in combo)
    if cost >= solo_sum - _COST_TOL:
        return None
    member_ids = tuple(sorted(shipment.shipment_id for shipment in combo))
    upgrade = 0
    if basis == "truck":
        upgrade = sum(1 for shipment in combo if shipment.service == "LTL")
    return Candidate(
        candidate_id=f"c:{'|'.join(member_ids)}:{truck.truck_type}",
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
