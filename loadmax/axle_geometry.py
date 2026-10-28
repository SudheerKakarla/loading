"""Post-solve axle check from pad X and trailer geometry.

Does not change the MIP. LoadMax front/rear cargo is still A+C / B+D on
pads. This maps each used pad to the center of its floor bay, then applies
the PackingSolver semi-trailer lever (include/packingsolver/algorithms/truck.hpp).

Trailer numbers are assumed (not in the Excel dump). Cargo-only kingpin /
rear (tare off) is the quantity that can be compared to LoadMax front/rear.
Drive/rear with tare on are scale-style axles and will be larger.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple


# 30-pad A-B: two lanes, 15 bays. Odd pads left (A/B), even pads right (C/D).
# 15 x 40" along the trailer is 50' of floor — a 53' van with pallets turned,
# not 15 pallets stuffed into a 40' box.
DEFAULT_PAD_PITCH_IN = 40.0

# US 53' dry van + day-cab tractor, inches and lb. Replace when the fleet
# spec is known. PackingSolver's README example is a short EU trailer in mm/kg.
ASSUMED_US_53 = {
    "tractor_weight": 17500.0,
    "front_axle_middle_axle_distance": 220.0,
    "front_axle_tractor_gravity_center_distance": 90.0,
    "front_axle_harness_distance": 200.0,
    "empty_trailer_weight": 14500.0,
    "harness_rear_axle_distance": 492.0,
    "trailer_gravity_center_rear_axle_distance": 200.0,
    "trailer_start_harness_distance": 36.0,
}


@dataclass(frozen=True)
class TrailerGeometry:
    tractor_weight: float
    front_axle_middle_axle_distance: float
    front_axle_tractor_gravity_center_distance: float
    front_axle_harness_distance: float
    empty_trailer_weight: float
    harness_rear_axle_distance: float
    trailer_gravity_center_rear_axle_distance: float
    trailer_start_harness_distance: float

    @classmethod
    def assumed_us_53(cls) -> "TrailerGeometry":
        return cls(**ASSUMED_US_53)

    def cargo_only(self) -> "TrailerGeometry":
        """Same distances, no tractor / empty-trailer weight."""
        return TrailerGeometry(
            tractor_weight=0.0,
            front_axle_middle_axle_distance=self.front_axle_middle_axle_distance,
            front_axle_tractor_gravity_center_distance=self.front_axle_tractor_gravity_center_distance,
            front_axle_harness_distance=self.front_axle_harness_distance,
            empty_trailer_weight=0.0,
            harness_rear_axle_distance=self.harness_rear_axle_distance,
            trailer_gravity_center_rear_axle_distance=self.trailer_gravity_center_rear_axle_distance,
            trailer_start_harness_distance=self.trailer_start_harness_distance,
        )


def pad_bay(position: int) -> int:
    """1-based bay along the trailer. Pads 1+2 = bay 1 (nose), 29+30 = bay 15 (door)."""
    return (int(position) + 1) // 2


def pad_x_center(position: int, pitch_in: float = DEFAULT_PAD_PITCH_IN) -> float:
    """X of the pad center from the cargo nose, same units as pitch."""
    return (pad_bay(position) - 0.5) * float(pitch_in)


def pad_x_map(
    positions: Iterable[int],
    pitch_in: float = DEFAULT_PAD_PITCH_IN,
) -> Dict[int, float]:
    return {int(p): pad_x_center(int(p), pitch_in) for p in positions}


def compute_axle_weights(
    weight_weighted_sum: float,
    weight: float,
    geo: TrailerGeometry,
) -> Tuple[float, float, float]:
    """PackingSolver SemiTrailerTruckData::compute_axle_weights.

    Returns (middle/drive axle, rear axle, kingpin/harness load).
    """
    if weight <= 0:
        return 0.0, 0.0, 0.0
    eje = weight_weighted_sum / weight
    ejr = (
        geo.trailer_start_harness_distance
        + geo.harness_rear_axle_distance
        - eje
    )
    harness = (
        weight * ejr
        + geo.empty_trailer_weight * geo.trailer_gravity_center_rear_axle_distance
    ) / geo.harness_rear_axle_distance
    rear = weight + geo.empty_trailer_weight - harness
    if geo.front_axle_middle_axle_distance:
        middle = (
            geo.tractor_weight * geo.front_axle_tractor_gravity_center_distance
            + harness * geo.front_axle_harness_distance
        ) / geo.front_axle_middle_axle_distance
    else:
        middle = harness
    return middle, rear, harness


def _bal_coeff(bal: Mapping, position: Any, keys: tuple) -> float:
    total = 0.0
    for key in keys:
        val = bal.get((position, key), 0.0)
        try:
            total += float(val or 0.0)
        except (TypeError, ValueError):
            pass
    return total


def loadmax_cargo_front_rear(
    items: Iterable[Any],
    input_params: Optional[dict] = None,
) -> Tuple[float, float]:
    """Same A+C / B+D formula as results_summary._cargo_front_rear."""
    ip = input_params or {}
    front_id = set(ip.get("frontPositionList") or [])
    rear_id = set(ip.get("rearPositionList") or [])
    bal = ip.get("positionGroupBalanceCoefficientDict") or {}
    if not front_id:
        front_id = set(range(1, 15))
    if not rear_id:
        rear_id = set(range(13, 31))
    if not bal:
        bal = _default_ab_balance()

    front = 0.0
    rear = 0.0
    for instr in items:
        pos = int(getattr(instr, "position", instr["Position"]))
        wt = float(getattr(instr, "weight", instr["Weight"]) or 0)
        if pos in front_id:
            coeff = _bal_coeff(bal, pos, ("A", "C"))
            front += wt * (coeff if bal else 1.0)
        if pos in rear_id:
            coeff = _bal_coeff(bal, pos, ("B", "D"))
            rear += wt * (coeff if bal else 1.0)
    return front, rear


def _default_ab_balance() -> dict:
    nonzero = {
        (1, "A"): 1.0, (2, "C"): 1.0, (3, "A"): 1.0, (4, "C"): 1.0,
        (5, "A"): 1.0, (6, "C"): 1.0, (7, "A"): 1.0, (8, "C"): 1.0,
        (9, "A"): 1.0, (10, "C"): 1.0, (11, "A"): 1.0, (12, "C"): 1.0,
        (13, "A"): 0.5, (13, "B"): 0.5, (14, "C"): 0.5, (14, "D"): 0.5,
        (15, "B"): 1.0, (16, "D"): 1.0, (17, "B"): 1.0, (18, "D"): 1.0,
        (19, "B"): 1.0, (20, "D"): 1.0, (21, "B"): 1.0, (22, "D"): 1.0,
        (23, "B"): 1.0, (24, "D"): 1.0, (25, "B"): 1.0, (26, "D"): 1.0,
        (27, "B"): 1.0, (28, "D"): 1.0, (29, "B"): 1.0, (30, "D"): 1.0,
    }
    bal = {}
    for p in range(1, 31):
        for s in ("A", "B", "C", "D"):
            bal[(p, s)] = nonzero.get((p, s), 0.0)
    return bal


def _item_pos_wt(instr: Any) -> Tuple[int, float]:
    if hasattr(instr, "position"):
        return int(instr.position), float(instr.weight or 0)
    return int(instr["Position"]), float(instr["Weight"] or 0)


def geometry_axles_for_items(
    items: Iterable[Any],
    *,
    pitch_in: float = DEFAULT_PAD_PITCH_IN,
    geo: Optional[TrailerGeometry] = None,
) -> dict:
    """Kingpin/rear from cargo X, plus optional tare-on drive/rear axles."""
    geo = geo or TrailerGeometry.assumed_us_53()
    weight = 0.0
    moment = 0.0
    by_bay: Dict[int, float] = defaultdict(float)
    for instr in items:
        pos, wt = _item_pos_wt(instr)
        if wt <= 0:
            continue
        x = pad_x_center(pos, pitch_in)
        weight += wt
        moment += wt * x
        by_bay[pad_bay(pos)] += wt

    x_cg = (moment / weight) if weight else 0.0
    cargo_geo = geo.cargo_only()
    _, rear_cargo, kingpin = compute_axle_weights(moment, weight, cargo_geo)
    drive, rear_tare, kingpin_tare = compute_axle_weights(moment, weight, geo)
    return {
        "cargo_weight": weight,
        "cargo_x_cg_in": x_cg,
        "physics_cargo_front": kingpin,
        "physics_cargo_rear": rear_cargo,
        "drive_axle_with_tare": drive,
        "rear_axle_with_tare": rear_tare,
        "kingpin_with_tare": kingpin_tare,
        "bay_weights": dict(by_bay),
    }


def compare_container(
    items: List[Any],
    input_params: Optional[dict] = None,
    *,
    pitch_in: float = DEFAULT_PAD_PITCH_IN,
    geo: Optional[TrailerGeometry] = None,
) -> dict:
    items = list(items)
    lm_front, lm_rear = loadmax_cargo_front_rear(items, input_params)
    phys = geometry_axles_for_items(items, pitch_in=pitch_in, geo=geo)
    front_delta = phys["physics_cargo_front"] - lm_front
    rear_delta = phys["physics_cargo_rear"] - lm_rear
    lm_total = lm_front + lm_rear
    phys_total = phys["physics_cargo_front"] + phys["physics_cargo_rear"]
    return {
        "n_pallets": len(list(items)),
        "loadmax_front": lm_front,
        "loadmax_rear": lm_rear,
        "physics_cargo_front": phys["physics_cargo_front"],
        "physics_cargo_rear": phys["physics_cargo_rear"],
        "front_delta": front_delta,
        "rear_delta": rear_delta,
        "front_delta_pct": (front_delta / lm_front * 100) if lm_front else 0.0,
        "rear_delta_pct": (rear_delta / lm_rear * 100) if lm_rear else 0.0,
        "loadmax_front_share_pct": (lm_front / lm_total * 100) if lm_total else 0.0,
        "physics_front_share_pct": (
            phys["physics_cargo_front"] / phys_total * 100
        ) if phys_total else 0.0,
        **phys,
    }
