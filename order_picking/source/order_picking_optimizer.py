"""
Order Picking Planning Optimizer
=================================

Two-phase optimizer for warehouse order picking, implementing the model
described in ``order_picking_planning_optimizer_spec.md``.

Phase 1 (Routing)
------------------
For each order, compute a closed picking route (depot -> every pick location
in the order -> depot) and its duration. The default is nearest neighbor
plus 2-opt on the directed travel times. A reversal is kept only when the
reversed walk is shorter; a tour longer than nearest neighbor is discarded.
``phase1_solver`` ``scip`` or ``highs`` solves a directed tour (one arc out
of each node, one arc in, leave-set cuts). The duration is that directed
walk, not an undirected edge cost. The undirected city model in
``tsp_open_solvers.py`` is a separate demo and is not used here.

The travel-time matrix is an input. ``build_travel_matrix.py`` builds it
from aisle centrelines. This module only looks up ``from_location``,
``to_location``, ``travel_time``.

Phase 2 (Assignment)
---------------------
Given per-order route durations from phase 1, a MILP assigns orders to
pickers to maximize the weighted sum of orders fulfilled within each
picker's shift time, respecting equipment eligibility and per-order
weight/volume capacity (C2, C4), while discouraging workload imbalance
across pickers (C5). The default backend is HiGHS
(``order_picking_highs.py``, via ``scipy.optimize.milp``). Pass
``assignment_solver="gurobi"`` to use Gurobi, or ``"auto"`` to use Gurobi
only when a license checks out. Order weighting follows a linear scheme (C8) unless
``staged_assignment`` is set. All orders equally weighted recovers plain
count maximization (``benchmark_mode``). A "must-ship" order is a large but
finite ``priority_weight``. The staged flag releases must-ship jobs by
count, then other jobs by weight, then minimises lateness. A finite
``priority_weight`` already on the order is used as-is. Otherwise
``compute_priority_weight`` fills it from cutoff wave, wait time, and
account tier before assignment.

Note on feasibility: the default phase 2 model is always feasible, since
leaving every order unfulfilled (all ``y[o] = 0``) is a trivially valid
solution. "Infeasibility" in that model shows up as a low objective and
orders in ``orders_not_fulfilled``. ``require_must_ship`` can make the
model infeasible when the required must-ship jobs cannot all fit.
"""

from __future__ import annotations

import argparse
import math
import numbers
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

import numpy as np

# None until the first license probe. False means import failed or no license.
_GUROBI_LICENSE_OK: Optional[bool] = None

# C8 weight bands. Due date means the order slips a pick wave, so urgency is
# a step against ``wave_hours``, not 1/hours (that blows up at the cutoff).
# Derived weights stay well below 1e6, the range where a blended MIP
# coefficient starts to dominate the numeric tolerance.
DEFAULT_WAVE_HOURS = 8.0
# One ordinary order (priority 1) is worth 1000 minutes of unevenness.
# Any positive weight shares a shift when the fulfilled set is unchanged.
# At this size, sharing does not pay for dropping an order that still fits.
SHARE_SHIFT_BALANCE = 0.001
# Later stages may swap equal-value jobs. The lock sits far below any weight.
_LOCK_TOLERANCE = 1e-6
# A must-ship count is an integer. Half a job cannot hide inside this gap.
_MUST_COUNT_ABS_GAP = 0.5
URGENCY_OVERDUE = 6.0
URGENCY_THIS_WAVE = 4.0
URGENCY_NEXT_WAVE = 2.0
URGENCY_LATER = 1.0
MUST_SHIP_WEIGHT = 1.0e5
MAX_PRIORITY_WEIGHT = 1.0e6
DEFAULT_TIER_MULTIPLIERS = {
    "key": 2.0,
    "a": 2.0,
    "standard": 1.0,
    "b": 1.0,
    "c": 1.0,
}


from tsp_open_solvers import (
    DIRECTED_SOLVERS,
    find_initial_tour,
    improve_tour_simultaneous,
    is_single_tour,
    tour_order,
)


# ---------------------------------------------------------------------------
# Phase 1: Routing
# ---------------------------------------------------------------------------

class _SubProblem:
    """Minimal stand-in for ``tsp_open_solvers.Problem``, built from a
    precomputed travel-time lookup instead of haversine distances between
    coordinates.

    Exposes the attributes the TSP helpers use (``n``, ``D``, ``neighbors``,
    ``iter_limit``, ``tour_distance``). ``D`` keeps both directions, so a
    one-way aisle stays one-way. ``neighbors`` is the N-best list from the
    city heuristic: the ``k`` closest other nodes.
    """

    def __init__(
        self,
        node_ids: Sequence[str],
        lookup: Dict[str, Dict[str, float]],
        order_id: Optional[str] = None,
    ):
        self.names = list(node_ids)
        self.n = len(self.names)
        if self.n < 3:
            raise ValueError("A routing subproblem needs at least 3 nodes (depot + 2 picks).")
        self.D = np.zeros((self.n, self.n))
        for i, a in enumerate(self.names):
            for j, b in enumerate(self.names):
                if i != j:
                    self.D[i, j] = _travel(lookup, a, b, order_id)
        k = min(int(math.floor(math.sqrt(self.n))), self.n - 1)
        self.k = max(k, 1)
        self.neighbors = []
        for i in range(self.n):
            order = sorted((j for j in range(self.n) if j != i), key=lambda j: (self.D[i, j], j))
            self.neighbors.append(order[: self.k])
        self.iter_limit = max(self.n // 3, 1)

    def tour_distance(self, nxt: Sequence[int]) -> float:
        return float(sum(self.D[i, nxt[i]] for i in range(self.n)))


def _travel(
    lookup: Dict[str, Dict[str, float]],
    origin: str,
    dest: str,
    order_id: Optional[str] = None,
) -> float:
    """Travel time for one directed pair. A missing or negative entry is an input error."""
    prefix = f"Order {order_id}: " if order_id is not None else ""
    try:
        value = float(lookup[origin][dest])
    except KeyError as exc:
        raise ValueError(f"{prefix}Travel time missing for {origin} -> {dest}.") from exc
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{prefix}Travel time for {origin} -> {dest} is not a number.") from exc
    if not math.isfinite(value) or value < 0:
        raise ValueError(
            f"{prefix}Travel time for {origin} -> {dest} must be a finite number >= 0."
        )
    return value


def _build_lookup(distance_matrix) -> Dict[str, Dict[str, float]]:
    """Accept either a dict-of-dicts travel-time lookup, or a long-format
    DataFrame with columns ``from_location``, ``to_location``,
    ``travel_time`` (as described in the specification's "Travel Distances /
    Times" input), and return a dict-of-dicts lookup.
    """
    if isinstance(distance_matrix, dict):
        return distance_matrix
    lookup: Dict[str, Dict[str, float]] = {}
    for row in distance_matrix.itertuples(index=False):
        lookup.setdefault(row.from_location, {})[row.to_location] = float(row.travel_time)
    return lookup


def route_order(
    order_id: str,
    pick_locations: Sequence[str],
    depot_id: str,
    distance_matrix,
    solver: str = "heuristic",
    time_limit: Optional[float] = None,
    pick_time: float = 0.0,
) -> dict:
    """Solve the phase 1 routing problem for a single order: a closed tour
    starting and ending at ``depot_id`` that visits every distinct pick
    location exactly once.

    Parameters
    ----------
    order_id : identifier of the order, carried through to the output only.
    pick_locations : storage locations to visit for this order (duplicates
        are dropped; C6, dedicated storage, assumes one location per item).
    depot_id : fixed start/end node (pack station or depot).
    distance_matrix : precomputed travel-time data, either a dict-of-dicts
        ``{from_location: {to_location: travel_time}}`` or a DataFrame as
        described above. Always assumed to already exist.
    solver : "heuristic" (default; nearest-neighbor + directed 2-opt,
        requires no optional packages), or "scip" / "highs" (exact directed
        tour: one arc in and one arc out of each node; requires
        ``pyscipopt`` / ``highspy``).
    time_limit : seconds, only used by the exact solvers.
    pick_time : handling time added to the travel time. The specification's
        route duration is travel plus pick time. Zero when lines omit it.

    Returns
    -------
    dict with ``order_id``, ``route`` (list of location ids, a closed tour
    starting and ending at ``depot_id``), and ``route_duration``.
    """
    if not math.isfinite(pick_time) or pick_time < 0:
        raise ValueError(f"pick_time for order {order_id} must be a finite number >= 0.")
    stops = [loc for loc in dict.fromkeys(pick_locations) if loc != depot_id]
    lookup = _build_lookup(distance_matrix)
    if not stops:
        raise ValueError(f"Order {order_id} has no pick locations.")
    # One stop has no TSP choice: walk out and back.
    if len(stops) == 1:
        loc = stops[0]
        travel = _travel(lookup, depot_id, loc, order_id) + _travel(lookup, loc, depot_id, order_id)
        return {
            "order_id": order_id,
            "route": [depot_id, loc, depot_id],
            "route_duration": travel + pick_time,
        }

    nodes = [depot_id] + stops
    problem = _SubProblem(nodes, lookup, order_id)
    depot_index = 0

    if solver == "heuristic":
        init = find_initial_tour(problem)
        best = improve_tour_simultaneous(problem, init)
        # 2-opt must not hand phase 2 a directed walk longer than nearest neighbor.
        if (not is_single_tour(best["nxt"])) or best["distance"] > init["distance"] + 1e-9:
            best = init
        nxt = best["nxt"]
        duration = best["distance"]
    elif solver in ("scip", "highs"):
        result = DIRECTED_SOLVERS[solver](problem, time_limit=time_limit)
        nxt = result.get("nxt")
        if not nxt or not is_single_tour(nxt):
            raise RuntimeError(
                f"{solver} did not return a single directed tour for order {order_id}: "
                f"status={result.get('status')}"
            )
        duration = problem.tour_distance(nxt)
    else:
        raise ValueError(f"Unknown solver '{solver}', expected 'heuristic', 'scip' or 'highs'.")

    order = tour_order(nxt, start=depot_index)
    route = [problem.names[i] for i in order] + [problem.names[depot_index]]
    return {"order_id": order_id, "route": route, "route_duration": duration + pick_time}


def route_orders(
    order_items: Iterable,
    depot_id: str,
    distance_matrix,
    solver: str = "heuristic",
    time_limit: Optional[float] = None,
) -> Dict[str, dict]:
    """Run phase 1 routing for every order in an "Order Lines / Items" table
    (any iterable of rows with ``order_id`` and ``location_id`` fields, for
    example the result of ``DataFrame.itertuples()``).
    """
    locations_by_order: Dict[str, List[str]] = {}
    pick_time_by_order: Dict[str, float] = {}
    for row in order_items.itertuples(index=False):
        order_id = row.order_id
        locations_by_order.setdefault(order_id, []).append(row.location_id)
        line_pick = _optional_hours(getattr(row, "pick_time", 0.0), default=0.0)
        pick_time_by_order[order_id] = pick_time_by_order.get(order_id, 0.0) + line_pick
    return {
        order_id: route_order(
            order_id,
            locs,
            depot_id,
            distance_matrix,
            solver,
            time_limit,
            pick_time_by_order.get(order_id, 0.0),
        )
        for order_id, locs in locations_by_order.items()
    }


# ---------------------------------------------------------------------------
# Priority weights (C8)
# ---------------------------------------------------------------------------

def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == ""
    if type(value).__name__ in {"NAType", "NaTType"}:
        return True
    if type(value).__name__ == "datetime64":
        return bool(value != value)
    if isinstance(value, bool):
        return False
    if isinstance(value, numbers.Real):
        return math.isnan(float(value))
    return False


def _field(row: Any, name: str, default: Any = None) -> Any:
    if not hasattr(row, name):
        return default
    return getattr(row, name)


def _optional_hours(value: Any, default: float = 0.0) -> float:
    if _is_missing(value):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Expected a number of hours, got {value!r}.") from exc
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"Expected a finite number of hours >= 0, got {value!r}.")
    return number


def _is_truthy_flag(value: Any) -> bool:
    if value is True:
        return True
    if value is False or _is_missing(value):
        return False
    if isinstance(value, numbers.Real) and not isinstance(value, bool):
        return float(value) != 0.0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y"}
    return False


def _coerce_clock(value: Any) -> Any:
    """Return a float clock, a datetime, or None when the cell is empty."""
    if _is_missing(value):
        return None
    if isinstance(value, bool):
        raise ValueError(f"Cannot read time value {value!r}.")
    if isinstance(value, numbers.Real):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError(f"Cannot read time value {value!r}.")
        return number
    if isinstance(value, datetime) or hasattr(value, "to_pydatetime"):
        return value
    if isinstance(value, str):
        text = value.strip()
        try:
            return float(text)
        except ValueError:
            pass
        try:
            return datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(f"Cannot read time value {value!r}.") from exc
    if hasattr(value, "year") and hasattr(value, "day"):
        return value
    raise ValueError(f"Cannot read time value {value!r}.")


def _hours_between(later: Any, earlier: Any) -> float:
    try:
        delta = later - earlier
    except TypeError as exc:
        raise ValueError("Times must both be numbers or both be datetimes.") from exc
    total_seconds = getattr(delta, "total_seconds", None)
    if callable(total_seconds):
        return float(total_seconds()) / 3600.0
    try:
        return float(delta)
    except (TypeError, ValueError) as exc:
        raise ValueError("Times must both be numbers or both be datetimes.") from exc


def _hours_until(cutoff: Any, now: Any) -> float:
    clock = _coerce_clock(cutoff)
    if clock is None:
        raise ValueError("cutoff_time is missing.")
    if now is None:
        if isinstance(clock, numbers.Real):
            return float(clock)
        raise ValueError("now is required when cutoff_time is a datetime.")
    current = _coerce_clock(now)
    if current is None:
        raise ValueError("now is required when cutoff_time is a datetime.")
    return _hours_between(clock, current)


def _wait_hours(row: Any, now: Any) -> float:
    wait_time = _field(row, "wait_time")
    if not _is_missing(wait_time):
        return _optional_hours(wait_time)
    queued = _field(row, "queued_since")
    if _is_missing(queued):
        return 0.0
    if now is None:
        raise ValueError(
            f"now is required to read queued_since for order {_field(row, 'order_id')}."
        )
    waited = _hours_between(_coerce_clock(now), _coerce_clock(queued))
    if waited < 0:
        raise ValueError(f"queued_since for order {_field(row, 'order_id')} is after now.")
    return waited


def _urgency_until_cutoff(hours_left: float, wave_hours: float) -> float:
    if hours_left < 0:
        return URGENCY_OVERDUE
    if hours_left <= wave_hours:
        return URGENCY_THIS_WAVE
    if hours_left <= 2.0 * wave_hours:
        return URGENCY_NEXT_WAVE
    return URGENCY_LATER


def _urgency_from_wait(wait_hours: float, wave_hours: float) -> float:
    # No cutoff: a long queue stands in for "due this wave", not for overdue.
    if wait_hours >= 2.0 * wave_hours:
        return URGENCY_THIS_WAVE
    if wait_hours >= wave_hours:
        return URGENCY_NEXT_WAVE
    return URGENCY_LATER


def _normalize_tier_table(tier_multipliers: Optional[Dict[Any, float]]) -> Dict[str, float]:
    source = DEFAULT_TIER_MULTIPLIERS if tier_multipliers is None else tier_multipliers
    table: Dict[str, float] = {}
    for key, value in source.items():
        try:
            mult = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Tier multiplier for {key!r} is not a number.") from exc
        if not math.isfinite(mult) or mult < 0:
            raise ValueError(f"Tier multiplier for {key!r} must be a finite number >= 0.")
        table[str(key).strip().lower()] = mult
    return table


def _tier_multiplier(tier: Any, table: Dict[str, float]) -> float:
    if isinstance(tier, bool):
        return table.get("key", 2.0) if tier else 1.0
    if _is_missing(tier):
        return 1.0
    key = str(tier).strip().lower()
    if key not in table:
        known = ", ".join(sorted(table))
        raise ValueError(f"Unknown account_tier {tier!r}. Known tiers: {known}.")
    return table[key]


def _explicit_priority(row: Any) -> Optional[float]:
    if not hasattr(row, "priority_weight"):
        return None
    raw = row.priority_weight
    if _is_missing(raw):
        return None
    try:
        number = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"priority_weight for order {_field(row, 'order_id')} is not a number."
        ) from exc
    if not math.isfinite(number) or number < 0 or number >= MAX_PRIORITY_WEIGHT:
        raise ValueError(
            f"priority_weight for order {_field(row, 'order_id')} must be finite, "
            f">= 0, and below {MAX_PRIORITY_WEIGHT:g}."
        )
    return number


def _check_weight(weight: float, order_id: Any) -> float:
    if not math.isfinite(weight) or weight < 0 or weight >= MAX_PRIORITY_WEIGHT:
        raise ValueError(
            f"Derived priority_weight {weight} for order {order_id} must be finite, "
            f">= 0, and below {MAX_PRIORITY_WEIGHT:g}."
        )
    return weight


def _priority_for_row(
    row: Any,
    now: Any,
    wave_hours: float,
    table: Dict[str, float],
    respect_explicit: bool,
) -> float:
    if respect_explicit:
        explicit = _explicit_priority(row)
        if explicit is not None:
            return explicit
    if _is_truthy_flag(_field(row, "must_ship")):
        return MUST_SHIP_WEIGHT
    tier = _field(row, "account_tier")
    if _is_missing(tier) and _is_truthy_flag(_field(row, "key_account")):
        tier = "key"
    cutoff = _field(row, "cutoff_time")
    if not _is_missing(cutoff):
        urgency = _urgency_until_cutoff(_hours_until(cutoff, now), wave_hours)
    else:
        urgency = _urgency_from_wait(_wait_hours(row, now), wave_hours)
    return _check_weight(urgency * _tier_multiplier(tier, table), _field(row, "order_id"))


def compute_priority_weight(
    orders,
    now: Any = None,
    wave_hours: float = DEFAULT_WAVE_HOURS,
    tier_multipliers: Optional[Dict[Any, float]] = None,
    respect_explicit: bool = True,
):
    """Fill ``priority_weight`` from the cutoff wave, wait time, and account tier.

    A finite ``priority_weight`` already on a row is kept when
    ``respect_explicit`` is true. ``solve_order_assignment`` applies
    ``benchmark_mode`` after this and forces every weight to 1.

    ``cutoff_time`` and ``now`` are both numbers on one clock, or both
    datetimes. A numeric ``cutoff_time`` with ``now`` omitted is hours
    remaining. With no cutoff, ``wait_time`` (hours) or ``queued_since``
    supplies urgency. Default tiers are key/A = 2 and standard/B/C = 1.
    Pass ``tier_multipliers`` to replace that table. ``must_ship`` sets
    ``MUST_SHIP_WEIGHT`` when no explicit weight is kept.
    """
    if not math.isfinite(wave_hours) or wave_hours <= 0:
        raise ValueError("wave_hours must be a finite number of hours > 0.")
    if not hasattr(orders, "copy") or not hasattr(orders, "itertuples"):
        raise TypeError("orders must be a DataFrame.")
    frame = orders.copy()
    table = _normalize_tier_table(tier_multipliers)
    frame["priority_weight"] = [
        _priority_for_row(row, now, wave_hours, table, respect_explicit)
        for row in frame.itertuples(index=False)
    ]
    return frame


# ---------------------------------------------------------------------------
# Phase 2: Assignment
# ---------------------------------------------------------------------------

def _eligible(order_row, picker_row) -> bool:
    """C4 (equipment eligibility) and C2 (per-order weight/volume capacity).

    A blank equipment or volume cell is no constraint. ``pandas.read_csv``
    turns a blank cell into NaN, and ``_is_missing`` treats that NaN as empty.
    """
    required_equipment = getattr(order_row, "required_equipment", None)
    if not _is_missing(required_equipment) and required_equipment != picker_row.equipment_type:
        return False
    if order_row.total_weight > picker_row.max_weight:
        return False
    total_volume = getattr(order_row, "total_volume", None)
    max_volume = getattr(picker_row, "max_volume", None)
    if (
        not _is_missing(total_volume)
        and not _is_missing(max_volume)
        and float(total_volume) > float(max_volume)
    ):
        return False
    return True


def _gurobi_license_ok() -> bool:
    """True when gurobipy imports and a license can be checked out."""
    global _GUROBI_LICENSE_OK
    if _GUROBI_LICENSE_OK is not None:
        return _GUROBI_LICENSE_OK
    try:
        import gurobipy as gp
    except ImportError:
        _GUROBI_LICENSE_OK = False
        return False
    env = None
    try:
        env = gp.Env(empty=True)
        env.setParam("OutputFlag", 0)
        env.start()
        _GUROBI_LICENSE_OK = True
    except Exception:
        _GUROBI_LICENSE_OK = False
    finally:
        if env is not None:
            env.dispose()
    return _GUROBI_LICENSE_OK


def _resolve_assignment_solver(requested: str) -> str:
    name = (requested or "highs").strip().lower()
    if name == "auto":
        return "gurobi" if _gurobi_license_ok() else "highs"
    if name in {"gurobi", "highs"}:
        if name == "gurobi" and not _gurobi_license_ok():
            raise RuntimeError(
                "Gurobi is not importable or has no license. "
                "Use assignment_solver='highs' or 'auto'."
            )
        return name
    raise ValueError(
        f"Unknown assignment_solver '{requested}', expected 'auto', 'gurobi', or 'highs'."
    )


def _public_assignment_result(
    weight: Dict[str, float],
    eligible_pickers: Dict[str, set],
    picker_ids: Sequence[str],
    solved: dict,
    solver_name: str,
) -> dict:
    """Map a raw Gurobi or HiGHS solve into the phase-2 solution dictionary."""
    is_optimal = bool(solved["is_optimal"])
    result = {
        "status": solved["status"],
        "is_optimal": is_optimal,
        "objective_value": solved["objective"] if is_optimal else None,
        "orders_fulfilled": [],
        "orders_not_fulfilled": [],
        "picker_assignments": {pid: [] for pid in picker_ids},
        "picker_workload": {},
        "order_priority_weight": {},
        "solver": solver_name,
    }
    if not is_optimal:
        return result
    y = solved["y"]
    x = solved["x"]
    for oid in weight:
        chosen = None
        best = 0.5
        for pid in eligible_pickers[oid]:
            val = float(x.get((oid, pid), 0.0))
            if val > best:
                best = val
                chosen = pid
        if float(y.get(oid, 0.0)) > 0.5 and chosen is not None:
            result["orders_fulfilled"].append(oid)
            result["order_priority_weight"][oid] = weight[oid]
            result["picker_assignments"][chosen].append(oid)
        else:
            reason = (
                "no_eligible_picker"
                if not eligible_pickers[oid]
                else "shift_time_limit_or_priority_tradeoff"
            )
            result["orders_not_fulfilled"].append({"order_id": oid, "reason": reason})
    result["picker_workload"] = {
        pid: float(solved["workload"].get(pid, 0.0)) for pid in picker_ids
    }
    return result


def _solve_assignment_gurobi(
    weight: Dict[str, float],
    duration: Dict[str, float],
    eligible_pairs,
    eligible_pickers: Dict[str, set],
    picker_ids: Sequence[str],
    picker_shift: Dict[str, float],
    balance_weight: float,
    time_limit: float,
    mip_gap: Optional[float],
    lateness_weight: float = 0.0,
    deadlines: Optional[Dict[str, float]] = None,
    lateness_tier: Optional[Dict[str, float]] = None,
    jack_penalty: float = 0.0,
    penalized_pairs: Sequence = (),
    forced_orders: Sequence[str] = (),
    stage: str = "weighted",
    locks: Optional[dict] = None,
    must_ship_ids: Sequence[str] = (),
    abs_gap: Optional[float] = None,
    accept_incumbent: bool = False,
    objective_upper: Optional[float] = None,
) -> dict:
    """Same model as ``solve_assignment_highs``, solved with Gurobi.

    Gurobi stops when either the relative gap or the absolute gap is met.
    The relative gap is set to 0 and the absolute gap to ``abs_gap``, so a
    looser caller ``mip_gap`` cannot end the solve early.
    ``objective_upper`` is recorded. It is not a second relative stop.
    """
    import time

    import gurobipy as gp
    from gurobipy import GRB

    del mip_gap
    held = locks or {}
    must_ids = [oid for oid in must_ship_ids if oid in weight]
    must_set = set(must_ids)
    tiers = lateness_tier or {}
    need_lateness = stage in {"lateness", "balance", "jack"} or "lateness" in held or (
        stage == "weighted" and lateness_weight > 0.0
    )
    dated = [
        oid
        for oid in weight
        if need_lateness and deadlines is not None and oid in deadlines
    ]
    known_stages = {"weighted", "must_count", "ordinary", "lateness", "balance", "jack"}
    if stage not in known_stages:
        raise ValueError(f"Unknown assignment stage {stage!r}.")

    model = gp.Model("order_picking_assignment")
    model.Params.OutputFlag = 0
    model.Params.TimeLimit = float(time_limit)
    model.Params.MIPGap = 0.0
    model.Params.MIPGapAbs = 0.0 if abs_gap is None else float(abs_gap)

    y = model.addVars(list(weight.keys()), vtype=GRB.BINARY, name="fulfilled")
    x = model.addVars(list(eligible_pairs), vtype=GRB.BINARY, name="assign")
    workload = model.addVars(list(picker_ids), lb=0.0, name="workload")
    u = model.addVars(list(picker_ids), lb=0.0, name="imbalance")

    model.addConstrs(
        (gp.quicksum(x[oid, pid] for pid in eligible_pickers[oid]) == y[oid] for oid in weight),
        name="single_picker",
    )
    model.addConstrs(
        (
            workload[pid]
            == gp.quicksum(duration[oid] * x[oid, pid] for oid in weight if (oid, pid) in x)
            for pid in picker_ids
        ),
        name="workload_def",
    )
    model.addConstrs(
        (workload[pid] <= float(picker_shift[pid]) for pid in picker_ids),
        name="shift_time_limit",
    )
    if picker_ids:
        avg_workload = gp.quicksum(workload[pid] for pid in picker_ids) / len(picker_ids)
        model.addConstrs(
            (u[pid] >= workload[pid] - avg_workload for pid in picker_ids), name="balance_pos"
        )
        model.addConstrs(
            (u[pid] >= avg_workload - workload[pid] for pid in picker_ids), name="balance_neg"
        )

    for oid in forced_orders:
        if oid not in y:
            raise ValueError(f"Forced order {oid} is not in the assignment.")
        model.addConstr(y[oid] == 1, name=f"force_{oid}")

    completion = {}
    late = {}
    if dated:
        from assignment_reports import edd_sequence

        sequence = [oid for oid in edd_sequence(list(weight), deadlines or {}) if oid in set(dated)]
        big_m = max((float(picker_shift[pid]) for pid in picker_ids), default=0.0)
        if big_m <= 0.0:
            big_m = 1.0
        earliest = min(float(deadlines[oid]) for oid in dated)
        big_m_late = big_m + max(0.0, -earliest)
        completion = model.addVars(dated, lb=0.0, name="completion")
        late = model.addVars(dated, lb=0.0, name="late")
        position = {oid: index for index, oid in enumerate(sequence)}
        for oid in sequence:
            prefix = [earlier for earlier in sequence if position[earlier] <= position[oid]]
            for pid in eligible_pickers.get(oid, ()):
                if (oid, pid) not in x:
                    continue
                prefix_time = gp.quicksum(
                    duration[earlier] * x[earlier, pid]
                    for earlier in prefix
                    if (earlier, pid) in x
                )
                model.addConstr(
                    completion[oid] >= prefix_time - big_m * (1 - x[oid, pid]),
                    name=f"edd_{oid}_{pid}",
                )
            model.addConstr(
                late[oid]
                >= completion[oid] - float(deadlines[oid]) - big_m_late * (1 - y[oid]),
                name=f"late_{oid}",
            )

    tolerance = float(held.get("tolerance", _LOCK_TOLERANCE))
    if "must_count" in held and must_ids:
        model.addConstr(
            gp.quicksum(y[oid] for oid in must_ids) >= float(held["must_count"]) - tolerance,
            name="lock_must_count",
        )
    if "ordinary_weight" in held:
        ordinary = [oid for oid in weight if oid not in must_set]
        model.addConstr(
            gp.quicksum(float(weight[oid]) * y[oid] for oid in ordinary)
            >= float(held["ordinary_weight"]) - tolerance,
            name="lock_ordinary",
        )
    if "lateness" in held and late:
        model.addConstr(
            gp.quicksum(float(tiers.get(oid, 1.0)) * late[oid] for oid in late)
            <= float(held["lateness"]) + tolerance,
            name="lock_lateness",
        )
    if "balance" in held and picker_ids:
        model.addConstr(
            gp.quicksum(u[pid] for pid in picker_ids) <= float(held["balance"]) + tolerance,
            name="lock_balance",
        )
    if "jack" in held:
        penalized = [pair for pair in penalized_pairs if pair in x]
        if penalized:
            model.addConstr(
                gp.quicksum(x[pair] for pair in penalized) <= float(held["jack"]) + tolerance,
                name="lock_jack",
            )

    if stage == "weighted":
        lateness_cost = gp.quicksum(
            float(lateness_weight) * float(tiers.get(oid, 1.0)) * late[oid] for oid in late
        )
        jack_cost = gp.quicksum(
            float(jack_penalty) * x[pair] for pair in penalized_pairs if pair in x and jack_penalty
        )
        objective = (
            gp.quicksum(float(weight[oid]) * y[oid] for oid in weight)
            - float(balance_weight) * gp.quicksum(u[pid] for pid in picker_ids)
            - lateness_cost
            - jack_cost
        )
        model.setObjective(objective, GRB.MAXIMIZE)
    elif stage == "must_count":
        model.setObjective(gp.quicksum(y[oid] for oid in must_ids), GRB.MAXIMIZE)
    elif stage == "ordinary":
        model.setObjective(
            gp.quicksum(float(weight[oid]) * y[oid] for oid in weight if oid not in must_set),
            GRB.MAXIMIZE,
        )
    elif stage == "lateness":
        model.setObjective(
            gp.quicksum(float(tiers.get(oid, 1.0)) * late[oid] for oid in late),
            GRB.MINIMIZE,
        )
    elif stage == "balance":
        model.setObjective(gp.quicksum(u[pid] for pid in picker_ids), GRB.MINIMIZE)
    else:
        model.setObjective(
            gp.quicksum(x[pair] for pair in penalized_pairs if pair in x),
            GRB.MINIMIZE,
        )

    started = time.perf_counter()
    model.optimize()
    runtime = time.perf_counter() - started
    diagnostics = _gurobi_diagnostics(model, abs_gap, GRB)
    diagnostics["runtime"] = runtime
    if objective_upper is None:
        diagnostics["objective_upper_used"] = None
    else:
        diagnostics["objective_upper_used"] = max(float(objective_upper), 1.0)

    proven = model.Status == GRB.OPTIMAL and model.SolCount > 0
    keep_statuses = {GRB.OPTIMAL, GRB.TIME_LIMIT}
    for name in ("SUBOPTIMAL", "INTERRUPTED", "NODE_LIMIT"):
        value = getattr(GRB, name, None)
        if value is not None:
            keep_statuses.add(value)
    usable = model.SolCount > 0 and model.Status in keep_statuses
    keep = proven or (accept_incumbent and usable)
    if not keep:
        return {
            "status": int(model.Status),
            "is_optimal": False,
            "objective": None,
            "y": {},
            "x": {},
            "workload": {},
            **diagnostics,
        }

    y_values = {oid: float(y[oid].X) for oid in weight}
    stage_value = _gurobi_stage_value(
        stage, y_values, late, u, x, weight, tiers, must_ids, picker_ids, penalized_pairs
    )
    objective_value = float(model.ObjVal) if stage == "weighted" else stage_value
    return {
        "status": int(model.Status),
        "is_optimal": True,
        "objective": objective_value,
        "stage_value": stage_value,
        "y": y_values,
        "x": {pair: float(x[pair].X) for pair in eligible_pairs},
        "workload": {pid: float(workload[pid].X) for pid in picker_ids},
        **diagnostics,
    }


def _gurobi_stage_value(
    stage: str,
    y_values: Dict[str, float],
    late,
    imbalance,
    assign,
    weight: Dict[str, float],
    tiers: Dict[str, float],
    must_ids: Sequence[str],
    picker_ids: Sequence[str],
    penalized_pairs: Sequence,
) -> float:
    """Natural-unit value of this stage, read off the Gurobi solution."""
    if stage == "must_count":
        return float(sum(y_values[oid] for oid in must_ids))
    if stage == "ordinary":
        must_set = set(must_ids)
        return float(sum(weight[oid] * y_values[oid] for oid in weight if oid not in must_set))
    if stage == "lateness":
        return float(sum(float(tiers.get(oid, 1.0)) * float(late[oid].X) for oid in late))
    if stage == "balance":
        return float(sum(float(imbalance[pid].X) for pid in picker_ids))
    if stage == "jack":
        return float(sum(float(assign[pair].X) for pair in penalized_pairs if pair in assign))
    return float(sum(weight[oid] * y_values[oid] for oid in weight))


def _gurobi_diagnostics(model, abs_gap: Optional[float], grb) -> dict:
    """Best bound and gap in the model's own sense."""
    bound = None
    try:
        bound_value = float(model.ObjBound)
    except Exception:
        bound_value = None
    if bound_value is not None and math.isfinite(bound_value):
        bound = bound_value
    rel_gap = None
    abs_reached = None
    if model.SolCount > 0:
        try:
            incumbent = float(model.ObjVal)
            rel_gap = float(model.MIPGap)
            if bound is not None:
                abs_reached = abs(incumbent - bound)
        except Exception:
            rel_gap = None
    return {
        "solver_bound": bound,
        "solver_gap": rel_gap,
        "solver_abs_gap": abs_reached,
        "time_limit_hit": int(model.Status) == int(grb.TIME_LIMIT),
        "mip_rel_gap_used": 0.0,
        "mip_abs_gap_used": None if abs_gap is None else float(abs_gap),
    }


def _is_must_ship_row(row: Any) -> bool:
    """True when the row is flagged must-ship or already carries that flat weight."""
    if _is_truthy_flag(_field(row, "must_ship")):
        return True
    weight = _field(row, "priority_weight")
    if _is_missing(weight):
        return False
    try:
        return float(weight) >= MUST_SHIP_WEIGHT - 1e-6
    except (TypeError, ValueError):
        return False


def _assignment_clock(
    order_rows: Sequence[Any],
) -> tuple:
    """Deadlines in minutes from the shift start, and the lateness tier of each job."""
    from assignment_reports import deadline_minutes

    deadlines: Dict[str, float] = {}
    tiers: Dict[str, float] = {}
    for row in order_rows:
        deadline = deadline_minutes(_field(row, "cutoff_time"))
        if deadline is not None:
            deadlines[row.order_id] = deadline
        tiers[row.order_id] = 100.0 if _is_must_ship_row(row) else 1.0
    return deadlines, tiers


def _picker_blockers(order_row: Any, picker_rows: Sequence[Any]) -> List[str]:
    """One word per picker that cannot take the order: equipment or capacity."""
    blockers = []
    for picker in picker_rows:
        if _eligible(order_row, picker):
            continue
        required = getattr(order_row, "required_equipment", None)
        if not _is_missing(required) and required != picker.equipment_type:
            blockers.append("equipment")
        else:
            blockers.append("capacity")
    return blockers


def _shift_start_minutes(picker) -> float:
    """Minutes from the wave start. A missing clock starts at 0."""
    raw = getattr(picker, "shift_start", None)
    if _is_missing(raw):
        return 0.0
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(value):
        return 0.0
    return value


def _cannot_meet_cutoff(
    job_duration: float,
    deadline: Optional[float],
    picker_ids: Sequence[str],
    shift_starts: Dict[str, float],
    shifts: Dict[str, float],
) -> bool:
    """True when every eligible picker is already too late to start this job.

    The window is ``A_r(T) = min(max(0, T - s_r), shift_r)``, not the raw cutoff.
    A job with no deadline, or with no eligible picker, is left to the model.
    """
    if deadline is None or not picker_ids:
        return False
    from assignment_reports import available_before_cutoff

    for pid in picker_ids:
        window = available_before_cutoff(deadline, shift_starts[pid], shifts[pid])
        if float(job_duration) <= window + 1e-9:
            return False
    return True


def _solve_backend(solver_name: str, **kwargs) -> dict:
    if solver_name == "gurobi":
        return _solve_assignment_gurobi(**kwargs)
    from order_picking_highs import solve_assignment_highs

    return solve_assignment_highs(**kwargs)


def _stage_record(name: str, solved: dict) -> dict:
    from assignment_reports import stop_reason

    incumbent = solved.get("stage_value")
    if incumbent is None:
        incumbent = solved.get("objective")
    time_limit_hit = bool(solved.get("time_limit_hit"))
    return {
        "stage": name,
        "status": solved.get("status"),
        "incumbent": incumbent,
        "bound": solved.get("solver_bound"),
        "abs_gap": solved.get("solver_abs_gap"),
        "rel_gap": solved.get("solver_gap"),
        "runtime": solved.get("runtime"),
        "time_limit_hit": time_limit_hit,
        "mip_rel_gap_used": solved.get("mip_rel_gap_used"),
        "mip_abs_gap_used": solved.get("mip_abs_gap_used"),
        "objective_upper_used": solved.get("objective_upper_used"),
        "stop": stop_reason(
            time_limit_hit,
            bool(solved.get("is_optimal")),
            solved.get("solver_abs_gap"),
            solved.get("mip_abs_gap_used"),
        ),
    }


def _objective_upper(stage: str, common: dict, must_ids: Sequence[str]) -> float:
    """Upper bound on a tight stage objective.

    A relative gap of ``absolute / scale`` stops within ``absolute`` only
    when ``scale`` is at least the objective size. Continuous lateness and
    imbalance can exceed the sum of the absolute coefficients.
    """
    weight = common["weight"]
    must_set = {oid for oid in must_ids if oid in weight}
    deadlines = common.get("deadlines") or {}
    tiers = common.get("lateness_tier") or {}
    shifts = [max(float(value), 0.0) for value in common["picker_shift"].values()]
    big_m = max(shifts) if shifts else 1.0
    if big_m <= 0.0:
        big_m = 1.0
    dated = [oid for oid in weight if oid in deadlines]
    if dated:
        earliest = min(float(deadlines[oid]) for oid in dated)
        big_m_late = big_m + max(0.0, -earliest)
    else:
        big_m_late = big_m
    lateness_upper = sum(float(tiers.get(oid, 1.0)) * big_m_late for oid in dated)
    # Sum of absolute deviations from the mean is at most twice the workloads.
    balance_upper = 2.0 * sum(shifts)
    jack_upper = float(len(common.get("penalized_pairs") or ()))
    release = sum(max(float(weight[oid]), 0.0) for oid in weight)
    ordinary = sum(
        max(float(weight[oid]), 0.0) for oid in weight if oid not in must_set
    )
    if stage == "must_count":
        return max(float(len(must_set)), 1.0)
    if stage == "ordinary":
        return max(ordinary, 1.0)
    if stage == "lateness":
        return max(lateness_upper, 1.0)
    if stage == "balance":
        return max(balance_upper, 1.0)
    if stage == "jack":
        return max(jack_upper, 1.0)
    upper = release
    balance_weight = float(common.get("balance_weight") or 0.0)
    lateness_weight = float(common.get("lateness_weight") or 0.0)
    jack_penalty = float(common.get("jack_penalty") or 0.0)
    if balance_weight:
        upper += abs(balance_weight) * balance_upper
    if lateness_weight:
        upper += abs(lateness_weight) * lateness_upper
    if jack_penalty:
        upper += abs(jack_penalty) * jack_upper
    return max(upper, 1.0)


def _stage_abs_gap(
    stage: str,
    weight: Dict[str, float],
    must_ids: Sequence[str],
    objective_upper: float,
) -> float:
    """Absolute stop for one stage, in that stage's own units."""
    from assignment_reports import reachable_absolute_gap

    if stage == "must_count":
        return _MUST_COUNT_ABS_GAP
    if stage == "ordinary":
        must_set = set(must_ids)
        ordinary = [weight[oid] for oid in weight if oid not in must_set]
        basis = ordinary or list(weight.values())
        return reachable_absolute_gap(basis, objective_upper)
    if stage == "jack":
        return min(_MUST_COUNT_ABS_GAP, reachable_absolute_gap(weight.values(), objective_upper))
    return reachable_absolute_gap(weight.values(), objective_upper)


def _solve_staged(
    solver_name: str,
    common: dict,
    must_ship_ids: Sequence[str],
    deadlines: Dict[str, float],
    balance_weight: float,
    prefer_jack: float,
) -> dict:
    """Release must-ship jobs, then other jobs, then lateness, balance, and jacks.

    Each stage locks its own objective value, not the chosen job set, so a
    later stage may swap equal-value jobs. The lock uses the incumbent, and
    a time-limit stop is recorded on that stage. The returned objective is
    the release-weight sum of the jobs that remain, not the last stage's
    lateness or jack count.
    """
    weight = common["weight"]
    must_ids = [oid for oid in must_ship_ids if oid in weight]
    must_set = set(must_ids)
    locks: Dict[str, float] = {"tolerance": _LOCK_TOLERANCE}
    stages: List[dict] = []
    plan: List[tuple] = []
    if must_ids:
        plan.append(("must_count", "must_count"))
    if any(oid not in must_set for oid in weight):
        plan.append(("ordinary", "ordinary_weight"))
    if any(oid in deadlines for oid in weight):
        plan.append(("lateness", "lateness"))
    if balance_weight > 0.0 and common["picker_ids"]:
        plan.append(("balance", "balance"))
    if prefer_jack > 0.0 and any(pair[0] in weight for pair in common["penalized_pairs"]):
        plan.append(("jack", "jack"))

    solved: dict = {
        "status": 0,
        "is_optimal": True,
        "objective": 0.0,
        "y": {},
        "x": {},
        "workload": {},
        "time_limit_hit": False,
    }
    for stage, lock_key in plan:
        call = dict(common)
        upper = _objective_upper(stage, common, must_ids)
        call.update(
            stage=stage,
            locks=dict(locks),
            must_ship_ids=must_ids,
            abs_gap=_stage_abs_gap(stage, weight, must_ids, upper),
            accept_incumbent=True,
            objective_upper=upper,
        )
        solved = _solve_backend(solver_name, **call)
        stages.append(_stage_record(stage, solved))
        if not solved.get("is_optimal"):
            solved["stages"] = stages
            solved["time_limit_hit"] = any(row["time_limit_hit"] for row in stages)
            return solved
        locks[lock_key] = float(solved["stage_value"])

    y = solved.get("y") or {}
    solved["objective"] = float(
        sum(float(weight[oid]) for oid in weight if float(y.get(oid, 0.0)) > 0.5)
    )
    solved["stages"] = stages
    solved["time_limit_hit"] = any(row["time_limit_hit"] for row in stages)
    return solved


def _attach_solver_log(result: dict, solved: dict, gap_requested: Optional[float]) -> None:
    """Copy bound, gap, and stage rows onto the public result."""
    from assignment_reports import stop_reason

    for key in (
        "solver_bound",
        "solver_gap",
        "solver_abs_gap",
        "time_limit_hit",
        "mip_rel_gap_used",
        "mip_abs_gap_used",
        "objective_upper_used",
        "runtime",
        "stages",
    ):
        if key in solved:
            result[key] = solved[key]
    result["gap_requested"] = gap_requested
    stages = list(result.get("stages") or [])
    if stages:
        if any(row.get("time_limit_hit") for row in stages):
            result["stop"] = "time_limit"
        else:
            result["stop"] = stages[-1].get("stop") or "proven"
        return
    result["stop"] = stop_reason(
        bool(solved.get("time_limit_hit")),
        bool(solved.get("is_optimal")),
        solved.get("solver_abs_gap"),
        solved.get("mip_abs_gap_used"),
    )


def solve_order_assignment(
    orders,
    pickers,
    route_durations: Dict[str, float],
    benchmark_mode: bool = False,
    balance_weight: float = 0.0,
    time_limit: float = 10.0,
    mip_gap: Optional[float] = None,
    now: Any = None,
    wave_hours: float = DEFAULT_WAVE_HOURS,
    tier_multipliers: Optional[Dict[Any, float]] = None,
    assignment_solver: str = "highs",
    lateness_weight: float = 0.0,
    prefer_jack: float = 0.0,
    require_must_ship: bool = False,
    staged_assignment: bool = False,
    withhold_if_late_at_start: bool = False,
) -> dict:
    """Phase 2: assign routed orders to pickers (C1-C5, C8).

    Parameters
    ----------
    orders : rows with order_id, total_weight, total_volume (optional),
        priority_weight (optional; derived when missing), cutoff_time,
        account_tier, wait_time, required_equipment (all optional).
    pickers : rows with picker_id, equipment_type, max_weight, max_volume
        (optional), shift_duration.
    route_durations : mapping order_id -> route duration, from phase 1.
    benchmark_mode : if True, every order's weight is set to 1 (plain count
        maximization), overriding ``priority_weight`` (C8).
    balance_weight : weight on the workload-imbalance penalty term (C5).
        0 disables workload balancing in the objective.
    time_limit : solver time limit in seconds, applied to each stage.
    mip_gap : recorded as ``gap_requested`` and not applied. The stop is an
        absolute tolerance of a tenth of the smallest positive weight, raised
        above solver noise when that still sits below half a job. SciPy only
        accepts a relative gap, so HiGHS sets
        ``mip_rel_gap = absolute / max(objective_upper, 1)``. Gurobi sets
        ``MIPGap = 0`` and ``MIPGapAbs`` to that absolute tolerance, because
        Gurobi stops when either gap is met.
    now, wave_hours, tier_multipliers : passed to ``compute_priority_weight``
        when a row has no explicit finite ``priority_weight``.
    assignment_solver : "highs" (default, open source), "gurobi", or "auto".
        "auto" uses Gurobi when a license checks out and HiGHS otherwise.
        "gurobi" raises if no license is available. The result includes
        ``solver`` set to the backend used.
    lateness_weight : 0 keeps today's objective. A positive value subtracts
        ``lateness_weight * tier * minutes_late``. The tier is 100 for a
        must-ship job and 1 otherwise. Finish times follow earliest-deadline
        order on each picker, starting at minute 0. ``cutoff_time`` is
        minutes from that start. The release weight is unchanged, including
        the flat 100000 must-ship weight.
    prefer_jack : 0 is off. A positive value is subtracted, in the same
        units as ``priority_weight``, for each jack-eligible job given to a
        forklift. Keep it below the smallest job weight so the penalty
        breaks ties toward a jack and does not pay for dropping the job.
    require_must_ship : when True, every must-ship job is released. The
        model can then be infeasible if those jobs cannot all fit the shift.
        This is not the staged solve. An empty plan without this flag stays
        a feasible optimum.
    staged_assignment : when True, release must-ship jobs by count, then
        maximise the weight of the other jobs that fit, then minimise
        lateness, then balance if ``balance_weight`` > 0, then the jack
        penalty if ``prefer_jack`` > 0. Each stage locks its objective
        value, not the job set. The default remains the single weighted
        objective. The public score is the release-weight sum.
    withhold_if_late_at_start : when True, a job whose duration exceeds
        ``A_r(T)`` for every eligible picker is left out before the solve,
        with reason ``cannot_meet_cutoff``. Off by default. Jobs removed
        here are not forced on by ``require_must_ship``.

    Returns
    -------
    dict matching the "Phase 2 Output: Solution Dictionary" in the
    specification, plus ``schedule``, ``late_jobs``, ``late_minutes``,
    and ``drops``.
    """
    orders = compute_priority_weight(
        orders,
        now=now,
        wave_hours=wave_hours,
        tier_multipliers=tier_multipliers,
    )
    order_rows = list(orders.itertuples(index=False))
    picker_rows = list(pickers.itertuples(index=False))
    if len({o.order_id for o in order_rows}) != len(order_rows):
        raise ValueError("order_id values must be unique.")
    missing_routes = [o.order_id for o in order_rows if o.order_id not in route_durations]
    if missing_routes:
        raise ValueError(f"Orders have no route duration: {missing_routes}.")

    if lateness_weight < 0 or not math.isfinite(lateness_weight):
        raise ValueError("lateness_weight must be a finite number >= 0.")
    if prefer_jack < 0 or not math.isfinite(prefer_jack):
        raise ValueError("prefer_jack must be a finite number >= 0.")
    from assignment_reports import reachable_absolute_gap

    full_duration = {o.order_id: float(route_durations[o.order_id]) for o in order_rows}
    full_deadlines, full_tiers = _assignment_clock(order_rows)
    picker_ids = [p.picker_id for p in picker_rows]
    picker_shift = {p.picker_id: float(p.shift_duration) for p in picker_rows}
    shift_starts = {p.picker_id: _shift_start_minutes(p) for p in picker_rows}
    full_pairs = [
        (o.order_id, p.picker_id) for o in order_rows for p in picker_rows if _eligible(o, p)
    ]
    full_eligible: Dict[str, set] = {o.order_id: set() for o in order_rows}
    for oid, pid in full_pairs:
        full_eligible[oid].add(pid)

    withheld = []
    active_rows = list(order_rows)
    if withhold_if_late_at_start:
        active_rows = []
        for row in order_rows:
            if _cannot_meet_cutoff(
                full_duration[row.order_id],
                full_deadlines.get(row.order_id),
                list(full_eligible[row.order_id]),
                shift_starts,
                picker_shift,
            ):
                withheld.append(row)
            else:
                active_rows.append(row)

    weight = {
        o.order_id: (1.0 if benchmark_mode else float(o.priority_weight)) for o in active_rows
    }
    duration = {oid: full_duration[oid] for oid in weight}
    eligible_pairs = [pair for pair in full_pairs if pair[0] in weight]
    eligible_pickers = {oid: set(full_eligible[oid]) for oid in weight}
    deadlines = {oid: full_deadlines[oid] for oid in weight if oid in full_deadlines}
    lateness_tier = {oid: full_tiers[oid] for oid in weight}
    equipment = {p.picker_id: str(p.equipment_type) for p in picker_rows}
    jack_orders = {oid for oid, pid in eligible_pairs if equipment.get(pid) == "pallet_jack"}
    penalized_pairs = [
        (oid, pid)
        for oid, pid in eligible_pairs
        if oid in jack_orders and equipment.get(pid) == "forklift"
    ]
    forced_orders = [
        row.order_id for row in active_rows if require_must_ship and _is_must_ship_row(row)
    ]
    must_ship_ids = [row.order_id for row in active_rows if _is_must_ship_row(row)]
    solver_name = _resolve_assignment_solver(assignment_solver)
    common = dict(
        weight=weight,
        duration=duration,
        eligible_pairs=eligible_pairs,
        eligible_pickers=eligible_pickers,
        picker_ids=picker_ids,
        picker_shift=picker_shift,
        balance_weight=balance_weight,
        time_limit=time_limit,
        mip_gap=mip_gap,
        lateness_weight=lateness_weight,
        deadlines=deadlines,
        lateness_tier=lateness_tier,
        jack_penalty=prefer_jack,
        penalized_pairs=penalized_pairs,
        forced_orders=forced_orders,
    )
    if staged_assignment:
        solved = _solve_staged(
            solver_name,
            common,
            must_ship_ids,
            deadlines,
            balance_weight,
            prefer_jack,
        )
    else:
        upper = _objective_upper("weighted", common, must_ship_ids)
        solved = _solve_backend(
            solver_name,
            **common,
            abs_gap=reachable_absolute_gap(weight.values(), upper),
            must_ship_ids=must_ship_ids,
            objective_upper=upper,
        )
    result = _public_assignment_result(weight, eligible_pickers, picker_ids, solved, solver_name)
    for row in withheld:
        result["orders_not_fulfilled"].append(
            {"order_id": row.order_id, "reason": "cannot_meet_cutoff"}
        )
    _attach_solver_log(result, solved, mip_gap)
    from assignment_reports import drop_detail, late_jobs, schedule_lateness

    schedule = schedule_lateness(result["picker_assignments"], full_duration, full_deadlines)
    result["schedule"] = schedule
    result["late_jobs"] = late_jobs(schedule)
    result["late_minutes"] = sum(float(row["minutes_late"]) for row in result["late_jobs"])
    drops = []
    workload = result.get("picker_workload") or {}
    for row in order_rows:
        missed = next(
            (item for item in result["orders_not_fulfilled"] if item["order_id"] == row.order_id),
            None,
        )
        if missed is None:
            continue
        if missed["reason"] == "cannot_meet_cutoff":
            detail = "cannot_meet_cutoff"
        else:
            eligible = full_eligible[row.order_id]
            detail = drop_detail(
                _picker_blockers(row, picker_rows) if not eligible else [],
                list(eligible),
                full_duration[row.order_id],
                workload,
                picker_shift,
            )
        drops.append({"order_id": row.order_id, "reason": missed["reason"], "detail": detail})
    result["drops"] = drops
    if not result["is_optimal"] and require_must_ship:
        result["infeasible_must_ship"] = True
        result["status_reason"] = "required_must_ship_jobs_cannot_all_fit"
    return result


def format_picking_solution(result: dict) -> str:
    """Formatted solution: fulfilled count, weighted score, assignments, workload spread."""
    fulfilled = list(result.get("orders_fulfilled") or [])
    missed = list(result.get("orders_not_fulfilled") or [])
    total = len(fulfilled) + len(missed)
    score = result.get("objective_value")
    score_text = "not optimal" if score is None else f"{float(score):g}"
    lines = [
        f"Orders fulfilled: {len(fulfilled)} / {total}",
        f"Weighted score: {score_text}",
        "Assignments:",
    ]
    assignments = result.get("picker_assignments") or {}
    if assignments:
        for picker_id, order_ids in assignments.items():
            listed = ", ".join(str(oid) for oid in order_ids) if order_ids else "(none)"
            lines.append(f"  {picker_id}: {listed}")
    else:
        lines.append("  (none)")
    lines.append("Workload:")
    workload = result.get("picker_workload") or {}
    if workload:
        for picker_id, load in workload.items():
            lines.append(f"  {picker_id}: {float(load):g}")
        spread = max(float(v) for v in workload.values()) - min(float(v) for v in workload.values())
        lines.append(f"Workload spread (max-min): {spread:g}")
    else:
        lines.append("  (none)")
        lines.append("Workload spread (max-min): 0")
    if missed:
        lines.append("Not fulfilled:")
        for item in missed:
            lines.append(f"  {item['order_id']}: {item['reason']}")
    if "late_minutes" in result:
        late_count = len(result.get("late_jobs") or [])
        lines.append(
            f"Late jobs: {late_count}. Late minutes: {float(result['late_minutes']):.3f}."
        )
    if result.get("infeasible_must_ship"):
        lines.append("Required must-ship jobs cannot all fit.")
    if any(key in result for key in ("solver_bound", "solver_gap", "solver_abs_gap")):
        lines.append(
            "Solver status: {status}. Bound: {bound}. Relative gap: {gap}. "
            "Absolute gap: {abs_gap}.".format(
                status=result.get("status"),
                bound=_format_measure(result.get("solver_bound")),
                gap=_format_measure(result.get("solver_gap")),
                abs_gap=_format_measure(result.get("solver_abs_gap")),
            )
        )
    if result.get("stop"):
        lines.append(f"Stop: {result['stop']}.")
    for stage in result.get("stages") or []:
        limit = "yes" if stage.get("time_limit_hit") else "no"
        lines.append(
            "Stage {name}: status {status}, stop {stop}, incumbent {incumbent}, "
            "bound {bound}, absolute gap {abs_gap}, relative gap {rel_gap}, "
            "runtime {runtime}s, time limit {limit}.".format(
                name=stage.get("stage"),
                status=stage.get("status"),
                stop=stage.get("stop", "n/a"),
                incumbent=_format_measure(stage.get("incumbent")),
                bound=_format_measure(stage.get("bound")),
                abs_gap=_format_measure(stage.get("abs_gap")),
                rel_gap=_format_measure(stage.get("rel_gap")),
                runtime=_format_measure(stage.get("runtime")),
                limit=limit,
            )
        )
    return "\n".join(lines)


def _format_measure(value: Any) -> str:
    if value is None:
        return "n/a"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if not math.isfinite(number):
        return "n/a"
    return f"{number:.6g}"


# ---------------------------------------------------------------------------
# End-to-end pipeline
# ---------------------------------------------------------------------------

def solve_order_picking(
    orders,
    order_items,
    pickers,
    distance_matrix,
    depot_id: str,
    phase1_solver: str = "heuristic",
    phase1_time_limit: Optional[float] = None,
    benchmark_mode: bool = False,
    balance_weight: float = 0.0,
    phase2_time_limit: float = 10.0,
    phase2_mip_gap: Optional[float] = None,
    now: Any = None,
    wave_hours: float = DEFAULT_WAVE_HOURS,
    tier_multipliers: Optional[Dict[Any, float]] = None,
    verbose: bool = False,
    assignment_solver: str = "highs",
    lateness_weight: float = 0.0,
    prefer_jack: float = 0.0,
    require_must_ship: bool = False,
    staged_assignment: bool = False,
    withhold_if_late_at_start: bool = False,
) -> dict:
    """Run phase 1 (routing) for every order, then phase 2 (assignment) over
    the resulting route durations. Returns the phase 2 solution dictionary
    with an added ``routes`` key holding the phase 1 output per order.

    ``verbose`` prints :func:`format_picking_solution`.
    """
    order_ids = [row.order_id for row in orders.itertuples(index=False)]
    routes = route_orders(order_items, depot_id, distance_matrix, phase1_solver, phase1_time_limit)
    missing = [oid for oid in order_ids if oid not in routes]
    if missing:
        raise ValueError(f"Orders have no pick lines: {missing}.")
    route_durations = {oid: routes[oid]["route_duration"] for oid in order_ids}
    result = solve_order_assignment(
        orders,
        pickers,
        route_durations,
        benchmark_mode,
        balance_weight,
        phase2_time_limit,
        phase2_mip_gap,
        now=now,
        wave_hours=wave_hours,
        tier_multipliers=tier_multipliers,
        assignment_solver=assignment_solver,
        lateness_weight=lateness_weight,
        prefer_jack=prefer_jack,
        require_must_ship=require_must_ship,
        staged_assignment=staged_assignment,
        withhold_if_late_at_start=withhold_if_late_at_start,
    )
    result["routes"] = routes
    if verbose:
        print(format_picking_solution(result))
    return result


def assignments_frame(result: dict) -> Any:
    """Rows ``picker_id, order_id, duration`` in ``picker_assignments`` list order.

    That order is the order the assignment solver returned the jobs. It is
    not a sequenced start time. The Gantt chart reads this shape.
    """
    import pandas as pd

    rows = []
    routes = result.get("routes") or {}
    for picker_id, order_ids in (result.get("picker_assignments") or {}).items():
        for order_id in order_ids:
            rows.append(
                {
                    "picker_id": picker_id,
                    "order_id": order_id,
                    "duration": float(routes[order_id]["route_duration"]),
                }
            )
    return pd.DataFrame(rows, columns=["picker_id", "order_id", "duration"])


def main(argv: Optional[Sequence[str]] = None) -> None:
    """Route the orders in the CSVs and assign them to pickers.

    Run from ``order_picking/`` as ``python source/order_picking_optimizer.py``.
    ``--assignments-out`` writes the Gantt CSV.
    """
    parser = argparse.ArgumentParser(
        description="Route each order from one depot, then assign orders to pickers."
    )
    parser.add_argument("--orders", required=True, type=Path, help="order_id, total_weight, and optional priority columns.")
    parser.add_argument("--items", required=True, type=Path, help="order_id, item_id, location_id, optional pick_time.")
    parser.add_argument("--pickers", required=True, type=Path, help="picker_id, equipment_type, max_weight, shift_duration.")
    parser.add_argument("--travel", required=True, type=Path, help="from_location, to_location, travel_time.")
    parser.add_argument("--depot", required=True, help="Depot id already present in the travel matrix.")
    parser.add_argument("--phase1", default="heuristic", choices=("heuristic", "scip", "highs"))
    parser.add_argument("--assignment", default="highs", choices=("highs", "gurobi", "auto"))
    parser.add_argument("--balance-weight", type=float, default=0.0)
    parser.add_argument(
        "--share-shift",
        action="store_true",
        help=(
            "Spread picking minutes across pickers "
            f"(balance weight {SHARE_SHIFT_BALANCE}). "
            "An ordinary order is still worth more than the unevenness this removes, "
            "so an order that fits is not dropped to make the shift look even."
        ),
    )
    parser.add_argument("--benchmark", action="store_true", help="Force every order weight to 1.")
    parser.add_argument(
        "--lateness-weight",
        type=float,
        default=0.0,
        help=(
            "Subtract this times a tier times minutes late. 0 keeps today's objective. "
            "The tier is 100 for a must-ship job and 1 otherwise. "
            "cutoff_time is minutes from the start of the shift."
        ),
    )
    parser.add_argument(
        "--prefer-jack",
        type=float,
        default=0.0,
        help=(
            "Penalty, in priority-weight units, for giving a jack-eligible job to a forklift. "
            "0 is off. Use a value below the smallest job weight so the job is not dropped."
        ),
    )
    parser.add_argument(
        "--require-must-ship",
        action="store_true",
        help="Release every must-ship job. The model is infeasible when those jobs cannot all fit.",
    )
    parser.add_argument(
        "--staged-assignment",
        action="store_true",
        help=(
            "Release must-ship jobs by count, then other jobs by weight, "
            "then minimise lateness. Balance and the jack penalty run only "
            "when those weights are positive. The default is one weighted objective."
        ),
    )
    parser.add_argument(
        "--withhold-if-late-at-start",
        action="store_true",
        help=(
            "Leave out a job whose duration exceeds the time every eligible "
            "picker can work before its cutoff. Reason: cannot_meet_cutoff."
        ),
    )
    parser.add_argument("--assignments-out", type=Path, default=None, help="Write picker_id, order_id, duration.")
    parser.add_argument(
        "--report-dir",
        type=Path,
        default=None,
        help="Write utilisation.csv, dropped.csv, and late_jobs.csv.",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    import pandas as pd

    result = solve_order_picking(
        pd.read_csv(args.orders),
        pd.read_csv(args.items),
        pd.read_csv(args.pickers),
        pd.read_csv(args.travel),
        args.depot,
        phase1_solver=args.phase1,
        benchmark_mode=args.benchmark,
        balance_weight=(
            SHARE_SHIFT_BALANCE
            if args.share_shift and args.balance_weight == 0.0
            else args.balance_weight
        ),
        assignment_solver=args.assignment,
        lateness_weight=args.lateness_weight,
        prefer_jack=args.prefer_jack,
        require_must_ship=args.require_must_ship,
        staged_assignment=args.staged_assignment,
        withhold_if_late_at_start=args.withhold_if_late_at_start,
        verbose=True,
    )
    if args.report_dir is not None:
        from assignment_reports import format_assignment_report, utilisation_rows, write_assignment_reports

        utilisation = utilisation_rows(pd.read_csv(args.pickers), result.get("picker_workload") or {})
        write_assignment_reports(
            args.report_dir,
            utilisation,
            result.get("drops") or [],
            result.get("schedule") or [],
            result.get("stages") or None,
        )
        print(format_assignment_report(utilisation, result.get("drops") or [], result.get("schedule") or []))
        print(f"Wrote {args.report_dir}")
    if args.assignments_out is not None:
        frame = assignments_frame(result)
        args.assignments_out.parent.mkdir(parents=True, exist_ok=True)
        frame.to_csv(args.assignments_out, index=False)
        print(f"Wrote {args.assignments_out}")


if __name__ == "__main__":
    main()
