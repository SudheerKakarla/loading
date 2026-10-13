"""Geometry of the aisle walk and the shift Gantt packing."""

from pathlib import Path

import pandas as pd
import pytest

from build_travel_matrix import build_travel_matrix, route_legs, route_walks
from plot_picking import gantt_intervals, main, plot_order_route, plot_picker_routes, plot_shift_gantt

EXAMPLE = Path(__file__).resolve().parents[1] / "inputs" / "layout_example"
EXAMPLE_OUT = Path(__file__).resolve().parents[1] / "outputs" / "layout_example"
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
    locations = pd.DataFrame(
        [
            {"location_id": "L1", "aisle_id": "A1", "y": 3.0},
            {"location_id": "L2", "aisle_id": "A2", "y": 6.0},
            {"location_id": "LOW", "aisle_id": "A1", "y": 2.0},
            {"location_id": "HIGH", "aisle_id": "A1", "y": 8.0},
        ]
    )
    return aisles, crosses, locations


def _length(points):
    total = 0.0
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        total += abs(x1 - x0) + abs(y1 - y0)
    return total


def test_l1_to_l2_walks_the_front_cross_aisle():
    """The 13-minute walk goes down A1, across the front, and up A2.
    The back cross-aisle is the longer way around."""
    aisles, crosses, locations = _layout()
    legs = route_walks(
        aisles, crosses, locations, ["DEPOT", "L1", "L2", "DEPOT"],
        depot_id="DEPOT", depot_x=0.0, depot_y=0.0,
    )
    assert legs[0][0] == (0.0, 0.0)
    assert legs[0][-1] == (0.0, 3.0)
    assert _length(legs[0]) == pytest.approx(3.0)
    assert legs[1][0] == (0.0, 3.0)
    assert legs[1][-1] == (4.0, 6.0)
    assert (4.0, 0.0) in legs[1]
    assert _length(legs[1]) == pytest.approx(13.0)
    assert max(point[1] for point in legs[1]) == pytest.approx(6.0)


def test_one_way_return_leaves_by_the_back():
    """A1 allows only increasing y, so HIGH back to LOW uses the back cross-aisle."""
    aisles, crosses, locations = _layout(one_way_a1="up")
    legs = route_walks(
        aisles, crosses, locations, ["HIGH", "LOW"],
        depot_id="DEPOT", depot_x=0.0, depot_y=0.0,
    )
    assert max(point[1] for point in legs[0]) == pytest.approx(10.0)


def test_drawn_length_matches_the_matrix_on_and_off_the_centreline():
    """Sample depot (0, 0) is on aisle A1, so DEPOT→L1 is length 3 and the
    matrix cell is 3. A depot at (-1, 0) adds a segment of length 1 onto the
    centreline, then the same aisle walk of 3, and the matrix cell is 4.
    Retrieval stays off the picture."""
    aisles, crosses, locations = _layout()
    on_line = route_walks(
        aisles, crosses, locations, ["DEPOT", "L1"],
        depot_id="DEPOT", depot_x=0.0, depot_y=0.0,
    )
    on_matrix = build_travel_matrix(
        aisles, crosses, locations, depot_id="DEPOT", depot_x=0.0, depot_y=0.0,
    )
    assert _length(on_line[0]) == pytest.approx(3.0)
    assert on_matrix["DEPOT"]["L1"] == pytest.approx(3.0)

    off_line = route_walks(
        aisles, crosses, locations, ["DEPOT", "L1"],
        depot_id="DEPOT", depot_x=-1.0, depot_y=0.0,
    )
    off_matrix = build_travel_matrix(
        aisles, crosses, locations, depot_id="DEPOT", depot_x=-1.0, depot_y=0.0, retrieval_time=0.0,
    )
    assert off_line[0][0] == (-1.0, 0.0)
    assert (0.0, 0.0) in off_line[0]
    assert _length(off_line[0]) == pytest.approx(4.0)
    assert off_matrix["DEPOT"]["L1"] == pytest.approx(4.0)


def test_missing_walk_is_a_dashed_red_segment():
    """A slot on an aisle the cross-aisles never reach has no walk.
    The picture keeps going and marks that step instead of raising."""
    aisles, crosses, locations = _layout()
    aisles = pd.concat(
        [aisles, pd.DataFrame([{"aisle_id": "A3", "x": 30.0, "y_min": 0.0, "y_max": 10.0, "one_way": ""}])],
        ignore_index=True,
    )
    locations = pd.concat(
        [locations, pd.DataFrame([{"location_id": "FAR", "aisle_id": "A3", "y": 4.0}])],
        ignore_index=True,
    )
    with pytest.raises(ValueError, match="No walk connects"):
        route_walks(
            aisles, crosses, locations, ["DEPOT", "FAR"],
            depot_id="DEPOT", depot_x=0.0, depot_y=0.0,
        )
    noted = route_legs(
        aisles, crosses, locations, ["DEPOT", "FAR"],
        depot_id="DEPOT", depot_x=0.0, depot_y=0.0,
    )
    assert noted[0].points is None
    assert noted[0].ends == ((0.0, 0.0), (30.0, 4.0))
    fig = plot_order_route(
        aisles, crosses, locations, ["DEPOT", "FAR"],
        depot_id="DEPOT", depot_x=0.0, depot_y=0.0,
    )
    dashed = [line for line in fig.axes[0].lines if line.get_gid() == "no-walk"]
    assert len(dashed) == 1
    assert dashed[0].get_linestyle() == "--"
    fig.clf()


def test_gantt_caption_says_the_order_is_display_only():
    result = {
        "picker_assignments": {"P1": ["O1"]},
        "routes": {"O1": {"route_duration": 26.0}},
        "orders_not_fulfilled": [],
    }
    pickers = pd.DataFrame([{"picker_id": "P1", "shift_duration": 40.0}])
    fig = plot_shift_gantt(result, pickers)
    xlabel = fig.axes[0].get_xlabel()
    assert "display" in xlabel or "does not sequence" in xlabel
    assert "Phase 2 does not sequence" in xlabel
    fig.clf()


def test_off_centre_depot_starts_at_the_physical_point():
    aisles, crosses, locations = _layout()
    legs = route_walks(
        aisles, crosses, locations, ["DEPOT", "L1"],
        depot_id="DEPOT", depot_x=-1.0, depot_y=0.0,
    )
    assert legs[0][0] == (-1.0, 0.0)
    assert legs[0][1] == (0.0, 0.0)
    assert legs[0][-1] == (0.0, 3.0)


def test_gantt_rejects_non_finite_duration():
    with pytest.raises(ValueError, match="finite"):
        gantt_intervals({"P1": ["O1"]}, {"O1": float("nan")})
    with pytest.raises(ValueError, match="finite"):
        gantt_intervals({"P1": ["O1"]}, {"O1": float("inf")})
    with pytest.raises(ValueError, match="finite"):
        gantt_intervals({"P1": ["O1"]}, {"O1": float("-inf")})


def test_gantt_packs_from_zero_in_listed_order():
    """Phase 2 does not sequence the shift. Bars follow the assignment list."""
    intervals = gantt_intervals(
        {"P1": ["BIG", "SMALL"]},
        {"BIG": 9.0, "SMALL": 4.0},
    )
    assert intervals["P1"] == [("BIG", 0.0, 9.0), ("SMALL", 9.0, 13.0)]


def test_route_and_gantt_write_pngs(tmp_path):
    aisles, crosses, locations = _layout()
    route_png = tmp_path / "route.png"
    fig = plot_order_route(
        aisles, crosses, locations, ["DEPOT", "L1", "L2", "DEPOT"],
        depot_id="DEPOT", depot_x=0.0, depot_y=0.0, out=route_png,
    )
    assert route_png.stat().st_size > 0
    fig.clf()

    result = {
        "picker_assignments": {"P1": ["O1"]},
        "routes": {"O1": {"route_duration": 26.0}},
        "orders_not_fulfilled": [{"order_id": "O2", "reason": "shift_time_limit_or_priority_tradeoff"}],
    }
    pickers = pd.DataFrame([{"picker_id": "P1", "shift_duration": 40.0}])
    gantt_png = tmp_path / "gantt.png"
    gantt = plot_shift_gantt(result, pickers, out=gantt_png)
    assert gantt_png.stat().st_size > 0
    gantt.clf()


def test_cli_writes_the_sample_route(tmp_path):
    out = tmp_path / "sample.png"
    main(
        [
            "route",
            "--aisles", str(SMALL / "aisles.csv"),
            "--cross-aisles", str(SMALL / "cross_aisles.csv"),
            "--locations", str(SMALL / "locations.csv"),
            "--depot-id", "DEPOT",
            "--depot-x", "0",
            "--depot-y", "0",
            "--route", "DEPOT", "L1", "L2", "DEPOT",
            "--out", str(out),
        ]
    )
    assert out.stat().st_size > 0


def test_picker_routes_draw_each_order_on_its_own_panel():
    """Two pickers, two depot trips. Each panel names its picker and its order."""
    aisles, crosses, locations = _layout()
    result = {
        "picker_assignments": {"P1": ["O1"], "P2": ["O2"]},
        "routes": {
            "O1": {"route": ["DEPOT", "L1", "DEPOT"]},
            "O2": {"route": ["DEPOT", "L2", "DEPOT"]},
        },
        "orders_not_fulfilled": [
            {"order_id": "O9", "reason": "shift_time_limit_or_priority_tradeoff"},
        ],
    }
    fig = plot_picker_routes(
        aisles, crosses, locations, result,
        depot_id="DEPOT", depot_x=0.0, depot_y=0.0,
    )
    assert len(fig.axes) == 2
    titles = [ax.get_title() for ax in fig.axes]
    assert any("P1" in title and "O1" in title for title in titles)
    assert any("P2" in title and "O2" in title for title in titles)
    gids = [
        artist.get_gid()
        for ax in fig.axes
        for artist in (*ax.patches, *ax.lines)
        if artist.get_gid()
    ]
    assert "walk-P1-O1" in gids
    assert "walk-P2-O2" in gids
    footer = " ".join(text.get_text() for text in fig.texts)
    assert "separate trip" in footer
    assert "O9" in footer
    fig.clf()


def test_picker_routes_require_a_route_for_every_assigned_order():
    aisles, crosses, locations = _layout()
    result = {"picker_assignments": {"P1": ["O1"]}, "routes": {}}
    with pytest.raises(ValueError, match="O1"):
        plot_picker_routes(
            aisles, crosses, locations, result,
            depot_id="DEPOT", depot_x=0.0, depot_y=0.0,
        )


def test_cli_writes_the_sample_floor(tmp_path):
    """The two-aisle map solves and writes one floor picture. Depot is (0, 0)."""
    out = tmp_path / "floor.png"
    main(
        [
            "floor",
            "--aisles", str(SMALL / "aisles.csv"),
            "--cross-aisles", str(SMALL / "cross_aisles.csv"),
            "--locations", str(SMALL / "locations.csv"),
            "--orders", str(SMALL / "orders.csv"),
            "--items", str(SMALL / "order_items.csv"),
            "--pickers", str(SMALL / "pickers.csv"),
            "--travel", str(SMALL / "travel_times_depot.csv"),
            "--depot-id", "DEPOT",
            "--depot-x", "0",
            "--depot-y", "0",
            "--out", str(out),
        ]
    )
    assert out.stat().st_size > 0


def test_cli_writes_the_sample_gantt(tmp_path):
    """The Gantt subcommand has no depot flags. The sample shift still writes a PNG."""
    out = tmp_path / "gantt.png"
    main(
        [
            "gantt",
            "--pickers", str(EXAMPLE / "pickers.csv"),
            "--assignments", str(EXAMPLE_OUT / "assignments.csv"),
            "--out", str(out),
        ]
    )
    assert out.stat().st_size > 0
