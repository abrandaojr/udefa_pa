"""Section 2. Data cleaning and organization."""

from __future__ import annotations

import math
from typing import Any

import ee

from .constants import FCBM_VT0007_CLASS_LABELS, FCBM_VT0007_REMAP
from .data_preparation import resolve_mapbiomas_years, select_mapbiomas_year
from .models import OrganizedData, ReferenceRaster
from .settings import Scenario


def clean_and_organize_data(
    settings: dict[str, Any],
    scenarios: list[Scenario],
    mapbiomas_image: ee.Image,
    area_of_interest: ee.FeatureCollection,
) -> OrganizedData:
    """Create analysis masks, persistence rasters, and harmonized reference rasters."""
    valid_mask = build_valid_analysis_mask(settings, mapbiomas_image)
    persistence_rasters = build_persistence_rasters(
        settings=settings,
        scenarios=scenarios,
        mapbiomas_image=mapbiomas_image,
        valid_mask=valid_mask,
        area_of_interest=area_of_interest,
    )
    references = load_reference_rasters(settings, area_of_interest)
    return OrganizedData(valid_mask, persistence_rasters, references)


def build_valid_analysis_mask(settings: dict[str, Any], mapbiomas_image: ee.Image) -> ee.Image:
    """Mask pixels assigned to classes excluded from this comparison."""
    years = resolve_mapbiomas_years(
        mapbiomas_image,
        range(settings["analysis"]["years"]["start"], settings["analysis"]["years"]["end"] + 1),
    )
    excluded_codes = [int(code) for code in settings["analysis"]["excluded_mapbiomas_codes"]]
    excluded_bands = [
        select_mapbiomas_year(mapbiomas_image, year)
        .remap(excluded_codes, [1] * len(excluded_codes), 0)
        .rename(f"excluded_{year}")
        for year in years
    ]
    return ee.Image.cat(excluded_bands).reduce(ee.Reducer.max()).eq(0).rename("valid_analysis_mask")


def build_persistence_rasters(
    settings: dict[str, Any],
    scenarios: list[Scenario],
    mapbiomas_image: ee.Image,
    valid_mask: ee.Image,
    area_of_interest: ee.FeatureCollection,
) -> dict[str, ee.Image]:
    """Build three-class MapBiomas forest-persistence rasters for each scenario."""
    forest_codes = [int(code) for code in settings["analysis"]["forest_codes"]]
    rasters: dict[str, ee.Image] = {}
    for scenario in scenarios:
        years = resolve_mapbiomas_years(mapbiomas_image, range(scenario.start_year, scenario.end_year + 1))
        annual_forest = [
            select_mapbiomas_year(mapbiomas_image, year)
            .remap(forest_codes, [1] * len(forest_codes), 0)
            .updateMask(valid_mask)
            .rename(f"forest_{year}")
            for year in years
        ]
        forest_frequency = ee.Image.cat(annual_forest).reduce(ee.Reducer.sum())
        number_of_years = len(years)
        nonforest_frequency = ee.Image(number_of_years).subtract(forest_frequency)
        threshold_years = int(math.ceil((scenario.threshold_percent / 100.0) * number_of_years))

        raster = (
            ee.Image(3)
            .where(nonforest_frequency.gte(threshold_years), 2)
            .where(forest_frequency.gte(threshold_years), 1)
            .updateMask(valid_mask)
            .clip(area_of_interest)
            .toByte()
            .rename(f"persistence_{scenario.identifier}")
        )
        rasters[scenario.label] = raster
    return rasters


def load_reference_rasters(
    settings: dict[str, Any],
    area_of_interest: ee.FeatureCollection,
) -> dict[str, ReferenceRaster]:
    """Load and harmonize CTrees-family reference rasters."""
    references: dict[str, ReferenceRaster] = {}
    for name, reference_settings in settings["references"].items():
        _assert_readable_asset(reference_settings["asset"], f"Reference {name}")
        image = ee.Image(reference_settings["asset"])
        if "remap" in reference_settings:
            remap = reference_settings["remap"]
            image = image.remap(remap["from"], remap["to"], remap.get("default", 0))
        if reference_settings.get("interpretation_scheme") == "fcbm_vt0007_table15":
            image = _interpret_fcbm_vt0007(image)
            reference_settings = {
                **reference_settings,
                "class_codes": sorted(FCBM_VT0007_CLASS_LABELS),
                "class_labels": FCBM_VT0007_CLASS_LABELS,
                "groups": {
                    "forest": ["Stable forest"],
                    "nonforest": ["Stable non-forest"],
                    "change": [
                        "Deforested in first half of HRP",
                        "Deforested in second half of HRP",
                    ],
                    "excluded": [],
                },
            }
        class_codes = [int(code) for code in reference_settings["class_codes"]]
        if class_codes and min(class_codes) >= 0 and max(class_codes) <= 255:
            image = image.toByte()
        excluded_codes = [
            int(code)
            for code, label in reference_settings["class_labels"].items()
            if str(label) in reference_settings.get("groups", {}).get("excluded", [])
        ]
        if excluded_codes:
            image = image.updateMask(image.remap(excluded_codes, [1] * len(excluded_codes), 0).eq(0))
        image = image.clip(area_of_interest).rename(name.lower())
        references[name] = ReferenceRaster(
            name=name,
            label=str(reference_settings.get("label", name)),
            image=image,
            class_codes=class_codes,
            class_labels={int(code): str(label) for code, label in reference_settings["class_labels"].items()},
            groups={key: [str(value) for value in values] for key, values in reference_settings["groups"].items()},
        )
    return references


def load_reference_metadata(settings: dict[str, Any]) -> dict[str, ReferenceRaster]:
    """Load CTrees reference schemas without opening Earth Engine raster assets."""
    references: dict[str, ReferenceRaster] = {}
    for name, reference_settings in settings["references"].items():
        if reference_settings.get("interpretation_scheme") == "fcbm_vt0007_table15":
            reference_settings = {
                **reference_settings,
                "class_codes": sorted(FCBM_VT0007_CLASS_LABELS),
                "class_labels": FCBM_VT0007_CLASS_LABELS,
                "groups": {
                    "forest": ["Stable forest"],
                    "nonforest": ["Stable non-forest"],
                    "change": [
                        "Deforested in first half of HRP",
                        "Deforested in second half of HRP",
                    ],
                    "excluded": [],
                },
            }
        references[name] = ReferenceRaster(
            name=name,
            label=str(reference_settings.get("label", name)),
            image=None,
            class_codes=[int(code) for code in reference_settings["class_codes"]],
            class_labels={int(code): str(label) for code, label in reference_settings["class_labels"].items()},
            groups={key: [str(value) for value in values] for key, values in reference_settings["groups"].items()},
        )
    return references


def _interpret_fcbm_vt0007(image: ee.Image) -> ee.Image:
    """Apply VMD0055 v1.1 Table 15 to raw FCBM transition values."""
    return image.remap(
        list(FCBM_VT0007_REMAP),
        [FCBM_VT0007_REMAP[index] for index in FCBM_VT0007_REMAP],
        0,
    )


def _assert_readable_asset(asset_id: str, label: str) -> None:
    try:
        ee.data.getAsset(asset_id)
    except Exception as exc:
        raise RuntimeError(f"{label} asset is missing or unreadable: {asset_id}") from exc
