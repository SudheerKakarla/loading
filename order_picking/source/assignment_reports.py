"""Reports on a finished picker assignment.

The default assignment model is unchanged. These functions read its result:

* utilisation is busy minutes divided by the shift, using the same minutes
  the shift constraint already sums (the phase-1 walk plus pick time);
* a dropped job keeps the solver's reason and gains a finer detail;
* lateness sequences each picker's jobs by earliest deadline and counts
  how late each finish is. Jobs with no deadline go last and are not late.

``cutoff_time`` is minutes from the start of the shift, the clock the
chunker writes. A blank cutoff is no deadline.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import pandas as pd

_EPS = 1e-6
_GAP_FRACTION = 0.1
_FEASIBILITY_REL = 1e-6
_NOISE_MARGIN = 10.0
_DOCK_TOLERANCE = 1e-4


def _positive_weights(weights: Iterable[float]) -> List[float]:
    positive = []
    for weight in weights:
        value = float(weight)
        if math.isfinite(value) and value > 0.0:
            positive.append(value)
    return positive


def absolute_gap(weights: Iterable[float]) -> float:
    """A tenth of the smallest positive weight.

    That margin is enough to keep the job set: one such job cannot hide
    inside the gap. A caller-supplied relative gap is not used to loosen it.
    """
    positive = _positive_weights(weights)
    if not positive:
        return 1e-6
    return min(positive) * _GAP_FRACTION


def reachable_absolute_gap(weights: Iterable[float], objective_upper: float) -> float:
    """Absolute stop below one job, and above solver noise when that fits.

    A relative feasibility tolerance near 1e-6 on an objective of size ``S``
    leaves noise of about ``1e-6 * S``. A target finer than that noise is
    never the reason the search stops. When the noise is already larger
    than one job, the blended objective cannot certify a safe gap, and the
    tenth-of-a-job target is kept for the staged stages whose scale is the
    ordinary weights.
    """
    positive = _positive_weights(weights)
    if not positive:
        return 1e-6
    smallest = min(positive)
    target = smallest * _GAP_FRACTION
    noise = _FEASIBILITY_REL * max(float(objective_upper), 1.0)
    if noise >= smallest:
        return target
    return min(max(target, _NOISE_MARGIN * noise), 0.5 * smallest)


def stopping_rel_gap(absolute: float, scale: float) -> float:
    """Relative gap whose slack is ``absolute`` when ``scale`` bounds the objective.

    SciPy's MILP wrapper accepts ``mip_rel_gap`` and not an absolute gap.
    HiGHS stops when the absolute slack is about ``mip_rel_gap`` times the
    objective size. ``scale`` has to be an upper bound on that size. A
    smaller denominator stops with a larger absolute slack than requested.
    """
    return float(absolute) / max(abs(float(scale)), 1.0)


def stop_reason(
    time_limit_hit: bool,
    is_optimal: bool,
    achieved_abs_gap: Optional[float],
    abs_gap_used: Optional[float],
) -> str:
    """``proven``, ``gap_reached``, ``time_limit``, or ``no_solution``."""
    if time_limit_hit:
        return "time_limit"
    if not is_optimal:
        return "no_solution"
    if achieved_abs_gap is None or abs_gap_used is None:
        return "proven"
    if achieved_abs_gap <= 1e-6 or achieved_abs_gap <= 0.01 * float(abs_gap_used):
        return "proven"
    return "gap_reached"


def available_before_cutoff(cutoff: float, shift_start: float, shift: float) -> float:
    """Minutes a picker who starts at ``shift_start`` can work before ``cutoff``."""
    window = max(0.0, float(cutoff) - float(shift_start))
    return min(window, float(shift))


def shared_crew_overages(
    tables: Sequence[pd.DataFrame],
    tolerance: float = _DOCK_TOLERANCE,
) -> List[dict]:
    """Pickers whose busy minutes across dock solves exceed the shift."""
    totals: Dict[str, float] = {}
    shifts: Dict[str, float] = {}
    for table in tables:
        for row in table.itertuples(index=False):
            picker_id = str(row.picker_id)
            totals[picker_id] = totals.get(picker_id, 0.0) + float(row.busy_minutes)
            shifts[picker_id] = float(row.available_minutes)
    over = []
    for picker_id in sorted(totals):
        busy = totals[picker_id]
        available = shifts[picker_id]
        if busy > available + tolerance:
            over.append(
                {
                    "picker_id": picker_id,
                    "busy_minutes": busy,
                    "available_minutes": available,
                }
            )
    return over


def shared_crew_screen_message(count: int, unit: str = "docks") -> str:
    """Line printed when the minute sum fits.

    Clock overlap and the walk between docks stay outside this screen.
    """
    return (
        f"Summed busy minutes fit the shift across {count} {unit}. "
        "Clock overlap and the walk between docks are outside this screen."
    )


def require_shared_crew(
    tables: Sequence[pd.DataFrame],
    tolerance: float = _DOCK_TOLERANCE,
) -> None:
    """Raise when summed busy minutes across docks exceed the shift.

    A return means those totals fit. Clock overlap and the walk between
    docks stay outside this screen. The dock solves are unchanged.
    """
    over = shared_crew_overages(tables, tolerance)
    if not over:
        return
    listed = ", ".join(
        f"{row['picker_id']} {row['busy_minutes']:.3f} of {row['available_minutes']:.3f}"
        for row in over
    )
    raise ValueError(f"A picker is booked past the shift across docks: {listed}.")


def deadline_minutes(value: Any) -> Optional[float]:
    """Numeric cutoff as minutes from the shift start. Blank is no deadline."""
    if value is None or type(value).__name__ in {"NAType", "NaTType"}:
        return None
    if isinstance(value, str) and value.strip() == "":
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    if isinstance(value, str):
        text = value.strip()
        try:
            number = float(text)
        except ValueError as exc:
            raise ValueError(
                f"cutoff_time {value!r} is not a number of minutes. "
                "Lateness reads the chunker clock, minutes from the shift start."
            ) from exc
    else:
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"cutoff_time {value!r} is not a number of minutes."
            ) from exc
    if not math.isfinite(number):
        raise ValueError(f"cutoff_time {value!r} is not a finite number of minutes.")
    return number


def edd_sequence(order_ids: Sequence[str], deadlines: Mapping[str, float]) -> List[str]:
    """Earliest deadline first. Undated jobs go last. Ties break on order id."""
    dated = sorted(
        (order_id for order_id in order_ids if order_id in deadlines),
        key=lambda order_id: (deadlines[order_id], order_id),
    )
    undated = sorted(order_id for order_id in order_ids if order_id not in deadlines)
    return dated + undated


def schedule_lateness(
    picker_assignments: Mapping[str, Sequence[str]],
    durations: Mapping[str, float],
    deadlines: Mapping[str, float],
) -> List[dict]:
    """Finish time of each assigned job when the picker works in deadline order.

    The clock starts at 0. A job's finish minute is the sum of its own
    duration and the durations of that picker's earlier jobs. Lateness is
    how far the finish is past the deadline, and never negative. An undated
    job is not late.
    """
    rows: List[dict] = []
    for picker_id, order_ids in picker_assignments.items():
        sequence = edd_sequence(list(order_ids), deadlines)
        finish = 0.0
        for position, order_id in enumerate(sequence, start=1):
            finish += float(durations[order_id])
            deadline = deadlines.get(order_id)
            late = 0.0 if deadline is None else max(0.0, finish - deadline)
            rows.append(
                {
                    "order_id": order_id,
                    "picker_id": picker_id,
                    "position": position,
                    "finish_minute": finish,
                    "deadline_minute": "" if deadline is None else deadline,
                    "minutes_late": late,
                }
            )
    return rows


def late_jobs(schedule: Sequence[Mapping[str, Any]]) -> List[dict]:
    """Jobs whose finish is after the deadline."""
    return [dict(row) for row in schedule if float(row["minutes_late"]) > _EPS]


def drop_detail(
    blockers: Sequence[str],
    eligible_picker_ids: Sequence[str],
    duration: float,
    workload: Mapping[str, float],
    shifts: Mapping[str, float],
) -> str:
    """Why an unassigned job was left out, given the solved workloads.

    ``blockers`` holds one word per picker when nobody is eligible:
    ``equipment`` or ``capacity``. A job that fits someone's remaining
    minutes was left for the objective. A job that fits nobody's remainder
    is shift capacity.
    """
    if not eligible_picker_ids:
        kinds = set(blockers)
        if kinds == {"equipment"}:
            return "equipment"
        if kinds == {"capacity"}:
            return "capacity"
        return "equipment_and_capacity"
    for picker_id in eligible_picker_ids:
        remaining = float(shifts[picker_id]) - float(workload.get(picker_id, 0.0))
        if remaining + _EPS >= float(duration):
            return "priority_tradeoff"
    return "shift_capacity"


def utilisation_rows(
    pickers: pd.DataFrame,
    workload: Mapping[str, float],
) -> List[dict]:
    """One row per picker. Busy minutes are the solved workload."""
    rows: List[dict] = []
    for picker in pickers.itertuples(index=False):
        available = float(picker.shift_duration)
        busy = float(workload.get(picker.picker_id, 0.0))
        if available <= 0:
            raise ValueError(f"{picker.picker_id} shift_duration must be > 0.")
        rows.append(
            {
                "picker_id": picker.picker_id,
                "equipment_type": picker.equipment_type,
                "busy_minutes": busy,
                "available_minutes": available,
                "utilisation_pct": 100.0 * busy / available,
            }
        )
    return rows


def _rollup(rows: Sequence[Mapping[str, Any]]) -> Tuple[float, float, float]:
    busy = sum(float(row["busy_minutes"]) for row in rows)
    available = sum(float(row["available_minutes"]) for row in rows)
    pct = 0.0 if available <= 0 else 100.0 * busy / available
    return busy, available, pct


def format_assignment_report(
    utilisation: Sequence[Mapping[str, Any]],
    drops: Sequence[Mapping[str, Any]],
    schedule: Sequence[Mapping[str, Any]],
) -> str:
    """Printed utilisation, lateness, and drops. The crew line states the dock rule."""
    busy, available, pct = _rollup(utilisation)
    lines = [
        "Crew: this solve uses every picker in the file.",
        "A second dock is a separate solve and sees this same crew again.",
        f"Busy {busy:.3f} min / available {available:.3f} min ({pct:.1f}%).",
    ]
    by_type: Dict[str, List[Mapping[str, Any]]] = {}
    for row in utilisation:
        by_type.setdefault(str(row["equipment_type"]), []).append(row)
    for equipment in sorted(by_type):
        type_busy, type_available, type_pct = _rollup(by_type[equipment])
        lines.append(
            f"  {equipment}: {type_busy:.3f} / {type_available:.3f} min ({type_pct:.1f}%)."
        )
    late = late_jobs(schedule)
    late_minutes = sum(float(row["minutes_late"]) for row in late)
    lines.append(f"Late jobs: {len(late)}. Late minutes: {late_minutes:.3f}.")
    details: Dict[str, int] = {}
    for drop in drops:
        detail = str(drop.get("detail", drop.get("reason", "")))
        details[detail] = details.get(detail, 0) + 1
    if details:
        listed = ", ".join(f"{count} {name}" for name, count in sorted(details.items()))
        lines.append(f"Dropped: {sum(details.values())} ({listed}).")
    else:
        lines.append("Dropped: 0.")
    return "\n".join(lines)


def write_assignment_reports(
    directory: Path,
    utilisation: Sequence[Mapping[str, Any]],
    drops: Sequence[Mapping[str, Any]],
    schedule: Sequence[Mapping[str, Any]],
    stages: Optional[Sequence[Mapping[str, Any]]] = None,
) -> None:
    """Write utilisation.csv, dropped.csv, and late_jobs.csv.

    A staged solve also writes stages.csv when ``stages`` is provided.
    """
    directory.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(
        list(utilisation),
        columns=[
            "picker_id",
            "equipment_type",
            "busy_minutes",
            "available_minutes",
            "utilisation_pct",
        ],
    ).to_csv(directory / "utilisation.csv", index=False)
    pd.DataFrame(
        list(drops),
        columns=["order_id", "reason", "detail"],
    ).to_csv(directory / "dropped.csv", index=False)
    pd.DataFrame(
        late_jobs(schedule),
        columns=[
            "order_id",
            "picker_id",
            "position",
            "finish_minute",
            "deadline_minute",
            "minutes_late",
        ],
    ).to_csv(directory / "late_jobs.csv", index=False)
    if stages:
        pd.DataFrame(list(stages)).to_csv(directory / "stages.csv", index=False)


def parse_scenarios(text: str) -> List[dict]:
    """Read the small scenario file used by the assignment runner.

    Each scenario is a name, an optional ``remove`` map of equipment to a
    count, and an optional ``deadline_minutes`` shift. Blank lines and
    ``#`` comments are ignored.
    """
    scenarios: List[dict] = []
    current: Optional[dict] = None
    in_remove = False
    saw_header = False
    for line_number, raw in enumerate(text.splitlines(), start=1):
        stripped = raw.split("#", 1)[0].rstrip()
        if not stripped.strip():
            continue
        if stripped.strip() == "scenarios:":
            saw_header = True
            continue
        if not saw_header:
            raise ValueError(f"Line {line_number}: expected 'scenarios:'.")
        indent = len(stripped) - len(stripped.lstrip(" "))
        body = stripped.strip()
        if body.startswith("- name:"):
            name = body.split(":", 1)[1].strip()
            if not name:
                raise ValueError(f"Line {line_number}: scenario name is blank.")
            current = {"name": name, "remove": {}, "deadline_minutes": 0.0}
            scenarios.append(current)
            in_remove = False
            continue
        if current is None:
            raise ValueError(f"Line {line_number}: scenario field before a name.")
        if body == "remove:":
            in_remove = True
            continue
        if body.startswith("deadline_minutes:"):
            current["deadline_minutes"] = float(body.split(":", 1)[1].strip())
            in_remove = False
            continue
        if in_remove and indent >= 4 and ":" in body:
            equipment, count_text = body.split(":", 1)
            count = int(count_text.strip())
            if count < 0:
                raise ValueError(f"Line {line_number}: remove count must be >= 0.")
            current["remove"][equipment.strip()] = count
            continue
        raise ValueError(f"Line {line_number}: unrecognised scenario line {body!r}.")
    if not scenarios:
        raise ValueError("The scenario file has no scenarios.")
    names = [item["name"] for item in scenarios]
    if len(names) != len(set(names)):
        raise ValueError("Scenario names must be unique.")
    return scenarios


def remove_pickers(pickers: pd.DataFrame, remove: Mapping[str, int]) -> pd.DataFrame:
    """Drop the first ``count`` picker ids of each equipment type."""
    kept = pickers.copy()
    for equipment, count in remove.items():
        matched = kept.index[kept["equipment_type"] == equipment].tolist()
        ordered = sorted(matched, key=lambda index: str(kept.at[index, "picker_id"]))
        if len(ordered) < count:
            raise ValueError(
                f"Cannot remove {count} {equipment} pickers; the file has {len(ordered)}."
            )
        drop = ordered[:count]
        kept = kept.drop(index=drop)
    return kept.reset_index(drop=True)


def shift_deadlines(orders: pd.DataFrame, minutes: float) -> pd.DataFrame:
    """Add ``minutes`` to every numeric cutoff. Blank cutoffs stay blank."""
    if not minutes:
        return orders
    shifted = orders.copy()
    if "cutoff_time" not in shifted.columns:
        return shifted
    values = []
    for value in shifted["cutoff_time"]:
        deadline = deadline_minutes(value)
        values.append("" if deadline is None else deadline + minutes)
    shifted["cutoff_time"] = values
    return shifted


def main(argv: Optional[Sequence[str]] = None) -> None:
    """Sum busy minutes across dock utilisation files and raise past the shift.

    A pass prints that the totals fit. Clock overlap and the walk between
    docks stay outside this screen.

    Run from ``order_picking/`` as
    ``python source/assignment_reports.py outputs/chunk_assign/PACK_A/base/utilisation.csv outputs/chunk_assign/PACK_B/base/utilisation.csv``.
    """
    import argparse

    parser = argparse.ArgumentParser(
        description="Report a picker booked past the shift across dock utilisation files."
    )
    parser.add_argument(
        "utilisation",
        nargs="+",
        type=Path,
        help="utilisation.csv files, one per dock solve.",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    tables = [pd.read_csv(path) for path in args.utilisation]
    require_shared_crew(tables)
    print(shared_crew_screen_message(len(tables), "dock files"))


if __name__ == "__main__":
    main()
