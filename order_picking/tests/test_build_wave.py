"""Crew config for the wave builder."""

from pathlib import Path

import pandas as pd
import pytest

from build_wave import default_settings_path, load_crew, load_wave_settings, write_wave


def _write_crew(folder: Path, text: str) -> Path:
    path = folder / "crew.csv"
    path.write_text(text, encoding="utf-8")
    return path


def test_custom_crew_sets_counts_and_ratings(tmp_path):
    """A crew file chooses how many machines, and the kilograms each one carries."""
    crew = _write_crew(
        tmp_path,
        "equipment_type,count,max_weight,max_volume,shift_duration\n"
        "pallet_jack,2,1000,1.0,400\n"
        "forklift,3,4000,3.0,360\n",
    )
    folder = write_wave(tmp_path / "wave", n_orders=30, seed=3, crew_path=crew)
    pickers = pd.read_csv(folder / "pickers.csv")
    orders = pd.read_csv(folder / "orders.csv")
    jacks = pickers[pickers.equipment_type == "pallet_jack"]
    forks = pickers[pickers.equipment_type == "forklift"]
    assert list(jacks.picker_id) == ["P01", "P02"]
    assert list(forks.picker_id) == ["P03", "P04", "P05"]
    assert set(jacks.max_weight) == {1000.0}
    assert set(jacks.shift_duration) == {400.0}
    assert set(forks.max_weight) == {4000.0}
    assert set(forks.max_volume) == {3.0}
    assert set(forks.shift_duration) == {360.0}

    heavy = orders[orders.required_equipment.fillna("") == "forklift"]
    light = orders[orders.required_equipment.fillna("") != "forklift"]
    assert not heavy.empty
    assert (heavy.total_weight > 1000).all()
    assert (heavy.total_weight <= 4000).all()
    assert (heavy.total_volume <= 3.0).all()
    assert (light.total_weight <= 1000).all()
    assert (light.total_volume <= 1.0).all()


def test_crew_rejects_a_bad_count_a_duplicate_and_a_tight_gap(tmp_path):
    header = "equipment_type,count,max_weight,max_volume,shift_duration\n"
    with pytest.raises(ValueError, match="count"):
        load_crew(_write_crew(tmp_path, header + "pallet_jack,0,2500,2.0,480\n"))
    with pytest.raises(ValueError, match="repeats"):
        load_crew(
            _write_crew(
                tmp_path,
                header + "pallet_jack,1,2500,2.0,480\npallet_jack,1,2500,2.0,480\n",
            )
        )
    tight = _write_crew(
        tmp_path,
        header + "pallet_jack,1,2500,2.0,480\nforklift,1,2700,4.0,480\n",
    )
    with pytest.raises(ValueError, match="heaviest"):
        write_wave(tmp_path / "wave", n_orders=1, seed=1, crew_path=tight)


def test_missing_crew_file_raises(tmp_path):
    with pytest.raises(ValueError, match="not found"):
        load_crew(tmp_path / "missing.csv")


def test_settings_change_pick_time_and_drop_heavy_orders(tmp_path):
    """Pick time, the heavy-order share, and the shift all come from the CSVs."""
    frame = pd.read_csv(default_settings_path())
    frame.loc[frame.name == "pick_time", "value"] = 1.25
    frame.loc[frame.name == "heavy_share", "value"] = 0
    settings = tmp_path / "wave_settings.csv"
    frame.to_csv(settings, index=False)
    crew = _write_crew(
        tmp_path,
        "equipment_type,count,max_weight,max_volume,shift_duration\n"
        "pallet_jack,1,2500,2.0,400\n"
        "forklift,1,5000,4.0,400\n",
    )
    folder = write_wave(
        tmp_path / "wave",
        n_orders=12,
        seed=1,
        crew_path=crew,
        settings_path=settings,
    )
    items = pd.read_csv(folder / "order_items.csv")
    orders = pd.read_csv(folder / "orders.csv")
    pickers = pd.read_csv(folder / "pickers.csv")
    assert set(items.pick_time) == {1.25}
    assert orders.required_equipment.fillna("").eq("").all()
    assert set(pickers.shift_duration) == {400.0}


def test_allowed_modes_are_copied_onto_each_picker(tmp_path):
    """A blank allowed_modes cell means every mode. The column stays absent when omitted."""
    crew = _write_crew(
        tmp_path,
        "equipment_type,count,max_weight,max_volume,shift_duration,allowed_modes\n"
        "pallet_jack,2,2500,2.0,480,pickface\n"
        "forklift,1,5000,4.0,480,bulk; pickface\n",
    )
    folder = write_wave(tmp_path / "wave", n_orders=4, seed=1, crew_path=crew)
    pickers = pd.read_csv(folder / "pickers.csv")
    jacks = pickers[pickers.equipment_type == "pallet_jack"]
    forks = pickers[pickers.equipment_type == "forklift"]
    assert set(jacks.allowed_modes) == {"pickface"}
    assert set(forks.allowed_modes) == {"bulk;pickface"}
    plain = _write_crew(
        tmp_path,
        "equipment_type,count,max_weight,max_volume,shift_duration\n"
        "pallet_jack,1,2500,2.0,480\n"
        "forklift,1,5000,4.0,480\n",
    )
    plain_folder = write_wave(tmp_path / "plain", n_orders=4, seed=1, crew_path=plain)
    assert "allowed_modes" not in pd.read_csv(plain_folder / "pickers.csv").columns
    duty = _write_crew(
        tmp_path,
        "equipment_type,count,max_weight,max_volume,shift_duration,home_depot,non_pick_minutes\n"
        "pallet_jack,1,2500,2.0,480,PACK_A,15\n"
        "forklift,1,5000,4.0,480,,0\n",
    )
    stamped = write_wave(tmp_path / "duty", n_orders=4, seed=1, crew_path=duty)
    pickers = pd.read_csv(stamped / "pickers.csv")
    assert pickers.loc[pickers.equipment_type == "pallet_jack", "home_depot"].iloc[0] == "PACK_A"
    assert pickers.loc[pickers.equipment_type == "forklift", "non_pick_minutes"].iloc[0] == pytest.approx(0)
    assert "max_pallets" not in pickers.columns
    assert "pick_mode" not in pd.read_csv(stamped / "orders.csv").columns
    free = load_crew(
        _write_crew(
            tmp_path,
            "equipment_type,count,max_weight,max_volume,shift_duration,allowed_modes\n"
            "pallet_jack,1,2500,2.0,480,reach\n"
            "forklift,1,5000,4.0,480,\n",
        )
    )
    assert free.loc[free.equipment_type == "pallet_jack", "allowed_modes"].iloc[0] == "reach"


def test_settings_reject_a_missing_or_unknown_name(tmp_path):
    frame = pd.read_csv(default_settings_path())
    short = frame[frame.name != "pick_time"]
    missing = tmp_path / "missing.csv"
    short.to_csv(missing, index=False)
    with pytest.raises(ValueError, match="pick_time"):
        load_wave_settings(missing)
    extra = pd.concat([frame, pd.DataFrame([{"name": "bonus", "value": 1}])], ignore_index=True)
    unknown = tmp_path / "unknown.csv"
    extra.to_csv(unknown, index=False)
    with pytest.raises(ValueError, match="bonus"):
        load_wave_settings(unknown)
