"""Shared data structures for the workflow."""

from __future__ import annotations

from dataclasses import dataclass

import ee
import pandas as pd


@dataclass(frozen=True)
class ReferenceRaster:
    """A CTrees reference raster and its class schema."""

    name: str
    label: str
    image: ee.Image | None
    class_codes: list[int]
    class_labels: dict[int, str]
    groups: dict[str, list[str]]


@dataclass(frozen=True)
class OrganizedData:
    """Cleaned Earth Engine rasters used for export."""

    valid_analysis_mask: ee.Image
    persistence_rasters: dict[str, ee.Image]
    references: dict[str, ReferenceRaster]


@dataclass(frozen=True)
class AnalysisResults:
    """Final local analytical tables."""

    area_tables: dict[str, pd.DataFrame]
    crosstab_tables: dict[tuple[str, str], pd.DataFrame]
    crosstab_percent_tables: dict[tuple[str, str], pd.DataFrame]
    agreement_metrics: pd.DataFrame
    reclassification_table: pd.DataFrame
    fcbm_comparison_tables: dict[str, pd.DataFrame]
    fcbm_comparison_metrics: pd.DataFrame
    all_class_tables: dict[str, pd.DataFrame]
    all_class_metrics: pd.DataFrame
    change_agreement_tables: dict[str, pd.DataFrame]
    change_area_timeseries: pd.DataFrame
    class_decomposition_tables: dict[str, pd.DataFrame]
    spatial_disagreement_tables: dict[str, pd.DataFrame]
    temporal_consistency: pd.DataFrame
