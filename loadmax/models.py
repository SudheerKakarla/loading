"""Pallet input records and loading-instruction output objects."""


class Pallet_Obj:
    def __init__(self):
        self.palletID = ''
        self.customer = ''
        self.material = ''
        self.batch = ''
        self.batch_date = ''
        self.full_plt_dmd = 0
        self.prtl_plt_dmd = 0
        self.prior_plt_dmd = 0
        self.prt_plt_frac = 0
        self.plt_wt = 0
        self.plt_ht = 0
        self.prtl_plt_wt = 0
        self.prtl_plt_ht = 0
        self.plt_length = 0
        self.plt_width = 0
        self.brand = ''
        self.is_prtl_plt = False
        self.stk_factor = 0
        self.bin_loc = ''
        self.peices_per_plt = 0

        # not populating for now
        self.sto_num = 0
        self.deliv_date = 0
        self.cases_per_plt = 0
        self.cases_per_lyr = 0
        self.lyrs_per_plt = 0
        self.pcs_per_plt = 0
        self.prtl_plt_cases = 0


# class SpecialPallet_Obj:
#     def __init__(self):
#         self.marketID = ''
#         self.marketPalletType = ''
#         self.marketPalletHeight = ''
#         self.marketPalletWeight = ''
#         self.marketBrandGroupDict = dict()


# class MarketBrandGroup_Obj:
#     def __init__(self):
#         self.marketID = ''
#         self.brandID = ''
#         self.groupID = ''


class Load_Obj:
    def __init__(self):
        self.palletID = ''
        self.palletID_II = ''
        self.marketID = ''
        self.container = ''
        self.sequence = ''
        self.position = ''
        self.layer = ''
        self.material = ''
        self.batch = ''
        self.batch_date = ''
        self.bin_loc = ''
        self.cases = ''
        self.total_cases = ''  # added by HKD
        self.sto_num = ''
        self.is_partial = False
        self.weight = ''
        self.pallet_type = ''
        self.material_count = ''
        self.height = 0
        self.brand = ''
        self.cube = 0
        self.is20ft = False

        # set by generate_loading_instructions()
        self.description = ''
        self.stacking_group = 'G1'
        self.location = ''
        self.shipTo = ''
