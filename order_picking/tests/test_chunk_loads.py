"""Chunker: truck lines become picker jobs. LoadMax is not an input."""

from pathlib import Path

import pandas as pd
import pytest

from build_truck_loads import write_truck_loads
from chunk_loads import ChunkSettings, chunk_loads, main, write_chunk_outputs
from order_picking_optimizer import compute_priority_weight, route_orders


def _settings(**overrides) -> ChunkSettings:
    values = dict(max_job_weight=500.0, job_overhead=0.0, default_pick_time=0.0)
    values.update(overrides)
    return ChunkSettings(**values)


def _loads(rows) -> pd.DataFrame:
    return pd.DataFrame(rows)


def _pair(origin, dest, minutes) -> dict:
    return {"from_location": origin, "to_location": dest, "travel_time": minutes}


def _star(depot, locations, minutes=1.0) -> pd.DataFrame:
    rows = []
    for location in locations:
        rows.append(_pair(depot, location, minutes))
        rows.append(_pair(location, depot, minutes))
    return pd.DataFrame(rows)


def _complete(nodes, minutes=1.0) -> pd.DataFrame:
    rows = []
    for origin in nodes:
        for dest in nodes:
            if origin != dest:
                rows.append(_pair(origin, dest, minutes))
    return pd.DataFrame(rows)


def _manhattan(depot, points) -> pd.DataFrame:
    nodes = {depot: (0.0, 0.0)}
    nodes.update(points)
    rows = []
    for origin, (ox, oy) in nodes.items():
        for dest, (dx, dy) in nodes.items():
            if origin == dest:
                continue
            rows.append(_pair(origin, dest, abs(ox - dx) + abs(oy - dy)))
    return pd.DataFrame(rows)


def test_destination_group_stays_together_and_the_oversize_group_splits():
    """T7. DEST_A fits in one job. DEST_B is over the cap and splits. Nothing mixes."""
    rows = []
    for index in range(4):
        rows.append(
            {
                "truck_id": "TRK1",
                "depot_id": "DEPOT",
                "destination_id": "DEST_A",
                "item_id": f"A{index}",
                "location_id": f"A{index}",
                "weight": 100.0,
            }
        )
    for index in range(3):
        rows.append(
            {
                "truck_id": "TRK1",
                "depot_id": "DEPOT",
                "destination_id": "DEST_B",
                "item_id": f"B{index}",
                "location_id": f"B{index}",
                "weight": 200.0,
            }
        )
    locations = ["DEPOT"] + [f"A{index}" for index in range(4)] + [f"B{index}" for index in range(3)]
    result = chunk_loads(
        _loads(rows),
        _complete(locations),
        _settings(max_job_weight=500.0, require_destination_grouping=True),
    )
    sets = [set(group.location_id) for _, group in result.items.groupby("order_id")]
    assert {"A0", "A1", "A2", "A3"} in sets
    dest_b = [locations for locations in sets if locations <= {"B0", "B1", "B2"}]
    assert len(dest_b) >= 2
    assert set().union(*dest_b) == {"B0", "B1", "B2"}
    assert result.orders["total_weight"].max() <= 500.0
    assert sum(len(group) for _, group in result.items.groupby("order_id")) == 7


def test_savings_keeps_each_side_of_the_depot_together():
    """T14. Three lines on each side, 300 kg cap. Walking is 26 per side."""
    points = {
        "L1": (10.0, 1.0),
        "L2": (10.0, 2.0),
        "L3": (10.0, 3.0),
        "L4": (-10.0, 1.0),
        "L5": (-10.0, 2.0),
        "L6": (-10.0, 3.0),
    }
    rows = [
        {
            "truck_id": "TRK1",
            "depot_id": "DEPOT",
            "item_id": name,
            "location_id": name,
            "weight": 100.0,
            "pick_time": 0.0,
        }
        for name in points
    ]
    travel = _manhattan("DEPOT", points)
    result = chunk_loads(_loads(rows), travel, _settings(max_job_weight=300.0))
    sets = {frozenset(group.location_id) for _, group in result.items.groupby("order_id")}
    assert sets == {frozenset({"L1", "L2", "L3"}), frozenset({"L4", "L5", "L6"})}
    assert result.jobs["walk_minutes"].sum() == pytest.approx(52.0)
    routes = route_orders(result.items, "DEPOT", travel)
    assert routes["TRK1-J01"]["route_duration"] == pytest.approx(26.0)
    assert routes["TRK1-J02"]["route_duration"] == pytest.approx(26.0)


def test_chunk_invariant_value_does_not_depend_on_how_many_jobs():
    """T15. Twenty lines at base weight 1 are worth 2.0 as two jobs or as four."""
    rows = [
        {
            "truck_id": "TRK1",
            "depot_id": "DEPOT",
            "item_id": f"I{index}",
            "location_id": "L1",
            "weight": 100.0,
            "base_weight": 1.0,
        }
        for index in range(20)
    ]
    travel = _star("DEPOT", ["L1"])
    two = chunk_loads(_loads(rows), travel, _settings(max_job_weight=1000.0, reference_order_lines=10.0))
    four = chunk_loads(_loads(rows), travel, _settings(max_job_weight=500.0, reference_order_lines=10.0))
    assert len(two.orders) == 2
    assert len(four.orders) == 4
    assert two.orders["priority_weight"].sum() == pytest.approx(2.0)
    assert four.orders["priority_weight"].sum() == pytest.approx(2.0)
    kept = compute_priority_weight(two.orders)
    assert list(kept["priority_weight"]) == list(two.orders["priority_weight"])


def test_one_line_over_the_cap_is_still_a_job():
    result = chunk_loads(
        _loads(
            [
                {
                    "truck_id": "TRK1",
                    "depot_id": "DEPOT",
                    "item_id": "HEAVY",
                    "location_id": "L1",
                    "weight": 800.0,
                }
            ]
        ),
        _star("DEPOT", ["L1"]),
        _settings(max_job_weight=500.0),
    )
    assert list(result.orders["order_id"]) == ["TRK1-J01"]
    assert bool(result.jobs.iloc[0]["exceeds_capacity"]) is True
    assert result.orders.iloc[0]["required_equipment"] == ""


def test_must_ship_line_sets_the_unscaled_weight():
    rows = [
        {
            "truck_id": "TRK1",
            "depot_id": "DEPOT",
            "item_id": name,
            "location_id": "L1",
            "weight": 10.0,
            "must_ship": name == "M",
            "base_weight": 1.0,
        }
        for name in ("M", "N")
    ]
    result = chunk_loads(_loads(rows), _star("DEPOT", ["L1"]), _settings(max_job_weight=100.0))
    assert len(result.orders) == 1
    assert result.orders.iloc[0]["priority_weight"] == pytest.approx(100000.0)
    assert result.orders.iloc[0]["must_ship"] == "true"


def test_soft_deadline_keeps_urgency_neutral_and_stores_the_cutoff():
    result = chunk_loads(
        _loads(
            [
                {
                    "truck_id": "TRK1",
                    "depot_id": "DEPOT",
                    "item_id": "I1",
                    "location_id": "L1",
                    "weight": 10.0,
                    "departure_time": 90.0,
                }
            ]
        ),
        _star("DEPOT", ["L1"]),
        _settings(deadline_mode="soft", staging_buffer=30.0, reference_order_lines=10.0),
    )
    assert result.orders.iloc[0]["cutoff_time"] == pytest.approx(60.0)
    assert result.orders.iloc[0]["priority_weight"] == pytest.approx(0.1)


def test_each_depot_gets_its_own_picker_folder(tmp_path: Path):
    rows = [
        {
            "truck_id": "TRK1",
            "depot_id": depot,
            "item_id": "I1",
            "location_id": "L1",
            "weight": 10.0,
        }
        for depot in ("PACK_A", "PACK_B")
    ]
    travel = pd.concat(
        [_star("PACK_A", ["L1"]), _star("PACK_B", ["L1"])],
        ignore_index=True,
    )
    result = chunk_loads(_loads(rows), travel, _settings())
    write_chunk_outputs(result, tmp_path)
    assert set(pd.read_csv(tmp_path / "orders.csv")["depot_id"]) == {"PACK_A", "PACK_B"}
    pack_a = pd.read_csv(tmp_path / "by_depot" / "PACK_A" / "orders.csv")
    pack_a_items = pd.read_csv(tmp_path / "by_depot" / "PACK_A" / "order_items.csv")
    assert list(pack_a["depot_id"]) == ["PACK_A"]
    assert set(pack_a_items["order_id"]) == set(pack_a["order_id"])


def test_missing_travel_leg_is_an_error():
    with pytest.raises(ValueError, match="L1 -> DEPOT"):
        chunk_loads(
            _loads(
                [
                    {
                        "truck_id": "TRK1",
                        "depot_id": "DEPOT",
                        "item_id": "I1",
                        "location_id": "L1",
                        "weight": 10.0,
                    }
                ]
            ),
            pd.DataFrame([_pair("DEPOT", "L1", 1.0)]),
            _settings(),
        )


def test_cli_writes_the_picker_files(tmp_path: Path):
    loads = tmp_path / "loads.csv"
    travel = tmp_path / "travel.csv"
    _loads(
        [
            {
                "truck_id": "TRK1",
                "depot_id": "DEPOT",
                "item_id": "I1",
                "location_id": "L1",
                "weight": 10.0,
            }
        ]
    ).to_csv(loads, index=False)
    _star("DEPOT", ["L1"]).to_csv(travel, index=False)
    out = tmp_path / "jobs"
    main(["--loads", str(loads), "--travel", str(travel), "--max-job-weight", "500", "--out", str(out)])
    written = pd.read_csv(out / "orders.csv")
    assert list(written["order_id"]) == ["TRK1-J01"]
    assert pd.read_csv(out / "by_depot" / "DEPOT" / "order_items.csv").iloc[0]["location_id"] == "L1"


def test_warehouse_truck_loads_keep_each_source_order_and_can_be_chunked(tmp_path: Path):
    root = Path(__file__).resolve().parents[1]
    fresh_path = tmp_path / "truck_loads.csv"
    write_truck_loads(fresh_path)
    fresh = pd.read_csv(fresh_path)
    checked = pd.read_csv(root / "inputs" / "chunk_warehouse" / "truck_loads.csv")
    pd.testing.assert_frame_equal(fresh, checked)
    orders = pd.read_csv(root / "inputs" / "layout_warehouse" / "orders.csv")
    summed = fresh.groupby("source_order_id")["weight"].sum()
    assert set(summed.index) == set(orders["order_id"])
    paired = orders.set_index("order_id")["total_weight"]
    assert (summed - paired).abs().max() <= 0.02
    assert fresh.groupby("truck_id")["depot_id"].nunique().max() == 1
    assert fresh.groupby("truck_id")["departure_time"].nunique().max() == 1
    assert fresh["volume"].notna().all()
    assert fresh.groupby("truck_id")["weight"].sum().max() > 2500
    truck = fresh[fresh["truck_id"] == fresh["truck_id"].iloc[0]]
    travel = pd.read_csv(root / "inputs" / "layout_warehouse" / "travel_times.csv")
    jobs = chunk_loads(truck, travel, _settings(max_job_weight=2500.0, max_job_volume=2.0))
    assert len(jobs.orders) < len(truck)
    assert set(jobs.items["location_id"]) == set(truck["location_id"])
