"""Build data/im_input_A_B.csv from photographed A-B (US10/US16) inputParams."""
from __future__ import annotations

import csv
from pathlib import Path

OUT = Path(__file__).with_name("im_input_A_B.csv")

FULL = [
    "plt_4", "plt_6", "plt_12", "plt_26", "plt_32", "plt_37", "plt_38",
    "plt_103", "plt_162", "plt_169", "plt_172", "plt_178", "plt_185",
    "plt_190", "plt_193", "plt_195", "plt_202", "plt_204", "plt_206",
    "plt_207", "plt_208", "plt_209", "plt_210", "plt_211", "plt_212",
    "plt_213", "plt_214", "plt_215", "plt_216", "plt_217", "plt_218",
    "plt_219",
]
PARTIAL = ["plt_221", "plt_222"]
ALL = FULL + PARTIAL

MATERIALS = [
    409512105, 120241400, 409509005, 306685200, 406748705, 409561210,
    401856105, 111927302, 409501209, 401809113, 409512005, 401801510,
    406730108, 409509205, 406812300, 406748805, 409161406, 406788805,
    681941705, 120202500, 120234500, 680640600, 406815005, 406814902,
    409167909, 409109507, 409164708, 511720200, 406818000, 681038309,
    681116100,
]

# Photographed skuPriorityIncentive (material -> priority). 5 SKUs are unlisted.
SKU_PRIORITY = {
    681941705: 1, 111927302: 1, 409512105: 1, 120202500: 1, 409509005: 1,
    120234500: 1, 120241400: 1, 680640600: 1,
    409501209: 2, 406815005: 2, 406814902: 2, 401809113: 2,
    306685200: 3, 409561210: 3, 409512005: 3, 409167909: 3, 406748705: 3,
    409509205: 3, 409161406: 3, 409109507: 3,
    406748805: 5, 406788805: 5, 406730108: 5, 409164708: 5, 401801510: 5,
    511720200: 5,
}

DEMAND = {
    "plt_4": 14.0, "plt_6": 1.0, "plt_12": 4.0, "plt_26": 2.0, "plt_32": 16.0,
    "plt_37": 13.0, "plt_38": 16.0, "plt_103": 1.0, "plt_162": 17.0, "plt_169": 8.0,
    "plt_172": 2.0, "plt_178": 20.0, "plt_185": 3.0, "plt_190": 5.0, "plt_193": 15.0,
    "plt_195": 2.0, "plt_202": 10.0, "plt_204": 10.0, "plt_206": 5.0, "plt_207": 1.0,
    "plt_208": 10.0, "plt_209": 1.0, "plt_210": 1.0, "plt_211": 3.0, "plt_212": 2.0,
    "plt_213": 17.0, "plt_214": 10.0, "plt_215": 3.0, "plt_216": 2.0, "plt_217": 1.0,
    "plt_218": 1.0, "plt_219": 1.0,
}

HEIGHT = {
    "plt_4": 3.8465, "plt_6": 3.2566666666666665, "plt_12": 3.827,
    "plt_26": 3.591666666666667, "plt_32": 3.827, "plt_37": 3.827, "plt_38": 3.827,
    "plt_103": 3.780833333333333, "plt_162": 3.2566666666666664, "plt_169": 3.8465,
    "plt_172": 3.780833333333333, "plt_178": 3.896, "plt_185": 3.87575,
    "plt_190": 3.83, "plt_193": 3.827, "plt_195": 3.780833333333333,
    "plt_202": 3.827, "plt_204": 3.8025, "plt_206": 3.827, "plt_207": 3.9375,
    "plt_208": 4.1875, "plt_209": 3.8625, "plt_210": 4.0, "plt_211": 3.40625,
    "plt_212": 3.40625, "plt_213": 3.846333333333333, "plt_214": 3.846333333333333,
    "plt_215": 3.827, "plt_216": 4.0, "plt_217": 3.825, "plt_218": 4.166666666666666,
    "plt_219": 3.8541666666666665, "plt_221": 3.0513333333333333, "plt_222": 2.4135,
}

WEIGHT = {
    "plt_4": 1235.0, "plt_6": 366.968, "plt_12": 1190.126, "plt_26": 602.0,
    "plt_32": 1213.7, "plt_37": 1190.126, "plt_38": 1190.126, "plt_103": 1308.6,
    "plt_162": 1042.36, "plt_169": 1208.0, "plt_172": 1315.0, "plt_178": 1235.0,
    "plt_185": 1554.28, "plt_190": 1221.575, "plt_193": 1190.126, "plt_195": 1300.6,
    "plt_202": 1213.7, "plt_204": 996.86, "plt_206": 1023.8, "plt_207": 893.96,
    "plt_208": 906.2, "plt_209": 1206.5, "plt_210": 1141.6999999999998,
    "plt_211": 965.0, "plt_212": 965.72, "plt_213": 1348.84, "plt_214": 1348.84,
    "plt_215": 943.16, "plt_216": 690.2, "plt_217": 1029.56, "plt_218": 858.2,
    "plt_219": 1068.809, "plt_221": 734.3619999999999, "plt_222": 490.7639999999998,
}

STACK = {
    "plt_4": 0, "plt_6": 0, "plt_12": 0, "plt_26": 2, "plt_32": 0, "plt_37": 0,
    "plt_38": 0, "plt_103": 0, "plt_162": 0, "plt_169": 0, "plt_172": 0,
    "plt_178": 0, "plt_185": 0, "plt_190": 0, "plt_193": 0, "plt_195": 0,
    "plt_202": 0, "plt_204": 0, "plt_206": 0, "plt_207": 2, "plt_208": 0,
    "plt_209": 0, "plt_210": 2, "plt_211": 2, "plt_212": 2, "plt_213": 0,
    "plt_214": 0, "plt_215": 0, "plt_216": 0, "plt_217": 0, "plt_218": 2,
    "plt_219": 2, "plt_221": 0, "plt_222": 0,
}

PALLET_PRIORITY = {
    "plt_4": 1, "plt_6": 1, "plt_12": 1, "plt_26": 3, "plt_32": 3, "plt_37": 3,
    "plt_38": 3, "plt_103": 5, "plt_162": 1, "plt_169": 2, "plt_172": 2,
    "plt_178": 3, "plt_185": 5, "plt_190": 5, "plt_193": 3, "plt_195": 5,
    "plt_202": 5, "plt_204": 3, "plt_206": 5, "plt_207": 1, "plt_208": 1,
    "plt_209": 1, "plt_210": 1, "plt_211": 2, "plt_212": 2, "plt_213": 3,
    "plt_214": 3, "plt_215": 5, "plt_216": 5, "plt_217": 5, "plt_218": 4,
    "plt_219": 4, "plt_221": 3, "plt_222": 3,
}

REFRIG = {plt: "" for plt in ALL}
REFRIG["plt_26"] = "Yes"
REFRIG["plt_207"] = "Yes"

# Photographed palletNumCasesDict through plt_216. Remaining five values
# were clipped by Excel wrap in the screenshot and are inferred:
# plt_217 ~ plt_206 (weight 1029 vs 1024), plt_218 ~ plt_208 (height 4.17 vs 4.19),
# plt_219 ~ plt_209 (height 3.85 vs 3.86), partials use 294 (fraction 178/294 and 116/294).
CASES = {
    "plt_4": 300, "plt_6": 224, "plt_12": 294, "plt_26": 105, "plt_32": 300,
    "plt_37": 294, "plt_38": 294, "plt_103": 320, "plt_162": 160, "plt_169": 300,
    "plt_172": 320, "plt_178": 300, "plt_185": 280, "plt_190": 325, "plt_193": 294,
    "plt_195": 320, "plt_202": 300, "plt_204": 492, "plt_206": 480, "plt_207": 120,
    "plt_208": 66, "plt_209": 110, "plt_210": 217, "plt_211": 24, "plt_212": 24,
    "plt_213": 176, "plt_214": 176, "plt_215": 480, "plt_216": 63,
    "plt_217": 480, "plt_218": 66, "plt_219": 110, "plt_221": 294, "plt_222": 294,
}

# Photographed pallet_sku_priority_incentive. P3 partials follow the P3 constant.
# P4 (plt_218/219) was clipped; 1000/438 continues 16000, 9100, 4000, 1 / 438.
SKU_INC_BY_PRI = {
    1: 36.529680365296805,
    2: 20.77625570776256,
    3: 9.132420091324201,
    4: 2.2831050228310505,
    5: 0.0022831050228310505,
}
PALLET_SKU_INC = {plt: SKU_INC_BY_PRI[PALLET_PRIORITY[plt]] for plt in ALL}
PALLET_SKU_INC["plt_26"] = 384.1324200913242   # photographed; refrigerated +375
PALLET_SKU_INC["plt_207"] = 411.5296803652968  # photographed; refrigerated +375

ONES = {plt: 1 for plt in ALL}

# Real palletMaterialDict from data/material_dict.jpg (Excel row 53).
MATERIAL = {
    "plt_4": 409512105, "plt_6": 120241400, "plt_12": 409509005,
    "plt_26": 306685200, "plt_32": 406748705, "plt_37": 409561210,
    "plt_38": 409561210, "plt_103": 401856105, "plt_162": 111927302,
    "plt_169": 409501209, "plt_172": 401809113, "plt_178": 409512005,
    "plt_185": 401801510, "plt_190": 406730108, "plt_193": 409509205,
    "plt_195": 406812300, "plt_202": 406748805, "plt_204": 409161406,
    "plt_206": 406788805, "plt_207": 681941705, "plt_208": 120202500,
    "plt_209": 120234500, "plt_210": 680640600, "plt_211": 406815005,
    "plt_212": 406814902, "plt_213": 409167909, "plt_214": 409109507,
    "plt_215": 409164708, "plt_216": 511720200, "plt_217": 406818000,
    "plt_218": 681038309, "plt_219": 681116100, "plt_221": 409561210,
    "plt_222": 409561210,
}

STACK_GROUP = {plt: ("G3" if STACK[plt] == 2 else "G1") for plt in ALL}
G2 = {plt: 0 for plt in ALL}
G3 = {plt: (1 if STACK[plt] == 2 else 0) for plt in ALL}
G4 = {plt: 0 for plt in ALL}

# Position geometry matches the A-B screenshot (same 30-slot truck layout as BR16).
BAL = {}
nonzero = {
    (1, "A"): 1.0, (2, "C"): 1.0, (3, "A"): 1.0, (4, "C"): 1.0,
    (5, "A"): 1.0, (6, "C"): 1.0, (7, "A"): 1.0, (8, "C"): 1.0,
    (9, "A"): 1.0, (10, "C"): 1.0, (11, "A"): 1.0, (12, "C"): 1.0,
    (13, "A"): 0.5, (13, "B"): 0.5, (14, "C"): 0.5, (14, "D"): 0.5,
    (15, "B"): 1.0, (16, "D"): 1.0, (17, "B"): 1.0, (18, "D"): 1.0,
    (19, "B"): 1.0, (20, "D"): 1.0, (21, "B"): 1.0, (22, "D"): 1.0,
    (23, "B"): 1.0, (24, "D"): 1.0, (25, "B"): 1.0, (26, "D"): 1.0,
    (27, "B"): 1.0, (28, "D"): 1.0, (29, "B"): 1.0, (30, "D"): 1.0,
}
for p in range(1, 31):
    for s in ("A", "B", "C", "D"):
        BAL[(p, s)] = nonzero.get((p, s), 0.0)

POS_GROUP = {
    1: "A", 2: "C", 3: "A", 4: "C", 5: "A", 6: "C", 7: "A", 8: "C",
    9: "A", 10: "C", 11: "A", 12: "C", 13: "B", 14: "D", 15: "B", 16: "D",
    17: "B", 18: "D", 19: "B", 20: "D", 21: "B", 22: "D", 23: "B", 24: "D",
    25: "B", 26: "D", 27: "B", 28: "D", 29: "B", 30: "D",
}


def py(val) -> str:
    """Format a Python literal the way the existing dumps do."""
    if isinstance(val, dict):
        inner = ", ".join(f"{py(k)}: {py(v)}" for k, v in val.items())
        return "{" + inner + "}"
    if isinstance(val, list):
        return "[" + ", ".join(py(x) for x in val) + "]"
    if isinstance(val, tuple):
        if len(val) == 1:
            return f"({py(val[0])},)"
        return "(" + ", ".join(py(x) for x in val) + ")"
    if val is None:
        return "None"
    if isinstance(val, bool):
        return "True" if val else "False"
    if isinstance(val, str):
        return repr(val)
    if isinstance(val, float):
        if val != val:  # NaN
            return "nan"
        return repr(val)
    return repr(val)


rows = [
    ("marketID", "A-B"),
    ("containerList", [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]),
    ("effectiveContainerList", [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]),
    ("layerList", [1, 2]),
    ("container_priority", {1: 800, 2: 700, 3: 600, 4: 500, 5: 400, 6: 300}),
    ("positionList", list(range(1, 31))),
    ("frontPositionList", list(range(1, 15))),
    ("rearPositionList", list(range(13, 31))),
    ("leftPositionList", [1, 3, 5, 7, 9, 11, 13, 13, 15, 17, 19, 21, 23, 25, 27, 29]),
    ("rightPositionList", [2, 4, 6, 8, 10, 12, 14, 14, 16, 18, 20, 22, 24, 26, 28, 30]),
    ("sectionList", ["A", "C", "B", "D"]),
    ("positionGroupBalanceCoefficientDict", BAL),
    ("positionGroupDict", POS_GROUP),
    ("positionSequenceDict", {i: i for i in range(1, 31)}),
    ("door_posn", [30, 29]),
    ("containerHeight", 9),
    ("containerHeightAtDoor", 3.5),
    ("max_plt_n", 30),
    ("allowContainerMaterial", []),
    ("first_plt_n", 12),
    ("max_lyr_n", 2),
    ("max_wt", 42000),
    ("max_front_wt", 19920),
    ("max_rear_wt", 22080),
    ("priorityIncentive", 2),
    ("sku_penalty", 1),
    ("unique_sku_n", 31),
    ("max_sku_n", 0),
    ("utilThreshold", 0.75),
    ("casesOrPalletsTruck", "Cases"),
    ("side_to_side_tolerance", 0.1),
    ("front_to_back_tolerance", 0),
    ("sku_penalty_factor", 3),
    ("load_allow_ht", 0),
    ("addedPalletWeight", 35),
    ("addedPalletHeight", 6),
    ("fp_area_tolerance", 0),
    ("enforce_fp_tolerance", "no"),
    ("fullPalletFootPrintArea", "{nan: nan, nan: nan, nan: nan, nan: nan}"),
    ("partialPalletFootPrintArea", "{nan: nan, nan: nan, nan: nan, nan: nan}"),
    ("crushingLimitByMaterial", "{nan: nan, nan: nan, nan: nan, nan: nan}"),
    ("skuPriorityIncentive", SKU_PRIORITY),
    ("skuPriorityIncentive_2", {}),
    ("fullPalletList", FULL),
    ("partialPalletList", PARTIAL),
    ("materialList", MATERIALS),
    ("fullPalletDemandByMaterialBatch", DEMAND),
    ("partialPalletDemandByMaterialBatch", {"plt_221": 1.0, "plt_222": 1.0}),
    ("partialFractionValueForPartialPallet", {
        "plt_221": 0.6054421768707474,
        "plt_222": 0.3945578231292508,
    }),
    ("palletHeightDict", HEIGHT),
    ("palletWeightDict", WEIGHT),
    # Rows 53-60 were not photographed. Fields below are required by the
    # reader/optimizer and are filled from remaining visible data + US defaults.
    ("palletLengthDict", {plt: 48 for plt in ALL}),
    ("palletWidthDict", {plt: 40 for plt in ALL}),
    ("palletMaterialDict", MATERIAL),
    ("palletDescriptionDict", {plt: "" for plt in ALL}),
    ("palletBrandDict", {plt: "" for plt in ALL}),
    ("containerBrandDict", {}),
    ("brand_group", {}),
    ("palletBinLocDict", {plt: "" for plt in ALL}),
    ("palletBatchDict", {plt: "" for plt in ALL}),
    ("palletBatchDateDict", {plt: "" for plt in ALL}),
    ("max_prior_full", dict(DEMAND)),
    ("max_prior_prtl", {"plt_221": 1.0, "plt_222": 1.0}),
    ("stackFactorPallet", STACK),
    ("palletPriorityDict", PALLET_PRIORITY),
    ("palletRefrigeratedDict", REFRIG),
    ("enforce_compact_packing", "yes"),
    ("palletNumCasesDict", CASES),
    ("pallet_sku_priority_incentive", PALLET_SKU_INC),
    ("palletPriority_numAvailableContainers", ONES),
    ("palletPriorityValue_numAvailableContainers", ONES),
    ("palletList", ALL),
    ("palletDemandDict", {**DEMAND, "plt_221": 1.0, "plt_222": 1.0}),
    ("palletProfitDict", PALLET_SKU_INC),
    ("palletStackingGroupDict", STACK_GROUP),
    ("stackFactorg2Pallet", G2),
    ("stackFactorg3Pallet", G3),
    ("stackFactorg4Pallet", G4),
    ("palletLocationDict", {plt: "" for plt in ALL}),
    ("palletPlantDict", {plt: "" for plt in ALL}),
    ("plantPriorityDict", {}),
]


def main() -> None:
    with OUT.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        for key, val in rows:
            if isinstance(val, str) and (
                key in (
                    "marketID", "casesOrPalletsTruck", "enforce_fp_tolerance",
                    "enforce_compact_packing",
                )
                or val.startswith("{")
                or val.startswith("[")
            ):
                w.writerow([key, val])
            else:
                w.writerow([key, py(val)])
    print(f"Wrote {OUT} ({OUT.stat().st_size} bytes, {len(rows)} params)")


if __name__ == "__main__":
    main()
