"""Re-assign one routed wave under a few crew and deadline scenarios.

Phase 1 runs once. Each scenario then changes the picker file or the
deadlines and solves phase 2 again. The comparison columns are the
assignment results: released jobs, busy minutes, late minutes, drops, and
utilisation. They are not the chunker's Held-Karp total.

Run from ``order_picking/``::

    python source/run_assignment_scenarios.py --orders ... --items ... --pickers ... --travel ... --depot PACK_A --scenarios scenarios/assignment.yaml --out outputs/scenarios/PACK_A
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Optional, Sequence

import pandas as pd

from assignment_reports import (
    format_assignment_report,
    parse_scenarios,
    remove_pickers,
    shift_deadlines,
    utilisation_rows,
    write_assignment_reports,
)
from order_picking_optimizer import route_orders, solve_order_assignment


def _rollup_pct(rows) -> tuple:
    busy = sum(float(row["busy_minutes"]) for row in rows)
    available = sum(float(row["available_minutes"]) for row in rows)
    pct = 0.0 if available <= 0 else 100.0 * busy / available
    return busy, pct


def run_scenarios(
    orders: pd.DataFrame,
    order_items: pd.DataFrame,
    pickers: pd.DataFrame,
    distance_matrix: pd.DataFrame,
    depot_id: str,
    scenarios: Sequence[dict],
    out_dir: Path,
    phase1_solver: str = "heuristic",
    lateness_weight: float = 0.0,
    prefer_jack: float = 0.0,
    require_must_ship: bool = False,
    balance_weight: float = 0.0,
    time_limit: float = 10.0,
) -> pd.DataFrame:
    """Route once, assign each scenario, and write ``comparison.csv`` plus reports."""
    order_ids = [row.order_id for row in orders.itertuples(index=False)]
    routes = route_orders(order_items, depot_id, distance_matrix, phase1_solver, None)
    missing = [oid for oid in order_ids if oid not in routes]
    if missing:
        raise ValueError(f"Orders have no pick lines: {missing}.")
    durations = {oid: routes[oid]["route_duration"] for oid in order_ids}
    out_dir.mkdir(parents=True, exist_ok=True)
    comparison = []
    for scenario in scenarios:
        name = scenario["name"]
        scenario_pickers = remove_pickers(pickers, scenario["remove"])
        scenario_orders = shift_deadlines(orders, float(scenario["deadline_minutes"]))
        result = solve_order_assignment(
            scenario_orders,
            scenario_pickers,
            durations,
            balance_weight=balance_weight,
            time_limit=time_limit,
            lateness_weight=lateness_weight,
            prefer_jack=prefer_jack,
            require_must_ship=require_must_ship,
        )
        report_dir = out_dir / name
        if result["is_optimal"]:
            utilisation = utilisation_rows(scenario_pickers, result["picker_workload"])
            write_assignment_reports(report_dir, utilisation, result["drops"], result["schedule"])
            busy, pct = _rollup_pct(utilisation)
            comparison.append(
                {
                    "scenario": name,
                    "status": "optimal",
                    "released": len(result["orders_fulfilled"]),
                    "dropped": len(result["drops"]),
                    "busy_minutes": busy,
                    "late_jobs": len(result["late_jobs"]),
                    "late_minutes": result["late_minutes"],
                    "utilisation_pct": pct,
                }
            )
            print(name)
            print(format_assignment_report(utilisation, result["drops"], result["schedule"]))
        else:
            comparison.append(
                {
                    "scenario": name,
                    "status": "infeasible" if result.get("infeasible_must_ship") else "not_optimal",
                    "released": 0,
                    "dropped": 0,
                    "busy_minutes": 0.0,
                    "late_jobs": 0,
                    "late_minutes": 0.0,
                    "utilisation_pct": 0.0,
                }
            )
            print(f"{name}: {comparison[-1]['status']}")
    frame = pd.DataFrame(comparison)
    frame.to_csv(out_dir / "comparison.csv", index=False)
    print(f"Wrote {out_dir / 'comparison.csv'}")
    return frame


def main(argv: Optional[Sequence[str]] = None) -> None:
    """Run the scenario file and write the comparison table."""
    parser = argparse.ArgumentParser(description="Assign one wave under several crew and deadline scenarios.")
    parser.add_argument("--orders", required=True, type=Path)
    parser.add_argument("--items", required=True, type=Path)
    parser.add_argument("--pickers", required=True, type=Path)
    parser.add_argument("--travel", required=True, type=Path)
    parser.add_argument("--depot", required=True)
    parser.add_argument("--scenarios", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--phase1", default="heuristic", choices=("heuristic", "scip", "highs"))
    parser.add_argument("--lateness-weight", type=float, default=0.0)
    parser.add_argument("--prefer-jack", type=float, default=0.0)
    parser.add_argument("--require-must-ship", action="store_true")
    parser.add_argument("--balance-weight", type=float, default=0.0)
    parser.add_argument("--time-limit", type=float, default=10.0)
    args = parser.parse_args(list(argv) if argv is not None else None)
    scenarios = parse_scenarios(args.scenarios.read_text(encoding="utf-8"))
    run_scenarios(
        pd.read_csv(args.orders),
        pd.read_csv(args.items),
        pd.read_csv(args.pickers),
        pd.read_csv(args.travel),
        args.depot,
        scenarios,
        args.out,
        phase1_solver=args.phase1,
        lateness_weight=args.lateness_weight,
        prefer_jack=args.prefer_jack,
        require_must_ship=args.require_must_ship,
        balance_weight=args.balance_weight,
        time_limit=args.time_limit,
    )


if __name__ == "__main__":
    main()
