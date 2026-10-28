"""A synthetic task log recovers known times. The profile is not a default."""

from pathlib import Path

import pandas as pd
import pytest

from calibrate_from_task_log import calibrate_log, main


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def _map(folder: Path) -> Path:
    return _write(
        folder / "column_map.csv",
        "source_column,target_column\n"
        "MODE,pick_mode\n"
        "JOB,job_id\n"
        "PICK,pick_minutes\n"
        "OH,overhead_minutes\n"
        "TRAVEL,travel_minutes\n"
        "DIST,distance_m\n"
        "DAY,event_date\n",
    )


def test_known_times_are_recovered_and_a_small_mode_stays_blank(tmp_path: Path):
    log = _write(
        tmp_path / "task_log.csv",
        "MODE,JOB,PICK,OH,TRAVEL,DIST,DAY\n"
        "bulk,B1,1,4,2,100,2026-01-01\n"
        "bulk,B2,2,4,2,100,2026-01-01\n"
        "bulk,B3,3,4,2,100,2026-01-02\n"
        "bulk,B4,4,4,2,100,2026-01-02\n"
        "bulk,B5,10,4,2,100,2026-01-03\n"
        "pickface,F1,9,1,2,100,2026-01-01\n"
        "pickface,F2,9,1,2,100,2026-01-01\n",
    )
    out = tmp_path / "out"
    assert calibrate_log(log, _map(tmp_path), out, min_rows=5) == 0
    profile = pd.read_csv(out / "mode_profile.csv")
    bulk = profile[profile.pick_mode == "bulk"].iloc[0]
    assert bulk.default_pick_time == pytest.approx(3.0)
    assert bulk.job_overhead == pytest.approx(4.0)
    assert pd.isna(bulk.pallet_basis)
    face = profile[profile.pick_mode == "pickface"].iloc[0]
    assert pd.isna(face.default_pick_time)
    assert pd.isna(face.job_overhead)
    report = (out / "calibration_report.md").read_text(encoding="utf-8")
    assert "2026-01-01 to 2026-01-03" in report
    assert "not a default" in report
    assert "50" in report
    assert "Too few rows" in report


def test_a_bad_pick_time_is_rejected_and_names_the_file_row_and_column(tmp_path: Path):
    log = _write(tmp_path / "task_log.csv", "MODE,JOB,PICK\nbulk,B1,nope\n")
    column_map = _write(
        tmp_path / "column_map.csv",
        "source_column,target_column\nMODE,pick_mode\nJOB,job_id\nPICK,pick_minutes\n",
    )
    out = tmp_path / "out"
    assert calibrate_log(log, column_map, out) == 1
    rejected = pd.read_csv(out / "calibration_rejected.csv")
    assert list(rejected.reason) == ["bad_pick_time"]
    assert "task_log.csv row 2 column pick_minutes" in rejected.detail.iloc[0]
    with pytest.raises(SystemExit) as exc:
        main(["--log", str(log), "--column-map", str(column_map), "--out", str(tmp_path / "cli")])
    assert exc.value.code not in (0, None)
