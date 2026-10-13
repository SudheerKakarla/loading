"""Two browser pages for the picking solutions: the warehouse walks and the assignment.

Each build adds one scenario to the dropdown on those pages. Choosing a scenario
redraws that layout and that shift. The warehouse map frames the selected order.
Scroll zooms toward the pointer, drag pans, and Reset shows the whole floor.
The pages do not animate a picker, and they do not chain orders into one walk.

Open ``warehouse.html`` in a browser. No server is required.
"""

from __future__ import annotations

import json
import math
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

_ROOT = Path(__file__).resolve().parents[1]
for _folder in ("source", "visualizer"):
    _path = str(_ROOT / _folder)
    if _path not in sys.path:
        sys.path.insert(0, _path)

from build_travel_matrix import load_layout, route_legs

_SCENARIO_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,80}$")

_COLORS = (
    "#0072B2",
    "#E69F00",
    "#009E73",
    "#D55E00",
    "#CC79A7",
    "#56B4E9",
    "#882255",
    "#332288",
)
_LANE = 0.42


def _num(value: float) -> float:
    return round(float(value), 4)


def _optional_number(value: object) -> Optional[float]:
    """Finite number, or None when the cell is blank."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return _num(number)


def _by_id(frame, key_name: str, column: str) -> Dict[str, Optional[float]]:
    """Map an id column to one numeric column. A missing column yields {}."""
    if frame is None or column not in frame.columns or key_name not in frame.columns:
        return {}
    return {
        str(getattr(row, key_name)): _optional_number(getattr(row, column))
        for row in frame.itertuples(index=False)
    }


def _panels(result: dict, pickers) -> Tuple[List[Tuple[str, List[str]]], Dict[str, float]]:
    """Picker order from the pickers table, then any assigned picker missing there."""
    if pickers is None:
        raise ValueError("A pickers table is required.")
    shifts: Dict[str, float] = {}
    order: List[str] = []
    for row in pickers.itertuples(index=False):
        picker_id = str(row.picker_id)
        if picker_id in shifts:
            raise ValueError(f"Duplicate picker_id {picker_id}.")
        shift = float(row.shift_duration)
        if not math.isfinite(shift) or shift < 0:
            raise ValueError(f"Picker {picker_id} has no usable shift_duration.")
        shifts[picker_id] = shift
        order.append(picker_id)
    assignments = result.get("picker_assignments")
    if assignments is None:
        raise ValueError("result has no picker_assignments.")
    normalized = {
        str(picker_id): [str(order_id) for order_id in order_ids]
        for picker_id, order_ids in assignments.items()
    }
    for picker_id in normalized:
        if picker_id not in shifts:
            raise ValueError(f"Picker {picker_id} has assignments and no shift_duration.")
    if not order:
        raise ValueError("result has no picker_assignments.")
    panels = [(picker_id, normalized.get(picker_id, [])) for picker_id in order]
    return panels, shifts


def _route_stops(routes: dict, order_id: str) -> List[str]:
    route = routes.get(order_id) if isinstance(routes, dict) else None
    stops = route.get("route") if isinstance(route, dict) else None
    try:
        named = [str(stop) for stop in stops]
    except TypeError:
        named = []
    if len(named) < 2:
        raise ValueError(f"Order {order_id} has no route.")
    return named


def _duration(routes: dict, order_id: str) -> float:
    route = routes.get(order_id) if isinstance(routes, dict) else None
    minutes = route.get("route_duration") if isinstance(route, dict) else None
    if minutes is None:
        raise ValueError(f"Order {order_id} has no route duration.")
    value = float(minutes)
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"Order {order_id} has no route duration.")
    return value


def _unfulfilled(result: dict) -> List[dict]:
    rows: List[dict] = []
    for item in result.get("orders_not_fulfilled") or []:
        if isinstance(item, dict):
            rows.append(
                {
                    "order_id": str(item.get("order_id", "")),
                    "reason": str(item.get("reason") or ""),
                }
            )
        else:
            rows.append({"order_id": str(item), "reason": ""})
    return rows


def _legs_for(
    aisles,
    cross_aisles,
    locations,
    stops: Sequence[str],
    depot_id: str,
    depot_x: float,
    depot_y: float,
    speed: float,
) -> List[dict]:
    drawn: List[dict] = []
    for leg in route_legs(
        aisles,
        cross_aisles,
        locations,
        stops,
        depot_id=depot_id,
        depot_x=depot_x,
        depot_y=depot_y,
        speed=speed,
    ):
        if leg.points is None:
            points = []
            if leg.ends is not None:
                (x0, y0), (x1, y1) = leg.ends
                points = [[_num(x0), _num(y0)], [_num(x1), _num(y1)]]
            drawn.append({"gap": True, "points": points, "note": leg.note})
            continue
        drawn.append(
            {
                "gap": False,
                "points": [[_num(x), _num(y)] for x, y in leg.points],
                "note": "",
            }
        )
    return drawn


def picking_view(
    aisles,
    cross_aisles,
    locations,
    result: dict,
    pickers,
    depot_id: str,
    depot_x: float,
    depot_y: float,
    speed: float = 1.0,
    orders=None,
) -> dict:
    """JSON-ready view of the layout, each assigned trip, and the shift rows.

    An assigned order with no visit list or no duration raises. Orders in
    ``orders_not_fulfilled`` are listed and are not given a walk. When
    ``orders`` is passed, each trip keeps its weight and volume so the page
    can show them against the picker's per-trip limits.
    """
    panels, shifts = _panels(result, pickers)
    routes = result.get("routes") or {}
    parsed_aisles, parsed_crosses, slots = load_layout(aisles, cross_aisles, locations)
    aisle_x = {aisle.aisle_id: aisle.x for aisle in parsed_aisles}
    order_ids = [order_id for _, assigned in panels for order_id in assigned]
    colors = {order_id: _COLORS[index % len(_COLORS)] for index, order_id in enumerate(sorted(set(order_ids)))}
    order_weight = _by_id(orders, "order_id", "total_weight")
    order_volume = _by_id(orders, "order_id", "total_volume")
    picker_weight = _by_id(pickers, "picker_id", "max_weight")
    picker_volume = _by_id(pickers, "picker_id", "max_volume")
    drawn_orders: Dict[str, dict] = {}
    for order_id in order_ids:
        stops = _route_stops(routes, order_id)
        legs = _legs_for(
            aisles,
            cross_aisles,
            locations,
            stops,
            depot_id,
            depot_x,
            depot_y,
            speed,
        )
        depot_name = str(depot_id)
        for index, leg in enumerate(legs):
            destination = stops[index + 1] if index + 1 < len(stops) else ""
            leg["role"] = "return" if destination == depot_name else "forward"
        drawn_orders[order_id] = {
            "duration": _duration(routes, order_id),
            "weight": order_weight.get(order_id),
            "volume": order_volume.get(order_id),
            "stops": stops,
            "legs": legs,
        }
    return {
        "depot": {"id": str(depot_id), "x": _num(depot_x), "y": _num(depot_y)},
        "aisles": [
            {
                "id": aisle.aisle_id,
                "x": _num(aisle.x),
                "y_min": _num(aisle.y_min),
                "y_max": _num(aisle.y_max),
                "one_way": aisle.one_way,
            }
            for aisle in parsed_aisles
        ],
        "crosses": [
            {
                "id": cross.cross_id,
                "y": _num(cross.y),
                "x_min": _num(cross.x_min),
                "x_max": _num(cross.x_max),
                "one_way": cross.one_way,
            }
            for cross in parsed_crosses
        ],
        "slots": [
            {"id": slot.location_id, "x": _num(aisle_x[slot.aisle_id]), "y": _num(slot.y)}
            for slot in slots
        ],
        "pickers": [
            {
                "id": picker_id,
                "shift": _num(shifts[picker_id]),
                "max_weight": picker_weight.get(picker_id),
                "max_volume": picker_volume.get(picker_id),
                "orders": order_list,
            }
            for picker_id, order_list in panels
        ],
        "orders": drawn_orders,
        "unfulfilled": _unfulfilled(result),
        "colors": colors,
        "lane": _LANE,
    }


def write_picking_pages(
    aisles,
    cross_aisles,
    locations,
    result: dict,
    pickers,
    depot_id: str,
    depot_x: float,
    depot_y: float,
    out_dir: Path,
    speed: float = 1.0,
    orders=None,
    scenario_id: Optional[str] = None,
    label: Optional[str] = None,
) -> Tuple[Path, Path]:
    """Write the two pages and register this solve in their scenario dropdown."""
    view = picking_view(
        aisles,
        cross_aisles,
        locations,
        result,
        pickers,
        depot_id,
        depot_x,
        depot_y,
        speed,
        orders=orders,
    )
    folder = Path(out_dir)
    folder.mkdir(parents=True, exist_ok=True)
    scenario_id = _checked_scenario_id(scenario_id or folder.name)
    _write_scenario(folder, scenario_id, label or scenario_id.replace("_", " "), view)
    warehouse = folder / "warehouse.html"
    assignment = folder / "assignment.html"
    warehouse.write_text(_page_html("warehouse"), encoding="utf-8")
    assignment.write_text(_page_html("assignment"), encoding="utf-8")
    return warehouse, assignment


def _checked_scenario_id(scenario_id: str) -> str:
    if not _SCENARIO_ID.match(scenario_id) or scenario_id in {"visualizer", "scenarios"}:
        raise ValueError(
            "Pass --scenario-id using letters, numbers, hyphens, or underscores. "
            f"{scenario_id!r} is not a scenario name."
        )
    return scenario_id


def _write_scenario(folder: Path, scenario_id: str, label: str, view: dict) -> None:
    """Store one view and add it to the dropdown index. The same id is replaced."""
    payload = json.dumps(view, separators=(",", ":")).replace("<", "\\u003c")
    scenarios = folder / "scenarios"
    scenarios.mkdir(parents=True, exist_ok=True)
    (scenarios / f"{scenario_id}.js").write_text(
        "window.PICKING_VIEWS = window.PICKING_VIEWS || {};\n"
        f"window.PICKING_VIEWS[{json.dumps(scenario_id)}] = {payload};\n",
        encoding="utf-8",
    )
    index_path = folder / "scenario_index.json"
    index: List[dict] = []
    if index_path.is_file():
        loaded = json.loads(index_path.read_text(encoding="utf-8"))
        if not isinstance(loaded, list):
            raise ValueError(f"{index_path} is not a list of scenarios.")
        index = [item for item in loaded if item.get("id") != scenario_id]
    index.append({"id": scenario_id, "label": label})
    index_path.write_text(json.dumps(index, indent=2) + "\n", encoding="utf-8")
    (folder / "scenario_index.js").write_text(
        "window.PICKING_INDEX = " + json.dumps(index) + ";\n",
        encoding="utf-8",
    )


def _page_html(page: str) -> str:
    body = _WAREHOUSE_BODY if page == "warehouse" else _ASSIGNMENT_BODY
    title = "Warehouse walks" if page == "warehouse" else "Assignment"
    return (
        _SHELL.replace("__TITLE__", title)
        .replace("__PAGE__", page)
        .replace("__BODY__", body)
        .replace("__CSS__", _CSS)
        .replace("__SCRIPT__", _SCRIPT)
    )


def result_from_assignments(assignments, orders, items, travel, depot_id: str) -> dict:
    """Pair orders from an assignment CSV and attach a phase-1 route to each.

    Bar minutes are the durations in the CSV when that column is present.
    The visit list is the phase-1 route. Orders in ``orders`` that are absent
    from the CSV are reported as not in the assignment file.
    """
    from order_picking_optimizer import route_orders

    routes = {
        order_id: dict(route)
        for order_id, route in route_orders(items, depot_id, travel).items()
    }
    paired: Dict[str, List[str]] = {}
    seen = set()
    for row in assignments.itertuples(index=False):
        picker_id = str(row.picker_id)
        order_id = str(row.order_id)
        if order_id not in routes:
            raise ValueError(f"Order {order_id} has no route.")
        saved = getattr(row, "duration", None)
        if saved is not None:
            try:
                minutes = float(saved)
            except (TypeError, ValueError):
                minutes = float("nan")
            if math.isfinite(minutes):
                if minutes < 0:
                    raise ValueError(f"Order {order_id} has no route duration.")
                routes[order_id]["route_duration"] = minutes
        paired.setdefault(picker_id, []).append(order_id)
        seen.add(order_id)
    left_out = []
    for row in orders.itertuples(index=False):
        order_id = str(row.order_id)
        if order_id not in seen:
            left_out.append({"order_id": order_id, "reason": "not in the assignment file"})
    return {
        "picker_assignments": paired,
        "routes": routes,
        "orders_not_fulfilled": left_out,
    }


def html_main(args) -> None:
    """CLI body for ``plot_picking.py html``."""
    import pandas as pd

    if args.depots is not None:
        frame = pd.read_csv(args.depots)
        if len(frame) != 1:
            raise ValueError(
                "These pages use one depot. "
                f"{args.depots} has {len(frame)} rows. "
                "Pass --depot-id, --depot-x, and --depot-y for the depot this wave uses."
            )
        row = frame.iloc[0]
        depot_id, depot_x, depot_y = str(row.depot_id), float(row.x), float(row.y)
    else:
        depot_id, depot_x, depot_y = str(args.depot_id), float(args.depot_x), float(args.depot_y)
    orders = pd.read_csv(args.orders)
    items = pd.read_csv(args.items)
    pickers = pd.read_csv(args.pickers)
    travel = pd.read_csv(args.travel)
    if args.assignments is not None:
        result = result_from_assignments(pd.read_csv(args.assignments), orders, items, travel, depot_id)
    else:
        from order_picking_optimizer import solve_order_picking

        result = solve_order_picking(orders, items, pickers, travel, depot_id=depot_id)
    aisles = pd.read_csv(args.aisles)
    crosses = pd.read_csv(args.cross_aisles)
    locations = pd.read_csv(args.locations)
    warehouse, assignment = write_picking_pages(
        aisles,
        crosses,
        locations,
        result,
        pickers,
        depot_id,
        depot_x,
        depot_y,
        args.out_dir,
        speed=args.speed,
        orders=orders,
        scenario_id=args.scenario_id,
        label=args.label,
    )
    print(f"Wrote {warehouse}")
    print(f"Wrote {assignment}")
    assigned = {
        str(picker_id): [str(order_id) for order_id in order_ids]
        for picker_id, order_ids in (result.get("picker_assignments") or {}).items()
    }
    for row in pickers.itertuples(index=False):
        picker_id = str(row.picker_id)
        order_ids = assigned.get(picker_id, [])
        listed = ", ".join(order_ids) if order_ids else "(none)"
        print(f"  {picker_id}: {listed}")


_SHELL = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
__CSS__
</style>
</head>
<body data-page="__PAGE__">
__BODY__
<script src="scenario_index.js"></script>
<script>
let DATA = null;
__SCRIPT__
</script>
</body>
</html>
"""

_WAREHOUSE_BODY = """
<header>
  <div>
    <h1>Warehouse walks</h1>
    <p class="lede">Choose a picker, then an order. Scroll to zoom the floor and drag to move it. The number at each stop is the visit order.</p>
  </div>
  <nav class="links">
    <label class="scenario">Scenario <select id="scenario"></select></label>
    <a id="jump" href="assignment.html">Assignment chart</a>
  </nav>
</header>
<div class="layout">
  <aside class="panel">
    <h2>Pickers</h2>
    <div id="pickers"></div>
    <h2>Orders</h2>
    <div id="orders"></div>
  </aside>
  <div class="panel map-panel">
    <div class="zoom">
      <button type="button" id="zoom-in" aria-label="Zoom in">+</button>
      <button type="button" id="zoom-out" aria-label="Zoom out">−</button>
      <button type="button" id="zoom-reset" class="reset" aria-label="Show the whole floor">Reset</button>
    </div>
    <svg id="map" role="img" aria-label="Warehouse aisles and the selected order walk"></svg>
  </div>
  <aside class="panel">
    <h2>This trip</h2>
    <div id="detail"></div>
  </aside>
</div>
<footer>
  <p>Each order is a separate trip from the depot and back. This picture does not chain a picker's orders into one walk.</p>
  <p id="missed"></p>
</footer>
"""

_ASSIGNMENT_BODY = """
<header>
  <div>
    <h1>Assignment</h1>
    <p class="lede">One row per picker. The gray bar is the shift. Each colored bar is one order.</p>
  </div>
  <nav class="links">
    <label class="scenario">Scenario <select id="scenario"></select></label>
    <a id="jump" href="warehouse.html">Warehouse walks</a>
  </nav>
</header>
<div class="assign">
  <div class="panel grow" id="gantt"></div>
  <aside class="panel">
    <h2>Selected order</h2>
    <div id="detail"></div>
  </aside>
</div>
<footer>
  <p>Minutes are packed from time zero in the listed order. Phase 2 does not sequence jobs inside the shift.</p>
  <p id="missed"></p>
</footer>
"""

_CSS = """
:root { color-scheme: light; }
* { box-sizing: border-box; }
body { margin: 0; font-family: "Segoe UI", system-ui, sans-serif; color: #102a43; background: #f4f7fb; }
header { display: flex; justify-content: space-between; align-items: end; gap: 16px; padding: 16px 20px 8px; }
.links { display: flex; gap: 16px; align-items: center; }
.scenario { display: flex; align-items: center; gap: 8px; font-size: 13px; color: #52606d; }
.scenario select { font: inherit; color: #102a43; padding: 4px 8px; min-width: 16rem; }
h1 { margin: 0; font-size: 22px; }
h2 { margin: 12px 0 6px; font-size: 13px; letter-spacing: 0.04em; text-transform: uppercase; color: #52606d; }
h2:first-child { margin-top: 0; }
.lede { margin: 4px 0 0; color: #52606d; font-size: 14px; }
a { color: #0072B2; }
.layout { display: grid; grid-template-columns: 240px minmax(0, 1fr) 260px; gap: 12px; padding: 8px 16px 12px; }
.assign { display: grid; grid-template-columns: minmax(0, 1fr) 280px; gap: 12px; padding: 8px 16px 12px; align-items: start; }
.panel { background: #fff; border: 1px solid #d9e2ec; border-radius: 10px; padding: 12px; }
.map-panel { position: relative; padding: 8px; }
.zoom { position: absolute; z-index: 2; top: 16px; right: 16px; display: flex; gap: 6px; }
.zoom button { width: 32px; height: 32px; border: 1px solid #d9e2ec; background: #fff; border-radius: 6px; cursor: pointer; font: inherit; font-size: 18px; line-height: 1; color: #102a43; }
.zoom button.reset { width: auto; padding: 0 10px; font-size: 13px; }
svg { width: 100%; height: calc(100vh - 150px); min-height: 460px; display: block; background: #fbfcfe; touch-action: none; user-select: none; }
svg.map-nav { cursor: grab; }
svg.map-nav.dragging { cursor: grabbing; }
button.choice { display: flex; align-items: center; width: 100%; text-align: left; margin: 4px 0; padding: 8px 10px; border: 1px solid #d9e2ec; background: #fff; border-radius: 6px; cursor: pointer; font: inherit; color: inherit; }
button.choice.on { border-color: #0072B2; background: #e8f4fc; }
.swatch { display: inline-block; width: 11px; height: 11px; border-radius: 2px; margin-right: 8px; vertical-align: -1px; flex: none; }
.row { display: grid; grid-template-columns: 56px minmax(0, 1fr) 128px; gap: 8px; align-items: center; margin: 10px 0; }
.who { font-weight: 650; }
.track { position: relative; height: 32px; border-radius: 6px; background: #e6ebf0; }
button.bar { position: absolute; top: 5px; height: 22px; border: 2px solid transparent; border-radius: 4px; color: #fff; font: inherit; font-size: 12px; cursor: pointer; padding: 0; overflow: hidden; }
button.bar.on { border-color: #102a43; }
.meta { font-size: 12px; color: #52606d; text-align: right; }
.stops { margin: 8px 0; padding-left: 18px; }
.stops li { margin: 2px 0; }
.minutes { font-size: 20px; margin: 0; }
.stat-head { margin: 12px 0 2px; font-size: 12px; letter-spacing: 0.04em; text-transform: uppercase; color: #52606d; }
.stat { display: flex; justify-content: space-between; gap: 12px; font-size: 14px; margin: 2px 0; }
.stat span:first-child { color: #52606d; }
.note { color: #52606d; font-size: 13px; }
.gap { color: #c0392b; font-size: 13px; }
footer { padding: 0 20px 18px; color: #52606d; font-size: 13px; }
footer p { margin: 4px 0; }
@media (max-width: 960px) {
  .layout, .assign { grid-template-columns: 1fr; }
  svg { height: 70vh; }
}
"""

_SCRIPT = r"""
const page = document.body.dataset.page;
const svgNS = "http://www.w3.org/2000/svg";
let pickerId = null;
let orderId = null;

function $(id) { return document.getElementById(id); }

function pickerById(id) { return DATA.pickers.find(function (p) { return p.id === id; }); }

function ownerOf(order) {
  return DATA.pickers.find(function (p) { return p.orders.indexOf(order) !== -1; });
}

function ordersOf(id) {
  const picker = pickerById(id);
  return picker ? picker.orders : [];
}

function formatMin(value) {
  const rounded = Math.round(value * 10) / 10;
  const text = Math.abs(rounded - Math.round(rounded)) < 1e-6 ? String(Math.round(rounded)) : String(rounded);
  return text + " min";
}

function init() {
  const query = new URLSearchParams(location.search);
  const askedPicker = query.get("picker");
  const askedOrder = query.get("order");
  if (askedPicker && pickerById(askedPicker)) pickerId = askedPicker;
  if (askedOrder && DATA.orders[askedOrder]) {
    orderId = askedOrder;
    const owner = ownerOf(askedOrder);
    if (owner) pickerId = owner.id;
  } else {
    const mine = ordersOf(pickerId);
    orderId = mine.length ? mine[0] : null;
  }
  render();
}

function scenarioParam() {
  const selected = $("scenario");
  const id = selected && selected.value ? selected.value : "";
  return id ? "scenario=" + encodeURIComponent(id) : "";
}

function render() {
  if (page === "warehouse") renderPickers();
  else renderGantt();
  paintDetail();
  paintMissed();
  const jump = $("jump");
  if (!jump) return;
  const scenario = scenarioParam();
  const base = page === "warehouse" ? "assignment.html" : "warehouse.html";
  if (!orderId) {
    jump.href = scenario ? base + "?" + scenario : base;
    return;
  }
  if (page === "warehouse") {
    jump.href = base + "?order=" + encodeURIComponent(orderId) + (scenario ? "&" + scenario : "");
    return;
  }
  jump.href = base + "?picker=" + encodeURIComponent(pickerId) + "&order=" + encodeURIComponent(orderId) + (scenario ? "&" + scenario : "");
}

function renderPickers() {
  const box = $("pickers");
  box.replaceChildren();
  DATA.pickers.forEach(function (picker) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "choice" + (picker.id === pickerId ? " on" : "");
    button.textContent = picker.orders.length ? picker.id + " (" + picker.orders.length + ")" : picker.id + " (no orders)";
    button.addEventListener("click", function () {
      pickerId = picker.id;
      orderId = picker.orders.length ? picker.orders[0] : null;
      framedOrder = null;
      mapView = null;
      render();
    });
    box.appendChild(button);
  });
  const list = $("orders");
  list.replaceChildren();
  ordersOf(pickerId).forEach(function (id) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "choice" + (id === orderId ? " on" : "");
    const swatch = document.createElement("span");
    swatch.className = "swatch";
    swatch.style.background = DATA.colors[id];
    button.appendChild(swatch);
    button.appendChild(document.createTextNode(id + "  " + formatMin(DATA.orders[id].duration)));
    button.addEventListener("click", function () {
      orderId = id;
      framedOrder = null;
      mapView = null;
      render();
    });
    list.appendChild(button);
  });
  drawMap();
}

function renderGantt() {
  const root = $("gantt");
  root.replaceChildren();
  DATA.pickers.forEach(function (picker) {
    const row = document.createElement("div");
    row.className = "row";
    const who = document.createElement("div");
    who.className = "who";
    who.textContent = picker.id;
    const track = document.createElement("div");
    track.className = "track";
    let cursor = 0;
    picker.orders.forEach(function (id) {
      const minutes = DATA.orders[id].duration;
      cursor += minutes;
    });
    const scale = Math.max(picker.shift, cursor, 1e-9);
    const shiftPct = picker.shift / scale * 100;
    track.style.background = "linear-gradient(90deg, #e6ebf0 0 " + shiftPct + "%, #fde8e8 " + shiftPct + "% 100%)";
    let start = 0;
    picker.orders.forEach(function (id) {
      const minutes = DATA.orders[id].duration;
      const bar = document.createElement("button");
      bar.type = "button";
      bar.className = "bar" + (id === orderId ? " on" : "");
      bar.style.left = (start / scale * 100) + "%";
      bar.style.width = Math.max(minutes / scale * 100, 0.8) + "%";
      bar.style.background = DATA.colors[id];
      bar.textContent = minutes / scale > 0.045 ? id : "";
      bar.title = id + ": " + formatMin(minutes);
      bar.addEventListener("click", function () {
        orderId = id;
        pickerId = picker.id;
        render();
      });
      track.appendChild(bar);
      start += minutes;
    });
    const meta = document.createElement("div");
    meta.className = "meta";
    meta.textContent = formatMin(cursor).replace(" min", "") + " / " + formatMin(picker.shift);
    row.appendChild(who);
    row.appendChild(track);
    row.appendChild(meta);
    root.appendChild(row);
  });
}

function formatQty(value) {
  if (value === null || value === undefined || !isFinite(Number(value))) return null;
  const rounded = Math.round(Number(value) * 10) / 10;
  if (Math.abs(rounded - Math.round(rounded)) < 1e-6) return String(Math.round(rounded));
  return String(rounded);
}

function formatPct(ratio) {
  const pct = Math.round(ratio * 1000) / 10;
  if (Math.abs(pct - Math.round(pct)) < 1e-6) return String(Math.round(pct));
  return String(pct);
}

function capacityText(used, cap) {
  const usedLabel = formatQty(used);
  const capLabel = formatQty(cap);
  if (usedLabel === null) return "not recorded";
  if (capLabel === null || !(Number(cap) > 0)) return usedLabel + " · no limit";
  return usedLabel + " / " + capLabel + " · " + formatPct(Number(used) / Number(cap)) + "%";
}

function shiftMinutes(picker) {
  let total = 0;
  (picker.orders || []).forEach(function (id) {
    const order = DATA.orders[id];
    if (order) total += order.duration;
  });
  return total;
}

function statHead(text) {
  const node = document.createElement("p");
  node.className = "stat-head";
  node.textContent = text;
  return node;
}

function statRow(label, text) {
  const row = document.createElement("div");
  row.className = "stat";
  const name = document.createElement("span");
  name.textContent = label;
  const value = document.createElement("span");
  value.textContent = text;
  row.appendChild(name);
  row.appendChild(value);
  return row;
}

function appendShift(box, picker) {
  const used = shiftMinutes(picker);
  box.appendChild(statHead(picker.id + " this shift"));
  box.appendChild(statRow("Time", formatQty(used) + " / " + formatQty(picker.shift) + " min · " + formatPct(picker.shift > 0 ? used / picker.shift : 0) + "%"));
}

function paintDetail() {
  const box = $("detail");
  if (!box) return;
  box.replaceChildren();
  const picker = pickerById(pickerId);
  if (!orderId || !DATA.orders[orderId]) {
    if (!picker) {
      const empty = document.createElement("p");
      empty.className = "note";
      empty.textContent = "No pickers.";
      box.appendChild(empty);
      return;
    }
    appendShift(box, picker);
    const empty = document.createElement("p");
    empty.className = "note";
    empty.textContent = picker.id + " has no orders in this solution.";
    box.appendChild(empty);
    return;
  }
  const order = DATA.orders[orderId];
  const title = document.createElement("p");
  title.className = "minutes";
  title.textContent = orderId + " · " + formatMin(order.duration);
  box.appendChild(title);
  box.appendChild(statHead("This order"));
  box.appendChild(statRow("Time", formatMin(order.duration)));
  box.appendChild(statRow("Weight", capacityText(order.weight, picker ? picker.max_weight : null)));
  box.appendChild(statRow("Volume", capacityText(order.volume, picker ? picker.max_volume : null)));
  const limit = document.createElement("p");
  limit.className = "note";
  limit.textContent = "Weight and volume are this trip. The picker returns to the depot before the next order.";
  box.appendChild(limit);
  if (picker) appendShift(box, picker);
  const list = document.createElement("ol");
  list.className = "stops";
  order.stops.forEach(function (stop, index) {
    const item = document.createElement("li");
    item.textContent = index === order.stops.length - 1 ? stop + "  · back to the depot" : stop;
    list.appendChild(item);
  });
  box.appendChild(list);
  const note = document.createElement("p");
  note.className = "note";
  note.textContent = "Arrow length is the walk. The minutes also include time spent picking at the slots.";
  box.appendChild(note);
  order.legs.forEach(function (leg) {
    if (!leg.note) return;
    const gap = document.createElement("p");
    gap.className = "gap";
    gap.textContent = leg.note;
    box.appendChild(gap);
  });
  const link = document.createElement("a");
  if (page === "warehouse") {
    link.href = "assignment.html?order=" + encodeURIComponent(orderId);
    link.textContent = "Show this order on the assignment chart";
  } else {
    link.href = "warehouse.html?picker=" + encodeURIComponent(pickerId) + "&order=" + encodeURIComponent(orderId);
    link.textContent = "Show this walk on the warehouse map";
  }
  box.appendChild(link);
}

function paintMissed() {
  const node = $("missed");
  if (!node) return;
  if (!DATA.unfulfilled.length) {
    node.textContent = "";
    return;
  }
  const names = DATA.unfulfilled.map(function (item) {
    return item.reason ? item.order_id + " (" + item.reason + ")" : item.order_id;
  });
  node.textContent = "Not drawn: " + names.join(", ") + ".";
}

let baseBox = null;
let baseView = null;
let mapView = null;
let framedOrder = null;
let mapNavReady = false;
let mapDrag = null;

function bounds() {
  const xs = [DATA.depot.x];
  const ys = [DATA.depot.y];
  DATA.aisles.forEach(function (aisle) { xs.push(aisle.x); ys.push(aisle.y_min, aisle.y_max); });
  DATA.crosses.forEach(function (cross) { xs.push(cross.x_min, cross.x_max); ys.push(cross.y); });
  DATA.slots.forEach(function (slot) { xs.push(slot.x); ys.push(slot.y); });
  return {
    x0: Math.min.apply(null, xs) - 1.8,
    y0: Math.min.apply(null, ys) - 2.0,
    x1: Math.max.apply(null, xs) + 2.4,
    y1: Math.max.apply(null, ys) + 1.8
  };
}

function sy(y, box) { return box.y1 - y; }

function el(name, attrs) {
  const node = document.createElementNS(svgNS, name);
  Object.keys(attrs).forEach(function (key) { node.setAttribute(key, attrs[key]); });
  return node;
}

function fullView(box) {
  return { x: box.x0, y: 0, w: box.x1 - box.x0, h: box.y1 - box.y0 };
}

function copyView(view) {
  return { x: view.x, y: view.y, w: view.w, h: view.h };
}

function applyMapView() {
  const map = $("map");
  if (!map || !mapView) return;
  map.setAttribute("viewBox", mapView.x + " " + mapView.y + " " + mapView.w + " " + mapView.h);
}

function clientToSvg(svg, clientX, clientY) {
  const point = svg.createSVGPoint();
  point.x = clientX;
  point.y = clientY;
  const ctm = svg.getScreenCTM();
  if (!ctm) return { x: mapView.x, y: mapView.y };
  const local = point.matrixTransform(ctm.inverse());
  return { x: local.x, y: local.y };
}

function clampPan(view) {
  const cx = Math.min(Math.max(view.x + view.w / 2, baseView.x), baseView.x + baseView.w);
  const cy = Math.min(Math.max(view.y + view.h / 2, baseView.y), baseView.y + baseView.h);
  return { x: view.x + (cx - (view.x + view.w / 2)), y: view.y + (cy - (view.y + view.h / 2)), w: view.w, h: view.h };
}

function orderSvgPoints() {
  const order = orderId && DATA.orders[orderId];
  if (!order || !baseBox) return [];
  const pts = [];
  order.stops.forEach(function (stop, index) {
    if (stop === DATA.depot.id) {
      pts.push([DATA.depot.x + (index === 0 ? -1.05 : 1.05), sy(DATA.depot.y, baseBox)]);
      return;
    }
    const slot = DATA.slots.find(function (item) { return item.id === stop; });
    if (slot) pts.push([slot.x, sy(slot.y, baseBox)]);
  });
  (order.legs || []).forEach(function (leg) {
    (leg.points || []).forEach(function (point) { pts.push([point[0], sy(point[1], baseBox)]); });
  });
  return pts;
}

function fitAspect(x0, y0, x1, y1) {
  let w = Math.max(x1 - x0, 1e-6);
  let h = Math.max(y1 - y0, 1e-6);
  const aspect = baseView.w / baseView.h;
  if (w / h > aspect) h = w / aspect;
  else w = h * aspect;
  return { x: (x0 + x1) / 2 - w / 2, y: (y0 + y1) / 2 - h / 2, w: w, h: h };
}

function frameForOrder() {
  const pts = orderSvgPoints();
  if (!pts.length) return null;
  let x0 = Infinity;
  let y0 = Infinity;
  let x1 = -Infinity;
  let y1 = -Infinity;
  pts.forEach(function (point) {
    x0 = Math.min(x0, point[0]);
    y0 = Math.min(y0, point[1]);
    x1 = Math.max(x1, point[0]);
    y1 = Math.max(y1, point[1]);
  });
  const padX = Math.max((x1 - x0) * 0.18, 2.2);
  const padY = Math.max((y1 - y0) * 0.18, 2.2);
  return fitAspect(x0 - padX, y0 - padY, x1 + padX, y1 + padY);
}

function resetMapView() {
  if (!baseView) return;
  mapView = copyView(baseView);
  framedOrder = orderId;
  applyMapView();
}

function zoomAt(svgX, svgY, factor) {
  if (!mapView || !baseView) return;
  const minW = baseView.w / 40;
  const nextW = Math.min(Math.max(mapView.w * factor, minW), baseView.w);
  const nextH = nextW * (baseView.h / baseView.w);
  const px = (svgX - mapView.x) / mapView.w;
  const py = (svgY - mapView.y) / mapView.h;
  mapView = clampPan({ x: svgX - px * nextW, y: svgY - py * nextH, w: nextW, h: nextH });
  framedOrder = orderId;
  applyMapView();
}

function zoomFromCenter(factor) {
  const svg = $("map");
  if (!svg) return;
  const rect = svg.getBoundingClientRect();
  const center = clientToSvg(svg, rect.left + rect.width / 2, rect.top + rect.height / 2);
  zoomAt(center.x, center.y, factor);
}

function bindMapNav() {
  const svg = $("map");
  if (!svg || mapNavReady) return;
  mapNavReady = true;
  svg.classList.add("map-nav");
  svg.addEventListener("wheel", function (ev) {
    if (!mapView) return;
    ev.preventDefault();
    const at = clientToSvg(svg, ev.clientX, ev.clientY);
    zoomAt(at.x, at.y, ev.deltaY > 0 ? 1.12 : 1 / 1.12);
  }, { passive: false });
  svg.addEventListener("pointerdown", function (ev) {
    if (ev.button !== 0 || !mapView) return;
    const rect = svg.getBoundingClientRect();
    const ctm = svg.getScreenCTM();
    mapDrag = {
      id: ev.pointerId,
      x: ev.clientX,
      y: ev.clientY,
      view: copyView(mapView),
      sx: ctm ? 1 / ctm.a : mapView.w / rect.width,
      sy: ctm ? 1 / ctm.d : mapView.h / rect.height
    };
    svg.classList.add("dragging");
    svg.setPointerCapture(ev.pointerId);
  });
  svg.addEventListener("pointermove", function (ev) {
    if (!mapDrag || ev.pointerId !== mapDrag.id) return;
    mapView = clampPan({
      x: mapDrag.view.x - (ev.clientX - mapDrag.x) * mapDrag.sx,
      y: mapDrag.view.y - (ev.clientY - mapDrag.y) * mapDrag.sy,
      w: mapDrag.view.w,
      h: mapDrag.view.h
    });
    framedOrder = orderId;
    applyMapView();
  });
  function endDrag(ev) {
    if (!mapDrag || ev.pointerId !== mapDrag.id) return;
    mapDrag = null;
    svg.classList.remove("dragging");
  }
  svg.addEventListener("pointerup", endDrag);
  svg.addEventListener("pointercancel", endDrag);
  svg.addEventListener("dblclick", function (ev) {
    ev.preventDefault();
    if (!baseView) return;
    mapView = frameForOrder() || copyView(baseView);
    framedOrder = orderId;
    applyMapView();
  });
  const zoomIn = $("zoom-in");
  const zoomOut = $("zoom-out");
  const zoomReset = $("zoom-reset");
  if (zoomIn) zoomIn.addEventListener("click", function () { zoomFromCenter(1 / 1.25); });
  if (zoomOut) zoomOut.addEventListener("click", function () { zoomFromCenter(1.25); });
  if (zoomReset) zoomReset.addEventListener("click", resetMapView);
}

function drawMap() {
  const map = $("map");
  if (!map) return;
  map.replaceChildren();
  const box = bounds();
  baseBox = box;
  baseView = fullView(box);
  if (orderId !== framedOrder || !mapView) mapView = frameForOrder() || copyView(baseView);
  framedOrder = orderId;
  applyMapView();
  bindMapNav();
  const defs = el("defs", {});
  map.appendChild(defs);
  DATA.aisles.forEach(function (aisle) {
    map.appendChild(el("line", {
      x1: aisle.x, y1: sy(aisle.y_min, box), x2: aisle.x, y2: sy(aisle.y_max, box),
      stroke: "#c5cdd6", "stroke-width": 0.55, "stroke-linecap": "butt"
    }));
    map.appendChild(el("text", {
      x: aisle.x, y: sy(aisle.y_max + 0.45, box), "text-anchor": "middle",
      "font-size": 0.7, fill: "#52606d"
    })).textContent = aisle.id;
    if (aisle.one_way === "up" || aisle.one_way === "down") {
      const mark = el("text", {
        x: aisle.x + 1.15,
        y: sy(aisle.y_min + 0.35 * (aisle.y_max - aisle.y_min), box),
        "font-size": 0.55, fill: "#7b8794"
      });
      mark.textContent = aisle.one_way === "up" ? "up only" : "down only";
      map.appendChild(mark);
    }
  });
  const crossYs = DATA.crosses.map(function (cross) { return cross.y; });
  const front = crossYs.length ? Math.min.apply(null, crossYs) : null;
  const back = crossYs.length ? Math.max.apply(null, crossYs) : null;
  DATA.crosses.forEach(function (cross) {
    map.appendChild(el("line", {
      x1: cross.x_min, y1: sy(cross.y, box), x2: cross.x_max, y2: sy(cross.y, box),
      stroke: "#c5cdd6", "stroke-width": 0.55, "stroke-linecap": "butt"
    }));
    let labelY = cross.y + 0.4;
    if (front !== null && Math.abs(cross.y - front) < 1e-6) labelY = cross.y - 1.15;
    else if (back !== null && Math.abs(cross.y - back) < 1e-6) labelY = cross.y - 0.45;
    const label = el("text", {
      x: (cross.x_min + cross.x_max) / 2, y: sy(labelY, box),
      "text-anchor": "middle", "font-size": 0.5, fill: "#7b8794"
    });
    label.textContent = cross.id;
    map.appendChild(label);
  });
  DATA.slots.forEach(function (slot) {
    map.appendChild(el("circle", { cx: slot.x, cy: sy(slot.y, box), r: 0.16, fill: "#9aa5b1" }));
  });
  map.appendChild(el("rect", {
    x: DATA.depot.x - 0.28, y: sy(DATA.depot.y, box) - 0.28,
    width: 0.56, height: 0.56, fill: "#102a43"
  }));
  const depotLabel = el("text", {
    x: DATA.depot.x + 0.45, y: sy(DATA.depot.y, box) + 0.7,
    "font-size": 0.55, fill: "#102a43"
  });
  depotLabel.textContent = DATA.depot.id;
  map.appendChild(depotLabel);
  if (orderId && DATA.orders[orderId]) {
    const color = DATA.colors[orderId];
    const stem = orderId.replace(/[^A-Za-z0-9_-]/g, "");
    const marker = el("marker", {
      id: "mk" + stem, viewBox: "0 0 10 10", refX: 8, refY: 5,
      markerWidth: 4, markerHeight: 4, orient: "auto"
    });
    marker.appendChild(el("path", { d: "M 0 0 L 10 5 L 0 10 z", fill: color }));
    defs.appendChild(marker);
    DATA.orders[orderId].legs.forEach(function (leg) {
      const points = leg.points || [];
      for (let i = 0; i < points.length - 1; i += 1) {
        map.appendChild(el("line", {
          x1: points[i][0], y1: sy(points[i][1], box),
          x2: points[i + 1][0], y2: sy(points[i + 1][1], box),
          stroke: leg.gap ? "#c0392b" : color,
          "stroke-width": 0.16,
          "stroke-linecap": "round",
          "stroke-dasharray": leg.gap ? "0.35 0.22" : "none",
          "marker-end": leg.gap ? "none" : "url(#mk" + stem + ")"
        }));
      }
    });
  }
  if (!orderId || !DATA.orders[orderId]) return;
  const left = DATA.aisles.length ? Math.min.apply(null, DATA.aisles.map(function (aisle) { return aisle.x; })) : DATA.depot.x;
  const seen = {};
  const color = DATA.colors[orderId];
  DATA.orders[orderId].stops.forEach(function (stop, index) {
    let x;
    let y;
    if (stop === DATA.depot.id) {
      x = DATA.depot.x + (index === 0 ? -1.05 : 1.05);
      y = DATA.depot.y;
    } else {
      const slot = DATA.slots.find(function (item) { return item.id === stop; });
      if (!slot) return;
      x = slot.x;
      y = slot.y;
      if (!seen[stop]) {
        seen[stop] = true;
        const onLeft = Math.abs(slot.x - left) < 1e-6;
        const label = el("text", {
          x: slot.x + (onLeft ? -0.55 : 0.55),
          y: sy(slot.y, box) - 0.55,
          "text-anchor": onLeft ? "end" : "start",
          "font-size": 0.46,
          fill: "#102a43"
        });
        label.textContent = stop;
        map.appendChild(label);
      }
    }
    map.appendChild(el("circle", {
      cx: x, cy: sy(y, box), r: 0.4,
      fill: "#ffffff", stroke: color, "stroke-width": 0.08
    }));
    const number = el("text", {
      x: x, y: sy(y, box) + 0.16,
      "text-anchor": "middle", "font-size": 0.48, "font-weight": "700", fill: "#102a43"
    });
    number.textContent = String(index + 1);
    map.appendChild(number);
  });
}

function rememberScenario(id) {
  try { localStorage.setItem("picking-scenario", id); } catch (err) { /* a private page can block storage */ }
}

function chosenScenarioId() {
  const index = window.PICKING_INDEX || [];
  const asked = new URLSearchParams(location.search).get("scenario");
  if (asked && index.some(function (item) { return item.id === asked; })) return asked;
  let stored = null;
  try { stored = localStorage.getItem("picking-scenario"); } catch (err) { stored = null; }
  if (stored && index.some(function (item) { return item.id === stored; })) return stored;
  return index.length ? index[index.length - 1].id : null;
}

function fillScenarioSelect(selected) {
  const select = $("scenario");
  if (!select) return;
  select.replaceChildren();
  (window.PICKING_INDEX || []).forEach(function (item) {
    const option = document.createElement("option");
    option.value = item.id;
    option.textContent = item.label;
    if (item.id === selected) option.selected = true;
    select.appendChild(option);
  });
}

function applyScenario(id) {
  const view = (window.PICKING_VIEWS || {})[id];
  if (!view) return;
  DATA = view;
  pickerId = DATA.pickers.length ? DATA.pickers[0].id : null;
  orderId = null;
  mapView = null;
  framedOrder = null;
  fillScenarioSelect(id);
  init();
}

function loadScenario(id) {
  if (!id) return;
  if (window.PICKING_VIEWS && window.PICKING_VIEWS[id]) {
    applyScenario(id);
    return;
  }
  const script = document.createElement("script");
  script.src = "scenarios/" + id + ".js";
  script.onload = function () { applyScenario(id); };
  script.onerror = function () {
    const box = $("detail");
    if (!box) return;
    box.replaceChildren();
    const note = document.createElement("p");
    note.className = "note";
    note.textContent = "Could not load scenario " + id + ".";
    box.appendChild(note);
  };
  document.body.appendChild(script);
}

function bootScenarios() {
  const index = window.PICKING_INDEX || [];
  const select = $("scenario");
  const initial = chosenScenarioId();
  fillScenarioSelect(initial);
  if (select) {
    select.onchange = function () {
      rememberScenario(select.value);
      const params = new URLSearchParams(location.search);
      params.set("scenario", select.value);
      params.delete("picker");
      params.delete("order");
      history.replaceState(null, "", "?" + params.toString());
      loadScenario(select.value);
    };
  }
  if (!index.length) {
    const box = $("detail");
    if (!box) return;
    box.replaceChildren();
    const note = document.createElement("p");
    note.className = "note";
    note.textContent = "No scenarios yet.";
    box.appendChild(note);
    return;
  }
  loadScenario(initial);
}

bootScenarios();
"""
