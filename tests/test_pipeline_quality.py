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
from src.mapbiomas_ctrees.google_services import _local_download_is_current, _raster_export_name_matches_target_grid
from src.mapbiomas_ctrees.local_tables import _first_match
from src.mapbiomas_ctrees.pipeline_state import audit_pipeline_state
from src.mapbiomas_ctrees.raster_exports import (
    RasterProduct,
    _normalize_raster_products,
    build_geotiff_mosaics,
    convert_geotiffs_to_idrisi,
)
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

    def test_raster_product_names_are_canonicalized_once(self) -> None:
        settings = {"grid": {"crs": "EPSG:5880", "scale_m": 30}, "earth_engine": {"scale_native_m": 30}}
        products = [
            RasterProduct("Tiny_Test", None, "base"),  # type: ignore[arg-type]
            RasterProduct("Tiny_Test_EPSG_5880_30m", None, "already canonical"),  # type: ignore[arg-type]
        ]

        normalized = _normalize_raster_products(products, settings)

        self.assertEqual([product.name for product in normalized], ["Tiny_Test_EPSG_5880_30m"])

    def test_drive_raster_download_filter_requires_target_suffix(self) -> None:
        settings = {"grid": {"crs": "EPSG:5880", "scale_m": 30}, "earth_engine": {"scale_native_m": 30}}

        self.assertTrue(
            _raster_export_name_matches_target_grid(
                "Tiny_Test_EPSG_5880_30m-0000000000-0000000000.tif",
                settings,
            )
        )
        self.assertFalse(
            _raster_export_name_matches_target_grid(
                "Tiny_Test_30m-0000000000-0000000000.tif",
                settings,
            )
        )

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

        self.assertTrue(any("Invalid artifact: derived_empty" in issue for issue in audit.issues))

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

    def test_geotiff_mosaic_build_is_incremental(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tile_dir = root / "tiles"
            mosaic_dir = root / "geotiff"
            tile_dir.mkdir()
            path = tile_dir / "Tiny_Test-00000-00000.tif"
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

            first = build_geotiff_mosaics(tile_dir, mosaic_dir)
            self.assertEqual([path.name for path in first], ["Tiny_Test.tif"])
            mosaic = mosaic_dir / "Tiny_Test.tif"
            first_mtime = mosaic.stat().st_mtime
            time.sleep(0.01)
            second = build_geotiff_mosaics(tile_dir, mosaic_dir)
            self.assertEqual([path.name for path in second], ["Tiny_Test.tif"])
            self.assertEqual(mosaic.stat().st_mtime, first_mtime)

    def test_geotiff_mosaics_only_use_epsg_5880_30m_tiles(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tile_dir = root / "tiles"
            mosaic_dir = root / "geotiff"
            tile_dir.mkdir()
            data = np.array([[1]], dtype=np.int16)
            valid_path = tile_dir / "Tiny_Test_EPSG_5880_30m-00000-00000.tif"
            invalid_path = tile_dir / "Tiny_Test_EPSG_4326_30m-00000-00000.tif"
            for path, crs, transform in (
                (valid_path, "EPSG:5880", from_origin(0, 30, 30, 30)),
                (invalid_path, "EPSG:4326", from_origin(-52, -1, 0.0002695, 0.0002695)),
            ):
                with rasterio.open(
                    path,
                    "w",
                    driver="GTiff",
                    height=1,
                    width=1,
                    count=1,
                    dtype="int16",
                    crs=crs,
                    transform=transform,
                    nodata=-9999,
                ) as dataset:
                    dataset.write(data, 1)

            mosaics = build_geotiff_mosaics(tile_dir, mosaic_dir)
            self.assertEqual([path.name for path in mosaics], ["Tiny_Test_EPSG_5880_30m.tif"])
            self.assertFalse((mosaic_dir / "Tiny_Test_EPSG_4326_30m.tif").exists())

    def test_idrisi_conversion_only_uses_epsg_5880_30m_geotiffs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            geotiff_dir = root / "geotiff"
            idrisi_dir = root / "idrisi"
            geotiff_dir.mkdir()
            idrisi_dir.mkdir()
            path = geotiff_dir / "Old_EPSG_4326_30m.tif"
            data = np.array([[1]], dtype=np.int16)
            with rasterio.open(
                path,
                "w",
                driver="GTiff",
                height=1,
                width=1,
                count=1,
                dtype="int16",
                crs="EPSG:4326",
                transform=from_origin(-52, -1, 0.0002695, 0.0002695),
                nodata=-9999,
            ) as dataset:
                dataset.write(data, 1)
            (idrisi_dir / "Old_EPSG_4326_30m.rst").write_bytes(b"stale")
            (idrisi_dir / "Old_EPSG_4326_30m.rdc").write_text("stale\n", encoding="ascii")

            converted = convert_geotiffs_to_idrisi(geotiff_dir, idrisi_dir)
            self.assertEqual(converted, [])
            self.assertFalse(path.exists())
            self.assertFalse((idrisi_dir / "Old_EPSG_4326_30m.rst").exists())
            self.assertFalse((idrisi_dir / "Old_EPSG_4326_30m.rdc").exists())

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
