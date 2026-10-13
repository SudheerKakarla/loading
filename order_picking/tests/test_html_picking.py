"""Browser pages for the warehouse walks and the assignment chart."""

import json
from pathlib import Path

import pandas as pd
import pytest

from html_picking import picking_view, write_picking_pages
from plot_picking import main

SMALL = Path(__file__).resolve().parents[1] / "inputs" / "layout_small"


def _layout():
    aisles = pd.DataFrame(
        [
            {"aisle_id": "A1", "x": 0.0, "y_min": 0.0, "y_max": 10.0, "one_way": ""},
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
        ]
    )
    return aisles, crosses, locations


def _pickers():
    return pd.DataFrame(
        [
            {"picker_id": "P1", "shift_duration": 480.0},
            {"picker_id": "P2", "shift_duration": 480.0},
        ]
    )


def _result():
    return {
        "picker_assignments": {"P1": ["O1"], "P2": ["O2"]},
        "routes": {
            "O1": {"route": ["DEPOT", "L1", "DEPOT"], "route_duration": 26.0},
            "O2": {"route": ["DEPOT", "L2", "DEPOT"], "route_duration": 6.0},
        },
        "orders_not_fulfilled": [
            {"order_id": "O9", "reason": "shift_time_limit_or_priority_tradeoff"},
        ],
    }


def _embedded(folder) -> dict:
    index = json.loads((folder / "scenario_index.json").read_text(encoding="utf-8"))
    scenario_id = index[-1]["id"]
    text = (folder / "scenarios" / f"{scenario_id}.js").read_text(encoding="utf-8")
    start = text.index("] = ") + len("] = ")
    return json.loads(text[start:].rstrip().rstrip(";"))


def test_picking_view_embeds_each_assigned_trip():
    aisles, crosses, locations = _layout()
    view = picking_view(
        aisles, crosses, locations, _result(), _pickers(),
        depot_id="DEPOT", depot_x=0.0, depot_y=0.0,
    )
    assert [picker["id"] for picker in view["pickers"]] == ["P1", "P2"]
    assert view["pickers"][0]["orders"] == ["O1"]
    assert view["orders"]["O1"]["stops"] == ["DEPOT", "L1", "DEPOT"]
    assert view["orders"]["O1"]["duration"] == 26.0
    assert [leg["role"] for leg in view["orders"]["O1"]["legs"]] == ["forward", "return"]
    points = [point for leg in view["orders"]["O1"]["legs"] for point in leg["points"]]
    assert [0.0, 0.0] in points
    assert any(point[0] == 0.0 and point[1] == 3.0 for point in points)
    assert view["unfulfilled"][0]["order_id"] == "O9"
    assert "O1" in view["colors"]
    assert view["orders"]["O1"]["weight"] is None
    assert view["pickers"][0]["max_weight"] is None


def test_picking_view_records_trip_capacity():
    """Weight and volume travel with the selected order. A blank volume is no limit."""
    aisles, crosses, locations = _layout()
    orders = pd.DataFrame(
        [
            {"order_id": "O1", "total_weight": 10.0, "total_volume": 0.4},
            {"order_id": "O2", "total_weight": 5.0, "total_volume": float("nan")},
        ]
    )
    pickers = pd.DataFrame(
        [
            {"picker_id": "P1", "shift_duration": 480.0, "max_weight": 50.0, "max_volume": 2.0},
            {"picker_id": "P2", "shift_duration": 480.0, "max_weight": 50.0},
        ]
    )
    view = picking_view(
        aisles, crosses, locations, _result(), pickers,
        depot_id="DEPOT", depot_x=0.0, depot_y=0.0, orders=orders,
    )
    assert view["orders"]["O1"]["weight"] == 10.0
    assert view["orders"]["O1"]["volume"] == 0.4
    assert view["orders"]["O2"]["weight"] == 5.0
    assert view["orders"]["O2"]["volume"] is None
    assert view["pickers"][0]["max_weight"] == 50.0
    assert view["pickers"][0]["max_volume"] == 2.0
    assert view["pickers"][1]["max_weight"] == 50.0
    assert view["pickers"][1]["max_volume"] is None


def test_picking_view_requires_a_route():
    aisles, crosses, locations = _layout()
    result = {"picker_assignments": {"P1": ["O1"]}, "routes": {}}
    with pytest.raises(ValueError, match="O1"):
        picking_view(
            aisles, crosses, locations, result, _pickers().iloc[:1],
            depot_id="DEPOT", depot_x=0.0, depot_y=0.0,
        )


def test_pages_share_one_result(tmp_path):
    aisles, crosses, locations = _layout()
    warehouse, assignment = write_picking_pages(
        aisles, crosses, locations, _result(), _pickers(),
        depot_id="DEPOT", depot_x=0.0, depot_y=0.0, out_dir=tmp_path,
    )
    warehouse_text = warehouse.read_text(encoding="utf-8")
    assignment_text = assignment.read_text(encoding="utf-8")
    assert "separate trip" in warehouse_text
    assert "does not sequence" in assignment_text
    assert "assignment.html" in warehouse_text
    assert "warehouse.html" in assignment_text
    assert 'id="scenario"' in warehouse_text
    assert 'id="scenario"' in assignment_text
    assert 'id="zoom-in"' in warehouse_text
    assert 'id="zoom-reset"' in warehouse_text
    assert "Scroll to zoom" in warehouse_text
    assert "mapView = null" in warehouse_text
    assert 'id="zoom-in"' not in assignment_text
    view = _embedded(warehouse.parent)
    assert view["orders"]["O2"]["stops"] == ["DEPOT", "L2", "DEPOT"]


def test_cli_html_reads_the_assignment_file(tmp_path):
    """A saved assignment chooses who is drawn. Its duration is the bar length."""
    assignments = tmp_path / "assignments.csv"
    assignments.write_text("picker_id,order_id,duration\nP1,O2,6.5\n", encoding="utf-8")
    out = tmp_path / "pages"
    main(
        [
            "html",
            "--aisles", str(SMALL / "aisles.csv"),
            "--cross-aisles", str(SMALL / "cross_aisles.csv"),
            "--locations", str(SMALL / "locations.csv"),
            "--orders", str(SMALL / "orders.csv"),
            "--items", str(SMALL / "order_items.csv"),
            "--pickers", str(SMALL / "pickers.csv"),
            "--travel", str(SMALL / "travel_times_depot.csv"),
            "--assignments", str(assignments),
            "--depot-id", "DEPOT",
            "--depot-x", "0",
            "--depot-y", "0",
            "--out-dir", str(out),
        ]
    )
    data = _embedded(out)
    assert data["pickers"][0]["id"] == "P1"
    assert data["pickers"][0]["orders"] == ["O2"]
    assert data["orders"]["O2"]["duration"] == 6.5
    assert data["orders"]["O2"]["stops"][0] == "DEPOT"
    assert data["orders"]["O2"]["stops"][-1] == "DEPOT"
    assert any(item["order_id"] == "O1" for item in data["unfulfilled"])
    assert (out / "warehouse.html").stat().st_size > 0
