# Mixed-Length Container Loading Optimization

## Overview

This model optimises the loading of pallets into one or more truck trailers (containers).
Each trailer has two longitudinal rows (left and right). Along each row the trailer is divided
into an endogenous sequence of "lots"; every lot corresponds to exactly one pallet position in
layer 1 and an optional stacked pallet in layer 2. Unlike a fixed-grid approach, each lot's
length equals the actual length of the pallet placed in it (read from `palletLengthDict`), so
pallets of different sizes naturally consume different amounts of floor space.

The model decides which pallets (full or partial) to assign to which
(container, layer, side, lot-slot) combinations in order to maximise a weighted fill score,
subject to physical feasibility, weight, height, stacking, and axle-balance constraints.

---

## Objective

Maximise the total weighted fill score across all containers.

Each placed pallet contributes a positive score determined by:
- A per-container priority factor (`container_priority`).
- A global incentive multiplier (`priorityIncentive`) applied to high-priority SKUs.
- For partial pallets, the score is scaled by the pallet's fractional fill value
  (`partialFractionValueForPartialPallet`).
- Priority pallets (identified via `skuPriorityIncentive` or their material mapping) receive
  the full `base x incentive` score; all others receive just `base`.
- Prior-loaded pallets (pallets already committed to a container before the optimisation run)
  also contribute an incentive score.
- A penalty term (`sku_penalty x sku_penalty_factor`) is subtracted for every unique SKU
  count that exceeds the allowed `unique_sku_n` limit.

---

## Constraints

### C1: Demand Cap
For each full pallet, the total number of times it is placed across all
(container, layer, side, lot) combinations must not exceed its available demand
(`fullPalletDemandByMaterialBatch`). The same rule applies to partial pallets.

### C2: At Most One Pallet Per (Layer, Lot)
Within any given (container, layer, side, lot-slot), at most one pallet may be placed
(full or partial combined).

### C3: Lot Occupancy Definition
A lot-slot on layer 1 is "occupied" (`occ = 1`) if and only if exactly one pallet is
assigned to it on layer 1. The occupancy indicator is linked directly to the sum of
layer-1 assignment variables.

### C4: Lot Length Definition
The continuous variable `col_len` for a lot-slot equals the length of the pallet placed
in layer 1 of that slot (zero if unoccupied). This ties the spatial length of the slot to
the physical pallet length.

### C5: Column Weight and Height Accumulation
`col_wt` accumulates the total weight of all pallets stacked in a lot (across all layers).
`stk_ht` accumulates the total stacked height across all layers.

### C6: Layer 2 Occupancy and Length Consistency
A layer-2 pallet may only occupy a lot that already has a layer-1 pallet (`occ2 <= occ`).
When layer 2 is occupied, its pallet length must exactly match `col_len` (the layer-1
length). This ensures stacked pallets are the same footprint.

### C7: No Stacking on Partial Layer-1 Pallets
If a partial pallet sits on layer 1 of a lot, no pallet may be placed on layer 2 of that
same lot.

### C8: Row Length Capacity
The sum of all lot lengths on a given (container, side) must not exceed the usable trailer
length `L`.

### C9: Cumulative Position Tracking (x_end)
`x_end[v,s,k]` is the running cumulative end position of lot k along the row.
- For lot 1: `x_end = col_len`
- For lot k > 1: `x_end = x_end[k-1] + col_len`

### C10: Front / Rear Zone Classification
Each lot is classified as "front" (`is_front = 1`) if its centre x-coordinate is less
than the axle split point, or "rear" otherwise. This is enforced via big-M linearisation
using the trailer length `L` as the big-M.

### C11: Door Zone Classification
Each lot is classified as "door" (`is_door = 1`) if its centre x-coordinate is within
`door_zone = 2 x min_pallet_length` of the rear door. Big-M linearisation is used.

### C12: Front Weight Extraction (w_front)
`w_front` captures `col_wt` when the lot is classified as front, and zero otherwise.
This is modelled with three linear inequalities (standard big-M product linearisation).

### C13: Total Vehicle Weight Cap
The sum of all column weights across both sides must not exceed `max_wt`.

### C14: Axle Weight Limits (optional)
When enabled (`max_front_wt` and `max_rear_wt` are both provided):
- Total front-zone weight <= `max_front_wt`
- Total rear-zone weight (= total weight minus front weight) <= `max_rear_wt`

### C15: Side-to-Side Weight Balance (optional)
When `enforceWeightBalancing` is active and `side_to_side_tolerance > 0`:
- Left-row total weight is within `(1 ± ss_tol)` times the right-row total weight.

### C16: Front-to-Back Weight Balance (optional)
When `enforceWeightBalancing` is active and `front_to_back_tolerance > 0`:
- Front total weight is within `(1 ± fb_tol)` times the rear total weight.

### C17: Height Limit per Lot
The stacked height of a lot must not exceed the interior trailer height `max_ht` minus
an allowance `load_allow_ht`. Lots in the door zone are subject to the (potentially
lower) door-height cap `containerHeightAtDoor - load_allow_ht`.

### C18: Crushing Prevention
For any occupied lot, the total weight on layer 1 must be greater than or equal to the
total weight on layer 2, preventing heavy pallets from being placed on top of lighter ones.

### C19: Compact Packing (optional)
When enabled (`enforce_compact_packing = yes`), lot k can only be occupied if lot k-1
is also occupied, ensuring pallets are loaded from the nose of the trailer outward without
leaving empty gaps.

### C20: SKU Tracking
`mtl_plt[v, mtl]` counts how many pallets of material `mtl` are loaded on container `v`.
`y_mtl_plt[v, mtl]` is a binary indicator that is 1 if at least one such pallet is loaded.
`mtl_n[v]` counts the number of distinct materials on container `v`.
`exceed_mtl_n` measures how far the maximum `mtl_n` across all containers exceeds the
allowed `unique_sku_n`.

### C21: Prior-Load Accounting
`y_prior_full[plt]` and `y_prior_prtl[plt]` are continuous variables bounded above by
the maximum number of prior-loaded units for each pallet. They are constrained to be no
greater than the total placed count across all containers, rewarding (via the objective)
the utilisation of pre-committed pallet slots.

### C22: Vendor Stacking Rules (optional)
When `vdr_stacking_rules` is enabled:
- Pallets with stacking flag `g2 = 1` or `g4 = 1` are forbidden from layer 2.
- Pallets with stacking flag `g3 = 1` or `g4 = 1` on layer 1 cannot coexist with any
  layer-2 pallet in the same lot.

---

## Input

### Containers
List of container / trailer identifiers.
- Field: `containerList` (list of any hashable IDs)

### Layers
Ordered list of layer identifiers (typically two: layer 1 = floor, layer 2 = stacked).
- Field: `layerList` (list, length >= 1)

### Full Pallets
List of identifiers for fully loaded pallets available for placement.
- Field: `fullPalletList` (list of any hashable IDs, may be empty)

### Partial Pallets
List of identifiers for partially loaded pallets available for placement.
- Field: `partialPalletList` (list of any hashable IDs, may be empty)

### Pallet Geometry
- `palletLengthDict` (dict: pallet_id -> float): Length of each pallet (inches or metres,
  consistent with the usable-length unit). Must be positive and finite for every pallet.
- `palletWeightDict` (dict: pallet_id -> float): Weight of each pallet.
- `palletHeightDict` (dict: pallet_id -> float): Height of each pallet.
- `palletWidthDict` (dict: pallet_id -> float, optional): Width used for volume (cube) 
  calculation in the loading instructions; defaults to 40 if missing.

### Pallet Demand
- `fullPalletDemandByMaterialBatch` (dict: pallet_id -> int): Maximum number of times
  each full pallet may be loaded. Defaults to 0.
- `partialPalletDemandByMaterialBatch` (dict: pallet_id -> int): Same for partial pallets.

### Trailer Dimensions
- `usableLength` / `usable_length` / `containerLength` / `container_length` /
  `trailerLength` / `trailer_length` (float, optional): Explicit usable interior length.
  If absent, inferred from `leftPositionList` and maximum pallet length.
- `containerHeight` (float): Maximum interior height. Default 9.0.
- `containerHeightAtDoor` (float, optional): Interior height at the door opening.
  Defaults to `containerHeight`.
- `max_wt` (float): Maximum gross vehicle weight.
- `load_allow_ht` (float, optional): Height allowance subtracted from both height caps.

### Axle and Balance Parameters
- `max_front_wt` (float, optional): Maximum weight on the front axle zone.
- `max_rear_wt` (float, optional): Maximum weight on the rear axle zone.
- `front_to_back_tolerance` (float, optional): Fractional tolerance for front/rear balance.
- `side_to_side_tolerance` (float, optional): Fractional tolerance for left/right balance.

### Position Geometry
- `leftPositionList` (list, optional): Ordered list of physical lot positions on the left
  row, used to derive the number of lots and the front/rear split.
- `frontPositionList` (set/list, optional): Subset of positions classified as "front".
- `max_plt_n` (int, optional): Total pallet capacity hint (both rows combined).
- `max_lyr_n` (int, optional): Maximum layer count hint.

### SKU and Material Data
- `materialList` (list, optional): List of material (SKU) identifiers.
- `palletMaterialDict` (dict: pallet_id -> material_id, optional): Maps each pallet to its
  material.
- `unique_sku_n` (int, optional): Maximum number of distinct SKUs allowed per container.
- `skuPriorityIncentive` (dict: material_id -> value, optional): High-priority SKU map.

### Objective Parameters
- `priorityIncentive` (float, optional): Incentive multiplier for high-priority pallets.
- `sku_penalty` (float, optional): Per-excess-SKU penalty in the objective.
- `partialFractionValueForPartialPallet` (dict: pallet_id -> float, optional): Fractional
  fill value for partial pallets (used as objective coefficient scale).
- `container_priority` (dict: container_id -> float, optional): Per-container priority
  multiplier; defaults to 1.0.

### Prior-Load Limits
- `max_prior_full` (dict: pallet_id -> float, optional): Upper bound on prior-loaded full
  pallets per pallet ID.
- `max_prior_prtl` (dict: pallet_id -> float, optional): Same for partial pallets.

### Packing Mode
- `enforce_compact_packing` (str/bool, optional): "yes"/"true"/"1" to enforce no gaps.
- `run_ocean` (bool): If True, weight-balancing and axle constraints are disabled.

### Vendor Stacking Flags (optional, used when `vdr_stacking_rules=True`)
- `stackFactorg2Pallet` (dict: pallet_id -> 0/1): Forbid layer-2 placement.
- `stackFactorg3Pallet` (dict: pallet_id -> 0/1): Forbid co-stacking with layer-2.
- `stackFactorg4Pallet` / `stackFactorq4Pallet` (dict: pallet_id -> 0/1): Both g2 and g3
  rules apply.

### Loading Instruction Fields (used in output only)
- `palletNumCasesDict`, `palletBatchDict`, `palletPlantDict`, `palletStackingGroupDict`,
  `palletMaterialDict`, `marketID` — metadata fields attached to each `LoadObj` output.

---

## Output

### solve() Return Dictionary
| Field | Type | Description |
|---|---|---|
| `status` | int | HiGHS/Gurobi status code (0 = optimal) |
| `is_optimal` | bool | True when status == 0 |
| `objective_value` | float or None | Maximum weighted fill score; None if no solution |
| `mip_gap` | float or None | Achieved MIP optimality gap |
| `runtime` | float | Wall-clock solve time in seconds |
| `message` | str | Human-readable status string |
| `usable_length` | float | Computed usable trailer length used by the model |
| `kmax` | int | Maximum number of lot-slots per row |

### generate_loading_instructions() Return
A sorted list of `LoadObj` objects, one per placed pallet, each carrying:
- `container`, `layer`, `bin_loc` (side), `sequence` (lot index)
- `palletID`, `material`, `batch`, `weight`, `height`, `cube`, `cases`
- `is_partial`, `pallet_type`, `stacking_group`, `shipTo`

### lot_rows() Return
List of dicts with per-lot geometry for diagnostics / results CSV:
`Container`, `Side`, `Lot`, `LotLength`, `X_end`, `X_center`, `Is_front`

---

## Test Scenarios

### T1: Minimal Single-Container, Single-Layer, Homogeneous Pallets
**Description**: One container, one layer, two identical full pallets that both fit.
Confirms basic assignment, demand cap, row-length constraint, and optimal fill.

**Input Data**:
- `containerList`: `["T1"]`
- `layerList`: `[1]`
- `fullPalletList`: `["P1", "P2"]`
- `partialPalletList`: `[]`
- `palletLengthDict`: `{"P1": 1.0, "P2": 1.0}`
- `palletWeightDict`: `{"P1": 100.0, "P2": 100.0}`
- `palletHeightDict`: `{"P1": 1.5, "P2": 1.5}`
- `fullPalletDemandByMaterialBatch`: `{"P1": 1, "P2": 1}`
- `usableLength`: `4.0`
- `max_wt`: `5000.0`
- `containerHeight`: `3.0`
- `leftPositionList`: `[1, 2]`
- `frontPositionList`: `[1]`
- `run_ocean`: `True` (disable balancing)

**Acceptance Criteria**:
- `is_optimal` is True
- Both P1 and P2 are placed (objective_value = 2.0 given default incentive = 1.0)
- Total lot lengths on each used row <= 4.0
- Each pallet appears at most once

### T2: Two-Layer Stacking with Length Match
**Description**: One container, two layers, two pallets of different lengths. Only the
pallet with matching length can be stacked on layer 2. Verifies C6 (length consistency)
and C18 (crushing prevention — layer-1 weight >= layer-2 weight).

**Input Data**:
- `containerList`: `["T2"]`
- `layerList`: `[1, 2]`
- `fullPalletList`: `["A", "B"]`
- `partialPalletList`: `[]`
- `palletLengthDict`: `{"A": 1.2, "B": 1.0}`
- `palletWeightDict`: `{"A": 200.0, "B": 100.0}`
- `palletHeightDict`: `{"A": 1.2, "B": 1.0}`
- `fullPalletDemandByMaterialBatch`: `{"A": 2, "B": 1}`
- `usableLength`: `3.0`
- `max_wt`: `5000.0`
- `containerHeight`: `3.0`
- `run_ocean`: `True`

**Acceptance Criteria**:
- `is_optimal` is True
- Any lot with a layer-2 pallet must have the same pallet type (same length) on layer 1
- Layer-1 weight >= layer-2 weight for all occupied lots

### T3: Demand Cap Enforcement
**Description**: One container, single layer, one pallet type with demand = 1. Two lot
slots are available but only one unit may be placed.

**Input Data**:
- `containerList`: `["T3"]`
- `layerList`: `[1]`
- `fullPalletList`: `["X"]`
- `partialPalletList`: `[]`
- `palletLengthDict`: `{"X": 1.0}`
- `palletWeightDict`: `{"X": 50.0}`
- `palletHeightDict`: `{"X": 1.0}`
- `fullPalletDemandByMaterialBatch`: `{"X": 1}`
- `usableLength`: `4.0`
- `max_wt`: `5000.0`
- `containerHeight`: `3.0`
- `run_ocean`: `True`

**Acceptance Criteria**:
- `is_optimal` is True
- Exactly 1 unit of pallet X is placed in total across both rows and both layers
- Objective value = 1.0

### T4: Infeasibility — Row Length Exceeded
**Description**: Demand requires more length than the trailer provides, making the model
infeasible.

**Input Data**:
- `containerList`: `["T4"]`
- `layerList`: `[1]`
- `fullPalletList`: `["Big"]`
- `partialPalletList`: `[]`
- `palletLengthDict`: `{"Big": 3.0}`
- `palletWeightDict`: `{"Big": 100.0}`
- `palletHeightDict`: `{"Big": 1.0}`
- `fullPalletDemandByMaterialBatch`: `{"Big": 5}` — 5 units x 3.0 = 15 m needed per row
- `usableLength`: `2.0` — only 2 m available per row
- `max_wt`: `5000.0`
- `containerHeight`: `3.0`
- `run_ocean`: `True`
- Force at least 1 unit placed: add a dummy constraint or rely on the model not finding
  a feasible solution that violates row length.

**Acceptance Criteria**:
- `is_optimal` is False OR `objective_value` reflects at most 0 placements
  (since no unit of "Big" fits within 2 m row length even once given kmax = 0)
- Model reports infeasible or zero placements

### T5: Side-to-Side Weight Balancing
**Description**: Two pallets of very different weights; the model must balance them across
left and right rows within the specified tolerance.

**Input Data**:
- `containerList`: `["T5"]`
- `layerList`: `[1]`
- `fullPalletList`: `["Heavy", "Light"]`
- `partialPalletList`: `[]`
- `palletLengthDict`: `{"Heavy": 1.0, "Light": 1.0}`
- `palletWeightDict`: `{"Heavy": 1000.0, "Light": 200.0}`
- `palletHeightDict`: `{"Heavy": 1.0, "Light": 1.0}`
- `fullPalletDemandByMaterialBatch`: `{"Heavy": 1, "Light": 1}`
- `usableLength`: `3.0`
- `max_wt`: `5000.0`
- `containerHeight`: `3.0`
- `side_to_side_tolerance`: `5.0` — very wide, just testing the constraint fires
- `run_ocean`: `False`
- `enforceWeightBalancing`: auto-set to True since run_ocean=False

**Acceptance Criteria**:
- `is_optimal` is True
- Left-row total weight is within (1 ± 5.0) of right-row total weight

### T6: Compact Packing
**Description**: With compact packing enabled, lots must be filled from the nose.

**Input Data**:
- `containerList`: `["T6"]`
- `layerList`: `[1]`
- `fullPalletList`: `["P"]`
- `partialPalletList`: `[]`
- `palletLengthDict`: `{"P": 1.0}`
- `palletWeightDict`: `{"P": 100.0}`
- `palletHeightDict`: `{"P": 1.0}`
- `fullPalletDemandByMaterialBatch`: `{"P": 1}`
- `usableLength`: `3.0`
- `max_wt`: `5000.0`
- `containerHeight`: `3.0`
- `enforce_compact_packing`: `"yes"`
- `run_ocean`: `True`

**Acceptance Criteria**:
- `is_optimal` is True
- The single placed pallet is in lot 1 (the first slot), not a later slot

---

## Out of Scope

- Two-dimensional bin packing (width placement is not modelled; rows are treated as
  independent one-dimensional strips).
- Multi-stop routing or sequencing of containers across routes.
- Continuous position variables for lot placement (the model uses ordered sequential lots,
  not free-floating x-coordinates for individual pallets).
- Real-time re-optimisation / rolling horizon.
- Heterogeneous trailer geometries per row (both rows share the same usable length L).
