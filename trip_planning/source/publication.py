"""Stage a directory, then publish it by rename or by file replacement with rollback.

New plan, build, and scenario roots are published by rename. An existing build
can replace its files one by one. A nonempty plan directory is never overwritten.
Transient Windows lock errors are retried. Other errors still fail.
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

_LOCK_ERRORS = {5, 32, 33}


def publish_new_directory(staging: Path, destination: Path) -> None:
    """Rename ``staging`` onto a path that must not already exist."""
    _refuse_link(destination)
    if destination.exists():
        raise FileExistsError(f"Refusing to overwrite {destination}.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    _rename_retry(staging, destination)


def publish_plan(staging: Path, destination: Path) -> None:
    """Publish a plan. An empty existing directory is filled. A nonempty one is refused."""
    _refuse_link(destination)
    if not destination.exists():
        publish_new_directory(staging, destination)
        return
    if any(destination.iterdir()):
        raise FileExistsError(f"Refusing to overwrite existing plan {destination}.")
    for source in list(staging.iterdir()):
        _rename_retry(source, destination / source.name)
    staging.rmdir()


def publish_replacing(staging: Path, destination: Path) -> None:
    """Replace files in an existing directory. Restore the previous files if a move fails."""
    _refuse_link(destination)
    if not destination.exists():
        publish_new_directory(staging, destination)
        return
    moved: list[tuple[Path, Path | None]] = []
    try:
        for source in list(staging.iterdir()):
            target = destination / source.name
            backup = None
            if target.exists() or target.is_symlink():
                backup = destination / f".{source.name}.publish-backup"
                if backup.exists():
                    backup.unlink()
                _rename_retry(target, backup)
            _rename_retry(source, target)
            moved.append((target, backup))
        for _target, backup in moved:
            if backup is not None and backup.exists():
                backup.unlink()
        staging.rmdir()
    except Exception:
        for target, backup in reversed(moved):
            if backup is None:
                if target.exists():
                    target.unlink()
                continue
            if target.exists():
                target.unlink()
            if backup.exists():
                backup.replace(target)
        raise


def staging_dir(destination: Path) -> Path:
    return destination.parent / f".{destination.name}.staging-{os.getpid()}"


def remove_tree(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)


def _rename_retry(source: Path, destination: Path) -> None:
    delay = 0.05
    last: OSError | None = None
    for attempt in range(5):
        try:
            os.replace(source, destination)
            return
        except OSError as exc:
            last = exc
            winerror = getattr(exc, "winerror", None)
            if winerror not in _LOCK_ERRORS or attempt == 4:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 0.5)
    if last is not None:
        raise last


def _refuse_link(path: Path) -> None:
    if path.is_symlink():
        raise ValueError(f"Refusing a symlink or junction path: {path}.")
