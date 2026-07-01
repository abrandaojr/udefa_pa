"""Section 1. Data preparation."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import re
from typing import Any

import ee

LOGGER = logging.getLogger(__name__)
_MAPBIOMAS_YEAR_CACHE: dict[int, list[int]] = {}


@dataclass(frozen=True)
class PreparedInputs:
    """Source data loaded from Earth Engine."""

    area_of_interest: ee.FeatureCollection
    mapbiomas_image: ee.Image
    pixel_area_hectares: ee.Image
    spatial_units: ee.FeatureCollection | None = None


def prepare_input_data(settings: dict[str, Any]) -> PreparedInputs:
    """Load the study area, MapBiomas image, and pixel-area raster."""
    earth_engine = settings["earth_engine"]
    aoi_settings = earth_engine["aoi"]

    _assert_readable_asset(aoi_settings["states_feature_collection"], "Area of interest")
    _assert_readable_asset(earth_engine["mapbiomas_image"], "MapBiomas image")

    area_of_interest = ee.FeatureCollection(aoi_settings["states_feature_collection"]).filter(
        ee.Filter.eq(aoi_settings["state_property"], str(aoi_settings["state_code"]))
    )
    mapbiomas_image = ee.Image(earth_engine["mapbiomas_image"])
    pixel_area_hectares = ee.Image.pixelArea().divide(10000).rename("area_hectares")
    spatial_units = _load_spatial_units(settings, area_of_interest)

    return PreparedInputs(
        area_of_interest=area_of_interest,
        mapbiomas_image=mapbiomas_image,
        pixel_area_hectares=pixel_area_hectares,
        spatial_units=spatial_units,
    )


def select_mapbiomas_year(mapbiomas_image: ee.Image, requested_year: int) -> ee.Image:
    """Select the requested MapBiomas year."""
    resolved_year = resolve_mapbiomas_year(mapbiomas_image, requested_year)
    return mapbiomas_image.select(f"classification_{resolved_year}")


def resolve_mapbiomas_year(mapbiomas_image: ee.Image, requested_year: int) -> int:
    """Return an available MapBiomas classification year for a requested analysis year."""
    available_years = _available_mapbiomas_years(mapbiomas_image)
    if requested_year in available_years:
        return requested_year
    raise RuntimeError(
        f"MapBiomas classification_{requested_year} is unavailable in the configured asset. "
        "Use MapBiomas Collection 10 or a later collection that includes all required primary years, including 2024."
    )


def resolve_mapbiomas_years(mapbiomas_image: ee.Image, requested_years: list[int] | range) -> list[int]:
    """Validate and return a sequence of requested MapBiomas classification years."""
    resolved: list[int] = []
    for requested_year in requested_years:
        year = resolve_mapbiomas_year(mapbiomas_image, int(requested_year))
        if not resolved or resolved[-1] != year:
            resolved.append(year)
    return resolved


def _available_mapbiomas_years(mapbiomas_image: ee.Image) -> list[int]:
    cache_key = id(mapbiomas_image)
    if cache_key in _MAPBIOMAS_YEAR_CACHE:
        return _MAPBIOMAS_YEAR_CACHE[cache_key]
    band_names = [str(name) for name in mapbiomas_image.bandNames().getInfo()]
    years = sorted(
        int(match.group(1))
        for band_name in band_names
        if (match := re.fullmatch(r"classification_(\d{4})", band_name))
    )
    if not years:
        raise RuntimeError("The MapBiomas image does not contain classification_YYYY bands.")
    _MAPBIOMAS_YEAR_CACHE[cache_key] = years
    return years


def _assert_readable_asset(asset_id: str, label: str) -> None:
    try:
        ee.data.getAsset(asset_id)
    except Exception as exc:
        raise RuntimeError(f"{label} asset is missing or unreadable: {asset_id}") from exc


def _load_spatial_units(
    settings: dict[str, Any],
    area_of_interest: ee.FeatureCollection,
) -> ee.FeatureCollection | None:
    spatial_settings = settings.get("analysis", {}).get("spatial_units", {})
    asset_id = str(spatial_settings.get("asset") or "").strip()
    if not asset_id:
        LOGGER.info("No IBGE municipality asset configured; municipality-level exports will be skipped.")
        return None
    _assert_readable_asset(asset_id, str(spatial_settings.get("provider", "Spatial units")))
    return ee.FeatureCollection(asset_id).filterBounds(area_of_interest.geometry())
