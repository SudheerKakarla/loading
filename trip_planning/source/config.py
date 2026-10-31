"""Run settings. Every business assumption from the spec is a field, not a constant."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Mapping

COMPAT_MODES = (
    "same_province",
    "same_group",
    "same_province_or_group",
    "any",
    "none",
)
HANDLING_BASES = ("per_drop", "per_truck")
TRUCK_TYPES = ("4W", "6W", "10W", "18W")


def norm_id(value: object) -> str:
    """Strip whitespace and a trailing ``.0`` left by spreadsheet ids."""
    if value is None or isinstance(value, bool):
        if isinstance(value, bool):
            return "true" if value else "false"
        return ""
    if isinstance(value, float):
        if not math.isfinite(value):
            return ""
        if value.is_integer():
            return str(int(value))
    text = str(value).strip()
    if text.lower() in {"", "nan", "none", "<na>", "nat", "inf", "-inf", "+inf"}:
        return ""
    if text.endswith(".0"):
        stem = text[:-2]
        if stem.isdigit() or (stem.startswith("-") and stem[1:].isdigit()):
            return stem
    return text


@dataclass
class TripConfig:
    """Defaults match specification section 5. CLI flags and config.yaml override them."""

    max_shipments_per_trip: int = 3
    max_early_days: int = 1
    transit_days: int = 0
    compat_mode: str = "same_province"
    max_drops_per_trip: int | None = None
    solo_only: tuple[str, ...] = ()
    handling_basis: str = "per_drop"
    max_trucks: int | None = None
    mip_rel_gap: float = 0.0
    time_limit_s: float = 600.0
    early_delivery_weight: float | None = None
    upgrade_weight: float | None = None
    transit_days_by_province: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.solo_only = tuple(norm_id(item) for item in self.solo_only if norm_id(item))
        self.transit_days_by_province = {
            str(province).strip(): _exact_int(days, f"transit_days_by_province[{province}]", minimum=0)
            for province, days in self.transit_days_by_province.items()
        }
        self.max_shipments_per_trip = _exact_int(
            self.max_shipments_per_trip, "max_shipments_per_trip", minimum=1
        )
        self.max_early_days = _exact_int(self.max_early_days, "max_early_days", minimum=0)
        self.transit_days = _exact_int(self.transit_days, "transit_days", minimum=0)
        self.max_drops_per_trip = _exact_int(
            self.max_drops_per_trip, "max_drops_per_trip", minimum=1, allow_none=True
        )
        self.max_trucks = _exact_int(self.max_trucks, "max_trucks", minimum=0, allow_none=True)
        if self.compat_mode not in COMPAT_MODES:
            raise ValueError(
                "compat_mode must be one of " + ", ".join(COMPAT_MODES) + "."
            )
        if self.handling_basis not in HANDLING_BASES:
            raise ValueError("handling_basis must be per_drop or per_truck.")
        self.mip_rel_gap = _exact_finite(self.mip_rel_gap, "mip_rel_gap", minimum=0.0)
        if self.mip_rel_gap > 1e-9:
            raise ValueError("mip_rel_gap must be between 0 and 1e-9.")
        self.time_limit_s = _exact_finite(self.time_limit_s, "time_limit_s", minimum=0.0, positive=True)
        self.early_delivery_weight = _optional_weight(self.early_delivery_weight, "early_delivery_weight")
        self.upgrade_weight = _optional_weight(self.upgrade_weight, "upgrade_weight")

    @property
    def drop_cap(self) -> int:
        if self.max_drops_per_trip is None:
            return self.max_shipments_per_trip
        return self.max_drops_per_trip

    def transit_for(self, province: str) -> int:
        if province in self.transit_days_by_province:
            return int(self.transit_days_by_province[province])
        return int(self.transit_days)


def _exact_int(value: object, name: str, *, minimum: int | None = None, allow_none: bool = False) -> int | None:
    """Reject booleans, fractional values, and numeric strings. JSON integers are ints."""
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer.")
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be >= {minimum}.")
    return value


def _exact_finite(
    value: object,
    name: str,
    *,
    minimum: float | None = None,
    positive: bool = False,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number.")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be a finite number.")
    if positive and number <= 0:
        raise ValueError(f"{name} must be a finite number > 0.")
    if minimum is not None and number < minimum:
        raise ValueError(f"{name} must be >= {minimum}.")
    return number


def _optional_weight(value: object, name: str) -> float | None:
    if value is None:
        return None
    return _exact_finite(value, name, minimum=0.0)


def tie_break_weights(n_shipments: int, max_early_days: int) -> tuple[float, float]:
    """Keep the total tie-break below 1 THB, which is the cost resolution.

    The safe bound on early days plus LTL-upgrade counts is
    ``n_shipments * (max_early_days + 1)``.
    """
    bound = max(0, int(n_shipments)) * (int(max_early_days) + 1)
    weight = 0.5 / (1.0 + bound)
    return weight, weight


def config_field_names() -> set[str]:
    return {item.name for item in fields(TripConfig)}


def load_config_file(path: Path) -> dict:
    """Read a flat YAML or JSON config. Nested YAML needs PyYAML."""
    text = Path(path).read_text(encoding="utf-8-sig")
    suffix = Path(path).suffix.lower()
    if suffix == ".json":
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError(f"Config {path} must be a JSON object.")
        return _coerce_config_dict(data)
    try:
        import yaml  # type: ignore
    except ImportError:
        yaml = None
    if yaml is not None and suffix in {".yaml", ".yml"}:
        data = yaml.safe_load(text)
        if data is None:
            return {}
        if not isinstance(data, dict):
            raise ValueError(f"Config {path} must be a mapping.")
        return _coerce_config_dict(data)
    return _coerce_config_dict(_parse_flat_yaml(text, path))


def _coerce_config_dict(data: Mapping) -> dict:
    out = dict(data)
    solo = out.get("solo_only")
    if isinstance(solo, str):
        out["solo_only"] = tuple(part.strip() for part in solo.split(",") if part.strip())
    elif isinstance(solo, list):
        out["solo_only"] = tuple(solo)
    raw_transit = out.get("transit_days_by_province")
    if isinstance(raw_transit, str):
        text = raw_transit.strip()
        out["transit_days_by_province"] = {} if text in {"", "{}"} else json.loads(text)
    return out


def _parse_flat_yaml(text: str, path: Path) -> dict:
    data: dict = {}
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        raw = lines[index]
        index += 1
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if line[:1] in {" ", "\t"}:
            raise ValueError(
                f"{path} uses nested YAML. Install PyYAML, or put the value on one line as JSON."
            )
        if ":" not in line:
            raise ValueError(f"Cannot parse config line in {path}: {raw}")
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if value == "":
            items = []
            while index < len(lines):
                nxt = lines[index].split("#", 1)[0].rstrip()
                if not nxt.strip():
                    index += 1
                    continue
                stripped = nxt.strip()
                if stripped.startswith("- "):
                    items.append(_parse_scalar(stripped[2:].strip()))
                    index += 1
                    continue
                break
            data[key] = items
            continue
        data[key] = _parse_scalar(value)
    return data


def _parse_scalar(value: str):
    if value == "[]":
        return []
    if value == "{}":
        return {}
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [_parse_scalar(part.strip()) for part in inner.split(",")]
    if value.startswith("{") and value.endswith("}"):
        return json.loads(value)
    lower = value.lower()
    if lower in {"true", "yes"}:
        return True
    if lower in {"false", "no"}:
        return False
    if lower in {"null", "none", "~"}:
        return None
    try:
        if any(mark in lower for mark in (".", "e")):
            return float(value)
        return int(value)
    except ValueError:
        return value.strip().strip('"').strip("'")
