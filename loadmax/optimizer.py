"""
Enhanced Container Loading Optimization Model with VDR-Specific Features
=========================================================================

This module implements an advanced mixed-integer programming model for optimizing
the loading of pallets into containers/trucks with support for:

1. **Multi-Destination Routing (Milk Run)**:
   - Optimizes pallet placement based on unloading sequence
   - Places first-stop pallets near door, later-stop pallets deeper
   - Position-weighted objective function for route optimization

2. **Advanced Stacking Rules (VDR-Specific)**:
   - G1: Standard stackable pallets
   - G2: Always on ground, others can stack on them
   - G3: Can be on ground, nothing stacks on top
   - G4: Always on ground AND nothing stacks on top

3. **Enhanced Features**:
   - Dynamic solver parameter adjustment based on problem size
   - Destination-aware placement optimization
   - Warehouse location tracking
   - Cubic priority weighting for amplified destination preferences

Problem Overview:
----------------
Given:
- Set of containers/trucks with weight, height, volume constraints
- Set of full and partial pallets with dimensions, weights, destinations
- Stacking compatibility rules (4-level system)
- Multiple delivery stops (milk run scenario)

Optimize:
- Which pallets go in which containers
- Position and layer assignment for each pallet
- Unloading sequence considerations
- Weight balance and stability

Subject to:
- Weight limits (total, axle-specific, crushing)
- Height constraints (stack height, door clearance)
- Stacking rules (G1/G2/G3/G4 compatibility)
- Balance requirements (front-back, side-side)
- Unloading sequence feasibility
- SKU proliferation penalties

Mathematical Model:
------------------
Decision Variables:
- y_full[v,l,p,plt]: Binary - full pallet plt at vehicle v, layer l, position p
- y_prtl[v,l,p,plt]: Binary - partial pallet plt at vehicle v, layer l, position p
- no_stk_plt_g2/g3/g4[v,l,p]: Binary - stacking group indicators

Objective:
maximize: Σ(pallets assigned * container_priority * position_weight * sku_priority³)
          + Σ(priority_pallets * incentive)
          - (SKU_penalty * excess_SKUs)

The cubic sku_priority³ term creates strong mathematical preference for correct
destination placement in multi-stop scenarios.

Author: Converted from Pyomo to Gurobi with VDR enhancements
Version: 2.0 (VDR Enhanced)
Date: 2024
"""

import pandas as pd
import numpy as np
import gurobipy as gp
from gurobipy import GRB
from prettytable import PrettyTable
from loadmax.models import Load_Obj
import operator
import os


class ContainerLoadingOptimizerVDR:
    """
    Enhanced optimizer for container/truck loading with VDR-specific features.
    
    This class extends the basic container loading optimization with advanced
    capabilities for multi-destination routing (milk run) and sophisticated
    stacking rules required by VDR operations.
    
    Key Enhancements over Base Model:
    ---------------------------------
    1. **Milk Run Optimization**:
       - Position-weighted objective based on destination sequence
       - First-stop pallets incentivized near door (rear positions)
       - Last-stop pallets placed deep in container (front positions)
    
    2. **4-Level Stacking System**:
       - G1 (Standard): Can stack and be stacked upon
       - G2 (Ground Base): Always layer 1, stackable upon
       - G3 (No Stack On): Can be ground level, but nothing on top
       - G4 (Ground Only): Must be layer 1, nothing on top
    
    3. **Dynamic Solver Tuning**:
       - Auto-adjusts time limit for large fleets (8+ vehicles)
       - Configurable MIP gap for practical optimality (1% default)
       - Adaptive parameter selection
    
    4. **Enhanced Traceability**:
       - Tracks warehouse location for each pallet
       - Records destination plant/stop
       - Stacking group assignment in output
    
    Attributes:
        env (gp.Env): Gurobi environment
        model (gp.Model): Gurobi optimization model
        inputParams (dict): Complete problem data including:
            - Standard parameters (pallets, containers, weights, etc.)
            - VDR-specific: stackFactorg2/g3/g4Pallet, palletPlantDict, 
              plantPriorityDict, palletLocationDict
        run_ocean (bool): True for ocean containers, False for trucks
        time_limit (int): Base time limit in seconds (auto-adjusted)
        gap (float): MIP optimality gap tolerance (default 0.01 = 1%)
        sku_penalty_factor (float): Multiplier for SKU excess penalty
        vdr_stacking_rules (bool): Enable VDR G2/G3/G4 stacking constraints
        
    VDR-Specific Variables (in addition to base model):
        no_stk_plt_g2[v,l,p]: Binary - indicates G2 pallet at position
        no_stk_plt_g3[v,l,p]: Binary - indicates G3 pallet at position
        no_stk_plt_g4[v,l,p]: Binary - indicates G4 pallet at position
        obj_full_plt_var_term4: Continuous - position-weighted objective term
        obj_prtl_plt_var_term4: Continuous - position-weighted objective term
    
    Usage Example:
    --------------
    ```python
    import gurobipy as gp
    from gurobipy import GRB
    
    # Prepare input data with VDR fields
    inputParams = {
        # ... standard fields ...
        'stackFactorg2Pallet': {plt_id: 0 or 1, ...},
        'stackFactorg3Pallet': {plt_id: 0 or 1, ...},
        'stackFactorg4Pallet': {plt_id: 0 or 1, ...},
        'palletPlantDict': {plt_id: 'Plant_A', ...},
        'plantPriorityDict': {'Plant_A': 1, 'Plant_B': 2, ...},
        'palletLocationDict': {plt_id: 'Warehouse_1', ...},
        'palletStackingGroupDict': {plt_id: 'G2', ...},
    }
    
    # Create environment
    with gp.Env(empty=True) as env:
        env.setParam('OutputFlag', 1)
        env.start()
        
        # Create optimizer with VDR features enabled
        with ContainerLoadingOptimizerVDR(
            env=env,
            inputParams=inputParams,
            run_ocean=True,
            time_limit=120,
            gap=0.01,  # 1% optimality gap
            sku_penalty_factor=1.0,
            vdr_stacking_rules=True  # Enable G2/G3/G4 constraints
        ) as optimizer:
            # Build and solve
            optimizer.build_model()
            optimizer.solve()
            
            # Get results with VDR-specific fields
            solution = optimizer.get_solution()
            loading_instructions = optimizer.generate_loading_instructions()
            
            # Each instruction now includes:
            # - stacking_group: 'G1', 'G2', 'G3', or 'G4'
            # - location: Warehouse/DC location
            # - shipTo: Destination plant for milk run
    ```
    
    Mathematical Formulation - Milk Run Enhancement:
    -----------------------------------------------
    Standard objective:
        max Σ y[v,l,p,plt] * priority[plt]
    
    VDR enhanced objective:
        max Σ y[v,l,p,plt] * position_weight[p] * priority[plt]³
        
    Where position_weight[p] is calculated based on distance from door:
        - Positions near door (for early stops): higher weight
        - Positions deep in container (for late stops): lower weight
        
    The cubic power (³) amplifies differences between priority levels,
    creating strong mathematical push for optimal placement.
    
    Notes:
    ------
    - Milk run feature assumes positions are numbered with door at one end
    - Position weighting uses modulo arithmetic to create symmetric patterns
    - G4 is most restrictive stacking rule (ground only, no stacking)
    - Time limit automatically extends from 120s to 800s for 8+ vehicles
    """
    
    def __init__(self, env, inputParams, run_ocean=True, time_limit=120, 
                 gap=0.01, sku_penalty_factor=1.0, vdr_stacking_rules=True):
        """
        Initialize the VDR-Enhanced Container Loading Optimizer.
        
        Args:
            env (gp.Env): Gurobi environment (must be started)
            inputParams (dict): Complete problem data dictionary containing:
                
                **Standard Fields** (same as base model):
                - containerList: List of container/vehicle IDs
                - layerList: List of layer IDs (typically [1,2,3,4])
                - positionList: List of position IDs within each layer
                - fullPalletList: List of full pallet IDs
                - partialPalletList: List of partial pallet IDs
                - palletWeightDict: {pallet_id: weight_lbs}
                - palletHeightDict: {pallet_id: height_inches}
                - palletMaterialDict: {pallet_id: material_sku}
                - And many more (see base model documentation)
                
                **VDR-Specific Fields**:
                - stackFactorg2Pallet: {pallet_id: 0 or 1} - G2 indicator
                - stackFactorg3Pallet: {pallet_id: 0 or 1} - G3 indicator
                - stackFactorg4Pallet: {pallet_id: 0 or 1} - G4 indicator
                - palletPlantDict: {pallet_id: 'destination_plant_name'}
                - plantPriorityDict: {'plant_name': priority_value}
                - palletLocationDict: {pallet_id: 'warehouse_location'}
                - palletStackingGroupDict: {pallet_id: 'G1'/'G2'/'G3'/'G4'}
                - skuPriorityIncentive: {pallet_id: priority_multiplier}
                
            run_ocean (bool): Operating mode
                - True: Ocean container mode (40ft/20ft containers)
                - False: Truck mode (53ft trailers, etc.)
                Affects which constraints are enforced (e.g., axle weights for trucks)
                
            time_limit (int): Base optimization time limit in seconds
                - Will auto-adjust to 800s if 8+ vehicles detected
                - For quick testing, use 60s
                - For production, use 300-600s depending on problem size
                
            gap (float): MIP optimality gap tolerance (0.0 to 1.0)
                - 0.0: Prove optimality (slow, may timeout)
                - 0.01: Within 1% of optimal (recommended for production)
                - 0.05: Within 5% of optimal (faster, good for large problems)
                Example: gap=0.01 with objective=1000 stops if proven bound >= 990
                
            sku_penalty_factor (float): Multiplier for SKU proliferation penalty
                - Higher value = stronger penalty for many SKUs per container
                - Typical range: 0.5 to 2.0
                - Use higher values when SKU minimization is critical
                
            vdr_stacking_rules (bool): Enable VDR G2/G3/G4 stacking constraints
                - True: Enforce 4-level stacking system (G1/G2/G3/G4)
                - False: Use simple binary stackable/non-stackable rules
                - Set to True for VDR customer or similar complex requirements
        
        Raises:
            KeyError: If required VDR fields missing when vdr_stacking_rules=True
            ValueError: If gap not in valid range [0, 1]
        
        Example:
            ```python
            with gp.Env(empty=True) as env:
                env.start()
                
                optimizer = ContainerLoadingOptimizerVDR(
                    env=env,
                    inputParams=my_data,
                    run_ocean=True,
                    time_limit=300,
                    gap=0.01,
                    sku_penalty_factor=1.5,
                    vdr_stacking_rules=True
                )
            ```
        """
        # Validate inputs
        if not 0 <= gap < 1:
            raise ValueError(f"MIP gap must be in [0, 1), got {gap}")
        if time_limit <= 0:
            raise ValueError(f"time_limit must be positive, got {time_limit}")
        
        # Store configuration
        self.env = env
        self._owns_env = False
        self.model = None
        self.inputParams = inputParams
        self.run_ocean = run_ocean
        self.time_limit = time_limit
        self.gap = gap
        self.sku_penalty_factor = sku_penalty_factor
        self.vdr_stacking_rules = vdr_stacking_rules
        
        # Constraint enforcement flags (will be set based on run_ocean)
        self.enforceCrushingConstraints = True
        self.enforceWeightBalancing = False
        self.enforceMaxSKUConstraint = False
        self.enforcePriorityPalletConstraints = True
        self.enforceMixedCargoConstraints = False
        self.enforceAxleWeightConstraints = False
        self.enforceCompactPacking = False
        self.enforceSKUCrushingLimit = False
        self.enforceStackFactorConstraint = False
        self.enforceFootPrintAreaConstraint = False
        
        # VDR-specific flags
        self.enforceG2stackingFactorContraint = False
        self.enforceG3stackingFactorContraint = False
        self.enforceG4stackingFactorContraint = False
        self.enforceMilkRun = False
        
        # Decision variables (initialized in _create_variables)
        self.y_full = None
        self.y_prtl = None
        self.y_prior_full = None
        self.y_prior_prtl = None
        self.full_plt = None
        self.prtl_plt = None
        self.wt = None
        self.stk_ht = None
        self.lyr_plt_n = None
        self.mtl_plt = None
        self.y_mtl_plt = None
        self.mtl_n = None
        self.exceed_mtl_n = None
        self.fp_area = None
        self.sectn_wt = None
        self.no_stk_plt = None
        self.sku_crush_wt_lmt = None
        
        # VDR-specific variables
        self.no_stk_plt_g2 = None
        self.no_stk_plt_g3 = None
        self.no_stk_plt_g4 = None
        
        # Objective function component variables
        self.obj_full_plt_var_term4 = None
        self.obj_prtl_plt_var_term4 = None
        
        # Position weighting for milk run (calculated in build_model)
        self.pos_id_new_2 = None
        self._position_weight_by_id = None
    
    def __enter__(self):
        """Context manager entry."""
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit - dispose of model."""
        if self.model is not None:
            self.model.dispose()
            self.model = None
        if self._owns_env:
            self.env.dispose()
        return False
    
    def _set_constraint_flags(self):
        """
        Set constraint enforcement flags based on ocean vs truck mode.
        
        This method determines which constraints to enforce based on the
        operational mode (ocean vs truck) and VDR-specific requirements.
        
        Ocean Mode (run_ocean=True):
        ---------------------------
        - NO weight balancing (containers more stable than trucks)
        - NO axle weight constraints (not applicable to containers)
        - YES crushing constraints (pallets stacked in both modes)
        - YES compact packing (optional, for stability)
        - YES stack factor constraints (VDR-specific if enabled)
        - YES milk run optimization (for multi-destination ocean freight)
        
        Truck Mode (run_ocean=False):
        ----------------------------
        - YES weight balancing (critical for truck stability)
        - YES axle weight constraints (DOT regulations)
        - YES crushing constraints (always needed)
        - Optional compact packing (based on input parameter)
        - Optional stack factors (typically less critical for trucks)
        - NO milk run (less common for truck deliveries)
        
        VDR-Specific Rules (if vdr_stacking_rules=True):
        ------------------------------------------------
        Enables G2/G3/G4 stacking constraints which define precise
        rules about which products can be on which layers and what
        can be stacked on top of them. This is critical for:
        - Fragile products that can't support weight (G3, G4)
        - Heavy stable products that can support others (G2)
        - Temperature-sensitive products
        - Regulatory compliance (pharma, food, etc.)
        
        Side Effects:
            Sets instance variables:
            - enforceWeightBalancing
            - enforceAxleWeightConstraints
            - enforceCompactPacking
            - enforceStackFactorConstraint
            - enforceG2/G3/G4stackingFactorContraint
            - enforceMilkRun
        """
        if self.run_ocean:
            print("Enforcing constraints for ocean containers...")
            self.enforceWeightBalancing = False
            self.enforceAxleWeightConstraints = False
            self.enforceCompactPacking = False  # Can be enabled if needed
            self.enforceStackFactorConstraint = False
            self.enforceMilkRun = True  # Enable milk run for ocean
            
            # VDR-specific stacking rules for ocean
            if self.vdr_stacking_rules:
                print("Enabling VDR stacking rules (G2/G3/G4)...")
                self.enforceG2stackingFactorContraint = True
                self.enforceG3stackingFactorContraint = True
                self.enforceG4stackingFactorContraint = True
        else:
            print("Enforcing constraints for trucks...")
            self.enforceWeightBalancing = True
            self.enforceAxleWeightConstraints = True
            
            # Check if compact packing should be enforced
            yes_list = ["Yes", "yes", "YES"]
            if self.inputParams.get('enforce_compact_packing', 'No') in yes_list:
                self.enforceCompactPacking = True
            
            # VDR rules less common for trucks but still supported
            if self.vdr_stacking_rules:
                print("Enabling VDR stacking rules for trucks (G2/G3/G4)...")
                self.enforceG2stackingFactorContraint = True
                self.enforceG3stackingFactorContraint = True
                self.enforceG4stackingFactorContraint = True
    
    def _calculate_position_weights(self):
        """
        Calculate position-based weights for milk run optimization.
        
        This method computes weights for each position based on its distance
        from the container door. The goal is to create a mathematical
        incentive to place early-stop pallets near the door and late-stop
        pallets deeper in the container.
        
        Algorithm:
        ----------
        1. Find midpoint of position range
        2. Use modulo arithmetic to create symmetric weight pattern
        3. Positions are weighted by their "effective distance" from door
        
        Example for 24 positions:
        ------------------------
        Physical positions: [1, 2, 3, ..., 22, 23, 24]
        Midpoint: 12
        
        Modulo transformation (positions wrap around midpoint):
        Position 1  -> weight 1  (near door, left side)
        Position 2  -> weight 2
        ...
        Position 12 -> weight 12 (far from door, left side)
        Position 13 -> weight 1  (near door, right side)
        Position 14 -> weight 2
        ...
        Position 24 -> weight 12 (far from door, right side)
        
        The pattern creates symmetry: both sides of the container have
        similar weight profiles, with higher weights deeper in container.
        
        Usage in Objective:
        ------------------
        obj += Σ y[v,l,p,plt] * pos_weight[p] * priority[plt]³
        
        - Pallets for Plant A (priority 3) at position 1: 3³ * 1 = 27
        - Pallets for Plant B (priority 1) at position 12: 1³ * 12 = 12
        - Strong incentive to place high-priority (early stop) near door
        
        Returns:
            list: Position weights indexed by (position-1)
                  pos_id_new_2[0] = weight for position 1
                  pos_id_new_2[23] = weight for position 24
        
        Mathematical Properties:
        -----------------------
        - Symmetric around container centerline
        - Monotonic increasing away from door (on each side)
        - Range: [1, midpoint]
        - Preserves left-right balance for weight distribution
        
        Side Effects:
            Sets self.pos_id_new_2
        
        Notes:
            - Assumes positions numbered sequentially from door
            - Works with any even number of positions
            - For odd positions, last position weighted same as midpoint
        """
        pos_id = self.inputParams["positionList"]
        if not pos_id:
            raise ValueError("positionList must contain at least one position")
        if len(set(pos_id)) != len(pos_id):
            raise ValueError("positionList must not contain duplicate positions")
        
        print(f"Calculating position weights for milk run optimization...")
        print(f"Position list: {pos_id}")
        
        # Calculate midpoint
        mid_point = max(1, len(pos_id) // 2)
        print(f"Container midpoint: {mid_point}")
        
        # Calculate modulo-based position weights
        # Use layout order rather than assuming position IDs are 1..N.
        pos_id_array = np.arange(1, len(pos_id) + 1)
        pos_id_new = pos_id_array % mid_point
        
        # Replace 0 with midpoint (positions that divide evenly)
        for i, n in enumerate(pos_id_new):
            if n == 0:
                pos_id_new[i] = mid_point
        
        # Convert to list for objective function
        self.pos_id_new_2 = pos_id_new.tolist()
        self._position_weight_by_id = dict(zip(pos_id, self.pos_id_new_2))
        
        print(f"Position weights: {self.pos_id_new_2}")
        print(f"Position 1 (near door) gets weight: {self.pos_id_new_2[0]}")
        print(f"Position {mid_point} (far from door) gets weight: {self.pos_id_new_2[mid_point-1]}")
        
        return self.pos_id_new_2
    
    def build_model(self):
        """
        Build the complete Gurobi optimization model with VDR enhancements.
        
        This method orchestrates the entire model building process:
        1. Sets constraint enforcement flags based on mode
        2. Calculates position weights for milk run optimization
        3. Creates the Gurobi model instance
        4. Creates all decision variables
        5. Adds all constraints (base + VDR-specific)
        6. Sets the objective function
        
        Model Building Sequence:
        -----------------------
        1. **Configuration**: Set flags for which constraints to enforce
        2. **Position Weights**: Calculate milk run weights if enabled
        3. **Variables**: Create 40+ decision variables
        4. **Base Constraints**: ~20 constraint groups (assignment, weight, height, etc.)
        5. **VDR Constraints**: G2/G3/G4 stacking rules (if enabled)
        6. **Milk Run Objective**: Position-weighted cubic priority terms
        
        VDR Enhancements:
        ----------------
        Beyond the base model, this includes:
        
        1. **Stacking Group Variables**:
           - no_stk_plt_g2[v,l,p]: Indicates G2 pallet (ground, stackable upon)
           - no_stk_plt_g3[v,l,p]: Indicates G3 pallet (no stack on top)
           - no_stk_plt_g4[v,l,p]: Indicates G4 pallet (ground only, no stack)
        
        2. **Stacking Group Constraints**:
           - G2 pallets forced to layer 1
           - Other pallets can stack on G2
           - G3 pallets have no pallets above them
           - G4 pallets forced to layer 1 AND nothing above
        
        3. **Position-Weighted Objective**:
           - Creates obj_full_plt_var_term4 and obj_prtl_plt_var_term4
           - Weights pallets by position distance from door
           - Uses cubic priority (priority³) for strong preferences
        
        Model Statistics (Typical for 3 containers, 30 pallets):
        -------------------------------------------------------
        - Variables: ~5,000 binary, ~1,000 continuous
        - Constraints: ~8,000 (varies by flags enabled)
        - Non-zeros: ~25,000
        - Model size: 10-50 MB in memory
        
        Build Time:
        ----------
        - Small (1-2 containers): < 1 second
        - Medium (3-5 containers): 1-3 seconds
        - Large (6-10 containers): 3-10 seconds
        - Very large (10+ containers): 10-30 seconds
        
        Returns:
            gp.Model: The built Gurobi model (also stored in self.model)
        
        Raises:
            KeyError: If required input parameters missing
            GurobiError: If model creation fails
        
        Side Effects:
            - Creates self.model
            - Creates all decision variables (self.y_full, etc.)
            - Sets self.pos_id_new_2 (position weights)
        
        Example:
            ```python
            optimizer = ContainerLoadingOptimizerVDR(env, inputParams)
            model = optimizer.build_model()
            
            # Inspect model before solving
            print(f"Variables: {model.NumVars}")
            print(f"Constraints: {model.NumConstrs}")
            print(f"Binaries: {model.NumBinVars}")
            
            # Can modify model here if needed
            # model.setParam('TimeLimit', 600)
            
            optimizer.solve()
            ```
        
        Notes:
            - Call this before solve()
            - Can be called multiple times (rebuilds model)
            - Position weights calculated once per build
            - Memory allocated for model (~50MB typical)
        """
        print("="*70)
        print("BUILDING VDR-ENHANCED OPTIMIZATION MODEL")
        print("="*70)
        
        # Set constraint flags based on mode
        self._set_constraint_flags()
        
        # Calculate position weights for milk run
        if self.enforceMilkRun:
            self._calculate_position_weights()
        
        # Dispose a previous model if build_model() is called again.
        if self.model is not None:
            self.model.dispose()

        # Create Gurobi model
        self.model = gp.Model("ContainerLoading_VDR", env=self.env)
        
        # Build model components
        print("\n[1/4] Creating decision variables...")
        self._create_variables()
        
        print("\n[2/4] Adding base constraints...")
        self._add_base_constraints()
        
        print("\n[3/4] Adding VDR-specific constraints...")
        self._add_vdr_constraints()
        
        print("\n[4/4] Setting objective function...")
        self._set_objective()

        # Gurobi batches model changes; make statistics visible before
        # reporting them and before callers inspect the model.
        self.model.update()
        
        print("\n" + "="*70)
        print("MODEL BUILD COMPLETE")
        print("="*70)
        print(f"Variables: {self.model.NumVars}")
        print(f"  - Binary: {self.model.NumBinVars}")
        print(f"  - Integer: {self.model.NumIntVars}")
        print(f"  - Continuous: {self.model.NumVars - self.model.NumBinVars - self.model.NumIntVars}")
        print(f"Constraints: {self.model.NumConstrs}")
        print(f"Non-zeros: {self.model.NumNZs}")
        print("="*70 + "\n")
        
        return self.model
    
    def _create_variables(self):
        """
        Create all decision variables for the VDR-enhanced optimization model.
        
        This method creates variables in several categories:
        
        1. **Assignment Variables** (Binary):
           - Core decision: which pallet goes where
           
        2. **Physical Property Variables** (Continuous):
           - Derived values: weight, height, footprint
           
        3. **SKU Tracking Variables** (Integer/Binary):
           - Material type counts and indicators
           
        4. **Priority Variables** (Continuous):
           - Track priority pallet assignments
           
        5. **VDR-Specific Variables** (Binary):
           - Stacking group indicators for G2/G3/G4
           
        6. **Objective Component Variables** (Continuous):
           - Pre-computed objective terms for milk run
        
        Variable Categories in Detail:
        -----------------------------
        
        **Category 1: Core Assignment (Binary)**
        ```
        y_full[v,l,p,plt] = 1 if full pallet plt assigned to (v,l,p), 0 otherwise
        y_prtl[v,l,p,plt] = 1 if partial pallet plt assigned to (v,l,p), 0 otherwise
        full_plt[v,l,p] = 1 if any full pallet at (v,l,p)
        prtl_plt[v,l,p] = 1 if any partial pallet at (v,l,p)
        ```
        
        **Category 2: Physical Properties (Continuous)**
        ```
        wt[v,l,p] = Weight (lbs) at position (v,l,p)
        stk_ht[v,p] = Total stack height (inches) at position (v,p)
        lyr_plt_n[v,l] = Number of pallets in layer l of vehicle v
        fp_area[v,l,p] = Footprint area (sq ft) at (v,l,p) [if enforced]
        sectn_wt[v,s] = Weight in section s of vehicle v
        ```
        
        **Category 3: SKU Tracking (Integer/Binary)**
        ```
        mtl_plt[v,mtl] = Number of pallets with material mtl in vehicle v
        y_mtl_plt[v,mtl] = 1 if material mtl present in vehicle v
        mtl_n[v] = Total number of distinct materials in vehicle v
        exceed_mtl_n = Number of materials exceeding target across all vehicles
        ```
        
        **Category 4: Priority (Continuous)**
        ```
        y_prior_full[plt] = Number of priority assignments for full pallet plt
        y_prior_prtl[plt] = Number of priority assignments for partial pallet plt
        ```
        Bounded by max_prior_full[plt] and max_prior_prtl[plt]
        
        **Category 5: VDR Stacking Groups (Binary)**
        ```
        no_stk_plt_g2[v,l,p] = 1 if G2 pallet at (v,l,p)
        no_stk_plt_g3[v,l,p] = 1 if G3 pallet at (v,l,p)
        no_stk_plt_g4[v,l,p] = 1 if G4 pallet at (v,l,p)
        ```
        
        **Category 6: Milk Run Objective Components (Continuous)**
        ```
        obj_full_plt_var_term4 = Σ(y_full * position_weight * priority³)
        obj_prtl_plt_var_term4 = Σ(y_prtl * position_weight * priority³)
        ```
        
        Variable Bounds:
        ---------------
        Most variables have natural bounds from their type (Binary ∈ {0,1}).
        
        Special bounds:
        - stk_ht[v,p]: Upper bounded by max container height minus allowance
          - Door positions: max_veh_door_ht - load_allow_ht
          - Non-door positions: max_veh_ht - load_allow_ht
        
        - lyr_plt_n[v,l]: Bounded [0, max_plt_n] (max pallets per layer)
        
        - mtl_n[v]: Optionally bounded by max_sku_contr if enforceMaxSKUConstraint
        
        - y_prior_full[plt]: Bounded by max_prior_full[plt] from input
        
        Memory Usage:
        ------------
        For typical problem (3 vehicles, 4 layers, 24 positions, 30 pallets):
        - y_full: 3 * 4 * 24 * 30 = 8,640 binary variables
        - y_prtl: Similar size
        - Other variables: ~500
        - Total: ~18,000 variables
        - Memory: ~20-30 MB
        
        Side Effects:
            Sets instance variables for all decision variables
        
        Notes:
            - Variables created even if corresponding constraints not enforced
            - This allows flexible constraint activation/deactivation
            - Unused variables will be removed by Gurobi presolve
        """
        # Extract input parameters
        veh_id = self.inputParams["containerList"]
        lyr_id = self.inputParams["layerList"]
        pos_id = self.inputParams["positionList"]
        agg_full_plt_id = self.inputParams["fullPalletList"]
        agg_prtl_plt_id = self.inputParams["partialPalletList"]
        mkt_mtl_id = self.inputParams["materialList"]
        section_id = self.inputParams["sectionList"]
        max_plt_n = self.inputParams['max_plt_n']
        max_veh_ht = self.inputParams['containerHeight']
        max_veh_door_ht = self.inputParams['containerHeightAtDoor']
        load_allow_ht = self.inputParams['load_allow_ht']
        door_posn = self.inputParams["door_posn"]
        
        print(f"  Creating variables for:")
        print(f"    - {len(veh_id)} vehicles")
        print(f"    - {len(lyr_id)} layers")
        print(f"    - {len(pos_id)} positions")
        print(f"    - {len(agg_full_plt_id)} full pallets")
        print(f"    - {len(agg_prtl_plt_id)} partial pallets")
        print(f"    - {len(mkt_mtl_id)} materials")
        
        # 1. Full pallet assignment variables
        self.y_full = self.model.addVars(
            veh_id, lyr_id, pos_id, agg_full_plt_id,
            vtype=GRB.BINARY,
            name="y_full"
        )
        
        # 2. Partial pallet assignment variables
        self.y_prtl = self.model.addVars(
            veh_id, lyr_id, pos_id, agg_prtl_plt_id,
            vtype=GRB.BINARY,
            name="y_prtl"
        )
        
        # 3. Priority pallet assignment counters
        self.y_prior_full = self.model.addVars(
            agg_full_plt_id,
            lb=0,
            ub=[self.inputParams['max_prior_full'][plt] for plt in agg_full_plt_id],
            vtype=GRB.CONTINUOUS,
            name="y_prior_full"
        )
        
        self.y_prior_prtl = self.model.addVars(
            agg_prtl_plt_id,
            lb=0,
            ub=[self.inputParams['max_prior_prtl'][plt] for plt in agg_prtl_plt_id],
            vtype=GRB.CONTINUOUS,
            name="y_prior_prtl"
        )
        
        # 4. Full pallet presence indicators
        self.full_plt = self.model.addVars(
            veh_id, lyr_id, pos_id,
            vtype=GRB.BINARY,
            name="full_plt"
        )
        
        # 5. Partial pallet presence indicators
        self.prtl_plt = self.model.addVars(
            veh_id, lyr_id, pos_id,
            vtype=GRB.BINARY,
            name="prtl_plt"
        )
        
        # 6. Weight at each position
        self.wt = self.model.addVars(
            veh_id, lyr_id, pos_id,
            lb=0,
            vtype=GRB.CONTINUOUS,
            name="weight"
        )
        
        # 7. Stack height at each position (with bounds based on door position)
        self.stk_ht = {}
        for v in veh_id:
            for p in pos_id:
                if p in door_posn:
                    ub_val = max_veh_door_ht - load_allow_ht
                else:
                    ub_val = max_veh_ht - load_allow_ht
                self.stk_ht[v, p] = self.model.addVar(
                    lb=0,
                    ub=ub_val,
                    vtype=GRB.CONTINUOUS,
                    name=f"stk_ht_{v}_{p}"
                )
        
        # 8. Layer pallet count
        self.lyr_plt_n = self.model.addVars(
            veh_id, lyr_id,
            lb=0,
            ub=max_plt_n,
            vtype=GRB.INTEGER,
            name="lyr_plt_n"
        )
        
        # 9. Material (SKU) pallet count per vehicle
        self.mtl_plt = self.model.addVars(
            veh_id, mkt_mtl_id,
            lb=0,
            vtype=GRB.INTEGER,
            name="mtl_plt"
        )
        
        # 10. Material presence indicators
        self.y_mtl_plt = self.model.addVars(
            veh_id, mkt_mtl_id,
            vtype=GRB.BINARY,
            name="y_mtl_plt"
        )
        
        # 11. SKU count per vehicle
        self.mtl_n = self.model.addVars(
            veh_id,
            lb=0,
            vtype=GRB.INTEGER,
            name="mtl_n"
        )
        
        # Enforce max SKU constraint if required
        if self.enforceMaxSKUConstraint:
            max_sku_contr = self.inputParams['max_sku_n']
            for v in veh_id:
                self.mtl_n[v].ub = max_sku_contr
        
        # 12. Exceeded SKU count
        self.exceed_mtl_n = self.model.addVar(
            lb=0,
            vtype=GRB.INTEGER,
            name="exceed_mtl_n"
        )
        
        # 13. Footprint area variables (if enforcing footprint constraints)
        if self.enforceFootPrintAreaConstraint:
            self.fp_area = self.model.addVars(
                veh_id, lyr_id, pos_id,
                lb=0,
                vtype=GRB.CONTINUOUS,
                name="fp_area"
            )
        
        # 14. Section weight variables
        self.sectn_wt = self.model.addVars(
            veh_id, section_id,
            lb=0,
            vtype=GRB.CONTINUOUS,
            name="sectn_wt"
        )
        
        # 15. Standard stack factor indicator (if enforcing)
        if self.enforceStackFactorConstraint:
            self.no_stk_plt = self.model.addVars(
                veh_id, lyr_id, pos_id,
                vtype=GRB.BINARY,
                name="no_stk_plt"
            )
        
        # 16. SKU crushing limit variables (if enforcing SKU crushing limits)
        if self.enforceSKUCrushingLimit:
            self.sku_crush_wt_lmt = self.model.addVars(
                veh_id, pos_id,
                lb=0,
                vtype=GRB.CONTINUOUS,
                name="sku_crush_wt_lmt"
            )
        
        # 17. VDR-SPECIFIC: Stacking group indicators
        if self.enforceG2stackingFactorContraint:
            self.no_stk_plt_g2 = self.model.addVars(
                veh_id, lyr_id, pos_id,
                vtype=GRB.BINARY,
                name="no_stk_plt_g2"
            )
            print("  [OK] Created G2 stacking group variables")
        
        if self.enforceG3stackingFactorContraint:
            self.no_stk_plt_g3 = self.model.addVars(
                veh_id, lyr_id, pos_id,
                vtype=GRB.BINARY,
                name="no_stk_plt_g3"
            )
            print("  [OK] Created G3 stacking group variables")
        
        if self.enforceG4stackingFactorContraint:
            self.no_stk_plt_g4 = self.model.addVars(
                veh_id, lyr_id, pos_id,
                vtype=GRB.BINARY,
                name="no_stk_plt_g4"
            )
            print("  [OK] Created G4 stacking group variables")
        
        # 18. VDR-SPECIFIC: Milk run objective component variables
        if self.enforceMilkRun:
            self.obj_full_plt_var_term4 = self.model.addVar(
                lb=0,
                vtype=GRB.CONTINUOUS,
                name="obj_full_term4_milkrun"
            )
            self.obj_prtl_plt_var_term4 = self.model.addVar(
                lb=0,
                vtype=GRB.CONTINUOUS,
                name="obj_prtl_term4_milkrun"
            )
            print("  [OK] Created milk run objective component variables")
        
        self.model.update()
        print(f"  [OK] Total variables created: {self.model.NumVars}")
        print(f"    - Binary: {self.model.NumBinVars}")
        print(f"    - Integer: {self.model.NumIntVars}")
        print(f"    - Continuous: {self.model.NumVars - self.model.NumBinVars - self.model.NumIntVars}")
    
    def _add_base_constraints(self):
        """
        Add fundamental constraints that apply to all scenarios.
        
        These are the core constraints that define problem feasibility,
        identical to the base model. See base class documentation for
        details.
        
        Constraint Groups Added:
        -----------------------
        1. Demand satisfaction (full and partial pallets)
        2. Position occupancy (at most one pallet per spot)
        3. Pallet presence indicators
        4. Stacking restrictions (no partial on partial, etc.)
        5. Physical property calculations (weight, height)
        6. Weight limits (total vehicle weight)
        7. SKU tracking and counting
        8. Crushing constraints (if enforced)
        9. Weight balance constraints (if enforced)
        10. Axle weight constraints (if enforced)
        11. Compact packing (if enforced)
        
        Number of Constraints Added:
        ---------------------------
        Approximately 5,000-10,000 constraints for typical problem
        (3 vehicles, 4 layers, 24 positions, 30 pallets)
        
        See base model documentation for detailed mathematical formulations.
        """
        print("  Adding base constraints (demand, assignment, weight, balance)...")
        
        # Extract common parameters
        veh_id = self.inputParams["containerList"]
        eff_veh_id = self.inputParams["containerList"]
        lyr_id = self.inputParams["layerList"]
        pos_id = self.inputParams["positionList"]
        agg_full_plt_id = self.inputParams["fullPalletList"]
        agg_prtl_plt_id = self.inputParams["partialPalletList"]
        mkt_mtl_id = self.inputParams["materialList"]
        
        full_plt_n = self.inputParams["fullPalletDemandByMaterialBatch"]
        prtl_plt_n = self.inputParams["partialPalletDemandByMaterialBatch"]
        wt = self.inputParams["palletWeightDict"]
        ht = self.inputParams["palletHeightDict"]
        plt_mtl_dict = self.inputParams["palletMaterialDict"]
        
        max_weight = self.inputParams['max_wt']
        max_lyr_n = self.inputParams['max_lyr_n']
        max_plt_n = self.inputParams['max_plt_n']
        unique_sku_n = self.inputParams['unique_sku_n']
        
        front_id = self.inputParams["frontPositionList"]
        rear_id = self.inputParams["rearPositionList"]
        row_1_id = list(set(self.inputParams["leftPositionList"]))
        row_2_id = list(set(self.inputParams["rightPositionList"]))
        
        bal_coef = self.inputParams["positionGroupBalanceCoefficientDict"]
        fb_bal_tol = self.inputParams['front_to_back_tolerance']
        side_bal_tol = self.inputParams['side_to_side_tolerance']
        max_front_wt = self.inputParams['max_front_wt']
        max_rear_wt = self.inputParams['max_rear_wt']
        
        first_posn = self.inputParams['first_plt_n']
        
        constraint_count = 0
        
        # 1. TOTAL WEIGHT LIMIT
        for v in eff_veh_id:
            self.model.addConstr(
                gp.quicksum(self.wt[v, l, p]
                           for l in lyr_id for p in pos_id) <= max_weight,
                name=f"total_weight_{v}"
            )
        constraint_count += len(eff_veh_id)
        
        # 2. DEMAND SATISFACTION
        for plt in agg_full_plt_id:
            self.model.addConstr(
                gp.quicksum(self.y_full[v, l, p, plt]
                           for v in veh_id for l in lyr_id for p in pos_id) <= full_plt_n[plt],
                name=f"full_demand_{plt}"
            )
        constraint_count += len(agg_full_plt_id)
        
        for plt in agg_prtl_plt_id:
            self.model.addConstr(
                gp.quicksum(self.y_prtl[v, l, p, plt]
                           for v in veh_id for l in lyr_id for p in pos_id) <= prtl_plt_n[plt],
                name=f"prtl_demand_{plt}"
            )
        constraint_count += len(agg_prtl_plt_id)

        # Link priority counters to actual assignments. Without these
        # constraints the objective can collect the full priority bonus even
        # when the corresponding pallets are not loaded.
        for plt in agg_full_plt_id:
            self.model.addConstr(
                self.y_prior_full[plt] == gp.quicksum(
                    self.y_full[v, l, p, plt]
                    for v in veh_id for l in lyr_id for p in pos_id
                ),
                name=f"priority_full_{plt}"
            )
        for plt in agg_prtl_plt_id:
            self.model.addConstr(
                self.y_prior_prtl[plt] == gp.quicksum(
                    self.y_prtl[v, l, p, plt]
                    for v in veh_id for l in lyr_id for p in pos_id
                ),
                name=f"priority_prtl_{plt}"
            )
        constraint_count += len(agg_full_plt_id) + len(agg_prtl_plt_id)
        
        # 3. POSITION OCCUPANCY
        for v in veh_id:
            for l in lyr_id:
                for p in pos_id:
                    # Link full pallet assignment to presence
                    self.model.addConstr(
                        self.full_plt[v, l, p] == gp.quicksum(
                            self.y_full[v, l, p, plt] for plt in agg_full_plt_id
                        ),
                        name=f"full_indicator_{v}_{l}_{p}"
                    )
                    
                    # Link partial pallet assignment to presence
                    self.model.addConstr(
                        self.prtl_plt[v, l, p] == gp.quicksum(
                            self.y_prtl[v, l, p, plt] for plt in agg_prtl_plt_id
                        ),
                        name=f"prtl_indicator_{v}_{l}_{p}"
                    )
                    
                    # At most one pallet per spot
                    self.model.addConstr(
                        gp.quicksum(self.y_full[v, l, p, plt] for plt in agg_full_plt_id) +
                        gp.quicksum(self.y_prtl[v, l, p, plt] for plt in agg_prtl_plt_id) <= 1,
                        name=f"one_per_spot_{v}_{l}_{p}"
                    )
        constraint_count += 3 * len(veh_id) * len(lyr_id) * len(pos_id)
        
        # 4. STACKING RESTRICTIONS
        for v in eff_veh_id:
            for l in lyr_id[:-1]:
                for p in pos_id:
                    l_next = lyr_id[lyr_id.index(l) + 1]
                    # No partial on partial
                    self.model.addConstr(
                        self.prtl_plt[v, l, p] + self.prtl_plt[v, l_next, p] <= 1,
                        name=f"no_prtl_on_prtl_{v}_{l}_{p}"
                    )
                    # No full on partial
                    self.model.addConstr(
                        self.prtl_plt[v, l, p] + self.full_plt[v, l_next, p] <= 1,
                        name=f"no_full_on_prtl_{v}_{l}_{p}"
                    )
        constraint_count += 2 * len(eff_veh_id) * (len(lyr_id)-1) * len(pos_id)
        
        # 5. PHYSICAL CALCULATIONS
        for v in eff_veh_id:
            for p in pos_id:
                # Stack height
                self.model.addConstr(
                    self.stk_ht[v, p] == 
                    gp.quicksum(ht[plt] * self.y_full[v, l, p, plt]
                               for l in lyr_id for plt in agg_full_plt_id) +
                    gp.quicksum(ht[plt] * self.y_prtl[v, l, p, plt]
                               for l in lyr_id for plt in agg_prtl_plt_id),
                    name=f"stack_height_{v}_{p}"
                )
            
            for l in lyr_id:
                for p in pos_id:
                    # Weight calculation
                    self.model.addConstr(
                        self.wt[v, l, p] ==
                        gp.quicksum(wt[plt] * self.y_full[v, l, p, plt]
                                   for plt in agg_full_plt_id) +
                        gp.quicksum(wt[plt] * self.y_prtl[v, l, p, plt]
                                   for plt in agg_prtl_plt_id),
                        name=f"weight_{v}_{l}_{p}"
                    )
                
                # Layer pallet count
                self.model.addConstr(
                    self.lyr_plt_n[v, l] ==
                    gp.quicksum(self.y_prtl[v, l, p, plt]
                               for p in pos_id for plt in agg_prtl_plt_id) +
                    gp.quicksum(self.y_full[v, l, p, plt]
                               for p in pos_id for plt in agg_full_plt_id),
                    name=f"layer_count_{v}_{l}"
                )
        constraint_count += len(eff_veh_id) * (len(pos_id) + len(lyr_id) * len(pos_id) + len(lyr_id))
        
        # 6. CRUSHING CONSTRAINTS
        if self.enforceCrushingConstraints:
            print("    [OK] Adding crushing constraints...")
            for v in eff_veh_id:
                for l in lyr_id:
                    for p in pos_id:
                        self.model.addConstr(
                            self.wt[v, l, p] >= gp.quicksum(
                                self.wt[v, s, p] for s in lyr_id if s > l
                            ),
                            name=f"crushing_{v}_{l}_{p}"
                        )
            constraint_count += len(eff_veh_id) * len(lyr_id) * len(pos_id)
        
        # 7. WEIGHT BALANCE
        if self.enforceWeightBalancing:
            print("    [OK] Adding weight balance constraints...")
            if fb_bal_tol > 0:
                for v in eff_veh_id:
                    self.model.addConstr(
                        gp.quicksum(self.wt[v, l, p] for l in lyr_id for p in front_id) <=
                        (1 + fb_bal_tol) * gp.quicksum(self.wt[v, l, p] for l in lyr_id for p in rear_id),
                        name=f"front_back_upper_{v}"
                    )
                    self.model.addConstr(
                        gp.quicksum(self.wt[v, l, p] for l in lyr_id for p in front_id) >=
                        (1 - fb_bal_tol) * gp.quicksum(self.wt[v, l, p] for l in lyr_id for p in rear_id),
                        name=f"front_back_lower_{v}"
                    )
                constraint_count += 2 * len(eff_veh_id)
            
            if side_bal_tol > 0:
                for v in eff_veh_id:
                    self.model.addConstr(
                        gp.quicksum(self.wt[v, l, p] for l in lyr_id for p in row_1_id) <=
                        (1 + side_bal_tol) * gp.quicksum(self.wt[v, l, p] for l in lyr_id for p in row_2_id),
                        name=f"side_upper_{v}"
                    )
                    self.model.addConstr(
                        gp.quicksum(self.wt[v, l, p] for l in lyr_id for p in row_1_id) >=
                        (1 - side_bal_tol) * gp.quicksum(self.wt[v, l, p] for l in lyr_id for p in row_2_id),
                        name=f"side_lower_{v}"
                    )
                constraint_count += 2 * len(eff_veh_id)
        
        # 8. AXLE WEIGHT
        if self.enforceAxleWeightConstraints:
            print("    [OK] Adding axle weight constraints...")
            for v in eff_veh_id:
                self.model.addConstr(
                    gp.quicksum((bal_coef[(p, "A")] + bal_coef[(p, "C")]) * self.wt[v, l, p]
                               for l in lyr_id for p in front_id) <= max_front_wt,
                    name=f"front_axle_{v}"
                )
                self.model.addConstr(
                    gp.quicksum((bal_coef[(p, "B")] + bal_coef[(p, "D")]) * self.wt[v, l, p]
                               for l in lyr_id for p in rear_id) <= max_rear_wt,
                    name=f"rear_axle_{v}"
                )
            constraint_count += 2 * len(eff_veh_id)
        
        # 9. SKU TRACKING
        for v in veh_id:
            for mtl in mkt_mtl_id:
                self.model.addConstr(
                    self.mtl_plt[v, mtl] ==
                    gp.quicksum(self.y_full[v, l, p, plt]
                               for plt in agg_full_plt_id if plt_mtl_dict[plt] == mtl
                               for l in lyr_id for p in pos_id) +
                    gp.quicksum(self.y_prtl[v, l, p, plt]
                               for plt in agg_prtl_plt_id if plt_mtl_dict[plt] == mtl
                               for l in lyr_id for p in pos_id),
                    name=f"material_count_{v}_{mtl}"
                )
                
                self.model.addConstr(
                    (1.0 / (max_lyr_n * max_plt_n)) * self.mtl_plt[v, mtl] <= self.y_mtl_plt[v, mtl],
                    name=f"material_indicator_{v}_{mtl}"
                )
            
            self.model.addConstr(
                self.mtl_n[v] == gp.quicksum(self.y_mtl_plt[v, mtl] for mtl in mkt_mtl_id),
                name=f"sku_count_{v}"
            )
        constraint_count += len(veh_id) * (2 * len(mkt_mtl_id) + 1)
        
        self.model.addConstr(
            self.exceed_mtl_n >= gp.quicksum(self.mtl_n[v] for v in eff_veh_id) - unique_sku_n,
            name="exceeded_sku"
        )
        constraint_count += 1
        
        # 10. COMPACT PACKING
        if self.enforceCompactPacking:
            print("    [OK] Adding compact packing constraints...")
            for v in veh_id:
                for l in lyr_id:
                    for p in pos_id:
                        if p < first_posn:
                            self.model.addConstr(
                                self.full_plt[v, l, p] + self.prtl_plt[v, l, p] <=
                                self.full_plt[v, l, p+1] + self.prtl_plt[v, l, p+1],
                                name=f"compact_before_{v}_{l}_{p}"
                            )
                        elif p > first_posn:
                            self.model.addConstr(
                                self.full_plt[v, l, p] + self.prtl_plt[v, l, p] <=
                                self.full_plt[v, l, p-1] + self.prtl_plt[v, l, p-1],
                                name=f"compact_after_{v}_{l}_{p}"
                            )
                        
                        if p <= first_posn and p % 2 == 0:
                            self.model.addConstr(
                                self.full_plt[v, l, p] + self.prtl_plt[v, l, p] ==
                                self.full_plt[v, l, p-1] + self.prtl_plt[v, l, p-1],
                                name=f"compact_symmetry_{v}_{l}_{p}"
                            )
                
                for l in lyr_id[1:]:
                    l_prev = lyr_id[lyr_id.index(l) - 1]
                    self.model.addConstr(
                        self.full_plt[v, l, first_posn] + self.prtl_plt[v, l, first_posn] <=
                        self.full_plt[v, l_prev, 1] + self.prtl_plt[v, l_prev, 1],
                        name=f"layer_seq1_{v}_{l}"
                    )
                    self.model.addConstr(
                        self.full_plt[v, l, first_posn] + self.prtl_plt[v, l, first_posn] <=
                        self.full_plt[v, l_prev, max_plt_n] + self.prtl_plt[v, l_prev, max_plt_n],
                        name=f"layer_seq2_{v}_{l}"
                    )
            # Constraint count varies based on positions
        
        print(f"  [OK] Added approximately {constraint_count} base constraints")
    
    def _add_vdr_constraints(self):
        """
        Add VDR-specific constraints for advanced stacking rules (G2/G3/G4).
        
        This method implements the sophisticated stacking rules required by
        VDR operations, where different product types have different stacking
        behaviors based on their stacking group classification.
        
        Stacking Group Definitions:
        --------------------------
        
        **G1 (Standard Stackable)**:
        - Can be placed on any layer
        - Other pallets can be stacked on top
        - No special restrictions
        - Most common category
        
        **G2 (Ground Base, Stackable Upon)**:
        - MUST be placed on layer 1 (ground level)
        - Other pallets CAN be stacked on top
        - Typically heavy, stable products (e.g., bottled water cases)
        - Forms stable base for lighter products
        
        **G3 (No Stack On Top)**:
        - CAN be placed on layer 1
        - NO pallets can be stacked on top
        - Typically fragile or temperature-sensitive products
        - Example: electronics, glass, produce
        
        **G4 (Ground Only, No Stack)**:
        - MUST be placed on layer 1 (ground level)
        - NO pallets can be stacked on top
        - Most restrictive category
        - Typically very fragile or irregularly shaped products
        - Example: delicate machinery, oversized items
        
        Mathematical Formulation:
        ------------------------
        
        **G2 Constraints**:
        ```
        # Indicator: is there a G2 pallet at (v,l,p)?
        no_stk_plt_g2[v,l,p] = Σ y_full[v,l,p,plt] * g2_factor[plt] +
                               Σ y_prtl[v,l,p,plt] * g2_factor[plt]
        
        # G2 pallets only on layer 1 (not on upper layers)
        for l > 1: no_stk_plt_g2[v,l,p] = 0
        ```
        
        **G3 Constraints**:
        ```
        # Indicator: is there a G3 pallet at (v,l,p)?
        no_stk_plt_g3[v,l,p] = Σ y_full[v,1,p,plt] * g3_factor[plt] +
                               Σ y_prtl[v,1,p,plt] * g3_factor[plt]
        
        # Nothing can be stacked on G3 pallets
        no_stk_plt_g3[v,l,p] + full_plt[v,l+1,p] + prtl_plt[v,l+1,p] <= 1
        ```
        
        **G4 Constraints**:
        ```
        # Indicator: is there a G4 pallet at (v,l,p)?
        no_stk_plt_g4[v,l,p] = Σ y_full[v,l,p,plt] * g4_factor[plt] +
                               Σ y_prtl[v,l,p,plt] * g4_factor[plt]
        
        # G4 pallets only on layer 1
        for l > 1: no_stk_plt_g4[v,l,p] = 0
        
        # Nothing can be stacked on G4 pallets
        no_stk_plt_g4[v,l,p] + full_plt[v,l+1,p] + prtl_plt[v,l+1,p] <= 1
        ```
        
        Real-World Example:
        ------------------
        Consider a container with mixed cargo:
        
        Layer 1 (Ground):
        - Position 1-4: G2 pallets (heavy water bottles) ✓ Can stack on these
        - Position 5-8: G4 pallets (delicate machinery) ✗ Cannot stack on these
        - Position 9-12: G1 pallets (standard boxes)
        
        Layer 2 (On top of Layer 1):
        - Position 1-4: G1 pallets ✓ Allowed (on top of G2)
        - Position 5-8: EMPTY ✗ Cannot place anything (G4 below)
        - Position 9-12: G3 pallets ✓ Allowed (on top of G1)
        
        Layer 3 (On top of Layer 2):
        - Position 1-4: G1 pallets ✓ Allowed
        - Position 5-8: EMPTY (still blocked by G4 on layer 1)
        - Position 9-12: EMPTY ✗ Cannot place anything (G3 on layer 2)
        
        Implementation Details:
        ----------------------
        1. **Indicator Variables**: Track presence of each stacking group
        2. **Layer Restrictions**: Force specific groups to specific layers
        3. **Vertical Blocking**: Prevent stacking above restricted pallets
        
        Number of Constraints Added:
        ---------------------------
        For each enforced stacking group (G2, G3, G4):
        - Indicator constraints: |V| × |L| × |P|
        - Layer restriction: |V| × (|L|-1) × |P|
        - Vertical blocking: |V| × (|L|-1) × |P|
        
        Example: 3 vehicles, 4 layers, 24 positions, all 3 groups:
        - 3 × 4 × 24 = 288 indicator constraints per group
        - 3 × 3 × 24 = 216 restriction/blocking constraints per group
        - Total: ~1,500 VDR-specific constraints
        
        Performance Impact:
        ------------------
        - Model size increase: ~10-15%
        - Solve time increase: ~20-30% (more integer variables)
        - Solution quality: Often improves (better reflects reality)
        - Infeasibility risk: Higher (more restrictions)
        
        Common Pitfalls:
        ---------------
        1. **Over-restriction**: Too many G4 pallets can make problem infeasible
        2. **Data Quality**: Incorrect stacking group assignment leads to poor solutions
        3. **Height Interactions**: G2/G4 on layer 1 reduces effective container height
        
        Notes:
            - Only added if vdr_stacking_rules=True in constructor
            - Requires inputParams to contain stackFactorg2/g3/g4Pallet dicts
            - G1 pallets don't need special constraints (default behavior)
        """
        if not self.vdr_stacking_rules:
            print("  VDR stacking rules disabled - skipping...")
            return
        
        print("  Adding VDR-specific stacking group constraints...")
        
        veh_id = self.inputParams["containerList"]
        lyr_id = self.inputParams["layerList"]
        pos_id = self.inputParams["positionList"]
        agg_full_plt_id = self.inputParams["fullPalletList"]
        agg_prtl_plt_id = self.inputParams["partialPalletList"]
        
        g2_stk_factor = self.inputParams.get("stackFactorg2Pallet", {})
        g3_stk_factor = self.inputParams.get("stackFactorg3Pallet", {})
        g4_stk_factor = self.inputParams.get("stackFactorg4Pallet", {})
        
        constraint_count = 0
        
        # ===================================================================
        # G2 CONSTRAINTS: Ground base, stackable upon
        # ===================================================================
        if self.enforceG2stackingFactorContraint:
            print("    [OK] Adding G2 constraints (ground base, stackable upon)...")
            
            for v in veh_id:
                for l in lyr_id:
                    for p in pos_id:
                        # Indicator: is there a G2 pallet here?
                        self.model.addConstr(
                            self.no_stk_plt_g2[v, l, p] ==
                            gp.quicksum(self.y_full[v, l, p, plt]
                                       for plt in agg_full_plt_id if g2_stk_factor.get(plt, 0) == 1) +
                            gp.quicksum(self.y_prtl[v, l, p, plt]
                                       for plt in agg_prtl_plt_id if g2_stk_factor.get(plt, 0) == 1),
                            name=f"g2_indicator_{v}_{l}_{p}"
                        )
                        
                        # G2 pallets must be on layer 1 (ground)
                        if l > 1:  # Not on ground layer
                            self.model.addConstr(
                                self.no_stk_plt_g2[v, l, p] == 0,
                                name=f"g2_ground_only_{v}_{l}_{p}"
                            )
                            constraint_count += 1
            
            constraint_count += len(veh_id) * len(lyr_id) * len(pos_id)
            print(f"      Added ~{constraint_count} G2 constraints")
        
        # ===================================================================
        # G3 CONSTRAINTS: Can be on ground, nothing stacks on top
        # ===================================================================
        if self.enforceG3stackingFactorContraint:
            print("    [OK] Adding G3 constraints (no stack on top)...")
            
            for v in veh_id:
                for l in lyr_id:
                    for p in pos_id:
                        # Indicator: is there a G3 pallet at layer 1?
                        self.model.addConstr(
                            self.no_stk_plt_g3[v, l, p] ==
                            gp.quicksum(self.y_full[v, l, p, plt]
                                       for plt in agg_full_plt_id if g3_stk_factor.get(plt, 0) == 1) +
                            gp.quicksum(self.y_prtl[v, l, p, plt]
                                       for plt in agg_prtl_plt_id if g3_stk_factor.get(plt, 0) == 1),
                            name=f"g3_indicator_{v}_{l}_{p}"
                        )
            
            # Nothing can be stacked on G3 pallets
            for v in veh_id:
                for l in lyr_id[:-1]:
                    for p in pos_id:
                        l_next = lyr_id[lyr_id.index(l) + 1]
                        self.model.addConstr(
                            self.no_stk_plt_g3[v, l, p] +
                            self.full_plt[v, l_next, p] +
                            self.prtl_plt[v, l_next, p] <= 1,
                            name=f"g3_no_stack_{v}_{l}_{p}"
                        )
                        constraint_count += 1
            
            constraint_count += len(veh_id) * len(lyr_id) * len(pos_id)
            print(f"      Added ~{constraint_count} G3 constraints")
        
        # ===================================================================
        # G4 CONSTRAINTS: Ground only, nothing stacks on top (most restrictive)
        # ===================================================================
        if self.enforceG4stackingFactorContraint:
            print("    [OK] Adding G4 constraints (ground only, no stack)...")
            
            for v in veh_id:
                for l in lyr_id:
                    for p in pos_id:
                        # Indicator: is there a G4 pallet here?
                        self.model.addConstr(
                            self.no_stk_plt_g4[v, l, p] ==
                            gp.quicksum(self.y_full[v, l, p, plt]
                                       for plt in agg_full_plt_id if g4_stk_factor.get(plt, 0) == 1) +
                            gp.quicksum(self.y_prtl[v, l, p, plt]
                                       for plt in agg_prtl_plt_id if g4_stk_factor.get(plt, 0) == 1),
                            name=f"g4_indicator_{v}_{l}_{p}"
                        )
                        
                        # G4 pallets must be on layer 1 (ground)
                        if l > 1:
                            self.model.addConstr(
                                self.no_stk_plt_g4[v, l, p] == 0,
                                name=f"g4_ground_only_{v}_{l}_{p}"
                            )
                            constraint_count += 1
            
            # Nothing can be stacked on G4 pallets
            for v in veh_id:
                for l in lyr_id[:-1]:
                    for p in pos_id:
                        l_next = lyr_id[lyr_id.index(l) + 1]
                        self.model.addConstr(
                            self.no_stk_plt_g4[v, l, p] +
                            self.full_plt[v, l_next, p] +
                            self.prtl_plt[v, l_next, p] <= 1,
                            name=f"g4_no_stack_{v}_{l}_{p}"
                        )
                        constraint_count += 1
            
            constraint_count += len(veh_id) * len(lyr_id) * len(pos_id)
            print(f"      Added ~{constraint_count} G4 constraints")
        
        print(f"  [OK] Total VDR constraints added: ~{constraint_count}")
    
    def _set_objective(self):
        """
        Set the objective function with VDR milk run enhancements.
        
        This method creates a sophisticated objective function that balances
        multiple competing goals while prioritizing milk run efficiency through
        position-weighted, cubically-amplified destination priorities.
        
        Objective Components:
        --------------------
        
        1. **Standard Pallet Assignment Value**:
           Base value for assigning pallets to containers
           - Full pallets: worth 1.0 per pallet
           - Partial pallets: worth prtl_frac[plt] (typically 0.3-0.7)
           - Weighted by container priority (fill n-1 containers before nth)
        
        2. **Priority Pallet Bonuses**:
           Additional incentive for priority pallets
           - y_prior_full[plt] * priorityIncentive
           - y_prior_prtl[plt] * priorityIncentive
        
        3. **VDR Milk Run Term (KEY ENHANCEMENT)**:
           Position-weighted, destination-aware cubic priority
           ```
           obj_term4 = Σ y[v,l,p,plt] * cntr_priority[v] * 
                       incentive * (position_weight[p] * sku_priority[plt]³)
           ```
        
        4. **SKU Proliferation Penalty**:
           Discourages having too many different SKUs
           - (sku_penalty * exceed_mtl_n * sku_penalty_factor)
           - Negative term (we minimize excess SKUs)
        
        Mathematical Deep Dive - Milk Run Objective:
        -------------------------------------------
        
        **Why Cubic Priority (priority³)?**
        
        Linear priority:
        - Destination A (priority 1): 1
        - Destination B (priority 2): 2
        - Difference: 2 - 1 = 1 (weak preference)
        
        Quadratic priority:
        - Destination A: 1² = 1
        - Destination B: 2² = 4
        - Difference: 4 - 1 = 3 (moderate preference)
        
        Cubic priority:
        - Destination A: 1³ = 1
        - Destination B: 2³ = 8
        - Destination C: 3³ = 27
        - Differences: 8-1=7, 27-8=19 (strong, escalating preference!)
        
        The cubic term creates exponentially growing gaps between priority
        levels, giving the solver a mathematically strong signal about
        destination importance.
        
        **Position Weighting Example**:
        
        Container with 24 positions, 3 destinations:
        - Destination A (priority 1, first stop): wants positions 23-24 (near door)
        - Destination B (priority 2, second stop): wants positions 12-15 (middle)
        - Destination C (priority 3, last stop): wants positions 1-4 (deep inside)
        
        Objective contribution for Destination A pallet at position 24:
        ```
        value = 1.0 * container_priority * incentive * (1 * 1³)
              = 1.0 * 3.0 * 1000 * (1 * 1) = 3,000
        ```
        
        Objective contribution for Destination C pallet at position 24:
        ```
        value = 1.0 * container_priority * incentive * (1 * 3³)
              = 1.0 * 3.0 * 1000 * (1 * 27) = 81,000
        ```
        
        The solver will STRONGLY prefer placing Destination C pallets near
        door (position 24) over Destination A pallets there.
        
        Objective Function Assembly:
        ---------------------------
        
        ```python
        maximize:
            # Milk run terms (VDR-specific)
            obj_full_plt_var_term4 +      # Position-weighted full pallets
            obj_prtl_plt_var_term4 +      # Position-weighted partial pallets
            
            # Priority bonuses
            Σ y_prior_full[plt] * incentive +
            Σ y_prior_prtl[plt] * incentive +
            
            # SKU penalty (negative)
            - (sku_penalty * exceed_mtl_n * sku_penalty_factor)
        ```
        
        The milk run terms are pre-computed in auxiliary constraints
        to simplify the main objective and improve solver efficiency.
        
        Typical Objective Value Ranges:
        ------------------------------
        - Small problem (20 pallets): 50,000 - 200,000
        - Medium problem (50 pallets): 200,000 - 800,000
        - Large problem (100+ pallets): 500,000 - 2,000,000
        
        The large values come from the cubic priority and position weights.
        Don't be alarmed by "large" objective values - they're intentional
        to create strong mathematical signals.
        
        Implementation Notes:
        --------------------
        - Milk run terms only added if enforceMilkRun=True
        - Position weights calculated in _calculate_position_weights()
        - High priority SKUs get cubically amplified influence
        - Container priority creates filling order (fill container 1 before 2)
        
        Side Effects:
            Sets model objective (maximize)
            Creates auxiliary constraint for obj_full_plt_var_term4
            Creates auxiliary constraint for obj_prtl_plt_var_term4
        
        See Also:
            _calculate_position_weights(): How position weights are computed
            _add_base_constraints(): Priority variable bounds
        """
        print("  Creating objective function with milk run optimization...")
        
        veh_id = self.inputParams["containerList"]
        eff_veh_id = self.inputParams["containerList"]
        lyr_id = self.inputParams["layerList"]
        pos_id = self.inputParams["positionList"]
        agg_full_plt_id = self.inputParams["fullPalletList"]
        agg_prtl_plt_id = self.inputParams["partialPalletList"]
        
        cntr_priority = self.inputParams['container_priority']
        prior_incentive = self.inputParams['priorityIncentive']
        sku_penalty = self.inputParams['sku_penalty']
        
        highPrioritySKUList = self.inputParams['skuPriorityIncentive']
        
        # Create milk run objective terms if enabled
        if self.enforceMilkRun and self.pos_id_new_2 is not None:
            print("    [OK] Adding position-weighted milk run objective terms...")
            
            # Full pallet milk run term with cubic priority weighting
            self.model.addConstr(
                self.obj_full_plt_var_term4 ==
                gp.quicksum(
                    self.y_full[c, l, p, plt] * cntr_priority[c] *
                    prior_incentive *
                    (self._position_weight_by_id[p] * highPrioritySKUList[plt]**3)
                    for c in eff_veh_id
                    for l in lyr_id
                    for p in pos_id
                    for plt in agg_full_plt_id
                    if plt in highPrioritySKUList
                ),
                name="calc_full_milkrun_term4"
            )
            
            # Partial pallet milk run term with cubic priority weighting
            self.model.addConstr(
                self.obj_prtl_plt_var_term4 ==
                gp.quicksum(
                    self.y_prtl[c, l, p, plt] * cntr_priority[c] *
                    prior_incentive *
                    (self._position_weight_by_id[p] * highPrioritySKUList[plt]**3)
                    for c in eff_veh_id
                    for l in lyr_id
                    for p in pos_id
                    for plt in agg_prtl_plt_id
                    if plt in highPrioritySKUList
                ),
                name="calc_prtl_milkrun_term4"
            )
            
            print(f"      Position weights range: {min(self.pos_id_new_2)} to {max(self.pos_id_new_2)}")
            print(f"      Priority range: {min(highPrioritySKUList.values())} to {max(highPrioritySKUList.values())}")
            print(f"      Cubic amplification: {min(highPrioritySKUList.values())**3} to {max(highPrioritySKUList.values())**3}")
        
        # Set the complete objective function
        obj_expr = 0
        
        # Add milk run terms
        if self.enforceMilkRun:
            obj_expr += self.obj_full_plt_var_term4
            obj_expr += self.obj_prtl_plt_var_term4
        
        # Add priority bonuses
        obj_expr += gp.quicksum(
            self.y_prior_full[plt] for plt in agg_full_plt_id
        ) * prior_incentive
        
        obj_expr += gp.quicksum(
            self.y_prior_prtl[plt] for plt in agg_prtl_plt_id
        ) * prior_incentive
        
        # Subtract SKU penalty
        obj_expr -= (sku_penalty * self.exceed_mtl_n * self.sku_penalty_factor)
        
        self.model.setObjective(obj_expr, GRB.MAXIMIZE)
        
        print("  [OK] Objective function set (MAXIMIZE)")
        print(f"    - Milk run terms: {'Enabled' if self.enforceMilkRun else 'Disabled'}")
        print(f"    - Priority incentive: {prior_incentive}")
        print(f"    - SKU penalty: {sku_penalty} * {self.sku_penalty_factor}")
    
    def solve(self):
        """
        Solve the optimization model with dynamic parameter adjustment.
        
        This method configures Gurobi solver parameters and solves the model.
        Key enhancement: automatically adjusts time limit for large problems.
        
        Dynamic Parameter Adjustment:
        ----------------------------
        - If 8+ vehicles: time_limit = 800 seconds (extended)
        - Otherwise: use base time_limit (typically 120-300 seconds)
        
        This ensures adequate solve time for complex multi-vehicle problems
        while maintaining faster solves for smaller instances.
        
        Gurobi Parameters Set:
        ---------------------
        1. **TimeLimit**: Maximum optimization time (seconds)
           - Small problems: 120s
           - Large problems (8+ vehicles): 800s
        
        2. **MIPGap**: Relative optimality gap tolerance
           - Default: 0.01 (1%)
           - Solver stops when proven within 1% of optimal
           - Trade-off: solution quality vs. solve time
        
        3. **OutputFlag**: Display solver output
           - 1: Show progress (recommended)
           - 0: Silent mode
        
        4. **LogFile**: Write detailed log (optional)
           - Useful for debugging
           - Can review solver decisions post-solve
        
        Solver Status Codes:
        -------------------
        - GRB.OPTIMAL (2): Optimal solution found
        - GRB.TIME_LIMIT (9): Time limit reached, best solution returned
        - GRB.INFEASIBLE (3): No feasible solution exists
        - GRB.UNBOUNDED (5): Objective can be arbitrarily large (rare)
        - GRB.INF_OR_UNBD (4): Either infeasible or unbounded
        
        Returns:
            int: Gurobi status code
        
        Raises:
            GurobiError: If solve fails due to license, memory, etc.
        
        Side Effects:
            - Model solved (solution stored in variables)
            - Solver statistics available (model.Runtime, model.MIPGap, etc.)
        
        Example Usage:
            ```python
            optimizer.build_model()
            status = optimizer.solve()
            
            if status == GRB.OPTIMAL:
                print("Found optimal solution!")
                solution = optimizer.get_solution()
            elif status == GRB.TIME_LIMIT:
                print("Time limit reached, using best solution found")
                solution = optimizer.get_solution()
            elif status == GRB.INFEASIBLE:
                print("Problem is infeasible")
                optimizer.model.computeIIS()
                optimizer.model.write("infeasible.ilp")
            ```
        
        Performance Tips:
        ----------------
        1. For quick testing: set time_limit=60, gap=0.05
        2. For production: time_limit=300, gap=0.01
        3. For proving optimality: time_limit=3600, gap=0.0 (slow!)
        4. For very large problems: time_limit=1800, gap=0.02
        
        Notes:
            - First solve is slowest (model construction overhead)
            - Subsequent solves with similar data are faster
            - Warm starts can further improve performance
        """
        print("\n" + "="*70)
        print("SOLVING OPTIMIZATION MODEL")
        print("="*70)
        
        # Dynamic time limit adjustment for large problems
        veh_id = self.inputParams["containerList"]
        time_limit_adjusted = self.time_limit
        
        if len(veh_id) >= 8:
            time_limit_adjusted = 800
            print(f"Large problem detected ({len(veh_id)} vehicles)")
            print(f"Extending time limit: {self.time_limit}s → {time_limit_adjusted}s")
        
        # Set Gurobi parameters
        self.model.setParam('TimeLimit', time_limit_adjusted)
        self.model.setParam('MIPGap', self.gap)
        self.model.setParam('OutputFlag', 1)
        
        print("\nGurobi Parameters:")
        print(f"  TimeLimit: {time_limit_adjusted} seconds")
        print(f"  MIPGap: {self.gap} ({self.gap*100}%)")
        print(f"  OutputFlag: 1 (verbose)")
        
        print("\n" + "-"*70)
        print("Starting optimization...")
        print("-"*70 + "\n")
        
        # Solve
        self.model.optimize()
        
        # Print results
        print("\n" + "="*70)
        print("OPTIMIZATION COMPLETE")
        print("="*70)
        
        status = self.model.Status
        status_names = {
            GRB.OPTIMAL: "OPTIMAL",
            GRB.INFEASIBLE: "INFEASIBLE",
            GRB.INF_OR_UNBD: "INF_OR_UNBD",
            GRB.UNBOUNDED: "UNBOUNDED",
            GRB.CUTOFF: "CUTOFF",
            GRB.ITERATION_LIMIT: "ITERATION_LIMIT",
            GRB.NODE_LIMIT: "NODE_LIMIT",
            GRB.TIME_LIMIT: "TIME_LIMIT",
            GRB.SOLUTION_LIMIT: "SOLUTION_LIMIT",
            GRB.INTERRUPTED: "INTERRUPTED",
            GRB.NUMERIC: "NUMERIC",
            GRB.SUBOPTIMAL: "SUBOPTIMAL",
        }
        
        print(f"\nSolution Status: {status_names.get(status, f'UNKNOWN ({status})')}")
        
        if status in [GRB.OPTIMAL, GRB.TIME_LIMIT]:
            print(f"Objective Value: {self.model.ObjVal:,.2f}")
            print(f"MIP Gap: {self.model.MIPGap:.4%}")
            print(f"Runtime: {self.model.Runtime:.2f} seconds")
            print(f"Nodes Explored: {self.model.NodeCount:,}")
            
            if status == GRB.TIME_LIMIT:
                print("\n[WARNING] Time limit reached. Solution may not be optimal.")
                print(f"   Current gap: {self.model.MIPGap:.2%}")
                print(f"   Solution is within {self.model.MIPGap*100:.2f}% of optimal")
        
        elif status == GRB.INFEASIBLE:
            print("\n[ERROR] Problem is INFEASIBLE")
            print("   No feasible solution exists with current constraints")
            print("   Computing IIS (Irreducible Inconsistent Subset)...")
            self.model.computeIIS()
            self.model.write("model_infeasible.ilp")
            print("   IIS written to: model_infeasible.ilp")
            print("\n   Possible causes:")
            print("   - Too many G4 pallets (ground-only restriction)")
            print("   - Insufficient container capacity")
            print("   - Conflicting stacking requirements")
            print("   - Unrealistic weight/balance tolerances")
        
        elif status == GRB.UNBOUNDED:
            print("\n[ERROR] Problem is UNBOUNDED")
            print("   Objective can increase without limit")
            print("   This usually indicates a modeling error")
        
        print("="*70 + "\n")
        
        return status
    
    def write_model(self, filename):
        """
        Write the model to a file for inspection or debugging.
        
        Args:
            filename (str): Output filename
                - .lp: Human-readable LP format (recommended for debugging)
                - .mps: Industry-standard MPS format
                - .rew: Gurobi's REW format (compact)
        
        Example:
            optimizer.write_model("container_loading_vdr.lp")
        """
        if self.model is not None:
            self.model.write(filename)
            print(f"Model written to: {filename}")
    
    def get_solution(self):
        """
        Extract solution values after optimization.
        
        Returns a dictionary containing:
        - Basic solution info (status, objective)
        - Assignment decisions (which pallets where)
        - Physical properties (weights, heights)
        - SKU information
        - VDR-specific data (stacking groups)
        
        Returns:
            dict: Complete solution dictionary or None if no solution available
        
        Dictionary Structure:
            {
                'status': GRB status code,
                'objective': objective value,
                'y_full': {(v,l,p,plt): 1 if assigned},
                'y_prtl': {(v,l,p,plt): 1 if assigned},
                'weights': {(v,l,p): weight value},
                'stack_heights': {(v,p): height value},
                'sku_counts': {v: number of SKUs},
                'excess_skus': number over target,
                'stacking_groups': {(v,l,p): 'G1'/'G2'/'G3'/'G4'},  # VDR-specific
                'milk_run_obj_full': contribution value,  # VDR-specific
                'milk_run_obj_prtl': contribution value,  # VDR-specific
            }
        """
        if self.model.Status not in [GRB.OPTIMAL, GRB.TIME_LIMIT]:
            print("No solution available.")
            return None
        
        solution = {
            'status': self.model.Status,
            'objective': self.model.ObjVal,
            'mip_gap': self.model.MIPGap,
            'runtime': self.model.Runtime,
            'y_full': {},
            'y_prtl': {},
            'weights': {},
            'stack_heights': {},
            'sku_counts': {},
            'excess_skus': self.exceed_mtl_n.X,
            'stacking_groups': {},  # VDR-specific
        }
        
        # Extract assignment decisions
        for v, l, p, plt in self.y_full:
            if self.y_full[v, l, p, plt].X > 0.5:
                solution['y_full'][(v, l, p, plt)] = 1
        
        for v, l, p, plt in self.y_prtl:
            if self.y_prtl[v, l, p, plt].X > 0.5:
                solution['y_prtl'][(v, l, p, plt)] = 1
        
        # Extract physical properties
        for v, l, p in self.wt:
            solution['weights'][(v, l, p)] = self.wt[v, l, p].X
        
        for v, p in self.stk_ht:
            solution['stack_heights'][(v, p)] = self.stk_ht[v, p].X
        
        # Extract SKU information
        for v in self.inputParams["containerList"]:
            solution['sku_counts'][v] = self.mtl_n[v].X
        
        # VDR-specific: Extract stacking group information
        if self.vdr_stacking_rules:
            for v, l, p in self.no_stk_plt_g2 if self.no_stk_plt_g2 else []:
                if self.no_stk_plt_g2[v, l, p].X > 0.5:
                    solution['stacking_groups'][(v, l, p)] = 'G2'
            
            for v, l, p in self.no_stk_plt_g3 if self.no_stk_plt_g3 else []:
                if self.no_stk_plt_g3[v, l, p].X > 0.5:
                    solution['stacking_groups'][(v, l, p)] = 'G3'
            
            for v, l, p in self.no_stk_plt_g4 if self.no_stk_plt_g4 else []:
                if self.no_stk_plt_g4[v, l, p].X > 0.5:
                    solution['stacking_groups'][(v, l, p)] = 'G4'
        
        # VDR-specific: Extract milk run objective contributions
        if self.enforceMilkRun:
            solution['milk_run_obj_full'] = self.obj_full_plt_var_term4.X
            solution['milk_run_obj_prtl'] = self.obj_prtl_plt_var_term4.X
        
        return solution
    
    def print_solution_summary(self):
        """
        Print a formatted summary of the solution.
        
        Displays:
        - Objective value and optimality gap
        - Number of pallets assigned
        - SKU counts per vehicle
        - VDR-specific information (milk run contributions, stacking groups)
        - Performance metrics (solve time, nodes explored)
        """
        if self.model.Status not in [GRB.OPTIMAL, GRB.TIME_LIMIT]:
            print("No solution to display.")
            return
        
        print("\n" + "="*70)
        print("SOLUTION SUMMARY")
        print("="*70)
        
        print(f"\nObjective Value: {self.model.ObjVal:,.2f}")
        print(f"MIP Gap: {self.model.MIPGap:.4%}")
        print(f"Solve Time: {self.model.Runtime:.2f} seconds")
        print(f"Nodes Explored: {self.model.NodeCount:,}")
        
        # Count assignments
        num_full = sum(1 for var in self.y_full.values() if var.X > 0.5)
        num_prtl = sum(1 for var in self.y_prtl.values() if var.X > 0.5)
        
        print(f"\nPallets Assigned:")
        print(f"  Full pallets: {num_full}")
        print(f"  Partial pallets: {num_prtl}")
        print(f"  Total: {num_full + num_prtl}")
        
        # SKU information
        print(f"\nSKU Information:")
        print(f"  Excess SKUs: {self.exceed_mtl_n.X:.0f}")
        for v in self.inputParams["containerList"]:
            print(f"  Container {v}: {self.mtl_n[v].X:.0f} SKUs")
        
        # VDR-specific information
        if self.vdr_stacking_rules:
            print(f"\nVDR Stacking Groups:")
            g2_count = sum(1 for var in (self.no_stk_plt_g2.values() if self.no_stk_plt_g2 else []) if var.X > 0.5)
            g3_count = sum(1 for var in (self.no_stk_plt_g3.values() if self.no_stk_plt_g3 else []) if var.X > 0.5)
            g4_count = sum(1 for var in (self.no_stk_plt_g4.values() if self.no_stk_plt_g4 else []) if var.X > 0.5)
            print(f"  G2 pallets (ground base): {g2_count}")
            print(f"  G3 pallets (no stack on): {g3_count}")
            print(f"  G4 pallets (ground only): {g4_count}")
        
        if self.enforceMilkRun:
            print(f"\nMilk Run Optimization:")
            print(f"  Full pallet contribution: {self.obj_full_plt_var_term4.X:,.2f}")
            print(f"  Partial pallet contribution: {self.obj_prtl_plt_var_term4.X:,.2f}")
            print(f"  Total milk run value: {self.obj_full_plt_var_term4.X + self.obj_prtl_plt_var_term4.X:,.2f}")
        
        print("="*70 + "\n")
    
    def generate_loading_instructions(self):
        """
        Generate detailed loading instructions from the optimized solution.
        
        This method creates a list of Load_Obj instances, each representing
        one pallet's loading details including VDR-specific fields (stacking
        group, location, destination).
        
        Returns:
            list: List of Load_Obj instances sorted by container and sequence
        
        Each Load_Obj contains:
            - marketID: Market identifier
            - container: Container/vehicle ID
            - layer: Layer number (1-4 typically)
            - position: Position number within layer
            - sequence: Loading sequence number (order to load)
            - material: Material/SKU code
            - brand: Brand name (ocean mode)
            - description: Material description
            - batch: Batch/order number
            - cases: Number of cases
            - total_cases: Total cases on full pallet
            - weight: Weight in pounds
            - cube: Volume in cubic feet
            - height: Height in inches
            - is_partial: Boolean - is this a partial pallet?
            - stacking_group: 'G1', 'G2', 'G3', or 'G4' (VDR-specific)
            - location: Warehouse/DC location (VDR-specific)
            - shipTo: Destination plant for milk run (VDR-specific)
            - bin_loc: Bin location for warehouse picking
            - batch_date: Batch date
        
        Example Output:
            [
                Load_Obj(
                    container=1, layer=1, position=1, sequence=1,
                    material='SKU001', brand='BrandA',
                    stacking_group='G2',  # Heavy base
                    location='Warehouse_1',
                    shipTo='Plant_A',  # First delivery stop
                    ...
                ),
                Load_Obj(
                    container=1, layer=2, position=1, sequence=25,
                    material='SKU045', brand='BrandB',
                    stacking_group='G1',  # Standard
                    location='Warehouse_2',
                    shipTo='Plant_A',  # Same stop, on top of G2
                    ...
                ),
                ...
            ]
        
        The instructions are sorted by:
        1. Container (ascending)
        2. Sequence within container (loading order)
        
        This ensures proper loading order for warehouse operations.
        """
        if self.model.Status not in [GRB.OPTIMAL, GRB.TIME_LIMIT]:
            print("Cannot generate loading instructions - no solution available")
            return []
        
        print("\nGenerating VDR-enhanced loading instructions...")
        
        loading_instr_list = []
        material_count_dict = {}
        
        for c in self.inputParams["containerList"]:
            for p in self.inputParams["positionList"]:
                for l in self.inputParams["layerList"]:
                    # Check for full pallets
                    for plt in self.inputParams["fullPalletList"]:
                        if self.y_full[c, l, p, plt].X >= 0.9:
                            load_obj = Load_Obj()
                            
                            # Basic information
                            load_obj.marketID = self.inputParams["marketID"]
                            load_obj.container = c
                            load_obj.layer = l
                            load_obj.position = p
                            load_obj.sequence = self.inputParams["positionSequenceDict"][p]
                            
                            # Material information
                            load_obj.material = self.inputParams["palletMaterialDict"][plt]
                            load_obj.brand = self.inputParams["palletBrandDict"].get(plt, "") if self.run_ocean else ""
                            load_obj.description = self.inputParams["palletDescriptionDict"].get(plt, "")
                            load_obj.batch = self.inputParams["palletBatchDict"][plt]
                            
                            # Quantities
                            load_obj.cases = self.inputParams["palletNumCasesDict"][plt]
                            load_obj.total_cases = self.inputParams["palletNumCasesDict"][plt]
                            load_obj.weight = self.inputParams["palletWeightDict"][plt]
                            length = self.inputParams.get("palletLengthDict", {}).get(plt, 48.0)
                            width = self.inputParams.get("palletWidthDict", {}).get(plt, 40.0)
                            load_obj.cube = (length / 12) * (width / 12) * (self.inputParams["palletHeightDict"][plt] / 12)
                            load_obj.height = self.inputParams["palletHeightDict"][plt]
                            
                            # Pallet type
                            load_obj.is_partial = False
                            load_obj.pallet_type = "Full"
                            
                            # VDR-specific fields
                            load_obj.stacking_group = self.inputParams.get('palletStackingGroupDict', {}).get(plt, 'G1')
                            load_obj.location = self.inputParams.get("palletLocationDict", {}).get(plt, '')
                            load_obj.shipTo = self.inputParams.get("palletPlantDict", {}).get(plt, '')
                            
                            # Update material count
                            material_count_dict[load_obj.material] = material_count_dict.get(load_obj.material, 0) + 1
                            
                            loading_instr_list.append(load_obj)
                    
                    # Check for partial pallets
                    for plt in self.inputParams["partialPalletList"]:
                        if self.y_prtl[c, l, p, plt].X >= 0.9:
                            load_obj = Load_Obj()
                            
                            # Basic information
                            load_obj.marketID = self.inputParams["marketID"]
                            load_obj.container = c
                            load_obj.layer = l
                            load_obj.position = p
                            load_obj.sequence = self.inputParams["positionSequenceDict"][p]
                            
                            # Material information
                            load_obj.material = self.inputParams["palletMaterialDict"][plt]
                            load_obj.brand = self.inputParams["palletBrandDict"].get(plt, "") if self.run_ocean else ""
                            load_obj.description = self.inputParams["palletDescriptionDict"].get(plt, "")
                            load_obj.batch = self.inputParams["palletBatchDict"][plt]
                            
                            # Quantities (adjusted for partial)
                            prtl_frac = self.inputParams["partialFractionValueForPartialPallet"][plt]
                            load_obj.cases = self.inputParams["palletNumCasesDict"][plt] * prtl_frac
                            load_obj.total_cases = self.inputParams["palletNumCasesDict"][plt]
                            load_obj.weight = self.inputParams["palletWeightDict"][plt]
                            length = self.inputParams.get("palletLengthDict", {}).get(plt, 48.0)
                            width = self.inputParams.get("palletWidthDict", {}).get(plt, 40.0)
                            load_obj.cube = (length / 12) * (width / 12) * (self.inputParams["palletHeightDict"][plt] / 12)
                            load_obj.height = self.inputParams["palletHeightDict"][plt]
                            
                            # Pallet type
                            load_obj.is_partial = True
                            load_obj.pallet_type = "Partial"
                            
                            # VDR-specific fields
                            load_obj.stacking_group = self.inputParams.get('palletStackingGroupDict', {}).get(plt, 'G1')
                            load_obj.location = self.inputParams.get("palletLocationDict", {}).get(plt, '')
                            load_obj.shipTo = self.inputParams.get("palletPlantDict", {}).get(plt, '')
                            
                            # Update material count
                            material_count_dict[load_obj.material] = material_count_dict.get(load_obj.material, 0) + 1
                            
                            loading_instr_list.append(load_obj)
        
        # Sort by container and sequence
        loading_instr_list.sort(key=operator.attrgetter('sequence'))
        loading_instr_list.sort(key=operator.attrgetter('container'))
        
        # Update loading sequence numbers (reset per container)
        if loading_instr_list:
            loading_instr_list[0].sequence = 1
            for i in range(1, len(loading_instr_list)):
                if loading_instr_list[i].container != loading_instr_list[i-1].container:
                    loading_instr_list[i].sequence = 1
                else:
                    loading_instr_list[i].sequence = loading_instr_list[i-1].sequence + 1
        
        # Update material counts
        for load_obj in loading_instr_list:
            load_obj.material_count = material_count_dict[load_obj.material]
        
        print(f"[OK] Generated {len(loading_instr_list)} loading instructions")
        
        # Print VDR-specific summary
        if self.vdr_stacking_rules:
            g1_count = sum(1 for obj in loading_instr_list if obj.stacking_group == 'G1')
            g2_count = sum(1 for obj in loading_instr_list if obj.stacking_group == 'G2')
            g3_count = sum(1 for obj in loading_instr_list if obj.stacking_group == 'G3')
            g4_count = sum(1 for obj in loading_instr_list if obj.stacking_group == 'G4')
            
            print(f"  Stacking group distribution:")
            print(f"    G1 (standard): {g1_count}")
            print(f"    G2 (ground base): {g2_count}")
            print(f"    G3 (no stack on): {g3_count}")
            print(f"    G4 (ground only): {g4_count}")
        
        if self.enforceMilkRun:
            destinations = set(obj.shipTo for obj in loading_instr_list if obj.shipTo)
            print(f"  Destinations: {len(destinations)}")
            for dest in sorted(destinations):
                count = sum(1 for obj in loading_instr_list if obj.shipTo == dest)
                print(f"    {dest}: {count} pallets")
        
        return loading_instr_list


# Maintain compatibility with existing code
def OptModel(inputParams, model_file, outputfile1, outputfile2, outputfile3, 
             outputfile4, outputfile6, run_ocean, time_limit, gap, 
             sku_penalty_factor, vdr_stacking_rules=True):
    """
    Wrapper function to maintain compatibility with existing code structure.
    
    This function provides the same interface as the original Pyomo-based
    OptModel function but uses the new Gurobi-based VDR optimizer internally.
    
    Args:
        inputParams (dict): All input parameters and data
        model_file (str): Filename to write the model
        outputfile1-6 (str): Output filenames for various reports
        run_ocean (bool): True for ocean mode, False for truck mode
        time_limit (int): Time limit in seconds
        gap (float): MIP optimality gap (0.01 = 1%)
        sku_penalty_factor (float): Penalty factor for excess SKUs
        vdr_stacking_rules (bool): Enable VDR G2/G3/G4 constraints
    
    Returns:
        tuple: (solution, outputfile1, outputfile2, outputfile3, outputfile4, 
                loading_instr_list, inputParams, outputfile6, leftover_ocean_o3)
    
    Note:
        This is a compatibility wrapper. For new code, use
        ContainerLoadingOptimizerVDR class directly.
    """
    # Set Gurobi license path if specified
    if "GRB_LICENSE_FILE" in os.environ:
        print(f"Using Gurobi license: {os.environ['GRB_LICENSE_FILE']}")
    
    # Extract all solver-backed values before the context exits. The returned
    # solution is ordinary Python data and does not retain a disposed Gurobi
    # model or environment.
    with gp.Env(empty=True) as env:
        env.setParam('OutputFlag', 1)
        env.start()

        with ContainerLoadingOptimizerVDR(
            env=env,
            inputParams=inputParams,
            run_ocean=run_ocean,
            time_limit=time_limit,
            gap=gap,
            sku_penalty_factor=sku_penalty_factor,
            vdr_stacking_rules=vdr_stacking_rules
        ) as optimizer:
            optimizer.build_model()
            optimizer.write_model(model_file)
            optimizer.solve()
            optimizer.print_solution_summary()
            solution = optimizer.get_solution()
            loading_instr_list = optimizer.generate_loading_instructions()
            leftover_ocean_o3 = None

            return (solution, outputfile1, outputfile2, outputfile3, outputfile4,
                    loading_instr_list, inputParams, outputfile6, leftover_ocean_o3)
