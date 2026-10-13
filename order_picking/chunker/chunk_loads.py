"""Cut a truck's pick lines into jobs the order picker can take one at a time.

The set-partitioning alternative is ``chunk_model.py``. Its equations are ``formulation.md``.
It reads a truck-load table and the travel-time matrix. It writes ``orders.csv``
and ``order_items.csv``. It does not read a LoadMax solve, and it does not
assign pickers.

Membership of a job comes from a directed Clarke-Wright savings heuristic.
The lines inside a job are then ordered by nearest neighbor from that job's
depot. Today's picker still takes one depot per solve, so the writer also
splits the jobs into ``by_depot/<depot_id>/``.
"""

from __future__ import annotations

import argparse
import math
import numbers
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence

import pandas as pd

_ROOT = Path(__file__).resolve().parents[1]
_SOURCE = str(_ROOT / "source")
if _SOURCE not in sys.path:
    sys.path.insert(0, _SOURCE)

from order_picking_optimizer import (  # noqa: E402
    DEFAULT_TIER_MULTIPLIERS,
    DEFAULT_WAVE_HOURS,
    MUST_SHIP_WEIGHT,
    _build_lookup,
    _coerce_clock,
    _hours_between,
    _is_missing,
    _is_truthy_flag,
    _normalize_tier_table,
    _tier_multiplier,
    _urgency_until_cutoff,
)

ORDER_COLUMNS = [
    "order_id",
    "total_weight",
    "total_volume",
    "priority_weight",
    "cutoff_time",
    "account_tier",
    "wait_time",
    "must_ship",
    "required_equipment",
    "truck_id",
    "depot_id",
]
ITEM_COLUMNS = [
    "order_id",
    "item_id",
    "location_id",
    "quantity",
    "weight",
    "volume",
    "pick_time",
]
JOB_COLUMNS = [
    "job_id",
    "truck_id",
    "depot_id",
    "cutoff_time",
    "priority_weight",
    "walk_minutes",
    "line_count",
    "exceeds_capacity",
]

_REQUIRED_LOAD_COLUMNS = ("truck_id", "depot_id", "item_id", "location_id", "weight")
_EPS = 1e-9


@dataclass(frozen=True)
class ChunkSettings:
    """Caps and weights for one chunker run. Times are minutes."""

    max_job_weight: float
    max_job_volume: Optional[float] = None
    max_job_lines: int = 15
    job_overhead: float = 5.0
    default_pick_time: float = 0.5
    staging_buffer: float = 30.0
    require_destination_grouping: bool = False
    reference_order_lines: float = 10.0
    must_ship_weight: float = MUST_SHIP_WEIGHT
    deadline_mode: str = "off"
    wave_hours: float = DEFAULT_WAVE_HOURS
    wave_start: Optional[str] = None

    def __post_init__(self) -> None:
        if self.max_job_weight <= 0 or not math.isfinite(self.max_job_weight):
            raise ValueError("max_job_weight must be a finite number > 0.")
        if self.max_job_volume is not None and (
            self.max_job_volume <= 0 or not math.isfinite(self.max_job_volume)
        ):
            raise ValueError("max_job_volume must be a finite number > 0 when set.")
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


@dataclass(frozen=True)
class _Line:
    line_key: str
    truck_id: str
    depot_id: str
    item_id: str
    location_id: str
    quantity: float
    weight: float
    volume: Optional[float]
    pick_time: Optional[float]
    must_ship: bool
    destination_id: str
    departure_minutes: Optional[float]
    account_tier: Optional[str]
    base_weight: Optional[float]


@dataclass
class _Unit:
    unit_id: str
    lines: List[_Line]


@dataclass
class _Route:
    route_id: str
    parts: List[_Unit]

    def lines(self) -> List[_Line]:
        collected: List[_Line] = []
        for part in self.parts:
            collected.extend(part.lines)
        return collected

    @property
    def head_id(self) -> str:
        return self.parts[0].unit_id

    @property
    def tail_id(self) -> str:
        return self.parts[-1].unit_id


@dataclass(frozen=True)
class ChunkResult:
    """Picker files plus the job table the chunker itself reports."""

    orders: pd.DataFrame
    items: pd.DataFrame
    jobs: pd.DataFrame


def chunk_loads(
    loads: pd.DataFrame,
    travel,
    settings: ChunkSettings,
) -> ChunkResult:
    """Split each (truck, depot) load into picker-feasible jobs.

    ``travel`` is a dict-of-dicts or a table with ``from_location``,
    ``to_location``, and ``travel_time``. A line heavier than the job cap
    is still emitted, alone, so the picker can report it.
    """
    lookup = _build_lookup(travel)
    lines = _read_lines(loads, settings)
    if not lines:
        raise ValueError("The truck-load table has no lines.")
    tiers = _normalize_tier_table(DEFAULT_TIER_MULTIPLIERS)
    built: List[dict] = []
    grouped: Dict[tuple, List[_Line]] = {}
    for line in lines:
        grouped.setdefault((line.truck_id, line.depot_id), []).append(line)
    for truck_id, depot_id in sorted(grouped):
        group = grouped[(truck_id, depot_id)]
        _check_group(group)
        units = _initial_units(group, lookup, settings)
        routes = _savings_routes(units, depot_id, lookup, settings)
        routes.sort(key=lambda route: min(line.line_key for line in route.lines()))
        for route in routes:
            ordered = _nearest_neighbor(depot_id, route.lines(), lookup)
            built.append(_job_record(ordered, lookup, settings, tiers))
    _number_jobs(built)
    return _frames(built, settings)


def write_chunk_outputs(result: ChunkResult, out_dir: Path) -> Path:
    """Write the combined picker files and one folder per depot.

    The combined files keep ``depot_id`` on every job. ``by_depot/<depot_id>/``
    holds the same rows for a single-depot picker run.
    """
    folder = Path(out_dir)
    folder.mkdir(parents=True, exist_ok=True)
    result.orders.to_csv(folder / "orders.csv", index=False)
    result.items.to_csv(folder / "order_items.csv", index=False)
    result.jobs.to_csv(folder / "jobs.csv", index=False)
    for depot_id, orders in result.orders.groupby("depot_id", sort=True):
        name = _depot_folder_name(str(depot_id))
        depot_dir = folder / "by_depot" / name
        depot_dir.mkdir(parents=True, exist_ok=True)
        job_ids = set(orders["order_id"])
        items = result.items[result.items["order_id"].isin(job_ids)]
        jobs = result.jobs[result.jobs["job_id"].isin(job_ids)]
        orders.to_csv(depot_dir / "orders.csv", index=False)
        items.to_csv(depot_dir / "order_items.csv", index=False)
        jobs.to_csv(depot_dir / "jobs.csv", index=False)
    return folder


def main(argv: Optional[Sequence[str]] = None) -> None:
    """Read a truck-load CSV and write picker jobs."""
    parser = argparse.ArgumentParser(
        description="Split truck pick lines into jobs for the order picker."
    )
    parser.add_argument("--loads", type=Path, required=True, help="Truck-load CSV.")
    parser.add_argument("--travel", type=Path, required=True, help="Travel-time CSV.")
    parser.add_argument("--out", type=Path, required=True, help="Folder for the job CSVs.")
    parser.add_argument("--max-job-weight", type=float, default=None)
    parser.add_argument("--max-job-volume", type=float, default=None)
    parser.add_argument("--max-job-lines", type=int, default=15)
    parser.add_argument("--pickers", type=Path, default=None, help="Use the heaviest picker's caps when a cap flag is omitted.")
    parser.add_argument("--job-overhead", type=float, default=5.0)
    parser.add_argument("--default-pick-time", type=float, default=0.5)
    parser.add_argument("--staging-buffer", type=float, default=30.0)
    parser.add_argument("--destination-grouping", action="store_true")
    parser.add_argument("--reference-order-lines", type=float, default=10.0)
    parser.add_argument("--must-ship-weight", type=float, default=MUST_SHIP_WEIGHT)
    parser.add_argument("--deadline-mode", choices=("off", "soft", "hard"), default="off")
    parser.add_argument("--wave-hours", type=float, default=DEFAULT_WAVE_HOURS)
    parser.add_argument("--wave-start", default=None, help="Origin for datetime departures.")
    args = parser.parse_args(argv)
    weight_cap, volume_cap = _caps_from_args(args)
    settings = ChunkSettings(
        max_job_weight=weight_cap,
        max_job_volume=volume_cap,
        max_job_lines=args.max_job_lines,
        job_overhead=args.job_overhead,
        default_pick_time=args.default_pick_time,
        staging_buffer=args.staging_buffer,
        require_destination_grouping=args.destination_grouping,
        reference_order_lines=args.reference_order_lines,
        must_ship_weight=args.must_ship_weight,
        deadline_mode=args.deadline_mode,
        wave_hours=args.wave_hours,
        wave_start=args.wave_start,
    )
    loads = pd.read_csv(args.loads)
    travel = pd.read_csv(args.travel)
    result = chunk_loads(loads, travel, settings)
    write_chunk_outputs(result, args.out)
    print(f"Wrote {len(result.orders)} jobs to {args.out}")


def _caps_from_args(args: argparse.Namespace) -> tuple:
    weight_cap = args.max_job_weight
    volume_cap = args.max_job_volume
    if weight_cap is None or (volume_cap is None and args.pickers is not None):
        if args.pickers is None:
            raise ValueError("Pass --max-job-weight or --pickers.")
        picker_weight, picker_volume = _heaviest_picker_caps(pd.read_csv(args.pickers))
        if weight_cap is None:
            weight_cap = picker_weight
        if volume_cap is None:
            volume_cap = picker_volume
    return weight_cap, volume_cap


def _heaviest_picker_caps(pickers: pd.DataFrame) -> tuple:
    if "max_weight" not in pickers.columns or pickers.empty:
        raise ValueError("The picker file needs a max_weight column and at least one row.")
    weights = pd.to_numeric(pickers["max_weight"], errors="coerce")
    if weights.isna().any() or (weights <= 0).any():
        raise ValueError("Every picker max_weight must be a finite number > 0.")
    row = pickers.loc[weights.idxmax()]
    volume = None
    if "max_volume" in pickers.columns and not _is_missing(row.max_volume):
        volume = float(row.max_volume)
        if volume <= 0 or not math.isfinite(volume):
            raise ValueError("The heaviest picker's max_volume must be a finite number > 0.")
    return float(row.max_weight), volume


def _read_lines(loads: pd.DataFrame, settings: ChunkSettings) -> List[_Line]:
    missing = [name for name in _REQUIRED_LOAD_COLUMNS if name not in loads.columns]
    if missing:
        raise ValueError(f"Truck-load table is missing columns: {', '.join(missing)}.")
    lines: List[_Line] = []
    for index, row in enumerate(loads.itertuples(index=False)):
        truck_id = _text(getattr(row, "truck_id"), f"row {index + 1} truck_id")
        depot_id = _text(getattr(row, "depot_id"), f"row {index + 1} depot_id")
        item_id = _text(getattr(row, "item_id"), f"row {index + 1} item_id")
        location_id = _text(getattr(row, "location_id"), f"row {index + 1} location_id")
        weight = _nonnegative(getattr(row, "weight"), f"row {index + 1} weight")
        quantity = 1.0
        if hasattr(row, "quantity") and not _is_missing(row.quantity):
            quantity = _nonnegative(row.quantity, f"row {index + 1} quantity")
        volume = None
        if hasattr(row, "volume") and not _is_missing(row.volume):
            volume = _nonnegative(row.volume, f"row {index + 1} volume")
        pick_time = None
        if hasattr(row, "pick_time") and not _is_missing(row.pick_time):
            pick_time = _nonnegative(row.pick_time, f"row {index + 1} pick_time")
        destination = ""
        if hasattr(row, "destination_id") and not _is_missing(row.destination_id):
            destination = str(row.destination_id).strip()
        departure = None
        if hasattr(row, "departure_time"):
            departure = _to_minutes(row.departure_time, settings.wave_start, f"row {index + 1} departure_time")
        tier = None
        if hasattr(row, "account_tier") and not _is_missing(row.account_tier):
            tier = str(row.account_tier).strip()
        base = None
        if hasattr(row, "base_weight") and not _is_missing(row.base_weight):
            base = _nonnegative(row.base_weight, f"row {index + 1} base_weight")
        lines.append(
            _Line(
                line_key=f"{index:06d}",
                truck_id=truck_id,
                depot_id=depot_id,
                item_id=item_id,
                location_id=location_id,
                quantity=quantity,
                weight=weight,
                volume=volume,
                pick_time=pick_time,
                must_ship=bool(hasattr(row, "must_ship") and _is_truthy_flag(row.must_ship)),
                destination_id=destination,
                departure_minutes=departure,
                account_tier=tier,
                base_weight=base,
            )
        )
    return lines


def _check_group(lines: Sequence[_Line]) -> None:
    label = f"{lines[0].truck_id} at {lines[0].depot_id}"
    if len({line.departure_minutes for line in lines}) > 1:
        raise ValueError(f"{label} has more than one departure_time.")
    if len({line.account_tier for line in lines}) > 1:
        raise ValueError(f"{label} has more than one account_tier.")
    if len({line.base_weight for line in lines}) > 1:
        raise ValueError(f"{label} has more than one base_weight.")


def _within_caps(lines: Sequence[_Line], settings: ChunkSettings) -> bool:
    weight = sum(line.weight for line in lines)
    if weight > settings.max_job_weight + _EPS:
        return False
    if len(lines) > settings.max_job_lines:
        return False
    if settings.max_job_volume is None:
        return True
    if any(line.volume is None for line in lines):
        return False
    volume = sum(line.volume or 0.0 for line in lines)
    return volume <= settings.max_job_volume + _EPS


def _leg(lookup: dict, origin: str, dest: str) -> float:
    if origin == dest:
        return 0.0
    try:
        value = float(lookup[origin][dest])
    except KeyError as exc:
        raise ValueError(f"Travel time missing for {origin} -> {dest}.") from exc
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Travel time for {origin} -> {dest} is not a number.") from exc
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"Travel time for {origin} -> {dest} must be a finite number >= 0.")
    return value


def _nearest_neighbor(depot: str, lines: Sequence[_Line], lookup: dict) -> List[_Line]:
    pending = list(lines)
    ordered: List[_Line] = []
    current = depot
    while pending:
        pending.sort(key=lambda line: (_leg(lookup, current, line.location_id), line.line_key))
        chosen = pending.pop(0)
        ordered.append(chosen)
        current = chosen.location_id
    return ordered


def _closed_walk(depot: str, lines: Sequence[_Line], lookup: dict) -> float:
    walked = 0.0
    current = depot
    for line in lines:
        walked += _leg(lookup, current, line.location_id)
        current = line.location_id
    return walked + _leg(lookup, current, depot)


def _text(value: object, label: str) -> str:
    if _is_missing(value):
        raise ValueError(f"{label} is blank.")
    text = str(value).strip()
    if text == "" or text.lower() == "nan":
        raise ValueError(f"{label} is blank.")
    return text


def _nonnegative(value: object, label: str) -> float:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a number.") from exc
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{label} must be a finite number >= 0.")
    return number


def _to_minutes(value: object, wave_start: Optional[str], label: str) -> Optional[float]:
    if _is_missing(value):
        return None
    clock = _coerce_clock(value)
    if isinstance(clock, numbers.Real):
        return float(clock)
    if wave_start is None:
        raise ValueError(f"{label} is a datetime and wave_start is required.")
    origin = _coerce_clock(wave_start)
    if isinstance(origin, numbers.Real):
        raise ValueError("wave_start must be a datetime when departure_time is a datetime.")
    return _hours_between(clock, origin) * 60.0


def _depot_folder_name(depot_id: str) -> str:
    if depot_id in {"", ".", ".."} or any(sep in depot_id for sep in "/\\"):
        raise ValueError(f"depot_id {depot_id!r} cannot be a folder name.")
    return depot_id


def _initial_units(lines: Sequence[_Line], lookup: dict, settings: ChunkSettings) -> List[_Unit]:
    depot = lines[0].depot_id
    if not settings.require_destination_grouping:
        return [_Unit(line.line_key, [line]) for line in lines]
    groups: Dict[tuple, List[_Line]] = {}
    for line in lines:
        if line.destination_id == "":
            key = ("line", line.line_key)
        else:
            key = ("dest", line.destination_id)
        groups.setdefault(key, []).append(line)
    units: List[_Unit] = []
    for key in sorted(groups):
        group = groups[key]
        if _within_caps(group, settings):
            ordered = _nearest_neighbor(depot, group, lookup)
            units.append(_Unit(f"{key[0]}-{key[1]}", ordered))
            continue
        ordered = _nearest_neighbor(depot, group, lookup)
        for piece in _pack_in_order(ordered, settings):
            units.append(_Unit(piece[0].line_key, piece))
    return units


def _pack_in_order(ordered: Sequence[_Line], settings: ChunkSettings) -> List[List[_Line]]:
    pieces: List[List[_Line]] = []
    current: List[_Line] = []
    for line in ordered:
        if not _within_caps([line], settings):
            if current:
                pieces.append(current)
                current = []
            pieces.append([line])
            continue
        trial = current + [line]
        if current and not _within_caps(trial, settings):
            pieces.append(current)
            current = [line]
        else:
            current = trial
    if current:
        pieces.append(current)
    return pieces


def _savings_routes(
    units: Sequence[_Unit],
    depot: str,
    lookup: dict,
    settings: ChunkSettings,
) -> List[_Route]:
    routes = {unit.unit_id: _Route(unit.unit_id, [unit]) for unit in units}
    head_route = {unit.unit_id: unit.unit_id for unit in units}
    tail_route = {unit.unit_id: unit.unit_id for unit in units}
    scored = []
    for left in units:
        for right in units:
            if left.unit_id == right.unit_id:
                continue
            origin = left.lines[-1].location_id
            dest = right.lines[0].location_id
            saving = (
                _leg(lookup, origin, depot)
                + _leg(lookup, depot, dest)
                - _leg(lookup, origin, dest)
                + settings.job_overhead
            )
            scored.append((saving, left.unit_id, right.unit_id))
    scored.sort(key=lambda item: (-item[0], item[1], item[2]))
    for saving, left_id, right_id in scored:
        if saving <= _EPS:
            break
        left_route_id = tail_route.get(left_id)
        right_route_id = head_route.get(right_id)
        if left_route_id is None or right_route_id is None or left_route_id == right_route_id:
            continue
        left_route = routes[left_route_id]
        right_route = routes[right_route_id]
        if left_route.tail_id != left_id or right_route.head_id != right_id:
            continue
        if not _within_caps(left_route.lines() + right_route.lines(), settings):
            continue
        head_route.pop(left_route.head_id, None)
        tail_route.pop(left_route.tail_id, None)
        head_route.pop(right_route.head_id, None)
        tail_route.pop(right_route.tail_id, None)
        left_route.parts.extend(right_route.parts)
        del routes[right_route_id]
        head_route[left_route.head_id] = left_route.route_id
        tail_route[left_route.tail_id] = left_route.route_id
    return list(routes.values())


def _job_record(lines: Sequence[_Line], lookup: dict, settings: ChunkSettings, tiers: dict) -> dict:
    first = lines[0]
    departure = first.departure_minutes
    cutoff = None
    if departure is not None:
        cutoff = departure - settings.staging_buffer
        if cutoff < -_EPS:
            raise ValueError(
                f"{first.truck_id} at {first.depot_id} has a cutoff before wave start."
            )
    volume_values = [line.volume for line in lines]
    total_volume = None if any(value is None for value in volume_values) else sum(volume_values)
    walk = _closed_walk(first.depot_id, lines, lookup)
    return {
        "truck_id": first.truck_id,
        "depot_id": first.depot_id,
        "cutoff": cutoff,
        "priority": _priority(lines, settings, tiers),
        "must_ship": any(line.must_ship for line in lines),
        "account_tier": first.account_tier or "",
        "total_weight": sum(line.weight for line in lines),
        "total_volume": total_volume,
        "walk": walk,
        "exceeds": not _within_caps(lines, settings),
        "lines": list(lines),
    }


def _priority(lines: Sequence[_Line], settings: ChunkSettings, tiers: dict) -> float:
    if any(line.must_ship for line in lines):
        return float(settings.must_ship_weight)
    known = [line.base_weight for line in lines if line.base_weight is not None]
    if known:
        base = float(known[0])
    else:
        base = _derived_base(lines[0], settings, tiers)
    weight = base * len(lines) / settings.reference_order_lines
    if not math.isfinite(weight) or weight < 0 or weight >= 1.0e6:
        raise ValueError(f"Job priority {weight} is outside the picker's weight range.")
    return weight


def _derived_base(line: _Line, settings: ChunkSettings, tiers: dict) -> float:
    # Soft and hard deadline modes keep urgency neutral. The cutoff is stored
    # for a later deadline constraint. It does not also raise the weight.
    if settings.deadline_mode in {"soft", "hard"}:
        urgency = 1.0
    elif line.departure_minutes is None:
        urgency = 1.0
    else:
        hours_left = (line.departure_minutes - settings.staging_buffer) / 60.0
        urgency = _urgency_until_cutoff(hours_left, settings.wave_hours)
    return urgency * _tier_multiplier(line.account_tier, tiers)


def _number_jobs(jobs: List[dict]) -> None:
    counters: Dict[str, int] = {}
    jobs.sort(key=lambda job: (job["truck_id"], job["depot_id"], job["lines"][0].line_key))
    for job in jobs:
        counters[job["truck_id"]] = counters.get(job["truck_id"], 0) + 1
        job["job_id"] = f"{job['truck_id']}-J{counters[job['truck_id']]:02d}"


def _frames(jobs: Sequence[dict], settings: ChunkSettings) -> ChunkResult:
    order_rows = []
    item_rows = []
    job_rows = []
    for job in jobs:
        order_rows.append(
            {
                "order_id": job["job_id"],
                "total_weight": job["total_weight"],
                "total_volume": "" if job["total_volume"] is None else job["total_volume"],
                "priority_weight": job["priority"],
                "cutoff_time": "" if job["cutoff"] is None else job["cutoff"],
                "account_tier": job["account_tier"],
                "wait_time": "",
                "must_ship": "true" if job["must_ship"] else "",
                "required_equipment": job.get("required_equipment", ""),
                "truck_id": job["truck_id"],
                "depot_id": job["depot_id"],
            }
        )
        for line in job["lines"]:
            pick_time = line.pick_time if line.pick_time is not None else settings.default_pick_time
            item_rows.append(
                {
                    "order_id": job["job_id"],
                    "item_id": line.item_id,
                    "location_id": line.location_id,
                    "quantity": line.quantity,
                    "weight": line.weight,
                    "volume": "" if line.volume is None else line.volume,
                    "pick_time": pick_time,
                }
            )
        job_rows.append(
            {
                "job_id": job["job_id"],
                "truck_id": job["truck_id"],
                "depot_id": job["depot_id"],
                "cutoff_time": "" if job["cutoff"] is None else job["cutoff"],
                "priority_weight": job["priority"],
                "walk_minutes": job["walk"],
                "line_count": len(job["lines"]),
                "exceeds_capacity": job["exceeds"],
            }
        )
    return ChunkResult(
        orders=pd.DataFrame(order_rows, columns=ORDER_COLUMNS),
        items=pd.DataFrame(item_rows, columns=ITEM_COLUMNS),
        jobs=pd.DataFrame(job_rows, columns=JOB_COLUMNS),
    )


if __name__ == "__main__":
    main()
