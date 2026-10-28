"""Solve each depot folder, then screen the shared crew by summed minutes.

Each depot is its own picker solve. With no ``home_depot`` column, each solve
sees the full picker file. After every depot has a utilisation table,
``require_shared_crew`` sums busy minutes and compares them with the shift
left after ``non_pick_minutes``. A pass is that total. Clock overlap and the
walk between docks stay outside the screen. It does not build a joint dock
model. A blank ``home_depot`` still lets one picker into every dock.

Run from ``order_picking/``::

    python source/run_docks.py --by-depot outputs/chunk_model/by_depot --pickers inputs/layout_warehouse/pickers.csv --travel inputs/layout_warehouse/travel_times.csv --out outputs/chunk_assign/gap_wave/weighted --lateness-weight 1 --time-limit 60
"""

from __future__ import annotations

import argparse
import math
from pathlib import Path
from typing import Dict, List, Optional, Sequence

import pandas as pd

from assignment_reports import (
    require_shared_crew,
    shared_crew_screen_message,
    staging_peaks,
    utilisation_rows,
    write_assignment_reports,
)
from order_picking_optimizer import format_picking_solution, solve_order_picking


def depot_folders(by_depot: Path, depots: Optional[Sequence[str]] = None) -> List[Path]:
    """Depot directories that each contain ``orders.csv`` and ``order_items.csv``."""
    if depots:
        folders = [by_depot / name for name in depots]
    else:
        folders = sorted(path for path in by_depot.iterdir() if path.is_dir())
    if not folders:
        raise ValueError(f"No depot folders in {by_depot}.")
    for folder in folders:
        if not (folder / "orders.csv").is_file() or not (folder / "order_items.csv").is_file():
            raise ValueError(f"{folder} needs orders.csv and order_items.csv.")
    return folders


def _solver_log_rows(result: dict) -> List[dict]:
    """One row per stage, or one weighted row when the solve has no stages."""
    stages = list(result.get("stages") or [])
    if stages:
        return [
            {
                "stage": stage.get("stage"),
                "status": stage.get("status"),
                "incumbent": stage.get("incumbent"),
                "bound": stage.get("bound"),
                "solver_abs_gap": stage.get("abs_gap"),
                "mip_rel_gap_used": stage.get("mip_rel_gap_used"),
                "mip_abs_gap_used": stage.get("mip_abs_gap_used"),
                "objective_upper_used": stage.get("objective_upper_used"),
                "runtime": stage.get("runtime"),
                "time_limit_hit": stage.get("time_limit_hit"),
                "stop": stage.get("stop"),
            }
            for stage in stages
        ]
    return [
        {
            "stage": "weighted",
            "status": result.get("status"),
            "incumbent": result.get("objective_value"),
            "bound": result.get("solver_bound"),
            "solver_abs_gap": result.get("solver_abs_gap"),
            "mip_rel_gap_used": result.get("mip_rel_gap_used"),
            "mip_abs_gap_used": result.get("mip_abs_gap_used"),
            "objective_upper_used": result.get("objective_upper_used"),
            "runtime": result.get("runtime"),
            "time_limit_hit": result.get("time_limit_hit"),
            "stop": result.get("stop"),
        }
    ]


def pickers_for_depot(pickers: pd.DataFrame, depot_id: str) -> pd.DataFrame:
    """Pickers whose home is this dock, plus pickers with a blank home.

    A missing ``home_depot`` column leaves the full crew, which is the
    current dock solve. A named home is absent from every other dock.
    """
    if "home_depot" not in pickers.columns:
        return pickers

    def at_home(value: object) -> bool:
        if value is None or (isinstance(value, float) and math.isnan(value)):
            return True
        text = str(value).strip()
        if text == "" or text.lower() == "nan":
            return True
        return text == depot_id

    chosen = pickers.loc[pickers["home_depot"].map(at_home)].reset_index(drop=True)
    if chosen.empty:
        raise ValueError(
            f"{depot_id} has no picker with home_depot {depot_id} or a blank home_depot."
        )
    return chosen


def load_staging_capacities(path: Path) -> Dict[str, float]:
    """Read ``depot_id,capacity_pallets``. The count is a report, not a constraint."""
    staging_path = Path(path)
    if not staging_path.is_file():
        raise ValueError(f"Staging file not found: {staging_path}")
    frame = pd.read_csv(staging_path)
    frame.columns = [str(name).strip() for name in frame.columns]
    missing = [name for name in ("depot_id", "capacity_pallets") if name not in frame.columns]
    if missing:
        raise ValueError(f"{staging_path} is missing column(s): {', '.join(missing)}.")
    found: Dict[str, float] = {}
    for index, row in frame.iterrows():
        label = f"row {int(index) + 2} of {staging_path}"
        depot = "" if pd.isna(row.depot_id) else str(row.depot_id).strip()
        if depot == "" or depot.lower() == "nan":
            raise ValueError(f"staging {label} column depot_id is blank.")
        if depot in found:
            raise ValueError(f"staging {label} repeats depot_id {depot}.")
        try:
            capacity = float(row.capacity_pallets)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"staging {label} column capacity_pallets must be a number.") from exc
        if not math.isfinite(capacity) or capacity < 0:
            raise ValueError(f"staging {label} column capacity_pallets must be a finite number >= 0.")
        found[depot] = capacity
    if not found:
        raise ValueError(f"{staging_path} has no depot rows.")
    return found


def _job_pallet_counts(folder: Path) -> Dict[str, float]:
    """Pallet counts from ``jobs.csv`` when that column was written. Otherwise none."""
    path = folder / "jobs.csv"
    if not path.is_file():
        return {}
    jobs = pd.read_csv(path)
    if "pallets" not in jobs.columns or "job_id" not in jobs.columns:
        return {}
    counts: Dict[str, float] = {}
    for row in jobs.itertuples(index=False):
        if pd.isna(row.pallets):
            continue
        counts[str(row.job_id)] = float(row.pallets)
    return counts


def solve_docks(
    by_depot: Path,
    pickers: pd.DataFrame,
    distance_matrix: pd.DataFrame,
    out_dir: Path,
    depots: Optional[Sequence[str]] = None,
    phase1_solver: str = "heuristic",
    assignment_solver: str = "highs",
    lateness_weight: float = 0.0,
    prefer_jack: float = 0.0,
    require_must_ship: bool = False,
    staged_assignment: bool = False,
    withhold_if_late_at_start: bool = False,
    balance_weight: float = 0.0,
    time_limit: float = 10.0,
    staging_capacity: Optional[float] = None,
    staging_capacities: Optional[Dict[str, float]] = None,
    fail_on_overflow: bool = False,
) -> List[dict]:
    """Solve each depot, write its reports, then screen the summed minutes.

    ``available_minutes`` is each picker's shift after ``non_pick_minutes``.
    The screen runs only after every requested depot has been written. A pass
    is that total. Clock overlap and the walk between docks stay outside it.
    A named ``home_depot`` keeps that picker off the other docks. A blank home
    is still eligible everywhere, so one crew can still be counted twice.
    Staging capacity is a report. It does not change the solve.
    """
    if staging_capacity is not None and (
        not math.isfinite(staging_capacity) or staging_capacity < 0
    ):
        raise ValueError("staging capacity must be a finite number >= 0.")
    folders = depot_folders(by_depot, depots)
    tables = []
    results: List[dict] = []
    overflows: List[str] = []
    for folder in folders:
        depot_id = folder.name
        depot_pickers = pickers_for_depot(pickers, depot_id)
        result = solve_order_picking(
            pd.read_csv(folder / "orders.csv"),
            pd.read_csv(folder / "order_items.csv"),
            depot_pickers,
            distance_matrix,
            depot_id,
            phase1_solver=phase1_solver,
            phase2_time_limit=time_limit,
            assignment_solver=assignment_solver,
            lateness_weight=lateness_weight,
            prefer_jack=prefer_jack,
            require_must_ship=require_must_ship,
            staged_assignment=staged_assignment,
            withhold_if_late_at_start=withhold_if_late_at_start,
            balance_weight=balance_weight,
        )
        utilisation = utilisation_rows(depot_pickers, result.get("picker_workload") or {})
        report_dir = out_dir / depot_id
        write_assignment_reports(
            report_dir,
            utilisation,
            result.get("drops") or [],
            result.get("schedule") or [],
            result.get("stages") or None,
        )
        pd.DataFrame(_solver_log_rows(result)).to_csv(report_dir / "solver_log.csv", index=False)
        print(format_picking_solution(result))
        print(f"Wrote {report_dir}")
        tables.append(pd.DataFrame(utilisation))
        results.append(result)
        capacity = None
        if staging_capacities is not None and depot_id in staging_capacities:
            capacity = staging_capacities[depot_id]
        elif staging_capacity is not None:
            capacity = staging_capacity
        counts = _job_pallet_counts(folder)
        peaks = staging_peaks(
            result.get("schedule") or [],
            pallets_by_job=counts,
            capacity=capacity,
        )
        _write_staging(report_dir, depot_id, peaks, capacity)
        minute = peaks["peak_minute"]
        minute_text = "none" if minute is None else f"{minute:g}"
        line = f"Staging {depot_id}: peak {peaks['peak']:g} pallets at minute {minute_text}."
        if peaks["over_capacity"]:
            line += (
                f" Over capacity {capacity:g} from minute {peaks['first_over_minute']:g}."
            )
            overflows.append(depot_id)
        print(line)
    require_shared_crew(tables)
    print(shared_crew_screen_message(len(tables)))
    if fail_on_overflow and overflows:
        names = ", ".join(overflows)
        raise ValueError(f"Staging capacity exceeded at {names}.")
    return results


def _write_staging(report_dir: Path, depot_id: str, peaks: dict, capacity: Optional[float]) -> None:
    """One staging row for this dock. Written even when no capacity was set."""
    pd.DataFrame(
        [
            {
                "depot_id": depot_id,
                "peak": peaks["peak"],
                "peak_pallets": peaks.get("peak_pallets", peaks["peak"]),
                "capacity": "" if capacity is None else capacity,
                "over_capacity": peaks["over_capacity"],
                "first_over_minute": "" if peaks["first_over_minute"] is None else peaks["first_over_minute"],
            }
        ]
    ).to_csv(report_dir / "staging.csv", index=False)


def main(argv: Optional[Sequence[str]] = None) -> None:
    """Solve the depot folders and check the shared crew."""
    parser = argparse.ArgumentParser(
        description="Solve each depot, then screen the crew by summed busy minutes."
    )
    parser.add_argument("--by-depot", required=True, type=Path)
    parser.add_argument("--pickers", required=True, type=Path)
    parser.add_argument("--travel", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--depot", action="append", default=None, help="Depot folder name. Repeat to select.")
    parser.add_argument("--phase1", default="heuristic", choices=("heuristic", "scip", "highs"))
    parser.add_argument("--assignment", default="highs", choices=("highs", "gurobi", "auto"))
    parser.add_argument("--lateness-weight", type=float, default=0.0)
    parser.add_argument("--prefer-jack", type=float, default=0.0)
    parser.add_argument("--require-must-ship", action="store_true")
    parser.add_argument("--staged-assignment", action="store_true")
    parser.add_argument("--withhold-if-late-at-start", action="store_true")
    parser.add_argument("--balance-weight", type=float, default=0.0)
    parser.add_argument("--time-limit", type=float, default=10.0)
    parser.add_argument(
        "--staging-capacity",
        type=float,
        default=None,
        help="Report a pallet peak above this count. It does not change the solve.",
    )
    parser.add_argument(
        "--staging-file",
        type=Path,
        default=None,
        help="CSV of depot_id,capacity_pallets. A dock in this file uses its own count.",
    )
    parser.add_argument(
        "--fail-on-staging-overflow",
        action="store_true",
        help="Raise after every dock report is written when a peak exceeds capacity.",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    capacities = load_staging_capacities(args.staging_file) if args.staging_file is not None else None
    solve_docks(
        args.by_depot,
        pd.read_csv(args.pickers),
        pd.read_csv(args.travel),
        args.out,
        depots=args.depot,
        phase1_solver=args.phase1,
        assignment_solver=args.assignment,
        lateness_weight=args.lateness_weight,
        prefer_jack=args.prefer_jack,
        require_must_ship=args.require_must_ship,
        staged_assignment=args.staged_assignment,
        withhold_if_late_at_start=args.withhold_if_late_at_start,
        balance_weight=args.balance_weight,
        time_limit=args.time_limit,
        staging_capacity=args.staging_capacity,
        staging_capacities=capacities,
        fail_on_overflow=args.fail_on_staging_overflow,
    )


if __name__ == "__main__":
    main()
