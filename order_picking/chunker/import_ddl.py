"""Turn a caller-mapped export into demand the allocator and chunker can read.

The column names live in ``column_map.csv`` (``source_column,target_column``).
This adapter does not hard-code a site or SAP name. A bad quantity, a
repeated line, or a missing SKU is written to ``import_rejected.csv`` and
the process exits non-zero. There is no SQL connection.

Run from ``order_picking/``::

    python chunker/import_ddl.py --source export.csv --column-map column_map.csv --sku-master sku_master.csv --out outputs/imported
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import pandas as pd

_DEMAND_COLUMNS = (
    "group_id",
    "truck_id",
    "depot_id",
    "sku_id",
    "batch_id",
    "quantity",
    "uom",
    "destination_id",
    "must_ship",
    "departure_time",
    "account_tier",
    "location_id",
    "item_id",
    "weight",
    "volume",
)
_REQUIRED_TARGETS = ("depot_id", "sku_id", "quantity")
_OPTIONAL_TARGETS = (
    "group_id",
    "truck_id",
    "batch_id",
    "uom",
    "destination_id",
    "must_ship",
    "departure_time",
    "account_tier",
    "location_id",
    "item_id",
)


def import_demand(source: Path, column_map: Path, sku_master: Path, out_dir: Path) -> int:
    """Write ``demand.csv``. Return 1 when any row was rejected."""
    source_path = Path(source)
    map_path = Path(column_map)
    master_path = Path(sku_master)
    export = _read_table(source_path)
    mapping = _read_column_map(map_path, export)
    master = _read_master(master_path)
    kept: List[dict] = []
    rejected: List[dict] = []
    seen = set()
    for offset, raw in enumerate(export.to_dict(orient="records"), start=2):
        mapped = {target: str(raw[source_name]).strip() for source_name, target in mapping.items()}
        sku_id = mapped.get("sku_id", "")
        group_id = mapped.get("group_id", "")
        truck_id = mapped.get("truck_id", "")
        location_id = mapped.get("location_id", "")
        batch_id = mapped.get("batch_id", "")
        destination_id = mapped.get("destination_id", "")
        quantity, quantity_error = _quantity(mapped.get("quantity", ""), offset, source_path)
        if quantity_error:
            rejected.append(_rejection(offset, sku_id, "bad_quantity", group_id or truck_id, location_id, quantity_error))
            continue
        for name in ("depot_id", "sku_id"):
            if mapped.get(name, "") == "":
                raise ValueError(f"{source_path} row {offset} column {name} is blank.")
        if group_id == "" and truck_id == "":
            raise ValueError(f"{source_path} row {offset} needs a mapped group_id or truck_id.")
        key = (group_id or truck_id, sku_id, location_id, batch_id, destination_id)
        if key in seen:
            rejected.append(_rejection(offset, sku_id, "duplicate", group_id or truck_id, location_id, ""))
            continue
        seen.add(key)
        sku = master.get(sku_id)
        if sku is None:
            rejected.append(_rejection(offset, sku_id, "missing_sku", group_id or truck_id, location_id, ""))
            continue
        item_id = mapped.get("item_id", "") or f"I{len(kept) + 1}"
        weight = "" if sku["weight"] is None else quantity * sku["weight"]
        volume = "" if sku["volume"] is None else quantity * sku["volume"]
        kept.append(
            {
                "group_id": group_id,
                "truck_id": truck_id,
                "depot_id": mapped.get("depot_id", ""),
                "sku_id": sku_id,
                "batch_id": batch_id,
                "quantity": quantity,
                "uom": mapped.get("uom", ""),
                "destination_id": destination_id,
                "must_ship": mapped.get("must_ship", ""),
                "departure_time": mapped.get("departure_time", ""),
                "account_tier": mapped.get("account_tier", ""),
                "location_id": location_id,
                "item_id": item_id,
                "weight": weight,
                "volume": volume,
            }
        )
    folder = Path(out_dir)
    folder.mkdir(parents=True, exist_ok=True)
    present = _present_columns(kept, mapping)
    pd.DataFrame(kept, columns=present).to_csv(folder / "demand.csv", index=False)
    if "location_id" in present and "weight" in present:
        load_columns = [
            name
            for name in (
                "group_id",
                "truck_id",
                "depot_id",
                "item_id",
                "sku_id",
                "location_id",
                "quantity",
                "weight",
                "volume",
                "destination_id",
                "must_ship",
                "departure_time",
                "account_tier",
                "batch_id",
            )
            if name in present
        ]
        pd.DataFrame(kept, columns=load_columns).to_csv(folder / "truck_loads.csv", index=False)
    pd.DataFrame(
        rejected,
        columns=["source_row", "sku_id", "reason", "group_id", "location_id", "detail"],
    ).to_csv(folder / "import_rejected.csv", index=False)
    print(f"Wrote {len(kept)} lines and {len(rejected)} rejected rows to {folder}")
    return 1 if rejected else 0


def _present_columns(kept: Sequence[dict], mapping: Dict[str, str]) -> List[str]:
    targets = set(mapping.values())
    present = []
    for name in _DEMAND_COLUMNS:
        if name in {"item_id", "weight", "volume"} and "location_id" not in targets:
            continue
        if name == "weight" and kept and all(row["weight"] == "" for row in kept):
            continue
        if name == "volume" and kept and all(row["volume"] == "" for row in kept):
            continue
        if name not in targets and name not in {"item_id", "weight", "volume", "group_id", "truck_id"}:
            continue
        if name == "group_id" and "group_id" not in targets:
            continue
        if name == "truck_id" and "truck_id" not in targets:
            continue
        if not kept and name not in targets and name not in {"item_id", "weight", "volume"}:
            continue
        present.append(name)
    if not kept:
        present = [name for name in ("group_id", "truck_id", "depot_id", "sku_id", "quantity") if name in targets or name in {"depot_id", "sku_id", "quantity"}]
        if "group_id" not in targets:
            present = [name for name in present if name != "group_id"]
        if "truck_id" not in targets:
            present = [name for name in present if name != "truck_id"]
    return present


def _read_table(path: Path) -> pd.DataFrame:
    if not path.is_file():
        raise ValueError(f"Export not found: {path}")
    frame = pd.read_csv(path, dtype=str, keep_default_na=False)
    frame.columns = [str(name).strip() for name in frame.columns]
    if frame.empty:
        raise ValueError(f"{path} has no rows.")
    return frame


def _read_column_map(path: Path, export: pd.DataFrame) -> Dict[str, str]:
    """Return source name to target name. Required targets must be mapped."""
    if not path.is_file():
        raise ValueError(f"Column map not found: {path}")
    frame = pd.read_csv(path, dtype=str, keep_default_na=False)
    frame.columns = [str(name).strip() for name in frame.columns]
    missing = [name for name in ("source_column", "target_column") if name not in frame.columns]
    if missing:
        raise ValueError(f"{path} is missing column(s): {', '.join(missing)}.")
    if frame.empty:
        raise ValueError(f"{path} has no rows.")
    mapping: Dict[str, str] = {}
    targets = set()
    allowed = set(_REQUIRED_TARGETS) | set(_OPTIONAL_TARGETS)
    for offset, row in enumerate(frame.itertuples(index=False), start=2):
        label = f"row {offset} of {path}"
        source_name = str(row.source_column).strip()
        target = str(row.target_column).strip()
        if source_name == "":
            raise ValueError(f"column map {label} column source_column is blank.")
        if target == "":
            raise ValueError(f"column map {label} column target_column is blank.")
        if target not in allowed:
            raise ValueError(f"column map {label} column target_column {target} is not a demand field.")
        if source_name not in export.columns:
            raise ValueError(f"column map {label} column source_column {source_name} is not in the export.")
        if source_name in mapping:
            raise ValueError(f"column map {label} repeats source_column {source_name}.")
        if target in targets:
            raise ValueError(f"column map {label} repeats target_column {target}.")
        targets.add(target)
        mapping[source_name] = target
    absent = [name for name in _REQUIRED_TARGETS if name not in targets]
    if absent:
        raise ValueError(f"{path} does not map a column to {', '.join(absent)}.")
    if "group_id" not in targets and "truck_id" not in targets:
        raise ValueError(f"{path} does not map a column to group_id or truck_id.")
    return mapping


def _read_master(path: Path) -> Dict[str, dict]:
    """Per-unit weight and optional volume, keyed by sku_id. Do not guess a weight."""
    if not path.is_file():
        raise ValueError(f"SKU master not found: {path}")
    frame = pd.read_csv(path, dtype=str, keep_default_na=False)
    frame.columns = [str(name).strip() for name in frame.columns]
    weight_column = "unit_weight_kg" if "unit_weight_kg" in frame.columns else "weight" if "weight" in frame.columns else ""
    if "sku_id" not in frame.columns or weight_column == "":
        raise ValueError(f"{path} needs columns sku_id and weight or unit_weight_kg.")
    volume_column = "unit_volume_m3" if "unit_volume_m3" in frame.columns else "volume" if "volume" in frame.columns else ""
    found: Dict[str, dict] = {}
    for offset, row in enumerate(frame.itertuples(index=False), start=2):
        label = f"row {offset} of {path}"
        sku_id = str(row.sku_id).strip()
        if sku_id == "":
            raise ValueError(f"sku master {label} column sku_id is blank.")
        if sku_id in found:
            raise ValueError(f"sku master {label} repeats sku_id {sku_id}.")
        weight = _positive(getattr(row, weight_column), label, weight_column, path)
        volume = None
        if volume_column and str(getattr(row, volume_column)).strip() != "":
            volume = _positive(getattr(row, volume_column), label, volume_column, path)
        found[sku_id] = {"weight": weight, "volume": volume}
    if not found:
        raise ValueError(f"{path} has no SKU rows.")
    return found


def _quantity(value: str, offset: int, path: Path) -> Tuple[Optional[float], str]:
    label = f"{path} row {offset} column quantity"
    if value == "":
        return None, f"{label} is blank."
    try:
        number = float(value)
    except ValueError:
        return None, f"{label} must be a number."
    if not math.isfinite(number) or number <= 0:
        return None, f"{label} must be a finite number > 0."
    return number, ""


def _positive(value: object, label: str, column: str, path: Path) -> float:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{path} {label} column {column} must be a number.") from exc
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{path} {label} column {column} must be a finite number > 0.")
    return number


def _rejection(source_row: int, sku_id: str, reason: str, group_id: str, location_id: str, detail: str) -> dict:
    return {
        "source_row": source_row,
        "sku_id": sku_id,
        "reason": reason,
        "group_id": group_id,
        "location_id": location_id,
        "detail": detail,
    }


def main(argv: Optional[Sequence[str]] = None) -> None:
    """Map an export and exit non-zero when rows were rejected."""
    parser = argparse.ArgumentParser(description="Map an export into a demand file.")
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--column-map", type=Path, required=True)
    parser.add_argument("--sku-master", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        code = import_demand(args.source, args.column_map, args.sku_master, args.out)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        sys.exit(1)
    if code:
        sys.exit(code)


if __name__ == "__main__":
    main()
