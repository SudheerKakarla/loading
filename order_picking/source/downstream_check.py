"""Report-only queue for the steps after picking.

Each step has ``resource_count`` servers. A job enters the first step at its
pick finish, waits for a free server, and then enters the next step. Process
time is ``fixed_minutes`` plus ``minutes_per_pallet`` times the job's pallet
count (default 1). Departure is the pick deadline plus the staging buffer.
The report does not change a solve.

``--lead-times`` on the chunker uses :func:`staging_buffer_from_lead_times`,
the sum of those times for one pallet. That sum replaces ``--staging-buffer``
only when the file is passed.
"""

from __future__ import annotations

import argparse
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence

import pandas as pd

_EPS = 1e-9


@dataclass(frozen=True)
class LeadStep:
    """One downstream step. Times are minutes. ``order`` is the sequence."""

    step: str
    minutes_per_pallet: float
    fixed_minutes: float
    resource_count: int
    order: int = 0


def read_lead_times(path: Path) -> List[LeadStep]:
    """Read ``step, order, fixed_minutes, minutes_per_pallet, resource_count``.

    A blank ``resource_count`` means 1. Steps are returned in ``order``.
    """
    lead_path = Path(path)
    if not lead_path.is_file():
        raise ValueError(f"Lead-time file not found: {lead_path}")
    frame = pd.read_csv(lead_path)
    frame.columns = [str(name).strip() for name in frame.columns]
    required = ("step", "order", "fixed_minutes", "minutes_per_pallet")
    missing = [name for name in required if name not in frame.columns]
    if missing:
        raise ValueError(f"{lead_path} is missing column(s): {', '.join(missing)}.")
    steps: List[LeadStep] = []
    seen = set()
    orders = set()
    for index, row in frame.iterrows():
        label = f"row {int(index) + 2} of {lead_path}"
        step = "" if pd.isna(row.step) else str(row.step).strip()
        if step == "" or step.lower() == "nan":
            raise ValueError(f"lead times {label} column step is blank.")
        if step in seen:
            raise ValueError(f"lead times {label} repeats step {step}.")
        seen.add(step)
        sequence = _whole(row.order, label, "order", minimum=1)
        if sequence in orders:
            raise ValueError(f"lead times {label} repeats order {sequence}.")
        orders.add(sequence)
        fixed = _nonnegative(row.fixed_minutes, label, "fixed_minutes")
        per_pallet = _nonnegative(row.minutes_per_pallet, label, "minutes_per_pallet")
        if "resource_count" not in frame.columns or _blank(row.resource_count):
            servers = 1
        else:
            servers = _whole(row.resource_count, label, "resource_count", minimum=1)
        steps.append(
            LeadStep(
                step=step,
                minutes_per_pallet=per_pallet,
                fixed_minutes=fixed,
                resource_count=servers,
                order=sequence,
            )
        )
    if not steps:
        raise ValueError(f"{lead_path} has no step rows.")
    steps.sort(key=lambda item: item.order)
    return steps


def staging_buffer_from_lead_times(path: Path) -> float:
    """Sum of fixed time plus one pallet at each step. This replaces the flat buffer."""
    return sum(step.fixed_minutes + step.minutes_per_pallet for step in read_lead_times(path))


def downstream_report(
    schedule: Sequence[Mapping[str, object]],
    steps: Sequence[LeadStep],
    pallets_by_job: Optional[Mapping[str, float]] = None,
    staging_buffer: float = 30.0,
    pallet_counts: Optional[Mapping[str, float]] = None,
) -> dict:
    """Queue each step and return one row per job.

    A job enters step 1 at its finish minute. Departure is
    ``deadline_minute + staging_buffer``. ``first_miss`` is the first job
    whose exit at that step is after departure. ``max_wait`` is the longest
    queue at that step.
    """
    if not steps:
        raise ValueError("downstream_report needs at least one step.")
    if staging_buffer < 0 or not math.isfinite(staging_buffer):
        raise ValueError("staging_buffer must be a finite number >= 0.")
    counts = pallets_by_job if pallets_by_job is not None else pallet_counts
    ordered = sorted(steps, key=lambda item: item.order)
    jobs = []
    for row in schedule:
        job_id = _job_id(row)
        deadline = _deadline(row)
        departure = None if deadline is None else deadline + float(staging_buffer)
        jobs.append(
            {
                "job_id": job_id,
                "depot_id": _text(row.get("depot_id")),
                "finish_minute": float(row["finish_minute"]),
                "departure_minute": departure,
                "pallets": _pallets(row, counts, job_id),
                "arrival_minute": float(row["finish_minute"]),
            }
        )
    jobs.sort(key=lambda item: (item["arrival_minute"], item["job_id"]))
    exits: Dict[str, Dict[str, float]] = {job["job_id"]: {} for job in jobs}
    first_miss: Dict[str, Optional[dict]] = {}
    max_wait: Dict[str, float] = {}
    for step in ordered:
        servers = [0.0] * step.resource_count
        missed: Optional[dict] = None
        longest = 0.0
        nxt = []
        for job in jobs:
            process = step.fixed_minutes + step.minutes_per_pallet * job["pallets"]
            server = min(range(step.resource_count), key=lambda index: (servers[index], index))
            start = max(job["arrival_minute"], servers[server])
            finish = start + process
            wait = start - job["arrival_minute"]
            servers[server] = finish
            longest = max(longest, wait)
            exits[job["job_id"]][step.step] = finish
            departure = job["departure_minute"]
            misses = departure is not None and finish > departure + _EPS
            if misses and (
                missed is None
                or finish < missed["exit_minute"] - _EPS
                or (abs(finish - missed["exit_minute"]) <= _EPS and job["job_id"] < missed["job_id"])
            ):
                missed = {"job_id": job["job_id"], "exit_minute": finish}
            nxt.append({**job, "arrival_minute": finish})
        jobs = sorted(nxt, key=lambda item: (item["arrival_minute"], item["job_id"]))
        first_miss[step.step] = missed
        max_wait[step.step] = longest
    original = []
    for row in schedule:
        job_id = _job_id(row)
        deadline = _deadline(row)
        departure = None if deadline is None else deadline + float(staging_buffer)
        ready = exits[job_id][ordered[-1].step]
        late = 0.0 if departure is None else max(0.0, ready - departure)
        record = {
            "job_id": job_id,
            "depot_id": _text(row.get("depot_id")),
            "finish_minute": float(row["finish_minute"]),
            "ready_minute": ready,
            "departure_minute": "" if departure is None else departure,
            "minutes_late_for_departure": late,
        }
        for step in ordered:
            record[f"{step.step}_exit_minute"] = exits[job_id][step.step]
        original.append(record)
    return {"rows": original, "first_miss": first_miss, "max_wait": max_wait}


def _job_id(row: Mapping[str, object]) -> str:
    for name in ("job_id", "order_id"):
        if name in row and not _blank(row.get(name)):
            return str(row.get(name)).strip()
    raise ValueError("A schedule row needs job_id or order_id.")


def _deadline(row: Mapping[str, object]) -> Optional[float]:
    raw = row.get("deadline_minute")
    if _blank(raw):
        return None
    try:
        number = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _pallets(row: Mapping[str, object], counts: Optional[Mapping[str, float]], job_id: str) -> float:
    raw: object = None
    if counts is not None and job_id in counts:
        raw = counts[job_id]
    elif "pallets" in row:
        raw = row.get("pallets")
    if _blank(raw):
        return 1.0
    count = float(raw)  # type: ignore[arg-type]
    if not math.isfinite(count) or count <= 0:
        raise ValueError(f"order {job_id} column pallets must be a finite number > 0.")
    return count


def _blank(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    text = str(value).strip()
    return text == "" or text.lower() == "nan"


def _text(value: object) -> str:
    if _blank(value):
        return ""
    return str(value).strip()


def _nonnegative(value: object, label: str, column: str) -> float:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"lead times {label} column {column} must be a number.") from exc
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"lead times {label} column {column} must be a finite number >= 0.")
    return number


def _whole(value: object, label: str, column: str, minimum: int) -> int:
    number = _nonnegative(value, label, column)
    if number < minimum or abs(number - round(number)) > 1e-9:
        raise ValueError(f"lead times {label} column {column} must be a whole number >= {minimum}.")
    return int(round(number))


def main(argv: Optional[Sequence[str]] = None) -> None:
    """Write the queue report. This command does not solve."""
    parser = argparse.ArgumentParser(description="Report the downstream queue. It does not change a plan.")
    parser.add_argument("--schedule", type=Path, required=True, help="CSV with job_id or order_id and finish_minute.")
    parser.add_argument("--lead-times", type=Path, required=True)
    parser.add_argument("--jobs", type=Path, default=None, help="Optional jobs.csv with a pallets column.")
    parser.add_argument("--staging-buffer", type=float, default=30.0)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(list(argv) if argv is not None else None)
    steps = read_lead_times(args.lead_times)
    schedule = pd.read_csv(args.schedule).to_dict(orient="records")
    counts = None
    if args.jobs is not None:
        jobs = pd.read_csv(args.jobs)
        id_column = "job_id" if "job_id" in jobs.columns else "order_id"
        if "pallets" in jobs.columns and id_column in jobs.columns:
            counts = {
                str(getattr(row, id_column)): float(row.pallets)
                for row in jobs.itertuples(index=False)
                if not pd.isna(row.pallets)
            }
    report = downstream_report(schedule, steps, counts, staging_buffer=args.staging_buffer)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(report["rows"]).to_csv(out, index=False)
    for step in steps:
        missed = report["first_miss"][step.step]
        if missed is None:
            print(f"{step.step}: no job misses departure.")
        else:
            print(
                f"{step.step}: first miss {missed['job_id']} "
                f"at minute {missed['exit_minute']:.3f}."
            )
        print(f"{step.step}: maximum wait {report['max_wait'][step.step]:.3f} minutes.")


if __name__ == "__main__":
    main()
