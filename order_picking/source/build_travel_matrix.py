"""Build the travel-time matrix that Phase 1 consumes.

The picker walks aisle and cross-aisle centrelines. This script turns that
layout into a graph, runs Dijkstra between the depot and every storage
location, and writes the long table ``order_picking_optimizer.py`` reads
(``from_location``, ``to_location``, ``travel_time``).

The graph is built once per layout. Rerun it when racking, one-way rules, or
walking speed change. The picker model never sees aisles; it only looks up
this matrix.

Inputs
------
aisles: ``aisle_id``, ``x``, ``y_min``, ``y_max``. Optional ``one_way`` is
blank (both directions), ``up`` (increasing y only), or ``down``.
cross_aisles: ``cross_id``, ``y``, ``x_min``, ``x_max``. Optional ``one_way``
is blank, ``right`` (increasing x only), or ``left``. A connector between
blocks is just a cross-aisle whose x range spans those blocks.
locations: ``location_id``, ``aisle_id``, ``y`` (position along that aisle).
Optional ``retrieval_time`` is minutes added on arrival at that slot. A blank
cell uses the global ``retrieval_time``. A stored 0 stays 0. Upper levels are
a larger retrieval time on that row, not a z coordinate. Left and right racks
on one aisle share this centreline stop.
depot: one ``(x, y)`` point, or a table of ``depot_id``, ``x``, ``y``. Each
depot is snapped onto the nearest centreline. Any remaining off-centre
distance is added to trips that start or end at that depot.

``speed`` is distance units per minute (coordinates in metres and speed in
metres per minute produce minutes). Leave retrieval time at 0 when order lines
already carry ``pick_time``, so that time is not counted twice. Item handling
belongs on the order line, not in this matrix.

With more than one depot, ``assign_nearest_depots`` labels each slot with the
depot of shortest there-and-back walk. Phase 1 still tours from the one depot
the caller passes.

Example
-------
Run these from ``order_picking/``.

python source/build_travel_matrix.py --aisles inputs/layout_example/aisles.csv --cross-aisles inputs/layout_example/cross_aisles.csv --locations inputs/layout_example/locations.csv --depot-id DEPOT --depot-x 0 --depot-y -2 --speed 1 --out inputs/layout_example/travel_times.csv
python source/build_travel_matrix.py --aisles inputs/layout_small/aisles.csv --cross-aisles inputs/layout_small/cross_aisles.csv --locations inputs/layout_small/locations.csv --depots inputs/layout_small/depots.csv --out inputs/layout_small/travel_times.csv --assignment-out outputs/layout_small/depot_assignment.csv
"""

from __future__ import annotations

import argparse
import heapq
import math
import numbers
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import pandas as pd

_TOL = 1e-6
_INF = float("inf")


@dataclass(frozen=True)
class Aisle:
    aisle_id: str
    x: float
    y_min: float
    y_max: float
    one_way: str


@dataclass(frozen=True)
class CrossAisle:
    cross_id: str
    y: float
    x_min: float
    x_max: float
    one_way: str


@dataclass(frozen=True)
class Slot:
    location_id: str
    aisle_id: str
    y: float
    retrieval_time: Optional[float] = None


@dataclass(frozen=True)
class Depot:
    depot_id: str
    x: float
    y: float


def _records(table, name: str) -> List[dict]:
    if hasattr(table, "to_dict"):
        return list(table.to_dict("records"))
    return [dict(row) for row in table]


def _required(row: dict, fields: Sequence[str], name: str, index: int) -> None:
    missing = [field for field in fields if field not in row or _blank(row[field])]
    if missing:
        raise ValueError(f"{name} row {index} is missing {', '.join(missing)}.")


def _blank(value) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and value.strip() == "":
        return True
    if type(value).__name__ in {"NAType", "NaTType"}:
        return True
    if type(value).__name__ == "datetime64":
        return bool(value != value)
    if isinstance(value, bool):
        return False
    if isinstance(value, numbers.Real):
        return math.isnan(float(value))
    return False


def _number(value, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a number, got {value!r}.") from exc
    if not math.isfinite(number):
        raise ValueError(f"{label} must be a finite number.")
    return number


def _one_way(value, allowed: Sequence[str], label: str) -> str:
    if _blank(value):
        return ""
    text = str(value).strip().lower()
    if text in {"both", "two-way", "twoway"}:
        return ""
    if text not in allowed:
        choices = ", ".join(allowed)
        raise ValueError(f"{label} must be blank or one of: {choices}. Got {value!r}.")
    return text


def _within(value: float, low: float, high: float) -> bool:
    return (low - _TOL) <= value <= (high + _TOL)


def _optional_minutes(value, label: str) -> Optional[float]:
    """Blank means 'use the caller default'. A number must be finite and >= 0."""
    if _blank(value):
        return None
    minutes = _number(value, label)
    if minutes < 0:
        raise ValueError(f"{label} must be >= 0.")
    return minutes


def load_layout(aisles, cross_aisles, locations) -> Tuple[List[Aisle], List[CrossAisle], List[Slot]]:
    """Read aisle, cross-aisle, and slot tables into layout records."""
    aisle_rows = _records(aisles, "aisles")
    cross_rows = _records(cross_aisles, "cross_aisles")
    slot_rows = _records(locations, "locations")
    if not aisle_rows:
        raise ValueError("At least one aisle is required.")

    parsed_aisles: List[Aisle] = []
    seen_aisles = set()
    for index, row in enumerate(aisle_rows, start=1):
        _required(row, ("aisle_id", "x", "y_min", "y_max"), "aisles", index)
        aisle_id = str(row["aisle_id"]).strip()
        if aisle_id in seen_aisles:
            raise ValueError(f"Duplicate aisle_id {aisle_id!r}.")
        seen_aisles.add(aisle_id)
        y_min = _number(row["y_min"], f"aisles.{aisle_id}.y_min")
        y_max = _number(row["y_max"], f"aisles.{aisle_id}.y_max")
        if y_max < y_min:
            raise ValueError(f"Aisle {aisle_id} has y_max below y_min.")
        parsed_aisles.append(
            Aisle(
                aisle_id=aisle_id,
                x=_number(row["x"], f"aisles.{aisle_id}.x"),
                y_min=y_min,
                y_max=y_max,
                one_way=_one_way(row.get("one_way"), ("up", "down"), f"aisles.{aisle_id}.one_way"),
            )
        )

    parsed_crosses: List[CrossAisle] = []
    seen_crosses = set()
    for index, row in enumerate(cross_rows, start=1):
        _required(row, ("cross_id", "y", "x_min", "x_max"), "cross_aisles", index)
        cross_id = str(row["cross_id"]).strip()
        if cross_id in seen_crosses:
            raise ValueError(f"Duplicate cross_id {cross_id!r}.")
        seen_crosses.add(cross_id)
        x_min = _number(row["x_min"], f"cross_aisles.{cross_id}.x_min")
        x_max = _number(row["x_max"], f"cross_aisles.{cross_id}.x_max")
        if x_max < x_min:
            raise ValueError(f"Cross-aisle {cross_id} has x_max below x_min.")
        parsed_crosses.append(
            CrossAisle(
                cross_id=cross_id,
                y=_number(row["y"], f"cross_aisles.{cross_id}.y"),
                x_min=x_min,
                x_max=x_max,
                one_way=_one_way(
                    row.get("one_way"), ("right", "left"), f"cross_aisles.{cross_id}.one_way"
                ),
            )
        )

    aisle_by_id = {aisle.aisle_id: aisle for aisle in parsed_aisles}
    parsed_slots: List[Slot] = []
    seen_slots = set()
    for index, row in enumerate(slot_rows, start=1):
        _required(row, ("location_id", "aisle_id", "y"), "locations", index)
        location_id = str(row["location_id"]).strip()
        if location_id in seen_slots:
            raise ValueError(f"Duplicate location_id {location_id!r}.")
        seen_slots.add(location_id)
        aisle_id = str(row["aisle_id"]).strip()
        aisle = aisle_by_id.get(aisle_id)
        if aisle is None:
            raise ValueError(f"Location {location_id} refers to unknown aisle {aisle_id!r}.")
        y = _number(row["y"], f"locations.{location_id}.y")
        if not _within(y, aisle.y_min, aisle.y_max):
            raise ValueError(f"Location {location_id} y={y} is outside aisle {aisle_id}.")
        retrieval = _optional_minutes(
            row.get("retrieval_time"), f"locations.{location_id}.retrieval_time"
        )
        parsed_slots.append(
            Slot(location_id=location_id, aisle_id=aisle_id, y=y, retrieval_time=retrieval)
        )
    return parsed_aisles, parsed_crosses, parsed_slots


def load_depots(depots) -> List[Depot]:
    """Read ``depot_id``, ``x``, ``y`` rows. One pack station is one row."""
    rows = _records(depots, "depots")
    if not rows:
        raise ValueError("At least one depot is required.")
    parsed: List[Depot] = []
    seen = set()
    for index, row in enumerate(rows, start=1):
        _required(row, ("depot_id", "x", "y"), "depots", index)
        depot_id = str(row["depot_id"]).strip()
        if depot_id in seen:
            raise ValueError(f"Duplicate depot_id {depot_id!r}.")
        seen.add(depot_id)
        parsed.append(
            Depot(
                depot_id=depot_id,
                x=_number(row["x"], f"depots.{depot_id}.x"),
                y=_number(row["y"], f"depots.{depot_id}.y"),
            )
        )
    return parsed


def _depots_for_build(
    depot_id: Optional[str],
    depot_x: Optional[float],
    depot_y: Optional[float],
    depots,
) -> List[Depot]:
    if depots is not None and (depot_id is not None or depot_x is not None or depot_y is not None):
        raise ValueError("Pass either one depot or a depots table.")
    if depots is not None:
        return load_depots(depots)
    if depot_id is None or depot_x is None or depot_y is None:
        raise ValueError("A depot is required: depot_id with depot_x and depot_y, or a depots table.")
    if not isinstance(depot_id, str) or depot_id.strip() == "":
        raise ValueError("depot_id is required.")
    return [Depot(depot_id.strip(), _number(depot_x, "depot_x"), _number(depot_y, "depot_y"))]


def _project_aisle(x: float, y: float, aisle: Aisle) -> Tuple[float, float, float]:
    snapped_y = min(max(y, aisle.y_min), aisle.y_max)
    return abs(x - aisle.x) + abs(y - snapped_y), aisle.x, snapped_y


def _project_cross(x: float, y: float, cross: CrossAisle) -> Tuple[float, float, float]:
    snapped_x = min(max(x, cross.x_min), cross.x_max)
    return abs(y - cross.y) + abs(x - snapped_x), snapped_x, cross.y


def _link_chain(
    adj: Dict[str, List[Tuple[str, float]]],
    points: List[Tuple[float, str]],
    speed: float,
    forward: str,
    backward: str,
    one_way: str,
) -> None:
    """Connect a centreline sorted by position. ``forward`` is increasing position."""
    ordered = sorted(points, key=lambda item: (item[0], item[1]))
    for (pos_a, node_a), (pos_b, node_b) in zip(ordered, ordered[1:]):
        if node_a == node_b:
            continue
        gap = abs(pos_b - pos_a) / speed
        if gap <= _TOL:
            _add(adj, node_a, node_b, 0.0, both=True)
            continue
        if one_way == "":
            _add(adj, node_a, node_b, gap, both=True)
        elif one_way == forward:
            _add(adj, node_a, node_b, gap, both=False)
        elif one_way == backward:
            _add(adj, node_b, node_a, gap, both=False)


def _add(adj: Dict[str, List[Tuple[str, float]]], origin: str, dest: str, length: float, both: bool) -> None:
    adj.setdefault(origin, []).append((dest, length))
    if both:
        adj.setdefault(dest, []).append((origin, length))
    else:
        adj.setdefault(dest, [])


def _dijkstra(adj: Dict[str, List[Tuple[str, float]]], source: str) -> Dict[str, float]:
    best = {source: 0.0}
    heap = [(0.0, source)]
    while heap:
        distance, node = heapq.heappop(heap)
        if distance > best[node]:
            continue
        for nxt, length in adj.get(node, ()):
            trial = distance + length
            if trial < best.get(nxt, _INF):
                best[nxt] = trial
                heapq.heappush(heap, (trial, nxt))
    return best


@dataclass
class _LayoutGraph:
    adj: Dict[str, List[Tuple[str, float]]]
    coords: Dict[str, Tuple[float, float]]
    physical: Dict[str, Tuple[float, float]]
    public: List[str]
    snap_extra: Dict[str, float]
    slot_retrieval: Dict[str, Optional[float]]


def _layout_graph(
    aisles,
    cross_aisles,
    locations,
    depot_id: Optional[str],
    depot_x: Optional[float],
    depot_y: Optional[float],
    speed: float,
    depots,
) -> _LayoutGraph:
    """Centreline graph, node coordinates, and the public depot and slot ids."""
    if not math.isfinite(speed) or speed <= 0:
        raise ValueError("speed must be a finite number of distance units per minute > 0.")
    parsed_depots = _depots_for_build(depot_id, depot_x, depot_y, depots)
    parsed_aisles, parsed_crosses, slots = load_layout(aisles, cross_aisles, locations)
    slot_ids = {slot.location_id for slot in slots}
    for depot in parsed_depots:
        if depot.depot_id in slot_ids:
            raise ValueError(f"depot_id {depot.depot_id!r} collides with a location_id.")

    aisle_points: Dict[str, List[Tuple[float, str]]] = {aisle.aisle_id: [] for aisle in parsed_aisles}
    cross_points: Dict[str, List[Tuple[float, str]]] = {cross.cross_id: [] for cross in parsed_crosses}
    coords: Dict[str, Tuple[float, float]] = {}

    for aisle in parsed_aisles:
        for cross in parsed_crosses:
            if _within(cross.y, aisle.y_min, aisle.y_max) and _within(aisle.x, cross.x_min, cross.x_max):
                node = f"j:{aisle.aisle_id}:{cross.cross_id}"
                aisle_points[aisle.aisle_id].append((cross.y, node))
                cross_points[cross.cross_id].append((aisle.x, node))
                coords[node] = (aisle.x, cross.y)

    aisle_by_id = {aisle.aisle_id: aisle for aisle in parsed_aisles}
    cross_by_id = {cross.cross_id: cross for cross in parsed_crosses}
    for slot in slots:
        aisle_points[slot.aisle_id].append((slot.y, slot.location_id))
        coords[slot.location_id] = (aisle_by_id[slot.aisle_id].x, slot.y)

    snap_extra: Dict[str, float] = {}
    physical: Dict[str, Tuple[float, float]] = {}
    for depot in parsed_depots:
        snap_dist, snap_kind, snap_id, snap_pos = _nearest_centreline(
            depot.x, depot.y, parsed_aisles, parsed_crosses
        )
        if snap_kind == "aisle":
            aisle_points[snap_id].append((snap_pos, depot.depot_id))
            coords[depot.depot_id] = (aisle_by_id[snap_id].x, snap_pos)
        else:
            cross_points[snap_id].append((snap_pos, depot.depot_id))
            coords[depot.depot_id] = (snap_pos, cross_by_id[snap_id].y)
        snap_extra[depot.depot_id] = snap_dist / speed
        physical[depot.depot_id] = (depot.x, depot.y)

    adj: Dict[str, List[Tuple[str, float]]] = {}
    for aisle_id, points in aisle_points.items():
        _link_chain(adj, points, speed, "up", "down", aisle_by_id[aisle_id].one_way)
    for cross_id, points in cross_points.items():
        _link_chain(adj, points, speed, "right", "left", cross_by_id[cross_id].one_way)

    public = [depot.depot_id for depot in parsed_depots] + [slot.location_id for slot in slots]
    slot_retrieval = {slot.location_id: slot.retrieval_time for slot in slots}
    return _LayoutGraph(adj, coords, physical, public, snap_extra, slot_retrieval)


def _shortest_nodes(adj: Dict[str, List[Tuple[str, float]]], source: str, dest: str) -> List[str]:
    """Node ids of one shortest directed walk, including both ends."""
    if source == dest:
        return [source]
    best = {source: 0.0}
    parent: Dict[str, Optional[str]] = {source: None}
    heap = [(0.0, source)]
    while heap:
        distance, node = heapq.heappop(heap)
        if distance > best[node]:
            continue
        if node == dest:
            break
        for nxt, length in adj.get(node, ()):
            trial = distance + length
            if trial < best.get(nxt, _INF):
                best[nxt] = trial
                parent[nxt] = node
                heapq.heappush(heap, (trial, nxt))
    if dest not in parent:
        raise ValueError(f"No walk connects {source} -> {dest}.")
    path: List[str] = []
    node: Optional[str] = dest
    while node is not None:
        path.append(node)
        node = parent[node]
    path.reverse()
    return path


def _dedupe_points(points: Sequence[Tuple[float, float]]) -> List[Tuple[float, float]]:
    kept: List[Tuple[float, float]] = []
    for point in points:
        if kept and abs(kept[-1][0] - point[0]) <= _TOL and abs(kept[-1][1] - point[1]) <= _TOL:
            continue
        kept.append(point)
    return kept


def _polyline(graph: _LayoutGraph, origin: str, dest: str) -> List[Tuple[float, float]]:
    nodes = _shortest_nodes(graph.adj, origin, dest)
    points: List[Tuple[float, float]] = []
    physical_origin = graph.physical.get(origin)
    if physical_origin is not None:
        points.append(physical_origin)
    points.extend(graph.coords[node] for node in nodes)
    physical_dest = graph.physical.get(dest)
    if physical_dest is not None:
        points.append(physical_dest)
    return _dedupe_points(points)


@dataclass(frozen=True)
class WalkLeg:
    """One step of a route picture.

    ``points`` is the centreline walk. It is ``None`` when that step has no
    walk. ``ends`` is the straight segment between the two stops when both
    have coordinates, so a picture can mark the gap. ``note`` is empty when
    the walk exists.
    """

    origin: str
    dest: str
    points: Optional[List[Tuple[float, float]]]
    ends: Optional[Tuple[Tuple[float, float], Tuple[float, float]]]
    note: str


def _stop_point(graph: _LayoutGraph, name: str) -> Optional[Tuple[float, float]]:
    if name in graph.physical:
        return graph.physical[name]
    return graph.coords.get(name)


def route_legs(
    aisles,
    cross_aisles,
    locations,
    route: Sequence[str],
    depot_id: Optional[str] = None,
    depot_x: Optional[float] = None,
    depot_y: Optional[float] = None,
    speed: float = 1.0,
    depots=None,
) -> List[WalkLeg]:
    """One step per consecutive pair. A missing walk is a leg with a note.

    A depot off the centreline is included in ``points`` when the walk exists.
    Retrieval time is not a point on the line.
    """
    stops = [str(stop) for stop in route]
    if len(stops) < 2:
        raise ValueError("A plotted route needs at least two stops.")
    graph = _layout_graph(aisles, cross_aisles, locations, depot_id, depot_x, depot_y, speed, depots)
    public = set(graph.public)
    legs: List[WalkLeg] = []
    for origin, dest in zip(stops, stops[1:]):
        origin_point = _stop_point(graph, origin)
        dest_point = _stop_point(graph, dest)
        ends = None
        if origin_point is not None and dest_point is not None:
            ends = (origin_point, dest_point)
        if origin not in public or dest not in public:
            missing = origin if origin not in public else dest
            legs.append(
                WalkLeg(
                    origin,
                    dest,
                    None,
                    ends,
                    f"{missing} is not a depot or slot in this layout.",
                )
            )
            continue
        try:
            points = _polyline(graph, origin, dest)
        except ValueError as exc:
            legs.append(WalkLeg(origin, dest, None, ends, str(exc)))
            continue
        legs.append(WalkLeg(origin, dest, points, ends, ""))
    return legs


def route_walks(
    aisles,
    cross_aisles,
    locations,
    route: Sequence[str],
    depot_id: Optional[str] = None,
    depot_x: Optional[float] = None,
    depot_y: Optional[float] = None,
    speed: float = 1.0,
    depots=None,
) -> List[List[Tuple[float, float]]]:
    """One centreline polyline per step of ``route``.

    Each polyline is the shortest directed walk along aisle and cross-aisle
    centrelines. A depot that sits off the centreline adds its physical
    point at the end that names it. Retrieval time is not a point on the line.
    A stop with no walk raises ``ValueError``. ``route_legs`` records that gap
    instead of raising.
    """
    legs = route_legs(
        aisles,
        cross_aisles,
        locations,
        route,
        depot_id=depot_id,
        depot_x=depot_x,
        depot_y=depot_y,
        speed=speed,
        depots=depots,
    )
    broken = [leg for leg in legs if leg.points is None]
    if broken:
        raise ValueError(broken[0].note)
    return [leg.points for leg in legs if leg.points is not None]


def build_travel_matrix(
    aisles,
    cross_aisles,
    locations,
    depot_id: Optional[str] = None,
    depot_x: Optional[float] = None,
    depot_y: Optional[float] = None,
    speed: float = 1.0,
    retrieval_time: float = 0.0,
    depots=None,
) -> Dict[str, Dict[str, float]]:
    """Return ``{origin: {destination: travel_time}}`` for every depot and slot.

    Travel time is centreline distance divided by ``speed``. Arrival at a slot
    adds that slot's ``retrieval_time`` when the cell is filled, otherwise the
    global ``retrieval_time``. Trips that end at a depot do not add retrieval
    time. Pass one depot via ``depot_id`` / ``depot_x`` / ``depot_y``, or pass
    ``depots`` and leave those three empty.
    """
    if not math.isfinite(speed) or speed <= 0:
        raise ValueError("speed must be a finite number of distance units per minute > 0.")
    if not math.isfinite(retrieval_time) or retrieval_time < 0:
        raise ValueError("retrieval_time must be a finite number of minutes >= 0.")
    graph = _layout_graph(
        aisles, cross_aisles, locations, depot_id, depot_x, depot_y, speed, depots
    )
    public = graph.public
    slot_retrieval = graph.slot_retrieval
    snap_extra = graph.snap_extra
    paths = {origin: _dijkstra(graph.adj, origin) for origin in public}
    missing = [
        f"{origin} -> {dest}"
        for origin in public
        for dest in public
        if origin != dest and not math.isfinite(paths[origin].get(dest, _INF))
    ]
    if missing:
        shown = ", ".join(missing[:8])
        raise ValueError(f"No walk connects {shown}.")

    matrix: Dict[str, Dict[str, float]] = {origin: {} for origin in public}
    for origin in public:
        for dest in public:
            if origin == dest:
                continue
            travel = paths[origin][dest]
            travel += snap_extra.get(origin, 0.0)
            travel += snap_extra.get(dest, 0.0)
            if dest in slot_retrieval:
                added = slot_retrieval[dest]
                travel += retrieval_time if added is None else added
            matrix[origin][dest] = travel
    return matrix


def _nearest_centreline(
    x: float, y: float, aisles: Sequence[Aisle], crosses: Sequence[CrossAisle]
) -> Tuple[float, str, str, float]:
    best: Optional[Tuple[float, str, str, float]] = None
    for aisle in aisles:
        distance, _sx, snapped_y = _project_aisle(x, y, aisle)
        candidate = (distance, "aisle", aisle.aisle_id, snapped_y)
        if best is None or candidate < best:
            best = candidate
    for cross in crosses:
        distance, snapped_x, _sy = _project_cross(x, y, cross)
        candidate = (distance, "cross", cross.cross_id, snapped_x)
        if best is None or candidate < best:
            best = candidate
    if best is None:
        raise ValueError("The depot needs at least one aisle or cross-aisle to snap to.")
    return best


def travel_matrix_frame(matrix: Dict[str, Dict[str, float]]) -> pd.DataFrame:
    """Long table with the columns Phase 1 accepts."""
    rows = [
        {"from_location": origin, "to_location": dest, "travel_time": time}
        for origin, dests in matrix.items()
        for dest, time in dests.items()
    ]
    frame = pd.DataFrame(rows, columns=["from_location", "to_location", "travel_time"])
    if frame.empty:
        return frame
    return frame.sort_values(["from_location", "to_location"]).reset_index(drop=True)


def assign_nearest_depots(
    matrix: Dict[str, Dict[str, float]],
    depot_ids: Sequence[str],
) -> Dict[str, str]:
    """Assign each slot to the depot with the shortest there-and-back walk.

    The cost is ``depot → location + location → depot``. Equal costs use the
    lexicographically smaller ``depot_id``. Retrieval time is on the slot, so
    it does not change which depot wins.
    """
    if not depot_ids:
        raise ValueError("At least one depot is required.")
    depots = [str(depot_id).strip() for depot_id in depot_ids]
    if any(depot_id == "" for depot_id in depots):
        raise ValueError("depot_id is required.")
    if len(set(depots)) != len(depots):
        raise ValueError("Duplicate depot_id in depot_ids.")
    for depot_id in depots:
        if depot_id not in matrix:
            raise ValueError(f"Depot {depot_id!r} is not in the travel matrix.")
    depot_set = set(depots)
    assignment: Dict[str, str] = {}
    for location in matrix:
        if location in depot_set:
            continue
        best_id: Optional[str] = None
        best_cost: Optional[float] = None
        for depot_id in depots:
            try:
                cost = matrix[depot_id][location] + matrix[location][depot_id]
            except KeyError as exc:
                raise ValueError(f"No travel time between {depot_id} and {location}.") from exc
            if not math.isfinite(cost):
                raise ValueError(f"No travel time between {depot_id} and {location}.")
            closer = best_cost is None or cost < best_cost - _TOL
            tied = best_cost is not None and abs(cost - best_cost) <= _TOL and depot_id < best_id
            if closer or tied:
                best_cost = cost
                best_id = depot_id
        if best_id is None:
            raise ValueError(f"No depot can serve {location}.")
        assignment[location] = best_id
    return assignment


def assignment_frame(
    matrix: Dict[str, Dict[str, float]],
    assignment: Dict[str, str],
) -> pd.DataFrame:
    """One row per slot: ``location_id``, ``depot_id``, ``round_trip_time``."""
    rows = []
    for location, depot_id in assignment.items():
        rows.append(
            {
                "location_id": location,
                "depot_id": depot_id,
                "round_trip_time": matrix[depot_id][location] + matrix[location][depot_id],
            }
        )
    frame = pd.DataFrame(rows, columns=["location_id", "depot_id", "round_trip_time"])
    if frame.empty:
        return frame
    return frame.sort_values("location_id").reset_index(drop=True)


def build_travel_matrix_from_csv(
    aisles_path: Path,
    cross_aisles_path: Path,
    locations_path: Path,
    depot_id: Optional[str] = None,
    depot_x: Optional[float] = None,
    depot_y: Optional[float] = None,
    speed: float = 1.0,
    retrieval_time: float = 0.0,
    depots_path: Optional[Path] = None,
) -> Dict[str, Dict[str, float]]:
    """Read the layout CSVs and return the travel-time lookup."""
    depots = pd.read_csv(depots_path) if depots_path is not None else None
    return build_travel_matrix(
        pd.read_csv(aisles_path),
        pd.read_csv(cross_aisles_path),
        pd.read_csv(locations_path),
        depot_id=depot_id,
        depot_x=depot_x,
        depot_y=depot_y,
        speed=speed,
        retrieval_time=retrieval_time,
        depots=depots,
    )


def _depot_ids_from_args(args: argparse.Namespace) -> List[str]:
    if args.depots is not None:
        return [depot.depot_id for depot in load_depots(pd.read_csv(args.depots))]
    return [str(args.depot_id).strip()]


def main(argv: Optional[Iterable[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="Build a picker travel-time matrix from aisle centrelines.")
    parser.add_argument("--aisles", required=True, type=Path)
    parser.add_argument("--cross-aisles", required=True, type=Path)
    parser.add_argument("--locations", required=True, type=Path)
    parser.add_argument("--depot-id", default=None)
    parser.add_argument("--depot-x", default=None, type=float)
    parser.add_argument("--depot-y", default=None, type=float)
    parser.add_argument(
        "--depots",
        default=None,
        type=Path,
        help="CSV of depot_id, x, y. Use this or the single --depot-* flags.",
    )
    parser.add_argument("--speed", type=float, default=1.0, help="Distance units per minute.")
    parser.add_argument(
        "--retrieval-time",
        type=float,
        default=0.0,
        help="Minutes added on arrival at a slot with a blank retrieval_time. Use 0 when lines carry pick_time.",
    )
    parser.add_argument("--out", required=True, type=Path, help="CSV with from_location, to_location, travel_time.")
    parser.add_argument(
        "--assignment-out",
        default=None,
        type=Path,
        help="CSV with location_id, depot_id, round_trip_time. Nearest depot by there-and-back time.",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    single = args.depot_id is not None or args.depot_x is not None or args.depot_y is not None
    if args.depots is not None and single:
        parser.error("Pass --depots or --depot-id/--depot-x/--depot-y, not both.")
    if args.depots is None and (args.depot_id is None or args.depot_x is None or args.depot_y is None):
        parser.error("A depot is required: --depots, or all of --depot-id, --depot-x, and --depot-y.")
    matrix = build_travel_matrix_from_csv(
        args.aisles,
        args.cross_aisles,
        args.locations,
        depot_id=args.depot_id,
        depot_x=args.depot_x,
        depot_y=args.depot_y,
        speed=args.speed,
        retrieval_time=args.retrieval_time,
        depots_path=args.depots,
    )
    frame = travel_matrix_frame(matrix)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(args.out, index=False)
    print(f"Wrote {len(frame)} travel times to {args.out}")
    if args.assignment_out is not None:
        assignment = assign_nearest_depots(matrix, _depot_ids_from_args(args))
        assigned = assignment_frame(matrix, assignment)
        args.assignment_out.parent.mkdir(parents=True, exist_ok=True)
        assigned.to_csv(args.assignment_out, index=False)
        print(f"Wrote {len(assigned)} depot assignments to {args.assignment_out}")


if __name__ == "__main__":
    main()
