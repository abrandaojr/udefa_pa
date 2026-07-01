"""Earth Engine table exports for the analytical workflow."""

from __future__ import annotations

import logging
import time
from typing import Any

import ee

from .data_preparation import PreparedInputs, select_mapbiomas_year
from .constants import CROSS_TABULATION_YEARS, CTREES_YEARS, PRIMARY_MAPBIOMAS_YEARS
from .models import ReferenceRaster
from .models import OrganizedData
from .raster_exports import _udef_a_fcbm_products

LOGGER = logging.getLogger(__name__)


def submit_table_exports(
    settings: dict[str, Any],
    persistence_rasters: dict[str, ee.Image],
    references: dict[str, ReferenceRaster],
    pixel_area_hectares: ee.Image,
    area_of_interest: ee.FeatureCollection,
) -> list[ee.batch.Task]:
    """Submit area and cross-tabulation CSV exports to Google Drive."""
    export_folder = settings["google"]["drive_subfolders"]["exports"]
    scale = int(settings["earth_engine"]["scale_native_m"])
    max_pixels = int(settings["earth_engine"]["max_pixels"])
    pending = _pending_task_descriptions()
    tasks: list[ee.batch.Task] = []

    for scenario_label, raster in persistence_rasters.items():
        area_name = f"Area_30m_{scenario_label}"
        if area_name not in pending:
            tasks.append(_start_table_export(area_name, export_folder, _area_by_class(
                raster, pixel_area_hectares, area_of_interest, scale, max_pixels
            )))

        for reference_name, reference in references.items():
            if reference.image is None:
                raise RuntimeError(f"Reference raster was not loaded: {reference_name}")
            export_name = f"XTab_30m_{scenario_label}_x_{reference_name}"
            if export_name in pending:
                continue
            tasks.append(_start_table_export(export_name, export_folder, _cross_tabulation(
                raster, reference.image, area_of_interest, scale, max_pixels
            )))

    return tasks


def submit_fcbm_comparison_exports(
    settings: dict[str, Any],
    prepared: PreparedInputs,
    organized: OrganizedData,
) -> list[ee.batch.Task]:
    """Submit CTrees FCBM versus MB-FCBM cross-tabulation exports."""
    export_folder = settings["google"]["drive_subfolders"]["exports"]
    scale = int(settings["earth_engine"]["scale_native_m"])
    max_pixels = int(settings["earth_engine"]["max_pixels"])
    pending = _pending_task_descriptions()
    products = {product.name: product for product in _udef_a_fcbm_products(settings, prepared, organized)}
    tasks: list[ee.batch.Task] = []

    for ctrees_name, ctrees_product in products.items():
        if "CTrees" not in ctrees_name:
            continue
        mapbiomas_name = ctrees_name.replace("CTrees", "MapBiomas", 1)
        mapbiomas_product = products.get(mapbiomas_name)
        if mapbiomas_product is None:
            continue
        prefix = "FCBM_XTab" if "FCBM_Index" in ctrees_name else "DerivedBinary_XTab"
        export_name = f"{prefix}_30m_{ctrees_name}_x_{mapbiomas_name}"
        if export_name in pending:
            continue
        tasks.append(
            _start_table_export(
                export_name,
                export_folder,
                _paired_cross_tabulation(
                    ctrees_product.image,
                    mapbiomas_product.image,
                    prepared.area_of_interest,
                    scale,
                    max_pixels,
                    100,
                ),
            )
        )
    tasks.extend(_submit_risk_map_binary_exports(settings, prepared, organized, products, pending))
    tasks.extend(_submit_spatial_unit_area_exports(settings, prepared, products, pending))
    tasks.extend(_submit_prompt_analysis_exports(settings, prepared, organized, pending))
    return tasks


def _submit_prompt_analysis_exports(
    settings: dict[str, Any],
    prepared: PreparedInputs,
    organized: OrganizedData,
    pending: set[str],
) -> list[ee.batch.Task]:
    """Submit the additional analytical exports required by the full prompt."""
    export_folder = settings["google"]["drive_subfolders"]["exports"]
    scale = int(settings["earth_engine"]["scale_native_m"])
    max_pixels = int(settings["earth_engine"]["max_pixels"])
    pixel_area = prepared.pixel_area_hectares
    forest_codes = [int(code) for code in settings["analysis"]["forest_codes"]]
    tasks: list[ee.batch.Task] = []
    ctrees_scheme_a = _ctrees_scheme_a_image(settings, organized)

    for year in CROSS_TABULATION_YEARS:
        if ctrees_scheme_a is None:
            continue
        mapbiomas_lulc = select_mapbiomas_year(prepared.mapbiomas_image, year).clip(prepared.area_of_interest)
        mapbiomas_binary = mapbiomas_lulc.remap(forest_codes, [1] * len(forest_codes), 0).toByte()
        ctrees_binary = ctrees_scheme_a.eq(2).toByte().rename("value")
        all_class_name = f"AllClass_XTab_30m_CTrees_{year}_x_MapBiomas_LULC_{year}"
        if all_class_name not in pending:
            tasks.append(_start_table_export(
                all_class_name,
                export_folder,
                _paired_cross_tabulation(ctrees_scheme_a, mapbiomas_lulc, prepared.area_of_interest, scale, max_pixels, 100),
            ))
        binary_name = f"Binary_XTab_30m_CTrees_{year}_x_MapBiomas_Binary_{year}"
        if binary_name not in pending:
            tasks.append(_start_table_export(
                binary_name,
                export_folder,
                _paired_cross_tabulation(ctrees_binary, mapbiomas_binary, prepared.area_of_interest, scale, max_pixels, 10),
            ))
        if prepared.spatial_units is not None:
            spatial_name = f"SpatialDisagreement_30m_CTrees_{year}_x_MapBiomas_Binary_{year}"
            if spatial_name not in pending:
                task = ee.batch.Export.table.toDrive(
                    collection=_spatial_disagreement_by_unit(
                        ctrees_binary,
                        mapbiomas_binary,
                        pixel_area,
                        prepared.spatial_units,
                        scale,
                        settings,
                    ),
                    description=spatial_name,
                    folder=export_folder,
                    fileNamePrefix=spatial_name,
                    fileFormat="CSV",
                )
                task.start()
                LOGGER.info("Submitted %s", spatial_name)
                tasks.append(task)

    for start_year, end_year in _available_change_intervals(organized):
        ctrees_start = _ctrees_binary_for_year(organized, start_year)
        ctrees_end = _ctrees_binary_for_year(organized, end_year)
        if ctrees_start is None or ctrees_end is None:
            continue
        mb_start = select_mapbiomas_year(prepared.mapbiomas_image, start_year).remap(forest_codes, [1] * len(forest_codes), 0)
        mb_end = select_mapbiomas_year(prepared.mapbiomas_image, end_year).remap(forest_codes, [1] * len(forest_codes), 0)
        ctrees_change = _transition_class(ctrees_start, ctrees_end)
        mb_change = _transition_class(mb_start, mb_end)
        change_name = f"ChangeAgreement_30m_CTrees_{start_year}_{end_year}_x_MapBiomas_{start_year}_{end_year}"
        if change_name not in pending:
            tasks.append(_start_table_export(
                change_name,
                export_folder,
                _paired_cross_tabulation(ctrees_change, mb_change, prepared.area_of_interest, scale, max_pixels, 10),
            ))
        area_name = f"ChangeAreaTimeSeries_30m_CTrees_x_MapBiomas_{start_year}_{end_year}"
        if area_name not in pending:
            tasks.append(_start_table_export(
                area_name,
                export_folder,
                _change_area_timeseries(ctrees_change, mb_change, pixel_area, prepared.area_of_interest, scale, max_pixels),
            ))

    reversal_name = "TemporalReversal_30m_CTrees_x_MapBiomas"
    if reversal_name not in pending:
        tasks.append(_start_table_export(
            reversal_name,
            export_folder,
            _temporal_reversal_summary(settings, prepared, organized, scale, max_pixels),
        ))
    return tasks


def _ctrees_scheme_a_image(settings: dict[str, Any], organized: OrganizedData) -> ee.Image | None:
    reference_name = str(settings.get("udef_a", {}).get("ctrees_fcbm_reference", "FCBM4"))
    reference = organized.references.get(reference_name)
    if reference is None or reference.image is None:
        LOGGER.warning("CTrees Scheme A reference %s is unavailable.", reference_name)
        return None
    return reference.image.toByte().rename("value")


def _submit_risk_map_binary_exports(
    settings: dict[str, Any],
    prepared: PreparedInputs,
    organized: OrganizedData,
    products: dict[str, Any],
    pending: set[str],
) -> list[ee.batch.Task]:
    """Submit MapBiomas binary forest/non-forest agreement exports for the three UDef-A risk maps."""
    export_folder = settings["google"]["drive_subfolders"]["exports"]
    scale = int(settings["earth_engine"]["scale_native_m"])
    max_pixels = int(settings["earth_engine"]["max_pixels"])
    forest_codes = [int(code) for code in settings["analysis"]["forest_codes"]]
    hrp_years = settings.get("udef_a", {}).get("hrp_years", {})
    risk_maps = [
        ("TestRisk", "T1", int(hrp_years.get("t1", 2009))),
        ("HRPRisk", "T2", int(hrp_years.get("t2", 2013))),
        ("ValidityRisk", "T3", int(hrp_years.get("t3", 2018))),
    ]
    tasks: list[ee.batch.Task] = []
    for risk_label, period, year in risk_maps:
        ctrees_name = f"UDefA_CTrees_Forest_{period}_{year}_30m"
        ctrees_product = products.get(ctrees_name)
        if ctrees_product is None:
            continue
        mapbiomas_binary = (
            select_mapbiomas_year(prepared.mapbiomas_image, year)
            .remap(forest_codes, [1] * len(forest_codes), 0)
            .updateMask(organized.valid_analysis_mask)
            .clip(prepared.area_of_interest)
            .toByte()
            .rename("value")
        )
        export_name = f"RiskMap_XTab_30m_CTrees_{risk_label}_Forest_{period}_{year}_x_MapBiomas_Binary_Forest_{year}"
        if export_name in pending:
            continue
        tasks.append(
            _start_table_export(
                export_name,
                export_folder,
                _paired_cross_tabulation(
                    ctrees_product.image,
                    mapbiomas_binary,
                    prepared.area_of_interest,
                    scale,
                    max_pixels,
                    100,
                ),
            )
        )
    return tasks


def _submit_spatial_unit_area_exports(
    settings: dict[str, Any],
    prepared: PreparedInputs,
    products: dict[str, Any],
    pending: set[str],
) -> list[ee.batch.Task]:
    """Submit class-area summaries by configured IBGE municipal polygons."""
    if prepared.spatial_units is None:
        return []

    export_folder = settings["google"]["drive_subfolders"]["exports"]
    scale = int(settings["earth_engine"]["scale_native_m"])
    tasks: list[ee.batch.Task] = []
    for product_name, product in sorted(products.items()):
        export_name = f"MunicipalArea_30m_{product_name}"
        if export_name in pending:
            continue
        task = ee.batch.Export.table.toDrive(
            collection=_area_by_spatial_unit(
                product.image,
                prepared.pixel_area_hectares,
                prepared.spatial_units,
                scale,
                settings,
            ),
            description=export_name,
            folder=export_folder,
            fileNamePrefix=export_name,
            fileFormat="CSV",
        )
        task.start()
        LOGGER.info("Submitted %s", export_name)
        tasks.append(task)
    return tasks


def wait_for_tasks(tasks: list[ee.batch.Task], poll_interval_seconds: int = 60) -> None:
    """Wait until all submitted Earth Engine tasks complete."""
    if not tasks:
        LOGGER.info("No Earth Engine tasks were submitted.")
        return

    while True:
        statuses = [task.status() for task in tasks]
        counts: dict[str, int] = {}
        for status in statuses:
            state = str(status.get("state", "UNKNOWN"))
            counts[state] = counts.get(state, 0) + 1
        LOGGER.info("Earth Engine task status: %s", counts)

        failed = [status for status in statuses if status.get("state") in {"FAILED", "CANCELLED"}]
        if failed:
            for status in failed:
                LOGGER.error("%s: %s", status.get("description"), status.get("error_message", ""))
            raise RuntimeError("One or more Earth Engine table exports failed.")

        if all(status.get("state") in {"COMPLETED", "FAILED", "CANCELLED"} for status in statuses):
            return
        time.sleep(poll_interval_seconds)


def _start_table_export(name: str, folder: str, values: ee.Dictionary) -> ee.batch.Task:
    task = ee.batch.Export.table.toDrive(
        collection=ee.FeatureCollection([ee.Feature(None, values)]),
        description=name,
        folder=folder,
        fileNamePrefix=name,
        fileFormat="CSV",
    )
    task.start()
    LOGGER.info("Submitted %s", name)
    return task


def _area_by_class(
    raster: ee.Image,
    pixel_area_hectares: ee.Image,
    area_of_interest: ee.FeatureCollection,
    scale: int,
    max_pixels: int,
) -> ee.Dictionary:
    combined = pixel_area_hectares.updateMask(raster.mask()).addBands(raster)
    return combined.reduceRegion(
        geometry=area_of_interest.geometry(),
        reducer=ee.Reducer.sum().group(groupField=1, groupName="class"),
        scale=scale,
        maxPixels=max_pixels,
    )


def _area_by_spatial_unit(
    raster: ee.Image,
    pixel_area_hectares: ee.Image,
    spatial_units: ee.FeatureCollection,
    scale: int,
    settings: dict[str, Any],
) -> ee.FeatureCollection:
    spatial_settings = settings.get("analysis", {}).get("spatial_units", {})
    id_property = str(spatial_settings.get("id_property", "CD_GEOCMU"))
    name_property = str(spatial_settings.get("name_property", "NM_MUNICIP"))
    combined = pixel_area_hectares.updateMask(raster.mask()).addBands(raster)
    reduced = combined.reduceRegions(
        collection=spatial_units,
        reducer=ee.Reducer.sum().group(groupField=1, groupName="class"),
        scale=scale,
        tileScale=8,
    )
    return reduced.map(
        lambda feature: feature.set(
            {
                "municipality_id": feature.get(id_property),
                "municipality_name": feature.get(name_property),
            }
        )
    )


def _spatial_disagreement_by_unit(
    ctrees_binary: ee.Image,
    mapbiomas_binary: ee.Image,
    pixel_area_hectares: ee.Image,
    spatial_units: ee.FeatureCollection,
    scale: int,
    settings: dict[str, Any],
) -> ee.FeatureCollection:
    spatial_settings = settings.get("analysis", {}).get("spatial_units", {})
    id_property = str(spatial_settings.get("id_property", "CD_GEOCMU"))
    name_property = str(spatial_settings.get("name_property", "NM_MUNICIP"))
    valid = ctrees_binary.mask().And(mapbiomas_binary.mask())
    disagreement_area = pixel_area_hectares.updateMask(valid).updateMask(ctrees_binary.neq(mapbiomas_binary)).rename("disagreement_hectares")
    evaluated_area = pixel_area_hectares.updateMask(valid).rename("evaluated_hectares")
    combined = evaluated_area.addBands(disagreement_area)
    reduced = combined.reduceRegions(
        collection=spatial_units,
        reducer=ee.Reducer.sum(),
        scale=scale,
        tileScale=8,
    )
    return reduced.map(
        lambda feature: feature.set(
            {
                "municipality_id": feature.get(id_property),
                "municipality_name": feature.get(name_property),
                "disagreement_percent": ee.Number(feature.get("disagreement_hectares"))
                .divide(ee.Number(feature.get("evaluated_hectares")))
                .multiply(100),
            }
        )
    )


def _cross_tabulation(
    persistence_raster: ee.Image,
    reference_raster: ee.Image,
    area_of_interest: ee.FeatureCollection,
    scale: int,
    max_pixels: int,
) -> ee.Dictionary:
    encoded = persistence_raster.multiply(10).add(reference_raster)
    return encoded.reduceRegion(
        geometry=area_of_interest.geometry(),
        reducer=ee.Reducer.frequencyHistogram(),
        scale=scale,
        maxPixels=max_pixels,
    )


def _paired_cross_tabulation(
    row_raster: ee.Image,
    column_raster: ee.Image,
    area_of_interest: ee.FeatureCollection,
    scale: int,
    max_pixels: int,
    multiplier: int,
) -> ee.Dictionary:
    encoded = row_raster.multiply(multiplier).add(column_raster)
    return encoded.reduceRegion(
        geometry=area_of_interest.geometry(),
        reducer=ee.Reducer.frequencyHistogram(),
        scale=scale,
        maxPixels=max_pixels,
    )


def _ctrees_binary_for_year(organized: OrganizedData, year: int) -> ee.Image | None:
    for name in (f"FCBM{ {2009: 1, 2013: 2, 2018: 3}.get(year, '') }_{year}", f"FCBM_{year}", str(year)):
        reference = organized.references.get(name)
        if reference is not None and reference.image is not None:
            return reference.image.eq(1).toByte().rename("value")
    for name, reference in organized.references.items():
        if str(year) in name and reference.image is not None and set(reference.class_codes).issubset({0, 1}):
            return reference.image.eq(1).toByte().rename("value")
    return None


def _available_change_intervals(organized: OrganizedData) -> list[tuple[int, int]]:
    years = [year for year in CTREES_YEARS if _ctrees_binary_for_year(organized, year) is not None]
    return [(first, second) for first, second in zip(years, years[1:])]


def _transition_class(start_forest: ee.Image, end_forest: ee.Image) -> ee.Image:
    return (
        ee.Image(2)
        .where(start_forest.eq(1).And(end_forest.eq(1)), 1)
        .where(start_forest.eq(1).And(end_forest.eq(0)), 3)
        .where(start_forest.eq(0).And(end_forest.eq(1)), 4)
        .updateMask(start_forest.mask().And(end_forest.mask()))
        .toByte()
        .rename("value")
    )


def _change_area_timeseries(
    ctrees_change: ee.Image,
    mapbiomas_change: ee.Image,
    pixel_area_hectares: ee.Image,
    area_of_interest: ee.FeatureCollection,
    scale: int,
    max_pixels: int,
) -> ee.Dictionary:
    ctrees_loss = pixel_area_hectares.updateMask(ctrees_change.eq(3)).reduceRegion(
        geometry=area_of_interest.geometry(),
        reducer=ee.Reducer.sum(),
        scale=scale,
        maxPixels=max_pixels,
        tileScale=8,
    ).get("area_hectares")
    mapbiomas_loss = pixel_area_hectares.updateMask(mapbiomas_change.eq(3)).reduceRegion(
        geometry=area_of_interest.geometry(),
        reducer=ee.Reducer.sum(),
        scale=scale,
        maxPixels=max_pixels,
        tileScale=8,
    ).get("area_hectares")
    return ee.Dictionary({"ctrees_loss_hectares": ctrees_loss, "mapbiomas_loss_hectares": mapbiomas_loss})


def _temporal_reversal_summary(
    settings: dict[str, Any],
    prepared: PreparedInputs,
    organized: OrganizedData,
    scale: int,
    max_pixels: int,
) -> ee.Dictionary:
    forest_codes = [int(code) for code in settings["analysis"]["forest_codes"]]
    pixel_area = prepared.pixel_area_hectares
    ctrees_reversal = _ctrees_reversal_image(organized)
    mapbiomas_series = [
        select_mapbiomas_year(prepared.mapbiomas_image, year).remap(forest_codes, [1] * len(forest_codes), 0).toByte()
        for year in PRIMARY_MAPBIOMAS_YEARS
    ]
    mapbiomas_reversal = _reversal_image(mapbiomas_series)
    return ee.Dictionary(
        {
            "ctrees_reversal_hectares": _area_of_binary(ctrees_reversal, pixel_area, prepared.area_of_interest, scale, max_pixels),
            "mapbiomas_reversal_hectares": _area_of_binary(mapbiomas_reversal, pixel_area, prepared.area_of_interest, scale, max_pixels),
        }
    )


def _ctrees_reversal_image(organized: OrganizedData) -> ee.Image:
    series = [_ctrees_binary_for_year(organized, year) for year in CTREES_YEARS]
    return _reversal_image([image for image in series if image is not None])


def _reversal_image(series: list[ee.Image]) -> ee.Image:
    if len(series) < 3:
        return ee.Image(0).toByte().rename("value")
    reversals = ee.Image(0)
    for previous, current, following in zip(series, series[1:], series[2:]):
        reversals = reversals.Or(previous.eq(following).And(current.neq(previous)))
    return reversals.toByte().rename("value")


def _area_of_binary(
    binary: ee.Image,
    pixel_area_hectares: ee.Image,
    area_of_interest: ee.FeatureCollection,
    scale: int,
    max_pixels: int,
) -> ee.Number:
    return ee.Number(
        pixel_area_hectares.updateMask(binary.eq(1)).reduceRegion(
            geometry=area_of_interest.geometry(),
            reducer=ee.Reducer.sum(),
            scale=scale,
            maxPixels=max_pixels,
            tileScale=8,
        ).get("area_hectares")
    )


def _pending_task_descriptions() -> set[str]:
    try:
        return {
            str(task["description"])
            for task in ee.data.getTaskList()
            if task.get("state") in {"READY", "RUNNING"}
        }
    except Exception:
        return set()
