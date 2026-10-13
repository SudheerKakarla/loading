"""Assignment reports and the optional lateness, jack, and must-ship flags."""

from pathlib import Path

import pandas as pd
import pytest

from assignment_reports import (
    deadline_minutes,
    drop_detail,
    edd_sequence,
    parse_scenarios,
    remove_pickers,
    schedule_lateness,
    shift_deadlines,
    utilisation_rows,
)
from order_picking_optimizer import solve_order_assignment


def test_edd_puts_undated_jobs_last_and_breaks_ties_by_id():
    assert edd_sequence(["B", "A", "C"], {"B": 5.0, "A": 5.0}) == ["A", "B", "C"]


def test_schedule_charges_lateness_only_after_the_deadline():
    schedule = schedule_lateness(
        {"P1": ["B", "A"]},
        {"A": 8.0, "B": 8.0},
        {"A": 10.0, "B": 12.0},
    )
    by_id = {row["order_id"]: row for row in schedule}
    assert by_id["A"]["position"] == 1
    assert by_id["A"]["finish_minute"] == pytest.approx(8.0)
    assert by_id["A"]["minutes_late"] == pytest.approx(0.0)
    assert by_id["B"]["finish_minute"] == pytest.approx(16.0)
    assert by_id["B"]["minutes_late"] == pytest.approx(4.0)


def test_undated_job_is_not_late():
    schedule = schedule_lateness({"P1": ["A", "B"]}, {"A": 5.0, "B": 5.0}, {"A": 5.0})
    by_id = {row["order_id"]: row for row in schedule}
    assert by_id["B"]["position"] == 2
    assert by_id["B"]["minutes_late"] == pytest.approx(0.0)


def test_blank_cutoff_is_no_deadline_and_text_is_rejected():
    assert deadline_minutes("") is None
    assert deadline_minutes(float("nan")) is None
    assert deadline_minutes(12.5) == pytest.approx(12.5)
    with pytest.raises(ValueError, match="minutes"):
        deadline_minutes("tomorrow")


def test_drop_detail_names_equipment_capacity_shift_and_tradeoff():
    assert drop_detail(["equipment"], [], 5.0, {}, {}) == "equipment"
    assert drop_detail(["capacity"], [], 5.0, {}, {}) == "capacity"
    assert drop_detail(["equipment", "capacity"], [], 5.0, {}, {}) == "equipment_and_capacity"
    assert drop_detail([], ["P1"], 4.0, {"P1": 9.0}, {"P1": 10.0}) == "shift_capacity"
    assert drop_detail([], ["P1"], 4.0, {"P1": 1.0}, {"P1": 10.0}) == "priority_tradeoff"


def test_utilisation_is_busy_over_shift():
    pickers = pd.DataFrame(
        [
            {"picker_id": "J", "equipment_type": "pallet_jack", "shift_duration": 100.0},
            {"picker_id": "F", "equipment_type": "forklift", "shift_duration": 50.0},
        ]
    )
    rows = utilisation_rows(pickers, {"J": 25.0, "F": 0.0})
    assert rows[0]["utilisation_pct"] == pytest.approx(25.0)
    assert rows[1]["busy_minutes"] == pytest.approx(0.0)


def test_scenario_file_and_picker_removal():
    scenario_path = Path(__file__).resolve().parents[1] / "scenarios" / "assignment.yaml"
    scenarios = parse_scenarios(scenario_path.read_text(encoding="utf-8"))
    assert [item["name"] for item in scenarios] == [
        "base",
        "one_forklift_down",
        "two_jacks_down",
        "deadlines_30_min_earlier",
    ]
    assert scenarios[1]["remove"] == {"forklift": 1}
    assert scenarios[3]["deadline_minutes"] == pytest.approx(-30.0)
    pickers = pd.DataFrame(
        [
            {"picker_id": "F2", "equipment_type": "forklift"},
            {"picker_id": "F1", "equipment_type": "forklift"},
            {"picker_id": "J1", "equipment_type": "pallet_jack"},
        ]
    )
    kept = remove_pickers(pickers, {"forklift": 1})
    assert set(kept["picker_id"]) == {"F2", "J1"}
    orders = pd.DataFrame([{"order_id": "A", "cutoff_time": 40.0}, {"order_id": "B", "cutoff_time": ""}])
    shifted = shift_deadlines(orders, -30.0)
    assert list(shifted["cutoff_time"]) == [10.0, ""]


def test_deadlines_do_not_change_the_default_objective():
    """A cutoff on the row is only a report until lateness_weight is set."""
    orders = pd.DataFrame(
        [
            {"order_id": "BIG", "total_weight": 1.0, "priority_weight": 5.0, "cutoff_time": 1.0},
            {"order_id": "SMALL1", "total_weight": 1.0, "priority_weight": 1.0, "cutoff_time": 1.0},
            {"order_id": "SMALL2", "total_weight": 1.0, "priority_weight": 1.0, "cutoff_time": 100.0},
        ]
    )
    pickers = pd.DataFrame(
        [{"picker_id": "P1", "equipment_type": "pallet_jack", "max_weight": 50.0, "shift_duration": 10.0}]
    )
    result = solve_order_assignment(
        orders, pickers, {"BIG": 9.0, "SMALL1": 4.0, "SMALL2": 4.0}, time_limit=10.0
    )
    assert result["orders_fulfilled"] == ["BIG"]
    assert result["objective_value"] == pytest.approx(5.0)
    assert result["drops"][0]["detail"] == "shift_capacity"


def test_lateness_weight_splits_jobs_that_would_miss_a_shared_deadline():
    orders = pd.DataFrame(
        [
            {"order_id": "A", "total_weight": 1.0, "priority_weight": 1.0, "cutoff_time": 10.0},
            {"order_id": "B", "total_weight": 1.0, "priority_weight": 1.0, "cutoff_time": 10.0},
        ]
    )
    pickers = pd.DataFrame(
        [
            {"picker_id": "P1", "equipment_type": "pallet_jack", "max_weight": 50.0, "shift_duration": 100.0},
            {"picker_id": "P2", "equipment_type": "pallet_jack", "max_weight": 50.0, "shift_duration": 100.0},
        ]
    )
    for solver in ("highs", "gurobi"):
        result = solve_order_assignment(
            orders,
            pickers,
            {"A": 10.0, "B": 10.0},
            lateness_weight=1.0,
            assignment_solver=solver,
            time_limit=10.0,
        )
        assert result["is_optimal"] is True
        assert set(result["orders_fulfilled"]) == {"A", "B"}
        assert result["late_minutes"] == pytest.approx(0.0)
        assert result["objective_value"] == pytest.approx(2.0)
        assert {len(result["picker_assignments"]["P1"]), len(result["picker_assignments"]["P2"])} == {1}


def test_on_time_jobs_keep_the_release_score():
    orders = pd.DataFrame(
        [
            {"order_id": "O1", "total_weight": 5.0, "priority_weight": 1.0, "cutoff_time": 1000.0},
            {"order_id": "O2", "total_weight": 5.0, "priority_weight": 1.0, "cutoff_time": 1000.0},
        ]
    )
    pickers = pd.DataFrame(
        [{"picker_id": "P1", "equipment_type": "pallet_jack", "max_weight": 50.0, "shift_duration": 100.0}]
    )
    plain = solve_order_assignment(orders, pickers, {"O1": 10.0, "O2": 10.0}, time_limit=10.0)
    timed = solve_order_assignment(
        orders, pickers, {"O1": 10.0, "O2": 10.0}, lateness_weight=1.0, time_limit=10.0
    )
    assert plain["orders_fulfilled"] == timed["orders_fulfilled"]
    assert timed["objective_value"] == pytest.approx(plain["objective_value"])
    assert timed["late_minutes"] == pytest.approx(0.0)


def test_prefer_jack_assigns_a_light_job_to_the_jack():
    orders = pd.DataFrame(
        [{"order_id": "LIGHT", "total_weight": 5.0, "priority_weight": 10.0}]
    )
    pickers = pd.DataFrame(
        [
            {"picker_id": "J", "equipment_type": "pallet_jack", "max_weight": 50.0, "shift_duration": 100.0},
            {"picker_id": "F", "equipment_type": "forklift", "max_weight": 50.0, "shift_duration": 100.0},
        ]
    )
    result = solve_order_assignment(
        orders, pickers, {"LIGHT": 5.0}, prefer_jack=1.0, time_limit=10.0
    )
    assert result["picker_assignments"]["J"] == ["LIGHT"]
    assert result["picker_assignments"]["F"] == []
    assert result["objective_value"] == pytest.approx(10.0)


def test_prefer_jack_can_drop_a_job_the_forklift_still_has_room_for():
    """The penalty is larger than the release weight, and the jack shift is too short."""
    orders = pd.DataFrame([{"order_id": "LIGHT", "total_weight": 1.0, "priority_weight": 1.0}])
    pickers = pd.DataFrame(
        [
            {"picker_id": "J", "equipment_type": "pallet_jack", "max_weight": 50.0, "shift_duration": 5.0},
            {"picker_id": "F", "equipment_type": "forklift", "max_weight": 50.0, "shift_duration": 100.0},
        ]
    )
    result = solve_order_assignment(
        orders, pickers, {"LIGHT": 10.0}, prefer_jack=5.0, time_limit=10.0
    )
    assert result["orders_fulfilled"] == []
    assert result["drops"] == [
        {
            "order_id": "LIGHT",
            "reason": "shift_time_limit_or_priority_tradeoff",
            "detail": "priority_tradeoff",
        }
    ]


def test_require_must_ship_is_infeasible_when_the_job_exceeds_the_shift():
    orders = pd.DataFrame(
        [
            {
                "order_id": "MUST",
                "total_weight": 1.0,
                "priority_weight": 100000.0,
                "must_ship": "true",
                "cutoff_time": 10.0,
            }
        ]
    )
    pickers = pd.DataFrame(
        [{"picker_id": "P1", "equipment_type": "pallet_jack", "max_weight": 50.0, "shift_duration": 10.0}]
    )
    forced = solve_order_assignment(
        orders, pickers, {"MUST": 50.0}, require_must_ship=True, time_limit=10.0
    )
    assert forced["is_optimal"] is False
    assert forced.get("infeasible_must_ship") is True
    optional = solve_order_assignment(orders, pickers, {"MUST": 50.0}, time_limit=10.0)
    assert optional["is_optimal"] is True
    assert optional["orders_fulfilled"] == []
    assert optional["drops"][0]["detail"] == "shift_capacity"


def test_heavy_job_detail_is_equipment_and_overweight_detail_is_capacity():
    jack = pd.DataFrame(
        [
            {
                "picker_id": "J",
                "equipment_type": "pallet_jack",
                "max_weight": 10.0,
                "max_volume": 5.0,
                "shift_duration": 100.0,
            }
        ]
    )
    heavy = solve_order_assignment(
        pd.DataFrame(
            [
                {
                    "order_id": "HEAVY",
                    "total_weight": 5.0,
                    "priority_weight": 1.0,
                    "required_equipment": "forklift",
                }
            ]
        ),
        jack,
        {"HEAVY": 5.0},
        time_limit=10.0,
    )
    bulky = solve_order_assignment(
        pd.DataFrame([{"order_id": "BULKY", "total_weight": 11.0, "priority_weight": 1.0}]),
        jack,
        {"BULKY": 5.0},
        time_limit=10.0,
    )
    assert heavy["orders_not_fulfilled"] == [{"order_id": "HEAVY", "reason": "no_eligible_picker"}]
    assert heavy["drops"][0]["detail"] == "equipment"
    assert bulky["drops"][0]["detail"] == "capacity"


def test_gurobi_matches_highs_on_the_jack_penalty():
    orders = pd.DataFrame([{"order_id": "LIGHT", "total_weight": 5.0, "priority_weight": 10.0}])
    pickers = pd.DataFrame(
        [
            {"picker_id": "J", "equipment_type": "pallet_jack", "max_weight": 50.0, "shift_duration": 100.0},
            {"picker_id": "F", "equipment_type": "forklift", "max_weight": 50.0, "shift_duration": 100.0},
        ]
    )
    highs = solve_order_assignment(
        orders, pickers, {"LIGHT": 5.0}, prefer_jack=1.0, assignment_solver="highs", time_limit=10.0
    )
    gurobi = solve_order_assignment(
        orders, pickers, {"LIGHT": 5.0}, prefer_jack=1.0, assignment_solver="gurobi", time_limit=10.0
    )
    assert gurobi["picker_assignments"]["J"] == highs["picker_assignments"]["J"]
    assert gurobi["objective_value"] == pytest.approx(highs["objective_value"])
