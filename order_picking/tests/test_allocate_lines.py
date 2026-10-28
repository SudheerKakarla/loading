"""Stock allocation: pallets, then cases, then an opened case for the remainder."""

import pandas as pd
import pytest

from allocate_lines import allocate_lines
from chunk_loads import ChunkSettings, chunk_loads
from chunk_model import ChunkModelSettings, PickerSpec, chunk_loads_model


def _travel() -> pd.DataFrame:
    rows = []
    for origin in ("PACK_A", "LB", "LF", "FAR", "SMALL"):
        for dest in ("PACK_A", "LB", "LF", "FAR", "SMALL"):
            if origin == dest:
                continue
            minutes = 1.0
            if "FAR" in (origin, dest):
                minutes = 9.0
            rows.append({"from_location": origin, "to_location": dest, "travel_time": minutes})
    return pd.DataFrame(rows)


def _master(**overrides) -> pd.DataFrame:
    row = {
        "sku_id": "S",
        "unit_weight_kg": 2.0,
        "unit_volume_m3": 0.1,
        "case_qty": 4,
        "pallet_qty": 10,
    }
    row.update(overrides)
    return pd.DataFrame([row])


def _stock(location_id: str, qty: float, location_type: str, expiry=1, pallet_id: str = "P") -> dict:
    return {
        "sku_id": "S",
        "location_id": location_id,
        "batch_id": "B",
        "pallet_id": pallet_id,
        "qty": qty,
        "expiry": expiry,
        "location_type": location_type,
    }


def _demand(quantity: float, **overrides) -> pd.DataFrame:
    row = {
        "group_id": "G1",
        "depot_id": "PACK_A",
        "sku_id": "S",
        "quantity": quantity,
        "destination_id": "STOP-1",
        "must_ship": "",
        "source_order_id": "SO1",
    }
    row.update(overrides)
    return pd.DataFrame([row])


def test_one_case_at_one_location_is_one_line(tmp_path):
    """Demand equal to one case stays one pick-face line at that location."""
    stock = pd.DataFrame([_stock("LF", 4, "pickface")])
    code = allocate_lines(_demand(4), stock, _master(), _travel(), tmp_path, demand_label="demand")
    assert code == 0
    loads = pd.read_csv(tmp_path / "truck_loads.csv")
    assert len(loads) == 1
    assert loads.iloc[0]["location_id"] == "LF"
    assert loads.iloc[0]["quantity"] == pytest.approx(4)
    assert loads.iloc[0]["pick_mode"] == "pickface"
    assert loads.iloc[0]["weight"] == pytest.approx(8)
    assert loads.iloc[0]["truck_id"] == "G1"
    assert not (tmp_path / "short_stock.csv").exists()
    jobs = chunk_loads(
        loads,
        _travel(),
        ChunkSettings(max_job_weight=500.0, job_overhead=0.0, default_pick_time=0.0),
    )
    assert len(jobs.orders) == 1
    cover = chunk_loads_model(
        loads,
        _travel(),
        [PickerSpec("P", "pallet_jack", 500.0, 2.0)],
        ChunkModelSettings(job_overhead=0.0, default_pick_time=0.0),
    )
    assert len(cover.orders) == 1


def test_a_pallet_then_a_case_and_the_older_pallet_wins(tmp_path):
    stock = pd.DataFrame(
        [
            _stock("FAR", 10, "bulk", expiry=1, pallet_id="OLD"),
            _stock("LB", 10, "bulk", expiry=5, pallet_id="NEW"),
            _stock("LF", 4, "pickface", expiry=3, pallet_id="FACE"),
        ]
    )
    assert allocate_lines(_demand(14), stock, _master(), _travel(), tmp_path) == 0
    loads = pd.read_csv(tmp_path / "truck_loads.csv")
    assert list(loads.pick_mode) == ["bulk", "pickface"]
    assert list(loads.location_id) == ["FAR", "LF"]
    assert list(loads.quantity) == [10, 4]


def test_a_case_taken_from_bulk_keeps_the_bulk_mode(tmp_path):
    stock = pd.DataFrame([_stock("LB", 8, "bulk", pallet_id="BULK")])
    assert allocate_lines(_demand(4), stock, _master(pallet_qty=""), _travel(), tmp_path) == 0
    loads = pd.read_csv(tmp_path / "truck_loads.csv")
    assert list(loads.pick_mode) == ["bulk"]
    assert loads.iloc[0]["quantity"] == pytest.approx(4)


def test_residual_opens_the_smallest_covering_case(tmp_path):
    stock = pd.DataFrame(
        [
            _stock("LF", 20, "pickface", pallet_id="BIG"),
            _stock("SMALL", 6, "pickface", pallet_id="SMALL"),
        ]
    )
    assert allocate_lines(_demand(3), stock, _master(pallet_qty=""), _travel(), tmp_path) == 0
    loads = pd.read_csv(tmp_path / "truck_loads.csv")
    assert len(loads) == 1
    assert loads.iloc[0]["location_id"] == "SMALL"
    assert loads.iloc[0]["pick_mode"] == "pickface"
    assert loads.iloc[0]["quantity"] == pytest.approx(3)
    summary = pd.read_csv(tmp_path / "allocation_summary.csv")
    assert summary.opened_case_count.iloc[0] == 1


def test_stock_is_not_oversold_across_two_demand_lines(tmp_path):
    stock = pd.DataFrame([_stock("LF", 4, "pickface")])
    demand = pd.DataFrame(
        [
            {**_demand(4).iloc[0].to_dict(), "source_order_id": "A"},
            {**_demand(4).iloc[0].to_dict(), "source_order_id": "B"},
        ]
    )
    assert allocate_lines(demand, stock, _master(pallet_qty=""), _travel(), tmp_path) == 1
    loads = pd.read_csv(tmp_path / "truck_loads.csv")
    assert loads.quantity.sum() == pytest.approx(4)
    short = pd.read_csv(tmp_path / "short_stock.csv")
    assert short.iloc[0]["requested"] == pytest.approx(4)
    assert short.iloc[0]["allocated"] == pytest.approx(0)
    assert short.iloc[0]["short"] == pytest.approx(4)


def test_short_stock_is_refused_unless_allowed_and_a_missing_sku_is_rejected(tmp_path):
    stock = pd.DataFrame([_stock("LF", 2, "pickface")])
    assert allocate_lines(_demand(5), stock, _master(), _travel(), tmp_path / "refused") == 1
    short = pd.read_csv(tmp_path / "refused" / "short_stock.csv")
    assert set(short.columns) == {"sku_id", "requested", "allocated", "short"}
    assert short.iloc[0]["short"] > 0
    fresh = pd.DataFrame([_stock("LF", 2, "pickface")])
    assert allocate_lines(_demand(5), fresh, _master(), _travel(), tmp_path / "allowed", allow_short=True) == 0
    missing = _demand(4, sku_id="NOPE")
    assert allocate_lines(missing, fresh, _master(), _travel(), tmp_path / "sku") == 1
    rejected = pd.read_csv(tmp_path / "sku" / "allocation_rejected.csv")
    assert list(rejected.reason) == ["missing_sku"]
    assert list(rejected.sku_id) == ["NOPE"]
    with pytest.raises(ValueError, match="Travel time missing"):
        allocate_lines(
            _demand(4),
            pd.DataFrame([_stock("MISSING", 4, "pickface")]),
            _master(),
            _travel(),
            tmp_path / "missing",
        )
