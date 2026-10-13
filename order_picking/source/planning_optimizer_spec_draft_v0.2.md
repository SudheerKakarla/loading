# Order Picking Planning Optimizer

**Design draft v0.2, not the live spec.** The picker code follows `order_picking_planning_optimizer_spec.md` in this folder. This draft's Phase 1 default (Held-Karp) and its single `deadline_penalty_weight` term were not what shipped. Lateness, when enabled, is minutes past `cutoff_time` under earliest-deadline order. The chunker equations are `../chunker/formulation.md`.

**Specification v0.2** — revised after review of v0.1.

## Revision notes (v0.1 to v0.2)

| # | Change | Where |
|---|---|---|
| 1 | Soft-deadline test T8 corrected; penalty-versus-weight behavior stated explicitly; companion test T8b added | C14, T8, T8b |
| 2 | Phase 1 default changed to exact Held-Karp for small orders; heuristic must be asymmetric-safe and has a realistic iteration cap | Phase 1, Model Settings, T13 |
| 3 | Missing input fields added: pick time, quantity, volume per line | Input |
| 4 | Units and time axis defined (minutes since wave start, shift start, datetime conversion, unit sanity checks) | Units and Time Axis |
| 5 | Chunked-job value normalized so chunking does not change a truck's total value; cutoff no longer double-counted when deadlines are enforced | C8, C16 |
| 6 | Chunker objective, algorithm and quality test defined (matrix-only, no aisle data needed) | C10 to C13, T14 |
| 7 | Reason codes refined with explicit rules | Output |
| 8 | Solver-neutral status, time limit, MIP gap, and assignment solver setting added | Model Settings, Output |
| 9 | Priority weight structure, equipment compatibility, balance formula, and validation rules added | C4, C5, C8, Validation |
| 10 | Tests T5, T6, T10 corrected; T11 to T20 added | Test Scenarios |

Items marked **[confirm]** are defaults chosen in this revision that should be checked against the reference implementation or the warehouse. They are collected under "Open Items" at the end.

## Overview

This project optimizes order picking in a warehouse that uses strict order picking (one picker completes one order at a time, with no batching of distinct customer orders into one trip) and dedicated storage (each item has one fixed, known storage location). Given a set of orders (jobs) to fulfill in a wave, and a set of pickers with different equipment and capacities, the optimizer decides which orders are fulfilled, which picker handles each, and the pick route for each, while balancing workload, respecting order priority, and, where applicable, enforcing outbound truck deadlines.

Typical order size: about 6 to 15 distinct items and 6 to 15 distinct storage locations per order.

The model follows a two-phase approach, based on the finding that a single simultaneous model does not scale to realistic order volumes:

- **Phase 1 (Routing).** For each order, independently compute the shortest closed picking route and its duration, starting and ending at that order's depot.
- **Phase 2 (Assignment).** Given the Phase 1 durations, assign orders to pickers to maximize the weighted number of orders fulfilled, respecting capacity, equipment eligibility, shift time, deadlines (when enabled), and workload balance.

### Phase 1 implementation

Phase 1 adapts the existing standard TSP code (`tsp_open_solvers.py`) rather than writing a new routing formulation. The only change for the warehouse context is the distance input: instead of haversine distance from latitude and longitude, each order's small distance submatrix (its depot plus its pick locations) is looked up from a precomputed, warehouse-specific travel-time matrix (see "Travel Times").

Because travel times may be asymmetric (one-way aisles), every method used in Phase 1 must be correct on asymmetric matrices. This is verified by T13 for each method, independent of how any method is implemented.

Methods, selected by `phase1_solver`:

| Value | Method | Notes |
|---|---|---|
| `auto` (default) | Held-Karp dynamic programming when the order has at most `held_karp_max_nodes` nodes (default 16, depot included); otherwise exact DFJ if a solver package is installed; otherwise the heuristic | Exact at the typical 6 to 15 location size, no extra package needed, handles asymmetric times |
| `held_karp` | Exact dynamic program | Forces Held-Karp; error above 20 nodes |
| `heuristic` | Nearest neighbor plus 2-opt | The gain computation must include the cost of reversed arcs inside the swapped segment so it is correct on asymmetric matrices. Iteration cap is `heuristic_max_iterations`, default `max(50, 5 × n)` |
| `scip` / `highs` | Exact directed DFJ with lazy subtour cuts | Requires the respective package |

Building the travel-time matrix (for example, shortest paths over an aisle and rack graph respecting racking, cross-aisles and one-way aisles) is out of scope. A separate script produces it, and this project only consumes it.

This optimizer is upstream of, and functionally separate from, a downstream truck-loading optimizer ("LoadMax") that builds pallets and loads trucks. This specification covers picking and release only.

### Load-driven releases and the chunker

Orders do not always originate as customer-sized orders. A common upstream case is a load builder that has already decided what must ship on a specific truck, by a specific departure time, from a specific dock. A truck's full load usually exceeds what one picker can carry in one trip, so it must first be broken into picker-feasible jobs by a distinct upstream stage, the **chunker**, which produces the same job shape Phase 1 and Phase 2 already consume. After chunking, each job is handled like any other order.

## Units and Time Axis

All durations (travel time, pick time, overhead, route duration, shift duration, overload) are in **minutes**. All datetimes are converted to **minutes since `wave_start`** before any arithmetic.

- `wave_start` (datetime, model setting): the time origin of the wave.
- Each picker has an optional `shift_start` (datetime, default `wave_start`). A picker's available time before a time T (minutes since wave start) is `A_r(T) = min(max(0, T − s_r), shift_duration_r)`, where `s_r` is the picker's shift start in minutes.
- Any helper that works in hours (for example, urgency bands measured against `wave_hours`) must use the same minutes converted to hours; it must not read raw durations directly.
- **Unit sanity warnings** (not errors), emitted before solving: a route duration above half of the longest shift; a median route duration below 0.1 minute; a travel-time matrix whose median leg is implausible for the configured layout. These catch calibration mistakes such as a travel speed set far too low.

## Objective

Maximize

```
sum_o  w_o * y_o                      (weighted jobs fulfilled)
 - balance_weight          * sum_r u_r          (workload imbalance, C5)
 - deadline_penalty_weight * Z                  (deadline overload, soft mode only, C14)
```

where `y_o` is 1 if job o is fulfilled.

- **Primary term:** the weighted sum of fulfilled jobs. Setting all weights to 1 recovers a plain count (benchmark mode).
- **Weighted, not lexicographic:** the model may leave a higher-weight job unfulfilled if several lower-weight jobs produce a better total. A "must-ship" job is represented by a very large finite weight.
- **Secondary term:** minimize workload imbalance (C5), weighted by `balance_weight`.
- **Tertiary term:** deadline overload (soft mode only), weighted by `deadline_penalty_weight`.

**Feasibility note.** The assignment model is always feasible: leaving every job unfulfilled is valid. Constraint pressure shows up as a lower objective, more unfulfilled jobs, and, in soft mode, nonzero overload. Hard deadline mode keeps this property: a job that cannot meet its cutoff is left unfulfilled instead of making the model infeasible.

**Interaction between weights and penalty.** Soft mode does not guarantee that every job is fulfilled. Overload is accepted only when a job's weight outweighs the penalty it causes. With a large `deadline_penalty_weight`, soft mode behaves almost like hard mode. To make the model accept lateness for a given job (for example, a must-ship job), give that job a weight larger than the penalty its lateness would cost.

## Constraints

### C1: Shift Time Limit
Each picker's total assigned duration must not exceed `shift_duration`.

### C2: Picker Capacity
Each picker has a maximum weight and, optionally, volume per trip. A job can be assigned to a picker only if its total weight and total volume are within that picker's limits (an eligibility test on each job, since each job is picked individually). The volume test is skipped when either the job or the picker has no volume value.

### C3: Single Picker per Job
A fulfilled job is assigned to exactly one picker, who completes it alone: `sum_r x[o,r] = y[o]`. A picker may receive several jobs; each is assigned whole.

### C4: Equipment Eligibility
A job with `required_equipment` blank is eligible for any picker. A job with a required type is eligible for pickers whose equipment is compatible with it according to `equipment_compatibility`.

Default **[confirm]**: a forklift picker is compatible with every requirement; a pallet-jack picker is compatible only with blank and `pallet_jack` requirements.

### C5: Workload Balance
Let `w_r = sum_o d_o * x[o,r]` be picker r's assigned duration (minutes) and `w_bar = (1/|R|) * sum_q w_q`. Since `|R|` is a known constant, this is linear. Imbalance variables satisfy

```
u_r >= w_r - w_bar
u_r >= w_bar - w_r
```

and enter the objective with weight `balance_weight` (default 0, disabled). Scale hint: with job weights near 1 to 2, a value around 0.001 to 0.01 per minute makes imbalance a tie-breaker rather than a competitor to throughput.

### C6: Dedicated Storage (Routing Assumption)
Within Phase 1, each item is at a single fixed location. No split-location picking. If several lines of one job share a location, the location is visited once.

### C7: Order Size (Routing Assumption)
A job typically has 6 to 15 distinct locations, which bounds the Phase 1 problem and is why exact Held-Karp is the default.

### C8: Priority Weighting
Each job carries a `priority_weight`. The optimizer maximizes the weighted sum of fulfilled jobs.

- `benchmark_mode = true` overrides every weight to 1.
- A must-ship job gets `must_ship_weight`, a very large finite value (default 100000).
- For customer orders, the weight is `urgency_factor × tier_factor`. For chunked jobs, see C16.
- Strict lexicographic priority is out of scope.

### C9: Precomputed Travel-Time Matrix (Routing Assumption)
Phase 1 assumes a location-to-location travel-time matrix exists as input. One flat table may contain several depot nodes; "built per depot" only describes how the file may be assembled. Computing the matrix is out of scope.

**Retrieval time.** Define exactly one place where per-stop retrieval time is counted. Either the matrix includes retrieval time at the destination, or `pick_time` carries it, but not both. This specification assumes the matrix contains **walking time only** and `pick_time` carries all per-line handling **[confirm]**.

### C10: Load Decomposition into Picker-Feasible Jobs (Chunker)
When an upstream load exceeds one picker's trip capacity, the chunker splits it into jobs such that each job satisfies C2. It uses the same travel-time matrix as Phase 1 and groups lines by walking proximity, not by arbitrary identifiers such as SKU id.

**Algorithm (default).** For each (truck, depot) group:

1. Units to be placed are individual lines or, when destination grouping is on (C11), whole destination groups.
2. Build jobs with the capacity-constrained savings heuristic on the matrix, with the job's depot as the hub. For linking unit i before unit j, the saving is `d(i→depot) + d(depot→j) − d(i→j) + job_overhead`. Directed travel times are used as given.
3. A merge is allowed only if the combined weight, volume and line count stay within `max_job_weight`, `max_job_volume` and `max_job_lines`.
4. Job ids are `truck_id-Jnn`.

The method uses only the matrix, so it works with the Locations schema as defined (no aisle or bay data required). A sort-and-cut S-shape rule may be offered as an alternative when aisle data exists.

A single line that exceeds the limit on its own cannot be chunked; it is emitted as its own job and will be reported `no_eligible_picker` if no picker can carry it.

### C11: Destination Grouping (Chunker)
When `require_destination_grouping` is true, a destination group is not split across jobs unless the group alone exceeds a job's limits. In that case it is split in walking order, where walking order for the group is the visiting order of a nearest-neighbor path from the depot over the group's locations, computed from the matrix.

### C12: Per-Job Depot (Routing)
Each job's route starts and ends at the depot named in its `depot_id`, not at one warehouse-wide depot.

### C13: Per-Job Handling Overhead
Each job has a fixed handling overhead `job_overhead` (minutes) for scanning at the start and handing over at the dock. It is added to the duration of **every** job, whether customer order or chunked job, so that all jobs are costed the same way. It also enters the chunker's merge decision, which discourages over-splitting.

### C14: Deadline Enforcement per Picker (Assignment)
Applies when `deadline_mode` is `soft` or `hard`. Let `T` range over every distinct `cutoff_time` in the wave (in minutes since wave start), and let `d_o` be job o's duration. For each picker r and each T:

```
sum over jobs o with cutoff_o <= T of  d_o * x[o,r]   <=   A_r(T) + z[r,T]
```

with `z[r,T] >= 0` in soft mode and `z[r,T] = 0` in hard mode. This is the exact feasibility condition for one resource processing jobs in earliest-cutoff-first order (Jackson's rule), provided all jobs are available at the start of the wave. Jobs without a cutoff contribute only to C1.

- **Soft mode (default).** `Z = sum over (r,T) of z[r,T]` is penalized with `deadline_penalty_weight`. Each distinct cutoff counts once, not each job. With `deadline_penalty_aggregation = "max"`, the penalty is applied to each picker's maximum overload instead. Soft mode can still leave jobs unfulfilled (see Objective).
- **Hard mode.** No overload is permitted. A job that cannot meet its cutoff is left unfulfilled and reported with its own reason.
- **Off.** C14 is skipped, and `cutoff_time` affects only the priority weight.

**Limitations (also under Out of Scope).** The condition is exact only when all jobs are available at the start of the wave, and walking between the dock where one job ends and the dock where the next begins is not included in durations.

### C15: Truck-Level All-or-Nothing (Optional)
When `truck_all_or_nothing` is true, all jobs with the same `truck_id` must be fulfilled together or not at all: a binary `v[t]` per truck with `y[o] = v[t]` for every job o of truck t. Disabled by default, since soft mode already surfaces at-risk trucks.

### C16: Value of Chunked Jobs and the Role of Cutoff
**Chunk-invariant value.** For a chunked job, `priority_weight = base_weight × job_lines / reference_order_lines`, where `base_weight` is computed as for an order from the truck's urgency and tier. The total value of a truck's jobs is then `base_weight × truck_lines / reference_order_lines`, independent of how many jobs the chunker creates. `reference_order_lines` is a setting, default 10 **[confirm]**. A job containing any `must_ship` line gets `must_ship_weight` unscaled.

**No double counting of cutoff.** When `deadline_mode` is `soft` or `hard`, the urgency component of `priority_weight` is set to its neutral value, and the cutoff influences the plan only through C14 **[confirm]**. When `deadline_mode` is `off`, urgency is computed from the cutoff as before.

## Input

All field names below are the canonical names. Sample data files must use exactly these names.

### Orders (Jobs)

| Field | Type | Notes |
|---|---|---|
| `order_id` | string | Unique. For chunked jobs, this is the job id |
| `total_weight` | float | Derived from lines when lines carry weight |
| `total_volume` | float, optional | Same |
| `required_equipment` | string, optional | Blank means any picker (C4) |
| `cutoff_time` | datetime, optional | Converted to minutes since wave start. Used for the deadline (C14) and, when deadlines are off, for the urgency weight |
| `account_tier` | string or int, optional | Used for the tier factor |
| `priority_weight` | float | Final objective weight. If missing, derived per C8 and C16 |
| `truck_id` | string, optional | Blank when not tied to a truck |
| `depot_id` | string, optional | Defaults to the single warehouse depot |

### Order Lines

| Field | Type | Notes |
|---|---|---|
| `order_id` | string | |
| `item_id` | string | |
| `location_id` | string | Must exist in the travel-time matrix |
| `quantity` | float | Default 1 |
| `weight` | float | Line weight, used for totals |
| `volume` | float, optional | Line volume |
| `pick_time` | float (minutes) | Handling time for this line. If absent, `default_pick_time` is used |

### Truck Loads (Chunker Input)

| Field | Type | Notes |
|---|---|---|
| `truck_id` | string | |
| `depot_id` | string | Dock this truck loads from |
| `departure_time` | datetime | The chunker sets each job's `cutoff_time` to this minus `staging_buffer` |
| `destination_id` | string, optional | For destination grouping (C11) |
| `item_id` | string | |
| `location_id` | string | |
| `quantity` | float | |
| `weight` | float | |
| `volume` | float, optional | Needed when `max_job_volume` is used |
| `pick_time` | float (minutes), optional | As above |
| `must_ship` | boolean, optional | Propagates per C16 |

### Locations

| Field | Type | Notes |
|---|---|---|
| `location_id` | string | Includes depots |
| `x`, `y` | float | Informational and for plotting; the chunker and Phase 1 use the matrix |

### Travel Times

Flat table; directed (the value from A to B may differ from B to A). One table may contain several depots.

| Field | Type | Notes |
|---|---|---|
| `from_location` | string | |
| `to_location` | string | |
| `travel_time` | float (minutes) | Walking time only (see C9) |

### Pickers

| Field | Type | Notes |
|---|---|---|
| `picker_id` | string | |
| `equipment_type` | string | For example `pallet_jack`, `forklift` |
| `max_weight` | float | Operational per-trip weight limit, not the machine's rated structural capacity |
| `max_volume` | float, optional | |
| `shift_duration` | float (minutes) | |
| `shift_start` | datetime, optional | Default `wave_start` |

### Model Settings

| Setting | Default | Meaning |
|---|---|---|
| `wave_start` | required when any datetime is used | Time origin |
| `benchmark_mode` | false | Overrides all weights to 1 |
| `balance_weight` | 0 | Weight on workload imbalance (C5) |
| `phase1_solver` | `auto` | See Phase 1 methods |
| `held_karp_max_nodes` | 16 | Largest order solved by Held-Karp under `auto` |
| `heuristic_max_iterations` | `max(50, 5 × n)` | Cap for 2-opt improvement |
| `assignment_solver` | `highs` | Phase 2 solver. `gurobi` accepted if licensed |
| `phase2_time_limit` | 60 seconds | |
| `mip_gap` | 0.01 | Relative gap at which Phase 2 may stop |
| `deadline_mode` | `soft` | `soft`, `hard` or `off` |
| `deadline_penalty_weight` | 0.5 × typical standard weight per minute **[confirm]** | Weight on overload in soft mode |
| `deadline_penalty_aggregation` | `sum` | `sum` or `max` |
| `job_overhead` | 5 minutes | C13 |
| `default_pick_time` | 0.5 minute per line | Used when a line has no `pick_time` |
| `staging_buffer` | 30 minutes | Subtracted from departure to get cutoff |
| `max_job_weight`, `max_job_volume` | Largest eligible picker's limit | Chunker capacity |
| `max_job_lines` | 15 | Chunker line cap |
| `require_destination_grouping` | false | Enables C11 |
| `reference_order_lines` | 10 | C16 |
| `must_ship_weight` | 100000 | C8 |
| `truck_all_or_nothing` | false | Enables C15 |
| `equipment_compatibility` | forklift compatible with all; pallet jack with blank and `pallet_jack` | C4 |

### Priority Weight Structure

`priority_weight = urgency_factor × tier_factor`, with `must_ship_weight` overriding when applicable. The urgency factor depends on hours until cutoff relative to `wave_hours` (default 8), in bands: within the current wave, the next wave, and later. In the reference implementation, a standard-tier job in the next-wave band (for example, a cutoff 10 or 12 hours out against `wave_hours` 8) has weight 2.0. The factor table for the other bands and for tiers is to be copied from the reference implementation **[confirm]**.

## Validation

Validation runs before any solve. Errors stop the run and name the offending record; warnings are reported and the run continues.

| Condition | Result |
|---|---|
| Duplicate `order_id` | Error |
| Order line whose `location_id` is not in the travel-time matrix | Error, naming order and location |
| Depot id with no row in the matrix | Error |
| Negative or non-numeric travel time | Error |
| `cutoff_time` earlier than `wave_start` | Error |
| Job with no lines | Error |
| Job whose weight or volume exceeds every picker | Not an error: job is reported `no_eligible_picker` |
| Unreachable location pair (missing leg in the matrix) | Error for any order that needs it |
| Unit sanity warnings (see Units and Time Axis) | Warning |

## Output

### Chunker Output: Jobs

`truck_id`, `job_id`, `depot_id`, `cutoff_time`, `priority_weight`, and the job's lines in Order Lines shape.

### Phase 1 Output: Order Routes

| Field | Notes |
|---|---|
| `order_id` | |
| `route` | Ordered locations, starting and ending at the job's depot |
| `route_duration` | Walking time plus pick time plus `job_overhead` (minutes) |

### Phase 2 Output: Solution Dictionary

| Field | Notes |
|---|---|
| `status` | Solver-neutral: `optimal`, `feasible` (stopped at time limit or gap), or `no_solution` |
| `is_optimal` | True only if proven optimal |
| `mip_gap` | Achieved relative gap |
| `solver` | Name of the solver used |
| `objective_value` | Value of the full objective, or None |
| `orders_fulfilled` | List of order ids |
| `orders_not_fulfilled` | List of `{order_id, reason}` (see below) |
| `picker_assignments` | picker_id to list of order ids |
| `picker_workload` | picker_id to assigned minutes |
| `picker_overload` | Soft mode only: picker_id to `{cutoff_time: overload_minutes}` |
| `truck_status` | truck_id to `{jobs_total, jobs_fulfilled, max_overload}`; shows which trucks are at risk |
| `order_priority_weight` | Fulfilled order id to weight used, for handoff to downstream systems |

The combined end-to-end result also includes `routes`, the full Phase 1 output keyed by `order_id`.

**Reason codes**, applied in this order:

1. `no_eligible_picker`: no picker satisfies C2 and C4.
2. `route_exceeds_available_window`: the job's duration alone exceeds the shift duration of every eligible picker (C1).
3. `deadline_exceeded_hard_mode` (hard mode only): the job fits a shift alone, but either it exceeds the time before its own cutoff for every eligible picker, or adding it to the final assignment of any eligible picker would violate a C14 constraint. Determined by a check on the final plan.
4. `shift_time_limit_or_priority_tradeoff`: otherwise. The job was feasible for at least one picker but was displaced by a better combination, including a soft-mode drop caused by the overload penalty.

### Print Output

Number of jobs fulfilled versus total, weighted score, picker assignments, workload balance, and any picker or cutoff overload, with at-risk trucks listed.

## Non-Functional Requirements

Proposed targets **[confirm]**:

- Scale: up to about 1,000 jobs and 30 pickers per wave. Phase 2 has one binary per eligible (job, picker) pair plus one per job, which is small enough for HiGHS.
- Phase 1 for a typical order (up to 16 nodes with Held-Karp) completes in well under a second.
- Reproducibility: the same input and settings produce the same plan. Solver parameters and seeds are logged.
- Chunker overhead and the assignment solve are reported separately in run logs.

## Test Scenarios

Unless stated, tests use `wave_start` as the time origin, `deadline_mode = off`, `job_overhead = 0`, and benchmark mode off.

### T1: Basic Feasible Case
Orders O1, O2 (weight 5, priority_weight 1). Picker P1 (pallet_jack, max_weight 50, shift 100). Durations 10 and 10.
Expect: optimal; both fulfilled; objective 2.0; P1 holds both.

### T2: Weighted Priority Trade-off
BIG (priority_weight 5, duration 9), SMALL1 and SMALL2 (weight 1, duration 4). P1 shift 10.
Expect: optimal; only BIG fulfilled; objective 5.0; SMALL1 and SMALL2 reported `shift_time_limit_or_priority_tradeoff`.

### T3: Equipment Eligibility Exclusion
HEAVY (`required_equipment` forklift, weight 1, duration 5). P1 pallet jack, max_weight 50, shift 100.
Expect: optimal; none fulfilled; objective 0.0; HEAVY reported `no_eligible_picker`.

### T4: Benchmark Mode vs Weighted Mode
Data from T2. Expect: benchmark off gives [BIG]; benchmark on gives {SMALL1, SMALL2} with objective 2.0.

### T5: No-Capacity Edge Case
O1, O2 (weight 1, duration 5 each). P1 shift 1.
Expect: optimal (not infeasible); none fulfilled; objective 0.0; workload of P1 is 0.0; both reported `route_exceeds_available_window`.

### T6: Phase 1 Routing Validity
Depot (0,0) and pick locations (1,0), (1,1), (0,1) with Euclidean travel time. `default_pick_time = 0` and `job_overhead = 0`. Run for every available Phase 1 method.
Expect: route starts and ends at the depot; each location appears once; duration is 4.0 (the perimeter) within 1e-9. The two crossing tours of length about 4.83 must not be returned.

### T7: Chunker Capacity Split with Destination Grouping
Truck TRK1: four lines at DEST_A of 100 kg each (400 total) and three lines at DEST_B of 200 kg each (600 total). `max_job_weight = 500`, `require_destination_grouping = true`.
Expect: all four DEST_A lines in one job; DEST_B lines split across at least two jobs; no job above 500 kg; every line appears in exactly one job.

### T8: Soft Deadline Mode with Overload Reporting
J1 (cutoff 10, duration 8, priority_weight 2), J2 (cutoff 12, duration 8, priority_weight 2). P1 shift 100, `shift_start = wave_start`. `deadline_mode = soft`, `deadline_penalty_weight = 0.1`.
Expect: optimal; both fulfilled; overload at cutoff 12 equals 4 (cumulative 16 against 12) and at cutoff 10 equals 0; objective 4 − 0.1 × 4 = 3.6.

### T8b: Soft Mode Still Drops a Job When the Penalty Is High
Same data as T8 with `deadline_penalty_weight = 1.0`.
Expect: one job fulfilled, the other reported `shift_time_limit_or_priority_tradeoff`; objective 2.0; picker overload zero. (Both fulfilled would score 4 − 4 = 0, so dropping one wins.)

### T9: Hard Deadline Mode
Same data as T8 with `deadline_mode = hard`.
Expect: optimal; exactly one of J1, J2 fulfilled; objective 2.0; the other reported `deadline_exceeded_hard_mode`; `picker_overload` empty.

### T10: Per-Job Depot Routing
J1 (depot DOCK_A) and J2 (depot DOCK_B), same single pick location. One flat matrix contains both docks: DOCK_A to the location 5 each way, DOCK_B to the location 20 each way. `default_pick_time = 0`, `job_overhead = 0`.
Expect: J1 duration 10; J2 duration 40.

### T11: Workload Balance (C5)
Four jobs of duration 10 and weight 1; two pickers (shift 100 each); `balance_weight = 0.1`.
Expect: both pickers at 20.0; objective 4.0. (A 4 and 0 split would score 4 − 0.1 × 40 = 0; a 3 and 1 split scores 4 − 0.1 × 20 = 2.)

### T12: Truck All-or-Nothing (C15)
Truck T1 has jobs A and B (weight 2, duration 6 each); truck T2 has job C (weight 3, duration 6). One picker, shift 14.
Expect with the rule off: objective 5.0, C plus one of A or B fulfilled (a partial truck). With the rule on: A and B fulfilled, objective 4.0.

### T13: Asymmetric Routing
(a) Four nodes: depot D and locations A, B, C. Directed arcs D→A, A→B, B→C, C→D cost 1; every other arc costs 10. Run for every available Phase 1 method.
Expect: route D, A, B, C, D with duration 4.
(b) Property test: 50 random asymmetric matrices with 6 locations. Held-Karp must equal brute-force enumeration; the heuristic and any solver result must be a valid tour with duration at least the optimum, and exact solvers must equal it.

### T14: Chunker Walking Quality (C10)
Manhattan matrix, unit speed, depot at (0,0), `job_overhead = 0`, `default_pick_time = 0`. Six lines of 100 kg: L1 to L3 at (10,1), (10,2), (10,3) and L4 to L6 at (−10,1), (−10,2), (−10,3). `max_job_weight = 300`.
Expect: the chunker returns {L1, L2, L3} and {L4, L5, L6} as the two jobs, with total walking 52 (26 each). Splitting in interleaved id order (L1, L4, L2 and L5, L3, L6) costs 90, so the chunked result must be at most 52, a reduction of about 42%.

### T15: Chunk-Invariant Value (C16)
A truck with 20 lines, standard base weight 1, `reference_order_lines = 10`, chunked twice with different `max_job_weight` values so one run makes 2 jobs and the other 4.
Expect: the sum of job priority weights is 2.0 in both runs.

### T16: Must-Ship Weight
MUST (weight 100000, duration 9) and two standard jobs (weight 2, duration 4 each). One picker, shift 10.
Expect: only MUST fulfilled; objective 100000.

### T17: Equipment Compatibility
A job requiring `pallet_jack` and a roster with only a forklift picker.
Expect with the default compatibility map: the job is fulfilled by the forklift picker. With a map that does not allow it, it is reported `no_eligible_picker`.

### T18: Time Axis and Units
`wave_start` 06:00. Job with cutoff 06:30, duration 25, hard mode, picker `shift_start` 06:10, shift_duration 480.
Expect: the cutoff converts to 30 minutes; available time before it is 20; the job is reported `deadline_exceeded_hard_mode`. A cutoff of 05:50 raises a validation error.

### T19: Validation
Check each row of the Validation table: duplicate order id, location missing from the matrix, negative travel time, and cutoff before wave start raise errors that name the offending record. A job heavier than every picker is not an error and is reported `no_eligible_picker`.

### T20: End-to-End Smoke Test
One truck load of 12 lines across two destinations, two pickers, small matrix with two docks.
Expect: every line appears in exactly one job; each job is within capacity; each route closes at its job's depot; each fulfilled job is assigned to exactly one picker; durations include overhead; the `truck_status` counts match `orders_fulfilled`.

## Out of Scope

- **Order batching** across distinct customer orders. This differs from the chunker, which decomposes one oversized load into jobs.
- Zone picking.
- Split-location picking (the same item in several locations).
- **Customer orders exceeding every picker's capacity** are not split by the optimizer. They are reported `no_eligible_picker`. A multi-trip option is a possible future extension.
- Stochastic or dynamic order arrival during the shift, and real-time replanning.
- **Release times.** C14 is exact only when every job is available at wave start. With mid-wave releases it is only a necessary condition.
- **Inter-dock travel.** The walk between the dock where one job ends and the dock where the next job starts is not included in durations.
- Picker-specific speed, fatigue, or learning effects, and congestion between pickers.
- Strict lexicographic priority.
- Construction of the travel-time matrix.
- A fully joint optimization of job composition and routing. The chunker is distance-aware but runs as a separate stage ahead of Phase 1. The literature on whether joint optimization is worth the added complexity is mixed.
- Dock scheduling and the load builder itself, which supply truck departure times and loads.
- **Truck loading and LoadMax** (pallet building, weight, axle, height and stacking rules, truck departure order via `container_priority`, and the SKU-level incentive `π`). This optimizer's `priority_weight` is intentionally separate from LoadMax's `container_priority` and `skuPriorityIncentive` and must not be merged with them. If both run together, disable the SKU-level incentive in LoadMax for pallets built from released orders, so it cannot displace a key-account or cutoff-driven order that picking already selected. Enforcement of this rule lives outside this project.
- Jobs that LoadMax drops at load time are not automatically re-queued here.

## Open Items

Defaults chosen in this revision that need confirmation:

1. **Walking-only matrix** and `pick_time` carrying all per-line handling (C9). If the existing matrix already includes retrieval time, either remove it from the matrix or set pick time accordingly.
2. **Priority factor table** for urgency bands and tiers, to be copied from the reference implementation (Priority Weight Structure).
3. **Chunk-invariant value** using `reference_order_lines = 10` (C16).
4. **Urgency excluded from the weight** when deadline enforcement is on (C16).
5. **Equipment compatibility map** defaults (C4).
6. **`deadline_penalty_weight` default** and the `sum` aggregation (C14).
7. **Non-functional targets** (scale and run time).
8. **Time-axis defaults**: `shift_start` equal to `wave_start`, and `staging_buffer` of 30 minutes.
