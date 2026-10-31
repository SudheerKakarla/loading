# Trip Planning: Implemented End-to-End Specification

Implementation snapshot: **2026-10-31**. Audience: maintainers and reviewers of the existing module. This document describes the current code, not a future build backlog. Dataset counts and costs labelled August are reference results, not constants in the planner. The optional retrospective contracts below are software capabilities, not invented or approved customer policies; absent optional data preserves the August default behavior.
Status labels for the four rules in section 5.1: **[CONFIRMED DEFAULT]** is the rule this module runs and the unit tests lock. **[PROVISIONAL PLACEHOLDER]** is implemented so the model and the synthetic tests run, and is not a settled billing or lane rule. Every other **[ASSUMPTION]** is still provisional. Software validation is not a carrier contract. Configurable settings expose the implemented choices; a different commercial formula is not implemented.

---

## 1. Purpose and scope

Given shipment demand (orders for ship-to customers, each with a Required Delivery Date, RDD), choose solo or shared bookings and departure dates within the implemented capacity, compatibility and arrival rules. With a fleet cap, first minimize the number of unplanned shipments; then minimize freight cost plus bounded early-delivery/LTL-upgrade preferences. Without a cap, cover all eligible shipments having a calendar-open legal date; otherwise report past-due or calendar-unavailable demand. Optimality is over generated candidates and the implemented pricing assumptions, not all possible routes or commercial contracts; a timeout may return an explicitly nonoptimal plan.

In scope:

- Read and validate the VA05 workbook into contract tables (`va05_adapter.py`, `build_inputs.py`).
- Compute solo-fleet, LTL-case, cheaper-of-two and workbook-rule baselines (`baseline_cost.py`).
- Group orders into shipments, generate candidate trips, solve a set-partitioning MIP with HiGHS, report the plan.
- Enrich plan exclusions and summarize missing-rate customers; run named, explicitly unapproved what-if scenarios with coverage-safe comparisons and atomic publication.

Out of scope: route sequencing inside a trip, pallet or floor layout (that is `loadmax/`), picking (that is `order_picking/`), carrier tendering, live data connections.

### 1.1 Current delivery status

| Area | Implemented behavior | Still required |
|------|----------------------|----------------|
| Core planning | Whole effective-RDD shipments, permitted solo/shared pricing, capacity/date checks, optional daily cap, validated MIP/fallback and reconciliation | Approval of commercial formulas and resource accounting; no route or physical-fit guarantee |
| Business restrictions | Customer pricing/sharing permissions, pair/solo-only rules, dated and weekly calendars, auditable approved-RDD inputs | Actual approved customer rules; optional files are not populated from ambiguous workbook notes |
| Rate-gap reporting | Enriched plan exclusions and customer-level `no_rate` rollup | Supply the missing rates; reporting alone does not recover excluded demand |
| Scenario analysis | Independent named config/capacity overlays, coverage-safe comparisons, hashes and protected publication | Execute and interpret the reference August batch; supply any hypothetical/approved capacity alternatives explicitly |

The latest change adds controls and analysis, not a new objective or a new approved tariff. Default August cost/count regressions remain unchanged. The five-scenario reference manifest is shipped and tested structurally; the complete five-scenario August batch has **not** been run as part of this update. See section 13 for business decisions versus unimplemented extensions.

Relationship to the repo: standalone module under `trip_planning/`. It does not import from `loadmax/future_addons/`. It reuses the pattern of `order_picking/chunker/chunk_model.py` (enumerate candidates, then set-partitioning MIP via `scipy.optimize.milp`).

## 2. Glossary

| Term | Meaning |
|------|---------|
| Order | One sales document on one RDD (from `orders.csv`). |
| Shipment | All orders for one ship-to with the same RDD. The unit that is planned. |
| Booking / report trip | One selected candidate on one ship date. A shared booking uses one truck; a solo booking may be a multi-truck fleet or carrier LTL billed per case. The legacy output name is `trip`. |
| RDD | Required Delivery Date. Hard deadline. |
| CSE | Case (carton). `Total CTN Convert` in the workbook. |
| FTL | Full truck load service. Priced per truck trip. |
| LTL | Less than truck load service. Priced per case, or per truck if cheaper (see 6.3). |
| 4W / 6W / 10W / 18W | Truck types by wheel count. |
| MT / GT | Modern trade / general trade. Selects the LTL case rate column. |

## 3. Data contracts

The planner reads four UTF-8, header-row CSVs, optionally with pair overrides, dated/weekly calendars and approved delivery-date overrides, from `--inputs`. They can be produced by `build_inputs.py` or supplied directly. The workbook builder defaults to `trip_planning/outputs/va05_aug2025/`. Ids are normalized by the shared `norm_id`: trim surrounding whitespace and remove numeric spreadsheet `.0` suffixes. Counts below describe the August workbook. Optional files are discovered automatically by exact filename; no approved operational policies have been supplied or added to the existing sample/August inputs.

### 3.1 `orders.csv` (1,436 rows)
One row per order. Required columns: `order_id`, `ship_to`, `doc_date`, `rdd`, `cases`, `weight_kg`, `volume_m3`. Optional: `po_numbers` (the PO Number(s) on the order's lines, `;`-separated; passed through to the reports only, never used in planning). Check the actual header in the file with `read_csv` and map by name. Dates are ISO `YYYY-MM-DD`. Quantity is `Total CTN Convert`.

### 3.2 `customers.csv` (233 rows)
One row per ship-to; required columns: `ship_to`, `province`, `group` (customer group), `service`, `sla_days`, `name`. Optional conflict provenance: `service_master`, `service_contract`, `service_conflict`; the workbook adapter also emits `in_master` and other descriptive columns. The adapter uses the nonblank rate-card contract service ahead of Master Customer service and preserves disagreement. **Direct CSV planning instead uses nonblank `customers.service` first, then `rates.service`; unrecognized/missing service resolves to FTL.** An effective customer/rate conflict is flagged. The "FTL" label inside LoadMax order numbers is a route label and is never used as service.

Optional permission columns: `allow_truck_pricing`, `allow_case_pricing`, `allow_sharing`. Missing columns or empty cells mean **true / legacy behavior**. Accept native booleans or trimmed case-insensitive strings `true`, `false`, `1`, `0`; reject other strings and numeric values in the in-memory API (numeric strings in CSV are supported). These only restrict existing contract eligibility: FTL remains truck-only even with `allow_case_pricing=true`; no flag supplies a missing price. LTL can use either permitted, actually priced route. A customer denying sharing cannot share between shipments, even with its own other-RDD shipment or an explicit pair allow; orders grouped into one shipment are not separate sharing members.

### 3.3 `rates.csv` (197 rows)
One row per ship-to; `ship_to` is required. Recognized optional columns are trip rates `trip_4w`, `trip_6w`, `trip_10w`, `trip_18w`; handling `handling_4w`, `handling_6w`, `handling_10w`, `handling_18w`; case rates `ltl_mt`, `ltl_gt`; `service`; and `multidrop`. Missing price columns behave as blank. Blank or zero trip rates mean not offered; blank handling means zero. MT takes precedence if both case rates exist. The workbook adapter converts zero case rates to blank; direct CSV zero case rates remain valid zero prices. `multidrop` is warning metadata, not a different pricing formula (35 August Foodland/Donki ship-tos have the remark).

### 3.4 `trucks.csv`
`truck_type`, `capacity_kg`, `capacity_m3`. August input values: 4W 1500 kg / 4 m3; 6W 5000 / 25; 10W 15000 / 35; 18W blank. **[ASSUMPTION]** 6W 25 m3 looks suspicious relative to the other types; retain and flag it, not automatically correct it. August 18W is unusable because its capacity is missing. This is not a hardcoded ban: direct inputs or explicit scenario overrides can make 18W usable when both capacities are positive and finite and the destination has an offered rate. No replacement capacities are supplied by the code.

### 3.5 `shipments.csv` (written by `shipments.py`, 329 eligible August rows)
`shipment_id`, `ship_to`, `rdd` (effective approved RDD), `cases`, `weight_kg`, `volume_m3`, `release_date` (max `doc_date` of its orders), `service`, `province`, `group`, `order_ids` (semicolon separated), plus the three resolved boolean permission columns. Grouping uses effective dates; original per-order dates are retained separately in reports.

The often-quoted **341** is positive-quantity raw demand grouped by ship-to/RDD, before rate eligibility: 12 groups have no rate, leaving **329** sent to planning. Zero-quantity orders are separately excluded; 341 is not the eligible shipment count. All 1,436 orders still reconcile through trips or exclusions.

### 3.6 `baseline_shipments.csv`, `baseline_summary.csv`
Outputs of `baseline_cost.py`; see section 9 for their current fields and accounting.

Second comparison point, the workbook's own truck rule (`Pivot_Summary` column W, `EST Truck Usage`), priced shipment by shipment with no sharing: sort usable truck types by capacity kg, then m³, and choose the first single type that fits both (August: 4W, then 6W, then 10W); otherwise `6W x ceil(kg / 6W kg capacity)`, which, like the workbook formula, ignores m³. Truck choice is capacity-first, not price/offer-first; if that type has no rate, the comparison may be unpriced or use an available LTL case rate. Priced with the same rate card; LTL pays cheaper of case and truck, a tie staying truck-priced. Detail fields are `sheet_truck`, `sheet_cost`, `sheet_basis`; summary fields include `sheet_rule_cost`, `sheet_rule_trucks`, `sheet_rule_<type>`, `sheet_rule_util_kg/m3`, `sheet_rule_unpriced_shipments` (counts/utilization cover truck-priced shipments only). Plan `sheet_rule_cost` always contains the computed total; only `saving_vs_sheet_rule_thb/percent` are blank unless every built shipment is planned and sheet-priced. August: 1,117,275 THB, 171 trucks, 50% kg / 42% m³. This is a formula estimate, not booked trucks.

### 3.7 Optional inputs
- `consolidation_overrides.csv`: `ship_to_a`, `ship_to_b`, `rule` (`allow` or `deny`). Symmetric.
- `planning_calendar.csv`: required columns `calendar_type,date,is_open,ship_to`. `calendar_type` is `dispatch` or `receiving` (trimmed/case-insensitive). Dispatch rows require blank `ship_to`; receiving rows allow blank for **global receiving** or a known normalized id in the **whole customer table**, even if that customer has no eligible demand. This extends the old dated contract: blank receiving scope is now global, not invalid. Date strings must be ISO `YYYY-MM-DD` (not compact dates, datetimes or impossible dates); native `date` objects are accepted in memory. `is_open` uses the strict boolean syntax above, **but blank is an error**. Every repeated normalized `(calendar_type, ship_to, date)` key fails, even identical duplicate entries.
- `weekly_calendar.csv`: required columns `calendar_type,ship_to,weekday,is_open,start_date,end_date`. Same dispatch/receiving scope and strict nonblank boolean rules. Weekday is a full name **Monday through Sunday**, trimmed/case-insensitive, not a number or abbreviation. Both bounds are mandatory strict ISO dates (native `date` in memory), inclusive, with `start_date <= end_date`. Reject overlapping ranges for the same normalized `(calendar_type, ship_to, weekday)`, **even with the same boolean**; different scopes may overlap. Header-only calendar files with valid schemas are no-ops; missing schemas fail.

- `delivery_date_overrides.csv`: required columns `order_id,approved_rdd,reason`. Unique known normalized order ids are validated against **all original orders**, including zero-quantity, no-rate and other excluded rows. Blank/unknown/duplicate ids fail. `approved_rdd` has the same strict ISO/native-date contract; `reason` must be nonblank after trimming. A header-only file is a valid no-op. Apply approvals to a deep copy **before ship-to/RDD grouping** (so shipments may split or merge), baselines and SLA warnings. Original identity, quantity, release date and raw dates are not edited. Raw dates must still validate; an approval cannot conceal a malformed original RDD. Without an explicit approval, no RDD is relaxed. An approval earlier than release yields the normal `past_due` outcome, not automatic late/earlier shipping. The caller supplies evidence of approval via this explicit file; the software does not authenticate a signatory.
- YAML/JSON configuration via `--config`, with explicitly supplied CLI flags taking precedence. All section 5 parameters are config fields; not all have dedicated CLI flags (section 11).

Calendar precedence for receiving on a date is **dated specific → dated global → weekly specific → weekly global → open**; dispatch uses dated global → weekly global → open. Explicit opens are preserved, so dated opens can reopen weekly closures. Dispatch filters departure; receiving filters **each member's arrival = departure + its province transit days**, including case-priced solos. No Sunday/GI policy is inferred, and transit remains elapsed calendar days, not business days. Calendars only filter existing release/transit/early bounds.

`InputTables` has trailing optional `planning_calendar`, `delivery_date_overrides` and `weekly_calendar` fields defaulting to `None`. The in-memory API is backward-compatible: `plan(orders, customers, rates, trucks, cfg=None, overrides=None, *, planning_calendar=None, delivery_date_overrides=None, weekly_calendar=None)`. `build_shipments` accepts keyword-only `delivery_date_overrides`; direct candidate callers may pass a validated immutable `PlanningCalendar` using `planning_calendar=`. No global mutable calendar or permission state is used. Original/effective date audit remains in existing report tables.

### 3.8 Validation and failure behavior
- Workbook demand is validated **line by line before aggregation**. Missing/malformed/nonfinite/boolean/negative cases, kg or m³ fail the build with context; they are not silently replaced by zero. Missing document, Ship-to, Sold-to or dates and conflicting Ship-to/Sold-to within document/RDD also fail. An order's document date is the latest of its lines. Formula caches must be present: the adapter reads cached Excel values, not formulas it recalculates.
- Direct CSV/in-memory planning validates required columns, order ids, dates and finite nonnegative quantities before exclusions/grouping. Blank order ids and duplicate normalized order ids fail. Missing ship-to becomes `no_master_customer`, not a silent dropped row. Duplicate normalized customer/rate/truck keys fail. Blank rate/truck keys are skipped by their parsers; blank customer keys fail.
- Negative/malformed/nonfinite monetary values fail; booleans are not numbers. Optional missing prices are permitted. Both LTL rate columns are validated even when MT takes precedence. Truck capacities must both be finite and positive to be usable; absent/nonpositive capacities make a type unusable and flagged, while malformed numeric cells fail parsing.
- Fatal validation errors stop the run; ordinary eligibility exclusions preserve quantities in the exclusion report. These are different outcomes. Workbook builds serialize and validate all nine build artifacts before publication (section 10).

## 4. Data facts the code must respect (August 2025 workbook)

- 1,406 documents became 1,436 orders (a document spanning several RDDs splits into orders).
- Totals that must reconcile after every load: 117,745 CSE, 702,897 kg, 1,841.29 m3.
- 48 orders have zero quantity. 57 orders have an RDD tighter than the customer SLA. 37 orders have ship-tos missing from Master Customer. 1 ship-to has a service conflict between sheets. 8 ship-tos have no rate (12 shipment groups, 12,940 CSE: 10.99% of input cases, 13.11% of m³).
- Lead time (RDD minus doc date) ranges 1 to 38 days; none is zero or negative.
- 341 positive-quantity raw shipment groups over 33 provinces (329 eligible after rates). Only 2 raw groups exceed one 10W; 48 need more than one truck.
- Trip rates are monotone across truck types (4W <= 6W <= 10W) for each ship-to.
- 4W handling is filled only for the 15 ship-tos with unloading remarks.
- Multidrop ship-tos are all LTL; their 4W trip rate is flat across distances.

## 5. Parameters (all configurable, defaults shown)

| Parameter | Default | Meaning |
|-----------|---------|---------|
| `max_shipments_per_trip` | 3 | Candidate size cap. Try 4 only after profiling. |
| `max_early_days` | 1 | **[ASSUMPTION]** Max days any shipment in a trip may arrive before its RDD. |
| `transit_days` | 0 | **[PROVISIONAL PLACEHOLDER]** Elapsed calendar days from ship date to delivery. See 5.1. Distance is not converted into transit time. |
| `transit_days_by_province` | empty mapping | Optional nonnegative integer days by province. A missing province uses `transit_days`. The default map is empty, so every province uses 0. |
| `compat_mode` | `same_province` | **[CONFIRMED DEFAULT]** Bootstrap for who may share a truck: `same_province`, `same_group`, `same_province_or_group`, `any`, `none`. Mutual and symmetric. See 5.1. |
| `max_drops_per_trip` | = `max_shipments_per_trip` | Cap on distinct ship-tos per trip. |
| `solo_only` | empty | List of ship-tos that never consolidate. |
| `handling_basis` | `per_drop` | **[ASSUMPTION]** `per_drop` or `per_truck`. See 6.2. |
| `max_trucks` | none | Optional total counted trucks per ship date, not per type/carrier. Nonnegative integer; zero is supported. Off by default. |
| `mip_rel_gap` | 0 | Must stay 0 (or <= 1e-9). The repo default 0.01 breaks the tie-breaks. |
| `time_limit_s` | 600 | Per component. |
| `early_delivery_weight` | derived | See 7.3. |
| `upgrade_weight` | derived | See 7.3. |

### 5.1 Four running defaults

Recorded 2026-10-31 from `source/pricing.py`, `source/candidates.py`, `source/config.py`, `source/baseline_cost.py`, and `tests/`. This is the behavior the folder builds and the unit tests lock. It is not an invoice audit and not a lane study.

| # | Rule | Status | Why this status |
|---|------|--------|-----------------|
| 1 | LTL is the cheaper of per-case billing and truck billing | **[CONFIRMED DEFAULT]** | This is the usual FAK / volume-break comparison, and it is the rule the pricer and the baseline both run. |
| 2 | A shared truck pays the max of the solo trip rates, plus per-drop handling | **[PROVISIONAL PLACEHOLDER]** | Conservative stand-in. A real multi-stop tariff is often a flat stop fee, a surcharge on the furthest zone, or a separate rate card. |
| 3 | Ship-tos share a truck only inside one province, unless an override says otherwise | **[CONFIRMED DEFAULT]** | Bootstrap so candidate generation can run before a lane table exists. Province is a crude proxy, not the long-term rule. |
| 4 | Transit is 0 days on every lane | **[PROVISIONAL PLACEHOLDER]** | Right for the synthetic tests. Highest-priority override before a plan on real orders is trusted, because it decides which RDD windows can share a truck. |

**1. LTL cheaper-of-two. Confirmed default.** `solo_quote` prices an FTL shipment as its cheapest feasible fleet (`trip rate + handling` on each truck). An LTL shipment pays `case rate × cases` only when that total is strictly below the fleet cost (tolerance `1e-9`). A tie stays on the truck, so an equal case price does not hide the fleet. MT (`ltl_mt`) wins when both case columns are filled; otherwise GT (`ltl_gt`). A missing case rate leaves the truck branch. A missing fleet with a case rate stays on the case branch.

The same comparison prices a shared truck in `trip_cost`. If any member is FTL, the trip is the truck price and the LTL case bill is not added; an LTL rider still enters the max trip rate and the handling. If every member is LTL, the cost is the minimum of the summed case bills and the truck price, and a tie stays `price_basis=truck`. The case branch does not add handling, because handling sits inside the truck price. One member with no case rate forces the truck branch. A shared booking still has to fit one offered truck even when the winning branch is the case price. Tests: `test_ltl_cheaper_of_two`, `test_ftl_rider_adds_nothing_for_the_ltl_cases`, `test_baseline_columns_and_three_totals`.

**2. Multi-drop max trip rate, per-drop handling. Provisional placeholder.** For one truck type, `trip_cost` charges `max(trip rate)` over the distinct ship-tos, plus handling. Default `handling_basis=per_drop` sums those ship-tos' handling amounts. `per_truck` uses the max handling instead. Blank handling is 0. Two shipments for the same ship-to are one drop. The `rates.multidrop` column does not select another formula. A true value only writes the flag `multidrop_priced_as_max_trip`. There is no Foodland/Donki rate card in this folder. The max-rate rule keeps a shared trip from costing less than the dearest solo trip rate on that truck, before the cheaper-of-two case comparison and before the "must beat the sum of solos" filter. That is a modeling convenience. Test: `test_multi_drop_is_max_trip_plus_handling`.

**3. Same province. Confirmed bootstrap default.** `compat_mode` defaults to `same_province`. Two different ship-tos match when both provinces are non-blank and equal after the trim applied at load (comparison is case-sensitive). A blank province matches nothing. `same_group`, `same_province_or_group`, `any`, and `none` are implemented switches, not the default. The check is pairwise. `solo_only` blocks sharing, including a ship-to with itself. An `allow` override beats the province rule; a `deny` override blocks a same-province pair. Two shipments of the same ship-to may still share under `none`, unless that ship-to is solo-only. This default will refuse a short cross-province pair and allow a long pair inside one province. It stands until a lane or distance table replaces it. Test: `test_compatibility_province_and_overrides`.

**4. Transit days = 0. Provisional placeholder, and the first real-data override.** `TripConfig.transit_days` is 0. `transit_days_by_province` is empty, so `transit_for` returns 0 for every province. `--transit-days` and a config map can override it; nothing in `inputs/` does.

`feasible_ship_date` tries one ship date: the earliest of `RDD − transit_for(province)`. That date must be on or after every member's release date, each arrival `ship date + transit` must fall on or before that member's RDD, and the raw RDD spread (`max RDD − min RDD`) must be at most `max_early_days` (default 1). The spread test does not use transit. With different transit days, a member can arrive more than `max_early_days` early and still be accepted; those days are only a tie-break (`early_days`). The consolidation search also drops pairs whose raw RDDs differ by more than `max_early_days`. At transit 0 the ship date is the earliest RDD, arrival is that same day, and two shipments share only when their RDDs differ by at most one day. The synthetic date test assumes this default and never sets a nonzero transit (`test_rdd_spread_and_release_reject_a_ship_date`). Replace the zero with lane or province transit before trusting which real orders can consolidate.

Customer permission columns, dispatch/receiving calendars, and a per-date fleet-count frontier are described later in this file. They are not read by `trip_planning/source` and they are not covered by `trip_planning/tests`.

## 6. Business rules and cost model

### 6.1 Unit and capacity
- Plan shipments, never split a shipment across different dates or trips, except through the solo fleet (6.4).
- Capacity is two dimensional: a shared load fits one truck if `sum kg <= capacity_kg` AND `sum m3 <= capacity_m3`. A solo fleet uses summed capacities and assumes divisible load, without constructing a physical per-truck packing. Reports expose `util_kg` and `util_m3` separately; summary utilization is total counted-truck load divided by total counted-truck capacity, not an average of row percentages. Case-priced solos have blank row utilization and are excluded from these ratios.
- A truck type is offered to a ship-to only if its trip rate is non-blank and its capacity is known.

### 6.2 Single ship-to, truck k
`T_k(j) = trip_k(j) + handling_k(j)` where blank handling counts 0. A blank trip rate means k is not offered.

### 6.3 Multi-ship-to trip on one truck k with drop set J
- Every ship-to in J must offer truck k, and the combined kg and m3 must fit k.
- Truck cost: `trip_cost = max_{j in J} trip_k(j) + handling`, where handling is `sum_j handling_k(j)` under `per_drop`, or `max_j handling_k(j)` under `per_truck`. **[PROVISIONAL PLACEHOLDER]** The max of the solo trip rates is the multi-drop price this module runs. It is not a confirmed carrier rule. See 5.1.
- LTL pricing is the cheaper of two. **[CONFIRMED DEFAULT]** See 5.1. A tie within `1e-9` stays on the truck. Workbook examples that illustrate the gap, not a second formula: ship-to 135448 on 2025-08-16 (case 12,557 vs 10W 5,722) and 616512 on 2025-08-20.
  - If any shipment is FTL, cost = `trip_cost` with no separate LTL case bill. LTL destinations still participate in maximum trip rate and handling; a rider is not necessarily zero incremental cost.
  - If every shipment is LTL, cost = `min(sum_s case_rate(s) * cases(s), trip_cost)`. Case rate is MT if present, otherwise GT. Under the case-priced branch no handling is added.
- Report which branch priced each trip: `price_basis` in {`truck`, `case`}.
- Permissions apply to **every member** of a branch. Truck pricing requires all members' truck permission, including LTL riders on an FTL trip. Case pricing requires every member to be LTL, case-permitted and to have an actual case rate. If one LTL member lacks a case rate, the all-LTL trip may still use the truck branch if all permit it. A truck-only member blocks the case branch even when cheaper; a case-only member blocks the truck branch. Shared trips still require an offered type and capacity for every destination even if billed by case. Sharing denial precedes all pair allows. When both branches are legal, the existing cheaper-of-two rule and truck-priced tie remain unchanged.

### 6.4 Solo candidates and fleet-resource alternatives
Without a fleet cap, generate one latest-calendar-open-date solo priced with `cheapest_fleet` (possibly several trucks): FTL = permitted fleet cost; LTL = cheaper of permitted case and fleet. The solo partition is feasible for every shipment with an open legal date. The cost objective also contains a bounded whole-baht preference, so no plan a full baht dearer than this feasible baseline may win; sub-baht cost differences can be outweighed (section 7.3).

With a fleet cap, retain the nondominated **cost / truck-count** frontier, not just the cheapest fleet. Price each truck budget up to the cheaper of the daily cap and the cheapest permitted fleet's count, including mixed-type fleets. Retain permitted LTL case pricing (0 counted trucks) even when a truck is cheaper; never reintroduce a forbidden branch through frontier generation. Expand each alternative over all calendar-open legal departure dates. The unconstrained solo baseline is a comparator, **not an upper bound** in constrained mode. A costlier fleet or consolidation can be necessary to serve more shipments. A remainder of a solo fleet cannot consolidate.

### 6.5 Consolidated trips
- 2 to `max_shipments_per_trip` shipments, exactly one truck, from the compatibility rule.
- Compatibility is pairwise across every member pair. `solo_only` takes precedence over overrides; deny/allow overrides apply to distinct ship-tos before the configured mode. Different shipments for the same ship-to may share even under `compat_mode=none`, unless solo-only. Province/group matches require a nonblank equal value (trimmed, case-sensitive). Drop count is distinct ship-tos, not shipment count. A contradictory allow/deny pair fails validation.
- One ship date is tried: the earliest `RDD − transit_for(province)`. It must be on or after every release date, each arrival must be on or before that member's RDD, and the raw RDD spread must be at most `max_early_days`. The spread test ignores transit. At the provisional default of 0 days, that date is the earliest RDD. See 5.1. The code does not intersect per-member early windows, and it does not enumerate other dates when `max_trucks` is set.
- Early days = `sum_s ((rdd_s - transit_for(province_s)) - ship date)`. A member who arrives more than `max_early_days` early is still legal when transit differs across the set; the extra days are a tie-break only.
- Dispatch and receiving calendars are not applied. `trip_planning/source` has no calendar reader.
- A consolidation is generated only when its cost is strictly below the sum of the solo costs (`1e-6` tolerance). The cheapest feasible single truck type is kept. This pruning stays on when `max_trucks` is set; the cap is a constraint in the MIP, not a second candidate generator. Case-priced consolidated bookings still count as one truck; case-priced solos count as zero.

### 6.6 Deadlines
RDD is hard. No shipment arrives after its RDD. A built shipment with `rdd - transit_for(province) < release_date` gets no candidate and is excluded as `past_due`. This is relative to release/RDD, **not today's date**: there is no rolling planning horizon or frozen-booking input. The 57 August orders with RDD tighter than SLA are planned as given and flagged `rdd_tighter_than_sla`. SLA is warning-only. **[ASSUMPTION]**

### 6.7 Exclusions and reason codes
Two separate lists in the output.

Data-quality exclusions (never sent to the MIP): `no_master_customer`, `no_rate`, `zero_quantity`, `truck_type_not_offered` (no usable truck/case route without a relevant restriction), `service_not_permitted` (an explicit pricing restriction leaves no actually priced permitted route). Existing master/zero/no-rate precedence is preserved; service restrictions are checked next, before the legacy no-offered-type check. FTL denied truck pricing is not reclassified to LTL. Keep original quantities so totals reconcile: planned + excluded = input totals.

Business leftovers (from the MIP): `past_due`, `calendar_unavailable`, `fleet_limit`. An empty release/transit/early date window is `past_due` as before; a nonempty window whose dates are all blocked by calendars is `calendar_unavailable`, with or without a cap (including cap zero). Remaining open-date leftovers under a cap use `fleet_limit`, including a nonoptimal timeout/fallback; it is not proof that the shipment cannot be served in a better plan. There is no `oversized` code, because solo fleets split. Calendar-unavailable demand stays in dateable baseline coverage and suppresses savings rather than disappearing from the comparison.

Every input order ends in exactly one place: in a trip, or in an exclusion list with one reason code.

## 7. Optimization model

### 7.1 Sets and data
- S: plannable shipments. C: candidates (solo and consolidated). Each candidate c has `members(c) subset S`, `cost(c)`, `date(c)`, `early(c)`, `upgrade(c)` (number of LTL shipments in a trip of 2 or more shipments that is truck-priced).
- Past-due and calendar-unavailable shipments have no candidate. Without a fleet cap every built shipment with an open legal date has a solo candidate. Under a cap an open-date FTL shipment may have no fleet alternative within the cap and remain unplanned.

### 7.2 Variables
`x_c` binary, 1 if candidate c is used. `u_s` binary, 1 if shipment s is left unplanned: allowed for no-candidate past-due/calendar-unavailable shipments, and for every shipment when a fleet cap is active. Count shipments, not orders, cases, tonnes, or revenue. The objective, cover, fleet constraints and fallback algorithm are unchanged by this retrospective phase; restrictions operate in ingestion/candidate generation.

### 7.3 Objective
minimize `sum_c (cost(c) + e * early(c) + g * upgrade(c)) * x_c + P * sum_s u_s`

- Without fleet caps, `P = sum solo(s) + 1`. With caps, `P = sum_s max_{c containing s} cost(c) + 1` (zero for members with no candidate). This bounds the cost of any partition, including costlier resource-saving alternatives. Together with total tie contribution below 1 THB, one fewer leftover shipment always wins. Then minimize cost with bounded early/upgrade preferences. This is **shipment-count-first**, not quantity-first; changing that priority requires business approval.
- Default weights are computed **once for all built shipments**, before decomposition, and reused in every component: `e = g = 0.5 / (1 + n * (max_early_days + 1))`. Here `n` includes past-due built shipments. This bounds total default tie contribution below 0.5 THB and hence below the documented 1 THB preference resolution.
- Report `total_cost` (sum of `cost(c)` only), `objective_value`, `tie_break_total`, `penalty_total` separately. The bounded early/upgrade preferences encode the documented business rules, rather than relying on arbitrary solver order.

**Monetary resolution retained:** exact fractional-baht costs are used and reported, without rounding prices or converting the preference resolution to cents. A difference of at least 1 THB always beats all tie preferences; a smaller difference may not. Custom weights must be finite/nonnegative and satisfy `n * (max_early_days * early_weight + upgrade_weight) < 1`. This data-sized bound is checked before solving. Integer configuration fields reject booleans, fractional values and numeric strings; province transit overrides require nonnegative integers. These are elapsed calendar days, not business calendars.

### 7.4 Constraints
- Cover: for each shipment s, `sum_{c : s in members(c)} x_c + u_s = 1`.
- Optional fleet limit: for each ship date d, `sum_{c : date(c)=d} trucks(c) * x_c <= max_trucks`. Only when `max_trucks` is supplied; this couples components.

### 7.5 Solving
- `scipy.optimize.milp` with `options={"mip_rel_gap": 0, "time_limit": ...}`.
- Decomposition: build a graph with an edge between two shipments if they appear in a common candidate. Solve each connected component separately (typically province by date window). Skip decomposition when `max_trucks` is supplied.
- If a component hits the time limit, keep the incumbent, and record `status` and `gap` per component in the report.
- Validate finite, integral, bounded decision values, exact cover and daily fleet use before accepting any incumbent. No usable incumbent: without caps use known feasible solos; with caps start from all leftovers and greedily add nonoverlapping feasible bookings. Mark these `fallback_<solver status>`, with unknown/infinite gap, never optimal. Greedy fallback guarantees feasibility, not optimal coverage. A configured tiny time limit can prevent proof despite the requested zero gap.
- Fleet pricing enumerates integer mixes when the product of count bounds is at most 2,000,000; otherwise a separate fleet MILP uses a **fixed 30 s limit and zero requested gap**, independent of `time_limit_s`. It validates rounded counts and covering capacity. Time-limited fleets carry `solver_status_1` and a gap; no usable incumbent falls back to a covering pure-type fleet where available. If no such fallback exists and infeasibility is not proven, pricing raises rather than guessing. Nonoptimal baseline/selected-candidate pricing is flagged and persisted; an optimal trip solve is optimal only over the supplied candidate prices.
- Generated August default candidates: 1,512 for 329 eligible shipments, including solos. Candidate enumeration can grow combinatorially with the size cap, permissive compatibility, and wider date windows; no general scalability guarantee is claimed.

## 8. Module layout

Implemented files under `trip_planning/source/`:

| File | Responsibility |
|------|----------------|
| `va05_adapter.py` | Cached workbook values to validated orders, customers, rates and trucks; diagnostic counts. |
| `baseline_cost.py` | Rate/truck parsing, divisible-load fleet enumeration/MILP and workbook-rule comparison. |
| `build_inputs.py` | Workbook CLI; stages and publishes nine validated build CSVs. |
| `config.py` | `TripConfig`, id normalization, tie weights and YAML/JSON parsing. |
| `retrospective.py` | Strict optional booleans/ISO dates, immutable dated/weekly dispatch/receiving rules with precedence, and copy-on-apply approved RDDs with audit. |
| `shipments.py` | `build_shipments(...) -> ShipmentBuild` (shipment table, exclusions, order-to-shipment lines, input totals and order ids); flags and CSV reader. |
| `pricing.py` | `solo_quote`, `solo_cost`, `trip_cost`; pure pricing functions taking rate/truck/config data. |
| `candidates.py` | `generate_candidates(...) -> CandidateSet` (candidates, built shipments, past-due/calendar-unavailable ids and immutable calendar); compatibility, dates, resource alternatives. |
| `trip_model.py` | `solve_trip_model(...) -> SolveResult`; partition MIP, incumbent checks, feasible fallback and component statuses. |
| `plan_trips.py` | `plan(...) -> PlanResult` in-memory API and `argparse` CLI orchestration. |
| `scenarios.py` | JSON manifest validation, independent config/capacity overlays, coverage-safe comparisons and atomic multi-scenario publication. |
| `reporting.py` | Report assembly, hard checks, summaries and validated plan publication. |
| `provenance.py` | Detached metadata snapshot, table/file/source hashes and environment versions. |
| `publication.py` | Staged directory rename or file replacement with rollback. |

Tests under `trip_planning/tests/` (pytest config exists: `trip_planning/pytest.ini`).

Conventions (from `AGENTS.md`): `pathlib.Path`, `argparse`, type annotations on new public functions, small functions, run `python -m py_compile` on each modified file, spawn the `code-checker` agent, do not add `ruff` or `mypy` as requirements. Environment: Windows PowerShell, `.venv` Python 3.12 with `openpyxl`, `pandas`, `scipy`, `numpy`, `pytest`. Do not edit `DECISIONS.md` until the user confirms a finding.

## 9. Implemented baselines (`baseline_cost.py`)

Baselines price **all built eligible shipments using effective approved RDD groups and permitted pricing**, with no sharing, no date scheduling and no daily fleet cap. Fleet loads are divisible; trip plus handling is charged per truck. Enumerated equal-cost mixes prefer fewer trucks, then more of smaller-ranked types. FTL pays a permitted fleet; LTL pays cheaper of permitted fleet and case, with a price tie staying truck-priced. The sheet comparison applies the same permissions, rather than comparing a restricted plan with a forbidden cheaper case/vehicle price. A case-only LTL comparison can use the case price without a known vehicle capacity (blank sheet truck, zero modeled vehicles).

`all_trips_*` now includes only permitted, priceable fleets; `all_trips_unpriced_shipments` makes partial coverage explicit when truck pricing is forbidden or unavailable. It is not a full-demand all-truck scenario in that case. `ltl_case_mix_cost` uses permitted cases, otherwise a permitted fleet. `cheaper_cost`/plan solo baseline never use forbidden prices. The metadata field `baseline_comparison` labels this scope; summary metrics stay numeric for workbook-builder compatibility. No unrestricted forbidden-price scenario is silently used for savings.

- Detail includes shipment identity/load/service; `case_cost`, `fleet_cost`, `baseline_cost`, `basis`; `fleet_status`, `fleet_gap`; `n_trucks`, `truck_mix`, `n_4W/6W/10W/18W`; and `sheet_truck`, `sheet_cost`, `sheet_basis`. Fleet count columns describe the computed fleet even if the cheaper billing branch is case; they must not be summed as actual chosen vehicle counts for case-priced solos.
- `all_trips_cost`, `all_trips_trucks`, `all_trips_<type>`, `all_trips_util_kg/m3`: send each priceable shipment as its computed fleet. August: 1,877,582.9375 THB; 379 trucks (303 4W, 43 6W, 33 10W); 52.06% kg / 46.48% m³.
- `ltl_case_mix_cost`: LTL per case when available, otherwise fleet; FTL fleet. August: 1,136,022.15 THB.
- `cheaper_cost`, `cheaper_ftl_cost`, `cheaper_ltl_cost`, `cheaper_<type>`: cheaper-of-two costs and counts of truck-priced fleets only. August: 1,100,354.51875 THB (434,142 FTL + 666,212.51875 LTL); counts 146 4W, 20 6W, 29 10W. These are **not** the 379 all-trips trucks.
- `sheet_rule_*`: workbook-formula comparison from section 3.6. `unpriced_shipments`, `sheet_rule_unpriced_shipments` and `fleet_pricing_nonoptimal_shipments` distinguish incomplete/unproven comparisons.
- LTL comparison counters distinguish case below/above/equal fleet. August: 158 below, 14 above, none equal; above-fleet median 769.5 cases.

Plan summary `baseline_cost` excludes past-due built shipments; `baseline_cost_all_built` retains all. **Calendar-unavailable shipments are still dateable before calendar filtering and remain in `baseline_cost`.** `saving_thb/percent` are blank if any such dateable shipment is unplanned (calendar closure or fleet cap), otherwise compare against dateable permitted solos (percent blank for zero baseline). With a cap, complete coverage can cost more than unconstrained solos. Sheet-rule saving fields are blank if any built shipment is unplanned or any sheet comparison is unpriced; `sheet_rule_cost` itself remains the computed total, not a blank marker. Standalone baseline CLI also applies approved dates and permissions, but remains an unscheduled comparator, not a calendar-feasible dispatch plan.

## 10. Outputs (`trip_planning/outputs/<run_name>/`)

| File | Content |
|------|---------|
| `trips.csv` | One row per selected booking, not per physical vehicle: `trip_id`, `ship_date`, `truck_type` (single type, fleet mix or `case`), `n_trucks`, `n_drops`, `cases`, `kg`, `m3`, `util_kg`, `util_m3`, `cost`, `price_basis`, `shipment_ids`, `po_numbers`. |
| `trip_shipments.csv` | One row per shipment in a trip: `trip_id`, `shipment_id`, `ship_to`, `rdd`, `ship_date`, `early_days`. |
| `trip_orders.csv` | One row per order: `order_id`, `trip_id`, `po_numbers`, plus `original_rdd`, `effective_rdd`, `override_reason`. Dates are ISO; unoverridden orders have equal original/effective dates and blank reason. |
| `exclusions.csv` | `order_id`, `shipment_id`, **`ship_to`, `customer_name`, `province`**, `category` (`data_quality` or `business`), `reason`, `cases`, `kg`, `m3`, `po_numbers`, plus per-order `original_rdd`, `effective_rdd`, `override_reason` audit, including zero-quantity/no-rate orders and business leftovers. Enrichment uses all demand and the customer table, not just eligible shipment lines; name prefers `customer_name`, then `name`, and unavailable descriptive values stay blank. |
| `missing_rate_customers.csv` | **Only `reason=no_rate`** exclusions, grouped by `ship_to,customer_name,province`: `orders` (distinct normalized order ids), summed `cases,kg,m3`, `first_rdd,last_rdd` from effective RDDs. Exact columns: `ship_to,customer_name,province,orders,cases,kg,m3,first_rdd,last_rdd`. Other missing/unusable-price reasons are not included; no matches produce a header-only table. |
| `summary.csv` | Input/planned/excluded quantities by reason; `total_cost`; `baseline_cost` (dateable solos) and `baseline_cost_all_built`; `saving_thb,saving_percent` (blank for dateable leftovers); `sheet_rule_cost` and `saving_vs_sheet_rule_thb,saving_vs_sheet_rule_percent` (blank for any built leftover or unpriced sheet comparison); truck mix/utilization, shared bookings, solver status/gap, tie-break and penalty totals. |
| `flags.csv` | `flag`, `key`, `detail`. Warning codes: `rdd_tighter_than_sla`, `capacity_suspicious`, `truck_without_capacity`, `service_conflict`, `service_missing`, `not_in_master_customer`, `multidrop_priced_as_max_trip`, `po_spans_ship_tos`, `baseline_fleet_not_optimal`, `solver_not_optimal`, `candidate_pricing_not_optimal`. |
| `shipments.csv` | All built eligible effective-date shipments with resolved permissions, including those later excluded as past due, calendar-unavailable or fleet-limited; not just planned shipments. |
| `baseline_shipments.csv`, `baseline_summary.csv` | Detail and metric/value baseline tables for all built eligible shipments (section 9). |
| `run_metadata.json` | Detached immutable run snapshot: unique run id, UTC time, full resolved config, canonical original input/output table SHA-256 (including optional calendar/date/pair tables), separately labelled `effective_orders_sha256` for the copied demand with audit columns, source-file SHA-256 and git revision, Python/NumPy/pandas/SciPy/embedded HiGHS versions, validation checks/errors/warnings, labelled baseline scope and pricing/solver statuses/gaps, resolved tie weights and penalty. CLI adds `cli_options` (all parsed flags/paths, alongside resolved config), raw input/config file hashes and refuses changed, added or removed recognized input files during planning. Hash checks are point-in-time checks, not filesystem locks. |

Reporting runtime includes input validation, candidate generation, solves and report assembly (not disk publication). Hard checks repeat dispatch and every member's arrival-calendar legality, pricing/sharing permissions and existing capacity/date/cover/reconciliation checks. Serialize all files into a sibling staging directory first. New run directories publish by rename; existing builds use per-file replacement with rollback on I/O failure, not a multi-file filesystem transaction. Plans refuse overwriting an existing run. Invalid plans or output tables changed after validation are not published as valid. CLI errors are concise and nonzero. Run snapshots are detached from mutable caller configuration and input tables.

Plan publication writes **ten CSVs plus one JSON**. Enriched exclusions and the missing-rate rollup are included in output table SHA-256 snapshots and post-validation mutation checks, with the same immutable/no-overwrite publication as other plan tables. Weekly input tables are also hashed. The builder remains unchanged: **nine CSVs** (the four inputs, `shipments.csv`, `exclusions.csv`, `issues.csv`, `baseline_shipments.csv`, `baseline_summary.csv`), with its existing exclusion schema, no customer enrichment/rollup and no plan metadata. Builder exclusions are data-quality only; scheduling exclusions come from planning. Builder summaries also contain `data_*` diagnostics. Validation/serialization failure leaves prior builds untouched; staged rollback protects replacement of existing build files. Use a fresh run directory for a new plan.

The builder's exclusion columns are `order_id,shipment_id,category,reason,cases,kg,m3,po_numbers,original_rdd,effective_rdd,override_reason`; it does not add the planner's customer enrichment or rate-gap rollup. Warnings listed above do not themselves invalidate a feasible plan. Nonoptimal status/gaps are visible in summary, flags and metadata; CLI prints aggregate solver status/gap. The original workbook service conflict (August customer 626378: master LTL, contract FTL, used FTL) survives adapter resolution.

**August booking semantics:** 240 report rows are **bookings**, not 240 physical trucks: 96 truck-priced fleet bookings + 144 case-priced carrier bookings. The 96 fleet bookings use **104 trucks**; a solo fleet may include several. The legacy `trips` summary name is retained for compatibility. Utilization excludes case-priced solos and does not measure carrier LTL vehicle utilization.

## 11. CLI

```
python trip_planning/source/plan_trips.py --inputs trip_planning/outputs/va05_aug2025 \
    --out trip_planning/outputs/plan_aug2025 \
    [--max-shipments-per-trip 3] [--max-early-days 1] [--transit-days 0] \
    [--compat-mode same_province] [--handling-basis per_drop] \
    [--max-drops-per-trip 3] [--solo-only ID1,ID2] \
    [--max-trucks N] [--time-limit 600] [--config config.yaml]
```
  Defaults follow section 5. Planner `--inputs` and `--out` are required. The builder accepts optional `--workbook` and `--out` (defaults: root VA05 workbook and `outputs/va05_aug2025`). Province transit mapping, custom tie weights and `mip_rel_gap` are config/API-only. YAML nesting requires optional PyYAML; JSON or flat YAML with inline JSON mappings works without it. Unknown config keys fail. Explicit flags override config values; CLI `--time-limit` maps to `time_limit_s`.

  To opt in, place validated `planning_calendar.csv`, `weekly_calendar.csv` and/or `delivery_date_overrides.csv` beside the four input CSVs, and add any customer permission columns to `customers.csv`. The same plan invocation discovers them; no additional flags, inferred closures or approval defaults are introduced. Use a fresh output directory; do not edit saved historical outputs to simulate a rerun.

  The planner catches validation/publication failures, prints a concise stderr message and exits nonzero; the workbook builder propagates validation exceptions. Invalid plans are not published. **A valid time-limited/fallback plan can exit zero**: callers must inspect status, gap and flags rather than equating process success with optimality or dispatch approval.

### 11.1 Named what-if scenarios

`python trip_planning/source/scenarios.py --inputs CONTRACT_DIRECTORY --scenarios MANIFEST.json --out NEW_DIRECTORY` runs the existing planner independently for each scenario. The JSON-only manifest requires `baseline` (an exact included scenario name) and a nonempty `scenarios` array. Each entry requires `name`; `config`, `truck_overrides`, `assumptions` default to `{}`, `[]`, `[]`. Reject unknown/duplicate JSON keys, nonfinite JSON numbers, unsafe names and case-insensitive duplicate names. Names are non-device ASCII slugs, at most 64 characters. Assumptions are strings, not approval evidence.

Each `config` overlays **`TripConfig` defaults separately**, not the named baseline or preceding scenario. Every scenario gets fresh copies of required/optional tables and pair overrides. `truck_overrides` entries require an existing supported `truck_type` and at least one of `capacity_kg,capacity_m3`, supplied as finite positive numeric values, never booleans. Unspecified capacities retain input values. All overlays validate before solving; no 6W/18W capacity is guessed.

Illustrative manifest only (not a built-in scenario set or approved policy):
```json
{"baseline":"default","scenarios":[
  {"name":"default","config":{},"truck_overrides":[],"assumptions":["Unapproved defaults"]},
  {"name":"transit0","config":{"transit_days":0}},
  {"name":"transit1","config":{"transit_days":1}},
  {"name":"transit2","config":{"transit_days":2}},
  {"name":"cap8","config":{"max_trucks":8}},
  {"name":"cap4","config":{"max_trucks":4}}
]}
```

A ready-to-use, unapproved manifest is shipped at `trip_planning/scenarios/reference.json`: baseline `transit0`, plus `transit1`, `transit2`, `cap8` and `cap4`. These use existing defaults except the named change. It deliberately contains no capacity overrides: supply explicit 6W/18W alternatives rather than inventing values. Running the manifest performs five solves and does not replace existing saved results.

Root outputs are `scenario_comparison.csv` and `scenario_metadata.json`, plus a named subdirectory per scenario containing **ten plan CSVs + `run_metadata.json`**. Comparison retains ordinary planner savings/suppression, adds a **matched planned-shipment solo baseline** and saving, input/planned/excluded quantities and reasons, bookings/consolidation, truck mix, solver status/gap, changed ship dates on common planned orders and truck-mix change. Cross-scenario `cost_delta_vs_baseline` (run minus baseline) and `saving_vs_baseline_thb/percent` are populated **only for identical planned normalized order IDs, each order's effective RDD and cases/kg/m³ coverage**; otherwise blank with `scope_explanation`. Equal totals or shipment IDs alone are insufficient; percent is also blank for zero baseline cost. Matched-solo savings do not establish equal cross-scenario coverage.

Every run and comparison is `assumed_unapproved`, even the baseline. Metadata records manifest, assumptions, resolved configs, explicit capacity overrides, original recognized-input and manifest hashes, run attribution and solver outcomes; root metadata also hashes the comparison CSV. The entire set stages in a sibling temporary directory and publishes by one rename into a **new root**, all-or-nothing. Existing destinations and symlink/junction paths are refused. Recognized inputs (including weekly calendars) and manifest are hashed before reading and rechecked for changes/additions/removals after serialization; any failure cleans staging without partial published scenarios. These are point-in-time checks, not locks or business approval.

Plan metadata also includes `optional_features` and detached `calendar_rules` (version, precedence, normalized dated openings/closures, weekly ranges). Metadata weekdays use Monday=0 through Sunday=6; the CSV contract requires full weekday names. Supplied rules are recorded, not authenticated as business approval.

New-directory publication retries only transient Windows permission/lock errors (codes 5/32/33), up to five attempts with bounded backoff. Destination collisions and persistent/other errors still fail; no fallback merges or overwrites another run. This applies to plan/build new directories and the scenario root.

Comparison interpretation:
- `saving_thb` is each scenario's ordinary dateable-solo comparison, with the planner's suppression rules; it is not the change against the named scenario baseline.
- `matched_solo_baseline_cost` and `matched_solo_saving_thb` price **only that scenario's served shipments** as solos. They may be present with incomplete demand coverage and must be read with excluded quantities; they are not full-demand savings.
- `scope_comparable=true` means the same served orders, effective dates and quantities, **not** full input coverage or approved assumptions. Two runs may exclude the same orders and still be comparable on their served scope. Different served IDs suppress cross-scenario delta/saving fields even if aggregate quantities match.
- `cost_delta_vs_baseline` is scenario minus named-baseline cost; negative means cheaper. `saving_vs_baseline_thb` uses the opposite sign. Departures may change without changing effective-RDD scope. Solver status/gaps must be checked before interpreting any difference as an optimal improvement.

## 12. Test plan

### 12.1 Unit tests (hand-verified expected values)
Use small synthetic tables built in the test. Write the arithmetic in a comment beside each expected value.

1. Single ship-to, one truck type: cost equals trip plus handling.
2. Blank trip rate: truck not offered; falls to next type.
3. Truck choice by capacity: 2,500 kg goes on one 6W, not two 4W, when cheaper.
4. Capacity is two dimensional: m3 binds before kg for a bulky load.
5. Multi-drop rate: max of trip rates plus handling per drop.
6. LTL cheaper-of-two: case price below truck price picks `case`; reverse picks `truck`.
7. FTL rider rule: mixed-service sharing uses the truck formula, not an additional LTL case bill; rider destinations can still increase the maximum rate or handling.
8. Dominance: non-saving consolidation is pruned only without a cap; costlier resource-saving consolidation is retained under a cap.
9. Arrival windows: reject late arrivals or per-member early days beyond `max_early_days`; test both compatible wide RDD spreads and incompatible equal RDDs with heterogeneous transit.
10. Compatibility: different provinces never share a truck under `same_province`; overrides allow and deny work.
11. Past due: reason `past_due`, not planned.
12. Exclusion reasons: each data-quality code is produced for a matching row.
13. Penalty sizing: one fewer unplanned shipment beats cost/tie differences in a proven solve; leftovers are shipment-count-first, not quantity-first. Fallback does not promise optimal coverage.
14. Tie-break: with equal cost, the plan with fewer early days wins; tie-breaks never change a plan whose cost differs by 1 THB or more.
15. Fleet limit: with `max_trucks` set, the limit is respected and a leftover gets `fleet_limit`.
16. Unconstrained solo comparison: a full-baht increase cannot win through ties; custom fractional-baht examples document the smaller-difference caveat. Do not apply a solo cost upper bound under caps.
17. Reconciliation: planned plus excluded equals input totals in cases, kg, m3.

### 12.1a Retrospective regressions
`test_retrospective.py` uses synthetic policies only: strict permission syntax and all solo/shared pricing branches (including FTL riders, missing case rates and sharing-deny precedence), cap-frontier resource alternatives, dispatch versus receiving with nonzero heterogeneous transit, explicit open exceptions, uncapped latest/capped all-date intersections, disjoint customer availability, total calendar closure and suppressed savings, approved-date split/merge/early dates, raw-date/input immutability and audit on every exclusion class, fatal optional schemas/ids/dates/booleans/duplicates, detached hashes/full CLI options, changed/added optional files, hard-check rejection and immutable publication. Exact August default cost/count checks supplement, not weaken, the original acceptance tests.

### 12.2 Real-data acceptance (August 2025)
Run the full pipeline on `trip_planning/outputs/va05_aug2025/` and assert:

- Input totals reconcile: 117,745 CSE, 702,897 kg, 1,841.29 m3. Planned plus excluded equals these.
- 341 positive-quantity raw ship-to/RDD groups; 329 eligible shipments from 1,436 orders, with excluded orders reconciled separately.
- `total_cost <= 1,100,355` THB (the cheaper-of-two baseline). Report the actual saving; do not assert a target.
- Every order is in exactly one trip or one exclusion with one reason.
- No trip ships after any member's RDD; no member arrives more than `max_early_days` early.
- Every counted-truck booking fits aggregate kg and m³ capacity; a case-priced solo has no modeled vehicle capacity.
- Every shared booking uses one offered truck type for all drops; a solo fleet may mix offered truck types.
- Solver gap is 0 for every component, or the report lists the component and its gap.
- Run time is reported. Do not run a 600 s solve for a docstring change; see `AGENTS.md`.

## 13. Open items for the business

1. LTL formula: **closed as a confirmed default** (section 5.1). Cheaper-of-two is the rule the module runs. An invoice check (135448 on 2025-08-16, 616512 on 2025-08-20) would be extra commercial evidence, not a second formula.
2. Handling: per drop or per truck. The default `per_drop` is part of the provisional multi-drop placeholder. The spec text still says only 15 ship-tos on 4W carry handling in the August workbook; that count is not re-derived in this folder.
3. Multi-drop price rule: **open provisional placeholder**. The code uses the max solo trip rate. A separate Foodland/Donki multidrop card is not loaded. `rates.multidrop` is a flag only.
4. Consolidation geography: **closed as a confirmed bootstrap default**. `same_province` is the running rule. A lane table would replace it later; it is not required to run or test this module.
5. Early delivery: is early arrival allowed, how many days, and any cost. The code cap is raw RDD spread versus `max_early_days` (default 1), not a per-member early window when transit differs.
6. Transit days for long lanes: **open provisional placeholder, and the first real-data override.** The default is 0 for every province. Do not trust consolidation on real orders until lane or province transit replaces it.
7. Capacities: 6W 25 m3 and 18W (blank).
8. The 57 orders with RDD tighter than SLA: plan as given (default) or exclude.
9. The 8 ship-tos with no rate (10.99% of input cases, 13.11% of m³): rate source needed.
10. The 37 orders with ship-tos missing from Master Customer. Implemented default: planned from the rate card and flagged `not_in_master_customer`; the `no_master_customer` exclusion fires only when a ship-to is absent from `customers.csv` itself.
11. Fleet limit: does a real per-day truck cap exist. Implemented accounting: case-priced LTL solos count as 0 trucks; all consolidated bookings consume one. Confirm carrier-resource semantics, not just invoice basis.
12. Is a remainder of a split shipment allowed to consolidate (currently no).
13. Operational approval gates: carrier invoices and actual booked-trip evidence (the workbook formulas are not invoices); signed handling/multi-drop/LTL tariffs and customer consolidation/early-arrival permissions; real lane transit times, dated/weekly receiving/dispatch policies and cutoff times; validated vehicle kg/m3 capacities and daily/type/carrier availability. Optional permission/date/calendar contracts and named scenarios are implemented, but **no approved operational policies are supplied or guessed**. Business-day transit, cutoff/appointment times, route/drop sequencing, driver hours, actual carrier LTL truck use, stock/picking/dock capacity and real-time execution remain outside this model. Defaults are unchanged; there is no live eligibility/horizon, holding/force mechanism, hub routing, pallet or physical loading change. Savings are a scenario estimate pending those approvals, not an approved dispatch instruction.

### 13.1 Business data and approvals pending (capability already available)

| Pending item | Existing capability | What must be supplied/decided |
|--------------|---------------------|-------------------------------|
| Missing customer rates | `missing_rate_customers.csv` identifies affected demand | Approved rates and service for excluded destinations; do not estimate them from other customers silently |
| Transit and vehicle capacity | Province transit mapping, direct truck inputs and scenario overlays | Measured/approved transit, confirmation of 6W volume, both 18W capacities, and destination offer/access suitability |
| Commercial pricing | Confirmed default: cheaper-of-two LTL. Provisional placeholders: max-rate multi-drop and per-drop handling | A different multi-drop tariff needs a separate implementation. Do not treat the max-rate rule as an invoice |
| Customer availability and sharing | Customer permissions, pair/solo-only rules, weekly schedules and dated exceptions | Approved restrictions, date ranges and early-delivery policy; GI/Sunday observations are not approval |
| RDD corrections | Explicit order-level override with original/effective audit | Authorized revised date and reason; no automatic interpretation of NLM notes |
| Fleet accounting | Optional total counted trucks per departure date | Actual daily cap and agreement on case-solo zero/shared-one accounting; carrier/type availability is not modeled |

### 13.2 Deferred software extensions (not implemented)

| Extension | Decision/data needed before implementation | Safe current interpretation |
|-----------|------------------------------------------|-----------------------------|
| Live eligibility and rolling horizon | Planning date; allowed Status/DlBl/Rj values; blank/unknown handling; mixed-status line/order treatment | Historical Completed/Not delivered is not an automatic retrospective exclusion. Source status detail is not preserved as a live eligibility contract. |
| Holding, force dispatch and minimum-load policy | Horizon, allowed deferral, deadlines, force semantics, load thresholds and objective/coverage treatment | No held state, forced-date input or minimum-load warning/constraint exists. Any future forced date must respect release, dispatch/arrival calendars and fleet feasibility; do not silently override conflicts. |
| Hub delivery | Confirmed customer-to-hub mapping, applicable rates, delivery completion definition and onward-leg scope | No rerouting from ANT/Rama II remarks. A hub-leg cost cannot be compared with a final-customer baseline as full-delivery savings. |
| Business-day transit and appointments | Transit counting rules, site time zones, appointment windows and cutoff times | Weekly availability is implemented; elapsed-day transit remains unchanged. |
| Pallet/physical capacity | SKU-line quantities/product completeness, palletization/rounding/stackability rules and truck pallet positions | Kg/m³ capacity and fractional pallet equivalents do not establish physical packing feasibility. |
| Routes and detailed resources | Inter-customer travel/time, sequencing rules, daily type/carrier supply and warehouse/dock constraints | Shared bookings are priced groups, not executable routes or warehouse schedules. |
| Actual-spend validation / EDI ingestion | Matching-period bookings/invoices, consistent served scope; matching EDI data and identity rules | Workbook formula comparisons and scenario estimates are not invoice-verified savings. |

### 13.3 Pending analysis and operational acceptance

- Run the shipped five-scenario reference manifest into a fresh output root and review cost **with coverage, exclusions and solver status**. It has not yet been executed as a complete August scenario batch; no scenario-result improvement is claimed here.
- Add capacity alternatives only when explicit values are available, labelled hypothetical or approved as appropriate. Do not copy synthetic test capacities into operational inputs.
- Obtain business review of the rate-gap report and supply approved rules before a dispatch pilot. Reconcile all input demand and compare equivalent delivery scope against actuals.
- Existing saved outputs retain their historical schema. Rerun to a fresh directory to obtain enriched exclusions, the tenth CSV and current metadata; do not edit old results to appear regenerated.
- A fast/slow pytest split is a possible maintenance improvement, not a model feature already delivered. No commit/deployment/business approval is implied by the verification record.

## 14. Verification and reference results

**Verified 2026-10-31: 417 tests passed in 348.81 seconds**, including August extracts and raw-workbook acceptance on this checkout. All trip-planning Python source/test files compiled. Ruff/Mypy were unavailable on PATH. The previous retrospective suite had 279 tests (195 pre-retrospective). Expanded coverage includes enriched exclusions/no-rate rollups, weekly calendars/reopening/precedence, real scenario CLI integration, independent overlays and scope-aware comparisons, atomic publication/input mutation and bounded Windows-lock retries. Existing strict validation, service conflicts, capped alternatives, heterogeneous transit, whole-baht preferences and fallback checks remain. Independent read-only review found no concrete high/medium defects. Acceptance tests skip when fixtures are unavailable; they do not promise arbitrary-data performance or business approval. Tests publish only to temporary directories. Run the suite with `python -m pytest -c trip_planning/pytest.ini trip_planning/tests -q` from the repository root.

Saved August default summary (`outputs/plan_aug2025/summary.csv`): input 117,745 cases / 702,897.2828 kg / 1,841.290246 m³; planned 1,357 orders / 104,805 cases / 606,228.7206 kg / 1,599.834224 m³; excluded 79 orders (31 no-rate + 48 zero-quantity), carrying 12,940 cases / 96,668.5622 kg / 241.456022 m³. Planned-cost comparison therefore covers **89.01% of input cases**, not the cost of all demand.

Plan cost 945,946.31875 THB versus dateable-solo baseline 1,100,354.51875 THB; modelled saving 154,408.2 THB (14.032587%). Sheet-rule baseline 1,117,274.51875 THB; modelled saving 171,328.2 THB (15.334477%). There are 240 bookings, 58 multi-shipment, 104 counted trucks (61 4W / 13 6W / 30 10W), utilization 69.7531% kg / 68.3696% m³, solver status optimal and gap 0. The stored runtime is historical/machine-dependent, not a performance requirement. Existing saved artifacts are not retroactively enriched with new metadata; rerun into a fresh directory to obtain the current output contract.

When changing Python, compile modified modules, run relevant regression tests and request independent review per `AGENTS.md`. Documentation-only synchronization does not require rerunning an optimization. Keep unapproved business rules clearly separated from implemented software behavior.