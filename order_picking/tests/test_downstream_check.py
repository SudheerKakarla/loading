"""The downstream queue is a report. Two jobs on one server show the wait."""

from pathlib import Path

import pandas as pd
import pytest

from downstream_check import LeadStep, downstream_report, read_lead_times, staging_buffer_from_lead_times


def test_one_server_queues_the_second_job_and_two_servers_do_not():
    steps = [LeadStep("wrap", minutes_per_pallet=0.0, fixed_minutes=10.0, resource_count=1, order=1)]
    report = downstream_report(
        [
            {"order_id": "B", "finish_minute": 0.0, "deadline_minute": 5.0, "depot_id": "PACK_A"},
            {"order_id": "A", "finish_minute": 0.0, "deadline_minute": 5.0, "depot_id": "PACK_A"},
        ],
        steps,
        staging_buffer=0.0,
    )
    rows = {row["job_id"]: row for row in report["rows"]}
    assert rows["A"]["wrap_exit_minute"] == pytest.approx(10.0)
    assert rows["B"]["wrap_exit_minute"] == pytest.approx(20.0)
    assert rows["A"]["minutes_late_for_departure"] == pytest.approx(5.0)
    assert rows["A"]["departure_minute"] == pytest.approx(5.0)
    assert report["max_wait"]["wrap"] == pytest.approx(10.0)
    assert report["first_miss"]["wrap"]["job_id"] == "A"
    both = downstream_report(
        [
            {"job_id": "B", "finish_minute": 0.0, "deadline_minute": 5.0},
            {"job_id": "A", "finish_minute": 0.0, "deadline_minute": 5.0},
        ],
        [LeadStep("wrap", minutes_per_pallet=0.0, fixed_minutes=10.0, resource_count=2, order=1)],
        staging_buffer=0.0,
    )
    assert both["max_wait"]["wrap"] == pytest.approx(0.0)
    assert {row["wrap_exit_minute"] for row in both["rows"]} == {10.0}


def test_lead_times_use_order_and_a_blank_resource_count_means_one(tmp_path: Path):
    path = tmp_path / "lead_times.csv"
    path.write_text(
        "step,order,fixed_minutes,minutes_per_pallet,resource_count\n"
        "wrapping,2,1,3,\n"
        "checker,1,3,2,1\n",
        encoding="utf-8",
    )
    steps = read_lead_times(path)
    assert [step.step for step in steps] == ["checker", "wrapping"]
    assert steps[1].resource_count == 1
    assert staging_buffer_from_lead_times(path) == pytest.approx(9.0)
    empty = pd.DataFrame([{"order_id": "A", "finish_minute": 0.0, "deadline_minute": 100.0}])
    report = downstream_report(empty.to_dict(orient="records"), steps)
    assert report["first_miss"]["checker"] is None
    assert report["rows"][0]["minutes_late_for_departure"] == pytest.approx(0.0)
    with pytest.raises(ValueError, match="column resource_count"):
        bad = tmp_path / "bad.csv"
        bad.write_text(
            "step,order,fixed_minutes,minutes_per_pallet,resource_count\nmove,1,1,1,0\n",
            encoding="utf-8",
        )
        read_lead_times(bad)
