# Trip Planning: End-to-End Specification

Audience: a coding agent that builds the whole module from this file alone.
Status: every rule marked **[ASSUMPTION]** is provisional and must be confirmed by the business. Build it as a configurable parameter, not as a hard-coded constant.

---

## 1. Purpose and scope

Given shipment demand (orders for ship-to customers, each with a Required Delivery Date, RDD), decide which orders travel together on which truck on which day, so that total freight cost is minimum and no order is delivered after its RDD.

In scope:

- Read the VA05 workbook into tables (done: `va05_adapter.py`, `build_inputs.py`).
- Compute a baseline cost (done in part: `baseline_cost.py`; extend per section 9).
- Group orders into shipments, generate candidate trips, solve a set-partitioning MIP with HiGHS, report the plan.

Out of scope: route sequencing inside a trip, pallet or floor layout (that is `loadmax/`), picking (that is `order_picking/`), carrier tendering, live data connections.

Relationship to the repo: standalone module under `trip_planning/`. It does not import from `loadmax/future_addons/`. It reuses the pattern of `order_picking/chunker/chunk_model.py` (enumerate candidates, then set-partitioning MIP via `scipy.optimize.milp`).

## 2. Glossary

| Term | Meaning |
|------|---------|
| Order | One sales document on one RDD (from `orders.csv`). |
| Shipment | All orders for one ship-to with the same RDD. The unit that is planned. |
| Trip | One truck leaving on one ship date carrying one or more whole shipments. |
| RDD | Required Delivery Date. Hard deadline. |
| CSE | Case (carton). `Total CTN Convert` in the workbook. |
| FTL | Full truck load service. Priced per truck trip. |
| LTL | Less than truck load service. Priced per case, or per truck if cheaper (see 6.3). |
| 4W / 6W / 10W / 18W | Truck types by wheel count. |
| MT / GT | Modern trade / general trade. Selects the LTL case rate column. |

## 3. Data contracts

All CSVs are UTF-8, header row, written by `build_inputs.py` into `trip_planning/outputs/va05_aug2025/` (or a path passed on the CLI). Ids are strings, normalized by `norm_id` (no trailing `.0`, no whitespace).

### 3.1 `orders.csv` (1,436 rows)
One row per order. Required columns: `order_id`, `ship_to`, `doc_date`, `rdd`, `cases`, `weight_kg`, `volume_m3`. Check the actual header in the file with `read_csv` and map by name. Dates are ISO `YYYY-MM-DD`. Quantity is `Total CTN Convert`.

### 3.2 `customers.csv` (233 rows)
One row per ship-to: `ship_to`, `province`, `group` (customer group), `service` (`FTL` or `LTL`), `sla_days`, `name`. Service comes from Master Customer or the rate card. The "FTL" label inside LoadMax order numbers is a route label and must NOT be used as service.

### 3.3 `rates.csv` (197 rows)
One row per ship-to. Trip rates per truck type (blank = truck not offered, 0 is converted to blank by the adapter): `trip_4w`, `trip_6w`, `trip_10w`, `trip_18w`. Handling per truck type: `handling_4w`, `handling_6w`, `handling_10w`, `handling_18w`. LTL case rates `ltl_mt`, `ltl_gt` (exactly one is filled on an LTL row; MT if present, otherwise GT). Also `multidrop` flag (35 Foodland/Donki ship-tos carry the "trip rate is multidrop model" remark).

### 3.4 `trucks.csv`
`truck_type`, `capacity_kg`, `capacity_m3`. Values: 4W 1500 kg / 4 m3; 6W 5000 / 25; 10W 15000 / 35; 18W blank. **[ASSUMPTION]** 6W 25 m3 looks wrong against its 5000 kg (4W gets 4 m3 per 1500 kg); keep as given, flag in the run report. 18W has a rate but no capacity, so it is excluded from planning and listed in the report.

### 3.5 `shipments.csv` (new, written by `shipments.py`, 341 rows on the August data)
`shipment_id`, `ship_to`, `rdd`, `cases`, `weight_kg`, `volume_m3`, `release_date` (max `doc_date` of its orders), `service`, `province`, `group`, `order_ids` (semicolon separated).

### 3.6 `baseline_shipments.csv`, `baseline_summary.csv`
Existing outputs of `baseline_cost.py`. Extended in section 9.

### 3.7 Optional inputs
- `consolidation_overrides.csv`: `ship_to_a`, `ship_to_b`, `rule` (`allow` or `deny`). Symmetric.
- `config.yaml` (or CLI flags): all parameters in section 5.

## 4. Data facts the code must respect (August 2025 workbook)

- 1,406 documents became 1,436 orders (a document spanning several RDDs splits into orders).
- Totals that must reconcile after every load: 117,745 CSE, 702,897 kg, 1,841.29 m3.
- 48 orders have zero quantity. 57 orders have an RDD tighter than the customer SLA. 37 orders have ship-tos missing from Master Customer. 1 ship-to has a service conflict between sheets. 8 ship-tos have no rate (12 shipments, about 12,940 CSE, about 11 percent of volume).
- Lead time (RDD minus doc date) ranges 1 to 38 days; none is zero or negative.
- 341 shipments over 33 provinces. Only 2 shipments exceed one 10W; 48 need more than one truck.
- Trip rates are monotone across truck types (4W <= 6W <= 10W) for each ship-to.
- 4W handling is filled only for the 15 ship-tos with unloading remarks.
- Multidrop ship-tos are all LTL; their 4W trip rate is flat across distances.

## 5. Parameters (all configurable, defaults shown)

| Parameter | Default | Meaning |
|-----------|---------|---------|
| `max_shipments_per_trip` | 3 | Candidate size cap. Try 4 only after profiling. |
| `max_early_days` | 1 | **[ASSUMPTION]** Max days any shipment in a trip may arrive before its RDD. |
| `transit_days` | 0 | **[ASSUMPTION]** Days from ship date to delivery. Long lanes (up to 1,928 km round trip) may need more; allow a per-province override file later. |
| `compat_mode` | `same_province` | **[ASSUMPTION]** Which ship-tos may share a truck: `same_province`, `same_group`, `same_province_or_group`, `any`, `none`. Mutual and symmetric. |
| `max_drops_per_trip` | = `max_shipments_per_trip` | Cap on distinct ship-tos per trip. |
| `solo_only` | empty | List of ship-tos that never consolidate. |
| `handling_basis` | `per_drop` | **[ASSUMPTION]** `per_drop` or `per_truck`. See 6.2. |
| `max_trucks` | none | Optional fleet limit per ship date. Off by default. |
| `mip_rel_gap` | 0 | Must stay 0 (or <= 1e-9). The repo default 0.01 breaks the tie-breaks. |
| `time_limit_s` | 600 | Per component. |
| `early_delivery_weight` | derived | See 7.3. |
| `upgrade_weight` | derived | See 7.3. |

## 6. Business rules and cost model

### 6.1 Unit and capacity
- Plan shipments, never split a shipment across different dates or trips, except through the solo fleet (6.4).
- Capacity is two dimensional: a load fits a truck if `sum kg <= capacity_kg` AND `sum m3 <= capacity_m3`. Utilization = max of the two ratios.
- A truck type is offered to a ship-to only if its trip rate is non-blank and its capacity is known.

### 6.2 Single ship-to, truck k
`T_k(j) = trip_k(j) + handling_k(j)` where blank handling counts 0. A blank trip rate means k is not offered.

### 6.3 Multi-ship-to trip on one truck k with drop set J
- Every ship-to in J must offer truck k, and the combined kg and m3 must fit k.
- Truck cost: `trip_cost = max_{j in J} trip_k(j) + handling`, where handling is `sum_j handling_k(j)` under `per_drop`, or `max_j handling_k(j)` under `per_truck`. **[ASSUMPTION: max of trip rates is the multi-drop price rule; confirm with carrier invoices.]**
- LTL pricing is the cheaper of two. **[ASSUMPTION: cheaper-of-two; verify on carrier invoices, e.g. ship-to 135448 on 2025-08-16 (case price 12,557 vs 10W 5,722) and 616512 on 2025-08-20.]**
  - If the trip contains any FTL shipment, cost = `trip_cost` (LTL riders ride free).
  - If every shipment is LTL, cost = `min(sum_s case_rate(s) * cases(s), trip_cost)`. Case rate is MT if present, otherwise GT. Under the case-priced branch no handling is added.
- Report which branch priced each trip: `price_basis` in {`truck`, `case`}.

### 6.4 Solo candidate for every shipment (guarantees feasibility)
For each shipment s, generate one solo candidate priced with the `cheapest_fleet` mix from `baseline_cost.py` (possibly several trucks, because a shipment can exceed one truck): FTL cost = fleet cost; LTL cost = `min(case cost, fleet cost)`. Ship date = `rdd - transit_days`. Solo cost `solo(s)` is stored. Because the solo plan is always available, `model total_cost <= baseline total` must hold. A remainder of a split shipment does not consolidate (documented limitation).

### 6.5 Consolidated trips
- 2 to `max_shipments_per_trip` shipments, exactly one truck, from the compatibility rule.
- Ship date `d = min over s in trip of (rdd_s - transit_days)`. Valid only if `d >= max(release_date)` and the RDD spread (max rdd minus min rdd) is at most `max_early_days`.
- Early days = `sum_s ((rdd_s - transit_days) - d)`.
- Generate a candidate only if its cost is strictly below `sum solo(s)` of its members (valid dominance pruning, since solo candidates are always available). For each shipment set keep only the cheapest feasible truck type.

### 6.6 Deadlines
RDD is hard. No shipment arrives after its RDD. Orders already past due (`rdd - transit_days < release_date`) cannot be planned: reason `past_due`. The 57 orders with RDD tighter than SLA are planned as given and flagged `rdd_tighter_than_sla`. **[ASSUMPTION]**

### 6.7 Exclusions and reason codes
Two separate lists in the output.

Data-quality exclusions (never sent to the MIP): `no_master_customer`, `no_rate`, `zero_quantity`, `truck_type_not_offered` (no usable truck type). Keep original quantities so totals reconcile: planned + excluded = input totals.

Business leftovers (from the MIP): `past_due`, `fleet_limit`. There is no `oversized` code, because solo fleets split.

Every input order ends in exactly one place: in a trip, or in an exclusion list with one reason code.

## 7. Optimization model

### 7.1 Sets and data
- S: plannable shipments. C: candidates (solo and consolidated). Each candidate c has `members(c) subset S`, `cost(c)`, `date(c)`, `early(c)`, `upgrade(c)` (number of LTL shipments in a trip of 2 or more shipments that is truck-priced).
- Every shipment has at least one candidate (its solo candidate).

### 7.2 Variables
`x_c` binary, 1 if candidate c is used. `u_s` binary, 1 if shipment s is left unplanned (only for shipments whose solo candidate cannot be dated, i.e. `past_due`; otherwise omit `u_s`).

### 7.3 Objective
minimize `sum_c (cost(c) + e * early(c) + g * upgrade(c)) * x_c + P * sum_s u_s`

- Penalty `P = sum over feasible s of solo(s) + 1`. It is flat, so leaving something out is never cheaper than shipping it.
- Tie-break weights are tiny and derived from the data so that the total tie-break contribution is below the smallest meaningful cost difference. Concretely, with `eps = 1` THB resolution of costs: `e = g = 0.5 / (1 + max_total_tie_units)` where `max_total_tie_units = max_candidate_early_sum_over_any_plan + max_candidate_upgrade_sum_over_any_plan` (use the number of shipments times `max_early_days + 1` as a safe bound).
- Report `total_cost` (sum of `cost(c)` only), `objective_value`, `tie_break_total`, `penalty_total` separately. A tie between plans must be settled by a business rule in the data, never by the weights.

### 7.4 Constraints
- Cover: for each shipment s, `sum_{c : s in members(c)} x_c + u_s = 1`.
- Optional fleet limit: for each ship date d, `sum_{c : date(c)=d} trucks(c) * x_c <= max_trucks`. Only when `max_trucks` is supplied; this couples components.

### 7.5 Solving
- `scipy.optimize.milp` with `options={"mip_rel_gap": 0, "time_limit": ...}`.
- Decomposition: build a graph with an edge between two shipments if they appear in a common candidate. Solve each connected component separately (typically province by date window). Skip decomposition when `max_trucks` is supplied.
- If a component hits the time limit, keep the incumbent, and record `status` and `gap` per component in the report.
- Candidate generation scale on the August data (same province, RDD spread <= 1): about 1,201 pairs, 6,515 triples, 301 quads (partial). That is small; enumerate fully and prune by dominance.

## 8. Module layout

Under `trip_planning/source/` (new files marked *):

| File | Responsibility |
|------|----------------|
| `va05_adapter.py` | Done. Workbook to tables. |
| `baseline_cost.py` | Done in part. Extend per section 9. |
| `build_inputs.py` | Done. Writes the CSVs. |
| `shipments.py` * | `build_shipments(orders, customers, rates, trucks, cfg) -> (shipments, exclusions)`. Groups by (ship_to, rdd), applies data-quality exclusions and reason codes, reconciles totals. |
| `pricing.py` * | `trip_cost(members, truck, cfg) -> (cost, price_basis)`, `solo_cost(shipment, cfg)`. Pure functions, no I/O. Single source of truth for 6.2 to 6.4. |
| `candidates.py` * | Compatibility rule, date window, capacity check, dominance pruning, best-truck selection, solo candidates. |
| `trip_model.py` * | Builds and solves the MIP (section 7), per component. Returns chosen candidates and solver status. |
| `plan_trips.py` * | `argparse` CLI. Wires everything. |
| `reporting.py` * | Writes outputs, summary, comparisons to baseline. |

Tests under `trip_planning/tests/` (pytest config exists: `trip_planning/pytest.ini`).

Conventions (from `AGENTS.md`): `pathlib.Path`, `argparse`, type annotations on new public functions, small functions, run `python -m py_compile` on each modified file, spawn the `code-checker` agent, do not add `ruff` or `mypy` as requirements. Environment: Windows PowerShell, `.venv` Python 3.12 with `openpyxl`, `pandas`, `scipy`, `numpy`, `pytest`. Do not edit `DECISIONS.md` until the user confirms a finding.

## 9. Baseline extension (`baseline_cost.py`)

Current baseline treats loads as divisible and puts handling per truck. Add the cheaper-of-two baseline so the model can be compared like for like:

- For FTL shipments: cheapest fleet cost.
- For LTL shipments: `min(case cost, cheapest fleet cost)`.
- Output columns in `baseline_shipments.csv`: add `case_cost`, `fleet_cost`, `baseline_cost`, `basis`.
- `baseline_summary.csv` must report: all-trips-per-truck total (1,877,583 THB), LTL-per-case mix (1,136,022), cheaper-of-two (1,100,355 = FTL 434,142 + LTL 666,213), trucks used (379: 303 4W, 43 6W, 33 10W), weighted utilization (52 percent kg, 46 percent m3).
- Facts to reproduce: LTL case cost is below truck cost in 158 of 172 shipments and above in 14 (median 770 CSE).
- Existing 5 tests must keep passing.

## 10. Outputs (`trip_planning/outputs/<run_name>/`)

| File | Content |
|------|---------|
| `trips.csv` | One row per truck trip: `trip_id`, `ship_date`, `truck_type`, `n_trucks`, `n_drops`, `cases`, `kg`, `m3`, `util_kg`, `util_m3`, `cost`, `price_basis`, `shipment_ids`. |
| `trip_shipments.csv` | One row per shipment in a trip: `trip_id`, `shipment_id`, `ship_to`, `rdd`, `ship_date`, `early_days`. |
| `trip_orders.csv` | One row per order: `order_id`, `trip_id`. |
| `exclusions.csv` | `order_id` or `shipment_id`, `category` (`data_quality` or `business`), `reason`, `cases`, `kg`, `m3`. |
| `summary.csv` | Totals: input, planned, excluded (by reason); total cost; baseline cost; saving (THB, percent); trucks by type; utilization; trips with 2+ shipments; solver status and gap per component; tie-break and penalty totals. |
| `flags.csv` | Warnings: `rdd_tighter_than_sla`, 6W capacity suspicious, 18W without capacity, service conflict. |

## 11. CLI

```
python trip_planning/source/plan_trips.py --inputs trip_planning/outputs/va05_aug2025 \
    --out trip_planning/outputs/plan_aug2025 \
    [--max-shipments-per-trip 3] [--max-early-days 1] [--transit-days 0] \
    [--compat-mode same_province] [--handling-basis per_drop] \
    [--max-trucks N] [--time-limit 600] [--config config.yaml]
```
Defaults follow section 5. Exit non-zero if reconciliation fails or any hard rule check fails.

## 12. Test plan

### 12.1 Unit tests (hand-verified expected values)
Use small synthetic tables built in the test. Write the arithmetic in a comment beside each expected value.

1. Single ship-to, one truck type: cost equals trip plus handling.
2. Blank trip rate: truck not offered; falls to next type.
3. Truck choice by capacity: 2,500 kg goes on one 6W, not two 4W, when cheaper.
4. Capacity is two dimensional: m3 binds before kg for a bulky load.
5. Multi-drop rate: max of trip rates plus handling per drop.
6. LTL cheaper-of-two: case price below truck price picks `case`; reverse picks `truck`.
7. FTL rider rule: an LTL shipment in a trip with an FTL shipment adds 0 cost.
8. Dominance: a consolidated candidate no cheaper than the sum of solos is not generated.
9. RDD: a trip whose ship date is after any member's RDD is never generated; a spread over `max_early_days` is rejected.
10. Compatibility: different provinces never share a truck under `same_province`; overrides allow and deny work.
11. Past due: reason `past_due`, not planned.
12. Exclusion reasons: each data-quality code is produced for a matching row.
13. Penalty sizing: `P` exceeds any possible cost saving; a shipment is never left out when feasible.
14. Tie-break: with equal cost, the plan with fewer early days wins; tie-breaks never change a plan whose cost differs by 1 THB or more.
15. Fleet limit: with `max_trucks` set, the limit is respected and a leftover gets `fleet_limit`.
16. Solo guarantee: model cost is never above the baseline on a random small instance.
17. Reconciliation: planned plus excluded equals input totals in cases, kg, m3.

### 12.2 Real-data acceptance (August 2025)
Run the full pipeline on `trip_planning/outputs/va05_aug2025/` and assert:

- Input totals reconcile: 117,745 CSE, 702,897 kg, 1,841.29 m3. Planned plus excluded equals these.
- 341 shipments built from 1,436 orders.
- `total_cost <= 1,100,355` THB (the cheaper-of-two baseline). Report the actual saving; do not assert a target.
- Every order is in exactly one trip or one exclusion with one reason.
- No trip ships after any member's RDD; no member arrives more than `max_early_days` early.
- Every trip fits its truck on kg and m3.
- Every trip uses one truck type for all drops and each drop's ship-to offers that type.
- Solver gap is 0 for every component, or the report lists the component and its gap.
- Run time is reported. Do not run a 600 s solve for a docstring change; see `AGENTS.md`.

## 13. Open items for the business

1. LTL formula: confirm cheaper-of-two against carrier invoices (135448 on 2025-08-16, 616512 on 2025-08-20).
2. Handling: per drop or per truck. Today only 15 ship-tos on 4W carry it.
3. Multi-drop price rule: max trip rate, or a separate multidrop table for the 35 Foodland and Donki ship-tos.
4. Consolidation rule: same province, same group, or something else. Real lane data may show cross-province routes.
5. Early delivery: is early arrival allowed, how many days, and any cost.
6. Transit days for long lanes.
7. Capacities: 6W 25 m3 and 18W (blank).
8. The 57 orders with RDD tighter than SLA: plan as given (default) or exclude.
9. The 8 ship-tos with no rate (about 11 percent of volume): rate source needed.
10. The 37 orders with ship-tos missing from Master Customer.
11. Fleet limit: does a real per-day truck cap exist.
12. Is a remainder of a split shipment allowed to consolidate (currently no).

## 14. Build order for the agent

1. Extend `baseline_cost.py` (section 9) and tests; confirm 1,100,355.
2. `shipments.py` and tests; confirm reconciliation.
3. `pricing.py` and tests 1 to 7.
4. `candidates.py` and tests 8 to 10.
5. `trip_model.py` and tests 11 to 16.
6. `plan_trips.py`, `reporting.py`, real-data acceptance (12.2).
7. `py_compile` every changed file, run all tests, spawn `code-checker`.
8. Stop and report. Do not write `DECISIONS.md` blocks until the user confirms results.