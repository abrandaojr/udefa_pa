from __future__ import annotations

import subprocess
subprocess.run(["cls"], shell=True, check=False)

"""Orchestrate the MapBiomas vs. CTrees analytical workflow.

Research questions addressed by this pipeline:
RQ1 pixel-level agreement between MapBiomas and CTrees;
RQ2 spatial pattern and extent of classification disagreement;
RQ3 forest-to-nonforest change area differences across scenarios;
RQ4 temporal consistency of MapBiomas forest change labels;
RQ5 total forest change area time series per scenario;
RQ6 municipal-level forest change patterns across scenarios;
RQ7 area by LULC class per scenario and year in hectares;
RQ8 internal consistency of the Verra binary reclassification schema.
"""

import argparse
import json
import logging
from pathlib import Path
import sys


def _bootstrap_local_venv_site_packages() -> None:
    """Allow an approved Python executable to reuse dependencies installed in .venv."""
    candidates = [
        Path.home() / ".venvs" / "tc-scripts" / "Lib" / "site-packages",
        Path(__file__).resolve().parent / ".venv" / "Lib" / "site-packages",
    ]
    for venv_site_packages in candidates:
        if venv_site_packages.exists():
            site_packages_text = str(venv_site_packages)
            if site_packages_text not in sys.path:
                sys.path.insert(0, site_packages_text)
            break


_bootstrap_local_venv_site_packages()

from src.mapbiomas_ctrees.constants import PRIMARY_MAPBIOMAS_YEARS, validate_verra_class_mappings
from src.mapbiomas_ctrees.data_cleaning import clean_and_organize_data, load_reference_metadata
from src.mapbiomas_ctrees.data_quality import assert_gate, required_csv_gate, required_raster_gate
from src.mapbiomas_ctrees.data_preparation import prepare_input_data
from src.mapbiomas_ctrees.export_tables import submit_fcbm_comparison_exports, submit_table_exports, wait_for_tasks
from src.mapbiomas_ctrees.figures import (
    generate_change_area_figure,
    generate_earth_engine_report_maps,
    generate_report_figures,
)
from src.mapbiomas_ctrees.google_services import (
    build_docs_service,
    build_drive_service,
    build_sheets_service,
    download_drive_exports,
    download_drive_raster_exports,
    ensure_drive_raster_folder,
    initialize_earth_engine,
    load_google_credentials,
    publish_powerpoint_as_google_slides,
)
from src.mapbiomas_ctrees.google_report import publish_google_doc_report
from src.mapbiomas_ctrees.local_tables import analyze_exported_tables
from src.mapbiomas_ctrees.logging_utils import configure_logging
from src.mapbiomas_ctrees.catalog import expected_csv_exports, expected_raster_products
from src.mapbiomas_ctrees.pipeline_state import (
    audit_pipeline_state,
    write_pipeline_audit_markdown,
    write_pipeline_state,
)
from src.mapbiomas_ctrees.presentation import build_powerpoint_presentation
from src.mapbiomas_ctrees.progress import WorkflowProgress
from src.mapbiomas_ctrees.raster_exports import (
    build_geotiff_mosaics,
    build_raster_products,
    build_raster_status_table,
    convert_geotiffs_to_idrisi,
    print_raster_status_table,
    submit_change_area_export,
    submit_raster_exports,
    validate_common_grid,
    wait_for_raster_tasks,
)
from src.mapbiomas_ctrees.reporting import WorkflowOutputs, write_results
from src.mapbiomas_ctrees.settings import ensure_output_directories, load_settings
from src.mapbiomas_ctrees.table_publication import publish_google_sheets_tables, publish_table_workbook

LOGGER = logging.getLogger(__name__)


def _log_folder_summary(label: str, directory: Path, patterns: tuple[str, ...]) -> None:
    """Log one concise output-folder summary."""
    count = 0
    if directory.exists():
        seen: set[Path] = set()
        for pattern in patterns:
            seen.update(path for path in directory.glob(pattern) if path.is_file())
        count = len(seen)
    LOGGER.info("%s | folder=%s | files=%s", label, directory, count)


def parse_arguments() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Run the UDefA Para analysis pipeline."
    )
    parser.add_argument("--config", default="config/settings.yaml", help="Path to the YAML settings file.")
    parser.add_argument("--output-root", default=None, help="Optional output directory override.")
    parser.add_argument(
        "--submit-exports",
        action="store_true",
        help="Submit Earth Engine CSV exports for area and cross-tabulation analyses.",
    )
    parser.add_argument(
        "--download-exports",
        action="store_true",
        help="Download completed Earth Engine CSV exports from Google Drive.",
    )
    parser.add_argument(
        "--submit-rasters",
        action="store_true",
        help="Submit aligned 30 m GeoTIFF raster exports to Google Drive.",
    )
    parser.add_argument(
        "--submit-change-areas",
        action="store_true",
        help="Submit forest-to-nonforest area tables to Google Drive.",
    )
    parser.add_argument(
        "--download-rasters",
        action="store_true",
        help="Download GeoTIFF raster exports and convert them to IDRISI.",
    )
    parser.add_argument(
        "--analyze",
        action="store_true",
        help="Analyze local CSV exports and write final results.",
    )
    parser.add_argument(
        "--skip-google-report",
        action="store_true",
        help="Write local analysis outputs without creating/updating the Google Docs report.",
    )
    parser.add_argument(
        "--no-wait",
        action="store_true",
        help="Submit Earth Engine tasks without waiting for completion.",
    )
    parser.add_argument(
        "--auth-only",
        action="store_true",
        help="Create or refresh Google OAuth credentials, then exit.",
    )
    parser.add_argument(
        "--audit-only",
        action="store_true",
        help="Write local pipeline_state.json and pipeline_audit.md, then exit without Google access.",
    )
    parser.add_argument(
        "--raster-root",
        default=None,
        help="Override the raster output root directory. An absolute path is recommended.",
    )
    parser.add_argument(
        "--sync-rasters",
        action="store_true",
        help="Download raster exports from Drive if they exist; submit Earth Engine exports if they do not.",
    )
    parser.add_argument("--verbose", action="store_true", help="Enable detailed logging.")
    args = parser.parse_args()
    if not any((
        args.submit_exports,
        args.download_exports,
        args.submit_rasters,
        args.submit_change_areas,
        args.download_rasters,
        args.sync_rasters,
        args.analyze,
        args.auth_only,
        args.audit_only,
    )):
        args.download_exports = True
        args.sync_rasters = True
        args.analyze = True
    return args


def main() -> None:
    """Execute the workflow in four sequential analytical sections."""
    args = parse_arguments()
    project_settings = load_settings(args.config, output_root=args.output_root)
    ensure_output_directories(project_settings)
    configure_logging(project_settings.output_directories["logs"], verbose=args.verbose)
    progress = WorkflowProgress(project_settings.output_directories["logs"] / "workflow_progress.md")
    LOGGER.info("Workflow progress table: %s", progress.path)

    raw_settings = project_settings.raw
    pixel_area_hectares = (float(raw_settings["earth_engine"]["scale_native_m"]) ** 2) / 10000
    prepared_inputs = None
    organized_data = None
    products = None

    validate_verra_class_mappings()

    verra_validation_path = project_settings.output_directories["reports"] / "mapbiomas_verra_validation.json"
    verra_validation_path.write_text(
        json.dumps(
            {
                "validated": True,
                "schemes": [
                    "FCBM_VT0007_TABLE15",
                    "FCBM_ACCURACY_TABLE16",
                    "UDEFA_RISK_GROUPS",
                    "MB_FCBM_TRANSITION_RULES",
                ],
                "verra_references": [
                    "VMD0055 v1.1 Table 15",
                    "VMD0055 v1.1 Table 16",
                    "VT0007 v1.0 Table 1",
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    credentials = None
    drive_service = None
    docs_service = None
    sheets_service = None
    if args.auth_only:
        progress.update("Google Credentials", "Authenticating", "Preparing Google credentials")
        load_google_credentials(raw_settings)
        progress.update("Google Credentials", "Ready", "Google credentials are ready")
        LOGGER.info("Google OAuth credentials are ready.")
        return

    needs_google_report = args.analyze and not args.skip_google_report
    if (
        args.submit_exports
        or args.download_exports
        or args.submit_rasters
        or args.submit_change_areas
        or args.download_rasters
        or args.sync_rasters
        or needs_google_report
    ):
        progress.update("Google Credentials", "Loading", "Initializing Drive")
        credentials = load_google_credentials(raw_settings)
        drive_service = build_drive_service(credentials)
        progress.update("Google Credentials", "Ready", "Drive initialized")
    if needs_google_report:
        docs_service = build_docs_service(credentials)
        sheets_service = build_sheets_service(credentials)

    references = load_reference_metadata(raw_settings)
    scenario_labels = [scenario.label for scenario in project_settings.scenarios]

    table_directory = project_settings.output_directories["tables"]
    if args.raster_root is not None:
        raster_root = Path(args.raster_root).resolve()
    else:
        raster_root = (project_settings.output_root / "rasters").resolve()
    geotiff_tile_directory = raster_root / "geotiff_tiles"
    geotiff_directory = raster_root / "geotiff"
    idrisi_directory = raster_root / "idrisi"

    expected_csvs = expected_csv_exports(project_settings.scenarios, list(references.keys()))
    expected_rasters = expected_raster_products(raw_settings, project_settings.scenarios)
    audit, state_path, audit_path = _refresh_pipeline_audit(
        project_settings,
        expected_csvs,
        expected_rasters,
        table_directory,
        geotiff_tile_directory,
        geotiff_directory,
        idrisi_directory,
        progress,
    )
    LOGGER.info(
        "Pipeline audit: %d product(s), %d issue(s). State=%s Audit=%s",
        audit.summary.get("total", 0),
        len(audit.issues),
        state_path,
        audit_path,
    )
    progress.update(
        "Audit",
        "Ready",
        f"{audit.summary.get('total', 0)} product(s), "
        f"{audit.summary.get('required_not_ready', 0)} required product(s) pending, "
        f"{len(audit.issues)} issue(s): {audit_path}",
    )
    if args.audit_only:
        progress.update(
            "Audit",
            "Ready",
            f"{audit.summary.get('total', 0)} product(s), {len(audit.issues)} issue(s): {audit_path}",
        )
        return

    if args.submit_exports or args.submit_rasters or args.submit_change_areas:
        initialize_earth_engine(raw_settings["earth_engine"]["project"], credentials=credentials)
        progress.update("Earth Engine", "Initialized", "Earth Engine project initialized")
        LOGGER.info("1. Data preparation")
        prepared_inputs = prepare_input_data(raw_settings)

        LOGGER.info("2. Data cleaning and organization")
        organized_data = clean_and_organize_data(
            settings=raw_settings,
            scenarios=project_settings.scenarios,
            mapbiomas_image=prepared_inputs.mapbiomas_image,
            area_of_interest=prepared_inputs.area_of_interest,
        )
        references = organized_data.references
        scenario_labels = list(organized_data.persistence_rasters.keys())

        products = build_raster_products(raw_settings, project_settings.scenarios, prepared_inputs, organized_data)

        if args.submit_exports:
            progress.update("Earth Engine", "Submitting", "CSV exports")
            LOGGER.info("3. Data analysis: submitting Earth Engine table exports")
            tasks = submit_table_exports(
                settings=raw_settings,
                persistence_rasters=organized_data.persistence_rasters,
                references=organized_data.references,
                pixel_area_hectares=prepared_inputs.pixel_area_hectares,
                area_of_interest=prepared_inputs.area_of_interest,
            )
            tasks.extend(submit_fcbm_comparison_exports(raw_settings, prepared_inputs, organized_data))
            change_task = submit_change_area_export(raw_settings, products, prepared_inputs.area_of_interest)
            if change_task is not None:
                tasks.append(change_task)
            if args.no_wait:
                progress.update("Earth Engine", "Queued", "CSV exports submitted")
                LOGGER.info("Table exports submitted. Wait for Earth Engine completion before analysis.")
                return
            progress.update("Earth Engine", "Waiting", "CSV exports")
            wait_for_tasks(tasks)
            progress.update("Earth Engine", "Ready", "CSV exports completed")
        if args.submit_rasters:
            progress.update("Earth Engine", "Submitting", "Raster exports")
            LOGGER.info("3. Data analysis: submitting aligned Earth Engine raster exports")
            ensure_drive_raster_folder(drive_service, raw_settings)
            tasks = submit_raster_exports(
                settings=raw_settings,
                products=products,
                area_of_interest=prepared_inputs.area_of_interest,
            )
            if args.no_wait:
                progress.update("Earth Engine", "Queued", "Raster exports submitted")
                LOGGER.info("Raster exports submitted. Wait for Earth Engine completion before download.")
            else:
                progress.update("Earth Engine", "Waiting", "Raster exports")
                wait_for_raster_tasks(tasks)
                progress.update("Earth Engine", "Ready", "Raster exports completed")
        if args.submit_change_areas:
            progress.update("Earth Engine", "Submitting", "Change-area export")
            LOGGER.info("3. Data analysis: submitting forest-to-nonforest change-area export")
            task = submit_change_area_export(raw_settings, products, prepared_inputs.area_of_interest)
            if args.no_wait:
                progress.update("Earth Engine", "Queued", "Change-area export submitted")
                LOGGER.info("Change-area export submitted. Wait for Earth Engine completion before download.")
                return
            if task is not None:
                progress.update("Earth Engine", "Waiting", "Change-area export")
                wait_for_tasks([task])
                progress.update("Earth Engine", "Ready", "Change-area export completed")

    if args.download_exports:
        if drive_service is None:
            raise RuntimeError("Google Drive service was not initialized.")
        local_tables = list(table_directory.glob("*.csv")) if table_directory.exists() else []
        csv_gate = required_csv_gate(audit)
        if csv_gate.ok:
            progress.update("CSV exports", "Ready", f"{len(local_tables)} local CSV files; required products ready")
            LOGGER.info(
                "Data analysis: %d CSV table(s) already present locally and required CSVs are ready. Skipping Drive check.",
                len(local_tables),
            )
        else:
            progress.update("CSV exports", "Downloading", csv_gate.summary)
            LOGGER.info("3. Data analysis: downloading exported CSV files")
            download_drive_exports(drive_service, raw_settings, table_directory)
            audit, _state_path, _audit_path = _refresh_pipeline_audit(
                project_settings,
                expected_csvs,
                expected_rasters,
                table_directory,
                geotiff_tile_directory,
                geotiff_directory,
                idrisi_directory,
                progress,
            )
            assert_gate(required_csv_gate(audit))
            progress.update("CSV exports", "Ready", f"{len(list(table_directory.glob('*.csv')))} local CSV files; required products ready")
            _log_folder_summary("CSV exports downloaded", table_directory, ("*.csv",))

    if args.download_rasters:
        if drive_service is None:
            raise RuntimeError("Google Drive service was not initialized.")
        progress.update("Download raster", "Downloading", "GeoTIFF raster exports")
        LOGGER.info("3. Data analysis: downloading aligned GeoTIFF raster exports")
        download_drive_raster_exports(
            drive_service,
            raw_settings,
            geotiff_tile_directory,
            lambda status, detail: progress.update("Download raster", status, detail),
        )
        progress.update("GeoTIFF mosaics", "Generating", "Building missing or stale mosaics")
        mosaics = build_geotiff_mosaics(geotiff_tile_directory, geotiff_directory)
        progress.update("IDRISI conversion", "Converting", f"{len(mosaics)} mosaic(s)")
        converted = convert_geotiffs_to_idrisi(geotiff_directory, idrisi_directory)
        for sidecar_path in geotiff_directory.glob("*.aux.xml"):
            sidecar_path.unlink(missing_ok=True)
        validate_common_grid(geotiff_directory)
        audit, _state_path, _audit_path = _refresh_pipeline_audit(
            project_settings,
            expected_csvs,
            expected_rasters,
            table_directory,
            geotiff_tile_directory,
            geotiff_directory,
            idrisi_directory,
            progress,
        )
        assert_gate(required_raster_gate(audit))
        progress.update("GeoTIFF mosaics", "Ready", f"{len(mosaics)} mosaic(s)")
        progress.update("IDRISI conversion", "Ready", f"{len(converted)} raster(s)")
        _log_folder_summary("GeoTIFF mosaics validated", geotiff_directory, ("*.tif", "*.tiff"))
        LOGGER.info(
            "Prepared %d local GeoTIFF mosaics in %s and converted %d rasters to IDRISI.",
            len(mosaics),
            geotiff_directory,
            len(converted),
        )

    if args.sync_rasters:
        if drive_service is None:
            raise RuntimeError("Google Drive service was not initialized.")
        local_tiles = _local_geotiffs(geotiff_tile_directory)
        local_mosaics = _local_geotiffs(geotiff_directory)
        missing_products = _missing_expected_rasters(local_mosaics, project_settings.scenarios)
        progress.update("Sync rasters", "Checking", f"{len(local_tiles)} tile(s), {len(local_mosaics)} mosaic(s)")
        if local_tiles and local_mosaics and not missing_products:
            progress.update("Sync rasters", "Ready", "Local cache is complete")
            LOGGER.info(
                "Sync rasters: %d tile(s) and %d mosaic(s) already present locally. Skipping Drive check and mosaic rebuild.",
                len(local_tiles),
                len(local_mosaics),
            )
            converted = convert_geotiffs_to_idrisi(geotiff_directory, idrisi_directory)
            progress.update("IDRISI conversion", "Ready", f"{len(converted)} raster(s) converted during this run")
            if converted:
                LOGGER.info("Converted %d GeoTIFF(s) to IDRISI format in %s.", len(converted), idrisi_directory)
            audit, _state_path, _audit_path = _refresh_pipeline_audit(
                project_settings,
                expected_csvs,
                expected_rasters,
                table_directory,
                geotiff_tile_directory,
                geotiff_directory,
                idrisi_directory,
                progress,
            )
        else:
            if missing_products:
                LOGGER.info(
                    "Sync rasters: %d expected raster product(s) missing locally (%s). Checking Drive for updates.",
                    len(missing_products),
                    "; ".join(missing_products),
                )
            ensure_drive_raster_folder(drive_service, raw_settings)
            progress.update("Sync rasters", "Drive", "Checking raster exports")
            LOGGER.info("Sync rasters: checking Drive for existing raster exports.")
            download_drive_raster_exports(
                drive_service,
                raw_settings,
                geotiff_tile_directory,
                lambda status, detail: progress.update("Download raster", status, detail),
            )
            local_tiles = _local_geotiffs(geotiff_tile_directory)
            if local_tiles:
                progress.update("GeoTIFF mosaics", "Generating", f"{len(local_tiles)} tile(s)")
                LOGGER.info("Sync rasters: %d tile(s) found. Building missing or stale mosaics.", len(local_tiles))
                mosaics = build_geotiff_mosaics(geotiff_tile_directory, geotiff_directory)
                progress.update("IDRISI conversion", "Converting", f"{len(mosaics)} mosaic(s)")
                converted = convert_geotiffs_to_idrisi(geotiff_directory, idrisi_directory)
                for sidecar_path in geotiff_directory.glob("*.aux.xml"):
                    sidecar_path.unlink(missing_ok=True)
                validate_common_grid(geotiff_directory)
                audit, _state_path, _audit_path = _refresh_pipeline_audit(
                    project_settings,
                    expected_csvs,
                    expected_rasters,
                    table_directory,
                    geotiff_tile_directory,
                    geotiff_directory,
                    idrisi_directory,
                    progress,
                )
                progress.update("GeoTIFF mosaics", "Ready", f"{len(mosaics)} mosaic(s)")
                progress.update("IDRISI conversion", "Ready", f"{len(converted)} raster(s)")
                _log_folder_summary("GeoTIFF mosaics validated", geotiff_directory, ("*.tif", "*.tiff"))
                LOGGER.info(
                    "Prepared %d local GeoTIFF mosaics in %s and converted %d rasters to IDRISI.",
                    len(mosaics),
                    geotiff_directory,
                    len(converted),
                )
                missing_products = _missing_expected_rasters(mosaics, project_settings.scenarios)
            if not local_tiles or missing_products:
                if not local_tiles:
                    progress.update("Sync rasters", "No tiles", "Submitting raster exports")
                    LOGGER.info("Sync rasters: no tiles found in Drive. Submitting Earth Engine raster exports.")
                else:
                    progress.update("Sync rasters", "Missing", f"{len(missing_products)} product(s)")
                    LOGGER.info(
                        "Sync rasters: %d expected raster product(s) still missing after Drive sync (%s). "
                        "Submitting Earth Engine raster exports.",
                        len(missing_products),
                        "; ".join(missing_products),
                    )
                ensure_drive_raster_folder(drive_service, raw_settings)
                initialize_earth_engine(raw_settings["earth_engine"]["project"], credentials=credentials)
                progress.update("Earth Engine", "Initialized", "Preparing missing rasters")
                if prepared_inputs is None:
                    prepared_inputs = prepare_input_data(raw_settings)
                if organized_data is None:
                    organized_data = clean_and_organize_data(
                        settings=raw_settings,
                        scenarios=project_settings.scenarios,
                        mapbiomas_image=prepared_inputs.mapbiomas_image,
                        area_of_interest=prepared_inputs.area_of_interest,
                    )
                if products is None:
                    products = build_raster_products(raw_settings, project_settings.scenarios, prepared_inputs, organized_data)
                products_to_submit = [
                    product for product in products if not (geotiff_directory / f"{product.name}.tif").exists()
                ]
                if len(products_to_submit) < len(products):
                    LOGGER.info(
                        "Sync rasters: %d product(s) already have a local GeoTIFF mosaic; not calling Earth Engine for those.",
                        len(products) - len(products_to_submit),
                    )
                tasks = submit_raster_exports(
                    settings=raw_settings,
                    products=products_to_submit,
                    area_of_interest=prepared_inputs.area_of_interest,
                )
                progress.update("Earth Engine", "Queued", f"{len(tasks)} raster export task(s)")
                LOGGER.info(
                    "Submitted %d raster export tasks. Re-run once Earth Engine completes to download and convert to IDRISI.",
                    len(tasks),
                )

        initialize_earth_engine(raw_settings["earth_engine"]["project"], credentials=credentials)
        progress.update("Earth Engine", "Initialized", "Updating the raster status table")
        if prepared_inputs is None:
            prepared_inputs = prepare_input_data(raw_settings)
        if organized_data is None:
            organized_data = clean_and_organize_data(
                settings=raw_settings,
                scenarios=project_settings.scenarios,
                mapbiomas_image=prepared_inputs.mapbiomas_image,
                area_of_interest=prepared_inputs.area_of_interest,
            )
        if products is None:
            products = build_raster_products(raw_settings, project_settings.scenarios, prepared_inputs, organized_data)
        status_table = build_raster_status_table(products, geotiff_directory, idrisi_directory, raster_root)
        print_raster_status_table(status_table)
        progress.update("Sync rasters", "Status", f"{len(status_table)} product(s) in the raster status table")

    if args.analyze:
        progress.update("Local analysis", "Running", "Computing final tables")
        LOGGER.info("3. Data analysis: computing final tables")
        assert_gate(required_csv_gate(audit))
        if _local_geotiffs(geotiff_directory):
            try:
                validate_common_grid(geotiff_directory)
            except RuntimeError as exc:
                LOGGER.warning("Grid validation: %s", exc)
            converted = convert_geotiffs_to_idrisi(geotiff_directory, idrisi_directory)
            audit, _state_path, _audit_path = _refresh_pipeline_audit(
                project_settings,
                expected_csvs,
                expected_rasters,
                table_directory,
                geotiff_tile_directory,
                geotiff_directory,
                idrisi_directory,
                progress,
            )
            progress.update("IDRISI conversion", "Ready", f"{len(converted)} raster(s) converted during this run")
            if converted:
                LOGGER.info("Converted %d GeoTIFF(s) to IDRISI format in %s.", len(converted), idrisi_directory)
        results = analyze_exported_tables(
            table_directory=table_directory,
            scenario_labels=scenario_labels,
            references=references,
            pixel_area_hectares=pixel_area_hectares,
            settings=raw_settings,
        )
        _log_folder_summary("CSV tables available", table_directory, ("*.csv",))
        LOGGER.info("4. Results and output generation")
        progress.update("Reports", "Generating", "Results, figures, and tables")
        written: WorkflowOutputs = write_results(results, project_settings)
        written["mapbiomas_verra_validation"] = verra_validation_path
        workbook_path = publish_table_workbook(
            table_directory=table_directory,
            output_path=project_settings.output_directories["reports"] / "all_tables.xlsx",
        )
        written["excel_tables"] = workbook_path
        if sheets_service is not None:
            spreadsheet_id = publish_google_sheets_tables(
                sheets_service=sheets_service,
                settings_path=project_settings.path,
                settings=raw_settings,
                table_directory=table_directory,
            )
            LOGGER.info("Google Sheets tables: https://docs.google.com/spreadsheets/d/%s/edit", spreadsheet_id)
        generated_figures = generate_report_figures(results, project_settings.output_directories["figures"])
        if credentials is not None:
            if prepared_inputs is None or organized_data is None:
                try:
                    initialize_earth_engine(raw_settings["earth_engine"]["project"], credentials=credentials)
                    prepared_inputs = prepare_input_data(raw_settings)
                    organized_data = clean_and_organize_data(
                        settings=raw_settings,
                        scenarios=project_settings.scenarios,
                        mapbiomas_image=prepared_inputs.mapbiomas_image,
                        area_of_interest=prepared_inputs.area_of_interest,
                    )
                except Exception:
                    LOGGER.exception("Could not reinitialize Earth Engine data for report maps.")
            figures_attempted = 0
            figures_failed = 0
            if prepared_inputs is not None and organized_data is not None:
                try:
                    ee_figures = generate_earth_engine_report_maps(
                        settings=raw_settings,
                        scenarios=project_settings.scenarios,
                        prepared=prepared_inputs,
                        organized=organized_data,
                        figure_directory=project_settings.output_directories["figures"],
                        change_agreement_tables=results.change_agreement_tables,
                        pixel_area_hectares=pixel_area_hectares,
                    )
                    generated_figures.extend(ee_figures)
                    figures_attempted += len(ee_figures)
                except Exception:
                    LOGGER.exception("Could not generate Earth Engine report maps.")
                    figures_failed += 1
                try:
                    change_figure = generate_change_area_figure(
                        table_directory=table_directory,
                        figure_directory=project_settings.output_directories["figures"],
                    )
                    figures_attempted += 1
                    if change_figure is not None:
                        generated_figures.append(change_figure)
                except Exception:
                    LOGGER.exception("Could not generate change area figure.")
                    figures_failed += 1
            LOGGER.info("Earth Engine map generation: %d attempted, %d failed.", figures_attempted, figures_failed)
        LOGGER.info("Generated %d report figures and maps.", len(generated_figures))
        presentation_path = build_powerpoint_presentation(
            table_directory=project_settings.output_directories["tables"],
            figure_directory=project_settings.output_directories["figures"],
            output_path=project_settings.output_directories["reports"] / "presentation.pptx",
        )
        written["powerpoint_presentation"] = presentation_path
        technical_report_path = (
            written.get("technical_report")
            or project_settings.output_directories["reports"] / "technical_report.md"
        )
        if args.skip_google_report:
            progress.update("Local analysis", "Ready", "Analysis completed")
            progress.update("Reports", "Ready", str(technical_report_path))
            LOGGER.info("Skipped Google Docs report update. Local report: %s", technical_report_path)
            LOGGER.info("Wrote %d output file groups.", len(written))
            LOGGER.info("Workflow complete.")
            return
        if docs_service is None or drive_service is None:
            raise RuntimeError("Google services were not initialized.")
        document_id, word_path = publish_google_doc_report(
            docs_service=docs_service,
            drive_service=drive_service,
            settings_path=project_settings.path,
            settings=raw_settings,
            report_text=technical_report_path.read_text(encoding="utf-8"),
            table_directory=project_settings.output_directories["tables"],
            figure_directory=project_settings.output_directories["figures"],
            word_output_path=project_settings.output_directories["reports"] / "technical_report.docx",
        )
        written["word_report"] = word_path
        presentation_id = publish_powerpoint_as_google_slides(
            drive_service=drive_service,
            settings_path=project_settings.path,
            settings=raw_settings,
            presentation_path=presentation_path,
        )
        LOGGER.info("Google Docs report: https://docs.google.com/document/d/%s/edit", document_id)
        LOGGER.info("Google Slides presentation: https://docs.google.com/presentation/d/%s/edit", presentation_id)
        LOGGER.info("Wrote %d output file groups.", len(written))
        progress.update("Local analysis", "Ready", "Analysis completed")
        progress.update("Reports", "Ready", "Docs, Word, Excel, and Slides updated")

    LOGGER.info("Workflow complete.")


def _missing_expected_rasters(local_mosaics: list[Path], scenarios: list) -> list[str]:
    """Return labels of mandatory raster products absent from local GeoTIFF mosaics.

    build_raster_products() adds many derived products, but only the full-class
    MapBiomas land-cover map per PRIMARY_MAPBIOMAS_YEARS and one persistence
    raster per configured scenario are checked here: those are the products a
    stale or partial local cache is most likely to be missing silently.
    """
    stems = {path.stem for path in local_mosaics}
    missing: list[str] = []
    for year in PRIMARY_MAPBIOMAS_YEARS:
        if not any("LULC" in stem and str(year) in stem for stem in stems):
            missing.append(f"MapBiomas LULC {year} (30 m, all classes)")
    for scenario in scenarios:
        marker = f"Persistence_Scen{scenario.identifier}"
        if not any(marker in stem for stem in stems):
            missing.append(f"Persistence scenario {scenario.label}")
    return missing


def _refresh_pipeline_audit(
    project_settings,
    expected_csvs,
    expected_rasters,
    table_directory: Path,
    geotiff_tile_directory: Path,
    geotiff_directory: Path,
    idrisi_directory: Path,
    progress: WorkflowProgress | None = None,
):
    audit = audit_pipeline_state(
        expected_csvs=expected_csvs,
        expected_rasters=expected_rasters,
        table_directory=table_directory,
        geotiff_tile_directory=geotiff_tile_directory,
        geotiff_directory=geotiff_directory,
        idrisi_directory=idrisi_directory,
    )
    state_path = write_pipeline_state(audit, project_settings.output_root / "pipeline_state.json")
    audit_path = write_pipeline_audit_markdown(audit, project_settings.output_directories["logs"] / "pipeline_audit.md")
    if progress is not None:
        progress.update_audit(audit, state_path, audit_path)
    return audit, state_path, audit_path


def _local_geotiffs(directory: Path) -> list[Path]:
    if not directory.exists():
        return []
    return sorted(
        path
        for path in directory.iterdir()
        if path.is_file() and path.suffix.lower() in {".tif", ".tiff"}
    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        LOGGER.warning("Workflow interrupted by user. Partial raster downloads were removed; rerun to resume.")
        raise SystemExit(130) from None
    except RuntimeError as exc:
        LOGGER.error("%s", exc)
        raise SystemExit(1) from None
