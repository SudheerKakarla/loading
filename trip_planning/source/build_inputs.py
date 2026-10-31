"""Write the nine workbook-build CSVs.

The builder stages every file, reads the four contract CSVs back, and only
then publishes. An existing directory is replaced file by file with rollback.
A validation error leaves the previous build in place. This command does not
catch validation errors: they propagate to the caller.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from baseline_cost import build_baseline, parse_rates, parse_trucks, summary_frame
from publication import publish_replacing, remove_tree, staging_dir
from shipments import build_shipments, read_inputs
from va05_adapter import read_workbook

BUILDER_EXCLUSION_COLUMNS = [
    "order_id",
    "shipment_id",
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
_MODULE = Path(__file__).resolve().parents[1]
_REPO = Path(__file__).resolve().parents[2]


def default_workbook() -> Path:
    """The single VA05 workbook at the repo root or in ``trip_planning``."""
    found: list[Path] = []
    for root in (_REPO, _MODULE):
        if not root.is_dir():
            continue
        for path in sorted(root.glob("*.xlsx")):
            if "va05" in path.name.lower():
                found.append(path.resolve())
    unique = list(dict.fromkeys(found))
    if len(unique) == 1:
        return unique[0]
    if not unique:
        raise FileNotFoundError("No VA05 workbook at the repo root or in trip_planning. Pass --workbook.")
    names = ", ".join(str(path) for path in unique)
    raise FileNotFoundError(f"More than one VA05 workbook. Pass --workbook. Found: {names}.")


def default_output_dir() -> Path:
    """``trip_planning/outputs/va05_aug2025``."""
    return _MODULE / "outputs" / "va05_aug2025"


def build_contract(workbook: Path, out_dir: Path) -> dict[str, int]:
    """Publish nine validated CSVs built from ``workbook``."""
    extract = read_workbook(workbook)
    destination = Path(out_dir)
    staging = staging_dir(destination)
    remove_tree(staging)
    staging.mkdir(parents=True)
    try:
        _write_csv(staging / "orders.csv", extract.orders)
        _write_csv(staging / "customers.csv", extract.customers)
        _write_csv(staging / "rates.csv", extract.rates)
        _write_csv(staging / "trucks.csv", extract.trucks)
        tables = read_inputs(staging)
        built = build_shipments(tables.orders, tables.customers, tables.rates, tables.trucks)
        detail, summary = build_baseline(
            built.shipments,
            parse_rates(tables.rates),
            parse_trucks(tables.trucks),
        )
        for key, value in extract.diagnostics.items():
            summary[key] = value
        exclusions = _builder_exclusions(built.exclusions)
        _write_csv(staging / "shipments.csv", built.shipments)
        _write_csv(staging / "exclusions.csv", exclusions)
        _write_csv(staging / "issues.csv", extract.issues)
        _write_csv(staging / "baseline_shipments.csv", detail)
        _write_csv(staging / "baseline_summary.csv", summary_frame(summary))
        publish_replacing(staging, destination)
    except Exception:
        remove_tree(staging)
        raise
    return dict(extract.diagnostics)


def _builder_exclusions(exclusions: pd.DataFrame) -> pd.DataFrame:
    if exclusions.empty:
        return pd.DataFrame(columns=BUILDER_EXCLUSION_COLUMNS)
    missing = [name for name in BUILDER_EXCLUSION_COLUMNS if name not in exclusions.columns]
    if missing:
        raise ValueError("Builder exclusions are missing columns: " + ", ".join(missing))
    return exclusions.loc[:, BUILDER_EXCLUSION_COLUMNS].reset_index(drop=True)


def _write_csv(path: Path, frame: pd.DataFrame) -> None:
    frame.to_csv(path, index=False)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build trip-planning CSVs from a VA05 workbook.")
    parser.add_argument("--workbook", type=Path, default=None, help="VA05 workbook. Default: the only *VA05*.xlsx nearby.")
    parser.add_argument("--out", type=Path, default=None, help="Output directory. Default: trip_planning/outputs/va05_aug2025.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    workbook = default_workbook() if args.workbook is None else args.workbook
    out_dir = default_output_dir() if args.out is None else args.out
    build_contract(workbook, out_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
