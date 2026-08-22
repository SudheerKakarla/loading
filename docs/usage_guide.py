"""
VDR-Enhanced Container Loading Optimizer - Usage Guide
======================================================

This guide explains how to use the VDR-enhanced Gurobi model for container
loading optimization with advanced features for milk run routing and
sophisticated stacking rules.

TABLE OF CONTENTS
-----------------
1. Quick Start
2. Feature Comparison: Base vs VDR Model
3. Input Data Requirements
4. VDR-Specific Features Deep Dive
5. Configuration Guide
6. Output Files and Reports
7. Troubleshooting
8. Performance Tuning
9. Examples
10. FAQ

==============================================================================
1. QUICK START
==============================================================================
"""

from pathlib import Path
import sys

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import gurobipy as gp
from gurobipy import GRB
from loadmax.optimizer import ContainerLoadingOptimizerVDR

# BASIC USAGE EXAMPLE
def quick_start_example():
    """
    Minimal example to get started with VDR optimizer.
    """
    # 1. Prepare input data (must include VDR-specific fields)
    inputParams = {
        # Standard fields (same as base model)
        'marketID': 'VDR_EMEA',
        'containerList': [1, 2, 3],
        'layerList': [1, 2, 3, 4],
        'positionList': list(range(1, 25)),
        'fullPalletList': ['PLT001', 'PLT002', 'PLT003'],
        'partialPalletList': ['PLT_P001', 'PLT_P002'],
        # ... (many more standard fields)
        
        # VDR-SPECIFIC FIELDS (NEW!)
        'stackFactorg2Pallet': {'PLT001': 1, 'PLT002': 0, 'PLT003': 0},  # G2 indicators
        'stackFactorg3Pallet': {'PLT001': 0, 'PLT002': 1, 'PLT003': 0},  # G3 indicators
        'stackFactorg4Pallet': {'PLT001': 0, 'PLT002': 0, 'PLT003': 1},  # G4 indicators
        'palletStackingGroupDict': {'PLT001': 'G2', 'PLT002': 'G3', 'PLT003': 'G4'},
        'palletPlantDict': {'PLT001': 'Plant_A', 'PLT002': 'Plant_B', 'PLT003': 'Plant_C'},
        'plantPriorityDict': {'Plant_A': 1, 'Plant_B': 2, 'Plant_C': 3},
        'palletLocationDict': {'PLT001': 'Warehouse_1', 'PLT002': 'Warehouse_2', 'PLT003': 'Warehouse_1'},
        'skuPriorityIncentive': {'PLT001': 1, 'PLT002': 2, 'PLT003': 3},
    }
    
    # 2. Create Gurobi environment
    with gp.Env(empty=True) as env:
        env.setParam('OutputFlag', 1)  # Show solver output
        env.start()
        
        # 3. Create optimizer with VDR features enabled
        with ContainerLoadingOptimizerVDR(
            env=env,
            inputParams=inputParams,
            run_ocean=True,           # Ocean container mode
            time_limit=120,           # 2 minutes
            gap=0.01,                 # 1% optimality gap
            sku_penalty_factor=1.0,
            vdr_stacking_rules=True   # Enable G2/G3/G4 constraints
        ) as optimizer:
            
            # 4. Build and solve
            optimizer.build_model()
            optimizer.solve()
            
            # 5. Get results
            optimizer.print_solution_summary()
            solution = optimizer.get_solution()
            loading_instructions = optimizer.generate_loading_instructions()
            
            # 6. Use the results
            print(f"\nTotal pallets loaded: {len(loading_instructions)}")
            for instr in loading_instructions[:5]:  # Print first 5
                print(f"  Container {instr.container}, Pos {instr.position}, "
                      f"Layer {instr.layer}: {instr.material} "
                      f"({instr.stacking_group}) -> {instr.shipTo}")


"""
==============================================================================
2. FEATURE COMPARISON: BASE vs VDR MODEL
==============================================================================

┌─────────────────────────────────────────────────────────────────────────┐
│ Feature                    │ Base Model      │ VDR Model                │
├────────────────────────────┼─────────────────┼──────────────────────────┤
│ Stacking Rules             │ Binary (Y/N)    │ 4-Level (G1/G2/G3/G4)    │
│ Multi-Destination          │ No              │ Yes (Milk Run)           │
│ Position Weighting         │ Uniform         │ Distance-based           │
│ Priority Amplification     │ Linear          │ Cubic (³)                │
│ Warehouse Tracking         │ No              │ Yes (Location field)     │
│ Destination Tracking       │ No              │ Yes (ShipTo field)       │
│ Dynamic Time Limit         │ No              │ Yes (8+ vehicles)        │
│ MIP Gap Tolerance          │ 0% (optimal)    │ Configurable (1% default)│
│ Objective Complexity       │ Simple          │ Multi-term with weights  │
└────────────────────────────┴─────────────────┴──────────────────────────┘

WHEN TO USE EACH MODEL:
-----------------------

Use BASE MODEL when:
✓ Single destination (FTL - Full Truck Load)
✓ Simple stacking rules (all pallets similar)
✓ All pallets have equal priority
✓ No unloading sequence requirements
✓ Standard warehouse operations

Use VDR MODEL when:
✓ Multi-stop deliveries (milk run routes)
✓ Complex product compatibility (fragile, heavy, etc.)
✓ Unloading sequence is critical
✓ Need destination-aware placement
✓ VDR or similar customer requirements
✓ Want faster solves (1% gap acceptable)

==============================================================================
3. INPUT DATA REQUIREMENTS
==============================================================================
"""

# Complete input data structure example
COMPLETE_INPUT_PARAMS = {
    # =========================================================================
    # STANDARD FIELDS (Required by both base and VDR models)
    # =========================================================================
    
    'marketID': 'VDR_EMEA_001',  # Market/customer identifier
    
    # Container/Vehicle lists
    'containerList': [1, 2, 3],  # List of container IDs
    'layerList': [1, 2, 3, 4],   # Layers (typically 1-4)
    'positionList': list(range(1, 25)),  # Positions per layer (24 for 40ft)
    'sectionList': ['A', 'B', 'C', 'D'],  # Container sections
    
    # Pallet lists
    'fullPalletList': ['FP001', 'FP002', 'FP003'],
    'partialPalletList': ['PP001', 'PP002'],
    'materialList': ['SKU001', 'SKU002', 'SKU003'],
    
    # Position definitions
    'frontPositionList': list(range(1, 13)),   # Front half
    'rearPositionList': list(range(13, 25)),   # Rear half
    'leftPositionList': [1,3,5,7,9,11,13,15,17,19,21,23],   # Left side
    'rightPositionList': [2,4,6,8,10,12,14,16,18,20,22,24], # Right side
    'door_posn': [23, 24],  # Positions near door
    
    # Position metadata
    'positionSequenceDict': {position: position for position in range(1, 25)},  # Loading sequence
    'positionGroupBalanceCoefficientDict': {
        (position, section): 1.0
        for position in range(1, 25)
        for section in ['A', 'B', 'C', 'D']
    },
    
    # Pallet properties
    'palletWeightDict': {'FP001': 2500, 'FP002': 2000},  # lbs
    'palletHeightDict': {'FP001': 72, 'FP002': 68},      # inches
    'palletNumCasesDict': {'FP001': 144, 'FP002': 120},
    'palletMaterialDict': {'FP001': 'SKU001'},
    'palletBrandDict': {'FP001': 'Brand_A'},
    'palletBatchDict': {'FP001': 'BATCH001'},
    'palletDescriptionDict': {'FP001': 'Product Description'},
    'palletBatchDateDict': {'FP001': '2024-01-15'},
    'palletBinLocDict': {'FP001': 'A-12-3'},
    
    # Demand
    'fullPalletDemandByMaterialBatch': {'FP001': 5, 'FP002': 3},
    'partialPalletDemandByMaterialBatch': {'PP001': 2},
    
    # Fractional values for partial pallets
    'partialFractionValueForPartialPallet': {'PP001': 0.5, 'PP002': 0.3},
    
    # Footprint areas (if using footprint constraints)
    'fullPalletFootPrintArea': {'FP001': 13.33},  # sq ft (40"x48")
    'partialPalletFootPrintArea': {'PP001': 10.0},
    
    # Stacking factors (base model)
    'stackFactorPallet': {'FP001': 0, 'FP002': 1},  # 0=stackable, 1=not
    
    # Container properties
    'container_priority': {1: 3, 2: 2, 3: 1},  # Fill order (high first)
    'containerBrandDict': {1: ['Brand_A', 'Brand_B']},
    
    # Physical limits
    'max_plt_n': 12,          # Max pallets per layer
    'max_lyr_n': 4,           # Max layers
    'max_wt': 45000,          # Max weight (lbs)
    'max_front_wt': 20000,    # Max front axle weight
    'max_rear_wt': 25000,     # Max rear axle weight
    'containerHeight': 110,   # Container height (inches)
    'containerHeightAtDoor': 102,  # Height at door
    'load_allow_ht': 2,       # Loading allowance
    
    # Tolerances
    'front_to_back_tolerance': 0.05,   # 5%
    'side_to_side_tolerance': 0.03,    # 3%
    'fp_area_tolerance': 0.1,          # 10%
    
    # SKU management
    'unique_sku_n': 15,       # Target unique SKUs
    'max_sku_n': 20,          # Hard limit on SKUs per container
    'sku_penalty': 100,       # Penalty per excess SKU
    'sku_penalty_factor': 1.0,
    
    # Priority settings
    'priorityIncentive': 1000,
    'max_prior_full': {'FP001': 5},   # Max priority assignments
    'max_prior_prtl': {'PP001': 2},
    'brand_group': 'BrandGroup_A',
    'first_plt_n': 12,        # First position in center
    
    # Crushing limits (if using SKU-specific crushing)
    'crushingLimitByMaterial': {'SKU001': 5000},  # lbs
    
    # Misc
    'threshold40': 0.85,      # 85% utilization threshold (ocean)
    'utilThreshold': 0.90,    # 90% utilization threshold (truck)
    'enforce_compact_packing': 'No',
    'enforce_fp_tolerance': 'No',
    
    # =========================================================================
    # VDR-SPECIFIC FIELDS (Required when vdr_stacking_rules=True)
    # =========================================================================
    
    # Stacking group indicators (0 or 1)
    'stackFactorg2Pallet': {
        'FP001': 1,  # This is a G2 pallet (ground base)
        'FP002': 0,  # Not G2
        'FP003': 0,
        'PP001': 0,
        'PP002': 0,
    },
    
    'stackFactorg3Pallet': {
        'FP001': 0,
        'FP002': 1,  # This is a G3 pallet (no stack on top)
        'FP003': 0,
        'PP001': 0,
        'PP002': 0,
    },
    
    'stackFactorg4Pallet': {
        'FP001': 0,
        'FP002': 0,
        'FP003': 1,  # This is a G4 pallet (ground only, no stack)
        'PP001': 0,
        'PP002': 0,
    },
    
    # Stacking group names (for reporting)
    'palletStackingGroupDict': {
        'FP001': 'G2',
        'FP002': 'G3',
        'FP003': 'G4',
        'PP001': 'G1',  # Default
        'PP002': 'G1',
    },
    
    # Milk run: destination plant for each pallet
    'palletPlantDict': {
        'FP001': 'Plant_Amsterdam',
        'FP002': 'Plant_Rotterdam',
        'FP003': 'Plant_Utrecht',
        'PP001': 'Plant_Amsterdam',
        'PP002': 'Plant_Rotterdam',
    },
    
    # Milk run: delivery priority for each plant (1=first stop, higher=later)
    'plantPriorityDict': {
        'Plant_Amsterdam': 1,   # First stop
        'Plant_Rotterdam': 2,   # Second stop
        'Plant_Utrecht': 3,     # Last stop
    },
    
    # Warehouse location for each pallet (for tracking/reporting)
    'palletLocationDict': {
        'FP001': 'Warehouse_Venlo',
        'FP002': 'Warehouse_Tilburg',
        'FP003': 'Warehouse_Venlo',
        'PP001': 'Warehouse_Venlo',
        'PP002': 'Warehouse_Tilburg',
    },
    
    # SKU priority for milk run objective (higher = more important for early stop)
    'skuPriorityIncentive': {
        'FP001': 1,   # Low priority (can go deep in container)
        'FP002': 2,   # Medium priority
        'FP003': 3,   # High priority (should be near door)
        'PP001': 1,
        'PP002': 2,
    },
    
    # Container brand groups (for mixed cargo)
    'newBrandGroupDict': {
        'Brand_A': 'PalletType_Standard',
        'Brand_B': 'PalletType_Premium',
    },
    
    # Pallet number for tracking (optional)
    'palletNumber': {'FP001': 'PN001'},
    
    # Customer-material-batch to bin location mapping (for "Many" bin locations)
    'customerMaterialBatch_BinLoc_Dict': {
        'VDR_EMEA_001_SKU001_BATCH001_Full': ['A-12-3', 'A-12-4'],
        'VDR_EMEA_001_SKU002_BATCH002_Partial': ['B-05-1'],
    },
    
    # Priority: number of available containers for each pallet (advanced)
    'palletPriorityValue_numAvailableContainers': {'FP001': 3},
    'palletPriority_numAvailableContainers': {'FP001': 1},
}


"""
==============================================================================
4. VDR-SPECIFIC FEATURES DEEP DIVE
==============================================================================

4.1 STACKING GROUPS (G1/G2/G3/G4)
-----------------------------------
"""

STACKING_GROUP_GUIDE = """
G1: STANDARD STACKABLE
---------------------
- Most common category
- Can be placed on any layer
- Other pallets can be stacked on top
- No special restrictions

Example products:
  • Standard boxes
  • Canned goods
  • Non-fragile items
  • Typical warehouse inventory

Usage in code:
  stackFactorg2Pallet[plt] = 0
  stackFactorg3Pallet[plt] = 0
  stackFactorg4Pallet[plt] = 0
  palletStackingGroupDict[plt] = 'G1'


G2: GROUND BASE, STACKABLE UPON
-------------------------------
- MUST be on layer 1 (ground level)
- Other pallets CAN stack on top
- Typically heavy, stable products
- Forms stable base for lighter items

Example products:
  • Bottled water (cases)
  • Heavy machinery parts
  • Steel products
  • Cement bags

Usage in code:
  stackFactorg2Pallet[plt] = 1  # ← KEY
  stackFactorg3Pallet[plt] = 0
  stackFactorg4Pallet[plt] = 0
  palletStackingGroupDict[plt] = 'G2'

Constraints enforced:
  • no_stk_plt_g2[v,l,p] forced to 0 for l > 1 (not on upper layers)
  • Can have other pallets above (no restriction on l+1)


G3: NO STACK ON TOP
------------------
- CAN be on layer 1
- NO pallets can be stacked on top
- Typically fragile or temperature-sensitive

Example products:
  • Electronics
  • Glass products
  • Fresh produce
  • Pharmaceuticals (temp-sensitive)

Usage in code:
  stackFactorg2Pallet[plt] = 0
  stackFactorg3Pallet[plt] = 1  # ← KEY
  stackFactorg4Pallet[plt] = 0
  palletStackingGroupDict[plt] = 'G3'

Constraints enforced:
  • no_stk_plt_g3[v,l,p] + full_plt[v,l+1,p] + prtl_plt[v,l+1,p] <= 1
  • Nothing allowed on layer above if G3 below


G4: GROUND ONLY, NO STACK
-------------------------
- MUST be on layer 1 (ground level)
- NO pallets can be stacked on top
- Most restrictive category
- Typically very fragile or oversized

Example products:
  • Delicate machinery
  • Irregularly shaped items
  • Extremely fragile products
  • Oversized equipment

Usage in code:
  stackFactorg2Pallet[plt] = 0
  stackFactorg3Pallet[plt] = 0
  stackFactorg4Pallet[plt] = 1  # ← KEY
  palletStackingGroupDict[plt] = 'G4'

Constraints enforced:
  • no_stk_plt_g4[v,l,p] forced to 0 for l > 1 (ground only)
  • no_stk_plt_g4[v,l,p] + full_plt[v,l+1,p] + prtl_plt[v,l+1,p] <= 1


COMPATIBILITY MATRIX:
--------------------
┌──────────────┬────────────────────────────────────────────────────┐
│ Stacking     │ Can be placed on:                                   │
│ Group        │ Layer 1  │  Layer 2+  │  Can stack on top?         │
├──────────────┼──────────┼────────────┼────────────────────────────┤
│ G1 Standard  │    ✓     │     ✓      │         ✓ Yes              │
│ G2 Ground    │    ✓     │     ✗      │         ✓ Yes              │
│ G3 No Stack  │    ✓     │     ✓      │         ✗ No               │
│ G4 Most Rest │    ✓     │     ✗      │         ✗ No               │
└──────────────┴──────────┴────────────┴────────────────────────────┘

WHAT CAN GO ON TOP OF WHAT:
---------------------------
┌──────────────┬────────────────────────────────────────────────────┐
│ Bottom Layer │ Can place on top:                                   │
│ Contains:    │ G1    │  G2    │  G3    │  G4                       │
├──────────────┼───────┼────────┼────────┼───────────────────────────┤
│ G1           │  ✓    │   ✗*   │   ✓    │   ✗*                      │
│ G2           │  ✓    │   ✗*   │   ✓    │   ✗*                      │
│ G3           │  ✗    │   ✗    │   ✗    │   ✗                       │
│ G4           │  ✗    │   ✗    │   ✗    │   ✗                       │
└──────────────┴───────┴────────┴────────┴───────────────────────────┘
* G2 and G4 can only be on layer 1, so can't be on top of anything
"""

"""
4.2 MILK RUN OPTIMIZATION
--------------------------
"""

MILK_RUN_GUIDE = """
CONCEPT:
--------
A milk run is a multi-stop delivery route where a single vehicle delivers
to multiple destinations. The challenge is to pack the container so that:
1. First-stop pallets are accessible (near door)
2. Last-stop pallets are deep in container
3. No restacking required at intermediate stops

MATHEMATICAL APPROACH:
---------------------
The VDR model uses position-weighted cubic priority to create strong
mathematical incentives for correct placement.

Objective term:
  Σ y[v,l,p,plt] * pos_weight[p] * priority[plt]³

where:
  • pos_weight[p] = distance from door (calculated via modulo)
  • priority[plt]³ = cubic amplification of destination priority

EXAMPLE SCENARIO:
----------------
Container with 24 positions, 3 delivery stops:

Positions: [1  2  3  4  5  6 ... 20 21 22 23 24]
           └─────┬─────┘           └──────┬─────┘
             Deep inside            Near door

Destinations:
  • Plant A (priority 1): First stop  → Want positions 20-24
  • Plant B (priority 2): Second stop → Want positions 10-14
  • Plant C (priority 3): Last stop   → Want positions 1-5

Position weights (calculated):
  Position 1-5:   weights 1-5   (deep, for last stop)
  Position 10-14: weights 10-14 (middle, for middle stop)
  Position 20-24: weights 8-12  (near door, for first stop)

Objective contributions (example pallet at position 24):
  • Plant A pallet: 1.0 * 12 * 1³ = 12     (low value)
  • Plant B pallet: 1.0 * 12 * 2³ = 96     (medium value)
  • Plant C pallet: 1.0 * 12 * 3³ = 324    (high value!)

The solver will strongly prefer placing Plant C (last stop) pallets
near the door because they have 27x the priority of Plant A.

CONFIGURATION:
-------------
1. Set palletPlantDict for each pallet:
   palletPlantDict = {
       'PLT001': 'Plant_A',
       'PLT002': 'Plant_B',
       'PLT003': 'Plant_C',
   }

2. Set plantPriorityDict for each plant:
   plantPriorityDict = {
       'Plant_A': 1,   # First stop (high priority for near door)
       'Plant_B': 2,   # Second stop
       'Plant_C': 3,   # Last stop (low priority, can go deep)
   }

3. Set skuPriorityIncentive for each pallet:
   skuPriorityIncentive = {
       'PLT001': 1,   # Corresponds to Plant_A priority
       'PLT002': 2,   # Corresponds to Plant_B priority
       'PLT003': 3,   # Corresponds to Plant_C priority
   }

4. Enable milk run in optimizer:
   optimizer = ContainerLoadingOptimizerVDR(
       ...,
       vdr_stacking_rules=True  # This also enables milk run for ocean mode
   )

POSITION WEIGHT CALCULATION:
---------------------------
The model calculates position weights using modulo arithmetic to create
symmetric patterns:

positions = [1, 2, 3, ..., 22, 23, 24]
midpoint = 24 // 2 = 12

pos_weights = positions % midpoint
# [1, 2, 3, ..., 11, 12, 1, 2, ..., 11, 12]

Replace 0 with midpoint:
# [1, 2, 3, ..., 11, 12, 1, 2, ..., 11, 12]

This creates two "zones" in the container (left and right sides) with
symmetric weight patterns, ensuring balanced loading while respecting
the milk run sequence.

VERIFYING MILK RUN SOLUTION:
----------------------------
After solving, check loading instructions:

for instr in loading_instructions:
    print(f"Container {instr.container}, Pos {instr.position}: "
          f"{instr.material} → {instr.shipTo}")

Expected pattern:
- Positions 1-8: Mostly Plant C (last stop)
- Positions 9-16: Mix of Plant B and C
- Positions 17-24: Mostly Plant A (first stop)
"""


"""
==============================================================================
5. CONFIGURATION GUIDE
==============================================================================
"""

CONFIGURATION_EXAMPLES = """
5.1 OCEAN MODE (Multi-destination containers)
---------------------------------------------
optimizer = ContainerLoadingOptimizerVDR(
    env=env,
    inputParams=inputParams,
    run_ocean=True,              # Ocean container mode
    time_limit=300,              # 5 minutes (containers are complex)
    gap=0.01,                    # 1% gap (good balance)
    sku_penalty_factor=1.5,      # Moderate SKU penalty
    vdr_stacking_rules=True      # Enable all VDR features
)

Constraints enforced:
  ✓ Crushing constraints
  ✓ Stack factor constraints (G2/G3/G4)
  ✓ Milk run optimization
  ✗ Weight balancing (not needed for containers)
  ✗ Axle weight constraints (not applicable)


5.2 TRUCK MODE (Local delivery)
-------------------------------
optimizer = ContainerLoadingOptimizerVDR(
    env=env,
    inputParams=inputParams,
    run_ocean=False,             # Truck mode
    time_limit=120,              # 2 minutes (trucks simpler)
    gap=0.02,                    # 2% gap (faster solves)
    sku_penalty_factor=2.0,      # High SKU penalty (simplify unloading)
    vdr_stacking_rules=False     # May not need VDR rules for local
)

Constraints enforced:
  ✓ Crushing constraints
  ✓ Weight balancing (critical for trucks)
  ✓ Axle weight constraints (DOT regulations)
  ✗ Milk run (less common for trucks)
  Optional: Compact packing (based on enforce_compact_packing param)


5.3 QUICK TESTING (Small problem, fast solve)
---------------------------------------------
optimizer = ContainerLoadingOptimizerVDR(
    env=env,
    inputParams=inputParams,
    run_ocean=True,
    time_limit=60,               # 1 minute only
    gap=0.05,                    # 5% gap (very fast)
    sku_penalty_factor=0.5,      # Low SKU penalty
    vdr_stacking_rules=False     # Disable for speed
)


5.4 PRODUCTION (High quality solution required)
-----------------------------------------------
optimizer = ContainerLoadingOptimizerVDR(
    env=env,
    inputParams=inputParams,
    run_ocean=True,
    time_limit=600,              # 10 minutes
    gap=0.001,                   # 0.1% gap (near-optimal)
    sku_penalty_factor=1.0,
    vdr_stacking_rules=True
)


5.5 LARGE FLEET (8+ vehicles)
-----------------------------
optimizer = ContainerLoadingOptimizerVDR(
    env=env,
    inputParams=inputParams,
    run_ocean=True,
    time_limit=120,              # Will auto-extend to 800s
    gap=0.02,                    # 2% gap (practical for large)
    sku_penalty_factor=1.0,
    vdr_stacking_rules=True
)
# Note: Time limit automatically adjusts to 800s when 8+ vehicles detected


5.6 GUROBI ENVIRONMENT CONFIGURATION
------------------------------------

# Basic environment (local solve)
env = gp.Env(empty=True)
env.setParam('OutputFlag', 1)      # Show progress
env.setParam('LogFile', 'solve.log')  # Log to file
env.start()

# Compute Server environment
env = gp.Env(empty=True)
env.setParam('ComputeServer', 'server1:61000,server2:61000')
env.setParam('ServerPassword', 'your_password')
env.setParam('OutputFlag', 1)
env.start()

# Instant Cloud environment
env = gp.Env(empty=True)
env.setParam('CloudAccessID', 'your_access_id')
env.setParam('CloudSecretKey', 'your_secret_key')
env.setParam('CloudPool', 'your_pool')
env.setParam('OutputFlag', 1)
env.start()

# Multi-threading configuration
env = gp.Env(empty=True)
env.setParam('Threads', 8)          # Use 8 threads
env.setParam('OutputFlag', 1)
env.start()
"""


"""
==============================================================================
6. OUTPUT FILES AND REPORTS
==============================================================================
"""

OUTPUT_FILES_GUIDE = """
The optimizer generates several output files for different purposes:

1. MODEL FILE (.lp or .mps)
---------------------------
File: model.lp
Content: Human-readable optimization model
Use: Debugging, model verification
Generated by: optimizer.write_model("model.lp")

2. CONTAINER SUMMARY (outputfile1.csv)
--------------------------------------
Columns:
  • Ship-To
  • Container
  • Full Pallets
  • Partial Pallets
  • Utilization: Pallets (%)
  • Utilization: Volume (%)
  • Utilization: Weight (%)
  • Cargo Weight: Front (lbs)
  • Cargo Weight: Rear (lbs)
  • SKU Count
  • Consider Holding To Consolidate (Yes/No)

Use: High-level container utilization analysis

3. CARGO SUMMARY (outputfile2.csv)
----------------------------------
Columns:
  • Ship-To
  • Container
  • Material (SKU)
  • Brand
  • Order Number
  • Material Description
  • Pallets (count)
  • Cases (total)
  • Weight [lb]
  • Cube [ft³]

Use: SKU-level summary per container

4. LEFTOVER/UNASSIGNED (outputfile3.csv)
----------------------------------------
Columns:
  • Ship-To
  • Material
  • Brand
  • Order Number
  • Cases (UOM)
  • Weight [lbs]
  • Pallets (unassigned count)

Use: Identify what couldn't be loaded, plan next shipment

5. LOADING INSTRUCTIONS (outputfile4.csv)
-----------------------------------------
Columns (VDR-enhanced):
  • Ship-To
  • Container
  • Sequence (loading order)
  • Position
  • Layer
  • Material SKU code
  • Brand
  • Material Description
  • Order Number
  • Cases
  • Weight [lb]
  • Cube [ft³]
  • Height
  • Total Cases
  • Is Partial?
  • Stacking Group (G1/G2/G3/G4) ← VDR-specific
  • Location (warehouse) ← VDR-specific
  • Destination (plant) ← VDR-specific

Use: Warehouse operations, loading crew instructions

6. WEIGHT BALANCE (outputfile6.csv)
-----------------------------------
Columns:
  • Ship-To
  • Container
  • Full Pallets
  • Partial Pallets
  • Total Wgt
  • Front (%)
  • Rear (%)
  • Left (%)
  • Right (%)

Use: Verify weight distribution compliance


GENERATING CUSTOM REPORTS:
--------------------------
solution = optimizer.get_solution()

# Custom report: Stacking group distribution
from collections import Counter
stacking_groups = Counter(solution['stacking_groups'].values())
print("Stacking Group Distribution:")
for group, count in stacking_groups.most_common():
    print(f"  {group}: {count} positions")

# Custom report: Milk run destinations
loading_instructions = optimizer.generate_loading_instructions()
destinations = {}
for instr in loading_instructions:
    dest = instr.shipTo
    if dest not in destinations:
        destinations[dest] = {'full': 0, 'partial': 0, 'weight': 0}
    
    if instr.is_partial:
        destinations[dest]['partial'] += 1
    else:
        destinations[dest]['full'] += 1
    destinations[dest]['weight'] += instr.weight

print("\\nMilk Run Summary:")
for dest, stats in sorted(destinations.items()):
    print(f"  {dest}:")
    print(f"    Full pallets: {stats['full']}")
    print(f"    Partial pallets: {stats['partial']}")
    print(f"    Total weight: {stats['weight']:,.0f} lbs")
"""


"""
==============================================================================
7. TROUBLESHOOTING
==============================================================================
"""

TROUBLESHOOTING_GUIDE = """
PROBLEM 1: Model is Infeasible
-------------------------------
Symptoms:
  • Solver status: GRB.INFEASIBLE
  • No solution found
  • IIS file generated

Causes & Solutions:
1. Too many G4 pallets (ground-only restriction)
   → Reduce G4 count or use more containers
   
2. Weight limits too restrictive
   → Check max_wt, max_front_wt, max_rear_wt values
   → Verify pallet weights are correct
   
3. Height constraints violated
   → Check containerHeight and palletHeightDict
   → Ensure load_allow_ht is reasonable
   
4. Stacking conflicts
   → Verify G2/G3/G4 assignments are correct
   → Check if too many non-stackable pallets
   
5. Balance tolerances too tight
   → Increase front_to_back_tolerance
   → Increase side_to_side_tolerance

Debugging steps:
1. Check IIS file: model_infeasible.ilp
2. Temporarily disable VDR constraints:
   vdr_stacking_rules=False
3. Increase tolerances:
   front_to_back_tolerance = 0.10 (10%)
4. Reduce demand by 10%, see if feasible


PROBLEM 2: Solver is Very Slow
-------------------------------
Symptoms:
  • Solve time > 10 minutes
  • MIP gap not improving
  • Many nodes explored but no progress

Solutions:
1. Increase MIP gap tolerance:
   gap=0.02  # 2% instead of 1%
   
2. Reduce time limit:
   time_limit=300  # Stop after 5 minutes
   
3. Disable VDR features temporarily:
   vdr_stacking_rules=False
   
4. Simplify problem:
   • Reduce number of containers
   • Aggregate similar SKUs
   • Round pallet weights/heights
   
5. Use warm start (if available from previous solve)

6. Try different MIPFocus:
   env.setParam('MIPFocus', 1)  # Focus on feasibility
   # or
   env.setParam('MIPFocus', 2)  # Focus on optimality


PROBLEM 3: Solution Quality is Poor
------------------------------------
Symptoms:
  • Low utilization (<70%)
  • Too many containers used
  • Suboptimal placement

Solutions:
1. Tighten MIP gap:
   gap=0.005  # 0.5% instead of 1%
   
2. Increase time limit:
   time_limit=600  # 10 minutes
   
3. Check input data quality:
   • Verify pallet dimensions
   • Check weight values
   • Validate stacking group assignments
   
4. Adjust priority incentives:
   priorityIncentive = 2000  # Increase from 1000
   
5. Review SKU penalty:
   sku_penalty_factor = 0.5  # Reduce if too restrictive


PROBLEM 4: Milk Run Not Working as Expected
-------------------------------------------
Symptoms:
  • First-stop pallets deep in container
  • Last-stop pallets near door
  • No clear destination pattern

Solutions:
1. Verify plantPriorityDict is correct:
   • First stop should have priority 1
   • Last stop should have highest number
   
2. Check skuPriorityIncentive values:
   • Should match plantPriorityDict values
   • Higher number = later stop
   
3. Increase priorityIncentive:
   priorityIncentive = 5000  # Amplify milk run effect
   
4. Verify position weights calculated:
   print(optimizer.pos_id_new_2)
   # Should show variation across positions
   
5. Check milk run is enabled:
   run_ocean=True and vdr_stacking_rules=True


PROBLEM 5: Stacking Groups Not Respected
----------------------------------------
Symptoms:
  • G2 pallets on upper layers
  • G4 pallets with items on top
  • G3 pallets buried under other pallets

Solutions:
1. Verify vdr_stacking_rules=True

2. Check stacking group dictionaries:
   print(inputParams['stackFactorg2Pallet'])
   print(inputParams['stackFactorg3Pallet'])
   print(inputParams['stackFactorg4Pallet'])
   
3. Ensure only one group per pallet:
   # Wrong: plt has both G2 and G4 set to 1
   # Right: plt has at most one group set to 1
   
4. Check solution:
   solution = optimizer.get_solution()
   print(solution['stacking_groups'])


PROBLEM 6: Gurobi License Issues
--------------------------------
Symptoms:
  • "No Gurobi license found"
  • "License expired"
  • "Not enough agents available"

Solutions:
1. Check license file location:
   echo $GRB_LICENSE_FILE  # Linux/Mac
   echo %GRB_LICENSE_FILE%  # Windows
   
2. Set license path in code:
   import os
   os.environ["GRB_LICENSE_FILE"] = "/path/to/gurobi.lic"
   
3. For Compute Server:
   env.setParam('ComputeServer', 'server:port')
   env.setParam('ServerPassword', 'password')
   
4. Check license type supports features:
   • Instant Cloud: All features
   • Academic: MIP, Compute Server not available
   • Trial: Time-limited
"""


"""
==============================================================================
8. PERFORMANCE TUNING
==============================================================================
"""

PERFORMANCE_TUNING_GUIDE = """
BENCHMARK TARGETS:
-----------------
Problem Size          │ Target Solve Time │ Expected Gap
──────────────────────┼───────────────────┼─────────────
1-2 containers        │ 10-30 seconds     │ 0% (optimal)
3-5 containers        │ 30-120 seconds    │ <0.5%
6-8 containers        │ 2-5 minutes       │ <1%
9-12 containers       │ 5-15 minutes      │ <2%
13+ containers        │ 10-30 minutes     │ <5%

If your solve times significantly exceed these, apply tuning below.


LEVEL 1: BASIC TUNING (Try first)
----------------------------------
1. Adjust MIP Gap:
   gap = 0.01  # 1% - good balance
   gap = 0.02  # 2% - faster, slightly lower quality
   gap = 0.005 # 0.5% - slower, higher quality

2. Set Appropriate Time Limit:
   Small problems: time_limit = 60
   Medium problems: time_limit = 300
   Large problems: time_limit = 600

3. Use Appropriate Mode:
   For testing: vdr_stacking_rules = False
   For production: vdr_stacking_rules = True


LEVEL 2: GUROBI PARAMETER TUNING
---------------------------------
env.setParam('MIPFocus', 1)     # Focus on finding good solutions fast
# or
env.setParam('MIPFocus', 2)     # Focus on proving optimality
# or
env.setParam('MIPFocus', 3)     # Focus on improving bound

env.setParam('Heuristics', 0.5) # Spend more time on heuristics
env.setParam('Presolve', 2)     # Aggressive presolve
env.setParam('Cuts', 2)         # Aggressive cut generation

# For multi-core systems:
env.setParam('Threads', 8)      # Use 8 threads


LEVEL 3: PROBLEM-SPECIFIC TUNING
---------------------------------
1. For problems with many SKUs:
   sku_penalty_factor = 2.0     # Stronger SKU penalty
   enforceMaxSKUConstraint = True
   max_sku_n = 15                # Lower SKU limit

2. For problems with tight weight limits:
   Relax tolerances slightly:
   front_to_back_tolerance = 0.06  # 6% instead of 5%
   side_to_side_tolerance = 0.04   # 4% instead of 3%

3. For problems with many G4 pallets:
   Consider splitting into multiple optimization runs:
   • First run: Only G1/G2 pallets
   • Second run: G3/G4 pallets with remaining space

4. For milk run problems:
   Simplify priorities if too many unique values:
   # Instead of: 1, 2, 3, 4, 5, 6, 7, 8
   # Use: 1, 1, 2, 2, 3, 3, 4, 4
   # (Group nearby stops with same priority)


LEVEL 4: ADVANCED TECHNIQUES
----------------------------
1. Warm Starting:
   # Solve once, save solution
   first_solution = optimizer.get_solution()
   
   # For second solve with similar data, provide warm start
   for (v,l,p,plt), value in first_solution['y_full'].items():
       if plt in new_inputParams['fullPalletList']:
           # Set start value for variable
           # (would need to extend optimizer class for this)
           pass

2. Problem Decomposition:
   Instead of solving all containers at once:
   • Solve containers 1-3
   • Solve containers 4-6
   • Solve containers 7-9
   # Much faster, slightly suboptimal

3. Constraint Relaxation:
   For initial solve, disable some constraints:
   enforceWeightBalancing = False
   enforceCompactPacking = False
   
   Then enable for refinement solve

4. Rolling Horizon:
   For very large fleets (20+ containers):
   • Optimize first 5 containers fully
   • Fix those assignments
   • Optimize next 5 containers
   • Repeat


MEMORY OPTIMIZATION:
-------------------
For very large problems:
env.setParam('NodefileStart', 2.0)      # Write to disk after 2GB
env.setParam('NodefileDir', '/tmp')     # Specify disk location


MEASURING PERFORMANCE:
---------------------
import time

start_time = time.time()
optimizer.build_model()
build_time = time.time() - start_time

start_time = time.time()
optimizer.solve()
solve_time = time.time() - start_time

print(f"Build time: {build_time:.2f}s")
print(f"Solve time: {solve_time:.2f}s")
print(f"Objective: {optimizer.model.ObjVal:,.0f}")
print(f"Gap: {optimizer.model.MIPGap:.2%}")
print(f"Nodes: {optimizer.model.NodeCount:,}")
"""


"""
==============================================================================
9. COMPLETE WORKING EXAMPLE
==============================================================================
"""

COMPLETE_EXAMPLE = '''
"""
Complete working example of VDR-enhanced container loading optimization.
This example demonstrates all key features with realistic data.
"""

import gurobipy as gp
from gurobipy import GRB
from loadmax.optimizer import ContainerLoadingOptimizerVDR
import pandas as pd

def create_sample_data():
    """Create sample input data for demonstration."""
    
    # Define 3 containers
    containers = [1, 2, 3]
    
    # Define 4 layers
    layers = [1, 2, 3, 4]
    
    # Define 24 positions (typical 40ft container)
    positions = list(range(1, 25))
    
    # Define pallets
    full_pallets = [f'FP{i:03d}' for i in range(1, 21)]  # 20 full pallets
    partial_pallets = [f'PP{i:03d}' for i in range(1, 6)]  # 5 partial pallets
    
    # Define materials (SKUs)
    materials = [f'SKU{i:03d}' for i in range(1, 11)]  # 10 unique SKUs
    
    # Create comprehensive inputParams
    inputParams = {
        # Basic identifiers
        'marketID': 'VDR_EMEA_Demo',
        
        # Lists
        'containerList': containers,
        'layerList': layers,
        'positionList': positions,
        'fullPalletList': full_pallets,
        'partialPalletList': partial_pallets,
        'materialList': materials,
        'sectionList': ['A', 'B', 'C', 'D'],
        
        # Position groups
        'frontPositionList': list(range(1, 13)),
        'rearPositionList': list(range(13, 25)),
        'leftPositionList': [p for p in positions if p % 2 == 1],
        'rightPositionList': [p for p in positions if p % 2 == 0],
        'door_posn': [23, 24],
        
        # Position metadata
        'positionSequenceDict': {p: p for p in positions},
        'positionGroupBalanceCoefficientDict': {
            (p, section): 1.0 for p in positions for section in ['A', 'B', 'C', 'D']
        },
        
        # Pallet properties (example: all pallets 2000 lbs, 70 inches, 100 cases)
        'palletWeightDict': {plt: 2000 for plt in full_pallets + partial_pallets},
        'palletHeightDict': {plt: 70 for plt in full_pallets + partial_pallets},
        'palletNumCasesDict': {plt: 100 for plt in full_pallets + partial_pallets},
        
        # Map pallets to materials
        'palletMaterialDict': {plt: materials[i % len(materials)] 
                              for i, plt in enumerate(full_pallets + partial_pallets)},
        
        # Brands (3 brands)
        'palletBrandDict': {plt: f'Brand_{chr(65 + i % 3)}' 
                           for i, plt in enumerate(full_pallets + partial_pallets)},
        
        # Batches
        'palletBatchDict': {plt: f'BATCH{(i // 5) + 1:03d}' 
                           for i, plt in enumerate(full_pallets + partial_pallets)},
        
        # Descriptions
        'palletDescriptionDict': {plt: f'Material {materials[i % len(materials)]} Description'
                                 for i, plt in enumerate(full_pallets + partial_pallets)},
        
        # Bin locations
        'palletBinLocDict': {plt: f'A-{(i // 5) + 1:02d}-{(i % 5) + 1}'
                            for i, plt in enumerate(full_pallets + partial_pallets)},
        
        # Batch dates
        'palletBatchDateDict': {plt: '2024-01-15' for plt in full_pallets + partial_pallets},
        
        # Demand (1 of each full pallet, 1 of each partial pallet)
        'fullPalletDemandByMaterialBatch': {plt: 1 for plt in full_pallets},
        'partialPalletDemandByMaterialBatch': {plt: 1 for plt in partial_pallets},
        
        # Partial fractions (50% for all partial pallets)
        'partialFractionValueForPartialPallet': {plt: 0.5 for plt in partial_pallets},
        
        # Footprint areas
        'fullPalletFootPrintArea': {plt: 13.33 for plt in full_pallets},  # 40"x48" = 13.33 sq ft
        'partialPalletFootPrintArea': {plt: 10.0 for plt in partial_pallets},
        
        # Stacking factor (basic)
        'stackFactorPallet': {plt: 0 for plt in full_pallets + partial_pallets},  # All stackable
        
        # Container priority (fill in order 1, 2, 3)
        'container_priority': {1: 3, 2: 2, 3: 1},
        
        # Container brands
        'containerBrandDict': {c: [f'Brand_A', f'Brand_B', f'Brand_C'] for c in containers},
        
        # Physical limits
        'max_plt_n': 12,
        'max_lyr_n': 4,
        'max_wt': 45000,
        'max_front_wt': 20000,
        'max_rear_wt': 25000,
        'containerHeight': 110,
        'containerHeightAtDoor': 102,
        'load_allow_ht': 2,
        
        # Tolerances
        'front_to_back_tolerance': 0.05,
        'side_to_side_tolerance': 0.03,
        'fp_area_tolerance': 0.1,
        
        # SKU management
        'unique_sku_n': 15,
        'max_sku_n': 20,
        'sku_penalty': 100,
        'sku_penalty_factor': 1.0,
        
        # Priority
        'priorityIncentive': 1000,
        'max_prior_full': {plt: 1 for plt in full_pallets},
        'max_prior_prtl': {plt: 1 for plt in partial_pallets},
        'brand_group': 'BrandGroup_Demo',
        'first_plt_n': 12,
        
        # Crushing limits (if needed)
        'crushingLimitByMaterial': {mtl: 8000 for mtl in materials},
        
        # Thresholds
        'threshold40': 0.85,
        'utilThreshold': 0.90,
        'enforce_compact_packing': 'No',
        'enforce_fp_tolerance': 'No',
        
        # ===================================================================
        # VDR-SPECIFIC FIELDS
        # ===================================================================
        
        # Stacking groups: Assign G2/G3/G4 to some pallets
        'stackFactorg2Pallet': {
            **{plt: 1 if i < 5 else 0 for i, plt in enumerate(full_pallets)},  # First 5 are G2
            **{plt: 0 for plt in partial_pallets}
        },
        
        'stackFactorg3Pallet': {
            **{plt: 1 if 5 <= i < 10 else 0 for i, plt in enumerate(full_pallets)},  # Next 5 are G3
            **{plt: 0 for plt in partial_pallets}
        },
        
        'stackFactorg4Pallet': {
            **{plt: 1 if 10 <= i < 13 else 0 for i, plt in enumerate(full_pallets)},  # Next 3 are G4
            **{plt: 0 for plt in partial_pallets}
        },
        
        # Stacking group names
        'palletStackingGroupDict': {
            **{plt: 'G2' if i < 5 else 'G3' if 5 <= i < 10 else 'G4' if 10 <= i < 13 else 'G1'
               for i, plt in enumerate(full_pallets)},
            **{plt: 'G1' for plt in partial_pallets}
        },
        
        # Milk run destinations (3 plants)
        'palletPlantDict': {
            **{plt: f'Plant_{chr(65 + i % 3)}' for i, plt in enumerate(full_pallets)},
            **{plt: f'Plant_{chr(65 + i % 3)}' for i, plt in enumerate(partial_pallets)}
        },
        
        # Plant priorities (Plant_A first, Plant_B second, Plant_C last)
        'plantPriorityDict': {
            'Plant_A': 1,
            'Plant_B': 2,
            'Plant_C': 3,
        },
        
        # Warehouse locations (2 warehouses)
        'palletLocationDict': {
            **{plt: f'Warehouse_{(i % 2) + 1}' for i, plt in enumerate(full_pallets)},
            **{plt: f'Warehouse_{(i % 2) + 1}' for i, plt in enumerate(partial_pallets)}
        },
        
        # SKU priorities (match plant priorities for demo)
        'skuPriorityIncentive': {
            **{plt: (i % 3) + 1 for i, plt in enumerate(full_pallets)},
            **{plt: (i % 3) + 1 for i, plt in enumerate(partial_pallets)}
        },
        
        # Brand groups
        'newBrandGroupDict': {
            'Brand_A': 'PalletType_Standard',
            'Brand_B': 'PalletType_Premium',
            'Brand_C': 'PalletType_Economy',
        },
    }
    
    return inputParams


def main():
    """Main execution function."""
    
    print("="*70)
    print("VDR-ENHANCED CONTAINER LOADING OPTIMIZATION - DEMO")
    print("="*70)
    
    # 1. Create sample data
    print("\n[1/5] Creating sample input data...")
    inputParams = create_sample_data()
    print(f"  ✓ Created data for {len(inputParams['fullPalletList'])} full pallets")
    print(f"  ✓ Created data for {len(inputParams['partialPalletList'])} partial pallets")
    print(f"  ✓ {len(inputParams['containerList'])} containers available")
    
    # 2. Create Gurobi environment
    print("\n[2/5] Initializing Gurobi environment...")
    with gp.Env(empty=True) as env:
        env.setParam('OutputFlag', 1)
        env.setParam('LogFile', 'optimization_demo.log')
        env.start()
        print("  ✓ Gurobi environment ready")
        
        # 3. Create optimizer
        print("\n[3/5] Creating VDR-enhanced optimizer...")
        with ContainerLoadingOptimizerVDR(
            env=env,
            inputParams=inputParams,
            run_ocean=True,
            time_limit=120,
            gap=0.01,
            sku_penalty_factor=1.0,
            vdr_stacking_rules=True
        ) as optimizer:
            print("  ✓ Optimizer created with VDR features enabled")
            
            # 4. Build and solve
            print("\n[4/5] Building and solving model...")
            optimizer.build_model()
            optimizer.write_model("demo_model.lp")
            
            status = optimizer.solve()
            
            # 5. Analyze results
            print("\n[5/5] Analyzing results...")
            if status in [GRB.OPTIMAL, GRB.TIME_LIMIT]:
                optimizer.print_solution_summary()
                
                # Get solution
                solution = optimizer.get_solution()
                
                # Generate loading instructions
                loading_instructions = optimizer.generate_loading_instructions()
                
                # Create summary DataFrame
                summary_data = []
                for instr in loading_instructions:
                    summary_data.append({
                        'Container': instr.container,
                        'Sequence': instr.sequence,
                        'Position': instr.position,
                        'Layer': instr.layer,
                        'Material': instr.material,
                        'Stacking Group': instr.stacking_group,
                        'Destination': instr.shipTo,
                        'Location': instr.location,
                        'Weight': instr.weight,
                    })
                
                df = pd.DataFrame(summary_data)
                
                # Save to CSV
                df.to_csv('demo_loading_instructions.csv', index=False)
                print("\n  ✓ Loading instructions saved to: demo_loading_instructions.csv")
                
                # Print summary statistics
                print("\n" + "="*70)
                print("SUMMARY STATISTICS")
                print("="*70)
                
                print(f"\nTotal pallets loaded: {len(loading_instructions)}")
                print(f"Containers used: {df['Container'].nunique()}")
                
                print("\nStacking Group Distribution:")
                for group, count in df['Stacking Group'].value_counts().items():
                    print(f"  {group}: {count} pallets")
                
                print("\nDestination Distribution:")
                for dest, count in df['Destination'].value_counts().items():
                    print(f"  {dest}: {count} pallets")
                
                print("\nMilk Run Verification (first 10 pallets):")
                print(df[['Container', 'Position', 'Destination', 'Stacking Group']].head(10))
                
            else:
                print("\n❌ Optimization failed or no solution found")
                print(f"   Status: {status}")


if __name__ == '__main__':
    main()
'''


"""
==============================================================================
10. FAQ
==============================================================================

Q: What's the difference between G2 and G4 pallets?
A: Both must be on ground (layer 1), but:
   - G2: Other pallets CAN stack on top (heavy stable base)
   - G4: NO pallets can stack on top (fragile or oversized)

Q: Can I use only some VDR features?
A: Yes. Set vdr_stacking_rules=False to disable G2/G3/G4 constraints
   while still getting milk run optimization in ocean mode.

Q: How do I know if milk run optimization is working?
A: Check loading instructions: pallets with priority 1 (first stop)
   should appear near door positions (higher numbers), while priority 3
   (last stop) should be deep inside (lower positions).

Q: What if I have more than 3 delivery stops?
A: No problem. Just assign unique priorities to each plant:
   plantPriorityDict = {'Plant_A': 1, 'Plant_B': 2, ..., 'Plant_Z': 26}
   The cubic amplification will handle many stops effectively.

Q: Can I mix ocean and truck constraints?
A: Not recommended. Choose run_ocean=True OR run_ocean=False.
   They enforce different constraint sets optimized for each mode.

Q: How do I handle "Many" bin locations?
A: Provide customerMaterialBatch_BinLoc_Dict with list of locations:
   {'Market_SKU_Batch_Type': ['A-12-1', 'A-12-2', 'A-12-3']}
   The optimizer will consume them sequentially.

Q: What's the performance impact of VDR features?
A: Typically 20-30% longer solve time due to additional constraints,
   but solution quality often improves. For production use, it's worth it.

Q: Can I use this with other Gurobi features (callbacks, tuning)?
A: Yes! The optimizer exposes self.model which is a standard Gurobi
   model. You can add callbacks, use tuning tool, etc. after build_model().

Q: How do I report a bug or request a feature?
A: Check the error message and IIS file first. If still stuck, create
   a minimal reproducible example and contact support.
"""

# End of guide
print(__doc__)
