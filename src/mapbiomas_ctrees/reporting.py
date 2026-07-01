"""Final table writing and technical report generation."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import TypedDict

import pandas as pd

from .models import AnalysisResults
from .settings import ProjectSettings


class WorkflowOutputs(TypedDict, total=False):
    technical_report: Path
    excel_tables: Path
    powerpoint_presentation: Path
    word_report: Path
    mapbiomas_verra_validation: Path
    agreement_metrics: Path
    fcbm_comparison_metrics: Path
    all_class_binary_accuracy_metrics: Path
    forest_change_area_timeseries: Path
    change_area_by_interval: Path
    temporal_consistency_reversals: Path
    reclassification_schema: Path


def write_results(results: AnalysisResults, settings: ProjectSettings) -> WorkflowOutputs:
    """Write all final analytical outputs."""
    table_directory = settings.output_directories["tables"]
    report_directory = settings.output_directories["reports"]
    table_directory.mkdir(parents=True, exist_ok=True)
    report_directory.mkdir(parents=True, exist_ok=True)

    written: dict[str, Path] = {}
    written.update(_write_area_tables(results, table_directory))
    written.update(_write_crosstabs(results, table_directory))
    written.update(_write_fcbm_comparisons(results, table_directory))
    pixel_area_hectares = (float(settings.raw["earth_engine"]["scale_native_m"]) ** 2) / 10000
    written.update(_write_prompt_tables(results, table_directory, pixel_area_hectares))

    metrics_path = table_directory / "agreement_metrics.csv"
    fcbm_metrics_path = table_directory / "fcbm_comparison_metrics.csv"
    all_class_metrics_path = table_directory / "all_class_binary_accuracy_metrics.csv"
    change_area_timeseries_path = table_directory / "forest_change_area_timeseries.csv"
    change_area_by_interval_path = table_directory / "change_area_by_interval.csv"
    temporal_consistency_path = table_directory / "temporal_consistency_reversals.csv"
    schema_path = table_directory / "mapbiomas_reclassification_schema.csv"
    report_path = report_directory / "technical_report.md"

    results.agreement_metrics.to_csv(metrics_path, index=False)
    results.fcbm_comparison_metrics.to_csv(fcbm_metrics_path, index=False)
    results.all_class_metrics.to_csv(all_class_metrics_path, index=False)
    results.change_area_timeseries.to_csv(change_area_timeseries_path, index=False)
    results.change_area_timeseries.to_csv(change_area_by_interval_path, index=False)
    results.temporal_consistency.to_csv(temporal_consistency_path, index=False)
    results.reclassification_table.to_csv(schema_path, index=False)
    report_path.write_text(build_technical_report(results, settings), encoding="utf-8")

    written["agreement_metrics"] = metrics_path
    written["fcbm_comparison_metrics"] = fcbm_metrics_path
    written["all_class_binary_accuracy_metrics"] = all_class_metrics_path
    written["forest_change_area_timeseries"] = change_area_timeseries_path
    written["change_area_by_interval"] = change_area_by_interval_path
    written["temporal_consistency_reversals"] = temporal_consistency_path
    written["reclassification_schema"] = schema_path
    written["technical_report"] = report_path
    return written


def build_technical_report(results: AnalysisResults, settings: ProjectSettings) -> str:
    """Build the sequential Markdown technical report."""
    metrics = results.agreement_metrics.sort_values(
        "overall_agreement_percent", ascending=False
    ).head(10)
    all_class_metrics = results.all_class_metrics.head(12)
    fcbm_metrics = results.fcbm_comparison_metrics.head(12)

    lines = [
        "# MapBiomas vs. CTrees Cross-Validation Technical Report: Para BVP2",
        "",
        f"TerraCarbon / Verra VMD0055 v1.1 | VT0007 v1.0 | {date.today().isoformat()}",
        "",
        "## 1. Introduction",
        "",
        f"The study area is {settings.raw['earth_engine']['aoi']['state_name']}, Brazil. "
        "The workflow loads the jurisdiction boundary, the IBGE municipal layer, MapBiomas Collection 10.1 land-cover classes, CTrees FCBM products, and pixel area in hectares.",
        "",
        "### 1.1 Study Area",
        "",
        f"The jurisdiction is {settings.raw['earth_engine']['aoi']['state_name']}, Brazil. All area statistics are reported in hectares or millions of hectares.",
        "",
        "### 1.2 Data Sources",
        "",
        "Primary years are 1985, 2009, 2013, 2018, and 2024 for MapBiomas, with shared CTrees comparison years 2009, 2013, and 2018.",
        "",
        "### 1.3 Methodological Framework",
        "",
        "FCBM products are interpreted according to VT0007 v1.0 and VMD0055 v1.1. The four-class interpretation from VMD0055 v1.1, Table 15 is used for operative CTrees change analysis. The three-class interpretation from VMD0055 v1.1, Table 16 is used only for accuracy metrics.",
        "",
        "### 1.4 FCBM Class Interpretation Schemes",
        "",
        "The eight raw FCBM transition values are retained for presentation and for deriving UDef-A inputs. Derived binary risk-map inputs use VT0007 v1.0, Table 1 and Section Data Requirements.",
        "",
        "## 2. Data Presentation",
        "",
        "MapBiomas primary years and binary forest/non-forest products are prepared before comparison. "
        "CTrees FCBM transition values are retained for data presentation and interpreted with VMD0055 v1.1, Table 15 for operative cross-tabulations.",
        "",
        "### 2.1 CTrees FCBM Raw Transition Values",
        "",
        "Raw FCBM values are pre-interpretation transition indices and are not used directly for operative agreement metrics.",
        "",
        "### 2.2 CTrees FCBM Four-Class Interpretation",
        "",
        "The CTrees operative classification follows VMD0055 v1.1, Table 15: stable non-forest, stable forest, deforested in the first half of the HRP, and deforested in the second half of the HRP.",
        "",
        "### 2.3 MapBiomas Annual Land Cover",
        "",
        "MapBiomas Collection 10.1 annual land-cover classes are exported for the primary years and retained as all-class reference columns in Section 4.1.",
        "",
        "### 2.4 MapBiomas Binary Forest / Non-Forest",
        "",
        "MapBiomas forest classes are converted to binary forest/non-forest for comparisons with CTrees snapshots and UDef-A binary products.",
        "",
        "## 3. FCBM-Derived Mandatory Inputs for UDef-A",
        "",
        "The workflow derives CTrees and MapBiomas FCBM binary forest/non-forest, distance-from-non-forest, and deforestation rasters for T1, T2, T3, the HRP, the calibration period, and the confirmation period per VT0007 v1.0, Section Data Requirements.",
        "",
        "### 3.1 CTrees-Derived FCBM Products",
        "",
        "CTrees raw FCBM classes 1 through 8 are derived from the configured T1, T2, and T3 CTrees snapshots when no raw eight-index asset is provided.",
        "",
        "### 3.2 MapBiomas-Derived FCBM Products",
        "",
        "The MB-FCBM applies the same eight-index truth table to MapBiomas binary forest/non-forest at T1, T2, and T3.",
        "",
        "## 4. Cross-Tabulation Analysis",
        "",
        "All-class and FCBM-derived agreement metrics are reported in hectares. FCBM accuracy metrics use the VMD0055 v1.1, Table 16 three-class interpretation, while timing-sensitive deforestation analyses retain the VT0007 Table 15 four-class interpretation.",
        "",
        "### 4.1 CTrees vs. MapBiomas: All-Class Agreement",
        "",
    ]
    lines.extend(_markdown_table(all_class_metrics) if not all_class_metrics.empty else ["No all-class accuracy metrics were available."])
    lines.extend([
        "",
        "### 4.2 CTrees FCBM vs. MB-FCBM: Eight-Index Agreement Matrix",
        "",
    ])
    lines.extend(_markdown_table(fcbm_metrics) if not fcbm_metrics.empty else ["No FCBM comparison metrics were available."])
    lines.extend([
        "",
        "### 4.3 Derived Binary Products: Agreement by Temporal Reference Point",
        "",
        "Binary UDef-A products are cross-tabulated by product and temporal reference point per VT0007 v1.0, Table 1.",
        "",
        "### 4.4 Forest Change Agreement Maps",
        "",
        "Change agreement maps distinguish stable forest agreement, stable non-forest agreement, forest loss agreement, forest gain agreement, and disagreement.",
        "",
        "### 4.5 Forest Change Area Time Series",
        "",
    ])
    lines.extend(_markdown_table(results.change_area_timeseries) if not results.change_area_timeseries.empty else ["No forest-change time series table was available."])
    lines.extend([
        "",
        "### 4.6 Agreement Decomposed by CTrees Class",
        "",
        "Class decomposition tables report the MapBiomas forest and non-forest share within each CTrees class to diagnose systematic disagreement.",
        "",
        "### 4.7 Spatial Distribution of Disagreement",
        "",
        "Municipality-level disagreement summaries use the configured IBGE municipal asset and report disagreement density as percent of evaluated pixels.",
        "",
        "### 4.8 Temporal Consistency within Each Dataset",
        "",
    ])
    lines.extend(_markdown_table(results.temporal_consistency) if not results.temporal_consistency.empty else ["No temporal consistency summary was available."])
    lines.extend([
        "",
        "## 5. Summary Statistics",
        "",
        f"Final tables were written to `{settings.output_directories['tables'].as_posix()}`. "
        f"The report was written to `{settings.output_directories['reports'].as_posix()}`.",
        "",
        "### Highest overall agreement values",
        "",
    ])
    lines.extend(_markdown_table(metrics) if not metrics.empty else ["No agreement metrics were available."])
    lines.extend([
        "",
        "### Output inventory",
        "",
        "- `agreement_metrics.csv`",
        "- `fcbm_comparison_metrics.csv`",
        "- `area_by_class_*.csv`",
        "- `crosstab_*_pixels.csv`",
        "- `crosstab_*_percent.csv`",
        "- `fcbm_comparison_*_pixels.csv`",
        "- `all_class_*` tables",
        "- `change_agreement_*` tables",
        "- `class_decomposition_*` tables",
        "- `spatial_disagreement_*` tables",
        "- `mapbiomas_reclassification_schema.csv`",
        "",
        "## 6. References",
        "",
        "- MapBiomas Project. Collection 10.1 annual land-cover and land-use maps of Brazil.",
        "- CTrees. Forest Change Based Maps and related forest reference products.",
        "- Verra. VMD0055 v1.1, Estimation of emission reductions from avoided unplanned deforestation.",
        "- Verra. VT0007 v1.0, Unplanned deforestation allocation tool.",
    ])
    return "\n".join(lines) + "\n"


def _write_area_tables(results: AnalysisResults, table_directory: Path) -> dict[str, Path]:
    paths = {}
    for scenario, table in results.area_tables.items():
        path = table_directory / f"area_by_class_{scenario}.csv"
        table.to_csv(path, index=False)
        paths[f"area_{scenario}"] = path
    return paths


def _write_crosstabs(results: AnalysisResults, table_directory: Path) -> dict[str, Path]:
    paths = {}
    for (scenario, reference), table in results.crosstab_tables.items():
        base = f"crosstab_{scenario}_x_{reference}"
        pixel_path = table_directory / f"{base}_pixels.csv"
        percent_path = table_directory / f"{base}_percent.csv"
        table.to_csv(pixel_path)
        results.crosstab_percent_tables[(scenario, reference)].to_csv(percent_path)
        paths[f"{base}_pixels"] = pixel_path
        paths[f"{base}_percent"] = percent_path
    return paths


def _write_fcbm_comparisons(results: AnalysisResults, table_directory: Path) -> dict[str, Path]:
    paths = {}
    for name, table in results.fcbm_comparison_tables.items():
        path = table_directory / f"fcbm_comparison_{name}_pixels.csv"
        table.to_csv(path)
        paths[f"fcbm_comparison_{name}_pixels"] = path
    return paths


def _write_prompt_tables(results: AnalysisResults, table_directory: Path, pixel_area_hectares: float) -> dict[str, Path]:
    paths = {}
    grouped_outputs = [
        ("all_class", results.all_class_tables),
        ("change_agreement", results.change_agreement_tables),
        ("class_decomposition", results.class_decomposition_tables),
        ("spatial_disagreement", results.spatial_disagreement_tables),
    ]
    for prefix, tables in grouped_outputs:
        for name, table in tables.items():
            path = table_directory / f"{prefix}_{name}.csv"
            table.to_csv(path, index=True)
            paths[f"{prefix}_{name}"] = path
            if prefix == "all_class":
                year = _year_from_name(name)
                if year:
                    pixel_path = table_directory / f"crosstab_allclass_{year}_pixels.csv"
                    hectare_path = table_directory / f"crosstab_allclass_{year}_ha.csv"
                    table.to_csv(pixel_path)
                    _pixel_table_to_hectares(table, pixel_area_hectares).to_csv(hectare_path)
                    paths[f"crosstab_allclass_{year}_pixels"] = pixel_path
                    paths[f"crosstab_allclass_{year}_ha"] = hectare_path
            if prefix == "class_decomposition":
                year = _year_from_name(name)
                if year:
                    decomposition = table.copy()
                    for source, target in (
                        ("mapbiomas_forest_pixels", "mapbiomas_forest_hectares"),
                        ("mapbiomas_nonforest_pixels", "mapbiomas_nonforest_hectares"),
                        ("evaluated_pixels", "row_total_hectares"),
                    ):
                        if source in decomposition.columns:
                            decomposition[target] = decomposition[source] * pixel_area_hectares
                    path_alias = table_directory / f"agreement_by_ctrees_class_{year}.csv"
                    decomposition.to_csv(path_alias, index=False)
                    paths[f"agreement_by_ctrees_class_{year}"] = path_alias
    return paths


def _pixel_table_to_hectares(table: pd.DataFrame, pixel_area_hectares: float) -> pd.DataFrame:
    converted = table.copy()
    for column in converted.columns:
        converted[column] = pd.to_numeric(converted[column], errors="ignore")
        if pd.api.types.is_numeric_dtype(converted[column]):
            converted[column] = converted[column] * pixel_area_hectares
    return converted


def _year_from_name(name: str) -> str | None:
    import re

    match = re.search(r"(20\d{2}|19\d{2})", name)
    return match.group(1) if match else None


def _markdown_table(frame: pd.DataFrame) -> list[str]:
    preferred_columns = [
        "scenario",
        "reference",
        "year",
        "dataset",
        "class",
        "interval",
        "evaluated_area_million_hectares",
        "overall_agreement_percent",
        "cohen_kappa",
        "user_accuracy_percent",
        "producer_accuracy_percent",
        "forest_agreement_percent",
        "nonforest_agreement_percent",
        "change_agreement_percent",
        "ctrees_loss_million_hectares",
        "mapbiomas_loss_million_hectares",
        "reversal_million_hectares",
    ]
    columns = [column for column in preferred_columns if column in frame.columns]
    if not columns:
        columns = list(frame.columns[:7])
    formatted = frame[columns].copy()
    for column in formatted.columns:
        if pd.api.types.is_numeric_dtype(formatted[column]):
            formatted[column] = formatted[column].map(lambda value: "" if pd.isna(value) else f"{value:.3f}")
    return [
        "| " + " | ".join(formatted.columns) + " |",
        "| " + " | ".join(["---"] * len(formatted.columns)) + " |",
        *["| " + " | ".join(str(value) for value in row) + " |" for row in formatted.itertuples(index=False, name=None)],
    ]
