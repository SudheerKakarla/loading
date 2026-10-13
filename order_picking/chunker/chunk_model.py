"""Set-partitioning chunker. HiGHS chooses the jobs; the savings heuristic only proposes some of them.

One truck at one dock is an exact cover of its eligible lines. A source order
that fits one picker stays in one piece. Several such orders may share a job
on that dock, including orders for different stops. ``same_stop=True`` (the
``--destination-grouping`` flag) keeps each job to one stop. The
objective is the sum of Held-Karp closed tours plus one overhead per job.
Pick time is in the cost and does not change the cover, because each line is
covered once.

A must-ship line is covered by the same equality as every other line. A second
equality restricts that line to priority-eligible jobs. A job that contains a
must-ship line is generated only when it is priority-eligible, so a cheaper
non-priority copy of the line cannot be selected beside the priority job.
"""

from __future__ import annotations

import argparse
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd
from scipy.optimize import Bounds, LinearConstraint, milp
from scipy.sparse import csr_matrix

_ROOT = Path(__file__).resolve().parents[1]
_SOURCE = str(_ROOT / "source")
if _SOURCE not in sys.path:
    sys.path.insert(0, _SOURCE)

from chunk_loads import (  # noqa: E402
    ChunkResult,
    ChunkSettings,
    _Unit,
    _check_group,
    _frames,
    _job_record,
    _leg,
    _nearest_neighbor,
    _number_jobs,
    _read_lines,
    _savings_routes,
    write_chunk_outputs,
)
from order_picking_optimizer import (  # noqa: E402
    DEFAULT_TIER_MULTIPLIERS,
    DEFAULT_WAVE_HOURS,
    MUST_SHIP_WEIGHT,
    _build_lookup,
    _is_missing,
    _normalize_tier_table,
)

_EPS = 1e-9
_MAX_TOUR_STOPS = 16
CellKey = Tuple[str, str, str]
OrderKey = Tuple[str, str]


@dataclass(frozen=True)
class PickerSpec:
    """One picker's equipment and the load that picker can take on one trip."""

    picker_id: str
    equipment_type: str
    max_weight: float
    max_volume: Optional[float] = None


@dataclass(frozen=True)
class CoverCandidate:
    """One feasible job the set-partitioning model may turn on."""

    candidate_id: str
    line_ids: Tuple[str, ...]
    cost: float
    priority: bool


@dataclass(frozen=True)
class ChunkModelSettings:
    """Knobs for the set-partitioning chunker. Times are minutes."""

    max_job_lines: int = 15
    job_overhead: float = 5.0
    default_pick_time: float = 0.5
    staging_buffer: float = 30.0
    reference_order_lines: float = 10.0
    must_ship_weight: float = MUST_SHIP_WEIGHT
    deadline_mode: str = "off"
    wave_hours: float = DEFAULT_WAVE_HOURS
    wave_start: Optional[str] = None
    priority_max_lines: Optional[int] = None
    max_enumerate_lines: int = 10
    subset_size_cap: int = 3
    max_jobs: Optional[int] = None
    time_limit: float = 120.0
    mip_gap: float = 0.0
    ignore_volume: bool = False
    same_stop: bool = False

    def __post_init__(self) -> None:
        if self.max_job_lines < 1:
            raise ValueError("max_job_lines must be at least 1.")
        if self.job_overhead < 0 or not math.isfinite(self.job_overhead):
            raise ValueError("job_overhead must be a finite number >= 0.")
        if self.default_pick_time < 0 or not math.isfinite(self.default_pick_time):
            raise ValueError("default_pick_time must be a finite number >= 0.")
        if self.staging_buffer < 0 or not math.isfinite(self.staging_buffer):
            raise ValueError("staging_buffer must be a finite number >= 0.")
        if self.reference_order_lines <= 0 or not math.isfinite(self.reference_order_lines):
            raise ValueError("reference_order_lines must be a finite number > 0.")
        if self.must_ship_weight <= 0 or not math.isfinite(self.must_ship_weight):
            raise ValueError("must_ship_weight must be a finite number > 0.")
        if self.deadline_mode not in {"off", "soft", "hard"}:
            raise ValueError("deadline_mode must be off, soft, or hard.")
        if self.wave_hours <= 0 or not math.isfinite(self.wave_hours):
            raise ValueError("wave_hours must be a finite number > 0.")
        if self.priority_max_lines is not None and self.priority_max_lines < 1:
            raise ValueError("priority_max_lines must be at least 1 when set.")
        if self.max_enumerate_lines < 1:
            raise ValueError("max_enumerate_lines must be at least 1.")
        if self.subset_size_cap < 1:
            raise ValueError("subset_size_cap must be at least 1.")
        if self.max_jobs is not None and self.max_jobs < 1:
            raise ValueError("max_jobs must be at least 1 when set.")
        if self.time_limit <= 0 or not math.isfinite(self.time_limit):
            raise ValueError("time_limit must be a finite number > 0.")
        if self.mip_gap < 0 or not math.isfinite(self.mip_gap):
            raise ValueError("mip_gap must be a finite number >= 0.")


@dataclass(frozen=True)
class CoverSolution:
    """Jobs HiGHS returned, their minutes, and the relative gap it reported.

    A gap of 0 means the cover is proved optimal. A positive gap means the
    time limit stopped the proof and this is the best cover found. The gap is
    ``(incumbent - bound) / incumbent`` as HiGHS reports it.
    """

    jobs: Tuple[CoverCandidate, ...]
    objective: float
    mip_gap: Optional[float]


@dataclass(frozen=True)
class TruckSolve:
    """One truck at one dock: the cover minutes and the solver gap."""

    truck_id: str
    depot_id: str
    minutes: float
    mip_gap: Optional[float]
    fallback: bool


@dataclass(frozen=True)
class ChunkModelResult:
    """Picker files, rows the model refused, and the cover's total minutes."""

    orders: pd.DataFrame
    items: pd.DataFrame
    jobs: pd.DataFrame
    rejected: pd.DataFrame
    objective: float
    fallback_depots: Tuple[Tuple[str, str], ...] = ()
    solves: Tuple[TruckSolve, ...] = ()

    def as_chunk(self) -> ChunkResult:
        return ChunkResult(self.orders, self.items, self.jobs)


def read_pickers(frame: pd.DataFrame) -> List[PickerSpec]:
    """Read picker id, equipment, and per-trip weight and volume caps."""
    if frame.empty or "max_weight" not in frame.columns or "equipment_type" not in frame.columns:
        raise ValueError("The picker file needs equipment_type, max_weight, and at least one row.")
    specs: List[PickerSpec] = []
    for index, row in enumerate(frame.itertuples(index=False)):
        label = f"picker row {index + 1}"
        picker_id = str(getattr(row, "picker_id", "")).strip()
        if picker_id == "" or picker_id.lower() == "nan":
            picker_id = f"P{index + 1}"
        equipment = getattr(row, "equipment_type", None)
        if _is_missing(equipment) or str(equipment).strip() == "":
            raise ValueError(f"{label} needs an equipment_type.")
        weight = float(row.max_weight)
        if not math.isfinite(weight) or weight <= 0:
            raise ValueError(f"{label} max_weight must be a finite number > 0.")
        volume = None
        if hasattr(row, "max_volume") and not _is_missing(row.max_volume):
            volume = float(row.max_volume)
            if not math.isfinite(volume) or volume <= 0:
                raise ValueError(f"{label} max_volume must be a finite number > 0 when set.")
        specs.append(
            PickerSpec(
                picker_id=picker_id,
                equipment_type=str(equipment).strip(),
                max_weight=weight,
                max_volume=volume,
            )
        )
    return specs


def failure_reason(
    weight: float,
    volume: Optional[float],
    pickers: Sequence[PickerSpec],
) -> Optional[str]:
    """Why no single picker can carry this weight and volume, or None when one can.

    Weight and volume are judged together on each picker. A line that is under
    the heaviest picker's weight and under another picker's volume is still
    rejected when no one picker satisfies both.
    """
    if any(_picker_fits(weight, volume, picker) for picker in pickers):
        return None
    weight_ok = any(weight <= picker.max_weight + _EPS for picker in pickers)
    volume_ok = any(_volume_fits(volume, picker) for picker in pickers)
    if not weight_ok and not volume_ok:
        return "exceeds_max_weight_and_volume"
    if not weight_ok:
        return "exceeds_max_weight"
    if not volume_ok:
        return "exceeds_max_volume"
    return "no_single_picker"


def required_equipment(
    weight: float,
    volume: Optional[float],
    pickers: Sequence[PickerSpec],
) -> str:
    """Blank when a pallet jack can carry the job. Otherwise that job's equipment.

    The live picker matches ``required_equipment`` as an exact string. A blank
    cell lets any picker take the job, and the weight and volume checks then
    drop a machine that is too small. A forklift-only job names ``forklift``.
    """
    able = [picker for picker in pickers if _picker_fits(weight, volume, picker)]
    if not able:
        raise ValueError("required_equipment was asked for a load no picker can carry.")
    if any(picker.equipment_type == "pallet_jack" for picker in able):
        return ""
    types = {picker.equipment_type for picker in able}
    if len(types) == 1:
        return next(iter(types))
    heaviest = max(able, key=lambda picker: (picker.max_weight, picker.equipment_type))
    return heaviest.equipment_type


def _selected_from_values(
    candidates: Sequence[CoverCandidate],
    values,
    line_ids: Sequence[str],
    must_ship_ids: Iterable[str],
) -> Optional[List[CoverCandidate]]:
    """Return the jobs whose values are on, when those jobs are an exact cover.

    A time limit often leaves ``values`` empty. That is not a cover. A time
    limit that did return a 0/1 cover is kept even though it is not proved optimal.
    """
    if values is None:
        return None
    selected = [candidate for candidate, value in zip(candidates, values) if value > 0.5]
    try:
        _audit_cover(selected, line_ids, set(must_ship_ids))
    except RuntimeError:
        return None
    return selected


def _reported_gap(result: object, objective: float) -> Optional[float]:
    """Relative MIP gap from HiGHS. Zero when the cover is proved optimal."""
    raw = getattr(result, "mip_gap", None)
    if raw is not None:
        gap = float(raw)
        if math.isfinite(gap):
            return max(0.0, gap)
    if int(getattr(result, "status", -1)) == 0:
        return 0.0
    bound = getattr(result, "mip_dual_bound", None)
    if bound is not None and math.isfinite(float(bound)) and abs(objective) > _EPS:
        return max(0.0, (objective - float(bound)) / abs(objective))
    return None


def solve_cover(
    candidates: Sequence[CoverCandidate],
    line_ids: Sequence[str],
    must_ship_ids: Iterable[str],
    max_jobs: Optional[int] = None,
    time_limit: float = 30.0,
    mip_gap: float = 0.0,
) -> Optional[CoverSolution]:
    """Minimize tour cost over an exact cover. HiGHS solves the binary model.

    Returns None when HiGHS returns no usable cover, including a time limit
    hit before any feasible cover exists. A cover that is in hand is returned
    even when the time limit stopped the proof. ``mip_gap`` is 0 when the
    cover is proved optimal, and the solver's relative gap otherwise.

    Every line, including a must-ship line, has ``sum_k a_lk z_k = 1`` over
    every candidate that contains it. A must-ship line whose priority pool is
    a strict subset has a second equality over that pool only. Together those
    rows forbid selecting a non-priority job that also contains the line.
    """
    lines = list(dict.fromkeys(line_ids))
    must_ship = set(must_ship_ids)
    if not lines:
        return CoverSolution((), 0.0, 0.0)
    by_id = {candidate.candidate_id: candidate for candidate in candidates}
    if len(by_id) != len(candidates):
        raise ValueError("Candidate ids must be unique.")
    index = {candidate.candidate_id: position for position, candidate in enumerate(candidates)}
    holders: Dict[str, List[CoverCandidate]] = {line_id: [] for line_id in lines}
    for candidate in candidates:
        for line_id in candidate.line_ids:
            if line_id not in holders:
                raise ValueError(f"Candidate {candidate.candidate_id} names unknown line {line_id}.")
            holders[line_id].append(candidate)
    for line_id, options in holders.items():
        if not options:
            raise ValueError(f"Line {line_id} has no candidate job.")
        if line_id in must_ship and not any(option.priority for option in options):
            raise ValueError(f"Must-ship line {line_id} has no priority-eligible job.")

    width = len(candidates)
    cost = np.array([candidate.cost for candidate in candidates], dtype=float)
    rows: List[Dict[int, float]] = []
    for line_id in lines:
        options = holders[line_id]
        rows.append({index[option.candidate_id]: 1.0 for option in options})
        if line_id not in must_ship:
            continue
        priority_options = [option for option in options if option.priority]
        if len(priority_options) == len(options):
            continue
        rows.append({index[option.candidate_id]: 1.0 for option in priority_options})
    if max_jobs is not None:
        rows.append({position: 1.0 for position in range(width)})

    data: List[float] = []
    row_index: List[int] = []
    col_index: List[int] = []
    lower: List[float] = []
    upper: List[float] = []
    for row_number, coeffs in enumerate(rows):
        is_cap = max_jobs is not None and row_number == len(rows) - 1
        lower.append(0.0 if is_cap else 1.0)
        upper.append(float(max_jobs) if is_cap else 1.0)
        for column, value in coeffs.items():
            data.append(value)
            row_index.append(row_number)
            col_index.append(column)
    matrix = csr_matrix((data, (row_index, col_index)), shape=(len(rows), width))
    integrality = np.ones(width, dtype=int)
    result = milp(
        c=cost,
        integrality=integrality,
        bounds=Bounds(np.zeros(width), np.ones(width)),
        constraints=LinearConstraint(matrix, lower, upper),
        options={"disp": False, "time_limit": float(time_limit), "mip_rel_gap": float(mip_gap), "presolve": True},
    )
    chosen = _selected_from_values(candidates, None if result.x is None else result.x, lines, must_ship)
    if chosen is None:
        return None
    objective = float(sum(candidate.cost for candidate in chosen))
    return CoverSolution(tuple(chosen), objective, _reported_gap(result, objective))


def chunk_loads_model(
    loads: pd.DataFrame,
    travel,
    pickers: Sequence[PickerSpec],
    settings: ChunkModelSettings,
) -> ChunkModelResult:
    """Split each truck into picker jobs by solving the set-partitioning model.

    ``travel`` is a dict-of-dicts or a table with ``from_location``,
    ``to_location``, and ``travel_time``. Lines and source orders that no
    single picker can carry are returned in ``rejected`` and are not given jobs.
    """
    if not pickers:
        raise ValueError("The set-partitioning chunker needs at least one picker.")
    lookup = _build_lookup(travel)
    parse_settings = _parse_settings(settings, pickers)
    lines = _read_lines(loads, parse_settings)
    if not lines:
        raise ValueError("The truck-load table has no lines.")
    order_of = _order_keys(loads, lines)
    _require_volumes(lines, pickers, settings.ignore_volume)
    kept, rejected_rows = _reject_infeasible(lines, order_of, pickers, settings.ignore_volume)
    tiers = _normalize_tier_table(DEFAULT_TIER_MULTIPLIERS)
    tour_cache: Dict[Tuple[str, Tuple[str, ...]], float] = {}
    grouped: Dict[Tuple[str, str], List] = {}
    for line in kept:
        grouped.setdefault((line.truck_id, line.depot_id), []).append(line)

    built: List[dict] = []
    total_cost = 0.0
    fallback: List[Tuple[str, str]] = []
    solves: List[TruckSolve] = []
    for truck_id, depot_id in sorted(grouped):
        group = grouped[(truck_id, depot_id)]
        _check_group(group)
        selected, cost, used_fallback, gap = _cover_depot(
            group, order_of, depot_id, lookup, pickers, settings, tour_cache
        )
        if used_fallback:
            fallback.append((truck_id, depot_id))
        solves.append(TruckSolve(truck_id, depot_id, cost, gap, used_fallback))
        total_cost += cost
        for candidate_lines in selected:
            built.append(
                _selected_job(
                    candidate_lines,
                    lookup,
                    pickers,
                    parse_settings,
                    tiers,
                    tour_cache,
                    settings.ignore_volume,
                )
            )
    _number_jobs(built)
    framed = _frames(built, parse_settings)
    rejected = pd.DataFrame(rejected_rows, columns=_REJECTED_COLUMNS)
    return ChunkModelResult(
        orders=framed.orders,
        items=framed.items,
        jobs=framed.jobs,
        rejected=rejected,
        objective=total_cost,
        fallback_depots=tuple(fallback),
        solves=tuple(solves),
    )


def write_model_outputs(result: ChunkModelResult, out_dir: Path) -> Path:
    """Write the picker files, one folder per dock, and ``rejected.csv``."""
    folder = write_chunk_outputs(result.as_chunk(), out_dir)
    result.rejected.to_csv(folder / "rejected.csv", index=False)
    pd.DataFrame(
        [
            {
                "truck_id": solve.truck_id,
                "depot_id": solve.depot_id,
                "minutes": solve.minutes,
                "mip_gap": "" if solve.mip_gap is None else solve.mip_gap,
                "fallback": solve.fallback,
            }
            for solve in result.solves
        ],
        columns=["truck_id", "depot_id", "minutes", "mip_gap", "fallback"],
    ).to_csv(folder / "solve.csv", index=False)
    return folder


def main(argv: Optional[Sequence[str]] = None) -> None:
    """Read a truck-load CSV and write jobs chosen by the HiGHS cover."""
    parser = argparse.ArgumentParser(
        description="Split truck pick lines into jobs with a HiGHS set-partitioning model."
    )
    parser.add_argument("--loads", type=Path, required=True, help="Truck-load CSV.")
    parser.add_argument("--travel", type=Path, required=True, help="Travel-time CSV.")
    parser.add_argument("--pickers", type=Path, required=True, help="Picker caps and equipment.")
    parser.add_argument("--out", type=Path, required=True, help="Folder for the job CSVs.")
    parser.add_argument("--max-job-lines", type=int, default=15)
    parser.add_argument("--job-overhead", type=float, default=5.0)
    parser.add_argument("--default-pick-time", type=float, default=0.5)
    parser.add_argument("--staging-buffer", type=float, default=30.0)
    parser.add_argument("--reference-order-lines", type=float, default=10.0)
    parser.add_argument("--must-ship-weight", type=float, default=MUST_SHIP_WEIGHT)
    parser.add_argument("--deadline-mode", choices=("off", "soft", "hard"), default="off")
    parser.add_argument("--wave-hours", type=float, default=DEFAULT_WAVE_HOURS)
    parser.add_argument("--wave-start", default=None)
    parser.add_argument(
        "--priority-max-lines",
        type=int,
        default=None,
        help="Optional cap on a job that carries a must-ship line. Omit it to allow any feasible job.",
    )
    parser.add_argument("--max-jobs", type=int, default=None)
    parser.add_argument("--time-limit", type=float, default=120.0)
    parser.add_argument("--mip-gap", type=float, default=0.0)
    parser.add_argument("--ignore-volume", action="store_true")
    parser.add_argument(
        "--destination-grouping",
        action="store_true",
        help="Keep each job to one stop. The default lets stops share a job.",
    )
    args = parser.parse_args(argv)
    settings = ChunkModelSettings(
        max_job_lines=args.max_job_lines,
        job_overhead=args.job_overhead,
        default_pick_time=args.default_pick_time,
        staging_buffer=args.staging_buffer,
        reference_order_lines=args.reference_order_lines,
        must_ship_weight=args.must_ship_weight,
        deadline_mode=args.deadline_mode,
        wave_hours=args.wave_hours,
        wave_start=args.wave_start,
        priority_max_lines=args.priority_max_lines,
        max_jobs=args.max_jobs,
        time_limit=args.time_limit,
        mip_gap=args.mip_gap,
        ignore_volume=args.ignore_volume,
        same_stop=args.destination_grouping,
    )
    result = chunk_loads_model(
        pd.read_csv(args.loads),
        pd.read_csv(args.travel),
        read_pickers(pd.read_csv(args.pickers)),
        settings,
    )
    write_model_outputs(result, args.out)
    print(
        f"Wrote {len(result.orders)} jobs and {len(result.rejected)} rejected rows "
        f"to {args.out} (cover {result.objective:.3f} minutes)"
    )
    for solve in result.solves:
        if solve.fallback:
            print(f"{solve.truck_id} at {solve.depot_id}: savings fallback, no MIP gap")
            continue
        if solve.mip_gap is None:
            print(f"{solve.truck_id} at {solve.depot_id}: {solve.minutes:.3f} minutes, gap not reported")
            continue
        print(
            f"{solve.truck_id} at {solve.depot_id}: {solve.minutes:.3f} minutes, "
            f"within {100.0 * solve.mip_gap:.2f}% of optimal"
        )


_REJECTED_COLUMNS = [
    "truck_id",
    "depot_id",
    "record_type",
    "record_id",
    "item_id",
    "reason",
    "weight",
    "volume",
]


def _picker_fits(weight: float, volume: Optional[float], picker: PickerSpec) -> bool:
    if weight > picker.max_weight + _EPS:
        return False
    return _volume_fits(volume, picker)


def _volume_fits(volume: Optional[float], picker: PickerSpec) -> bool:
    if volume is None or picker.max_volume is None:
        return True
    return volume <= picker.max_volume + _EPS


def _bundle_volume(lines: Sequence) -> Optional[float]:
    if any(line.volume is None for line in lines):
        return None
    return float(sum(line.volume for line in lines))


def _parse_settings(settings: ChunkModelSettings, pickers: Sequence[PickerSpec]) -> ChunkSettings:
    volumes = [picker.max_volume for picker in pickers if picker.max_volume is not None]
    return ChunkSettings(
        max_job_weight=max(picker.max_weight for picker in pickers),
        max_job_volume=max(volumes) if volumes and not settings.ignore_volume else None,
        max_job_lines=settings.max_job_lines,
        job_overhead=settings.job_overhead,
        default_pick_time=settings.default_pick_time,
        staging_buffer=settings.staging_buffer,
        reference_order_lines=settings.reference_order_lines,
        must_ship_weight=settings.must_ship_weight,
        deadline_mode=settings.deadline_mode,
        wave_hours=settings.wave_hours,
        wave_start=settings.wave_start,
    )


def _order_keys(loads: pd.DataFrame, lines: Sequence) -> Dict[str, Optional[str]]:
    """Map a line to its source order. None means the file has no source-order column."""
    if "source_order_id" not in loads.columns:
        return {line.line_key: None for line in lines}
    keys: Dict[str, Optional[str]] = {}
    rows = list(loads.itertuples(index=False))
    if len(rows) != len(lines):
        raise ValueError("The truck-load table and the parsed lines do not match.")
    for line, row in zip(lines, rows):
        raw = row.source_order_id
        if _is_missing(raw) or str(raw).strip() == "":
            keys[line.line_key] = line.line_key
        else:
            keys[line.line_key] = str(raw).strip()
    return keys


def _require_volumes(lines: Sequence, pickers: Sequence[PickerSpec], ignore_volume: bool) -> None:
    if ignore_volume:
        return
    if not any(picker.max_volume is not None for picker in pickers):
        return
    missing = [line.item_id for line in lines if line.volume is None]
    if missing:
        raise ValueError(
            "A line is missing volume and a picker has max_volume "
            f"({missing[0]}). Supply volumes, or pass --ignore-volume."
        )


def _reject_infeasible(
    lines: Sequence,
    order_of: Dict[str, Optional[str]],
    pickers: Sequence[PickerSpec],
    ignore_volume: bool,
) -> Tuple[list, List[dict]]:
    rejected: List[dict] = []
    surviving = []
    for line in lines:
        reason = failure_reason(line.weight, None if ignore_volume else line.volume, pickers)
        if reason is None:
            surviving.append(line)
            continue
        rejected.append(_rejection(line.truck_id, line.depot_id, "line", line.line_key, line.item_id, reason, line.weight, line.volume))
    by_order: Dict[OrderKey, list] = {}
    for line in surviving:
        order_id = order_of[line.line_key]
        if order_id is None:
            continue
        by_order.setdefault((line.truck_id, order_id), []).append(line)
    bad_orders = set()
    for (truck_id, order_id), group in sorted(by_order.items()):
        weight = float(sum(line.weight for line in group))
        volume = None if ignore_volume else _bundle_volume(group)
        reason = failure_reason(weight, volume, pickers)
        if reason is None:
            continue
        bad_orders.add((truck_id, order_id))
        rejected.append(
            _rejection(truck_id, group[0].depot_id, "order", order_id, "", reason, weight, volume)
        )
    kept = []
    for line in surviving:
        order_id = order_of[line.line_key]
        if order_id is not None and (line.truck_id, order_id) in bad_orders:
            continue
        kept.append(line)
    return kept, rejected


def _rejection(
    truck_id: str,
    depot_id: str,
    record_type: str,
    record_id: str,
    item_id: str,
    reason: str,
    weight: float,
    volume: Optional[float],
) -> dict:
    return {
        "truck_id": truck_id,
        "depot_id": depot_id,
        "record_type": record_type,
        "record_id": record_id,
        "item_id": item_id,
        "reason": reason,
        "weight": weight,
        "volume": "" if volume is None else volume,
    }


def _cover_depot(
    lines: Sequence,
    order_of: Dict[str, Optional[str]],
    depot_id: str,
    lookup: dict,
    pickers: Sequence[PickerSpec],
    settings: ChunkModelSettings,
    tour_cache: Dict[Tuple[str, Tuple[str, ...]], float],
) -> Tuple[List[list], float, bool, Optional[float]]:
    """Return the chosen jobs, their minutes, the fallback flag, and the MIP gap."""
    cells: Dict[Tuple[str, ...], list] = {}
    for line in lines:
        if settings.same_stop:
            key: Tuple[str, ...] = (line.truck_id, line.depot_id, line.destination_id)
        else:
            key = (line.truck_id, line.depot_id)
        cells.setdefault(key, []).append(line)
    candidates: List[CoverCandidate] = []
    payloads: Dict[str, list] = {}
    serial = 0
    for key in sorted(cells):
        atoms = _order_atoms(cells[key], order_of, depot_id, pickers, settings)
        if settings.same_stop:
            produced = _bundles_from_atoms(atoms, depot_id, lookup, pickers, settings)
        else:
            produced = _feasible_order_unions(atoms, depot_id, pickers, settings)
        for bundle in produced:
            serial += 1
            candidate_id = f"c{serial:05d}"
            line_ids = tuple(line.line_key for line in bundle)
            cost = _candidate_cost(bundle, depot_id, lookup, settings, tour_cache)
            priority = any(line.must_ship for line in bundle)
            candidates.append(CoverCandidate(candidate_id, line_ids, cost, priority))
            payloads[candidate_id] = list(bundle)
    solved = solve_cover(
        candidates,
        [line.line_key for line in lines],
        [line.line_key for line in lines if line.must_ship],
        settings.max_jobs,
        settings.time_limit,
        settings.mip_gap,
    )
    if solved is not None:
        return (
            [payloads[candidate.candidate_id] for candidate in solved.jobs],
            solved.objective,
            False,
            solved.mip_gap,
        )
    bundles = _savings_jobs(lines, depot_id, lookup, pickers, settings)
    cost = sum(_candidate_cost(bundle, depot_id, lookup, settings, tour_cache) for bundle in bundles)
    return bundles, cost, True, None


def _order_atoms(
    lines: Sequence,
    order_of: Dict[str, Optional[str]],
    depot_id: str,
    pickers: Sequence[PickerSpec],
    settings: ChunkModelSettings,
) -> List[list]:
    """One atom per source order that fits in a single job. Other lines stay loose.

    A later step may put several atoms in one job. An order that fits a picker,
    the line cap, and the 16-location tour limit is not split.
    """
    buckets: Dict[str, list] = {}
    for line in lines:
        order_id = order_of[line.line_key]
        key = line.line_key if order_id is None else order_id
        buckets.setdefault(key, []).append(line)
    atoms: List[list] = []
    for key in sorted(buckets):
        group = sorted(buckets[key], key=lambda line: line.line_key)
        if len(group) > 1 and _keep_order_whole(group, depot_id, pickers, settings):
            atoms.append(group)
            continue
        for line in group:
            atoms.append([line])
    return atoms


def _bundles_from_atoms(
    atoms: Sequence[list],
    depot_id: str,
    lookup: dict,
    pickers: Sequence[PickerSpec],
    settings: ChunkModelSettings,
) -> List[list]:
    """Feasible unions of whole orders at one stop.

    A stop with at most ``max_enumerate_lines`` orders contributes every union.
    A larger stop contributes each order alone and the Clarke-Wright merges.
    It does not list every pair and triple.
    """
    bundles: List[list] = []
    seen = set()

    def add(bundle: Sequence) -> None:
        if not _bundle_ok(bundle, depot_id, pickers, settings):
            return
        identity = tuple(sorted(line.line_key for line in bundle))
        if identity in seen:
            return
        seen.add(identity)
        bundles.append(list(bundle))

    if len(atoms) <= settings.max_enumerate_lines:
        width = len(atoms)
        for mask in range(1, 1 << width):
            chosen: List = []
            for bit in range(width):
                if mask & (1 << bit):
                    chosen.extend(atoms[bit])
            add(chosen)
        return bundles

    for atom in atoms:
        add(atom)
    cw = _cw_settings(settings, pickers)
    units = []
    for atom in atoms:
        ordered = _nearest_neighbor(depot_id, atom, lookup)
        units.append(_Unit(ordered[0].line_key, ordered))
    for route in _savings_routes(units, depot_id, lookup, cw):
        add(route.lines())
    return bundles


def _feasible_order_unions(
    atoms: Sequence[list],
    depot_id: str,
    pickers: Sequence[PickerSpec],
    settings: ChunkModelSettings,
) -> List[list]:
    """Every feasible union of whole-order atoms, including unions across stops.

    Line count, pick locations, weight, and volume only grow as an order is
    added, so an extension that already fails is not expanded. This is the
    pool when stops may mix. It does not fall back to Clarke-Wright columns.
    """
    if not atoms:
        return []
    atom_lines = [list(atom) for atom in atoms]
    weights = [float(sum(line.weight for line in atom)) for atom in atom_lines]
    volumes = [0.0 if settings.ignore_volume else float(_bundle_volume(atom) or 0.0) for atom in atom_lines]
    locations = [
        frozenset(line.location_id for line in atom if line.location_id != depot_id) for atom in atom_lines
    ]
    musts = [any(line.must_ship for line in atom) for atom in atom_lines]
    counts = [len(atom) for atom in atom_lines]
    heaviest = max(picker.max_weight for picker in pickers)
    if settings.ignore_volume or any(picker.max_volume is None for picker in pickers):
        roomiest: Optional[float] = None
    else:
        roomiest = max(picker.max_volume for picker in pickers if picker.max_volume is not None)
    bundles: List[list] = []
    chosen: List[int] = []

    def rec(start: int, weight: float, volume: float, locs: frozenset, must: bool, count: int) -> None:
        for index in range(start, len(atom_lines)):
            new_count = count + counts[index]
            if new_count > settings.max_job_lines:
                continue
            new_locs = locs | locations[index]
            if len(new_locs) > _MAX_TOUR_STOPS:
                continue
            new_weight = weight + weights[index]
            if new_weight > heaviest + _EPS:
                continue
            new_volume = volume + volumes[index]
            if roomiest is not None and new_volume > roomiest + _EPS:
                continue
            new_must = must or musts[index]
            if new_must and settings.priority_max_lines is not None and new_count > settings.priority_max_lines:
                continue
            chosen.append(index)
            merged: List = []
            for picked in chosen:
                merged.extend(atom_lines[picked])
            if _bundle_ok(merged, depot_id, pickers, settings):
                bundles.append(merged)
                rec(index + 1, new_weight, new_volume, new_locs, new_must, new_count)
            chosen.pop()

    rec(0, 0.0, 0.0, frozenset(), False, 0)
    return bundles


def _bundle_ok(
    lines: Sequence,
    depot_id: str,
    pickers: Sequence[PickerSpec],
    settings: ChunkModelSettings,
) -> bool:
    if not lines or len(lines) > settings.max_job_lines:
        return False
    if _tour_stops(lines, depot_id) > _MAX_TOUR_STOPS:
        return False
    if not _priority_ok(lines, settings.priority_max_lines):
        return False
    volume = None if settings.ignore_volume else _bundle_volume(lines)
    weight = float(sum(line.weight for line in lines))
    return failure_reason(weight, volume, pickers) is None


def _keep_order_whole(
    lines: Sequence,
    depot_id: str,
    pickers: Sequence[PickerSpec],
    settings: ChunkModelSettings,
) -> bool:
    return _bundle_ok(lines, depot_id, pickers, settings)


def _tour_stops(lines: Sequence, depot_id: str) -> int:
    return len({line.location_id for line in lines if line.location_id != depot_id})


def _savings_jobs(
    lines: Sequence,
    depot_id: str,
    lookup: dict,
    pickers: Sequence[PickerSpec],
    settings: ChunkModelSettings,
) -> List[list]:
    """Clarke-Wright jobs for one dock, used when HiGHS has no cover to keep.

    A merge that does not fit one picker, or that has more than 16 locations,
    is replaced by one job per line so the fallback still covers every line.
    """
    cw = _cw_settings(settings, pickers)
    ordered = sorted(lines, key=lambda line: line.line_key)
    units = [_Unit(line.line_key, [line]) for line in ordered]
    bundles: List[list] = []
    for route in _savings_routes(units, depot_id, lookup, cw):
        bundle = route.lines()
        if _bundle_ok(bundle, depot_id, pickers, settings):
            bundles.append(bundle)
            continue
        bundles.extend([[line] for line in bundle])
    return bundles


def _cw_settings(settings: ChunkModelSettings, pickers: Sequence[PickerSpec]) -> ChunkSettings:
    """Caps from one real picker: the highest weight, and that picker's own volume."""
    anchor = max(pickers, key=lambda picker: (picker.max_weight, picker.max_volume or 0.0, picker.picker_id))
    volume = None if settings.ignore_volume else anchor.max_volume
    return ChunkSettings(
        max_job_weight=anchor.max_weight,
        max_job_volume=volume,
        max_job_lines=min(settings.max_job_lines, _MAX_TOUR_STOPS),
        job_overhead=settings.job_overhead,
        default_pick_time=0.0,
    )


def _priority_ok(lines: Sequence, priority_max_lines: Optional[int]) -> bool:
    """A must-ship job is priority-eligible unless an optional line cap says otherwise.

    The default cap is unset, so any feasible job that contains the must-ship
    line is eligible. Jobs that fail the cap are not generated. That is what
    keeps a non-priority copy of the line out of the model.
    """
    if not any(line.must_ship for line in lines):
        return True
    if priority_max_lines is None:
        return True
    return len(lines) <= priority_max_lines


def _candidate_cost(
    lines: Sequence,
    depot_id: str,
    lookup: dict,
    settings: ChunkModelSettings,
    tour_cache: Dict[Tuple[str, Tuple[str, ...]], float],
) -> float:
    walk = _held_karp(depot_id, [line.location_id for line in lines], lookup, tour_cache)
    pick = 0.0
    for line in lines:
        pick += settings.default_pick_time if line.pick_time is None else line.pick_time
    return walk + settings.job_overhead + pick


def _held_karp(
    depot: str,
    locations: Sequence[str],
    lookup: dict,
    cache: Dict[Tuple[str, Tuple[str, ...]], float],
) -> float:
    """Length of the shortest closed directed tour of the distinct locations."""
    unique: List[str] = []
    seen = set()
    for location in locations:
        if location == depot or location in seen:
            continue
        seen.add(location)
        unique.append(location)
    key = (depot, tuple(sorted(unique)))
    if key in cache:
        return cache[key]
    count = len(unique)
    if count == 0:
        cache[key] = 0.0
        return 0.0
    if count > 16:
        raise ValueError(f"Held-Karp was asked for {count} locations; the job line cap should keep a tour at 16 nodes or fewer.")
    nodes = [depot] + unique
    # One lookup per leg. The subset loop then reads this table.
    span = count + 1
    dist = [[0.0] * span for _ in range(span)]
    for origin in range(span):
        for dest in range(span):
            if origin != dest:
                dist[origin][dest] = _leg(lookup, nodes[origin], nodes[dest])

    width = 1 << count
    infinity = float("inf")
    best = [[infinity] * count for _ in range(width)]
    for stop in range(count):
        best[1 << stop][stop] = dist[0][stop + 1]
    for mask in range(width):
        for stop in range(count):
            current = best[mask][stop]
            if current >= infinity or not mask & (1 << stop):
                continue
            row = dist[stop + 1]
            for nxt in range(count):
                if mask & (1 << nxt):
                    continue
                updated = current + row[nxt + 1]
                nxt_mask = mask | (1 << nxt)
                if updated < best[nxt_mask][nxt]:
                    best[nxt_mask][nxt] = updated
    full = width - 1
    length = min(best[full][stop] + dist[stop + 1][0] for stop in range(count))
    if not math.isfinite(length):
        raise ValueError(f"No closed tour from {depot} through {unique}.")
    cache[key] = float(length)
    return cache[key]


def _selected_job(
    lines: Sequence,
    lookup: dict,
    pickers: Sequence[PickerSpec],
    settings: ChunkSettings,
    tiers: dict,
    tour_cache: Dict[Tuple[str, Tuple[str, ...]], float],
    ignore_volume: bool,
) -> dict:
    depot_id = lines[0].depot_id
    ordered = _nearest_neighbor(depot_id, list(lines), lookup)
    record = _job_record(ordered, lookup, settings, tiers)
    record["walk"] = _held_karp(depot_id, [line.location_id for line in ordered], lookup, tour_cache)
    record["exceeds"] = False
    volume = None if ignore_volume else record["total_volume"]
    record["required_equipment"] = required_equipment(record["total_weight"], volume, pickers)
    return record


def _audit_cover(
    selected: Sequence[CoverCandidate],
    line_ids: Sequence[str],
    must_ship: Iterable[str],
) -> None:
    must = set(must_ship)
    counts = {line_id: 0 for line_id in line_ids}
    for candidate in selected:
        carries_must = False
        for line_id in candidate.line_ids:
            if line_id not in counts:
                raise RuntimeError(f"Selected job {candidate.candidate_id} names unknown line {line_id}.")
            counts[line_id] += 1
            carries_must = carries_must or line_id in must
        if carries_must and not candidate.priority:
            raise RuntimeError(
                f"Selected job {candidate.candidate_id} carries a must-ship line and is not priority-eligible."
            )
    broken = [line_id for line_id, count in counts.items() if count != 1]
    if broken:
        preview = ", ".join(broken[:5])
        raise RuntimeError(f"The cover does not contain each line once ({preview}).")


if __name__ == "__main__":
    main()
