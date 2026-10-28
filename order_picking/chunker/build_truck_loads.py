"""Build a chunker truck-load file from the eight-aisle warehouse orders.

The picker files in ``inputs/layout_warehouse`` already are finished trips.
This script replays ``gen_warehouse_dataset.py`` with the same seed, keeps
each line's kilograms and cubic metres, and places whole source orders onto
trucks. The chunker may then split a truck. It does not keep the original
order together.

``cutoff_time`` on the picker orders is hours remaining. It is not copied.
Each truck gets one departure, in minutes from the start of the wave.
"""

from __future__ import annotations

import argparse
import contextlib
import io
from pathlib import Path
from typing import List

import pandas as pd

_ROOT = Path(__file__).resolve().parents[1]
_GENERATOR = _ROOT / "gen_warehouse_dataset.py"
DEFAULT_OUT = _ROOT / "inputs" / "chunk_warehouse" / "truck_loads.csv"

MIN_TRUCK_KG = 8000.0
MAX_TRUCK_KG = 12000.0
FIRST_DEPARTURE_MIN = 60.0
DEPARTURE_STEP_MIN = 25.0
LOAD_COLUMNS = [
    "truck_id",
    "depot_id",
    "destination_id",
    "departure_time",
    "source_order_id",
    "item_id",
    "sku_id",
    "location_id",
    "quantity",
    "weight",
    "volume",
    "pick_time",
    "must_ship",
    "account_tier",
]

_ITEM_PAIRS = (
    (
        """\
        items_rows.append(
            {
                "order_id": oid,
                "item_id": f"{oid}-{line_no}",
                "location_id": sku_to_loc[sid],
                "pick_time": PICK_TIME_PER_LINE,
            }
        )""",
        """\
        items_rows.append(
            {
                "order_id": oid,
                "item_id": f"{oid}-{line_no}",
                "sku_id": sid,
                "location_id": sku_to_loc[sid],
                "quantity": qty,
                "weight": w,
                "volume": v,
                "pick_time": PICK_TIME_PER_LINE,
            }
        )""",
    ),
    (
        """\
            items_rows.append(
                {
                    "order_id": oid,
                    "item_id": f"{oid}-{line_no}",
                    "location_id": sku_to_loc[sid],
                    "pick_time": PICK_TIME_PER_LINE,
                }
            )""",
        """\
            items_rows.append(
                {
                    "order_id": oid,
                    "item_id": f"{oid}-{line_no}",
                    "sku_id": sid,
                    "location_id": sku_to_loc[sid],
                    "quantity": qty,
                    "weight": w,
                    "volume": v,
                    "pick_time": PICK_TIME_PER_LINE,
                }
            )""",
    ),
)


def replay_order_lines(work_dir: Path) -> tuple:
    """Run the warehouse generator and return orders plus line weights.

    The line weight, volume, quantity, and SKU are the values that generator
    used before it wrote the picker files. The picker files do not keep them.
    """
    text = _GENERATOR.read_text(encoding="utf-8")
    for old_item, new_item in _ITEM_PAIRS:
        if text.count(old_item) != 1:
            raise RuntimeError("The warehouse generator no longer has the expected line writer.")
        text = text.replace(old_item, new_item, 1)
    old_fields = (
        'write_csv(OUT / "order_items.csv", items_rows, '
        '["order_id", "item_id", "location_id", "pick_time"])'
    )
    new_fields = (
        'write_csv(OUT / "order_items.csv", items_rows, '
        '["order_id", "item_id", "sku_id", "location_id", "quantity", "weight", "volume", "pick_time"])'
    )
    if old_fields not in text:
        raise RuntimeError("The warehouse generator no longer writes order_items.csv as expected.")
    text = text.replace(old_fields, new_fields, 1)
    text = text.replace(
        'OUT = Path("warehouse_dataset")',
        f'OUT = Path(r"{Path(work_dir)}")',
        1,
    )
    with contextlib.redirect_stdout(io.StringIO()):
        exec(compile(text, str(_GENERATOR), "exec"), {"__name__": "warehouse_gen"})
    orders = pd.read_csv(Path(work_dir) / "orders.csv")
    items = pd.read_csv(Path(work_dir) / "order_items.csv")
    return orders, items


def pack_truck_loads(orders: pd.DataFrame, items: pd.DataFrame) -> pd.DataFrame:
    """Place whole source orders onto trucks of about 8 to 12 tonnes.

    A source order is never split across trucks. Dock assignment alternates
    between PACK_A and PACK_B. Lines of one order share a destination.
    """
    grouped = [group.iloc[0] for _, group in orders.groupby("order_id", sort=True)]
    trucks: List[List[pd.Series]] = []
    current: List[pd.Series] = []
    current_kg = 0.0
    for order in grouped:
        weight = float(order["total_weight"])
        if current and current_kg >= MIN_TRUCK_KG and current_kg + weight > MAX_TRUCK_KG:
            trucks.append(current)
            current = []
            current_kg = 0.0
        current.append(order)
        current_kg += weight
    if current:
        trucks.append(current)

    by_order = {order_id: group for order_id, group in items.groupby("order_id", sort=False)}
    rows = []
    for index, truck in enumerate(trucks, start=1):
        truck_id = f"TRK{index:02d}"
        depot_id = "PACK_A" if index % 2 == 1 else "PACK_B"
        departure = FIRST_DEPARTURE_MIN + (index - 1) * DEPARTURE_STEP_MIN
        for order in truck:
            order_id = str(order["order_id"])
            lines = by_order[order_id]
            if abs(float(lines["weight"].sum()) - float(order["total_weight"])) > 0.02:
                raise ValueError(f"{order_id} line weights do not add up to the order total.")
            destination = f"STOP-{(int(order_id[1:]) - 1) % 4 + 1}"
            must_ship = _flag(order["must_ship"])
            for line in lines.itertuples(index=False):
                rows.append(
                    {
                        "truck_id": truck_id,
                        "depot_id": depot_id,
                        "destination_id": destination,
                        "departure_time": departure,
                        "source_order_id": order_id,
                        "item_id": line.item_id,
                        "sku_id": line.sku_id,
                        "location_id": line.location_id,
                        "quantity": line.quantity,
                        "weight": line.weight,
                        "volume": line.volume,
                        "pick_time": line.pick_time,
                        "must_ship": "true" if must_ship else "",
                        "account_tier": "",
                    }
                )
    return pd.DataFrame(rows, columns=LOAD_COLUMNS)


def write_truck_loads(out_path: Path = DEFAULT_OUT) -> pd.DataFrame:
    """Replay the warehouse generator and write ``truck_loads.csv``."""
    work = out_path.parent / "_replay"
    work.mkdir(parents=True, exist_ok=True)
    try:
        orders, items = replay_order_lines(work)
        loads = pack_truck_loads(orders, items)
    finally:
        for child in work.glob("*"):
            child.unlink()
        work.rmdir()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    loads.to_csv(out_path, index=False)
    return loads


def main(argv: List[str] | None = None) -> None:
    """Write the sample truck-load file."""
    parser = argparse.ArgumentParser(description="Write chunker truck loads for the eight-aisle warehouse.")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)
    loads = write_truck_loads(args.out)
    trucks = loads.groupby("truck_id")["weight"].sum()
    print(f"Wrote {len(loads)} lines, {len(trucks)} trucks, to {args.out}")
    print(f"Truck weight {trucks.min():.0f} to {trucks.max():.0f} kg")


def _missing(value: object) -> bool:
    if value is None:
        return True
    try:
        if pd.isna(value):
            return True
    except TypeError:
        return False
    return str(value).strip() == ""


def _flag(value: object) -> bool:
    if _missing(value):
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


if __name__ == "__main__":
    main()
