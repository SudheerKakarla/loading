"""
Container Loading Optimization - HiGHS Implementation
======================================================

This module re-implements ContainerLoadingOptimizerVDR using the HiGHS solver
via scipy.optimize.milp.  It is a drop-in functional equivalent of the
Gurobi-based version and contains all four bug fixes identified in the review:

  H1  Priority pallet bonus linked to actual pallet assignments
  H2  Wrapper returns solution data (dict), not a disposed optimizer object
  H3  G3 stacking indicator uses loop variable l, not hardcoded layer 1
  H4  Cube formula converts height to feet; description uses correct field

HiGHS is accessed through scipy.optimize.milp, which ships with SciPy >= 1.7
and requires no separate licence.  The interface is:

    min  c @ x
    s.t. b_l <= A @ x <= b_u
         lb  <= x      <= ub
         x[i] in Z  for i in integrality_set

Key design differences from the Gurobi version
-----------------------------------------------
* All variables are identified by a flat integer index managed by
  VariableRegistry.  There are no named tupledict objects.
* Constraints are assembled as a scipy.sparse.lil_matrix and then converted
  to csc_array before the solve call.
* Maximisation is achieved by negating the objective vector.
* Binary variables are declared as integrality=1 with bounds [0, 1].
* Integer variables are declared as integrality=1 with the appropriate ub.

Author : Converted from Gurobi to HiGHS with VDR enhancements and bug fixes
Version: 2.1 (HiGHS port)
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import scipy.sparse as sp
from scipy.optimize import Bounds, LinearConstraint, milp

from loadmax.data_reader import (
    CapacityPlan,
    capacity_infeasible_message,
    resolve_capacity_classes,
)

# ---------------------------------------------------------------------------
# Lightweight pallet result container (replaces loadmax.models.Load_Obj)
# ---------------------------------------------------------------------------

@dataclass
class LoadObj:
    """Represents one pallet's loading instruction in the optimised plan."""
    marketID: str = ""
    container: int = 0
    layer: int = 0
    position: int = 0
    sequence: int = 0
    material: str = ""
    brand: str = ""
    description: str = ""          # H4-B fix: populated from description dict
    batch: str = ""
    cases: float = 0.0
    total_cases: float = 0.0
    weight: float = 0.0
    cube: float = 0.0              # H4-A fix: all dimensions in feet
    height: float = 0.0
    is_partial: bool = False
    pallet_type: str = "Full"
    stacking_group: str = "G1"
    location: str = ""
    shipTo: str = ""
    bin_loc: str = ""
    batch_date: str = ""
    material_count: int = 0


# ---------------------------------------------------------------------------
# Variable registry
# ---------------------------------------------------------------------------

class VariableRegistry:
    """
    Maps (name, *indices) tuples to flat integer positions in the variable
    vector passed to scipy.optimize.milp.

    Usage
    -----
    reg = VariableRegistry()
    reg.add("y_full", veh_id, lyr_id, pos_id, plt_id, integrality=1, lb=0, ub=1)
    idx = reg["y_full", v, l, p, plt]   # integer index into x vector
    """

    def __init__(self):
        self._index: Dict[Tuple, int] = {}
        self._lb: List[float] = []
        self._ub: List[float] = []
        self._integrality: List[int] = []
        self._obj: List[float] = []        # objective coefficients (minimise)

    # ------------------------------------------------------------------
    # Building phase
    # ------------------------------------------------------------------

    def add(self, name: str, *index_sets,
            integrality: int = 0,
            lb: float = 0.0,
            ub: float = np.inf,
            obj: float = 0.0) -> None:
        """Register all combinations of index_sets under the given name."""
        if not index_sets:
            key = (name,)
            if key not in self._index:
                self._index[key] = len(self._lb)
                self._lb.append(lb)
                self._ub.append(ub)
                self._integrality.append(integrality)
                self._obj.append(obj)
            return
        import itertools
        for combo in itertools.product(*index_sets):
            key = (name,) + combo
            if key not in self._index:
                self._index[key] = len(self._lb)
                self._lb.append(lb)
                self._ub.append(ub)
                self._integrality.append(integrality)
                self._obj.append(obj)

    def add_single(self, name: str, *indices,
                   integrality: int = 0,
                   lb: float = 0.0,
                   ub: float = np.inf,
                   obj: float = 0.0) -> int:
        """Register one specific (name, *indices) key and return its index."""
        key = (name,) + tuple(indices)
        if key not in self._index:
            self._index[key] = len(self._lb)
            self._lb.append(lb)
            self._ub.append(ub)
            self._integrality.append(integrality)
            self._obj.append(obj)
        return self._index[key]

    # ------------------------------------------------------------------
    # Query phase
    # ------------------------------------------------------------------

    def __getitem__(self, key) -> int:
        """Return the flat index for (name, *indices)."""
        if not isinstance(key, tuple):
            key = (key,)
        return self._index[key]

    def get(self, key, default=None):
        if not isinstance(key, tuple):
            key = (key,)
        return self._index.get(key, default)

    def __len__(self) -> int:
        return len(self._lb)

    # ------------------------------------------------------------------
    # Finalisation helpers
    # ------------------------------------------------------------------

    def lb_array(self) -> np.ndarray:
        return np.array(self._lb, dtype=float)

    def ub_array(self) -> np.ndarray:
        return np.array(self._ub, dtype=float)

    def integrality_array(self) -> np.ndarray:
        return np.array(self._integrality, dtype=int)

    def obj_array(self) -> np.ndarray:
        return np.array(self._obj, dtype=float)

    def set_obj(self, key, value: float) -> None:
        if not isinstance(key, tuple):
            key = (key,)
        self._obj[self._index[key]] = value


# ---------------------------------------------------------------------------
# Constraint builder
# ---------------------------------------------------------------------------

class ConstraintBuilder:
    """
    Builds the sparse constraint matrix row by row.

    Each constraint is  b_l <= row @ x <= b_u.
    Equality is expressed as b_l == b_u.
    """

    def __init__(self, n_vars: int):
        self._n = n_vars
        self._rows: List[Dict[int, float]] = []
        self._bl: List[float] = []
        self._bu: List[float] = []

    def add(self,
            coeffs: Dict[int, float],
            bl: float = -np.inf,
            bu: float = np.inf) -> None:
        """Add one constraint. coeffs maps variable index -> coefficient."""
        self._rows.append(coeffs)
        self._bl.append(bl)
        self._bu.append(bu)

    def add_eq(self, coeffs: Dict[int, float], rhs: float) -> None:
        self.add(coeffs, bl=rhs, bu=rhs)

    def add_le(self, coeffs: Dict[int, float], rhs: float) -> None:
        self.add(coeffs, bl=-np.inf, bu=rhs)

    def add_ge(self, coeffs: Dict[int, float], rhs: float) -> None:
        self.add(coeffs, bl=rhs, bu=np.inf)

    def to_linear_constraint(self) -> LinearConstraint:
        """Convert to scipy LinearConstraint (uses csc_array internally)."""
        m = len(self._rows)
        n = self._n
        lil = sp.lil_matrix((m, n), dtype=float)
        for r, coeffs in enumerate(self._rows):
            for col, val in coeffs.items():
                lil[r, col] = val
        A = sp.csc_array(lil)
        return LinearConstraint(A,
                                np.array(self._bl),
                                np.array(self._bu))


# ---------------------------------------------------------------------------
# Main optimizer
# ---------------------------------------------------------------------------

class ContainerLoadingOptimizerHiGHS:
    """
    Container / truck loading optimizer backed by HiGHS via scipy.optimize.milp.

    Supports all features of the Gurobi version:
      - Multi-destination milk-run (position-weighted cubic priority)
      - VDR stacking rules G2 / G3 / G4
      - Weight limits, crushing, optional axle / balance constraints
      - SKU proliferation penalty

    Bug fixes applied (vs original Gurobi version)
    -----------------------------------------------
    H1  y_prior_full / y_prior_prtl are linked to assignment variables via
        equality constraints so the bonus reflects actual loading.
    H2  The public solve() method returns a plain dict; the caller never
        holds a reference to an object whose resources have been freed.
    H3  G3 indicator constraint uses the loop variable l (not hardcoded 1).
    H4  Cube = (40/12) * (48/12) * (height_inches/12) gives cubic feet.
        description is read from palletDescriptionDict, not palletBinLocDict.

    Parameters
    ----------
    inputParams : dict
        Problem data.  Keys are identical to the Gurobi version.
    run_ocean : bool
        True for ocean containers, False for trucks.
    time_limit : float
        Maximum solve time in seconds (auto-extended to 800 s for 8+ vehicles).
    gap : float
        Relative MIP gap tolerance in [0, 1).
    sku_penalty_factor : float
        Multiplier applied to the SKU excess penalty term.
    vdr_stacking_rules : bool
        Enable G2 / G3 / G4 stacking constraints.
    """

    INF = 1e30   # used wherever scipy expects a large finite number

    def __init__(self,
                 inputParams: dict,
                 run_ocean: bool = False,
                 time_limit: float = 120.0,
                 gap: float = 0.01,
                 sku_penalty_factor: float = 1.0,
                 vdr_stacking_rules: bool = True):

        if not 0.0 <= gap < 1.0:
            raise ValueError(f"gap must be in [0, 1), got {gap}")
        if time_limit <= 0:
            raise ValueError(f"time_limit must be positive, got {time_limit}")

        self.inputParams = inputParams
        self.run_ocean = run_ocean
        self.time_limit = time_limit
        self.gap = gap
        self.sku_penalty_factor = sku_penalty_factor
        self.vdr_stacking_rules = vdr_stacking_rules

        # Constraint flags (set in _set_constraint_flags)
        self.enforceCrushingConstraints = True
        self.enforceWeightBalancing = False
        self.enforceMaxSKUConstraint = False
        self.enforcePriorityPalletConstraints = True
        self.enforceAxleWeightConstraints = False
        self.enforceCompactPacking = False
        self.enforceStackFactorConstraint = False
        self.enforceFootPrintAreaConstraint = False
        self.enforceG2stackingFactorContraint = False
        self.enforceG3stackingFactorContraint = False
        self.enforceG4stackingFactorContraint = False
        self.enforceMilkRun = False
        self._max_front_wt: Optional[float] = None
        self._max_rear_wt: Optional[float] = None
        self._cap_plan: Optional[CapacityPlan] = None

        # Built during build_model()
        self._reg: Optional[VariableRegistry] = None
        self._cb: Optional[ConstraintBuilder] = None
        self._result = None          # scipy OptimizeResult
        self._pos_weights: Optional[Dict[Any, float]] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def build_model(self) -> None:
        """Build variable registry and constraint matrix."""
        print("=" * 70)
        print("BUILDING CONTAINER LOADING MODEL  [HiGHS / scipy.optimize.milp]")
        print("=" * 70)

        self._set_constraint_flags()
        if self.enforceMilkRun:
            self._calculate_position_weights()

        print("\n[1/4] Registering decision variables ...")
        self._reg = VariableRegistry()
        self._register_variables()

        print("\n[2/4] Building base constraints ...")
        self._cb = ConstraintBuilder(len(self._reg))
        self._add_base_constraints()

        print("\n[3/4] Building VDR stacking constraints ...")
        self._add_vdr_constraints()

        print("\n[4/4] Assembling objective vector ...")
        self._build_objective()

        print("\nMODEL BUILD COMPLETE")
        print(f"  Variables   : {len(self._reg)}")
        print(f"  Constraints : {len(self._cb._rows)}")
        print("=" * 70 + "\n")

    def solve(self) -> dict:
        """
        Solve the model and return a solution dictionary.

        Returns
        -------
        dict with keys:
            status        : int  (0=optimal, 1=time/node limit, 2=infeasible,
                                  3=unbounded, 4=other)
            is_optimal    : bool
            objective_value : float or None
            mip_gap       : float or None
            runtime       : float
            message       : str
            x             : np.ndarray or None  (full variable vector)
        """
        if self._reg is None:
            raise RuntimeError("Call build_model() before solve().")

        # Dynamic time-limit extension for large fleets
        veh_id = self.inputParams["containerList"]
        tl = 800.0 if len(veh_id) >= 8 else self.time_limit

        print("\n" + "=" * 70)
        print("SOLVING  [HiGHS via scipy.optimize.milp]")
        print(f"  Vehicles     : {len(veh_id)}")
        print(f"  Time limit   : {tl} s")
        print(f"  MIP rel gap  : {self.gap}")
        print("=" * 70)

        # scipy.optimize.milp minimises, so we already negated the objective
        c = self._reg.obj_array()
        integrality = self._reg.integrality_array()
        bounds = Bounds(lb=self._reg.lb_array(), ub=self._reg.ub_array())
        constraints = self._cb.to_linear_constraint()

        options = {
            "disp": True,
            "time_limit": tl,
            "mip_rel_gap": self.gap,
            "presolve": True,
        }

        t0 = time.perf_counter()
        res = milp(c=c,
                   integrality=integrality,
                   bounds=bounds,
                   constraints=constraints,
                   options=options)
        runtime = time.perf_counter() - t0
        self._result = res

        status_map = {0: "OPTIMAL", 1: "TIME/NODE LIMIT",
                      2: "INFEASIBLE", 3: "UNBOUNDED", 4: "OTHER"}
        print(f"\nStatus  : {status_map.get(res.status, str(res.status))}")
        print(f"Message : {res.message}")
        has_solution = res.x is not None and res.fun is not None
        if has_solution:
            # objective was negated for minimisation; undo the negation
            print(f"Obj val : {-res.fun:,.4f}")
            print(f"MIP gap : {getattr(res, 'mip_gap', float('nan')):.4%}")
        message = res.message
        plan = self._cap_plan
        if res.status == 2 and plan is not None and plan.has_rule:
            extra = capacity_infeasible_message(plan)
            print(extra)
            message = f"{res.message} {extra}"
        print(f"Runtime : {runtime:.2f} s")
        print("=" * 70 + "\n")

        return {
            "status": res.status,
            "is_optimal": res.status == 0,
            "has_solution": has_solution,
            "objective_value": (-res.fun) if has_solution else None,
            "mip_gap": getattr(res, "mip_gap", None),
            "runtime": runtime,
            "message": message,
            "x": res.x if has_solution else None,
        }

    def get_solution(self) -> Optional[dict]:
        """
        Extract structured solution from the last solve() call.

        Returns None if no feasible solution is available.

        Returns
        -------
        dict with keys:
            y_full        : {(v,l,p,plt): 1}  assigned full pallets
            y_prtl        : {(v,l,p,plt): 1}  assigned partial pallets
            weights       : {(v,l,p): float}
            stack_heights : {(v,p): float}
            sku_counts    : {v: int}
            excess_skus   : float
            stacking_groups : {(v,l,p): 'G2'|'G3'|'G4'}
            pallet_count  : {v: int}   derived from the assignment
            pallet_cap    : {v: int}   nominal, or the lower class cap
            rc_present    : {v: 0|1}   truck holds a Class R pallet
            tc_present    : {v: 0|1}   truck holds a Class T pallet
            rc_tc_mix     : {v: 0|1}   truck holds both
        """
        if self._result is None or self._result.x is None:
            print("No feasible solution available.")
            return None

        x = self._result.x
        reg = self._reg
        ip = self.inputParams

        veh_id = ip["containerList"]
        lyr_id = ip["layerList"]
        pos_id = ip["positionList"]
        full_id = ip["fullPalletList"]
        prtl_id = ip["partialPalletList"]

        sol: dict = {
            "y_full": {}, "y_prtl": {},
            "weights": {}, "stack_heights": {},
            "sku_counts": {}, "excess_skus": 0.0,
            "stacking_groups": {},
        }

        for v in veh_id:
            for l in lyr_id:
                for p in pos_id:
                    for plt in full_id:
                        idx = reg.get(("y_full", v, l, p, plt))
                        if idx is not None and x[idx] > 0.5:
                            sol["y_full"][(v, l, p, plt)] = 1
                    for plt in prtl_id:
                        idx = reg.get(("y_prtl", v, l, p, plt))
                        if idx is not None and x[idx] > 0.5:
                            sol["y_prtl"][(v, l, p, plt)] = 1
                    for l2 in lyr_id:
                        idx = reg.get(("wt", v, l2, p))
                        if idx is not None:
                            sol["weights"][(v, l2, p)] = x[idx]
                idx2 = reg.get(("stk_ht", v, p))
                if idx2 is not None:
                    sol["stack_heights"][(v, p)] = x[idx2]
            idx3 = reg.get(("mtl_n", v))
            if idx3 is not None:
                sol["sku_counts"][v] = x[idx3]

        idx4 = reg.get(("exceed_mtl_n",))
        if idx4 is not None:
            sol["excess_skus"] = x[idx4]

        plan = self._cap_plan or resolve_capacity_classes(ip)
        class_r = plan.class_r_ids
        class_t = plan.class_t_ids
        pallet_count = {v: 0 for v in veh_id}
        class_r_count = {v: 0 for v in veh_id}
        class_t_count = {v: 0 for v in veh_id}
        for (v, _l, _p, plt) in sol["y_full"]:
            pallet_count[v] = pallet_count.get(v, 0) + 1
            if plt in class_r:
                class_r_count[v] = class_r_count.get(v, 0) + 1
            if plt in class_t:
                class_t_count[v] = class_t_count.get(v, 0) + 1
        for (v, _l, _p, plt) in sol["y_prtl"]:
            pallet_count[v] = pallet_count.get(v, 0) + 1
            if plt in class_r:
                class_r_count[v] = class_r_count.get(v, 0) + 1
            if plt in class_t:
                class_t_count[v] = class_t_count.get(v, 0) + 1
        sol["pallet_count"] = {}
        sol["pallet_cap"] = {}
        sol["rc_present"] = {}
        sol["tc_present"] = {}
        sol["rc_tc_mix"] = {}
        sol["class_r_count"] = {}
        sol["class_t_count"] = {}
        for v in veh_id:
            has_r = class_r_count.get(v, 0) > 0
            has_t = class_t_count.get(v, 0) > 0
            sol["pallet_count"][v] = pallet_count.get(v, 0)
            sol["class_r_count"][v] = class_r_count.get(v, 0)
            sol["class_t_count"][v] = class_t_count.get(v, 0)
            sol["rc_present"][v] = 1 if has_r else 0
            sol["tc_present"][v] = 1 if has_t else 0
            sol["rc_tc_mix"][v] = 1 if has_r and has_t else 0
            sol["pallet_cap"][v] = plan.applicable_cap(v, has_r, has_t)

        # VDR stacking groups
        for v in veh_id:
            for l in lyr_id:
                for p in pos_id:
                    for grp, name in (("no_stk_plt_g2", "G2"),
                                      ("no_stk_plt_g3", "G3"),
                                      ("no_stk_plt_g4", "G4")):
                        idx5 = reg.get((grp, v, l, p))
                        if idx5 is not None and x[idx5] > 0.5:
                            sol["stacking_groups"][(v, l, p)] = name

        return sol

    def generate_loading_instructions(self) -> List[LoadObj]:
        """
        Build a list of LoadObj records from the optimised solution.

        Bug fixes applied
        -----------------
        H4-A  cube = (pallet_width_ft) * (pallet_depth_ft) * (height_ft)
              where height_ft = height_inches / 12.
        H4-B  description is read from palletDescriptionDict (not
              palletBinLocDict).
        """
        if self._result is None or self._result.x is None:
            print("Cannot generate instructions - no solution available.")
            return []

        x = self._result.x
        reg = self._reg
        ip = self.inputParams

        instr: List[LoadObj] = []
        mtl_count: Dict[str, int] = {}

        def _make(c, l, p, plt, is_partial: bool) -> LoadObj:
            obj = LoadObj()
            obj.marketID = ip.get("marketID", "")
            obj.palletID = plt
            obj.container = c
            obj.layer = l
            obj.position = p
            obj.sequence = ip["positionSequenceDict"][p]
            obj.material = ip["palletMaterialDict"][plt]
            obj.brand = ip.get("palletBrandDict", {}).get(plt, "") if self.run_ocean else ""
            # H4-B fix: use the description dict, not the bin location dict
            obj.description = ip.get("palletDescriptionDict", {}).get(plt, "")
            obj.bin_loc = ip.get("palletBinLocDict", {}).get(plt, "")
            obj.batch = ip["palletBatchDict"][plt]
            obj.batch_date = ip.get("palletBatchDateDict", {}).get(plt, "")

            raw_weight = ip["palletWeightDict"][plt]
            raw_height = ip["palletHeightDict"][plt]      # inches
            pallet_w_ft = ip.get("palletWidthDict",  {}).get(plt, 40.0) / 12.0
            pallet_d_ft = ip.get("palletLengthDict", {}).get(plt, 48.0) / 12.0
            height_ft = raw_height / 12.0                 # H4-A fix

            obj.weight = raw_weight
            obj.height = raw_height
            # H4-A fix: cube in cubic feet, all dimensions converted from inches
            obj.cube = pallet_w_ft * pallet_d_ft * height_ft

            if is_partial:
                frac = ip["partialFractionValueForPartialPallet"][plt]
                obj.cases = ip["palletNumCasesDict"][plt] * frac
                obj.is_partial = True
                obj.pallet_type = "Partial"
            else:
                obj.cases = ip["palletNumCasesDict"][plt]
                obj.is_partial = False
                obj.pallet_type = "Full"

            obj.total_cases = ip["palletNumCasesDict"][plt]
            obj.stacking_group = ip.get("palletStackingGroupDict", {}).get(plt, "G1")
            obj.location = ip.get("palletLocationDict", {}).get(plt, "")
            obj.shipTo = ip.get("palletPlantDict", {}).get(plt, "")

            mtl_count[obj.material] = mtl_count.get(obj.material, 0) + 1
            return obj

        for c in ip["containerList"]:
            for p in ip["positionList"]:
                for l in ip["layerList"]:
                    for plt in ip["fullPalletList"]:
                        idx = reg.get(("y_full", c, l, p, plt))
                        if idx is not None and x[idx] >= 0.9:
                            instr.append(_make(c, l, p, plt, False))
                    for plt in ip["partialPalletList"]:
                        idx = reg.get(("y_prtl", c, l, p, plt))
                        if idx is not None and x[idx] >= 0.9:
                            instr.append(_make(c, l, p, plt, True))

        # Sort by container, then original sequence
        instr.sort(key=lambda o: (o.container, o.sequence))

        # Re-number sequence per container starting at 1
        prev_c, seq = None, 0
        for obj in instr:
            if obj.container != prev_c:
                seq = 1
                prev_c = obj.container
            else:
                seq += 1
            obj.sequence = seq

        # Fill material counts
        for obj in instr:
            obj.material_count = mtl_count.get(obj.material, 0)

        print(f"Generated {len(instr)} loading instructions.")
        return instr

    # ------------------------------------------------------------------
    # Internal: constraint flag setup
    # ------------------------------------------------------------------

    @staticmethod
    def _positive_limit(val: Any) -> Optional[float]:
        """Return a finite positive number, or None if the field is missing."""
        if val is None or val == "" or val is False:
            return None
        if isinstance(val, (list, tuple, dict, set)):
            return None
        try:
            x = float(val)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(x) or x <= 0:
            return None
        return x

    def _container_priority(self, container_id: Any) -> float:
        raw = self.inputParams.get("container_priority") or {}
        if container_id in raw:
            return float(raw[container_id])
        if isinstance(container_id, (int, float)):
            if int(container_id) in raw:
                return float(raw[int(container_id)])
            if float(container_id) in raw:
                return float(raw[float(container_id)])
        return 1.0

    def _set_constraint_flags(self) -> None:
        ip = self.inputParams
        yes = {"Yes", "yes", "YES"}
        self._max_front_wt = self._positive_limit(ip.get("max_front_wt"))
        self._max_rear_wt = self._positive_limit(ip.get("max_rear_wt"))

        if self.run_ocean:
            print("Mode: ocean containers")
            self.enforceWeightBalancing = False
            self.enforceAxleWeightConstraints = False
            self.enforceCompactPacking = False
            self.enforceStackFactorConstraint = False
            self.enforceMilkRun = True
            if self.vdr_stacking_rules:
                self.enforceG2stackingFactorContraint = True
                self.enforceG3stackingFactorContraint = True
                self.enforceG4stackingFactorContraint = True
        else:
            print("Mode: trucks")
            self.enforceWeightBalancing = True
            # Front/rear axle caps are optional. Skip them when the input
            # has no usable numbers (empty list, 0, or missing).
            if self._max_front_wt is not None and self._max_rear_wt is not None:
                self.enforceAxleWeightConstraints = True
                print(f"  Axle limits: front={self._max_front_wt}, "
                      f"rear={self._max_rear_wt}")
            else:
                self.enforceAxleWeightConstraints = False
                print("  Axle limits not provided — skipping front/rear "
                      "axle constraints (not mandatory)")
            if ip.get("enforce_compact_packing", "No") in yes:
                self.enforceCompactPacking = True
                print("  Compact packing: enabled")
            if self.vdr_stacking_rules:
                self.enforceG2stackingFactorContraint = True
                self.enforceG3stackingFactorContraint = True
                self.enforceG4stackingFactorContraint = True

        # Allow caller to override max-SKU enforcement via inputParams
        if ip.get("enforceMaxSKUConstraint", False):
            self.enforceMaxSKUConstraint = True

        self._cap_plan = resolve_capacity_classes(ip)
        plan = self._cap_plan
        if plan.class_r_active:
            print(
                f"  Class R cap {plan.cap_r} on {len(plan.class_r_ids)} pallet ids, "
                f"{plan.mode} consolidation, penalty {plan.lambda_r:g}"
            )
        if plan.class_t_cap_active:
            print(f"  Class T cap {plan.cap_t} on {len(plan.class_t_ids)} pallet ids")
        if plan.mix_active:
            print(f"  Class R / Class T mix penalty {plan.mu_mix:g}")
        if plan.use_nominal_cap:
            print(f"  Nominal pallet caps: {plan.nominal_cap}")
        if plan.use_per_truck_wt:
            print(f"  Per-truck weight caps: {plan.max_wt_by_truck}")

    # ------------------------------------------------------------------
    # Internal: position weights for milk-run
    # ------------------------------------------------------------------

    def _calculate_position_weights(self) -> Dict[Any, float]:
        pos_id = self.inputParams["positionList"]
        if not pos_id:
            raise ValueError("positionList must contain at least one position")
        if len(set(pos_id)) != len(pos_id):
            raise ValueError("positionList must not contain duplicate positions")

        # Use layout order rather than assuming IDs are 1..N. A one-position
        # layout receives the only sensible weight: 1.
        mid = max(1, len(pos_id) // 2)
        arr = np.arange(1, len(pos_id) + 1) % mid
        arr[arr == 0] = mid
        self._pos_weights = dict(zip(pos_id, arr.tolist()))
        print(f"Position weights (1=near door): {self._pos_weights}")
        return self._pos_weights

    # ------------------------------------------------------------------
    # Internal: variable registration
    # ------------------------------------------------------------------

    def _register_variables(self) -> None:
        ip = self.inputParams
        reg = self._reg

        veh_id = ip["containerList"]
        lyr_id = ip["layerList"]
        pos_id = ip["positionList"]
        full_id = ip["fullPalletList"]
        prtl_id = ip["partialPalletList"]
        mtl_id = ip["materialList"]
        sec_id = ip["sectionList"]

        max_plt_n = ip["max_plt_n"]
        max_lyr_n = ip["max_lyr_n"]
        max_ht = ip["containerHeight"]
        door_ht = ip["containerHeightAtDoor"]
        allow_ht = ip["load_allow_ht"]
        door_posn = set(ip["door_posn"])

        # --- Binary assignment variables ---
        reg.add("y_full",  veh_id, lyr_id, pos_id, full_id, integrality=1, lb=0, ub=1)
        reg.add("y_prtl",  veh_id, lyr_id, pos_id, prtl_id, integrality=1, lb=0, ub=1)
        reg.add("full_plt", veh_id, lyr_id, pos_id, integrality=1, lb=0, ub=1)
        reg.add("prtl_plt", veh_id, lyr_id, pos_id, integrality=1, lb=0, ub=1)

        # --- Priority counters (H1: bounded by max_prior; linked later) ---
        max_prior_full = ip.get("max_prior_full") or {}
        max_prior_prtl = ip.get("max_prior_prtl") or {}
        for plt in full_id:
            reg.add_single("y_prior_full", plt,
                           integrality=0,
                           lb=0,
                           ub=float(max_prior_full.get(plt, 0) or 0))
        for plt in prtl_id:
            reg.add_single("y_prior_prtl", plt,
                           integrality=0,
                           lb=0,
                           ub=float(max_prior_prtl.get(plt, 0) or 0))

        # --- Continuous physical properties ---
        reg.add("wt", veh_id, lyr_id, pos_id, integrality=0, lb=0)

        for v in veh_id:
            for p in pos_id:
                ub_ht = (door_ht - allow_ht) if p in door_posn else (max_ht - allow_ht)
                reg.add_single("stk_ht", v, p, integrality=0, lb=0, ub=ub_ht)

        reg.add("lyr_plt_n", veh_id, lyr_id, integrality=1, lb=0, ub=max_plt_n)

        reg.add("mtl_plt",   veh_id, mtl_id, integrality=1, lb=0)
        reg.add("y_mtl_plt", veh_id, mtl_id, integrality=1, lb=0, ub=1)

        mtl_n_ub = ip.get("max_sku_n", self.INF) if self.enforceMaxSKUConstraint else self.INF
        reg.add("mtl_n", veh_id, integrality=1, lb=0, ub=mtl_n_ub)

        reg.add_single("exceed_mtl_n", integrality=1, lb=0)

        reg.add("sectn_wt", veh_id, sec_id, integrality=0, lb=0)

        # --- VDR stacking group indicators ---
        if self.enforceG2stackingFactorContraint:
            reg.add("no_stk_plt_g2", veh_id, lyr_id, pos_id, integrality=1, lb=0, ub=1)
        if self.enforceG3stackingFactorContraint:
            reg.add("no_stk_plt_g3", veh_id, lyr_id, pos_id, integrality=1, lb=0, ub=1)
        if self.enforceG4stackingFactorContraint:
            reg.add("no_stk_plt_g4", veh_id, lyr_id, pos_id, integrality=1, lb=0, ub=1)

        # --- Milk-run objective components ---
        if self.enforceMilkRun:
            reg.add_single("obj_full_term4", integrality=0, lb=0)
            reg.add_single("obj_prtl_term4", integrality=0, lb=0)

        plan = self._cap_plan
        if plan is not None and plan.class_r_active:
            reg.add("rc_present", veh_id, integrality=1, lb=0, ub=1)
        if plan is not None and (plan.class_t_cap_active or plan.mix_active):
            reg.add("tc_present", veh_id, integrality=1, lb=0, ub=1)
        if plan is not None and plan.mix_active:
            reg.add("rc_tc_mix", veh_id, integrality=1, lb=0, ub=1)

        print(f"  Registered {len(reg)} variables.")

    # ------------------------------------------------------------------
    # Internal: base constraints
    # ------------------------------------------------------------------

    def _add_base_constraints(self) -> None:
        ip = self.inputParams
        reg = self._reg
        cb = self._cb

        veh_id = ip["containerList"]
        lyr_id = ip["layerList"]
        pos_id = ip["positionList"]
        full_id = ip["fullPalletList"]
        prtl_id = ip["partialPalletList"]
        mtl_id = ip["materialList"]

        wt_dict = ip["palletWeightDict"]
        ht_dict = ip["palletHeightDict"]
        plt_mtl = ip["palletMaterialDict"]

        max_wt = ip["max_wt"]
        max_lyr_n = ip["max_lyr_n"]
        max_plt_n = ip["max_plt_n"]
        unique_sku = ip["unique_sku_n"]

        front_id = ip["frontPositionList"]
        rear_id = ip["rearPositionList"]
        row1_id = list(set(ip["leftPositionList"]))
        row2_id = list(set(ip["rightPositionList"]))
        bal_coef = ip["positionGroupBalanceCoefficientDict"]
        fb_tol = ip["front_to_back_tolerance"]
        ss_tol = ip["side_to_side_tolerance"]
        max_front_wt = self._max_front_wt
        max_rear_wt = self._max_rear_wt

        full_demand = ip["fullPalletDemandByMaterialBatch"]
        prtl_demand = ip["partialPalletDemandByMaterialBatch"]

        prior_incentive = ip["priorityIncentive"]

        # 1. Total weight limit. Per-truck caps replace the scalar when provided.
        plan = self._cap_plan
        for v in veh_id:
            coeffs = {reg["wt", v, l, p]: 1.0
                      for l in lyr_id for p in pos_id}
            if plan is not None and plan.use_per_truck_wt:
                cb.add_le(coeffs, plan.max_wt_by_truck[v])
            else:
                cb.add_le(coeffs, max_wt)

        # 2. Demand satisfaction (each pallet assigned at most its demand)
        for plt in full_id:
            coeffs = {reg["y_full", v, l, p, plt]: 1.0
                      for v in veh_id for l in lyr_id for p in pos_id}
            cb.add_le(coeffs, full_demand[plt])

        for plt in prtl_id:
            coeffs = {reg["y_prtl", v, l, p, plt]: 1.0
                      for v in veh_id for l in lyr_id for p in pos_id}
            cb.add_le(coeffs, prtl_demand[plt])

        # 3. Position occupancy: full_plt indicator + prtl_plt indicator
        for v in veh_id:
            for l in lyr_id:
                for p in pos_id:
                    fp = reg["full_plt", v, l, p]
                    pp = reg["prtl_plt", v, l, p]

                    # full_plt == sum of y_full
                    eq_coeffs: Dict[int, float] = {fp: 1.0}
                    for plt in full_id:
                        eq_coeffs[reg["y_full", v, l, p, plt]] = -1.0
                    cb.add_eq(eq_coeffs, 0.0)

                    # prtl_plt == sum of y_prtl
                    eq_coeffs2: Dict[int, float] = {pp: 1.0}
                    for plt in prtl_id:
                        eq_coeffs2[reg["y_prtl", v, l, p, plt]] = -1.0
                    cb.add_eq(eq_coeffs2, 0.0)

                    # At most one pallet per spot
                    one_coeffs: Dict[int, float] = {}
                    for plt in full_id:
                        one_coeffs[reg["y_full", v, l, p, plt]] = 1.0
                    for plt in prtl_id:
                        one_coeffs[reg["y_prtl", v, l, p, plt]] = 1.0
                    cb.add_le(one_coeffs, 1.0)

        self._add_capacity_class_constraints()

        # 3b. Compact packing (same as Gurobi): fill from first_plt_n
        #     toward both ends, pair left/right, and stack layer 2 only
        #     after the first-position column is used on layer 1.
        if self.enforceCompactPacking:
            first_posn = ip.get("first_plt_n") or 0
            try:
                first_posn = int(first_posn)
            except (TypeError, ValueError):
                first_posn = 0
            pos_set = set(pos_id)
            max_posn = ip.get("max_plt_n")
            try:
                max_posn = int(max_posn) if max_posn not in (None, "") else max(pos_id)
            except (TypeError, ValueError):
                max_posn = max(pos_id)
            if max_posn not in pos_set:
                max_posn = max(pos_id)
            print(f"    [OK] Adding compact packing constraints "
                  f"(first_plt_n={first_posn}, max_pos={max_posn})...")

            for v in veh_id:
                for l in lyr_id:
                    for p in pos_id:
                        if p < first_posn and (p + 1) in pos_set:
                            cb.add_le({
                                reg["full_plt", v, l, p]: 1.0,
                                reg["prtl_plt", v, l, p]: 1.0,
                                reg["full_plt", v, l, p + 1]: -1.0,
                                reg["prtl_plt", v, l, p + 1]: -1.0,
                            }, 0.0)
                        elif p > first_posn and (p - 1) in pos_set:
                            cb.add_le({
                                reg["full_plt", v, l, p]: 1.0,
                                reg["prtl_plt", v, l, p]: 1.0,
                                reg["full_plt", v, l, p - 1]: -1.0,
                                reg["prtl_plt", v, l, p - 1]: -1.0,
                            }, 0.0)
                        if p <= first_posn and p % 2 == 0 and (p - 1) in pos_set:
                            cb.add_eq({
                                reg["full_plt", v, l, p]: 1.0,
                                reg["prtl_plt", v, l, p]: 1.0,
                                reg["full_plt", v, l, p - 1]: -1.0,
                                reg["prtl_plt", v, l, p - 1]: -1.0,
                            }, 0.0)

                if first_posn in pos_set:
                    for i, l in enumerate(lyr_id[1:]):
                        l_prev = lyr_id[i]
                        occ_first = {
                            reg["full_plt", v, l, first_posn]: 1.0,
                            reg["prtl_plt", v, l, first_posn]: 1.0,
                        }
                        if 1 in pos_set:
                            cb.add_le({
                                **occ_first,
                                reg["full_plt", v, l_prev, 1]: -1.0,
                                reg["prtl_plt", v, l_prev, 1]: -1.0,
                            }, 0.0)
                        if max_posn in pos_set:
                            cb.add_le({
                                **occ_first,
                                reg["full_plt", v, l_prev, max_posn]: -1.0,
                                reg["prtl_plt", v, l_prev, max_posn]: -1.0,
                            }, 0.0)

        # 4. Stacking restrictions (no partial on partial, no full on partial)
        for v in veh_id:
            for i, l in enumerate(lyr_id[:-1]):
                l_next = lyr_id[i + 1]
                for p in pos_id:
                    pp_cur  = reg["prtl_plt", v, l,      p]
                    pp_next = reg["prtl_plt", v, l_next, p]
                    fp_next = reg["full_plt", v, l_next, p]
                    cb.add_le({pp_cur: 1.0, pp_next: 1.0}, 1.0)   # no prtl on prtl
                    cb.add_le({pp_cur: 1.0, fp_next: 1.0}, 1.0)   # no full on prtl

        # 5. Physical calculations
        for v in veh_id:
            # Stack height
            for p in pos_id:
                ht_coeffs: Dict[int, float] = {reg["stk_ht", v, p]: 1.0}
                for l in lyr_id:
                    for plt in full_id:
                        ht_coeffs[reg["y_full", v, l, p, plt]] = -ht_dict[plt]
                    for plt in prtl_id:
                        ht_coeffs[reg["y_prtl", v, l, p, plt]] = -ht_dict[plt]
                cb.add_eq(ht_coeffs, 0.0)

            for l in lyr_id:
                # Weight at each position
                for p in pos_id:
                    w_coeffs: Dict[int, float] = {reg["wt", v, l, p]: 1.0}
                    for plt in full_id:
                        w_coeffs[reg["y_full", v, l, p, plt]] = -wt_dict[plt]
                    for plt in prtl_id:
                        w_coeffs[reg["y_prtl", v, l, p, plt]] = -wt_dict[plt]
                    cb.add_eq(w_coeffs, 0.0)

                # Layer pallet count
                lc_coeffs: Dict[int, float] = {reg["lyr_plt_n", v, l]: 1.0}
                for p in pos_id:
                    for plt in full_id:
                        lc_coeffs[reg["y_full", v, l, p, plt]] = -1.0
                    for plt in prtl_id:
                        lc_coeffs[reg["y_prtl", v, l, p, plt]] = -1.0
                cb.add_eq(lc_coeffs, 0.0)

        # 6. Crushing constraints
        #    Weight at layer l must be >= sum of weights on all layers above l.
        #    That is: wt[v,l,p] - sum_{s > l} wt[v,s,p] >= 0
        if self.enforceCrushingConstraints:
            for v in veh_id:
                for i, l in enumerate(lyr_id):
                    for p in pos_id:
                        cr_coeffs: Dict[int, float] = {reg["wt", v, l, p]: 1.0}
                        for s in lyr_id[i + 1:]:
                            cr_coeffs[reg["wt", v, s, p]] = -1.0
                        cb.add_ge(cr_coeffs, 0.0)

        # 7. Weight balance (trucks)
        if self.enforceWeightBalancing:
            for v in veh_id:
                front_wt = {reg["wt", v, l, p]: 1.0
                            for l in lyr_id for p in front_id}
                rear_wt  = {reg["wt", v, l, p]: 1.0
                            for l in lyr_id for p in rear_id}
                if fb_tol > 0:
                    # front <= (1+tol)*rear  =>  front - (1+tol)*rear <= 0
                    merged = dict(front_wt)
                    for k, v2 in rear_wt.items():
                        merged[k] = merged.get(k, 0.0) - (1 + fb_tol) * v2
                    cb.add_le(merged, 0.0)
                    # front >= (1-tol)*rear  =>  front - (1-tol)*rear >= 0
                    merged2 = dict(front_wt)
                    for k, v2 in rear_wt.items():
                        merged2[k] = merged2.get(k, 0.0) - (1 - fb_tol) * v2
                    cb.add_ge(merged2, 0.0)

                row1_w = {reg["wt", v, l, p]: 1.0
                          for l in lyr_id for p in row1_id}
                row2_w = {reg["wt", v, l, p]: 1.0
                          for l in lyr_id for p in row2_id}
                if ss_tol > 0:
                    merged3 = dict(row1_w)
                    for k, v2 in row2_w.items():
                        merged3[k] = merged3.get(k, 0.0) - (1 + ss_tol) * v2
                    cb.add_le(merged3, 0.0)
                    merged4 = dict(row1_w)
                    for k, v2 in row2_w.items():
                        merged4[k] = merged4.get(k, 0.0) - (1 - ss_tol) * v2
                    cb.add_ge(merged4, 0.0)

        # 8. Axle weight (trucks)
        #    Same formula as the Gurobi model: front uses A+C, rear uses B+D.
        #    Split positions (13/14) contribute 0.5 to each axle so
        #    front cargo + rear cargo == total cargo weight.
        if self.enforceAxleWeightConstraints:
            for v in veh_id:
                front_ax: Dict[int, float] = {}
                for l in lyr_id:
                    for p in front_id:
                        coeff = float(bal_coef.get((p, "A"), 0.0) or 0.0) + float(
                            bal_coef.get((p, "C"), 0.0) or 0.0
                        )
                        front_ax[reg["wt", v, l, p]] = front_ax.get(
                            reg["wt", v, l, p], 0.0) + coeff
                cb.add_le(front_ax, max_front_wt)

                rear_ax: Dict[int, float] = {}
                for l in lyr_id:
                    for p in rear_id:
                        coeff = float(bal_coef.get((p, "B"), 0.0) or 0.0) + float(
                            bal_coef.get((p, "D"), 0.0) or 0.0
                        )
                        rear_ax[reg["wt", v, l, p]] = rear_ax.get(
                            reg["wt", v, l, p], 0.0) + coeff
                cb.add_le(rear_ax, max_rear_wt)

        # 9. SKU tracking
        for v in veh_id:
            for mtl in mtl_id:
                # mtl_plt[v,mtl] == count of pallets with this material
                mc: Dict[int, float] = {reg["mtl_plt", v, mtl]: 1.0}
                for plt in full_id:
                    if plt_mtl[plt] == mtl:
                        for l in lyr_id:
                            for p in pos_id:
                                mc[reg["y_full", v, l, p, plt]] = -1.0
                for plt in prtl_id:
                    if plt_mtl[plt] == mtl:
                        for l in lyr_id:
                            for p in pos_id:
                                mc[reg["y_prtl", v, l, p, plt]] = -1.0
                cb.add_eq(mc, 0.0)

                # y_mtl_plt[v,mtl] >= mtl_plt[v,mtl] / (max_lyr_n * max_plt_n)
                # => mtl_plt - (max_lyr_n*max_plt_n)*y_mtl_plt <= 0
                scale = float(max_lyr_n * max_plt_n)
                cb.add_le({reg["mtl_plt", v, mtl]: 1.0,
                           reg["y_mtl_plt", v, mtl]: -scale}, 0.0)

            # mtl_n[v] == sum of y_mtl_plt[v,.]
            mn: Dict[int, float] = {reg["mtl_n", v]: 1.0}
            for mtl in mtl_id:
                mn[reg["y_mtl_plt", v, mtl]] = -1.0
            cb.add_eq(mn, 0.0)

        # exceed_mtl_n >= sum(mtl_n[v]) - unique_sku_n
        ex: Dict[int, float] = {reg["exceed_mtl_n"]: 1.0}  # type: ignore[index]
        for v in veh_id:
            ex[reg["mtl_n", v]] = -1.0
        cb.add_ge(ex, -unique_sku)

        # 10. H1 FIX: priority bonus cannot exceed actual assignments.
        #     Use y_prior <= sum(y), not equality.
        #     Equality plus max_prior=0 would force every assignment to 0.
        for plt in full_id:
            coeffs = {reg["y_prior_full", plt]: 1.0}
            for v in veh_id:
                for l in lyr_id:
                    for p in pos_id:
                        coeffs[reg["y_full", v, l, p, plt]] = -1.0
            cb.add_le(coeffs, 0.0)

        for plt in prtl_id:
            coeffs = {reg["y_prior_prtl", plt]: 1.0}
            for v in veh_id:
                for l in lyr_id:
                    for p in pos_id:
                        coeffs[reg["y_prtl", v, l, p, plt]] = -1.0
            cb.add_le(coeffs, 0.0)

        print(f"  Base constraints built: {len(cb._rows)}")

    def _class_load_coeffs(self, vehicle: Any, pallet_ids: set, kind: str) -> Dict[int, float]:
        """Coefficients of assignments of one class on one truck. kind is y_full or y_prtl."""
        reg = self._reg
        ip = self.inputParams
        coeffs: Dict[int, float] = {}
        if not pallet_ids:
            return coeffs
        for layer in ip["layerList"]:
            for pos in ip["positionList"]:
                for plt in pallet_ids:
                    idx = reg.get((kind, vehicle, layer, pos, plt))
                    if idx is not None:
                        coeffs[idx] = 1.0
        return coeffs

    def _occupancy_coeffs(self, vehicle: Any) -> Dict[int, float]:
        reg = self._reg
        ip = self.inputParams
        coeffs: Dict[int, float] = {}
        for layer in ip["layerList"]:
            for pos in ip["positionList"]:
                coeffs[reg["full_plt", vehicle, layer, pos]] = 1.0
                coeffs[reg["prtl_plt", vehicle, layer, pos]] = 1.0
        return coeffs

    def _add_capacity_class_constraints(self) -> None:
        """Nominal caps, class presence, and conditional pallet caps.

        The presence link is two-sided. ``sum(y) <= M * indicator`` forces the
        indicator on when a class pallet is loaded. ``sum(y) >= indicator``
        keeps it off when the truck has none, so a zero penalty cannot tighten
        the cap of a truck that does not hold the class.
        """
        plan = self._cap_plan
        if plan is None or not plan.has_rule:
            return
        ip = self.inputParams
        reg = self._reg
        cb = self._cb
        veh_id = ip["containerList"]
        full_ids = set(ip["fullPalletList"])
        prtl_ids = set(ip["partialPalletList"])
        class_r_full = plan.class_r_ids & full_ids
        class_r_prtl = plan.class_r_ids & prtl_ids
        class_t_full = plan.class_t_ids & full_ids
        class_t_prtl = plan.class_t_ids & prtl_ids

        if plan.use_nominal_cap:
            for vehicle in veh_id:
                cb.add_le(self._occupancy_coeffs(vehicle), float(plan.nominal_cap[vehicle]))

        def _presence(vehicle: Any, full_set: set, prtl_set: set, name: str, big: int) -> None:
            loaded = self._class_load_coeffs(vehicle, full_set, "y_full")
            loaded.update(self._class_load_coeffs(vehicle, prtl_set, "y_prtl"))
            upper = dict(loaded)
            upper[reg[name, vehicle]] = -float(big)
            cb.add_le(upper, 0.0)
            lower = dict(loaded)
            lower[reg[name, vehicle]] = -1.0
            cb.add_ge(lower, 0.0)

        if plan.class_r_active:
            for vehicle in veh_id:
                big = min(plan.nominal_cap[vehicle], plan.demand_r)
                _presence(vehicle, class_r_full, class_r_prtl, "rc_present", big)
                cap_row = self._occupancy_coeffs(vehicle)
                cap_row[reg["rc_present", vehicle]] = float(
                    plan.nominal_cap[vehicle] - plan.cap_r
                )
                cb.add_le(cap_row, float(plan.nominal_cap[vehicle]))
            if plan.hard_mode:
                limit = {reg["rc_present", vehicle]: 1.0 for vehicle in veh_id}
                cb.add_le(limit, float(plan.truck_limit))
                print(
                    f"    [OK] Hard Class R consolidation: at most {plan.truck_limit} trucks"
                )

        need_t = plan.class_t_cap_active or plan.mix_active
        if need_t:
            for vehicle in veh_id:
                big_t = min(plan.nominal_cap[vehicle], plan.demand_t)
                _presence(vehicle, class_t_full, class_t_prtl, "tc_present", big_t)
                if plan.class_t_cap_active:
                    cap_row = self._occupancy_coeffs(vehicle)
                    cap_row[reg["tc_present", vehicle]] = float(
                        plan.nominal_cap[vehicle] - plan.cap_t
                    )
                    cb.add_le(cap_row, float(plan.nominal_cap[vehicle]))

        if plan.mix_active:
            for vehicle in veh_id:
                cb.add_ge(
                    {
                        reg["rc_tc_mix", vehicle]: 1.0,
                        reg["rc_present", vehicle]: -1.0,
                        reg["tc_present", vehicle]: -1.0,
                    },
                    -1.0,
                )

    # ------------------------------------------------------------------
    # Internal: VDR stacking constraints
    # ------------------------------------------------------------------

    def _add_vdr_constraints(self) -> None:
        if not self.vdr_stacking_rules:
            print("  VDR stacking rules disabled.")
            return

        ip = self.inputParams
        reg = self._reg
        cb = self._cb

        veh_id = ip["containerList"]
        lyr_id = ip["layerList"]
        pos_id = ip["positionList"]
        full_id = ip["fullPalletList"]
        prtl_id = ip["partialPalletList"]

        g2 = ip.get("stackFactorg2Pallet") or {}
        g3 = ip.get("stackFactorg3Pallet") or {}
        g4 = ip.get("stackFactorg4Pallet") or ip.get("stackFactorq4Pallet") or {}

        g2_full = [plt for plt in full_id if g2.get(plt, 0) == 1]
        g2_prtl = [plt for plt in prtl_id if g2.get(plt, 0) == 1]
        g3_full = [plt for plt in full_id if g3.get(plt, 0) == 1]
        g3_prtl = [plt for plt in prtl_id if g3.get(plt, 0) == 1]
        g4_full = [plt for plt in full_id if g4.get(plt, 0) == 1]
        g4_prtl = [plt for plt in prtl_id if g4.get(plt, 0) == 1]

        # ---- G2: ground base, others may stack on it ----
        if self.enforceG2stackingFactorContraint:
            for v in veh_id:
                for l in lyr_id:
                    for p in pos_id:
                        ind = reg["no_stk_plt_g2", v, l, p]
                        # indicator == sum of G2 pallets at (v,l,p)
                        coeffs: Dict[int, float] = {ind: 1.0}
                        for plt in g2_full:
                            coeffs[reg["y_full", v, l, p, plt]] = -1.0
                        for plt in g2_prtl:
                            coeffs[reg["y_prtl", v, l, p, plt]] = -1.0
                        cb.add_eq(coeffs, 0.0)

                        # G2 must be on layer 1
                        if l != lyr_id[0]:
                            cb.add_eq({ind: 1.0}, 0.0)

        # ---- G3: no stack on top (H3 fix: use l, not hardcoded 1) ----
        if self.enforceG3stackingFactorContraint:
            for v in veh_id:
                for i, l in enumerate(lyr_id):
                    for p in pos_id:
                        ind = reg["no_stk_plt_g3", v, l, p]
                        # H3 FIX: indicator uses loop variable l
                        coeffs2: Dict[int, float] = {ind: 1.0}
                        for plt in g3_full:
                            coeffs2[reg["y_full", v, l, p, plt]] = -1.0
                        for plt in g3_prtl:
                            coeffs2[reg["y_prtl", v, l, p, plt]] = -1.0
                        cb.add_eq(coeffs2, 0.0)

                # Nothing can be placed on top of a G3 pallet
                for i, l in enumerate(lyr_id[:-1]):
                    l_next = lyr_id[i + 1]
                    for p in pos_id:
                        ind = reg["no_stk_plt_g3", v, l, p]
                        fp_next = reg["full_plt", v, l_next, p]
                        pp_next = reg["prtl_plt", v, l_next, p]
                        cb.add_le({ind: 1.0, fp_next: 1.0, pp_next: 1.0}, 1.0)

        # ---- G4: ground only, nothing stacks on top (most restrictive) ----
        if self.enforceG4stackingFactorContraint:
            for v in veh_id:
                for l in lyr_id:
                    for p in pos_id:
                        ind = reg["no_stk_plt_g4", v, l, p]
                        coeffs3: Dict[int, float] = {ind: 1.0}
                        for plt in g4_full:
                            coeffs3[reg["y_full", v, l, p, plt]] = -1.0
                        for plt in g4_prtl:
                            coeffs3[reg["y_prtl", v, l, p, plt]] = -1.0
                        cb.add_eq(coeffs3, 0.0)

                        if l != lyr_id[0]:
                            cb.add_eq({ind: 1.0}, 0.0)

                for i, l in enumerate(lyr_id[:-1]):
                    l_next = lyr_id[i + 1]
                    for p in pos_id:
                        ind = reg["no_stk_plt_g4", v, l, p]
                        fp_next = reg["full_plt", v, l_next, p]
                        pp_next = reg["prtl_plt", v, l_next, p]
                        cb.add_le({ind: 1.0, fp_next: 1.0, pp_next: 1.0}, 1.0)

        print(f"  VDR constraints built; total rows now: {len(cb._rows)}")

    # ------------------------------------------------------------------
    # Internal: objective vector assembly
    # ------------------------------------------------------------------

    def _build_objective(self) -> None:
        """
        Populate the objective coefficient vector.

        scipy.optimize.milp minimises, so all coefficients are negated to
        convert the original maximisation objective.

        Two truck objectives from future_addons/optimizer_pyomo.py are kept here. The
        live one is the four-term fill (priority SKUs get extra
        priorityIncentive). The older simple fill is commented out below,
        not deleted.
        """
        ip = self.inputParams
        reg = self._reg

        veh_id = ip["containerList"]
        lyr_id = ip["layerList"]
        pos_id = ip["positionList"]
        full_id = ip["fullPalletList"]
        prtl_id = ip["partialPalletList"]
        plt_mtl = ip.get("palletMaterialDict") or {}

        incentive = ip.get("priorityIncentive", 1.0) or 1.0
        sku_penalty = ip.get("sku_penalty", 0.0) or 0.0
        sku_map = ip.get("skuPriorityIncentive") or {}
        prtl_frac = ip.get("partialFractionValueForPartialPallet") or {}

        cb = self._cb

        def _frac(plt: Any) -> float:
            try:
                f = float(prtl_frac.get(plt, 1.0))
            except (TypeError, ValueError):
                f = 1.0
            if not math.isfinite(f) or f <= 0:
                return 1.0
            return f

        def _is_high_priority_sku(plt: Any) -> bool:
            # Pyomo: plt_mtl_dict[plt] in highPrioritySKUList
            if plt in sku_map:
                return True
            mtl = plt_mtl.get(plt)
            if mtl in sku_map or str(mtl) in sku_map:
                return True
            try:
                return int(mtl) in sku_map
            except (TypeError, ValueError):
                return False

        # ------------------------------------------------------------------
        # OLD Pyomo fill (commented, not deleted). Every full pallet gets
        # container_priority; partials get fraction * container_priority.
        # No extra multiplier for SKUs in skuPriorityIncentive.
        #
        #   max  sum y_full * cntr_priority
        #      + sum y_prtl * prtl_frac * cntr_priority
        #      + sum y_prior * priorityIncentive
        #      - sku_penalty * exceed_mtl_n * sku_penalty_factor
        #
        # for c in veh_id:
        #     cp = self._container_priority(c)
        #     for l in lyr_id:
        #         for p in pos_id:
        #             for plt in full_id:
        #                 reg.set_obj(("y_full", c, l, p, plt), -cp)
        #             for plt in prtl_id:
        #                 reg.set_obj(("y_prtl", c, l, p, plt), -cp * _frac(plt))
        # ------------------------------------------------------------------

        # LIVE Pyomo fill (future_addons/optimizer_pyomo.py obj_full/prtl term1+term2):
        #   high-priority SKU: container_priority * priorityIncentive
        #   other SKU:         container_priority
        #   partials:          that value * prtl_frac
        for c in veh_id:
            cp = self._container_priority(c)
            for l in lyr_id:
                for p in pos_id:
                    for plt in full_id:
                        base = cp * incentive if _is_high_priority_sku(plt) else cp
                        reg.set_obj(("y_full", c, l, p, plt), -base)
                    for plt in prtl_id:
                        base = cp * incentive if _is_high_priority_sku(plt) else cp
                        reg.set_obj(("y_prtl", c, l, p, plt), -base * _frac(plt))

        # ---- Milk-run terms: auxiliary equality constraints ----
        if self.enforceMilkRun and self._pos_weights:
            pw = self._pos_weights
            # obj_full_term4 == sum(y_full * cntr_priority * incentive * pw*sku^3)
            full_t4_coeffs: Dict[int, float] = {reg["obj_full_term4"]: 1.0}  # type: ignore[index]
            for c in veh_id:
                cp = self._container_priority(c)
                for l in lyr_id:
                    for p in pos_id:
                        for plt in full_id:
                            if plt in sku_map:
                                coeff = (cp * incentive
                                         * pw[p] * (sku_map[plt] ** 3))
                                full_t4_coeffs[reg["y_full", c, l, p, plt]] = (
                                    full_t4_coeffs.get(reg["y_full", c, l, p, plt], 0.0) - coeff
                                )
            cb.add_eq(full_t4_coeffs, 0.0)

            prtl_t4_coeffs: Dict[int, float] = {reg["obj_prtl_term4"]: 1.0}  # type: ignore[index]
            for c in veh_id:
                cp = self._container_priority(c)
                for l in lyr_id:
                    for p in pos_id:
                        for plt in prtl_id:
                            if plt in sku_map:
                                coeff = (cp * incentive
                                         * pw[p] * (sku_map[plt] ** 3))
                                prtl_t4_coeffs[reg["y_prtl", c, l, p, plt]] = (
                                    prtl_t4_coeffs.get(reg["y_prtl", c, l, p, plt], 0.0) - coeff
                                )
            cb.add_eq(prtl_t4_coeffs, 0.0)

            # Objective: -1 * obj_full_term4, -1 * obj_prtl_term4
            reg.set_obj(("obj_full_term4",), -1.0)
            reg.set_obj(("obj_prtl_term4",), -1.0)

        # ---- Priority bonuses (H1: y_prior <= assignments, UB = max_prior) ----
        for plt in full_id:
            reg.set_obj(("y_prior_full", plt), -incentive)
        for plt in prtl_id:
            reg.set_obj(("y_prior_prtl", plt), -incentive)

        # ---- SKU penalty (positive coefficient because we minimise) ----
        reg.set_obj(("exceed_mtl_n",), sku_penalty * self.sku_penalty_factor)

        # Class penalties are positive under minimisation. The same lambda
        # applies to every truck, so no truck is preferred for Class R.
        plan = self._cap_plan
        if plan is not None and plan.class_r_active:
            for v in veh_id:
                reg.set_obj(("rc_present", v), plan.lambda_r)
        if plan is not None and plan.mix_active:
            for v in veh_id:
                reg.set_obj(("rc_tc_mix", v), plan.mu_mix)

        print("  Objective vector assembled (Pyomo 4-term fill + priority - SKU penalty).")


# ---------------------------------------------------------------------------
# Compatibility wrapper  (H2 fix: returns dict, not the optimizer object)
# ---------------------------------------------------------------------------

def OptModelHiGHS(inputParams: dict,
                  run_ocean: bool = False,
                  time_limit: float = 120.0,
                  gap: float = 0.01,
                  sku_penalty_factor: float = 1.0,
                  vdr_stacking_rules: bool = True) -> dict:
    """
    Solve the container loading problem with HiGHS and return result data.

    H2 fix: this function owns the optimizer object's lifecycle entirely.
    The caller receives a plain dict with solution data, not a reference to
    the optimizer, so there is no risk of accessing a freed object.

    Parameters
    ----------
    inputParams : dict
        Problem data (identical schema to the Gurobi version).
    run_ocean, time_limit, gap, sku_penalty_factor, vdr_stacking_rules
        Same semantics as ContainerLoadingOptimizerHiGHS.

    Returns
    -------
    dict with keys:
        solve_result         : dict returned by optimizer.solve()
        solution             : dict returned by optimizer.get_solution()
        loading_instructions : list of LoadObj
        inputParams          : the original inputParams (unchanged)
    """
    optimizer = ContainerLoadingOptimizerHiGHS(
        inputParams=inputParams,
        run_ocean=run_ocean,
        time_limit=time_limit,
        gap=gap,
        sku_penalty_factor=sku_penalty_factor,
        vdr_stacking_rules=vdr_stacking_rules,
    )
    optimizer.build_model()
    solve_result = optimizer.solve()                   # H2: dict, not optimizer
    solution = optimizer.get_solution()
    loading_instructions = optimizer.generate_loading_instructions()

    return {
        "solve_result": solve_result,
        "solution": solution,
        "loading_instructions": loading_instructions,
        "inputParams": inputParams,
    }
