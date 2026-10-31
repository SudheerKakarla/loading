"""Write the plan files and check the hard rules from the specification."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import pandas as pd

from baseline_cost import _format_metric, build_baseline, parse_rates, parse_trucks
from candidates import CandidateSet
from config import TRUCK_TYPES, TripConfig
from provenance import (
    config_payload,
    environment_versions,
    file_sha256,
    git_revision,
    new_run_id,
    table_sha256,
    utc_now,
)
from publication import publish_plan, remove_tree, staging_dir
from retrospective import empty_calendar
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
    "po_numbers",
]
TRIP_SHIPMENT_COLUMNS = ["trip_id", "shipment_id", "ship_to", "rdd", "ship_date", "early_days"]
TRIP_ORDER_COLUMNS = [
    "order_id",
    "trip_id",
    "po_numbers",
    "original_rdd",
    "effective_rdd",
    "override_reason",
]
EXCLUSION_COLUMNS = [
    "order_id",
    "shipment_id",
    "ship_to",
    "customer_name",
    "province",
    "category",
    "reason",
    "cases",
    "kg",
    "m3",
    "po_numbers",
    "original_rdd",
    "effective_rdd",
    "override_reason",
]
FLAG_COLUMNS = ["flag", "key", "detail"]
MISSING_RATE_COLUMNS = [
    "ship_to",
    "customer_name",
    "province",
    "orders",
    "cases",
    "kg",
    "m3",
    "first_rdd",
    "last_rdd",
]


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
    missing_rates: pd.DataFrame
    shipments: pd.DataFrame
    baseline_shipments: pd.DataFrame
    baseline_summary: pd.DataFrame
    order_scope: pd.DataFrame
    metadata: dict

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
    calendar_blocked = set(candidates.calendar_unavailable_ids)
    calendar = candidates.calendar if candidates.calendar is not None else empty_calendar()
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
                "po_numbers": _po_list(lines_by_shipment, shipment_ids),
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
                order_rows.append(
                    {
                        "order_id": line["order_id"],
                        "trip_id": trip_id,
                        "po_numbers": line.get("po_numbers", ""),
                        "original_rdd": _iso(line.get("original_rdd", line.get("rdd"))),
                        "effective_rdd": _iso(line.get("effective_rdd", line.get("rdd"))),
                        "override_reason": line.get("override_reason", ""),
                    }
                )

    business = []
    for shipment_id in sorted(unplanned):
        if shipment_id in past_due:
            reason = "past_due"
        elif shipment_id in calendar_blocked:
            reason = "calendar_unavailable"
        else:
            reason = "fleet_limit"
        for line in sorted(lines_by_shipment.get(shipment_id, []), key=lambda item: item["order_id"]):
            business.append(_exclusion_from_line(line, shipment_id, "business", reason))
    exclusions = pd.concat(
        [built.exclusions, _frame(business, EXCLUSION_COLUMNS)],
        ignore_index=True,
    )
    if not exclusions.empty:
        exclusions = exclusions.sort_values(
            ["category", "reason", "order_id"], kind="mergesort"
        ).reset_index(drop=True)

    effective = {
        str(row["order_id"]): row["effective_rdd"]
        for row in built.effective_orders.to_dict("records")
    }
    baseline_detail, baseline_summary = build_baseline(built.shipments, rate_cards, truck_types)
    flags = _solver_flags(
        build_flags(orders, customers, rates, trucks, effective),
        candidates,
        solved,
        baseline_summary,
    )
    baseline_cost = float(sum(cost for _shipment_id, cost in candidates.solo_baseline))
    baseline_all = float(baseline_summary["cheaper_cost"])
    dateable_left_out = sorted(unplanned - past_due)
    saving = None if dateable_left_out else baseline_cost - solved.total_cost
    saving_pct = None
    if saving is not None and baseline_cost > _TOL:
        saving_pct = 100.0 * saving / baseline_cost
    sheet_cost = float(baseline_summary["sheet_rule_cost"])
    sheet_blocked = bool(unplanned) or int(baseline_summary["sheet_rule_unpriced_shipments"]) > 0
    sheet_saving = None if sheet_blocked else sheet_cost - solved.total_cost
    sheet_saving_pct = None
    if sheet_saving is not None and sheet_cost > _TOL:
        sheet_saving_pct = 100.0 * sheet_saving / sheet_cost

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
        calendar,
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
        sheet_cost,
        sheet_saving,
        sheet_saving_pct,
        runtime_s,
    )
    missing_rates = _missing_rates(exclusions)
    scope = _order_scope(order_rows, lines, trip_rows)
    metadata = _base_metadata(cfg, built, candidates, solved, baseline_summary, exclusions, missing_rates)
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
        missing_rates=missing_rates,
        shipments=built.shipments,
        baseline_shipments=baseline_detail,
        baseline_summary=_metric_frame(baseline_summary),
        order_scope=scope,
        metadata=metadata,
    )


def write_plan(result: PlanResult, out_dir: Path) -> None:
    """Write ten CSVs and run_metadata.json. Refuse a directory that already holds a plan."""
    if not result.ok:
        raise ValueError("Refusing to publish a plan that failed its checks.")
    destination = Path(out_dir)
    staging = staging_dir(destination)
    remove_tree(staging)
    staging.mkdir(parents=True)
    try:
        _write_table(staging / "trips.csv", result.trips)
        _write_table(staging / "trip_shipments.csv", result.trip_shipments)
        _write_table(staging / "trip_orders.csv", result.trip_orders)
        _write_table(staging / "exclusions.csv", result.exclusions)
        _write_table(staging / "missing_rate_customers.csv", result.missing_rates)
        _write_table(staging / "summary.csv", result.summary)
        _write_table(staging / "flags.csv", result.flags)
        _write_table(staging / "shipments.csv", result.shipments)
        _write_table(staging / "baseline_shipments.csv", result.baseline_shipments)
        _write_table(staging / "baseline_summary.csv", result.baseline_summary)
        result.metadata["output_files"] = {
            path.name: file_sha256(path) for path in sorted(staging.glob("*.csv"))
        }
        _write_metadata(staging / "run_metadata.json", result.metadata)
        publish_plan(staging, destination)
    except Exception:
        remove_tree(staging)
        raise


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
    calendar,
) -> list[str]:
    errors = []
    all_ids = {shipment.shipment_id for shipment in candidates.shipments}
    if planned & unplanned or planned | unplanned != all_ids:
        errors.append("Every built shipment must be in one trip or one business exclusion.")
    allowed_left_out = set(candidates.past_due_ids) | set(candidates.calendar_unavailable_ids)
    if cfg.max_trucks is None and any(shipment_id not in allowed_left_out for shipment_id in unplanned):
        errors.append("A shipment was left unplanned without a fleet limit.")
    baseline_map = dict(candidates.solo_baseline)
    covered_solo = sum(baseline_map[shipment_id] for shipment_id in planned if shipment_id in baseline_map)
    for candidate in solved.chosen:
        members = [by_id[shipment_id] for shipment_id in candidate.member_ids]
        if not calendar.dispatch_open(candidate.ship_date):
            errors.append(f"{candidate.candidate_id} departs on a closed dispatch date.")
        if len(members) >= 2 and any(not member.allow_sharing for member in members):
            errors.append(f"{candidate.candidate_id} shares a shipment that is not allowed to share.")
        if candidate.price_basis == "truck" and any(not member.allow_truck for member in members):
            errors.append(f"{candidate.candidate_id} uses a truck price a member does not permit.")
        if candidate.price_basis == "case" and any(
            member.service != "LTL" or not member.allow_case for member in members
        ):
            errors.append(f"{candidate.candidate_id} uses a case price a member does not permit.")
        early_sum = 0
        for member in members:
            arrival = candidate.ship_date + timedelta(days=cfg.transit_for(member.province))
            if not calendar.receiving_open(member.ship_to, arrival):
                errors.append(f"{candidate.candidate_id} arrives for {member.shipment_id} on a closed date.")
            if arrival > member.rdd:
                errors.append(f"{candidate.candidate_id} delivers {member.shipment_id} after its RDD.")
            if candidate.ship_date < member.release_date:
                errors.append(f"{candidate.candidate_id} ships {member.shipment_id} before release.")
            ready = member.rdd - timedelta(days=cfg.transit_for(member.province))
            early = (ready - candidate.ship_date).days
            if early < 0 or early > cfg.max_early_days:
                errors.append(f"{candidate.candidate_id} early days for {member.shipment_id} are outside the window.")
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
    if cfg.max_trucks is None and solved.total_cost > covered_solo + 1 - 1e-9:
        errors.append(
            f"Plan cost {solved.total_cost} is a full baht above the solo baseline {covered_solo}."
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
    sheet_cost: float,
    sheet_saving: float | None,
    sheet_saving_pct: float | None,
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
    fallbacks = [status for status in statuses if status.startswith("fallback_")]
    if fallbacks:
        solver_status = fallbacks[0]
    elif not statuses or all(status == "optimal" for status in statuses):
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
            ("sheet_rule_cost", sheet_cost),
            ("saving_vs_sheet_rule_thb", "" if sheet_saving is None else sheet_saving),
            ("saving_vs_sheet_rule_percent", "" if sheet_saving_pct is None else sheet_saving_pct),
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


def _iso(value: object) -> str:
    if value is None:
        return ""
    if hasattr(value, "isoformat"):
        text = value.isoformat()
        return text[:10] if "T" in text else str(value)[:10] if len(str(value)) >= 10 and str(value)[4:5] == "-" else text
    text = str(value).strip()
    return "" if text.lower() in {"nan", "none", "nat"} else text[:10]


def _po_list(lines_by_shipment: dict[str, list[dict]], shipment_ids: list[str]) -> str:
    found: list[str] = []
    for shipment_id in shipment_ids:
        for line in lines_by_shipment.get(shipment_id, []):
            for token in str(line.get("po_numbers", "")).split(";"):
                token = token.strip()
                if token and token not in found:
                    found.append(token)
    return ";".join(sorted(found))


def _exclusion_from_line(line: dict, shipment_id: str, category: str, reason: str) -> dict:
    return {
        "order_id": line["order_id"],
        "shipment_id": shipment_id,
        "ship_to": line.get("ship_to", ""),
        "customer_name": line.get("customer_name", ""),
        "province": line.get("province", ""),
        "category": category,
        "reason": reason,
        "cases": line["cases"],
        "kg": line["weight_kg"],
        "m3": line["volume_m3"],
        "po_numbers": line.get("po_numbers", ""),
        "original_rdd": _iso(line.get("original_rdd", line.get("rdd"))),
        "effective_rdd": _iso(line.get("effective_rdd", line.get("rdd"))),
        "override_reason": line.get("override_reason", ""),
    }


def _solver_flags(flags: pd.DataFrame, candidates: CandidateSet, solved: SolveResult, baseline: dict) -> pd.DataFrame:
    extra = []
    if int(baseline.get("fleet_pricing_nonoptimal_shipments", 0) or 0) > 0:
        extra.append(
            {
                "flag": "baseline_fleet_not_optimal",
                "key": "baseline",
                "detail": "at least one solo fleet price is not proven optimal",
            }
        )
    if any(component.status != "optimal" for component in solved.components):
        extra.append(
            {
                "flag": "solver_not_optimal",
                "key": "trip_model",
                "detail": ", ".join(f"{component.component_id}={component.status}" for component in solved.components),
            }
        )
    if any(candidate.pricing_status not in {"", "optimal"} for candidate in candidates.candidates):
        extra.append(
            {
                "flag": "candidate_pricing_not_optimal",
                "key": "candidates",
                "detail": "at least one candidate price is not proven optimal",
            }
        )
    if not extra:
        return flags
    return pd.concat([flags, _frame(extra, FLAG_COLUMNS)], ignore_index=True).sort_values(
        ["flag", "key"], kind="mergesort"
    ).reset_index(drop=True)


def _missing_rates(exclusions: pd.DataFrame) -> pd.DataFrame:
    if exclusions.empty or "reason" not in exclusions.columns:
        return _frame([], MISSING_RATE_COLUMNS)
    subset = exclusions[exclusions["reason"].astype(str) == "no_rate"]
    if subset.empty:
        return _frame([], MISSING_RATE_COLUMNS)
    rows = []
    grouped = subset.groupby(["ship_to", "customer_name", "province"], dropna=False, sort=True)
    for (ship_to, name, province), group in grouped:
        dates = sorted(str(value) for value in group["effective_rdd"] if str(value).strip())
        rows.append(
            {
                "ship_to": ship_to,
                "customer_name": name,
                "province": province,
                "orders": int(group["order_id"].nunique()),
                "cases": float(group["cases"].sum()),
                "kg": float(group["kg"].sum()),
                "m3": float(group["m3"].sum()),
                "first_rdd": dates[0] if dates else "",
                "last_rdd": dates[-1] if dates else "",
            }
        )
    return _frame(rows, MISSING_RATE_COLUMNS)


def _order_scope(order_rows: list[dict], lines: list[dict], trip_rows: list[dict]) -> pd.DataFrame:
    by_order = {line["order_id"]: line for line in lines}
    ship_dates = {row["trip_id"]: row["ship_date"] for row in trip_rows}
    rows = []
    for order in order_rows:
        line = by_order.get(order["order_id"], {})
        rows.append(
            {
                "order_id": order["order_id"],
                "effective_rdd": order.get("effective_rdd", ""),
                "cases": line.get("cases", 0),
                "kg": line.get("weight_kg", 0),
                "m3": line.get("volume_m3", 0),
                "ship_date": ship_dates.get(order["trip_id"], ""),
            }
        )
    return _frame(rows, ["order_id", "effective_rdd", "cases", "kg", "m3", "ship_date"])


def _base_metadata(cfg, built, candidates, solved, baseline_summary, exclusions, missing_rates) -> dict:
    root = Path(__file__).resolve().parents[2]
    return {
        "run_id": new_run_id(),
        "created_utc": utc_now(),
        "approval": "assumed_unapproved",
        "config": config_payload(cfg),
        "baseline_comparison": baseline_summary.get("baseline_comparison", ""),
        "tie_weights": {"early": solved.early_weight, "upgrade": solved.upgrade_weight},
        "penalty": solved.penalty,
        "solver_statuses": [component.status for component in solved.components],
        "solver_gaps": [component.gap if component.gap != float("inf") else "inf" for component in solved.components],
        "optional_features": {
            "permissions": True,
            "dated_calendar": True,
            "weekly_calendar": True,
            "delivery_date_overrides": True,
            "scenarios": True,
        },
        "calendar_rules": candidates.calendar.snapshot() if candidates.calendar is not None else empty_calendar().snapshot(),
        "tables": {
            "shipments": table_sha256(built.shipments),
            "exclusions": table_sha256(exclusions),
            "missing_rate_customers": table_sha256(missing_rates),
            "effective_orders_sha256": table_sha256(built.effective_orders),
        },
        "environment": environment_versions(),
        "git_revision": git_revision(root),
    }


def _metric_frame(summary: dict) -> pd.DataFrame:
    return pd.DataFrame([{"metric": key, "value": _format_metric(value)} for key, value in summary.items()])


def _write_table(path: Path, frame: pd.DataFrame) -> None:
    frame.to_csv(path, index=False)


def _write_metadata(path: Path, payload: dict) -> None:
    import json

    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


