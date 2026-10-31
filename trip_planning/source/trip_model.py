"""HiGHS set partitioning. Every shipment is covered once, at minimum cost.

The model only sees candidates. It does not price trucks or read files.
Leaving a feasible shipment unplanned is allowed only when a fleet cap is set,
and then only by paying more than the cost of shipping everything.
"""

from __future__ import annotations

import math
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Sequence

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp
from scipy.sparse import csr_matrix

from config import TripConfig, tie_break_weights

_STATUS = {0: "optimal", 1: "time_limit", 2: "infeasible", 3: "unbounded", 4: "other"}


@dataclass(frozen=True)
class ComponentStatus:
    component_id: str
    shipment_ids: tuple[str, ...]
    status: str
    gap: float
    candidate_count: int


@dataclass(frozen=True)
class SolveResult:
    chosen: tuple
    unplanned_ids: tuple[str, ...]
    total_cost: float
    objective_value: float
    tie_break_total: float
    penalty_total: float
    penalty: float
    early_weight: float
    upgrade_weight: float
    components: tuple[ComponentStatus, ...]
    runtime_s: float


def solve_trip_model(candidates: Sequence, shipment_ids: Sequence[str], cfg: TripConfig) -> SolveResult:
    """Choose a partition of ``shipment_ids``. Past-due ids have no candidate."""
    started = time.perf_counter()
    ids = tuple(dict.fromkeys(shipment_ids))
    if len(ids) != len(tuple(shipment_ids)):
        raise ValueError("shipment_ids contains a duplicate.")
    early_weight, upgrade_weight = _weights(len(ids), cfg)
    _require_weight_bound(len(ids), cfg, early_weight, upgrade_weight)
    _require_candidates(candidates, ids)
    penalty = _penalty(candidates, cfg)
    if not ids:
        return SolveResult((), (), 0.0, 0.0, 0.0, 0.0, penalty, early_weight, upgrade_weight, (), 0.0)

    if cfg.max_trucks is not None:
        blocks = [(ids, list(candidates))]
    else:
        blocks = _components(ids, candidates)

    chosen = []
    unplanned: list[str] = []
    statuses: list[ComponentStatus] = []
    total_cost = 0.0
    tie_break = 0.0
    penalty_total = 0.0
    for index, (block_ids, block_candidates) in enumerate(blocks):
        component_id = f"cmp_{index:03d}"
        block = _solve_block(
            block_candidates,
            block_ids,
            cfg,
            early_weight,
            upgrade_weight,
            penalty,
            component_id,
        )
        chosen.extend(block["chosen"])
        unplanned.extend(block["unplanned"])
        total_cost += block["total_cost"]
        tie_break += block["tie_break"]
        penalty_total += block["penalty_total"]
        statuses.append(block["status"])
    objective = total_cost + tie_break + penalty_total
    return SolveResult(
        tuple(sorted(chosen, key=lambda candidate: candidate.candidate_id)),
        tuple(sorted(unplanned)),
        total_cost,
        objective,
        tie_break,
        penalty_total,
        penalty,
        early_weight,
        upgrade_weight,
        tuple(statuses),
        time.perf_counter() - started,
    )


def _weights(n_shipments: int, cfg: TripConfig) -> tuple[float, float]:
    derived_early, derived_upgrade = tie_break_weights(n_shipments, cfg.max_early_days)
    early = derived_early if cfg.early_delivery_weight is None else float(cfg.early_delivery_weight)
    upgrade = derived_upgrade if cfg.upgrade_weight is None else float(cfg.upgrade_weight)
    return early, upgrade


def _require_weight_bound(n_shipments: int, cfg: TripConfig, early: float, upgrade: float) -> None:
    total = n_shipments * (cfg.max_early_days * early + upgrade)
    if total >= 1:
        raise ValueError(
            "Tie-break weights can reach 1 THB across this plan. "
            "Lower early_delivery_weight or upgrade_weight."
        )


def _penalty(candidates: Sequence, cfg: TripConfig | None = None) -> float:
    """Uncapped: one baht above the solo costs. Capped: one baht above the dearest covering cost."""
    if cfg is not None and cfg.max_trucks is not None:
        dearest: dict[str, float] = {}
        for candidate in candidates:
            for shipment_id in candidate.member_ids:
                cost = float(candidate.cost)
                if shipment_id not in dearest or cost > dearest[shipment_id]:
                    dearest[shipment_id] = cost
        return float(sum(dearest.values()) + 1.0)
    solos: dict[str, float] = {}
    for candidate in candidates:
        if candidate.is_solo and len(candidate.member_ids) == 1:
            solos[candidate.member_ids[0]] = float(candidate.cost)
    return float(sum(solos.values()) + 1.0)


def _require_candidates(candidates: Sequence, shipment_ids: Sequence[str]) -> None:
    known = set(shipment_ids)
    seen: set[str] = set()
    for candidate in candidates:
        if candidate.candidate_id in seen:
            raise ValueError(f"Duplicate candidate id {candidate.candidate_id}.")
        seen.add(candidate.candidate_id)
        if not candidate.member_ids:
            raise ValueError(f"Candidate {candidate.candidate_id} has no shipments.")
        missing = [shipment_id for shipment_id in candidate.member_ids if shipment_id not in known]
        if missing:
            raise ValueError(
                f"Candidate {candidate.candidate_id} names unknown shipments: {', '.join(missing)}."
            )
        if not math.isfinite(float(candidate.cost)) or float(candidate.cost) < -1e-9:
            raise ValueError(f"Candidate {candidate.candidate_id} has a cost that is not finite and nonnegative.")
        if isinstance(candidate.n_trucks, bool) or not isinstance(candidate.n_trucks, int) or candidate.n_trucks < 0:
            raise ValueError(f"Candidate {candidate.candidate_id} has an invalid truck count.")
        if candidate.early_days < 0 or candidate.upgrade < 0:
            raise ValueError(f"Candidate {candidate.candidate_id} has a negative tie-break count.")


def _components(shipment_ids: Sequence[str], candidates: Sequence) -> list[tuple[tuple[str, ...], list]]:
    parent = {shipment_id: shipment_id for shipment_id in shipment_ids}

    def find(shipment_id: str) -> str:
        while parent[shipment_id] != shipment_id:
            parent[shipment_id] = parent[parent[shipment_id]]
            shipment_id = parent[shipment_id]
        return shipment_id

    def union(left: str, right: str) -> None:
        root_left, root_right = find(left), find(right)
        if root_left != root_right:
            parent[root_right] = root_left

    for candidate in candidates:
        members = tuple(candidate.member_ids)
        missing = [shipment_id for shipment_id in members if shipment_id not in parent]
        if missing:
            raise ValueError(
                f"Candidate {candidate.candidate_id} names unknown shipments: {', '.join(missing)}."
            )
        if len(members) >= 2:
            for other in members[1:]:
                union(members[0], other)

    blocks: dict[str, list] = defaultdict(list)
    owners: dict[str, list[str]] = defaultdict(list)
    for shipment_id in shipment_ids:
        owners[find(shipment_id)].append(shipment_id)
    seen: set[str] = set()
    for candidate in candidates:
        root = find(candidate.member_ids[0])
        if candidate.candidate_id in seen:
            continue
        seen.add(candidate.candidate_id)
        blocks[root].append(candidate)
    ordered = []
    for root, ids in owners.items():
        ordered.append((tuple(sorted(ids)), blocks.get(root, [])))
    ordered.sort(key=lambda item: item[0][0])
    return ordered


def _solve_block(
    candidates: Sequence,
    shipment_ids: Sequence[str],
    cfg: TripConfig,
    early_weight: float,
    upgrade_weight: float,
    penalty: float,
    component_id: str,
) -> dict:
    ids = tuple(shipment_ids)
    covered: dict[str, list[int]] = {shipment_id: [] for shipment_id in ids}
    for index, candidate in enumerate(candidates):
        for shipment_id in candidate.member_ids:
            covered[shipment_id].append(index)
    if cfg.max_trucks is not None:
        unplanned_ids = list(ids)
    else:
        unplanned_ids = [shipment_id for shipment_id in ids if not covered[shipment_id]]

    n_x = len(candidates)
    u_index = {shipment_id: n_x + index for index, shipment_id in enumerate(unplanned_ids)}
    width = n_x + len(unplanned_ids)
    rows: list[int] = []
    cols: list[int] = []
    data: list[float] = []
    lower: list[float] = []
    upper: list[float] = []
    for row, shipment_id in enumerate(ids):
        for candidate_index in covered[shipment_id]:
            rows.append(row)
            cols.append(candidate_index)
            data.append(1.0)
        if shipment_id in u_index:
            rows.append(row)
            cols.append(u_index[shipment_id])
            data.append(1.0)
        if not covered[shipment_id] and shipment_id not in u_index:
            raise RuntimeError(f"Shipment {shipment_id} has no candidate and cannot be left unplanned.")
        lower.append(1.0)
        upper.append(1.0)

    if cfg.max_trucks is not None:
        by_date: dict[object, list[int]] = defaultdict(list)
        for index, candidate in enumerate(candidates):
            if candidate.n_trucks > 0:
                by_date[candidate.ship_date].append(index)
        row = len(ids)
        for indexes in by_date.values():
            for index in indexes:
                rows.append(row)
                cols.append(index)
                data.append(float(candidates[index].n_trucks))
            lower.append(0.0)
            upper.append(float(cfg.max_trucks))
            row += 1

    cost = np.zeros(width)
    for index, candidate in enumerate(candidates):
        cost[index] = (
            float(candidate.cost)
            + early_weight * float(candidate.early_days)
            + upgrade_weight * float(candidate.upgrade)
        )
    for index in u_index.values():
        cost[index] = penalty

    matrix = csr_matrix((data, (rows, cols)), shape=(len(lower), width))
    result = milp(
        c=cost,
        integrality=np.ones(width, dtype=int),
        bounds=Bounds(np.zeros(width), np.ones(width)),
        constraints=LinearConstraint(matrix, np.array(lower, dtype=float), np.array(upper, dtype=float)),
        options={
            "disp": False,
            "time_limit": float(cfg.time_limit_s),
            "mip_rel_gap": float(cfg.mip_rel_gap),
            "presolve": True,
        },
    )
    status_name = _STATUS.get(int(result.status), "other")
    gap = _gap(result)
    chosen, unplanned = _read_solution(result, candidates, u_index, width)
    usable = chosen is not None and _fleet_ok(chosen, cfg)
    if usable:
        try:
            _require_partition(ids, chosen, unplanned)
        except RuntimeError:
            usable = False
    if not usable:
        chosen, unplanned = _fallback(candidates, ids, cfg)
        status_name = f"fallback_{status_name}"
        gap = float("inf")
    else:
        _require_partition(ids, chosen, unplanned)
    total_cost = float(sum(candidate.cost for candidate in chosen))
    tie_break = float(
        sum(
            early_weight * candidate.early_days + upgrade_weight * candidate.upgrade
            for candidate in chosen
        )
    )
    penalty_total = float(penalty * len(unplanned))
    return {
        "chosen": chosen,
        "unplanned": unplanned,
        "total_cost": total_cost,
        "tie_break": tie_break,
        "penalty_total": penalty_total,
        "status": ComponentStatus(component_id, ids, status_name, gap, len(candidates)),
    }


def _read_solution(result, candidates: Sequence, u_index: dict[str, int], width: int):
    if result.x is None or int(result.status) not in {0, 1} or len(result.x) != width:
        return None, None
    chosen = []
    unplanned = []
    for index, value in enumerate(result.x):
        number = float(value)
        if not math.isfinite(number) or number < -1e-5 or number > 1 + 1e-5:
            return None, None
        rounded = int(round(number))
        if abs(number - rounded) > 1e-4:
            return None, None
        if rounded == 0:
            continue
        if index < len(candidates):
            chosen.append(candidates[index])
        else:
            for shipment_id, variable in u_index.items():
                if variable == index:
                    unplanned.append(shipment_id)
    return chosen, unplanned


def _fleet_ok(chosen: Sequence, cfg: TripConfig) -> bool:
    if cfg.max_trucks is None:
        return True
    used: dict[object, int] = defaultdict(int)
    for candidate in chosen:
        used[candidate.ship_date] += int(candidate.n_trucks)
    return all(count <= cfg.max_trucks for count in used.values())


def _fallback(candidates: Sequence, shipment_ids: Sequence[str], cfg: TripConfig) -> tuple[list, list]:
    """A feasible cover. It does not claim the best coverage or the best cost."""
    if cfg.max_trucks is None:
        best = {}
        for candidate in candidates:
            if not candidate.is_solo or len(candidate.member_ids) != 1:
                continue
            shipment_id = candidate.member_ids[0]
            current = best.get(shipment_id)
            rank = (candidate.early_days, candidate.cost, candidate.candidate_id)
            if current is None or rank < current[0]:
                best[shipment_id] = (rank, candidate)
        chosen = [item[1] for item in best.values()]
        covered = set(best)
        return chosen, [shipment_id for shipment_id in shipment_ids if shipment_id not in covered]
    remaining = set(shipment_ids)
    used: dict[object, int] = defaultdict(int)
    chosen = []
    ranked = sorted(
        candidates,
        key=lambda candidate: (
            -len(candidate.member_ids),
            candidate.cost,
            candidate.early_days,
            candidate.candidate_id,
        ),
    )
    for candidate in ranked:
        if any(shipment_id not in remaining for shipment_id in candidate.member_ids):
            continue
        if used[candidate.ship_date] + int(candidate.n_trucks) > cfg.max_trucks:
            continue
        chosen.append(candidate)
        used[candidate.ship_date] += int(candidate.n_trucks)
        for shipment_id in candidate.member_ids:
            remaining.discard(shipment_id)
    return chosen, [shipment_id for shipment_id in shipment_ids if shipment_id in remaining]


def _require_partition(shipment_ids: Sequence[str], chosen: Sequence, unplanned: Sequence[str]) -> None:
    seen: dict[str, int] = {shipment_id: 0 for shipment_id in shipment_ids}
    for candidate in chosen:
        for shipment_id in candidate.member_ids:
            if shipment_id not in seen:
                raise RuntimeError(f"Solver selected unknown shipment {shipment_id}.")
            seen[shipment_id] += 1
    for shipment_id in unplanned:
        if shipment_id not in seen:
            raise RuntimeError(f"Solver left unknown shipment {shipment_id} unplanned.")
        seen[shipment_id] += 1
    broken = [shipment_id for shipment_id, count in seen.items() if count != 1]
    if broken:
        raise RuntimeError("Solver cover is not a partition: " + ", ".join(broken) + ".")


def _gap(result) -> float:
    if int(result.status) == 0:
        return 0.0
    gap = getattr(result, "mip_gap", None)
    if gap is None:
        return float("inf")
    gap = float(gap)
    if math.isnan(gap):
        return float("inf")
    return gap
