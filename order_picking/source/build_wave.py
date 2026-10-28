"""Build a full pick wave on the six-aisle map.

The building, depot, and travel times stay the ones in ``layout_example``.
The crew comes from ``inputs/crew.csv``: how many of each machine, and the
kilograms, volume, and shift minutes for that machine. The order mix comes
from ``inputs/wave_settings.csv``: wave size, pick time, line counts, how
often an order is heavy, and the priority mix. One order is still one trip.

Run from ``order_picking/``::

    python source/build_wave.py --out inputs/layout_wave
"""

from __future__ import annotations

import argparse
import math
import random
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import List, Sequence, Tuple

import pandas as pd

_EXAMPLE_FILES = (
    "aisles.csv",
    "cross_aisles.csv",
    "locations.csv",
    "depots.csv",
    "travel_times.csv",
)
_CREW_COLUMNS = (
    "equipment_type",
    "count",
    "max_weight",
    "max_volume",
    "shift_duration",
)
_SETTING_NAMES = (
    "n_orders",
    "seed",
    "pick_time",
    "min_lines",
    "max_lines",
    "heavy_share",
    "light_floor_kg",
    "light_margin_kg",
    "heavy_over_kg",
    "heavy_margin_kg",
    "light_volume_fill",
    "heavy_volume_fill",
    "volume_cap_gap",
    "must_ship_share",
    "key_cutoff",
    "must_ship_weight",
    "key_priority",
    "standard_priority",
    "wait_min_hours",
    "wait_max_hours",
)


@dataclass(frozen=True)
class WaveSettings:
    """Scalar knobs for one generated wave. Loaded from ``wave_settings.csv``."""

    n_orders: int
    seed: int
    pick_time: float
    min_lines: int
    max_lines: int
    heavy_share: float
    light_floor_kg: int
    light_margin_kg: int
    heavy_over_kg: int
    heavy_margin_kg: int
    light_volume_fill: float
    heavy_volume_fill: float
    volume_cap_gap: float
    must_ship_share: float
    key_cutoff: float
    must_ship_weight: int
    key_priority: int
    standard_priority: int
    wait_min_hours: float
    wait_max_hours: float


def default_crew_path() -> Path:
    """Return the shipped crew config, ``inputs/crew.csv``."""
    return Path(__file__).resolve().parents[1] / "inputs" / "crew.csv"


def default_settings_path() -> Path:
    """Return the shipped wave settings, ``inputs/wave_settings.csv``."""
    return Path(__file__).resolve().parents[1] / "inputs" / "wave_settings.csv"


def load_crew(path: Path) -> pd.DataFrame:
    """Read a crew config and return one row per equipment type.

    ``count`` is how many pickers use that machine. ``max_weight`` is kilograms
    per trip. ``max_volume`` and ``shift_duration`` are that machine's other
    caps. Shift minutes live in this file, on each row. A missing file, a
    missing column, a blank type, a duplicate type, or a count below 1 raises
    ``ValueError``.
    """
    crew_path = Path(path)
    if not crew_path.is_file():
        raise ValueError(f"Crew file not found: {crew_path}")
    frame = pd.read_csv(crew_path)
    frame.columns = [str(name).strip() for name in frame.columns]
    missing = [name for name in _CREW_COLUMNS if name not in frame.columns]
    if missing:
        raise ValueError(f"Crew file {crew_path} is missing column(s): {', '.join(missing)}")
    if frame.empty:
        raise ValueError(f"Crew file {crew_path} has no equipment rows.")

    rows: List[dict] = []
    seen: set[str] = set()
    for index, raw in frame.iterrows():
        label = f"row {index + 2} of {crew_path}"
        equipment = "" if pd.isna(raw.equipment_type) else str(raw.equipment_type).strip()
        if not equipment:
            raise ValueError(f"Crew {label} needs an equipment_type.")
        if equipment in seen:
            raise ValueError(f"Crew {label} repeats equipment_type {equipment}.")
        seen.add(equipment)
        rows.append(
            {
                "equipment_type": equipment,
                "count": _positive_int(raw["count"], f"{label} count"),
                "max_weight": _positive_number(raw.max_weight, f"{label} max_weight"),
                "max_volume": _positive_number(raw.max_volume, f"{label} max_volume"),
                "shift_duration": _positive_number(raw.shift_duration, f"{label} shift_duration"),
            }
        )
    return pd.DataFrame(rows, columns=list(_CREW_COLUMNS))


def load_wave_settings(path: Path) -> WaveSettings:
    """Read ``name,value`` settings. A missing name or an unknown name raises."""
    settings_path = Path(path)
    if not settings_path.is_file():
        raise ValueError(f"Wave settings file not found: {settings_path}")
    frame = pd.read_csv(settings_path)
    frame.columns = [str(name).strip() for name in frame.columns]
    if "name" not in frame.columns or "value" not in frame.columns:
        raise ValueError(f"Wave settings file {settings_path} needs columns name and value.")
    raw: dict[str, object] = {}
    for index, row in frame.iterrows():
        label = f"row {index + 2} of {settings_path}"
        name = "" if pd.isna(row["name"]) else str(row["name"]).strip()
        if not name:
            raise ValueError(f"Wave settings {label} needs a name.")
        if name in raw:
            raise ValueError(f"Wave settings {label} repeats {name}.")
        raw[name] = row["value"]
    missing = [name for name in _SETTING_NAMES if name not in raw]
    unknown = [name for name in raw if name not in _SETTING_NAMES]
    if missing:
        raise ValueError(f"Wave settings file {settings_path} is missing: {', '.join(missing)}")
    if unknown:
        raise ValueError(f"Wave settings file {settings_path} has unknown name(s): {', '.join(unknown)}")
    return _checked_settings(raw, settings_path)


def write_wave(
    out_dir: Path,
    n_orders: int | None = None,
    seed: int | None = None,
    crew_path: Path | None = None,
    settings_path: Path | None = None,
) -> Path:
    """Write one wave folder and return that folder.

    ``n_orders`` below 1 raises. Omit ``n_orders`` or ``seed`` to use the
    settings file. The geometry files are copied from ``inputs/layout_example``.
    Pickers come from the crew file. Order weights, pick time, and the visit
    lists come from the settings and ``seed``, so the same arguments rewrite
    the same CSVs.
    """
    settings = load_wave_settings(
        Path(settings_path) if settings_path is not None else default_settings_path()
    )
    order_count = settings.n_orders if n_orders is None else int(n_orders)
    wave_seed = settings.seed if seed is None else int(seed)
    if order_count < 1:
        raise ValueError("n_orders must be at least 1.")
    crew = load_crew(Path(crew_path) if crew_path is not None else default_crew_path())
    too_small = crew[crew.max_volume <= settings.volume_cap_gap]
    if not too_small.empty:
        names = ", ".join(str(name) for name in too_small.equipment_type)
        raise ValueError(
            f"max_volume must be above volume_cap_gap ({settings.volume_cap_gap}) for: {names}"
        )
    _weight_bands(crew, settings)

    example = Path(__file__).resolve().parents[1] / "inputs" / "layout_example"
    folder = Path(out_dir)
    folder.mkdir(parents=True, exist_ok=True)
    for name in _EXAMPLE_FILES:
        shutil.copyfile(example / name, folder / name)

    locations = pd.read_csv(folder / "locations.csv")
    slots = [str(slot) for slot in locations["location_id"]]
    if len(slots) < 2:
        raise ValueError("The layout needs at least two slots.")

    rng = random.Random(wave_seed)
    orders, items = _orders_and_items(rng, order_count, slots, crew, settings)
    orders.to_csv(folder / "orders.csv", index=False)
    items.to_csv(folder / "order_items.csv", index=False)
    _pickers(crew).to_csv(folder / "pickers.csv", index=False)
    return folder


def _checked_settings(raw: dict[str, object], path: Path) -> WaveSettings:
    def cell(name: str) -> object:
        return raw[name]

    n_orders = _positive_int(cell("n_orders"), f"{path} n_orders")
    seed = _whole(cell("seed"), f"{path} seed")
    pick_time = _non_negative(cell("pick_time"), f"{path} pick_time")
    min_lines = _positive_int(cell("min_lines"), f"{path} min_lines")
    max_lines = _positive_int(cell("max_lines"), f"{path} max_lines")
    if max_lines < min_lines:
        raise ValueError(f"{path} max_lines must be at least min_lines.")
    heavy_share = _unit_share(cell("heavy_share"), f"{path} heavy_share")
    light_floor_kg = _positive_int(cell("light_floor_kg"), f"{path} light_floor_kg")
    light_margin_kg = _whole_non_negative(cell("light_margin_kg"), f"{path} light_margin_kg")
    heavy_over_kg = _whole_non_negative(cell("heavy_over_kg"), f"{path} heavy_over_kg")
    heavy_margin_kg = _whole_non_negative(cell("heavy_margin_kg"), f"{path} heavy_margin_kg")
    light_fill = _fill(cell("light_volume_fill"), f"{path} light_volume_fill")
    heavy_fill = _fill(cell("heavy_volume_fill"), f"{path} heavy_volume_fill")
    volume_cap_gap = _non_negative(cell("volume_cap_gap"), f"{path} volume_cap_gap")
    must_ship_share = _unit_share(cell("must_ship_share"), f"{path} must_ship_share")
    key_cutoff = _unit_share(cell("key_cutoff"), f"{path} key_cutoff")
    if key_cutoff < must_ship_share:
        raise ValueError(f"{path} key_cutoff must be at least must_ship_share.")
    must_ship_weight = _whole_non_negative(cell("must_ship_weight"), f"{path} must_ship_weight")
    key_priority = _whole_non_negative(cell("key_priority"), f"{path} key_priority")
    standard_priority = _whole_non_negative(cell("standard_priority"), f"{path} standard_priority")
    wait_min = _non_negative(cell("wait_min_hours"), f"{path} wait_min_hours")
    wait_max = _non_negative(cell("wait_max_hours"), f"{path} wait_max_hours")
    if wait_max <= wait_min:
        raise ValueError(f"{path} wait_max_hours must be above wait_min_hours.")
    return WaveSettings(
        n_orders=n_orders,
        seed=seed,
        pick_time=pick_time,
        min_lines=min_lines,
        max_lines=max_lines,
        heavy_share=heavy_share,
        light_floor_kg=light_floor_kg,
        light_margin_kg=light_margin_kg,
        heavy_over_kg=heavy_over_kg,
        heavy_margin_kg=heavy_margin_kg,
        light_volume_fill=light_fill,
        heavy_volume_fill=heavy_fill,
        volume_cap_gap=volume_cap_gap,
        must_ship_share=must_ship_share,
        key_cutoff=key_cutoff,
        must_ship_weight=must_ship_weight,
        key_priority=key_priority,
        standard_priority=standard_priority,
        wait_min_hours=wait_min,
        wait_max_hours=wait_max,
    )


def _pickers(crew: pd.DataFrame) -> pd.DataFrame:
    rows = []
    number = 1
    for crew_row in crew.itertuples(index=False):
        for _ in range(int(crew_row.count)):
            rows.append(
                {
                    "picker_id": f"P{number:02d}",
                    "equipment_type": crew_row.equipment_type,
                    "max_weight": float(crew_row.max_weight),
                    "max_volume": float(crew_row.max_volume),
                    "shift_duration": float(crew_row.shift_duration),
                }
            )
            number += 1
    return pd.DataFrame(rows)


def _weight_bands(crew: pd.DataFrame, settings: WaveSettings) -> Tuple[pd.Series, int, int, int]:
    """Return the lightest machine and the light and heavy kilogram bounds."""
    light = _one_extreme(crew, minimum=True)
    heavy = _one_extreme(crew, minimum=False)
    light_kg = int(round(float(light.max_weight)))
    heavy_kg = int(round(float(heavy.max_weight)))
    light_hi = light_kg - settings.light_margin_kg
    heavy_lo = light_kg + settings.heavy_over_kg
    heavy_hi = heavy_kg - settings.heavy_margin_kg
    if light_hi < settings.light_floor_kg:
        raise ValueError(
            "The lightest max_weight must be at least "
            f"{settings.light_floor_kg + settings.light_margin_kg} kg."
        )
    if heavy_hi < heavy_lo:
        gap = settings.heavy_over_kg + settings.heavy_margin_kg
        raise ValueError(
            f"The heaviest max_weight must be at least {gap} kg above the lightest "
            "so a heavy order fits the heavier machine only."
        )
    return light, light_hi, heavy_lo, heavy_hi


def _one_extreme(crew: pd.DataFrame, minimum: bool) -> pd.Series:
    column = crew["max_weight"]
    target = column.min() if minimum else column.max()
    rows = crew[column == target]
    if len(rows) != 1:
        end = "lightest" if minimum else "heaviest"
        raise ValueError(f"The crew needs one {end} max_weight. {len(rows)} rows share {target}.")
    return rows.iloc[0]


def _orders_and_items(
    rng: random.Random,
    n_orders: int,
    slots: Sequence[str],
    crew: pd.DataFrame,
    settings: WaveSettings,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    light, light_hi, heavy_lo, heavy_hi = _weight_bands(crew, settings)
    by_weight = crew.sort_values(["max_weight", "equipment_type"], kind="mergesort")
    order_rows: List[dict] = []
    item_rows: List[dict] = []
    line_hi = min(settings.max_lines, len(slots))
    if line_hi < settings.min_lines:
        raise ValueError(
            f"The layout has {len(slots)} slots, which is below min_lines ({settings.min_lines})."
        )
    for number in range(1, n_orders + 1):
        order_id = f"O{number:03d}"
        if rng.random() < settings.heavy_share:
            weight = rng.randint(heavy_lo, heavy_hi)
            carrier = _lightest_that_carries(by_weight, weight)
            volume = _order_volume(weight, carrier, settings.heavy_volume_fill, settings.volume_cap_gap)
            equipment = str(carrier.equipment_type)
        else:
            weight = rng.randint(settings.light_floor_kg, light_hi)
            volume = _order_volume(weight, light, settings.light_volume_fill, settings.volume_cap_gap)
            equipment = ""
        roll = rng.random()
        if roll < settings.must_ship_share:
            tier, priority, must_ship = "key", settings.must_ship_weight, "true"
        elif roll < settings.key_cutoff:
            tier, priority, must_ship = "key", settings.key_priority, ""
        else:
            tier, priority, must_ship = "standard", settings.standard_priority, ""
        order_rows.append(
            {
                "order_id": order_id,
                "total_weight": weight,
                "total_volume": volume,
                "required_equipment": equipment,
                "account_tier": tier,
                "cutoff_time": "",
                "wait_time": round(rng.uniform(settings.wait_min_hours, settings.wait_max_hours), 1),
                "must_ship": must_ship,
                "priority_weight": priority,
            }
        )
        n_lines = rng.randint(settings.min_lines, line_hi)
        for line, slot in enumerate(rng.sample(list(slots), n_lines), start=1):
            item_rows.append(
                {
                    "order_id": order_id,
                    "item_id": f"I{line}",
                    "location_id": slot,
                    "pick_time": settings.pick_time,
                }
            )
    return pd.DataFrame(order_rows), pd.DataFrame(item_rows)


def _lightest_that_carries(by_weight: pd.DataFrame, weight: int) -> pd.Series:
    carriers = by_weight[by_weight.max_weight >= weight]
    if carriers.empty:
        raise ValueError(f"No equipment can carry {weight} kg.")
    return carriers.iloc[0]


def _order_volume(weight: int, machine: pd.Series, fill: float, cap_gap: float) -> float:
    cap_kg = float(machine.max_weight)
    cap_volume = float(machine.max_volume)
    raw = round(weight / cap_kg * (cap_volume * fill), 2)
    room = round(cap_volume - cap_gap, 2)
    return min(room, raw)


def _positive_int(value: object, label: str) -> int:
    number = _positive_number(value, label)
    if not float(number).is_integer():
        raise ValueError(f"{label} must be a whole number.")
    return int(number)


def _whole(value: object, label: str) -> int:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a whole number.") from exc
    if not math.isfinite(number) or not number.is_integer():
        raise ValueError(f"{label} must be a whole number.")
    return int(number)


def _whole_non_negative(value: object, label: str) -> int:
    number = _whole(value, label)
    if number < 0:
        raise ValueError(f"{label} must be zero or more.")
    return number


def _positive_number(value: object, label: str) -> float:
    number = _finite(value, label)
    if number <= 0:
        raise ValueError(f"{label} must be a positive number.")
    return number


def _non_negative(value: object, label: str) -> float:
    number = _finite(value, label)
    if number < 0:
        raise ValueError(f"{label} must be zero or more.")
    return number


def _unit_share(value: object, label: str) -> float:
    number = _finite(value, label)
    if number < 0 or number > 1:
        raise ValueError(f"{label} must be from 0 to 1.")
    return number


def _fill(value: object, label: str) -> float:
    number = _finite(value, label)
    if number <= 0 or number > 1:
        raise ValueError(f"{label} must be above 0 and at most 1.")
    return number


def _finite(value: object, label: str) -> float:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a number.") from exc
    if not math.isfinite(number):
        raise ValueError(f"{label} must be a number.")
    return number


def main(argv: Sequence[str] | None = None) -> None:
    """Write the wave CSVs."""
    parser = argparse.ArgumentParser(description="Write a full pick wave on the six-aisle map.")
    parser.add_argument("--out", type=Path, required=True, help="Folder for the wave CSVs.")
    parser.add_argument(
        "--orders",
        type=int,
        default=None,
        help="Number of orders. Default is n_orders in the settings file.",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="Random seed. Default is seed in the settings file.",
    )
    parser.add_argument(
        "--crew",
        type=Path,
        default=None,
        help=(
            "Crew config CSV: equipment_type, count, max_weight, max_volume, shift_duration. "
            "Default inputs/crew.csv."
        ),
    )
    parser.add_argument(
        "--settings",
        type=Path,
        default=None,
        help="Wave settings CSV with columns name,value. Default inputs/wave_settings.csv.",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    folder = write_wave(
        args.out,
        n_orders=args.orders,
        seed=args.seed,
        crew_path=args.crew,
        settings_path=args.settings,
    )
    print(f"Wrote {folder}")


if __name__ == "__main__":
    main()
