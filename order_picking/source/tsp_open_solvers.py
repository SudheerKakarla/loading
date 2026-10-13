#!/usr/bin/env python3
"""
Traveling Salesman: Python port of the AIMMS 'TravelingSalesman' example
using the open-source solvers HiGHS (highspy) and SCIP (pyscipopt).

What is ported (names follow the AIMMS model):
  * Data       : cities with latitude/longitude (CSV, or the AIMMS DEX JSON layouts:
                 Dense / Sparse / RowOriented / ColumnOriented), optional filters that
                 mirror pr_addByCountry and pr_addByLimit, built-in sample cities.
  * Distance   : p_def_haversineDistance (spherical law of cosines, R = 6371 km).
  * Neighbors  : s_neighbors = NBest(...) with p_def_maxNeighbors = floor(sqrt(p_maxCities)).
  * Heuristics : pr_findInitialTour        nearest-neighbor tour from the first city
                 pr_improveTourCyclic      2-opt, walks the tour (next arc <-> best neighbor arc)
                 pr_improveTourSimultaneous 2-opt, applies the globally best swap each iteration
                 iteration limit = floor(card(s_nodes)/3), same 'useless loop' stopping rule.
  * Exact      : mp_TSP, one binary variable per undirected arc, degree-2 constraints,
                 lazy subtour elimination  sum(arcs leaving S) >= 2.
                   - SCIP and HiGHS: solve / find subtours / add cuts / re-solve until one tour
                     (the AIMMS model uses a solver callback; see notes in each function).
  * Lower bound: pr_solveRelaxedModel as written in the AIMMS model: the same MIP solved
                 WITHOUT the subtour constraint (so a valid lower bound).
  * Timing     : per-method elapsed seconds (stopwatch library equivalent).

Not ported: the AIMMS WebUI pages, AIMMS PRO delegation and the PositionStack API calls.

Differences to be aware of:
  * AIMMS keeps only cities whose latitude AND longitude are non-zero (numeric 'and');
    here a city is kept when both are present and finite.
  * LoopCount in the simultaneous heuristic is implemented as an iteration counter from 1.
  * Ties in argmax/argmin pick the first candidate in input order.
"""
import argparse
import csv
import itertools
import json
import math
import random
import sys
import time

import numpy as np

EARTH_RADIUS_KM = 6371.0


# ----------------------------------------------------------------------------
# Data
# ----------------------------------------------------------------------------
SAMPLE_CITIES = [
    ("Amsterdam", 52.3676, 4.9041), ("Athens", 37.9838, 23.7275),
    ("Berlin", 52.5200, 13.4050), ("Brussels", 50.8503, 4.3517),
    ("Budapest", 47.4979, 19.0402), ("Copenhagen", 55.6761, 12.5683),
    ("Dublin", 53.3498, -6.2603), ("Helsinki", 60.1699, 24.9384),
    ("Istanbul", 41.0082, 28.9784), ("Lisbon", 38.7223, -9.1393),
    ("London", 51.5072, -0.1276), ("Madrid", 40.4168, -3.7038),
    ("Moscow", 55.7558, 37.6173), ("Oslo", 59.9139, 10.7522),
    ("Paris", 48.8566, 2.3522), ("Prague", 50.0755, 14.4378),
    ("Rome", 41.9028, 12.4964), ("Stockholm", 59.3293, 18.0686),
    ("Vienna", 48.2082, 16.3738), ("Warsaw", 52.2297, 21.0122),
    ("Zurich", 47.3769, 8.5417), ("Barcelona", 41.3874, 2.1686),
    ("Munich", 48.1351, 11.5820), ("Milan", 45.4642, 9.1900),
    ("Hamburg", 53.5511, 9.9937),
]


def _first(row, keys):
    for k in keys:
        if k in row and row[k] not in (None, ""):
            return row[k]
    return None


def _to_float(v):
    try:
        f = float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def _rows_from_json(doc):
    """Return a list of (name, lat, lon) from any of the four AIMMS DEX layouts."""
    tbl = doc["datasets"]["LatLongDataset"]["tables"]["LatLong"]
    if "columns" in tbl:                                   # ColumnOriented
        c = tbl["columns"]
        return list(zip(c["name"], c["latitude"], c["longitude"]))
    rows = tbl["rows"]
    if isinstance(rows, dict):                             # RowOriented: meta + data
        meta = [m.lower() for m in rows.get("meta", ["name", "latitude", "longitude"])]
        idx = {m: i for i, m in enumerate(meta)}
        return [(r[idx["name"]], r[idx["latitude"]], r[idx["longitude"]]) for r in rows["data"]]
    return [(r["name"], r["latitude"], r["longitude"]) for r in rows]  # Dense / Sparse


def load_cities(path, country=None, limit=None, seed=0):
    """Load cities; optionally filter by country (CSV only) and sample 'limit' at random."""
    if path is None:
        raw = list(SAMPLE_CITIES)
    elif path.lower().endswith(".json"):
        with open(path, encoding="utf-8") as fh:
            raw = _rows_from_json(json.load(fh))
    else:
        raw = []
        with open(path, newline="", encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh):
                row = {k.lower().strip(): v for k, v in row.items()}
                if country and (_first(row, ["country", "iso3", "iso2"]) or "").lower() != country.lower():
                    continue
                raw.append((_first(row, ["name", "city", "city_ascii"]),
                            _first(row, ["latitude", "lat"]),
                            _first(row, ["longitude", "lng", "lon", "long"])))
    cities = []
    seen = set()
    for name, lat, lon in raw:
        lat, lon = _to_float(lat), _to_float(lon)
        if name is None or lat is None or lon is None:
            continue
        key = str(name)
        if key in seen:                                    # set elements must be unique
            continue
        seen.add(key)
        cities.append((key, lat, lon))
    if limit and len(cities) > limit:                      # pr_addByLimit
        rnd = random.Random(seed)
        cities = rnd.sample(cities, limit)
    if len(cities) < 3:
        sys.exit("Need at least 3 cities with coordinates.")
    return cities


class Problem:
    def __init__(self, cities, max_cities=100):
        self.names = [c[0] for c in cities]
        self.lat = np.array([c[1] for c in cities])
        self.lon = np.array([c[2] for c in cities])
        self.n = len(cities)
        self.D = self._haversine_matrix()
        # p_def_maxNeighbors = floor(sqrt(p_maxCities)); s_neighbors = NBest(...)
        k = min(int(math.floor(math.sqrt(max_cities))), self.n - 1)
        self.k = k
        self.neighbors = []
        for i in range(self.n):
            order = sorted((j for j in range(self.n) if j != i), key=lambda j: (self.D[i, j], j))
            self.neighbors.append(order[:k])
        self.iter_limit = self.n // 3                      # p_def_iterationLimit

    def _haversine_matrix(self):
        a = np.radians(90.0 - self.lat)
        lon = self.lon
        cosd = (np.cos(a)[:, None] * np.cos(a)[None, :]
                + np.sin(a)[:, None] * np.sin(a)[None, :]
                * np.cos(np.radians(lon[:, None] - lon[None, :])))
        D = EARTH_RADIUS_KM * np.arccos(np.clip(cosd, -1.0, 1.0))
        np.fill_diagonal(D, 0.0)
        return D

    def tour_distance(self, nxt):
        return float(sum(self.D[i, nxt[i]] for i in range(self.n)))


def tour_order(nxt, start=0):
    order, c = [start], nxt[start]
    while c != start:
        order.append(c)
        c = nxt[c]
    return order


def is_single_tour(nxt):
    """True when ``nxt`` is one cycle through every node and the walk from 0 returns to 0."""
    n = len(nxt)
    seen = set()
    node = 0
    for _ in range(n):
        if isinstance(node, bool) or not isinstance(node, (int, np.integer)):
            return False
        node = int(node)
        if node < 0 or node >= n or node in seen:
            return False
        seen.add(node)
        node = nxt[node]
    return node == 0 and len(seen) == n


# ----------------------------------------------------------------------------
# Heuristics
# ----------------------------------------------------------------------------
def find_initial_tour(P):
    """pr_findInitialTour: start at the first city, always go to the closest unvisited city."""
    t0 = time.perf_counter()
    nxt, prv = [None] * P.n, [None] * P.n
    remaining = set(range(1, P.n))
    node = 0
    while remaining:
        best = min(sorted(remaining), key=lambda j: P.D[node, j])
        nxt[node] = best
        node = best
        remaining.discard(node)
    nxt[node] = 0
    for i in range(P.n):
        prv[nxt[i]] = i
    return {"nxt": nxt, "prv": prv, "distance": P.tour_distance(nxt),
            "swaps": 0, "time": time.perf_counter() - t0}


def _forward_segment(nxt, a, c):
    """Nodes from ``next(a)`` through ``c`` in the current tour.

    ``None`` when ``c`` is not strictly ahead of ``a`` within one cycle.
    A one-node segment means the move reverses nothing.
    """
    n = len(nxt)
    if isinstance(a, bool) or isinstance(c, bool):
        return None
    if not isinstance(a, (int, np.integer)) or not isinstance(c, (int, np.integer)):
        return None
    a, c = int(a), int(c)
    if a == c or a < 0 or c < 0 or a >= n or c >= n:
        return None
    node = nxt[a]
    segment = []
    seen = set()
    for _ in range(n):
        if isinstance(node, bool) or not isinstance(node, (int, np.integer)):
            return None
        node = int(node)
        if node < 0 or node >= n or node in seen or node == a:
            return None
        segment.append(node)
        if node == c:
            return segment
        seen.add(node)
        node = nxt[node]
    return None


def _distance_change(P, nxt, i, j):
    """Directed gain from reversing the chain ``next(i)`` .. ``j``.

    The move deletes the walk into that chain and along it, and adds the
    reversed walk. On a symmetric matrix the reversed edges cost the same
    as the forward edges, so this reduces to the four endpoint terms.
    A one-node chain reverses nothing and the gain is 0.
    """
    segment = _forward_segment(nxt, i, j)
    if not segment:
        return 0.0
    distance = P.D
    first = segment[0]
    last = segment[-1]
    after = nxt[last]
    gain = distance[i, first] + distance[last, after] - distance[i, last] - distance[first, after]
    for left, right in zip(segment, segment[1:]):
        gain += distance[left, right] - distance[right, left]
    return gain


def _apply_two_opt(nxt, prv, a, c):
    """Literal port of the AIMMS 'swap cities in tour' block (reverses the chain next(a)..c)."""
    o = nxt[a]
    d = nxt[c]
    nxt[o] = d
    prv[d] = o
    o = c
    dd = prv[o]
    while dd != a:
        od = dd
        nxt[o] = od
        dd = prv[od]
        prv[od] = o
        o = od
    nxt[a] = c
    prv[c] = a


def improve_tour_cyclic(P, init):
    """pr_improveTourCyclic: walk the tour and swap the next arc with its best neighbor arc."""
    t0 = time.perf_counter()
    nxt, prv = list(init["nxt"]), list(init["prv"])
    swaps, useless, node = 0, 0, 0
    while swaps < P.iter_limit and useless <= P.n:
        useless += 1
        cand = max(P.neighbors[node], key=lambda j: _distance_change(P, nxt, node, j))
        if _distance_change(P, nxt, node, cand) > 0:
            swaps += 1
            useless = 0
            _apply_two_opt(nxt, prv, node, cand)
        node = nxt[node]
    return {"nxt": nxt, "prv": prv, "distance": P.tour_distance(nxt),
            "swaps": swaps, "time": time.perf_counter() - t0}


def improve_tour_simultaneous(P, init):
    """pr_improveTourSimultaneous: apply the globally best neighbor swap each iteration."""
    t0 = time.perf_counter()
    nxt, prv = list(init["nxt"]), list(init["prv"])
    swaps, loop = 0, 1

    def best_pair():
        best, arg = -math.inf, None
        for i in range(P.n):
            for j in P.neighbors[i]:
                ch = _distance_change(P, nxt, i, j)
                if ch > best:
                    best, arg = ch, (i, j)
        return best, arg

    best, arg = best_pair()
    while best > 0 and loop <= P.iter_limit:
        swaps += 1
        _apply_two_opt(nxt, prv, *arg)
        loop += 1
        best, arg = best_pair()
    return {"nxt": nxt, "prv": prv, "distance": P.tour_distance(nxt),
            "swaps": swaps, "time": time.perf_counter() - t0}


# ----------------------------------------------------------------------------
# Exact model (shared helpers)
# ----------------------------------------------------------------------------
def arcs_of(n):
    """v_networkTour is declared for i_node1 > i_node2 only."""
    return [(i, j) for i in range(n) for j in range(i)]


def find_subtours(n, chosen):
    """pr_determineSubtours: split the chosen arcs into connected cycles."""
    adj = {i: [] for i in range(n)}
    for i, j in chosen:
        adj[i].append(j)
        adj[j].append(i)
    seen, comps = set(), []
    for s in range(n):
        if s in seen:
            continue
        comp, stack = [], [s]
        seen.add(s)
        while stack:
            u = stack.pop()
            comp.append(u)
            for v in adj[u]:
                if v not in seen:
                    seen.add(v)
                    stack.append(v)
        comps.append(sorted(comp))
    return comps


def cut_arcs(n, S):
    """Arcs with exactly one end in S (sum of these >= 2 is the subtour cut)."""
    inS = set(S)
    return [(i, j) for (i, j) in arcs_of(n) if (i in inS) != (j in inS)]


def arcs_to_next(n, chosen):
    """Convert undirected arcs of a single tour into a 'next city' array starting at city 0."""
    adj = {i: [] for i in range(n)}
    for i, j in chosen:
        adj[i].append(j)
        adj[j].append(i)
    nxt, prev, cur = [None] * n, None, 0
    for _ in range(n):
        cands = [v for v in adj[cur] if v != prev] or adj[cur]
        step = cands[0]
        nxt[cur] = step
        prev, cur = cur, step
    return nxt


# ----------------------------------------------------------------------------
# SCIP
# ----------------------------------------------------------------------------
def solve_scip(P, time_limit=None, relaxed=False, verbose=False, max_rounds=10000):
    """Exact TSP with SCIP: solve -> find subtours -> add cuts -> re-solve, until one tour.

    relaxed=True skips subtour elimination (the AIMMS lower bound).
    (A native lazy-constraint handler was tried first; in my tests with pyscipopt 6.2.1 /
    SCIP 10 it accepted solutions that still had subtours, so the verified cut loop is used.)
    """
    from pyscipopt import Model, quicksum

    n, t0 = P.n, time.perf_counter()
    m = Model("TSP")
    if not verbose:
        m.hideOutput()
    x = {a: m.addVar(vtype="B", obj=float(P.D[a]), name=f"x_{a[0]}_{a[1]}") for a in arcs_of(n)}

    def arc(i, j):
        return x[(i, j)] if i > j else x[(j, i)]

    for k in range(n):                                     # c_degreeCheck
        m.addCons(quicksum(arc(k, j) for j in range(n) if j != k) == 2, name=f"deg_{k}")

    cuts, rounds, status, obj, bound, chosen = 0, 0, "unknown", None, None, None
    while rounds < max_rounds:
        rounds += 1
        if time_limit:
            m.setParam("limits/time", max(1.0, float(time_limit) - (time.perf_counter() - t0)))
        m.optimize()
        status = m.getStatus()
        if status != "optimal" or m.getNSols() == 0:
            break
        obj, bound = m.getObjVal(), m.getDualbound()
        chosen = [a for a in x if m.getVal(x[a]) > 0.5]
        comps = find_subtours(n, chosen)
        if relaxed or len(comps) == 1:
            break
        m.freeTransform()                                  # allow adding constraints
        for S in comps:                                    # lazy cuts, one per subtour
            m.addCons(quicksum(x[a] for a in cut_arcs(n, S)) >= 2)
            cuts += 1
    out = {"solver": "SCIP", "status": status, "time": time.perf_counter() - t0,
           "cuts": cuts, "rounds": rounds}
    if obj is not None:
        out.update(objective=obj, bound=bound, chosen=chosen)
    return out


# ----------------------------------------------------------------------------
# HiGHS
# ----------------------------------------------------------------------------
def solve_highs(P, time_limit=None, relaxed=False, verbose=False, max_rounds=10000):
    """Exact TSP with HiGHS: iterate solve -> find subtours -> add cuts, until one tour."""
    import highspy

    n, t0 = P.n, time.perf_counter()
    arcs = arcs_of(n)
    index = {a: c for c, a in enumerate(arcs)}
    h = highspy.Highs()
    h.setOptionValue("output_flag", bool(verbose))
    if time_limit:
        h.setOptionValue("time_limit", float(time_limit))
    inf = highspy.kHighsInf
    for a in arcs:
        h.addVar(0.0, 1.0)
    h.changeColsCost(len(arcs), np.arange(len(arcs), dtype=np.int32),
                     np.array([P.D[a] for a in arcs], dtype=float))
    h.changeColsIntegrality(len(arcs), np.arange(len(arcs), dtype=np.int32),
                            np.array([highspy.HighsVarType.kInteger] * len(arcs)))
    for k in range(n):                                     # c_degreeCheck
        idx = np.array([index[(k, j) if k > j else (j, k)] for j in range(n) if j != k], dtype=np.int32)
        h.addRow(2.0, 2.0, len(idx), idx, np.ones(len(idx)))
    h.changeObjectiveSense(highspy.ObjSense.kMinimize)

    cuts, rounds, status, obj, bound, chosen = 0, 0, "unknown", None, None, None
    while rounds < max_rounds:
        rounds += 1
        if time_limit:
            h.setOptionValue("time_limit", max(1.0, float(time_limit) - (time.perf_counter() - t0)))
        h.run()
        mstat = h.getModelStatus()
        status = h.modelStatusToString(mstat)
        info = h.getInfo()
        if mstat not in (highspy.HighsModelStatus.kOptimal,):
            break
        sol = h.getSolution().col_value
        obj, bound = info.objective_function_value, info.mip_dual_bound
        chosen = [a for a, v in zip(arcs, sol) if v > 0.5]
        comps = find_subtours(n, chosen)
        if relaxed or len(comps) == 1:
            break
        for S in comps:                                    # lazy cuts, one per subtour
            idx = np.array([index[a] for a in cut_arcs(n, S)], dtype=np.int32)
            h.addRow(2.0, inf, len(idx), idx, np.ones(len(idx)))
            cuts += 1
    out = {"solver": "HiGHS", "status": status, "time": time.perf_counter() - t0,
           "cuts": cuts, "rounds": rounds}
    if obj is not None:
        out.update(objective=obj, bound=bound, chosen=chosen)
    return out


# ----------------------------------------------------------------------------
# Directed exact tour (warehouse one-way matrix)
# The city demo above stays undirected. A one-way aisle needs an arc in each
# direction, with the duration equal to the directed walk.
# ----------------------------------------------------------------------------
def directed_arcs(n):
    """One binary variable for every ordered pair i -> j, i != j."""
    return [(i, j) for i in range(n) for j in range(n) if i != j]


def successors_of(n, chosen):
    """Map each node to its unique chosen successor, or None if that map is not a function."""
    nxt = [None] * n
    for i, j in chosen:
        if i < 0 or j < 0 or i >= n or j >= n or i == j or nxt[i] is not None:
            return None
        nxt[i] = j
    if any(item is None for item in nxt):
        return None
    return nxt


def directed_cycles(nxt):
    """Node lists, one per directed cycle in a complete successor map."""
    n = len(nxt)
    seen = [False] * n
    cycles = []
    for start in range(n):
        if seen[start]:
            continue
        path_index = {}
        order = []
        node = start
        while node is not None and node not in path_index and not seen[node]:
            path_index[node] = len(order)
            order.append(node)
            node = nxt[node]
        if node is not None and node in path_index:
            cycle = order[path_index[node]:]
            cycles.append(cycle)
            for item in cycle:
                seen[item] = True
        for item in order:
            seen[item] = True
    return cycles


def leaving_arcs(n, subset):
    """Directed arcs with the tail in ``subset`` and the head outside it."""
    inside = set(subset)
    if not inside or len(inside) >= n:
        return []
    return [(i, j) for i in inside for j in range(n) if j not in inside]


def _directed_result(solver, status, t0, cuts, rounds, objective, bound, chosen, nxt):
    out = {"solver": solver, "status": status, "time": time.perf_counter() - t0,
           "cuts": cuts, "rounds": rounds}
    if objective is not None:
        out.update(objective=objective, bound=bound, chosen=chosen, nxt=nxt)
    return out


def solve_directed_scip(P, time_limit=None, verbose=False, max_rounds=10000):
    """Exact directed tour with SCIP.

    One arc leaves each node and one arc enters each node. A component that
    is not the whole node set gets a cut: at least one chosen arc must leave it.
    """
    from pyscipopt import Model, quicksum

    n, t0 = P.n, time.perf_counter()
    arcs = directed_arcs(n)
    m = Model("ATSP")
    if not verbose:
        m.hideOutput()
    x = {(i, j): m.addVar(vtype="B", obj=float(P.D[i, j]), name=f"x_{i}_{j}") for i, j in arcs}
    for k in range(n):
        m.addCons(quicksum(x[k, j] for j in range(n) if j != k) == 1, name=f"out_{k}")
        m.addCons(quicksum(x[i, k] for i in range(n) if i != k) == 1, name=f"in_{k}")

    cuts, rounds, status = 0, 0, "unknown"
    objective, bound, chosen, nxt = None, None, None, None
    while rounds < max_rounds:
        rounds += 1
        if time_limit:
            m.setParam("limits/time", max(1.0, float(time_limit) - (time.perf_counter() - t0)))
        m.optimize()
        status = m.getStatus()
        if status != "optimal" or m.getNSols() == 0:
            break
        objective, bound = m.getObjVal(), m.getDualbound()
        chosen = [arc for arc in arcs if m.getVal(x[arc]) > 0.5]
        nxt = successors_of(n, chosen)
        if nxt is None:
            break
        cycles = directed_cycles(nxt)
        if len(cycles) == 1 and len(cycles[0]) == n:
            break
        m.freeTransform()
        added = 0
        for subset in cycles:
            leaving = leaving_arcs(n, subset)
            if not leaving:
                continue
            m.addCons(quicksum(x[arc] for arc in leaving) >= 1)
            added += 1
        cuts += added
        if added == 0:
            break
    return _directed_result("SCIP", status, t0, cuts, rounds, objective, bound, chosen, nxt)


def solve_directed_highs(P, time_limit=None, verbose=False, max_rounds=10000):
    """Exact directed tour with HiGHS. Same in-degree, out-degree, and leave-set cuts as SCIP."""
    import highspy

    n, t0 = P.n, time.perf_counter()
    arcs = directed_arcs(n)
    index = {arc: col for col, arc in enumerate(arcs)}
    h = highspy.Highs()
    h.setOptionValue("output_flag", bool(verbose))
    if time_limit:
        h.setOptionValue("time_limit", float(time_limit))
    inf = highspy.kHighsInf
    for _arc in arcs:
        h.addVar(0.0, 1.0)
    h.changeColsCost(len(arcs), np.arange(len(arcs), dtype=np.int32),
                     np.array([P.D[i, j] for i, j in arcs], dtype=float))
    h.changeColsIntegrality(len(arcs), np.arange(len(arcs), dtype=np.int32),
                            np.array([highspy.HighsVarType.kInteger] * len(arcs)))
    for k in range(n):
        out_idx = np.array([index[(k, j)] for j in range(n) if j != k], dtype=np.int32)
        in_idx = np.array([index[(i, k)] for i in range(n) if i != k], dtype=np.int32)
        h.addRow(1.0, 1.0, len(out_idx), out_idx, np.ones(len(out_idx)))
        h.addRow(1.0, 1.0, len(in_idx), in_idx, np.ones(len(in_idx)))
    h.changeObjectiveSense(highspy.ObjSense.kMinimize)

    cuts, rounds, status = 0, 0, "unknown"
    objective, bound, chosen, nxt = None, None, None, None
    while rounds < max_rounds:
        rounds += 1
        if time_limit:
            h.setOptionValue("time_limit", max(1.0, float(time_limit) - (time.perf_counter() - t0)))
        h.run()
        model_status = h.getModelStatus()
        status = h.modelStatusToString(model_status)
        info = h.getInfo()
        if model_status != highspy.HighsModelStatus.kOptimal:
            break
        solution = h.getSolution().col_value
        objective, bound = info.objective_function_value, info.mip_dual_bound
        chosen = [arc for arc, value in zip(arcs, solution) if value > 0.5]
        nxt = successors_of(n, chosen)
        if nxt is None:
            break
        cycles = directed_cycles(nxt)
        if len(cycles) == 1 and len(cycles[0]) == n:
            break
        added = 0
        for subset in cycles:
            leaving = leaving_arcs(n, subset)
            if not leaving:
                continue
            cols = np.array([index[arc] for arc in leaving], dtype=np.int32)
            h.addRow(1.0, inf, len(cols), cols, np.ones(len(cols)))
            added += 1
        cuts += added
        if added == 0:
            break
    return _directed_result("HiGHS", status, t0, cuts, rounds, objective, bound, chosen, nxt)


SOLVERS = {"scip": solve_scip, "highs": solve_highs}
DIRECTED_SOLVERS = {"scip": solve_directed_scip, "highs": solve_directed_highs}


# ----------------------------------------------------------------------------
# Reporting / plotting
# ----------------------------------------------------------------------------
def plot_tours(P, tours, path):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib not installed; skipping plot.")
        return
    cols = 2 if len(tours) > 1 else 1
    rows = math.ceil(len(tours) / cols)
    fig, axes = plt.subplots(rows, cols, figsize=(7 * cols, 5.5 * rows), squeeze=False)
    for ax, (title, order) in zip(axes.ravel(), tours):
        xs = [P.lon[i] for i in order + [order[0]]]
        ys = [P.lat[i] for i in order + [order[0]]]
        ax.plot(xs, ys, "-", lw=1.2, color="#1f6f8b")
        ax.scatter(P.lon, P.lat, s=18, color="#d1495b", zorder=3)
        if P.n <= 40:
            for i, nm in enumerate(P.names):
                ax.annotate(nm, (P.lon[i], P.lat[i]), fontsize=6, xytext=(2, 2), textcoords="offset points")
        ax.set_title(title, fontsize=10)
        ax.set_xlabel("longitude")
        ax.set_ylabel("latitude")
    for ax in axes.ravel()[len(tours):]:
        ax.axis("off")
    fig.tight_layout()
    fig.savefig(path, dpi=140)
    print(f"Plot saved to {path}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", help="CSV (name,latitude,longitude[,country]) or AIMMS DEX JSON; default: built-in sample")
    ap.add_argument("--country", help="keep only this country (CSV with a country column)")
    ap.add_argument("--limit", type=int, help="random sample of this many cities (like pr_addByLimit)")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--max-cities", type=int, default=100, help="p_maxCities; neighbors = floor(sqrt(max-cities))")
    ap.add_argument("--solver", choices=["scip", "highs", "both"], default="both")
    ap.add_argument("--time-limit", type=float, default=None, help="seconds per exact solve")
    ap.add_argument("--skip-exact", action="store_true")
    ap.add_argument("--skip-bound", action="store_true")
    ap.add_argument("--plot", default="tsp_tours.png", help="PNG output ('' to disable)")
    ap.add_argument("--json", default="", help="write full results to this JSON file")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    cities = load_cities(args.data, args.country, args.limit, args.seed)
    P = Problem(cities, args.max_cities)
    print(f"{P.n} cities, {P.k} neighbors per city, iteration limit {P.iter_limit}\n")

    results, tours = {}, []
    init = find_initial_tour(P)
    cyc = improve_tour_cyclic(P, init)
    sim = improve_tour_simultaneous(P, init)
    for key, label, r in (("initial", "Nearest neighbor", init),
                          ("cyclic", "2-opt cyclic", cyc),
                          ("simultaneous", "2-opt simultaneous", sim)):
        assert is_single_tour(r["nxt"]), f"{label}: not a single tour"
        results[key] = {"distance_km": r["distance"], "swaps": r["swaps"], "seconds": r["time"],
                        "order": [P.names[i] for i in tour_order(r["nxt"])]}
        tours.append((f"{label}: {r['distance']:.0f} km", tour_order(r["nxt"])))

    exact = {}
    if not args.skip_exact:
        names = ["scip", "highs"] if args.solver == "both" else [args.solver]
        for nm in names:
            res = SOLVERS[nm](P, args.time_limit, relaxed=False, verbose=args.verbose)
            if "chosen" in res:
                assert len(find_subtours(P.n, res["chosen"])) == 1, f"{nm}: result has subtours"
                nx = arcs_to_next(P.n, res["chosen"])
                res["distance_check"] = P.tour_distance(nx)
                res["order"] = [P.names[i] for i in tour_order(nx)]
                if not exact:
                    tours.append((f"Exact ({res['solver']}): {res['objective']:.0f} km", tour_order(nx)))
            exact[nm] = res
        if not args.skip_bound:
            lb = SOLVERS[names[0]](P, args.time_limit, relaxed=True, verbose=args.verbose)
            results["lower_bound"] = {"solver": lb["solver"], "value_km": lb.get("objective"),
                                      "seconds": lb["time"],
                                      "note": "MIP without subtour constraint, as in pr_solveRelaxedModel"}

    print(f"{'Method':<28}{'Distance (km)':>15}{'Time (s)':>12}{'Details':>26}")
    print("-" * 81)
    for key, label in (("initial", "Nearest neighbor"), ("cyclic", "2-opt cyclic"), ("simultaneous", "2-opt simultaneous")):
        r = results[key]
        print(f"{label:<28}{r['distance_km']:>15.1f}{r['seconds']:>12.4f}{str(r['swaps']) + ' swaps':>26}")
    for nm, r in exact.items():
        if "objective" in r:
            det = f"{r['status']}, {r['cuts']} cuts"
            print(f"{'Exact ' + r['solver']:<28}{r['objective']:>15.1f}{r['time']:>12.4f}{det:>26}")
        else:
            print(f"{'Exact ' + r['solver']:<28}{'no solution':>15}{r['time']:>12.4f}{r['status']:>26}")
    if "lower_bound" in results and results["lower_bound"]["value_km"] is not None:
        lb = results["lower_bound"]
        print(f"{'Lower bound (no subtour)':<28}{lb['value_km']:>15.1f}{lb['seconds']:>12.4f}")
    best = [r["objective"] for r in exact.values() if "objective" in r]
    if best:
        opt = min(best)
        print("\nGap of heuristics to the exact optimum:")
        for key, label in (("initial", "Nearest neighbor"), ("cyclic", "2-opt cyclic"), ("simultaneous", "2-opt simultaneous")):
            print(f"  {label:<22}{100 * (results[key]['distance_km'] / opt - 1):6.2f}%")
        first = next(iter(exact.values()))
        print("\nOptimal tour:", " -> ".join(first["order"] + [first["order"][0]]))
    results["exact"] = {k: {kk: vv for kk, vv in v.items() if kk != "chosen"} for k, v in exact.items()}

    if args.plot:
        plot_tours(P, tours, args.plot)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(results, fh, indent=2)
        print(f"Results saved to {args.json}")


if __name__ == "__main__":
    main()
