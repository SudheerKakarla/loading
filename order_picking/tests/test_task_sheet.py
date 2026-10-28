"""The task sheet lists each item line in the chunker's order. It does not solve."""

from pathlib import Path

import pandas as pd

from task_sheet import TOUR_NOTE, build_task_sheet, main


def test_two_pickers_follow_deadlines_and_a_late_job_shows_minutes_late():
    assignments = pd.DataFrame(
        [
            {"picker_id": "P2", "order_id": "LATE", "duration": 30.0},
            {"picker_id": "P1", "order_id": "SOON", "duration": 10.0},
            {"picker_id": "P1", "order_id": "LATER", "duration": 10.0},
        ]
    )
    orders = pd.DataFrame(
        [
            {"order_id": "LATER", "cutoff_time": 40.0, "truck_id": "T2"},
            {"order_id": "SOON", "cutoff_time": 5.0, "truck_id": "T1"},
            {"order_id": "LATE", "cutoff_time": 10.0},
        ]
    )
    items = pd.DataFrame(
        [
            {"order_id": "SOON", "item_id": "A", "location_id": "L1", "quantity": 1, "weight": 2},
            {"order_id": "SOON", "item_id": "B", "location_id": "L2", "quantity": 1, "weight": 3},
            {"order_id": "LATER", "item_id": "C", "location_id": "L3", "quantity": 4, "weight": 1},
            {"order_id": "LATE", "item_id": "D", "location_id": "L4", "quantity": 1, "weight": 1},
        ]
    )
    pickers = pd.DataFrame(
        [
            {"picker_id": "P1", "equipment_type": "pallet_jack"},
            {"picker_id": "P2", "equipment_type": "forklift"},
        ]
    )
    sheet = build_task_sheet(assignments, orders, items, pickers)
    assert len(sheet) == 4
    first = sheet[sheet.picker_id == "P1"]
    assert list(first.job_id) == ["SOON", "SOON", "LATER"]
    assert list(first.sequence) == [1, 2, 3]
    assert list(first.item_id) == ["A", "B", "C"]
    assert first.planned_finish_minute.iloc[1] == 10.0
    assert first.minutes_late.iloc[0] > 0
    late = sheet[sheet.job_id == "LATE"].iloc[0]
    assert late.minutes_late == 20.0
    assert late.equipment_type == "forklift"
    assert late.truck_id == ""
    assert late.depot_id == ""
    assert late.pick_mode == ""


def test_missing_optional_columns_stay_blank_and_the_file_names_the_tour(tmp_path: Path):
    assignments = pd.DataFrame([{"picker_id": "P1", "order_id": "O1", "duration": 4.0}])
    orders = pd.DataFrame([{"order_id": "O1", "cutoff_time": ""}])
    items = pd.DataFrame([{"order_id": "O1", "item_id": "A", "location_id": "L1"}])
    pickers = pd.DataFrame([{"picker_id": "P1", "equipment_type": "pallet_jack"}])
    sheet = build_task_sheet(assignments, orders, items, pickers)
    assert sheet.iloc[0]["truck_id"] == ""
    assert sheet.iloc[0]["quantity"] == ""
    assert sheet.iloc[0]["minutes_late"] == 0.0
    out = tmp_path / "task_sheet.csv"
    main(
        [
            "--assignments",
            str(_csv(tmp_path / "assignments.csv", assignments)),
            "--orders",
            str(_csv(tmp_path / "orders.csv", orders)),
            "--items",
            str(_csv(tmp_path / "items.csv", items)),
            "--pickers",
            str(_csv(tmp_path / "pickers.csv", pickers)),
            "--out",
            str(out),
            "--html",
            str(tmp_path / "task_sheet.html"),
        ]
    )
    text = out.read_text(encoding="utf-8")
    assert text.startswith(f"# {TOUR_NOTE}")
    assert "chunker's tour order" in (tmp_path / "task_sheet.html").read_text(encoding="utf-8")


def _csv(path: Path, frame: pd.DataFrame) -> Path:
    frame.to_csv(path, index=False)
    return path
