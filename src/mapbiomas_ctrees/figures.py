"""Lightweight PNG figure generation for report publication."""

from __future__ import annotations

import math
from pathlib import Path
import time
import urllib.request
from typing import Any

import ee
import pandas as pd
from PIL import Image, ImageDraw, ImageFont

from .constants import MAPBIOMAS_CLASS_COLORS, MAPBIOMAS_LAND_COVER_CLASSES, PRIMARY_MAPBIOMAS_YEARS
from .data_preparation import PreparedInputs, resolve_mapbiomas_year, select_mapbiomas_year
from .models import AnalysisResults, OrganizedData
from .settings import Scenario


WIDTH = 1600
HEIGHT = 1000
MARGIN = 110
INK = "#202124"
MUTED = "#5f6368"
GRID = "#d0d7de"
BLUE = "#2b6cb0"
GREEN = "#238b45"
ORANGE = "#d95f02"
RED = "#c0392b"
LOSS_RED = "#ff2d2d"
CTREES_ONLY_CYAN = "#00e5ff"
MAPBIOMAS_ONLY_YELLOW = "#ffcc00"
PURPLE = "#7570b3"
YELLOW = "#e6ab02"
SKY = "#66a9cf"
PAPER = "#ffffff"
PALETTE_PERSISTENCE = ["238b45", "c7b37f", "d95f02"]
PALETTE_BINARY = ["c7b37f", "238b45"]
PALETTE_CHANGE = ["c7b37f", "238b45", "ff2d2d", "66a9cf", "e0b85a"]
PALETTE_CONCORDANCE = ["bdbdbd", "238b45", "c7b37f", "c0392b"]
PALETTE_CHANGE4 = ["238b45", "c7b37f", "ff2d2d", "66a9cf"]
PALETTE_AGREEMENT5 = ["238b45", "c7b37f", "d95f02", "66a9cf", "c0392b"]
PALETTE_LOSS_AGREEMENT3 = ["ff2d2d", "00e5ff", "ffcc00"]
MAPBIOMAS_LEGEND_GROUPS = [
    (
        "Forest",
        [1, 3, 4, 5, 6, 49],
    ),
    (
        "Herbaceous and Shrubby Vegetation",
        [10, 11, 12, 32, 29, 50],
    ),
    (
        "Farming",
        [14, 15, 18, 19, 39, 20, 40, 62, 41, 36, 46, 47, 35, 48, 9, 21],
    ),
    (
        "Non vegetated area",
        [22, 23, 24, 30, 75, 25],
    ),
    (
        "Water",
        [26, 33, 31],
    ),
    (
        "Not Observed",
        [27],
    ),
]
PERSISTENCE_COLORS = {
    "Persistent Forest": GREEN,
    "Persistent Non-Forest": "#c7b37f",
    "Land-Cover Change": ORANGE,
}


def generate_report_figures(results: AnalysisResults, figure_directory: Path) -> list[Path]:
    """Generate core report figures from final analysis tables."""
    figure_directory.mkdir(parents=True, exist_ok=True)
    paths = [
        _agreement_metrics_figure(results.agreement_metrics, figure_directory),
        _area_bar_figure(results.area_tables, figure_directory),
    ]
    temporal_consistency = _temporal_consistency_or_fallback(results)
    temporal_consistency_figure = _temporal_consistency_figure(temporal_consistency, figure_directory)
    if temporal_consistency_figure is not None:
        paths.append(temporal_consistency_figure)
    change_area_timeseries = _change_area_timeseries_figure(_change_area_timeseries_or_fallback(results), figure_directory)
    if change_area_timeseries is not None:
        paths.append(change_area_timeseries)
    paths.extend(_class_decomposition_figures(results.class_decomposition_tables, figure_directory))
    paths.extend(_spatial_disagreement_figures(results.spatial_disagreement_tables, figure_directory))
    paths.extend(_crosstab_heatmaps(results.crosstab_percent_tables, figure_directory))
    return [path for path in paths if path.exists()]


def _remove_legacy_figure(output_directory: Path, *names: str) -> None:
    """Remove figure files whose names no longer describe their contents."""
    for name in names:
        (output_directory / name).unlink(missing_ok=True)


def generate_earth_engine_report_maps(
    settings: dict[str, Any],
    scenarios: list[Scenario],
    prepared: PreparedInputs,
    organized: OrganizedData,
    figure_directory: Path,
    change_agreement_tables: dict[str, pd.DataFrame] | None = None,
    pixel_area_hectares: float = 0.09,
) -> list[Path]:
    """Generate map PNGs expected by the Google Docs report."""
    figure_directory.mkdir(parents=True, exist_ok=True)
    region = prepared.area_of_interest.geometry().bounds()
    boundary = prepared.area_of_interest
    dimensions = 1400
    paths: list[Path] = []

    scenario_by_id = {scenario.identifier: scenario for scenario in scenarios}
    for scenario_id in ("A", "C", "F"):
        scenario = scenario_by_id.get(scenario_id)
        if scenario is None:
            continue
        raster = organized.persistence_rasters.get(scenario.label)
        if raster is None:
            continue
        path = figure_directory / f"Map_Scen{scenario_id}_{scenario.threshold_percent:g}pct_{scenario.start_year}_{scenario.end_year}.png"
        paths.append(
            _thumbnail_map(
                raster,
                path,
                region,
                f"Scenario {scenario_id}: MapBiomas forest persistence ({scenario.start_year}-{scenario.end_year})",
                {"min": 1, "max": 3, "palette": PALETTE_PERSISTENCE},
                dimensions,
                [("Persistent forest", GREEN), ("Persistent non-forest", "#c7b37f"), ("Land-cover change", ORANGE)],
                boundary=boundary,
            )
        )

    for requested_display_year in PRIMARY_MAPBIOMAS_YEARS:
        display_year = resolve_mapbiomas_year(prepared.mapbiomas_image, requested_display_year)
        mb_annual = select_mapbiomas_year(prepared.mapbiomas_image, requested_display_year).clip(prepared.area_of_interest)
        visual_image, visual_params, legend = _mapbiomas_land_cover_visualization(mb_annual)
        paths.append(
            _thumbnail_map(
                visual_image,
                figure_directory / f"Map_MB_Annual_{display_year}.png",
                region,
                f"MapBiomas Collection 10 land cover, {display_year}",
                visual_params,
                dimensions,
                legend,
                boundary=boundary,
            )
        )

    paths.extend(_snapshot_panel(prepared, organized, figure_directory, region, dimensions))
    paths.extend(_agreement_maps(organized, figure_directory, region, dimensions, boundary))
    paths.extend(_forest_to_nonforest_maps(settings, prepared, organized, figure_directory, region, dimensions, boundary))
    if change_agreement_tables is not None:
        forest_codes = [int(code) for code in settings["analysis"]["forest_codes"]]
        paths.extend(
            _change_agreement_panels(
                prepared=prepared,
                organized=organized,
                output_directory=figure_directory,
                region=region,
                dimensions=dimensions,
                forest_codes=forest_codes,
                pixel_area_hectares=pixel_area_hectares,
                change_agreement_tables=change_agreement_tables,
            )
        )
    return [path for path in paths if path.exists()]


def generate_change_area_figure(table_directory: Path, figure_directory: Path) -> Path | None:
    """Generate a publication figure for forest-to-nonforest area estimates."""
    output = figure_directory / "Figure_07_ForestToNonForest_Area_30m.png"
    frame = _mapbiomas_interval_loss_frame(table_directory)
    if frame.empty:
        output.unlink(missing_ok=True)
        output.with_suffix(".pdf").unlink(missing_ok=True)
        return None
    figure_directory.mkdir(parents=True, exist_ok=True)
    image, draw = _canvas(
        "MapBiomas Forest Loss by Interval",
        "Area classified as forest at the start year and non-forest at the end year for primary MapBiomas intervals.",
    )
    labels = [str(value) for value in frame["interval"]]
    values = [float(value) for value in frame["area_million_hectares"]]
    _draw_horizontal_bar_chart(draw, labels, values, "Area (Mha)", max_value=max(max(values), 1), color=ORANGE)
    _save_figure(image, output)
    return output


def _mapbiomas_interval_loss_frame(table_directory: Path) -> pd.DataFrame:
    interval_path = table_directory / "change_area_by_interval.csv"
    if interval_path.exists():
        interval_frame = _read_csv_or_empty(interval_path)
        if not interval_frame.empty and {"dataset", "interval", "forest_loss_million_hectares"}.issubset(interval_frame.columns):
            frame = interval_frame[interval_frame["dataset"].astype(str).str.lower().eq("mapbiomas")].copy()
            frame = frame.rename(columns={"forest_loss_million_hectares": "area_million_hectares"})
            return _ordered_mapbiomas_intervals(frame[["interval", "area_million_hectares"]])
        if not interval_frame.empty and {"interval", "mapbiomas_loss_million_hectares"}.issubset(interval_frame.columns):
            frame = interval_frame.rename(columns={"mapbiomas_loss_million_hectares": "area_million_hectares"})
            return _ordered_mapbiomas_intervals(frame[["interval", "area_million_hectares"]])

    for filename in ("change_area_forest_to_nonforest.csv", "ChangeArea_ForestToNonForest_30m.csv"):
        path = table_directory / filename
        if not path.exists():
            continue
        frame = _read_csv_or_empty(path)
        if not {"source", "area_million_hectares"}.issubset(frame.columns):
            continue
        frame = frame[frame["source"].astype(str).str.startswith("Change_ForestToNonForest_MapBiomas_")].copy()
        if frame.empty:
            continue
        years = frame["source"].astype(str).str.extract(r"MapBiomas_(\d{4})_(\d{4})")
        frame["interval"] = years[0] + "-" + years[1]
        return _ordered_mapbiomas_intervals(frame[["interval", "area_million_hectares"]])
    return pd.DataFrame(columns=["interval", "area_million_hectares"])


def _read_csv_or_empty(path: Path) -> pd.DataFrame:
    """Read a CSV table, treating empty or malformed partial exports as unavailable."""
    try:
        return pd.read_csv(path)
    except (pd.errors.EmptyDataError, FileNotFoundError):
        return pd.DataFrame()


def _ordered_mapbiomas_intervals(frame: pd.DataFrame) -> pd.DataFrame:
    order = ["1985-2009", "2009-2013", "2013-2018", "2018-2024"]
    frame = frame[frame["interval"].isin(order)].copy()
    frame["interval"] = pd.Categorical(frame["interval"], categories=order, ordered=True)
    return frame.sort_values("interval")


def _agreement_metrics_figure(metrics: pd.DataFrame, output_directory: Path) -> Path:
    path = output_directory / "Figure_01_AgreementMetrics_30m.png"
    image, draw = _canvas(
        "Highest CTrees and MapBiomas Agreement Values",
        "Top 15 overall agreement values from cross-tabulated MapBiomas persistence classes and CTrees reference products. Full results are reported in agreement_metrics.csv.",
    )
    if metrics.empty:
        _center_text(draw, "No agreement metrics available.", WIDTH / 2, HEIGHT / 2, 36)
        _save_figure(image, path)
        return path

    frame = metrics.copy().sort_values("overall_agreement_percent", ascending=False).head(15)
    labels = [
        f"{_clean_chart_label(str(row.scenario))} x {row.reference}"
        for row in frame.itertuples()
    ]
    values = [float(value) if pd.notna(value) else 0 for value in frame["overall_agreement_percent"]]
    _draw_horizontal_bar_chart(draw, labels, values, "Overall agreement (%)", max_value=100, color=BLUE)
    _save_figure(image, path)
    return path


def _change_area_timeseries_or_fallback(results: AnalysisResults) -> pd.DataFrame:
    """Return forest-loss time series data, deriving it from area tables when the direct table is absent."""
    normalized = _normalize_change_area_timeseries(results.change_area_timeseries)
    if not normalized.empty:
        return normalized

    rows_by_interval: dict[str, dict[str, float | str]] = {}
    for source_name, table in _iter_result_tables(results):
        interval = _forest_loss_interval_from_source(source_name)
        dataset = _forest_loss_dataset_from_source(source_name)
        if table.empty:
            continue

        normalized = _normalize_change_area_timeseries(table)
        if not normalized.empty:
            for row in normalized.itertuples(index=False):
                target = rows_by_interval.setdefault(
                    str(row.interval),
                    {
                        "interval": str(row.interval),
                        "ctrees_loss_million_hectares": 0.0,
                        "mapbiomas_loss_million_hectares": 0.0,
                    },
                )
                target["ctrees_loss_million_hectares"] = max(
                    float(target["ctrees_loss_million_hectares"]),
                    float(row.ctrees_loss_million_hectares),
                )
                target["mapbiomas_loss_million_hectares"] = max(
                    float(target["mapbiomas_loss_million_hectares"]),
                    float(row.mapbiomas_loss_million_hectares),
                )
            continue

        if interval is None or dataset is None:
            continue
        area_mha = _forest_loss_area_from_area_table(table)
        if area_mha is None:
            area_mha = _forest_loss_area_from_total_table(table)
        if area_mha is None:
            continue

        row = rows_by_interval.setdefault(
            interval,
            {
                "interval": interval,
                "ctrees_loss_million_hectares": 0.0,
                "mapbiomas_loss_million_hectares": 0.0,
            },
        )
        row[f"{dataset}_loss_million_hectares"] = float(area_mha)

    if not rows_by_interval:
        return pd.DataFrame(
            columns=[
                "interval",
                "ctrees_loss_million_hectares",
                "mapbiomas_loss_million_hectares",
            ]
        )
    frame = pd.DataFrame(rows_by_interval.values())
    frame["interval"] = pd.Categorical(
        frame["interval"],
        categories=["2009-2013", "2013-2018", "2018-2024"],
        ordered=True,
    )
    return frame.sort_values("interval").reset_index(drop=True)


def _iter_result_tables(results: AnalysisResults) -> list[tuple[str, pd.DataFrame]]:
    """Yield every dataframe carried by AnalysisResults with a stable source name."""
    tables: list[tuple[str, pd.DataFrame]] = []
    for field_name, value in vars(results).items():
        if isinstance(value, pd.DataFrame):
            tables.append((field_name, value))
        elif isinstance(value, dict):
            for key, item in value.items():
                if isinstance(item, pd.DataFrame):
                    tables.append((str(key), item))
    return tables


def _normalize_change_area_timeseries(frame: pd.DataFrame) -> pd.DataFrame:
    """Normalize known forest-loss time-series table layouts to the figure schema."""
    output_columns = [
        "interval",
        "ctrees_loss_million_hectares",
        "mapbiomas_loss_million_hectares",
    ]
    if frame.empty:
        return pd.DataFrame(columns=output_columns)

    if set(output_columns).issubset(frame.columns):
        normalized = frame[output_columns].copy()
        normalized["ctrees_loss_million_hectares"] = pd.to_numeric(
            normalized["ctrees_loss_million_hectares"], errors="coerce"
        ).fillna(0)
        normalized["mapbiomas_loss_million_hectares"] = pd.to_numeric(
            normalized["mapbiomas_loss_million_hectares"], errors="coerce"
        ).fillna(0)
        return normalized

    if {"interval", "dataset", "forest_loss_million_hectares"}.issubset(frame.columns):
        working = frame.copy()
        working["dataset_key"] = working["dataset"].astype(str).str.lower()
        working["value"] = pd.to_numeric(working["forest_loss_million_hectares"], errors="coerce").fillna(0)
        rows = []
        for interval, group in working.groupby("interval", dropna=False):
            rows.append(
                {
                    "interval": str(interval),
                    "ctrees_loss_million_hectares": float(
                        group.loc[group["dataset_key"].str.contains("ctrees|fcbm", regex=True), "value"].sum()
                    ),
                    "mapbiomas_loss_million_hectares": float(
                        group.loc[group["dataset_key"].str.contains("mapbiomas", regex=False), "value"].sum()
                    ),
                }
            )
        return pd.DataFrame(rows, columns=output_columns)

    if {"interval", "source", "area_million_hectares"}.issubset(frame.columns):
        working = frame.copy()
        working["dataset_key"] = working["source"].astype(str).str.lower()
        working["value"] = pd.to_numeric(working["area_million_hectares"], errors="coerce").fillna(0)
        rows = []
        for interval, group in working.groupby("interval", dropna=False):
            rows.append(
                {
                    "interval": str(interval),
                    "ctrees_loss_million_hectares": float(
                        group.loc[group["dataset_key"].str.contains("ctrees|fcbm", regex=True), "value"].sum()
                    ),
                    "mapbiomas_loss_million_hectares": float(
                        group.loc[group["dataset_key"].str.contains("mapbiomas", regex=False), "value"].sum()
                    ),
                }
            )
        return pd.DataFrame(rows, columns=output_columns)

    if {"source", "area_million_hectares"}.issubset(frame.columns):
        rows_by_interval: dict[str, dict[str, float | str]] = {}
        for row in frame.itertuples(index=False):
            source = str(getattr(row, "source"))
            interval = _forest_loss_interval_from_source(source)
            dataset = _forest_loss_dataset_from_source(source)
            if interval is None or dataset is None:
                continue
            value = pd.to_numeric(getattr(row, "area_million_hectares"), errors="coerce")
            value = 0.0 if pd.isna(value) else float(value)
            target = rows_by_interval.setdefault(
                interval,
                {
                    "interval": interval,
                    "ctrees_loss_million_hectares": 0.0,
                    "mapbiomas_loss_million_hectares": 0.0,
                },
            )
            target[f"{dataset}_loss_million_hectares"] = value
        return pd.DataFrame(rows_by_interval.values(), columns=output_columns)

    return pd.DataFrame(columns=output_columns)


def _forest_loss_interval_from_source(source_name: str) -> str | None:
    name = str(source_name)
    if "2009" in name and "2013" in name:
        return "2009-2013"
    if "2013" in name and "2018" in name:
        return "2013-2018"
    if "2018" in name and "2024" in name:
        return "2018-2024"
    return None


def _forest_loss_dataset_from_source(source_name: str) -> str | None:
    name = str(source_name).lower()
    if "mapbiomas" in name:
        return "mapbiomas"
    if "ctrees" in name or "fcbm" in name:
        return "ctrees"
    return None


def _forest_loss_area_from_area_table(table: pd.DataFrame) -> float | None:
    area_column = next(
        (
            column
            for column in ("area_million_hectares", "million_hectares", "area_mha", "area_ha")
            if column in table.columns
        ),
        None,
    )
    if area_column is None:
        return None
    if "class" in table.columns:
        class_text = table["class"].astype(str).str.lower()
        loss_rows = table[class_text.str.contains("forest to non|forest-to-non|deforest|loss", regex=True)]
    elif "label" in table.columns:
        label_text = table["label"].astype(str).str.lower()
        loss_rows = table[label_text.str.contains("forest to non|forest-to-non|deforest|loss", regex=True)]
    else:
        loss_rows = table
    if loss_rows.empty:
        return None
    values = pd.to_numeric(loss_rows[area_column], errors="coerce").fillna(0)
    area = float(values.sum())
    if area_column == "area_ha":
        area = area / 1_000_000
    return area


def _forest_loss_area_from_total_table(table: pd.DataFrame) -> float | None:
    """Extract forest-loss area from aggregate table layouts without explicit class rows."""
    candidate_columns = (
        "forest_loss_million_hectares",
        "loss_million_hectares",
        "deforestation_million_hectares",
        "forest_to_nonforest_million_hectares",
        "forest_to_non_forest_million_hectares",
        "area_million_hectares",
        "million_hectares",
        "area_mha",
        "forest_loss_ha",
        "loss_ha",
        "deforestation_ha",
        "forest_to_nonforest_ha",
        "forest_to_non_forest_ha",
        "area_ha",
    )
    for column in candidate_columns:
        if column not in table.columns:
            continue
        values = pd.to_numeric(table[column], errors="coerce").fillna(0)
        if values.empty:
            continue
        area = float(values.sum())
        if column.endswith("_ha") or column == "area_ha":
            area = area / 1_000_000
        return area
    return None


def _change_area_timeseries_figure(frame: pd.DataFrame, output_directory: Path) -> Path | None:
    _remove_legacy_figure(output_directory, "Figure_08_ForestChangeArea_TimeSeries_30m.png")
    path = output_directory / "Figure_08_ForestLossAreaByInterval_CTreesMapBiomas_30m.png"
    required_columns = {
        "interval",
        "ctrees_loss_million_hectares",
        "mapbiomas_loss_million_hectares",
    }
    if frame.empty or not required_columns.issubset(frame.columns):
        path.unlink(missing_ok=True)
        return None
    image, draw = _canvas(
        "Forest Loss Area by Time Interval",
        "Total forest loss in CTrees and MapBiomas for shared CTrees intervals, reported in millions of hectares.",
    )
    labels = []
    values = []
    colors = []
    for row in frame.itertuples(index=False):
        labels.extend([f"{row.interval}, CTrees", f"{row.interval}, MapBiomas"])
        values.extend([float(row.ctrees_loss_million_hectares), float(row.mapbiomas_loss_million_hectares)])
        colors.extend([ORANGE, BLUE])
    _draw_grouped_horizontal_bars(draw, labels, values, colors, "Forest loss area (Mha)", max(max(values), 1))
    _save_figure(image, path)
    return path


def _temporal_consistency_or_fallback(results: AnalysisResults) -> pd.DataFrame:
    """Return temporal reversal data, deriving it from explicit regrowth/gain columns when needed."""
    if not results.temporal_consistency.empty:
        return results.temporal_consistency

    frame = results.change_area_timeseries.copy()
    if frame.empty:
        return pd.DataFrame(columns=["dataset", "reversal_million_hectares"])

    candidates = [
        ("ctrees_gain_million_hectares", "CTrees"),
        ("ctrees_regrowth_million_hectares", "CTrees"),
        ("mapbiomas_gain_million_hectares", "MapBiomas"),
        ("mapbiomas_regrowth_million_hectares", "MapBiomas"),
    ]
    rows = []
    for column, dataset in candidates:
        if column not in frame.columns:
            continue
        value = pd.to_numeric(frame[column], errors="coerce").fillna(0).sum()
        rows.append({"dataset": dataset, "reversal_million_hectares": float(value)})
    if not rows:
        return pd.DataFrame(columns=["dataset", "reversal_million_hectares"])
    return (
        pd.DataFrame(rows)
        .groupby("dataset", as_index=False)["reversal_million_hectares"]
        .sum()
        .sort_values("dataset")
    )


def _temporal_consistency_figure(frame: pd.DataFrame, output_directory: Path) -> Path | None:
    _remove_legacy_figure(output_directory, "Figure_11_TemporalReversal_Area_30m.png")
    path = output_directory / "Figure_11_TemporalReversalAreaByDataset_30m.png"
    required_columns = {"dataset", "reversal_million_hectares"}
    if frame.empty or not required_columns.issubset(frame.columns):
        path.unlink(missing_ok=True)
        return None
    image, draw = _canvas(
        "Temporal Reversal Area by Dataset",
        "Pixels with forest/non-forest reversals across the time series indicate classification uncertainty and should be excluded from stable-forest references.",
    )
    labels = [str(value) for value in frame["dataset"]]
    values = [float(value) for value in frame["reversal_million_hectares"]]
    _draw_horizontal_bar_chart(draw, labels, values, "Reversal area (Mha)", max(max(values), 1), color=RED)
    _save_figure(image, path)
    return path


def _class_decomposition_figures(tables: dict[str, pd.DataFrame], output_directory: Path) -> list[Path]:
    paths = []
    for index, (name, frame) in enumerate(sorted(tables.items()), 1):
        path = output_directory / f"Figure_09_ClassDecomposition_{index:02d}_{_safe_filename(name)}.png"
        image, draw = _canvas(
            "MapBiomas Forest Share within CTrees Classes",
            f"Section 2.D diagnostic for {name}. Bars are normalized within each CTrees class.",
        )
        if frame.empty:
            _center_text(draw, "No class decomposition data available.", WIDTH / 2, HEIGHT / 2, 34)
        else:
            pivot = frame.set_index("ctrees_class")[["mapbiomas_forest_percent", "mapbiomas_nonforest_percent"]].rename(
                columns={
                    "mapbiomas_forest_percent": "MapBiomas forest",
                    "mapbiomas_nonforest_percent": "MapBiomas non-forest",
                }
            )
            _draw_percent_stacked_bar_chart(draw, pivot, {"MapBiomas forest": GREEN, "MapBiomas non-forest": "#c7b37f"})
        _save_figure(image, path)
        paths.append(path)
    return paths


def _spatial_disagreement_figures(tables: dict[str, pd.DataFrame], output_directory: Path) -> list[Path]:
    paths = []
    for index, (name, frame) in enumerate(sorted(tables.items()), 1):
        path = output_directory / f"Figure_10_SpatialDisagreement_{index:02d}_{_safe_filename(name)}.png"
        image, draw = _canvas(
            "Municipal Concentration of CTrees and MapBiomas Disagreement",
            f"Top municipalities by disagreement density for {name}. Values are percent of evaluated pixels.",
        )
        if frame.empty or "disagreement_percent" not in frame.columns:
            _center_text(draw, "No municipal disagreement table available.", WIDTH / 2, HEIGHT / 2, 34)
        else:
            name_column = "municipality_name" if "municipality_name" in frame.columns else "NM_MUNICIP"
            top = frame.sort_values("disagreement_percent", ascending=False).head(15)
            labels = [str(value).title() for value in top[name_column]]
            values = [float(value) for value in top["disagreement_percent"]]
            _draw_horizontal_bar_chart(draw, labels, values, "Disagreement density (%)", max(max(values), 1), color=RED)
        _save_figure(image, path)
        paths.append(path)
    return paths


def _snapshot_panel(
    prepared: PreparedInputs,
    organized: OrganizedData,
    output_directory: Path,
    region: ee.Geometry,
    dimensions: int,
) -> list[Path]:
    panels = []
    for reference_name in ("FCBM1_2009", "FCBM2_2013", "FCBM3_2018"):
        reference = organized.references.get(reference_name)
        if reference and reference.image is not None:
            panels.append((reference.label, reference.image, {"min": 0, "max": 1, "palette": PALETTE_BINARY}))
    if not panels:
        return []
    _remove_legacy_figure(output_directory, "Figure_04_CTrees_Snapshot_x_MB_LULC.png")
    return [
        _panel_map(
            panels,
            output_directory / "Figure_04_CTreesSnapshotForestCover_2009_2018.png",
            "CTrees Snapshot Forest-Cover Products",
            region,
            dimensions,
            [("Non-forest", "#c7b37f"), ("Forest", GREEN)],
            boundary=prepared.area_of_interest,
        )
    ]


def _change_panel(
    organized: OrganizedData,
    output_directory: Path,
    region: ee.Geometry,
    dimensions: int,
    boundary: ee.FeatureCollection,
) -> list[Path]:
    panels = []
    for reference_name in ("DMJSS", "FCBM4"):
        reference = organized.references.get(reference_name)
        if reference and reference.image is not None:
            panels.append((reference.label, reference.image, {"min": 0, "max": 4, "palette": PALETTE_CHANGE}))
    if not panels:
        return []
    return [
        _panel_map(
            panels,
            output_directory / "Figure_05_CTrees_Change_x_MB_LULC.png",
            "CTrees change products",
            region,
            dimensions,
            [
                ("Stable non-forest", "#c7b37f"),
                ("Stable forest", GREEN),
                ("Deforestation", LOSS_RED),
                ("Regrowth", SKY),
                ("Other / buffer", "#bdbdbd"),
            ],
            boundary=boundary,
        )
    ]


def _agreement_maps(
    organized: OrganizedData,
    output_directory: Path,
    region: ee.Geometry,
    dimensions: int,
    boundary: ee.FeatureCollection,
) -> list[Path]:
    paths = []
    fcbm4 = organized.references.get("FCBM4")
    dmjss = organized.references.get("DMJSS")
    if dmjss and dmjss.image is not None:
        paths.append(
            _thumbnail_map(
                dmjss.image,
                output_directory / "Map_Agreement_Change_2009_2013.png",
                region,
                "CTrees DMJSS Forest Transition Classes",
                {"min": 0, "max": 4, "palette": PALETTE_CHANGE},
                dimensions,
                [("Stable non-forest", "#c7b37f"), ("Stable forest", GREEN), ("Deforestation", LOSS_RED), ("Regrowth", SKY)],
                boundary=boundary,
            )
        )
    if fcbm4 and fcbm4.image is not None:
        paths.append(
            _thumbnail_map(
                fcbm4.image,
                output_directory / "Map_Agreement_Change_2013_2018.png",
                region,
                "CTrees FCBM4 Forest Transition Classes",
                {"min": 0, "max": 4, "palette": PALETTE_CHANGE},
                dimensions,
                [("No data", "#bdbdbd"), ("Stable non-forest", "#c7b37f"), ("Stable forest", GREEN), ("Regrowth", SKY), ("Deforestation", LOSS_RED)],
                boundary=boundary,
            )
        )
    return paths


def _forest_to_nonforest_maps(
    settings: dict[str, Any],
    prepared: PreparedInputs,
    organized: OrganizedData,
    output_directory: Path,
    region: ee.Geometry,
    dimensions: int,
    boundary: ee.FeatureCollection,
) -> list[Path]:
    forest_codes = [int(code) for code in settings["analysis"]["forest_codes"]]
    start_year = int(settings["analysis"]["years"]["start"])
    end_year = int(settings["analysis"]["years"]["end"])
    start_forest = select_mapbiomas_year(prepared.mapbiomas_image, start_year).remap(
        forest_codes, [1] * len(forest_codes), 0
    )
    resolved_end_year = resolve_mapbiomas_year(prepared.mapbiomas_image, end_year)
    end_forest = select_mapbiomas_year(prepared.mapbiomas_image, end_year).remap(
        forest_codes, [1] * len(forest_codes), 0
    )
    products: list[tuple[str, ee.Image, str]] = [
        (
            f"Map_Change_ForestToNonForest_MapBiomas_{start_year}_{resolved_end_year}.png",
            start_forest.eq(1).And(end_forest.eq(0)).updateMask(organized.valid_analysis_mask).clip(prepared.area_of_interest),
            f"MapBiomas forest-to-nonforest change, {start_year}-{resolved_end_year}",
        )
    ]
    for reference_name in ("FCBM4",):
        reference = organized.references.get(reference_name)
        if reference is None or reference.image is None:
            continue
        deforestation_codes = [
            code for code, label in reference.class_labels.items() if "deforestation" in label.lower()
        ]
        if deforestation_codes:
            products.append(
                (
                    f"Map_Change_ForestToNonForest_CTrees_{reference_name}.png",
                    reference.image.remap(deforestation_codes, [1] * len(deforestation_codes), 0).clip(prepared.area_of_interest),
                    f"CTrees forest-to-nonforest change: {reference.label}",
                )
            )
    for first_name, second_name in (("FCBM1_2009", "FCBM2_2013"), ("FCBM2_2013", "FCBM3_2018")):
        first = organized.references.get(first_name)
        second = organized.references.get(second_name)
        if first is None or second is None or first.image is None or second.image is None:
            continue
        products.append(
            (
                f"Map_Change_ForestToNonForest_CTrees_{first_name}_to_{second_name}.png",
                first.image.eq(1).And(second.image.eq(0)).clip(prepared.area_of_interest),
                f"CTrees forest-to-nonforest change: {first.label} to {second.label}",
            )
        )

    paths = []
    for filename, image, title in products:
        paths.append(
            _thumbnail_map(
                image.toByte(),
                output_directory / filename,
                region,
                title,
                {"min": 0, "max": 1, "palette": ["f7f7f7", _hex_without_hash(LOSS_RED)]},
                dimensions,
                [("No forest-to-nonforest change", "#f7f7f7"), ("Forest to non-forest", LOSS_RED)],
                boundary=boundary,
            )
        )
    return paths


def _change_class_image(start_forest: ee.Image, end_forest: ee.Image) -> ee.Image:
    """Encode per-pixel change class: 1=stable forest, 2=stable non-forest, 3=loss, 4=gain."""
    return (
        ee.Image(2)
        .where(start_forest.eq(1).And(end_forest.eq(1)), 1)
        .where(start_forest.eq(1).And(end_forest.eq(0)), 3)
        .where(start_forest.eq(0).And(end_forest.eq(1)), 4)
        .updateMask(start_forest.mask().And(end_forest.mask()))
        .toByte()
        .rename("value")
    )


def _five_class_agreement_image(ctrees_change: ee.Image, mb_change: ee.Image) -> ee.Image:
    """Encode 2.B five-class agreement: 1-4 = matched class, 5 = disagree."""
    agree = ctrees_change.eq(mb_change)
    return (
        ee.Image(5)
        .where(agree.And(ctrees_change.eq(1)), 1)
        .where(agree.And(ctrees_change.eq(2)), 2)
        .where(agree.And(ctrees_change.eq(3)), 3)
        .where(agree.And(ctrees_change.eq(4)), 4)
        .updateMask(ctrees_change.mask().And(mb_change.mask()))
        .toByte()
        .rename("value")
    )


def _forest_loss_agreement_image(ctrees_change: ee.Image, mb_change: ee.Image) -> ee.Image:
    """Encode forest-loss agreement: 1=both loss, 2=CTrees only, 3=MapBiomas only."""
    ctrees_loss = ctrees_change.eq(3)
    mb_loss = mb_change.eq(3)
    return (
        ee.Image(0)
        .where(ctrees_loss.And(mb_loss), 1)
        .where(ctrees_loss.And(mb_loss.Not()), 2)
        .where(mb_loss.And(ctrees_loss.Not()), 3)
        .updateMask(ctrees_loss.Or(mb_loss))
        .toByte()
        .rename("value")
    )


def _mapbiomas_land_cover_visualization(image: ee.Image) -> tuple[ee.Image, dict[str, Any], list[tuple[str, str]]]:
    """Prepare a categorical MapBiomas land-cover visualization with one color per class code."""
    class_codes = sorted(MAPBIOMAS_LAND_COVER_CLASSES)
    visual_codes = list(range(1, len(class_codes) + 1))
    visual_image = image.remap(class_codes, visual_codes).rename("mapbiomas_land_cover_visual").selfMask()
    palette = [_hex_without_hash(MAPBIOMAS_CLASS_COLORS.get(code, "#bdbdbd")) for code in class_codes]
    legend = [
        (f"{code} {MAPBIOMAS_LAND_COVER_CLASSES[code]}", MAPBIOMAS_CLASS_COLORS.get(code, "#bdbdbd"))
        for code in class_codes
    ]
    return visual_image, {"min": 1, "max": len(class_codes), "palette": palette}, legend


def _ctrees_binary_image_for_year(organized: OrganizedData, year: int) -> ee.Image | None:
    """Return a 0/1 CTrees forest image for the given year, or None if unavailable."""
    year_to_name = {2009: "FCBM1_2009", 2013: "FCBM2_2013", 2018: "FCBM3_2018", 2024: "FCBM4"}
    for name in (year_to_name.get(year), f"FCBM_{year}"):
        if name is None:
            continue
        reference = organized.references.get(name)
        if reference is not None and reference.image is not None:
            return reference.image.eq(1).toByte().rename("value")
    return None


def _change_agreement_panels(
    prepared: PreparedInputs,
    organized: OrganizedData,
    output_directory: Path,
    region: ee.Geometry,
    dimensions: int,
    forest_codes: list[int],
    pixel_area_hectares: float,
    change_agreement_tables: dict[str, pd.DataFrame],
) -> list[Path]:
    """Generate Section 2.B three-panel change-agreement figures, one per interval."""
    legend = [
        ("Forest loss agreement", LOSS_RED),
        ("Forest loss CTrees only", CTREES_ONLY_CYAN),
        ("Forest loss MapBiomas only", MAPBIOMAS_ONLY_YELLOW),
    ]
    thumb_dim = min(560, dimensions // 3)
    paths: list[Path] = []

    _remove_legacy_figure(
        output_directory,
        "Figure_2B_ChangeAgreement_2009_2013.png",
        "Figure_2B_ChangeAgreement_2013_2018.png",
        "Figure_2B_ChangeAgreement_2018_2024.png",
        "Figure_2B_ForestLossAgreement_2018_2024.png",
    )

    for start_year, end_year in [(2009, 2013), (2013, 2018)]:
        ctrees_start = _ctrees_binary_image_for_year(organized, start_year)
        ctrees_end = _ctrees_binary_image_for_year(organized, end_year)
        if ctrees_start is None or ctrees_end is None:
            continue

        mb_start = (
            select_mapbiomas_year(prepared.mapbiomas_image, start_year)
            .remap(forest_codes, [1] * len(forest_codes), 0)
            .updateMask(organized.valid_analysis_mask)
            .clip(prepared.area_of_interest)
            .toByte()
        )
        mb_end = (
            select_mapbiomas_year(prepared.mapbiomas_image, end_year)
            .remap(forest_codes, [1] * len(forest_codes), 0)
            .updateMask(organized.valid_analysis_mask)
            .clip(prepared.area_of_interest)
            .toByte()
        )
        ctrees_change = _change_class_image(ctrees_start, ctrees_end)
        mb_change = _change_class_image(mb_start, mb_end)
        agreement = _forest_loss_agreement_image(ctrees_change, mb_change)

        table_key = next(
            (k for k in change_agreement_tables if str(start_year) in k and str(end_year) in k),
            None,
        )
        loss_agree_mha = math.nan
        ctrees_only_mha = math.nan
        mapbiomas_only_mha = math.nan
        if table_key:
            tbl = change_agreement_tables[table_key]
            loss_agree_px = _table_cell(tbl, "Forest loss", "Forest loss")
            ctrees_loss_total = _table_cell(tbl, "Forest loss", "row_total")
            mapbiomas_loss_total = _table_cell(tbl, "column_total", "Forest loss")
            ctrees_only_px = max(ctrees_loss_total - loss_agree_px, 0)
            mapbiomas_only_px = max(mapbiomas_loss_total - loss_agree_px, 0)
            loss_agree_mha = loss_agree_px * pixel_area_hectares / 1_000_000
            ctrees_only_mha = ctrees_only_px * pixel_area_hectares / 1_000_000
            mapbiomas_only_mha = mapbiomas_only_px * pixel_area_hectares / 1_000_000

        panels_spec = [
            (
                f"CTrees, {start_year}-{end_year}",
                ctrees_change.clip(prepared.area_of_interest),
                {"min": 1, "max": 4, "palette": PALETTE_CHANGE4},
            ),
            (
                f"MapBiomas, {start_year}-{end_year}",
                mb_change.clip(prepared.area_of_interest),
                {"min": 1, "max": 4, "palette": PALETTE_CHANGE4},
            ),
            (
                f"Forest loss agreement, {start_year}-{end_year}",
                agreement.clip(prepared.area_of_interest),
                {"min": 1, "max": 3, "palette": PALETTE_LOSS_AGREEMENT3},
            ),
        ]

        path = output_directory / f"Figure_2B_ForestLossAgreement_CTreesMapBiomas_{start_year}_{end_year}.png"
        raw_paths: list[Path] = []
        thumbs: list[tuple[str, Image.Image]] = []
        try:
            for idx, (panel_title, image, vis) in enumerate(panels_spec):
                raw_path = path.with_name(f"{path.stem}_{idx}.raw.png")
                url = _visualize_with_boundary(image, vis, prepared.area_of_interest).getThumbURL(
                    {"region": region, "dimensions": thumb_dim, "format": "png"}
                )
                _download_thumbnail(url, raw_path)
                raw_paths.append(raw_path)
                with Image.open(raw_path) as raw:
                    thumbs.append((panel_title, raw.convert("RGB").copy()))
        except Exception:
            for rp in raw_paths:
                rp.unlink(missing_ok=True)
            continue

        thumb_w = max(t.width for _, t in thumbs)
        thumb_h = max(t.height for _, t in thumbs)
        header_h, label_h, gap, legend_h, footer_h = 118, 42, 18, 78, 44
        out_w = 3 * thumb_w + 4 * gap
        out_h = header_h + label_h + thumb_h + gap + legend_h + footer_h
        canvas = Image.new("RGB", (out_w, out_h), PAPER)
        draw = ImageDraw.Draw(canvas)

        _draw_wrapped_text(
            draw,
            f"Section 2.B  Forest Change Agreement, {start_year}-{end_year}",
            gap, 22, out_w - 2 * gap, _font(32, bold=True), INK,
        )
        _draw_wrapped_text(
            draw,
            "CTrees vs. MapBiomas pixel-level agreement per VT0007 v1.0. "
            "Third panel: forest-loss agreement, CTrees-only loss, and MapBiomas-only loss.",
            gap, 68, out_w - 2 * gap, _font(20), MUTED,
        )
        draw.line((gap, 112, out_w - gap, 112), fill=GRID, width=2)

        for idx, (panel_title, thumb) in enumerate(thumbs):
            x = gap + idx * (thumb_w + gap)
            y = header_h + label_h
            draw.text((x, header_h + 6), panel_title, fill=INK, font=_font(22, bold=True))
            canvas.paste(thumb, (x, y))
            draw.rectangle((x, y, x + thumb.width, y + thumb.height), outline=INK, width=2)

        _draw_legend(draw, legend, gap, header_h + label_h + thumb_h + gap)

        if not any(math.isnan(value) for value in (loss_agree_mha, ctrees_only_mha, mapbiomas_only_mha)):
            draw.text(
                (gap, out_h - footer_h + 4),
                f"Loss agreement: {loss_agree_mha:.2f} Mha | CTrees only: {ctrees_only_mha:.2f} Mha | MapBiomas only: {mapbiomas_only_mha:.2f} Mha",
                fill=RED,
                font=_font(22, bold=True),
            )

        _save_figure(canvas, path)
        for rp in raw_paths:
            rp.unlink(missing_ok=True)
        paths.append(path)

    return paths


def _concordance_image(mapbiomas_persistence: ee.Image, reference: ee.Image) -> ee.Image:
    mb_forest = mapbiomas_persistence.eq(1)
    mb_nonforest = mapbiomas_persistence.eq(2)
    mb_change = mapbiomas_persistence.eq(3)
    ref_forest = reference.eq(2)
    ref_nonforest = reference.eq(1)
    ref_change = reference.eq(3).Or(reference.eq(4))
    return (
        ee.Image(0)
        .where(mb_forest.And(ref_forest), 1)
        .where(mb_nonforest.And(ref_nonforest), 2)
        .where(mb_change.And(ref_change).Not(), 3)
        .where(mb_forest.And(ref_forest).Not().And(mb_nonforest.And(ref_nonforest).Not()).And(mb_change.And(ref_change)), 3)
        .updateMask(mapbiomas_persistence.mask())
        .toByte()
    )


def _area_bar_figure(area_tables: dict[str, pd.DataFrame], output_directory: Path) -> Path:
    _remove_legacy_figure(output_directory, "Figure_02_AreaBar_30m.png")
    path = output_directory / "Figure_02_MapBiomasPersistenceAreaByScenario_30m.png"
    image, draw = _canvas(
        "MapBiomas Forest Persistence by Scenario (Area, Mha)",
        "Area by persistence class for each analytical scenario, reported in millions of hectares.",
    )
    rows: list[dict[str, Any]] = []
    for scenario, table in sorted(area_tables.items()):
        for row in table.itertuples(index=False):
            rows.append(
                {
                    "scenario": scenario,
                    "class": str(row.mapbiomas_class),
                    "area": float(row.area_million_hectares),
                }
            )
    if not rows:
        _center_text(draw, "No area tables available.", WIDTH / 2, HEIGHT / 2, 36)
        _save_figure(image, path)
        return path

    frame = pd.DataFrame(rows)
    pivot = frame.pivot_table(index="scenario", columns="class", values="area", aggfunc="sum").fillna(0)
    _draw_stacked_bar_chart(draw, pivot)
    _save_figure(image, path)
    return path


def _crosstab_heatmaps(crosstabs: dict[tuple[str, str], pd.DataFrame], output_directory: Path) -> list[Path]:
    paths = []
    preferred = [
        ("A_100pct_1985-2024", "DMJSS", "Figure_03_DMJSS_CrossRef_30m.png"),
        ("A_100pct_1985-2024", "FCBM1_2009", "Figure_03_FCBM1_2009_CrossRef_30m.png"),
        ("A_100pct_1985-2024", "FCBM2_2013", "Figure_03_FCBM2_2013_CrossRef_30m.png"),
        ("A_100pct_1985-2024", "FCBM3_2018", "Figure_03_FCBM3_2018_CrossRef_30m.png"),
        ("A_100pct_1985-2024", "FCBM4", "Figure_03_FCBM4_CrossRef_30m.png"),
    ]
    for scenario, reference, filename in preferred:
        table = crosstabs.get((scenario, reference))
        if table is None:
            continue
        path = output_directory / filename
        image, draw = _canvas(
            f"{reference} and MapBiomas Persistence Cross-Tabulation",
            f"Scenario {_clean_chart_label(scenario)}. Cell values are percentages of the study area.",
        )
        _draw_heatmap_table(draw, table)
        _save_figure(image, path)
        paths.append(path)
    return paths


def _canvas(title: str, subtitle: str = "MapBiomas x CTrees, Para, Brazil") -> tuple[Image.Image, ImageDraw.ImageDraw]:
    image = Image.new("RGB", (WIDTH, HEIGHT), PAPER)
    draw = ImageDraw.Draw(image)
    _draw_wrapped_text(draw, title, MARGIN, 42, WIDTH - 2 * MARGIN, _font(38, bold=True), INK, line_spacing=8)
    _draw_wrapped_text(draw, subtitle, MARGIN, 104, WIDTH - 2 * MARGIN, _font(22), MUTED, line_spacing=5)
    draw.line((MARGIN, 166, WIDTH - MARGIN, 166), fill=GRID, width=2)
    return image, draw


def _draw_horizontal_bar_chart(
    draw: ImageDraw.ImageDraw,
    labels: list[str],
    values: list[float],
    axis_label: str,
    max_value: float,
    color: str,
) -> None:
    left, top, right, bottom = 560, 180, WIDTH - 170, HEIGHT - 120
    chart_width = right - left
    rows = max(len(values), 1)
    row_gap = 8
    bar_height = max(14, min(34, (bottom - top - row_gap * (rows - 1)) / rows))
    tick_count = 5
    for tick_index in range(tick_count + 1):
        value = max_value * tick_index / tick_count
        x = left + chart_width * value / max_value
        draw.line((x, top - 8, x, bottom + 6), fill=GRID, width=1)
        draw.text((x - 18, bottom + 18), f"{value:.0f}", fill=MUTED, font=_font(18))
    draw.line((left, bottom + 6, right, bottom + 6), fill=INK, width=2)
    for index, (label, value) in enumerate(zip(labels, values)):
        y0 = top + index * (bar_height + row_gap)
        y1 = y0 + bar_height
        x1 = left + chart_width * min(value, max_value) / max_value
        _draw_wrapped_text(draw, label, MARGIN, y0 - 2, left - MARGIN - 28, _font(19), INK, max_lines=2)
        draw.rectangle((left, y0, x1, y1), fill=color)
        draw.text((x1 + 10, y0 - 2), f"{value:.1f}", fill=INK, font=_font(19, bold=True))
    draw.text((left, HEIGHT - 58), axis_label, fill=MUTED, font=_font(22))


def _draw_bar_chart(
    draw: ImageDraw.ImageDraw,
    labels: list[str],
    values: list[float],
    axis_label: str,
    max_value: float,
) -> None:
    left, top, right, bottom = MARGIN, 200, WIDTH - 70, HEIGHT - 230
    draw.line((left, bottom, right, bottom), fill=INK, width=2)
    draw.line((left, top, left, bottom), fill=INK, width=2)
    for tick in range(0, 101, 20):
        y = bottom - (bottom - top) * tick / max_value
        draw.line((left, y, right, y), fill=GRID, width=1)
        draw.text((35, y - 12), str(tick), fill=MUTED, font=_font(24))
    bar_gap = 8
    bar_width = max(8, (right - left - bar_gap * (len(values) + 1)) / max(len(values), 1))
    for index, (label, value) in enumerate(zip(labels, values)):
        x0 = left + bar_gap + index * (bar_width + bar_gap)
        x1 = x0 + bar_width
        y0 = bottom - (bottom - top) * min(value, max_value) / max_value
        color = BLUE if index % 2 == 0 else SKY
        draw.rectangle((x0, y0, x1, bottom), fill=color)
        draw.text((x0, y0 - 30), f"{value:.1f}", fill=INK, font=_font(20))
        _rotated_label(draw, label, int(x0 + bar_width / 2), bottom + 12)
    draw.text((MARGIN, HEIGHT - 70), axis_label, fill=MUTED, font=_font(24))


def _draw_stacked_bar_chart(draw: ImageDraw.ImageDraw, pivot: pd.DataFrame) -> None:
    left, top, right, bottom = 370, 185, WIDTH - 170, HEIGHT - 150
    ordered_columns = [column for column in PERSISTENCE_COLORS if column in pivot.columns]
    ordered_columns.extend(column for column in pivot.columns if column not in ordered_columns)
    pivot = pivot[ordered_columns]
    totals = pivot.sum(axis=1)
    max_total = max(float(totals.max()), 1)
    bar_height = min(74, (bottom - top) / max(len(pivot), 1) - 16)
    for index, (scenario, row) in enumerate(pivot.iterrows()):
        y0 = top + index * ((bottom - top) / max(len(pivot), 1)) + 8
        y1 = y0 + bar_height
        _draw_wrapped_text(draw, _clean_chart_label(str(scenario)), MARGIN, y0 + 7, left - MARGIN - 18, _font(22), INK, max_lines=2)
        x = left
        for column in pivot.columns:
            value = float(row[column])
            width = (right - left) * value / max_total
            color = PERSISTENCE_COLORS.get(str(column), PURPLE)
            draw.rectangle((x, y0, x + width, y1), fill=color)
            if width > 78:
                draw.text((x + 8, y0 + 17), f"{value:.1f}", fill="white" if color != "#c7b37f" else INK, font=_font(18, bold=True))
            x += width
        draw.text((right + 10, y0 + 14), f"{float(totals[scenario]):.1f} Mha", fill=MUTED, font=_font(20))
    legend_x = MARGIN
    legend_y = HEIGHT - 105
    for index, column in enumerate(pivot.columns):
        x = legend_x + (index % 3) * 430
        y = legend_y + (index // 3) * 36
        draw.rectangle((x, y, x + 24, y + 24), fill=PERSISTENCE_COLORS.get(str(column), PURPLE))
        draw.text((x + 34, y - 2), str(column), fill=INK, font=_font(22))


def _draw_percent_stacked_bar_chart(draw: ImageDraw.ImageDraw, pivot: pd.DataFrame, colors: dict[str, str]) -> None:
    left, top, right, bottom = 520, 190, WIDTH - 170, HEIGHT - 150
    bar_height = min(64, (bottom - top) / max(len(pivot), 1) - 14)
    for tick in range(0, 101, 20):
        x = left + (right - left) * tick / 100
        draw.line((x, top - 8, x, bottom + 8), fill=GRID, width=1)
        draw.text((x - 18, bottom + 18), str(tick), fill=MUTED, font=_font(18))
    for index, (label, row) in enumerate(pivot.iterrows()):
        y0 = top + index * ((bottom - top) / max(len(pivot), 1)) + 8
        y1 = y0 + bar_height
        _draw_wrapped_text(draw, str(label), MARGIN, y0 + 5, left - MARGIN - 25, _font(20), INK, max_lines=2)
        x = left
        for column in pivot.columns:
            value = float(row[column]) if pd.notna(row[column]) else 0
            width = (right - left) * value / 100
            color = colors.get(str(column), PURPLE)
            draw.rectangle((x, y0, x + width, y1), fill=color)
            if width > 70:
                draw.text((x + 8, y0 + 14), f"{value:.1f}", fill="white" if color != "#c7b37f" else INK, font=_font(18, bold=True))
            x += width
    legend_y = HEIGHT - 96
    cursor_x = MARGIN
    for label, color in colors.items():
        draw.rectangle((cursor_x, legend_y, cursor_x + 24, legend_y + 24), fill=color)
        draw.text((cursor_x + 34, legend_y - 2), label, fill=INK, font=_font(22))
        cursor_x += 360
    draw.text((left, HEIGHT - 58), "Share of CTrees class (%)", fill=MUTED, font=_font(22))


def _draw_grouped_horizontal_bars(
    draw: ImageDraw.ImageDraw,
    labels: list[str],
    values: list[float],
    colors: list[str],
    axis_label: str,
    max_value: float,
) -> None:
    left, top, right, bottom = 560, 185, WIDTH - 170, HEIGHT - 130
    chart_width = right - left
    row_gap = 8
    bar_height = max(16, min(30, (bottom - top - row_gap * max(len(values) - 1, 0)) / max(len(values), 1)))
    for tick_index in range(6):
        value = max_value * tick_index / 5
        x = left + chart_width * value / max_value
        draw.line((x, top - 8, x, bottom + 6), fill=GRID, width=1)
        draw.text((x - 18, bottom + 18), f"{value:.1f}", fill=MUTED, font=_font(18))
    for index, (label, value, color) in enumerate(zip(labels, values, colors)):
        y0 = top + index * (bar_height + row_gap)
        y1 = y0 + bar_height
        x1 = left + chart_width * min(value, max_value) / max_value
        _draw_wrapped_text(draw, label, MARGIN, y0 - 2, left - MARGIN - 28, _font(18), INK, max_lines=2)
        draw.rectangle((left, y0, x1, y1), fill=color)
        draw.text((x1 + 10, y0 - 2), f"{value:.3f}", fill=INK, font=_font(18, bold=True))
    draw.text((left, HEIGHT - 58), axis_label, fill=MUTED, font=_font(22))


def _draw_heatmap_table(draw: ImageDraw.ImageDraw, table: pd.DataFrame) -> None:
    data = table.copy()
    if "column_total" in data.index:
        data = data.drop(index="column_total")
    if "row_total" in data.columns:
        data = data.drop(columns=["row_total"])
    rows = list(data.index)
    columns = list(data.columns)
    left, top = 360, 235
    cell_w = min(220, (WIDTH - left - 110) / max(len(columns), 1))
    cell_h = min(112, (HEIGHT - top - 160) / max(len(rows), 1))
    for col_index, column in enumerate(columns):
        x = left + col_index * cell_w
        _draw_wrapped_text(draw, str(column), x + 8, top - 74, cell_w - 10, _font(18), INK, max_lines=3)
    for row_index, row_name in enumerate(rows):
        y = top + row_index * cell_h
        _draw_wrapped_text(draw, str(row_name), MARGIN, y + cell_h / 2 - 25, left - MARGIN - 28, _font(20), INK, max_lines=2)
        for col_index, column in enumerate(columns):
            x = left + col_index * cell_w
            value = float(data.loc[row_name, column])
            fill = _heat_color(value)
            draw.rectangle((x, y, x + cell_w, y + cell_h), fill=fill, outline="white", width=3)
            text = f"{value:.1f}%"
            bbox = draw.textbbox((0, 0), text, font=_font(22, bold=True))
            draw.text((x + (cell_w - (bbox[2] - bbox[0])) / 2, y + cell_h / 2 - 13), text, fill=INK, font=_font(22, bold=True))


def _thumbnail_map(
    image: ee.Image,
    path: Path,
    region: ee.Geometry,
    title: str,
    vis_params: dict[str, Any],
    dimensions: int,
    legend: list[tuple[str, str]],
    boundary: ee.FeatureCollection | None = None,
) -> Path:
    raw_path = path.with_suffix(".raw.png")
    url = _visualize_with_boundary(image, vis_params, boundary).getThumbURL(
        {"region": region, "dimensions": dimensions, "format": "png"}
    )
    _download_thumbnail(url, raw_path)
    with Image.open(raw_path) as raw:
        raw = raw.convert("RGB")
        output_width = raw.width + 80
        legend_y = raw.height + 142
        legend_height = _legend_height(legend, 40, output_width - 80)
        output = Image.new("RGB", (output_width, legend_y + legend_height + 36), PAPER)
        draw = ImageDraw.Draw(output)
        draw.text((40, 26), title, fill=INK, font=_font(34, bold=True))
        draw.text((40, 68), "Earth Engine thumbnail; visualization clipped to Para, Brazil", fill=MUTED, font=_font(21))
        output.paste(raw, (40, 118))
        draw.rectangle((40, 118, 40 + raw.width, 118 + raw.height), outline=INK, width=2)
        _draw_north_arrow(draw, 40 + raw.width - 82, 142)
        _draw_scale_bar(draw, 70, 118 + raw.height - 55)
        _draw_legend(draw, legend, 40, legend_y, output_width - 80)
        _save_figure(output, path)
    raw_path.unlink(missing_ok=True)
    return path


def _panel_map(
    panels: list[tuple[str, ee.Image, dict[str, Any]]],
    path: Path,
    title: str,
    region: ee.Geometry,
    dimensions: int,
    legend: list[tuple[str, str]],
    boundary: ee.FeatureCollection | None = None,
) -> Path:
    if not panels:
        return path
    raw_paths = []
    thumbs = []
    thumb_dimensions = min(900, dimensions)
    for index, (panel_title, image, vis_params) in enumerate(panels):
        raw_path = path.with_name(f"{path.stem}_{index}.raw.png")
        url = _visualize_with_boundary(image, vis_params, boundary).getThumbURL(
            {"region": region, "dimensions": thumb_dimensions, "format": "png"}
        )
        _download_thumbnail(url, raw_path)
        raw_paths.append(raw_path)
        with Image.open(raw_path) as raw:
            thumbs.append((panel_title, raw.convert("RGB").copy()))

    columns = 2 if len(thumbs) > 1 else 1
    thumb_w = max(thumb.width for _, thumb in thumbs)
    thumb_h = max(thumb.height for _, thumb in thumbs)
    rows = (len(thumbs) + columns - 1) // columns
    header_h = 110
    label_h = 44
    legend_h = 88
    output = Image.new(
        "RGB",
        (columns * thumb_w + (columns + 1) * 30, header_h + rows * (thumb_h + label_h + 24) + legend_h),
        PAPER,
    )
    draw = ImageDraw.Draw(output)
    draw.text((30, 24), title, fill=INK, font=_font(34, bold=True))
    for index, (panel_title, thumb) in enumerate(thumbs):
        row = index // columns
        col = index % columns
        x = 30 + col * (thumb_w + 30)
        y = header_h + row * (thumb_h + label_h + 24)
        draw.text((x, y), panel_title, fill=INK, font=_font(26, bold=True))
        output.paste(thumb, (x, y + label_h))
        draw.rectangle((x, y + label_h, x + thumb.width, y + label_h + thumb.height), outline=INK, width=2)
    _draw_legend(draw, legend, 30, output.height - 62)
    _save_figure(output, path)
    for raw_path in raw_paths:
        raw_path.unlink(missing_ok=True)
    return path


def _visualize_with_boundary(
    image: ee.Image,
    vis_params: dict[str, Any],
    boundary: ee.FeatureCollection | None,
    width: int = 3,
) -> ee.Image:
    visual = image.visualize(**vis_params)
    if boundary is None:
        return visual
    outline = ee.Image().byte().paint(boundary, 1, width).selfMask().visualize(palette=["ffffff"])
    return visual.blend(outline)


def _download_thumbnail(url: str, path: Path, attempts: int = 3) -> None:
    temporary = path.with_suffix(path.suffix + ".download")
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            path.unlink(missing_ok=True)
            temporary.unlink(missing_ok=True)
            with urllib.request.urlopen(url, timeout=120) as response:
                with temporary.open("wb") as output:
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        output.write(chunk)
            temporary.replace(path)
            return
        except Exception as exc:
            last_error = exc
            temporary.unlink(missing_ok=True)
            path.unlink(missing_ok=True)
            time.sleep(min(attempt * 2, 10))
    raise RuntimeError(f"Could not download Earth Engine thumbnail {path.name} after {attempts} attempts.") from last_error


def _draw_legend(
    draw: ImageDraw.ImageDraw,
    items: list[tuple[str, str]],
    x: int,
    y: int,
    max_width: int | None = None,
) -> None:
    if max_width is None:
        max_width = WIDTH - x - 250
    if _is_mapbiomas_land_cover_legend(items):
        _draw_mapbiomas_land_cover_legend(draw, x, y, max_width)
        return
    cursor_x = x
    cursor_y = y
    for label, color in items:
        draw.rectangle((cursor_x, cursor_y, cursor_x + 28, cursor_y + 28), fill=color, outline=GRID)
        draw.text((cursor_x + 38, cursor_y - 2), label, fill=INK, font=_font(22))
        cursor_x += 38 + max(140, len(label) * 13)
        if cursor_x > x + max_width:
            cursor_x = x
            cursor_y += 38


def _legend_height(items: list[tuple[str, str]], x: int, max_width: int) -> int:
    if not items:
        return 0
    if _is_mapbiomas_land_cover_legend(items):
        return _mapbiomas_land_cover_legend_height()
    cursor_x = x
    rows = 1
    for label, _ in items:
        cursor_x += 38 + max(140, len(label) * 13)
        if cursor_x > x + max_width:
            cursor_x = x
            rows += 1
    return rows * 38


def _is_mapbiomas_land_cover_legend(items: list[tuple[str, str]]) -> bool:
    labels = {label for label, _ in items}
    expected = {f"{code} {MAPBIOMAS_LAND_COVER_CLASSES[code]}" for code in MAPBIOMAS_LAND_COVER_CLASSES}
    return labels == expected


def _draw_mapbiomas_land_cover_legend(draw: ImageDraw.ImageDraw, x: int, y: int, max_width: int) -> None:
    column_gap = 26
    column_width = max(360, int((max_width - column_gap * 2) / 3))
    item_height = 34
    columns = [
        MAPBIOMAS_LEGEND_GROUPS[:2],
        MAPBIOMAS_LEGEND_GROUPS[2:3],
        MAPBIOMAS_LEGEND_GROUPS[3:],
    ]
    heading_font = _font(18, bold=True)
    label_font = _font(16)
    for column_index, groups in enumerate(columns):
        cursor_x = x + column_index * (column_width + column_gap)
        cursor_y = y
        for group_name, codes in groups:
            draw.text((cursor_x, cursor_y), group_name, fill=INK, font=heading_font)
            cursor_y += 26
            for code in codes:
                color = MAPBIOMAS_CLASS_COLORS[code]
                label = f"{code} {MAPBIOMAS_LAND_COVER_CLASSES[code]}"
                draw.rectangle((cursor_x, cursor_y + 2, cursor_x + 18, cursor_y + 20), fill=color, outline=GRID)
                _draw_wrapped_text(
                    draw,
                    label,
                    cursor_x + 28,
                    cursor_y,
                    column_width - 30,
                    label_font,
                    INK,
                    line_spacing=1,
                    max_lines=2,
                )
                cursor_y += item_height
            cursor_y += 10


def _mapbiomas_land_cover_legend_height() -> int:
    item_height = 34

    def column_height(groups: list[tuple[str, list[int]]]) -> int:
        return sum(26 + len(codes) * item_height + 10 for _, codes in groups)

    columns = [
        MAPBIOMAS_LEGEND_GROUPS[:2],
        MAPBIOMAS_LEGEND_GROUPS[2:3],
        MAPBIOMAS_LEGEND_GROUPS[3:],
    ]
    return max(column_height(groups) for groups in columns)


def _hex_without_hash(color: str) -> str:
    return color[1:] if color.startswith("#") else color


def _draw_north_arrow(draw: ImageDraw.ImageDraw, x: int, y: int) -> None:
    draw.polygon([(x, y), (x - 16, y + 54), (x, y + 42), (x + 16, y + 54)], fill=INK)
    draw.text((x - 10, y + 58), "N", fill=INK, font=_font(22, bold=True))


def _draw_scale_bar(draw: ImageDraw.ImageDraw, x: int, y: int) -> None:
    width = 220
    segment = width // 2
    draw.rectangle((x - 12, y - 34, x + width + 60, y + 34), fill="white", outline=GRID)
    draw.rectangle((x, y, x + segment, y + 14), fill=INK)
    draw.rectangle((x + segment, y, x + width, y + 14), fill="white", outline=INK)
    draw.text((x - 2, y + 18), "0", fill=INK, font=_font(18))
    draw.text((x + segment - 20, y + 18), "100", fill=INK, font=_font(18))
    draw.text((x + width - 24, y + 18), "200 km", fill=INK, font=_font(18))


def _heat_color(value: float) -> str:
    value = max(0, min(100, value)) / 100
    red = int(245 - value * 120)
    green = int(247 - value * 50)
    blue = int(255 - value * 170)
    return f"#{red:02x}{green:02x}{blue:02x}"


def _rotated_label(draw: ImageDraw.ImageDraw, text: str, x: int, y: int) -> None:
    image = Image.new("RGBA", (180, 80), (255, 255, 255, 0))
    label_draw = ImageDraw.Draw(image)
    label_draw.text((0, 0), text, fill=INK, font=_font(18))
    rotated = image.rotate(65, expand=True)
    draw.bitmap((x - 20, y), rotated, fill=INK)


def _draw_wrapped_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    x: float,
    y: float,
    width: float,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    fill: str,
    line_spacing: int = 4,
    max_lines: int | None = None,
) -> float:
    words = str(text).split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        bbox = draw.textbbox((0, 0), candidate, font=font)
        if bbox[2] - bbox[0] <= width or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    if max_lines is not None and len(lines) > max_lines:
        lines = lines[:max_lines]
        while lines[-1] and draw.textbbox((0, 0), lines[-1] + "...", font=font)[2] - draw.textbbox((0, 0), lines[-1] + "...", font=font)[0] > width:
            lines[-1] = lines[-1][:-1].rstrip()
        lines[-1] = lines[-1] + "..."
    line_height = draw.textbbox((0, 0), "Ag", font=font)[3] - draw.textbbox((0, 0), "Ag", font=font)[1]
    for line in lines:
        draw.text((x, y), line, fill=fill, font=font)
        y += line_height + line_spacing
    return y


def _clean_chart_label(value: str) -> str:
    text = value.replace("_", " ").replace("-", " to ")
    text = text.replace("pct", " percent")
    replacements = {
        "A 100 percent 1985 to 2024": "Scenario A, 100 percent, 1985 to 2024",
        "B 95 percent 1985 to 2024": "Scenario B, 95 percent, 1985 to 2024",
        "C 50 percent 1985 to 2024": "Scenario C, 50 percent, 1985 to 2024",
        "D 100 percent 2015 to 2024": "Scenario D, 100 percent, 2015 to 2024",
        "E 100 percent 2013 to 2024": "Scenario E, 100 percent, 2013 to 2024",
        "F 100 percent 2018 to 2024": "Scenario F, 100 percent, 2018 to 2024",
    }
    text = " ".join(text.split())
    return replacements.get(text, text)


def _center_text(draw: ImageDraw.ImageDraw, text: str, x: float, y: float, size: int) -> None:
    bbox = draw.textbbox((0, 0), text, font=_font(size))
    draw.text((x - (bbox[2] - bbox[0]) / 2, y), text, fill=MUTED, font=_font(size))


def _safe_filename(value: str) -> str:
    return "".join(character if character.isalnum() or character in {"_", "-"} else "_" for character in value)[:120]


def _table_cell(table: pd.DataFrame, row: str, column: str) -> float:
    if row not in table.index or column not in table.columns:
        return 0.0
    value = table.loc[row, column]
    return float(value) if pd.notna(value) else 0.0


def _save_figure(image: Image.Image, path: Path) -> None:
    image.save(path, dpi=(300, 300))


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        "C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf",
        "C:/Windows/Fonts/segoeuib.ttf" if bold else "C:/Windows/Fonts/segoeui.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()
