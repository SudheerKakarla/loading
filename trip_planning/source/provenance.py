"""Detached run snapshot: hashes, versions, and the resolved settings.

The snapshot is data. It does not lock the files it hashed, and it does not
record a business approval.
"""

from __future__ import annotations

import hashlib
import math
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from uuid import uuid4

import pandas as pd

from config import TripConfig


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def table_sha256(frame: pd.DataFrame) -> str:
    """Hash a table independent of row order and column order."""
    work = frame.copy()
    work.columns = [str(column) for column in work.columns]
    work = work.reindex(sorted(work.columns), axis=1).fillna("")
    for column in work.columns:
        work[column] = work[column].map(_cell_text)
    sort_columns = list(work.columns)
    if sort_columns:
        work = work.sort_values(sort_columns, kind="mergesort")
    payload = work.to_csv(index=False, lineterminator="\n").encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def git_revision(start: Path) -> str:
    try:
        output = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=start,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return ""
    return output.strip()


def environment_versions() -> dict[str, str]:
    versions = {"python": sys.version.split()[0]}
    for name in ("numpy", "pandas", "scipy"):
        try:
            module = __import__(name)
        except ImportError:
            versions[name] = ""
            continue
        versions[name] = str(getattr(module, "__version__", ""))
    versions["highs"] = _highs_version()
    return versions


def config_payload(cfg: TripConfig) -> dict:
    payload = {}
    for name in cfg.__dataclass_fields__:
        payload[name] = _json_ready(getattr(cfg, name))
    return payload


def new_run_id() -> str:
    return uuid4().hex


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _highs_version() -> str:
    try:
        from scipy.optimize._highs_wrapper import _highs_lib
    except ImportError:
        return ""
    version = getattr(_highs_lib, "highs_version", None)
    if version is None:
        return ""
    try:
        return str(version())
    except TypeError:
        return str(version)


def _cell_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and not math.isfinite(value):
        return ""
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def _json_ready(value: object):
    if isinstance(value, dict):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(item) for item in value]
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, float) and not math.isfinite(value):
        return "inf" if value > 0 else "-inf"
    return value
