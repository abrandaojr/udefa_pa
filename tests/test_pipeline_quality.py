from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import from_origin

from src.mapbiomas_ctrees.catalog import expected_csv_exports, expected_raster_products
from src.mapbiomas_ctrees.data_quality import required_csv_gate, required_raster_gate
from src.mapbiomas_ctrees.google_services import _local_download_is_current
from src.mapbiomas_ctrees.local_tables import _first_match
from src.mapbiomas_ctrees.pipeline_state import audit_pipeline_state
from src.mapbiomas_ctrees.raster_exports import convert_geotiffs_to_idrisi
from src.mapbiomas_ctrees.settings import Scenario


class PipelineQualityTests(unittest.TestCase):
    def test_catalog_contains_required_csv_and_raster_products(self) -> None:
        scenarios = [Scenario("A", 100, 1985, 2024)]
        settings = {"grid": {"crs": "EPSG:5880", "scale_m": 30}, "earth_engine": {"scale_native_m": 30}}

        csvs = expected_csv_exports(scenarios, ["DMJSS"])
        rasters = expected_raster_products(settings, scenarios)

        self.assertIn("Area_30m_A_100pct_1985-2024", {item.name for item in csvs})
        self.assertIn("XTab_30m_A_100pct_1985-2024_x_DMJSS", {item.name for item in csvs})
        self.assertIn("UDefA_MB_LULC_T0_1985_EPSG_5880_30m", {item.name for item in rasters})
        self.assertIn("UDefA_MB_Persistence_ScenA_100pct_1985-2024_EPSG_5880_30m", {item.name for item in rasters})

    def test_audit_marks_required_csv_missing(self) -> None:
        scenarios = [Scenario("A", 100, 1985, 2024)]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            audit = audit_pipeline_state(
                expected_csvs=expected_csv_exports(scenarios, ["DMJSS"]),
                expected_rasters=[],
                table_directory=root / "tables",
                geotiff_tile_directory=root / "tiles",
                geotiff_directory=root / "geotiff",
                idrisi_directory=root / "idrisi",
            )

        self.assertGreaterEqual(audit.summary.get("required_not_ready", 0), 1)

    def test_required_csv_gate_rejects_ambiguous_csv(self) -> None:
        scenarios = [Scenario("A", 100, 1985, 2024)]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tables = root / "tables"
            tables.mkdir()
            (tables / "Area_30m_A_100pct_1985-2024.csv").write_text("groups\n", encoding="utf-8")
            (tables / "Area_30m_A_100pct_1985-2024_copy.csv").write_text("groups\n", encoding="utf-8")
            (tables / "XTab_30m_A_100pct_1985-2024_x_DMJSS.csv").write_text("histogram\n", encoding="utf-8")
            audit = audit_pipeline_state(
                expected_csvs=expected_csv_exports(scenarios, ["DMJSS"]),
                expected_rasters=[],
                table_directory=tables,
                geotiff_tile_directory=root / "tiles",
                geotiff_directory=root / "geotiff",
                idrisi_directory=root / "idrisi",
            )

        self.assertFalse(required_csv_gate(audit).ok)

    def test_required_csv_gate_rejects_structurally_invalid_csv(self) -> None:
        scenarios = [Scenario("A", 100, 1985, 2024)]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tables = root / "tables"
            tables.mkdir()
            (tables / "Area_30m_A_100pct_1985-2024.csv").write_text("", encoding="utf-8")
            (tables / "XTab_30m_A_100pct_1985-2024_x_DMJSS.csv").write_text("histogram\n", encoding="utf-8")
            audit = audit_pipeline_state(
                expected_csvs=expected_csv_exports(scenarios, ["DMJSS"]),
                expected_rasters=[],
                table_directory=tables,
                geotiff_tile_directory=root / "tiles",
                geotiff_directory=root / "geotiff",
                idrisi_directory=root / "idrisi",
            )

        self.assertFalse(required_csv_gate(audit).ok)

    def test_required_raster_gate_rejects_partial_required_raster(self) -> None:
        scenarios = [Scenario("A", 100, 1985, 2024)]
        settings = {"grid": {"crs": "EPSG:5880", "scale_m": 30}, "earth_engine": {"scale_native_m": 30}}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tiles = root / "tiles"
            tiles.mkdir()
            (tiles / "UDefA_MB_LULC_T0_1985_EPSG_5880_30m.tif").write_bytes(b"tile")
            audit = audit_pipeline_state(
                expected_csvs=[],
                expected_rasters=expected_raster_products(settings, scenarios),
                table_directory=root / "tables",
                geotiff_tile_directory=tiles,
                geotiff_directory=root / "geotiff",
                idrisi_directory=root / "idrisi",
            )

        self.assertFalse(required_raster_gate(audit).ok)

    def test_observed_rasters_are_aggregated_across_local_stages(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            geotiff = root / "geotiff"
            idrisi = root / "idrisi"
            geotiff.mkdir()
            idrisi.mkdir()
            (geotiff / "Observed_Product.tif").write_bytes(b"tif")
            (idrisi / "Observed_Product.rst").write_bytes(b"rst")
            (idrisi / "Observed_Product.rdc").write_bytes(b"rdc")
            audit = audit_pipeline_state(
                expected_csvs=[],
                expected_rasters=[],
                table_directory=root / "tables",
                geotiff_tile_directory=root / "tiles",
                geotiff_directory=geotiff,
                idrisi_directory=idrisi,
            )

        observed = [
            product
            for product in audit.products.values()
            if product.name == "Observed_Product"
        ]
        self.assertEqual(len(observed), 1)
        self.assertEqual(set(observed[0].stages), {"mosaic", "idrisi_rst", "idrisi_rdc"})

    def test_invalid_observed_csv_is_reported_as_issue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tables = root / "tables"
            tables.mkdir()
            (tables / "derived_empty.csv").write_text("", encoding="utf-8")
            audit = audit_pipeline_state(
                expected_csvs=[],
                expected_rasters=[],
                table_directory=tables,
                geotiff_tile_directory=root / "tiles",
                geotiff_directory=root / "geotiff",
                idrisi_directory=root / "idrisi",
            )

        self.assertTrue(any("Artefato invalido: derived_empty" in issue for issue in audit.issues))

    def test_first_match_rejects_ambiguous_required_csv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Area_30m_A.csv").write_text("groups\n", encoding="utf-8")
            (root / "Area_30m_A_copy.csv").write_text("groups\n", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                _first_match(root, "Area_30m_A*.csv")

    def test_idrisi_conversion_is_incremental(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            geotiff_dir = root / "geotiff"
            idrisi_dir = root / "idrisi"
            geotiff_dir.mkdir()
            path = geotiff_dir / "Tiny_Test.tif"
            data = np.array([[1, 2], [3, 4]], dtype=np.int16)
            with rasterio.open(
                path,
                "w",
                driver="GTiff",
                height=2,
                width=2,
                count=1,
                dtype="int16",
                crs="EPSG:5880",
                transform=from_origin(0, 60, 30, 30),
                nodata=-9999,
            ) as dataset:
                dataset.write(data, 1)

            first = convert_geotiffs_to_idrisi(geotiff_dir, idrisi_dir)
            self.assertEqual(len(first), 1)
            rst = idrisi_dir / "Tiny_Test.rst"
            self.assertEqual(rst.stat().st_size, 2 * 2 * np.dtype(np.int16).itemsize)
            first_mtime = rst.stat().st_mtime
            time.sleep(0.01)
            second = convert_geotiffs_to_idrisi(geotiff_dir, idrisi_dir)
            self.assertEqual(second, [])
            self.assertEqual(rst.stat().st_mtime, first_mtime)

    def test_download_manifest_backfill_rejects_local_file_older_than_drive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "Raster.tif"
            data = np.array([[1]], dtype=np.int16)
            with rasterio.open(
                path,
                "w",
                driver="GTiff",
                height=1,
                width=1,
                count=1,
                dtype="int16",
                crs="EPSG:5880",
                transform=from_origin(0, 30, 30, 30),
                nodata=-9999,
            ) as dataset:
                dataset.write(data, 1)
            old_time = time.time() - 3600
            path.touch()
            import os

            os.utime(path, (old_time, old_time))
            drive_item = {
                "id": "drive-id",
                "name": path.name,
                "size": str(path.stat().st_size + 100),
                "modifiedTime": "2999-01-01T00:00:00.000Z",
            }

            self.assertFalse(_local_download_is_current(path, drive_item))


if __name__ == "__main__":
    unittest.main()
