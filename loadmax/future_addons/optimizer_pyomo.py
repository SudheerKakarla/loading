import pandas as pd
import pyomo.environ as pe
from pyomo.core import TransformationFactory
from pyomo.gdp import *
from prettytable import PrettyTable
from UtilityClass import Load_Obj
import operator

# ################################ Turn ON/OFF specific constraints #############################
# enforceCrushingConstraints = True
# enforceWeightBalancing = False
# enforceMaxSKUConstraint = False
# enforcePriorityPalletConstraints = True
#
# # usually for ocean
# enforceMixedCargoConstraints = False # need not be considered since optimization is run for each market, brand group separately
#
# # usually for trucks
# enforceAxleWeightConstraints = False#True
# enforceCompactPacking = False#True
# enforceSKUCrushingLimit = False
# enforceStackFactorConstraint = False
# enforceFootPrintAreaConstraint = False #inputParams['enforce_fp_tolerance']
# ################################################################################################


def SetGurobiOptions(opt):
    opt.options['TimeLimit'] = 60
    opt.options['MIPFocus'] = 1
    # opt.options['ImproveStartTime'] = computationTime / 6
    opt.options['Presolve'] = 2
    print("Gurobi optimizer settings:", opt.options)


def OptModel(inputParams, model_file, outputfile1, outputfile2, outputfile3, outputfile4, outputfile6, run_ocean, time_limit, sp):
    print("#################### Optimization model #######################")

    ########################  turn on/off constraints  ########################
    if run_ocean: # OCEAN
        print("Enforcing constraints for ocean....")
        enforceCrushingConstraints = True
        enforceWeightBalancing = False
        enforceMaxSKUConstraint = False # keep it off
        enforcePriorityPalletConstraints = True

        # usually for ocean
        enforceMixedCargoConstraints = False  # need not be considered since optimization is run for each market, brand group separately

        # usually for trucks
        enforceAxleWeightConstraints = False  # True
        enforceCompactPacking = False  # True
        enforceSKUCrushingLimit = False
        enforceStackFactorConstraint = False
        enforceFootPrintAreaConstraint = False  # inputParams['enforce_fp_tolerance']

    else: # TRUCK
        print("Enforcing constraints for truck....")
        enforceCrushingConstraints = True
        enforceWeightBalancing = True
        enforceMaxSKUConstraint = False # keep it off
        enforcePriorityPalletConstraints = True

        # usually for ocean
        enforceMixedCargoConstraints = False

        # usually for trucks
        enforceCompactPacking = False#True
        yes_list = ["Yes", "yes", "YES"]
        if inputParams['enforce_compact_packing'] in yes_list:
            enforceCompactPacking = True#True

        enforceAxleWeightConstraints = True

        enforceSKUCrushingLimit = False # get latest data file version
        enforceStackFactorConstraint = False#True
        enforceFootPrintAreaConstraint = False#True  # inputParams['enforce_fp_tolerance']

    ######################## data inputs ###########################
    print("Generating inputs for optimization model...")

    # # set input parameter values
    # SetInputParameterVals(inputParams)

    # lists
    veh_id = inputParams["containerList"]
    # eff_veh_id = inputParams["effectiveContainerList"]
    eff_veh_id = inputParams["containerList"]
    lyr_id = inputParams["layerList"]
    pos_id = inputParams["positionList"]
    agg_full_plt_id = inputParams["fullPalletList"]
    agg_prtl_plt_id = inputParams["partialPalletList"]
    # agg_prior_plt_id = inputParams["priorityPalletList"]
    plt_mtl_dict = inputParams["palletMaterialDict"]
    plt_brand_dict = inputParams["palletBrandDict"]
    cntr_brand_dict = inputParams["containerBrandDict"]

    front_id = inputParams["frontPositionList"]
    rear_id = inputParams["rearPositionList"]
    row_1_id = inputParams["leftPositionList"]
    row_2_id = inputParams["rightPositionList"]
    door_posn = inputParams["door_posn"]

    mkt_mtl_id = inputParams["materialList"]
    section_id = inputParams["sectionList"]
    full_plt_n = inputParams["fullPalletDemandByMaterialBatch"]
    prtl_plt_n = inputParams["partialPalletDemandByMaterialBatch"]
    bal_coef = inputParams["positionGroupBalanceCoefficientDict"]
    prtl_frac = inputParams["partialFractionValueForPartialPallet"]
    ht = inputParams["palletHeightDict"]
    wt = inputParams["palletWeightDict"]
    full_plt_area = inputParams["fullPalletFootPrintArea"]
    prtl_plt_area = inputParams["partialPalletFootPrintArea"]
    stk_factor = inputParams["stackFactorPallet"]
    # allow_cntr_mtl = inputParams["allowContainerMaterial"]
    crush_lmt = inputParams["crushingLimitByMaterial"]
    max_prior_full = inputParams['max_prior_full']
    max_prior_prtl = inputParams['max_prior_prtl']
    brand_grp = inputParams['brand_group']
    cntr_priority = inputParams['container_priority']
    plt_priorityVal_NAC = inputParams["palletPriorityValue_numAvailableContainers"]
    plt_priority_NAC = inputParams["palletPriority_numAvailableContainers"]

    # single values
    first_posn = inputParams['first_plt_n']  # first pallet position in a layer
    max_plt_n = inputParams['max_plt_n']  # max number of pallets per layer
    max_lyr_n = inputParams['max_lyr_n']  # max number of layers per container
    max_weight = inputParams['max_wt']
    max_front_wt = inputParams['max_front_wt']
    max_rear_wt = inputParams['max_rear_wt']
    prior_incentive = inputParams['priorityIncentive']
    sku_penalty = inputParams['sku_penalty']
    #sku_penalty_factor = inputParams['sku_penalty_factor']
    sku_penalty_factor = sp
    # contr_ht= inputParams['containerHeight']
    # contr_door_ht = inputParams['containerHeightAtDoor']
    fb_bal_tol = inputParams['front_to_back_tolerance']
    side_bal_tol = inputParams['side_to_side_tolerance']
    fp_area_tol = inputParams['fp_area_tolerance']
    unique_sku_n = inputParams['unique_sku_n']  # total unique number of SKUs in all containers combined
    max_sku_contr = inputParams['max_sku_n']  # total unique number of SKUs in all containers combined
    max_veh_ht = inputParams['containerHeight']
    max_veh_door_ht = inputParams['containerHeightAtDoor']
    load_allow_ht = inputParams['load_allow_ht']

    highPrioritySKUList = inputParams['skuPriorityIncentive']

    ########################## BUILDING MODEL ##################################

    ######################## create new model ########################
    model = pe.ConcreteModel()

    ########################  variables ###########################
    print("Creating variables for optimization model...")

    # whether to assign full pallet stk to (v, l, p)
    model.y_full = pe.Var(veh_id, lyr_id, pos_id, agg_full_plt_id, initialize=0, within=pe.Binary)

    # whether to assign partial pallet stk to (v, l, p)
    model.y_prtl = pe.Var(veh_id, lyr_id, pos_id, agg_prtl_plt_id, initialize=0, within=pe.Binary)

    # number of priority pallets assigned for given material and batch
    model.y_prior_full = pe.Var(agg_full_plt_id, initialize=0, within=pe.NonNegativeReals)
    model.y_prior_prtl = pe.Var(agg_prtl_plt_id, initialize=0, within=pe.NonNegativeReals)

    # assignment of full pallet at (v, l, p)
    model.full_plt = pe.Var(veh_id, lyr_id, pos_id, within=pe.Binary)

    # assignment of partial pallet at (v, l, p)
    model.prtl_plt = pe.Var(veh_id, lyr_id, pos_id, within=pe.Binary)

    # weight at (v, l, p)
    model.wt = pe.Var(veh_id, lyr_id, pos_id, within=pe.NonNegativeReals)

    # stack height at (v, p)
    model.stk_ht = pe.Var(veh_id, pos_id, within=pe.NonNegativeReals)

    # total layer plt number at (v, l)
    model.lyr_plt_n = pe.Var(veh_id, lyr_id,bounds=(0, inputParams['max_plt_n']),within=pe.NonNegativeIntegers)
    # CHANGE - app.mkt_limit_df.loc[mkt, 'max_wt']

    # amount of pallets with SKU mtl in vehicle v
    model.mtl_plt = pe.Var(veh_id, mkt_mtl_id,within=pe.NonNegativeIntegers)

    # if SKU mtl is in vehicle v
    model.y_mtl_plt = pe.Var(veh_id, mkt_mtl_id, within=pe.Binary)

    # SKU number in vehicle v
    model.mtl_n = pe.Var(veh_id, within=pe.NonNegativeIntegers)

    # exceeded SKU number
    model.exceed_mtl_n = pe.Var(within=pe.NonNegativeIntegers)

    # CHANGE - area footprint of pallet at (v,l,p)
    model.fp_area = pe.Var(veh_id, lyr_id, pos_id, within=pe.NonNegativeIntegers)

    # CHANGE - weight of pallets in container section (c,g)
    model.sectn_wt = pe.Var(veh_id, section_id, within=pe.NonNegativeIntegers)

    # CHANGE - is stacking allowed at a given position, layer, vehicle
    model.no_stk_plt = pe.Var(veh_id, lyr_id, pos_id, within=pe.Binary)

    # SKU crushing limit
    model.sku_crush_wt_lmt = pe.Var(veh_id, pos_id, within=pe.NonNegativeIntegers)
    ######################### constraints #########################

    print("Generating constraints for optimization model...")

    # total vehicle weight limit
    def con_total_wt(m, v):
        return sum(m.wt[v, l, p]
                   for l in lyr_id
                   for p in pos_id) <= inputParams['max_wt']
    model.con_total_wt = pe.Constraint(eff_veh_id, rule=con_total_wt)
    # CHANGE - app.mkt_limit_df.loc[mkt, 'max_wt']


    # every pallet plt in agg_full_plt_id has been assigned exactly
    # full_plt_n[plt] times in total
    def con_full_plt_asgn(m, plt):
        print("Full pallet demand for ",plt, " is ",  full_plt_n[plt])
        # if run_ocean:
        #     return sum(m.y_full[v, l, p, plt]
        #               for v in veh_id
        #               for l in lyr_id
        #               for p in pos_id) <= full_plt_n[plt]
        # else:
        #     return sum(m.y_full[v, l, p, plt]
        #               for v in veh_id
        #               for l in lyr_id
        #               for p in pos_id) == full_plt_n[plt]
        return sum(m.y_full[v, l, p, plt]
                      for v in veh_id
                      for l in lyr_id
                      for p in pos_id) <= full_plt_n[plt]
    model.con_full_plt_asgn = pe.Constraint(agg_full_plt_id, rule=con_full_plt_asgn)

    # every pallet plt in agg_prtl_plt_id has been assigned exactly
    # prtl_plt_n[plt] times in total
    def con_prtl_plt_asgn(m, plt):
        print("Partial pallet demand for ",plt, " is ",  prtl_plt_n[plt])
        # if run_ocean:
        #     return sum(m.y_prtl[v, l, p, plt]
        #               for v in veh_id
        #               for l in lyr_id
        #               for p in pos_id) <= prtl_plt_n[plt]
        # else:
        #     return sum(m.y_prtl[v, l, p, plt]
        #               for v in veh_id
        #               for l in lyr_id
        #               for p in pos_id) == prtl_plt_n[plt]
        return sum(m.y_prtl[v, l, p, plt]
                      for v in veh_id
                      for l in lyr_id
                      for p in pos_id) <= prtl_plt_n[plt]
    model.con_prtl_plt_asgn = pe.Constraint(agg_prtl_plt_id, rule=con_prtl_plt_asgn)

    # if a full pallet is assigned to (v, l, p)
    def con_full_plt(m, v, l, p):
        return m.full_plt[v, l, p] == sum(m.y_full[v, l, p, plt] for plt in agg_full_plt_id)
    model.con_full_plt = pe.Constraint(veh_id, lyr_id, pos_id,rule=con_full_plt)

    # if a partial pallet is assigned to (v, l, p)
    def con_prtl_plt(m, v, l, p):
        return m.prtl_plt[v, l, p] == sum(m.y_prtl[v, l, p, plt] for plt in agg_prtl_plt_id)
    model.con_prtl_plt = pe.Constraint(veh_id, lyr_id, pos_id,rule=con_prtl_plt)

    # each spot can be assigned at most one pallet
    def con_spot_asgn(m, v, l, p):
        return sum(m.y_full[v, l, p, plt] for plt in agg_full_plt_id) + \
               sum(m.y_prtl[v, l, p, plt] for plt in agg_prtl_plt_id) <= 1
    model.con_spot_asgn = pe.Constraint(veh_id, lyr_id, pos_id,rule=con_spot_asgn)

    # no partial pallet on partial pallet
    def con_no_prtl_on_prtl(m, v, l, p):
        # skip pallets on the second layer
        if lyr_id.index(l) >= len(lyr_id)-1:
            return pe.Constraint.Skip
        return m.prtl_plt[v, l, p] + m.prtl_plt[v, l + 1, p] <= 1
    model.con_no_prtl_on_prtl = pe.Constraint(eff_veh_id, lyr_id, pos_id, rule=con_no_prtl_on_prtl)

    # no full pallet on partial pallet
    def con_no_full_on_prtl(m, v, l, p):
        # skip pallets on the second layer
        if lyr_id.index(l) >= len(lyr_id)-1:
            return pe.Constraint.Skip
        return m.prtl_plt[v, l, p] + m.full_plt[v, l + 1, p] <= 1
    model.con_no_full_on_prtl = pe.Constraint(eff_veh_id, lyr_id, pos_id, rule=con_no_full_on_prtl)

    # calculate stack height
    def con_stk_ht(m, v, p):
        return m.stk_ht[v, p] == \
               sum(ht[plt] * m.y_full[v, l, p, plt] for l in lyr_id
                   for plt in agg_full_plt_id) + \
               sum(ht[plt] * m.y_prtl[v, l, p, plt] for l in lyr_id
                   for plt in agg_prtl_plt_id)
    model.con_stk_ht = pe.Constraint(eff_veh_id, pos_id, rule=con_stk_ht)

    # calculate pallet weight at each spot (v, l, p)
    def con_wt(m, v, l, p):
        return m.wt[v, l, p] == \
               sum(wt[plt] * m.y_full[v, l, p, plt] for plt in agg_full_plt_id) + \
               sum(wt[plt] * m.y_prtl[v, l, p, plt] for plt in agg_prtl_plt_id)
    model.con_wt = pe.Constraint(eff_veh_id, lyr_id, pos_id, rule=con_wt)

    if enforceCrushingConstraints:

        # crushing constraints - combined
        def con_crush_comb(m, v, l, p):
            return m.wt[v, l, p] >= sum(m.wt[v, s, p] for s in lyr_id if s > l)
        model.con_crush_comb = pe.Constraint(eff_veh_id, lyr_id, pos_id, rule=con_crush_comb)

        # # crushing weight: top one should be lighter than the bottom one
        # def con_top_lighter(m, v, l, p):
        #     # skip layer pair (0, 1)
        #     if lyr_id.index(l) == 0:
        #         return pe.Constraint.Skip
        #     return m.wt[v, l, p] <= m.wt[v, l - 1, p]
        # model.con_top_lighter = pe.Constraint(eff_veh_id, lyr_id, pos_id, rule=con_top_lighter)
        #
        # # crushing weight: top two layers in total should be lighter than the bottom one
        # def con_top_total_lighter(m, v, p):
        #     return m.wt[v, 1, p] >= m.wt[v, 2, p] + m.wt[v, 3, p]
        #     # return m.wt[v, 1, p] >= sum(m.wt[v, l, p]  for l in lyr_id and l > 1)
        # model.con_top_total_lighter = pe.Constraint(eff_veh_id, pos_id, rule=con_top_total_lighter)

    if enforceWeightBalancing:

        # end-to-end balance: the adjusted weight of pallets in the front
        # should be no larger/smaller than the ones in the rear by app.fb_bal_tol
        if fb_bal_tol > 0:
            def con_bal_ete_1(m, v):
                # return sum((bal_coef[(p, "A")] + bal_coef[(p, "C")])* m.wt[v, l, p] for l in lyr_id for p in front_id) <= \
                #       (1 + fb_bal_tol) * sum((bal_coef[(p, "B")] + bal_coef[(p, "D")]) * m.wt[v, l, p] for l in lyr_id for p in rear_id)
                return sum(m.wt[v, l, p] for l in lyr_id for p in front_id) <= \
                        (1 + fb_bal_tol) * sum(m.wt[v, l, p] for l in lyr_id for p in rear_id)
            model.con_bal_ete_1 = pe.Constraint(eff_veh_id, rule=con_bal_ete_1)

            def con_bal_ete_2(m, v):
                # return sum((bal_coef[(p, "A")] + bal_coef[(p, "C")]) * m.wt[v, l, p] for l in lyr_id for p in front_id) >= \
                #       (1 - fb_bal_tol) *  sum((bal_coef[(p, "B")] + bal_coef[(p, "D")]) * m.wt[v, l, p] for l in lyr_id for p in rear_id)
                return sum(m.wt[v, l, p] for l in lyr_id for p in front_id) >= \
                        (1 - fb_bal_tol) * sum( m.wt[v, l, p] for l in lyr_id for p in rear_id)
            model.con_bal_ete_2 = pe.Constraint(eff_veh_id, rule=con_bal_ete_2)

        # side-to-side balance: the adjusted weight of pallets in one side should be no larger/smaller
        # than the ones in the other side by app.side_bal_tol
        if side_bal_tol > 0:
            row_1_id = list(set(row_1_id))
            row_2_id = list(set(row_2_id))
            print("Side to side tolerance enforced:", side_bal_tol)
            def con_bal_sts_1(m, v):
                # return sum(m.wt[v, l, p] * bal_coef[p] for l in lyr_id for p in row_1_id) <= \
                #        (1 + side_bal_tol) * sum(m.wt[v, l, p] * bal_coef[p] for l in lyr_id for p in row_2_id)
                return sum(m.wt[v, l, p] for l in lyr_id for p in row_1_id) <= \
                       (1 + side_bal_tol) * sum(m.wt[v, l, p] for l in lyr_id for p in row_2_id)
            model.con_bal_sts_1 = pe.Constraint(eff_veh_id, rule=con_bal_sts_1)

            def con_bal_sts_2(m, v):
                # return sum(m.wt[v, l, p] * bal_coef[p] for l in lyr_id for p in row_1_id) >= \
                #        (1 - side_bal_tol) * sum(m.wt[v, l, p] * bal_coef[p] for l in lyr_id for p in row_2_id)
                return sum(m.wt[v, l, p] for l in lyr_id for p in row_1_id) >= \
                       (1 - side_bal_tol) * sum(m.wt[v, l, p] for l in lyr_id for p in row_2_id)
            model.con_bal_sts_2 = pe.Constraint(eff_veh_id, rule=con_bal_sts_2)

    # calculate pallet number each layer
    def con_lyr_plt_n(m, v, l):
        return sum(m.y_prtl[v, l, p, plt] for p in pos_id for plt in agg_prtl_plt_id) + \
               sum(m.y_full[v, l, p, plt] for p in pos_id for plt in agg_full_plt_id) \
               == m.lyr_plt_n[v, l]
    model.con_lyr_plt_n = pe.Constraint(eff_veh_id, lyr_id, rule=con_lyr_plt_n)

    if enforceAxleWeightConstraints:
        print("Axle weight constraints enforced...")
        # front axle weight limit
        def con_front_wt(m, v):
            return sum((bal_coef[(p, "A")] + bal_coef[(p, "C")]) * m.wt[v, l, p] for l in lyr_id for p in front_id) <= max_front_wt
            # return sum(m.wt[v, l, p] for l in lyr_id for p in front_id) <= max_front_wt
        model.con_front_wt = pe.Constraint(eff_veh_id, rule=con_front_wt)
        # CHANGE - app.mkt_limit_df.loc[mkt, 'max_front_wt']

        # rear axle weight limit
        def con_rear_wt(m, v):
            return sum((bal_coef[(p, "B")] + bal_coef[(p, "D")]) * m.wt[v, l, p] for l in lyr_id for p in rear_id) <= max_rear_wt
            # return sum(m.wt[v, l, p] for l in lyr_id for p in rear_id) <= max_rear_wt
        model.con_rear_wt = pe.Constraint(eff_veh_id, rule=con_rear_wt)
        # CHANGE - app.mkt_limit_df.loc[mkt, 'max_front_wt']


    # relationship between mtl_plt and y_full and y_prtl
    def plt_mtl_indicator(plt, mtl):
        if plt_mtl_dict[plt] == mtl:
            return 1
        else:
            return 0
    def con_mtl_plt(m, v, mtl):
        return m.mtl_plt[v, mtl] == \
               sum(m.y_full[v, l, p, plt] * plt_mtl_indicator(plt, mtl) for plt in agg_full_plt_id for l in lyr_id for p in pos_id) + \
               sum(m.y_prtl[v, l, p, plt] * plt_mtl_indicator(plt, mtl) for plt in agg_prtl_plt_id for l in lyr_id for p in pos_id)
    model.con_mtl_plt = pe.Constraint(veh_id, mkt_mtl_id, rule=con_mtl_plt)
    # CHANGE - full_mtl_plt[mtl, plt] instead of plt_mtl_indicator
    # CHANGE - prtl_mtl_plt[mtl, plt] instead of plt_mtl_indicator

    ########### relationship between mtl_plt and y_mtl_plt MODELED AS LINEAR AND LOGICAL CONSTRAINTS
    def con_y_mtl_plt_1(m, v, mtl):
        # return m.mtl_plt[v, mtl] <= 20 * m.y_mtl_plt[v, mtl]
        # return m.mtl_plt[v, mtl] <= max_lyr_n * max_plt_n *  m.y_mtl_plt[v, mtl]
        return (1/(max_lyr_n * max_plt_n))*m.mtl_plt[v, mtl] <= m.y_mtl_plt[v, mtl]
    model.con_y_mtl_plt_1 = pe.Constraint(veh_id, mkt_mtl_id, rule=con_y_mtl_plt_1)
    # def con_y_mtl_plt_2(m, v, mtl):
    #     return m.mtl_plt[v, mtl] >= m.y_mtl_plt[v, mtl]
    # model.con_y_mtl_plt_2 = pe.Constraint(veh_id, mkt_mtl_id, rule=con_y_mtl_plt_2)

    # def _NoCollision(disjunct, m, v, mtl):
    #     model = disjunct.model()
    #     lhs = model.t[i] + sum(model.tau[i, m] for m in model.STAGES if m < j)
    #     rhs = model.t[k] + sum(model.tau[k, m] for m in model.STAGES if m < j)
    #     if m.mtl_plt[v, mtl] :
    #         disjunct.c = pe.Constraint(expr=lhs + model.tau[i, j] <= rhs)
    #     else:
    #         disjunct.c = pe.Constraint(expr=rhs + model.tau[k, j] <= lhs)
    #
    # model.NoCollision = Disjunct(veh_id, mkt_mtl_id, rule=_NoCollision)

    # material types in truck v
    def con_mtl_n(m, v):
        return m.mtl_n[v] == sum(m.y_mtl_plt[v, mtl] for mtl in mkt_mtl_id)
    model.con_mtl_n = pe.Constraint(veh_id, expr=con_mtl_n)

    # exceeded material type
    def con_exceed_mtl_n(m):
        return m.exceed_mtl_n >= sum(m.mtl_n[v] for v in eff_veh_id) - unique_sku_n
    model.con_exceed_mtl_n = pe.Constraint(expr=con_exceed_mtl_n)
    # CHANGE - app.unique_sku_n.loc[(mkt, bg), 'n']

    ################ new constraints - Nitish #################
    if enforceMaxSKUConstraint:
        # # bound on max SKUs per container
        # def con_max_mtl_n(m, v):
        #     return m.mtl_n[v] <= max_sku_contr
        # model.con_max_mtl_n = pe.Constraint(veh_id, expr=con_max_mtl_n)
        # # CHANGE - app.unique_sku_n.loc[(mkt, bg), 'n']
        for v in veh_id:
            model.mtl_n[v].setub(max_sku_contr)

    # bound on stack height at door and non-door positions
    for v in veh_id:
        for p in pos_id:
            if p in door_posn:
                model.stk_ht[v,p].setub(max_veh_door_ht - load_allow_ht)
            else:
                model.stk_ht[v,p].setub(max_veh_ht - load_allow_ht)
    # # non-door position
    # def con_max_stk_ht(m, v, p):
    #     if p in door_posn:
    #         return pe.Constraint.Skip
    #     return m.stk_ht[v, p] <= max_veh_ht - load_allow_ht
    # model.con_max_stk_ht = pe.Constraint(veh_id, pos_id, expr=con_max_stk_ht)
    # # door position
    # def con_max_stk_door_ht(m, v, p):
    #     if p not in door_posn:
    #         return pe.Constraint.Skip
    #     return m.stk_ht[v, p] <= max_veh_door_ht - load_allow_ht
    # model.con_max_stk_door_ht = pe.Constraint(veh_id, pos_id, expr=con_max_stk_door_ht)
    # # CHANGE - app.mkt_limit_df.loc[mkt, 'veh_ht']

    # bound on the number of pallets in a given layer
    for v in veh_id:
        for l in lyr_id:
            model.lyr_plt_n[v, l].setub(max_plt_n)
    # def con_max_plt_lyr_n(m, v, l):
    #     return m.lyr_plt_n[v, l] <= max_plt_n
    # model.con_max_plt_lyr_n = pe.Constraint(veh_id, lyr_id, expr=con_max_plt_lyr_n)

    # enforce the pallet footprint area in each position in the container
    if enforceFootPrintAreaConstraint:
        if fp_area_tol > 0:
            def con_fp_area(m, v, l, p):
                return m.fp_area[v, l, p] == sum(m.y_full[v, l, p, plt] * full_plt_area[plt] for plt in agg_full_plt_id) + \
                                            sum(m.y_prtl[v, l, p, plt] * prtl_plt_area[plt] for plt in agg_prtl_plt_id)
            model.con_fp_area = pe.Constraint(veh_id, lyr_id, pos_id, expr=con_fp_area)

            def con_fp_area_tol(m, v, l, p):
                if l == 1:
                    return pe.Constraint.Skip
                return m.fp_area[v, l, p] <= m.fp_area[v, l-1, p] * (1 + fp_area_tol)
            model.con_fp_area_tol = pe.Constraint(veh_id, lyr_id, pos_id, expr=con_fp_area_tol)



    if enforceStackFactorConstraint:
        # prohibit stacking on top of pallets with stacking factor = 1
        def con_stk_fctr_plt(m, v, l, p):
            # TODO - fix the stack factor value for correct results
            # if stk_factor[plt] != 1:
            #     return pe.Constraint.Skip
            return m.no_stk_plt[v,l,p] == sum(m.y_full[v, l, p, plt] * stk_factor[plt] for plt in agg_full_plt_id if stk_factor[plt] == 1)
        model.con_stk_fctr_plt = pe.Constraint(veh_id, lyr_id, pos_id, expr=con_stk_fctr_plt)

        def con_stk_fctr_plt_II(m, v, l, p):
            if lyr_id.index(l) >= len(lyr_id)-1:
                return pe.Constraint.Skip
            return m.no_stk_plt[v, l, p] + m.full_plt[v, l+1, p] + m.prtl_plt[v, l+1, p] <= 1
        model.con_stk_fctr_plt_II = pe.Constraint(veh_id, lyr_id, pos_id, expr=con_stk_fctr_plt_II)

    ######## constraints for mixed cargo
    # if enforceMixedCargoConstraints:
    #     # full
    #     def con_mix_cargo_full_plt(m, v, plt):
    #         bin_param_ind = 0
    #         if plt_brand_dict[plt] in cntr_brand_dict[v]:
    #             bin_param_ind = 1
    #         return sum(m.y_full[v, l, p, plt] for l in lyr_id for p in pos_id) <= max_lyr_n * max_plt_n * bin_param_ind
    #     model.con_mix_cargo_full_plt = pe.Constraint(veh_id, agg_full_plt_id, expr=con_mix_cargo_full_plt)
    #     # partial
    #     def con_mix_cargo_prtl_plt(m, v, plt):
    #         bin_param_ind = 0
    #         if plt_brand_dict[plt] in cntr_brand_dict[v]:
    #             bin_param_ind = 1
    #         return sum(m.y_prtl[v, l, p, plt] for l in lyr_id for p in pos_id) <= max_lyr_n * max_plt_n * bin_param_ind
    #     model.con_mix_cargo_prtl_plt = pe.Constraint(veh_id, agg_prtl_plt_id, expr=con_mix_cargo_prtl_plt)

    ######## constraints for number of balanced pallets ########
    if enforcePriorityPalletConstraints:
        # # full
        # def con_bal_plt_full(m, plt):
        #     return m.y_prior_full[plt] <= sum(m.y_full[v, l, p, plt] for v in veh_id for l in lyr_id for p in pos_id)
        # model.con_bal_plt_full = pe.Constraint(agg_full_plt_id, expr=con_bal_plt_full)
        # # partial
        # def con_bal_plt_prtl(m, plt):
        #     return m.y_prior_prtl[plt] <= sum(m.y_prtl[v, l, p, plt] for v in veh_id for l in lyr_id for p in pos_id)
        # model.con_bal_plt_prtl = pe.Constraint(agg_prtl_plt_id, expr=con_bal_plt_prtl)
        # bound for full
        for plt in agg_full_plt_id:
            model.y_prior_full[plt].setub(max_prior_full[plt])
        # bound for partial
        for plt in agg_prtl_plt_id:
            model.y_prior_prtl[plt].setub(max_prior_prtl[plt])


    # # constraints for compact packing
    if enforceCompactPacking:
        print("Enforcing compact packing constraints...")
        def con_comp_pack_I(m, v, l, p):
            if p >= first_posn:
                return pe.Constraint.Skip
            return m.full_plt[v, l, p] + m.prtl_plt[v, l, p] <= m.full_plt[v, l, p+1] + m.prtl_plt[v, l, p+1]

        def con_comp_pack_II(m, v, l, p):
            if p <= first_posn:
                return pe.Constraint.Skip
            return m.full_plt[v, l, p] + m.prtl_plt[v, l, p] <= m.full_plt[v, l, p-1] + m.prtl_plt[v, l, p-1]

        def con_comp_pack_III(m, v, l, p):
            if p > first_posn or p % 2 > 0:
                return pe.Constraint.Skip
            return m.full_plt[v, l, p] + m.prtl_plt[v, l, p] == m.full_plt[v, l, p-1] + m.prtl_plt[v, l, p-1]

        model.con_comp_pack_I = pe.Constraint(veh_id, lyr_id, pos_id, expr=con_comp_pack_I)
        model.con_comp_pack_II = pe.Constraint(veh_id, lyr_id, pos_id, expr=con_comp_pack_II)
        model.con_comp_pack_III = pe.Constraint(veh_id, lyr_id, pos_id, expr=con_comp_pack_III)

        def con_lyr_seq_I(m, v, l):
            if l == 1:
                return pe.Constraint.Skip
            return m.full_plt[v, l, first_posn] + m.prtl_plt[v, l, first_posn] <= m.full_plt[v, l-1, 1] + m.prtl_plt[v, l-1, 1]
        def con_lyr_seq_II(m, v, l):
            if l == 1:
                return pe.Constraint.Skip
            return m.full_plt[v, l, first_posn] + m.prtl_plt[v, l, first_posn] <= m.full_plt[v, l-1, max_plt_n] + m.prtl_plt[v, l-1, max_plt_n]

        model.con_lyr_seq_I = pe.Constraint(veh_id, lyr_id, expr=con_lyr_seq_I)
        model.con_lyr_seq_II = pe.Constraint(veh_id, lyr_id, expr=con_lyr_seq_II)

    ######## SKU Crushing Limit
    if enforceSKUCrushingLimit:
        def con_calc_sku_crushLimit(m, v, p):
            return m.sku_crush_wt_lmt[v,p] == sum(m.y_full[v, 1, p, plt] * crush_lmt[plt] for plt in agg_full_plt_id)
        model.con_calc_sku_crushLimit = pe.Constraint(veh_id, pos_id, expr=con_calc_sku_crushLimit)
        def con_enf_crushLimit(m, v, p):
            return sum(m.wt[v,l,p] for l in lyr_id if l >=2) <=  m.sku_crush_wt_lmt[v,p]
        model.con_enf_crushLimit = pe.Constraint(veh_id, pos_id, expr=con_enf_crushLimit)

    ######## Truck - Priority Incentive for given SKU_batch
    model.obj_full_plt_var_term1 = pe.Var(within=pe.NonNegativeReals)
    model.obj_full_plt_var_term2 = pe.Var(within=pe.NonNegativeReals)
    model.obj_prtl_plt_var_term1 = pe.Var(within=pe.NonNegativeReals)
    model.obj_prtl_plt_var_term2 = pe.Var(within=pe.NonNegativeReals)


    def calc_full_plt_term1(m):

        return m.obj_full_plt_var_term1 ==  sum(m.y_full[c, l, p, plt] * cntr_priority[c] * inputParams['priorityIncentive']
        for c in eff_veh_id
        for l in lyr_id
        for p in pos_id
        for plt in agg_full_plt_id if eff_veh_id.index(c) <= len(eff_veh_id)-1 and plt_mtl_dict[plt] in highPrioritySKUList)

    model.calc_full_plt_term1 = pe.Constraint(expr=calc_full_plt_term1)

    def calc_full_plt_term2(m):

        return m.obj_full_plt_var_term2 ==  sum(m.y_full[c, l, p, plt] * cntr_priority[c]
        for c in eff_veh_id
        for l in lyr_id
        for p in pos_id
        for plt in agg_full_plt_id if plt_mtl_dict[plt] not in highPrioritySKUList)


    model.calc_full_plt_term2 = pe.Constraint(expr=calc_full_plt_term2)

    def calc_prtl_plt_term1(m):

        return m.obj_prtl_plt_var_term1 ==  sum(m.y_prtl[c, l, p, plt]* prtl_frac[plt] * cntr_priority[c] * inputParams['priorityIncentive']
        for c in eff_veh_id
        for l in lyr_id
        for p in pos_id
        for plt in agg_prtl_plt_id if eff_veh_id.index(c) <= len(eff_veh_id)-1 and plt_mtl_dict[plt] in highPrioritySKUList)

    model.calc_prtl_plt_term1 = pe.Constraint(expr=calc_prtl_plt_term1)

    def calc_prtl_plt_term2(m):

        return m.obj_prtl_plt_var_term2 ==  sum(m.y_prtl[c, l, p, plt]* prtl_frac[plt] * cntr_priority[c]
        for c in eff_veh_id
        for l in lyr_id
        for p in pos_id
        for plt in agg_prtl_plt_id if plt_mtl_dict[plt] not in highPrioritySKUList)


    model.calc_prtl_plt_term2 = pe.Constraint(expr=calc_prtl_plt_term2)

    # model.left_occ_weight =  pe.Var(veh_id, within=pe.NonNegativeReals)
    # model.right_occ_weight =  pe.Var(veh_id, within=pe.NonNegativeReals)
    # def calc_left_occ_weight(m,v):
    #     return m.left_occ_weight[v] == sum(m.wt[v, l, p] for l in lyr_id for p in row_1_id)
    # model.calc_left_occ_weight = pe.Constraint(veh_id, expr=calc_left_occ_weight)
    # def calc_right_occ_weight(m,v):
    #     return m.right_occ_weight[v] == sum(m.wt[v, l, p] for l in lyr_id for p in row_2_id)
    # model.calc_right_occ_weight = pe.Constraint(veh_id, expr=calc_right_occ_weight)
# -------------------------------------------------------------

    ######## Truck limit (Max Number of Trucks to Optimize at Once)
    # for all the ocean runs
    # for truck run - where no limit on max truck at a time


    print("Normal Optimization - Generating objective function for optimization model...")

    # CHANGE - original objective was: the sum of partial pallets (1 for full pallets) in # effective vehicles - exceeded SKU number
    # CHANGE  - original was (app.sku_penalty * m.exceed_mtl_n)
    # CHANGE  - posnBalCoeff[p] added
    # CHANGE  - sum(m.y_prior[plt] for plt in agg_prior_plt_id) * modelParams['PriorityIncentive']
    # Objective is to maximize the weighted sum of full pallets, partial pallets, priority pallets and account for penalty for excess SKUs
    """
    def obj(m):
        return sum(m.y_full[c, l, p, plt] * cntr_priority[c] #* plt_priorityVal_NAC[plt]
                   for c in eff_veh_id
                   for l in lyr_id
                   for p in pos_id
                   for plt in agg_full_plt_id) \
               + sum(m.y_prtl[c, l, p, plt] * prtl_frac[plt] * cntr_priority[c] #* plt_priorityVal_NAC[plt]
                    for c in eff_veh_id
                    for l in lyr_id
                    for p in pos_id
                    for plt in agg_prtl_plt_id) \
               + sum(m.y_prior_full[plt] for plt in agg_full_plt_id) * inputParams['priorityIncentive'] \
               + sum(m.y_prior_prtl[plt] for plt in agg_prtl_plt_id) * inputParams['priorityIncentive'] \
                - (sku_penalty * m.exceed_mtl_n * sku_penalty_factor)
    """
    def obj(m):
        return m.obj_full_plt_var_term1 + m.obj_full_plt_var_term2 \
               + m.obj_prtl_plt_var_term1 + m.obj_prtl_plt_var_term2 \
               + sum(m.y_prior_full[plt] for plt in agg_full_plt_id) * inputParams['priorityIncentive'] \
               + sum(m.y_prior_prtl[plt] for plt in agg_prtl_plt_id) * inputParams['priorityIncentive'] \
                - (sku_penalty * m.exceed_mtl_n * sku_penalty_factor)
    model.obj = pe.Objective(rule=obj, sense=pe.maximize)

    print("Solving optimization model...")

    ###### CBC SOLVER ######
    # opt = pe.SolverFactory('cbc')
    # opt.options['seconds'] = 120

    ###### XPRESS (COMMUNITY) SOLVER ######
    opt = pe.SolverFactory('xpress_direct')
    opt.options['MAXTIME'] = -time_limit
    opt.options['MIPRELSTOP'] = 0.01

    ###### GUROBI SOLVER ######
    # opt = pe.SolverFactory('gurobi')
    # SetGurobiOptions(opt)

    model.write(model_file)

    # results = opt.solve(model, timelimit = 120, tee = True)
    results = opt.solve(model, tee=True)

    print("Writing optimization results...")
    results.write()

    outputfile1, outputfile2, outputfile3, outputfile4, outputfile6, loading_instr_list, inputParams = PrintOutput(model, inputParams, outputfile1, outputfile2, outputfile3, outputfile4, outputfile6, run_ocean)

    # validation based on variable values
    num_full_plts_assigned = 0
    num_prtl_plts_assigned = 0
    # print variable values (validation)
    for c in eff_veh_id:
        for l in lyr_id:
            for p in pos_id:
                for plt in agg_full_plt_id:
                    if pe.value(model.y_full[c, l, p, plt]) > 0.001:
                        if eff_veh_id.index(c) <= len(eff_veh_id)-2 and plt_mtl_dict[plt] in highPrioritySKUList:
                            print("Full pallet variable value HIGH priority (obj fnc) is ", pe.value(model.y_full[c, l, p, plt])* cntr_priority[c]* inputParams['priorityIncentive'])
                            print("Container:",c, " position:", p)
                            num_full_plts_assigned+=1
                        if plt_mtl_dict[plt] not in highPrioritySKUList:
                            print("Full pallet variable value (obj fnc) is ", pe.value(model.y_full[c, l, p, plt])* cntr_priority[c])
                            print("Container:",c, " position:", p)
                            num_full_plts_assigned+=1
    for c in eff_veh_id:
        for l in lyr_id:
            for p in pos_id:
                for plt in agg_prtl_plt_id:
                    if pe.value(model.y_prtl[c, l, p, plt]) > 0.001:
                        if eff_veh_id.index(c) <= len(eff_veh_id)-2 and plt_mtl_dict[plt] in highPrioritySKUList:
                            print("Partial pallet variable value HIGH priority (obj fnc) is ", pe.value(model.y_prtl[c, l, p, plt])* prtl_frac[plt] * cntr_priority[c]* inputParams['priorityIncentive'])
                            print("Container:",c, " position:", p)
                            num_prtl_plts_assigned+=1
                        if plt_mtl_dict[plt] not in highPrioritySKUList:
                            print("Partial pallet variable value (obj fnc) is ", pe.value(model.y_prtl[c, l, p, plt])* prtl_frac[plt]* cntr_priority[c])
                            print("Container:",c, " position:", p)
                            num_prtl_plts_assigned+=1

    print("Number of full pallets assigned (based on variable values):", num_full_plts_assigned)
    print("Number of partial pallets assigned (based on variable values):", num_prtl_plts_assigned)
    print("SKU penalty:", sku_penalty)
    print("SKU penalty factor:", sku_penalty_factor)
    print("Total unique number of SKUs:", unique_sku_n)
    print("Excess number of SKU's (optimization variable):",pe.value(model.exceed_mtl_n))
    print("Priority incentive:", inputParams['priorityIncentive'])
    print("High Priority SKUs:", inputParams['skuPriorityIncentive'])
    # """
    print("Full pallet - var1 (Priority incentive):", pe.value(model.obj_full_plt_var_term1))
    print("Full pallet - var2:", pe.value(model.obj_full_plt_var_term2))
    print("Partial pallet - var1 (Priority incentive):", pe.value(model.obj_prtl_plt_var_term1))
    print("Partial pallet - var2:", pe.value(model.obj_prtl_plt_var_term2))
    # """
    # for c in veh_id:
    #     print("Total left occupied weight:", pe.value(model.left_occ_weight[c]))
    #     print("Total right occupied weight:", pe.value(model.right_occ_weight[c]))
    return model, outputfile1, outputfile2, outputfile3, outputfile4, loading_instr_list, inputParams, outputfile6

def PrintOutput(model, inputParams, outputfile1, outputfile2, outputfile3, outputfile4, outputfile6, run_ocean):

    print("Writing optimization results...")

    total_full_plts_assigned = 0
    total_prtl_plts_assigned = 0

    print(inputParams)          # added by HKD
    print(model)

    # display variable values #
    ####################### Output 1: Container Summary #############################
    output1 = []
    output1.append(['Ship-To', 'Container', 'Full Pallets', 'Partial Pallets', 'Utilization:Pallets', 'Utilization:Volume',
                   'Utilization:Weight', 'Cargo Weight:Front', 'Cargo Weight:Rear', 'SKU Count', 'Consider Holding To Consolidate'])
    ptab = PrettyTable(
        ['Ship-To', 'Container', 'Full Pallets', 'Partial Pallets', 'Utilization:Pallets', 'Utilization:Volume', 'Utilization:Weight', 'Cargo Weight:Front', 'Cargo Weight:Rear', 'SKU Count', 'Consider Holding To Consolidate'])
    total_full_plts_assigned_pltWise_main = {}
    for plt in inputParams["fullPalletList"]:
        if plt not in total_full_plts_assigned_pltWise_main:
            total_full_plts_assigned_pltWise_main[plt] = 0
    total_prtl_plts_assigned_pltWise_main = {}
    for plt in inputParams["partialPalletList"]:
        if plt not in total_prtl_plts_assigned_pltWise_main:
            total_prtl_plts_assigned_pltWise_main[plt] = 0

    for cntr in inputParams["containerList"]:
        total_full_plts_assigned_pltWise, total_prtl_plts_assigned_pltWise, total_occ_weight, total_occ_weight_front, total_occ_weight_rear, total_full_plts_cntr_assigned, total_prtl_plts_cntr_assigned, total_perc_utilized_pallets, total_perc_utilized_weight, total_perc_utilized_volume = ReturnContainerStats(
            model, cntr, inputParams)
        total_full_plts_assigned += total_full_plts_cntr_assigned
        total_prtl_plts_assigned += total_prtl_plts_cntr_assigned
        for plt in total_full_plts_assigned_pltWise:
            total_full_plts_assigned_pltWise_main[plt] += total_full_plts_assigned_pltWise[plt]
        for plt in total_prtl_plts_assigned_pltWise:
            total_prtl_plts_assigned_pltWise_main[plt] += total_prtl_plts_assigned_pltWise[plt]

        newCol = " "
        if run_ocean:  # Ocean
            threshold_util = inputParams['threshold40']
            if inputParams['threshold40'] <= 1:
                threshold_util = threshold_util * 100
            if total_perc_utilized_pallets < threshold_util:
                print("Utilization for container ", cntr,
                      " is less than threshold value")

        else:  # Truck
            
            threshold_util = inputParams['utilThreshold']
            if inputParams['utilThreshold'] <= 1:
                threshold_util = threshold_util * 100
            if total_perc_utilized_pallets < threshold_util:
                print("Utilization for container ", cntr, " is ", total_perc_utilized_pallets,
                      " and it is less than threshold value, ", threshold_util)
                newCol = "Yes"
                
        ptab.add_row(
            [inputParams['marketID'], cntr, total_full_plts_cntr_assigned, total_prtl_plts_cntr_assigned, total_perc_utilized_pallets, total_perc_utilized_volume, total_perc_utilized_weight, total_occ_weight_front, total_occ_weight_rear, pe.value(model.mtl_n[cntr]), newCol])
        # print(c, total_occ_weight, inputParams['max_wt'], total_occ_weight_front,  max_front_wt, total_occ_weight_rear, max_rear_wt,
        #       total_full_plts_cntr_assigned, total_prtl_plts_cntr_assigned, pe.value(model.mtl_n[c]))
        output1.append(
            [inputParams['marketID'], cntr, total_full_plts_cntr_assigned, total_prtl_plts_cntr_assigned, total_perc_utilized_pallets, total_perc_utilized_volume, total_perc_utilized_weight, total_occ_weight_front, total_occ_weight_rear, pe.value(model.mtl_n[cntr]), newCol])

    pd.DataFrame(output1).to_csv(outputfile1, index=False, header=False)
    print("container summary")
    print(ptab)
    
    
    # print("############################################################################")
    # for c in veh_id:
    #     print("Number of materials in container ", c, " is ", pe.value(model.mtl_n[c]))

    ######################### Output 2: Comparision of input & output / is there any leftover? ################################
    total_full_plt_dmd = 0.0
    for plt in inputParams["fullPalletDemandByMaterialBatch"]:
        total_full_plt_dmd += inputParams["fullPalletDemandByMaterialBatch"][plt]
    total_prtl_plt_dmd = 0.0
    for plt in inputParams["partialPalletDemandByMaterialBatch"]:
        total_prtl_plt_dmd += inputParams["partialPalletDemandByMaterialBatch"][plt]
    ptab = PrettyTable(['Pallet', 'Input data (num_pallets)',
                       'Solutions (pallets_assigned)',
                        'Leftover (pallets unassigned)'])
    ptab.add_row(["Full pallets", total_full_plt_dmd,
                 total_full_plts_assigned, total_full_plt_dmd-total_full_plts_assigned])
    ptab.add_row(["Partial pallets", total_prtl_plt_dmd,
                 total_prtl_plts_assigned, total_prtl_plt_dmd-total_prtl_plts_assigned])
    print(ptab)
    print("Total number of materials:", len(inputParams["materialList"]))
    print("Max SKU limit:", inputParams['max_sku_n'])
    # print("Total number of full pallets in input data:", total_full_plt_dmd)
    # print("Total number of partial pallets in input data:", total_prtl_plt_dmd)
    # print("Total number of full pallets assigned:", total_full_plts_assigned)
    # print("Total number of partial pallets assigned:", total_prtl_plts_assigned)

    ########################### Output-3: Leftover #############################

    if run_ocean:
        total_full_plt_dmd = {}
        ptab = PrettyTable(['Ship-To', 'Pallet Type', 'Pallet Name', 'Material', 'Brand', 'Order Number', 'Input data (num_pallets)',
                           'Solutions (pallets_assigned)',
                            'Leftover (pallets unassigned)', 'Cases(UOM)', 'Weight[lbs]'])
        output3 = []
        output3.append(['Ship-To', 'Material', 'Brand', 'Order Number',
                       'Cases(UOM)', 'Weight[lbs]', 'Pallets'])

        for plt in inputParams["fullPalletDemandByMaterialBatch"]:
            if plt not in total_full_plt_dmd:
                total_full_plt_dmd[plt] = 0
            total_full_plt_dmd[plt] = inputParams["fullPalletDemandByMaterialBatch"][plt]
            print(plt, total_full_plt_dmd[plt])
            ptab.add_row([inputParams['marketID'], "Full pallets", plt, str(inputParams['palletMaterialDict'][plt]), inputParams["palletBrandDict"][plt], inputParams['palletBatchDict'][plt], total_full_plt_dmd[plt],
                          total_full_plts_assigned_pltWise_main[plt], total_full_plt_dmd[plt]-total_full_plts_assigned_pltWise_main[plt], (total_full_plt_dmd[plt]-total_full_plts_assigned_pltWise_main[plt])*inputParams['palletNumCasesDict'][plt], (total_full_plt_dmd[plt]-total_full_plts_assigned_pltWise_main[plt])*inputParams["palletWeightDict"][plt]])
            output3.append([inputParams['marketID'], inputParams['palletMaterialDict'][plt], inputParams["palletBrandDict"][plt], inputParams['palletBatchDict'][plt], (total_full_plt_dmd[plt]-total_full_plts_assigned_pltWise_main[plt]) *
                           inputParams['palletNumCasesDict'][plt], (total_full_plt_dmd[plt]-total_full_plts_assigned_pltWise_main[plt])*inputParams["palletWeightDict"][plt], total_full_plt_dmd[plt]-total_full_plts_assigned_pltWise_main[plt]])
        total_prtl_plt_dmd = {}
        for plt in inputParams["partialPalletDemandByMaterialBatch"]:
            if plt not in total_prtl_plt_dmd:
                total_prtl_plt_dmd[plt] = 0
            total_prtl_plt_dmd[plt] += inputParams["partialPalletDemandByMaterialBatch"][plt]
            print(plt, total_prtl_plt_dmd[plt])
            ptab.add_row([inputParams['marketID'], "Partial pallets", plt, inputParams['palletMaterialDict'][plt], inputParams["palletBrandDict"][plt], inputParams['palletBatchDict'][plt], total_prtl_plt_dmd[plt],
                          total_prtl_plts_assigned_pltWise_main[plt], total_prtl_plt_dmd[plt]-total_prtl_plts_assigned_pltWise_main[plt], (total_prtl_plt_dmd[plt]-total_prtl_plts_assigned_pltWise_main[plt])*inputParams['palletNumCasesDict'][plt], (total_prtl_plt_dmd[plt]-total_prtl_plts_assigned_pltWise_main[plt])*inputParams["palletWeightDict"][plt]])
            output3.append([inputParams['marketID'], inputParams['palletMaterialDict'][plt], inputParams["palletBrandDict"][plt], inputParams['palletBatchDict'][plt], (total_prtl_plt_dmd[plt]-total_prtl_plts_assigned_pltWise_main[plt]) *
                           inputParams['palletNumCasesDict'][plt]*inputParams['partialFractionValueForPartialPallet'][plt], (total_prtl_plt_dmd[plt]-total_prtl_plts_assigned_pltWise_main[plt])*inputParams["palletWeightDict"][plt]* inputParams['partialFractionValueForPartialPallet'][plt], (total_prtl_plt_dmd[plt]-total_prtl_plts_assigned_pltWise_main[plt])*inputParams['partialFractionValueForPartialPallet'][plt]])
        print("Leftover - Ocean:")
        print(ptab)

        o3 = pd.DataFrame(output3)
        o3.columns = o3.iloc[0]
        output3a = []
        output3a.append(['Ship-To', 'Material', 'Brand', 'Order Number',
                         'Cases(UOM)', 'Weight[lbs]', 'Pallets'])
        # print(o3)
        uniqueSKU = o3["Material"].unique()
        for plt in uniqueSKU[1:]:
            x = o3[o3['Material'] == plt]
            output3a.append([inputParams['marketID'], plt, x['Brand'].unique()[0], x['Order Number'].unique()[
                            0], x['Cases(UOM)'].sum(), x['Weight[lbs]'].sum(), x['Pallets'].sum()])

        pd.DataFrame(output3a).to_csv(outputfile3, index=False, header=False)
    else:

        ptab = PrettyTable(['Ship-To', 'Pallet Type', 'Pallet Name', 'Material', 'Order Number', 'Input data (num_pallets)',
                           'Solutions (pallets_assigned)',
                            'Leftover (pallets unassigned)', 'Cases(UOM)', 'Weight[lbs]'])
        output3 = []
        output3.append(['Ship-To', 'Material', 'Order Number',
                       'Cases(UOM)', 'Weight[lbs]', 'Pallets'])

        total_full_plt_dmd = {}
        for plt in inputParams["fullPalletDemandByMaterialBatch"]:
            if plt not in total_full_plt_dmd:
                total_full_plt_dmd[plt] = 0
            total_full_plt_dmd[plt] = inputParams["fullPalletDemandByMaterialBatch"][plt]
            print(plt, total_full_plt_dmd[plt])
            ptab.add_row([inputParams['marketID'], "Full pallets", plt, str(inputParams['palletMaterialDict'][plt]), inputParams['palletBatchDict'][plt], total_full_plt_dmd[plt],
                          total_full_plts_assigned_pltWise_main[plt], total_full_plt_dmd[plt]-total_full_plts_assigned_pltWise_main[plt], (total_full_plt_dmd[plt]-total_full_plts_assigned_pltWise_main[plt])*inputParams['palletNumCasesDict'][plt], (total_full_plt_dmd[plt]-total_full_plts_assigned_pltWise_main[plt])*inputParams["palletWeightDict"][plt]])
            output3.append([inputParams['marketID'], inputParams['palletMaterialDict'][plt], inputParams['palletBatchDict'][plt], (total_full_plt_dmd[plt]-total_full_plts_assigned_pltWise_main[plt]) *
                           inputParams['palletNumCasesDict'][plt], (total_full_plt_dmd[plt]-total_full_plts_assigned_pltWise_main[plt])*inputParams["palletWeightDict"][plt], total_full_plt_dmd[plt]-total_full_plts_assigned_pltWise_main[plt]])
        total_prtl_plt_dmd = {}
        for plt in inputParams["partialPalletDemandByMaterialBatch"]:
            if plt not in total_prtl_plt_dmd:
                total_prtl_plt_dmd[plt] = 0
            total_prtl_plt_dmd[plt] += inputParams["partialPalletDemandByMaterialBatch"][plt]
            print(plt, total_prtl_plt_dmd[plt])
            ptab.add_row([inputParams['marketID'], "Partial pallets", plt, inputParams['palletMaterialDict'][plt], inputParams['palletBatchDict'][plt], total_prtl_plt_dmd[plt],
                          total_prtl_plts_assigned_pltWise_main[plt], total_prtl_plt_dmd[plt]-total_prtl_plts_assigned_pltWise_main[plt], (total_prtl_plt_dmd[plt]-total_prtl_plts_assigned_pltWise_main[plt])*inputParams['palletNumCasesDict'][plt], (total_prtl_plt_dmd[plt]-total_prtl_plts_assigned_pltWise_main[plt])*inputParams["palletWeightDict"][plt]])
            output3.append([inputParams['marketID'], inputParams['palletMaterialDict'][plt], inputParams['palletBatchDict'][plt], (total_prtl_plt_dmd[plt]-total_prtl_plts_assigned_pltWise_main[plt]) *
                           inputParams['palletNumCasesDict'][plt]*inputParams['partialFractionValueForPartialPallet'][plt], (total_prtl_plt_dmd[plt]-total_prtl_plts_assigned_pltWise_main[plt])*inputParams["palletWeightDict"][plt]*inputParams['partialFractionValueForPartialPallet'][plt], (total_prtl_plt_dmd[plt]-total_prtl_plts_assigned_pltWise_main[plt])*inputParams['partialFractionValueForPartialPallet'][plt]])
        print(ptab)

        o3 = pd.DataFrame(output3)
        o3.columns = o3.iloc[0]
        output3a = []
        output3a.append(['Ship-To', 'Material', 'Order Number',
                         'Cases(UOM)', 'Weight[lbs]', 'Pallets'])
        # print(o3)
        uniqueSKU = o3["Material"].unique()
        for plt in uniqueSKU[1:]:
            x = o3[o3['Material'] == plt]
            output3a.append([inputParams['marketID'], plt, x['Order Number'].unique()[
                            0], x['Cases(UOM)'].sum(), x['Weight[lbs]'].sum(), x['Pallets'].sum()])

        pd.DataFrame(output3a).to_csv(outputfile3, index=False, header=False)


    ######################## Output 2: Cargo Summary ########################
    output2 = []
    output2.append(['Ship-To', 'Container', 'Material', 'Brand', 'Order Number',
                   'Material Description', 'Pallets', 'Cases', 'Weight[lb]', 'Cube[ft^3]'])
    ptab = PrettyTable(
        ['Ship-To', 'Container', 'Material', 'Brand', 'Order Number', 'Material Description', 'Pallets', 'Cases', 'Weight[lb]', 'Cube[ft^3]'])




    ######################## Output 3 #######################################
    for plt in inputParams["fullPalletList"]:
        if pe.value(model.y_prior_full[plt]) > 0:
            print("Total number of (FULL) priority pallets assigned for full pallet :", plt, " is ",
                  pe.value(model.y_prior_full[plt]))
    for plt in inputParams["partialPalletList"]:
        if pe.value(model.y_prior_prtl[plt]) > 0:
            print("Total number of (PARTIAL) priority pallets assigned for partial pallet :", plt, " is ",
                  pe.value(model.y_prior_prtl[plt]))

    ############################ Output 4: Loading Instructions, output 2: Cargo Summary ######################################
    if run_ocean:
        output4 = []
        loading_instr_list = []

        output4.append(['Ship-To', 'Container', 'Sequence', 'Position',  'Layer',  # 'Pallet type', 'Pallet ID',"Pallet ID(orig)", 'Weight',
                        'Material SKU code', 'Brand', 'Material Description', 'Order Number', 'Cases', 'Weight[lb]', 'Cube[ft^3]', 'Height', 'Total Cases', 'Is Partial ?'])  # 'Cases', 'Batch Date', 'Bin Location'])
        ptab = PrettyTable(['Ship-To', 'Container', 'Sequence', 'Position',  'Layer',  # 'Pallet type', 'Pallet ID',"Pallet ID(orig)", 'Weight',
                            'Material SKU code', 'Brand', 'Material Description', 'Order Number', 'Cases', 'Weight[lb]', 'Cube[ft^3]', 'Height', 'Total Cases', 'Is Partial ?'])  # 'Cases', 'Batch Date', 'Bin Location'])
        loading_instr_list, inputParams = GenerateLoadingInstructions(
            model, inputParams, run_ocean)
        # print(loading_instr_list)

        for load_obj in loading_instr_list:
            ptab.add_row([inputParams['marketID'], int(load_obj.container), load_obj.sequence, load_obj.position, load_obj.layer,  # load_obj.pallet_type, load_obj.palletID, load_obj.palletID_II, load_obj.weight,
                          load_obj.material, load_obj.brand, load_obj.bin_loc, load_obj.batch, load_obj.cases, load_obj.weight, load_obj.cube, load_obj.height, load_obj.total_cases, load_obj.is_partial])  # , load_obj.cases, load_obj.batch_date,load_obj.bin_loc])
            output4.append([inputParams['marketID'], int(load_obj.container), load_obj.sequence, load_obj.position, load_obj.layer,  # load_obj.pallet_type, load_obj.palletID, load_obj.palletID_II, load_obj.weight,
                            load_obj.material, load_obj.brand, load_obj.bin_loc, load_obj.batch, load_obj.cases, load_obj.weight, load_obj.cube, load_obj.height, load_obj.total_cases, load_obj.is_partial])  # load_obj.cases, load_obj.batch_date, load_obj.bin_loc])
            # print(load_obj.height)

        o4 = pd.DataFrame(output4)
        o4.columns = o4.iloc[0]
        #print("Total weight[lb]: ", o4['Weight[lb]'].sum())

        output2 = []
        output2.append(['Ship-To', 'Container', 'Material', 'Brand', 'Order Number',
                       'Material Description', 'Pallets', 'Cases', 'Weight[lb]', 'Cube[ft^3]'])
        ptab2 = PrettyTable(
            ['Ship-To', 'Container', 'Material', 'Brand', 'Order Number', 'Material Description', 'Pallets', 'Cases', 'Weight[lb]', 'Cube[ft^3]'])

        uniqueSKU = o4["Material SKU code"].unique()
        uniqueTrucks = o4['Container'].unique()
        uniqueBatches = o4['Order Number'].unique()
        print(uniqueSKU[1:], uniqueTrucks[1:], uniqueBatches[1:])

        for t in uniqueTrucks[1:]:
            for sku in uniqueSKU[1:]:
                #for b in uniqueBatches[1:]:
                x = o4[(o4['Container'] == t) & (o4['Material SKU code'] == sku)]
                # print(t, sku)
                #print(x)
                if not x.empty:
                    ptab2.add_row([inputParams['marketID'], t, sku, x['Brand'].unique()[0], x['Order Number'].unique()[0], x['Material Description'].unique()[
                                  0], x['Cases'].sum()/x['Total Cases'].unique()[0], x['Cases'].sum(), x['Weight[lb]'].sum(), x['Cube[ft^3]'].sum()])
                    output2.append([inputParams['marketID'], t, sku, x['Brand'].unique()[0], x['Order Number'].unique()[0], x['Material Description'].unique()[
                                   0], x['Cases'].sum()/x['Total Cases'].unique()[0], x['Cases'].sum(), x['Weight[lb]'].sum(), x['Cube[ft^3]'].sum()])

        #print("Total weight[lb]: ", output2['Weight[lb]'].sum())

        pd.DataFrame(output2).to_csv(outputfile2, index=False, header=False)
        del o4['Total Cases']
        o4.to_csv(outputfile4, index=False, header=False)
        print("Loading Instr: Ocean")
        print(ptab)
        print("Cargo Summary: Ocean")
        print(ptab2)

    else:
        output4 = []
        loading_instr_list = []

        output4.append(['Ship-To', 'Container', 'Sequence', 'Position',  'Layer',  # 'Pallet type', 'Pallet ID',"Pallet ID(orig)", 'Weight',
                        'Material SKU code', 'Brand', 'Material Description', 'Order Number', 'Cases', 'Weight[lb]', 'Cube[ft^3]', 'Total Cases', 'Is Partial ?'])  # 'Cases', 'Batch Date', 'Bin Location'])
        ptab = PrettyTable(['Ship-To', 'Container', 'Sequence', 'Position',  'Layer',  # 'Pallet type', 'Pallet ID',"Pallet ID(orig)", 'Weight',
                            'Material SKU code', 'Brand', 'Material Description', 'Order Number', 'Cases', 'Weight[lb]', 'Cube[ft^3]', 'Total Cases', 'Is Partial ?'])  # 'Cases', 'Batch Date', 'Bin Location'])
        loading_instr_list, inputParams = GenerateLoadingInstructions(model, inputParams, run_ocean)
        print(':AK__load_instr')

        for load_obj in loading_instr_list:
            ptab.add_row([inputParams['marketID'], int(load_obj.container), load_obj.sequence, load_obj.position, load_obj.layer,  # load_obj.pallet_type, load_obj.palletID, load_obj.palletID_II, load_obj.weight,
                          load_obj.material, "", load_obj.description, load_obj.batch, load_obj.cases, load_obj.weight, load_obj.cube, load_obj.total_cases, load_obj.is_partial])  # , load_obj.cases, load_obj.batch_date,load_obj.bin_loc])
            output4.append([inputParams['marketID'], int(load_obj.container), load_obj.sequence, load_obj.position, load_obj.layer,  # load_obj.pallet_type, load_obj.palletID, load_obj.palletID_II, load_obj.weight,
                            load_obj.material, "", load_obj.description, load_obj.batch, load_obj.cases, load_obj.weight, load_obj.cube, load_obj.total_cases, load_obj.is_partial])  # load_obj.cases, load_obj.batch_date, load_obj.bin_loc])
            # print(load_obj.height)

        o4 = pd.DataFrame(output4)
        #o4.to_csv('\AK_O4.csv')
        #print(':AK__load_instr_END')
        o4.columns = o4.iloc[0]

        output2 = []
        output2.append(['Ship-To', 'Container', 'Material', 'Brand', 'Order Number',
                       'Material Description', 'Pallets', 'Cases', 'Weight[lb]', 'Cube[ft^3]'])
        ptab2 = PrettyTable(
            ['Ship-To', 'Container', 'Material', 'Brand', 'Order Number', 'Material Description', 'Pallets', 'Cases', 'Weight[lb]', 'Cube[ft^3]'])

        uniqueSKU = o4["Material SKU code"].unique()
        uniqueTrucks = o4['Container'].unique()
        print('AK_test')
        print(uniqueSKU[1:], uniqueTrucks[1:])
        print(o4)
        print('-----------------------------')
        for t in uniqueTrucks[1:]:
            for sku in uniqueSKU[1:]:
                x = o4[(o4['Container'] == t) & (o4['Material SKU code'] == sku)]
                print(t, sku)
                print(x)
                if not x.empty:
                    """
                    ptab2.add_row([inputParams['marketID'], t, sku, "", str(x['Order Number'].unique()[0]), x['Material Description'].unique()[
                                  0], len(x.index), x['Cases'].sum(), x['Weight[lb]'].sum(), x['Cube[ft^3]'].sum()])
                    output2.append([inputParams['marketID'], t, sku, "", str(x['Order Number'].unique()[0]), x['Material Description'].unique()[
                                   0], len(x.index), x['Cases'].sum(), x['Weight[lb]'].sum(), x['Cube[ft^3]'].sum()])
                    """
                    ptab2.add_row([inputParams['marketID'], t, sku, "", str(x['Order Number'].unique()[0]), x['Material Description'].unique()[
                                  0], x['Cases'].sum()/x['Total Cases'].unique()[0], x['Cases'].sum(), x['Weight[lb]'].sum(), x['Cube[ft^3]'].sum()])
                    output2.append([inputParams['marketID'], t, sku, "", str(x['Order Number'].unique()[0]), x['Material Description'].unique()[
                                   0], x['Cases'].sum()/x['Total Cases'].unique()[0], x['Cases'].sum(), x['Weight[lb]'].sum(), x['Cube[ft^3]'].sum()])

        #output2['Order Number'] = output2['Order Number'].astype(str)
        pd.DataFrame(output2).to_csv(outputfile2, index=False, header=False)

        del o4['Total Cases']
        o4.to_csv(outputfile4, index=False, header=False)

        print(ptab)
        print(ptab2)

    ############################ Output 5: Weight Balance ######################################
    if run_ocean:
        output5 = []
        output5.append(['Ship-To', 'Container', 'Full Pallets', 'Partial Pallets', 'Total Wgt',
                       'Front', 'Rear', 'Left', 'Right'])
        ptab5 = PrettyTable(['Ship-To', 'Container', 'Full Pallets', 'Partial Pallets', 'Total Wgt', 'Front', 'Rear', 'Left', 'Right'])

        uniqueSKU = o4["Material SKU code"].unique()
        uniqueTrucks = o4['Container'].unique()
        
        print("Creating Weight Balance tab - Opt model")
        print(uniqueSKU[1:], uniqueTrucks[1:])

        for t in uniqueTrucks[1:]:
            #for b in uniqueBatches[1:]:
            x = o4[(o4['Container'] == t)]
            print(t)
            if not x.empty:
                items = x['Is Partial ?'].tolist()
                count_full = items.count(False)
                count_partial = items.count(True)
                total_occ_weight, total_occ_weight_front, total_occ_weight_rear,  total_occ_weight_right, total_occ_weight_left = ReturnContainerStats_2(model, t, inputParams)
                cont_id = "Container_" + str(t)
                ptab5.add_row([inputParams['marketID'], cont_id, count_full, count_partial, total_occ_weight, (total_occ_weight_front/total_occ_weight)*100, (total_occ_weight_rear/total_occ_weight)*100, (total_occ_weight_right/total_occ_weight)*100, (total_occ_weight_left/total_occ_weight)*100])
                output5.append([inputParams['marketID'], cont_id, count_full, count_partial, total_occ_weight, (total_occ_weight_front/total_occ_weight)*100, (total_occ_weight_rear/total_occ_weight)*100, (total_occ_weight_right/total_occ_weight)*100, (total_occ_weight_left/total_occ_weight)*100])
        
        
        #pd.DataFrame(output5).to_csv(outputfile2, index=False, header=False)
        pd.DataFrame(output5).to_csv(outputfile6, index=False, header=False)
        print(ptab5)
    else:
        output5 = []
        output5.append(['Ship-To', 'Container', 'Full Pallets', 'Partial Pallets', 'Total Wgt',
                       'Front', 'Rear', 'Left', 'Right'])
        ptab5 = PrettyTable(['Ship-To', 'Container', 'Full Pallets', 'Partial Pallets', 'Total Wgt', 'Front', 'Rear', 'Left', 'Right'])

        uniqueSKU = o4["Material SKU code"].unique()
        uniqueTrucks = o4['Container'].unique()
        
        print("Creating Weight Balance tab - Opt model")
        print(uniqueSKU[1:], uniqueTrucks[1:])

        for t in uniqueTrucks[1:]:
            #for b in uniqueBatches[1:]:
            x = o4[(o4['Container'] == t)]
            print(t)
            if not x.empty:
                items = x['Is Partial ?'].tolist()
                count_full = items.count(False)
                count_partial = items.count(True)
                total_occ_weight, total_occ_weight_front, total_occ_weight_rear,  total_occ_weight_right, total_occ_weight_left = ReturnContainerStats_2(model, t, inputParams)
                
                ptab5.add_row([inputParams['marketID'], t, count_full, count_partial, total_occ_weight, total_occ_weight_front/total_occ_weight, total_occ_weight_rear/total_occ_weight, total_occ_weight_right/total_occ_weight, total_occ_weight_left/total_occ_weight])
                output5.append([inputParams['marketID'], t, count_full, count_partial, total_occ_weight, total_occ_weight_front/total_occ_weight, total_occ_weight_rear/total_occ_weight, total_occ_weight_right/total_occ_weight, total_occ_weight_left/total_occ_weight])
        
        
        #pd.DataFrame(output5).to_csv(outputfile2, index=False, header=False)
        pd.DataFrame(output5).to_csv(outputfile6, index=False, header=False)
        print(ptab5)
                
    return outputfile1, outputfile2, outputfile3, outputfile4, outputfile6, loading_instr_list, inputParams


def ReturnContainerStats(model, cntr, inputParams):

    total_full_plts_assigned = 0
    total_prtl_plts_assigned = 0
    total_full_plts_assigned_pltWise = {}
    for plt in inputParams["fullPalletList"]:
        if plt not in total_full_plts_assigned_pltWise:
            total_full_plts_assigned_pltWise[plt] = 0
    total_prtl_plts_assigned_pltWise = {}
    for plt in inputParams["partialPalletList"]:
        if plt not in total_prtl_plts_assigned_pltWise:
            total_prtl_plts_assigned_pltWise[plt] = 0
    loadedContainer_LessUtilized = False

    total_occ_weight = 0.0
    total_occ_weight_front = 0.0
    total_occ_weight_rear = 0.0
    total_occ_weight_left = 0.0
    total_occ_weight_right = 0.0
    total_full_plts_cntr_assigned = 0
    total_prtl_plts_cntr_assigned = 0
    total_stacked_height = {}
    for p in range(1, len(inputParams["positionList"])+1):
        total_stacked_height[p] = 0

    bal_coef = inputParams["positionGroupBalanceCoefficientDict"]

    for l in inputParams["layerList"]:
        for p in inputParams["positionList"]:
            for plt in inputParams["fullPalletList"]:
                if pe.value(model.y_full[cntr, l, p, plt]) >= 0.9:
                    total_full_plts_assigned += 1
                    total_full_plts_assigned_pltWise[plt] += 1
                    total_full_plts_cntr_assigned += 1
                    total_occ_weight += inputParams["palletWeightDict"][plt]
                    """
                    if p in inputParams["frontPositionList"]:
                        total_occ_weight_front += (bal_coef[(p, "A")] + bal_coef[(
                            p, "C")])*inputParams["palletWeightDict"][plt]
                    if p in inputParams["rearPositionList"]:
                        total_occ_weight_rear += (bal_coef[(p, "B")] + bal_coef[(
                            p, "D")])*inputParams["palletWeightDict"][plt]
                    """
                    if p in inputParams["frontPositionList"]:
                        total_occ_weight_front += inputParams["palletWeightDict"][plt]
                    if p in inputParams["rearPositionList"]:
                        total_occ_weight_rear += inputParams["palletWeightDict"][plt]

                    if p in inputParams["leftPositionList"]:
                        total_occ_weight_left += inputParams["palletWeightDict"][plt]
                    if p in inputParams["rightPositionList"]:
                        total_occ_weight_right += inputParams["palletWeightDict"][plt]

                    print("Container ", cntr, " layer ", l, " position ", p,
                          " is occupied by full pallet ", plt)  # wt[plt])
                    if l == 1:
                        total_stacked_height[p] = inputParams['palletHeightDict'][plt]
                    else:
                        total_stacked_height[p] = total_stacked_height[p] + \
                            inputParams['palletHeightDict'][plt]

            for plt in inputParams["partialPalletList"]:
                if pe.value(model.y_prtl[cntr, l, p, plt]) >= 0.9:
                    total_prtl_plts_assigned += 1
                    total_prtl_plts_assigned_pltWise[plt] += 1
                    total_prtl_plts_cntr_assigned += 1
                    total_occ_weight += inputParams["palletWeightDict"][plt]
                    """
                    if p in inputParams["frontPositionList"]:
                        total_occ_weight_front += (bal_coef[(p, "A")] + bal_coef[(
                            p, "C")])*inputParams["palletWeightDict"][plt]
                    if p in inputParams["rearPositionList"]:
                        total_occ_weight_rear += (bal_coef[(p, "B")] + bal_coef[(
                            p, "D")])*inputParams["palletWeightDict"][plt]
                    """
                    if p in inputParams["frontPositionList"]:
                        total_occ_weight_front += inputParams["palletWeightDict"][plt]
                    if p in inputParams["rearPositionList"]:
                        total_occ_weight_rear += inputParams["palletWeightDict"][plt]
                        
                    if p in inputParams["leftPositionList"]:
                        total_occ_weight_left += inputParams["palletWeightDict"][plt]
                    if p in inputParams["rightPositionList"]:
                        total_occ_weight_right += inputParams["palletWeightDict"][plt]

                    if l == 1:
                        total_stacked_height[p] = inputParams['palletHeightDict'][plt]
                    else:
                        total_stacked_height[p] = total_stacked_height[p] + \
                            inputParams['palletHeightDict'][plt]

    print("Stacked height for all the positions: ", total_stacked_height)
    print("Final stacked height is ", sum(total_stacked_height.values()))
    print("Total occupied pallet weight (left):", total_occ_weight_left)
    print("Total occupied pallet weight (right):", total_occ_weight_right)
    print("Total occupied pallet weight (front):", total_occ_weight_front)
    print("Total occupied pallet weight (rear):", total_occ_weight_rear)
    # print(pe.value(model.con_stk_ht))

    # changes were done by HKD
    total_perc_utilized_pallets = (
        (total_full_plts_cntr_assigned + total_prtl_plts_cntr_assigned)/(2 * inputParams['max_plt_n']))*100
    total_perc_utilized_weight = (
        total_occ_weight / inputParams['max_wt']) * 100
    total_perc_utilized_volume = (sum(total_stacked_height.values()) / (inputParams['containerHeight'] * (inputParams['max_plt_n']-2) + (2 * (inputParams['containerHeightAtDoor']))))*100

    return total_full_plts_assigned_pltWise, total_prtl_plts_assigned_pltWise, total_occ_weight, total_occ_weight_front, total_occ_weight_rear, total_full_plts_cntr_assigned, total_prtl_plts_cntr_assigned, total_perc_utilized_pallets, total_perc_utilized_weight, total_perc_utilized_volume

def ReturnContainerStats_2(model, cntr, inputParams):

    total_full_plts_assigned = 0
    total_prtl_plts_assigned = 0
    total_full_plts_assigned_pltWise = {}
    for plt in inputParams["fullPalletList"]:
        if plt not in total_full_plts_assigned_pltWise:
            total_full_plts_assigned_pltWise[plt] = 0
    total_prtl_plts_assigned_pltWise = {}
    for plt in inputParams["partialPalletList"]:
        if plt not in total_prtl_plts_assigned_pltWise:
            total_prtl_plts_assigned_pltWise[plt] = 0
    loadedContainer_LessUtilized = False

    total_occ_weight = 0.0
    total_occ_weight_front = 0.0
    total_occ_weight_rear = 0.0
    total_occ_weight_left = 0.0
    total_occ_weight_right = 0.0
    total_full_plts_cntr_assigned = 0
    total_prtl_plts_cntr_assigned = 0
    total_stacked_height = {}
    for p in range(1, len(inputParams["positionList"])+1):
        total_stacked_height[p] = 0

    bal_coef = inputParams["positionGroupBalanceCoefficientDict"]

    for l in inputParams["layerList"]:
        for p in inputParams["positionList"]:
            for plt in inputParams["fullPalletList"]:
                if pe.value(model.y_full[cntr, l, p, plt]) >= 0.9:
                    total_full_plts_assigned += 1
                    total_full_plts_assigned_pltWise[plt] += 1
                    total_full_plts_cntr_assigned += 1
                    total_occ_weight += inputParams["palletWeightDict"][plt]
                    """
                    if p in inputParams["frontPositionList"]:
                        total_occ_weight_front += (bal_coef[(p, "A")] + bal_coef[(
                            p, "C")])*inputParams["palletWeightDict"][plt]
                    if p in inputParams["rearPositionList"]:
                        total_occ_weight_rear += (bal_coef[(p, "B")] + bal_coef[(
                            p, "D")])*inputParams["palletWeightDict"][plt]
                    """
                    if p in inputParams["frontPositionList"]:
                        total_occ_weight_front += inputParams["palletWeightDict"][plt]
                    if p in inputParams["rearPositionList"]:
                        total_occ_weight_rear += inputParams["palletWeightDict"][plt]
                        
                    if p in inputParams["leftPositionList"]:
                        total_occ_weight_left += inputParams["palletWeightDict"][plt]
                    if p in inputParams["rightPositionList"]:
                        total_occ_weight_right += inputParams["palletWeightDict"][plt]

                    print("Container ", cntr, " layer ", l, " position ", p,
                          " is occupied by full pallet ", plt)  # wt[plt])
                    if l == 1:
                        total_stacked_height[p] = inputParams['palletHeightDict'][plt]
                    else:
                        total_stacked_height[p] = total_stacked_height[p] + \
                            inputParams['palletHeightDict'][plt]

            for plt in inputParams["partialPalletList"]:
                if pe.value(model.y_prtl[cntr, l, p, plt]) >= 0.9:
                    total_prtl_plts_assigned += 1
                    total_prtl_plts_assigned_pltWise[plt] += 1
                    total_prtl_plts_cntr_assigned += 1
                    total_occ_weight += inputParams["palletWeightDict"][plt]
                    """
                    if p in inputParams["frontPositionList"]:
                        total_occ_weight_front += (bal_coef[(p, "A")] + bal_coef[(
                            p, "C")])*inputParams["palletWeightDict"][plt]
                    if p in inputParams["rearPositionList"]:
                        total_occ_weight_rear += (bal_coef[(p, "B")] + bal_coef[(
                            p, "D")])*inputParams["palletWeightDict"][plt]
                    """
                    if p in inputParams["frontPositionList"]:
                        total_occ_weight_front += inputParams["palletWeightDict"][plt]
                    if p in inputParams["rearPositionList"]:
                        total_occ_weight_rear += inputParams["palletWeightDict"][plt]
                        
                    if p in inputParams["leftPositionList"]:
                        total_occ_weight_left += inputParams["palletWeightDict"][plt]
                    if p in inputParams["rightPositionList"]:
                        total_occ_weight_right += inputParams["palletWeightDict"][plt]

                    if l == 1:
                        total_stacked_height[p] = inputParams['palletHeightDict'][plt]
                    else:
                        total_stacked_height[p] = total_stacked_height[p] + \
                            inputParams['palletHeightDict'][plt]

    print("Stacked height for all the positions: ", total_stacked_height)
    print("Final stacked height is ", sum(total_stacked_height.values()))
    print("Total occupied weight: ", total_occ_weight)
    print("Total occupied pallet weight (left):", total_occ_weight_left)
    print("Total occupied pallet weight (right):", total_occ_weight_right)
    print("Total occupied pallet weight (front):", total_occ_weight_front)
    print("Total occupied pallet weight (rear):", total_occ_weight_rear)
    # print(pe.value(model.con_stk_ht))

    # changes were done by HKD
    total_perc_utilized_pallets = (
        (total_full_plts_cntr_assigned + total_prtl_plts_cntr_assigned)/(2 * inputParams['max_plt_n']))*100
    total_perc_utilized_weight = (
        total_occ_weight / inputParams['max_wt']) * 100
    total_perc_utilized_volume = (sum(total_stacked_height.values()) / (inputParams['containerHeight'] * (inputParams['max_plt_n']-2) + (2 * (inputParams['containerHeightAtDoor']))))*100

    return total_occ_weight, total_occ_weight_front, total_occ_weight_rear, total_occ_weight_right, total_occ_weight_left 


def GenerateLoadingInstructions(model, inputParams, run_ocean):
    loading_instr_list = []
    bin_locs_assigned = []
    material_count_dict = dict()  # counting the number of pallets of given SKU
    # palletIDs_dict= dict()
    for c in inputParams["containerList"]:
        for p in inputParams["positionList"]:
            for l in inputParams["layerList"]:
                for plt in inputParams["fullPalletList"]:
                    #print(pe.value(model.y_full[c, l, p, plt]))
                    if pe.value(model.y_full[c, l, p, plt]) >= 0.9:# ==1

                        load_obj = Load_Obj()
                        load_obj.marketID = inputParams["marketID"]
                        load_obj.container = c
                        load_obj.layer = l
                        load_obj.position = p
                        load_obj.sequence = inputParams["positionSequenceDict"][p]

                        load_obj.material = inputParams["palletMaterialDict"][plt]
                        #load_obj.brand = inputParams["palletBrandDict"][plt]
                        if run_ocean:
                            load_obj.brand = inputParams["palletBrandDict"][plt]
                        else:
                            load_obj.brand = ""


                        if run_ocean:
                            load_obj.description = ""
                        else:
                            load_obj.description = inputParams["palletDescriptionDict"][plt]
                            #load_obj.description = ""

                        #load_obj.description = "A"
                        mtl_count = 0
                        if load_obj.material in material_count_dict:
                            mtl_count = material_count_dict[load_obj.material]
                        material_count_dict.update({load_obj.material: mtl_count+1})

                        load_obj.batch = inputParams["palletBatchDict"][plt]
                        load_obj.bin_loc = inputParams["palletBinLocDict"][plt]
                        load_obj.pallet_type = "Full"
                        
                        if load_obj.bin_loc == 'Many':
                            key_str_binloc = inputParams["marketID"] + "_" + str(load_obj.material) + "_" + str(load_obj.batch) + "_" + str(load_obj.pallet_type)
                            print(key_str_binloc)
                            if key_str_binloc in inputParams["customerMaterialBatch_BinLoc_Dict"]:
                                bin_loc_list = inputParams["customerMaterialBatch_BinLoc_Dict"][key_str_binloc]
                                print(bin_loc_list)
                                bin_loc = bin_loc_list[0]
                                load_obj.bin_loc = bin_loc
                                if len(bin_loc_list) >= 1:
                                    bin_loc_list.pop(0)
                        """
                            # check for duplication
                            if bin_loc in bin_locs_assigned:
                                print("Bin loc assigned more than once.....")
                            bin_locs_assigned.append(bin_loc)
                        """
                        load_obj.palletID = "pallet_" + load_obj.bin_loc
                        load_obj.palletID_II = plt
                        # load_obj.palletID = plt
                        load_obj.cases = inputParams["palletNumCasesDict"][plt]
                        load_obj.total_cases = (inputParams["palletNumCasesDict"][plt])         # added by HKD
                        load_obj.sto_num = ''
                        load_obj.weight = inputParams["palletWeightDict"][plt]
                        load_obj.cube = (40/12) * (48/12) * \
                            inputParams["palletHeightDict"][plt]
                        load_obj.height = inputParams["palletHeightDict"][plt]
                        load_obj.is_partial = False
                        load_obj.batch_date = inputParams["palletBatchDateDict"][plt]
                        # palletIDs_dict.update({load_obj.palletID:load_obj.palletID_II})
                        loading_instr_list.append(load_obj)

                for plt in inputParams["partialPalletList"]:
                    if pe.value(model.y_prtl[c, l, p, plt]) >= 0.9:
                        load_obj = Load_Obj()
                        load_obj.marketID = inputParams["marketID"]
                        load_obj.container = c
                        load_obj.layer = l
                        load_obj.position = p
                        load_obj.sequence = inputParams["positionSequenceDict"][p]

                        if run_ocean:
                            load_obj.description = ""
                        else:
                            load_obj.description = inputParams["palletDescriptionDict"][plt]
                            #load_obj.description = ""

                        #load_obj.description = "A"
                        load_obj.material = inputParams["palletMaterialDict"][plt]
                        #load_obj.brand = inputParams["palletBrandDict"][plt]
                        if run_ocean:
                            load_obj.brand = inputParams["palletBrandDict"][plt]
                        else:
                            load_obj.brand = ""
                        mtl_count = 0
                        if load_obj.material in material_count_dict:
                            mtl_count = material_count_dict[load_obj.material]
                        material_count_dict.update({load_obj.material: mtl_count + 1})

                        load_obj.batch = inputParams["palletBatchDict"][plt]
                        load_obj.bin_loc = inputParams["palletBinLocDict"][plt]

                        bin_locs_assigned.append(load_obj.bin_loc)

                        #"""
                        if load_obj.bin_loc == 'Many':
                            key_str_binloc = inputParams["marketID"] + "_" + str(inputParams["palletMaterialDict"][plt]) + "_" + str(inputParams["palletBatchDict"][plt])
                            if key_str_binloc in inputParams["customerMaterialBatch_BinLoc_Dict"]:
                                bin_loc = inputParams["customerMaterialBatch_BinLoc_Dict"][key_str_binloc][0]
                                load_obj.bin_loc = bin_loc
                                inputParams["customerMaterialBatch_BinLoc_Dict"][key_str_binloc] = inputParams["customerMaterialBatch_BinLoc_Dict"][key_str_binloc][1:]
                        #"""
                        load_obj.palletID = "pallet_" + load_obj.bin_loc
                        load_obj.palletID_II = plt
                        # load_obj.palletID = plt
                        load_obj.pallet_type = "Partial"
                        #load_obj.cases = (inputParams["palletNumCasesDict"][plt])*(inputParams["partialFractionValueForPartialPallet"][plt])
                        if run_ocean:
                            load_obj.cases = (inputParams["palletNumCasesDict"][plt])*(inputParams["partialFractionValueForPartialPallet"][plt])
                        else:
                            load_obj.cases = (inputParams["palletNumCasesDict"][plt])*(inputParams["partialFractionValueForPartialPallet"][plt])
                        load_obj.total_cases = (inputParams["palletNumCasesDict"][plt])         # added by HKD
                        load_obj.sto_num = ''
                        load_obj.weight = inputParams["palletWeightDict"][plt]
                        load_obj.cube = (40/12) * (48/12) * \
                            inputParams["palletHeightDict"][plt]
                        load_obj.height = inputParams["palletHeightDict"][plt]
                        load_obj.is_partial = True
                        load_obj.batch_date = inputParams["palletBatchDateDict"][plt]
                        # palletIDs_dict.update({load_obj.palletID: load_obj.palletID_II})
                        loading_instr_list.append(load_obj)

    loading_instr_list.sort(key=operator.attrgetter('sequence'))
    loading_instr_list.sort(key=operator.attrgetter('container'))
    # update loading sequence
    print("loading_instr_list: ", loading_instr_list)
    
    if len(loading_instr_list) > 0:
        loading_instr_list[0].sequence = 1
        for i in range(1, len(loading_instr_list)):
            if loading_instr_list[i].container != loading_instr_list[i - 1].container:
                loading_instr_list[i].sequence = 1
                continue
            loading_instr_list[i].sequence = loading_instr_list[i - 1].sequence + 1
    
        # # mapping of pallet IDs (based on bin location for pickslip sorting) to original pallet ID
        # inputParams["palletIDs_dict"] = palletIDs_dict
    
        # update material count for material SKU
        for load_obj in loading_instr_list:
            load_obj.material_count = material_count_dict[load_obj.material]

    return loading_instr_list, inputParams