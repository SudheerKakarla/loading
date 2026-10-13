"""Set-partitioning chunker: HiGHS covers each truck, and rejected loads stay out of the model."""

from pathlib import Path

import pandas as pd
import pytest

from chunk_model import (
    ChunkModelSettings,
    CoverCandidate,
    PickerSpec,
    _selected_from_values,
    chunk_loads_model,
    failure_reason,
    main,
    solve_cover,
)


def _crew() -> list:
    return [
        PickerSpec("PJ", "pallet_jack", 2500.0, 2.0),
        PickerSpec("FK", "forklift", 5000.0, 4.0),
    ]


def _settings(**overrides) -> ChunkModelSettings:
    values = dict(job_overhead=0.0, default_pick_time=0.0, max_job_lines=3)
    values.update(overrides)
    return ChunkModelSettings(**values)


def _line(item: str, location: str, weight: float, **overrides) -> dict:
    row = {
        "truck_id": "TRK",
        "depot_id": "DEPOT",
        "destination_id": "STOP",
        "source_order_id": "O1",
        "item_id": item,
        "location_id": location,
        "weight": weight,
        "volume": 0.1,
    }
    row.update(overrides)
    return row


def _axis(points: dict) -> pd.DataFrame:
    rows = []
    for origin, origin_x in points.items():
        for dest, dest_x in points.items():
            if origin == dest:
                continue
            rows.append(
                {
                    "from_location": origin,
                    "to_location": dest,
                    "travel_time": abs(origin_x - dest_x),
                }
            )
    return pd.DataFrame(rows)


def _groups(items: pd.DataFrame) -> set:
    return {frozenset(group.item_id) for _, group in items.groupby("order_id")}


def test_cover_picks_the_shorter_pair_and_leaves_each_line_in_one_job():
    """Three stops on a line. The minimum cover is {B, C} plus {A}, at 22 minutes."""
    loads = pd.DataFrame(
        [
            _line("A", "A", 10.0),
            _line("B", "B", 10.0),
            _line("C", "C", 10.0),
        ]
    )
    travel = _axis({"DEPOT": 0.0, "A": 1.0, "B": 2.0, "C": 10.0})
    result = chunk_loads_model(loads, travel, _crew(), _settings(max_job_lines=2))
    assert result.rejected.empty
    assert result.objective == pytest.approx(22.0)
    assert _groups(result.items) == {frozenset({"A"}), frozenset({"B", "C"})}
    assert set(result.items.item_id) == {"A", "B", "C"}
    assert result.items.groupby("order_id").item_id.count().max() == 2


def test_directed_tour_uses_the_short_direction():
    """Held-Karp must take D-A-B-D at cost 3, not the reverse at cost 30."""
    loads = pd.DataFrame([_line("A", "A", 10.0), _line("B", "B", 10.0)])
    travel = pd.DataFrame(
        [
            {"from_location": "DEPOT", "to_location": "A", "travel_time": 1.0},
            {"from_location": "A", "to_location": "B", "travel_time": 1.0},
            {"from_location": "B", "to_location": "DEPOT", "travel_time": 1.0},
            {"from_location": "DEPOT", "to_location": "B", "travel_time": 10.0},
            {"from_location": "B", "to_location": "A", "travel_time": 10.0},
            {"from_location": "A", "to_location": "DEPOT", "travel_time": 10.0},
        ]
    )
    result = chunk_loads_model(loads, travel, _crew(), _settings())
    assert len(result.orders) == 1
    assert result.objective == pytest.approx(3.0)
    assert result.jobs.walk_minutes.iloc[0] == pytest.approx(3.0)


def test_two_source_orders_at_one_stop_share_a_job_when_that_tour_is_shorter():
    loads = pd.DataFrame(
        [
            _line("A", "A", 10.0, source_order_id="O1"),
            _line("B", "B", 10.0, source_order_id="O2"),
        ]
    )
    travel = _axis({"DEPOT": 0.0, "A": 1.0, "B": 2.0})
    result = chunk_loads_model(loads, travel, _crew(), _settings())
    assert len(result.orders) == 1
    assert result.objective == pytest.approx(4.0)
    assert set(result.items.item_id) == {"A", "B"}


def test_orders_at_different_stops_stay_apart_when_destination_grouping_is_on():
    loads = pd.DataFrame(
        [
            _line("A", "A", 10.0, source_order_id="O1", destination_id="STOP-1"),
            _line("B", "B", 10.0, source_order_id="O2", destination_id="STOP-2"),
        ]
    )
    travel = _axis({"DEPOT": 0.0, "A": 1.0, "B": 2.0})
    result = chunk_loads_model(loads, travel, _crew(), _settings(same_stop=True))
    assert len(result.orders) == 2
    assert result.objective == pytest.approx(6.0)


def test_close_orders_on_different_stops_share_a_job():
    """The default joins these stops. The joint tour costs 4 and the separate tours cost 6."""
    loads = pd.DataFrame(
        [
            _line("A", "A", 10.0, source_order_id="O1", destination_id="STOP-1"),
            _line("B", "B", 10.0, source_order_id="O2", destination_id="STOP-2"),
        ]
    )
    travel = _axis({"DEPOT": 0.0, "A": 1.0, "B": 2.0})
    result = chunk_loads_model(loads, travel, _crew(), _settings())
    assert len(result.orders) == 1
    assert result.objective == pytest.approx(4.0)
    assert set(result.items.item_id) == {"A", "B"}
    assert len(result.solves) == 1
    assert result.solves[0].fallback is False
    assert result.solves[0].mip_gap == pytest.approx(0.0)


def test_a_fitting_order_stays_whole_when_two_trips_would_walk_less():
    """A and B are one order. Separate tours cost 4. The joint tour costs 102."""
    loads = pd.DataFrame([_line("A", "A", 10.0), _line("B", "B", 10.0)])
    travel = pd.DataFrame(
        [
            {"from_location": "DEPOT", "to_location": "A", "travel_time": 1.0},
            {"from_location": "A", "to_location": "DEPOT", "travel_time": 1.0},
            {"from_location": "DEPOT", "to_location": "B", "travel_time": 1.0},
            {"from_location": "B", "to_location": "DEPOT", "travel_time": 1.0},
            {"from_location": "A", "to_location": "B", "travel_time": 100.0},
            {"from_location": "B", "to_location": "A", "travel_time": 100.0},
        ]
    )
    result = chunk_loads_model(loads, travel, _crew(), _settings())
    assert len(result.orders) == 1
    assert result.objective == pytest.approx(102.0)


def test_no_highs_cover_falls_back_to_savings(monkeypatch):
    monkeypatch.setattr("chunk_model.solve_cover", lambda *args, **kwargs: None)
    loads = pd.DataFrame(
        [
            _line("A", "A", 10.0, source_order_id="O1"),
            _line("B", "B", 10.0, source_order_id="O2"),
        ]
    )
    travel = _axis({"DEPOT": 0.0, "A": 1.0, "B": 2.0})
    result = chunk_loads_model(loads, travel, _crew(), _settings())
    assert result.fallback_depots == (("TRK", "DEPOT"),)
    assert result.solves[0].fallback is True
    assert result.solves[0].mip_gap is None
    assert set(result.items.item_id) == {"A", "B"}
    assert len(result.orders) == 1


def test_a_missing_highs_vector_is_not_a_cover():
    candidates = [CoverCandidate("only", ("L1",), 1.0, False)]
    assert _selected_from_values(candidates, None, ["L1"], []) is None
    chosen = _selected_from_values(candidates, [1.0], ["L1"], [])
    assert chosen is not None and chosen[0].candidate_id == "only"


def test_order_heavier_than_every_picker_is_rejected_without_a_job():
    """Each 3000 kg line fits the forklift. The 6000 kg order does not, so it is not split."""
    loads = pd.DataFrame(
        [
            _line("A", "A", 3000.0, volume=0.5),
            _line("B", "B", 3000.0, volume=0.5),
        ]
    )
    result = chunk_loads_model(loads, _axis({"DEPOT": 0.0, "A": 1.0, "B": 2.0}), _crew(), _settings())
    assert result.orders.empty
    assert list(result.rejected.record_type) == ["order"]
    assert result.rejected.reason.iloc[0] == "exceeds_max_weight"
    assert result.rejected.record_id.iloc[0] == "O1"


def test_weight_and_volume_must_fit_the_same_picker():
    """2000 kg is under one picker and 2.0 m3 is under the other. Neither picker can take both."""
    crossed = [
        PickerSpec("HEAVY", "forklift", 5000.0, 1.0),
        PickerSpec("BULKY", "pallet_jack", 1000.0, 4.0),
    ]
    assert failure_reason(2000.0, 2.0, crossed) == "no_single_picker"
    loads = pd.DataFrame([_line("A", "A", 2000.0, volume=2.0)])
    result = chunk_loads_model(loads, _axis({"DEPOT": 0.0, "A": 1.0}), crossed, _settings())
    assert result.orders.empty
    assert result.rejected.reason.iloc[0] == "no_single_picker"
    assert result.rejected.record_type.iloc[0] == "line"


def test_order_that_splits_across_two_pickers_capabilities_is_rejected():
    """1500 kg and 0.4 m3 fit each machine. The order's 3000 kg and 0.8 m3 fit neither."""
    crossed = [
        PickerSpec("HEAVY", "forklift", 5000.0, 0.5),
        PickerSpec("BULKY", "pallet_jack", 2500.0, 4.0),
    ]
    loads = pd.DataFrame(
        [
            _line("A", "A", 1500.0, volume=0.4),
            _line("B", "B", 1500.0, volume=0.4),
        ]
    )
    result = chunk_loads_model(loads, _axis({"DEPOT": 0.0, "A": 1.0, "B": 2.0}), crossed, _settings())
    assert result.orders.empty
    assert list(result.rejected.record_type) == ["order"]
    assert result.rejected.reason.iloc[0] == "no_single_picker"


def test_forklift_only_line_is_a_job_and_names_the_forklift():
    loads = pd.DataFrame([_line("A", "A", 3000.0, volume=1.0)])
    result = chunk_loads_model(loads, _axis({"DEPOT": 0.0, "A": 1.0}), _crew(), _settings())
    assert len(result.orders) == 1
    assert result.orders.required_equipment.iloc[0] == "forklift"
    assert result.rejected.empty


def test_must_ship_line_is_covered_once_inside_the_cheaper_priority_job():
    loads = pd.DataFrame(
        [
            _line("A", "A", 10.0, must_ship="true"),
            _line("B", "B", 10.0),
        ]
    )
    travel = _axis({"DEPOT": 0.0, "A": 1.0, "B": 2.0})
    result = chunk_loads_model(loads, travel, _crew(), _settings(job_overhead=5.0))
    assert len(result.orders) == 1
    assert result.objective == pytest.approx(9.0)
    assert result.orders.priority_weight.iloc[0] == pytest.approx(100000.0)
    assert set(result.items.item_id) == {"A", "B"}


def test_priority_line_cap_keeps_the_must_ship_line_in_its_own_job():
    loads = pd.DataFrame(
        [
            _line("A", "A", 10.0, must_ship="true"),
            _line("B", "B", 10.0),
        ]
    )
    travel = _axis({"DEPOT": 0.0, "A": 1.0, "B": 2.0})
    result = chunk_loads_model(
        loads, travel, _crew(), _settings(job_overhead=5.0, priority_max_lines=1)
    )
    assert len(result.orders) == 2
    must = result.orders[result.orders.priority_weight == 100000.0]
    assert len(must) == 1
    must_items = result.items[result.items.order_id == must.order_id.iloc[0]]
    assert list(must_items.item_id) == ["A"]


def test_non_priority_copy_of_a_must_ship_line_is_not_selected():
    """The cheap group also contains L1. C1 and C2 together leave it off."""
    candidates = [
        CoverCandidate("priority", ("L1",), 100.0, True),
        CoverCandidate("cheap", ("L1", "L2"), 1.0, False),
        CoverCandidate("other", ("L2",), 10.0, False),
    ]
    solved = solve_cover(candidates, ["L1", "L2"], ["L1"])
    assert solved is not None
    assert [candidate.candidate_id for candidate in solved.jobs] == ["priority", "other"]
    assert solved.objective == pytest.approx(110.0)
    assert solved.mip_gap == pytest.approx(0.0)


def test_missing_volume_names_the_ignore_flag():
    loads = pd.DataFrame([_line("A", "A", 10.0, volume=None)])
    travel = _axis({"DEPOT": 0.0, "A": 1.0})
    with pytest.raises(ValueError, match="--ignore-volume"):
        chunk_loads_model(loads, travel, _crew(), _settings())


def test_cli_writes_jobs_and_rejected_rows(tmp_path: Path):
    loads = tmp_path / "loads.csv"
    travel = tmp_path / "travel.csv"
    pickers = tmp_path / "pickers.csv"
    pd.DataFrame([_line("A", "A", 10.0)]).to_csv(loads, index=False)
    _axis({"DEPOT": 0.0, "A": 1.0}).to_csv(travel, index=False)
    pd.DataFrame(
        [
            {
                "picker_id": "PJ",
                "equipment_type": "pallet_jack",
                "max_weight": 2500.0,
                "max_volume": 2.0,
            }
        ]
    ).to_csv(pickers, index=False)
    out = tmp_path / "jobs"
    main(["--loads", str(loads), "--travel", str(travel), "--pickers", str(pickers), "--out", str(out)])
    assert (out / "orders.csv").exists()
    assert (out / "rejected.csv").exists()
    assert (out / "solve.csv").exists()
    solve = pd.read_csv(out / "solve.csv")
    assert list(solve.columns) == ["truck_id", "depot_id", "minutes", "mip_gap", "fallback"]
    assert len(solve) == 1
    assert solve.mip_gap.iloc[0] == pytest.approx(0.0)
    assert bool(solve.fallback.iloc[0]) is False
    assert len(pd.read_csv(out / "orders.csv")) == 1
