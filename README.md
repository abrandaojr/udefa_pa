# UDefA Para Analysis Pipeline

This repository contains the Python workflow used to prepare, audit, synchronize, and analyze UDefA Para products derived from MapBiomas and CTrees references.

It is not a mirror of the upstream UDefA application. It is the execution repository for the Para analysis script: product catalog checks, Earth Engine export management, raster synchronization, IDRISI conversion, local metrics, and report/presentation publication.

The script is focused on one reproducible pipeline:

1. audit expected CSV and raster products;
2. submit or download Earth Engine exports through Google Drive;
3. build aligned 30 m GeoTIFF mosaics and IDRISI raster products;
4. compare MapBiomas persistence scenarios with CTrees FCBM and DMJSS references;
5. generate tables, figures, a technical report, and presentation outputs.

Generated outputs are intentionally not committed. The repository tracks source code, configuration, tests, and documentation only; rasters, reports, logs, OAuth tokens, and local data products are recreated by running the workflow.

## Repository Scope

Tracked in Git:

- workflow source code and helper modules;
- UDefA Para configuration;
- pipeline audit and quality-gate tests;
- documentation describing how to reproduce the run.

Not tracked in Git:

- Google OAuth credentials and local virtual environments;
- Earth Engine exports downloaded from Drive;
- GeoTIFF tiles, mosaics, IDRISI rasters, and raster inventories;
- generated CSV/XLSX tables, figures, logs, Word reports, and PowerPoint decks.

## Directory Structure

```text
.
|-- main.py
|-- run_workflow.ps1
|-- requirements.txt
|-- config/
|   `-- settings.yaml
|-- data/
|   |-- input/
|   `-- intermediate/
|-- outputs/
|   |-- logs/
|   |-- reports/
|   `-- tables/
`-- src/
    `-- mapbiomas_ctrees/
        |-- constants.py
        |-- data_cleaning.py
        |-- data_preparation.py
        |-- export_tables.py
        |-- google_services.py
        |-- local_tables.py
        |-- logging_utils.py
        |-- models.py
        |-- reporting.py
        `-- settings.py
```

## What The Script Really Does

- `main.py`: command-line orchestration and workflow gates.
- `catalog.py` and `pipeline_state.py`: expected product inventory, local audit state, and progress reporting.
- `data_preparation.py` and `data_cleaning.py`: Earth Engine inputs, masks, persistence scenarios, and reference harmonization.
- `export_tables.py` and `raster_exports.py`: CSV and raster export submission, download synchronization, GeoTIFF mosaics, IDRISI conversion, and grid validation.
- `local_tables.py`, `figures.py`, and `reporting.py`: local analysis, publication tables, maps, figures, and technical report outputs.
- `google_services.py`, `google_report.py`, `table_publication.py`, and `presentation.py`: Google Drive, Docs, Sheets, and Slides publication steps.
- `tests/`: regression tests for pipeline quality gates, raster conversion, and export synchronization logic.

## Installation

Create and activate a Python environment, then install dependencies:

```bash
pip install -r requirements.txt
```

Authenticate Earth Engine:

```bash
earthengine authenticate
```

For Google Drive downloads and Google Docs report updates, place an OAuth client file at `.google_auth/client_secret.json` or use Application Default Credentials.

If Application Default Credentials expire or are revoked, refresh them with the workflow scopes:

```bash
gcloud auth application-default revoke --quiet
gcloud auth application-default login --scopes "https://www.googleapis.com/auth/cloud-platform,https://www.googleapis.com/auth/earthengine,https://www.googleapis.com/auth/drive,https://www.googleapis.com/auth/documents"
```

If Google shows "This app is blocked" during that login, create your own OAuth Client ID in Google Cloud Console:

1. Open APIs & Services > Credentials in the `ee-abrandaojr` project.
2. Create OAuth client ID with Application type `Desktop app`.
3. Download the JSON and save it as `.google_auth/client_secret.json`.
4. Run:

```bash
python main.py --auth-only
```

The workflow will create `.google_auth/token.json` from that client and reuse it on later runs.

## Configuration

Edit `config/settings.yaml` to define:

- the Earth Engine project;
- the study area;
- the MapBiomas image;
- CTrees reference assets and class groups;
- MapBiomas forest and excluded classes;
- persistence scenarios;
- output folders.
- the Google Docs report title in `google.report_document_title`.
- optionally, an existing fixed Google Docs report ID in `google.report_document_id`.

If `google.report_document_id` is empty, the first `--analyze` run creates the Google Docs report and stores the new ID in `config/settings.yaml`. Subsequent runs update that same document.

If you already have a document, the fixed report ID is the long identifier in a Google Docs URL:

```text
https://docs.google.com/document/d/THIS_IS_THE_DOCUMENT_ID/edit
```

When the Google scopes change, the saved token at `.google_auth/token.json` may need to be regenerated. The workflow detects missing scopes and starts authorization again when an OAuth client file is available.

## Running The Workflow

On Windows, the simplest entry point is:

```powershell
.\run_workflow.ps1
```

That command runs `main.py` with the default behavior: download ready CSV exports, sync rasters, and run analysis.

Submit Earth Engine CSV exports:

```bash
python main.py --submit-exports
```

Submit exports and wait for completion:

```bash
python main.py --submit-exports
```

Submit exports without waiting:

```bash
python main.py --submit-exports --no-wait
```

Download completed CSV exports from Google Drive:

```bash
python main.py --download-exports
```

Submit all aligned 30 m raster products as GeoTIFF exports:

```bash
python main.py --submit-rasters --submit-change-areas --no-wait
```

After Earth Engine finishes those tasks, download GeoTIFF rasters and convert them to IDRISI:

```bash
python main.py --download-rasters
```

Downloaded Earth Engine GeoTIFF tiles are saved to `outputs/rasters/geotiff_tiles`. Local mosaic GeoTIFFs are saved to `outputs/rasters/geotiff`, IDRISI `.rst/.rdc` files are saved to `outputs/rasters/idrisi`, and `outputs/rasters/raster_grid_inventory.csv` records the row/column count, CRS, affine transform, and 30 m pixel size for grid checks.

The IDRISI conversion also refreshes palette sidecars and writes a combined 16:9 map panel at `outputs/rasters/idrisi/idrisi_maps_panel.png`. The PNG is generated at 4000 x 2250 px / 300 dpi so it fits a 13.33 x 7.5 in widescreen slide. When a local `UDefA_ParaStateMask` raster is available, every pixel outside the state of Para is written as IDRISI missing data and rendered black; `Valid_Analysis_Mask` is used only as a fallback for older local outputs. To regenerate only that panel from existing local `.rst` files:

```bash
python main.py --generate-idrisi-panel
```

Analyze local exported CSV files and write final outputs:

```bash
python main.py --analyze
```

This command also writes all CSV tables to one Excel workbook at `outputs/reports/all_tables.xlsx`, publishes the same tables to the configured Google Sheets workbook, replaces the fixed Google Docs report configured in `google.report_document_id`, and downloads a Word copy to `outputs/reports/technical_report.docx`. The Google Docs and Word reports contain only graphs and maps; tables are kept in Excel and Google Sheets.

Analyze local exported CSV files without Google Drive or Google Docs authentication:

```bash
python main.py --analyze --skip-google-report
```

Run download and analysis together:

```bash
python main.py --download-exports --analyze
```

Running `python main.py` with no flags downloads completed CSV exports from Google Drive, syncs raster products, and then runs the local analysis.

Audit local pipeline state without Google access:

```bash
python main.py --audit-only
```

This writes `outputs/pipeline_state.json`, `outputs/logs/pipeline_audit.md`, and updates the single VS Code dashboard at `outputs/logs/progresso_workflow.md`. The progress dashboard shows active workflow steps, data-quality summary, blocking issues, and required products in one place; the JSON keeps the full machine-readable inventory.

Use a different output folder:

```bash
python main.py --analyze --output-root outputs/run_001
```

## Outputs

Final outputs are written under `outputs/` and are ignored by Git:

- `outputs/tables/agreement_metrics.csv`
- `outputs/tables/area_by_class_*.csv`
- `outputs/tables/crosstab_*_pixels.csv`
- `outputs/tables/crosstab_*_percent.csv`
- `outputs/tables/mapbiomas_reclassification_schema.csv`
- `outputs/reports/technical_report.md`
- `outputs/reports/technical_report.docx`
- `outputs/reports/doc_assets/*.jpg`
- `outputs/figures/*.png`, when figure and map outputs are available locally
- `outputs/figures/*.pdf`, matching the generated PNG figures
- `outputs/logs/workflow.log`
- `outputs/logs/progresso_workflow.md`
- `outputs/logs/pipeline_audit.md`
- `outputs/pipeline_state.json`
- `outputs/reports/presentation.pptx`

When Google reporting is enabled, the workflow also updates the configured Google Docs report, Google Sheets workbook, and Google Slides presentation imported from the generated PowerPoint file.

## Notes

The workflow assumes that the authenticated account can read the Earth Engine assets listed in `config/settings.yaml`. Earth Engine exports are asynchronous, so table exports must finish before the local analysis step can parse them.
