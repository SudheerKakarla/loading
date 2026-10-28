"""The DDL adapter applies a caller-supplied column map. It does not invent a weight."""

from pathlib import Path

import pandas as pd
import pytest

from chunk_loads import ChunkSettings, chunk_loads
from import_ddl import import_demand, main


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def _files(folder: Path) -> tuple:
    source = _write(
        folder / "export.csv",
        "GRP,DOCK,SKU,QTY,LOC,BATCH,DEST\n"
        "G1,PACK_A,SKU1,4,L1,B1,STOP-1\n"
        "G1,PACK_A,SKU1,4,L1,B1,STOP-1\n"
        "G1,PACK_A,SKU9,2,L2,B1,STOP-1\n",
    )
    column_map = _write(
        folder / "column_map.csv",
        "source_column,target_column\n"
        "GRP,group_id\n"
        "DOCK,depot_id\n"
        "SKU,sku_id\n"
        "QTY,quantity\n"
        "LOC,location_id\n"
        "BATCH,batch_id\n"
        "DEST,destination_id\n",
    )
    master = _write(
        folder / "sku_master.csv",
        "sku_id,unit_weight_kg,unit_volume_m3\nSKU1,2,0.1\n",
    )
    return source, column_map, master


def test_mapping_keeps_one_line_and_rejects_a_duplicate_and_a_missing_sku(tmp_path: Path):
    source, column_map, master = _files(tmp_path)
    out = tmp_path / "out"
    assert import_demand(source, column_map, master, out) == 1
    loads = pd.read_csv(out / "demand.csv")
    assert len(loads) == 1
    assert loads.iloc[0]["group_id"] == "G1"
    assert loads.iloc[0]["item_id"] == "I1"
    assert loads.iloc[0]["weight"] == pytest.approx(8.0)
    assert loads.iloc[0]["volume"] == pytest.approx(0.4)
    assert "truck_id" not in loads.columns
    rejected = pd.read_csv(out / "import_rejected.csv")
    assert set(rejected.reason) == {"duplicate", "missing_sku"}
    travel = pd.DataFrame(
        [
            {"from_location": "PACK_A", "to_location": "L1", "travel_time": 1.0},
            {"from_location": "L1", "to_location": "PACK_A", "travel_time": 1.0},
        ]
    )
    jobs = chunk_loads(
        pd.read_csv(out / "truck_loads.csv"),
        travel,
        ChunkSettings(max_job_weight=500.0, job_overhead=0.0, default_pick_time=0.0),
    )
    assert len(jobs.orders) == 1
    assert jobs.orders.depot_id.iloc[0] == "PACK_A"


def test_a_bad_quantity_is_rejected_and_names_the_file_row_and_column(tmp_path: Path):
    source = _write(
        tmp_path / "export.csv",
        "GRP,DOCK,SKU,QTY,LOC\nG1,PACK_A,SKU1,nope,L1\n",
    )
    column_map = _write(
        tmp_path / "column_map.csv",
        "source_column,target_column\n"
        "GRP,group_id\nDOCK,depot_id\nSKU,sku_id\nQTY,quantity\nLOC,location_id\n",
    )
    master = _write(tmp_path / "sku_master.csv", "sku_id,weight\nSKU1,2\n")
    out = tmp_path / "out"
    assert import_demand(source, column_map, master, out) == 1
    rejected = pd.read_csv(out / "import_rejected.csv")
    assert list(rejected.reason) == ["bad_quantity"]
    assert "export.csv row 2 column quantity" in rejected.detail.iloc[0]
    with pytest.raises(SystemExit) as exc:
        main(
            [
                "--source",
                str(source),
                "--column-map",
                str(column_map),
                "--sku-master",
                str(master),
                "--out",
                str(tmp_path / "cli"),
            ]
        )
    assert exc.value.code not in (0, None)
