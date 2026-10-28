"""S0 baseline. Later optional columns must leave these runs unchanged.

The six-order commands are the ones in README.md. The savings command is the
jack-capped run in that same file (2500 kg, 2.0 m3). The cover is the first
truck in truck_loads.csv only. There is no 16-truck run.
"""

from pathlib import Path

import pandas as pd
from pandas.testing import assert_frame_equal

from chunk_loads import main as savings_main
from chunk_model import main as cover_main
from order_picking_optimizer import main as solve_main

_ROOT = Path(__file__).resolve().parents[1]
_BASELINE = Path(__file__).resolve().parent / "baseline"
_EXAMPLE = _ROOT / "inputs" / "layout_example"
_LOADS = _ROOT / "inputs" / "chunk_warehouse" / "truck_loads.csv"
_TRAVEL = _ROOT / "inputs" / "layout_warehouse" / "travel_times.csv"
_PICKERS = _ROOT / "inputs" / "layout_warehouse" / "pickers.csv"


def _assert_csv_matches(got: Path, expected: Path) -> None:
    """Fail when a regenerated CSV differs from the checked-in baseline."""
    actual = pd.read_csv(got)
    frozen = pd.read_csv(expected)
    assert list(actual.columns) == list(frozen.columns), expected.name
    assert_frame_equal(
        actual.reset_index(drop=True),
        frozen.reset_index(drop=True),
        check_dtype=False,
        rtol=1e-9,
        atol=1e-9,
    )


def test_six_order_plans_match_baseline(tmp_path, capsys):
    """Balance 0 and --share-shift both release six orders at score 100006."""
    base = [
        "--orders",
        str(_EXAMPLE / "orders.csv"),
        "--items",
        str(_EXAMPLE / "order_items.csv"),
        "--pickers",
        str(_EXAMPLE / "pickers.csv"),
        "--travel",
        str(_EXAMPLE / "travel_times.csv"),
        "--depot",
        "DEPOT",
    ]
    plain = tmp_path / "six_orders" / "assignments.csv"
    shared = tmp_path / "six_orders_shared" / "assignments.csv"
    solve_main(base + ["--assignments-out", str(plain)])
    solve_main(base + ["--share-shift", "--assignments-out", str(shared)])
    text = capsys.readouterr().out
    assert text.count("Orders fulfilled: 6 / 6") == 2
    assert text.count("Weighted score: 100006") == 2
    _assert_csv_matches(plain, _BASELINE / "six_orders" / "assignments.csv")
    _assert_csv_matches(shared, _BASELINE / "six_orders_shared" / "assignments.csv")


def test_savings_chunker_matches_baseline(tmp_path):
    """The documented jack-capped savings run matches the frozen job files."""
    out = tmp_path / "savings"
    savings_main(
        [
            "--loads",
            str(_LOADS),
            "--travel",
            str(_TRAVEL),
            "--max-job-weight",
            "2500",
            "--max-job-volume",
            "2",
            "--out",
            str(out),
        ]
    )
    frozen = _BASELINE / "savings"
    for name in ("orders.csv", "order_items.csv", "jobs.csv"):
        _assert_csv_matches(out / name, frozen / name)


def test_first_truck_cover_matches_baseline(tmp_path):
    """HiGHS cover of the first truck only. TRK01 is 14 jobs and 253.200 minutes."""
    loads = pd.read_csv(_LOADS)
    first = loads["truck_id"].iloc[0]
    subset = tmp_path / "truck_first.csv"
    loads[loads["truck_id"] == first].to_csv(subset, index=False)
    out = tmp_path / "cover_first_truck"
    cover_main(
        [
            "--loads",
            str(subset),
            "--travel",
            str(_TRAVEL),
            "--pickers",
            str(_PICKERS),
            "--out",
            str(out),
        ]
    )
    frozen = _BASELINE / "cover_first_truck"
    for name in ("orders.csv", "order_items.csv", "jobs.csv", "rejected.csv", "solve.csv"):
        _assert_csv_matches(out / name, frozen / name)
