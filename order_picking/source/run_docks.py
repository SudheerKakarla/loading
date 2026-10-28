"""Solve each depot folder, then screen the shared crew by summed minutes.

Each depot is its own picker solve and sees the full picker file. After
every depot has a utilisation table, ``require_shared_crew`` sums busy
minutes and compares them with ``shift_duration``. A pass is that total.
Clock overlap and the walk between docks stay outside the screen. It does
not build a joint dock model.

Run from ``order_picking/``::

    python source/run_docks.py --by-depot outputs/chunk_model/by_depot --pickers inputs/layout_warehouse/pickers.csv --travel inputs/layout_warehouse/travel_times.csv --out outputs/chunk_assign/gap_wave/weighted --lateness-weight 1 --time-limit 60
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import List, Optional, Sequence

import pandas as pd

from assignment_reports import (
    require_shared_crew,
    shared_crew_screen_message,
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
) -> List[dict]:
    """Solve each depot, write its reports, then screen the summed minutes.

    ``available_minutes`` is each picker's ``shift_duration``. The screen
    runs only after every requested depot has been written. A pass is that
    total. Clock overlap and the walk between docks stay outside it.
    """
    folders = depot_folders(by_depot, depots)
    tables = []
    results: List[dict] = []
    for folder in folders:
        depot_id = folder.name
        result = solve_order_picking(
            pd.read_csv(folder / "orders.csv"),
            pd.read_csv(folder / "order_items.csv"),
            pickers,
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
        utilisation = utilisation_rows(pickers, result.get("picker_workload") or {})
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
    require_shared_crew(tables)
    print(shared_crew_screen_message(len(tables)))
    return results


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
    args = parser.parse_args(list(argv) if argv is not None else None)
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
    )


if __name__ == "__main__":
    main()
