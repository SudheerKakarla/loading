"""Picker task sheet. One row per stop, in the chunker's item order.

Stops follow the line order of order_items.csv (the chunker's tour order).
The sequence of jobs for one picker is earliest deadline first. This command
does not call the solver, and it does not claim a warehouse system can import
the file.
"""

from __future__ import annotations

import argparse
import math
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence

import pandas as pd

from assignment_reports import deadline_minutes, schedule_lateness

TOUR_NOTE = "Stops follow the line order of order_items.csv (the chunker's tour order)."
_COLUMNS = (
    "picker_id",
    "equipment_type",
    "sequence",
    "job_id",
    "truck_id",
    "depot_id",
    "pick_mode",
    "location_id",
    "item_id",
    "quantity",
    "weight",
    "planned_start_minute",
    "planned_finish_minute",
    "deadline_minute",
    "minutes_late",
)


def build_task_sheet(
    assignments: pd.DataFrame,
    orders: pd.DataFrame,
    items: pd.DataFrame,
    pickers: pd.DataFrame,
    travel: Optional[pd.DataFrame] = None,
    depot_id: Optional[str] = None,
) -> pd.DataFrame:
    """One stop per item line, sequenced by each picker's deadline order.

    With a travel table and a depot, each stop's times are the walk from the
    previous location plus that line's pick time. The walk back to the dock
    is not a row. Without both, the time cells are blank.
    """
    named_depot = "" if depot_id is None else str(depot_id).strip()
    if (travel is None) != (named_depot == ""):
        raise ValueError("Pass a travel table and a depot together, or neither.")
    lookup = None if travel is None else _travel_lookup(travel)
    grouped: Dict[str, List[str]] = {}
    durations: Dict[str, float] = {}
    for row in assignments.itertuples(index=False):
        picker_id = str(row.picker_id).strip()
        order_id = str(row.order_id).strip()
        grouped.setdefault(picker_id, []).append(order_id)
        durations[order_id] = float(row.duration)
    deadlines: Dict[str, float] = {}
    order_fields = _order_fields(orders)
    if "cutoff_time" in orders.columns:
        for row in orders.itertuples(index=False):
            deadline = deadline_minutes(getattr(row, "cutoff_time", None))
            if deadline is not None:
                deadlines[str(row.order_id).strip()] = deadline
    equipment = _equipment(pickers)
    timed = schedule_lateness(grouped, durations, deadlines)
    by_picker: Dict[str, List[dict]] = {}
    for row in timed:
        by_picker.setdefault(str(row["picker_id"]), []).append(row)
    item_groups = _items_by_order(items)
    sheet: List[dict] = []
    for picker_id in sorted(by_picker):
        sequence = 0
        for job in by_picker[picker_id]:
            order_id = str(job["order_id"])
            lines = item_groups.get(order_id) or [_blank_item()]
            duration = float(durations[order_id])
            finish = float(job["finish_minute"])
            start = finish - duration
            fields = order_fields.get(order_id, {})
            clock = start
            previous = named_depot
            for line in lines:
                sequence += 1
                if lookup is None:
                    line_start: object = ""
                    line_finish: object = ""
                else:
                    location = line.get("location_id", "")
                    clock += _leg(lookup, previous, location)
                    line_start = clock
                    clock += _pick_minutes(line)
                    line_finish = clock
                    if location:
                        previous = location
                sheet.append(
                    {
                        "picker_id": picker_id,
                        "equipment_type": equipment.get(picker_id, ""),
                        "sequence": sequence,
                        "job_id": order_id,
                        "truck_id": fields.get("truck_id", ""),
                        "depot_id": fields.get("depot_id", ""),
                        "pick_mode": fields.get("pick_mode", ""),
                        "location_id": line.get("location_id", ""),
                        "item_id": line.get("item_id", ""),
                        "quantity": line.get("quantity", ""),
                        "weight": line.get("weight", ""),
                        "planned_start_minute": line_start,
                        "planned_finish_minute": line_finish,
                        "deadline_minute": job["deadline_minute"],
                        "minutes_late": job["minutes_late"],
                    }
                )
    return pd.DataFrame(sheet, columns=list(_COLUMNS))


def write_task_sheet(frame: pd.DataFrame, path: Path) -> None:
    """Write the sheet. The first row is the column header."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(destination, index=False)


def write_task_html(frame: pd.DataFrame, path: Path) -> None:
    """Printable page grouped by picker. The standard library writes the file."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    parts = [
        "<!DOCTYPE html>",
        "<html lang=\"en\">",
        "<head>",
        "<meta charset=\"utf-8\">",
        "<title>Picker task sheet</title>",
        "<style>",
        "body { margin: 24px; font-family: \"Segoe UI\", system-ui, sans-serif; color: #102a43; background: #f4f7fb; }",
        "h1 { font-size: 22px; margin-bottom: 4px; }",
        "p { margin-top: 0; color: #334e68; }",
        "h2 { font-size: 18px; margin-top: 28px; }",
        "table { border-collapse: collapse; width: 100%; background: white; }",
        "th, td { border: 1px solid #bcccdc; padding: 6px 8px; text-align: left; font-size: 13px; }",
        "th { background: #d9e2ec; }",
        "</style>",
        "</head>",
        "<body>",
        "<h1>Picker task sheet</h1>",
        f"<p>{TOUR_NOTE} Stop times are the walk along that order plus each line's pick time. The walk back to the dock is not a row. Without a travel file those cells are blank.</p>",
    ]
    if frame.empty:
        parts.append("<p>No stops.</p>")
    else:
        for picker_id, group in frame.groupby("picker_id", sort=True):
            equipment = str(group.equipment_type.iloc[0])
            label = f"{picker_id}" if equipment == "" else f"{picker_id} ({equipment})"
            parts.append(f"<h2>{_escape(label)}</h2>")
            parts.append("<table>")
            parts.append("<tr>" + "".join(f"<th>{_escape(name)}</th>" for name in _COLUMNS) + "</tr>")
            for row in group.itertuples(index=False):
                cells = "".join(f"<td>{_escape(getattr(row, name))}</td>" for name in _COLUMNS)
                parts.append(f"<tr>{cells}</tr>")
            parts.append("</table>")
    parts.append("</body></html>")
    destination.write_text("\n".join(parts), encoding="utf-8")


def _order_fields(orders: pd.DataFrame) -> Dict[str, dict]:
    found: Dict[str, dict] = {}
    for row in orders.to_dict(orient="records"):
        order_id = str(row.get("order_id", "")).strip()
        found[order_id] = {
            "truck_id": _cell(row, "truck_id"),
            "depot_id": _cell(row, "depot_id"),
            "pick_mode": _cell(row, "pick_mode"),
        }
    return found


def _items_by_order(items: pd.DataFrame) -> Dict[str, List[dict]]:
    grouped: Dict[str, List[dict]] = {}
    for row in items.to_dict(orient="records"):
        order_id = str(row.get("order_id", "")).strip()
        grouped.setdefault(order_id, []).append(
            {
                "location_id": _cell(row, "location_id"),
                "item_id": _cell(row, "item_id"),
                "quantity": _cell(row, "quantity"),
                "weight": _cell(row, "weight"),
                "pick_time": row.get("pick_time", ""),
            }
        )
    return grouped


def _equipment(pickers: pd.DataFrame) -> Dict[str, str]:
    found: Dict[str, str] = {}
    if "picker_id" not in pickers.columns:
        return found
    for row in pickers.to_dict(orient="records"):
        found[str(row.get("picker_id", "")).strip()] = _cell(row, "equipment_type")
    return found


def _blank_item() -> dict:
    return {"location_id": "", "item_id": "", "quantity": "", "weight": "", "pick_time": ""}


def _travel_lookup(travel: pd.DataFrame) -> Dict[str, Dict[str, float]]:
    found: Dict[str, Dict[str, float]] = {}
    for row in travel.itertuples(index=False):
        origin = str(row.from_location).strip()
        dest = str(row.to_location).strip()
        found.setdefault(origin, {})[dest] = float(row.travel_time)
    return found


def _leg(lookup: Mapping[str, Mapping[str, float]], origin: str, dest: str) -> float:
    if dest == "" or origin == dest:
        return 0.0
    try:
        return float(lookup[origin][dest])
    except KeyError as exc:
        raise ValueError(f"Travel time from {origin} to {dest} is missing.") from exc


def _pick_minutes(line: Mapping[str, object]) -> float:
    raw = line.get("pick_time", "")
    if raw is None or raw == "" or (isinstance(raw, float) and pd.isna(raw)):
        return 0.0
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"pick_time must be a number, got {raw!r}.") from exc
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"pick_time must be a finite number >= 0, got {raw!r}.")
    return value


def _cell(row: Mapping[str, object], column: str) -> str:
    if column not in row:
        return ""
    value = row[column]
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    return str(value).strip()


def _escape(value: object) -> str:
    text = "" if value is None else str(value)
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def main(argv: Optional[Sequence[str]] = None) -> None:
    """Write the task sheet from an assignment file. This command does not solve."""
    parser = argparse.ArgumentParser(description="Write a picker task sheet from an assignment file.")
    parser.add_argument("--assignments", type=Path, required=True, help="CSV with picker_id, order_id, duration.")
    parser.add_argument("--orders", type=Path, required=True)
    parser.add_argument("--items", type=Path, required=True)
    parser.add_argument("--pickers", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True, help="task_sheet.csv")
    parser.add_argument("--html", type=Path, default=None, help="Optional printable page.")
    parser.add_argument("--travel", type=Path, default=None, help="from_location, to_location, travel_time.")
    parser.add_argument("--depot", default=None, help="Dock the walk starts from. Required with --travel.")
    args = parser.parse_args(list(argv) if argv is not None else None)
    travel = pd.read_csv(args.travel) if args.travel is not None else None
    frame = build_task_sheet(
        pd.read_csv(args.assignments),
        pd.read_csv(args.orders),
        pd.read_csv(args.items),
        pd.read_csv(args.pickers),
        travel=travel,
        depot_id=args.depot,
    )
    write_task_sheet(frame, args.out)
    if args.html is not None:
        write_task_html(frame, args.html)
    print(f"Wrote {len(frame)} stops to {args.out}")


if __name__ == "__main__":
    main()
