"""Write the plan files and check the hard rules from the specification."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import pandas as pd

from baseline_cost import _format_metric, build_baseline, parse_rates, parse_trucks
from candidates import CandidateSet
from config import TRUCK_TYPES, TripConfig
from shipments import Shipment, ShipmentBuild, build_flags
from trip_model import SolveResult

_TOL = 1e-4

TRIP_COLUMNS = [
    "trip_id",
    "ship_date",
    "truck_type",
    "n_trucks",
    "n_drops",
    "cases",
    "kg",
    "m3",
    "util_kg",
    "util_m3",
    "cost",
    "price_basis",
    "shipment_ids",
]
TRIP_SHIPMENT_COLUMNS = ["trip_id", "shipment_id", "ship_to", "rdd", "ship_date", "early_days"]
TRIP_ORDER_COLUMNS = ["order_id", "trip_id"]
EXCLUSION_COLUMNS = ["order_id", "shipment_id", "category", "reason", "cases", "kg", "m3"]
FLAG_COLUMNS = ["flag", "key", "detail"]


@dataclass
class PlanResult:
    trips: pd.DataFrame
    trip_shipments: pd.DataFrame
    trip_orders: pd.DataFrame
    exclusions: pd.DataFrame
    summary: pd.DataFrame
    flags: pd.DataFrame
    total_cost: float
    baseline_cost: float
    baseline_cost_all_built: float
    objective_value: float
    tie_break_total: float
    penalty_total: float
    errors: list[str]
    runtime_s: float
    n_shipments: int
    n_candidates: int

    @property
    def ok(self) -> bool:
        return not self.errors


def summary_map(result: PlanResult) -> dict[str, str]:
    return dict(zip(result.summary["metric"].astype(str), result.summary["value"].astype(str)))


def assemble_plan(
    orders: pd.DataFrame,
    customers: pd.DataFrame,
    rates: pd.DataFrame,
    trucks: pd.DataFrame,
    cfg: TripConfig,
    built: ShipmentBuild,
    candidates: CandidateSet,
    solved: SolveResult,
    runtime_s: float,
) -> PlanResult:
    """Turn a solve into the section 10 files and the reconciliation checks."""
    rate_cards = parse_rates(rates)
    truck_types = parse_trucks(trucks)
    by_id = {shipment.shipment_id: shipment for shipment in candidates.shipments}
    past_due = set(candidates.past_due_ids)
    planned_ids = [shipment_id for candidate in solved.chosen for shipment_id in candidate.member_ids]
    planned = set(planned_ids)
    unplanned = set(solved.unplanned_ids)
    lines = built.lines.to_dict("records")
    lines_by_shipment: dict[str, list[dict]] = {}
    for line in lines:
        lines_by_shipment.setdefault(line["shipment_id"], []).append(line)

    trip_rows = []
    member_rows = []
    order_rows = []
    ordered = sorted(solved.chosen, key=lambda candidate: (candidate.ship_date, candidate.truck_type, candidate.candidate_id))
    for index, candidate in enumerate(ordered, start=1):
        trip_id = f"T{index:03d}"
        members = [by_id[shipment_id] for shipment_id in candidate.member_ids]
        drops = sorted({member.ship_to for member in members})
        shipment_ids = sorted(candidate.member_ids)
        trip_rows.append(
            {
                "trip_id": trip_id,
                "ship_date": candidate.ship_date.isoformat(),
                "truck_type": candidate.truck_type,
                "n_trucks": candidate.n_trucks,
                "n_drops": len(drops),
                "cases": candidate.cases,
                "kg": candidate.weight_kg,
                "m3": candidate.volume_m3,
                "util_kg": _util(candidate.weight_kg, candidate.capacity_kg),
                "util_m3": _util(candidate.volume_m3, candidate.capacity_m3),
                "cost": candidate.cost,
                "price_basis": candidate.price_basis,
                "shipment_ids": ";".join(shipment_ids),
            }
        )
        for member in sorted(members, key=lambda shipment: shipment.shipment_id):
            ready = member.rdd - timedelta(days=cfg.transit_for(member.province))
            member_rows.append(
                {
                    "trip_id": trip_id,
                    "shipment_id": member.shipment_id,
                    "ship_to": member.ship_to,
                    "rdd": member.rdd.isoformat(),
                    "ship_date": candidate.ship_date.isoformat(),
                    "early_days": (ready - candidate.ship_date).days,
                }
            )
        for shipment_id in shipment_ids:
            for line in sorted(lines_by_shipment.get(shipment_id, []), key=lambda item: item["order_id"]):
                order_rows.append({"order_id": line["order_id"], "trip_id": trip_id})

    business = []
    for shipment_id in sorted(unplanned):
        reason = "past_due" if shipment_id in past_due else "fleet_limit"
        for line in sorted(lines_by_shipment.get(shipment_id, []), key=lambda item: item["order_id"]):
            business.append(
                {
                    "order_id": line["order_id"],
                    "shipment_id": shipment_id,
                    "category": "business",
                    "reason": reason,
                    "cases": line["cases"],
                    "kg": line["weight_kg"],
                    "m3": line["volume_m3"],
                }
            )
    exclusions = pd.concat(
        [built.exclusions, _frame(business, EXCLUSION_COLUMNS)],
        ignore_index=True,
    )
    if not exclusions.empty:
        exclusions = exclusions.sort_values(
            ["category", "reason", "order_id"], kind="mergesort"
        ).reset_index(drop=True)

    flags = build_flags(orders, customers, rates, trucks)
    _baseline_detail, baseline_summary = build_baseline(built.shipments, rate_cards, truck_types)
    solo_costs = {
        candidate.member_ids[0]: candidate.cost
        for candidate in candidates.candidates
        if candidate.is_solo
    }
    baseline_cost = float(sum(solo_costs.values()))
    baseline_all = float(baseline_summary["cheaper_cost"])
    dateable_left_out = sorted(unplanned - past_due)
    saving = None if dateable_left_out else baseline_cost - solved.total_cost
    saving_pct = None
    if saving is not None and baseline_cost > _TOL:
        saving_pct = 100.0 * saving / baseline_cost

    errors = _hard_rules(
        cfg,
        built,
        candidates,
        solved,
        by_id,
        rate_cards,
        planned,
        unplanned,
        lines,
        exclusions,
    )
    summary = _summary(
        cfg,
        built,
        candidates,
        solved,
        exclusions,
        order_rows,
        trip_rows,
        baseline_cost,
        baseline_all,
        saving,
        saving_pct,
        runtime_s,
    )
    return PlanResult(
        trips=_frame(trip_rows, TRIP_COLUMNS),
        trip_shipments=_frame(member_rows, TRIP_SHIPMENT_COLUMNS),
        trip_orders=_frame(order_rows, TRIP_ORDER_COLUMNS),
        exclusions=exclusions if not exclusions.empty else _frame([], EXCLUSION_COLUMNS),
        summary=summary,
        flags=flags if not flags.empty else _frame([], FLAG_COLUMNS),
        total_cost=solved.total_cost,
        baseline_cost=baseline_cost,
        baseline_cost_all_built=baseline_all,
        objective_value=solved.objective_value,
        tie_break_total=solved.tie_break_total,
        penalty_total=solved.penalty_total,
        errors=errors,
        runtime_s=runtime_s,
        n_shipments=len(candidates.shipments),
        n_candidates=len(candidates.candidates),
    )


def write_plan(result: PlanResult, out_dir: Path) -> None:
    destination = Path(out_dir)
    destination.mkdir(parents=True, exist_ok=True)
    result.trips.to_csv(destination / "trips.csv", index=False)
    result.trip_shipments.to_csv(destination / "trip_shipments.csv", index=False)
    result.trip_orders.to_csv(destination / "trip_orders.csv", index=False)
    result.exclusions.to_csv(destination / "exclusions.csv", index=False)
    result.summary.to_csv(destination / "summary.csv", index=False)
    result.flags.to_csv(destination / "flags.csv", index=False)


def _hard_rules(
    cfg: TripConfig,
    built: ShipmentBuild,
    candidates: CandidateSet,
    solved: SolveResult,
    by_id: dict[str, Shipment],
    rates,
    planned: set[str],
    unplanned: set[str],
    lines: list[dict],
    exclusions: pd.DataFrame,
) -> list[str]:
    errors = []
    all_ids = {shipment.shipment_id for shipment in candidates.shipments}
    if planned & unplanned or planned | unplanned != all_ids:
        errors.append("Every built shipment must be in one trip or one business exclusion.")
    if any(shipment_id not in candidates.past_due_ids and shipment_id in unplanned for shipment_id in unplanned):
        if cfg.max_trucks is None:
            errors.append("A shipment was left unplanned without a fleet limit.")
    covered_solo = 0.0
    for candidate in solved.chosen:
        members = [by_id[shipment_id] for shipment_id in candidate.member_ids]
        covered_solo += sum(
            item.cost
            for item in candidates.candidates
            if item.is_solo and item.member_ids[0] in candidate.member_ids
        )
        spread = (max(member.rdd for member in members) - min(member.rdd for member in members)).days
        if spread > cfg.max_early_days:
            errors.append(f"{candidate.candidate_id} RDD spread is {spread} days.")
        early_sum = 0
        for member in members:
            arrival = candidate.ship_date + timedelta(days=cfg.transit_for(member.province))
            if arrival > member.rdd:
                errors.append(f"{candidate.candidate_id} delivers {member.shipment_id} after its RDD.")
            if candidate.ship_date < member.release_date:
                errors.append(f"{candidate.candidate_id} ships {member.shipment_id} before release.")
            ready = member.rdd - timedelta(days=cfg.transit_for(member.province))
            early = (ready - candidate.ship_date).days
            if early < 0:
                errors.append(f"{candidate.candidate_id} has negative early days for {member.shipment_id}.")
            early_sum += early
        if early_sum != candidate.early_days:
            errors.append(f"{candidate.candidate_id} early days do not match its members.")
        if candidate.n_trucks > 0:
            if candidate.weight_kg > candidate.capacity_kg + _TOL or candidate.volume_m3 > candidate.capacity_m3 + _TOL:
                errors.append(f"{candidate.candidate_id} exceeds truck capacity.")
            for truck_type, count in candidate.counts:
                if count <= 0:
                    continue
                for member in members:
                    if truck_type not in rates[member.ship_to].trip:
                        errors.append(
                            f"{candidate.candidate_id} uses {truck_type}, which {member.ship_to} does not offer."
                        )
    if solved.total_cost > covered_solo + _TOL:
        errors.append(
            f"Plan cost {solved.total_cost} is above the solo baseline {covered_solo} of the planned shipments."
        )
    if abs(solved.total_cost + solved.tie_break_total + solved.penalty_total - solved.objective_value) > _TOL:
        errors.append("Objective does not equal cost plus tie-break plus penalty.")

    order_ids = set(built.order_ids)
    excluded_ids = set() if exclusions.empty else set(exclusions["order_id"].astype(str))
    # trip orders are not in this function's signature; reconstruct from lines and planned.
    planned_orders = {line["order_id"] for line in lines if line["shipment_id"] in planned}
    if planned_orders & excluded_ids:
        errors.append("An order is both planned and excluded.")
    if planned_orders | excluded_ids != order_ids:
        errors.append("An order is missing from trips and exclusions, or an extra order appeared.")
    if not exclusions.empty and exclusions["order_id"].duplicated().any():
        errors.append("An order has more than one exclusion row.")
    excluded_cases = 0.0 if exclusions.empty else float(exclusions["cases"].sum())
    excluded_kg = 0.0 if exclusions.empty else float(exclusions["kg"].sum())
    excluded_m3 = 0.0 if exclusions.empty else float(exclusions["m3"].sum())
    planned_cases = sum(line["cases"] for line in lines if line["shipment_id"] in planned)
    planned_kg = sum(line["weight_kg"] for line in lines if line["shipment_id"] in planned)
    planned_m3 = sum(line["volume_m3"] for line in lines if line["shipment_id"] in planned)
    if abs(planned_cases + excluded_cases - built.input_cases) > _TOL:
        errors.append("Cases do not reconcile: planned plus excluded must equal the input.")
    if abs(planned_kg + excluded_kg - built.input_kg) > _TOL:
        errors.append("Kilograms do not reconcile: planned plus excluded must equal the input.")
    if abs(planned_m3 + excluded_m3 - built.input_m3) > _TOL:
        errors.append("Cubic metres do not reconcile: planned plus excluded must equal the input.")
    del covered_solo
    return errors


def _summary(
    cfg: TripConfig,
    built: ShipmentBuild,
    candidates: CandidateSet,
    solved: SolveResult,
    exclusions: pd.DataFrame,
    order_rows: list[dict],
    trip_rows: list[dict],
    baseline_cost: float,
    baseline_all: float,
    saving: float | None,
    saving_pct: float | None,
    runtime_s: float,
) -> pd.DataFrame:
    planned_orders = {row["order_id"] for row in order_rows}
    metrics: list[tuple[str, object]] = [
        ("input_orders", len(built.order_ids)),
        ("input_cases", built.input_cases),
        ("input_kg", built.input_kg),
        ("input_m3", built.input_m3),
        ("shipments", len(candidates.shipments)),
        ("candidates", len(candidates.candidates)),
        ("planned_orders", len(planned_orders)),
        ("planned_cases", sum(row["cases"] for row in trip_rows)),
        ("planned_kg", sum(row["kg"] for row in trip_rows)),
        ("planned_m3", sum(row["m3"] for row in trip_rows)),
        ("excluded_orders", 0 if exclusions.empty else int(len(exclusions))),
        ("excluded_cases", 0.0 if exclusions.empty else float(exclusions["cases"].sum())),
        ("excluded_kg", 0.0 if exclusions.empty else float(exclusions["kg"].sum())),
        ("excluded_m3", 0.0 if exclusions.empty else float(exclusions["m3"].sum())),
    ]
    if not exclusions.empty:
        for reason, group in exclusions.groupby("reason", sort=True):
            metrics.append((f"excluded_{reason}_orders", int(len(group))))
            metrics.append((f"excluded_{reason}_cases", float(group["cases"].sum())))
            metrics.append((f"excluded_{reason}_kg", float(group["kg"].sum())))
            metrics.append((f"excluded_{reason}_m3", float(group["m3"].sum())))
    truck_counts = {truck_type: 0 for truck_type in TRUCK_TYPES}
    cap_kg = 0.0
    cap_m3 = 0.0
    load_kg = 0.0
    load_m3 = 0.0
    multi = 0
    for candidate in solved.chosen:
        if len(candidate.member_ids) >= 2:
            multi += 1
        for truck_type, count in candidate.counts:
            truck_counts[truck_type] = truck_counts.get(truck_type, 0) + count
        if candidate.n_trucks > 0 and candidate.capacity_kg > 0:
            cap_kg += candidate.capacity_kg
            cap_m3 += candidate.capacity_m3
            load_kg += candidate.weight_kg
            load_m3 += candidate.volume_m3
    statuses = [component.status for component in solved.components]
    if not statuses or all(status == "optimal" for status in statuses):
        solver_status = "optimal"
    elif any(status == "time_limit" for status in statuses):
        solver_status = "time_limit"
    else:
        solver_status = statuses[0]
    gaps = [component.gap for component in solved.components]
    metrics.extend(
        [
            ("total_cost", solved.total_cost),
            ("baseline_cost", baseline_cost),
            ("baseline_cost_all_built", baseline_all),
            ("saving_thb", "" if saving is None else saving),
            ("saving_percent", "" if saving_pct is None else saving_pct),
            ("trips", len(trip_rows)),
            ("trips_multi_shipment", multi),
            ("trucks", sum(truck_counts.values())),
            ("util_kg", (load_kg / cap_kg) if cap_kg else 0.0),
            ("util_m3", (load_m3 / cap_m3) if cap_m3 else 0.0),
            ("objective_value", solved.objective_value),
            ("tie_break_total", solved.tie_break_total),
            ("penalty_total", solved.penalty_total),
            ("runtime_s", runtime_s),
            ("solver_runtime_s", solved.runtime_s),
            ("solver_status", solver_status),
            ("solver_gap_max", max(gaps) if gaps else 0.0),
            ("assumptions", _assumptions(cfg)),
        ]
    )
    for truck_type, count in truck_counts.items():
        metrics.append((f"trucks_{truck_type}", count))
    for component in solved.components:
        metrics.append((f"component_{component.component_id}_status", component.status))
        metrics.append((f"component_{component.component_id}_gap", component.gap))
        metrics.append((f"component_{component.component_id}_shipments", len(component.shipment_ids)))
        metrics.append((f"component_{component.component_id}_candidates", component.candidate_count))
    return pd.DataFrame(
        [{"metric": name, "value": _format_metric(value)} for name, value in metrics]
    )


def _assumptions(cfg: TripConfig) -> str:
    fleet = "none" if cfg.max_trucks is None else str(cfg.max_trucks)
    return (
        "multi-drop price = max trip rate; LTL = cheaper of case and truck; "
        f"max_early_days={cfg.max_early_days}; transit_days={cfg.transit_days}; "
        f"compat_mode={cfg.compat_mode}; handling_basis={cfg.handling_basis}; "
        f"max_shipments_per_trip={cfg.max_shipments_per_trip}; max_trucks={fleet}"
    )


def _util(load: float, capacity: float) -> float | str:
    if capacity <= _TOL:
        return ""
    return load / capacity


def _frame(rows: list[dict], columns: list[str]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=columns)
    return pd.DataFrame(rows, columns=columns)


