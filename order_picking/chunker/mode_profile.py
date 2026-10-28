"""Per-mode caps and times. A blank cell keeps the chunker's global value.

A filled line, weight, or volume cap is the smaller of that number and the
global cap. It does not raise the global cap. Pick time and job overhead
still replace the global value. ``pallet_basis`` is ``job`` (one pallet per
job) or ``line`` (one pallet per line). The numbers in an example file are
illustrative.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

import pandas as pd

_NUMBER_COLUMNS = (
    "max_job_lines",
    "max_job_weight",
    "max_job_volume",
    "default_pick_time",
    "job_overhead",
)


@dataclass(frozen=True)
class ModeCaps:
    """Caps for one pick mode. None means use the global chunker setting."""

    max_job_lines: Optional[int] = None
    max_job_weight: Optional[float] = None
    max_job_volume: Optional[float] = None
    default_pick_time: Optional[float] = None
    job_overhead: Optional[float] = None
    pallet_basis: str = "job"


def read_mode_profile(path: Path) -> Dict[str, ModeCaps]:
    """Read ``mode_profile.csv``. Errors name the file, the row, and the column."""
    profile_path = Path(path)
    if not profile_path.is_file():
        raise ValueError(f"Mode profile not found: {profile_path}")
    frame = pd.read_csv(profile_path, index_col=False)
    frame.columns = [str(name).strip() for name in frame.columns]
    if "pick_mode" not in frame.columns:
        raise ValueError(f"{profile_path} is missing column pick_mode.")
    unknown = [name for name in frame.columns if name != "pick_mode" and name not in _NUMBER_COLUMNS and name != "pallet_basis"]
    if unknown:
        raise ValueError(f"{profile_path} has unknown column(s): {', '.join(unknown)}.")
    found: Dict[str, ModeCaps] = {}
    for index, raw in frame.iterrows():
        label = f"row {int(index) + 2} of {profile_path}"
        mode = "" if pd.isna(raw.pick_mode) else str(raw.pick_mode).strip().lower()
        if mode == "" or mode == "nan":
            raise ValueError(f"mode profile {label} column pick_mode is blank.")
        if mode in found:
            raise ValueError(f"mode profile {label} repeats pick_mode {mode}.")
        basis = _basis(raw, frame, label)
        found[mode] = ModeCaps(
            max_job_lines=_optional_int(raw, frame, "max_job_lines", label),
            max_job_weight=_optional_number(raw, frame, "max_job_weight", label, minimum=0.0, positive=True),
            max_job_volume=_optional_number(raw, frame, "max_job_volume", label, minimum=0.0, positive=True),
            default_pick_time=_optional_number(raw, frame, "default_pick_time", label, minimum=0.0, positive=False),
            job_overhead=_optional_number(raw, frame, "job_overhead", label, minimum=0.0, positive=False),
            pallet_basis=basis,
        )
    if not found:
        raise ValueError(f"{profile_path} has no mode rows.")
    return found


def _tighter(global_value, mode_value):
    """The smaller cap. A missing side leaves the side that is set."""
    if mode_value is None:
        return global_value
    if global_value is None:
        return mode_value
    return min(global_value, mode_value)


def effective_caps(settings, mode: Optional[str]) -> tuple:
    """Effective line, weight, volume, pick time, and overhead for one mode.

    Line, weight, and volume use the tighter of the global cap and the mode
    cap. Pick time and overhead use the mode value when it is set.
    """
    lines = getattr(settings, "max_job_lines", None)
    weight = getattr(settings, "max_job_weight", None)
    volume = getattr(settings, "max_job_volume", None)
    pick = getattr(settings, "default_pick_time", None)
    overhead = getattr(settings, "job_overhead", None)
    table = getattr(settings, "mode_caps", None) or {}
    caps = table.get(mode) if mode else None
    if caps is not None:
        lines = _tighter(lines, caps.max_job_lines)
        weight = _tighter(weight, caps.max_job_weight)
        volume = _tighter(volume, caps.max_job_volume)
        if caps.default_pick_time is not None:
            pick = caps.default_pick_time
        if caps.job_overhead is not None:
            overhead = caps.job_overhead
    return lines, weight, volume, pick, overhead


def pallet_basis_for(settings, mode: Optional[str]) -> str:
    """``line`` counts every line. ``job`` counts the job as one pallet."""
    table = getattr(settings, "mode_caps", None) or {}
    caps = table.get(mode) if mode else None
    if caps is None or not caps.pallet_basis:
        return "job"
    return caps.pallet_basis


def _blank(value: object) -> bool:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return True
    text = str(value).strip()
    return text == "" or text.lower() == "nan"


def _basis(raw, frame: pd.DataFrame, label: str) -> str:
    if "pallet_basis" not in frame.columns or _blank(raw.pallet_basis):
        return "job"
    text = str(raw.pallet_basis).strip().lower()
    if text not in {"job", "line"}:
        raise ValueError(f"mode profile {label} column pallet_basis must be job or line.")
    return text


def _optional_number(raw, frame: pd.DataFrame, column: str, label: str, minimum: float, positive: bool) -> Optional[float]:
    if column not in frame.columns or _blank(getattr(raw, column)):
        return None
    try:
        number = float(getattr(raw, column))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"mode profile {label} column {column} must be a number.") from exc
    if not math.isfinite(number) or number < minimum or (positive and number <= 0):
        bound = "> 0" if positive else ">= 0"
        raise ValueError(f"mode profile {label} column {column} must be a finite number {bound}.")
    return number


def _optional_int(raw, frame: pd.DataFrame, column: str, label: str) -> Optional[int]:
    number = _optional_number(raw, frame, column, label, minimum=1.0, positive=True)
    if number is None:
        return None
    if abs(number - round(number)) > 1e-9:
        raise ValueError(f"mode profile {label} column {column} must be a whole number >= 1.")
    return int(round(number))
