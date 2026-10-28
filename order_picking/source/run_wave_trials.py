"""Measure the five open picker trials. Defaults stay as they are.

The trials are a Phase 1 walk gap, an opt-in two-tier chunk, an opt-in
load-stop sequence, a dock-staging count, and a 10% / 20% tour inflation.
Nothing here turns those options on for a normal solve.

Run from ``order_picking/``::

    python source/run_wave_trials.py
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence

import pandas as pd

_ROOT = Path(__file__).resolve().parents[1]
_CHUNKER = str(_ROOT / "chunker")
if _CHUNKER not in sys.path:
    sys.path.insert(0, _CHUNKER)

from assignment_reports import (  # noqa: E402
    load_inversions,
    require_shared_crew,
    shared_crew_screen_message,
    staging_peaks,
    stop_depth,
    utilisation_rows,
    walk_gap_summary,
    write_assignment_reports,
)
from chunk_model import (  # noqa: E402
    ChunkModelSettings,
    _held_karp,
    chunk_loads_model,
    read_pickers,
    write_model_outputs,
)
from order_picking_optimizer import (  # noqa: E402
    _build_lookup,
    _optional_hours,
    route_orders,
    solve_order_assignment,
)


def job_load_stops(items: pd.DataFrame, loads: pd.DataFrame) -> Dict[str, float]:
    """Deepest destination on each job. A missing item sorts as stop 0."""
    depth_by_item: Dict[str, float] = {}
    for row in loads.itertuples(index=False):
        depth_by_item[str(row.item_id)] = stop_depth(row.destination_id)
    stops: Dict[str, float] = {}
    for row in items.itertuples(index=False):
        depth = depth_by_item.get(str(row.item_id), 0.0)
        current = stops.get(row.order_id)
        if current is None or depth > current:
            stops[row.order_id] = depth
    return stops


def _pick_minutes(items: pd.DataFrame) -> Dict[str, float]:
    totals: Dict[str, float] = {}
    for row in items.itertuples(index=False):
        totals[row.order_id] = totals.get(row.order_id, 0.0) + _optional_hours(
            getattr(row, "pick_time", 0.0), 0.0
        )
    return totals


def _stored_walks(jobs_path: Path) -> Dict[str, float]:
    if not jobs_path.is_file():
        return {}
    frame = pd.read_csv(jobs_path)
    if "walk_minutes" not in frame.columns:
        return {}
    key = "job_id" if "job_id" in frame.columns else "order_id"
    return {str(row[key]): float(row.walk_minutes) for _, row in frame.iterrows()}


def measure_walks(
    depot_id: str,
    items: pd.DataFrame,
    travel: pd.DataFrame,
    jobs_path: Path,
) -> List[dict]:
    """Heuristic walk against a fresh Held-Karp walk. Pick time stays out of both."""
    routes = route_orders(items, depot_id, travel, solver="heuristic")
    lookup = _build_lookup(travel)
    picks = _pick_minutes(items)
    stored = _stored_walks(jobs_path)
    cache: Dict = {}
    rows: List[dict] = []
    for order_id, route in routes.items():
        group = items[items.order_id == order_id]
        locations = [str(row.location_id) for row in group.itertuples(index=False)]
        held = _held_karp(depot_id, locations, lookup, cache)
        pick = float(picks.get(order_id, 0.0))
        heuristic = float(route["route_duration"]) - pick
        rows.append(
            {
                "depot_id": depot_id,
                "order_id": order_id,
                "heuristic_walk": heuristic,
                "held_karp_walk": held,
                "gap": heuristic - held,
                "pick_minutes": pick,
                "heuristic_duration": float(route["route_duration"]),
                "held_karp_duration": held + pick,
                "stored_walk": stored.get(str(order_id), ""),
                "open_swap": bool(route.get("open_swap")),
                "line_count": int(len(group)),
            }
        )
    return rows


def _truck_of(orders: pd.DataFrame) -> Dict[str, str]:
    if "truck_id" not in orders.columns:
        return {}
    trucks: Dict[str, str] = {}
    for row in orders.itertuples(index=False):
        truck = getattr(row, "truck_id", "")
        if truck is None or (isinstance(truck, float) and pd.isna(truck)):
            continue
        text = str(truck).strip()
        if text:
            trucks[row.order_id] = text
    return trucks


def _forklift_only(orders: pd.DataFrame) -> List[str]:
    if "required_equipment" not in orders.columns:
        return []
    chosen: List[str] = []
    for row in orders.itertuples(index=False):
        equipment = getattr(row, "required_equipment", "")
        if equipment is None or (isinstance(equipment, float) and pd.isna(equipment)):
            continue
        if str(equipment).strip() == "forklift":
            chosen.append(row.order_id)
    return chosen


def assignment_metrics(
    result: dict,
    orders: pd.DataFrame,
    pickers: pd.DataFrame,
    durations: Mapping[str, float],
) -> dict:
    """Released weight, lateness, jack and forklift minutes for one dock solve."""
    utilisation = utilisation_rows(pickers, result.get("picker_workload") or {})
    jack = [row for row in utilisation if row["equipment_type"] == "pallet_jack"]
    fork = [row for row in utilisation if row["equipment_type"] == "forklift"]
    jack_busy = sum(float(row["busy_minutes"]) for row in jack)
    jack_available = sum(float(row["available_minutes"]) for row in jack)
    fork_busy = sum(float(row["busy_minutes"]) for row in fork)
    fulfilled = set(result.get("orders_fulfilled") or [])
    only = _forklift_only(orders)
    only_assigned = [order_id for order_id in only if order_id in fulfilled]
    released = None
    if result.get("is_optimal") and result.get("objective_value") is not None:
        released = float(result["objective_value"])
    busiest = max(utilisation, key=lambda row: float(row["busy_minutes"]))
    return {
        "released_weight": released,
        "released_jobs": len(fulfilled),
        "job_count": len(orders),
        "dropped": len(result.get("orders_not_fulfilled") or []),
        "late_minutes": float(result.get("late_minutes") or 0.0),
        "jack_busy": jack_busy,
        "jack_available": jack_available,
        "forklift_busy": fork_busy,
        "forklift_only_jobs": len(only),
        "forklift_only_assigned": len(only_assigned),
        "forklift_only_minutes": sum(float(durations[order_id]) for order_id in only_assigned),
        "max_picker_id": busiest["picker_id"],
        "max_picker_busy": float(busiest["busy_minutes"]),
        "is_optimal": bool(result.get("is_optimal")),
        "stop": result.get("stop"),
        "utilisation": utilisation,
    }


def solve_depot(
    orders: pd.DataFrame,
    pickers: pd.DataFrame,
    durations: Mapping[str, float],
    *,
    lateness_weight: float,
    staged: bool,
    load_stops: Optional[Mapping[str, float]],
    time_limit: float,
) -> dict:
    """One dock assignment. The caller already routed the jobs."""
    return solve_order_assignment(
        orders,
        pickers,
        dict(durations),
        lateness_weight=lateness_weight,
        staged_assignment=staged,
        load_stops=None if load_stops is None else dict(load_stops),
        time_limit=time_limit,
        assignment_solver="highs",
    )


def _screen(tables: Sequence[pd.DataFrame]) -> str:
    try:
        require_shared_crew(tables)
    except ValueError as exc:
        return str(exc)
    return shared_crew_screen_message(len(tables))


def _scale(durations: Mapping[str, float], factor: float) -> Dict[str, float]:
    return {order_id: float(value) * factor for order_id, value in durations.items()}


def frozen_congestion(
    path: Path,
    depot_id: str,
    plan: str,
    factors: Sequence[float],
) -> List[dict]:
    """Scale one saved utilisation file. This does not build a congestion model."""
    frame = pd.read_csv(path)
    rows: List[dict] = []
    for factor in factors:
        for record in frame.itertuples(index=False):
            busy = float(record.busy_minutes) * factor
            available = float(record.available_minutes)
            rows.append(
                {
                    "plan": plan,
                    "depot_id": depot_id,
                    "picker_id": record.picker_id,
                    "equipment_type": record.equipment_type,
                    "factor": factor,
                    "busy_minutes": busy,
                    "available_minutes": available,
                    "exceeds_shift": busy > available + 1e-4,
                }
            )
    return rows


def _write_summary(path: Path, rows: Sequence[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(list(rows)).to_csv(path, index=False)


def run_saved_wave(
    by_depot: Path,
    pickers: pd.DataFrame,
    travel: pd.DataFrame,
    loads: pd.DataFrame,
    out_dir: Path,
    time_limit: float,
) -> List[dict]:
    """Route the saved jobs once, then solve the assignment variants."""
    summaries: List[dict] = []
    gap_rows: List[dict] = []
    prepared = []
    for folder in sorted(path for path in by_depot.iterdir() if path.is_dir()):
        orders = pd.read_csv(folder / "orders.csv")
        items = pd.read_csv(folder / "order_items.csv")
        print(f"Routing {folder.name} ({len(orders)} jobs)", flush=True)
        walks = measure_walks(folder.name, items, travel, folder / "jobs.csv")
        gap_rows.extend(walks)
        durations = {row["order_id"]: row["heuristic_duration"] for row in walks}
        held = {row["order_id"]: row["held_karp_duration"] for row in walks}
        prepared.append(
            {
                "depot": folder.name,
                "orders": orders,
                "items": items,
                "durations": durations,
                "held": held,
                "stops": job_load_stops(items, loads),
                "trucks": _truck_of(orders),
            }
        )
    _write_summary(out_dir / "phase1_gap.csv", gap_rows)
    gaps = [float(row["gap"]) for row in gap_rows]
    summary = walk_gap_summary(gaps)
    worst = max(gap_rows, key=lambda row: float(row["gap"]))
    stored_delta = []
    for row in gap_rows:
        if row["stored_walk"] == "":
            continue
        stored_delta.append(abs(float(row["stored_walk"]) - float(row["held_karp_walk"])))
    open_swaps = sum(1 for row in gap_rows if row["open_swap"])
    negative = sum(1 for gap in gaps if gap < -1e-6)
    print(
        "Phase 1 walk gap: "
        f"mean {summary['mean']:.4f}, p95 {summary['p95']:.4f}, "
        f"worst {worst['order_id']} {float(worst['gap']):.4f} "
        f"({worst['depot_id']}). Open swaps {open_swaps}. "
        f"Heuristic shorter than Held-Karp: {negative}.",
        flush=True,
    )
    _write_summary(
        out_dir / "phase1_gap_summary.csv",
        [
            {
                **summary,
                "worst_order_id": worst["order_id"],
                "worst_depot_id": worst["depot_id"],
                "worst_heuristic_walk": worst["heuristic_walk"],
                "worst_held_karp_walk": worst["held_karp_walk"],
                "open_swaps": open_swaps,
                "heuristic_shorter_than_held_karp": negative,
                "max_abs_stored_minus_held_karp": max(stored_delta) if stored_delta else "",
            }
        ],
    )

    variants = [
        ("lateness0_heuristic", "durations", 0.0, False, False, 1.0),
        ("lateness0_x110", "durations", 0.0, False, False, 1.10),
        ("lateness0_x120", "durations", 0.0, False, False, 1.20),
        ("staged_heuristic", "durations", 1.0, True, False, 1.0),
        ("staged_held_karp", "held", 1.0, True, False, 1.0),
        ("staged_load_stops", "durations", 1.0, True, True, 1.0),
    ]
    for name, duration_key, lateness, staged, use_stops, factor in variants:
        print(f"Assigning {name}", flush=True)
        tables = []
        plan_rows = []
        for depot in prepared:
            durations = _scale(depot[duration_key], factor)
            stops = depot["stops"] if use_stops else None
            result = solve_depot(
                depot["orders"],
                pickers,
                durations,
                lateness_weight=lateness,
                staged=staged,
                load_stops=stops,
                time_limit=time_limit,
            )
            metrics = assignment_metrics(result, depot["orders"], pickers, durations)
            schedule = result.get("schedule") or []
            inversions = load_inversions(schedule, depot["trucks"], depot["stops"])
            staging = staging_peaks(schedule, staging_buffer=30.0)
            report_dir = out_dir / name / depot["depot"]
            write_assignment_reports(
                report_dir,
                metrics["utilisation"],
                result.get("drops") or [],
                schedule,
                result.get("stages") or None,
            )
            pd.DataFrame(inversions["pairs"]).to_csv(report_dir / "inversions.csv", index=False)
            tables.append(pd.DataFrame(metrics["utilisation"]))
            plan_rows.append(
                {
                    "plan": name,
                    "depot_id": depot["depot"],
                    "released_weight": metrics["released_weight"],
                    "released_jobs": metrics["released_jobs"],
                    "job_count": metrics["job_count"],
                    "dropped": metrics["dropped"],
                    "late_minutes": metrics["late_minutes"],
                    "jack_busy": metrics["jack_busy"],
                    "jack_available": metrics["jack_available"],
                    "forklift_busy": metrics["forklift_busy"],
                    "forklift_only_jobs": metrics["forklift_only_jobs"],
                    "forklift_only_assigned": metrics["forklift_only_assigned"],
                    "forklift_only_minutes": metrics["forklift_only_minutes"],
                    "max_picker_id": metrics["max_picker_id"],
                    "max_picker_busy": metrics["max_picker_busy"],
                    "inversions": inversions["count"],
                    "staging_peak": staging["peak"],
                    "staging_peak_minute": staging["peak_minute"],
                    "finished_after_departure": staging["finished_after_departure"],
                    "is_optimal": metrics["is_optimal"],
                    "stop": metrics["stop"],
                    "duration_factor": factor,
                    "shared_crew": "",
                }
            )
        screen = _screen(tables)
        for row in plan_rows:
            row["shared_crew"] = screen
        summaries.extend(plan_rows)
        print(f"  {screen}", flush=True)
        _write_summary(out_dir / "assignment_summary.csv", summaries)
    return summaries


def run_two_tier(
    loads: pd.DataFrame,
    travel: pd.DataFrame,
    pickers: pd.DataFrame,
    picker_frame: pd.DataFrame,
    out_dir: Path,
    saved_solve: Path,
    time_limit: float,
) -> dict:
    """Rechunk with the jack-tier rule, then one staged assignment."""
    print("Two-tier rechunk (jack_tier_merges, mix stops)", flush=True)
    result = chunk_loads_model(
        loads,
        travel,
        pickers,
        ChunkModelSettings(jack_tier_merges=True),
    )
    chunk_dir = out_dir / "two_tier_chunk"
    write_model_outputs(result, chunk_dir)
    saved_minutes = ""
    if saved_solve.is_file():
        saved = pd.read_csv(saved_solve)
        saved_minutes = float(saved["minutes"].sum())
    fallbacks = sum(1 for solve in result.solves if solve.fallback)
    print(
        f"Two-tier cover {len(result.orders)} jobs, {result.objective:.3f} minutes, "
        f"fallbacks {fallbacks}",
        flush=True,
    )
    tables = []
    rows = []
    for depot_id in sorted(result.orders["depot_id"].unique()):
        orders = result.orders[result.orders.depot_id == depot_id].copy()
        items = result.items[result.items.order_id.isin(set(orders.order_id))].copy()
        print(f"Routing two-tier {depot_id} ({len(orders)} jobs)", flush=True)
        routes = route_orders(items, str(depot_id), travel, solver="heuristic")
        durations = {order_id: float(route["route_duration"]) for order_id, route in routes.items()}
        # Same sequence as the saved staged plan: earliest deadline, order-id ties.
        solved = solve_depot(
            orders,
            picker_frame,
            durations,
            lateness_weight=1.0,
            staged=True,
            load_stops=None,
            time_limit=time_limit,
        )
        metrics = assignment_metrics(solved, orders, picker_frame, durations)
        report_dir = out_dir / "two_tier_staged" / str(depot_id)
        write_assignment_reports(
            report_dir,
            metrics["utilisation"],
            solved.get("drops") or [],
            solved.get("schedule") or [],
            solved.get("stages") or None,
        )
        tables.append(pd.DataFrame(metrics["utilisation"]))
        rows.append(
            {
                "plan": "two_tier_staged",
                "depot_id": depot_id,
                "released_weight": metrics["released_weight"],
                "released_jobs": metrics["released_jobs"],
                "job_count": metrics["job_count"],
                "dropped": metrics["dropped"],
                "late_minutes": metrics["late_minutes"],
                "jack_busy": metrics["jack_busy"],
                "jack_available": metrics["jack_available"],
                "forklift_busy": metrics["forklift_busy"],
                "forklift_only_jobs": metrics["forklift_only_jobs"],
                "forklift_only_assigned": metrics["forklift_only_assigned"],
                "forklift_only_minutes": metrics["forklift_only_minutes"],
                "max_picker_id": metrics["max_picker_id"],
                "max_picker_busy": metrics["max_picker_busy"],
                "is_optimal": metrics["is_optimal"],
                "stop": metrics["stop"],
                "cover_minutes": result.objective,
                "saved_cover_minutes": saved_minutes,
                "fallbacks": fallbacks,
                "shared_crew": "",
            }
        )
    screen = _screen(tables)
    for row in rows:
        row["shared_crew"] = screen
    print(f"  {screen}", flush=True)
    return {"rows": rows, "objective": result.objective, "jobs": len(result.orders)}


def main(argv: Optional[Sequence[str]] = None) -> None:
    """Run the five measurements and write ``wave_trials``."""
    parser = argparse.ArgumentParser(description="Measure the five open picker trials.")
    parser.add_argument("--by-depot", type=Path, default=_ROOT / "outputs" / "chunk_model" / "by_depot")
    parser.add_argument("--loads", type=Path, default=_ROOT / "inputs" / "chunk_warehouse" / "truck_loads.csv")
    parser.add_argument("--travel", type=Path, default=_ROOT / "inputs" / "layout_warehouse" / "travel_times.csv")
    parser.add_argument("--pickers", type=Path, default=_ROOT / "inputs" / "layout_warehouse" / "pickers.csv")
    parser.add_argument("--out", type=Path, default=_ROOT / "outputs" / "chunk_assign" / "wave_trials")
    parser.add_argument(
        "--frozen-base",
        type=Path,
        default=_ROOT / "outputs" / "chunk_assign",
        help="Parent of PACK_A/base and PACK_B/base. The stress test reads those utilisation files.",
    )
    parser.add_argument("--time-limit", type=float, default=60.0)
    parser.add_argument("--skip-two-tier", action="store_true")
    args = parser.parse_args(list(argv) if argv is not None else None)
    pickers = pd.read_csv(args.pickers)
    travel = pd.read_csv(args.travel)
    loads = pd.read_csv(args.loads)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "staging_assumptions.txt").write_text(
        staging_peaks([], 30.0)["assumptions"] + "\n",
        encoding="utf-8",
    )
    frozen_rows: List[dict] = []
    plans = (
        ("base", "prefer-0 baseline"),
        ("gap_wave/staged", "gap-wave staged"),
    )
    for depot_id in ("PACK_A", "PACK_B"):
        for folder, label in plans:
            path = args.frozen_base / folder / depot_id / "utilisation.csv"
            if folder == "base":
                path = args.frozen_base / depot_id / "base" / "utilisation.csv"
            if not path.is_file():
                if folder == "base" and depot_id == "PACK_B":
                    raise FileNotFoundError(f"Prefer-0 baseline utilisation is missing: {path}")
                continue
            frozen_rows.extend(
                frozen_congestion(path, depot_id, f"{label} {path.as_posix()}", (1.0, 1.10, 1.20))
            )
    _write_summary(args.out / "congestion_frozen.csv", frozen_rows)
    over = [row for row in frozen_rows if row["exceeds_shift"] and row["factor"] > 1.0]
    print(f"Frozen scaled minutes over the shift: {len(over)} picker-rows", flush=True)
    run_saved_wave(args.by_depot, pickers, travel, loads, args.out, args.time_limit)
    if args.skip_two_tier:
        return
    tier = run_two_tier(
        loads,
        travel,
        read_pickers(pickers),
        pickers,
        args.out,
        _ROOT / "outputs" / "chunk_model" / "solve.csv",
        args.time_limit,
    )
    existing = pd.read_csv(args.out / "assignment_summary.csv")
    pd.concat([existing, pd.DataFrame(tier["rows"])], ignore_index=True).to_csv(
        args.out / "assignment_summary.csv", index=False
    )


if __name__ == "__main__":
    main()
