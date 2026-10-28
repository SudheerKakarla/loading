"""Measure pick time, overhead, and walking speed from a task log.

The column names live in a caller map (``source_column,target_column``).
This command does not hard-code a site or a voice-system name. It writes
``mode_profile.csv`` and ``calibration_report.md``. Those files are a
measurement of the log. They are not defaults. A mode with fewer than
``--min-rows`` accepted rows keeps its profile cells blank.

Run from ``order_picking/``::

    python source/calibrate_from_task_log.py --log task_log.csv --column-map column_map.csv --out outputs/calibration
"""

from __future__ import annotations

import argparse
import math
import sys
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import pandas as pd

_PROFILE_COLUMNS = (
    "pick_mode",
    "max_job_lines",
    "max_job_weight",
    "max_job_volume",
    "default_pick_time",
    "job_overhead",
    "pallet_basis",
)
_REQUIRED = ("pick_mode", "job_id")
_OPTIONAL = (
    "pick_minutes",
    "start_minute",
    "finish_minute",
    "line_count",
    "overhead_minutes",
    "travel_minutes",
    "distance_m",
    "from_location",
    "to_location",
    "event_date",
)
_DEFAULT_MIN_ROWS = 5


def calibrate_log(
    log: Path,
    column_map: Path,
    out_dir: Path,
    distance: Optional[Path] = None,
    min_rows: int = _DEFAULT_MIN_ROWS,
) -> int:
    """Write the profile and the report. Return 1 when any row was rejected."""
    if min_rows < 1:
        raise ValueError("min_rows must be a whole number >= 1.")
    log_path = Path(log)
    export = _read_table(log_path, "Task log")
    mapping = _read_column_map(Path(column_map), export)
    distances = _read_distances(Path(distance), mapping) if distance is not None else None
    _check_speed_inputs(mapping, distances, Path(column_map))
    accepted: List[dict] = []
    rejected: List[dict] = []
    overhead_seen: Dict[Tuple[str, str], Tuple[float, int]] = {}
    for offset, raw in enumerate(export.to_dict(orient="records"), start=2):
        mapped = {target: str(raw[source]).strip() for source, target in mapping.items()}
        row, error, reason = _observation(mapped, offset, log_path, distances)
        if error:
            rejected.append(_rejection(offset, mapped.get("pick_mode", ""), mapped.get("job_id", ""), reason, error))
            continue
        key = (row["pick_mode"], row["job_id"])
        if row["overhead"] is not None:
            previous = overhead_seen.get(key)
            if previous is not None and abs(previous[0] - row["overhead"]) > 1e-9:
                rejected.append(
                    _rejection(
                        offset,
                        row["pick_mode"],
                        row["job_id"],
                        "overhead_conflict",
                        f"{log_path} row {offset} column overhead_minutes disagrees with row {previous[1]}.",
                    )
                )
                continue
            overhead_seen.setdefault(key, (row["overhead"], offset))
        accepted.append(row)
    folder = Path(out_dir)
    folder.mkdir(parents=True, exist_ok=True)
    profile = _profile_rows(accepted, min_rows)
    pd.DataFrame(profile, columns=list(_PROFILE_COLUMNS)).to_csv(folder / "mode_profile.csv", index=False)
    report = _report(accepted, rejected, profile, min_rows, log_path)
    (folder / "calibration_report.md").write_text(report, encoding="utf-8")
    if rejected:
        pd.DataFrame(
            rejected,
            columns=["source_row", "pick_mode", "job_id", "reason", "detail"],
        ).to_csv(folder / "calibration_rejected.csv", index=False)
    print(
        f"Wrote {len(profile)} mode row(s) from {len(accepted)} log row(s). "
        f"{len(rejected)} row(s) rejected."
    )
    print("Review calibration_report.md before any value becomes a default.")
    return 1 if rejected else 0


def _observation(
    mapped: Dict[str, str],
    offset: int,
    path: Path,
    distances: Optional[Dict[Tuple[str, str], float]],
) -> Tuple[Optional[dict], str, str]:
    mode = mapped.get("pick_mode", "").strip().lower()
    job_id = mapped.get("job_id", "").strip()
    if mode == "":
        return None, f"{path} row {offset} column pick_mode is blank.", "blank_mode"
    if job_id == "":
        return None, f"{path} row {offset} column job_id is blank.", "blank_job"
    pick, pick_error = _pick_minutes(mapped, offset, path)
    if pick_error:
        return None, pick_error, "bad_pick_time"
    lines, line_error = _line_count(mapped, offset, path)
    if line_error:
        return None, line_error, "bad_line_count"
    overhead, overhead_error = _optional_minutes(mapped, "overhead_minutes", offset, path)
    if overhead_error:
        return None, overhead_error, "bad_overhead"
    speed, speed_error = _speed(mapped, offset, path, distances)
    if speed_error:
        return None, speed_error, "bad_travel"
    return (
        {
            "pick_mode": mode,
            "job_id": job_id,
            "pick_per_line": pick / lines,
            "overhead": overhead,
            "speed": speed,
            "event_date": mapped.get("event_date", ""),
            "source_row": offset,
        },
        "",
        "",
    )


def _pick_minutes(mapped: Dict[str, str], offset: int, path: Path) -> Tuple[Optional[float], str]:
    if "pick_minutes" in mapped:
        return _required_minutes(mapped.get("pick_minutes", ""), offset, path, "pick_minutes")
    start, start_error = _required_minutes(mapped.get("start_minute", ""), offset, path, "start_minute")
    if start_error:
        return None, start_error
    finish, finish_error = _required_minutes(mapped.get("finish_minute", ""), offset, path, "finish_minute")
    if finish_error:
        return None, finish_error
    if finish + 1e-9 < start:
        return None, f"{path} row {offset} column finish_minute is before start_minute."
    return finish - start, ""


def _line_count(mapped: Dict[str, str], offset: int, path: Path) -> Tuple[float, str]:
    if "line_count" not in mapped or mapped.get("line_count", "") == "":
        return 1.0, ""
    number, error = _required_minutes(mapped["line_count"], offset, path, "line_count")
    if error:
        return 1.0, error
    if abs(number - round(number)) > 1e-9 or number < 1:
        return 1.0, f"{path} row {offset} column line_count must be a whole number >= 1."
    return float(number), ""


def _speed(
    mapped: Dict[str, str],
    offset: int,
    path: Path,
    distances: Optional[Dict[Tuple[str, str], float]],
) -> Tuple[Optional[float], str]:
    if "travel_minutes" not in mapped:
        return None, ""
    minutes, error = _required_minutes(mapped.get("travel_minutes", ""), offset, path, "travel_minutes")
    if error:
        return None, error
    if "distance_m" in mapped and mapped.get("distance_m", "") != "":
        distance, distance_error = _required_minutes(mapped["distance_m"], offset, path, "distance_m")
        if distance_error:
            return None, distance_error
    else:
        origin = mapped.get("from_location", "")
        dest = mapped.get("to_location", "")
        if distances is None:
            return None, f"{path} row {offset} column travel_minutes has no distance."
        distance = distances.get((origin, dest))
        if distance is None:
            distance = distances.get((dest, origin))
        if distance is None:
            return None, f"{path} row {offset} column from_location {origin} to {dest} has no distance."
    if distance <= 0:
        return None, f"{path} row {offset} column distance_m must be a finite number > 0."
    if minutes <= 0:
        return None, f"{path} row {offset} column travel_minutes must be a finite number > 0."
    return distance / minutes, ""


def _profile_rows(accepted: Sequence[dict], min_rows: int) -> List[dict]:
    modes = sorted({row["pick_mode"] for row in accepted})
    profile = []
    for mode in modes:
        rows = [row for row in accepted if row["pick_mode"] == mode]
        picks = [row["pick_per_line"] for row in rows]
        overheads = _job_values(rows, "overhead")
        enough = len(rows) >= min_rows
        profile.append(
            {
                "pick_mode": mode,
                "max_job_lines": "",
                "max_job_weight": "",
                "max_job_volume": "",
                "default_pick_time": _median(picks) if enough else "",
                "job_overhead": _median(overheads) if enough and overheads else "",
                "pallet_basis": "",
            }
        )
    return profile


def _job_values(rows: Sequence[dict], field: str) -> List[float]:
    found: Dict[str, float] = {}
    for row in rows:
        value = row[field]
        if value is None or row["job_id"] in found:
            continue
        found[row["job_id"]] = float(value)
    return list(found.values())


def _report(
    accepted: Sequence[dict],
    rejected: Sequence[dict],
    profile: Sequence[dict],
    min_rows: int,
    log_path: Path,
) -> str:
    dates = _dates(accepted)
    lines = [
        "# Calibration report",
        "",
        "This file measures the supplied log. It is not a default. "
        "Review it before any value is copied into the profile the chunker uses.",
        "",
        "## Sample",
        "",
        f"- Log: `{log_path}`",
        f"- Accepted rows: {len(accepted)}",
        f"- Rejected rows: {len(rejected)}",
        f"- Minimum rows before a cell is filled: {min_rows}",
        f"- Dates: {dates}",
        "",
        "Pick time is minutes per line (the row's pick minutes divided by `line_count`, or by 1). "
        "The 90th percentile is the nearest rank `ceil(0.90 * n)` of the sorted values. "
        "Overhead is one value per job. Walking speed is distance divided by recorded travel minutes. "
        "Caps, `pallet_basis`, and speed are not written into `mode_profile.csv`.",
        "",
    ]
    by_mode = {row["pick_mode"]: row for row in profile}
    for mode in sorted(by_mode):
        rows = [row for row in accepted if row["pick_mode"] == mode]
        picks = [row["pick_per_line"] for row in rows]
        overheads = _job_values(rows, "overhead")
        speeds = [row["speed"] for row in rows if row["speed"] is not None]
        jobs = sorted({row["job_id"] for row in rows})
        lines.append(f"## {mode}")
        lines.append("")
        lines.append(f"- Accepted rows: {len(rows)}")
        lines.append(f"- Jobs: {len(jobs)}")
        if len(rows) < min_rows:
            lines.append(
                f"- Too few rows. `default_pick_time` and `job_overhead` are blank "
                f"({len(rows)} accepted, minimum {min_rows})."
            )
        else:
            lines.append(f"- Pick time median: {_median(picks):.6g} minutes per line")
            lines.append(f"- Pick time 90th percentile: {_percentile(picks, 0.90):.6g} minutes per line")
            if overheads:
                lines.append(f"- Job overhead median: {_median(overheads):.6g} minutes")
            else:
                lines.append("- Job overhead: the log has no `overhead_minutes` for this mode. The cell is blank.")
        if speeds and len(rows) >= min_rows:
            lines.append(
                f"- Walking speed median: {_median(speeds):.6g} distance units per minute "
                "(report only; not a profile cell)."
            )
            lines.append(f"- Walking speed 90th percentile: {_percentile(speeds, 0.90):.6g}")
        elif speeds:
            lines.append("- Walking speed was computed and left out of the profile because the mode has too few rows.")
        else:
            lines.append("- Walking speed: the log has no travel time and distance for this mode.")
        lines.append("")
    lines.append("## Gaps")
    lines.append("")
    if not accepted:
        lines.append("- The log has no accepted rows.")
    gaps = []
    for row in profile:
        if row["default_pick_time"] == "":
            gaps.append(f"- `{row['pick_mode']}` pick time is blank.")
        if row["job_overhead"] == "":
            gaps.append(f"- `{row['pick_mode']}` job overhead is blank.")
    gaps.append("- `pallet_basis`, `max_job_lines`, `max_job_weight`, and `max_job_volume` are blank. This log does not measure them.")
    gaps.append("- Walking speed is not a column of `mode_profile.csv`.")
    lines.extend(gaps)
    lines.append("")
    return "\n".join(lines)


def _dates(accepted: Sequence[dict]) -> str:
    raw = [row["event_date"] for row in accepted if row["event_date"]]
    if not raw:
        return "the log has no event_date values."
    parsed = []
    for text in raw:
        try:
            parsed.append(date.fromisoformat(text))
        except ValueError:
            return "event_date values were not all YYYY-MM-DD."
    return f"{min(parsed).isoformat()} to {max(parsed).isoformat()}"


def _median(values: Sequence[float]) -> float:
    ordered = sorted(float(value) for value in values)
    count = len(ordered)
    mid = count // 2
    if count % 2 == 1:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _percentile(values: Sequence[float], fraction: float) -> float:
    ordered = sorted(float(value) for value in values)
    rank = math.ceil(fraction * len(ordered))
    return ordered[rank - 1]


def _read_table(path: Path, label: str) -> pd.DataFrame:
    if not path.is_file():
        raise ValueError(f"{label} not found: {path}")
    frame = pd.read_csv(path, dtype=str, keep_default_na=False)
    frame.columns = [str(name).strip() for name in frame.columns]
    if frame.empty:
        raise ValueError(f"{path} has no rows.")
    return frame


def _read_column_map(path: Path, export: pd.DataFrame) -> Dict[str, str]:
    if not path.is_file():
        raise ValueError(f"Column map not found: {path}")
    frame = _read_table(path, "Column map")
    missing = [name for name in ("source_column", "target_column") if name not in frame.columns]
    if missing:
        raise ValueError(f"{path} is missing column(s): {', '.join(missing)}.")
    mapping: Dict[str, str] = {}
    targets = set()
    allowed = set(_REQUIRED) | set(_OPTIONAL)
    for offset, row in enumerate(frame.itertuples(index=False), start=2):
        label = f"row {offset} of {path}"
        source_name = str(row.source_column).strip()
        target = str(row.target_column).strip()
        if source_name == "":
            raise ValueError(f"column map {label} column source_column is blank.")
        if target == "":
            raise ValueError(f"column map {label} column target_column is blank.")
        if target not in allowed:
            raise ValueError(f"column map {label} column target_column {target} is not a task-log field.")
        if source_name not in export.columns:
            raise ValueError(f"column map {label} column source_column {source_name} is not in the log.")
        if source_name in mapping:
            raise ValueError(f"column map {label} repeats source_column {source_name}.")
        if target in targets:
            raise ValueError(f"column map {label} repeats target_column {target}.")
        targets.add(target)
        mapping[source_name] = target
    absent = [name for name in _REQUIRED if name not in targets]
    if absent:
        raise ValueError(f"{path} does not map a column to {', '.join(absent)}.")
    has_pick = "pick_minutes" in targets
    has_span = "start_minute" in targets or "finish_minute" in targets
    if has_pick and has_span:
        raise ValueError(f"{path} maps both pick_minutes and a start/finish pair.")
    if not has_pick and not {"start_minute", "finish_minute"} <= targets:
        raise ValueError(f"{path} does not map pick_minutes or both start_minute and finish_minute.")
    return mapping


def _check_speed_inputs(mapping: Dict[str, str], distances: Optional[dict], map_path: Path) -> None:
    targets = set(mapping.values())
    if "travel_minutes" not in targets:
        if distances is not None:
            raise ValueError(f"{map_path} has no travel_minutes column, so a distance file is unused.")
        return
    has_distance = "distance_m" in targets or (
        distances is not None and "from_location" in targets and "to_location" in targets
    )
    if not has_distance:
        raise ValueError(f"{map_path} maps travel_minutes without distance_m or a distance file.")


def _read_distances(path: Path, mapping: Dict[str, str]) -> Dict[Tuple[str, str], float]:
    frame = _read_table(path, "Distance file")
    missing = [name for name in ("from_location", "to_location", "distance_m") if name not in frame.columns]
    if missing:
        raise ValueError(f"{path} is missing column(s): {', '.join(missing)}.")
    if "from_location" not in mapping.values() or "to_location" not in mapping.values():
        raise ValueError(f"{path} was given but the column map has no from_location and to_location.")
    found: Dict[Tuple[str, str], float] = {}
    for offset, raw in enumerate(frame.to_dict(orient="records"), start=2):
        origin = str(raw["from_location"]).strip()
        dest = str(raw["to_location"]).strip()
        if origin == "" or dest == "":
            raise ValueError(f"{path} row {offset} column from_location or to_location is blank.")
        number, error = _required_minutes(str(raw["distance_m"]).strip(), offset, path, "distance_m")
        if error:
            raise ValueError(error)
        found[(origin, dest)] = float(number)
    if not found:
        raise ValueError(f"{path} has no distance rows.")
    return found


def _required_minutes(value: str, offset: int, path: Path, column: str) -> Tuple[Optional[float], str]:
    label = f"{path} row {offset} column {column}"
    if value == "":
        return None, f"{label} is blank."
    try:
        number = float(value)
    except ValueError:
        return None, f"{label} must be a number."
    if not math.isfinite(number) or number < 0:
        return None, f"{label} must be a finite number >= 0."
    return number, ""


def _optional_minutes(mapped: Dict[str, str], column: str, offset: int, path: Path) -> Tuple[Optional[float], str]:
    if column not in mapped or mapped.get(column, "") == "":
        return None, ""
    return _required_minutes(mapped[column], offset, path, column)


def _rejection(source_row: int, pick_mode: str, job_id: str, reason: str, detail: str) -> dict:
    return {
        "source_row": source_row,
        "pick_mode": pick_mode,
        "job_id": job_id,
        "reason": reason,
        "detail": detail,
    }


def main(argv: Optional[Sequence[str]] = None) -> None:
    """Measure a task log and exit non-zero when rows were rejected."""
    parser = argparse.ArgumentParser(description="Measure a task log. The report is not a default.")
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--column-map", type=Path, required=True)
    parser.add_argument("--distance", type=Path, default=None, help="Optional from_location,to_location,distance_m.")
    parser.add_argument("--min-rows", type=int, default=_DEFAULT_MIN_ROWS)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        code = calibrate_log(args.log, args.column_map, args.out, args.distance, args.min_rows)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        sys.exit(1)
    if code:
        sys.exit(code)


if __name__ == "__main__":
    main()
