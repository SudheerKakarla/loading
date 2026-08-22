import os
import pandas as pd
import sys
import math
from datetime import datetime, timedelta
from operator import itemgetter, attrgetter
from prettytable import PrettyTable
import openpyxl
from openpyxl.styles import PatternFill
from openpyxl import load_workbook
from openpyxl.utils.dataframe import dataframe_to_rows
#from pandas import ExcelWriter
#import xlrd
from UtilityClass_Loadbreak import Pallet_Obj, Result_Obj, Plant_Obj
import xlsxwriter
import heapq
import shutil

# folder_path = "C:\\Users\\NUmang\\OneDrive - JNJ\\Desktop\\LoadMax\\LoadBreak"
# output_text_file = folder_path + "\\LoadBreak Output.txt"
output_text_file = "LoadBreak Output.txt"
# output_csv_file = "LoadBreak Output.csv"
# output_excel_file = "LoadBreak Output.xlsx"
# output_template_file = "loadbreak_output_template.xlsx"
output_log = []
triple_stacks_list = []


# Inputs:
#   startDate
#   endDate
#   targetRatio:            default = 2
#   breakPartialPallets:    default = false
#   maxBreakHgt:            default = 0.6 [m]
#   maxBreakWgtFract:       default = 0.5

def ReadData(app, COOIS_File, outputTemplate_File):#, modelParams_File):
    x = os.path.isfile(COOIS_File)
    print("Is valid file path:", x)

    dtString = ''
    debugPrint = False

    output_dataChecks = []

    ## startDate and endDate: implied to be start of the day on given dates

    # # read model parameters worksheet
    # xl = pd.ExcelFile(modelParams_File)
    # df = xl.parse("ModelParams")
    # data = df.values

    # startDate = data[0][1]
    # endDate = data[1][1]
    # endDateF = endDate + timedelta(days=1)
    # # targetFract = 0.0 # set later in the code
    # targetRatio = data[2][1]
    # breakPartialPallets = data[3][1]
    # maxBreakHgt = data[4][1]
    # # maxBreakWgtFract = data[5][1]
    # considerPartialPalletsInOriginalList = data[5][1]
    # print(startDate, " ", endDate, " ", endDateF, " ", targetRatio, " ", breakPartialPallets, " ",
    #       maxBreakHgt, " ", considerPartialPalletsInOriginalList)

    startDate = datetime.strptime(app.startDate, "%Y-%m-%d")
    endDate = datetime.strptime(app.endDate, "%Y-%m-%d")
    endDateF = endDate + timedelta(days=1)
    targetRatio = app.targetRatio
    breakPartialPallets = app.breakPartialPallets
    maxBreakHgt = app.maxBreakHgt
    considerPartialPalletsInOriginalList = app.considerPartialPalletsInOriginalList
    print(startDate, " ", endDate, " ", endDateF, " ", targetRatio, " ", breakPartialPallets, " ",
          maxBreakHgt, " ", considerPartialPalletsInOriginalList)

    # Dynamic COOIS data file - COOIS worksheet
    # Read from COOIS sheet into dataframe df
    xl = pd.ExcelFile(COOIS_File)
    df = xl.parse("Worksheet")

    #sys.stdout.open()
    #stdout = sys.stdout
    print('*** Starting LoadBreak...')

    # sys.stdout = open(output_text_file, 'w')
    #LBout = open('LoadBreak Output.txt', 'w')

    #LBout.write('Input Data from COOIS file: ', COOIS_File)
    str_val = 'Input Data from COOIS file: ' + str(COOIS_File)
    output_log.append(str_val)

    if ( debugPrint ):
        print(df)
    print('')
    data = df.values

    # Lists:
    #   palletCompleteList  = all pallets (from COOIS)
    #   palletList          = subset within date range
    #   palletEligibleList  = full pallets which are eligible to break
    #   palletBreakList     = full pallets to break
    #   plantList           = plant codes (expecting 230A and 330A)
    #   resultList          = LoadBreak results

    palletCompleteList = []
    palletList = []
    palletEligibleList = []
    palletBreakList = []
    plantList = []
    resultList = []

    # Dictionaries:
    palletMaterialDict = dict()
    palletMatDescrDict = dict()
    palletPlantDict    = dict()
    palletWeightDict   = dict()
    palletHeightDict   = dict()
    palletLayersDict   = dict()
    palletPartialDict  = dict()

    targetFract = dict()
    countFull  = dict()
    countFullIneligible = dict()
    countPart  = dict()
    avgWgtFull = dict()
    avgHgtFull = dict()
    maxWgtFull = dict()
    breakHgt   = dict()
    breakWgt   = dict()
    numToBreak = dict()
    avgWgtBreak = dict()
    avgHgtBreak = dict()
    maxWgtBreak = dict()
    maxHgtBreak = dict()
    resultDict  = dict()
    fullPltWgtThreshold = dict()


    # For all data rows from COOIS....
    #prodPallet = Pallet_Obj()

    for i in range(len(data)):
        prodPallet = Pallet_Obj()
        prodPallet.recordID = str(df.iloc[i, df.columns.get_loc("Record Id")])
        prodPallet.pallet_num = df.iloc[i, df.columns.get_loc("No. Pallet")]
        prodPallet.palletID = prodPallet.recordID + "_" + str(prodPallet.pallet_num)
        prodPallet.order_num = df.iloc[i, df.columns.get_loc("Order No.")]
        prodPallet.material = str(df.iloc[i, df.columns.get_loc("Material")])
        prodPallet.material_descr = str(df.iloc[i, df.columns.get_loc("Material Description")])
        prodPallet.total_pallets = df.iloc[i, df.columns.get_loc("Total Pallets")]
        prodPallet.plant = str(df.iloc[i, df.columns.get_loc("Plant")])
        prodPallet.brand = str(df.iloc[i, df.columns.get_loc("Brand")])
        prodPallet.is_prtl_plt = bool(df.iloc[i, df.columns.get_loc("Partial Pallet")])
        prodPallet.pallet_wt = df.iloc[i, df.columns.get_loc("Pallet Weight (KG)")]
        prodPallet.pallet_ht = df.iloc[i, df.columns.get_loc("Pallet Height (mm)")]
        prodPallet.plt_layers = df.iloc[i, df.columns.get_loc("Layers per Pallet")]

        #prodPallet.date_time = datetime(df.iloc[i, df.columns.get_loc("Output Time")])
        #prodPallet.date_time = df.iloc[i, df.columns.get_loc("Output Time")]

        # read Output Time as a string and then convert to date time as needed
        dtString = df.iloc[i, df.columns.get_loc("Output Time")]
        prodPallet.date_time = datetime.strptime(dtString, '%Y-%m-%d %H:%M:%S')

        #print(prodPallet.palletID)
        #print(prodPallet.is_prtl_plt)
        #print(prodPallet.date_time)
        #prodPallet.date_time = prodPallet.date_time + datetime.timedelta(days=10)
        #print(prodPallet.date_time.ctime())

        # List of all pallets: palletCompleteList
        palletCompleteList.append(prodPallet)

        # Filter data within specified date range
        # List of pallets within date range: palletList
        if ( ( prodPallet.date_time >= startDate ) and ( prodPallet.date_time < endDateF ) ):
            palletList.append(prodPallet)
            if ( prodPallet.plant not in plantList):
                plantList.append(prodPallet.plant)

        # flag data issues
        # check_str = "Data issues"
        # output_dataChecks.append(check_str)
        if not prodPallet.is_prtl_plt:
            if prodPallet.pallet_wt > 800 or prodPallet.pallet_wt < 50:
                check_str = "Pallet record ID " + prodPallet.recordID + " weight is outside normal range (50-800)"
                if check_str not in output_dataChecks:
                    output_dataChecks.append(check_str)
            if (0.001 * prodPallet.pallet_ht) > 1.3 or (0.001 * prodPallet.pallet_ht) < 0.7:
                check_str = "Pallet record ID " + prodPallet.recordID + " height is outside normal range (0.7-1.3)"
                if check_str not in output_dataChecks:
                    output_dataChecks.append(check_str)

    if ( debugPrint ):
        print(plantList)
        print('')
    # Populate dictionaries based on palletList:
    #  palletMaterialDict
    #  palletMatDescrDict
    #  palletPlantDict
    #  palletWeightDict
    #  palletHeightDict
    #  palletLayersDict
    #  palletPartialDict

    palletCountPartial = 0
    for i in range(len(palletList)):
        palletMaterialDict.update({(palletList[i].plant, palletList[i].palletID): palletList[i].material})
        palletMatDescrDict.update({(palletList[i].plant, palletList[i].palletID): palletList[i].material_descr})
        palletWeightDict.update({(palletList[i].plant, palletList[i].palletID): palletList[i].pallet_wt})
        palletHeightDict.update({(palletList[i].plant, palletList[i].palletID): palletList[i].pallet_ht})
        palletLayersDict.update({(palletList[i].plant, palletList[i].palletID): palletList[i].plt_layers})
        palletPartialDict.update({(palletList[i].plant, palletList[i].palletID): palletList[i].is_prtl_plt})

        #palletMaterialDict.update({palletList[i].palletID: palletList[i].material})
        #palletMatDescrDict.update({palletList[i].palletID: palletList[i].material_descr})
        #palletPlantDict.update({palletList[i].palletID: palletList[i].plant})
        #palletWeightDict.update({palletList[i].palletID: palletList[i].pallet_wt})
        #palletHeightDict.update({palletList[i].palletID: palletList[i].pallet_ht})
        #palletLayersDict.update({palletList[i].palletID: palletList[i].plt_layers})
        #palletPartialDict.update({palletList[i].palletID: palletList[i].is_prtl_plt})

        if ( palletList[i].is_prtl_plt ):
            palletCountPartial += 1

    #print(palletMaterialDict)
    #print(palletLayersDict)
    #print(palletPartialDict)

    palletCountAll  = len(data)
    palletCount     = len(palletList)
    palletCountFull = palletCount - palletCountPartial

    #sys.stdout = open('LoadBreak Output.txt', 'w')

    #print('COOIS Data within the Specified Date Range:')
    str_val = '  Total Number of Pallets:' + str(palletCountAll)
    output_log.append(str_val)
    str_val = '  Start Date:' + str(startDate.ctime())
    output_log.append(str_val)
    str_val = '  End Date:' + str(endDateF.ctime())
    output_log.append(str_val)
    str_val = '  Number of Pallets (for selected date range):     ' + str(palletCount)
    output_log.append(str_val)
    str_val = '  Number of Full Pallets:' + str(palletCountFull)
    output_log.append(str_val)
    str_val = '  Number of Part Pallets:' + str(palletCountPartial)
    output_log.append(str_val)

    print('  Total Number of Pallets:', palletCountAll)
    print('  Start Date:', startDate.ctime())
    print('  End Date:', endDateF.ctime())
    print('  Number of Pallets (for selected date range):     ', palletCount)
    print('  Number of Full Pallets:', palletCountFull)
    print('  Number of Part Pallets:', palletCountPartial)
    print('')

    # print('Analysis by Market ------------------------------------------------------------------------------------------------------')
    # print('')

    ########## Determine eligibility (weights and heights ##########:

    palletList.sort(key=attrgetter('pallet_wt'))

    for p in range(len(plantList)):
        if ( debugPrint ):
            print('  Plant:', p, plantList[p])
        plant = Plant_Obj()
        plant.plantID = p
        countFull[p] = 0
        countPart[p] = 0
        countFullIneligible[p] = 0
        avgWgtFull[p] = 0
        maxWgtFull[p] = 0
        avgHgtFull[p] = 0
        numToBreak[p] = 0


        activeFullPalletWeightListForEligibility = []
        activeFullPalletWeightListForEligibility_indices = []
        for i in range(len(palletList)):
            if ( palletList[i].plant == plantList[p] ):
                if not palletList[i].is_prtl_plt:
                    activeFullPalletWeightListForEligibility.append(palletList[i].pallet_wt)
                    activeFullPalletWeightListForEligibility_indices.append(i)
        str_val = "Original list of weights of full pallets for market " + str(plantList[p]) + " is " + str(activeFullPalletWeightListForEligibility)
        output_log.append(str_val)
        str_val = "Original list of weight indices of full pallets for market " + str(plantList[p]) + " is " + str(activeFullPalletWeightListForEligibility_indices)
        output_log.append(str_val)

        # Update pallet counts for each plant
        # breakHgt: denotes the max height of partial pallets when a full pallet is broken, should not exceed Max Hgt limit

        for i in range(len(palletList)):
            if ( palletList[i].plant == plantList[p] ):
                if ( palletList[i].is_prtl_plt ):
                    countPart[p] += 1
                else:
                    countFull[p] += 1
                    avgWgtFull[p] += palletList[i].pallet_wt
                    avgHgtFull[p] += palletList[i].pallet_ht
                    maxWgtFull[p] = max(maxWgtFull[p], palletList[i].pallet_wt)
                    breakHgt[p,palletList[i].material] = 0.001 * ( palletList[i].pallet_ht / palletList[i].plt_layers) * math.ceil( palletList[i].plt_layers / 2)
                    breakHgt[p,palletList[i].material] = round(breakHgt[p,palletList[i].material],3)
                    breakWgt[p,palletList[i].material] = ( palletList[i].pallet_wt / palletList[i].plt_layers) * math.ceil( palletList[i].plt_layers / 2)

                    # Check to see if breakHgt is greater than max allowable hgt
                    if ( breakHgt[p,palletList[i].material] > maxBreakHgt ):
                        palletList[i].is_eligible_to_break = False

                        print('...debug: pallet too tall to break, material = ', palletList[i].material, ' hgt = ', breakHgt[p,palletList[i].material], ' plant = ', palletList[i].plant )

        # # LoadBreak Calculations: calculate fraction of pallets to break to meet target ratio
        # if considerPartialPalletsInOriginalList:
        #     targetFract[p] = (1.0 - (targetRatio * countPart[p] / countFull[p])) / (2 * targetRatio + 1)
        # else:
        #     targetFract[p] = 1.0 / (2 * targetRatio + 1)


        # # identify the lightest pallets based on target ratio and eligibility for height
        # numToBreak[p] = math.floor(countFull[p] * targetFract[p])
        # pallets_break_weights = heapq.nsmallest(numToBreak[p], activeFullPalletWeightListForEligibility)
        # wt_threshold_middleLayer_plt = max(pallets_break_weights)

        # # calculate lightest pallet greater than 50% of maximum full pallet weight that should be used to calculate threshold weight value
        # fullPltWgtThreshold[p] = maxWgtFull[p]
        # for i in range(len(palletList)):
        #     if ( palletList[i].plant == plantList[p] ):
        #         if not palletList[i].is_prtl_plt:
        #             if palletList[i].pallet_wt >= maxBreakWgtFract * maxWgtFull[p] and palletList[i].pallet_wt <= fullPltWgtThreshold[p]:
        #                 fullPltWgtThreshold[p] = palletList[i].pallet_wt

        # WEIGHT - Identify full pallets that can be used in bottom and middle layers for triple stacking
        for i in range(len(palletList)):
            if ( palletList[i].plant == plantList[p] ):
                if ( not palletList[i].is_prtl_plt ):

                    if i in activeFullPalletWeightListForEligibility_indices: # pallet has not been considered thus far in triple stacking

                        # if ( breakWgt[p,palletList[i].material] > maxWgtFull[p] * maxBreakWgtFract ):

                        index_plt_active_list = activeFullPalletWeightListForEligibility_indices.index(i)

                        # if palletList[i].pallet_wt==71.4:
                        #     print("HELLO:", palletList[i].pallet_wt)
                        #     print("HELLO2:", i)

                        max_weight_vals, indices_list, layer2_weight_vals, weight_found = \
                            GetWeightVals(activeFullPalletWeightListForEligibility, palletList[i].pallet_wt, index_plt_active_list,
                                          breakWgt[p, palletList[i].material])#, wt_threshold_middleLayer_plt)

                        if not weight_found:
                            palletList[i].is_eligible_to_break = False
                            # if palletList[i].pallet_wt == 71.4:
                            #     print("HELLO3:", palletList[i].pallet_wt)
                            #     print("HELLO4:", i)
                        else:
                            if palletList[i].is_eligible_to_break:  # eligible to be broken both by height and weight
                                output_log.append("----")
                                str_val = "max weight (bottom layer) vals:" + str(max_weight_vals)
                                output_log.append(str_val)
                                str_val = "middle layer weight vals:" + str(layer2_weight_vals)
                                output_log.append(str_val)
                                str_val = "full pallet weight (eligible to be broken):" + str(palletList[i].pallet_wt)
                                output_log.append(str_val)
                                str_val = "break partial weight val:" + str(breakWgt[p, palletList[i].material])
                                output_log.append(str_val)
                                str_val = "Initial list of weights of full pallets for market ", str(
                                    plantList[p]) + " is " + str(activeFullPalletWeightListForEligibility)
                                output_log.append(str_val)

                                # pallet under consideration
                                del activeFullPalletWeightListForEligibility[index_plt_active_list]  # update weight list
                                del activeFullPalletWeightListForEligibility_indices[index_plt_active_list]  # update weight index list

                                # bottom and middle layer pallets
                                for ctr in range(4):
                                    max_index = max(indices_list) # get max index value from list

                                    # anything used in triple stacking is not eligible to be brokem
                                    index_plt_list = activeFullPalletWeightListForEligibility_indices[max_index]
                                    palletList[index_plt_list].is_eligible_to_break = False # includes pallet identified to be broken, corrected outside the loop

                                    del activeFullPalletWeightListForEligibility[max_index]  # update weight list
                                    del activeFullPalletWeightListForEligibility_indices[max_index]  # update weight list

                                    indices_list.remove(max_index)


                                # # remove all weights used in triple stack from active weight list
                                # activeFullPalletWeightListForEligibility.remove(max_weight_vals[0])
                                # activeFullPalletWeightListForEligibility.remove(max_weight_vals[1])
                                # activeFullPalletWeightListForEligibility.remove(layer2_weight_vals[0])
                                # activeFullPalletWeightListForEligibility.remove(layer2_weight_vals[1])
                                # activeFullPalletWeightListForEligibility.remove(palletList[i].pallet_wt)
                                # print("Updated list of weights of full pallets for market ", plantList[p], " is ",
                                #       activeFullPalletWeightListForEligibility)
                                #
                                # # remove all indices used in triple stack from active index list
                                # activeFullPalletWeightListForEligibility_indices.remove(index1)

                                trpl_stck_1 = []
                                trpl_stck_2 = []
                                break_wtA = round(
                                    (palletList[i].pallet_wt / palletList[i].plt_layers) * math.ceil(palletList[i].plt_layers / 2), 1)
                                break_wtB = round(
                                    (palletList[i].pallet_wt / palletList[i].plt_layers) * math.floor(palletList[i].plt_layers / 2), 1)
                                break_htA = round(
                                    0.001 * (palletList[i].pallet_ht / palletList[i].plt_layers) * math.ceil(palletList[i].plt_layers / 2), 3)
                                break_htB = round(
                                    0.001 * (palletList[i].pallet_ht / palletList[i].plt_layers) * math.floor(palletList[i].plt_layers / 2), 3)

                                trpl_stck_1.append(plantList[p])
                                trpl_stck_1.append(palletList[i].material)
                                trpl_stck_1.append(max_weight_vals[0])
                                trpl_stck_1.append(layer2_weight_vals[0])
                                trpl_stck_1.append(break_wtA)
                                trpl_stck_1.append(palletList[i].pallet_wt)
                                trpl_stck_1.append(break_htA)

                                trpl_stck_2.append(plantList[p])
                                trpl_stck_2.append(palletList[i].material)
                                trpl_stck_2.append(max_weight_vals[1])
                                trpl_stck_2.append(layer2_weight_vals[1])
                                trpl_stck_2.append(break_wtB)
                                trpl_stck_2.append(palletList[i].pallet_wt)
                                trpl_stck_2.append(break_htB)

                                triple_stacks_list.append(trpl_stck_1)
                                triple_stacks_list.append(trpl_stck_2)

                                str_val = "Updated list of weights of full pallets for market " + str(
                                    plantList[p]) + " is " + str(activeFullPalletWeightListForEligibility)
                                output_log.append(str_val)
                                output_log.append("----")

                    # if (breakWgt[p, palletList[i].material] > maxWgtFull[p] - fullPltWgtThreshold[p]):
                    #     palletList[i].is_eligible_to_break = False
                    #     #print('...debug: pallet too heavy to break, material = ', palletList[i].material, ' wgt = ', breakWgt[p,palletList[i].material] )

        # Based on height and weight considerations, update list of eligible pallets to break (palletEligibleList) and count for reporting
        for i in range(len(palletList)):
            if ( palletList[i].plant == plantList[p] ):
                if ( not palletList[i].is_prtl_plt ):
                    if ( palletList[i].is_eligible_to_break ):
                        palletEligibleList.append(palletList[i])
                    else:
                        countFullIneligible[p] += 1


        # Calc average weight and height for full pallets
        if ( countFull[p] > 0 ):
            avgWgtFull[p] = round(avgWgtFull[p] / countFull[p],1)
            avgHgtFull[p] = round(0.001 * avgHgtFull[p] / countFull[p],3)

        #countFullIneligible[p] = 0

        # Summary output:
        print('  Market: ',plantList[p])
        print('    Count Full:                ', countFull[p])
        print('    Count Full Ineligible:     ', countFullIneligible[p])
        print('    Count Part:                ', countPart[p])
        print('    Full, Avg Wgt [kg]:        ', avgWgtFull[p])
        print('    Full, Max Wgt [kg]:        ', maxWgtFull[p])
        # print('    Full, Plt Wgt To Calc Threhold [kg]:        ', fullPltWgtThreshold[p])
        print('    Full, Avg Hgt [m]:         ', avgHgtFull[p])
        print('')
        if ( debugPrint ):
            print('breakHgt = ', breakHgt)
            print('breakWgt = ', breakWgt)

    # LoadBreak Calculations: calculate fraction of pallets to break to meet target ratio
    for p in range(len(plantList)):
        if countFull[p] == 0:
            print("Full pallet count for market ", plantList[p], " is zero")
            targetFract[p] = 0
        else:
            if considerPartialPalletsInOriginalList:
                targetFract[p] = (1.0 - (targetRatio * countPart[p] / countFull[p])) / (2 * targetRatio + 1)
            else:
                targetFract[p] = 1.0 / ( 2 * targetRatio + 1 )

            targetFract[p] = max(targetFract[p], 0)

    print('LoadBreak Options -------------------------------------------------------------------------------------------------------')
    print('')
    #print('  LoadBreak Options:')
    print('  Break Partial Pallets (input):  ', breakPartialPallets)
    print('  Target Ratio          (input):  ', targetRatio)
    print('  Target Fraction for each market       (output): ')
    for p in range(len(plantList)):
        print(plantList[p], ":",  round(targetFract[p],3))
    print('  Max Break Height      (input):  ', maxBreakHgt)

    print('')

    # Initial Algorithm: disregard breaking of partial pallets
    #   Identify full pallets to break: palletBreakList
    #     Sort eligible list by weight to favor breaking the lightest full pallets

    print('LoadBreak Summary -------------------------------------------------------------------------------------------------------')
    print('')

    palletEligibleList.sort(key=attrgetter('pallet_wt'))

    for p in range(len(plantList)):
        print('  Market: ', plantList[p])

        avgWgtBreak[p] = 0
        avgHgtBreak[p] = 0
        maxWgtBreak[p] = 0
        maxHgtBreak[p] = 0

        if ( breakPartialPallets ):
            print('...debug: option to break partial pallets is True', breakPartialPallets)

        numToBreak[p] = math.floor(countFull[p]*targetFract[p])
        print('    number of full pallets to break  = ', numToBreak[p])
        if ( numToBreak[p] > ( countFull[p] - countFullIneligible[p] ) ):
                print('    Number of eligible full pallets is less than the target quantity to be broken !')
                numToBreak[p] = countFull[p] - countFullIneligible[p]
                print('    adjusted number of full pallets to break = ', numToBreak[p])
        print('    number of remaining full pallets = ', countFull[p] - numToBreak[p])
        print('    number of new partial pallets    = ', 2 * numToBreak[p])
        # print('    ratio of full / partial pallets (broken) = ', round((countFull[p] - numToBreak[p]) * 0.5 / numToBreak[p], 2))
        if countPart[p] + 2 * numToBreak[p] > 0:
            print('    ratio of full / partial pallets (overall) = ', round((countFull[p] - numToBreak[p]) / (countPart[p] + 2 * numToBreak[p]), 2))

        #print('')

        # Identify full pallets to break: palletBreakList
        #   Sort eligible list by weight to favor breaking the lightest full pallets

        countBreak = 0
        for pallet in palletEligibleList:
            if ( pallet.plant == plantList[p] ):
                if ( countBreak < numToBreak[p] ):
                    countBreak += 1
                    palletBreakList.append(pallet)
                    if ( debugPrint ):
                        print('  Wgt = ', pallet.pallet_wt)
                else:
                    #print('  done with plant = ', plantList[p] )
                    #print('')
                    break

        # update statistics on partial pallets from breaking of full pallets:
        for pallet in palletBreakList:
            if ( pallet.plant == plantList[p] ):
                avgWgtBreak[p] += pallet.pallet_wt
                avgHgtBreak[p] += pallet.pallet_ht
                #maxWgtBreak[p] = max(maxWgtFull[p], palletList[i].pallet_wt)

                # Calc height for partial pallets (A, B) from breaking of full pallet:
                pallet.break_htA = round( 0.001 * ( pallet.pallet_ht / pallet.plt_layers) * math.ceil( pallet.plt_layers / 2), 3)
                pallet.break_htB = round( 0.001 * ( pallet.pallet_ht / pallet.plt_layers) * math.floor( pallet.plt_layers / 2), 3)

                # Calc weight for partial pallets (A, B) from breaking of full pallet:
                pallet.break_wtA = round( ( pallet.pallet_wt / pallet.plt_layers) * math.ceil( pallet.plt_layers / 2), 1)
                pallet.break_wtB = round( ( pallet.pallet_wt / pallet.plt_layers) * math.floor( pallet.plt_layers / 2), 1)

                # Calc layers for partial pallets (A, B) from breaking of full pallet:
                pallet.break_layerA = math.ceil( pallet.plt_layers / 2 )
                pallet.break_layerB = math.floor( pallet.plt_layers / 2 )

                # Update Maximum height and weight, based on A since A will be larger when there is an odd number of layers:
                maxHgtBreak[p] = max ( maxHgtBreak[p], pallet.break_htA )
                maxWgtBreak[p] = max ( maxWgtBreak[p], pallet.break_wtA )

                if ( debugPrint ):
                    print('  ... new partial pallet: ', pallet.plant, ' , ', pallet.material, ', ', pallet.material_descr, ', ',
                          pallet.break_layerA, ', ', pallet.break_layerB, ', ', pallet.break_htA, ', ', pallet.break_htB, ', ',
                          pallet.break_wtA, ', ', pallet.break_wtB)

                if ( debugPrint ):
                    print('  Hgt A = ', pallet.break_htA)
                    print('  Hgt B = ', pallet.break_htB)


        if ( numToBreak[p] > 0 ):
            avgWgtBreak[p] = round( 0.5 * avgWgtBreak[p] / numToBreak[p], 1)
            avgHgtBreak[p] = round( 0.001 * 0.5 * avgHgtBreak[p] / numToBreak[p], 3)

            print('    average weight of new partial pallets = ', avgWgtBreak[p])
            print('    average height of new partial pallets = ', avgHgtBreak[p])
            print('    maximum weight of new partial pallets = ', maxWgtBreak[p])
            print('    maximum height of new partial pallets = ', maxHgtBreak[p])
            print('')


        # Sort final results and prepare output for Excel
        palletBreakList.sort(key=attrgetter('plant','material'))

    matString = ''
    countBreak = 0
    for pallet in palletBreakList:
        if ( debugPrint ):
            print('...debug', pallet.plant, pallet.material )

        #check for new material...
        if ( not matString == pallet.material ):

            result = Result_Obj()
            result.plant = pallet.plant
            result.material = pallet.material
            result.material_descr = pallet.material_descr
            result.layerA = pallet.break_layerA
            result.layerB = pallet.break_layerB
            result.htA = pallet.break_htA
            result.htB = pallet.break_htB
            result.wtA = pallet.break_wtA
            result.wtB = pallet.break_wtB
            result.wt = result.wtA + result.wtB
            result.plant = pallet.plant

            resultList.append(result)
            # Set current material and reset count=1
            matString = pallet.material
            countBreak = 1
            result.countBreak = countBreak
            if ( debugPrint ):
                print('...new: ', matString)
        else:
            countBreak += 1
            result.countBreak = countBreak
            if ( debugPrint ):
                print('...same:', countBreak)

    print('LoadBreak Recommendations -----------------------------------------------------------------------------------------------')
    print('')

    # Output Summary - use PrettyTable for nice output to console or a file:
    output1 = []
    ptab = PrettyTable( ['Market', 'Material', 'Description', '# Full pallets to break', '# Layers in full pallet', '# Layers in 1st partial pallet', '# Layers in 2nd partial pallet', 'Partial Pallets', 'Height-1st partial pallet', 'Height-2nd partial pallet', 'Weight-1st partial pallet', 'Weight-2nd partial pallet', 'Total Weight (full pallet before breaking)'] )
    output1.append(['Market', 'Material', 'Description', '# Full pallets to break', '# Layers in full pallet', '# Layers in 1st partial pallet', '# Layers in 2nd partial pallet', 'Partial Pallets', 'Height-1st partial pallet', 'Height-2nd partial pallet', 'Weight-1st partial pallet', 'Weight-2nd partial pallet', 'Total Weight (full pallet before breaking)'])

    resultList.sort(key=attrgetter('wt'))
    for result in resultList:
        num_layers = result.layerA + result.layerB
        ptab.add_row([result.plant, result.material, result.material_descr, result.countBreak, num_layers, result.layerA, result.layerB, (2*result.countBreak), result.htA, result.htB, result.wtA, result.wtB, result.wt])
        output1.append(
            [result.plant, result.material, result.material_descr, result.countBreak, num_layers, result.layerA, result.layerB, (2*result.countBreak), result.htA, result.htB, result.wtA, result.wtB, result.wt])
    # print(ptab)
    # pd.DataFrame(output1).to_csv(output_csv_file, index=False, header=False)

    # Triple Stacks
    output2 = []
    output2.append(
        ['Market', 'Material', 'Bottom layer wt.', 'Middle layer wt.', 'Top layer wt - Partial pallet break weight', 'Pallet weight (before breaking)', 'Partial pallet break height'])
    for trpl_stck in triple_stacks_list:
        output2.append([
            trpl_stck[0], trpl_stck[1], trpl_stck[2], trpl_stck[3], trpl_stck[4], trpl_stck[5], trpl_stck[6]
        ])

    #sys.stdout.close()
    sys.stdout = sys.__stdout__

    # Detailed output
    output0 = []
    output0.append(
        ['Market', 'Initial_Count_Full', 'Count Full (Ineligible to break)', 'Initial_Count_Partial',
        'Full pallets to break (based on target ratio)', ' Adjusted number of full pallets to break (based on eligibility)',
        'New_Count_Full', 'New_Count_Partial', 'Ratio_of_full_and_partial_pallets'])
    output0_columns =  ['Market', 'Initial_Count_Full', 'Count Full (Ineligible to break)', 'Initial_Count_Partial',
        'Full pallets to break (based on target ratio)', ' Adjusted number of full pallets to break (based on eligibility)',
        'New_Count_Full', 'New_Count_Partial', 'Ratio_of_full_and_partial_pallets']
    for p in range(len(plantList)):
        num_To_Break = math.floor(countFull[p]*targetFract[p])
        adjusted_num_To_Break = num_To_Break
        if ( num_To_Break > ( countFull[p] - countFullIneligible[p] ) ):
            adjusted_num_To_Break = countFull[p] - countFullIneligible[p]
        new_full_pallets =  countFull[p] - adjusted_num_To_Break
        new_partial_pallets = countPart[p] + 2 * adjusted_num_To_Break
        ratio_full_partial = 0
        if new_partial_pallets > 0:
            ratio_full_partial = round(new_full_pallets / new_partial_pallets, 2)

        output0.append([plantList[p], countFull[p], countFullIneligible[p], countPart[p], num_To_Break,
        adjusted_num_To_Break, new_full_pallets, new_partial_pallets, ratio_full_partial])

    ########## writing output to template file ###########
    shutil.copy2(outputTemplate_File, 'LoadBreak Output.xlsx')
    output_excel_file = "LoadBreak Output.xlsx"
    wb = load_workbook(output_excel_file)
    # specify font style
    from openpyxl.styles import Font
    font = Font(size=9)

    # create a list for tab names and tables - populate all tabs (except log tab)
    tab_dfs = [
        ('Breaking Instructions', pd.DataFrame(output1[1:])),
        ('Results Summary', pd.DataFrame(output0[1:])),
        ('Triple Stacking Info', pd.DataFrame(output2[1:])),
        # ('Log', pd.DataFrame(output_log)),
        ('Data Checks', pd.DataFrame(output_dataChecks[1:])),
    ]

    # write tables cell by cell
    for tab, df in tab_dfs:
        rows = dataframe_to_rows(df, index=False, header=False)
        for r_idx, row in enumerate(rows, 1):
            for c_idx, value in enumerate(row, 1):
                wb[tab].cell(row=r_idx + 3, column=c_idx + 1,
                             value=value).font = font

    # save file
    wb.save(output_excel_file)

    # # populate log tab
    # with pd.ExcelWriter(output_template_file) as writer:
    #     # excel worksheet - output log
    #     pd.DataFrame(output_log).to_excel(writer, sheet_name='Log', index=False, header=False)
    ############################################################

    # # excel workbook output
    # with pd.ExcelWriter(output_excel_file) as writer:
    #     # excel worksheet - output summary
    #     pd.DataFrame(output1).to_excel(writer, sheet_name='Breaking Instructions', index=False, header=False)
    #     # excel worksheet - detailed output
    #     # lines = []
    #     # with open(output_text_file) as f:
    #     #     lines = f.readlines()
    #     # pd.DataFrame(lines).to_excel(writer, sheet_name='Detailed Output', index=False, header=False)

    #      # excel worksheet - breaking instructions
    #     pd.DataFrame(output0).to_excel(writer, sheet_name='Results Summary', index=False, header=False)

    #     # excel worksheet - triple stacks
    #     pd.DataFrame(output2).to_excel(writer, sheet_name='Triple stacking Info', index=False, header=False)
    #     # excel worksheet - output log
    #     pd.DataFrame(output_log).to_excel(writer, sheet_name='Log', index=False, header=False)

    #     # excel worksheet - data validation
    #     pd.DataFrame(output_dataChecks).to_excel(writer, sheet_name='Data Checks', index=False, header=False)

    ######### printing output to UI #########

    output0_df = pd.DataFrame(output0)
    print(output0_df)
    new_header = output0_df.iloc[0] #grab the first row for the header
    output0_df = output0_df[1:] #take the data less the header row
    output0_df.columns = new_header #set the header row as the df header
    print(output0_df)
    new_output = output0_df.copy()
    app.Market = pd.Index(output0_df["Market"])
    app.output0_df = output0_df
    # print("check point")
    # print(output0_df.columns)
    # print(output0_df.head())
    app.output0_df.set_index('Market', inplace=True)
    # print(output0_df.Initial_Count_Full)
    # print(output0_df.Initial_Count_Full['230A'])
    # print(type(output0_df.Initial_Count_Full['230A']))
    app.output0_df.Initial_Count_Full = app.output0_df.Initial_Count_Full.astype(int)
    app.output0_df.Initial_Count_Partial = app.output0_df.Initial_Count_Partial.astype(int)
    app.output0_df.New_Count_Full = app.output0_df.New_Count_Full.astype(int)
    app.output0_df.New_Count_Partial = app.output0_df.New_Count_Partial.astype(int)
    app.output0_df.Ratio_of_full_and_partial_pallets = app.output0_df.Ratio_of_full_and_partial_pallets.astype(float)
    # app.output0_df['index1'] = df.index

    # output0_df.columns = output0_columns
    # print("HELLO")
    # print(output0_df.columns)
    # print("HELLO2")
    # print(output0_df.head())

    app.output0_df = app.output0_df[['Initial_Count_Full', 'Initial_Count_Partial',
            'New_Count_Full', 'New_Count_Partial', 'Ratio_of_full_and_partial_pallets']]
    print("Loadbreak - output")
    print(app.output0_df)
    print(output0_df)
    #################################################

    # # color coding specific cells
    # wb = openpyxl.load_workbook(output_excel_file)
    # ws = wb['Breaking Instructions']
    # fill_cell = PatternFill(patternType='solid',fgColor='00FFFF00')
    # ws['A1'].fill = fill_cell
    # ws['B1'].fill = fill_cell
    # ws['D1'].fill = fill_cell
    # ws['E1'].fill = fill_cell
    # ws['F1'].fill = fill_cell
    # ws['G1'].fill = fill_cell
    # wb.save(output_excel_file)


    # # # excel worksheet - output summary
    # # pd.DataFrame(output1).to_excel(output_excel_file, sheet_name='Output Summary', index=False, header=False)
    # # # excel worksheet - detailed output
    # # lines = []
    # # with open(output_text_file) as f:
    # #     lines = f.readlines()
    # #     # print(contents)
    # # # csv_output = pd.read_csv(output_text_file, delim_whitespace=True)
    # # pd.DataFrame(lines).to_excel(output_excel_file, sheet_name='Detailed Output', index=False, header=False)

    # # hiding log sheet
    # wb = openpyxl.load_workbook(output_excel_file)
    # ws = wb['Log']
    # ws.sheet_state = 'hidden'

    # # bold font excel headers
    # from openpyxl.styles import Font
    # wb = openpyxl.load_workbook(output_excel_file)
    # ws = wb['Breaking Instructions']
    # bold_font = Font(bold=True)
    # for cell in ws["1:1"]:
    #     cell.font = bold_font




    print('')
    print('LoadBreak execution complete!')

    return output_excel_file, new_output

# given a list of weight values - return the max weight (mw), lightest weight (lw) greater than break_weight such that
# mw >= lw + break_weight AND lw >= break_weight
# also return binary - FALSE if such weights dont exist
def GetWeightVals(weight_list, pallet_wt_Iappigible, ind_plt, break_weight):#, wt_threshold_middleLayer_plt):
    # max_weight_val = max(weight_list)

    # remove pallet (whose eligbility is being determined) from consideration
    weight_list.remove(pallet_wt_Iappigible)

    max_weight_vals = heapq.nlargest(2, weight_list)
    weight_indices_list = heapq.nlargest(2, enumerate(weight_list), key=lambda x: x[1])
    indices_list = []
    for vals in weight_indices_list:
        indices_list.append(vals[0])


    weight_found = False

    layer2_weight_val_I = 0
    layer2_weight_val_I_index = -1
    weight_found_I = False
    for i in range(len(weight_list)):
        val = weight_list[i]
        if val >= break_weight and \
                max_weight_vals[0] >= val + break_weight and \
                val > layer2_weight_val_I and \
                i not in indices_list:
            layer2_weight_val_I = val
            layer2_weight_val_I_index = i
            weight_found_I = True

    layer2_weight_val_II = 0
    layer2_weight_val_II_index = -1
    if weight_found_I: # if triple stacking found for 1 partial pallet
        for i in range(len(weight_list)):
            val = weight_list[i]
            if val >= break_weight and \
                    max_weight_vals[1] >= val + break_weight and \
                    val > layer2_weight_val_II and \
                    i != layer2_weight_val_I_index and \
                    i not in indices_list: # should be distinct from layer2 weight identified earlier

                layer2_weight_val_II = val
                layer2_weight_val_II_index = i
                weight_found = True

    layer2_weight_vals = []
    layer2_weight_vals.append(layer2_weight_val_I)
    layer2_weight_vals.append(layer2_weight_val_II)

    # NOTE: these are indices of the weight list (not the original "palletList")
    indices_list.append(layer2_weight_val_I_index)
    indices_list.append(layer2_weight_val_II_index)

    # # append index of pallet (whose eligbility is being determined)
    # indices_list.append(ind_plt)

    # add pallet (whose eligbility is being determined) back to the list
    weight_list.insert(ind_plt, pallet_wt_Iappigible)

    return max_weight_vals, indices_list, layer2_weight_vals, weight_found