"""Solver-specific result folders under optimization_results/.

Example:
  optimization_results/highs/im_input_A_B/loading_instructions.csv
  optimization_results/minizinc/im_input_A_B/loading_instructions.csv
  optimization_results/mixed_length/im_input_A_B/loading_instructions.csv

Same input filename with different bytes uses <stem>_<hash8> so a stale
warm-start from another dump is not reused.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


RESULTS_DIRNAME = "optimization_results"


def results_root(repo_root: Path) -> Path:
    return Path(repo_root) / RESULTS_DIRNAME


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_run_dir(
    results_root_path: Path,
    solver: str,
    csv_path: Path,
    override: Optional[Path] = None,
) -> Path:
    """Return optimization_results/<solver>/<stem>/ (or <stem>_<hash8>)."""
    if override is not None:
        return Path(override)
    solver = str(solver).strip().lower()
    if not solver:
        raise ValueError("solver name is empty")
    stem = csv_path.stem
    digest8 = sha256_file(csv_path)[:8]
    named = Path(results_root_path) / solver / stem
    meta = named / "source_metadata.json"
    if meta.is_file():
        try:
            old = json.loads(meta.read_text(encoding="utf-8"))
            old8 = str(old.get("input_sha256") or "")[:8]
            if old8 and old8 != digest8:
                return Path(results_root_path) / solver / f"{stem}_{digest8}"
        except (OSError, json.JSONDecodeError, TypeError):
            pass
    return named


def write_source_metadata(run_dir: Path, payload: dict) -> Path:
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / "source_metadata.json"
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def metadata_payload(
    *,
    solver: str,
    csv_path: Path,
    extra: Optional[dict] = None,
) -> dict:
    body: dict[str, Any] = {
        "solver": solver,
        "scenario": csv_path.stem,
        "input_csv": str(csv_path.resolve()),
        "input_sha256": sha256_file(csv_path),
        "created_utc": datetime.now(timezone.utc).isoformat(),
    }
    if extra:
        body.update(extra)
    return body


def find_run_file(
    results_root_path: Path,
    solver: str,
    csv_path: Path,
    filename: str,
    legacy_names: Optional[list] = None,
) -> Optional[Path]:
    """Prefer the solver/scenario file; fall back to a legacy root CSV only
    when this input still maps to the unsuffixed stem folder."""
    run_dir = resolve_run_dir(results_root_path, solver, csv_path)
    current = run_dir / filename
    if current.is_file():
        return current
    if run_dir.name != csv_path.stem:
        return None
    for name in legacy_names or []:
        legacy = Path(results_root_path) / name
        if legacy.is_file():
            return legacy
    return None
