"""Semantic raster-name normalization used to collapse duplicate products."""

from __future__ import annotations

from pathlib import Path
import re


_YEAR_TO_TX: dict[int, str] = {
    1985: "T0_1985",
    2009: "T1_2009",
    2013: "T2_2013",
    2018: "T3_2018",
    2024: "T4_2024",
}

_PERIOD_LABEL: dict[tuple[int, int], str] = {
    (1985, 2009): "Hist_1985_2009",
    (2009, 2013): "Cal_2009_2013",
    (2013, 2018): "Con_2013_2018",
    (2018, 2024): "T3T4_2018_2024",
    (1985, 2024): "T0T4_1985_2024",
}

_LABEL_TO_PERIOD: dict[str, tuple[int, int]] = {
    label.lower(): years for years, label in _PERIOD_LABEL.items()
}


def raster_product_stem(name: str | Path) -> str:
    """Return the product stem, stripping GeoTIFF tile row/column suffixes."""
    stem = Path(str(name)).stem
    for separator in ("-", "_"):
        parts = stem.rsplit(separator, 2)
        if len(parts) == 3 and all(part.isdigit() for part in parts[1:]):
            return parts[0]
    return stem


def strip_raster_grid_suffix(stem: str) -> str:
    """Remove CRS/resolution suffix variants without changing product meaning."""
    clean = raster_product_stem(stem)
    patterns = (
        r"(?:_\d+m)?_EPSG_\d+_\d+m$",
        r"(?:_\d+m)?_EPSG_\d+$",
        r"_\d+m$",
    )
    changed = True
    while changed:
        changed = False
        for pattern in patterns:
            updated = re.sub(pattern, "", clean, flags=re.IGNORECASE)
            if updated != clean:
                clean = updated
                changed = True
    return clean


def raster_semantic_key(name: str | Path) -> str:
    """Return a stable key for rasters that show the same variable."""
    core = strip_raster_grid_suffix(str(name))
    lower = core.lower()

    if lower in {"valid_analysis_mask", "udefa_validmask"}:
        return "valid_mask"

    key = _fcbm_key(core, lower)
    if key is not None:
        return key

    key = _mapbiomas_key(core, lower)
    if key is not None:
        return key

    key = _ctrees_key(core, lower)
    if key is not None:
        return key

    normalized = re.sub(r"[^a-z0-9]+", "_", lower).strip("_")
    return normalized or lower


def preferred_raster_product_stem(stems: list[str] | set[str]) -> str:
    """Choose the canonical filename stem to keep for a duplicate semantic group."""
    if not stems:
        raise ValueError("Cannot choose a preferred raster stem from an empty group.")
    return sorted(set(stems), key=_raster_name_sort_key)[0]


def is_preferred_raster_product_stem(stem: str, candidates: list[str] | set[str]) -> bool:
    """Return True when stem is the preferred representative among candidates."""
    group = [candidate for candidate in candidates if raster_semantic_key(candidate) == raster_semantic_key(stem)]
    return stem == preferred_raster_product_stem(group or [stem])


def _mapbiomas_key(core: str, lower: str) -> str | None:
    year = _year_from_text(core)
    period = _period_from_text(core)

    if "persistence" in lower:
        scenario = re.sub(r".*persistence_(?:scen)?", "", lower, flags=re.IGNORECASE)
        return f"mb_persistence_{_key_part(scenario)}"

    if ("lulc" in lower or "landcover" in lower or "land_cover" in lower) and year is not None:
        return f"mb_lulc_{year}"

    if ("udefa_mb_" in lower or "mapbiomas" in lower) and ("distfromnf" in lower or "dist_from_nf" in lower):
        tx = _tx_from_text(core)
        return f"mb_distance_from_nonforest_{tx or year or 'unknown'}"

    if (
        "udefa_mb_nonforest_input" in lower
        or "udefa_mb_non_forest_input" in lower
        or "udefa_mapbiomas_nonforest_" in lower
        or "mapbiomas_nonforest_" in lower
    ):
        tx = _tx_from_text(core)
        return f"mb_nonforest_{tx or year or 'unknown'}"

    if (
        "forestnonforest" in lower
        or "binary_forest" in lower
        or "udefa_mb_forest_input" in lower
        or "mapbiomas_forest_" in lower
        or "udefa_mb_forest_" in lower
        or "udefa_mapbiomas_forest_" in lower
    ) and year is not None:
        return f"mb_forest_{year}"

    if (
        "mapbiomas_change" in lower
        or "udefa_mb_forestchange4" in lower
    ) and period is not None:
        return f"mb_forest_change4_{period[0]}_{period[1]}"

    if (
        "mapbiomas_forestloss" in lower
        or "udefa_mb_forestloss" in lower
        or "change_foresttononforest_mapbiomas" in lower
    ) and period is not None:
        return f"mb_forest_loss_{period[0]}_{period[1]}"

    if "udefa_mb_dmjss" in lower:
        return "mb_dmjss"

    return None


def _ctrees_key(core: str, lower: str) -> str | None:
    year = _year_from_text(core)
    period = _period_from_text(core)

    has_ct_source = "udefa_ct_" in lower or "udefa_ctrees_" in lower or "ctrees_" in lower

    if has_ct_source and ("distfromnf" in lower or "dist_from_nf" in lower):
        tx = _tx_from_text(core)
        return f"ct_distance_from_nonforest_{tx or year or 'unknown'}"

    if (
        "udefa_ct_nonforest_input" in lower
        or "udefa_ct_non_forest_input" in lower
        or "udefa_ctrees_nonforest_" in lower
        or "ctrees_nonforest_" in lower
    ):
        tx = _tx_from_text(core)
        return f"ct_nonforest_{tx or year or 'unknown'}"

    if (
        "udefa_ct_forest_input" in lower
        or "ctrees_forest_" in lower
        or "udefa_ct_forest_" in lower
        or "udefa_ctrees_forest_" in lower
        or "ctrees_fcbm1" in lower
        or "ctrees_fcbm2" in lower
        or "ctrees_fcbm3" in lower
    ) and year is not None:
        return f"ct_forest_{year}"

    if "fcbm4_forestloss" in lower or "change_foresttononforest_ctrees_fcbm4" in lower:
        return "ct_fcbm4_forest_loss"

    if "ct_forestloss" in lower or "change_foresttononforest_ctrees" in lower:
        if period is not None:
            return f"ct_forest_loss_{period[0]}_{period[1]}"

    if ("forestchange4" in lower or "ctrees_forestchange" in lower) and period is not None:
        return f"ct_forest_change4_{period[0]}_{period[1]}"

    if ("agreement" in lower or "cross_forestlossagreement" in lower) and period is not None:
        return f"ct_mb_loss_agreement_{period[0]}_{period[1]}"

    if "udefa_ct_dmjss" in lower or lower == "ctrees_dmjss":
        return "ct_dmjss"

    if "udefa_ct_fcbm4" in lower or lower == "ctrees_fcbm4":
        return "ct_fcbm4"

    return None


def _fcbm_key(core: str, lower: str) -> str | None:
    source = None
    if "udefa_ct_fcbm" in lower or "ctrees_fcbm" in lower:
        source = "ct"
    elif "udefa_mb_fcbm" in lower or "mapbiomas_fcbm" in lower:
        source = "mb"
    if source is None:
        return None

    if "accuracy" in lower or "table16" in lower:
        return f"{source}_fcbm_accuracy"
    if "vt0007" in lower or "table15" in lower:
        return f"{source}_fcbm_vt0007"
    if "transidx" in lower or "index" in lower or "transition" in lower:
        return f"{source}_fcbm_transition_index"
    return None


def _year_from_text(text: str) -> int | None:
    years = [int(value) for value in re.findall(r"(?:19|20)\d{2}", text)]
    if len(years) == 1:
        return years[0]
    return None


def _tx_from_text(text: str) -> str | None:
    match = re.search(r"\b(T[0-4])[_-]((?:19|20)\d{2})\b", text, flags=re.IGNORECASE)
    if match:
        return f"{match.group(1).upper()}_{match.group(2)}"
    year = _year_from_text(text)
    if year is not None:
        return _YEAR_TO_TX.get(year, str(year))
    return None


def _period_from_text(text: str) -> tuple[int, int] | None:
    lower = text.lower()
    for label, period in _LABEL_TO_PERIOD.items():
        if label in lower:
            return period

    years = [int(value) for value in re.findall(r"(?:19|20)\d{2}", text)]
    if len(years) >= 2:
        return years[0], years[1]

    if "calibration" in lower or "_cal" in lower:
        return 2009, 2013
    if "confirmation" in lower or "_con" in lower:
        return 2013, 2018
    if "hrp" in lower:
        return 2009, 2018
    return None


def _raster_name_sort_key(stem: str) -> tuple[int, int, int, str]:
    core = strip_raster_grid_suffix(stem)
    lower = core.lower()
    canonical_prefix = int(
        not (
            lower.startswith("udefa_mb_")
            or lower.startswith("udefa_ct_")
            or lower.startswith("udefa_validmask")
        )
    )
    legacy_projection_order = int("_30m_epsg_" in stem.lower())
    legacy_long_source = int(
        lower.startswith("mapbiomas_")
        or lower.startswith("ctrees_")
        or lower.startswith("udefa_mapbiomas_")
        or lower.startswith("udefa_ctrees_")
        or lower.startswith("change_foresttononforest_")
        or lower.startswith("cross_forestlossagreement_")
        or lower.startswith("valid_analysis_mask")
    )
    return (canonical_prefix, legacy_projection_order, legacy_long_source, stem.lower())


def _key_part(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
