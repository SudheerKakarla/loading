"""Assignment reports and the optional lateness, jack, and must-ship flags."""

from pathlib import Path

import pandas as pd
import pytest

from assignment_reports import (
    absolute_gap,
    deadline_minutes,
    drop_detail,
    edd_sequence,
    load_inversions,
    parse_scenarios,
    reachable_absolute_gap,
    remove_pickers,
    require_shared_crew,
    schedule_lateness,
    shift_deadlines,
    staging_peaks,
    stop_depth,
    stop_reason,
    utilisation_rows,
    walk_gap_summary,
)
from assignment_reports import main as crew_main
from order_picking_optimizer import format_picking_solution, solve_order_assignment


def test_edd_puts_undated_jobs_last_and_breaks_ties_by_id():
    assert edd_sequence(["B", "A", "C"], {"B": 5.0, "A": 5.0}) == ["A", "B", "C"]


def test_load_stops_break_a_shared_deadline_by_deepest_stop():
    stops = {"A": 1.0, "B": 4.0, "C": 9.0}
    assert edd_sequence(["A", "B", "C"], {"A": 5.0, "B": 5.0}, stops) == ["B", "A", "C"]
    assert edd_sequence(["A", "B"], {"A": 1.0, "B": 5.0}, {"A": 0.0, "B": 9.0}) == ["A", "B"]
    assert edd_sequence(["B", "A"], {"A": 5.0, "B": 5.0}, {"A": 2.0}) == ["A", "B"]
    assert stop_depth("STOP-4") == pytest.approx(4.0)
    assert stop_depth("") == pytest.approx(0.0)
    assert stop_depth(None) == pytest.approx(0.0)


def test_schedule_uses_the_deepest_stop_inside_one_deadline():
    schedule = schedule_lateness(
        {"P1": ["A", "B"]},
        {"A": 8.0, "B": 3.0},
        {"A": 5.0, "B": 5.0},
        {"A": 1.0, "B": 4.0},
    )
    by_id = {row["order_id"]: row for row in schedule}
    assert by_id["B"]["position"] == 1
    assert by_id["B"]["minutes_late"] == pytest.approx(0.0)
    assert by_id["A"]["finish_minute"] == pytest.approx(11.0)
    assert by_id["A"]["minutes_late"] == pytest.approx(6.0)


def test_load_inversions_count_a_deeper_stop_that_finishes_later():
    schedule = [
        {"order_id": "DEEP", "finish_minute": 20.0},
        {"order_id": "NEAR", "finish_minute": 10.0},
        {"order_id": "OTHER", "finish_minute": 5.0},
    ]
    trucks = {"DEEP": "T1", "NEAR": "T1", "OTHER": "T2"}
    stops = {"DEEP": 4.0, "NEAR": 1.0, "OTHER": 3.0}
    found = load_inversions(schedule, trucks, stops)
    assert found["count"] == 1
    assert found["pairs"][0]["deeper_order_id"] == "DEEP"
    cleared = [
        {"order_id": "DEEP", "finish_minute": 10.0},
        {"order_id": "NEAR", "finish_minute": 20.0},
    ]
    assert load_inversions(cleared, {"DEEP": "T1", "NEAR": "T1"}, stops)["count"] == 0


def test_staging_peak_counts_jobs_waiting_until_departure():
    waiting = [
        {"order_id": "A", "finish_minute": 10.0, "deadline_minute": 20.0},
        {"order_id": "B", "finish_minute": 15.0, "deadline_minute": 20.0},
    ]
    peak = staging_peaks(waiting, staging_buffer=30.0)
    assert peak["peak"] == 2
    assert peak["finished_after_departure"] == 0
    assert "2500 kg" in peak["assumptions"]
    handoff = [
        {"order_id": "A", "finish_minute": 0.0, "deadline_minute": 10.0},
        {"order_id": "B", "finish_minute": 30.0, "deadline_minute": 20.0},
    ]
    # Buffer 20. A waits on [0, 30) and B on [30, 40). The departure is applied first, so the peak is 1.
    assert staging_peaks(handoff, staging_buffer=20.0)["peak"] == 1
    late = [{"order_id": "A", "finish_minute": 40.0, "deadline_minute": 0.0}]
    missed = staging_peaks(late, staging_buffer=30.0)
    assert missed["peak"] == 0
    assert missed["finished_after_departure"] == 1
    with pytest.raises(ValueError, match="staging_buffer"):
        staging_peaks(waiting, staging_buffer=-1.0)


def test_walk_gap_summary_reports_mean_p95_and_worst():
    summary = walk_gap_summary([0.0, 1.0, 2.0, 3.0, 4.0])
    assert summary["count"] == 5
    assert summary["mean"] == pytest.approx(2.0)
    assert summary["p95"] == pytest.approx(4.0)
    assert summary["worst"] == pytest.approx(4.0)
    with pytest.raises(ValueError, match="at least one"):
        walk_gap_summary([])


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
    assert drop_detail(["pick_mode"], [], 5.0, {}, {}) == "pick_mode"
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
    assert forced["status_reason"] == "required_must_ship_jobs_cannot_all_fit"
    assert "cannot all fit" in format_picking_solution(forced)
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


def _one_picker(shift: float, shift_start: float = 0.0) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "picker_id": "P1",
                "equipment_type": "pallet_jack",
                "max_weight": 50.0,
                "shift_duration": shift,
                "shift_start": shift_start,
            }
        ]
    )


def test_shared_crew_flags_only_a_real_overage():
    over = [
        pd.DataFrame([{"picker_id": "FK02", "busy_minutes": 300.0, "available_minutes": 480.0}]),
        pd.DataFrame([{"picker_id": "FK02", "busy_minutes": 200.0, "available_minutes": 480.0}]),
    ]
    with pytest.raises(ValueError, match="FK02"):
        require_shared_crew(over)
    require_shared_crew(
        [
            pd.DataFrame([{"picker_id": "FK02", "busy_minutes": 200.0, "available_minutes": 480.0}]),
            pd.DataFrame([{"picker_id": "FK02", "busy_minutes": 200.0, "available_minutes": 480.0}]),
        ]
    )
    require_shared_crew(
        [
            pd.DataFrame([{"picker_id": "FK02", "busy_minutes": 240.0, "available_minutes": 480.0}]),
            pd.DataFrame([{"picker_id": "FK02", "busy_minutes": 240.0 + 1e-6, "available_minutes": 480.0}]),
        ]
    )


def test_shared_crew_cli_reads_two_utilisation_files(tmp_path):
    within = [
        {"picker_id": "P", "busy_minutes": 100.0, "available_minutes": 480.0},
    ]
    first = tmp_path / "a.csv"
    second = tmp_path / "b.csv"
    third = tmp_path / "c.csv"
    pd.DataFrame(within).to_csv(first, index=False)
    pd.DataFrame(within).to_csv(second, index=False)
    crew_main([str(first), str(second)])
    pd.DataFrame(
        [{"picker_id": "P", "busy_minutes": 400.0, "available_minutes": 480.0}]
    ).to_csv(third, index=False)
    with pytest.raises(ValueError, match="P "):
        crew_main([str(first), str(third)])


def test_withhold_uses_the_available_window_not_the_raw_cutoff():
    """Duration 20 fits a cutoff of 30, but not a picker who starts at minute 15."""
    orders = pd.DataFrame(
        [{"order_id": "LATE", "total_weight": 1.0, "priority_weight": 2.0, "cutoff_time": 30.0}]
    )
    pickers = _one_picker(480.0, shift_start=15.0)
    withheld = solve_order_assignment(
        orders,
        pickers,
        {"LATE": 20.0},
        withhold_if_late_at_start=True,
        time_limit=10.0,
    )
    assert withheld["orders_not_fulfilled"] == [
        {"order_id": "LATE", "reason": "cannot_meet_cutoff"}
    ]
    assert withheld["drops"] == [
        {"order_id": "LATE", "reason": "cannot_meet_cutoff", "detail": "cannot_meet_cutoff"}
    ]
    assert withheld["orders_fulfilled"] == []
    kept = solve_order_assignment(orders, pickers, {"LATE": 20.0}, time_limit=10.0)
    assert kept["orders_fulfilled"] == ["LATE"]

    on_time = solve_order_assignment(
        pd.DataFrame(
            [{"order_id": "OK", "total_weight": 1.0, "priority_weight": 2.0, "cutoff_time": 20.0}]
        ),
        _one_picker(480.0),
        {"OK": 20.0},
        withhold_if_late_at_start=True,
        time_limit=10.0,
    )
    assert on_time["orders_fulfilled"] == ["OK"]

    blocked = solve_order_assignment(
        pd.DataFrame(
            [
                {
                    "order_id": "HEAVY",
                    "total_weight": 1.0,
                    "priority_weight": 2.0,
                    "cutoff_time": 10.0,
                    "required_equipment": "forklift",
                }
            ]
        ),
        _one_picker(480.0),
        {"HEAVY": 50.0},
        withhold_if_late_at_start=True,
        time_limit=10.0,
    )
    assert blocked["orders_not_fulfilled"] == [
        {"order_id": "HEAVY", "reason": "no_eligible_picker"}
    ]


def test_withhold_runs_before_a_must_ship_force():
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
    result = solve_order_assignment(
        orders,
        _one_picker(100.0),
        {"MUST": 50.0},
        require_must_ship=True,
        withhold_if_late_at_start=True,
        time_limit=10.0,
    )
    assert result["is_optimal"] is True
    assert result.get("infeasible_must_ship") is not True
    assert result["drops"][0]["detail"] == "cannot_meet_cutoff"


def _release_wave():
    rows = [
        {
            "order_id": "MUST",
            "total_weight": 1.0,
            "priority_weight": 100000.0,
            "must_ship": "true",
            "cutoff_time": 10000.0,
        }
    ]
    rows.extend(
        {
            "order_id": f"J{index:02d}",
            "total_weight": 1.0,
            "priority_weight": 2.0,
            "cutoff_time": 10000.0,
        }
        for index in range(1, 21)
    )
    orders = pd.DataFrame(rows)
    durations = {row["order_id"]: 1.0 for row in rows}
    pickers = _one_picker(1000.0)
    return orders, pickers, durations


def test_reachable_gap_sits_above_noise_and_below_one_job():
    """Noise on a huge objective lifts the stop, and never past half a job."""
    assert reachable_absolute_gap([4.0, 100000.0], 1.0e6) == pytest.approx(2.0)
    assert reachable_absolute_gap([0.4], 1.0e6) == pytest.approx(0.04)
    assert reachable_absolute_gap([2.0] * 20, 40.0) == pytest.approx(absolute_gap([2.0]))
    assert stop_reason(True, True, 0.0, 0.2) == "time_limit"
    assert stop_reason(False, False, None, 0.2) == "no_solution"
    assert stop_reason(False, True, 1e-8, 0.2) == "proven"
    assert stop_reason(False, True, 0.2, 0.2) == "gap_reached"


def _assert_encoded_gap(result, smallest_weight: float) -> None:
    """The caller gap is stored, and the relative stop uses the objective upper bound."""
    assert result["gap_requested"] == pytest.approx(0.05)
    assert result["mip_abs_gap_used"] is not None
    assert result["mip_abs_gap_used"] < smallest_weight
    upper = float(result["objective_upper_used"])
    assert upper >= 1.0
    if result["solver"] == "gurobi":
        assert result["mip_rel_gap_used"] == pytest.approx(0.0)
    else:
        assert result["mip_rel_gap_used"] == pytest.approx(result["mip_abs_gap_used"] / upper)
    text = format_picking_solution(result)
    assert "Solver status:" in text
    assert "Bound:" in text


def test_staged_release_ignores_a_five_percent_gap_and_keeps_every_job():
    orders, pickers, durations = _release_wave()
    for solver in ("highs", "gurobi"):
        staged = solve_order_assignment(
            orders,
            pickers,
            durations,
            lateness_weight=1.0,
            mip_gap=0.05,
            staged_assignment=True,
            assignment_solver=solver,
            time_limit=10.0,
        )
        assert staged["is_optimal"] is True
        assert len(staged["orders_fulfilled"]) == 21
        assert staged["objective_value"] == pytest.approx(100040.0)
        assert staged["late_minutes"] == pytest.approx(0.0)
        names = [row["stage"] for row in staged["stages"]]
        assert names[:3] == ["must_count", "ordinary", "lateness"]
        by_name = {row["stage"]: row for row in staged["stages"]}
        ordinary_gap = absolute_gap([2.0])
        assert by_name["must_count"]["mip_abs_gap_used"] == pytest.approx(0.5)
        assert by_name["ordinary"]["mip_abs_gap_used"] == pytest.approx(ordinary_gap)
        assert ordinary_gap <= by_name["lateness"]["mip_abs_gap_used"] < 2.0
        # The count stage encodes an absolute slack of 0.5. On an objective
        # of size 1 that relative value is 0.5, which is not the caller's 5%.
        assert by_name["must_count"]["mip_rel_gap_used"] != pytest.approx(0.05)
        for name in ("ordinary", "lateness"):
            row = by_name[name]
            if solver == "gurobi":
                assert row["mip_rel_gap_used"] == pytest.approx(0.0)
            else:
                assert row["mip_rel_gap_used"] == pytest.approx(
                    row["mip_abs_gap_used"] / row["objective_upper_used"]
                )
        for row in staged["stages"]:
            assert row["time_limit_hit"] is False
            assert row["stop"] == "proven"
        assert staged["stop"] == "proven"
        weighted = solve_order_assignment(
            orders,
            pickers,
            durations,
            lateness_weight=1.0,
            mip_gap=0.05,
            assignment_solver=solver,
            time_limit=10.0,
        )
        assert len(weighted["orders_fulfilled"]) == 21
        assert weighted["mip_abs_gap_used"] < 2.0
        _assert_encoded_gap(weighted, 2.0)
        assert weighted["stop"] == "proven"


def test_staged_keeps_both_jobs_when_lateness_costs_more_than_weight():
    """T8b: both jobs score 0 once lateness is charged; one job scores 2."""
    orders = pd.DataFrame(
        [
            {"order_id": "J1", "total_weight": 1.0, "priority_weight": 2.0, "cutoff_time": 10.0},
            {"order_id": "J2", "total_weight": 1.0, "priority_weight": 2.0, "cutoff_time": 12.0},
        ]
    )
    pickers = _one_picker(100.0)
    durations = {"J1": 8.0, "J2": 8.0}
    for solver in ("highs", "gurobi"):
        weighted = solve_order_assignment(
            orders,
            pickers,
            durations,
            lateness_weight=1.0,
            assignment_solver=solver,
            time_limit=10.0,
        )
        assert len(weighted["orders_fulfilled"]) == 1
        assert weighted["objective_value"] == pytest.approx(2.0)
        assert weighted["drops"][0]["detail"] == "priority_tradeoff"
        staged = solve_order_assignment(
            orders,
            pickers,
            durations,
            lateness_weight=1.0,
            mip_gap=0.05,
            staged_assignment=True,
            assignment_solver=solver,
            time_limit=10.0,
        )
        assert set(staged["orders_fulfilled"]) == {"J1", "J2"}
        assert staged["late_minutes"] == pytest.approx(4.0)
        assert staged["objective_value"] == pytest.approx(4.0)
        assert staged["gap_requested"] == pytest.approx(0.05)
        assert staged["stop"] == "proven"
        for row in staged["stages"]:
            assert row["mip_abs_gap_used"] < 2.0
            assert row["stop"] == "proven"
            if solver == "gurobi":
                assert row["mip_rel_gap_used"] == pytest.approx(0.0)
            else:
                assert row["mip_rel_gap_used"] == pytest.approx(
                    row["mip_abs_gap_used"] / row["objective_upper_used"]
                )


def test_load_stops_change_the_sum_of_late_minutes_inside_one_deadline():
    """Same cutoff. Shortest job first leaves 6 late minutes. Longest first leaves 9."""
    orders = pd.DataFrame(
        [
            {"order_id": "A", "total_weight": 1.0, "priority_weight": 2.0, "cutoff_time": 5.0},
            {"order_id": "B", "total_weight": 1.0, "priority_weight": 2.0, "cutoff_time": 5.0},
        ]
    )
    pickers = _one_picker(100.0)
    durations = {"A": 8.0, "B": 3.0}
    for solver in ("highs", "gurobi"):
        plain = solve_order_assignment(
            orders,
            pickers,
            durations,
            lateness_weight=1.0,
            staged_assignment=True,
            assignment_solver=solver,
            time_limit=10.0,
        )
        ordered = solve_order_assignment(
            orders,
            pickers,
            durations,
            lateness_weight=1.0,
            staged_assignment=True,
            assignment_solver=solver,
            time_limit=10.0,
            load_stops={"A": 1.0, "B": 4.0},
        )
        assert set(plain["orders_fulfilled"]) == {"A", "B"}
        assert set(ordered["orders_fulfilled"]) == {"A", "B"}
        assert plain["objective_value"] == pytest.approx(4.0)
        assert ordered["objective_value"] == pytest.approx(4.0)
        assert plain["late_minutes"] == pytest.approx(9.0)
        assert ordered["late_minutes"] == pytest.approx(6.0)
        plain_lateness = next(row for row in plain["stages"] if row["stage"] == "lateness")
        ordered_lateness = next(row for row in ordered["stages"] if row["stage"] == "lateness")
        assert plain_lateness["incumbent"] == pytest.approx(9.0)
        assert ordered_lateness["incumbent"] == pytest.approx(6.0)
        plain_pos = {row["order_id"]: row["position"] for row in plain["schedule"]}
        ordered_pos = {row["order_id"]: row["position"] for row in ordered["schedule"]}
        assert plain_pos["A"] == 1
        assert ordered_pos["B"] == 1


def _write_depot(folder: Path) -> None:
    folder.mkdir(parents=True)
    pd.DataFrame([{"order_id": "J1"}]).to_csv(folder / "orders.csv", index=False)
    pd.DataFrame([{"order_id": "J1", "location_id": "L1"}]).to_csv(
        folder / "order_items.csv", index=False
    )


def test_run_docks_checks_the_shift_from_the_crew_file(tmp_path, monkeypatch, capsys):
    """The dock runner uses shift_duration and screens summed minutes after both docks."""
    import run_docks

    by_depot = tmp_path / "by_depot"
    _write_depot(by_depot / "PACK_A")
    _write_depot(by_depot / "PACK_B")
    pickers = pd.DataFrame(
        [
            {
                "picker_id": "P1",
                "equipment_type": "pallet_jack",
                "max_weight": 50.0,
                "shift_duration": 400.0,
            }
        ]
    )
    travel = pd.DataFrame(columns=["from_location", "to_location", "travel_time"])
    loads = {"PACK_A": 250.0, "PACK_B": 160.0}

    def fake_solve(orders, items, crew, matrix, depot_id, **kwargs):
        return {
            "is_optimal": True,
            "picker_workload": {"P1": loads[depot_id]},
            "picker_assignments": {"P1": []},
            "orders_fulfilled": [],
            "orders_not_fulfilled": [],
            "drops": [],
            "schedule": [],
            "objective_value": 0.0,
            "status": 0,
            "stop": "proven",
            "solver_bound": 0.0,
            "solver_gap": 0.0,
            "solver_abs_gap": 0.0,
            "mip_rel_gap_used": 0.0,
            "mip_abs_gap_used": 0.2,
            "objective_upper_used": 1.0,
            "runtime": 0.0,
            "time_limit_hit": False,
        }

    monkeypatch.setattr(run_docks, "solve_order_picking", fake_solve)
    out = tmp_path / "out"
    with pytest.raises(ValueError, match="P1"):
        run_docks.solve_docks(by_depot, pickers, travel, out)
    for name in ("PACK_A", "PACK_B"):
        table = pd.read_csv(out / name / "utilisation.csv")
        assert table.iloc[0]["available_minutes"] == pytest.approx(400.0)
    loads["PACK_A"] = 200.0
    loads["PACK_B"] = 150.0
    run_docks.solve_docks(by_depot, pickers, travel, tmp_path / "fit")
    screen = capsys.readouterr().out
    assert "Summed busy minutes fit the shift across 2 docks." in screen
    assert "Clock overlap and the walk between docks are outside this screen." in screen
    fit = pd.read_csv(tmp_path / "fit" / "PACK_B" / "utilisation.csv")
    assert fit.iloc[0]["available_minutes"] == pytest.approx(400.0)
    assert fit.iloc[0]["busy_minutes"] == pytest.approx(150.0)


def test_staging_capacity_reports_the_first_minute_over_the_peak():
    """No capacity leaves the peak report. A capacity of one flags the second pallet."""
    waiting = [
        {"order_id": "A", "finish_minute": 10.0, "deadline_minute": 20.0},
        {"order_id": "B", "finish_minute": 15.0, "deadline_minute": 20.0},
    ]
    plain = staging_peaks(waiting, staging_buffer=30.0)
    assert plain["peak"] == 2
    assert plain["over_capacity"] is False
    assert plain["first_over_minute"] is None
    over = staging_peaks(waiting, staging_buffer=30.0, capacity=1)
    assert over["over_capacity"] is True
    assert over["first_over_minute"] == pytest.approx(15.0)
    counted = staging_peaks(
        waiting,
        staging_buffer=30.0,
        capacity=3,
        pallet_counts={"A": 2, "B": 2},
    )
    assert counted["peak"] == pytest.approx(4)
    assert counted["first_over_minute"] == pytest.approx(15.0)


def test_non_pick_minutes_shrink_the_shift_and_a_blank_does_not():
    """Duties reduce the minutes the solver and the utilisation table both use."""
    orders = pd.DataFrame(
        [
            {
                "order_id": "O1",
                "total_weight": 10.0,
                "total_volume": 0.1,
                "priority_weight": 1.0,
                "required_equipment": "",
                "cutoff_time": "",
            }
        ]
    )
    on_duty = pd.DataFrame(
        [
            {
                "picker_id": "P1",
                "equipment_type": "pallet_jack",
                "max_weight": 100.0,
                "max_volume": 2.0,
                "shift_duration": 100.0,
                "non_pick_minutes": 40.0,
            }
        ]
    )
    blocked = solve_order_assignment(orders, on_duty, {"O1": 70.0})
    assert blocked["orders_fulfilled"] == []
    free = on_duty.drop(columns=["non_pick_minutes"])
    released = solve_order_assignment(orders, free, {"O1": 70.0})
    assert released["orders_fulfilled"] == ["O1"]
    rows = utilisation_rows(on_duty, {"P1": 50.0})
    assert rows[0]["available_minutes"] == pytest.approx(60.0)
    with pytest.raises(ValueError, match="P1"):
        require_shared_crew(
            [
                pd.DataFrame(utilisation_rows(on_duty, {"P1": 40.0})),
                pd.DataFrame(utilisation_rows(on_duty, {"P1": 30.0})),
            ]
        )


def _quiet_solve(orders, items, crew, matrix, depot_id, **kwargs):
    return {
        "is_optimal": True,
        "picker_workload": {picker_id: 10.0 for picker_id in crew.picker_id},
        "picker_assignments": {},
        "orders_fulfilled": [],
        "orders_not_fulfilled": [],
        "drops": [],
        "schedule": [
            {"order_id": "J1", "finish_minute": 0.0, "deadline_minute": 10.0, "minutes_late": 0.0},
            {"order_id": "J2", "finish_minute": 5.0, "deadline_minute": 10.0, "minutes_late": 0.0},
        ],
        "objective_value": 0.0,
        "status": 0,
        "stop": "proven",
        "solver_bound": 0.0,
        "solver_gap": 0.0,
        "solver_abs_gap": 0.0,
        "mip_rel_gap_used": 0.0,
        "mip_abs_gap_used": 0.2,
        "objective_upper_used": 1.0,
        "runtime": 0.0,
        "time_limit_hit": False,
        "stages": [],
    }


def test_home_depot_hides_a_picker_and_staging_overflow_is_only_a_report(tmp_path, monkeypatch, capsys):
    """A named home stays off the other dock. Overflow is printed and does not stop the screen."""
    import run_docks

    by_depot = tmp_path / "by_depot"
    _write_depot(by_depot / "PACK_A")
    _write_depot(by_depot / "PACK_B")
    pickers = pd.DataFrame(
        [
            {
                "picker_id": "P1",
                "equipment_type": "pallet_jack",
                "max_weight": 50.0,
                "shift_duration": 400.0,
                "home_depot": "PACK_A",
            },
            {
                "picker_id": "P2",
                "equipment_type": "pallet_jack",
                "max_weight": 50.0,
                "shift_duration": 400.0,
                "home_depot": "",
            },
        ]
    )
    seen = {}

    def fake_solve(orders, items, crew, matrix, depot_id, **kwargs):
        seen[depot_id] = list(crew.picker_id)
        return _quiet_solve(orders, items, crew, matrix, depot_id, **kwargs)

    monkeypatch.setattr(run_docks, "solve_order_picking", fake_solve)
    travel = pd.DataFrame(columns=["from_location", "to_location", "travel_time"])
    out = tmp_path / "out"
    run_docks.solve_docks(by_depot, pickers, travel, out, staging_capacity=1)
    assert seen["PACK_A"] == ["P1", "P2"]
    assert seen["PACK_B"] == ["P2"]
    assert list(pd.read_csv(out / "PACK_B" / "utilisation.csv").picker_id) == ["P2"]
    screen = capsys.readouterr().out
    assert not (out / "staging_overflow.csv").exists()
    for depot_id in ("PACK_A", "PACK_B"):
        staging = pd.read_csv(out / depot_id / "staging.csv")
        assert staging.depot_id.iloc[0] == depot_id
        assert staging.peak.iloc[0] == pytest.approx(2)
        assert bool(staging.over_capacity.iloc[0]) is True
        assert f"Staging {depot_id}:" in screen
    with pytest.raises(ValueError, match="PACK_A"):
        run_docks.solve_docks(
            by_depot,
            pickers,
            travel,
            tmp_path / "failed",
            staging_capacity=1,
            fail_on_overflow=True,
        )
    assert (tmp_path / "failed" / "PACK_B" / "staging.csv").is_file()
