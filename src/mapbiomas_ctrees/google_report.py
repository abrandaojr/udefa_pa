"""Google Docs report publication."""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any

import pandas as pd
import yaml

from .google_services import export_google_doc_as_word
from .google_services import upload_report_image


def publish_google_doc_report(
    docs_service: Any,
    drive_service: Any,
    settings_path: Path,
    settings: dict[str, Any],
    report_text: str,
    table_directory: Path,
    figure_directory: Path,
    word_output_path: Path,
) -> tuple[str, Path]:
    """Create or update the configured Google Docs report and export it as Word."""
    document_id = _ensure_report_document(docs_service, settings_path, settings)
    image_paths = _prepare_report_images(figure_directory, word_output_path.parent / "doc_assets")
    _ = table_directory
    _write_report_catalogs(word_output_path.parent, image_paths)
    image_uris = [
        (image_path, upload_report_image(drive_service, settings, image_path))
        for image_path in image_paths
    ]
    body_text = _build_google_doc_body(report_text, image_paths)
    _replace_document_content(docs_service, document_id, body_text, image_uris)
    word_path = export_google_doc_as_word(drive_service, document_id, word_output_path)
    return document_id, word_path


def _ensure_report_document(docs_service: Any, settings_path: Path, settings: dict[str, Any]) -> str:
    """Return the fixed document ID, creating and storing it when needed."""
    document_id = str(settings["google"].get("report_document_id", "")).strip()
    if document_id:
        return document_id

    title = str(
        settings["google"].get("report_document_title")
        or f"{settings['project']['name']} Technical Report"
    )
    created = docs_service.documents().create(body={"title": title}).execute()
    document_id = str(created["documentId"])
    settings["google"]["report_document_id"] = document_id
    _store_report_document_id(settings_path, document_id)
    return document_id


def _replace_document_content(
    docs_service: Any,
    document_id: str,
    body_text: str,
    image_uris: list[tuple[Path, str]],
) -> None:
    """Replace document text and insert report images."""
    document = docs_service.documents().get(documentId=document_id).execute()
    end_index = document["body"]["content"][-1]["endIndex"]
    requests = []
    if end_index > 2:
        requests.append({"deleteContentRange": {"range": {"startIndex": 1, "endIndex": end_index - 1}}})
    requests.append({"insertText": {"location": {"index": 1}, "text": body_text}})

    docs_service.documents().batchUpdate(
        documentId=document_id,
        body={"requests": requests},
    ).execute()

    for figure_number, (image_path, uri) in enumerate(image_uris, 1):
        _append_image(docs_service, document_id, image_path, uri, figure_number, label="Figure")


def _store_report_document_id(settings_path: Path, document_id: str) -> None:
    """Persist the created report document ID for subsequent runs."""
    data = yaml.safe_load(settings_path.read_text(encoding="utf-8"))
    data.setdefault("google", {})["report_document_id"] = document_id
    settings_path.write_text(
        yaml.safe_dump(data, sort_keys=False, allow_unicode=False),
        encoding="utf-8",
    )


def _build_google_doc_body(report_text: str, image_paths: list[Path]) -> str:
    """Build the Google Docs body with only figures and maps."""
    sections = [
        _report_title(report_text),
        "\nFIGURES AND MAPS\n",
        _list_of_figures(image_paths),
        "\nFigures and maps are inserted below.\n" if image_paths else "No PNG figures or maps were found.\n",
    ]
    return "\n".join(sections).strip() + "\n"


def _tables_text(table_paths: list[Path]) -> list[str]:
    """Return compact text versions of all final local tables."""
    if not table_paths:
        return ["No CSV tables were found.\n"]

    sections = []
    for table_number, path in enumerate(table_paths, 1):
        sections.append(f"\nTable {table_number}. {_table_title(path)}\n")
        sections.append(f"Source file: {path.name}\n")
        try:
            frame = pd.read_csv(path)
            sections.append(_format_table(frame))
            sections.append("\n")
        except Exception:
            sections.append("The table could not be rendered as text.\n")
    return sections


def _list_of_tables(table_paths: list[Path]) -> str:
    """Build a numbered list of tables for report front matter."""
    lines = ["LIST OF TABLES"]
    if not table_paths:
        lines.append("No tables were found.")
    else:
        lines.extend(
            f"Table {number}. {_table_title(path)} ({path.name})"
            for number, path in enumerate(table_paths, 1)
        )
    return "\n".join(lines) + "\n"


def _list_of_figures(image_paths: list[Path]) -> str:
    """Build a numbered list of figures and maps for report front matter."""
    lines = ["LIST OF FIGURES"]
    if not image_paths:
        lines.append("No figures or maps were selected.")
    else:
        lines.extend(
            f"Figure {number}. {_figure_title(path)} ({path.name})"
            for number, path in enumerate(image_paths, 1)
        )
    return "\n".join(lines) + "\n"


def _write_report_catalogs(report_directory: Path, image_paths: list[Path]) -> None:
    """Write the local list of figures included in the report."""
    report_directory.mkdir(parents=True, exist_ok=True)
    (report_directory / "list_of_tables.csv").unlink(missing_ok=True)
    pd.DataFrame(
        [
            {"number": number, "title": _figure_title(path), "file": path.name}
            for number, path in enumerate(image_paths, 1)
        ]
    ).to_csv(report_directory / "list_of_figures.csv", index=False)


def _select_report_tables(table_directory: Path) -> list[Path]:
    priority_names = ["agreement_metrics.csv", "mapbiomas_reclassification_schema.csv"]
    priority_paths = [table_directory / name for name in priority_names if (table_directory / name).exists()]
    remaining_paths = [
        path for path in sorted(table_directory.glob("*.csv"))
        if path not in set(priority_paths) and _is_essential_report_table(path)
    ]
    return priority_paths + remaining_paths


def _is_essential_report_table(path: Path) -> bool:
    essential_prefixes = (
        "area_",
        "Area_",
        "change_area",
        "ChangeArea_",
        "forest_change",
        "ForestChange_",
        "temporal_consistency",
        "municipal_",
        "MapBiomas_",
    )
    essential_names = {
        "agreement_metrics.csv",
        "mapbiomas_reclassification_schema.csv",
        "change_area_by_interval.csv",
        "change_area_forest_to_nonforest.csv",
        "forest_change_area_timeseries.csv",
        "temporal_consistency_reversals.csv",
    }
    return path.name in essential_names or path.name.startswith(essential_prefixes)


def _format_table(frame: pd.DataFrame) -> str:
    """Return a clean text table with consistent labels and numeric precision."""
    formatted = frame.copy()
    formatted.columns = [_column_title(column) for column in formatted.columns]
    for column in formatted.columns:
        if pd.api.types.is_numeric_dtype(formatted[column]):
            formatted[column] = formatted[column].map(_format_number)
        else:
            formatted[column] = formatted[column].fillna("").astype(str)

    widths = [
        max(len(str(column)), *(len(str(value)) for value in formatted[column]))
        for column in formatted.columns
    ]
    header = " | ".join(str(column).ljust(width) for column, width in zip(formatted.columns, widths))
    rule = " | ".join("-" * width for width in widths)
    rows = [
        " | ".join(str(value).ljust(width) for value, width in zip(row, widths))
        for row in formatted.itertuples(index=False, name=None)
    ]
    return "\n".join([header, rule, *rows])


def _format_number(value: Any) -> str:
    """Format table numbers for technical reporting."""
    if pd.isna(value):
        return ""
    number = float(value)
    if number == 0:
        return "0"
    if abs(number) >= 1000 and number.is_integer():
        return f"{number:,.0f}"
    if abs(number) >= 100:
        return f"{number:,.2f}"
    if abs(number) >= 1:
        return f"{number:,.3f}"
    return f"{number:.6f}"


def _column_title(column: Any) -> str:
    """Convert source column names to report column labels."""
    text = str(column).strip()
    replacements = {
        "_": " ",
        "pct": "percent",
        "mha": "million hectares",
        "ha": "hectares",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    return " ".join(word.capitalize() if word.islower() else word for word in text.split())


def _table_title(path: Path) -> str:
    """Return a readable title for a CSV table."""
    name = path.stem
    if name == "agreement_metrics":
        return "Agreement metrics by scenario and CTrees reference"
    if name == "mapbiomas_reclassification_schema":
        return "MapBiomas class reclassification schema"
    if name.startswith("area_by_class_"):
        return f"Area by persistence class for {_clean_title(name.removeprefix('area_by_class_'))}"
    if name.startswith("area_"):
        return f"Area summary for {_clean_title(name.removeprefix('area_'))}"
    if name.startswith("Area_"):
        return f"Area summary for {_clean_title(name.removeprefix('Area_'))}"
    if name.startswith(("crosstab_", "XTab_")):
        return f"Cross-tabulation for {_clean_title(name)}"
    return _clean_title(name)


def _figure_title(path: Path) -> str:
    """Return a readable title for a figure or map image."""
    return _clean_title(path.stem)


def _clean_title(value: str) -> str:
    """Convert file-stem text to a readable report title."""
    text = value.replace("_", " ").replace("-", " to ")
    replacements = {
        "pct": "percent",
        "30m": "30 m",
        "MB": "MapBiomas",
        "CTrees": "CTrees",
        "LULC": "land use and land cover",
        "XTab": "cross-tabulation",
        "CrossRef": "cross-reference",
        "AreaBar": "area by class",
        "AgreementMetrics": "agreement metrics",
        "ScenA": "scenario A",
        "ScenB": "scenario B",
        "ScenC": "scenario C",
        "ScenD": "scenario D",
        "ScenE": "scenario E",
        "ScenF": "scenario F",
        "f2nf": "forest to non-forest",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    text = re.sub(r"(\d+)percent", r"\1 percent", text, flags=re.IGNORECASE)
    text = " ".join(text.split()).title()
    return text.replace("30 M", "30 m")


def _select_report_images(figure_directory: Path) -> list[Path]:
    """Select essential PNG figures and maps for insertion into Google Docs."""
    if not figure_directory.exists():
        return []
    excluded_names = {
        "Figure_2B_ChangeAgreement_2018_2024.png",
        "Figure_2B_ChangeAgreement_2009_2013.png",
        "Figure_2B_ChangeAgreement_2013_2018.png",
        "Figure_04_CTrees_Snapshot_x_MB_LULC.png",
        "Figure_02_AreaBar_30m.png",
        "Figure_08_ForestChangeArea_TimeSeries_30m.png",
        "Figure_11_TemporalReversal_Area_30m.png",
    }
    priority_names = [
        "Figure_01_AgreementMetrics_30m.png",
        "Figure_02_MapBiomasPersistenceAreaByScenario_30m.png",
        "Figure_03_DMJSS_CrossRef_30m.png",
        "Figure_03_FCBM1_2009_CrossRef_30m.png",
        "Figure_03_FCBM2_2013_CrossRef_30m.png",
        "Figure_03_FCBM3_2018_CrossRef_30m.png",
        "Figure_03_FCBM4_CrossRef_30m.png",
        "Figure_04_CTreesSnapshotForestCover_2009_2018.png",
        "Figure_07_ForestToNonForest_Area_30m.png",
        "Figure_08_ForestLossAreaByInterval_CTreesMapBiomas_30m.png",
        "Figure_11_TemporalReversalAreaByDataset_30m.png",
        "Figure_2B_ForestLossAgreement_CTreesMapBiomas_2009_2013.png",
        "Figure_2B_ForestLossAgreement_CTreesMapBiomas_2013_2018.png",
        "Map_Agreement_Change_2009_2013.png",
        "Map_Agreement_Change_2013_2018.png",
        "Map_Change_ForestToNonForest_MapBiomas_1985_2024.png",
        "Map_Change_ForestToNonForest_CTrees_DMJSS.png",
        "Map_Change_ForestToNonForest_CTrees_FCBM4.png",
        "Map_Change_ForestToNonForest_CTrees_FCBM1_2009_to_FCBM2_2013.png",
        "Map_Change_ForestToNonForest_CTrees_FCBM2_2013_to_FCBM3_2018.png",
        "Map_ScenA_100pct_1985_2024.png",
        "Map_ScenC_50pct_1985_2024.png",
        "Map_ScenF_100pct_2018_2024.png",
        "Map_MB_Annual_1985.png",
        "Map_MB_Annual_2009.png",
        "Map_MB_Annual_2013.png",
        "Map_MB_Annual_2018.png",
        "Map_MB_Annual_2024.png",
    ]
    priority_paths = [
        figure_directory / name
        for name in priority_names
        if name not in excluded_names and (figure_directory / name).exists()
    ]
    remaining_paths = [
        path for path in sorted(figure_directory.glob("*.png"))
        if path.name not in excluded_names and path not in set(priority_paths)
    ]
    return priority_paths + remaining_paths


def _prepare_report_images(figure_directory: Path, asset_directory: Path) -> list[Path]:
    """Create compact image copies for Google Docs insertion."""
    selected = _select_report_images(figure_directory)
    if not selected:
        return []
    asset_directory.mkdir(parents=True, exist_ok=True)
    prepared = []
    for image_path in selected:
        target = asset_directory / f"{image_path.stem}.jpg"
        try:
            from PIL import Image

            with Image.open(image_path) as image:
                image = image.convert("RGB")
                image.thumbnail((1400, 1400))
                image.save(target, format="JPEG", quality=72, optimize=True)
            prepared.append(target)
        except Exception:
            prepared.append(image_path)
    return prepared


def _inventory_text(table_directory: Path, figure_directory: Path) -> list[str]:
    """List all local tables and image outputs available on disk."""
    sections = ["Tables saved locally:\n"]
    sections.extend(f"- {path.name}\n" for path in sorted(table_directory.glob("*.csv")))
    sections.append("\nFigures and maps saved locally:\n")
    sections.extend(f"- {path.name}\n" for path in sorted(figure_directory.glob("*.png")))
    return sections


def _append_image(
    docs_service: Any,
    document_id: str,
    image_path: Path,
    uri: str,
    item_number: int,
    label: str,
) -> None:
    """Append one image at the end of the Google Docs report."""
    document = docs_service.documents().get(documentId=document_id).execute()
    end_index = document["body"]["content"][-1]["endIndex"] - 1
    caption = f"{label} {item_number}. {_figure_title(image_path)}. {_figure_reference(image_path)}"
    requests = [
        {
            "insertText": {
                "location": {"index": end_index},
                "text": f"\n{caption}\n",
            }
        },
        {
            "insertInlineImage": {
                "location": {"index": end_index + len(caption) + 2},
                "uri": uri,
                "objectSize": {
                    "height": {"magnitude": 360, "unit": "PT"},
                    "width": {"magnitude": 520, "unit": "PT"},
                },
            }
        },
    ]
    try:
        docs_service.documents().batchUpdate(documentId=document_id, body={"requests": requests}).execute()
    except Exception:
        fallback = (
            f"\n{caption}\n"
            f"Image link: {uri}\n"
            "The asset is stored in Google Drive and locally in outputs.\n"
        )
        document = docs_service.documents().get(documentId=document_id).execute()
        end_index = document["body"]["content"][-1]["endIndex"] - 1
        docs_service.documents().batchUpdate(
            documentId=document_id,
            body={"requests": [{"insertText": {"location": {"index": end_index}, "text": fallback}}]},
        ).execute()


def _google_doc_text(markdown_text: str) -> str:
    """Convert the local Markdown report to simple Google Docs text."""
    lines = []
    for line in markdown_text.splitlines():
        if line.startswith("# "):
            lines.append(line[2:].upper())
        elif line.startswith("## "):
            lines.append(line[3:].upper())
        elif line.startswith("### "):
            lines.append(line[4:])
        elif line.startswith("- `") and line.endswith("`"):
            lines.append("- " + line[3:-1])
        else:
            lines.append(line)
    return "\n".join(lines).strip() + "\n"


def _report_title(markdown_text: str) -> str:
    for line in markdown_text.splitlines():
        if line.startswith("# "):
            return line[2:].strip().upper() + "\n"
    return "MAPBIOMAS CTREES PARA TECHNICAL REPORT\n"


def _figure_reference(path: Path) -> str:
    name = path.stem.lower()
    if "foresttononforest" in name or "change" in name:
        return "Class scheme: deforestation and forest-to-nonforest change; source: VT0007 v1.0 Section Data Requirements and VMD0055 v1.1 Table 15."
    if "ctrees_snapshot" in name or "fcbm" in name:
        return "Class scheme: CTrees FCBM forest/non-forest interpretation; source: VT0007 v1.0 Table 1."
    if "agreement" in name or "crossref" in name:
        return "Class scheme: accuracy assessment groups; source: VMD0055 v1.1 Table 16."
    if "map_scen" in name or "mapbiomas" in name or "mb_annual" in name:
        return "Class scheme: MapBiomas forest/non-forest analytical reclassification for comparison with CTrees."
    return "Source: VMD0055 v1.1 and VT0007 v1.0 analytical workflow."
