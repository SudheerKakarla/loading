"""Static pictures of pick routes on the warehouse floor and of one shift.

The route picture draws the aisle centrelines and the shortest directed walk
between consecutive stops. Arrows follow that walk. A marker is a stop; the
picture has no clock, so standing time is not a length on the map.

The floor picture is the same warehouse once per picker. Each assigned order
is its own trip from the depot and back. The picture does not chain a
picker's orders into one walk.

The shift picture is a Gantt chart. Phase 2 chooses which orders a picker
gets. It does not choose the order of those jobs inside the shift, so each
row packs bars from time zero in the order ``picker_assignments`` lists them.
The gray track is the shift. The gap after the last bar is idle time.

matplotlib draws the pictures. It is listed in the repo requirements.txt.
The optimizer does not import it.
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

_ROOT = Path(__file__).resolve().parents[1]
for _folder in ("source", "visualizer"):
    _path = str(_ROOT / _folder)
    if _path not in sys.path:
        sys.path.insert(0, _path)

import pandas as pd

from build_travel_matrix import load_layout, route_legs


def _pyplot():
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError as exc:
        raise ImportError("Plotting needs matplotlib. The picker solve does not.") from exc
    return plt


def gantt_intervals(
    assignments: Dict[str, Sequence[str]],
    durations: Dict[str, float],
) -> Dict[str, List[Tuple[str, float, float]]]:
    """Pack each picker's orders from time zero in list order.

    Returns ``picker_id -> [(order_id, start, end), ...]``. The solver does
    not sequence jobs inside the shift; the list order is the one in
    ``picker_assignments``.
    """
    packed: Dict[str, List[Tuple[str, float, float]]] = {}
    for picker_id, order_ids in assignments.items():
        clock = 0.0
        bars: List[Tuple[str, float, float]] = []
        for order_id in order_ids:
            if order_id not in durations:
                raise ValueError(f"Order {order_id} has no route duration.")
            minutes = float(durations[order_id])
            if not math.isfinite(minutes) or minutes < 0:
                raise ValueError(f"Duration for order {order_id} must be a finite number >= 0.")
            bars.append((str(order_id), clock, clock + minutes))
            clock += minutes
        packed[str(picker_id)] = bars
    return packed


def plot_order_route(
    aisles,
    cross_aisles,
    locations,
    route: Sequence[str],
    depot_id: Optional[str] = None,
    depot_x: Optional[float] = None,
    depot_y: Optional[float] = None,
    speed: float = 1.0,
    depots=None,
    title: Optional[str] = None,
    out: Optional[Path] = None,
):
    """Draw aisle centrelines and arrows along the walk for ``route``.

    ``route`` is the visit order, depot first and last. Each connected step is
    the shortest centreline walk, including the segment from an off-centre
    depot onto the aisle. A step with no walk is a dashed red straight line
    labeled ``no walk``. Retrieval time is not drawn. At speed 1 and retrieval
    0, the arrow length matches the matrix. Returns the matplotlib figure.
    ``out`` also writes a PNG.
    """
    plt = _pyplot()
    parsed_aisles, parsed_crosses, slots = load_layout(aisles, cross_aisles, locations)
    legs = route_legs(
        aisles,
        cross_aisles,
        locations,
        route,
        depot_id=depot_id,
        depot_x=depot_x,
        depot_y=depot_y,
        speed=speed,
        depots=depots,
    )
    fig, ax = plt.subplots(figsize=(8, 6))
    for aisle in parsed_aisles:
        ax.plot([aisle.x, aisle.x], [aisle.y_min, aisle.y_max], color="#c5cdd6", lw=6, solid_capstyle="butt", zorder=1)
    for cross in parsed_crosses:
        ax.plot([cross.x_min, cross.x_max], [cross.y, cross.y], color="#c5cdd6", lw=6, solid_capstyle="butt", zorder=1)
    colors = plt.cm.tab10.colors
    gap_notes: List[str] = []
    for index, leg in enumerate(legs):
        color = colors[index % len(colors)]
        if leg.points is None:
            gap_notes.append(leg.note or f"No walk connects {leg.origin} -> {leg.dest}.")
            if leg.ends is None:
                continue
            (x0, y0), (x1, y1) = leg.ends
            (gap_line,) = ax.plot(
                [x0, x1],
                [y0, y1],
                linestyle="--",
                color="#c0392b",
                lw=1.6,
                zorder=3,
            )
            gap_line.set_gid("no-walk")
            ax.annotate(
                f"no walk {leg.origin} → {leg.dest}",
                ((x0 + x1) / 2.0, (y0 + y1) / 2.0),
                color="#c0392b",
                fontsize=8,
                zorder=4,
            )
            continue
        for (x0, y0), (x1, y1) in zip(leg.points, leg.points[1:]):
            ax.annotate(
                "",
                xy=(x1, y1),
                xytext=(x0, y0),
                arrowprops={"arrowstyle": "->", "color": color, "lw": 1.8},
                zorder=3,
            )
    named = [str(stop) for stop in route]
    depot_names = {str(depot_id)} if depot_id else set()
    if depots is not None:
        depot_frame = depots if hasattr(depots, "itertuples") else None
        rows = depot_frame.itertuples(index=False) if depot_frame is not None else depots
        for row in rows:
            depot_names.add(str(row.depot_id if hasattr(row, "depot_id") else row["depot_id"]))
    revisited = [stop for stop in named[1:-1] if stop in depot_names]
    if revisited:
        gap_notes.append(
            "Depot appears in the middle of the route ("
            + ", ".join(revisited)
            + "). The picture draws that walk. Phase 1 did not choose a second depot."
        )
    aisle_x = {aisle.aisle_id: aisle.x for aisle in parsed_aisles}
    slot_xy = {slot.location_id: (aisle_x[slot.aisle_id], slot.y) for slot in slots}
    seen = set()
    for index, stop in enumerate(route):
        if stop in seen:
            continue
        seen.add(stop)
        if stop in slot_xy:
            x, y = slot_xy[stop]
        elif depot_id is not None and stop == str(depot_id) and depot_x is not None and depot_y is not None:
            x, y = float(depot_x), float(depot_y)
        else:
            placed = False
            x, y = 0.0, 0.0
            for leg in legs:
                if leg.ends is None:
                    continue
                if leg.origin == str(stop):
                    x, y = leg.ends[0]
                    placed = True
                    break
                if leg.dest == str(stop):
                    x, y = leg.ends[1]
                    placed = True
                    break
            if not placed:
                continue
        ax.scatter([x], [y], s=36, color="#1f2933", zorder=4)
        ax.annotate(f"{index}:{stop}", (x, y), textcoords="offset points", xytext=(4, 4), fontsize=8)
    ax.set_aspect("equal", adjustable="datalim")
    ax.set_xlabel("x across aisles. Arrow length is distance. Off-centre depot is its own segment. Retrieval is not drawn.")
    ax.set_ylabel("y  (along an aisle)")
    ax.set_title(title or " → ".join(str(stop) for stop in route))
    if gap_notes:
        fig.text(0.01, 0.01, " | ".join(gap_notes), fontsize=8, color="#c0392b")
        fig.tight_layout(rect=(0, 0.08, 1, 1))
    else:
        fig.tight_layout()
    if out is not None:
        path = Path(out)
        path.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(path, dpi=120)
    return fig


# Okabe–Ito colors that stay distinct on a white floor. Yellow is omitted.
_ROUTE_COLORS = (
    "#0072B2",
    "#E69F00",
    "#009E73",
    "#D55E00",
    "#CC79A7",
    "#56B4E9",
    "#882255",
    "#332288",
)
_LANE_OFFSET = 0.42
_FLOOR_CAPTION = (
    "Each order is a separate trip from the depot and back. "
    "This picture does not chain a picker's orders into one walk."
)


def _offset_segment(
    x0: float,
    y0: float,
    x1: float,
    y1: float,
    amount: float,
) -> Tuple[float, float, float, float]:
    """Shift a segment onto a parallel lane. A zero-length segment stays put."""
    dx = x1 - x0
    dy = y1 - y0
    length = math.hypot(dx, dy)
    if length <= 1e-9 or amount == 0.0:
        return x0, y0, x1, y1
    scale = amount / length
    return x0 - dy * scale, y0 + dx * scale, x1 - dy * scale, y1 + dx * scale


def _picker_panels(result: dict, pickers) -> List[Tuple[str, List[str]]]:
    """One ``(picker_id, order_ids)`` pair per panel, pickers-table order first."""
    if "picker_assignments" not in result:
        if pickers is None:
            raise ValueError("result has no picker_assignments.")
        assignments: Dict[str, List[str]] = {}
    else:
        assignments = {
            str(picker_id): [str(order_id) for order_id in order_ids]
            for picker_id, order_ids in result["picker_assignments"].items()
        }
    if pickers is None:
        picker_ids = list(assignments.keys())
    else:
        picker_ids = [str(row.picker_id) for row in pickers.itertuples(index=False)]
        for picker_id in assignments:
            if picker_id not in picker_ids:
                picker_ids.append(picker_id)
    if not picker_ids:
        raise ValueError("result has no picker_assignments.")
    return [(picker_id, assignments.get(picker_id, [])) for picker_id in picker_ids]


def _route_stops(routes: dict, order_id: str) -> List[str]:
    """Visit list for one assigned order. A missing list raises."""
    route = routes.get(order_id) if isinstance(routes, dict) else None
    stops = route.get("route") if isinstance(route, dict) else None
    try:
        named = [str(stop) for stop in stops]
    except TypeError:
        named = []
    if len(named) < 2:
        raise ValueError(f"Order {order_id} has no route.")
    return named


def _draw_floor_background(ax, parsed_aisles, parsed_crosses, slots) -> Dict[str, Tuple[float, float]]:
    """Gray centrelines, aisle names, and a dot for every slot."""
    aisle_x = {aisle.aisle_id: aisle.x for aisle in parsed_aisles}
    for aisle in parsed_aisles:
        ax.plot(
            [aisle.x, aisle.x],
            [aisle.y_min, aisle.y_max],
            color="#c5cdd6",
            lw=6,
            solid_capstyle="butt",
            zorder=1,
        )
        ax.text(
            aisle.x,
            aisle.y_max + 0.55,
            aisle.aisle_id,
            ha="center",
            va="bottom",
            fontsize=8,
            color="#52606d",
            zorder=2,
        )
        if aisle.one_way == "up":
            direction = "up only"
        elif aisle.one_way == "down":
            direction = "down only"
        else:
            direction = ""
        if direction:
            mark_y = aisle.y_min + 0.35 * (aisle.y_max - aisle.y_min)
            ax.text(
                aisle.x + 1.15,
                mark_y,
                direction,
                ha="left",
                va="center",
                fontsize=6,
                color="#7b8794",
                zorder=2,
            )
    front_y = min((cross.y for cross in parsed_crosses), default=None)
    back_y = max((cross.y for cross in parsed_crosses), default=None)
    for cross in parsed_crosses:
        ax.plot(
            [cross.x_min, cross.x_max],
            [cross.y, cross.y],
            color="#c5cdd6",
            lw=6,
            solid_capstyle="butt",
            zorder=1,
        )
        mid_x = (cross.x_min + cross.x_max) / 2.0
        if front_y is not None and abs(cross.y - front_y) <= 1e-9:
            label_y = cross.y - 1.15
            va = "center"
        elif back_y is not None and abs(cross.y - back_y) <= 1e-9:
            label_y = cross.y - 0.35
            va = "top"
        else:
            label_y = cross.y + 0.4
            va = "bottom"
        ax.text(
            mid_x,
            label_y,
            cross.cross_id,
            ha="center",
            va=va,
            fontsize=6,
            color="#7b8794",
            zorder=2,
        )
    slot_xy: Dict[str, Tuple[float, float]] = {}
    for slot in slots:
        point = (aisle_x[slot.aisle_id], slot.y)
        slot_xy[slot.location_id] = point
        ax.scatter([point[0]], [point[1]], s=12, color="#9aa5b1", zorder=2)
    return slot_xy


def _draw_walk(ax, leg, color: str, amount: float, gid: str) -> None:
    """Arrows along one leg, or a dashed red segment when the walk is missing."""
    from matplotlib.patches import FancyArrowPatch

    if leg.points is None:
        if leg.ends is None:
            return
        (x0, y0), (x1, y1) = leg.ends
        x0, y0, x1, y1 = _offset_segment(x0, y0, x1, y1, amount)
        (gap_line,) = ax.plot(
            [x0, x1],
            [y0, y1],
            linestyle="--",
            color="#c0392b",
            lw=1.6,
            zorder=3,
        )
        gap_line.set_gid("no-walk")
        ax.annotate(
            f"no walk {leg.origin} → {leg.dest}",
            ((x0 + x1) / 2.0, (y0 + y1) / 2.0),
            color="#c0392b",
            fontsize=7,
            ha="center",
            zorder=4,
        )
        return
    for (x0, y0), (x1, y1) in zip(leg.points, leg.points[1:]):
        if math.hypot(x1 - x0, y1 - y0) <= 1e-9:
            continue
        xa, ya, xb, yb = _offset_segment(x0, y0, x1, y1, amount)
        arrow = FancyArrowPatch(
            (xa, ya),
            (xb, yb),
            arrowstyle="-|>",
            mutation_scale=11,
            lw=1.7,
            color=color,
            shrinkA=0,
            shrinkB=0,
            zorder=3,
            gid=gid,
        )
        ax.add_patch(arrow)


def _floor_limits(parsed_aisles, parsed_crosses, slot_xy, depot_x: float, depot_y: float):
    xs = [depot_x]
    ys = [depot_y]
    for aisle in parsed_aisles:
        xs.append(aisle.x)
        ys.extend((aisle.y_min, aisle.y_max))
    for cross in parsed_crosses:
        xs.extend((cross.x_min, cross.x_max))
        ys.append(cross.y)
    for x, y in slot_xy.values():
        xs.append(x)
        ys.append(y)
    return (min(xs) - 1.8, max(xs) + 2.4), (min(ys) - 2.0, max(ys) + 1.8)


def plot_picker_routes(
    aisles,
    cross_aisles,
    locations,
    result: dict,
    depot_id: str,
    depot_x: float,
    depot_y: float,
    pickers=None,
    speed: float = 1.0,
    title: Optional[str] = None,
    out: Optional[Path] = None,
):
    """Draw the warehouse once per picker, with each assigned order as its own trip.

    Arrows follow the directed centreline walk. Orders that share an aisle are
    shifted onto parallel lanes so both walks stay visible. A picker with no
    jobs still gets a panel when ``pickers`` is passed. Orders in
    ``orders_not_fulfilled`` are named under the picture and are not drawn.
    Returns the matplotlib figure. ``out`` also writes a PNG.
    """
    plt = _pyplot()
    from matplotlib.lines import Line2D

    panels = _picker_panels(result, pickers)
    routes = result.get("routes") or {}
    walks = [
        (picker_id, order_ids, [_route_stops(routes, order_id) for order_id in order_ids])
        for picker_id, order_ids in panels
    ]
    parsed_aisles, parsed_crosses, slots = load_layout(aisles, cross_aisles, locations)
    n_panels = len(walks)
    fig, axes = plt.subplots(1, n_panels, figsize=(5.1 * n_panels, 8.0), squeeze=False)
    gap_notes: List[str] = []
    limits = None
    for ax, (picker_id, order_ids, order_routes) in zip(axes[0], walks):
        slot_xy = _draw_floor_background(ax, parsed_aisles, parsed_crosses, slots)
        if limits is None:
            limits = _floor_limits(parsed_aisles, parsed_crosses, slot_xy, float(depot_x), float(depot_y))
        visited: Dict[str, Tuple[float, float]] = {}
        handles = []
        n_orders = len(order_ids)
        for index, (order_id, route) in enumerate(zip(order_ids, order_routes)):
            color = _ROUTE_COLORS[index % len(_ROUTE_COLORS)]
            amount = (index - (n_orders - 1) / 2.0) * _LANE_OFFSET
            gid = f"walk-{picker_id}-{order_id}"
            legs = route_legs(
                aisles,
                cross_aisles,
                locations,
                route,
                depot_id=depot_id,
                depot_x=depot_x,
                depot_y=depot_y,
                speed=speed,
            )
            for leg in legs:
                if leg.note:
                    gap_notes.append(leg.note)
                _draw_walk(ax, leg, color, amount, gid)
            for stop in route:
                if stop == str(depot_id) or stop in visited:
                    continue
                if stop in slot_xy:
                    visited[stop] = slot_xy[stop]
            handles.append(Line2D([0], [0], color=color, lw=2, label=order_id))
        ax.scatter([float(depot_x)], [float(depot_y)], s=70, marker="s", color="#102a43", zorder=5)
        ax.annotate(
            str(depot_id),
            (float(depot_x), float(depot_y)),
            textcoords="offset points",
            xytext=(5, -10),
            fontsize=8,
            color="#102a43",
            zorder=6,
        )
        left_aisle = min(aisle.x for aisle in parsed_aisles)
        for stop, (x, y) in visited.items():
            ax.scatter([x], [y], s=28, color="#102a43", zorder=4)
            on_cross = any(abs(y - cross.y) <= 0.2 for cross in parsed_crosses)
            if abs(x - left_aisle) <= 1e-6:
                xytext = (-5, 8 if on_cross else 3)
                ha = "right"
            else:
                xytext = (5, 8 if on_cross else 3)
                ha = "left"
            ax.annotate(
                stop,
                (x, y),
                textcoords="offset points",
                xytext=xytext,
                ha=ha,
                fontsize=7,
                color="#102a43",
                zorder=6,
            )
        x_lim, y_lim = limits
        ax.set_xlim(x_lim)
        ax.set_ylim(y_lim)
        ax.set_aspect("equal", adjustable="box")
        for spine in ax.spines.values():
            spine.set_color("#d9e2ec")
        ax.tick_params(labelsize=7, colors="#52606d")
        if order_ids:
            rows = math.ceil(len(order_ids) / 3)
            ax.set_title(f"{picker_id}: {', '.join(order_ids)}", pad=8 + 15 * rows)
            ax.legend(
                handles=handles,
                loc="lower center",
                bbox_to_anchor=(0.5, 1.0),
                ncol=min(len(order_ids), 3),
                fontsize=7,
                frameon=False,
                borderaxespad=0.15,
                handlelength=1.5,
                columnspacing=0.9,
            )
        else:
            ax.set_title(f"{picker_id}: no orders")
        ax.set_xlabel("x")
    axes[0][0].set_ylabel("y  (along an aisle)")
    missed = list(result.get("orders_not_fulfilled") or [])
    missed_text = ""
    if missed:
        parts: List[str] = []
        for item in missed:
            if isinstance(item, dict):
                name = str(item.get("order_id", ""))
                reason = str(item.get("reason", "") or "")
                parts.append(f"{name} ({reason})" if reason else name)
            else:
                parts.append(str(item))
        missed_text = " Not drawn: " + ", ".join(parts) + "."
    fig.text(0.01, 0.012, _FLOOR_CAPTION + missed_text, fontsize=8, color="#52606d")
    if gap_notes:
        unique_notes = list(dict.fromkeys(gap_notes))
        fig.text(0.01, 0.045, " | ".join(unique_notes), fontsize=8, color="#c0392b")
    if title:
        fig.suptitle(title)
    bottom = 0.08 if gap_notes else 0.06
    top = 0.86 if title else 0.90
    fig.tight_layout(rect=(0, bottom, 1, top))
    if out is not None:
        path = Path(out)
        path.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(path, dpi=130, bbox_inches="tight", pad_inches=0.28)
    return fig


def plot_shift_gantt(result: dict, pickers, title: Optional[str] = None, out: Optional[Path] = None):
    """Draw one row per picker. Gray is the shift. Colored bars are assigned orders.

    Bars start at time zero and follow ``picker_assignments``. That sequence
    is a picture of the workload, not a start-time decision from the solver.
    """
    plt = _pyplot()
    if "picker_assignments" not in result:
        raise ValueError("result has no picker_assignments.")
    routes = result.get("routes") or {}
    durations = {
        order_id: float(route["route_duration"])
        for order_id, route in routes.items()
    }
    assignments = {str(pid): [str(oid) for oid in order_ids] for pid, order_ids in result["picker_assignments"].items()}
    intervals = gantt_intervals(assignments, durations)
    shifts: Dict[str, float] = {}
    for row in pickers.itertuples(index=False):
        shifts[str(row.picker_id)] = float(row.shift_duration)
    for picker_id in assignments:
        if picker_id not in shifts:
            raise ValueError(f"Picker {picker_id} has assignments and no shift_duration.")
    picker_ids = list(dict.fromkeys([*shifts.keys(), *assignments.keys()]))
    fig_h = max(2.4, 0.55 * len(picker_ids) + 1.2)
    fig, ax = plt.subplots(figsize=(10, fig_h))
    colors = plt.cm.tab10.colors
    order_color: Dict[str, tuple] = {}
    for row_index, picker_id in enumerate(picker_ids):
        shift = shifts.get(picker_id, 0.0)
        ax.barh(row_index, shift, height=0.62, color="#e6ebf0", zorder=1)
        for order_id, start, end in intervals.get(picker_id, []):
            if order_id not in order_color:
                order_color[order_id] = colors[len(order_color) % len(colors)]
            width = end - start
            ax.barh(row_index, width, left=start, height=0.38, color=order_color[order_id], zorder=2)
            if width > 0:
                ax.text(start + width / 2, row_index, order_id, ha="center", va="center", fontsize=8, color="white", zorder=3)
    ax.set_yticks(range(len(picker_ids)))
    ax.set_yticklabels(picker_ids)
    ax.set_xlabel(
        "Minutes from time zero. Bar order follows the assignment list for this picture only. "
        "Phase 2 does not sequence jobs inside the shift."
    )
    ax.set_title(title or "Display only: not an optimized job sequence")
    missed = list(result.get("orders_not_fulfilled") or [])
    if missed:
        text = "Not released: " + ", ".join(f"{item['order_id']} ({item['reason']})" for item in missed)
        fig.text(0.01, 0.01, text, fontsize=8)
        fig.tight_layout(rect=(0, 0.06, 1, 1))
    else:
        fig.tight_layout()
    if out is not None:
        path = Path(out)
        path.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(path, dpi=120)
    return fig


def _route_main(args: argparse.Namespace) -> None:
    plot_order_route(
        pd.read_csv(args.aisles),
        pd.read_csv(args.cross_aisles),
        pd.read_csv(args.locations),
        args.route,
        depot_id=args.depot_id,
        depot_x=args.depot_x,
        depot_y=args.depot_y,
        speed=args.speed,
        depots=pd.read_csv(args.depots) if args.depots is not None else None,
        title=args.title,
        out=args.out,
    )
    print(f"Wrote {args.out}")


def _gantt_main(args: argparse.Namespace) -> None:
    assigned = pd.read_csv(args.assignments)
    required = {"picker_id", "order_id", "duration"}
    missing = required - set(assigned.columns)
    if missing:
        raise ValueError(f"assignments CSV is missing {sorted(missing)}.")
    assignments: Dict[str, List[str]] = {}
    durations: Dict[str, float] = {}
    for row in assigned.itertuples(index=False):
        assignments.setdefault(str(row.picker_id), []).append(str(row.order_id))
        durations[str(row.order_id)] = float(row.duration)
    result = {
        "picker_assignments": assignments,
        "routes": {order_id: {"route_duration": minutes} for order_id, minutes in durations.items()},
        "orders_not_fulfilled": [],
    }
    if args.left_out:
        result["orders_not_fulfilled"] = [
            {"order_id": order_id, "reason": "left out"} for order_id in args.left_out
        ]
    plot_shift_gantt(result, pd.read_csv(args.pickers), title=args.title, out=args.out)
    print(f"Wrote {args.out}")


def _floor_depot(args: argparse.Namespace) -> Tuple[str, float, float]:
    """The one depot this picture's tours start from."""
    if args.depots is not None:
        frame = pd.read_csv(args.depots)
        if len(frame) != 1:
            raise ValueError(
                "The floor picture solves one depot. "
                f"{args.depots} has {len(frame)} rows. "
                "Pass --depot-id, --depot-x, and --depot-y for the depot this wave uses."
            )
        row = frame.iloc[0]
        return str(row.depot_id), float(row.x), float(row.y)
    return str(args.depot_id), float(args.depot_x), float(args.depot_y)


def _floor_main(args: argparse.Namespace) -> None:
    from order_picking_optimizer import solve_order_picking

    depot_id, depot_x, depot_y = _floor_depot(args)
    orders = pd.read_csv(args.orders)
    items = pd.read_csv(args.items)
    pickers = pd.read_csv(args.pickers)
    travel = pd.read_csv(args.travel)
    result = solve_order_picking(orders, items, pickers, travel, depot_id=depot_id)
    plot_picker_routes(
        pd.read_csv(args.aisles),
        pd.read_csv(args.cross_aisles),
        pd.read_csv(args.locations),
        result,
        depot_id=depot_id,
        depot_x=depot_x,
        depot_y=depot_y,
        pickers=pickers,
        speed=args.speed,
        title=args.title,
        out=args.out,
    )
    print(f"Wrote {args.out}")


def _html_main(args) -> None:
    from html_picking import html_main

    html_main(args)


def main(argv: Optional[Sequence[str]] = None) -> None:
    parser = argparse.ArgumentParser(
        description="Draw a pick route, a floor picture, a shift Gantt, or two HTML pages."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    route = commands.add_parser("route", help="Arrows along the aisle walk for one order.")
    route.add_argument("--aisles", required=True, type=Path)
    route.add_argument("--cross-aisles", required=True, type=Path)
    route.add_argument("--locations", required=True, type=Path)
    route.add_argument("--route", required=True, nargs="+", help="Visit order, depot first and last.")
    route.add_argument("--depot-id", default=None)
    route.add_argument("--depot-x", default=None, type=float)
    route.add_argument("--depot-y", default=None, type=float)
    route.add_argument("--depots", default=None, type=Path)
    route.add_argument("--speed", default=1.0, type=float)
    route.add_argument("--title", default=None)
    route.add_argument("--out", required=True, type=Path)
    route.set_defaults(func=_route_main)

    gantt = commands.add_parser("gantt", help="One row per picker, bars packed from time zero.")
    gantt.add_argument("--pickers", required=True, type=Path, help="CSV with picker_id, shift_duration.")
    gantt.add_argument(
        "--assignments",
        required=True,
        type=Path,
        help="CSV with picker_id, order_id, duration. Row order is the bar order.",
    )
    gantt.add_argument("--left-out", nargs="*", default=[])
    gantt.add_argument("--title", default=None)
    gantt.add_argument("--out", required=True, type=Path)
    gantt.set_defaults(func=_gantt_main)

    floor = commands.add_parser("floor", help="Warehouse map with one panel per picker.")
    floor.add_argument("--aisles", required=True, type=Path)
    floor.add_argument("--cross-aisles", required=True, type=Path)
    floor.add_argument("--locations", required=True, type=Path)
    floor.add_argument("--orders", required=True, type=Path)
    floor.add_argument("--items", required=True, type=Path)
    floor.add_argument("--pickers", required=True, type=Path)
    floor.add_argument("--travel", required=True, type=Path)
    floor.add_argument("--depot-id", default=None)
    floor.add_argument("--depot-x", default=None, type=float)
    floor.add_argument("--depot-y", default=None, type=float)
    floor.add_argument("--depots", default=None, type=Path)
    floor.add_argument("--speed", default=1.0, type=float)
    floor.add_argument("--title", default=None)
    floor.add_argument("--out", required=True, type=Path)
    floor.set_defaults(func=_floor_main)

    html = commands.add_parser(
        "html",
        help="Write warehouse.html and assignment.html for one solution.",
    )
    html.add_argument("--aisles", required=True, type=Path)
    html.add_argument("--cross-aisles", required=True, type=Path)
    html.add_argument("--locations", required=True, type=Path)
    html.add_argument("--orders", required=True, type=Path)
    html.add_argument("--items", required=True, type=Path)
    html.add_argument("--pickers", required=True, type=Path)
    html.add_argument("--travel", required=True, type=Path)
    html.add_argument(
        "--assignments",
        default=None,
        type=Path,
        help="Saved picker_id, order_id, duration. Omit to solve the wave.",
    )
    html.add_argument("--depot-id", default=None)
    html.add_argument("--depot-x", default=None, type=float)
    html.add_argument("--depot-y", default=None, type=float)
    html.add_argument("--depots", default=None, type=Path)
    html.add_argument("--speed", default=1.0, type=float)
    html.add_argument("--out-dir", required=True, type=Path)
    html.add_argument(
        "--scenario-id",
        default=None,
        help="Dropdown id. Defaults to the output folder name. Letters, numbers, hyphens, underscores.",
    )
    html.add_argument("--label", default=None, help="Text shown in the scenario dropdown.")
    html.set_defaults(func=_html_main)

    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.command in ("route", "floor", "html"):
        single = args.depot_id is not None or args.depot_x is not None or args.depot_y is not None
        if args.depots is not None and single:
            parser.error("Pass --depots or --depot-id/--depot-x/--depot-y, not both.")
        if args.depots is None and (args.depot_id is None or args.depot_x is None or args.depot_y is None):
            parser.error("A depot is required: --depots, or all of --depot-id, --depot-x, and --depot-y.")
    args.func(args)


if __name__ == "__main__":
    main()
