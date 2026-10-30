"""Command line for the trip planner.

Reads the shipment CSVs, builds candidates, and asks HiGHS for the cheapest
set of trips that covers every shipment once.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import pandas as pd

from candidates import generate_candidates
from config import TripConfig, config_field_names, load_config_file
from reporting import PlanResult, assemble_plan, summary_map, write_plan
from shipments import build_shipments, read_inputs
from trip_model import solve_trip_model


def plan(
    orders: pd.DataFrame,
    customers: pd.DataFrame,
    rates: pd.DataFrame,
    trucks: pd.DataFrame,
    cfg: TripConfig | None = None,
    overrides: pd.DataFrame | None = None,
) -> PlanResult:
    """Run the pipeline on tables already in memory."""
    started = time.perf_counter()
    if cfg is None:
        cfg = TripConfig()
    built = build_shipments(orders, customers, rates, trucks, cfg)
    candidates = generate_candidates(built.shipments, rates, trucks, cfg, overrides)
    solved = solve_trip_model(
        candidates.candidates,
        [shipment.shipment_id for shipment in candidates.shipments],
        cfg,
    )
    return assemble_plan(
        orders,
        customers,
        rates,
        trucks,
        cfg,
        built,
        candidates,
        solved,
        time.perf_counter() - started,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Plan truck trips that cover each shipment once at minimum freight cost."
    )
    parser.add_argument("--inputs", type=Path, required=True, help="Folder with orders, customers, rates, and trucks CSVs.")
    parser.add_argument("--out", type=Path, required=True, help="Folder for trips, exclusions, summary, and flags.")
    parser.add_argument("--config", type=Path, default=None, help="YAML or JSON overrides for section 5 parameters.")
    parser.add_argument("--max-shipments-per-trip", type=int, default=None)
    parser.add_argument("--max-early-days", type=int, default=None)
    parser.add_argument("--transit-days", type=int, default=None)
    parser.add_argument(
        "--compat-mode",
        choices=["same_province", "same_group", "same_province_or_group", "any", "none"],
        default=None,
    )
    parser.add_argument("--handling-basis", choices=["per_drop", "per_truck"], default=None)
    parser.add_argument("--max-drops-per-trip", type=int, default=None)
    parser.add_argument("--max-trucks", type=int, default=None, help="Fleet cap per ship date. Off unless set.")
    parser.add_argument("--time-limit", type=float, default=None, help="Seconds per connected component. Default 600.")
    parser.add_argument("--solo-only", default=None, help="Comma-separated ship-tos that never consolidate.")
    return parser


def config_from_args(args: argparse.Namespace) -> TripConfig:
    data: dict = {}
    if args.config is not None:
        data.update(load_config_file(args.config))
    cli = {
        "max_shipments_per_trip": args.max_shipments_per_trip,
        "max_early_days": args.max_early_days,
        "transit_days": args.transit_days,
        "compat_mode": args.compat_mode,
        "handling_basis": args.handling_basis,
        "max_drops_per_trip": args.max_drops_per_trip,
        "max_trucks": args.max_trucks,
        "time_limit_s": args.time_limit,
    }
    for key, value in cli.items():
        if value is not None:
            data[key] = value
    if args.solo_only:
        data["solo_only"] = tuple(part.strip() for part in str(args.solo_only).split(",") if part.strip())
    unknown = sorted(set(data) - config_field_names())
    if unknown:
        raise ValueError("Unknown config keys: " + ", ".join(unknown))
    return TripConfig(**data)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = config_from_args(args)
    tables = read_inputs(args.inputs)
    result = plan(
        tables.orders,
        tables.customers,
        tables.rates,
        tables.trucks,
        cfg,
        tables.overrides,
    )
    write_plan(result, args.out)
    _print_result(result, args.out)
    if not result.ok:
        for error in result.errors:
            print(error, file=sys.stderr)
        return 1
    return 0


def _print_result(result: PlanResult, out_dir: Path) -> None:
    values = summary_map(result)
    print(f"Shipments: {result.n_shipments}")
    print(f"Candidates: {result.n_candidates}")
    print(f"Trips: {values.get('trips', '0')}")
    print(f"Cost: {result.total_cost:,.2f} THB")
    print(f"Baseline (dateable solos): {result.baseline_cost:,.2f} THB")
    print(f"Baseline (all built shipments): {result.baseline_cost_all_built:,.2f} THB")
    saving = values.get("saving_thb", "")
    if saving:
        print(f"Saving: {saving} THB ({values.get('saving_percent', '')}%)")
    print(f"Solver: {values.get('solver_status', '')} gap {values.get('solver_gap_max', '')}")
    print(f"Runtime: {result.runtime_s:.2f} s")
    print(f"Wrote {out_dir}")
    print(values.get("assumptions", ""))


if __name__ == "__main__":
    raise SystemExit(main())
