# Trip Planning — Detailed Old-versus-New Comparison

**Comparison date:** 31 October 2026  
**Audience:** business reviewers, planners, developers and maintainers  
**Old snapshot:** [trip_planning_old](trip_planning_old)  
**Current implementation:** [source](source), [tests](tests), [specification.md](specification.md) and [trip_planning_model.html](trip_planning_model.html)

> **In plain language:** the old version could group orders, price possible bookings and optimize a plan. The new version keeps that foundation, fixes important edge cases, adds explicit customer/date/availability controls, makes missing-rate demand actionable, supports controlled scenario comparisons, and records enough evidence to audit a run. It is still a shipment-consolidation model—not a route optimizer, physical truck-loading system or approved live dispatch system.

## Contents

1. [How to read this comparison](#1-how-to-read-this-comparison)
2. [Executive summary](#2-executive-summary)
3. [What stayed the same](#3-what-stayed-the-same)
4. [Architecture and file-by-file changes](#4-architecture-and-file-by-file-changes)
5. [Workbook ingestion and input validation](#5-workbook-ingestion-and-input-validation)
6. [Shipments, service provenance and PO traceability](#6-shipments-service-provenance-and-po-traceability)
7. [New customer permissions](#7-new-customer-permissions)
8. [Approved delivery-date changes](#8-approved-delivery-date-changes)
9. [Correct arrival windows and new calendars](#9-correct-arrival-windows-and-new-calendars)
10. [Fleet-cap planning corrections](#10-fleet-cap-planning-corrections)
11. [Objective, solver validation and fallback](#11-objective-solver-validation-and-fallback)
12. [Pricing and baseline changes](#12-pricing-and-baseline-changes)
13. [Reports and exclusion changes](#13-reports-and-exclusion-changes)
14. [Scenario analysis](#14-scenario-analysis)
15. [Audit metadata and safe publication](#15-audit-metadata-and-safe-publication)
16. [Tests and verification evidence](#16-tests-and-verification-evidence)
17. [What the saved results actually prove](#17-what-the-saved-results-actually-prove)
18. [Migration and compatibility checklist](#18-migration-and-compatibility-checklist)
19. [Pending business decisions and software gaps](#19-pending-business-decisions-and-software-gaps)
20. [Final assessment](#20-final-assessment)

---

## 1. How to read this comparison

### 1.1 Comparison method

This report compares **the actual files in the attached old folder** with the current files. It is a snapshot comparison, not a reconstruction of every intermediate commit or development date.

The comparison used:

- Content-hash comparisons to identify added, changed and identical files.
- Direct inspection of old/current source, tests and specifications.
- Fresh reads of the stored sample and August summary CSVs and their headers.
- The existing documented verification record for the current implementation.

**No optimization or test suite was rerun to prepare this report.** No source code, input policies, workbook or saved result was changed. Only this report was added.

### 1.2 Important evidence boundaries

1. **Old specification versus old implementation:** [trip_planning_old/specification.md](trip_planning_old/specification.md) is partly a build-oriented specification. It mentions workbook-adapter and builder modules that are not in the attached old source folder. Therefore a promise in that specification is not proof that the attached snapshot contains the capability.
2. **Old code was not featureless:** it already had fleet caps, province transit configuration, several baselines, warning flags, reconciliation and component solver statuses. This report does not count those as entirely new.
3. **Saved output versus current capability:** current source can produce a richer schema than some checked-in results. Saved results are historical artifacts; they are not automatically regenerated when code changes.
4. **Software capability versus business approval:** an optional rule input does not establish that its contents are approved. Likewise, a successful process or passing tests does not establish dispatch approval.
5. **Optimization baseline versus old software:** a solo-shipment baseline is a planning alternative. A saving against that baseline is not automatically a saving caused by this software upgrade.

### 1.3 Change categories used below

| Category | Meaning |
|---|---|
| Retained | Existed in the attached old code and remains in the current implementation |
| Corrected / strengthened | Existing behavior was repaired, made more complete or checked more rigorously |
| Added | Not present in the attached old source snapshot |
| Clarified | Documentation/report interpretation was corrected without necessarily changing a commercial formula |
| Pending | Still needs business input, a separate implementation or operational validation |

## 2. Executive summary

### 2.1 Main changes at a glance

| Area | Attached old version | Current version | Practical benefit |
|---|---|---|---|
| Planning foundation | Group shipments, generate candidates, HiGHS set partitioning | Same foundation | No replacement with an unrelated model |
| Workbook-to-contract pipeline | Referenced in old specification, but adapter/builder absent from attached source | Adapter and validated nine-artifact builder included | Reproducible path from the workbook to planning inputs |
| Fleet caps | Daily constraint existed, but candidate choices were too aggressively reduced | Cost/truck-count alternatives, all legal dates and cap-aware shared candidates | A resource-saving option is not discarded merely because it costs more |
| Transit/date correctness | Province overrides existed, but shared-date logic used raw RDD spread | Intersect each member's release/transit/early-arrival window | Correct handling of different lane transit times |
| Customer restrictions | Compatibility modes, pair overrides and solo-only list | Those controls plus pricing/sharing permissions | Enforce customer-specific commercial restrictions |
| RDD corrections | No dedicated approved-date input/audit | Explicit per-order override, applied before grouping | Transparent split/merge of effective-date shipments |
| Availability | No dispatch/receiving calendar inputs | Dated and weekly rules with opening exceptions | Prevent bookings on supplied closed dates |
| Missing rates | Excluded order rows with a reason | Customer-enriched exclusions plus customer-level no-rate rollup | Clear list of demand requiring rate maintenance |
| Plan output contract | Six CSVs | Ten CSVs plus run metadata JSON | Detail, comparison and traceability in one plan bundle |
| Scenarios | Manual independent runs only | Validated named manifest, independent overlays and scope-aware comparison | Safer, repeatable what-if analysis |
| Solver handling | Basic incumbent selection and partition check; no-incumbent failure | Candidate/incumbent checks and explicitly nonoptimal feasible fallback | Invalid solver values are not accepted as a valid plan |
| Publication | Direct writes into a potentially existing destination | Staging, validation, hashes and protected publication | Lower risk of overwritten or half-written results |
| Test inventory | 4 test modules, 20 test definitions | 13 test modules, 153 test definitions | Much broader coverage of failure paths and optional rules |
| Documentation | Build instructions and provisional business assumptions | Implemented specification, HTML explanation and explicit pending-work register | Clearer distinction between delivered software and unresolved decisions |

### 2.2 Most important business interpretation

The upgrade is **not** a claim that a new tariff was approved or that the default plan is now cheaper than the old program's full-data plan. Its largest gains are **correctness under constraints, control, auditability and interpretation**.

Optional policies are inactive when absent. Existing default August regression values were retained through the recent implementation phases. A fresh, controlled old-versus-new August execution was not performed for this report, so no numerical software-version improvement is claimed.

## 3. What stayed the same

These foundations already existed and should not be presented as new features:

| Retained behavior | Explanation |
|---|---|
| Four required contract tables | Orders, customers, rates and truck capacities remain the main planner inputs |
| Whole-shipment planning | Orders for one ship-to/RDD are grouped; a shipment is not distributed among different selected bookings/dates |
| Solo fleet | One solo booking can contain several trucks; load is divisible in aggregate kg/m³ |
| Shared booking | Two or more shipments share one modeled truck, within configured member/drop limits |
| Capacity checks | Both kg and m³ matter; physical pallet packing is not constructed |
| Truck pricing | Trip rate plus handling for solos; maximum destination trip rate plus configured handling for shared bookings |
| LTL pricing | Cheaper of eligible case billing and fleet/truck billing, with truck billing retained on a price tie |
| Compatibility settings | Same province, same group, either, any or none; symmetric pair allow/deny and solo-only restrictions |
| Province transit configuration | `transit_days_by_province` already existed in the old configuration; its validation and use in shared windows were improved |
| Daily fleet cap | `max_trucks` and the per-departure-date constraint already existed; the candidate space and penalty supporting them were corrected |
| Solver family | SciPy `milp` using embedded HiGHS, binary set-partitioning decisions |
| Decomposition | Connected candidate components without a cap; coupled solve with a daily cap |
| Requested gap/time settings | Zero requested MIP gap; default 600 seconds per trip-model component |
| Default preference formula | Global data-sized early-delivery/LTL-upgrade weights already existed; custom-weight bounds were strengthened |
| Quantity reconciliation | Planned plus excluded quantities must equal input quantities |
| Basic warning/reporting | SLA, suspicious capacity, missing truck capacity, service conflict and solver component information already existed |
| Core commercial uncertainty | Pricing, transit, early delivery and resource accounting still need operational confirmation |

The default settings remain: maximum three shipments per shared booking, maximum one day early, zero elapsed transit days, same-province compatibility, per-drop handling and no fleet cap.

**Source evidence:** [trip_planning_old/source/config.py](trip_planning_old/source/config.py), [trip_planning_old/source/pricing.py](trip_planning_old/source/pricing.py), [trip_planning_old/source/candidates.py](trip_planning_old/source/candidates.py), [trip_planning_old/source/trip_model.py](trip_planning_old/source/trip_model.py).

## 4. Architecture and file-by-file changes

### 4.1 Old flow

Prepared contract CSVs → shipment grouping/exclusions → solo/shared pricing → candidate generation → HiGHS selection → report checks → six directly written CSVs.

### 4.2 Current flow

Workbook adapter/builder **or** directly supplied contract CSVs → input snapshot/validation → approved-RDD copy → effective-date grouping and permissions → combined dispatch/receiving calendars → cap-aware candidate generation → validated HiGHS selection/fallback → baselines and hard checks → ten report tables plus metadata → staged publication.

The scenario runner repeats the existing planner with independently copied inputs/settings, creates a scope-aware comparison, and publishes the whole scenario set together.

### 4.3 Source inventory

There are **9 old Python source files and 15 current Python source files**, counting the initializer: **8 changed, 6 added, 1 identical, none removed**. These counts describe the attached source folders only.

| Old source | Current source | Status and main change |
|---|---|---|
| [trip_planning_old/source/__init__.py](trip_planning_old/source/__init__.py) | [source/__init__.py](source/__init__.py) | Identical initializer |
| [trip_planning_old/source/config.py](trip_planning_old/source/config.py) | [source/config.py](source/config.py) | Changed: stricter types, transit validation and identifier handling; defaults retained |
| [trip_planning_old/source/shipments.py](trip_planning_old/source/shipments.py) | [source/shipments.py](source/shipments.py) | Changed: permissions, approved-date audit, PO traceability, provenance flags and earlier validation |
| [trip_planning_old/source/pricing.py](trip_planning_old/source/pricing.py) | [source/pricing.py](source/pricing.py) | Changed: permitted pricing branches and fleet-pricing status propagation |
| [trip_planning_old/source/candidates.py](trip_planning_old/source/candidates.py) | [source/candidates.py](source/candidates.py) | Changed: correct arrival windows, calendars, capped alternatives/dates and pricing statuses |
| [trip_planning_old/source/trip_model.py](trip_planning_old/source/trip_model.py) | [source/trip_model.py](source/trip_model.py) | Changed: capped penalty bound, candidate/incumbent checks, fallback and tie-weight safety |
| [trip_planning_old/source/baseline_cost.py](trip_planning_old/source/baseline_cost.py) | [source/baseline_cost.py](source/baseline_cost.py) | Changed: capped fleet pricing, status/gap checks, permission-aware baselines and workbook-rule comparator |
| [trip_planning_old/source/plan_trips.py](trip_planning_old/source/plan_trips.py) | [source/plan_trips.py](source/plan_trips.py) | Changed: optional inputs, snapshots, hashes and safer CLI failure handling |
| [trip_planning_old/source/reporting.py](trip_planning_old/source/reporting.py) | [source/reporting.py](source/reporting.py) | Changed: richer reports, stronger hard checks, scope-aware savings and validated publication |
| Not in attached old source | [source/va05_adapter.py](source/va05_adapter.py) | Added: workbook mapping/validation and source diagnostics |
| Not in attached old source | [source/build_inputs.py](source/build_inputs.py) | Added: validated workbook-to-CSV build CLI |
| Not in attached old source | [source/retrospective.py](source/retrospective.py) | Added: strict permissions/date overrides and immutable dated/weekly calendar rules |
| Not in attached old source | [source/scenarios.py](source/scenarios.py) | Added: named scenario validation, overlays, comparison and publication |
| Not in attached old source | [source/provenance.py](source/provenance.py) | Added: detached run snapshot, hashes, source revision and environment versions |
| Not in attached old source | [source/publication.py](source/publication.py) | Added: staged directory/file publication, replacement rollback and bounded Windows-lock retry |

The mathematical selection model remains concentrated in [source/trip_model.py](source/trip_model.py). However, **business feasibility is not confined to that file**: grouping, permissions, pricing and candidate generation determine what the solver is allowed to select.

## 5. Workbook ingestion and input validation

### 5.1 Workbook pipeline now included in this snapshot

The current adapter reads cached Excel values and maps VA05 demand, customer master, rate card and truck capacity information into the planning contracts. The builder validates the tables, writes staged CSVs, reads those serialized contracts back, and builds shipment/baseline artifacts before publication.

Its nine build artifacts are:

1. Orders contract.
2. Customer contract.
3. Rate contract.
4. Truck-capacity contract.
5. Eligible shipment detail.
6. Data-quality exclusions.
7. Source issues/diagnostic counts.
8. Baseline shipment detail.
9. Baseline summary.

**This is different from the planner's ten-CSV-plus-JSON bundle.** The builder does not create the planner's customer-enriched exclusions, rate-gap rollup or run metadata. Its exclusions are eligibility/data-quality outcomes, not scheduling leftovers.

### 5.2 Validation strengthened without pretending the old code had none

| Validation area | Old behavior | Current behavior |
|---|---|---|
| Required columns and duplicate keys | Already checked in several parsers | Retained and extended to new contracts |
| Order quantities | Required finite/nonnegative values already checked in CSV planning | Retained; booleans rejected as numbers and workbook lines checked before aggregation |
| Dates on excluded orders | Date parsing occurred after basic eligibility filtering | Raw dates are validated before exclusions/overrides can hide malformed demand |
| Workbook line corruption | Adapter absent from attached old source | Invalid/missing/nonfinite/negative/boolean demand values fail with context, not silent zero replacement |
| Workbook destination identity | Adapter absent from attached old source | Missing identities and conflicting ship-to/sold-to within document/RDD fail before aggregation |
| Release date aggregation | CSV grouping already used latest member document date | Workbook order construction also uses latest contributing line document date |
| LTL prices | Parsed numerically, but negative case values were not explicitly rejected there | Both MT and GT are validated, including the column not selected for billing |
| Config integer fields | Comparisons/coercion could admit booleans or truncate province values | Exact integer/nonboolean checks; negative/fractional/string transit settings rejected |
| Custom preference weights | Individual finite/nonnegative check existed | Also enforce the whole-plan total preference bound before solving |
| Nonfinite identifiers | Some missing forms normalized | Additional nonfinite/missing forms are normalized consistently |

Malformed data is a **fatal validation failure**. A valid row with no rate is an **exclusion with preserved quantities**. The report distinguishes these outcomes; there is no policy of silently turning corrupted rows into zero demand.

**Limit:** Excel formulas are not recalculated by this pipeline. Suitable cached values must exist in the workbook.

**Evidence:** [source/va05_adapter.py](source/va05_adapter.py), [source/build_inputs.py](source/build_inputs.py), [source/config.py](source/config.py), [source/shipments.py](source/shipments.py), [tests/test_input_validation.py](tests/test_input_validation.py), [tests/test_va05_adapter.py](tests/test_va05_adapter.py).

## 6. Shipments, service provenance and PO traceability

### 6.1 Grouping foundation retained, effective date added

Old grouping key: **ship-to + original RDD**.  
Current grouping key: **ship-to + effective RDD after explicit approved overrides**.

The release date remains the latest contributing document date. Orders remain traceable to shipments and selected bookings. Original quantities and identities are not changed by a date override.

### 6.2 PO numbers are reporting context, not planning identity

The current pipeline preserves optional `po_numbers`, cleans them into sorted/de-duplicated semicolon lists, and carries them into booking, order and exclusion reports. It also flags a PO appearing across multiple ship-tos.

This does **not** merge those destinations, change service, enforce a PO-wide delivery date or make PO the order's optimization key. A head-office PO split across stores may be legitimate; the flag is informational.

### 6.3 Service conflict provenance is visible

- The new workbook adapter preserves `service_master`, `service_contract` and `service_conflict`.
- When building from Excel, a nonblank rate-card contract service takes precedence over master service.
- Direct CSV planning still resolves nonblank customer service first, then rate service; unrecognized/missing effective service is priced as FTL.
- The warning preserves the disagreement rather than letting an already-resolved value make it disappear.
- The August example is customer 626378: master LTL, contract FTL, effective FTL.

These two precedence paths are intentional distinctions to understand—not a claim that all direct CSV service resolution changed to contract-first.

### 6.4 Missing from workbook master is not always missing from the contract

The current adapter can construct a customer record from available rate-card information and flag that it was not in Master Customer. Such a customer can still be planned when a rate exists.

`no_master_customer` means the order's ship-to is absent from the actual customer contract, or blank. It does **not** automatically mean every order absent from the workbook's Master Customer sheet is excluded. The 37 August orders with that provenance are not all silently discarded for that reason.

**Evidence:** [source/shipments.py](source/shipments.py), [source/va05_adapter.py](source/va05_adapter.py), [source/reporting.py](source/reporting.py).

## 7. New customer permissions

The old version had compatibility modes and pair/solo-only rules. It did not have these three customer permission columns:

| Field | Default when missing/blank | Restriction |
|---|---|---|
| `allow_truck_pricing` | True | Whether the customer may use an existing truck-priced branch |
| `allow_case_pricing` | True | Whether an LTL customer may use an existing case-priced branch |
| `allow_sharing` | True | Whether its shipment may share a booking with another shipment |

### 7.1 What permissions do and do not do

- They restrict existing contract eligibility; they do not invent prices, capacities or a new service.
- FTL remains truck-only even if case permission is true.
- All members must permit the chosen shared-pricing branch.
- A case-only member cannot be placed on a truck-priced shared trip.
- A truck-only member blocks the case branch even when case billing is cheaper.
- A sharing denial overrides a pair allow, including sharing with another shipment for the same customer.
- Orders already grouped into one shipment are not separate sharing members.
- An all-LTL shared booking still needs a common offered vehicle and sufficient capacity even if the price basis is case.
- If explicit restrictions remove every actually priced legal branch, `service_not_permitted` explains the exclusion.

### 7.2 Example

Suppose an LTL customer has a 600 THB case quote and an 800 THB truck quote:

- Both branches permitted → select 600 THB case pricing.
- Case pricing forbidden → 800 THB truck pricing can remain legal.
- Truck pricing forbidden → only the actual case quote can be used.
- Both forbidden → exclude as service not permitted; do not silently override the restriction.

Strict parsing accepts native booleans or trimmed strings `true`, `false`, `1`, `0`. Missing/blank permissions preserve legacy behavior. Invalid strings and numeric API booleans represented as integers are rejected rather than guessed. Calendar booleans differ: blank is an error.

**Evidence:** [source/retrospective.py](source/retrospective.py), [source/pricing.py](source/pricing.py), [source/candidates.py](source/candidates.py), [tests/test_retrospective.py](tests/test_retrospective.py).

## 8. Approved delivery-date changes

The current planner accepts an optional order-level approval table with `order_id`, `approved_rdd` and `reason`.

### 8.1 Before versus after

| Old | Current |
|---|---|
| No dedicated approved-date contract or per-order original/effective audit | Explicit override applied to a deep copy before shipment grouping, baseline pricing and SLA flags |
| An edited source date could obscure the original business request | Reports retain `original_rdd`, `effective_rdd` and `override_reason` |

Validation covers known normalized order IDs across **all** demand, including no-rate/zero-quantity exclusions. Unknown, duplicate or blank IDs, invalid dates and empty reasons fail. Raw dates must validate first; an override cannot hide an invalid original RDD.

### 8.2 Why before-grouping matters

Two orders for customer A originally due on 10 and 11 August are two shipments. If an explicit approval moves the first order to 11 August, they may become one shipment. Moving only one order out of a shared date group may split it instead.

This is a controlled change in the planning unit—not a post-solve relabeling of a date. Quantity/release rules still apply. An approved date before release produces the normal past-due outcome; it does not authorize an impossible departure.

**Limit:** the software records supplied approval data but does not authenticate a business signatory. Workbook notes are not automatically imported as approvals.

**Evidence:** [source/retrospective.py](source/retrospective.py), [source/shipments.py](source/shipments.py), [source/plan_trips.py](source/plan_trips.py), [tests/test_retrospective.py](tests/test_retrospective.py).

## 9. Correct arrival windows and new calendars

### 9.1 Corrected: different transit times require member-level windows

The old configuration already supported province transit days. However, the old shared-candidate logic combined an RDD-spread test with a single selected departure date. Raw RDD spread is not sufficient when transit differs by member.

Current logic computes, for each shipment:

$$
\text{deadline}_s = \text{effective RDD}_s - \text{transit}_s
$$

$$
\text{legal departure window}_s =
[\max(\text{release}_s,\ \text{deadline}_s-\text{max early days}),\ \text{deadline}_s]
$$

A shared booking needs a departure in the intersection of every member's window. Each member's arrival is checked independently.

**Example 1 — old logic could reject a legal combination:** A is due 10 August with zero transit; B is due 12 August with two days' transit. Both have a latest departure of 10 August. With one day maximum early delivery, they can be compatible despite a two-day raw RDD spread.

**Example 2 — equal RDDs do not guarantee compatibility:** A and B are both due 10 August; A has zero transit and B two days. With one day maximum early arrival and sufficiently early releases, A's window is 9–10 August and B's is 7–8 August. They cannot share. Merely checking that neither arrival is late could overlook A arriving too early.

Candidate prefiltering now uses transit-adjusted departure deadlines, and report hard checks also enforce each member's early bound.

### 9.2 Added: explicit dated availability

Optional dated calendar fields: `calendar_type`, `date`, `is_open`, `ship_to`.

- Dispatch rules act on departure date and are global.
- Receiving rules act on each member's arrival date and may be global or customer-specific.
- Explicit closures remove dates; explicit openings can provide exceptions.
- No surviving date in an otherwise valid window produces `calendar_unavailable`, distinct from `past_due`.
- Calendar checks also apply to case-priced solos.

### 9.3 Added: recurring weekly availability

Optional weekly fields: `calendar_type`, `ship_to`, `weekday`, `is_open`, `start_date`, `end_date`.

Full weekday names are required. Both date bounds are mandatory and inclusive. Overlapping ranges for the same rule type/scope/weekday fail, even if they repeat the same open/closed value. Different scopes may overlap.

Receiving precedence is:

1. Dated customer-specific rule.
2. Dated global rule.
3. Weekly customer-specific rule.
4. Weekly global rule.
5. Open when no rule applies.

Dispatch uses the global levels only. Thus an explicit dated opening can reopen a weekly closure. Calendar rules are immutable snapshots rather than global mutable state.

### 9.4 Date-selection behavior

- Without a fleet cap: keep the latest feasible calendar-open date for a shipment/set.
- With a fleet cap: keep every feasible calendar-open date so the optimizer can distribute counted trucks across days.
- Calendars only filter the release/transit/early window; they never authorize late delivery or departure before release.
- Transit is still **elapsed calendar days**, not business-day arithmetic.
- No Sunday closure, Saturday dispatch policy or GI-based rule is inferred automatically.

**Evidence:** [trip_planning_old/source/candidates.py](trip_planning_old/source/candidates.py), [source/candidates.py](source/candidates.py), [source/retrospective.py](source/retrospective.py), [tests/test_pending_fixes.py](tests/test_pending_fixes.py), [tests/test_weekly_calendar.py](tests/test_weekly_calendar.py).

## 10. Fleet-cap planning corrections

The old version already imposed a daily truck constraint. The main defect was that the solver could only choose from an incomplete set of resource alternatives.

### 10.1 Corrected: cheapest is not always the only useful solo

**Old:** retain the cheapest solo quote.  
**Current with a cap:** retain nondominated cost/truck-count choices, including mixed-type fleets and permitted case pricing.

Illustrative choices for a shipment:

| Alternative | Freight cost | Counted trucks | Why it can matter |
|---|---:|---:|---|
| Two small trucks | 1,000 THB | 2 | Cheapest unconstrained fleet |
| One larger truck | 1,200 THB | 1 | Leaves one vehicle available under a cap |
| Permitted carrier case service | 1,300 THB | 0 | Does not consume modeled solo fleet under the current accounting convention |

The old cheapest-only candidate cannot express the second or third tradeoff when the first is cheaper. The current frontier keeps useful alternatives. It is not a new surcharge; it exposes legal decisions to the existing model.

### 10.2 Corrected: do not prune useful sharing under a cap

Without a cap, a shared booking that costs at least its members' solos can be discarded. Under a cap, it may consume fewer vehicles and enable more shipments to be served. The current generator disables that cost-only pruning in capped mode.

### 10.3 Corrected: keep alternative dates

The old generator used one departure date. The new generator expands legal dates under a cap, including earlier permitted dates. This avoids creating artificial shortages on the latest date when an earlier departure could serve the demand legally.

### 10.4 Resource semantics retained and clarified

- Truck-priced solo fleet: counts its fleet vehicles.
- Case-priced solo: counts zero modeled trucks.
- Shared booking: counts one modeled truck, including a case-priced shared booking.
- The cap is total counted trucks per departure date, not daily supply by truck type or carrier.
- Counted trucks are not a complete measure of the carrier's real LTL network vehicles.

These conventions need business approval. A cap of zero is supported, but it does not mean every service must disappear: a permitted case-priced solo may consume zero modeled trucks.

**Evidence:** [source/candidates.py](source/candidates.py), [source/baseline_cost.py](source/baseline_cost.py), [source/trip_model.py](source/trip_model.py), [tests/test_pending_fixes.py](tests/test_pending_fixes.py), [tests/test_retrospective.py](tests/test_retrospective.py).

## 11. Objective, solver validation and fallback

### 11.1 Same mathematical family, stronger capped penalty

Both versions minimize freight plus early/upgrade preferences and penalties for unplanned shipments, subject to exact cover and optional daily fleet capacity.

Without caps, the penalty remains one THB above the sum of feasible solo costs. With caps, the current penalty uses an upper bound derived from the maximum candidate cost containing each shipment, plus one. This remains valid when a resource-saving candidate costs more than unconstrained solos.

For a proven solution, capped priority is:

1. **Fewest unplanned shipments.**
2. Lowest freight cost plus bounded preferences among equal-coverage solutions.

This coverage-first intent was already present in the old model; the penalty was strengthened to support the expanded candidate space. It is **shipment-count-first**, not cases-, tonnes-, revenue- or priority-customer-first.

### 11.2 Default tie formula retained; custom bounds enforced

The default weights were already computed globally before decomposition:

$$
e=g=\frac{0.5}{1+n(\text{max early days}+1)}
$$

The new code additionally rejects custom weights whose maximum aggregate contribution reaches one THB. Exact fractional-baht costs are still used and reported. Preferences cannot overturn a full-baht cost difference, but **may outweigh a sub-baht difference**. The model was not converted into strict lexicographic minimization down to the last satang.

### 11.3 Added: candidate and incumbent validation

| Check | Old | Current |
|---|---|---|
| Duplicate shipment IDs | Checked | Retained |
| Exact selected partition | Checked | Retained |
| Candidate IDs/member validity/cost/resource counts | Limited checks in downstream paths | Explicit pre-solve validation |
| Solver vector shape and finite values | No comprehensive check | Checked |
| Near-integral binary values and bounds | Selected using a threshold | Checked before rounding/selection |
| Constraint activity after rounding | No general incumbent matrix check | Checked for cover and fleet constraints |
| Daily fleet use after selection/fallback | Constraint supplied to solver | Independently rechecked |

### 11.4 Added: safe, explicitly nonoptimal fallback

Old trip-model behavior raised an error when there was no usable incumbent. Current behavior can use a known feasible construction:

- Without caps: available validated solos plus unavoidable no-candidate leftovers.
- With caps: start from all leftovers, greedily add nonoverlapping feasible bookings within daily resources.

Fallback status is explicitly prefixed `fallback_`, and optimality is not claimed. A feasible greedy fallback does not guarantee maximum coverage or minimum cost. A valid time-limited/fallback plan may exit successfully; users must inspect statuses/gaps, not just process exit code.

### 11.5 Fleet-pricing solver safety

The separate fleet pricer already used enumeration up to 2,000,000 count combinations and otherwise a fixed 30-second, zero-requested-gap MILP. These thresholds are **retained**, not new performance guarantees.

Current changes add:

- Optional maximum fleet truck count for frontier generation.
- Finite/integral/bounded count validation and actual covering-capacity checks.
- `FleetMix.status` and `FleetMix.gap`, propagated into candidate/baseline reporting.
- A known covering pure-type fleet fallback when available, clearly nonoptimal.
- An error instead of guessing if no validated fallback exists and infeasibility is not proven.

An optimal trip selection over approximate candidate prices does not prove a globally exact freight optimum. Nonoptimal candidate/baseline pricing is visible through warnings and metadata.

**Evidence:** [trip_planning_old/source/trip_model.py](trip_planning_old/source/trip_model.py), [source/trip_model.py](source/trip_model.py), [source/baseline_cost.py](source/baseline_cost.py), [tests/test_trip_model.py](tests/test_trip_model.py), [tests/test_pending_fixes.py](tests/test_pending_fixes.py).

## 12. Pricing and baseline changes

### 12.1 Commercial formulas mostly retained

The upgrade did not replace max-rate multi-drop pricing, cheaper-of-two LTL, handling bases or truck-price ties with a new approved tariff. The main pricing additions are permissions, safer numeric handling and solver-quality visibility.

The phrase **“LTL rider is free”** is now explained more carefully: on an FTL-containing shared booking there is no separate LTL case bill, but the rider's destination can still raise the maximum trip rate or handling. That is a clarification of how the formula works, not a universal zero-incremental-cost promise.

### 12.2 Baselines that already existed

The attached old baseline module already computed:

- All shipments as independently priced truck fleets.
- LTL case/FTL fleet mix.
- Cheaper-of-two solo cost and FTL/LTL subtotals.
- Fleet counts, aggregate utilization and LTL below/above/equal-fleet comparisons.
- A standalone baseline writer/CLI.

What was missing was their integration into the normal six-file plan bundle, plus the new comparisons/quality/scope controls below. It would be inaccurate to say baseline calculations were invented entirely in the current version.

### 12.3 Added or corrected baseline behavior

| Change | Meaning |
|---|---|
| Workbook-rule comparator | Adds capacity-first vehicle selection matching the workbook formula, with separate detail/summary/savings fields |
| Permission-aware comparison | No comparison against a price the customer is explicitly forbidden to use |
| Effective-date grouping | Approved RDD splits/merges are reflected in baseline shipments |
| Pricing quality | Fleet status/gap and nonoptimal-pricing counts are exposed |
| Partial all-truck scope | `all_trips_unpriced_shipments` identifies when all-truck totals are not full coverage |
| Median correction | Even-sized above-fleet case counts use the proper midpoint median instead of the old upper-middle observation |
| Plan detail publication | Baseline shipment and summary tables are included in each current plan |
| Multiple solo alternatives | Baseline calculation does not double-count capped candidates across dates/resource choices |

The workbook-rule comparator is a **formula comparator**, not a physically guaranteed or invoice-derived execution baseline. Its large-load fallback intentionally follows the workbook's 6W weight-based rule, which ignores m³; the planner's own feasible bookings still check aggregate kg/m³.

### 12.4 Savings scope and suppression

The old report already suppressed ordinary savings for dateable fleet-limited leftovers. Current logic retains that safeguard and extends it to calendars and new comparison fields:

- `baseline_cost`: permitted unconstrained solos for dateable shipments; calendar-blocked demand stays included.
- `baseline_cost_all_built`: also retains past-due built shipments.
- `saving_thb` / `saving_percent`: blank if any otherwise-dateable built shipment is unplanned, including calendar closures or caps.
- Workbook-rule savings: blank if any built shipment is unplanned or any workbook-rule comparison is unpriced.
- Under caps, full coverage may legitimately cost more than unconstrained solos; the old unconditional selected-solo cost check is no longer applied to that mode.
- Without caps, the solo bound is interpreted consistently with the documented whole-baht preference resolution.

**Evidence:** [trip_planning_old/source/baseline_cost.py](trip_planning_old/source/baseline_cost.py), [source/baseline_cost.py](source/baseline_cost.py), [source/reporting.py](source/reporting.py), [tests/test_baseline_cost.py](tests/test_baseline_cost.py).

## 13. Reports and exclusion changes

### 13.1 Normal plan bundle: six CSVs → ten CSVs plus JSON

| Artifact | Old normal plan | Current normal plan |
|---|---|---|
| Booking/trip table | Yes | Retained; optional PO list added |
| Booking-to-shipment table | Yes | Retained |
| Booking-to-order table | Yes | Adds PO and original/effective-date audit |
| Exclusions | Yes | Adds customer identity/location, PO and date audit |
| Summary | Yes | Adds workbook comparison and richer status/scope interpretation |
| Flags | Yes | Adds PO/master-provenance and solver/pricing quality warnings |
| Eligible shipment table | Not written by normal planner | Included, with resolved permissions |
| Baseline shipment detail | Separate baseline writer existed | Included in normal plan |
| Baseline summary | Separate baseline writer existed | Included in normal plan |
| Missing-rate customer rollup | No | Added |
| Run metadata JSON | No | Added |

The exact table contract is centralized in `OUTPUT_TABLES` in [source/reporting.py](source/reporting.py). This same collection is used for hashes/publication checks so the new tables receive the same safeguards as the original ones.

### 13.2 Main field additions

| Report | Important additions |
|---|---|
| Booking | `po_numbers` |
| Order assignment | `po_numbers`, `original_rdd`, `effective_rdd`, `override_reason` |
| Exclusion | `ship_to`, `customer_name`, `province`, `po_numbers`, original/effective RDD and reason |
| Shipment | `allow_truck_pricing`, `allow_case_pricing`, `allow_sharing` |
| Baseline detail | `fleet_status`, `fleet_gap`, `sheet_truck`, `sheet_cost`, `sheet_basis` |
| Summary | Workbook-rule cost/saving fields alongside existing coverage/cost/truck/status metrics |

### 13.3 New actionable missing-rate rollup

The rollup contains:

`ship_to`, `customer_name`, `province`, `orders`, `cases`, `kg`, `m3`, `first_rdd`, `last_rdd`.

It groups **only exclusions whose reason is `no_rate`**, counts distinct normalized orders, totals demand and uses effective RDD for the date range. It does not mix in zero quantity, service restrictions, calendars or fleet shortages. A no-match result still has the correct headers.

Enrichment uses **all order demand**, not only eligible shipment lines. Consequently the rows most likely to need attention—no-rate/zero-quantity/excluded orders—do not lose customer context merely because they never entered the MIP.

### 13.4 Reason codes

| Code | Old/current | Meaning |
|---|---|---|
| `no_master_customer` | Retained | Missing/blank customer identity in the contract |
| `zero_quantity` | Retained | Zero-case order under existing exclusion precedence |
| `no_rate` | Retained | No rate row for the customer |
| `truck_type_not_offered` | Retained | No usable existing pricing/vehicle route |
| `service_not_permitted` | Added | Explicit pricing restrictions leave no actually priced permitted branch |
| `past_due` | Retained | Release/transit/RDD leaves no legal date window; not a comparison with today's date |
| `calendar_unavailable` | Added | A nonempty legal date window has no open dispatch/receiving date |
| `fleet_limit` | Retained | Open-date demand left out in capped planning; fallback/time-limit leftovers do not prove global impossibility |

Every order still ends in exactly one selected booking or one exclusion, and quantities reconcile. There is still no oversized exclusion simply for exceeding one vehicle: a solo fleet can use multiple vehicles.

### 13.5 Bookings are not physical trucks

The old code already allowed fleet solos and zero-count case solos. The documentation now makes their implication explicit:

- One report row represents one selected **booking**, not necessarily one physical truck.
- A solo booking may represent several trucks or a case-priced carrier service with zero modeled trucks.
- Shared bookings use one modeled truck.
- Aggregate utilization excludes case-priced solos and does not estimate the carrier's LTL vehicle utilization.

**Evidence:** [source/reporting.py](source/reporting.py), [tests/test_rate_gaps.py](tests/test_rate_gaps.py), [specification.md](specification.md).

## 14. Scenario analysis

### 14.1 Added orchestration, not a second optimization model

[source/scenarios.py](source/scenarios.py) calls the same planner repeatedly. A JSON manifest names the baseline and scenarios, with optional config changes, explicit truck-capacity overrides and assumption notes.

Each scenario starts from **TripConfig defaults independently**, not from the baseline's settings or the previous scenario. Required/optional tables are copied so one scenario cannot mutate the next one's inputs.

### 14.2 Validation and overlays

- Require a named included baseline and a nonempty scenario list.
- Reject unknown/duplicate JSON keys and nonfinite numbers.
- Validate safe, case-insensitively unique names; reject traversal/Windows device names.
- Capacity overlays must target an existing supported truck type and supply at least one positive finite numeric capacity.
- Unspecified capacity fields retain the original value.
- Validate all overlays before any solve.
- No 6W or 18W capacity is guessed.
- Every run/comparison is labeled `assumed_unapproved`.

### 14.3 Three different savings concepts

| Measure | What it compares | Important limitation |
|---|---|---|
| Ordinary planner saving | Dateable permitted solos versus that scenario's plan | Suppressed for dateable leftovers |
| Matched-solo saving | Only that scenario's served shipments as solos versus its plan | Can remain present with partial coverage; not a full-demand saving |
| Cross-scenario saving/delta | Named baseline scenario versus another scenario | Populated only when served scope matches |

Cross-scenario comparability requires identical normalized planned order IDs, each order's effective RDD, and its original cases/kg/m³ coverage. Equal aggregate totals or equal shipment IDs are insufficient.

`scope_comparable=true` does **not** mean all input demand was served. Both runs can exclude the same orders and still be comparable on their shared served scope.

- `cost_delta_vs_baseline` = scenario cost minus baseline cost; negative means cheaper.
- `saving_vs_baseline_thb` = baseline cost minus scenario cost; positive means cheaper.
- Percentage saving is blank when baseline cost is zero.
- Departure changes and truck-mix changes are reported separately; departure can change without changing effective-RDD scope.

### 14.4 Reference set and publication

[scenarios/reference.json](scenarios/reference.json) contains five unapproved cases:

| Name | Change from defaults |
|---|---|
| `transit0` | Zero-transit baseline |
| `transit1` | One elapsed transit day |
| `transit2` | Two elapsed transit days |
| `cap8` | Maximum eight counted trucks per departure date |
| `cap4` | Maximum four counted trucks per departure date |

There are no capacity replacements in this reference set. The complete five-scenario August batch has **not** been executed as part of the documented update/report; no comparative numbers are invented here.

The runner stages all individual plan bundles plus the root comparison/metadata, then publishes into a new scenario root. Existing destinations and symlink/junction paths are refused. Any scenario/input/publication failure prevents a partially published scenario set.

**Evidence:** [source/scenarios.py](source/scenarios.py), [scenarios/reference.json](scenarios/reference.json), [tests/test_scenarios.py](tests/test_scenarios.py).

## 15. Audit metadata and safe publication

### 15.1 Added run attribution

The current metadata records:

- Unique run ID and UTC creation time.
- Resolved configuration, derived weights and leftover penalty.
- Original input-table hashes and separately labeled effective-order hash.
- Optional policy presence and normalized calendar rules/precedence.
- Source-file hashes and Git revision when available.
- Python, NumPy, pandas, SciPy and embedded HiGHS versions when exposed.
- Validation outcome/errors/warnings and solver component statuses/gaps.
- Baseline fleet-pricing status/gap and labeled comparison scope.
- Output-table hashes.
- CLI options and raw recognized-input/config hashes for CLI runs.

This snapshot is encoded independently of later caller mutation. It makes “which inputs/settings/code produced this?” answerable, but it is not a business signature or a guarantee of bit-identical solver behavior on every environment.

### 15.2 Before versus after publication

| Situation | Old normal planner | Current behavior |
|---|---|---|
| Output directory already contains a run | Created/reused destination and wrote directly | Refuses publication if any managed plan output filename already exists; choose a fresh run directory |
| Hard checks fail | CLI wrote reports before testing `result.ok` | Invalid plan is refused before valid publication |
| Serialization fails halfway | Earlier files could already be replaced | Serialize/stage first |
| Tables mutate after validation | No stored table-hash binding | Validate against stored output hashes before publication |
| Input files change during a CLI run | No before/after binding | Recognized input changes/additions/removals are detected |
| New directory publication | Multiple direct file writes | Rename staged directory into place |
| Existing workbook-build replacement | Builder absent from attached source | File replacements with backup/rollback on I/O failure |
| Brief Windows lock | No dedicated bounded handling | At most five attempts for lock/permission codes 5/32/33; persistent/other failures propagate |

**Limits:** hash checks are point-in-time checks, not file locks. Replacing several files in an existing build is not a filesystem-wide transaction. Readers should consume completed run directories, not watch a build midway through replacement. No retry silently merges/overwrites a concurrent run.

For precision, the normal plan writer's immutable check is against its managed output filenames, not a blanket prohibition on every pre-existing directory. An empty directory, or one containing only unrelated names, can be accepted by the writer; this uses the existing-directory file-install path rather than a single new-directory rename. The scenario runner is stricter and refuses an already existing scenario root. A fresh destination is the recommended practice for both.

**Evidence:** [source/provenance.py](source/provenance.py), [source/publication.py](source/publication.py), [source/plan_trips.py](source/plan_trips.py), [source/reporting.py](source/reporting.py), [tests/test_publication_locks.py](tests/test_publication_locks.py).

## 16. Tests and verification evidence

### 16.1 Exact inventory comparison

| Inventory measure | Old snapshot | Current snapshot |
|---|---:|---:|
| Python source files, including initializer | 9 | 15 |
| Test modules matching the test naming convention | 4 | 13 |
| Top-level `def test_...` definitions | 20 | 153 |
| Test helper | Present | Content-identical |
| Pytest configuration | Present | Content-identical |

**Definitions are not executed test cases.** Parameterization expands a definition into multiple cases, and fixture-dependent tests may skip. The recorded **417 passed** result is a historical execution record, not a count inferred from 153 definitions and not a test rerun for this report. There is no newly executed old-suite result to compare against it.

### 16.2 Per-module changes

| Old test module | Current test module | Definitions old → current | Status / coverage |
|---|---|---:|---|
| [trip_planning_old/tests/test_candidates.py](trip_planning_old/tests/test_candidates.py) | [tests/test_candidates.py](tests/test_candidates.py) | 3 → 3 | Content-identical inherited candidate tests |
| [trip_planning_old/tests/test_pricing.py](trip_planning_old/tests/test_pricing.py) | [tests/test_pricing.py](tests/test_pricing.py) | 9 → 9 | Content-identical inherited pricing tests |
| [trip_planning_old/tests/test_sample_plan.py](trip_planning_old/tests/test_sample_plan.py) | [tests/test_sample_plan.py](tests/test_sample_plan.py) | 2 → 3 | Expanded real-data/output acceptance |
| [trip_planning_old/tests/test_trip_model.py](trip_planning_old/tests/test_trip_model.py) | [tests/test_trip_model.py](tests/test_trip_model.py) | 6 → 6 | Modified tests; original core model coverage retained/adjusted |
| Not present | [tests/test_baseline_cost.py](tests/test_baseline_cost.py) | 0 → 11 | Fleet/baseline calculations and comparisons |
| Not present | [tests/test_input_validation.py](tests/test_input_validation.py) | 0 → 17 | Malformed demand/config/rates/identities and contract handling |
| Not present | [tests/test_pending_fixes.py](tests/test_pending_fixes.py) | 0 → 23 | Cap alternatives, transit correctness, solver/publication/provenance regressions |
| Not present | [tests/test_va05_adapter.py](tests/test_va05_adapter.py) | 0 → 4 | Workbook mapping, diagnostics and raw-workbook acceptance |
| Not present | [tests/test_retrospective.py](tests/test_retrospective.py) | 0 → 32 | Permissions, approved dates, dated calendars, audit and immutability |
| Not present | [tests/test_weekly_calendar.py](tests/test_weekly_calendar.py) | 0 → 19 | Weekly schemas, overlaps, scope, reopening and precedence |
| Not present | [tests/test_rate_gaps.py](tests/test_rate_gaps.py) | 0 → 4 | Exclusion enrichment, rate rollup, effective dates and publication hashes |
| Not present | [tests/test_scenarios.py](tests/test_scenarios.py) | 0 → 19 | Manifest, overlays, served-scope comparisons, actual CLI orchestration and failures |
| Not present | [tests/test_publication_locks.py](tests/test_publication_locks.py) | 0 → 3 | Parameterized Windows retry, collision and propagation checks |

The old optional August test only checked CLI success when its local extract existed. Current acceptance asserts much more of the exact August reference and includes raw-workbook acceptance. Tests needing unavailable fixtures may skip; the old attached folder does not itself contain an August extract/output bundle.

### 16.3 Existing verification record

[specification.md](specification.md) records **417 tests passed in 348.81 seconds on 31 October 2026**, with trip-planning Python compilation clean. Ruff/Mypy were unavailable on PATH. This report preserves that record without presenting it as newly executed validation.

Passing synthetic rules does not prove those rules are operationally approved, and an exact August regression does not prove performance on arbitrary future demand.

## 17. What the saved results actually prove

### 17.1 Sample inputs and outputs

All four sample contract files in [trip_planning_old/inputs/sample](trip_planning_old/inputs/sample) and [inputs/sample](inputs/sample) have identical bytes. All six saved sample-plan files in [trip_planning_old/outputs/sample_plan](trip_planning_old/outputs/sample_plan) and [outputs/sample_plan](outputs/sample_plan) are also byte-identical.

Fresh reads of the stored summaries show:

| Metric | Old saved sample | Current saved sample |
|---|---:|---:|
| Input orders | 9 | 9 |
| Input cases / kg / m³ | 47 / 1,770 / 4.3 | 47 / 1,770 / 4.3 |
| Built shipments / candidates | 5 / 5 | 5 / 5 |
| Planned orders / cases | 5 / 38 | 5 / 38 |
| Excluded orders / cases | 4 / 9 | 4 / 9 |
| Freight cost | 1,164 THB | 1,164 THB |
| Dateable-solo baseline | 1,324 THB | 1,324 THB |
| Saving | 160 THB | 160 THB |
| Bookings / shared bookings / counted trucks | 3 / 1 / 1 | 3 / 1 / 1 |

This proves the **stored examples are identical**, not that both programs were freshly run and produced identical bytes today. Current tests retain the hand-verified sample arithmetic; current source would now publish a richer bundle to a new destination.

### 17.2 August reference, freshly read from saved tables

Source: [outputs/plan_aug2025/summary.csv](outputs/plan_aug2025/summary.csv), with booking detail in [outputs/plan_aug2025/trips.csv](outputs/plan_aug2025/trips.csv).

| Metric | Saved August result |
|---|---:|
| Input orders | 1,436 |
| Input cases | 117,745 |
| Input kg | 702,897.2828 |
| Input m³ | 1,841.290246 |
| Eligible shipments | 329 |
| Generated candidates | 1,512 |
| Planned orders | 1,357 |
| Planned cases / kg / m³ | 104,805 / 606,228.7206 / 1,599.834224 |
| Excluded orders | 79: 31 no-rate + 48 zero-quantity |
| Excluded cases / kg / m³ | 12,940 / 96,668.5622 / 241.456022 |
| Plan cost | 945,946.31875 THB |
| Dateable-solo baseline | 1,100,354.51875 THB |
| Modeled saving versus solos | 154,408.2 THB / 14.032587% |
| Workbook-rule baseline | 1,117,274.51875 THB |
| Modeled saving versus workbook rule | 171,328.2 THB / 15.334477% |
| Selected bookings / multi-shipment bookings | 240 / 58 |
| Counted trucks | 104: 61 × 4W, 13 × 6W, 30 × 10W |
| Aggregate utilization kg / m³ | 69.7531% / 68.3696% |
| Reported solve status / maximum gap | Optimal / 0 |

### 17.3 Three corrections to common interpretations

1. **341 is not the eligible shipment count.** It is the positive-quantity raw ship-to/RDD grouping count before rate eligibility. Twelve no-rate groups leave 329 eligible shipments.
2. **240 bookings are not 240 trucks.** The saved booking table has 96 truck-priced fleet bookings and 144 case-priced carrier bookings; the fleet bookings account for 104 modeled trucks.
3. **The saving is not for all input demand.** Planned cases are about 89.01% of input cases. The no-rate gap is 10.99% of cases and 13.11% of m³. These are scenario estimates for priced/served scope, not full-demand invoice savings.

The attached old folder has no comparable stored full-August plan. Therefore this report does **not** say the new program improved an old August cost by 154,408.2 THB. That figure is plan-versus-solo saving, not measured software-version delta.

### 17.4 Historical schema warning

The saved current August folder contains **nine CSVs plus metadata**, not the newest ten-CSV contract. Its exclusions lack customer enrichment/date audit, its order assignments lack the new date audit, and its shipment table lacks permission columns. The missing-rate rollup has not been retroactively added.

Likewise, the saved current sample folder still contains only the historical six CSVs. This is expected: existing artifacts were deliberately preserved. A fresh current run into a new directory is required to obtain the newest schema. For the August no-rate demand, that new rollup would not be header-only merely because the old saved folder lacks it.

## 18. Migration and compatibility checklist

### 18.1 What existing users can retain

- The four required contract tables and legacy planner CLI concepts.
- Existing default settings and compatibility modes.
- Existing pair overrides and solo-only settings.
- Existing `plan(orders, customers, rates, trucks, cfg, overrides)` call pattern.
- Existing absence of optional policies: missing permission/calendar/approval inputs preserve default behavior.

### 18.2 What callers and report consumers must review

| Check | Required action |
|---|---|
| New optional API inputs | Use keyword-only `planning_calendar`, `delivery_date_overrides`, `weekly_calendar` when needed |
| Manual dataclass construction | Review direct `PlanResult` construction and candidate/result assumptions; expanded data structures are not guaranteed positional drop-ins |
| CSV consumers | Read columns by name; tolerate/add fields and consume new report tables deliberately |
| Table count assumptions | Do not hardcode six plan CSVs or confuse nine builder artifacts with ten plan CSVs |
| Destination reuse | Choose a fresh plan/scenario destination; do not depend on old overwrite behavior |
| Previously tolerated values | Clean boolean/fractional/string integer configuration and malformed original dates/prices before migration |
| Capped optimization | Expect more candidate choices and potentially changed coverage/dates/costs on constrained inputs |
| Solver success | Check `result.ok`, statuses, gaps and flags; exit zero alone does not mean optimal/approved |
| Result interpretation | Distinguish bookings from trucks, scope-specific savings from all-demand savings |
| Scenario configuration | Repeat intended settings in each scenario; overrides do not inherit the named baseline |
| Old saved files | Treat their schema as historical; do not rewrite them to look like a new run |

### 18.3 Recommended controlled adoption

1. Preserve the old snapshot and historical results.
2. Validate real input contracts; resolve fatal data errors first.
3. Run a fresh default plan and verify input/planned/excluded reconciliation and status.
4. Review the customer-level rate gap before interpreting savings as a full-demand opportunity.
5. Add only explicit, authorized permissions/availability/RDD adjustments.
6. Compare approved or clearly hypothetical alternatives using the named scenario runner.
7. Compare costs only with equivalent served scope and inspect solver quality.
8. Obtain operational/carrier approval before dispatching from the model.

## 19. Pending business decisions and software gaps

### 19.1 Capability exists; actual data/approval is still required

| Business item | Current support | Still pending |
|---|---|---|
| Missing destination rates | No-rate exclusions and customer rollup | Supply approved tariff/service for uncovered demand |
| Transit | Global/province settings and scenarios | Measured lane times; no distance-to-days inference |
| Truck capacities | Input table and explicit scenario overlays | Confirm 6W volume and supply both 18W capacities |
| Pricing formulas | Max-rate sharing, cheaper-of-two, two handling bases | Carrier/invoice confirmation; other formulas need separate implementation |
| Customer sharing/pricing permissions | Restriction columns and pair/solo-only rules | Approved customer policy values |
| Dispatch/receiving availability | Dated/weekly schedules and opening exceptions | Actual approved schedules/date ranges |
| Revised RDDs | Explicit order approval plus audit | Authorized revised date/reason, not automatic NLM interpretation |
| Fleet cap | Total counted trucks per departure date | Actual cap and agreed case-service resource semantics |
| SLA/early delivery | Warning-only SLA, configured early window | Business confirmation of allowed treatment/cost |

18W is **not hardcoded prohibited**: current August capacity is missing, so it is unusable by default. Explicit valid kg/m³ values and offered rates can enable it. No operational replacement capacities have been invented.

### 19.2 Not implemented; requires a separate feature specification

| Extension | Missing decisions/data |
|---|---|
| Live eligibility / rolling horizon | Planning date, status/block/rejection mappings, unknown-value policy, mixed-line/order rules and frozen bookings |
| Holding / force dispatch / minimum load | Deferral deadlines, forced-date meaning, thresholds, objective/coverage priorities and conflict behavior |
| Hub delivery | Customer-to-hub mapping, service completion, hub/onward rates and equivalent final-delivery cost scope |
| Business-day transit / appointments | Working-day counting, appointment windows, cutoffs and time zones |
| Physical pallets/loading | SKU-line pipeline, product completeness, pallet rounding/mixing, stacking and truck positions |
| Route sequencing / driver hours | Inter-customer distance/time, visit order, duration and execution rules |
| Detailed availability | Daily vehicle supply by type/carrier, warehouse/picking/dock constraints |
| Actual-spend validation / live EDI | Matching executed trips/invoices or EDI identifiers for the same demand period/scope |

No Sunday policy is inferred from historical GI dates. No hub is inferred from ANT/Rama II remarks. “Add Case (Only FTL)” is commercially ambiguous and is not converted into a guessed charge. Fractional pallet equivalents are not physical pallet counts. Historical Completed/Not delivered status is not automatically used to filter retrospective demand.

### 19.3 Analysis and acceptance still pending

- Execute and interpret the full five-scenario August reference set in a fresh root.
- Obtain real-policy/carrier review and validate a dispatch pilot.
- Reconcile against actual booked/invoiced freight with equivalent served scope.
- Consider a fast/slow test split as maintenance work; it is not a delivered model feature.

The detailed current pending register is maintained in [specification.md](specification.md) and summarized in [trip_planning_model.html](trip_planning_model.html).

## 20. Final assessment

The change is best described as **an expansion and hardening of the original planner**, not a replacement of its optimization approach.

### Delivered

1. A workbook ingestion/build path included in the current snapshot.
2. More complete and correct date/resource alternatives for heterogeneous transit and capped planning.
3. Explicit permissions, approved dates and dated/recurring availability.
4. Better solver validation and honestly labeled fallback/pricing quality.
5. Baselines integrated into plan outputs, workbook comparison and scope-safe savings.
6. Actionable missing-rate reporting and PO/customer/date traceability.
7. Independent named scenario analysis with safe comparisons.
8. Reproducible run metadata and safer output publication.
9. Expanded tests and documentation of implemented versus pending behavior.

### Not claimed

- A fresh old-versus-new full-August cost improvement.
- Recovery of no-rate demand without supplying rates.
- Approved operational policies, invoices or real dispatch authorization.
- Complete routes, physical loading, live order eligibility, holding/force dispatch or hub delivery.

**Bottom line:** the current version can express more of the real business restrictions and resource tradeoffs, detect more bad inputs/results, and explain its decisions much more clearly. The remaining work is a combination of supplying approved business data, running controlled analyses, and separately specifying genuinely new operational features.