"""Raster products, aligned image exports, and IDRISI conversion."""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import math
from pathlib import Path
import re
import struct
import time
from typing import Any
import warnings

import ee

def build_geotiff_product_mosaics(input_directory, output_directory):
    """Public wrapper for product-level GeoTIFF mosaics."""
    mosaics = build_geotiff_mosaics(input_directory, output_directory)
    if not mosaics:
        raise RuntimeError(
            f"No GeoTIFF mosaics were created in {output_directory}. "
            f"Input directory was {input_directory}."
        )
    return mosaics



def _binary_change_class_image(start_forest: ee.Image, end_forest: ee.Image) -> ee.Image:
    """Encode binary forest change: 1 stable forest, 2 stable non-forest, 3 loss, 4 gain."""
    return (
        ee.Image(2)
        .where(start_forest.eq(1).And(end_forest.eq(1)), 1)
        .where(start_forest.eq(1).And(end_forest.eq(0)), 3)
        .where(start_forest.eq(0).And(end_forest.eq(1)), 4)
        .updateMask(start_forest.mask().And(end_forest.mask()))
        .toByte()
        .rename("change_class")
    )


def _ctrees_binary_forest_image_for_year(organized, year: int) -> ee.Image | None:
    """Return a CTrees binary forest image for the requested snapshot year."""
    reference_names = {
        2009: ("FCBM1_2009", "FCBM1", "CTrees_FCBM1_2009"),
        2013: ("FCBM2_2013", "FCBM2", "CTrees_FCBM2_2013"),
        2018: ("FCBM3_2018", "FCBM3", "CTrees_FCBM3_2018"),
    }
    for reference_name in reference_names.get(year, ()):
        reference = organized.references.get(reference_name)
        if reference is not None and reference.image is not None:
            return reference.image.eq(1).toByte().rename("forest")
    return None


def _safe_export_name(name: str) -> str:
    """Return a string safe for use in Earth Engine export names."""
    return re.sub(r"[^A-Za-z0-9_\-]", "_", str(name).strip()).strip("_")


def _ctrees_export_name(reference_name: str, suffix: str = "") -> str:
    """Return a clear GeoTIFF export name for CTrees source rasters."""
    clean_name = str(reference_name).strip()
    aliases = {
        "DMJSS": "CTrees_DMJSS_InitialDeforestationMap",
        "FCBM1": "CTrees_FCBM1_ForestCover_2009",
        "FCBM1_2009": "CTrees_FCBM1_ForestCover_2009",
        "FCBM2": "CTrees_FCBM2_ForestCover_2013",
        "FCBM2_2013": "CTrees_FCBM2_ForestCover_2013",
        "FCBM3": "CTrees_FCBM3_ForestCover_2018",
        "FCBM3_2018": "CTrees_FCBM3_ForestCover_2018",
    }
    base = aliases.get(clean_name, f"CTrees_{_safe_export_name(clean_name)}")
    if suffix:
        return f"{base}_{_safe_export_name(suffix)}"
    return base


def _projection_suffix(settings: dict[str, Any]) -> str:
    """Return a compact projection suffix for GeoTIFF export names."""
    grid = settings.get("grid", {})
    crs = str(grid.get("crs") or settings.get("earth_engine", {}).get("crs") or "unknown_crs")
    scale = grid.get("scale_m") or settings.get("earth_engine", {}).get("scale_native_m") or "unknown"
    crs_label = crs.replace(":", "_").replace("/", "_")
    return f"{crs_label}_{scale}m"


def _with_projection_suffix(name: str, settings: dict[str, Any]) -> str:
    """Append projection information to a GeoTIFF export name unless already present."""
    suffix = _projection_suffix(settings)
    clean_name = _safe_export_name(name)
    if clean_name.endswith(suffix):
        return clean_name
    return f"{clean_name}_{suffix}"


def _geotiff_export_kwargs(product: RasterProduct, settings: dict[str, Any], folder: str, region: ee.Geometry) -> dict[str, Any]:
    """Return explicit Earth Engine GeoTIFF export arguments for one mosaic-ready product."""
    grid = settings.get("grid", {})
    earth_engine = settings.get("earth_engine", {})
    crs = grid.get("crs") or earth_engine.get("crs")
    scale = grid.get("scale_m") or earth_engine.get("scale_native_m")
    if not crs or not scale:
        raise RuntimeError("GeoTIFF export requires explicit grid.crs and grid.scale_m settings.")

    return {
        "image": product.image,
        "description": _with_projection_suffix(product.name, settings),
        "folder": folder,
        "fileNamePrefix": _with_projection_suffix(product.name, settings),
        "region": region,
        "scale": float(scale),
        "crs": str(crs),
        "maxPixels": int(grid.get("max_pixels") or earth_engine.get("max_pixels") or 10_000_000_000_000),
        "fileFormat": "GeoTIFF",
        "formatOptions": {"cloudOptimized": False},
    }


def start_geotiff_export_tasks(products: list[RasterProduct], settings: dict[str, Any], folder: str, region: ee.Geometry) -> list[Any]:
    """Start one explicit GeoTIFF export task per product."""
    tasks = []
    for product in products:
        task = ee.batch.Export.image.toDrive(
            **_geotiff_export_kwargs(product, settings, folder, region)
        )
        task.start()
        tasks.append(task)
    return tasks


def _write_geotiff_projection_manifest(products: list[RasterProduct], settings: dict[str, Any], output_directory: Path) -> None:
    """Write a manifest documenting the intended projection of all exported GeoTIFFs."""
    grid = settings.get("grid", {})
    crs = str(grid.get("crs") or settings.get("earth_engine", {}).get("crs") or "")
    scale = grid.get("scale_m") or settings.get("earth_engine", {}).get("scale_native_m")
    anchor_asset = str(grid.get("anchor_asset") or "")
    output_directory.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(
        [
            {
                "geotiff_name": product.name,
                "crs": crs,
                "pixel_size_m": scale,
                "grid_anchor_asset": anchor_asset,
                "projection_note": f"Exported on common grid {crs} at {scale} m pixels.",
            }
            for product in products
        ]
    ).to_csv(output_directory / "geotiff_projection_manifest.csv", index=False)


def _forest_loss_cross_image(ctrees_loss: ee.Image, mapbiomas_loss: ee.Image) -> ee.Image:
    """Encode forest-loss cross map: 1 agreement, 2 CTrees only, 3 MapBiomas only."""
    return (
        ee.Image(0)
        .where(ctrees_loss.And(mapbiomas_loss), 1)
        .where(ctrees_loss.And(mapbiomas_loss.Not()), 2)
        .where(mapbiomas_loss.And(ctrees_loss.Not()), 3)
        .updateMask(ctrees_loss.Or(mapbiomas_loss))
        .toByte()
        .rename("forest_loss_cross")
    )
import numpy as np
import pandas as pd
import rasterio
from rasterio.crs import CRS
from rasterio.merge import merge
from rasterio.warp import Resampling, calculate_default_transform, reproject
from rasterio.windows import Window

from .constants import (
    CROSS_TABULATION_YEARS,
    DMJSS_BUFFER_DISTANCE_M,
    FCBM_ACCURACY_REMAP,
    FCBM_RISK_INDEX_GROUPS,
    FCBM_VT0007_REMAP,
    MAPBIOMAS_CLASS_COLORS,
    MAPBIOMAS_LAND_COVER_CLASSES,
    MB_FCBM_TRANSITION_RULES,
    PRIMARY_MAPBIOMAS_YEARS,
)
from .data_preparation import PreparedInputs, resolve_mapbiomas_year, select_mapbiomas_year
from .models import OrganizedData
from .raster_naming import preferred_raster_product_stem, raster_product_stem, raster_semantic_key
from .settings import Scenario

LOGGER = logging.getLogger(__name__)


_YEAR_TO_TX: dict[int, str] = {1985: "T0_1985", 2009: "T1_2009", 2013: "T2_2013", 2018: "T3_2018", 2024: "T4_2024"}

_PERIOD_LABEL: dict[tuple[int, int], str] = {
    (1985, 2009): "Hist_1985_2009",
    (2009, 2013): "Cal_2009_2013",
    (2013, 2018): "Con_2013_2018",
    (2018, 2024): "T3T4_2018_2024",
    (1985, 2024): "T0T4_1985_2024",
}

_REFERENCE_UDEFA_NAME: dict[str, str] = {
    "DMJSS":     "UDefA_Ct_DMJSS_2009_2018",
    "FCBM1":     "UDefA_Ct_Forest_T1_2009",
    "FCBM1_2009":"UDefA_Ct_Forest_T1_2009",
    "FCBM2":     "UDefA_Ct_Forest_T2_2013",
    "FCBM2_2013":"UDefA_Ct_Forest_T2_2013",
    "FCBM3":     "UDefA_Ct_Forest_T3_2018",
    "FCBM3_2018":"UDefA_Ct_Forest_T3_2018",
    "FCBM4":     "UDefA_Ct_FCBM4",
}


@dataclass(frozen=True)
class RasterProduct:
    """One aligned raster product used in the analysis."""

    name: str
    image: ee.Image
    description: str


def build_raster_products(
    settings: dict[str, Any],
    scenarios: list[Scenario],
    prepared: PreparedInputs,
    organized: OrganizedData,
) -> list[RasterProduct]:
    """Build all rasters that should be inspectable outside Earth Engine."""
    valid_mask = organized.valid_analysis_mask.toByte().rename("value")
    products = [
        RasterProduct("UDefA_ValidMask", valid_mask, "Valid analysis area mask"),
    ]

    forest_codes = [int(code) for code in settings["analysis"]["forest_codes"]]
    for year in PRIMARY_MAPBIOMAS_YEARS:
        tx = _YEAR_TO_TX.get(year, f"T{year}")
        mapbiomas_forest = (
            select_mapbiomas_year(prepared.mapbiomas_image, year)
            .remap(forest_codes, [1] * len(forest_codes), 0)
            .clip(prepared.area_of_interest)
            .toByte()
            .rename("value")
        )
        products.append(
            RasterProduct(
                name=_with_projection_suffix(f"UDefA_MB_LULC_{tx}", settings),
                image=select_mapbiomas_year(prepared.mapbiomas_image, year)
                .clip(prepared.area_of_interest)
                .toInt16()
                .rename("value"),
                description=f"MapBiomas Collection 10 land-cover classes, {year}",
            )
        )
        products.append(
            RasterProduct(
                name=_with_projection_suffix(f"UDefA_MB_Forest_{tx}", settings),
                image=mapbiomas_forest,
                description=f"MapBiomas binary forest/non-forest, {year}",
            )
        )

    for scenario_label, image in sorted(organized.persistence_rasters.items()):
        products.append(
            RasterProduct(
                name=_with_projection_suffix(
                    f"UDefA_MB_Persistence_Scen{_safe_export_name(scenario_label)}", settings
                ),
                image=image.clip(prepared.area_of_interest).toByte().rename("value"),
                description=f"MapBiomas forest-persistence scenario {scenario_label}",
            )
        )

    for start_year, end_year in zip(PRIMARY_MAPBIOMAS_YEARS, PRIMARY_MAPBIOMAS_YEARS[1:]):
        period = _PERIOD_LABEL.get((start_year, end_year), f"{start_year}_{end_year}")
        mapbiomas_start = (
            select_mapbiomas_year(prepared.mapbiomas_image, start_year)
            .remap(forest_codes, [1] * len(forest_codes), 0)
            .clip(prepared.area_of_interest)
            .toByte()
        )
        mapbiomas_end = (
            select_mapbiomas_year(prepared.mapbiomas_image, end_year)
            .remap(forest_codes, [1] * len(forest_codes), 0)
            .clip(prepared.area_of_interest)
            .toByte()
        )
        mapbiomas_change = _binary_change_class_image(mapbiomas_start, mapbiomas_end)
        mapbiomas_loss = mapbiomas_change.eq(3).toByte().rename("value")
        products.append(
            RasterProduct(
                name=_with_projection_suffix(f"UDefA_MB_ForestChange4_{period}", settings),
                image=mapbiomas_change,
                description=f"MapBiomas 4-class forest change, {start_year}-{end_year}",
            )
        )

        ctrees_start = _ctrees_binary_forest_image_for_year(organized, start_year)
        ctrees_end = _ctrees_binary_forest_image_for_year(organized, end_year)
        if ctrees_start is None or ctrees_end is None:
            continue
        ctrees_change = _binary_change_class_image(
            ctrees_start.clip(prepared.area_of_interest).toByte(),
            ctrees_end.clip(prepared.area_of_interest).toByte(),
        )
        ctrees_loss = ctrees_change.eq(3).toByte().rename("value")
        products.append(
            RasterProduct(
                name=_with_projection_suffix(f"UDefA_Ct_ForestChange4_{period}", settings),
                image=ctrees_change,
                description=f"CTrees 4-class forest change, {start_year}-{end_year}",
            )
        )
        products.append(
            RasterProduct(
                name=_with_projection_suffix(f"UDefA_Ct_MB_Agreement_{period}", settings),
                image=_forest_loss_cross_image(ctrees_loss, mapbiomas_loss),
                description=f"CTrees vs MapBiomas forest loss agreement map, {start_year}-{end_year}",
            )
        )

    for name, reference in organized.references.items():
        if reference.image is not None:
            udefa_name = _REFERENCE_UDEFA_NAME.get(name, f"UDefA_Ct_{_safe_export_name(name)}")
            products.append(
                RasterProduct(
                    udefa_name,
                    reference.image.toInt16().rename("value"),
                    f"CTrees reference raster: {reference.label}",
                )
            )

    products.extend(_forest_to_nonforest_products(settings, prepared, organized))
    products.extend(_udef_a_fcbm_products(settings, prepared, organized))
    products.extend(_dmjss_mb_products(settings, prepared, organized))
    return _normalize_raster_products(products, settings)


def _normalize_raster_products(products: list[RasterProduct], settings: dict[str, Any]) -> list[RasterProduct]:
    """Return raster products with one canonical EPSG/resolution suffix."""
    normalized: list[RasterProduct] = []
    seen: set[str] = set()
    for product in products:
        name = _with_projection_suffix(product.name, settings)
        if name in seen:
            LOGGER.debug("Skipping duplicate raster product after normalization: %s", name)
            continue
        seen.add(name)
        normalized.append(RasterProduct(name=name, image=product.image, description=product.description))
    return normalized


def write_change_area_tables(
    settings: dict[str, Any],
    prepared: PreparedInputs,
    organized: OrganizedData,
    table_directory: Path,
) -> list[Path]:
    """Write forest-to-nonforest area tables for MapBiomas and CTrees products."""
    table_directory.mkdir(parents=True, exist_ok=True)
    try:
        ee.data.setDeadline(600000)
    except Exception:
        pass
    pixel_area = ee.Image.pixelArea().divide(10000).rename("area_hectares")
    scale = int(settings["earth_engine"]["scale_native_m"])
    max_pixels = int(settings["earth_engine"]["max_pixels"])
    rows = []
    for product in _forest_to_nonforest_products(settings, prepared, organized):
        try:
            area = (
                pixel_area.updateMask(product.image.eq(1))
                .reduceRegion(
                    reducer=ee.Reducer.sum(),
                    geometry=prepared.area_of_interest.geometry(),
                    scale=scale,
                    maxPixels=max_pixels,
                    tileScale=8,
                )
                .get("area_hectares")
                .getInfo()
            )
            hectares = float(area or 0)
        except Exception:
            LOGGER.exception("Could not compute change area for %s.", product.name)
            hectares = float("nan")
        rows.append(
            {
                "source": product.name,
                "description": product.description,
                "change_class": "Forest to non-forest",
                "area_hectares": hectares,
                "area_million_hectares": hectares / 1_000_000,
            }
        )
    path = table_directory / "change_area_forest_to_nonforest.csv"
    pd.DataFrame(rows).to_csv(path, index=False)
    return [path]


def submit_raster_exports(
    settings: dict[str, Any],
    products: list[RasterProduct],
    area_of_interest: ee.FeatureCollection,
) -> list[ee.batch.Task]:
    """Submit aligned GeoTIFF raster exports to Google Drive."""
    folder = settings["google"]["drive_subfolders"].get("rasters", "02_raster_exports")
    pending = _pending_task_descriptions()
    tasks: list[ee.batch.Task] = []
    region = area_of_interest.geometry()

    for product in products:
        export_name = product.name
        if export_name in pending:
            continue
        task = ee.batch.Export.image.toDrive(
            **_geotiff_export_kwargs(product, settings, folder, region)
        )
        task.start()
        LOGGER.info("Submitted raster export %s", export_name)
        tasks.append(task)
    return tasks


def submit_change_area_export(
    settings: dict[str, Any],
    products: list[RasterProduct],
    area_of_interest: ee.FeatureCollection,
) -> ee.batch.Task | None:
    """Submit forest-to-nonforest area table export to Google Drive."""
    export_name = "ChangeArea_ForestToNonForest_30m"
    if export_name in _pending_task_descriptions():
        LOGGER.info("Change-area export is already pending.")
        return None
    pixel_area = ee.Image.pixelArea().divide(10000).rename("area_hectares")
    scale = int(settings["earth_engine"]["scale_native_m"])
    max_pixels = int(settings["earth_engine"]["max_pixels"])
    features = []
    for product in products:
        if "ForestLoss" not in product.name:
            continue
        area = pixel_area.updateMask(product.image.eq(1)).reduceRegion(
            reducer=ee.Reducer.sum(),
            geometry=area_of_interest.geometry(),
            scale=scale,
            maxPixels=max_pixels,
            tileScale=8,
        ).get("area_hectares")
        features.append(
            ee.Feature(
                None,
                {
                    "source": product.name,
                    "description": product.description,
                    "change_class": "Forest to non-forest",
                    "area_hectares": area,
                    "area_million_hectares": ee.Number(area).divide(1_000_000),
                },
            )
        )
    if not features:
        return None
    task = ee.batch.Export.table.toDrive(
        collection=ee.FeatureCollection(features),
        description=export_name,
        folder=settings["google"]["drive_subfolders"]["exports"],
        fileNamePrefix=export_name,
        fileFormat="CSV",
    )
    task.start()
    LOGGER.info("Submitted %s", export_name)
    return task


def wait_for_raster_tasks(tasks: list[ee.batch.Task], poll_interval_seconds: int = 60) -> None:
    """Wait for submitted raster exports to finish."""
    if not tasks:
        LOGGER.info("No Earth Engine raster tasks were submitted.")
        return
    while True:
        statuses = [task.status() for task in tasks]
        counts: dict[str, int] = {}
        for status in statuses:
            state = str(status.get("state", "UNKNOWN"))
            counts[state] = counts.get(state, 0) + 1
        LOGGER.info("Raster export task status: %s", counts)
        failed = [status for status in statuses if status.get("state") in {"FAILED", "CANCELLED"}]
        if failed:
            for status in failed:
                LOGGER.error("%s: %s", status.get("description"), status.get("error_message", ""))
            raise RuntimeError("One or more Earth Engine raster exports failed.")
        if all(status.get("state") in {"COMPLETED", "FAILED", "CANCELLED"} for status in statuses):
            return
        time.sleep(poll_interval_seconds)


def _export_duration_history_path(raster_root: Path) -> Path:
    """Return the path to the local record of past export durations, used for ETA estimates."""
    return raster_root / "export_duration_history.json"


def _load_duration_history(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_duration_history(path: Path, history: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(history, indent=2), encoding="utf-8")


def _record_export_duration(history: dict[str, Any], category: str, seconds: float) -> None:
    durations: dict[str, list[float]] = history.setdefault("durations", {})
    values = durations.setdefault(category, [])
    values.append(round(seconds, 1))
    del values[:-20]


def _average_export_duration(history: dict[str, Any], category: str) -> float | None:
    values = history.get("durations", {}).get(category, [])
    if not values:
        return None
    return sum(values) / len(values)


def _format_duration(seconds: float) -> str:
    seconds = max(seconds, 0.0)
    minutes, whole_seconds = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    return f"{minutes}m{whole_seconds:02d}s"


def build_raster_status_table(
    products: list[RasterProduct],
    geotiff_directory: Path,
    idrisi_directory: Path,
    raster_root: Path,
) -> pd.DataFrame:
    """Build one status row per expected raster product.

    Reports, per product: whether the GeoTIFF mosaic and IDRISI pair already
    exist locally, whether an Earth Engine export task is queued/running/done
    for it, and (once enough export history has been recorded) an ETA based on
    the average completion time of past exports in the same product category.
    """
    history_path = _export_duration_history_path(raster_root)
    history = _load_duration_history(history_path)

    try:
        ee_tasks = ee.data.getTaskList()
    except Exception:
        ee_tasks = []
    tasks_by_name = {str(task.get("description")): task for task in ee_tasks}

    now_ms = time.time() * 1000.0
    recorded_ids = set(history.get("recorded_task_ids", []))
    history_changed = False
    rows: list[dict[str, str]] = []

    for product in products:
        name = product.name
        geotiff_present = (geotiff_directory / f"{name}.tif").exists()
        idrisi_present = _idrisi_outputs_present(name, idrisi_directory)
        task = tasks_by_name.get(name)
        category = _idrisi_product_type(name)

        if task is not None:
            task_id = str(task.get("id") or name)
            state = str(task.get("state", "UNKNOWN"))
            created_ms = task.get("creation_timestamp_ms")
            updated_ms = task.get("update_timestamp_ms")
            if state == "COMPLETED" and created_ms and updated_ms and task_id not in recorded_ids:
                _record_export_duration(history, category, (float(updated_ms) - float(created_ms)) / 1000.0)
                recorded_ids.add(task_id)
                history_changed = True

        if idrisi_present:
            status, detail = "IDRISI ready", ""
        elif geotiff_present:
            status, detail = "GeoTIFF downloaded", "IDRISI conversion pending"
        elif task is not None and str(task.get("state")) in {"READY", "RUNNING"}:
            state = str(task.get("state"))
            created_ms = task.get("creation_timestamp_ms")
            elapsed_seconds = (now_ms - float(created_ms)) / 1000.0 if created_ms else None
            average_seconds = _average_export_duration(history, category)
            status = "Earth Engine: running" if state == "RUNNING" else "Earth Engine: queued"
            if elapsed_seconds is None:
                detail = "no timing information"
            elif average_seconds is not None:
                remaining = max(average_seconds - elapsed_seconds, 0.0)
                detail = (
                    f"elapsed {_format_duration(elapsed_seconds)}, "
                    f"ETA ~{_format_duration(remaining)}"
                )
            else:
                detail = f"elapsed {_format_duration(elapsed_seconds)}, ETA unknown (no history)"
        elif task is not None and str(task.get("state")) in {"FAILED", "CANCELLED"}:
            status, detail = f"Earth Engine: {task.get('state')}", str(task.get("error_message", ""))
        else:
            status, detail = "Not submitted", ""

        rows.append({"product": name, "status": status, "detail": detail})

    if history_changed:
        history["recorded_task_ids"] = sorted(recorded_ids)[-500:]
        _save_duration_history(history_path, history)

    return pd.DataFrame(rows)


def _idrisi_outputs_present(name: str, idrisi_directory: Path) -> bool:
    rst_path = idrisi_directory / f"{name}.rst"
    rdc_path = idrisi_directory / f"{name}.rdc"
    pal_path = idrisi_directory / f"{name}.pal"
    smp_path = idrisi_directory / f"{name}.smp"
    if not rst_path.exists() or not rdc_path.exists():
        return False
    legend = _IDRISI_LEGENDS.get(_idrisi_product_type(name), [])
    return not legend or (pal_path.exists() and smp_path.exists())


def print_raster_status_table(table: pd.DataFrame) -> None:
    """Log a concise raster status summary, with the full table in verbose logs."""
    if table.empty:
        LOGGER.info("No raster products to report status for.")
        return
    counts = table["status"].value_counts().to_dict()
    LOGGER.info("Raster export status: %d product(s); %s", len(table), counts)
    LOGGER.debug("Raster export status table:\n%s", table.to_string(index=False))


def prune_duplicate_geotiff_products(directory: Path, label: str = "GeoTIFF raster") -> int:
    """Remove local GeoTIFF products that duplicate a preferred semantic raster name."""
    return _prune_duplicate_geotiffs(directory, label)


def prune_duplicate_idrisi_products(directory: Path) -> int:
    """Remove duplicate IDRISI product stems, keeping the preferred semantic name."""
    grouped: dict[str, set[str]] = {}
    if not directory.exists():
        return 0
    for path in directory.iterdir():
        if path.is_file() and path.suffix.lower() in {".rst", ".rdc", ".pal", ".smp"}:
            grouped.setdefault(raster_semantic_key(path.stem), set()).add(path.stem)

    removed = 0
    for stems in grouped.values():
        if len(stems) <= 1:
            continue
        keep_stem = preferred_raster_product_stem(stems)
        for stem in sorted(stems):
            if stem == keep_stem:
                continue
            for suffix in (".rst", ".rdc", ".pal", ".smp", ".rst.tmp", ".rdc.tmp", ".pal.tmp", ".smp.tmp"):
                path = directory / f"{stem}{suffix}"
                if path.exists():
                    path.unlink(missing_ok=True)
                    removed += 1
            LOGGER.warning("Removed duplicate IDRISI raster %s; kept %s.", stem, keep_stem)
    return removed


def ensure_idrisi_palettes(directory: Path) -> list[Path]:
    """Create or refresh IDRISI palette sidecars for existing rasters with known legends."""
    if not directory.exists():
        return []

    written: list[Path] = []
    stems = {
        path.stem
        for path in directory.iterdir()
        if path.is_file() and path.suffix.lower() in {".rst", ".rdc"}
    }
    for stem in sorted(stems):
        rst_path = directory / f"{stem}.rst"
        rdc_path = directory / f"{stem}.rdc"
        if not rst_path.exists() or not rdc_path.exists():
            continue

        pal_path = directory / f"{stem}.pal"
        smp_path = directory / f"{stem}.smp"
        pal_tmp = pal_path.with_suffix(pal_path.suffix + ".tmp")
        smp_tmp = smp_path.with_suffix(smp_path.suffix + ".tmp")
        legend = _IDRISI_LEGENDS.get(_idrisi_product_type(stem), [])
        if not legend:
            pal_path.unlink(missing_ok=True)
            smp_path.unlink(missing_ok=True)
            pal_tmp.unlink(missing_ok=True)
            smp_tmp.unlink(missing_ok=True)
            continue

        expected_text = _idrisi_pal_text(legend)
        expected_smp = _idrisi_smp_bytes(legend)
        pal_current = False
        smp_current = False
        try:
            pal_current = pal_path.exists() and pal_path.read_text(encoding="ascii") == expected_text
        except Exception:
            pass
        try:
            smp_current = smp_path.exists() and smp_path.read_bytes() == expected_smp
        except Exception:
            pass
        if not pal_current:
            pal_tmp.write_text(expected_text, encoding="ascii")
            pal_tmp.replace(pal_path)
            written.append(pal_path)
        if not smp_current:
            smp_tmp.write_bytes(expected_smp)
            smp_tmp.replace(smp_path)
            written.append(smp_path)
    return written


def generate_idrisi_raster_panel(
    idrisi_directory: Path,
    output_path: Path | None = None,
    columns: int = 4,
    thumbnail_size: tuple[int, int] = (420, 360),
) -> Path | None:
    """Render all local IDRISI rasters into one PNG panel inside the IDRISI folder."""
    if not idrisi_directory.exists():
        return None
    rst_paths = sorted(path for path in idrisi_directory.glob("*.rst") if path.with_suffix(".rdc").exists())
    if not rst_paths:
        return None

    from PIL import Image, ImageDraw, ImageFont

    output_path = output_path or (idrisi_directory / "idrisi_maps_panel.png")
    columns = max(1, int(columns))
    rows = math.ceil(len(rst_paths) / columns)
    thumb_width, thumb_height = thumbnail_size
    title_height = 78
    cell_title_height = 54
    gap = 22
    margin = 32
    cell_width = thumb_width
    cell_height = cell_title_height + thumb_height
    panel_width = margin * 2 + columns * cell_width + (columns - 1) * gap
    panel_height = margin * 2 + title_height + rows * cell_height + (rows - 1) * gap

    panel = Image.new("RGB", (panel_width, panel_height), "white")
    draw = ImageDraw.Draw(panel)
    title_font = _pil_font(ImageFont, 34, bold=True)
    cell_font = _pil_font(ImageFont, 15, bold=True)
    small_font = _pil_font(ImageFont, 12, bold=False)
    title = f"IDRISI Raster Map Panel ({len(rst_paths)} maps)"
    draw.text((margin, 24), title, fill=(116, 0, 0), font=title_font)
    draw.text((margin, 58), str(idrisi_directory), fill=(70, 70, 70), font=small_font)

    for index, rst_path in enumerate(rst_paths):
        row, col = divmod(index, columns)
        x = margin + col * (cell_width + gap)
        y = margin + title_height + row * (cell_height + gap)
        metadata = _read_idrisi_rdc(rst_path.with_suffix(".rdc"))
        image = _render_idrisi_thumbnail(rst_path, metadata, thumbnail_size)
        label = metadata.get("file title") or _idrisi_title(rst_path.stem)
        draw.rounded_rectangle(
            (x - 8, y - 8, x + cell_width + 8, y + cell_height + 8),
            radius=6,
            outline=(210, 210, 210),
            fill=(248, 248, 248),
        )
        _draw_wrapped_text(draw, label, (x, y), cell_width, cell_font, fill=(116, 0, 0), max_lines=2)
        panel.paste(image, (x, y + cell_title_height))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    panel.save(output_path)
    LOGGER.info("Generated IDRISI raster panel: %s", output_path)
    return output_path


def convert_geotiffs_to_idrisi(geotiff_directory: Path, idrisi_directory: Path) -> list[Path]:
    """Convert local GeoTIFF rasters to IDRISI .rst/.rdc pairs.

    Also removes any .rst/.rdc/.pal/.smp left over from products that no longer
    exist as GeoTIFFs, so renamed or retired products don't linger under
    their old, stale file names alongside the current ones.
    """
    idrisi_directory.mkdir(parents=True, exist_ok=True)
    _remove_non_target_geotiffs(geotiff_directory)
    _prune_duplicate_geotiffs(geotiff_directory, "GeoTIFF raster")
    prune_duplicate_idrisi_products(idrisi_directory)
    current_stems = {geotiff.stem for geotiff in _iter_target_geotiff_files(geotiff_directory)}
    for stale_path in (
        list(idrisi_directory.glob("*.rst"))
        + list(idrisi_directory.glob("*.rdc"))
        + list(idrisi_directory.glob("*.pal"))
        + list(idrisi_directory.glob("*.smp"))
    ):
        if stale_path.stem not in current_stems:
            stale_path.unlink(missing_ok=True)
    palettes = ensure_idrisi_palettes(idrisi_directory)
    if palettes:
        LOGGER.info("Created or refreshed %d IDRISI palette file(s) in %s.", len(palettes), idrisi_directory)

    written = []
    for geotiff in _iter_target_geotiff_files(geotiff_directory):
        try:
            if _idrisi_outputs_current(geotiff, idrisi_directory):
                LOGGER.debug("Skipping current IDRISI raster: %s", geotiff.stem)
                continue
            written.append(_write_idrisi_pair(geotiff, idrisi_directory))
        except Exception:
            _cleanup_idrisi_temporaries(geotiff, idrisi_directory)
            LOGGER.exception("Failed to convert %s to IDRISI format; skipping.", geotiff.name)
    return written


def build_geotiff_mosaics(geotiff_directory: Path, mosaic_directory: Path) -> list[Path]:
    """Create missing or stale LZW-compressed GeoTIFF mosaics per exported raster product."""
    mosaic_directory.mkdir(parents=True, exist_ok=True)
    _remove_non_target_geotiffs(mosaic_directory)
    _prune_duplicate_geotiffs(geotiff_directory, "GeoTIFF tile")
    _prune_duplicate_geotiffs(mosaic_directory, "GeoTIFF mosaic")
    groups: dict[str, list[Path]] = {}
    for path in _iter_target_geotiff_files(geotiff_directory):
        groups.setdefault(_geotiff_product_stem(path), []).append(path)

    mosaics: list[Path] = []
    created = 0
    skipped = 0
    for product_stem, paths in sorted(groups.items()):
        output_path = mosaic_directory / f"{product_stem}.tif"
        if _mosaic_is_current(output_path, paths):
            mosaics.append(output_path)
            skipped += 1
            LOGGER.debug("Skipping current GeoTIFF mosaic: %s", output_path.name)
            continue
        if len(paths) == 1 and paths[0].stem == product_stem:
            _copy_geotiff_lzw(paths[0], output_path)
        else:
            _write_geotiff_mosaic(paths, output_path)
        mosaics.append(output_path)
        created += 1
        LOGGER.debug("Prepared GeoTIFF mosaic %s from %d source file(s).", output_path.name, len(paths))
    if mosaics:
        LOGGER.info(
            "GeoTIFF mosaics available: %d total, %d created or refreshed, %d reused.",
            len(mosaics),
            created,
            skipped,
        )
    return mosaics


def _mosaic_is_current(output_path: Path, source_paths: list[Path]) -> bool:
    if not output_path.exists() or output_path.stat().st_size <= 0:
        return False
    if not source_paths:
        return True
    newest_source = max(path.stat().st_mtime for path in source_paths)
    return output_path.stat().st_mtime >= newest_source


def validate_common_grid(
    geotiff_directory: Path,
    expected_pixel_size_m: float = 30.0,
    expected_crs: str = "EPSG:5880",
    tolerance: float = 1e-6,
) -> pd.DataFrame:
    """Write grid metadata and fail if local GeoTIFF rasters do not share the required grid."""
    rows = []
    for path in _iter_geotiff_files(geotiff_directory):
        with rasterio.open(path) as dataset:
            rows.append(
                {
                    "file": path.name,
                    "width": dataset.width,
                    "height": dataset.height,
                    "crs": str(dataset.crs),
                    "transform": tuple(round(value, 9) for value in dataset.transform),
                    "pixel_width": abs(dataset.transform.a),
                    "pixel_height": abs(dataset.transform.e),
                }
            )
    frame = pd.DataFrame(rows)
    if not frame.empty:
        frame.to_csv(geotiff_directory.parent / "raster_grid_inventory.csv", index=False)
        _assert_common_grid(frame, expected_pixel_size_m, expected_crs, tolerance)
    return frame


def _iter_geotiff_files(directory: Path) -> list[Path]:
    """Return only real GeoTIFF files, excluding sidecars and temporary files."""
    if not directory.exists():
        return []
    return sorted(
        path
        for path in directory.iterdir()
        if path.is_file() and path.suffix.lower() in {".tif", ".tiff"}
    )


def _iter_target_geotiff_files(directory: Path) -> list[Path]:
    return [path for path in _iter_geotiff_files(directory) if _is_target_geotiff(path)]


def _remove_non_target_geotiffs(directory: Path) -> None:
    for path in _iter_geotiff_files(directory):
        if _is_target_geotiff(path):
            continue
        _unlink_raster_file_with_sidecars(path)
        LOGGER.warning("Removed non-target GeoTIFF mosaic: %s", path.name)


def _prune_duplicate_geotiffs(directory: Path, label: str) -> int:
    grouped: dict[str, dict[str, list[Path]]] = {}
    for path in _iter_geotiff_files(directory):
        if not _is_target_geotiff(path):
            continue
        product_stem = _geotiff_product_stem(path)
        key = raster_semantic_key(product_stem)
        grouped.setdefault(key, {}).setdefault(product_stem, []).append(path)

    removed = 0
    for product_paths in grouped.values():
        if len(product_paths) <= 1:
            continue
        keep_stem = preferred_raster_product_stem(set(product_paths))
        for product_stem, paths in sorted(product_paths.items()):
            if product_stem == keep_stem:
                continue
            for path in paths:
                if _unlink_raster_file_with_sidecars(path):
                    removed += 1
            LOGGER.warning("Removed duplicate %s %s; kept %s.", label, product_stem, keep_stem)
    return removed


def _unlink_raster_file_with_sidecars(path: Path) -> bool:
    removed = False
    try:
        path.unlink(missing_ok=True)
        removed = True
    except OSError as exc:
        LOGGER.warning("Could not remove duplicate raster %s: %s", path.name, exc)
        return False
    for sidecar in (
        path.with_name(path.name + ".drive.json"),
        path.with_name(path.name + ".rejected.json"),
        path.with_suffix(path.suffix + ".aux.xml"),
    ):
        try:
            sidecar.unlink(missing_ok=True)
        except OSError as exc:
            LOGGER.warning("Could not remove raster sidecar %s: %s", sidecar.name, exc)
    return removed


def _is_target_geotiff(path: Path) -> bool:
    try:
        with rasterio.open(path) as dataset:
            return _dataset_is_target_grid(dataset)
    except Exception as exc:
        LOGGER.warning("Ignoring unreadable GeoTIFF %s: %s", path.name, exc)
        return False


def _assert_common_grid(
    frame: pd.DataFrame,
    expected_pixel_size_m: float,
    expected_crs: str,
    tolerance: float,
) -> None:
    grid_columns = ["width", "height", "crs", "transform"]
    if frame[grid_columns].drop_duplicates().shape[0] > 1:
        examples = ", ".join(str(value) for value in frame["file"].head(10))
        raise RuntimeError(
            "GeoTIFF mosaics do not share a common grid. "
            f"Example files: {examples}. See raster_grid_inventory.csv for full grid metadata."
        )
    crs_values = set(frame["crs"].astype(str))
    if crs_values != {expected_crs}:
        raise RuntimeError(
            f"GeoTIFF mosaics must use {expected_crs}; found {', '.join(sorted(crs_values))}."
        )
    invalid_pixel_size = frame[
        (frame["pixel_width"].sub(expected_pixel_size_m).abs() > tolerance)
        | (frame["pixel_height"].sub(expected_pixel_size_m).abs() > tolerance)
    ]
    if not invalid_pixel_size.empty:
        bad_files = ", ".join(str(value) for value in invalid_pixel_size["file"].head(10))
        raise RuntimeError(
            f"GeoTIFF mosaics must use {expected_pixel_size_m:g} x {expected_pixel_size_m:g} m pixels. "
            f"Invalid files: {bad_files}."
        )


def _geotiff_product_stem(path: Path) -> str:
    return raster_product_stem(path)


def _copy_geotiff_lzw(source_path: Path, output_path: Path) -> None:
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    with rasterio.open(source_path) as source:
        profile = source.profile.copy()
        profile.update(compress="lzw")
        with rasterio.open(temporary, "w", **profile) as target:
            for band_index in range(1, source.count + 1):
                target.write(source.read(band_index), band_index)
            target.update_tags(**source.tags())
            for band_index in range(1, source.count + 1):
                target.update_tags(band_index, **source.tags(band_index))
    temporary.replace(output_path)


def _write_geotiff_mosaic(source_paths: list[Path], output_path: Path) -> None:
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    datasets = [rasterio.open(path) for path in source_paths]
    try:
        n_bands = datasets[0].count
        # Merge one band to get output dimensions without allocating all bands at once.
        first_band, transform = merge(datasets, indexes=[1])
        height, width = first_band.shape[1], first_band.shape[2]
        profile = datasets[0].profile.copy()
        profile.update(
            height=height,
            width=width,
            transform=transform,
            count=n_bands,
            compress="lzw",
            bigtiff="IF_SAFER",
        )
        with rasterio.open(temporary, "w", **profile) as target:
            target.write(first_band[0], 1)
            for band_index in range(2, n_bands + 1):
                band_data, _ = merge(datasets, indexes=[band_index])
                target.write(band_data[0], band_index)
            target.update_tags(**datasets[0].tags())
            for band_index in range(1, n_bands + 1):
                target.update_tags(band_index, **datasets[0].tags(band_index))
    finally:
        for dataset in datasets:
            dataset.close()
    temporary.replace(output_path)


def _forest_to_nonforest_products(
    settings: dict[str, Any],
    prepared: PreparedInputs,
    organized: OrganizedData,
) -> list[RasterProduct]:
    forest_codes = [int(code) for code in settings["analysis"]["forest_codes"]]
    products: list[RasterProduct] = []
    for start_year, end_year in zip(PRIMARY_MAPBIOMAS_YEARS, PRIMARY_MAPBIOMAS_YEARS[1:]):
        resolved_end_year = resolve_mapbiomas_year(prepared.mapbiomas_image, end_year)
        start_forest = select_mapbiomas_year(prepared.mapbiomas_image, start_year).remap(
            forest_codes, [1] * len(forest_codes), 0
        )
        end_forest = select_mapbiomas_year(prepared.mapbiomas_image, end_year).remap(
            forest_codes, [1] * len(forest_codes), 0
        )
        mapbiomas_change = (
            start_forest.eq(1)
            .And(end_forest.eq(0))
            .updateMask(organized.valid_analysis_mask)
            .clip(prepared.area_of_interest)
            .toByte()
            .rename("value")
        )
        period = _PERIOD_LABEL.get((start_year, resolved_end_year), f"{start_year}_{resolved_end_year}")
        products.append(
            RasterProduct(
                f"UDefA_MB_ForestLoss_{period}",
                mapbiomas_change,
                f"MapBiomas forest loss {start_year}-{resolved_end_year}",
            )
        )

    for reference_name in ("FCBM4",):
        reference = organized.references.get(reference_name)
        if reference is None or reference.image is None:
            continue
        deforestation_codes = [
            code
            for code, label in reference.class_labels.items()
            if "deforestation" in label.lower()
        ]
        if not deforestation_codes:
            continue
        change = (
            reference.image.remap(deforestation_codes, [1] * len(deforestation_codes), 0)
            .clip(prepared.area_of_interest)
            .toByte()
            .rename("value")
        )
        products.append(
            RasterProduct(
                f"UDefA_Ct_{reference_name}_ForestLoss",
                change,
                f"CTrees forest loss derived from {reference.label}",
            )
        )

    snapshot_pairs = [
        ("FCBM1_2009", "FCBM2_2013", (2009, 2013)),
        ("FCBM2_2013", "FCBM3_2018", (2013, 2018)),
    ]
    for first_name, second_name, year_pair in snapshot_pairs:
        first = organized.references.get(first_name)
        second = organized.references.get(second_name)
        if first is None or second is None or first.image is None or second.image is None:
            continue
        period = _PERIOD_LABEL.get(year_pair, f"{year_pair[0]}_{year_pair[1]}")
        change = first.image.eq(1).And(second.image.eq(0)).clip(prepared.area_of_interest).toByte().rename("value")
        products.append(
            RasterProduct(
                f"UDefA_Ct_ForestLoss_{period}",
                change,
                f"CTrees forest loss {year_pair[0]}-{year_pair[1]} ({first.label} → {second.label})",
            )
        )
    return products


def _udef_a_fcbm_products(
    settings: dict[str, Any],
    prepared: PreparedInputs,
    organized: OrganizedData,
) -> list[RasterProduct]:
    hrp_settings = settings.get("udef_a", {})
    hrp_years = hrp_settings.get("hrp_years", {})
    t1 = int(hrp_years.get("t1", CROSS_TABULATION_YEARS[0]))
    t2 = int(hrp_years.get("t2", CROSS_TABULATION_YEARS[1]))
    t3 = int(hrp_years.get("t3", CROSS_TABULATION_YEARS[2]))
    source_name = str(hrp_settings.get("ctrees_fcbm_reference", "FCBM4"))
    ctrees_fcbm = _ctrees_fcbm_index_image(settings, organized, source_name, prepared.area_of_interest, t1, t2, t3)
    if ctrees_fcbm is None:
        return []

    mb_fcbm = _mapbiomas_fcbm_index(settings, prepared, organized.valid_analysis_mask, t1, t2, t3)
    products = [
        RasterProduct(
            "UDefA_Ct_FCBM_TransIdx",
            ctrees_fcbm.toByte().rename("value"),
            "CTrees raw FCBM transition index (classes 1-8)",
        ),
        RasterProduct(
            "UDefA_MB_FCBM_TransIdx",
            mb_fcbm.toByte().rename("value"),
            "MapBiomas-derived FCBM transition index (classes 1-8)",
        ),
        RasterProduct(
            "UDefA_Ct_FCBM_VT0007",
            _remap_fcbm(ctrees_fcbm, FCBM_VT0007_REMAP).toByte().rename("value"),
            "CTrees FCBM - VMD0055 Table 15 four-class scheme",
        ),
        RasterProduct(
            "UDefA_MB_FCBM_VT0007",
            _remap_fcbm(mb_fcbm, FCBM_VT0007_REMAP).toByte().rename("value"),
            "MapBiomas FCBM - VMD0055 Table 15 four-class scheme",
        ),
        RasterProduct(
            "UDefA_Ct_FCBM_Accuracy",
            _remap_fcbm(ctrees_fcbm, FCBM_ACCURACY_REMAP).toByte().rename("value"),
            "CTrees FCBM - VMD0055 Table 16 accuracy assessment scheme",
        ),
        RasterProduct(
            "UDefA_MB_FCBM_Accuracy",
            _remap_fcbm(mb_fcbm, FCBM_ACCURACY_REMAP).toByte().rename("value"),
            "MapBiomas FCBM - VMD0055 Table 16 accuracy assessment scheme",
        ),
    ]
    for src, fcbm in (("Ct", ctrees_fcbm), ("MB", mb_fcbm)):
        products.extend(_udef_a_binary_products(src, fcbm, t1, t2, t3, int(settings["earth_engine"]["scale_native_m"])))
    return products


def _ctrees_fcbm_index_image(
    settings: dict[str, Any],
    organized: OrganizedData,
    source_name: str,
    area_of_interest: ee.FeatureCollection,
    t1: int,
    t2: int,
    t3: int,
) -> ee.Image | None:
    source_asset = str(settings.get("udef_a", {}).get("ctrees_fcbm_asset") or "").strip()
    if source_asset:
        return ee.Image(source_asset).clip(area_of_interest).toByte().rename("value")
    derived = _ctrees_fcbm_index_from_snapshots(settings, organized, area_of_interest, t1, t2, t3)
    if derived is not None:
        return derived
    reference_settings = settings.get("references", {}).get(source_name, {})
    if reference_settings.get("asset"):
        _validate_raw_fcbm_schema(source_name, reference_settings.get("class_codes", []))
        return ee.Image(reference_settings["asset"]).clip(area_of_interest).toByte().rename("value")
    reference = organized.references.get(source_name)
    if reference is None or reference.image is None:
        LOGGER.warning("UDef-A FCBM source %s is unavailable; mandatory derived rasters were skipped.", source_name)
        return None
    _validate_raw_fcbm_schema(source_name, reference.class_codes)
    return reference.image.clip(area_of_interest).toByte().rename("value")


def _ctrees_fcbm_index_from_snapshots(
    settings: dict[str, Any],
    organized: OrganizedData,
    area_of_interest: ee.FeatureCollection,
    t1: int,
    t2: int,
    t3: int,
) -> ee.Image | None:
    snapshot_settings = settings.get("udef_a", {}).get("ctrees_snapshot_references", {})
    default_names = {
        "t1": f"FCBM1_{t1}",
        "t2": f"FCBM2_{t2}",
        "t3": f"FCBM3_{t3}",
    }
    snapshot_names = {
        period: str(snapshot_settings.get(period) or default_names[period]).strip()
        for period in ("t1", "t2", "t3")
    }
    if not all(snapshot_names.values()):
        return None

    snapshots: list[ee.Image] = []
    for period, reference_name in snapshot_names.items():
        reference = organized.references.get(reference_name)
        if reference is None or reference.image is None:
            LOGGER.warning(
                "CTrees snapshot %s for %s is unavailable; raw FCBM classes 1-8 could not be derived.",
                reference_name,
                period.upper(),
            )
            return None
        snapshots.append(reference.image.eq(1).toByte())

    start, midpoint, end = snapshots
    valid_mask = start.mask().And(midpoint.mask()).And(end.mask())
    fcbm = _fcbm_index_from_binary_series(start, midpoint, end).updateMask(valid_mask).clip(area_of_interest)
    LOGGER.info(
        "Derived CTrees raw FCBM classes 1-8 from snapshots %s, %s, and %s.",
        snapshot_names["t1"],
        snapshot_names["t2"],
        snapshot_names["t3"],
    )
    return fcbm


def _validate_raw_fcbm_schema(source_name: str, class_codes: Any) -> None:
    codes = {int(code) for code in class_codes}
    if set(range(1, 9)).issubset(codes):
        return
    raise RuntimeError(
        "UDef-A and MB-FCBM products require a raw CTrees FCBM transition-index raster "
        "with classes 1-8 per VT0007 v1.0, Table 1. "
        f"The configured source '{source_name}' does not expose classes 1-8. "
        "Set udef_a.ctrees_fcbm_asset in config/settings.yaml to the raw FCBM asset, "
        "or point udef_a.ctrees_fcbm_reference to a reference entry whose class_codes include 1-8."
    )


def _mapbiomas_fcbm_index(
    settings: dict[str, Any],
    prepared: PreparedInputs,
    valid_mask: ee.Image,
    t1: int,
    t2: int,
    t3: int,
) -> ee.Image:
    forest_codes = [int(code) for code in settings["analysis"]["forest_codes"]]
    forests = [
        select_mapbiomas_year(prepared.mapbiomas_image, year)
        .remap(forest_codes, [1] * len(forest_codes), 0)
        .updateMask(valid_mask)
        for year in (t1, t2, t3)
    ]
    start, midpoint, end = forests
    fcbm = _fcbm_index_from_binary_series(start, midpoint, end).updateMask(valid_mask).clip(prepared.area_of_interest)
    return fcbm.toByte().rename("value")


def _fcbm_index_from_binary_series(start: ee.Image, midpoint: ee.Image, end: ee.Image) -> ee.Image:
    fcbm = ee.Image(1)
    for (start_value, midpoint_value, end_value), transition_index in MB_FCBM_TRANSITION_RULES.items():
        if transition_index == 1:
            continue
        fcbm = fcbm.where(
            start.eq(start_value).And(midpoint.eq(midpoint_value)).And(end.eq(end_value)),
            transition_index,
        )
    return fcbm.toByte().rename("value")


def _udef_a_binary_products(
    src: str,
    fcbm: ee.Image,
    t1: int,
    t2: int,
    t3: int,
    scale: int,
) -> list[RasterProduct]:
    periods = {
        f"T1_{t1}": (t1, "t1_forest", "t1_nonforest"),
        f"T2_{t2}": (t2, "t2_forest", "t2_nonforest"),
        f"T3_{t3}": (t3, "t3_forest", "t3_nonforest"),
    }
    products: list[RasterProduct] = []
    for period, (year, forest_key, nonforest_key) in periods.items():
        forest = _binary_from_fcbm(fcbm, FCBM_RISK_INDEX_GROUPS[forest_key])
        nonforest = _binary_from_fcbm(fcbm, FCBM_RISK_INDEX_GROUPS[nonforest_key])
        products.extend(
            [
                RasterProduct(
                    f"UDefA_{src}_Forest_Input_{period}",
                    forest,
                    f"UDefA {src} forest input at {period} ({year})",
                ),
                RasterProduct(
                    f"UDefA_{src}_NonForest_Input_{period}",
                    nonforest,
                    f"UDefA {src} non-forest input at {period} ({year})",
                ),
                RasterProduct(
                    f"UDefA_{src}_DistFromNF_{period}",
                    _distance_from_nonforest(nonforest, scale),
                    f"UDefA {src} Euclidean distance from non-forest at {period} ({year})",
                ),
            ]
        )
    products.extend(
        [
            RasterProduct(
                f"UDefA_{src}_ForestLoss_HRP_{t1}_{t3}",
                _binary_from_fcbm(fcbm, FCBM_RISK_INDEX_GROUPS["hrp_deforestation"]),
                f"UDefA {src} forest loss during the full HRP ({t1}-{t3})",
            ),
            RasterProduct(
                f"UDefA_{src}_ForestLoss_Cal_{t1}_{t2}",
                _binary_from_fcbm(fcbm, FCBM_RISK_INDEX_GROUPS["calibration_deforestation"]),
                f"UDefA {src} forest loss during the calibration period ({t1}-{t2})",
            ),
            RasterProduct(
                f"UDefA_{src}_ForestLoss_Con_{t2}_{t3}",
                _binary_from_fcbm(fcbm, FCBM_RISK_INDEX_GROUPS["confirmation_deforestation"]),
                f"UDefA {src} forest loss during the confirmation period ({t2}-{t3})",
            ),
        ]
    )
    return products


def _binary_from_fcbm(fcbm: ee.Image, indices: list[int]) -> ee.Image:
    return fcbm.remap(indices, [1] * len(indices), 0).toByte().rename("value")


def _remap_fcbm(fcbm: ee.Image, remap: dict[int, int]) -> ee.Image:
    return fcbm.remap(list(remap), [remap[index] for index in remap], 0).rename("value")


def _distance_from_nonforest(nonforest: ee.Image, scale: int) -> ee.Image:
    return (
        nonforest.selfMask()
        .fastDistanceTransform(4096, "pixels", "squared_euclidean")
        .sqrt()
        .multiply(scale)
        .unmask(0)
        .toFloat()
        .rename("value")
    )


def _dmjss_mb_image(
    settings: dict[str, Any],
    prepared: PreparedInputs,
    valid_mask: ee.Image,
    years: list[int],
) -> ee.Image:
    """Classify MapBiomas forest/non-forest snapshots into DMJSS-style classes.

    Generalizes the official DMJSS decision tree (built from three CTrees FCBM
    snapshots) to the four primary MapBiomas comparison years: a pixel is
    judged Stable Forest/Non-Forest or Deforestation/Regrowth from its first
    vs. last snapshot, then any pixel within DMJSS_BUFFER_DISTANCE_M of a
    changed pixel is reclassified as Buffer, mirroring the official treatment
    of boundary uncertainty around deforestation/regrowth edges.
    """
    forest_codes = [int(code) for code in settings["analysis"]["forest_codes"]]
    snapshots = [
        select_mapbiomas_year(prepared.mapbiomas_image, year)
        .remap(forest_codes, [1] * len(forest_codes), 0)
        .updateMask(valid_mask)
        .toByte()
        for year in years
    ]
    start, end = snapshots[0], snapshots[-1]
    deforestation = start.eq(1).And(end.eq(0))
    regrowth = start.eq(0).And(end.eq(1))

    dmjss = (
        ee.Image(0)
        .where(start.eq(1).And(end.eq(1)), 1)
        .where(deforestation, 2)
        .where(regrowth, 3)
    )

    change_mask = deforestation.Or(regrowth)
    scale = int(settings["earth_engine"]["scale_native_m"])
    near_change = (
        change_mask.selfMask()
        .fastDistanceTransform(4096, "pixels", "squared_euclidean")
        .sqrt()
        .multiply(scale)
        .lte(DMJSS_BUFFER_DISTANCE_M)
        .unmask(0)
    )
    dmjss = dmjss.where(near_change.And(change_mask.Not()), 4)
    return dmjss.updateMask(valid_mask).clip(prepared.area_of_interest).toByte().rename("value")


def _dmjss_mb_products(
    settings: dict[str, Any],
    prepared: PreparedInputs,
    organized: OrganizedData,
) -> list[RasterProduct]:
    years = sorted(set(CROSS_TABULATION_YEARS) | {PRIMARY_MAPBIOMAS_YEARS[-1]})
    dmjss = _dmjss_mb_image(settings, prepared, organized.valid_analysis_mask, years)
    return [
        RasterProduct(
            name=_with_projection_suffix(f"UDefA_MB_DMJSS_{years[0]}_{years[-1]}", settings),
            image=dmjss,
            description=(
                "MapBiomas-derived DMJSS-style stratification "
                f"({years[0]}-{years[-1]}): Stable Non-Forest, Stable Forest, "
                "Deforestation, Regrowth, Buffer"
            ),
        )
    ]


def _mapbiomas_lulc_legend() -> list[tuple[int, str, str]]:
    return [
        (code, MAPBIOMAS_LAND_COVER_CLASSES[code], MAPBIOMAS_CLASS_COLORS[code])
        for code in sorted(MAPBIOMAS_LAND_COVER_CLASSES)
    ]


_IDRISI_NO_DATA_COLOR = "#000000"
_IDRISI_NO_CHANGE_COLOR = "#ffffff"
_IDRISI_FOREST_COLOR = "#238b45"
_IDRISI_STABLE_FOREST_COLOR = "#006d2c"
_IDRISI_NON_FOREST_COLOR = "#c7b37f"
_IDRISI_STABLE_NON_FOREST_COLOR = "#d9c98f"
_IDRISI_LOSS_COLOR = "#e31a1c"
_IDRISI_LOSS_EARLY_COLOR = "#fdae61"
_IDRISI_LOSS_LATE_COLOR = "#d7191c"
_IDRISI_GAIN_COLOR = "#2c7fb8"
_IDRISI_REGROWTH_COLOR = "#41b6c4"
_IDRISI_BUFFER_COLOR = "#ffff99"
_IDRISI_AGREEMENT_COLOR = "#7f0000"
_IDRISI_CTREES_ONLY_COLOR = "#00bcd4"
_IDRISI_MAPBIOMAS_ONLY_COLOR = "#ffcc33"
_IDRISI_TEMPORAL_MIDPOINT_COLOR = "#756bb1"
_IDRISI_TEMPORAL_CONTINUOUS_COLOR = "#084081"
_IDRISI_DEFORESTED_REGROWTH_COLOR = "#b15928"


_IDRISI_LEGENDS: dict[str, list[tuple[int, str, str]]] = {
    "persistence": [
        (1, "Persistent Forest", _IDRISI_STABLE_FOREST_COLOR),
        (2, "Persistent Non-Forest", _IDRISI_STABLE_NON_FOREST_COLOR),
        (3, "Land-Cover Change", _IDRISI_LOSS_EARLY_COLOR),
    ],
    "binary_forest": [
        (0, "Non-Forest", _IDRISI_NON_FOREST_COLOR),
        (1, "Forest", _IDRISI_FOREST_COLOR),
    ],
    "dmjss": [
        (0, "Stable Non-Forest", _IDRISI_STABLE_NON_FOREST_COLOR),
        (1, "Stable Forest", _IDRISI_STABLE_FOREST_COLOR),
        (2, "Deforestation", _IDRISI_LOSS_COLOR),
        (3, "Regrowth", _IDRISI_REGROWTH_COLOR),
        (4, "Buffer", _IDRISI_BUFFER_COLOR),
    ],
    "forest_loss": [
        (0, "No Change", _IDRISI_NO_CHANGE_COLOR),
        (1, "Forest to Non-Forest (Loss)", _IDRISI_LOSS_COLOR),
    ],
    "change4": [
        (0, "No Data", _IDRISI_NO_DATA_COLOR),
        (1, "Stable Forest", _IDRISI_STABLE_FOREST_COLOR),
        (2, "Stable Non-Forest", _IDRISI_STABLE_NON_FOREST_COLOR),
        (3, "Forest Loss", _IDRISI_LOSS_COLOR),
        (4, "Forest Gain", _IDRISI_GAIN_COLOR),
    ],
    "valid_mask": [
        (0, "Outside Analysis Area", _IDRISI_NO_CHANGE_COLOR),
        (1, "Valid Analysis Area", _IDRISI_FOREST_COLOR),
    ],
    "loss_agreement3": [
        (1, "Both Sources Agree (Loss)", _IDRISI_AGREEMENT_COLOR),
        (2, "CTrees Only", _IDRISI_CTREES_ONLY_COLOR),
        (3, "MapBiomas Only", _IDRISI_MAPBIOMAS_ONLY_COLOR),
    ],
    "fcbm_index8": [
        (1, "No forest at T1, T2, or T3", _IDRISI_STABLE_NON_FOREST_COLOR),
        (2, "Forest only at T3", _IDRISI_GAIN_COLOR),
        (3, "Forest only at T2", _IDRISI_TEMPORAL_MIDPOINT_COLOR),
        (4, "Forest at T2 and T3", _IDRISI_TEMPORAL_CONTINUOUS_COLOR),
        (5, "Stable Forest", _IDRISI_STABLE_FOREST_COLOR),
        (6, "Deforested T1->T2, remained non-forest", _IDRISI_LOSS_EARLY_COLOR),
        (7, "Deforested T1->T2, regrew by T3", _IDRISI_DEFORESTED_REGROWTH_COLOR),
        (8, "Deforested T2->T3", _IDRISI_LOSS_LATE_COLOR),
    ],
    "fcbm_vt0007": [
        (1, "Stable Non-Forest", _IDRISI_STABLE_NON_FOREST_COLOR),
        (2, "Stable Forest", _IDRISI_STABLE_FOREST_COLOR),
        (3, "Deforested - First Half of HRP", _IDRISI_LOSS_EARLY_COLOR),
        (4, "Deforested - Second Half of HRP", _IDRISI_LOSS_LATE_COLOR),
    ],
    "fcbm4": [
        (0, "No Data", _IDRISI_NO_DATA_COLOR),
        (1, "Stable Non-Forest", _IDRISI_STABLE_NON_FOREST_COLOR),
        (2, "Stable Forest", _IDRISI_STABLE_FOREST_COLOR),
        (3, "Deforested - First Half of HRP", _IDRISI_LOSS_EARLY_COLOR),
        (4, "Deforested - Second Half of HRP", _IDRISI_LOSS_LATE_COLOR),
    ],
    "fcbm_accuracy": [
        (1, "Non-Forest at End of HRP", _IDRISI_NON_FOREST_COLOR),
        (2, "Forest at End of HRP", _IDRISI_FOREST_COLOR),
        (3, "Deforested within HRP", _IDRISI_LOSS_COLOR),
    ],
    "binary_nonforest": [
        (0, "Forest", _IDRISI_FOREST_COLOR),
        (1, "Non-Forest", _IDRISI_NON_FOREST_COLOR),
    ],
    "distance": [],
    "lulc": _mapbiomas_lulc_legend(),
}


def _idrisi_product_type(stem: str) -> str:
    s = stem.lower()
    if "distfromnf" in s or "dist_from_nf" in s or "distance" in s:
        return "distance"
    if "persistence" in s:
        return "persistence"
    if "agreement" in s or "cross_forestloss" in s or "lossagreement" in s:
        return "loss_agreement3"
    if "fcbm4" in s:
        return "fcbm4"
    if "forestchange4" in s:
        return "change4"
    if "forestloss" in s or "change_foresttononforest" in s or "change_f2nf" in s:
        return "forest_loss"
    if "lulc" in s or "landcover" in s or "land_cover" in s:
        return "lulc"
    if "vt0007" in s or "table15" in s:
        return "fcbm_vt0007"
    if "accuracy" in s or "table16" in s:
        return "fcbm_accuracy"
    if "transidx" in s or "fcbm_index" in s or "fcbm_transition" in s:
        return "fcbm_index8"
    if "mapbiomas_change_" in s or ("change" in s and any(x in s for x in ["stab", "loss", "gain"])):
        return "change4"
    if "valid" in s or "mask" in s:
        return "valid_mask"
    if "dmjss" in s:
        return "dmjss"
    if "forestnonforest" in s or "binary_forest" in s:
        return "binary_forest"
    if (
        "nonforest_input" in s
        or "non_forest_input" in s
        or "binary_nonforest" in s
        or re.search(r"(?:^|_)non[_-]?forest(?:_|$)", s)
    ):
        return "binary_nonforest"
    if "forest_input" in s:
        return "binary_forest"
    return "binary_forest"


def _idrisi_title(stem: str) -> str:
    s = stem
    for suffix in ("_30m", "_EPSG_5880_30m", "_EPSG_4326_30m", "_EPSG_10857_30m", "_EPSG_5880", "_EPSG_4326", "_EPSG_10857"):
        s = s.replace(suffix, "")
    pairs = [
        ("UDefA_MB_ForestAnnual_SIRGAS", "MapBiomas Annual Forest Cover 1985-2024 (SIRGAS)"),
        ("UDefA_MB_LULC_Annual_SIRGAS", "MapBiomas Annual Land Cover 1985-2024 (SIRGAS)"),
        ("UDefA_Ct_ForestLoss_Cal_SIRGAS", "CTrees Forest Loss - Calibration Period (SIRGAS)"),
        ("UDefA_Ct_ForestLoss_Con_SIRGAS", "CTrees Forest Loss - Confirmation Period (SIRGAS)"),
        ("UDefA_Ct_DMJSS_SIRGAS", "CTrees DMJSS Deforestation Map (SIRGAS)"),
        ("UDefA_Ct_Forest_T1_SIRGAS", "CTrees Forest Cover T1 (SIRGAS)"),
        ("UDefA_Ct_Forest_T2_SIRGAS", "CTrees Forest Cover T2 (SIRGAS)"),
        ("UDefA_Ct_Forest_T3_SIRGAS", "CTrees Forest Cover T3 (SIRGAS)"),
        ("UDefA_MB_ForestLoss_Cal_SIRGAS", "MapBiomas Forest Loss - Calibration Period (SIRGAS)"),
        ("UDefA_MB_ForestLoss_Con_SIRGAS", "MapBiomas Forest Loss - Confirmation Period (SIRGAS)"),
        ("UDefA_MB_ForestLoss_HRP_SIRGAS", "MapBiomas Forest Loss - HRP (SIRGAS)"),
        ("para_mapbiomas_forest_annual_1985_2024_sirgas", "MapBiomas Annual Forest Cover 1985-2024 (Para, SIRGAS)"),
        ("para_ctrees_change_f2nf_2009_2013", "CTrees Forest Loss 2009-2013 (Para, SIRGAS)"),
        ("para_ctrees_change_f2nf_2013_2018", "CTrees Forest Loss 2013-2018 (Para, SIRGAS)"),
        ("para_ctrees_change_f2nf", "CTrees Forest to Non-Forest Change (Para, SIRGAS)"),
        ("para_ctrees_DMJSS_sirgas", "CTrees Deforestation Map DMJSS (Para, SIRGAS)"),
        ("para_ctrees_FCBM1_2009_sirgas", "CTrees Forest Cover 2009 - FCBM1 (Para, SIRGAS)"),
        ("para_ctrees_FCBM2_2013_sirgas", "CTrees Forest Cover 2013 - FCBM2 (Para, SIRGAS)"),
        ("para_ctrees_FCBM3_2018_sirgas", "CTrees Forest Cover 2018 - FCBM3 (Para, SIRGAS)"),
        ("UDefA_ValidMask", "Valid Analysis Area Mask"),
        ("UDefA_MB_LULC_", "MapBiomas Land Cover - "),
        ("UDefA_MB_Forest_", "MapBiomas Forest Cover - "),
        ("UDefA_MB_Persistence_Scen", "MapBiomas Forest Persistence - Scenario "),
        ("UDefA_MB_ForestChange4_", "MapBiomas 4-Class Forest Change - "),
        ("UDefA_Ct_ForestChange4_", "CTrees 4-Class Forest Change - "),
        ("UDefA_Ct_MB_Agreement_", "CTrees vs MapBiomas Loss Agreement - "),
        ("UDefA_Ct_FCBM4_ForestLoss", "CTrees FCBM4 Forest Loss"),
        ("UDefA_MB_ForestLoss_", "MapBiomas Forest Loss - "),
        ("UDefA_Ct_ForestLoss_", "CTrees Forest Loss - "),
        ("UDefA_Ct_FCBM4", "CTrees FCBM4 4-Class Reclassification"),
        ("UDefA_Ct_FCBM_TransIdx", "CTrees FCBM Transition Index (1-8)"),
        ("UDefA_MB_FCBM_TransIdx", "MapBiomas FCBM Transition Index (1-8)"),
        ("UDefA_Ct_FCBM_VT0007", "CTrees FCBM - VMD0055 Table 15 Classes"),
        ("UDefA_MB_FCBM_VT0007", "MapBiomas FCBM - VMD0055 Table 15 Classes"),
        ("UDefA_Ct_FCBM_Accuracy", "CTrees FCBM - VMD0055 Table 16 Classes"),
        ("UDefA_MB_FCBM_Accuracy", "MapBiomas FCBM - VMD0055 Table 16 Classes"),
        ("UDefA_Ct_Forest_Input_", "CTrees Forest Input - "),
        ("UDefA_Ct_NonForest_Input_", "CTrees Non-Forest Input - "),
        ("UDefA_Ct_DistFromNF_", "CTrees Distance from Non-Forest - "),
        ("UDefA_MB_Forest_Input_", "MapBiomas Forest Input - "),
        ("UDefA_MB_NonForest_Input_", "MapBiomas Non-Forest Input - "),
        ("UDefA_MB_DistFromNF_", "MapBiomas Distance from Non-Forest - "),
        ("UDefA_Ct_Forest_", "CTrees Forest Cover - "),
        ("UDefA_Ct_DMJSS", "CTrees DMJSS Deforestation Map "),
        ("UDefA_MB_DMJSS", "MapBiomas DMJSS Deforestation Map "),
        ("Valid_Analysis_Mask", "Valid Analysis Area Mask"),
        ("MapBiomas_Persistence_A_100pct_1985-2024", "MapBiomas Forest Persistence - Scenario A (100%, 1985-2024)"),
        ("MapBiomas_Persistence_B_95pct_1985-2024", "MapBiomas Forest Persistence - Scenario B (95%, 1985-2024)"),
        ("MapBiomas_Persistence_C_50pct_1985-2024", "MapBiomas Forest Persistence - Scenario C (50%, 1985-2024)"),
        ("MapBiomas_Persistence_D_100pct_2015-2024", "MapBiomas Forest Persistence - Scenario D (100%, 2015-2024)"),
        ("MapBiomas_Persistence_E_100pct_2013-2024", "MapBiomas Forest Persistence - Scenario E (100%, 2013-2024)"),
        ("MapBiomas_Persistence_F_100pct_2018-2024", "MapBiomas Forest Persistence - Scenario F (100%, 2018-2024)"),
        ("Change_ForestToNonForest_MapBiomas_1985_2009", "MapBiomas Forest Loss 1985-2009"),
        ("Change_ForestToNonForest_MapBiomas_2009_2013", "MapBiomas Forest Loss 2009-2013"),
        ("Change_ForestToNonForest_MapBiomas_2013_2018", "MapBiomas Forest Loss 2013-2018"),
        ("Change_ForestToNonForest_MapBiomas_2018_2024", "MapBiomas Forest Loss 2018-2024"),
        ("Change_ForestToNonForest_MapBiomas_1985_2024", "MapBiomas Forest Loss 1985-2024"),
        ("Change_ForestToNonForest_CTrees_FCBM1_2009_to_FCBM2_2013", "CTrees Forest Loss 2009-2013"),
        ("Change_ForestToNonForest_CTrees_FCBM2_2013_to_FCBM3_2018", "CTrees Forest Loss 2013-2018"),
        ("Change_ForestToNonForest_CTrees_FCBM4", "CTrees Forest Loss - FCBM4"),
        ("CTrees_FCBM1_2009", "CTrees Forest Cover 2009 - FCBM1"),
        ("CTrees_FCBM2_2013", "CTrees Forest Cover 2013 - FCBM2"),
        ("CTrees_FCBM3_2018", "CTrees Forest Cover 2018 - FCBM3"),
        ("CTrees_FCBM4", "CTrees FCBM4 4-Class Reclassification"),
        ("CTrees_DMJSS", "CTrees Deforestation Map - DMJSS"),
        ("MapBiomas_LandCover_", "MapBiomas Land Cover Classes "),
        ("MapBiomas_ForestNonForest_", "MapBiomas Binary Forest/Non-Forest "),
        ("MapBiomas_Binary_Forest_", "MapBiomas Binary Forest "),
        ("MapBiomas_LULC_", "MapBiomas Land Cover "),
        ("MapBiomas_Change_", "MapBiomas Forest Change "),
        ("MapBiomas_ForestLoss_", "MapBiomas Forest Loss "),
        ("Cross_ForestLossAgreement_CTrees_x_MapBiomas_", "Agreement Map - CTrees vs MapBiomas Forest Loss "),
        ("CTrees_FCBM_VT0007_Table15_", "CTrees FCBM - VMD0055 Table 15 Classes "),
        ("MapBiomas_FCBM_VT0007_Table15_", "MapBiomas FCBM - VMD0055 Table 15 Classes "),
        ("CTrees_FCBM_Accuracy_Table16_", "CTrees FCBM - VMD0055 Table 16 Classes "),
        ("MapBiomas_FCBM_Accuracy_Table16_", "MapBiomas FCBM - VMD0055 Table 16 Classes "),
        ("CTrees_FCBM_Index_", "CTrees FCBM Transition Index "),
        ("MapBiomas_FCBM_Index_", "MapBiomas FCBM Transition Index "),
    ]
    for pattern, replacement in pairs:
        if pattern.lower() in s.lower():
            idx = s.lower().index(pattern.lower())
            suffix_part = s[idx + len(pattern):]
            s = replacement + suffix_part.replace("_", " ").strip()
            break
    s = s.replace("_", " ").replace("--", "-").strip()
    return s[:70]


def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _idrisi_pal_text(legend: list[tuple[int, str, str]]) -> str:
    color_map = {value: _hex_to_rgb(color) for value, _label, color in legend}
    lines = []
    for i in range(256):
        r, g, b = color_map.get(i, (0, 0, 0))
        lines.append(f"{r} {g} {b}")
    return "\n".join(lines) + "\n"


def _idrisi_smp_bytes(legend: list[tuple[int, str, str]]) -> bytes:
    color_map = {value: _hex_to_rgb(color) for value, _label, color in legend}
    data = bytearray()
    data.extend(b"[Idrisi]")
    data.extend(bytes((1, 11, 8, 18)))
    data.extend(struct.pack("<HHH", 255, 0, 255))
    for i in range(256):
        data.extend(color_map.get(i, (0, 0, 0)))
    return bytes(data)


def _write_idrisi_pal(pal_path: Path, legend: list[tuple[int, str, str]]) -> None:
    pal_path.write_text(_idrisi_pal_text(legend), encoding="ascii")


def _write_idrisi_smp(smp_path: Path, legend: list[tuple[int, str, str]]) -> None:
    smp_path.write_bytes(_idrisi_smp_bytes(legend))


def _idrisi_legend_lines(legend: list[tuple[int, str, str]]) -> list[str]:
    lines = [f"legend cats : {len(legend)}"]
    for value, label, _color in legend:
        lines.append(f"code {value:<7}: {label}")
    return lines


def _rdc_has_current_legend(rdc_path: Path, legend: list[tuple[int, str, str]]) -> bool:
    try:
        rdc_text = rdc_path.read_text(encoding="ascii")
    except Exception:
        return False
    return all(line in rdc_text for line in _idrisi_legend_lines(legend))


def _rdc_has_current_title(rdc_path: Path, title: str) -> bool:
    try:
        rdc_text = rdc_path.read_text(encoding="ascii")
    except Exception:
        return False
    return f"file title  : {title}" in rdc_text


def _read_idrisi_rdc(rdc_path: Path) -> dict[str, str]:
    metadata: dict[str, str] = {}
    try:
        lines = rdc_path.read_text(encoding="ascii").splitlines()
    except UnicodeDecodeError:
        lines = rdc_path.read_text(encoding="latin-1").splitlines()
    for line in lines:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        metadata[key.strip().lower()] = value.strip()
    return metadata


def _idrisi_metadata_int(metadata: dict[str, str], key: str, default: int = 0) -> int:
    try:
        return int(float(metadata.get(key, str(default))))
    except (TypeError, ValueError):
        return default


def _idrisi_metadata_float(metadata: dict[str, str], key: str, default: float = 0.0) -> float:
    try:
        return float(metadata.get(key, str(default)))
    except (TypeError, ValueError):
        return default


def _read_idrisi_color_table(stem_path: Path) -> np.ndarray | None:
    smp_path = stem_path.with_suffix(".smp")
    if smp_path.exists():
        data = smp_path.read_bytes()
        if len(data) >= 18 + 256 * 3 and data[:8] == b"[Idrisi]":
            return np.frombuffer(data[18 : 18 + 256 * 3], dtype=np.uint8).reshape(256, 3).copy()

    pal_path = stem_path.with_suffix(".pal")
    if pal_path.exists():
        rows: list[tuple[int, int, int]] = []
        for line in pal_path.read_text(encoding="ascii").splitlines()[:256]:
            parts = line.split()
            if len(parts) < 3:
                return None
            try:
                rows.append(tuple(max(0, min(255, int(part))) for part in parts[:3]))
            except ValueError:
                return None
        if len(rows) == 256:
            return np.array(rows, dtype=np.uint8)
    return None


def _render_idrisi_thumbnail(rst_path: Path, metadata: dict[str, str], thumbnail_size: tuple[int, int]):
    from PIL import Image

    width = _idrisi_metadata_int(metadata, "columns")
    height = _idrisi_metadata_int(metadata, "rows")
    if width <= 0 or height <= 0:
        raise RuntimeError(f"Invalid IDRISI dimensions for {rst_path.name}")

    target_width, target_height = thumbnail_size
    stride = max(1, math.ceil(max(width / target_width, height / target_height)))
    raster = np.memmap(rst_path, dtype=np.int16, mode="r", shape=(height, width))
    sample = np.asarray(raster[::stride, ::stride])
    color_table = _read_idrisi_color_table(rst_path)
    flag_value = _idrisi_metadata_int(metadata, "flag value", _IDRISI_NODATA)
    valid_mask = sample != flag_value

    if color_table is not None and sample.size and sample.min() >= 0 and sample.max() <= 255:
        rgb = color_table[sample.astype(np.uint8)]
        rgb = rgb.copy()
        rgb[~valid_mask] = (0, 0, 0)
    else:
        rgb = _grayscale_thumbnail(sample, valid_mask, metadata)

    image = Image.fromarray(rgb.astype(np.uint8), "RGB")
    image.thumbnail(thumbnail_size, Image.Resampling.NEAREST)
    canvas = Image.new("RGB", thumbnail_size, "black")
    x = (thumbnail_size[0] - image.width) // 2
    y = (thumbnail_size[1] - image.height) // 2
    canvas.paste(image, (x, y))
    return canvas


def _grayscale_thumbnail(sample: np.ndarray, valid_mask: np.ndarray, metadata: dict[str, str]) -> np.ndarray:
    valid_values = sample[valid_mask]
    if valid_values.size:
        data_min = _idrisi_metadata_float(metadata, "display min", float(valid_values.min()))
        data_max = _idrisi_metadata_float(metadata, "display max", float(valid_values.max()))
        if data_max <= data_min:
            data_min = float(valid_values.min())
            data_max = float(valid_values.max())
    else:
        data_min, data_max = 0.0, 1.0
    scale = 255.0 / max(data_max - data_min, 1.0)
    gray = np.clip((sample.astype(np.float32) - data_min) * scale, 0, 255).astype(np.uint8)
    gray[~valid_mask] = 0
    return np.stack([gray, gray, gray], axis=2)


def _pil_font(image_font_module: Any, size: int, bold: bool = False):
    names = ["arialbd.ttf", "Arial Bold.ttf"] if bold else ["arial.ttf", "Arial.ttf"]
    for name in names:
        try:
            return image_font_module.truetype(name, size=size)
        except OSError:
            continue
    return image_font_module.load_default()


def _draw_wrapped_text(draw: Any, text: str, xy: tuple[int, int], width: int, font: Any, fill: tuple[int, int, int], max_lines: int) -> None:
    words = str(text).replace("_", " ").split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if _text_width(draw, candidate, font) <= width or not current:
            current = candidate
            continue
        lines.append(current)
        current = word
        if len(lines) >= max_lines:
            break
    if current and len(lines) < max_lines:
        lines.append(current)
    if len(lines) == max_lines and len(" ".join(words)) > len(" ".join(lines)):
        while lines[-1] and _text_width(draw, lines[-1] + "...", font) > width:
            lines[-1] = lines[-1][:-1].rstrip()
        lines[-1] = lines[-1] + "..."
    x, y = xy
    for offset, line in enumerate(lines[:max_lines]):
        draw.text((x, y + offset * 20), line, fill=fill, font=font)


def _text_width(draw: Any, text: str, font: Any) -> int:
    bbox = draw.textbbox((0, 0), text, font=font)
    return int(bbox[2] - bbox[0])


_IDRISI_BLOCK_ROWS = 2048
_IDRISI_TARGET_CRS = CRS.from_epsg(5880)
_IDRISI_TARGET_RESOLUTION_M = 30.0
_IDRISI_NODATA = -9999


def _needs_reproject(dataset: rasterio.DatasetReader) -> bool:
    """Return True if the GeoTIFF must be reprojected to EPSG:5880 before IDRISI export."""
    if not _dataset_is_target_grid(dataset):
        return True
    if dataset.crs:
        return bool(dataset.crs.is_geographic)
    t = dataset.transform
    x_min = t.c
    x_max = t.c + t.a * dataset.width
    y_max = t.f
    y_min = t.f + t.e * dataset.height
    return (
        -180.0 <= x_min <= 180.0
        and -180.0 <= x_max <= 180.0
        and -90.0 <= y_min <= 90.0
        and -90.0 <= y_max <= 90.0
    )


def _dataset_is_target_grid(dataset: rasterio.DatasetReader, tolerance: float = 1e-6) -> bool:
    if dataset.crs is None or CRS.from_user_input(dataset.crs) != _IDRISI_TARGET_CRS:
        return False
    return (
        abs(abs(dataset.transform.a) - _IDRISI_TARGET_RESOLUTION_M) <= tolerance
        and abs(abs(dataset.transform.e) - _IDRISI_TARGET_RESOLUTION_M) <= tolerance
    )


def _reproject_to_target(dataset: rasterio.DatasetReader) -> tuple[np.ndarray, rasterio.transform.Affine, int, int]:
    """Reproject band 1 to EPSG:5880 at 30 m, returning (array_int16, transform, width, height)."""
    dst_transform, dst_width, dst_height = calculate_default_transform(
        dataset.crs,
        _IDRISI_TARGET_CRS,
        dataset.width,
        dataset.height,
        left=dataset.bounds.left,
        bottom=dataset.bounds.bottom,
        right=dataset.bounds.right,
        top=dataset.bounds.top,
        resolution=_IDRISI_TARGET_RESOLUTION_M,
    )
    src_data = dataset.read(1).astype(np.float32)
    src_nodata = dataset.nodata if dataset.nodata is not None else _IDRISI_NODATA
    dst_data = np.full((dst_height, dst_width), src_nodata, dtype=np.float32)
    reproject(
        source=src_data,
        destination=dst_data,
        src_transform=dataset.transform,
        src_crs=dataset.crs,
        dst_transform=dst_transform,
        dst_crs=_IDRISI_TARGET_CRS,
        src_nodata=src_nodata,
        dst_nodata=src_nodata,
        resampling=Resampling.nearest,
    )
    result = dst_data.astype(np.int16)
    LOGGER.debug(
        "Reprojected %s from %s to EPSG:5880 at 30 m (%dx%d → %dx%d).",
        dataset.name, dataset.crs, dataset.width, dataset.height, dst_width, dst_height,
    )
    return result, dst_transform, dst_width, dst_height


def _write_idrisi_pair(geotiff_path: Path, idrisi_directory: Path) -> Path:
    idrisi_directory.mkdir(parents=True, exist_ok=True)
    rst_path = idrisi_directory / f"{geotiff_path.stem}.rst"
    rdc_path = idrisi_directory / f"{geotiff_path.stem}.rdc"
    pal_path = idrisi_directory / f"{geotiff_path.stem}.pal"
    smp_path = idrisi_directory / f"{geotiff_path.stem}.smp"
    rst_tmp = rst_path.with_suffix(rst_path.suffix + ".tmp")
    rdc_tmp = rdc_path.with_suffix(rdc_path.suffix + ".tmp")
    pal_tmp = pal_path.with_suffix(pal_path.suffix + ".tmp")
    smp_tmp = smp_path.with_suffix(smp_path.suffix + ".tmp")
    for temporary in (rst_tmp, rdc_tmp, pal_tmp, smp_tmp):
        temporary.unlink(missing_ok=True)

    with rasterio.open(geotiff_path) as dataset:
        nodata = dataset.nodata if dataset.nodata is not None else _IDRISI_NODATA
        nodata_i16 = np.int16(nodata)

        if _needs_reproject(dataset):
            arr2d, transform, width, height = _reproject_to_target(dataset)
            arr2d[arr2d == nodata_i16] = nodata_i16
            arr_min_v = int(arr2d[arr2d != nodata_i16].min()) if (arr2d != nodata_i16).any() else 0
            arr_max_v = int(arr2d[arr2d != nodata_i16].max()) if (arr2d != nodata_i16).any() else 0
            arr2d.tofile(rst_tmp)
        else:
            transform = dataset.transform
            width = dataset.width
            height = dataset.height
            arr_min_v: int | None = None
            arr_max_v: int | None = None
            with open(rst_tmp, "wb") as rst_file:
                for row_start in range(0, height, _IDRISI_BLOCK_ROWS):
                    window_height = min(_IDRISI_BLOCK_ROWS, height - row_start)
                    window = Window(0, row_start, width, window_height)
                    block = np.empty((window_height, width), dtype=np.int16)
                    mask_values = np.empty((window_height, width), dtype=np.uint8)
                    with warnings.catch_warnings():
                        warnings.filterwarnings(
                            "ignore",
                            message="Setting the shape on a NumPy array has been deprecated.*",
                            category=DeprecationWarning,
                        )
                        dataset.read(1, window=window, out=block)
                        dataset.read_masks(1, window=window, out=mask_values)
                    mask = mask_values == 0
                    block[mask] = nodata_i16
                    block.tofile(rst_file)
                    valid_block = block[~mask]
                    if valid_block.size:
                        block_min = int(valid_block.min())
                        block_max = int(valid_block.max())
                        arr_min_v = block_min if arr_min_v is None else min(arr_min_v, block_min)
                        arr_max_v = block_max if arr_max_v is None else max(arr_max_v, block_max)
            if arr_min_v is None:
                arr_min_v = 0
                arr_max_v = 0

        x_min = transform.c
        x_max = transform.c + transform.a * width
        y_max = transform.f
        y_min = transform.f + transform.e * height
        ref_system = "plane"
        ref_units = "m"

        product_type = _idrisi_product_type(geotiff_path.stem)
        legend = _IDRISI_LEGENDS.get(product_type, [])
        title = _idrisi_title(geotiff_path.stem)
        value_units = "m" if product_type == "distance" else "class"
        legend_lines = _idrisi_legend_lines(legend)
        rdc_tmp.write_text(
            "\n".join(
                [
                    "file format : IDRISI Raster A.1",
                    f"file title  : {title}",
                    "data type   : integer",
                    "file type   : binary",
                    "columns     : " + str(width),
                    "rows        : " + str(height),
                    f"ref. system : {ref_system}",
                    f"ref. units  : {ref_units}",
                    "unit dist.  : 1.0000000",
                    f"min. X      : {x_min:.3f}",
                    f"max. X      : {x_max:.3f}",
                    f"min. Y      : {y_min:.3f}",
                    f"max. Y      : {y_max:.3f}",
                    "pos'n error : unknown",
                    f"resolution  : {abs(transform.a):.8f}",
                    f"min. value  : {arr_min_v}",
                    f"max. value  : {arr_max_v}",
                    f"display min : {arr_min_v}",
                    f"display max : {arr_max_v}",
                    f"value units : {value_units}",
                    "value error : unknown",
                    f"flag value  : {int(nodata)}",
                    "flag def'n  : missing data",
                ]
                + legend_lines
            )
            + "\n",
            encoding="ascii",
        )
        if legend:
            _write_idrisi_pal(pal_tmp, legend)
            _write_idrisi_smp(smp_tmp, legend)
    expected_rst_bytes = int(width) * int(height) * np.dtype(np.int16).itemsize
    if not rst_tmp.exists() or rst_tmp.stat().st_size != expected_rst_bytes:
        raise RuntimeError(
            f"IDRISI temporary raster has invalid size for {geotiff_path.name}: "
            f"{rst_tmp.stat().st_size if rst_tmp.exists() else 0} bytes, expected {expected_rst_bytes}"
        )
    if not rdc_tmp.exists() or rdc_tmp.stat().st_size <= 0:
        raise RuntimeError(f"IDRISI temporary metadata was not written for {geotiff_path.name}")
    if legend and (not pal_tmp.exists() or pal_tmp.stat().st_size <= 0):
        raise RuntimeError(f"IDRISI temporary palette was not written for {geotiff_path.name}")
    if legend and (not smp_tmp.exists() or smp_tmp.stat().st_size <= 0):
        raise RuntimeError(f"IDRISI temporary symbol palette was not written for {geotiff_path.name}")

    rst_tmp.replace(rst_path)
    rdc_tmp.replace(rdc_path)
    if legend:
        pal_tmp.replace(pal_path)
        smp_tmp.replace(smp_path)
    else:
        pal_path.unlink(missing_ok=True)
        smp_path.unlink(missing_ok=True)
        pal_tmp.unlink(missing_ok=True)
        smp_tmp.unlink(missing_ok=True)
    return rst_path


def _idrisi_outputs_current(geotiff_path: Path, idrisi_directory: Path) -> bool:
    rst_path = idrisi_directory / f"{geotiff_path.stem}.rst"
    rdc_path = idrisi_directory / f"{geotiff_path.stem}.rdc"
    pal_path = idrisi_directory / f"{geotiff_path.stem}.pal"
    smp_path = idrisi_directory / f"{geotiff_path.stem}.smp"
    if not rst_path.exists() or not rdc_path.exists():
        return False
    source_mtime = geotiff_path.stat().st_mtime
    if rst_path.stat().st_mtime < source_mtime or rdc_path.stat().st_mtime < source_mtime:
        return False
    with rasterio.open(geotiff_path) as dataset:
        width, height = _idrisi_expected_dimensions(dataset)
    expected_rst_bytes = int(width) * int(height) * np.dtype(np.int16).itemsize
    if rst_path.stat().st_size != expected_rst_bytes:
        return False
    product_type = _idrisi_product_type(geotiff_path.stem)
    legend = _IDRISI_LEGENDS.get(product_type, [])
    title = _idrisi_title(geotiff_path.stem)
    if not _rdc_has_current_title(rdc_path, title):
        return False
    if legend and (
        not pal_path.exists()
        or pal_path.stat().st_mtime < source_mtime
        or pal_path.stat().st_size <= 0
        or not smp_path.exists()
        or smp_path.stat().st_mtime < source_mtime
        or smp_path.stat().st_size <= 0
    ):
        return False
    if legend:
        try:
            if pal_path.read_text(encoding="ascii") != _idrisi_pal_text(legend):
                return False
            if smp_path.read_bytes() != _idrisi_smp_bytes(legend):
                return False
        except Exception:
            return False
        if not _rdc_has_current_legend(rdc_path, legend):
            return False
    elif pal_path.exists() or smp_path.exists():
        return False
    return True


def _idrisi_expected_dimensions(dataset: rasterio.DatasetReader) -> tuple[int, int]:
    if not _needs_reproject(dataset):
        return dataset.width, dataset.height
    _transform, width, height = calculate_default_transform(
        dataset.crs,
        _IDRISI_TARGET_CRS,
        dataset.width,
        dataset.height,
        left=dataset.bounds.left,
        bottom=dataset.bounds.bottom,
        right=dataset.bounds.right,
        top=dataset.bounds.top,
        resolution=_IDRISI_TARGET_RESOLUTION_M,
    )
    return width, height


def _cleanup_idrisi_temporaries(geotiff_path: Path, idrisi_directory: Path) -> None:
    for suffix in (".rst.tmp", ".rdc.tmp", ".pal.tmp", ".smp.tmp"):
        (idrisi_directory / f"{geotiff_path.stem}{suffix}").unlink(missing_ok=True)


def _pending_task_descriptions() -> set[str]:
    try:
        return {
            str(task["description"])
            for task in ee.data.getTaskList()
            if task.get("state") in {"READY", "RUNNING"}
        }
    except Exception:
        return set()
