# Order Picking Planning Optimizer

This is the picker specification the optimizer implements. An earlier design draft that was not shipped is `planning_optimizer_spec_draft_v0.2.md`. The truck-to-job equations are `../chunker/formulation.md`.

## Overview

This project optimizes order picking operations in a warehouse that uses strict order picking (one picker completes one order at a time, no batching) and dedicated storage locations (each item has a fixed, known storage location). Given a set of customer orders to fulfill during a shift, and a set of pickers with different equipment types and capacities, the optimizer determines which orders can be fulfilled within the shift, which picker is assigned to each fulfilled order, and the pick route for each order, while balancing workload across pickers and respecting order priority.

Typical order size in the target warehouse: medium orders, with approximately 6 to 15 distinct items and 6 to 15 distinct storage locations per order. This scale is relevant to the routing phase (phase 1), since it determines whether an exact subtour-elimination formulation per order is adequate, or whether a lazy-constraint / heuristic approach is preferable. At this order size, an exact small-scale routing formulation per order is expected to remain tractable.

The model follows a two-phase approach, based on the finding that a single simultaneous model does not scale to realistic order volumes:
- Phase 1 (Routing): For each order, independently compute the shortest picking route (a single-picker traveling salesman style route) and its duration, starting and ending at a depot.
- Phase 2 (Assignment): Given the per-order route durations from phase 1, assign orders to pickers to maximize the weighted number of orders fulfilled within the shift time limit, respecting picker capacity, equipment eligibility, and balancing workload across pickers. The default backend is HiGHS. Pass `assignment_solver="gurobi"` for Gurobi, or `"auto"` to use Gurobi only when a license is available.

Phase 1 implementation note: nearest neighbor and 2-opt come from `tsp_open_solvers.py` and run on the directed travel times. A 2-opt move reverses the chain between two stops and is kept only when that reversed walk is shorter. If the search would return a longer directed tour, the nearest-neighbor tour stands. The duration is the directed walk. Exact `scip` and `highs` solve a directed tour on the same nodes: one arc leaves each node, one arc enters each node, and every proper subset must have at least one chosen arc leaving it. The undirected city model (`solve_scip`, `solve_highs`, degree 2) is not used for warehouse orders. Each order's matrix is a lookup in the precomputed travel-time table (see "Travel Distances / Times" below), not a haversine distance. The heuristic is the default because it needs no extra package and fits the 6-to-15-location order size. `scip` and `highs` are there when the directed tour must be exact.

The travel-time matrix is built by `build_travel_matrix.py` (aisle and cross-aisle centrelines, Dijkstra, optional one-way segments). The picker model does not rebuild it. Each solve only looks up the finished matrix.

This optimizer is upstream of, and functionally separate from, a downstream truck-loading optimizer ("LoadMax") that builds pallets and loads them onto trucks under weight, axle, height, and stacking rules. The two systems use distinct priority mechanisms by design (see Out of Scope). This specification covers the picking and release decision only; truck loading is out of scope.

## Objective

Primary objective: Maximize the weighted sum of orders fulfilled within the shift (the current pick wave), where each order's weight (`priority_weight`) reflects its cutoff/wave urgency and, optionally, an account-tier multiplier. Setting all order weights equal to 1 recovers the benchmark case of maximizing a plain count of orders fulfilled. The weight formula is in C8. A finite `priority_weight` already stored on the order is the final weight. Otherwise the solver fills it before optimizing.

Secondary objective: Minimize the imbalance of workload (total picking time) across pickers, weighted against the primary objective.

Priority handling for this objective is a weighted-sum (linear) scheme, not a strict lexicographic rule: the model may choose to leave a higher-weight order unfulfilled if fulfilling several lower-weight orders instead produces a better total weighted score. A separate "must-ship" case can still be represented within this same linear objective by assigning such an order a sufficiently large finite weight, rather than by introducing a hard lexicographic constraint.

Note on feasibility: this assignment model is always feasible. Leaving every order unfulfilled is a trivially valid solution, so there is no scenario analogous to a hard infeasible case; constraint pressure instead shows up as a lower achieved objective value and a larger `orders_not_fulfilled` list.

## Constraints

### C1: Shift Time Limit
Each picker's total picking time (sum of route durations for assigned orders) must not exceed the shift duration.

### C2: Picker Capacity
Each picker has a maximum weight and/or volume capacity per trip; orders assigned to a picker must respect this capacity on a per-order basis (since each order is picked individually, not batched).

### C3: Single Picker per Order (Strict Picking)
Each order, if fulfilled, is assigned to exactly one picker who completes the entire order alone.

### C4: Picker-Equipment Eligibility
An order can only be assigned to a picker whose equipment type (for example, pallet jack vs. forklift) is compatible with the order's requirements (for example, heavy or palletized items require a forklift).

### C5: Workload Balance
The difference between each picker's total assigned picking time and the average picking time across pickers should be minimized (soft constraint via objective term, not a hard limit).

### C6: Dedicated Storage (Routing Assumption)
Within phase 1, each item in an order is assumed to be located at a single, fixed storage location (no split-location picking).

### C7: Order Size (Routing Assumption)
Each order typically involves 6 to 15 distinct items/locations, which bounds the size of the per-order routing problem solved in phase 1.

### C8: Priority Weighting (Weighted Objective, Not Strict Priority)
Each order carries a `priority_weight`. The optimizer maximizes the total weighted sum of fulfilled orders rather than a simple count. An equal-weight configuration (`priority_weight = 1` for all orders, equivalently `benchmark_mode = true`) is retained as a benchmark that recovers plain count-maximization. Strict lexicographic prioritization is out of scope. A must-ship order is a large finite weight (`must_ship` sets 100000, which stays below the 1e6 coefficient range).

Derivation, applied only when the row has no finite `priority_weight` (`compute_priority_weight`):

- Urgency is a wave band. One wave defaults to 8 hours (`wave_hours`). A numeric `cutoff_time` with no `now` means hours remaining. Otherwise hours remaining is `cutoff_time - now` (both numbers, or both datetimes).
- Overdue (hours remaining < 0): urgency 6. Due this wave (0 to `wave_hours`): 4. Due next wave (up to two waves): 2. Later: 1.
- No cutoff: the same 4 / 2 / 1 bands use how long the order has waited (`wait_time` in hours, or `now - queued_since`). Waiting is never the overdue band.
- Account tier multiplies urgency. Defaults, until real multipliers replace them: `key` and `A` = 2; `standard`, `B`, and `C` = 1; blank = 1. A boolean tier, or `key_account` when tier is blank, uses the `key` multiplier. An unknown tier is an error. Pass `tier_multipliers` to replace the whole table.
- `priority_weight = urgency × tier multiplier`.

`benchmark_mode` forces every weight to 1 after that derivation.

### C9: Precomputed Travel-Time Matrix (Routing Assumption)
Phase 1 reads a location-to-location travel-time matrix. `build_travel_matrix.py` produces that matrix from aisles, cross-aisles, slot positions, and one or more depots. Rerun it when the layout, one-way rules, retrieval times, or walking speed change. The picker solve only consumes the result. Phase 1 still starts and ends at the one depot the caller passes.

Each aisle is one centreline. A slot's `y` is the stop on that line, and the left and right racks of the aisle share it. The walk graph has no z coordinate. Extra time to reach an upper level is a larger `retrieval_time` on that slot. A separate stop per rack face, or a lift moving while the picker travels, is outside this model.

## Input

### Orders
DataFrame containing the orders to be considered for the shift (pick wave).
- Fields:
  - order_id (string): Unique identifier for the order
  - total_weight (float): Total physical weight of the order (used for picker capacity constraints)
  - total_volume (float, optional): Total physical volume of the order (used for picker capacity constraints, if applicable)
  - required_equipment (string, optional): Equipment type required to fulfill this order (for example, "forklift"); if blank or not provided, the order is eligible for any picker's equipment type. Used for C4.
  - cutoff_time (float or datetime, optional): Time by which the order must be released to make its outbound truck/wave. "Due date" means the order slips to the next pick wave if missed, not a multi-day due date. Used only when `priority_weight` is absent.
  - account_tier (string, bool, or number, optional): Customer account tier. Defaults: key/A = 2×, standard/B/C = 1×. Unknown names raise. Used only when `priority_weight` is absent.
  - key_account (bool, optional): Used as tier `key` when `account_tier` is blank.
  - wait_time (float, optional): Hours the order has already been queued. Used when `cutoff_time` is absent.
  - queued_since (float or datetime, optional): Clock time the order entered the queue. Requires `now`. Used when `cutoff_time` and `wait_time` are absent.
  - must_ship (bool, optional): When true, and no explicit `priority_weight` is set, the weight is 100000.
  - priority_weight (float, optional): Final weight used in the objective. When this cell is a finite number >= 0 and below 1e6, it is kept. When it is missing, it is derived as in C8. `benchmark_mode` still forces it to 1.

### Order Lines / Items
DataFrame mapping orders to the items they contain and the storage location of each item.
- Fields:
  - order_id (string): Order identifier
  - item_id (string): Item identifier
  - location_id (string): Storage location of the item
  - pick_time (float, optional): Handling time for this line. Added into `route_duration`. Omitted lines contribute 0. Duplicate locations are visited once; their pick times still add.
- Typical scale: 6 to 15 order lines (and 6 to 15 distinct locations) per order. One distinct location is a there-and-back walk, not a TSP. An order with no locations is an error.

### Locations
Not read by this optimizer. Coordinates belong to the separate script that builds the travel-time matrix (C9). This project consumes that matrix only.

### Travel Distances / Times
Precomputed data giving the travel time between every pair of locations that can appear together in an order (including the depot). Supplied either as a dict-of-dicts lookup or as a DataFrame.
- Fields (DataFrame form):
  - from_location (string): Origin location
  - to_location (string): Destination location
  - travel_time (float): Time (or distance) to travel between the two locations
- Produced by `build_travel_matrix.py` from aisle centrelines. See `inputs/layout_example/` for the CSV shapes. A block connector is a cross-aisle whose x range spans those blocks.
- `locations.csv` may include `retrieval_time` (minutes). A finite value is added when a walk arrives at that slot. A blank cell uses the global `--retrieval-time` (default 0). A stored 0 stays 0. This is slot handling, including reach time for an upper level. It does not depend on which item is picked. Item handling stays on the order line as `pick_time`. Leave retrieval at 0 when lines already carry `pick_time`, so the same minutes are not counted twice.
- One depot is `--depot-id`, `--depot-x`, `--depot-y`. Several pack stations are `--depots`, a CSV with `depot_id`, `x`, `y`. All of them are nodes in one graph. `--assignment-out` writes `location_id`, `depot_id`, `round_trip_time`: each slot goes to the depot with the shortest there-and-back walk. Equal times use the lexicographically smaller `depot_id`. That label is layout data. Phase 1 does not choose the depot, and it does not split an order across depots. Run one wave per pack station on the slots assigned to it.

### Pickers
DataFrame containing the pickers available for the shift.
- Fields:
  - picker_id (string): Unique identifier for the picker
  - equipment_type (string): Type of equipment used (for example, pallet_jack, forklift)
  - max_weight (float): Maximum weight capacity per order/trip, in kilograms. The six-aisle sample (`inputs/layout_example/pickers.csv`) uses 2500 for a pallet jack and 5000 for a forklift. The wave crew is `inputs/crew.csv` (count, kilograms, volume, shift minutes). The wave's order mix is `inputs/wave_settings.csv` (size, seed, pick time, line counts, heavy share, priority mix). The builder reads those files instead of a fixed fleet. A heavier order cannot ride the lighter machine even when `required_equipment` is blank.
  - max_volume (float, optional): Maximum volume capacity per order/trip (if applicable)
  - shift_duration (float): Total available working time for the picker during the shift

### Model Settings
- benchmark_mode (boolean, model-level setting, default false): When true, overrides all `priority_weight` values to 1, so the objective reduces to maximizing a plain count of orders fulfilled. Used as a comparison baseline against the weighted production objective, and as the recommended v1 starting mode if cutoff/account-tier data is not yet available from source systems.
- balance_weight (float, model-level setting, default 0): Weight applied to the workload-imbalance penalty term (C5) in the objective. 0 disables workload balancing, focusing purely on weighted throughput. The CLI flag `--share-shift` sets this to 0.001: one ordinary order is worth 1000 minutes of unevenness, so the minutes spread across eligible pickers without dropping an order that still fits.
- phase1_solver (string, model-level setting, default "heuristic"): Selects the phase 1 routing method: "heuristic" (nearest-neighbor plus directed 2-opt, no additional packages required), or "scip" / "highs" (exact directed tour, one arc in and one arc out of each node, requiring the respective optional package). The duration passed to phase 2 is the directed walk of the tour that is returned.
- assignment_solver (string, model-level setting, default "highs"): Phase 2 assignment backend. The default is HiGHS through `scipy.optimize.milp` (`order_picking_highs.py`). "gurobi" requires a license and raises if none is available. "auto" uses Gurobi when `gurobipy` imports and a license checks out, and otherwise HiGHS. The solution dictionary includes `solver` (`"gurobi"` or `"highs"`). `status` is that backend's own status code; `is_optimal` is the portable flag.
- now (float or datetime, optional): Clock used with `cutoff_time` and `queued_since`. Required when those values are datetimes.
- wave_hours (float, default 8): Length of one pick wave, in hours.
- tier_multipliers (dict, optional): Replaces the default account-tier table. Keys are tier names.
- verbose (boolean, default false): When true, `solve_order_picking` prints `format_picking_solution`.

## Output

### Phase 1 Output: Order Routes
- Fields:
  - order_id (string): Order identifier
  - route (list): Ordered sequence of locations visited to pick the order, starting and ending at the depot
  - route_duration (float): Total time to complete the route (travel plus pick time)

### Phase 2 Output: Solution Dictionary
- Fields:
  - status (int): Backend status code. Gurobi's optimal code is 2. HiGHS via SciPy uses 0 for optimal.
  - solver (string): "gurobi" or "highs", the phase 2 backend that produced this result
  - is_optimal (boolean): Whether an optimal solution was found
  - objective_value (float or None): Weighted objective value if optimal, None otherwise
  - orders_fulfilled (list): Order IDs that were fulfilled
  - orders_not_fulfilled (list): Order IDs that were not fulfilled, each with a reason ("no_eligible_picker" if no picker is compatible under C2/C4, or "shift_time_limit_or_priority_tradeoff" if the order was feasible for at least one picker but not selected by the optimal weighted assignment)
  - picker_assignments (dict): Mapping of picker_id to list of assigned order_ids
  - picker_workload (dict): Mapping of picker_id to total assigned picking time
  - order_priority_weight (dict): Mapping of fulfilled order_id to the `priority_weight` used in the objective, provided for downstream use (for example, as the input weight for pallets of that order in a separate downstream loading optimizer). This project does not perform that downstream loading step; it only produces this value as a handoff.
- The combined end-to-end result additionally includes a `routes` key holding the full Phase 1 Output (above) for every order, keyed by order_id.

### Print Output
`format_picking_solution(result)` returns the display text: orders fulfilled versus total orders, weighted score, picker assignments, each picker's workload, and the max-minus-min workload spread. `solve_order_picking(..., verbose=True)` prints that text. A non-optimal solve prints the score as "not optimal".

## Test Scenarios

### T1: Basic Feasible Case
**Description**: With ample shift time and equal order weights, all orders should be fulfilled and assigned to an eligible picker.

**Input Data**:
- Orders: O1 (weight 5, priority_weight 1), O2 (weight 5, priority_weight 1)
- Pickers: P1 (pallet_jack, max_weight 50, shift_duration 100)
- Route durations (phase 1 output): O1 = 10, O2 = 10

**Acceptance Criteria**:
- is_optimal is True
- Both O1 and O2 are in orders_fulfilled
- objective_value equals 2.0
- picker_assignments["P1"] contains both orders

### T2: Weighted Priority Trade-off
**Description**: One picker with limited shift time cannot fit both a single high-weight order and two low-weight orders together. The weighted objective should prefer the single high-weight order over the larger count of low-weight orders, demonstrating C8's non-strict weighted priority.

**Input Data**:
- Orders: BIG (priority_weight 5), SMALL1 (priority_weight 1), SMALL2 (priority_weight 1)
- Pickers: P1 (shift_duration 10)
- Route durations: BIG = 9, SMALL1 = 4, SMALL2 = 4

**Acceptance Criteria**:
- is_optimal is True
- orders_fulfilled contains only BIG (weight 5 beats SMALL1+SMALL2's combined weight 2)
- objective_value equals 5.0
- SMALL1 and SMALL2 appear in orders_not_fulfilled with reason "shift_time_limit_or_priority_tradeoff"

### T3: Equipment Eligibility Exclusion
**Description**: An order requiring a forklift cannot be assigned to a pallet-jack-only picker, even though weight and shift time would otherwise allow it (C4).

**Input Data**:
- Orders: HEAVY (required_equipment "forklift", priority_weight 1)
- Pickers: P1 (equipment_type pallet_jack, max_weight 50, shift_duration 100)
- Route durations: HEAVY = 5

**Acceptance Criteria**:
- is_optimal is True
- orders_fulfilled is empty
- objective_value equals 0.0
- orders_not_fulfilled contains HEAVY with reason "no_eligible_picker"

### T4: Benchmark Mode vs. Weighted Mode
**Description**: Using the same data as T2, enabling benchmark_mode should select a different order set (maximizing count) than the weighted production objective (maximizing weighted score), confirming C8's benchmark/production distinction.

**Input Data**: Same as T2.

**Acceptance Criteria**:
- With benchmark_mode False: orders_fulfilled is [BIG]
- With benchmark_mode True: orders_fulfilled is {SMALL1, SMALL2} and objective_value equals 2.0

### T5: No-Capacity Edge Case
**Description**: Every order's route duration exceeds the only picker's shift duration. The model should remain feasible (optimal) with no orders fulfilled, rather than reporting infeasibility, consistent with the specification's note that this problem has no hard-infeasible case.

**Input Data**:
- Orders: O1, O2 (priority_weight 1 each)
- Pickers: P1 (shift_duration 1)
- Route durations: O1 = 5, O2 = 5

**Acceptance Criteria**:
- is_optimal is True
- orders_fulfilled is empty
- objective_value equals 0.0
- picker_workload["P1"] equals 0.0

### T6: Phase 1 Routing Validity
**Description**: A single order's route, computed with the default heuristic solver, must be a valid closed tour starting and ending at the depot and visiting every pick location exactly once.

**Input Data**:
- Depot plus 3 pick locations arranged in a unit square (so the optimal tour is the square's perimeter)

**Acceptance Criteria**:
- route starts and ends at the depot
- route visits the depot plus all 3 pick locations, each exactly once
- route_duration equals the known optimal perimeter distance within a small tolerance

### T7: Derived Priority Weight
**Description**: Rows with no `priority_weight` take the C8 band formula. An explicit `priority_weight` is kept. `benchmark_mode` still forces 1. An unknown tier raises.

**Acceptance Criteria**:
- Hours remaining 4, tier A, wave 8 hours → weight 8 (urgency 4 × tier 2)
- Hours remaining 10 → weight 2 (next wave)
- Hours remaining 20 → weight 1
- Hours remaining −1 → weight 6 (overdue)
- No cutoff, `wait_time` 16, wave 8 → weight 4
- No cutoff and no wait → weight 1
- Explicit `priority_weight` 5 is kept even if the cutoff is overdue
- `must_ship` with no explicit weight → 100000
- Unknown tier name raises `ValueError`
- The same band weights, fed through assignment with the T2 durations, fulfill the this-wave order; `benchmark_mode` fulfills the two later orders instead

### T8: Short Route and Pick Time
**Description**: One pick location is depot → location → depot. Line `pick_time` is added to travel. A missing travel-time pair raises `ValueError`. An order with no lines raises `ValueError` from `solve_order_picking`.

### T9: Capacity Exclusion
**Description**: An order heavier than every picker's `max_weight`, or larger than every `max_volume`, is `no_eligible_picker` (C2), same as an equipment mismatch.

## Out of Scope

- Order batching (multiple orders picked in a single tour)
- Zone picking
- Split-location picking (same item stored in multiple locations)
- Stochastic or dynamic order arrival during the shift
- Picker-specific speed variation, fatigue, or learning effects
- Real-time replanning once the shift has started
- Strict lexicographic order priority (always fulfilling higher-priority orders first regardless of total throughput impact); a "must-ship" order is instead modeled as a very large finite `priority_weight` within the same linear objective
- Rebuilding the travel-time matrix inside a pick-wave solve. `build_travel_matrix.py` does that offline; Phase 1 only looks up the finished matrix (C9)
- Per-item retrieval time inside the travel matrix. A visit's handling time is the order line's `pick_time`
- A z coordinate or a lift-travel edge in the walk graph. Upper-level effort is the slot's `retrieval_time`
- Left and right rack faces as distinct stops. Both share the aisle centreline (C9)
- Choosing the depot inside Phase 1 or Phase 2, and splitting one order across depots. Nearest-depot labels from `build_travel_matrix.py` are static layout output, not zone picking
- Truck loading and the downstream LoadMax optimizer itself (pallet building, weight/axle/height/stack constraints, truck departure order via `container_priority`, and the SKU-level 2x incentive `π`). This optimizer's `priority_weight` field is intentionally separate from LoadMax's `container_priority` and `skuPriorityIncentive` fields; they are not the same priority mechanism and must not be merged into one shared field. If both systems are run together operationally, the SKU-level 2x incentive in LoadMax should be disabled for pallets built from this optimizer's released orders, so that it cannot displace a key-account or cutoff-driven order that picking already selected; this coordination rule is noted here for awareness but its enforcement lives outside this project.
- Orders that LoadMax drops at load time (trailer full) are not automatically re-queued by this optimizer; feeding dropped pallets back into the next pick wave is a process to be handled outside this specification's scope.
