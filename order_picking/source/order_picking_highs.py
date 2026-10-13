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

from typing import Dict, Optional, Sequence, Tuple

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp
from scipy.sparse import csr_matrix


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
    """
    order_ids = list(weight.keys())
    pairs = list(eligible_pairs)
    pickers = list(picker_ids)
    dated = [
        oid
        for oid in order_ids
        if lateness_weight > 0.0 and deadlines is not None and oid in deadlines
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
    for oid, i in idx_y.items():
        c[i] = -float(weight[oid])
    for pid, i in idx_u.items():
        c[i] = float(balance_weight)
    tiers = lateness_tier or {}
    for oid, i in idx_l.items():
        c[i] = float(lateness_weight) * float(tiers.get(oid, 1.0))
    for pair in penalized_pairs:
        if pair in idx_x and jack_penalty:
            c[idx_x[pair]] += float(jack_penalty)

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

        sequence = [oid for oid in edd_sequence(order_ids, deadlines or {}) if oid in idx_c]
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

    data: list = []
    row_index: list = []
    col_index: list = []
    for row_number, coeffs in enumerate(rows):
        for col, value in coeffs.items():
            data.append(float(value))
            row_index.append(row_number)
            col_index.append(col)
    matrix = csr_matrix((data, (row_index, col_index)), shape=(len(rows), n))

    options = {
        "disp": False,
        "time_limit": float(time_limit),
        "presolve": True,
    }
    if mip_gap is not None:
        options["mip_rel_gap"] = float(mip_gap)

    res = milp(
        c=c,
        integrality=integrality,
        bounds=Bounds(lb, ub),
        constraints=LinearConstraint(matrix, row_lb, row_ub),
        options=options,
    )

    optimal = int(res.status) == 0 and res.x is not None
    if not optimal:
        return {
            "status": int(res.status),
            "is_optimal": False,
            "objective": None,
            "y": {},
            "x": {},
            "workload": {},
        }

    x = res.x
    return {
        "status": int(res.status),
        "is_optimal": True,
        "objective": float(-res.fun),
        "y": {oid: float(x[idx_y[oid]]) for oid in order_ids},
        "x": {pair: float(x[idx_x[pair]]) for pair in pairs},
        "workload": {pid: float(x[idx_w[pid]]) for pid in pickers},
    }
