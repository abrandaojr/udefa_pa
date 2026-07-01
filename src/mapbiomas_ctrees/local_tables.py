"""Local CSV parsing and metric computation."""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any

import pandas as pd

from .constants import (
    CROSS_TABULATION_YEARS,
    CTREES_YEARS,
    FCBM_ACCURACY_CLASS_LABELS,
    FCBM_ACCURACY_REMAP,
    FCBM_TRANSITION_LABELS,
    FCBM_VT0007_CLASS_LABELS,
    MAPBIOMAS_LAND_COVER_CLASSES,
    MAPBIOMAS_PERSISTENCE_LABELS,
)
from .models import AnalysisResults, ReferenceRaster


def analyze_exported_tables(
    table_directory: Path,
    scenario_labels: list[str],
    references: dict[str, ReferenceRaster],
    pixel_area_hectares: float,
    settings: dict[str, Any],
) -> AnalysisResults:
    """Load local Earth Engine CSV exports and compute final tables."""
    area_tables = {
        scenario: parse_area_export(_first_match(table_directory, f"Area_30m_{scenario}*.csv"))
        for scenario in scenario_labels
    }

    crosstab_tables: dict[tuple[str, str], pd.DataFrame] = {}
    crosstab_percent_tables: dict[tuple[str, str], pd.DataFrame] = {}
    for scenario in scenario_labels:
        for reference_name, reference in references.items():
            path = _first_match(table_directory, f"XTab_30m_{scenario}_x_{reference_name}*.csv")
            absolute, percent = parse_crosstab_export(path, reference)
            crosstab_tables[(scenario, reference_name)] = absolute
            crosstab_percent_tables[(scenario, reference_name)] = percent
    fcbm_comparison_tables = parse_fcbm_comparison_exports(table_directory)
    all_class_tables = parse_all_class_exports(table_directory)
    change_agreement_tables = parse_change_agreement_exports(table_directory)

    return AnalysisResults(
        area_tables=area_tables,
        crosstab_tables=crosstab_tables,
        crosstab_percent_tables=crosstab_percent_tables,
        agreement_metrics=compute_agreement_metrics(crosstab_tables, references, pixel_area_hectares),
        reclassification_table=build_reclassification_table(settings),
        fcbm_comparison_tables=fcbm_comparison_tables,
        fcbm_comparison_metrics=compute_fcbm_comparison_metrics(fcbm_comparison_tables),
        all_class_tables=all_class_tables,
        all_class_metrics=compute_all_class_metrics(table_directory),
        change_agreement_tables=change_agreement_tables,
        change_area_timeseries=parse_change_area_timeseries(table_directory),
        class_decomposition_tables=compute_class_decomposition(all_class_tables),
        spatial_disagreement_tables=parse_spatial_disagreement_exports(table_directory),
        temporal_consistency=parse_temporal_consistency(table_directory),
    )


def parse_area_export(path: Path) -> pd.DataFrame:
    """Parse one grouped area export."""
    raw = str(pd.read_csv(path)["groups"].iloc[0])
    groups = _parse_groups(raw)
    total_area = sum(group.get("sum", 0.0) for group in groups)
    rows = [
        {
            "class_code": int(group["class"]),
            "mapbiomas_class": MAPBIOMAS_PERSISTENCE_LABELS.get(int(group["class"]), "Unknown"),
            "area_hectares": float(group.get("sum", 0.0)),
            "area_million_hectares": float(group.get("sum", 0.0)) / 1_000_000,
            "share_percent": _percent(float(group.get("sum", 0.0)), total_area),
        }
        for group in groups
    ]
    return pd.DataFrame(rows).sort_values("class_code")


def parse_crosstab_export(path: Path, reference: ReferenceRaster) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Parse one encoded histogram export into pixel and percent cross-tabs."""
    raw = pd.read_csv(path)
    histogram_columns = [column for column in raw.columns if column not in {"system:index", ".geo"}]
    if not histogram_columns:
        raise ValueError(f"No histogram column found in {path}")

    matrix = {
        MAPBIOMAS_PERSISTENCE_LABELS[code]: {ref_code: 0.0 for ref_code in reference.class_codes}
        for code in MAPBIOMAS_PERSISTENCE_LABELS
    }
    total = 0.0
    for encoded_value, count in _parse_histogram(str(raw[histogram_columns[0]].iloc[0])).items():
        encoded_code = int(float(encoded_value))
        mapbiomas_code = encoded_code // 10
        reference_code = encoded_code % 10
        row_label = MAPBIOMAS_PERSISTENCE_LABELS.get(mapbiomas_code)
        if row_label and reference_code in reference.class_codes:
            matrix[row_label][reference_code] += count
            total += count

    absolute = pd.DataFrame(matrix).T
    absolute.columns = [reference.class_labels.get(code, str(code)) for code in reference.class_codes]
    absolute["row_total"] = absolute.sum(axis=1)
    column_total = absolute.sum(axis=0)
    column_total.name = "column_total"
    absolute = pd.concat([absolute, column_total.to_frame().T])
    percent = absolute / total * 100 if total else absolute.copy()
    return absolute, percent


def compute_agreement_metrics(
    crosstab_tables: dict[tuple[str, str], pd.DataFrame],
    references: dict[str, ReferenceRaster],
    pixel_area_hectares: float,
) -> pd.DataFrame:
    """Compute agreement metrics from cross-tabulated pixel counts."""
    evaluated_rows = list(MAPBIOMAS_PERSISTENCE_LABELS.values())
    rows: list[dict[str, Any]] = []
    for (scenario, reference_name), table in sorted(crosstab_tables.items()):
        reference = references[reference_name]
        forest_columns = _existing_columns(table, reference.groups.get("forest", []))
        nonforest_columns = _existing_columns(table, reference.groups.get("nonforest", []))
        change_columns = _existing_columns(table, reference.groups.get("change", []))
        evaluated_columns = forest_columns + nonforest_columns + change_columns

        evaluated_pixels = sum(_cell(table, row, column) for row in evaluated_rows for column in evaluated_columns)
        forest_agreement = sum(_cell(table, "Persistent Forest", column) for column in forest_columns)
        nonforest_agreement = sum(_cell(table, "Persistent Non-Forest", column) for column in nonforest_columns)
        change_agreement = sum(_cell(table, "Land-Cover Change", column) for column in change_columns)
        agreement_pixels = forest_agreement + nonforest_agreement + change_agreement

        rows.append({
            "scenario": scenario,
            "reference": reference_name,
            "reference_label": reference.label,
            "evaluated_pixels": evaluated_pixels,
            "evaluated_area_million_hectares": evaluated_pixels * pixel_area_hectares / 1_000_000,
            "agreement_pixels": agreement_pixels,
            "overall_agreement_percent": _percent(agreement_pixels, evaluated_pixels),
            "forest_agreement_percent": _percent(
                forest_agreement,
                sum(_column_total(table, evaluated_rows, column) for column in forest_columns),
            ),
            "nonforest_agreement_percent": _percent(
                nonforest_agreement,
                sum(_column_total(table, evaluated_rows, column) for column in nonforest_columns),
            ),
            "change_agreement_percent": _percent(
                change_agreement,
                sum(_column_total(table, evaluated_rows, column) for column in change_columns),
            ),
        })
    return pd.DataFrame(rows)


def build_reclassification_table(settings: dict[str, Any]) -> pd.DataFrame:
    """Document the MapBiomas class treatment used in the analysis."""
    forest_codes = {int(code) for code in settings["analysis"]["forest_codes"]}
    excluded_codes = {int(code) for code in settings["analysis"]["excluded_mapbiomas_codes"]}
    rows = []
    for code, label in sorted(MAPBIOMAS_LAND_COVER_CLASSES.items()):
        role = "Forest" if code in forest_codes else "Excluded" if code in excluded_codes else "Non-Forest"
        rows.append({"mapbiomas_code": code, "mapbiomas_class": label, "analytical_role": role})
    return pd.DataFrame(rows)


def parse_fcbm_comparison_exports(table_directory: Path) -> dict[str, pd.DataFrame]:
    """Parse CTrees FCBM versus MB-FCBM comparison exports when present."""
    tables: dict[str, pd.DataFrame] = {}
    for path in sorted(table_directory.glob("*XTab_30m_*CTrees*_x_*MapBiomas*.csv")):
        raw = pd.read_csv(path)
        histogram_columns = [column for column in raw.columns if column not in {"system:index", ".geo"}]
        if not histogram_columns:
            continue
        labels = FCBM_TRANSITION_LABELS if path.name.startswith("FCBM_XTab") else {0: "Non-forest", 1: "Forest"}
        matrix = {labels[code]: {other: 0.0 for other in labels} for code in labels}
        for encoded_value, count in _parse_histogram(str(raw[histogram_columns[0]].iloc[0])).items():
            encoded_code = int(float(encoded_value))
            row_code = encoded_code // 100
            column_code = encoded_code % 100
            if row_code in labels and column_code in labels:
                matrix[labels[row_code]][labels[column_code]] += count
        table = pd.DataFrame(matrix).T
        table.columns = [labels[code] for code in labels]
        table["row_total"] = table.sum(axis=1)
        column_total = table.sum(axis=0)
        column_total.name = "column_total"
        tables[path.stem] = pd.concat([table, column_total.to_frame().T])
    return tables


def compute_fcbm_comparison_metrics(tables: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Compute agreement metrics for CTrees FCBM versus MB-FCBM products."""
    rows: list[dict[str, Any]] = []
    for name, table in sorted(tables.items()):
        metric_table = _accuracy_scheme_table(table) if name.startswith("FCBM_XTab") else table
        classes = [item for item in metric_table.index if item != "column_total"]
        total = sum(_cell(metric_table, row, column) for row in classes for column in classes)
        observed = sum(_cell(metric_table, label, label) for label in classes)
        expected = sum(
            _cell(metric_table, row, "row_total") * _cell(metric_table, "column_total", row)
            for row in classes
        )
        kappa = ((observed / total) - (expected / (total * total))) / (1 - (expected / (total * total))) if total and total * total != expected else math.nan
        for label in classes:
            rows.append(
                {
                    "comparison": name,
                    "class": label,
                    "evaluated_pixels": total,
                    "overall_agreement_percent": _percent(observed, total),
                    "cohen_kappa": kappa,
                    "user_accuracy_percent": _percent(_cell(metric_table, label, label), _cell(metric_table, label, "row_total")),
                    "producer_accuracy_percent": _percent(_cell(metric_table, label, label), _cell(metric_table, "column_total", label)),
                    "class_scheme": "VMD0055 v1.1 Table 16" if name.startswith("FCBM_XTab") else "Binary product-specific agreement",
                }
            )
    return pd.DataFrame(rows, columns=[
        "comparison",
        "class",
        "evaluated_pixels",
        "overall_agreement_percent",
        "cohen_kappa",
        "user_accuracy_percent",
        "producer_accuracy_percent",
        "class_scheme",
    ])


def parse_all_class_exports(table_directory: Path) -> dict[str, pd.DataFrame]:
    """Parse CTrees annual class versus MapBiomas land-cover cross-tabulations."""
    tables: dict[str, pd.DataFrame] = {}
    for path in sorted(table_directory.glob("AllClass_XTab_30m_CTrees_*_x_MapBiomas_LULC_*.csv")):
        raw = pd.read_csv(path)
        histogram_columns = [column for column in raw.columns if column not in {"system:index", ".geo"}]
        if not histogram_columns:
            continue
        rows = FCBM_VT0007_CLASS_LABELS
        columns = MAPBIOMAS_LAND_COVER_CLASSES
        matrix = {rows[code]: {label: 0.0 for label in columns.values()} for code in rows}
        for encoded_value, count in _parse_histogram(str(raw[histogram_columns[0]].iloc[0])).items():
            encoded_code = int(float(encoded_value))
            row_code = encoded_code // 100
            column_code = encoded_code % 100
            if row_code in rows and column_code in columns:
                matrix[rows[row_code]][columns[column_code]] += count
        table = pd.DataFrame(matrix).T
        table["row_total"] = table.sum(axis=1)
        column_total = table.sum(axis=0)
        column_total.name = "column_total"
        tables[path.stem] = pd.concat([table, column_total.to_frame().T])
    return tables


def compute_all_class_metrics(table_directory: Path) -> pd.DataFrame:
    """Compute binary accuracy metrics for shared-year CTrees and MapBiomas cross-tabs."""
    rows = []
    for path in sorted(table_directory.glob("Binary_XTab_30m_CTrees_*_x_MapBiomas_Binary_*.csv")):
        table = _parse_binary_xtab(path, multiplier=10)
        classes = ["Non-forest", "Forest"]
        total = sum(_cell(table, row, column) for row in classes for column in classes)
        observed = sum(_cell(table, label, label) for label in classes)
        expected = sum(_cell(table, row, "row_total") * _cell(table, "column_total", row) for row in classes)
        kappa = ((observed / total) - (expected / (total * total))) / (1 - (expected / (total * total))) if total and total * total != expected else math.nan
        year_match = re.search(r"CTrees_(\d{4})_x_MapBiomas", path.stem)
        for label in classes:
            rows.append(
                {
                    "year": int(year_match.group(1)) if year_match else None,
                    "class": label,
                    "evaluated_pixels": total,
                    "overall_agreement_percent": _percent(observed, total),
                    "cohen_kappa": kappa,
                    "user_accuracy_percent": _percent(_cell(table, label, label), _cell(table, label, "row_total")),
                    "producer_accuracy_percent": _percent(_cell(table, label, label), _cell(table, "column_total", label)),
                    "class_scheme": "Binary forest/non-forest collapse for Section 2.A; accuracy metrics reported per VMD0055 v1.1, Table 16.",
                }
            )
    return pd.DataFrame(rows)


def parse_change_agreement_exports(table_directory: Path) -> dict[str, pd.DataFrame]:
    """Parse forest-change agreement matrices for shared CTrees intervals."""
    tables: dict[str, pd.DataFrame] = {}
    labels = {
        1: "Stable forest",
        2: "Stable non-forest",
        3: "Forest loss",
        4: "Forest gain",
    }
    for path in sorted(table_directory.glob("ChangeAgreement_30m_CTrees_*_x_MapBiomas_*.csv")):
        raw = pd.read_csv(path)
        histogram_columns = [column for column in raw.columns if column not in {"system:index", ".geo"}]
        if not histogram_columns:
            continue
        matrix = {labels[code]: {other: 0.0 for other in labels.values()} for code in labels}
        for encoded_value, count in _parse_histogram(str(raw[histogram_columns[0]].iloc[0])).items():
            encoded_code = int(float(encoded_value))
            row_code = encoded_code // 10
            column_code = encoded_code % 10
            if row_code in labels and column_code in labels:
                matrix[labels[row_code]][labels[column_code]] += count
        table = pd.DataFrame(matrix).T
        table["row_total"] = table.sum(axis=1)
        column_total = table.sum(axis=0)
        column_total.name = "column_total"
        tables[path.stem] = pd.concat([table, column_total.to_frame().T])
    return tables


def parse_change_area_timeseries(table_directory: Path) -> pd.DataFrame:
    """Parse paired CTrees and MapBiomas forest-loss area exports."""
    rows = []
    for path in sorted(table_directory.glob("ChangeAreaTimeSeries_30m_CTrees_x_MapBiomas_*.csv")):
        frame = pd.read_csv(path)
        match = re.search(r"_(\d{4})_(\d{4})$", path.stem)
        rows.append(
            {
                "interval": f"{match.group(1)}-{match.group(2)}" if match else path.stem,
                "ctrees_loss_hectares": float(frame.get("ctrees_loss_hectares", pd.Series([math.nan])).iloc[0]),
                "ctrees_loss_million_hectares": float(frame.get("ctrees_loss_hectares", pd.Series([math.nan])).iloc[0]) / 1_000_000,
                "mapbiomas_loss_hectares": float(frame.get("mapbiomas_loss_hectares", pd.Series([math.nan])).iloc[0]),
                "mapbiomas_loss_million_hectares": float(frame.get("mapbiomas_loss_hectares", pd.Series([math.nan])).iloc[0]) / 1_000_000,
            }
        )
    return pd.DataFrame(rows)


def compute_class_decomposition(tables: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    """Compute MapBiomas forest/non-forest shares within each CTrees class."""
    decomposition: dict[str, pd.DataFrame] = {}
    forest_labels = {MAPBIOMAS_LAND_COVER_CLASSES[code] for code in (1, 3, 4) if code in MAPBIOMAS_LAND_COVER_CLASSES}
    for name, table in tables.items():
        rows = []
        for row_name in [index for index in table.index if index != "column_total"]:
            total = _cell(table, row_name, "row_total")
            forest = sum(_cell(table, row_name, column) for column in forest_labels if column in table.columns)
            nonforest = total - forest
            rows.append(
                {
                    "ctrees_class": row_name,
                    "mapbiomas_forest_pixels": forest,
                    "mapbiomas_forest_percent": _percent(forest, total),
                    "mapbiomas_nonforest_pixels": nonforest,
                    "mapbiomas_nonforest_percent": _percent(nonforest, total),
                    "evaluated_pixels": total,
                }
            )
        decomposition[name] = pd.DataFrame(rows)
    return decomposition


def parse_spatial_disagreement_exports(table_directory: Path) -> dict[str, pd.DataFrame]:
    """Parse municipality-level disagreement summaries."""
    tables: dict[str, pd.DataFrame] = {}
    for path in sorted(table_directory.glob("SpatialDisagreement_30m_CTrees_*_x_MapBiomas_Binary_*.csv")):
        frame = pd.read_csv(path)
        if "disagreement_percent" not in frame.columns and {"disagreement_hectares", "evaluated_hectares"}.issubset(frame.columns):
            frame["disagreement_percent"] = frame["disagreement_hectares"] / frame["evaluated_hectares"] * 100
        tables[path.stem] = frame
    return tables


def parse_temporal_consistency(table_directory: Path) -> pd.DataFrame:
    """Parse within-dataset temporal reversal area summary."""
    path = table_directory / "TemporalReversal_30m_CTrees_x_MapBiomas.csv"
    if not path.exists():
        matches = sorted(table_directory.glob("TemporalReversal_30m_CTrees_x_MapBiomas*.csv"))
        if not matches:
            return pd.DataFrame(columns=["dataset", "reversal_hectares", "reversal_million_hectares"])
        path = matches[0]
    frame = pd.read_csv(path)
    rows = []
    for dataset, column in (("CTrees", "ctrees_reversal_hectares"), ("MapBiomas", "mapbiomas_reversal_hectares")):
        value = float(frame.get(column, pd.Series([math.nan])).iloc[0])
        rows.append({"dataset": dataset, "reversal_hectares": value, "reversal_million_hectares": value / 1_000_000})
    return pd.DataFrame(rows)


def _parse_binary_xtab(path: Path, multiplier: int) -> pd.DataFrame:
    raw = pd.read_csv(path)
    histogram_columns = [column for column in raw.columns if column not in {"system:index", ".geo"}]
    labels = {0: "Non-forest", 1: "Forest"}
    matrix = {labels[code]: {other: 0.0 for other in labels.values()} for code in labels}
    if histogram_columns:
        for encoded_value, count in _parse_histogram(str(raw[histogram_columns[0]].iloc[0])).items():
            encoded_code = int(float(encoded_value))
            row_code = encoded_code // multiplier
            column_code = encoded_code % multiplier
            if row_code in labels and column_code in labels:
                matrix[labels[row_code]][labels[column_code]] += count
    table = pd.DataFrame(matrix).T
    table["row_total"] = table.sum(axis=1)
    column_total = table.sum(axis=0)
    column_total.name = "column_total"
    return pd.concat([table, column_total.to_frame().T])


def _accuracy_scheme_table(table: pd.DataFrame) -> pd.DataFrame:
    labels = FCBM_TRANSITION_LABELS
    accuracy_labels = FCBM_ACCURACY_CLASS_LABELS
    matrix = {accuracy_labels[code]: {other_label: 0.0 for other_label in accuracy_labels.values()} for code in accuracy_labels}
    for row_code, row_label in labels.items():
        for column_code, column_label in labels.items():
            accuracy_row = accuracy_labels[FCBM_ACCURACY_REMAP[row_code]]
            accuracy_column = accuracy_labels[FCBM_ACCURACY_REMAP[column_code]]
            matrix[accuracy_row][accuracy_column] += _cell(table, row_label, column_label)
    aggregated = pd.DataFrame(matrix).T
    aggregated["row_total"] = aggregated.sum(axis=1)
    column_total = aggregated.sum(axis=0)
    column_total.name = "column_total"
    return pd.concat([aggregated, column_total.to_frame().T])


def _first_match(directory: Path, pattern: str) -> Path:
    matches = sorted(directory.glob(pattern))
    if not matches:
        raise FileNotFoundError(f"Missing required file matching {pattern} in {directory}")
    if len(matches) > 1:
        names = ", ".join(path.name for path in matches[:10])
        raise RuntimeError(
            f"Ambiguous required file pattern {pattern} in {directory}: "
            f"{len(matches)} matches ({names})"
        )
    return matches[0]


def _parse_groups(value: str) -> list[dict[str, float]]:
    return [
        {key: float(number) for key, number in re.findall(r"([A-Za-z_]+)=([\d.E+\-]+)", body)}
        for body in re.findall(r"\{([^}]+)\}", value)
    ]


def _parse_histogram(value: str) -> dict[str, float]:
    return {key: float(count) for key, count in re.findall(r"([\d.]+)=([\d.E+\-]+)", value)}


def _existing_columns(table: pd.DataFrame, columns: list[str]) -> list[str]:
    return [column for column in columns if column in table.columns]


def _cell(table: pd.DataFrame, row: str, column: str) -> float:
    if row not in table.index or column not in table.columns:
        return 0.0
    return float(table.loc[row, column])


def _column_total(table: pd.DataFrame, rows: list[str], column: str) -> float:
    return sum(_cell(table, row, column) for row in rows)


def _percent(numerator: float, denominator: float) -> float:
    return numerator / denominator * 100 if denominator else math.nan
