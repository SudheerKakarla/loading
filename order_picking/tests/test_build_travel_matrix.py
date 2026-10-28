"""Tests for the aisle-centreline travel-time matrix."""

from pathlib import Path

import pandas as pd
import pytest

from build_travel_matrix import (
    assign_nearest_depots,
    assignment_frame,
    build_travel_matrix,
    main,
    travel_matrix_frame,
)
from order_picking_optimizer import route_order

EXAMPLE = Path(__file__).resolve().parents[1] / "inputs" / "layout_example"
SMALL = Path(__file__).resolve().parents[1] / "inputs" / "layout_small"


def _layout(one_way_a1: str = ""):
    aisles = pd.DataFrame(
        [
            {"aisle_id": "A1", "x": 0.0, "y_min": 0.0, "y_max": 10.0, "one_way": one_way_a1},
            {"aisle_id": "A2", "x": 4.0, "y_min": 0.0, "y_max": 10.0, "one_way": ""},
        ]
    )
    crosses = pd.DataFrame(
        [
            {"cross_id": "FRONT", "y": 0.0, "x_min": 0.0, "x_max": 4.0, "one_way": ""},
            {"cross_id": "BACK", "y": 10.0, "x_min": 0.0, "x_max": 4.0, "one_way": ""},
        ]
    )
    return aisles, crosses


def test_same_aisle_and_cross_aisle_use_the_shorter_centreline():
    """Two slots in one aisle use the along-aisle gap. Slots in neighbouring
    aisles use the shorter of the front and back cross-aisles."""
    aisles, crosses = _layout()
    locations = pd.DataFrame(
        [
            {"location_id": "NEAR", "aisle_id": "A1", "y": 3.0},
            {"location_id": "FAR", "aisle_id": "A1", "y": 6.0},
            {"location_id": "OTHER", "aisle_id": "A2", "y": 2.0},
        ]
    )
    matrix = build_travel_matrix(
        aisles, crosses, locations, depot_id="DEPOT", depot_x=0.0, depot_y=0.0, speed=1.0
    )
    assert matrix["NEAR"]["FAR"] == pytest.approx(3.0)
    assert matrix["FAR"]["NEAR"] == pytest.approx(3.0)
    # NEAR y=3 to front (3) + cross (4) + up to OTHER y=2 (2) = 9.
    # Via the back cross: 7 + 4 + 8 = 19.
    assert matrix["NEAR"]["OTHER"] == pytest.approx(9.0)
    assert matrix["DEPOT"]["NEAR"] == pytest.approx(3.0)
    assert matrix["OTHER"]["DEPOT"] == pytest.approx(6.0)


def test_one_way_aisle_forces_the_long_way_back():
    """Walking up A1 is the direct gap. Walking down has to leave through the
    back cross-aisle and return on A2."""
    aisles, crosses = _layout(one_way_a1="up")
    locations = pd.DataFrame(
        [
            {"location_id": "LOW", "aisle_id": "A1", "y": 2.0},
            {"location_id": "HIGH", "aisle_id": "A1", "y": 8.0},
        ]
    )
    matrix = build_travel_matrix(
        aisles, crosses, locations, depot_id="DEPOT", depot_x=0.0, depot_y=0.0, speed=1.0
    )
    assert matrix["LOW"]["HIGH"] == pytest.approx(6.0)
    assert matrix["HIGH"]["LOW"] == pytest.approx(22.0)


def test_speed_and_retrieval_time_match_the_paper_split():
    """Speed turns distance into minutes. Retrieval is added on arrival at a
    slot and not on the return to the depot."""
    aisles, crosses = _layout()
    locations = pd.DataFrame([{"location_id": "L1", "aisle_id": "A1", "y": 4.0}])
    matrix = build_travel_matrix(
        aisles,
        crosses,
        locations,
        depot_id="DEPOT",
        depot_x=0.0,
        depot_y=0.0,
        speed=2.0,
        retrieval_time=0.5,
    )
    assert matrix["DEPOT"]["L1"] == pytest.approx(2.5)
    assert matrix["L1"]["DEPOT"] == pytest.approx(2.0)


def test_unreachable_slot_and_bad_coordinate_raise():
    aisles, crosses = _layout()
    aisles = pd.concat(
        [aisles, pd.DataFrame([{"aisle_id": "A3", "x": 30.0, "y_min": 0.0, "y_max": 10.0}])],
        ignore_index=True,
    )
    stranded = pd.DataFrame([{"location_id": "L3", "aisle_id": "A3", "y": 1.0}])
    with pytest.raises(ValueError, match="No walk connects"):
        build_travel_matrix(
            aisles, crosses, stranded, depot_id="DEPOT", depot_x=0.0, depot_y=0.0
        )
    outside = pd.DataFrame([{"location_id": "L1", "aisle_id": "A1", "y": 12.0}])
    with pytest.raises(ValueError, match="outside aisle"):
        build_travel_matrix(
            aisles, crosses, outside, depot_id="DEPOT", depot_x=0.0, depot_y=0.0
        )


def test_frame_feeds_the_picker_route():
    """The long table is the Phase 1 input. The example layout's shortest
    closed walk is depot to L1 to L2 and back: 3 + 13 + 10 = 26."""
    matrix = build_travel_matrix(
        pd.read_csv(SMALL / "aisles.csv"),
        pd.read_csv(SMALL / "cross_aisles.csv"),
        pd.read_csv(SMALL / "locations.csv"),
        depot_id="DEPOT",
        depot_x=0.0,
        depot_y=0.0,
        speed=1.0,
    )
    frame = travel_matrix_frame(matrix)
    assert list(frame.columns) == ["from_location", "to_location", "travel_time"]
    route = route_order("O1", ["L1", "L2"], "DEPOT", frame, solver="heuristic")
    assert route["route"][0] == "DEPOT"
    assert route["route"][-1] == "DEPOT"
    assert route["route_duration"] == pytest.approx(26.0)


def test_cli_writes_the_lookup_csv(tmp_path):
    out = tmp_path / "travel_times.csv"
    main(
        [
            "--aisles",
            str(SMALL / "aisles.csv"),
            "--cross-aisles",
            str(SMALL / "cross_aisles.csv"),
            "--locations",
            str(SMALL / "locations.csv"),
            "--depot-id",
            "DEPOT",
            "--depot-x",
            "0",
            "--depot-y",
            "0",
            "--speed",
            "1",
            "--out",
            str(out),
        ]
    )
    written = pd.read_csv(out)
    pair = written[(written.from_location == "DEPOT") & (written.to_location == "L1")]
    assert pair.iloc[0].travel_time == pytest.approx(3.0)


def test_slot_retrieval_overrides_the_global_default_and_zero_stays_zero():
    """A filled retrieval_time is the arrival penalty for that slot. A blank
    cell uses the global value. A stored 0 does not fall back to the global."""
    aisles, crosses = _layout()
    locations = pd.DataFrame(
        [
            {"location_id": "L1", "aisle_id": "A1", "y": 4.0, "retrieval_time": 2.0},
            {"location_id": "L2", "aisle_id": "A1", "y": 6.0, "retrieval_time": None},
            {"location_id": "L0", "aisle_id": "A1", "y": 1.0, "retrieval_time": 0.0},
        ]
    )
    matrix = build_travel_matrix(
        aisles,
        crosses,
        locations,
        depot_id="DEPOT",
        depot_x=0.0,
        depot_y=0.0,
        speed=1.0,
        retrieval_time=0.5,
    )
    assert matrix["DEPOT"]["L1"] == pytest.approx(6.0)
    assert matrix["DEPOT"]["L2"] == pytest.approx(6.5)
    assert matrix["DEPOT"]["L0"] == pytest.approx(1.0)
    assert matrix["L1"]["DEPOT"] == pytest.approx(4.0)
    assert matrix["L1"]["L2"] == pytest.approx(2.5)
    assert matrix["L2"]["L1"] == pytest.approx(4.0)


def test_pandas_na_retrieval_uses_the_global_default():
    """A pd.NA retrieval cell is blank, so the global minutes are added."""
    aisles, crosses = _layout()
    locations = pd.DataFrame(
        [{"location_id": "L1", "aisle_id": "A1", "y": 4.0, "retrieval_time": pd.NA}]
    )
    matrix = build_travel_matrix(
        aisles,
        crosses,
        locations,
        depot_id="DEPOT",
        depot_x=0.0,
        depot_y=0.0,
        speed=1.0,
        retrieval_time=0.5,
    )
    assert matrix["DEPOT"]["L1"] == pytest.approx(4.5)


def test_negative_slot_retrieval_raises():
    aisles, crosses = _layout()
    locations = pd.DataFrame(
        [{"location_id": "L1", "aisle_id": "A1", "y": 4.0, "retrieval_time": -1.0}]
    )
    with pytest.raises(ValueError, match="retrieval_time must be >= 0"):
        build_travel_matrix(
            aisles, crosses, locations, depot_id="DEPOT", depot_x=0.0, depot_y=0.0
        )


def test_round_trip_picks_the_depot_a_short_outbound_would_miss():
    """Outbound time alone would pick D_FRONT. The return is long, so the
    there-and-back rule picks D_BACK. A tie uses the smaller depot id."""
    matrix = {
        "D_BACK": {"L": 4.0, "D_FRONT": 5.0, "MID": 3.0},
        "D_FRONT": {"L": 1.0, "D_BACK": 5.0, "MID": 3.0},
        "L": {"D_FRONT": 10.0, "D_BACK": 4.0, "MID": 1.0},
        "MID": {"D_FRONT": 3.0, "D_BACK": 3.0, "L": 1.0},
    }
    assert assign_nearest_depots(matrix, ["D_FRONT", "D_BACK"])["MID"] == "D_BACK"
    assignment = assign_nearest_depots(matrix, ["D_BACK", "D_FRONT"])
    assert assignment["L"] == "D_BACK"
    assert assignment["MID"] == "D_BACK"
    frame = assignment_frame(matrix, assignment)
    mid = frame[frame.location_id == "MID"].iloc[0]
    assert mid.depot_id == "D_BACK"
    assert mid.round_trip_time == pytest.approx(6.0)


def test_two_depots_label_each_slot_and_reject_a_mixed_call():
    """PACK_A sits on A1 and PACK_B on A2. L1's there-and-back is shorter to
    PACK_A; L2's is shorter to PACK_B. One depot and a depots table together
    is an error."""
    aisles, crosses = _layout()
    locations = pd.DataFrame(
        [
            {"location_id": "L1", "aisle_id": "A1", "y": 3.0},
            {"location_id": "L2", "aisle_id": "A2", "y": 6.0},
        ]
    )
    depots = pd.DataFrame(
        [
            {"depot_id": "PACK_A", "x": 0.0, "y": 0.0},
            {"depot_id": "PACK_B", "x": 4.0, "y": 0.0},
        ]
    )
    matrix = build_travel_matrix(aisles, crosses, locations, depots=depots, speed=1.0)
    assert matrix["PACK_A"]["PACK_B"] == pytest.approx(4.0)
    assert matrix["PACK_A"]["L1"] == pytest.approx(3.0)
    assert matrix["PACK_B"]["L2"] == pytest.approx(6.0)
    assignment = assign_nearest_depots(matrix, ["PACK_A", "PACK_B"])
    assert assignment == {"L1": "PACK_A", "L2": "PACK_B"}
    with pytest.raises(ValueError, match="either one depot or a depots table"):
        build_travel_matrix(
            aisles,
            crosses,
            locations,
            depot_id="DEPOT",
            depot_x=0.0,
            depot_y=0.0,
            depots=depots,
        )


def test_cli_writes_nearest_depot_assignments(tmp_path):
    out = tmp_path / "travel_times.csv"
    assigned = tmp_path / "depot_assignment.csv"
    main(
        [
            "--aisles",
            str(SMALL / "aisles.csv"),
            "--cross-aisles",
            str(SMALL / "cross_aisles.csv"),
            "--locations",
            str(SMALL / "locations.csv"),
            "--depots",
            str(SMALL / "depots.csv"),
            "--out",
            str(out),
            "--assignment-out",
            str(assigned),
        ]
    )
    labels = pd.read_csv(assigned).set_index("location_id")
    assert labels.loc["L1", "depot_id"] == "PACK_A"
    assert labels.loc["L2", "depot_id"] == "PACK_B"
    with pytest.raises(SystemExit):
        main(
            [
                "--aisles",
                str(SMALL / "aisles.csv"),
                "--cross-aisles",
                str(SMALL / "cross_aisles.csv"),
                "--locations",
                str(SMALL / "locations.csv"),
                "--depots",
                str(SMALL / "depots.csv"),
                "--depot-id",
                "DEPOT",
                "--depot-x",
                "0",
                "--depot-y",
                "0",
                "--out",
                str(out),
            ]
        )


def test_warehouse_travel_matrix_matches_the_checked_in_table():
    """Depot (0, -2) on the six-aisle map. A3 is one-way up, so the return is the long walk."""
    aisles = pd.read_csv(EXAMPLE / "aisles.csv")
    crosses = pd.read_csv(EXAMPLE / "cross_aisles.csv")
    locations = pd.read_csv(EXAMPLE / "locations.csv")
    matrix = build_travel_matrix(
        aisles, crosses, locations, depot_id="DEPOT", depot_x=0.0, depot_y=-2.0, speed=1.0
    )
    assert matrix["DEPOT"]["L-A1-01"] == pytest.approx(5.0)
    assert matrix["L-A3-01"]["L-A3-04"] == pytest.approx(18.0)
    assert matrix["L-A3-04"]["L-A3-01"] == pytest.approx(50.0)
    checked = pd.read_csv(EXAMPLE / "travel_times.csv")
    built = travel_matrix_frame(matrix)
    merged = checked.merge(built, on=["from_location", "to_location"], suffixes=("_file", "_built"))
    assert len(merged) == len(checked) == len(built)
    assert (merged.travel_time_file - merged.travel_time_built).abs().max() == pytest.approx(0.0)
