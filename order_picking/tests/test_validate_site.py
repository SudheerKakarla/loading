"""The site template runs through the chunker, the picker, the docks, and the sheet."""

import shutil
from pathlib import Path

import pandas as pd
import pytest

from build_travel_matrix import build_travel_matrix, travel_matrix_frame
from chunk_loads import ChunkSettings, chunk_loads, write_chunk_outputs
from order_picking_optimizer import assignments_frame, solve_order_picking
from run_docks import load_staging_capacities, solve_docks
from task_sheet import build_task_sheet
from validate_site import validate_site

_SITE = Path(__file__).resolve().parents[1] / "inputs" / "site_template"


def test_location_type_is_checked_and_does_not_change_the_walk(tmp_path: Path):
    with_type = validate_site(_SITE)
    plain = pd.read_csv(_SITE / "locations.csv").drop(columns=["location_type"])
    other = travel_matrix_frame(
        build_travel_matrix(
            pd.read_csv(_SITE / "aisles.csv"),
            pd.read_csv(_SITE / "cross_aisles.csv"),
            plain,
            depots=pd.read_csv(_SITE / "depots.csv"),
            speed=1.0,
        )
    )
    pd.testing.assert_frame_equal(with_type, other)
    copy = tmp_path / "site"
    shutil.copytree(_SITE, copy)
    locations = pd.read_csv(copy / "locations.csv")
    locations.loc[locations.location_id == "L1", "location_type"] = "shelf"
    locations.to_csv(copy / "locations.csv", index=False)
    with pytest.raises(ValueError, match="column location_type"):
        validate_site(copy)


def test_a_missing_location_names_the_line_file(tmp_path: Path):
    lines = tmp_path / "loads.csv"
    lines.write_text(
        "truck_id,depot_id,item_id,location_id,weight\nT1,PACK_A,A,MISSING,10\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="loads.csv row 2 column location_id MISSING"):
        validate_site(_SITE, lines=lines)


def test_template_runs_the_chunker_picker_docks_and_task_sheet(tmp_path: Path):
    travel = validate_site(_SITE)
    loads = pd.read_csv(_SITE / "loads.csv")
    chunked = chunk_loads(
        loads,
        travel,
        ChunkSettings(max_job_weight=2500.0, job_overhead=0.0, default_pick_time=0.0),
    )
    assert set(chunked.orders.depot_id) == {"PACK_A", "PACK_B"}
    written = tmp_path / "chunk"
    write_chunk_outputs(chunked, written)
    pack = written / "by_depot" / "PACK_A"
    orders = pd.read_csv(pack / "orders.csv")
    items = pd.read_csv(pack / "order_items.csv")
    pickers = pd.read_csv(_SITE / "pickers.csv")
    result = solve_order_picking(orders, items, pickers, travel, "PACK_A", verbose=False)
    assignments = assignments_frame(result)
    assert len(assignments) == len(orders)
    sheet = build_task_sheet(assignments, orders, items, pickers)
    assert len(sheet) == len(items)
    assert list(sheet.item_id) == list(items.item_id)
    solve_docks(
        written / "by_depot",
        pickers,
        travel,
        tmp_path / "docks",
        staging_capacities=load_staging_capacities(_SITE / "staging.csv"),
        time_limit=10.0,
    )
    for depot_id in ("PACK_A", "PACK_B"):
        assert (tmp_path / "docks" / depot_id / "staging.csv").is_file()
