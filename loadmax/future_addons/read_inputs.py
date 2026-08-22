"""Reading in data for truck  loadmax.

Data is read from
    * input datafile (generated as output from preprocessing code)
    * loadmax input template file

Additional pre-processing:
    * The additional pallet weight and height are added to each pallet (i.e. in case of special pallets like wooden pallets etc.)
    * the bounds on max balanced pallets (both full and partial) are calculated based on corresponding input data column value
    * Logic to estimate the number of containers
    * updating container height and container height door to account for loading allowance
    * Assign priorities to containers to prioritize loading of first N-1 containers
    * Priority pallets (column E in Data worksheet) - needs to be handled in pre-processing code

Missing:
    * Optimization - accounting for threshold vals


"""

import os
import pandas as pd
from UtilityClass import Pallet_Obj
# from UtilityClass import SpecialPallet_Obj, MarketBrandGroup_Obj
import xlrd
import math

# folderpath = "C:\\Users\\NUmang\\OneDrive - JNJ\\Desktop\\LoadMax\\Data files\\output_folder"
# intermediate_input_file = folderpath + "\\intermediate_input_file_truck.csv"
# market_id_input = 'UD30'

def ReadData(inputTemplate_Data, inputPreprocessed_Data, intermediate_input_file, market_id_input):
    x = os.path.isfile(inputTemplate_Data)
    print("Is valid file path:", x)

    inputParams = dict()

    # TODO - check a) crushing limit by SKU b) pallet footprint c) stack factor

    inputParams["marketID"] = market_id_input
    inputParams["containerList"] = []
    inputParams["effectiveContainerList"] = inputParams["containerList"]
    inputParams["layerList"] = []
    inputParams['container_priority'] = dict()  # DONE

    # TODO - account for special pallets ?
    # TODO - pre-assign containers to brands


    ############### Obtained from - Config worksheet (Template file) ##############
    # position lists
    inputParams["positionList"] = [] # DONE
    inputParams["frontPositionList"] = []  # DONE
    inputParams["rearPositionList"] = [] # DONE
    inputParams["leftPositionList"] = [] # DONE
    inputParams["rightPositionList"] = [] # DONE

    inputParams["sectionList"] = [] # DONE

    inputParams["positionGroupBalanceCoefficientDict"] = dict() # DONE
    inputParams["positionGroupDict"] = dict() # DONE
    inputParams["positionSequenceDict"] = dict() # DONE

    inputParams["door_posn"] = [] # DONE

    inputParams['containerHeight'] = 0  # DONE
    inputParams['containerHeightAtDoor'] = 0  # DONE
    inputParams['max_plt_n'] = 0  # DONE

    inputParams["allowContainerMaterial"] = []  # TODO - do i need it for truck ?
    # inputParams["crushingLimitByMaterial"] = []  # TODO - from where do i get it ?

    # first position in container layer (need for compact packing, esp for Trucks)
    inputParams['first_plt_n'] = 0 # DONE
    # max number of layers
    inputParams['max_lyr_n'] = 2 # TODO - from where do I update (if need to) ?
    if inputParams['max_lyr_n'] == 2:
        inputParams["layerList"] = [1, 2]
    else:
        inputParams["layerList"] = [1, 2, 3]

    ############### Obtained from - Limit worksheet (Template file) ##############
    # max Container Weight Limit
    inputParams['max_wt'] = [] # DONE
    # max Container Weight Limit - front and back
    inputParams['max_front_wt'] = [] # DONE
    inputParams['max_rear_wt'] = [] # DONE


    ############### Obtained from - Options worksheet (Template file) ##############
    inputParams['priorityIncentive'] = 1.1 # TODO - from where to get it  ?
    inputParams['sku_penalty'] = 1 # TODO - clarify (again!)


    # unique number of SKUs in all containers combined
    inputParams['unique_sku_n'] = [] # DONE
    # max allowed number of SKUs per container
    inputParams['max_sku_n'] = 0  # DONE # TODO - why is max SKU values 0 ? (0 implies ignore)

    inputParams['utilThreshold'] = 0.0 # DONE
    inputParams['casesOrPalletsTruck'] = "" # DONE
    inputParams['side_to_side_tolerance'] = 0.0 # DONE
    inputParams['front_to_back_tolerance'] = 0.0  # TODO - do we need it for truck  ?
    inputParams['sku_penalty_factor'] = 0.0 # DONE

    inputParams['load_allow_ht'] = 0.0 # DONE TODO - already included in heights in market_config?
    inputParams['addedPalletWeight'] = 0.0 # DONE
    inputParams['addedPalletHeight'] = 0.0 # DONE

    inputParams['fp_area_tolerance'] = 0.0 # DONE
    inputParams['enforce_fp_tolerance'] = False # DONE

    ############### Obtained from - MasterData_Supplement worksheet (Template file) ##############
    inputParams["fullPalletFootPrintArea"] = dict()
    inputParams["partialPalletFootPrintArea"] = dict()
    inputParams["crushingLimitByMaterial"] = dict()

    ############### Obtained from - Data worksheet (Template file) ##############
    inputParams["skuPriorityIncentive"] = dict()

    ############### Obtained from - Other file (output of preprocessing code) ##############

    inputParams["fullPalletList"] = [] # DONE
    inputParams["partialPalletList"] = [] # DONE
    inputParams["materialList"] = [] # DONE
    inputParams["fullPalletDemandByMaterialBatch"] = [] # DONE
    inputParams["partialPalletDemandByMaterialBatch"] = [] # DONE
    inputParams["partialFractionValueForPartialPallet"] = [] # DONE
    inputParams["palletHeightDict"] = [] # DONE
    inputParams["palletWeightDict"] = [] # DONE
    inputParams["palletMaterialDict"] = [] # DONE
    inputParams["palletDescriptionDict"] = [] # Done
    inputParams["palletBrandDict"] = []  # EMPTY
    inputParams["containerBrandDict"] = dict()  # EMPTY
    inputParams['brand_group'] = dict()  # EMPTY
    inputParams["palletBinLocDict"] = dict() # EMPTY (with artifical pallet entry as "")
    inputParams["palletBatchDict"] = dict()
    inputParams["palletBatchDateDict"] = dict()

    # number of balanced pallets
    inputParams['max_prior_full'] = dict()  # TODO - verify logic below  ?
    inputParams['max_prior_prtl'] = dict()  # TODO - verify logic below  ?
    # inputParams["priorityPalletList"] = [] # TODO - do not need it ?

    inputParams["stackFactorPallet"] = dict()

    inputParams = ReadInputTemplateFile(inputTemplate_Data, inputParams, market_id_input)
    inputParams = ReadPreProcessedDataFile(inputPreprocessed_Data, inputParams, market_id_input)
    est_num_containers = EstimateNumContainers(inputParams)
    for i in range(est_num_containers):
        inputParams["containerList"].append(float(1 + i))

    # logic to prioritize containers
    inputParams = AssignPrioritiesToContainers(inputParams)

    # initialize
    plt_priority_dict = dict()
    plt_priorityValue_dict = dict()
    for plt in inputParams["fullPalletList"]:
        plt_priority_dict.update({plt: 1})
        plt_priorityValue_dict.update({plt: 1})
    for plt in inputParams["partialPalletList"]:
        plt_priority_dict.update({plt: 1})
        plt_priorityValue_dict.update({plt: 1})
    inputParams["palletPriority_numAvailableContainers"] = plt_priority_dict
    inputParams["palletPriorityValue_numAvailableContainers"] = plt_priorityValue_dict

    pd.DataFrame(inputParams.items()).to_csv(intermediate_input_file)

    return inputParams

####################################### input template file #######################################
def ReadInputTemplateFile(inputTemplate_Data, inputParams, market_id_input):


    print("#########################################################################")
    print("Reading input template file.....")
    print("#########################################################################")

    xl = pd.ExcelFile(inputTemplate_Data)

    ##############################################################################
    df = xl.parse("Config")
    data_layout = df.values

    posn_grp_dict = dict()
    posn_grp_weight_dict = dict()
    posn_seq_dict = dict()

    for i in range(len(data_layout)):

        posn = df.iloc[i, df.columns.get_loc("Pos")]
        grp = df.iloc[i, df.columns.get_loc("Groups")]

        posn_grp_dict.update({posn: grp})
        posn_grp_weight_dict[(posn, grp)] = df.iloc[i, df.columns.get_loc("Wgt")]
        posn_seq_dict.update({posn: posn})

        if posn not in inputParams["positionList"]:
            inputParams["positionList"].append(posn)
        if grp == "A" or  grp == "C":
            inputParams["frontPositionList"].append(posn)
        if grp == "B" or grp == "D":
            inputParams["rearPositionList"].append(posn)
        if grp == "A" or  grp == "B":
            inputParams["leftPositionList"].append(posn)
        if grp == "C" or  grp == "D":
            inputParams["rightPositionList"].append(posn)

        if grp not in inputParams["sectionList"]:
            inputParams["sectionList"].append(grp)

    # posn_list_len = len(inputParams["door_posn"])
    door_posn_1 = inputParams["positionList"][- 1]
    door_posn_2 = inputParams["positionList"][- 2]
    inputParams["door_posn"].append(door_posn_1)
    inputParams["door_posn"].append(door_posn_2)

    # updating balance coefficients to zeros for keys not present
    for grp in inputParams["sectionList"]:
        for posn in inputParams["positionList"]:
            if (posn, grp) not in posn_grp_weight_dict:
                posn_grp_weight_dict[(posn, grp)] = 0.0
    inputParams["positionGroupBalanceCoefficientDict"] = posn_grp_weight_dict
    inputParams["positionGroupDict"] = posn_grp_dict
    inputParams["positionSequenceDict"] = posn_seq_dict

    print("###################################################")
    print("Printing layout data....")
    print("Printing posn-grp data....")
    for key in posn_grp_dict:
        print(key, posn_grp_dict[key])
    print("Printing posn-group-weight data....")
    for key in posn_grp_weight_dict:
        print(key, posn_grp_weight_dict[key])
    # print("Printing posn-sequence data....")

    print("Position list:", inputParams["positionList"])
    print("Front position list:", inputParams["frontPositionList"])
    print("Rear position list:", inputParams["rearPositionList"])
    print("Left position list:", inputParams["leftPositionList"])
    print("Right position list:", inputParams["rightPositionList"])
    print("Door position:", inputParams["door_posn"])
    print("Section list....")
    for grp in inputParams["sectionList"]:
        print(grp)

    inputParams['max_plt_n'] = data_layout[0][5]
    inputParams['first_plt_n'] = data_layout[1][5]
    inputParams['containerHeight'] = data_layout[0][6]
    inputParams['containerHeightAtDoor'] = data_layout[0][7]
    print("Maximum number of pallets per layer....:", inputParams['max_plt_n'])
    print("Container height....:", inputParams['containerHeight'])
    print("Container height at door....:", inputParams['containerHeightAtDoor'])


    ##############################################################################
    df = xl.parse("Limits")
    data_limits = df.values

    max_wt_dict = dict()
    max_front_wt_dict = dict()
    max_rear_wt_dict = dict()

    for i in range(len(data_limits)):
        mkt_id = data_limits[i][1]
        max_wt_dict.update({mkt_id:data_limits[i][4]})
        max_front_wt_dict.update({mkt_id: data_limits[i][5]})
        max_rear_wt_dict.update({mkt_id: data_limits[i][6]})

    inputParams['max_wt'] = max_wt_dict[market_id_input]
    inputParams['max_front_wt'] = max_front_wt_dict[market_id_input]
    inputParams['max_rear_wt'] = max_rear_wt_dict[market_id_input]

    print("###################################################")
    print("Printing data on Limits...")
    print("Printing max weight limits")
    for key in max_wt_dict:
        print(key, max_wt_dict[key])
    print("Printing max front weight limits")
    for key in max_front_wt_dict:
        print(key, max_front_wt_dict[key])
    print("Printing max rear weight limits")
    for key in max_rear_wt_dict:
        print(key, max_rear_wt_dict[key])
    print("max weight limit for specified market:", inputParams['max_wt'])
    print("max front weight limit for specified market:", inputParams['max_front_wt'])
    print("max rear weight limit for specified market:", inputParams['max_rear_wt'])

   ###################################################################################
    df = xl.parse("Options")
    data_options = df.values
    print("Printing data options...")
    # print(data_options)
    i = -1
    inputParams['utilThreshold'] = data_options[6+i][2]
    inputParams['casesOrPalletsTruck'] = data_options[10+i][2]
    # inputParams['front_to_back_tolerance'] = 0.1  # TODO - from where to get it  ?
    inputParams['side_to_side_tolerance'] = data_options[14+i][2]
    inputParams['sku_penalty_factor'] = data_options[16+i][2]
    inputParams['max_sku_n'] = data_options[17+i][2]

    # updating container height (at door and otherwise) accounting for loading allowance
    inputParams['load_allow_ht'] = data_options[20+i][2]
    inputParams['containerHeight'] = inputParams['containerHeight']  - inputParams['load_allow_ht']
    inputParams['containerHeightAtDoor'] = inputParams['containerHeightAtDoor'] - inputParams['load_allow_ht']
    print("Updated container height at door:", inputParams['containerHeight'])
    print("Updated container height:", inputParams['containerHeightAtDoor'])

    inputParams['addedPalletWeight'] = data_options[21 + i][2]
    inputParams['addedPalletHeight'] = data_options[22 + i][2]

    inputParams['enforce_fp_tolerance'] = data_options[25+i][2]
    inputParams['fp_area_tolerance'] = data_options[26 + i][2]
    inputParams['enforce_compact_packing'] = data_options[31 + i][2]

    print("###################################################")
    print("Utilization threshold:", inputParams['utilThreshold'])
    print("Cases or pallets?:", inputParams['casesOrPalletsTruck'])
    print("Side to side tolerance:", inputParams['side_to_side_tolerance'])

    print("Maximum SKUs:", inputParams['max_sku_n'])
    print("SKU penalty factor:", inputParams['sku_penalty_factor'])

    print("Loading allowance (height):", inputParams['load_allow_ht'])
    print("Added pallet weight:", inputParams['addedPalletWeight'])
    print("Added pallet height:", inputParams['addedPalletHeight'])

    print("Enforce footprint constraint?:", inputParams['enforce_fp_tolerance'])
    print("Footrpint area tolerance:", inputParams['fp_area_tolerance'])
    
    print("Enforce compact packing:", inputParams['enforce_compact_packing'])
    ###################################################################################
    df = xl.parse("Master_Supplement")
    data_mstr = df.values
    print("Printing master data supplement (footprint, crushing limits etc.)...")

    for i in range(len(data_mstr)):
        mat_sku = df.iloc[i, df.columns.get_loc("SKU")]
        fp_area = df.iloc[i, df.columns.get_loc("Length [in]")] * df.iloc[i, df.columns.get_loc("Width [in]")]
        inputParams["crushingLimitByMaterial"].update({mat_sku: df.iloc[i, df.columns.get_loc("Crushing Limit")]})
        inputParams["fullPalletFootPrintArea"].update({mat_sku: fp_area})
        inputParams["partialPalletFootPrintArea"].update({mat_sku: fp_area})

    for key in inputParams["crushingLimitByMaterial"]:
        print(key, inputParams["crushingLimitByMaterial"][key], inputParams["fullPalletFootPrintArea"][key], inputParams["partialPalletFootPrintArea"][key])


    ###################################################################################
    df = xl.parse("Data")
    data_data = df.values
    print("Printing Data tab - to get the priority SKUs")

    for i in range(len(data_data)):
        #if (str(df.iloc[i, df.columns.get_loc("Priority")]).title() == "Yes" and (str(df.iloc[i, df.columns.get_loc("Ship To")]) == str(market_id_input)):
        if (str(df.iloc[i, df.columns.get_loc("Priority")]) == "YES" or str(df.iloc[i, df.columns.get_loc("Priority")]) == "Yes" or str(df.iloc[i, df.columns.get_loc("Priority")]) == "yes") and (str(df.iloc[i, df.columns.get_loc("Ship To")]) == str(market_id_input)):
            mat_sku = df.iloc[i, df.columns.get_loc("SKU")]
            #mat_batch_sku = str(df.iloc[i, df.columns.get_loc("SKU")]) + "_" + str(df.iloc[i, df.columns.get_loc("Order Number")])
            inputParams["skuPriorityIncentive"].update({mat_sku: df.iloc[i, df.columns.get_loc("Priority")]})

    for key in inputParams["skuPriorityIncentive"]:
        print(key, inputParams["skuPriorityIncentive"][key])
    return inputParams

####################################### preprocessed input data file #######################################
def ReadPreProcessedDataFile(inputPreprocessed_Data, inputParams, market_id_input):

    print("#########################################################################")
    print("Reading pre-processed file.....")
    print("#########################################################################")

    palletList = []
    fullPalletList = []
    partialPalletList = []
    priorityPalletList = []
    materialList = []
    fullPalletDemandByMaterialBatch = dict()
    partialPalletDemandByMaterialBatch = dict()
    partialFractionValueForPartialPallet = dict()

    # pallet heights
    palletHeightDict = dict()

    # pallet weights
    palletWeightDict = dict()

    # pallet material dictionary
    palletMaterialDict = dict()

    # pallet description dictionary
    palletDescriptionDict = dict()

    # pallet foot print area
    fullPalletFootprintAreaDict = dict()
    partialPalletFootprintAreaDict = dict()

    # stack factor for pallets
    stackFactorPalletDict = dict()

    # pallet batch
    palletBatchDict = dict()

    # pallet batch date dict
    palletBatchDateDict = dict()

    # pallet number of cases
    palletNumCasesDict = dict()

    # pallet bin location
    palletBinLocDict = dict()

    # input data file
    # xl = pd.ExcelFile(inputPreprocessed_Data)
    # df = xl.parse("Data")
    # data = df.values
    # # print("Number of rows in data file:", range(len(data)))

    df = pd.read_csv(inputPreprocessed_Data)
    data = df.values

    df['Batch'] = df['Batch'].astype(str)

    for i in range(len(data)):
        # if i > 25:
        #     break
        plt_mtl_btch = Pallet_Obj()
        plt_mtl_btch.customer = df.iloc[i, df.columns.get_loc("Customer")]
        if plt_mtl_btch.customer != market_id_input:
            continue
        plt_mtl_btch.palletID = "plt" + "_" + str(i)
        plt_mtl_btch.material = df.iloc[i, df.columns.get_loc("Material")]
        plt_mtl_btch.description = df.iloc[i, df.columns.get_loc("Description")]
        plt_mtl_btch.batch = (df.iloc[i, df.columns.get_loc("Batch")])
        plt_mtl_btch.full_plt_dmd = float(df.iloc[i, df.columns.get_loc("Pallet")])
        plt_mtl_btch.prtl_plt_dmd = float(df.iloc[i, df.columns.get_loc("Partial Pallets")])
        plt_mtl_btch.prior_plt_dmd = float(df.iloc[i, df.columns.get_loc("Balance Pallets")])
        plt_mtl_btch.prt_plt_frac = float(df.iloc[i, df.columns.get_loc("Partial Pallet Fraction")])
        plt_mtl_btch.plt_wt = float(df.iloc[i, df.columns.get_loc("Pallet Weight")]) #+ inputParams['addedPalletWeight']
        plt_mtl_btch.plt_ht = float(df.iloc[i, df.columns.get_loc("Pallet Height")]) #+ (inputParams['addedPalletHeight'] / 12)
        plt_mtl_btch.prtl_plt_wt = float(df.iloc[i, df.columns.get_loc("Partial Pallet Weight")]) #+ inputParams['addedPalletWeight']
        plt_mtl_btch.prtl_plt_ht = float(df.iloc[i, df.columns.get_loc("Partial Pallet Height")]) #+ (inputParams['addedPalletHeight'] / 12)
        # plt_mtl_btch.plt_length = float(df.iloc[i, df.columns.get_loc("Pallet length")]) # MISSING
        # plt_mtl_btch.plt_width = float(df.iloc[i, df.columns.get_loc("Pallet width")]) # MISSING
        plt_mtl_btch.brand = df.iloc[i, df.columns.get_loc("Brand")] # MISSING
        plt_mtl_btch.stk_factor = df.iloc[i, df.columns.get_loc("stk_factor")]
        plt_mtl_btch.cases_per_plt = df.iloc[i, df.columns.get_loc("cses/plt")]
        plt_mtl_btch.batch_date = ""

        if plt_mtl_btch.prtl_plt_dmd > 0:
            plt_mtl_btch.is_prtl_plt = True
            plt_mtl_btch.plt_wt = plt_mtl_btch.prtl_plt_wt
            plt_mtl_btch.plt_ht = plt_mtl_btch.prtl_plt_ht
        if plt_mtl_btch.prior_plt_dmd > 0:
            if plt_mtl_btch.is_prtl_plt:
                inputParams['max_prior_prtl'].update({plt_mtl_btch.palletID: plt_mtl_btch.prior_plt_dmd})
            else:
                inputParams['max_prior_full'].update({plt_mtl_btch.palletID: plt_mtl_btch.prior_plt_dmd})

        palletList.append(plt_mtl_btch)

        palletHeightDict.update({plt_mtl_btch.palletID: plt_mtl_btch.plt_ht})

        palletWeightDict.update({plt_mtl_btch.palletID: plt_mtl_btch.plt_wt})

        stackFactorPalletDict.update({plt_mtl_btch.palletID: plt_mtl_btch.stk_factor})

        palletBatchDict.update({plt_mtl_btch.palletID: plt_mtl_btch.batch})

        palletNumCasesDict.update({plt_mtl_btch.palletID: plt_mtl_btch.cases_per_plt})

        palletBinLocDict.update({plt_mtl_btch.palletID: ""})

        palletBatchDateDict.update({plt_mtl_btch.palletID: plt_mtl_btch.batch_date})

        if plt_mtl_btch.material not in materialList:
            materialList.append(plt_mtl_btch.material)
        # print("Length of pallet list:", len(palletList))

        if plt_mtl_btch.is_prtl_plt:
            partialPalletDemandByMaterialBatch.update({plt_mtl_btch.palletID: plt_mtl_btch.prtl_plt_dmd})
            partialFractionValueForPartialPallet.update({plt_mtl_btch.palletID: plt_mtl_btch.prt_plt_frac})
            partialPalletList.append(plt_mtl_btch.palletID)
        else:
            fullPalletDemandByMaterialBatch.update({plt_mtl_btch.palletID: plt_mtl_btch.full_plt_dmd})
            fullPalletList.append(plt_mtl_btch.palletID)

        palletMaterialDict.update({plt_mtl_btch.palletID: plt_mtl_btch.material})
        palletDescriptionDict.update({plt_mtl_btch.palletID: plt_mtl_btch.description})
    print("Printing all records...")
    for plt in palletList:
        print(plt.palletID, plt.customer, plt.material, plt.batch, plt.full_plt_dmd, plt.prtl_plt_dmd,
              plt.prior_plt_dmd,
              plt.prt_plt_frac, plt.plt_wt, plt.plt_ht, plt.prtl_plt_wt, plt.prtl_plt_ht, plt.plt_length, plt.plt_width,
              plt.brand, plt.is_prtl_plt, plt.stk_factor)

    print("Printing list of full pallets...")
    for plt in fullPalletList:
        print(plt)

    print("Printing list of partial pallets...")
    for plt in partialPalletList:
        print(plt)

    print("Printing list of full pallet demand values...")
    for plt in fullPalletDemandByMaterialBatch:
        print(plt, fullPalletDemandByMaterialBatch[plt])

    print("Printing list of partial pallet demand values...")
    for plt in partialPalletDemandByMaterialBatch:
        print(plt, partialPalletDemandByMaterialBatch[plt])

    print("Printing list of partial pallet fraction values...")
    for plt in partialFractionValueForPartialPallet:
        print(plt, partialFractionValueForPartialPallet[plt])

    print("Printing list of materials...")
    for plt in materialList:
        print(plt)

    print("Printing dictionary of pallet materials...")
    for plt in palletMaterialDict:
        print(plt, "_", palletMaterialDict[plt])

    # done by HKD

    print("Printing description of pallet materials....")
    for plt in palletDescriptionDict:
        print(plt, "_", palletDescriptionDict[plt])

    print("Printing partial balanced/priority pallets...")
    for plt in fullPalletList:
        if plt not in inputParams['max_prior_full']:
            inputParams['max_prior_full'][plt] = 0.0
    for plt in partialPalletList:
        if plt not in inputParams['max_prior_prtl']:
            inputParams['max_prior_prtl'][plt] = 0.0

    for key in inputParams['max_prior_prtl']:
        print(key, inputParams['max_prior_prtl'][key])
    print("Printing full balanced/priority pallets...")
    for key in inputParams['max_prior_full']:
        print(key, inputParams['max_prior_full'][key])

    # populated
    inputParams["fullPalletList"] = fullPalletList
    inputParams["partialPalletList"] = partialPalletList
    inputParams["materialList"] = materialList
    inputParams["fullPalletDemandByMaterialBatch"] = fullPalletDemandByMaterialBatch
    inputParams["partialPalletDemandByMaterialBatch"] = partialPalletDemandByMaterialBatch
    inputParams["partialFractionValueForPartialPallet"] = partialFractionValueForPartialPallet
    inputParams["palletHeightDict"] = palletHeightDict
    inputParams["palletWeightDict"] = palletWeightDict
    inputParams["palletMaterialDict"] = palletMaterialDict
    inputParams["palletDescriptionDict"] = palletDescriptionDict
    inputParams["stackFactorPallet"] = stackFactorPalletDict
    inputParams["palletBatchDict"] = palletBatchDict
    inputParams["palletBinLocDict"] = palletBinLocDict
    inputParams["palletNumCasesDict"] = palletNumCasesDict
    inputParams["palletBatchDateDict"] = palletBatchDateDict

    inputParams['unique_sku_n'] = len(inputParams["materialList"])
    print("######## Truck-InputParams ########")
    print(palletDescriptionDict)
    print(inputParams)
    print("Total number of unique SKUs:", inputParams['unique_sku_n'])

    return inputParams

# logic to estimate the number of containers
def EstimateNumContainers(inputParams):

    num_est_containers = 0

    num_full_plts = 0
    num_prtl_plts = 0
    for plt in inputParams["fullPalletDemandByMaterialBatch"]:
        num_full_plts += inputParams["fullPalletDemandByMaterialBatch"][plt]
    for plt in inputParams["partialPalletDemandByMaterialBatch"]:
        num_prtl_plts += inputParams["partialPalletDemandByMaterialBatch"][plt]

    sum_prtl_plts = 0
    plts_per_layer = inputParams['max_plt_n']

    # # old logic
    # for plt in inputParams["partialFractionValueForPartialPallet"]:
    #     sum_prtl_plts += inputParams["partialFractionValueForPartialPallet"][plt]

    # new logic
    for plt in inputParams["partialFractionValueForPartialPallet"]:
        if inputParams["partialFractionValueForPartialPallet"][plt] >= 0.25:
            sum_prtl_plts += 1
        else:
            sum_prtl_plts += inputParams["partialFractionValueForPartialPallet"][plt]

    print("Number of full pallets:", num_full_plts)
    print("Number of partial pallets:", num_prtl_plts)
    print("Sum of partial pallets:", sum_prtl_plts)
    print("Number of pallets per layer:", plts_per_layer)

    num_est_containers = math.ceil((num_full_plts + sum_prtl_plts)/(2*plts_per_layer))

    print("Estimated number of containers (based on pallet count):", num_est_containers)

    # update based on pallet weights if required
    total_plt_weight = 0.0
    for plt in inputParams["fullPalletDemandByMaterialBatch"]:
        total_plt_weight += inputParams["fullPalletDemandByMaterialBatch"][plt] * inputParams["palletWeightDict"][plt]
    for plt in inputParams["partialPalletDemandByMaterialBatch"]:
        total_plt_weight += inputParams["partialPalletDemandByMaterialBatch"][plt] * inputParams["palletWeightDict"][plt]

    est_containers_wtLimit =  math.ceil(total_plt_weight/inputParams['max_wt'])

    if est_containers_wtLimit > num_est_containers:
        num_est_containers = est_containers_wtLimit
        print("Estimated number of containers (updated based on weight limit):", num_est_containers)

    return num_est_containers


# logic to prioritize containers
def AssignPrioritiesToContainers(inputParams):

    est_num_containers = len(inputParams["containerList"])
    for i in range(est_num_containers):
        inputParams['container_priority'].update({inputParams["containerList"][i]: 1})
    if est_num_containers >= 2:
         inputParams['container_priority'].update({inputParams["containerList"][est_num_containers - 1]: 0.9})
    # if est_num_containers >= 3:
    #      inputParams['container_priority'].update({inputParams["containerList"][est_num_containers - 2]: 0.9})

    print("Container priorities:", inputParams['container_priority'])
    return inputParams