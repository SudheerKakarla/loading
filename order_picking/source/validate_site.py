"""Check a site folder and build its travel matrix.

The folder holds the layout files the travel builder already reads, plus a
line file. ``location_type`` on ``locations.csv`` is optional. The builder
does not use it. This command checks the value and then ignores it.

``--speed`` is distance units per minute. The default is 1, the same default
as the travel builder. It is not a measured walking speed.

Run from ``order_picking/``::

    python source/validate_site.py --site inputs/site_template --out outputs/site_template/travel_times.csv
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional, Sequence

import pandas as pd

from build_travel_matrix import build_travel_matrix, travel_matrix_frame

_LAYOUT = ("aisles.csv", "cross_aisles.csv", "locations.csv", "depots.csv")


def validate_site(folder: Path, lines: Optional[Path] = None, speed: float = 1.0) -> pd.DataFrame:
    """Build the travel matrix and require every pick location to be in it."""
    site = Path(folder)
    tables = {name: _read(site / name) for name in _LAYOUT}
    _check_location_types(tables["locations.csv"], site / "locations.csv")
    line_path = Path(lines) if lines is not None else _line_file(site)
    line_frame = _read(line_path)
    if "location_id" not in line_frame.columns:
        raise ValueError(f"{line_path} is missing column location_id.")
    matrix = build_travel_matrix(
        tables["aisles.csv"],
        tables["cross_aisles.csv"],
        tables["locations.csv"],
        depots=tables["depots.csv"],
        speed=speed,
    )
    depots = {str(row.depot_id).strip() for row in tables["depots.csv"].itertuples(index=False)}
    for offset, raw in enumerate(line_frame.to_dict(orient="records"), start=2):
        location_id = "" if _blank(raw.get("location_id")) else str(raw.get("location_id")).strip()
        if location_id == "":
            raise ValueError(f"{line_path} row {offset} column location_id is blank.")
        if location_id not in matrix:
            raise ValueError(
                f"{line_path} row {offset} column location_id {location_id} is not in the travel matrix."
            )
        if "depot_id" in line_frame.columns:
            depot_id = "" if _blank(raw.get("depot_id")) else str(raw.get("depot_id")).strip()
            if depot_id and depot_id not in depots:
                raise ValueError(f"{line_path} row {offset} column depot_id {depot_id} is not in depots.csv.")
    return travel_matrix_frame(matrix)


def _line_file(site: Path) -> Path:
    for name in ("loads.csv", "order_items.csv"):
        path = site / name
        if path.is_file():
            return path
    raise ValueError(f"{site} has no loads.csv or order_items.csv.")


def _check_location_types(frame: pd.DataFrame, path: Path) -> None:
    if "location_type" not in frame.columns:
        return
    for offset, raw in enumerate(frame.to_dict(orient="records"), start=2):
        if _blank(raw.get("location_type")):
            continue
        text = str(raw.get("location_type")).strip().lower()
        if text not in {"bulk", "pickface"}:
            raise ValueError(f"{path} row {offset} column location_type must be bulk or pickface.")


def _read(path: Path) -> pd.DataFrame:
    if not path.is_file():
        raise ValueError(f"Site file not found: {path}")
    frame = pd.read_csv(path)
    frame.columns = [str(name).strip() for name in frame.columns]
    if frame.empty:
        raise ValueError(f"{path} has no rows.")
    return frame


def _blank(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, float) and value != value:
        return True
    return str(value).strip() == ""


def main(argv: Optional[Sequence[str]] = None) -> None:
    """Validate a site folder and write the travel matrix."""
    parser = argparse.ArgumentParser(description="Validate a site folder and write its travel matrix.")
    parser.add_argument("--site", type=Path, required=True)
    parser.add_argument("--lines", type=Path, default=None, help="Pick lines. Default is loads.csv, then order_items.csv.")
    parser.add_argument("--speed", type=float, default=1.0, help="Distance units per minute. Default 1.")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        frame = validate_site(args.site, args.lines, args.speed)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        sys.exit(1)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(out, index=False)
    print(f"Wrote {len(frame)} travel rows to {out}")


if __name__ == "__main__":
    main()
