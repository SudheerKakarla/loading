"""
Unit tests for order_picking_optimizer.py.

Each test is deliberately tiny (2-4 orders, 1-2 pickers, 3-4 routing nodes)
to keep solve time and memory well within sandbox limits. Gurobi's time
limit is capped at 10 seconds per test via solve_order_assignment's
``time_limit`` parameter (default already 10s; tests pass it explicitly for
clarity).

Scenario ids below reference the "Test Scenarios" section of
order_picking_planning_optimizer_spec.md.
"""

import io
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import scipy.optimize  # HiGHS import is slow; keep it outside the T1 timer.

from order_picking_optimizer import (
    MUST_SHIP_WEIGHT,
    _is_missing,
    compute_priority_weight,
    format_picking_solution,
    main,
    route_order,
    solve_order_assignment,
    solve_order_picking,
)


# ---------------------------------------------------------------------------
# T1: Basic feasible case - all orders fulfilled under equal weights
# ---------------------------------------------------------------------------
def test_t1_basic_feasible_all_orders_fulfilled():
    """T1: with ample shift time and equal weights, both orders should be
    fulfilled and assigned to eligible pickers."""
    orders = pd.DataFrame(
        [
            {"order_id": "O1", "total_weight": 5.0, "priority_weight": 1.0},
            {"order_id": "O2", "total_weight": 5.0, "priority_weight": 1.0},
        ]
    )
    pickers = pd.DataFrame(
        [{"picker_id": "P1", "equipment_type": "pallet_jack", "max_weight": 50.0, "shift_duration": 100.0}]
    )
    route_durations = {"O1": 10.0, "O2": 10.0}

    t0 = time.perf_counter()
    result = solve_order_assignment(orders, pickers, route_durations, time_limit=10.0)
    elapsed = time.perf_counter() - t0

    assert result["is_optimal"] is True
    assert set(result["orders_fulfilled"]) == {"O1", "O2"}
    assert result["objective_value"] == pytest.approx(2.0)
    assert result["picker_assignments"]["P1"] == ["O1", "O2"]
    assert elapsed < 10.0


# ---------------------------------------------------------------------------
# T2: Weighted priority trade-off (C8) - fewer, higher-weight orders beat
# more, lower-weight orders when shift time cannot fit both groups.
# ---------------------------------------------------------------------------
def test_t2_weighted_priority_trade_off():
    """T2: one picker, shift_duration=10. A single high-weight order
    (duration 9, weight 5) cannot be combined with anything else, while two
    low-weight orders (duration 4 each, weight 1 each) fit together
    (total 8 <= 10, total weight 2). The weighted objective should prefer
    the single high-weight order (5 > 2), demonstrating non-strict,
    weighted priority rather than count maximization."""
    orders = pd.DataFrame(
        [
            {"order_id": "BIG", "total_weight": 1.0, "priority_weight": 5.0},
            {"order_id": "SMALL1", "total_weight": 1.0, "priority_weight": 1.0},
            {"order_id": "SMALL2", "total_weight": 1.0, "priority_weight": 1.0},
        ]
    )
    pickers = pd.DataFrame(
        [{"picker_id": "P1", "equipment_type": "pallet_jack", "max_weight": 50.0, "shift_duration": 10.0}]
    )
    route_durations = {"BIG": 9.0, "SMALL1": 4.0, "SMALL2": 4.0}

    result = solve_order_assignment(orders, pickers, route_durations, time_limit=10.0)

    assert result["is_optimal"] is True
    assert result["orders_fulfilled"] == ["BIG"]
    assert result["objective_value"] == pytest.approx(5.0)
    reasons = {d["order_id"]: d["reason"] for d in result["orders_not_fulfilled"]}
    assert reasons["SMALL1"] == "shift_time_limit_or_priority_tradeoff"
    assert reasons["SMALL2"] == "shift_time_limit_or_priority_tradeoff"


# ---------------------------------------------------------------------------
# T3: Equipment eligibility (C4) excludes an incompatible assignment
# ---------------------------------------------------------------------------
def test_t3_equipment_eligibility_excludes_order():
    """T3: an order requiring a forklift cannot be assigned to a
    pallet-jack-only picker, so it is left unfulfilled with reason
    'no_eligible_picker', even though shift time and weight would
    otherwise allow it."""
    orders = pd.DataFrame(
        [
            {
                "order_id": "HEAVY",
                "total_weight": 5.0,
                "priority_weight": 1.0,
                "required_equipment": "forklift",
            }
        ]
    )
    pickers = pd.DataFrame(
        [{"picker_id": "P1", "equipment_type": "pallet_jack", "max_weight": 50.0, "shift_duration": 100.0}]
    )
    route_durations = {"HEAVY": 5.0}

    result = solve_order_assignment(orders, pickers, route_durations, time_limit=10.0)

    assert result["is_optimal"] is True
    assert result["orders_fulfilled"] == []
    assert result["objective_value"] == pytest.approx(0.0)
    assert result["orders_not_fulfilled"] == [{"order_id": "HEAVY", "reason": "no_eligible_picker"}]


# ---------------------------------------------------------------------------
# T4: benchmark_mode (equal weights) selects a different order set than the
# weighted production objective on the same data (C8).
# ---------------------------------------------------------------------------
def test_t4_benchmark_mode_differs_from_weighted_mode():
    """T4: using the same data as T2, benchmark_mode=True should maximize a
    plain count of orders fulfilled (choosing the two small orders), while
    the weighted (production) objective chooses the single big order."""
    orders = pd.DataFrame(
        [
            {"order_id": "BIG", "total_weight": 1.0, "priority_weight": 5.0},
            {"order_id": "SMALL1", "total_weight": 1.0, "priority_weight": 1.0},
            {"order_id": "SMALL2", "total_weight": 1.0, "priority_weight": 1.0},
        ]
    )
    pickers = pd.DataFrame(
        [{"picker_id": "P1", "equipment_type": "pallet_jack", "max_weight": 50.0, "shift_duration": 10.0}]
    )
    route_durations = {"BIG": 9.0, "SMALL1": 4.0, "SMALL2": 4.0}

    weighted = solve_order_assignment(orders, pickers, route_durations, benchmark_mode=False, time_limit=10.0)
    benchmark = solve_order_assignment(orders, pickers, route_durations, benchmark_mode=True, time_limit=10.0)

    assert weighted["orders_fulfilled"] == ["BIG"]
    assert set(benchmark["orders_fulfilled"]) == {"SMALL1", "SMALL2"}
    assert benchmark["objective_value"] == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# T5: Edge case - no picker can fit any order within shift time; model
# remains feasible (all y=0) with objective 0, not infeasible.
# ---------------------------------------------------------------------------
def test_t5_no_capacity_edge_case_remains_feasible():
    """T5: every order's route duration exceeds the only picker's shift
    duration, so the optimal solution fulfills no orders. The model stays
    feasible (optimal) since leaving all orders unfulfilled is a valid
    solution, consistent with the specification's note that this problem
    has no infeasible case analogous to the diet problem's T3."""
    orders = pd.DataFrame(
        [
            {"order_id": "O1", "total_weight": 1.0, "priority_weight": 1.0},
            {"order_id": "O2", "total_weight": 1.0, "priority_weight": 1.0},
        ]
    )
    pickers = pd.DataFrame(
        [{"picker_id": "P1", "equipment_type": "pallet_jack", "max_weight": 50.0, "shift_duration": 1.0}]
    )
    route_durations = {"O1": 5.0, "O2": 5.0}

    result = solve_order_assignment(orders, pickers, route_durations, time_limit=10.0)

    assert result["is_optimal"] is True
    assert result["orders_fulfilled"] == []
    assert result["objective_value"] == pytest.approx(0.0)
    assert result["picker_workload"]["P1"] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# T6: Phase 1 routing - a single order's route is a valid closed tour
# starting and ending at the depot, visiting every pick location once.
# ---------------------------------------------------------------------------
def test_t6_phase1_routing_valid_closed_tour():
    """T6: a 3-location order routed with the heuristic solver should
    produce a closed tour (depot -> ... -> depot) visiting the depot plus
    every pick location exactly once, with a positive route duration no
    worse than the perimeter of the unit square used in this fixture."""
    depot = "DEPOT"
    locations = ["L1", "L2", "L3"]
    # Unit-square-ish layout: DEPOT(0,0), L1(1,0), L2(1,1), L3(0,1)
    distance_matrix = {
        "DEPOT": {"L1": 1.0, "L2": 1.4142, "L3": 1.0},
        "L1": {"DEPOT": 1.0, "L2": 1.0, "L3": 1.4142},
        "L2": {"DEPOT": 1.4142, "L1": 1.0, "L3": 1.0},
        "L3": {"DEPOT": 1.0, "L1": 1.4142, "L2": 1.0},
    }

    t0 = time.perf_counter()
    result = route_order("ORDER1", locations, depot, distance_matrix, solver="heuristic")
    elapsed = time.perf_counter() - t0

    route = result["route"]
    assert route[0] == depot
    assert route[-1] == depot
    assert set(route[:-1]) == {depot, "L1", "L2", "L3"}
    assert len(route) == 5  # depot, 3 locations, depot
    assert result["route_duration"] == pytest.approx(4.0, abs=1e-3)  # optimal perimeter tour
    assert elapsed < 10.0


# ---------------------------------------------------------------------------
# End-to-end pipeline smoke test (phase 1 + phase 2 together)
# ---------------------------------------------------------------------------
def test_end_to_end_pipeline_smoke_test():
    """Smoke test combining phase 1 routing and phase 2 assignment on a
    minimal 1-order, 1-picker instance."""
    depot = "DEPOT"
    distance_matrix = {
        "DEPOT": {"L1": 1.0, "L2": 1.0},
        "L1": {"DEPOT": 1.0, "L2": 1.0},
        "L2": {"DEPOT": 1.0, "L1": 1.0},
    }
    orders = pd.DataFrame([{"order_id": "O1", "total_weight": 1.0, "priority_weight": 1.0}])
    order_items = pd.DataFrame(
        [
            {"order_id": "O1", "item_id": "ITEM1", "location_id": "L1"},
            {"order_id": "O1", "item_id": "ITEM2", "location_id": "L2"},
        ]
    )
    pickers = pd.DataFrame(
        [{"picker_id": "P1", "equipment_type": "pallet_jack", "max_weight": 50.0, "shift_duration": 100.0}]
    )

    result = solve_order_picking(orders, order_items, pickers, distance_matrix, depot, phase2_time_limit=10.0)

    assert result["is_optimal"] is True
    assert result["orders_fulfilled"] == ["O1"]
    assert "O1" in result["routes"]
    assert result["routes"]["O1"]["route"][0] == depot
    assert result["routes"]["O1"]["route"][-1] == depot


# ---------------------------------------------------------------------------
# T7: C8 derives priority_weight from cutoff bands, wait, and tier.
# An explicit priority_weight is kept. benchmark_mode still forces 1.
# ---------------------------------------------------------------------------
def test_t7_priority_bands_tiers_and_explicit_weight():
    """T7: wave bands and default tiers fill a missing priority_weight.
    key/A is 2x. Overdue is 6, this wave is 4, next wave is 2, later is 1.
    A stored priority_weight wins over the cutoff. must_ship is finite."""
    orders = pd.DataFrame(
        [
            {"order_id": "THIS", "total_weight": 1.0, "cutoff_time": 4.0, "account_tier": "A"},
            {"order_id": "NEXT", "total_weight": 1.0, "cutoff_time": 10.0},
            {"order_id": "LATER", "total_weight": 1.0, "cutoff_time": 20.0},
            {"order_id": "LATE", "total_weight": 1.0, "cutoff_time": -1.0},
            {"order_id": "WAIT", "total_weight": 1.0, "wait_time": 16.0},
            {"order_id": "PLAIN", "total_weight": 1.0},
            {"order_id": "KEPT", "total_weight": 1.0, "priority_weight": 5.0, "cutoff_time": -1.0},
            {"order_id": "SHIP", "total_weight": 1.0, "must_ship": True},
        ]
    )

    weighted = compute_priority_weight(orders, wave_hours=8.0)
    by_id = dict(zip(weighted["order_id"], weighted["priority_weight"]))
    assert by_id["THIS"] == pytest.approx(8.0)
    assert by_id["NEXT"] == pytest.approx(2.0)
    assert by_id["LATER"] == pytest.approx(1.0)
    assert by_id["LATE"] == pytest.approx(6.0)
    assert by_id["WAIT"] == pytest.approx(4.0)
    assert by_id["PLAIN"] == pytest.approx(1.0)
    assert by_id["KEPT"] == pytest.approx(5.0)
    assert by_id["SHIP"] == pytest.approx(MUST_SHIP_WEIGHT)

    clock = compute_priority_weight(
        pd.DataFrame(
            [
                {
                    "order_id": "CLOCK",
                    "cutoff_time": datetime(2026, 10, 10, 12, 0),
                }
            ]
        ),
        now=datetime(2026, 10, 10, 8, 0),
        wave_hours=8.0,
    )
    assert clock.loc[0, "priority_weight"] == pytest.approx(4.0)

    recomputed = compute_priority_weight(orders, respect_explicit=False)
    kept = recomputed.loc[recomputed["order_id"] == "KEPT", "priority_weight"].iloc[0]
    assert kept == pytest.approx(6.0)

    with pytest.raises(ValueError, match="Unknown account_tier"):
        compute_priority_weight(pd.DataFrame([{"order_id": "X", "account_tier": "gold"}]))


def test_t7_derived_weight_changes_the_assignment():
    """T7 assignment: a this-wave tier-A order (weight 8, duration 9) beats
    two later orders (weight 1, duration 4) that fit together. benchmark_mode
    fulfills the two later orders and scores 2."""
    orders = pd.DataFrame(
        [
            {"order_id": "BIG", "total_weight": 1.0, "cutoff_time": 4.0, "account_tier": "A"},
            {"order_id": "SMALL1", "total_weight": 1.0, "cutoff_time": 20.0},
            {"order_id": "SMALL2", "total_weight": 1.0, "cutoff_time": 20.0},
        ]
    )
    pickers = pd.DataFrame(
        [{"picker_id": "P1", "equipment_type": "pallet_jack", "max_weight": 50.0, "shift_duration": 10.0}]
    )
    route_durations = {"BIG": 9.0, "SMALL1": 4.0, "SMALL2": 4.0}

    weighted = solve_order_assignment(orders, pickers, route_durations, time_limit=10.0)
    benchmark = solve_order_assignment(
        orders, pickers, route_durations, benchmark_mode=True, time_limit=10.0
    )

    assert weighted["is_optimal"] is True
    assert weighted["orders_fulfilled"] == ["BIG"]
    assert weighted["objective_value"] == pytest.approx(8.0)
    assert weighted["order_priority_weight"]["BIG"] == pytest.approx(8.0)
    assert set(benchmark["orders_fulfilled"]) == {"SMALL1", "SMALL2"}
    assert benchmark["objective_value"] == pytest.approx(2.0)
    assert benchmark["order_priority_weight"] == {"SMALL1": 1.0, "SMALL2": 1.0}


def test_t7_key_account_flag_and_queued_since():
    """A blank tier with key_account uses the key multiplier. queued_since
    measures wait against now when cutoff and wait_time are absent."""
    now = datetime(2026, 10, 10, 8, 0)
    orders = pd.DataFrame(
        [
            {
                "order_id": "KEY",
                "queued_since": datetime(2026, 10, 9, 8, 0),
                "key_account": True,
            }
        ]
    )
    weighted = compute_priority_weight(orders, now=now, wave_hours=8.0)
    assert weighted.loc[0, "priority_weight"] == pytest.approx(8.0)


# ---------------------------------------------------------------------------
# T8: one-stop route, pick time, missing travel, order with no lines
# ---------------------------------------------------------------------------
def test_t8_one_stop_route_adds_pick_time():
    """T8: a single location is depot -> location -> depot. pick_time adds
    to the there-and-back travel, and does not require a 3-node TSP."""
    distance_matrix = {"DEPOT": {"L1": 2.0}, "L1": {"DEPOT": 3.0}}
    result = route_order("O1", ["L1"], "DEPOT", distance_matrix, pick_time=1.5)
    assert result["route"] == ["DEPOT", "L1", "DEPOT"]
    assert result["route_duration"] == pytest.approx(6.5)


def test_t8_line_pick_time_sums_on_the_tour():
    """Two lines on a 2-stop order add their pick times on top of travel.
    All travel edges are 1, so the tour is 3 and the pick times add 1.5."""
    depot = "DEPOT"
    distance_matrix = {
        "DEPOT": {"L1": 1.0, "L2": 1.0},
        "L1": {"DEPOT": 1.0, "L2": 1.0},
        "L2": {"DEPOT": 1.0, "L1": 1.0},
    }
    orders = pd.DataFrame([{"order_id": "O1", "total_weight": 1.0, "priority_weight": 1.0}])
    order_items = pd.DataFrame(
        [
            {"order_id": "O1", "item_id": "A", "location_id": "L1", "pick_time": 1.0},
            {"order_id": "O1", "item_id": "B", "location_id": "L2", "pick_time": 0.5},
        ]
    )
    pickers = pd.DataFrame(
        [{"picker_id": "P1", "equipment_type": "pallet_jack", "max_weight": 50.0, "shift_duration": 100.0}]
    )
    result = solve_order_picking(
        orders, order_items, pickers, distance_matrix, depot, phase2_time_limit=10.0
    )
    assert result["routes"]["O1"]["route_duration"] == pytest.approx(4.5)


def test_t8_missing_travel_and_missing_lines_raise():
    """A hole in the travel matrix, and an order with no lines, are input
    errors rather than a solver failure."""
    with pytest.raises(ValueError, match="Order O1: Travel time missing"):
        route_order("O1", ["L1"], "DEPOT", {"DEPOT": {}, "L1": {}})

    orders = pd.DataFrame([{"order_id": "O1", "total_weight": 1.0, "priority_weight": 1.0}])
    order_items = pd.DataFrame(columns=["order_id", "item_id", "location_id"])
    pickers = pd.DataFrame(
        [{"picker_id": "P1", "equipment_type": "pallet_jack", "max_weight": 50.0, "shift_duration": 100.0}]
    )
    with pytest.raises(ValueError, match="no pick lines"):
        solve_order_picking(orders, order_items, pickers, {}, "DEPOT", phase2_time_limit=10.0)


# ---------------------------------------------------------------------------
# T9: weight and volume capacity use the same no_eligible_picker reason
# ---------------------------------------------------------------------------
def test_t9_weight_and_volume_capacity_exclude_order():
    """T9: C2 rejects an order the picker cannot carry, by weight or volume."""
    pickers = pd.DataFrame(
        [
            {
                "picker_id": "P1",
                "equipment_type": "pallet_jack",
                "max_weight": 10.0,
                "max_volume": 5.0,
                "shift_duration": 100.0,
            }
        ]
    )
    heavy = pd.DataFrame(
        [{"order_id": "HEAVY", "total_weight": 11.0, "priority_weight": 1.0}]
    )
    bulky = pd.DataFrame(
        [{"order_id": "BULKY", "total_weight": 1.0, "total_volume": 6.0, "priority_weight": 1.0}]
    )

    heavy_result = solve_order_assignment(heavy, pickers, {"HEAVY": 5.0}, time_limit=10.0)
    bulky_result = solve_order_assignment(bulky, pickers, {"BULKY": 5.0}, time_limit=10.0)

    assert heavy_result["orders_not_fulfilled"] == [
        {"order_id": "HEAVY", "reason": "no_eligible_picker"}
    ]
    assert bulky_result["orders_not_fulfilled"] == [
        {"order_id": "BULKY", "reason": "no_eligible_picker"}
    ]


def test_format_picking_solution_lists_score_and_spread():
    """Print output names the fulfilled count, score, assignment, and spread."""
    text = format_picking_solution(
        {
            "orders_fulfilled": ["O1"],
            "orders_not_fulfilled": [
                {"order_id": "O2", "reason": "shift_time_limit_or_priority_tradeoff"}
            ],
            "objective_value": 4.0,
            "picker_assignments": {"P1": ["O1"]},
            "picker_workload": {"P1": 10.0},
        }
    )
    assert "Orders fulfilled: 1 / 2" in text
    assert "Weighted score: 4" in text
    assert "P1: O1" in text
    assert "Workload spread (max-min): 0" in text
    assert "O2: shift_time_limit_or_priority_tradeoff" in text


# ---------------------------------------------------------------------------
# Phase 2 open-source backend: same assignment model on HiGHS.
# ---------------------------------------------------------------------------
def _tradeoff_inputs():
    orders = pd.DataFrame(
        [
            {"order_id": "BIG", "total_weight": 1.0, "priority_weight": 5.0},
            {"order_id": "SMALL1", "total_weight": 1.0, "priority_weight": 1.0},
            {"order_id": "SMALL2", "total_weight": 1.0, "priority_weight": 1.0},
        ]
    )
    pickers = pd.DataFrame(
        [{"picker_id": "P1", "equipment_type": "pallet_jack", "max_weight": 50.0, "shift_duration": 10.0}]
    )
    route_durations = {"BIG": 9.0, "SMALL1": 4.0, "SMALL2": 4.0}
    return orders, pickers, route_durations


def test_highs_assignment_matches_weighted_and_benchmark_cases():
    """HiGHS solves the same phase-2 model: T2 prefers BIG, T4 benchmark
    prefers the two small orders, T3 and T5 stay feasible with nobody picked."""
    orders, pickers, route_durations = _tradeoff_inputs()
    weighted = solve_order_assignment(
        orders, pickers, route_durations, assignment_solver="highs", time_limit=10.0
    )
    benchmark = solve_order_assignment(
        orders,
        pickers,
        route_durations,
        benchmark_mode=True,
        assignment_solver="highs",
        time_limit=10.0,
    )
    assert weighted["solver"] == "highs"
    assert weighted["is_optimal"] is True
    assert weighted["orders_fulfilled"] == ["BIG"]
    assert weighted["objective_value"] == pytest.approx(5.0)
    assert weighted["picker_workload"]["P1"] == pytest.approx(9.0)
    assert set(benchmark["orders_fulfilled"]) == {"SMALL1", "SMALL2"}
    assert benchmark["objective_value"] == pytest.approx(2.0)

    blocked = pd.DataFrame(
        [
            {
                "order_id": "HEAVY",
                "total_weight": 5.0,
                "priority_weight": 1.0,
                "required_equipment": "forklift",
            }
        ]
    )
    jack = pd.DataFrame(
        [{"picker_id": "P1", "equipment_type": "pallet_jack", "max_weight": 50.0, "shift_duration": 100.0}]
    )
    excluded = solve_order_assignment(
        blocked, jack, {"HEAVY": 5.0}, assignment_solver="highs", time_limit=10.0
    )
    assert excluded["orders_not_fulfilled"] == [{"order_id": "HEAVY", "reason": "no_eligible_picker"}]
    assert excluded["objective_value"] == pytest.approx(0.0)

    too_long = pd.DataFrame(
        [
            {"order_id": "O1", "total_weight": 1.0, "priority_weight": 1.0},
            {"order_id": "O2", "total_weight": 1.0, "priority_weight": 1.0},
        ]
    )
    short_shift = pd.DataFrame(
        [{"picker_id": "P1", "equipment_type": "pallet_jack", "max_weight": 50.0, "shift_duration": 1.0}]
    )
    none_fit = solve_order_assignment(
        too_long, short_shift, {"O1": 5.0, "O2": 5.0}, assignment_solver="highs", time_limit=10.0
    )
    assert none_fit["is_optimal"] is True
    assert none_fit["orders_fulfilled"] == []
    assert none_fit["picker_workload"]["P1"] == pytest.approx(0.0)


def test_default_solver_is_highs_and_gurobi_agrees():
    """The default assignment backend is HiGHS. On the T2 instance it picks
    the same order and score as an explicit Gurobi solve. ``auto`` still
    selects a licensed backend when one is present."""
    orders, pickers, route_durations = _tradeoff_inputs()
    default = solve_order_assignment(orders, pickers, route_durations, time_limit=10.0)
    highs = solve_order_assignment(
        orders, pickers, route_durations, assignment_solver="highs", time_limit=10.0
    )
    gurobi = solve_order_assignment(
        orders, pickers, route_durations, assignment_solver="gurobi", time_limit=10.0
    )
    auto = solve_order_assignment(
        orders, pickers, route_durations, assignment_solver="auto", time_limit=10.0
    )
    assert default["solver"] == "highs"
    assert highs["solver"] == "highs"
    assert gurobi["solver"] == "gurobi"
    assert default["orders_fulfilled"] == gurobi["orders_fulfilled"]
    assert default["objective_value"] == pytest.approx(gurobi["objective_value"])
    assert auto["solver"] in {"gurobi", "highs"}
    assert auto["orders_fulfilled"] == highs["orders_fulfilled"]


def test_unknown_assignment_solver_raises():
    orders, pickers, route_durations = _tradeoff_inputs()
    with pytest.raises(ValueError, match="assignment_solver"):
        solve_order_assignment(
            orders, pickers, route_durations, assignment_solver="cplex", time_limit=10.0
        )


def _square_matrix(asymmetric: bool) -> dict:
    """Four nodes where nearest neighbor is DEPOT-L1-L2-L3.

    The symmetric matrix has a shorter reversal. The asymmetric matrix makes
    that same reversal use an expensive return edge, so 2-opt must keep the
    nearest-neighbor walk.
    """
    matrix = {
        "DEPOT": {"L1": 2.0, "L2": 3.0, "L3": 4.0},
        "L1": {"DEPOT": 2.0, "L2": 2.0, "L3": 3.0},
        "L2": {"DEPOT": 3.0, "L1": 2.0, "L3": 6.0},
        "L3": {"DEPOT": 4.0, "L1": 3.0, "L2": 6.0},
    }
    if asymmetric:
        matrix["L2"]["L1"] = 100.0
        matrix["DEPOT"]["L3"] = 100.0
    return matrix


def test_symmetric_two_opt_shortens_the_nearest_neighbor_tour():
    """A reversal whose middle edges cost the same both ways is still accepted."""
    result = route_order(
        "O1", ["L1", "L2", "L3"], "DEPOT", _square_matrix(False), solver="heuristic"
    )
    assert result["route"] == ["DEPOT", "L2", "L1", "L3", "DEPOT"]
    assert result["route_duration"] == pytest.approx(12.0)


def test_asymmetric_two_opt_does_not_keep_a_longer_reversal():
    """The endpoint-only delta is positive here, and the reversed walk is not.

    Nearest neighbor is DEPOT, L1, L2, L3 and costs 14. Reversing L1..L2
    looks shorter if the return edge L2 -> L1 is ignored, and costs 110 if
    that edge is paid. The directed gain refuses it.
    """
    result = route_order(
        "O1", ["L1", "L2", "L3"], "DEPOT", _square_matrix(True), solver="heuristic"
    )
    assert result["route"] == ["DEPOT", "L1", "L2", "L3", "DEPOT"]
    assert result["route_duration"] == pytest.approx(14.0)


def test_directed_exact_tour_uses_the_cheap_direction():
    """One-way triangle: the walk 1+1+1 is optimal. An undirected model prices
    a different number and may orient the cycle either way."""
    pytest.importorskip("highspy")
    matrix = {
        "DEPOT": {"A": 1.0, "B": 10.0},
        "A": {"DEPOT": 10.0, "B": 1.0},
        "B": {"DEPOT": 1.0, "A": 10.0},
    }
    result = route_order("O1", ["A", "B"], "DEPOT", matrix, solver="highs")
    assert result["route"] == ["DEPOT", "A", "B", "DEPOT"]
    assert result["route_duration"] == pytest.approx(3.0)


def test_directed_exact_tour_rejects_a_cheaper_pair_of_cycles():
    """Two cheap 2-cycles cost 4 and do not visit every stop in one tour.
    Every Hamiltonian tour costs 12. The cut loop has to reach that tour."""
    pytest.importorskip("highspy")
    nodes = ["DEPOT", "L1", "L2", "L3"]
    cheap = {("DEPOT", "L1"), ("L1", "DEPOT"), ("L2", "L3"), ("L3", "L2")}
    matrix = {
        origin: {
            dest: (1.0 if (origin, dest) in cheap else 5.0)
            for dest in nodes
            if dest != origin
        }
        for origin in nodes
    }
    result = route_order("O1", ["L1", "L2", "L3"], "DEPOT", matrix, solver="highs")
    route = result["route"]
    assert route[0] == "DEPOT" and route[-1] == "DEPOT"
    assert set(route[:-1]) == set(nodes)
    walked = sum(matrix[a][b] for a, b in zip(route, route[1:]))
    assert result["route_duration"] == pytest.approx(walked)
    assert walked == pytest.approx(12.0)


def test_pandas_missing_markers_are_not_must_ship():
    """pd.NA and NaT are empty cells. They do not become a must-ship flag."""
    assert _is_missing(pd.NA) is True
    assert _is_missing(pd.NaT) is True
    assert _is_missing(np.datetime64("NaT", "ns")) is True
    orders = pd.DataFrame(
        [
            {
                "order_id": "O1",
                "total_weight": 1.0,
                "priority_weight": pd.NA,
                "must_ship": pd.NA,
            }
        ]
    )
    weighted = compute_priority_weight(orders)
    assert float(weighted.iloc[0].priority_weight) == pytest.approx(1.0)


def test_blank_equipment_from_csv_is_not_a_requirement():
    """A blank CSV cell is NaN. That is no equipment requirement and no volume cap."""
    orders = pd.read_csv(
        io.StringIO(
            "order_id,total_weight,total_volume,required_equipment,priority_weight\n"
            "O1,5.0,,,1.0\n"
        )
    )
    assert pd.isna(orders.loc[0, "required_equipment"])
    assert pd.isna(orders.loc[0, "total_volume"])
    pickers = pd.DataFrame(
        [{"picker_id": "P1", "equipment_type": "pallet_jack", "max_weight": 50.0, "shift_duration": 100.0}]
    )
    result = solve_order_assignment(
        orders, pickers, {"O1": 5.0}, time_limit=10.0, assignment_solver="highs"
    )
    assert result["is_optimal"] is True
    assert result["orders_fulfilled"] == ["O1"]
    assert result["orders_not_fulfilled"] == []


def test_small_layout_cli_fulfills_both_orders(tmp_path, capsys):
    """The two-aisle map at depot (0, 0): O1 is 26 minutes, O2 is 6."""
    example = Path(__file__).resolve().parents[1] / "inputs" / "layout_small"
    out = tmp_path / "assignments.csv"
    main(
        [
            "--orders",
            str(example / "orders.csv"),
            "--items",
            str(example / "order_items.csv"),
            "--pickers",
            str(example / "pickers.csv"),
            "--travel",
            str(example / "travel_times_depot.csv"),
            "--depot",
            "DEPOT",
            "--assignments-out",
            str(out),
        ]
    )
    assert "Orders fulfilled: 2 / 2" in capsys.readouterr().out
    written = pd.read_csv(out)
    got = {row.order_id: float(row.duration) for row in written.itertuples(index=False)}
    assert got["O1"] == pytest.approx(26.0)
    assert got["O2"] == pytest.approx(6.0)


def test_warehouse_sample_fulfills_every_order(tmp_path, capsys):
    """Six orders on the six-aisle map. Blank equipment stays eligible.

    Weights are 1, 2, 1, 1, 100000, 1. All six fit in the shifts, so the
    score is 100006. O4 names a forklift, so only P3 can take it.
    """
    root = Path(__file__).resolve().parents[1]
    example = root / "inputs" / "layout_example"
    out = tmp_path / "assignments.csv"
    main(
        [
            "--orders",
            str(example / "orders.csv"),
            "--items",
            str(example / "order_items.csv"),
            "--pickers",
            str(example / "pickers.csv"),
            "--travel",
            str(example / "travel_times.csv"),
            "--depot",
            "DEPOT",
            "--assignments-out",
            str(out),
        ]
    )
    text = capsys.readouterr().out
    assert "Orders fulfilled: 6 / 6" in text
    assert "Weighted score: 100006" in text
    written = pd.read_csv(out)
    checked = pd.read_csv(root / "outputs" / "layout_example" / "assignments.csv")
    got = {row.order_id: float(row.duration) for row in written.itertuples(index=False)}
    expected = {row.order_id: float(row.duration) for row in checked.itertuples(index=False)}
    assert got.keys() == expected.keys()
    for order_id, minutes in expected.items():
        assert got[order_id] == pytest.approx(minutes)
    forklift = written[written.order_id == "O4"]
    assert list(forklift.picker_id) == ["P3"]


def test_share_shift_uses_both_pallet_jacks(tmp_path, capsys):
    """Sharing the minutes keeps every order and gives both jacks some work.

    O4 is 3200 kg and names a forklift, so it stays on P3. The pallet jack
    limit is 2500 kg.
    """
    root = Path(__file__).resolve().parents[1]
    example = root / "inputs" / "layout_example"
    out = tmp_path / "assignments.csv"
    main(
        [
            "--orders",
            str(example / "orders.csv"),
            "--items",
            str(example / "order_items.csv"),
            "--pickers",
            str(example / "pickers.csv"),
            "--travel",
            str(example / "travel_times.csv"),
            "--depot",
            "DEPOT",
            "--share-shift",
            "--assignments-out",
            str(out),
        ]
    )
    text = capsys.readouterr().out
    assert "Orders fulfilled: 6 / 6" in text
    assert "Weighted score: 100006" in text
    written = pd.read_csv(out)
    light = written[written.order_id != "O4"]
    assert {"P1", "P2"} <= set(light.picker_id)
    assert list(written.loc[written.order_id == "O4", "picker_id"]) == ["P3"]


def test_wave_inputs_use_case_study_equipment(tmp_path):
    """The shipped crew config is eight jacks at 2500 kg and four forks at 5000 kg.

    An order over 2500 kg names a forklift. The same seed rewrites the same
    order file from that crew config.
    """
    from build_wave import load_crew, load_wave_settings, write_wave

    root = Path(__file__).resolve().parents[1]
    crew = load_crew(root / "inputs" / "crew.csv")
    assert list(crew.equipment_type) == ["pallet_jack", "forklift"]
    assert list(crew["count"]) == [8, 4]
    assert list(crew.max_weight) == [2500.0, 5000.0]
    assert set(crew.shift_duration) == {480.0}
    settings = load_wave_settings(root / "inputs" / "wave_settings.csv")
    assert settings.n_orders == 300
    assert settings.pick_time == 0.5
    assert settings.heavy_share == 0.15
    assert settings.must_ship_weight == 100000
    wave = root / "inputs" / "layout_wave"
    pickers = pd.read_csv(wave / "pickers.csv")
    orders = pd.read_csv(wave / "orders.csv")
    jacks = pickers[pickers.equipment_type == "pallet_jack"]
    forks = pickers[pickers.equipment_type == "forklift"]
    assert len(orders) == 300
    assert len(jacks) == 8
    assert len(forks) == 4
    assert set(jacks.max_weight) == {2500.0}
    assert set(forks.max_weight) == {5000.0}
    heavy = orders[orders.total_weight > 2500]
    assert not heavy.empty
    assert set(heavy.required_equipment) == {"forklift"}
    light = orders[orders.required_equipment.fillna("") != "forklift"]
    assert (light.total_weight <= 2500).all()

    regenerated = write_wave(tmp_path / "wave", n_orders=300, seed=20261010)
    got = pd.read_csv(regenerated / "orders.csv")
    assert got.equals(orders)
