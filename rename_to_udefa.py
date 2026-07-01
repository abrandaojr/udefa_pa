"""Rename existing raster outputs to the UDefA naming convention.

Run once after updating the code. Safe to re-run (already-renamed files are skipped).
Usage:
    python rename_to_udefa.py [--dry-run]
"""
import argparse
import sys
from pathlib import Path

RASTER_ROOT = Path(__file__).parent / "outputs" / "rasters"
GEOTIFF_DIR = RASTER_ROOT / "geotiff"
IDRISI_DIR = RASTER_ROOT / "idrisi"
TILES_DIR = RASTER_ROOT / "geotiff_tiles"

# Ordered longest-first so longer patterns match before shorter prefixes.
# Each entry: (old_stem_prefix, new_stem_prefix)
# For single-file renames the prefix IS the full stem.
# For tile files the suffix "-XXXX-XXXX" is preserved automatically.
STEM_RENAMES: list[tuple[str, str]] = [
    # --- Old CTrees ForestChange names (from 89-product submission) ---
    ("CTrees_ForestChange_1985_2009_StableLossGain_EPSG_5880_30m", "UDefA_Ct_ForestChange4_Hist_EPSG_5880_30m"),
    ("CTrees_ForestChange_2009_2013_StableLossGain_EPSG_5880_30m", "UDefA_Ct_ForestChange4_Cal_EPSG_5880_30m"),
    ("CTrees_ForestChange_2013_2018_StableLossGain_EPSG_5880_30m", "UDefA_Ct_ForestChange4_Con_EPSG_5880_30m"),
    ("CTrees_ForestChange_2018_2024_StableLossGain_EPSG_5880_30m", "UDefA_Ct_ForestChange4_T3T4_EPSG_5880_30m"),
    # --- Old MapBiomas names ---
    ("MapBiomas_Binary_Forest_2009_30m_EPSG_5880_30m", "UDefA_MB_Forest_T1_EPSG_5880_30m"),
    ("MapBiomas_Binary_Forest_2013_30m_EPSG_5880_30m", "UDefA_MB_Forest_T2_EPSG_5880_30m"),
    ("MapBiomas_Binary_Forest_2018_30m_EPSG_5880_30m", "UDefA_MB_Forest_T3_EPSG_5880_30m"),
    ("MapBiomas_Binary_Forest_2024_30m_EPSG_5880_30m", "UDefA_MB_Forest_T4_EPSG_5880_30m"),
    ("MapBiomas_LULC_2024_30m_EPSG_5880_30m", "UDefA_MB_LULC_T4_EPSG_5880_30m"),
    ("MapBiomas_LULC_2009_EPSG_5880_30m", "UDefA_MB_LULC_T1_EPSG_5880_30m"),
    ("MapBiomas_LULC_2013_EPSG_5880_30m", "UDefA_MB_LULC_T2_EPSG_5880_30m"),
    ("MapBiomas_LULC_2018_EPSG_5880_30m", "UDefA_MB_LULC_T3_EPSG_5880_30m"),
    ("MapBiomas_LULC_2024_EPSG_5880_30m", "UDefA_MB_LULC_T4_EPSG_5880_30m"),
    ("MapBiomas_LandCover_2009_EPSG_5880_30m", "UDefA_MB_LULC_T1_EPSG_5880_30m"),
    ("MapBiomas_LandCover_2013_EPSG_5880_30m", "UDefA_MB_LULC_T2_EPSG_5880_30m"),
    ("MapBiomas_LandCover_2018_EPSG_5880_30m", "UDefA_MB_LULC_T3_EPSG_5880_30m"),
    ("MapBiomas_LandCover_2024_EPSG_5880_30m", "UDefA_MB_LULC_T4_EPSG_5880_30m"),
    ("MapBiomas_LandCover_1985_EPSG_5880_30m", "UDefA_MB_LULC_T0_EPSG_5880_30m"),
    ("MapBiomas_ForestNonForest_1985_EPSG_5880_30m", "UDefA_MB_Forest_T0_EPSG_5880_30m"),
    ("MapBiomas_ForestNonForest_2009_EPSG_5880_30m", "UDefA_MB_Forest_T1_EPSG_5880_30m"),
    ("MapBiomas_ForestNonForest_2013_EPSG_5880_30m", "UDefA_MB_Forest_T2_EPSG_5880_30m"),
    ("MapBiomas_ForestNonForest_2018_EPSG_5880_30m", "UDefA_MB_Forest_T3_EPSG_5880_30m"),
    ("MapBiomas_ForestNonForest_2024_EPSG_5880_30m", "UDefA_MB_Forest_T4_EPSG_5880_30m"),
    ("MapBiomas_ForestLoss_1985_2009_EPSG_5880_30m", "UDefA_MB_ForestLoss_Hist_EPSG_5880_30m"),
    ("MapBiomas_ForestLoss_2009_2013_EPSG_5880_30m", "UDefA_MB_ForestLoss_Cal_EPSG_5880_30m"),
    ("MapBiomas_ForestLoss_2013_2018_EPSG_5880_30m", "UDefA_MB_ForestLoss_Con_EPSG_5880_30m"),
    ("MapBiomas_ForestLoss_2018_2024_EPSG_5880_30m", "UDefA_MB_ForestLoss_T3T4_EPSG_5880_30m"),
    ("MapBiomas_Change_1985_2009_EPSG_5880_30m", "UDefA_MB_ForestChange4_Hist_EPSG_5880_30m"),
    ("MapBiomas_Change_2009_2013_EPSG_5880_30m", "UDefA_MB_ForestChange4_Cal_EPSG_5880_30m"),
    ("MapBiomas_Change_2013_2018_EPSG_5880_30m", "UDefA_MB_ForestChange4_Con_EPSG_5880_30m"),
    ("MapBiomas_Change_2018_2024_EPSG_5880_30m", "UDefA_MB_ForestChange4_T3T4_EPSG_5880_30m"),
    ("MapBiomas_FCBM_Accuracy_Table16_2009_2013_2018_30m_EPSG_5880_30m", "UDefA_MB_FCBM_Accuracy_EPSG_5880_30m"),
    ("MapBiomas_FCBM_Index_2009_2013_2018_30m_EPSG_5880_30m", "UDefA_MB_FCBM_TransIdx_EPSG_5880_30m"),
    ("MapBiomas_FCBM_VT0007_Table15_2009_2013_2018_30m_EPSG_5880_30m", "UDefA_MB_FCBM_VT0007_EPSG_5880_30m"),
    # --- ForestToNonForest → UDefA_*_ForestLoss ---
    ("Change_ForestToNonForest_CTrees_Calibration_2009_2013_30m_EPSG_5880_30m", "UDefA_Ct_ForestLoss_Cal_EPSG_5880_30m"),
    ("Change_ForestToNonForest_CTrees_Confirmation_2013_2018_30m_EPSG_5880_30m", "UDefA_Ct_ForestLoss_Con_EPSG_5880_30m"),
    ("Change_ForestToNonForest_CTrees_HRP_2009_2018_30m_EPSG_5880_30m", "UDefA_Ct_ForestLoss_HRP_EPSG_5880_30m"),
    ("Change_ForestToNonForest_CTrees_DMJSS_30m_EPSG_5880_30m", "UDefA_Ct_DMJSS_ForestLoss_EPSG_5880_30m"),
    ("Change_ForestToNonForest_CTrees_DMJSS_30m", "UDefA_Ct_DMJSS_ForestLoss_EPSG_5880_30m"),
    ("Change_ForestToNonForest_CTrees_FCBM1_2009_to_FCBM2_2013_30m_EPSG_5880_30m", "UDefA_Ct_ForestLoss_Cal_EPSG_5880_30m"),
    ("Change_ForestToNonForest_CTrees_FCBM1_2009_to_FCBM2_2013_30m", "UDefA_Ct_ForestLoss_Cal_EPSG_5880_30m"),
    ("Change_ForestToNonForest_CTrees_FCBM2_2013_to_FCBM3_2018_30m_EPSG_5880_30m", "UDefA_Ct_ForestLoss_Con_EPSG_5880_30m"),
    ("Change_ForestToNonForest_CTrees_FCBM2_2013_to_FCBM3_2018_30m", "UDefA_Ct_ForestLoss_Con_EPSG_5880_30m"),
    ("Change_ForestToNonForest_CTrees_FCBM4_30m", "UDefA_Ct_FCBM4_ForestLoss_EPSG_5880_30m"),
    ("Change_ForestToNonForest_MapBiomas_HRP_2009_2018_30m_EPSG_5880_30m", "UDefA_MB_ForestLoss_HRP_EPSG_5880_30m"),
    ("Change_ForestToNonForest_MapBiomas_Calibration_2009_2013_30m_EPSG_5880_30m", "UDefA_MB_ForestLoss_Cal_EPSG_5880_30m"),
    ("Change_ForestToNonForest_MapBiomas_Confirmation_2013_2018_30m_EPSG_5880_30m", "UDefA_MB_ForestLoss_Con_EPSG_5880_30m"),
    ("Change_ForestToNonForest_MapBiomas_1985_2009_30m_EPSG_5880_30m", "UDefA_MB_ForestLoss_Hist_EPSG_5880_30m"),
    ("Change_ForestToNonForest_MapBiomas_2009_2013_30m_EPSG_5880_30m", "UDefA_MB_ForestLoss_Cal_EPSG_5880_30m"),
    ("Change_ForestToNonForest_MapBiomas_2013_2018_30m_EPSG_5880_30m", "UDefA_MB_ForestLoss_Con_EPSG_5880_30m"),
    ("Change_ForestToNonForest_MapBiomas_2018_2024_30m_EPSG_5880_30m", "UDefA_MB_ForestLoss_T3T4_EPSG_5880_30m"),
    ("Change_ForestToNonForest_MapBiomas_1985_2024_30m", "UDefA_MB_ForestLoss_T0T4_EPSG_5880_30m"),
    # --- Cross agreement ---
    ("Cross_ForestLossAgreement_CTrees_x_MapBiomas_2009_2013_EPSG_5880_30m", "UDefA_Ct_MB_Agreement_Cal_EPSG_5880_30m"),
    ("Cross_ForestLossAgreement_CTrees_x_MapBiomas_2013_2018_EPSG_5880_30m", "UDefA_Ct_MB_Agreement_Con_EPSG_5880_30m"),
    ("Cross_ForestLossAgreement_CTrees_x_MapBiomas_2018_2024_EPSG_5880_30m", "UDefA_Ct_MB_Agreement_T3T4_EPSG_5880_30m"),
    # --- CTrees snapshots ---
    ("CTrees_DMJSS_30m_EPSG_5880_30m", "UDefA_Ct_DMJSS_EPSG_5880_30m"),
    ("CTrees_DMJSS_30m", "UDefA_Ct_DMJSS_EPSG_5880_30m"),
    ("CTrees_FCBM1_2009_30m_EPSG_5880_30m", "UDefA_Ct_Forest_T1_EPSG_5880_30m"),
    ("CTrees_FCBM1_2009_30m", "UDefA_Ct_Forest_T1_EPSG_5880_30m"),
    ("CTrees_FCBM2_2013_30m_EPSG_5880_30m", "UDefA_Ct_Forest_T2_EPSG_5880_30m"),
    ("CTrees_FCBM2_2013_30m", "UDefA_Ct_Forest_T2_EPSG_5880_30m"),
    ("CTrees_FCBM3_2018_30m_EPSG_5880_30m", "UDefA_Ct_Forest_T3_EPSG_5880_30m"),
    ("CTrees_FCBM3_2018_30m", "UDefA_Ct_Forest_T3_EPSG_5880_30m"),
    ("CTrees_FCBM4_30m_EPSG_5880_30m", "UDefA_Ct_Forest_T4_EPSG_5880_30m"),
    ("CTrees_FCBM4_30m", "UDefA_Ct_Forest_T4_EPSG_5880_30m"),
    # --- CTrees FCBM products ---
    ("CTrees_FCBM_Accuracy_Table16_2009_2013_2018_30m_EPSG_5880_30m", "UDefA_Ct_FCBM_Accuracy_EPSG_5880_30m"),
    ("CTrees_FCBM_Index_2009_2013_2018_30m_EPSG_5880_30m", "UDefA_Ct_FCBM_TransIdx_EPSG_5880_30m"),
    ("CTrees_FCBM_VT0007_Table15_2009_2013_2018_30m_EPSG_5880_30m", "UDefA_Ct_FCBM_VT0007_EPSG_5880_30m"),
    # --- MapBiomas persistence ---
    ("MapBiomas_Persistence_A_100pct_1985-2024_EPSG_5880_30m", "UDefA_MB_Persistence_ScenA_EPSG_5880_30m"),
    ("MapBiomas_Persistence_B_95pct_1985-2024_EPSG_5880_30m", "UDefA_MB_Persistence_ScenB_EPSG_5880_30m"),
    ("MapBiomas_Persistence_C_50pct_1985-2024_EPSG_5880_30m", "UDefA_MB_Persistence_ScenC_EPSG_5880_30m"),
    ("MapBiomas_Persistence_D_100pct_2015-2024_EPSG_5880_30m", "UDefA_MB_Persistence_ScenD_EPSG_5880_30m"),
    ("MapBiomas_Persistence_E_100pct_2013-2024_EPSG_5880_30m", "UDefA_MB_Persistence_ScenE_EPSG_5880_30m"),
    ("MapBiomas_Persistence_F_100pct_2018-2024_EPSG_5880_30m", "UDefA_MB_Persistence_ScenF_EPSG_5880_30m"),
    ("MapBiomas_Persistence_A_100pct_1985-2024_30m", "UDefA_MB_Persistence_ScenA_EPSG_5880_30m"),
    ("MapBiomas_Persistence_B_95pct_1985-2024_30m", "UDefA_MB_Persistence_ScenB_EPSG_5880_30m"),
    ("MapBiomas_Persistence_C_50pct_1985-2024_30m", "UDefA_MB_Persistence_ScenC_EPSG_5880_30m"),
    ("MapBiomas_Persistence_D_100pct_2015-2024_30m", "UDefA_MB_Persistence_ScenD_EPSG_5880_30m"),
    ("MapBiomas_Persistence_E_100pct_2013-2024_30m", "UDefA_MB_Persistence_ScenE_EPSG_5880_30m"),
    ("MapBiomas_Persistence_F_100pct_2018-2024_30m", "UDefA_MB_Persistence_ScenF_EPSG_5880_30m"),
    # --- Old UDefA names (CTrees/MapBiomas → Ct/MB, removed year, removed _30m) ---
    ("UDefA_CTrees_Forest_T1_2009_30m_EPSG_5880_30m", "UDefA_Ct_Forest_Input_T1_EPSG_5880_30m"),
    ("UDefA_CTrees_Forest_T2_2013_30m_EPSG_5880_30m", "UDefA_Ct_Forest_Input_T2_EPSG_5880_30m"),
    ("UDefA_CTrees_Forest_T3_2018_30m_EPSG_5880_30m", "UDefA_Ct_Forest_Input_T3_EPSG_5880_30m"),
    ("UDefA_CTrees_NonForest_T1_2009_30m_EPSG_5880_30m", "UDefA_Ct_NonForest_Input_T1_EPSG_5880_30m"),
    ("UDefA_CTrees_NonForest_T2_2013_30m_EPSG_5880_30m", "UDefA_Ct_NonForest_Input_T2_EPSG_5880_30m"),
    ("UDefA_CTrees_NonForest_T3_2018_30m_EPSG_5880_30m", "UDefA_Ct_NonForest_Input_T3_EPSG_5880_30m"),
    ("UDefA_MapBiomas_Forest_T1_2009_30m_EPSG_5880_30m", "UDefA_MB_Forest_Input_T1_EPSG_5880_30m"),
    ("UDefA_MapBiomas_Forest_T2_2013_30m_EPSG_5880_30m", "UDefA_MB_Forest_Input_T2_EPSG_5880_30m"),
    ("UDefA_MapBiomas_Forest_T3_2018_30m_EPSG_5880_30m", "UDefA_MB_Forest_Input_T3_EPSG_5880_30m"),
    ("UDefA_MapBiomas_NonForest_T1_2009_30m_EPSG_5880_30m", "UDefA_MB_NonForest_Input_T1_EPSG_5880_30m"),
    ("UDefA_MapBiomas_NonForest_T2_2013_30m_EPSG_5880_30m", "UDefA_MB_NonForest_Input_T2_EPSG_5880_30m"),
    ("UDefA_MapBiomas_NonForest_T3_2018_30m_EPSG_5880_30m", "UDefA_MB_NonForest_Input_T3_EPSG_5880_30m"),
    # --- ValidMask ---
    ("Valid_Analysis_Mask_30m_EPSG_5880_30m", "UDefA_ValidMask_EPSG_5880_30m"),
    ("Valid_Analysis_Mask_30m", "UDefA_ValidMask_EPSG_5880_30m"),
    # --- SIRGAS para_* files ---
    ("para_ctrees_change_f2nf_2009_2013", "UDefA_Ct_ForestLoss_Cal_SIRGAS"),
    ("para_ctrees_change_f2nf_2013_2018", "UDefA_Ct_ForestLoss_Con_SIRGAS"),
    ("para_ctrees_change_f2nf", "UDefA_Ct_DMJSS_ForestLoss_SIRGAS"),
    ("para_ctrees_DMJSS_30m_sirgas", "UDefA_Ct_DMJSS_SIRGAS"),
    ("para_ctrees_FCBM1_2009_30m_sirgas", "UDefA_Ct_Forest_T1_SIRGAS"),
    ("para_ctrees_FCBM2_2013_30m_sirgas", "UDefA_Ct_Forest_T2_SIRGAS"),
    ("para_ctrees_FCBM3_2018_30m_sirgas", "UDefA_Ct_Forest_T3_SIRGAS"),
    ("para_ctrees_FCBM4_30m_sirgas", "UDefA_Ct_Forest_T4_SIRGAS"),
    ("para_mb_change_f2nf_2009_2013", "UDefA_MB_ForestLoss_Cal_SIRGAS"),
    ("para_mb_change_f2nf_2013_2018", "UDefA_MB_ForestLoss_Con_SIRGAS"),
    ("para_persistence_30m_sirgas_ScenA_100pct_1985_2024", "UDefA_MB_Persistence_ScenA_SIRGAS"),
    ("para_persistence_30m_sirgas_ScenB_95pct_1985_2024", "UDefA_MB_Persistence_ScenB_SIRGAS"),
    ("para_persistence_30m_sirgas_ScenC_50pct_1985_2024", "UDefA_MB_Persistence_ScenC_SIRGAS"),
    ("para_persistence_30m_sirgas_ScenD_100pct_2015_2024", "UDefA_MB_Persistence_ScenD_SIRGAS"),
    ("para_persistence_30m_sirgas_ScenE_100pct_2013_2024", "UDefA_MB_Persistence_ScenE_SIRGAS"),
    ("para_persistence_30m_sirgas_ScenF_100pct_2018_2024", "UDefA_MB_Persistence_ScenF_SIRGAS"),
    ("para_mapbiomas_forest_annual_1985_2024_30m_sirgas", "UDefA_MB_ForestAnnual_SIRGAS"),
    ("para_mapbiomas_lulc_annual_1985_2024_30m_sirgas", "UDefA_MB_LULC_Annual_SIRGAS"),
]


def _new_name(stem: str) -> str | None:
    """Return the new stem for a given file stem, or None if no rename needed."""
    for old_prefix, new_prefix in STEM_RENAMES:
        if stem == old_prefix:
            return new_prefix
        # Tile files: old_prefix + "-XXXXXXXXXX-XXXXXXXXXX"
        if stem.startswith(old_prefix + "-") and stem[len(old_prefix)] == "-":
            tail = stem[len(old_prefix):]
            return new_prefix + tail
    return None


def rename_directory(directory: Path, extensions: list[str], dry_run: bool) -> int:
    if not directory.exists():
        return 0
    count = 0
    seen_targets: dict[str, str] = {}
    for path in sorted(directory.iterdir()):
        if path.suffix.lower() not in extensions:
            continue
        new_stem = _new_name(path.stem)
        if new_stem is None:
            continue
        new_path = path.with_name(new_stem + path.suffix)
        if new_path == path:
            continue
        if new_path.name in seen_targets:
            print(f"  SKIP (conflict): {path.name} -> {new_path.name}  (already mapped from {seen_targets[new_path.name]})")
            continue
        seen_targets[new_path.name] = path.name
        if new_path.exists():
            print(f"  SKIP (exists):   {path.name} -> {new_path.name}")
            continue
        if dry_run:
            print(f"  DRY-RUN: {path.name} -> {new_path.name}")
        else:
            try:
                path.rename(new_path)
                print(f"  RENAMED: {path.name} -> {new_path.name}")
            except PermissionError:
                print(f"  LOCKED (skip): {path.name}")
                continue
        count += 1
    return count


def main() -> None:
    parser = argparse.ArgumentParser(description="Rename raster outputs to UDefA convention.")
    parser.add_argument("--dry-run", action="store_true", help="Print renames without applying them.")
    args = parser.parse_args()

    geotiff_ext = [".tif", ".tiff", ".aux"]
    idrisi_ext = [".rst", ".rdc", ".pal"]
    tile_ext = [".tif", ".tiff"]

    total = 0
    print(f"\n=== GeoTIFF mosaics ({GEOTIFF_DIR}) ===")
    total += rename_directory(GEOTIFF_DIR, geotiff_ext, args.dry_run)

    print(f"\n=== IDRISI files ({IDRISI_DIR}) ===")
    total += rename_directory(IDRISI_DIR, idrisi_ext, args.dry_run)

    print(f"\n=== GeoTIFF tiles ({TILES_DIR}) ===")
    total += rename_directory(TILES_DIR, tile_ext, args.dry_run)

    label = "Would rename" if args.dry_run else "Renamed"
    print(f"\n{label} {total} file(s) total.")


if __name__ == "__main__":
    main()
