"""Class labels used by the UDefA Para analysis pipeline."""

PRIMARY_MAPBIOMAS_YEARS = [1985, 2009, 2013, 2018, 2024]
CTREES_YEARS = [2009, 2013, 2018]
CROSS_TABULATION_YEARS = [2009, 2013, 2018]

DMJSS_CLASS_LABELS = {
    0: "Stable Non-Forest",
    1: "Stable Forest",
    2: "Deforestation",
    3: "Regrowth",
    4: "Buffer",
}
DMJSS_BUFFER_DISTANCE_M = 210

MAPBIOMAS_PERSISTENCE_LABELS = {
    1: "Persistent Forest",
    2: "Persistent Non-Forest",
    3: "Land-Cover Change",
}

FCBM_TRANSITION_LABELS = {
    1: "No forest at HRP start, midpoint, or end",
    2: "Forest only at HRP end",
    3: "Forest only at HRP midpoint",
    4: "Forest at HRP midpoint and end",
    5: "Stable forest",
    6: "Deforested in the first half of the HRP and remained non-forest",
    7: "Deforested in the first half of the HRP and regrew by HRP end",
    8: "Deforested in the second half of the HRP",
}

FCBM_VT0007_CLASS_LABELS = {
    1: "Stable non-forest",
    2: "Stable forest",
    3: "Deforested in first half of HRP",
    4: "Deforested in second half of HRP",
}

FCBM_VT0007_REMAP = {
    1: 1,
    2: 1,
    3: 1,
    4: 1,
    5: 2,
    6: 3,
    7: 3,
    8: 4,
}
SCHEME_A_VT0007_TABLE15_REMAP = FCBM_VT0007_REMAP

FCBM_ACCURACY_CLASS_LABELS = {
    1: "Non-forest at end of HRP",
    2: "Forest at end of HRP",
    3: "Deforested within HRP",
}

FCBM_ACCURACY_REMAP = {
    1: 1,
    2: 2,
    3: 1,
    4: 2,
    5: 2,
    6: 3,
    7: 3,
    8: 3,
}
SCHEME_B_ACCURACY_TABLE16_REMAP = FCBM_ACCURACY_REMAP

FCBM_RISK_INDEX_GROUPS = {
    "t1_forest": [5, 6, 7, 8],
    "t1_nonforest": [1, 2, 3, 4],
    "t2_forest": [5, 8],
    "t2_nonforest": [1, 2, 3, 4, 6, 7],
    "t3_forest": [5],
    "t3_nonforest": [1, 2, 3, 4, 6, 7, 8],
    "hrp_deforestation": [6, 7, 8],
    "calibration_deforestation": [6, 7],
    "confirmation_deforestation": [8],
}
SCHEME_C_UDEFA_RISK_GROUPS = FCBM_RISK_INDEX_GROUPS

MB_FCBM_TRANSITION_RULES = {
    (0, 0, 0): 1,
    (0, 0, 1): 2,
    (0, 1, 0): 3,
    (0, 1, 1): 4,
    (1, 1, 1): 5,
    (1, 0, 0): 6,
    (1, 0, 1): 7,
    (1, 1, 0): 8,
}


def validate_verra_class_mappings() -> None:
    """Fail fast if the Verra FCBM interpretation constants are inconsistent."""
    expected_vt0007 = {1: 1, 2: 1, 3: 1, 4: 1, 5: 2, 6: 3, 7: 3, 8: 4}
    expected_accuracy = {1: 1, 2: 2, 3: 1, 4: 2, 5: 2, 6: 3, 7: 3, 8: 3}
    expected_risk = {
        "t1_forest": [5, 6, 7, 8],
        "t1_nonforest": [1, 2, 3, 4],
        "t2_forest": [5, 8],
        "t2_nonforest": [1, 2, 3, 4, 6, 7],
        "t3_forest": [5],
        "t3_nonforest": [1, 2, 3, 4, 6, 7, 8],
        "hrp_deforestation": [6, 7, 8],
        "calibration_deforestation": [6, 7],
        "confirmation_deforestation": [8],
    }
    expected_transition_rules = {
        (0, 0, 0): 1,
        (0, 0, 1): 2,
        (0, 1, 0): 3,
        (0, 1, 1): 4,
        (1, 1, 1): 5,
        (1, 0, 0): 6,
        (1, 0, 1): 7,
        (1, 1, 0): 8,
    }
    if FCBM_VT0007_REMAP != expected_vt0007 or SCHEME_A_VT0007_TABLE15_REMAP != expected_vt0007:
        raise RuntimeError("Invalid VMD0055 v1.1 Table 15 FCBM mapping.")
    if FCBM_ACCURACY_REMAP != expected_accuracy or SCHEME_B_ACCURACY_TABLE16_REMAP != expected_accuracy:
        raise RuntimeError("Invalid VMD0055 v1.1 Table 16 FCBM mapping.")
    if FCBM_RISK_INDEX_GROUPS != expected_risk or SCHEME_C_UDEFA_RISK_GROUPS != expected_risk:
        raise RuntimeError("Invalid VT0007 UDef-A risk-map FCBM grouping.")
    if MB_FCBM_TRANSITION_RULES != expected_transition_rules:
        raise RuntimeError("Invalid MB-FCBM eight-index transition truth table.")

MAPBIOMAS_LAND_COVER_CLASSES = {
    1: "Forest",
    3: "Forest Formation",
    4: "Savanna Formation",
    5: "Mangrove",
    6: "Floodable Forest",
    9: "Forest Plantation",
    10: "Herbaceous and Shrubby Vegetation",
    11: "Wetland",
    12: "Grassland",
    14: "Farming",
    15: "Pasture",
    18: "Agriculture",
    19: "Temporary Crop",
    20: "Sugar Cane",
    21: "Mosaic of Uses",
    22: "Non vegetated area",
    23: "Beach, Dune and Sand Spot",
    24: "Urban Area",
    25: "Other non Vegetated Areas",
    26: "Water",
    27: "Not Observed",
    29: "Rocky Outcrop",
    30: "Mining",
    31: "Aquaculture",
    32: "Hypersaline Tidal Flat",
    33: "River, Lake, and Ocean",
    35: "Palm Oil",
    36: "Perennial Crop",
    39: "Soybean",
    40: "Rice",
    41: "Other Temporary Crops",
    46: "Coffee",
    47: "Citrus",
    48: "Other Perennial Crops",
    49: "Wooded Sandbank Vegetation",
    50: "Herbaceous Sandbank Vegetation",
    62: "Cotton",
    75: "Photovoltaic Power Plant",
}
