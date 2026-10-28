"""Open-source phase-2 assignment for the order-picking optimizer.

Same MILP as ``solve_order_assignment`` in ``order_picking_optimizer.py``:
maximize the weighted sum of fulfilled orders, minus a workload-imbalance
penalty, subject to one picker per order, shift time, and equipment and
capacity limits (those last two are applied before this solve, by dropping
ineligible order-picker pairs).

The solver is HiGHS through ``scipy.optimize.milp``. SciPy minimizes, so the
objective coefficients are the negation of the Gurobi maximization model.
"""

from __future__ import annotations

import math
import time
from typing import Dict, Optional, Sequence, Tuple

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp
from scipy.sparse import csr_matrix

from assignment_reports import stopping_rel_gap


def solve_assignment_highs(
    weight: Dict[str, float],
    duration: Dict[str, float],
    eligible_pairs: Sequence[Tuple[str, str]],
    eligible_pickers: Dict[str, set],
    picker_ids: Sequence[str],
    picker_shift: Dict[str, float],
    balance_weight: float = 0.0,
    time_limit: float = 10.0,
    mip_gap: Optional[float] = None,
    lateness_weight: float = 0.0,
    deadlines: Optional[Dict[str, float]] = None,
    lateness_tier: Optional[Dict[str, float]] = None,
    jack_penalty: float = 0.0,
    penalized_pairs: Sequence[Tuple[str, str]] = (),
    forced_orders: Sequence[str] = (),
    stage: str = "weighted",
    locks: Optional[dict] = None,
    must_ship_ids: Sequence[str] = (),
    abs_gap: Optional[float] = None,
    accept_incumbent: bool = False,
    objective_upper: Optional[float] = None,
    load_stops: Optional[Dict[str, float]] = None,
) -> dict:
    """Solve the picker-assignment MILP with HiGHS.

    Returns a raw solve dict: ``status`` (SciPy MILP status, 0 = optimal),
    ``is_optimal``, ``objective`` (maximization value, or None), and the
    ``y``, ``x``, and ``workload`` values. The caller maps that into the
    public solution dictionary.

    With the optional arguments at their defaults the model is the original
    release-weight maximization. ``lateness_weight`` charges finish time past
    a deadline when each picker works in earliest-deadline order.
    ``jack_penalty`` charges a jack-eligible job given to a forklift.
    ``forced_orders`` fixes those jobs to be released.

    ``stage`` selects the objective. ``weighted`` is the single combined
    objective. The staged stages are ``must_count``, ``ordinary``,
    ``lateness``, ``balance``, and ``jack``. ``locks`` holds earlier stage
    values that this solve must keep. ``abs_gap`` is the absolute slack
    allowed at termination. A relative ``mip_gap`` from the caller is not
    applied. ``objective_upper`` is an upper bound on the objective size
    and is the relative-gap denominator. When it is omitted, that
    denominator is the sum of the absolute coefficients.
    ``accept_incumbent`` keeps a time-limit solution.
    """
    order_ids = list(weight.keys())
    pairs = list(eligible_pairs)
    pickers = list(picker_ids)
    must_ids = [oid for oid in must_ship_ids if oid in weight]
    held = locks or {}
    need_lateness = stage in {"lateness", "balance", "jack"} or "lateness" in held or (
        stage == "weighted" and lateness_weight > 0.0
    )
    dated = [
        oid
        for oid in order_ids
        if need_lateness and deadlines is not None and oid in deadlines
    ]
    n_y = len(order_ids)
    n_x = len(pairs)
    n_w = len(pickers)
    n_u = len(pickers)
    n_c = len(dated)
    n_l = len(dated)
    n = n_y + n_x + n_w + n_u + n_c + n_l

    idx_y = {oid: i for i, oid in enumerate(order_ids)}
    idx_x = {pair: n_y + k for k, pair in enumerate(pairs)}
    idx_w = {pid: n_y + n_x + k for k, pid in enumerate(pickers)}
    idx_u = {pid: n_y + n_x + n_w + k for k, pid in enumerate(pickers)}
    base_c = n_y + n_x + n_w + n_u
    idx_c = {oid: base_c + k for k, oid in enumerate(dated)}
    idx_l = {oid: base_c + n_c + k for k, oid in enumerate(dated)}

    if n == 0:
        return {
            "status": 0,
            "is_optimal": True,
            "objective": 0.0,
            "y": {},
            "x": {},
            "workload": {},
        }

    c = np.zeros(n)
    tiers = lateness_tier or {}
    must_set = set(must_ids)
    if stage == "weighted":
        for oid, i in idx_y.items():
            c[i] = -float(weight[oid])
        for pid, i in idx_u.items():
            c[i] = float(balance_weight)
        for oid, i in idx_l.items():
            c[i] = float(lateness_weight) * float(tiers.get(oid, 1.0))
        for pair in penalized_pairs:
            if pair in idx_x and jack_penalty:
                c[idx_x[pair]] += float(jack_penalty)
    elif stage == "must_count":
        for oid in must_ids:
            c[idx_y[oid]] = -1.0
    elif stage == "ordinary":
        for oid, i in idx_y.items():
            if oid not in must_set:
                c[i] = -float(weight[oid])
    elif stage == "lateness":
        for oid, i in idx_l.items():
            c[i] = float(tiers.get(oid, 1.0))
    elif stage == "balance":
        for pid, i in idx_u.items():
            c[i] = 1.0
    elif stage == "jack":
        for pair in penalized_pairs:
            if pair in idx_x:
                c[idx_x[pair]] += 1.0
    else:
        raise ValueError(f"Unknown assignment stage {stage!r}.")

    integrality = np.zeros(n, dtype=int)
    integrality[: n_y + n_x] = 1
    lb = np.zeros(n)
    ub = np.ones(n)
    for oid in forced_orders:
        if oid not in idx_y:
            raise ValueError(f"Forced order {oid} is not in the assignment.")
        lb[idx_y[oid]] = 1.0
    if n_w:
        ub[n_y + n_x :] = np.inf

    rows: list = []
    row_lb: list = []
    row_ub: list = []

    def add_row(coeffs: Dict[int, float], lower: float, upper: float) -> None:
        rows.append(coeffs)
        row_lb.append(lower)
        row_ub.append(upper)

    for oid in order_ids:
        coeffs = {idx_y[oid]: -1.0}
        for pid in eligible_pickers[oid]:
            coeffs[idx_x[(oid, pid)]] = 1.0
        add_row(coeffs, 0.0, 0.0)

    for pid in pickers:
        defined = {idx_w[pid]: 1.0}
        for oid in order_ids:
            pair = (oid, pid)
            if pair in idx_x:
                defined[idx_x[pair]] = -float(duration[oid])
        add_row(defined, 0.0, 0.0)
        add_row({idx_w[pid]: 1.0}, -np.inf, float(picker_shift[pid]))

    n_pickers = len(pickers)
    if n_pickers:
        share = 1.0 / n_pickers
        for pid in pickers:
            above = {idx_u[pid]: 1.0}
            below = {idx_u[pid]: 1.0}
            for other in pickers:
                above[idx_w[other]] = share
                below[idx_w[other]] = -share
            above[idx_w[pid]] = above[idx_w[pid]] - 1.0
            below[idx_w[pid]] = below[idx_w[pid]] + 1.0
            add_row(above, 0.0, np.inf)
            add_row(below, 0.0, np.inf)

    if dated:
        from assignment_reports import edd_sequence

        sequence = [
            oid
            for oid in edd_sequence(order_ids, deadlines or {}, load_stops)
            if oid in idx_c
        ]
        big_m = max((float(picker_shift[pid]) for pid in pickers), default=0.0)
        if big_m <= 0.0:
            big_m = 1.0
        earliest = min(float(deadlines[oid]) for oid in dated) if deadlines else 0.0
        big_m_late = big_m + max(0.0, -earliest)
        position = {oid: index for index, oid in enumerate(sequence)}
        for oid in sequence:
            prefix = [earlier for earlier in sequence if position[earlier] <= position[oid]]
            for pid in eligible_pickers.get(oid, ()):
                pair = (oid, pid)
                if pair not in idx_x:
                    continue
                # C >= prefix - M (1 - x). That is C - prefix - M x >= -M.
                coeffs = {idx_c[oid]: 1.0, idx_x[pair]: -big_m}
                for earlier in prefix:
                    earlier_pair = (earlier, pid)
                    if earlier_pair in idx_x:
                        coeffs[idx_x[earlier_pair]] = coeffs.get(idx_x[earlier_pair], 0.0) - float(
                            duration[earlier]
                        )
                add_row(coeffs, -big_m, np.inf)
            add_row(
                {idx_l[oid]: 1.0, idx_c[oid]: -1.0, idx_y[oid]: -big_m_late},
                -float(deadlines[oid]) - big_m_late,
                np.inf,
            )

    tolerance = float(held.get("tolerance", 1e-6))
    if "must_count" in held:
        coeffs = {idx_y[oid]: 1.0 for oid in must_ids}
        add_row(coeffs, float(held["must_count"]) - tolerance, np.inf)
    if "ordinary_weight" in held:
        coeffs = {
            idx_y[oid]: float(weight[oid])
            for oid in order_ids
            if oid not in must_set
        }
        add_row(coeffs, float(held["ordinary_weight"]) - tolerance, np.inf)
    if "lateness" in held and idx_l:
        coeffs = {idx_l[oid]: float(tiers.get(oid, 1.0)) for oid in idx_l}
        add_row(coeffs, -np.inf, float(held["lateness"]) + tolerance)
    if "balance" in held:
        add_row({idx_u[pid]: 1.0 for pid in pickers}, -np.inf, float(held["balance"]) + tolerance)
    if "jack" in held:
        coeffs = {}
        for pair in penalized_pairs:
            if pair in idx_x:
                coeffs[idx_x[pair]] = 1.0
        if coeffs:
            add_row(coeffs, -np.inf, float(held["jack"]) + tolerance)

    data: list = []
    row_index: list = []
    col_index: list = []
    for row_number, coeffs in enumerate(rows):
        for col, value in coeffs.items():
            data.append(float(value))
            row_index.append(row_number)
            col_index.append(col)
    matrix = csr_matrix((data, (row_index, col_index)), shape=(len(rows), n))

    del mip_gap  # A caller relative gap must not loosen this solve.
    # ``objective_upper`` bounds the objective. The sum of |coefficients|
    # does not, once a continuous lateness or balance variable is present.
    if objective_upper is None:
        scale = max(float(np.abs(c).sum()), 1.0)
    else:
        scale = max(float(objective_upper), 1.0)
    rel_gap = 0.0 if abs_gap is None else stopping_rel_gap(float(abs_gap), scale)
    options = {
        "disp": False,
        "time_limit": float(time_limit),
        "presolve": True,
        "mip_rel_gap": rel_gap,
    }
    maximize = stage in {"weighted", "must_count", "ordinary"}
    started = time.perf_counter()
    res = milp(
        c=c,
        integrality=integrality,
        bounds=Bounds(lb, ub),
        constraints=LinearConstraint(matrix, row_lb, row_ub),
        options=options,
    )
    runtime = time.perf_counter() - started

    proven = int(res.status) == 0 and res.x is not None
    usable = res.x is not None and int(res.status) in {0, 1}
    keep = proven or (accept_incumbent and usable)
    diagnostics = _highs_diagnostics(res, rel_gap, abs_gap, maximize)
    diagnostics["runtime"] = runtime
    diagnostics["objective_upper_used"] = scale
    if not keep:
        return {
            "status": int(res.status),
            "is_optimal": False,
            "objective": None,
            "y": {},
            "x": {},
            "workload": {},
            **diagnostics,
        }

    x = res.x
    y_values = {oid: float(x[idx_y[oid]]) for oid in order_ids}
    stage_value = _stage_value(
        stage, x, idx_y, idx_l, idx_u, idx_x, weight, tiers, must_ids, pickers, penalized_pairs
    )
    objective = stage_value if stage != "weighted" else float(-res.fun)
    return {
        "status": int(res.status),
        "is_optimal": True,
        "objective": objective,
        "stage_value": stage_value,
        "y": y_values,
        "x": {pair: float(x[idx_x[pair]]) for pair in pairs},
        "workload": {pid: float(x[idx_w[pid]]) for pid in pickers},
        **diagnostics,
    }


def _stage_value(
    stage: str,
    x,
    idx_y: Dict[str, int],
    idx_l: Dict[str, int],
    idx_u: Dict[str, int],
    idx_x: Dict[Tuple[str, str], int],
    weight: Dict[str, float],
    tiers: Dict[str, float],
    must_ids: Sequence[str],
    pickers: Sequence[str],
    penalized_pairs: Sequence[Tuple[str, str]],
) -> float:
    """Natural-unit value of this stage, read off the solution."""
    if stage == "must_count":
        return float(sum(x[idx_y[oid]] for oid in must_ids))
    if stage == "ordinary":
        must_set = set(must_ids)
        return float(sum(weight[oid] * x[idx_y[oid]] for oid in weight if oid not in must_set))
    if stage == "lateness":
        return float(sum(float(tiers.get(oid, 1.0)) * x[idx_l[oid]] for oid in idx_l))
    if stage == "balance":
        return float(sum(x[idx_u[pid]] for pid in pickers))
    if stage == "jack":
        return float(sum(x[idx_x[pair]] for pair in penalized_pairs if pair in idx_x))
    return float(sum(weight[oid] * x[idx_y[oid]] for oid in weight))


def _highs_diagnostics(res, rel_gap: float, abs_gap: Optional[float], maximize: bool) -> dict:
    """Bound and gap in this stage's natural sense.

    SciPy minimizes. ``mip_dual_bound`` is a lower bound on ``fun``. A
    maximization stage reports the negation, so its bound is ``-dual``.
    """
    dual = getattr(res, "mip_dual_bound", None)
    gap = getattr(res, "mip_gap", None)
    fun = getattr(res, "fun", None)
    abs_reached = None
    bound = None
    if dual is not None and fun is not None:
        try:
            dual_value = float(dual)
            fun_value = float(fun)
        except (TypeError, ValueError):
            dual_value = None
            fun_value = None
        if dual_value is not None and fun_value is not None and math.isfinite(dual_value):
            abs_reached = abs(fun_value - dual_value)
            bound = -dual_value if maximize else dual_value
    return {
        "solver_bound": bound,
        "solver_gap": None if gap is None else float(gap),
        "solver_abs_gap": abs_reached,
        "time_limit_hit": int(res.status) == 1,
        "mip_rel_gap_used": float(rel_gap),
        "mip_abs_gap_used": None if abs_gap is None else float(abs_gap),
    }
