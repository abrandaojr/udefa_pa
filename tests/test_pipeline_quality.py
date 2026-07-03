from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import from_origin

from main import (
    _missing_expected_rasters,
    _products_missing_local_mosaics,
    _products_missing_local_or_drive_mosaics,
    _required_raster_semantic_keys,
)
from src.mapbiomas_ctrees.catalog import expected_csv_exports, expected_raster_products
from src.mapbiomas_ctrees.data_quality import required_csv_gate, required_raster_gate
from src.mapbiomas_ctrees.google_services import (
    _local_download_is_current,
    _preferred_drive_raster_stems,
    _raster_export_name_matches_target_grid,
)
from src.mapbiomas_ctrees.local_tables import _first_match
from src.mapbiomas_ctrees.pipeline_state import audit_pipeline_state
from src.mapbiomas_ctrees.raster_naming import raster_semantic_key
from src.mapbiomas_ctrees.raster_exports import (
    _IDRISI_LEGENDS,
    _IDRISI_NODATA,
    RasterProduct,
    _idrisi_pal_text,
    _idrisi_smp_bytes,
    _idrisi_title,
    _idrisi_product_type,
    _normalize_raster_products,
    _study_area_boundary,
    _study_area_mask_geotiff,
    _study_area_mask_idrisi,
    build_geotiff_mosaics,
    build_raster_status_table,
    convert_geotiffs_to_idrisi,
    generate_idrisi_raster_panel,
    prune_duplicate_idrisi_products,
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
        self.assertIn("UDefA_ParaStateMask_EPSG_5880_30m", {item.name for item in rasters})

    def test_catalog_does_not_expect_ctrees_agreement_without_ctrees_2024(self) -> None:
        scenarios = [Scenario("A", 100, 1985, 2024)]
        settings = {"grid": {"crs": "EPSG:5880", "scale_m": 30}, "earth_engine": {"scale_native_m": 30}}

        raster_names = {item.name for item in expected_raster_products(settings, scenarios)}

        self.assertIn("UDefA_MB_ForestChange4_T3T4_2018_2024_EPSG_5880_30m", raster_names)
        self.assertNotIn("UDefA_Ct_ForestChange4_T3T4_2018_2024_EPSG_5880_30m", raster_names)
        self.assertNotIn("UDefA_Ct_MB_Agreement_T3T4_2018_2024_EPSG_5880_30m", raster_names)

    def test_raster_product_names_are_canonicalized_once(self) -> None:
        settings = {"grid": {"crs": "EPSG:5880", "scale_m": 30}, "earth_engine": {"scale_native_m": 30}}
        products = [
            RasterProduct("Tiny_Test", None, "base"),  # type: ignore[arg-type]
            RasterProduct("Tiny_Test_EPSG_5880_30m", None, "already canonical"),  # type: ignore[arg-type]
        ]

        normalized = _normalize_raster_products(products, settings)

        self.assertEqual([product.name for product in normalized], ["Tiny_Test_EPSG_5880_30m"])

    def test_raster_normalization_masks_products_to_ibge_state_area(self) -> None:
        class FakeImage:
            def __init__(self) -> None:
                self.masks: list[object] = []

            def updateMask(self, mask: object) -> "FakeImage":
                self.masks.append(mask)
                return self

        settings = {"grid": {"crs": "EPSG:5880", "scale_m": 30}, "earth_engine": {"scale_native_m": 30}}
        product_image = FakeImage()
        valid_image = FakeImage()
        state_image = FakeImage()
        valid_mask = object()
        state_mask = object()

        _normalize_raster_products(
            [
                RasterProduct("MapBiomas_ForestLoss_2009_2013", product_image, "loss"),  # type: ignore[arg-type]
                RasterProduct("UDefA_ValidMask", valid_image, "valid"),  # type: ignore[arg-type]
                RasterProduct("UDefA_ParaStateMask", state_image, "state"),  # type: ignore[arg-type]
            ],
            settings,
            valid_mask=valid_mask,  # type: ignore[arg-type]
            state_mask=state_mask,  # type: ignore[arg-type]
        )

        self.assertEqual(product_image.masks, [valid_mask, state_mask])
        self.assertEqual(valid_image.masks, [state_mask])
        self.assertEqual(state_image.masks, [state_mask])

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

    def test_required_raster_gate_accepts_equivalent_legacy_raster_name(self) -> None:
        scenarios = [Scenario("A", 100, 1985, 2024)]
        settings = {"grid": {"crs": "EPSG:5880", "scale_m": 30}, "earth_engine": {"scale_native_m": 30}}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tiles = root / "tiles"
            geotiff = root / "geotiff"
            idrisi = root / "idrisi"
            for directory in (tiles, geotiff, idrisi):
                directory.mkdir()
            legacy = "MapBiomas_Persistence_A_100pct_1985-2024_EPSG_5880_30m"
            (tiles / f"{legacy}.tif").write_bytes(b"tile")
            (geotiff / f"{legacy}.tif").write_bytes(b"mosaic")
            (idrisi / f"{legacy}.rst").write_bytes(b"rst")
            (idrisi / f"{legacy}.rdc").write_text("rdc\n", encoding="ascii")
            (idrisi / f"{legacy}.pal").write_text("pal\n", encoding="ascii")

            audit = audit_pipeline_state(
                expected_csvs=[],
                expected_rasters=[
                    spec
                    for spec in expected_raster_products(settings, scenarios)
                    if spec.name == "UDefA_MB_Persistence_ScenA_100pct_1985-2024_EPSG_5880_30m"
                ],
                table_directory=root / "tables",
                geotiff_tile_directory=tiles,
                geotiff_directory=geotiff,
                idrisi_directory=idrisi,
            )

        product = audit.products["UDefA_MB_Persistence_ScenA_100pct_1985-2024_EPSG_5880_30m"]
        self.assertEqual(product.status, "ready")
        self.assertTrue(required_raster_gate(audit).ok)

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
            self.assertTrue((idrisi_dir / "Tiny_Test.pal").exists())
            self.assertTrue((idrisi_dir / "Tiny_Test.smp").exists())
            first_mtime = rst.stat().st_mtime
            time.sleep(0.01)
            second = convert_geotiffs_to_idrisi(geotiff_dir, idrisi_dir)
            self.assertEqual(second, [])
            self.assertEqual(rst.stat().st_mtime, first_mtime)

    def test_idrisi_conversion_masks_pixels_outside_para_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            geotiff_dir = root / "geotiff"
            idrisi_dir = root / "idrisi"
            geotiff_dir.mkdir()
            transform = from_origin(0, 60, 30, 30)
            raster_data = np.array([[1, 1], [0, 0]], dtype=np.int16)
            state_mask = np.array([[1, 0], [1, 0]], dtype=np.int16)
            for name, data in {
                "Tiny_Test_EPSG_5880_30m": raster_data,
                "UDefA_ParaStateMask_EPSG_5880_30m": state_mask,
            }.items():
                with rasterio.open(
                    geotiff_dir / f"{name}.tif",
                    "w",
                    driver="GTiff",
                    height=2,
                    width=2,
                    count=1,
                    dtype="int16",
                    crs="EPSG:5880",
                    transform=transform,
                    nodata=_IDRISI_NODATA,
                ) as dataset:
                    dataset.write(data, 1)

            convert_geotiffs_to_idrisi(geotiff_dir, idrisi_dir)

            values = np.fromfile(idrisi_dir / "Tiny_Test_EPSG_5880_30m.rst", dtype=np.int16).reshape(2, 2)
            self.assertTrue(np.array_equal(values, np.array([[1, _IDRISI_NODATA], [0, _IDRISI_NODATA]], dtype=np.int16)))
            rdc = (idrisi_dir / "Tiny_Test_EPSG_5880_30m.rdc").read_text(encoding="ascii")
            self.assertIn("Outside Para state boundary set to missing data", rdc)

    def test_idrisi_conversion_prioritizes_para_state_mask_over_valid_mask(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            geotiff_dir = root / "geotiff"
            idrisi_dir = root / "idrisi"
            geotiff_dir.mkdir()
            idrisi_dir.mkdir()
            transform = from_origin(0, 60, 30, 30)
            for name, data in {
                "UDefA_ValidMask_EPSG_5880_30m": np.array([[0, 0], [0, 0]], dtype=np.int16),
                "UDefA_ParaStateMask_EPSG_5880_30m": np.array([[1, 0], [1, 1]], dtype=np.int16),
            }.items():
                with rasterio.open(
                    geotiff_dir / f"{name}.tif",
                    "w",
                    driver="GTiff",
                    height=2,
                    width=2,
                    count=1,
                    dtype="int16",
                    crs="EPSG:5880",
                    transform=transform,
                    nodata=_IDRISI_NODATA,
                ) as dataset:
                    dataset.write(data, 1)
                (idrisi_dir / f"{name}.rst").write_bytes(b"rst")
                (idrisi_dir / f"{name}.rdc").write_text("columns : 2\nrows    : 2\n", encoding="ascii")

            self.assertEqual(_study_area_mask_geotiff(geotiff_dir).name, "UDefA_ParaStateMask_EPSG_5880_30m.tif")
            self.assertEqual(_study_area_mask_idrisi(idrisi_dir).name, "UDefA_ParaStateMask_EPSG_5880_30m.rst")

    def test_idrisi_conversion_writes_mapbiomas_lulc_palette(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            geotiff_dir = root / "geotiff"
            idrisi_dir = root / "idrisi"
            geotiff_dir.mkdir()
            path = geotiff_dir / "UDefA_MB_LULC_T0_1985_EPSG_5880_30m.tif"
            data = np.array([[3, 15], [26, 75]], dtype=np.int16)
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

            convert_geotiffs_to_idrisi(geotiff_dir, idrisi_dir)

            pal_lines = (idrisi_dir / "UDefA_MB_LULC_T0_1985_EPSG_5880_30m.pal").read_text(
                encoding="ascii"
            ).splitlines()
            self.assertEqual(pal_lines[3], "31 141 73")
            self.assertEqual(pal_lines[15], "237 222 142")
            self.assertEqual(pal_lines[26], "37 50 228")
            rdc = (idrisi_dir / "UDefA_MB_LULC_T0_1985_EPSG_5880_30m.rdc").read_text(encoding="ascii")
            self.assertIn("legend cats : 38", rdc)
            self.assertIn("code 75     : Photovoltaic Power Plant", rdc)

    def test_idrisi_conversion_refreshes_stale_palette_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            geotiff_dir = root / "geotiff"
            idrisi_dir = root / "idrisi"
            geotiff_dir.mkdir()
            idrisi_dir.mkdir()
            stem = "CTrees_FCBM4_30m_EPSG_5880_30m"
            path = geotiff_dir / f"{stem}.tif"
            data = np.array([[0, 1], [3, 4]], dtype=np.int16)
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

            (idrisi_dir / f"{stem}.rst").write_bytes(b"\0" * (2 * 2 * np.dtype(np.int16).itemsize))
            (idrisi_dir / f"{stem}.rdc").write_text("legend cats : 2\ncode 0      : Non-Forest\n", encoding="ascii")
            (idrisi_dir / f"{stem}.pal").write_text(("127 127 127\n" * 256), encoding="ascii")
            import os

            future_time = time.time() + 60
            for output in idrisi_dir.iterdir():
                os.utime(output, (future_time, future_time))

            converted = convert_geotiffs_to_idrisi(geotiff_dir, idrisi_dir)

            self.assertEqual([path.name for path in converted], [f"{stem}.rst"])
            pal_lines = (idrisi_dir / f"{stem}.pal").read_text(encoding="ascii").splitlines()
            smp_bytes = (idrisi_dir / f"{stem}.smp").read_bytes()
            self.assertEqual(pal_lines[0], "0 0 0")
            self.assertEqual(pal_lines[4], "215 25 28")
            self.assertEqual(smp_bytes[18:21], bytes([0, 0, 0]))
            self.assertEqual(smp_bytes[30:33], bytes([215, 25, 28]))
            rdc = (idrisi_dir / f"{stem}.rdc").read_text(encoding="ascii")
            self.assertIn("legend cats : 5", rdc)
            self.assertIn("code 4      : Deforested - Second Half of HRP", rdc)

    def test_idrisi_conversion_creates_missing_palette_without_rewriting_rst(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            geotiff_dir = root / "geotiff"
            idrisi_dir = root / "idrisi"
            geotiff_dir.mkdir()
            stem = "CTrees_DMJSS_30m_EPSG_5880_30m"
            path = geotiff_dir / f"{stem}.tif"
            data = np.array([[0, 1], [2, 3]], dtype=np.int16)
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
            self.assertEqual([output.name for output in first], [f"{stem}.rst"])
            rst_path = idrisi_dir / f"{stem}.rst"
            pal_path = idrisi_dir / f"{stem}.pal"
            smp_path = idrisi_dir / f"{stem}.smp"
            rst_mtime = rst_path.stat().st_mtime
            pal_path.unlink()
            smp_path.unlink()
            time.sleep(0.01)

            second = convert_geotiffs_to_idrisi(geotiff_dir, idrisi_dir)

            self.assertEqual(second, [])
            self.assertTrue(pal_path.exists())
            self.assertTrue(smp_path.exists())
            self.assertEqual(rst_path.stat().st_mtime, rst_mtime)
            self.assertEqual(pal_path.read_text(encoding="ascii").splitlines()[2], "227 26 28")
            self.assertEqual(smp_path.read_bytes()[24:27], bytes([227, 26, 28]))

    def test_raster_status_requires_idrisi_palette_when_legend_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            geotiff_dir = root / "geotiff"
            idrisi_dir = root / "idrisi"
            geotiff_dir.mkdir()
            idrisi_dir.mkdir()
            name = "CTrees_DMJSS_30m_EPSG_5880_30m"
            (geotiff_dir / f"{name}.tif").write_bytes(b"tif")
            (idrisi_dir / f"{name}.rst").write_bytes(b"rst")
            (idrisi_dir / f"{name}.rdc").write_text("rdc\n", encoding="ascii")
            product = RasterProduct(name, None, "dmjss")  # type: ignore[arg-type]

            missing_palette = build_raster_status_table([product], geotiff_dir, idrisi_dir, root)
            self.assertEqual(missing_palette.loc[0, "status"], "GeoTIFF downloaded")
            self.assertEqual(missing_palette.loc[0, "detail"], "IDRISI conversion pending")

            (idrisi_dir / f"{name}.pal").write_text(_idrisi_pal_text(_IDRISI_LEGENDS["dmjss"]), encoding="ascii")
            (idrisi_dir / f"{name}.smp").write_bytes(_idrisi_smp_bytes(_IDRISI_LEGENDS["dmjss"]))
            ready = build_raster_status_table([product], geotiff_dir, idrisi_dir, root)
            self.assertEqual(ready.loc[0, "status"], "IDRISI ready")

    def test_generate_idrisi_raster_panel_writes_png(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            geotiff_dir = root / "geotiff"
            idrisi_dir = root / "idrisi"
            geotiff_dir.mkdir()
            for name, data in {
                "CTrees_FCBM1_2009_30m_EPSG_5880_30m": np.array([[0, 1], [1, 0]], dtype=np.int16),
                "MapBiomas_Change_2009_2013_EPSG_5880_30m": np.array([[1, 2], [3, 4]], dtype=np.int16),
            }.items():
                with rasterio.open(
                    geotiff_dir / f"{name}.tif",
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
            convert_geotiffs_to_idrisi(geotiff_dir, idrisi_dir)

            panel = generate_idrisi_raster_panel(idrisi_dir, columns=2, thumbnail_size=(80, 60), panel_size=(1600, 900))

            self.assertEqual(panel, idrisi_dir / "idrisi_maps_panel.png")
            self.assertTrue(panel.exists())
            from PIL import Image

            with Image.open(panel) as image:
                self.assertEqual(image.size, (1600, 900))
                self.assertAlmostEqual(image.width / image.height, 16 / 9, places=3)

    def test_idrisi_product_type_groups_equivalent_raster_names(self) -> None:
        examples = {
            "UDefA_MB_LULC_T0_1985_EPSG_5880_30m": "lulc",
            "MapBiomas_LandCover_1985_EPSG_5880_30m": "lulc",
            "MapBiomas_LULC_1985_EPSG_5880_30m": "lulc",
            "UDefA_MB_Forest_T0_1985_EPSG_5880_30m": "binary_forest",
            "MapBiomas_ForestNonForest_1985_EPSG_5880_30m": "binary_forest",
            "CTrees_DMJSS_30m_EPSG_5880_30m": "dmjss",
            "UDefA_Ct_DMJSS_2009_2018_EPSG_5880_30m": "dmjss",
            "UDefA_Ct_FCBM4_EPSG_5880_30m": "fcbm4",
            "CTrees_FCBM4_EPSG_5880_30m": "fcbm4",
            "UDefA_MB_DistFromNF_T1_2009_EPSG_5880_30m": "distance",
            "UDefA_CTrees_NonForest_T1_2009_30m_EPSG_5880_30m": "binary_nonforest",
        }
        for stem, expected_type in examples.items():
            with self.subTest(stem=stem):
                self.assertEqual(_idrisi_product_type(stem), expected_type)

    def test_idrisi_dmjss_palette_uses_semantic_colors(self) -> None:
        palette_lines = _idrisi_pal_text(_IDRISI_LEGENDS["dmjss"]).splitlines()

        self.assertEqual(palette_lines[:5], ["217 201 143", "0 109 44", "227 26 28", "65 182 196", "255 255 153"])
        self.assertEqual(_idrisi_product_type("CTrees_DMJSS_30m_EPSG_5880_30m"), "dmjss")

    def test_idrisi_binary_forest_palette_uses_semantic_colors(self) -> None:
        palette_lines = _idrisi_pal_text(_IDRISI_LEGENDS["binary_forest"]).splitlines()

        self.assertEqual(palette_lines[0], "199 179 127")
        self.assertEqual(palette_lines[1], "35 139 69")
        self.assertEqual(_idrisi_product_type("CTrees_FCBM1_2009_30m_EPSG_5880_30m"), "binary_forest")

    def test_idrisi_change4_palette_uses_semantic_colors(self) -> None:
        palette_lines = _idrisi_pal_text(_IDRISI_LEGENDS["change4"]).splitlines()

        self.assertEqual(palette_lines[:5], ["0 0 0", "0 109 44", "217 201 143", "227 26 28", "44 127 184"])
        self.assertEqual(_IDRISI_LEGENDS["change4"][0], (0, "No Data", "#000000"))

    def test_idrisi_forest_loss_palette_uses_black_background_and_vivid_red_loss(self) -> None:
        palette_lines = _idrisi_pal_text(_IDRISI_LEGENDS["forest_loss"]).splitlines()

        self.assertEqual(palette_lines[0], "0 0 0")
        self.assertEqual(palette_lines[1], "255 0 0")

    def test_study_area_boundary_marks_inside_edge(self) -> None:
        inside = np.array(
            [
                [0, 0, 0, 0, 0],
                [0, 1, 1, 1, 0],
                [0, 1, 1, 1, 0],
                [0, 1, 1, 1, 0],
                [0, 0, 0, 0, 0],
            ],
            dtype=bool,
        )

        boundary = _study_area_boundary(inside)

        self.assertTrue(boundary[1, 1])
        self.assertTrue(boundary[1, 2])
        self.assertTrue(boundary[3, 3])
        self.assertFalse(boundary[2, 2])
        self.assertFalse(boundary[0, 0])

    def test_study_area_boundary_ignores_internal_holes(self) -> None:
        inside = np.array(
            [
                [0, 0, 0, 0, 0, 0, 0],
                [0, 1, 1, 1, 1, 1, 0],
                [0, 1, 1, 1, 1, 1, 0],
                [0, 1, 1, 0, 1, 1, 0],
                [0, 1, 1, 1, 1, 1, 0],
                [0, 1, 1, 1, 1, 1, 0],
                [0, 0, 0, 0, 0, 0, 0],
            ],
            dtype=bool,
        )

        boundary = _study_area_boundary(inside)

        self.assertTrue(boundary[1, 3])
        self.assertFalse(boundary[3, 2])
        self.assertFalse(boundary[3, 4])

    def test_idrisi_fcbm_table_palettes_use_semantic_colors(self) -> None:
        self.assertEqual(
            _idrisi_pal_text(_IDRISI_LEGENDS["fcbm4"]).splitlines()[:5],
            ["0 0 0", "217 201 143", "0 109 44", "253 174 97", "215 25 28"],
        )
        self.assertEqual(
            _idrisi_pal_text(_IDRISI_LEGENDS["fcbm_vt0007"]).splitlines()[1:5],
            ["217 201 143", "0 109 44", "253 174 97", "215 25 28"],
        )
        self.assertEqual(
            _idrisi_pal_text(_IDRISI_LEGENDS["fcbm_accuracy"]).splitlines()[1:4],
            ["199 179 127", "35 139 69", "227 26 28"],
        )

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

    def test_geotiff_mosaics_prune_duplicate_semantic_raster_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tile_dir = root / "tiles"
            mosaic_dir = root / "geotiff"
            tile_dir.mkdir()
            data = np.array([[3]], dtype=np.int16)
            canonical = tile_dir / "UDefA_MB_LULC_T0_1985_EPSG_5880_30m-00000-00000.tif"
            legacy = tile_dir / "MapBiomas_LandCover_1985_EPSG_5880_30m-00000-00000.tif"
            for path in (canonical, legacy):
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
            (legacy.with_name(legacy.name + ".drive.json")).write_text("{}", encoding="utf-8")

            mosaics = build_geotiff_mosaics(tile_dir, mosaic_dir)

            self.assertEqual([path.name for path in mosaics], ["UDefA_MB_LULC_T0_1985_EPSG_5880_30m.tif"])
            self.assertTrue(canonical.exists())
            self.assertFalse(legacy.exists())
            self.assertFalse((legacy.with_name(legacy.name + ".drive.json")).exists())

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

    def test_idrisi_duplicate_prune_prefers_canonical_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            canonical = "UDefA_MB_LULC_T0_1985_EPSG_5880_30m"
            legacy = "MapBiomas_LandCover_1985_EPSG_5880_30m"
            for stem in (canonical, legacy):
                for suffix in (".rst", ".rdc", ".pal", ".smp"):
                    (root / f"{stem}{suffix}").write_text(stem, encoding="ascii")

            removed = prune_duplicate_idrisi_products(root)

            self.assertEqual(removed, 4)
            self.assertTrue((root / f"{canonical}.rst").exists())
            self.assertFalse((root / f"{legacy}.rst").exists())
            self.assertFalse((root / f"{legacy}.rdc").exists())
            self.assertFalse((root / f"{legacy}.pal").exists())
            self.assertFalse((root / f"{legacy}.smp").exists())

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

    def test_drive_raster_dedupe_prefers_canonical_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            local_canonical = root / "UDefA_MB_LULC_T0_1985_EPSG_5880_30m.tif"
            local_canonical.write_bytes(b"local")
            settings = {"grid": {"crs": "EPSG:5880", "scale_m": 30}, "earth_engine": {"scale_native_m": 30}}
            drive_items = [
                {"name": "MapBiomas_LandCover_1985_EPSG_5880_30m.tif"},
                {"name": "UDefA_MB_LULC_T0_1985_EPSG_5880_30m.tif"},
            ]

            preferred = _preferred_drive_raster_stems(drive_items, settings, root)

            self.assertEqual(preferred["mb_lulc_1985"], "UDefA_MB_LULC_T0_1985_EPSG_5880_30m")

    def test_drive_raster_dedupe_keeps_sources_separate(self) -> None:
        settings = {"grid": {"crs": "EPSG:5880", "scale_m": 30}, "earth_engine": {"scale_native_m": 30}}
        drive_items = [
            {"name": "CTrees_FCBM2_2013_30m_EPSG_5880_30m.tif"},
            {"name": "UDefA_MB_Forest_Input_T2_2013_EPSG_5880_30m.tif"},
        ]

        preferred = _preferred_drive_raster_stems(drive_items, settings, Path("missing-directory"))

        self.assertEqual(preferred["ct_forest_2013"], "CTrees_FCBM2_2013_30m_EPSG_5880_30m")
        self.assertEqual(preferred["mb_forest_2013"], "UDefA_MB_Forest_Input_T2_2013_EPSG_5880_30m")

    def test_missing_expected_rasters_uses_semantic_equivalence(self) -> None:
        scenarios = [Scenario("A", 100, 1985, 2024)]
        local_mosaics = [
            Path(f"MapBiomas_LandCover_{year}_EPSG_5880_30m.tif")
            for year in (1985, 2009, 2013, 2018, 2024)
        ]
        local_mosaics.append(Path("MapBiomas_Persistence_A_100pct_1985-2024_EPSG_5880_30m.tif"))

        self.assertEqual(_missing_expected_rasters(local_mosaics, scenarios), [])

    def test_products_missing_local_mosaics_uses_semantic_equivalence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "MapBiomas_LandCover_1985_EPSG_5880_30m.tif").write_bytes(b"local")
            products = [
                RasterProduct("UDefA_MB_LULC_T0_1985_EPSG_5880_30m", None, "same"),  # type: ignore[arg-type]
                RasterProduct("UDefA_MB_LULC_T1_2009_EPSG_5880_30m", None, "missing"),  # type: ignore[arg-type]
            ]

            missing = _products_missing_local_mosaics(products, root)

        self.assertEqual([product.name for product in missing], ["UDefA_MB_LULC_T1_2009_EPSG_5880_30m"])

    def test_products_missing_local_or_drive_mosaics_checks_drive_before_gee(self) -> None:
        products = [
            RasterProduct("UDefA_MB_LULC_T0_1985_EPSG_5880_30m", None, "drive"),  # type: ignore[arg-type]
            RasterProduct("UDefA_MB_LULC_T1_2009_EPSG_5880_30m", None, "missing"),  # type: ignore[arg-type]
        ]
        drive_keys = {"mb_lulc_1985"}

        missing = _products_missing_local_or_drive_mosaics(products, Path("missing-directory"), drive_keys)

        self.assertEqual([product.name for product in missing], ["UDefA_MB_LULC_T1_2009_EPSG_5880_30m"])

    def test_products_missing_local_or_drive_mosaics_checks_local_tiles_before_gee(self) -> None:
        products = [
            RasterProduct("UDefA_MB_LULC_T0_1985_EPSG_5880_30m", None, "tile"),  # type: ignore[arg-type]
            RasterProduct("UDefA_MB_LULC_T1_2009_EPSG_5880_30m", None, "missing"),  # type: ignore[arg-type]
        ]
        local_tile_keys = {"mb_lulc_1985"}

        missing = _products_missing_local_or_drive_mosaics(products, Path("missing-directory"), local_tile_keys)

        self.assertEqual([product.name for product in missing], ["UDefA_MB_LULC_T1_2009_EPSG_5880_30m"])

    def test_required_raster_semantic_keys_are_used_for_pre_gee_check(self) -> None:
        scenarios = [Scenario("A", 100, 1985, 2024)]
        settings = {"grid": {"crs": "EPSG:5880", "scale_m": 30}, "earth_engine": {"scale_native_m": 30}}

        keys = _required_raster_semantic_keys(expected_raster_products(settings, scenarios))

        self.assertIn("mb_lulc_1985", keys)
        self.assertIn("mb_persistence_a_100pct_1985_2024", keys)

    def test_raster_semantic_key_keeps_loss_and_agreement_separate(self) -> None:
        self.assertEqual(
            raster_semantic_key("MapBiomas_ForestLoss_2009_2013_EPSG_5880_30m"),
            "mb_forest_loss_2009_2013",
        )
        self.assertEqual(
            raster_semantic_key("Cross_ForestLossAgreement_CTrees_x_MapBiomas_2009_2013_EPSG_5880_30m"),
            "ct_mb_loss_agreement_2009_2013",
        )

    def test_raster_semantic_key_keeps_ctrees_loss_periods_separate(self) -> None:
        self.assertEqual(
            raster_semantic_key("Change_ForestToNonForest_CTrees_Calibration_2009_2013_30m_EPSG_5880_30m"),
            "ct_forest_loss_2009_2013",
        )
        self.assertEqual(
            raster_semantic_key("Change_ForestToNonForest_CTrees_HRP_2009_2018_30m_EPSG_5880_30m"),
            "ct_forest_loss_2009_2018",
        )
        self.assertNotEqual(
            raster_semantic_key("Change_ForestToNonForest_CTrees_Calibration_2009_2013_30m_EPSG_5880_30m"),
            raster_semantic_key("Change_ForestToNonForest_CTrees_HRP_2009_2018_30m_EPSG_5880_30m"),
        )

    def test_idrisi_title_uses_canonical_ctrees_loss_period(self) -> None:
        self.assertEqual(
            _idrisi_title("UDefA_Ct_ForestLoss_Cal_2009_2013_EPSG_5880_30m"),
            "CTrees Forest Loss - Cal 2009 2013",
        )
        self.assertEqual(
            _idrisi_title("UDefA_Ct_ForestLoss_HRP_2009_2018_EPSG_5880_30m"),
            "CTrees Forest Loss - HRP 2009 2018",
        )

    def test_raster_semantic_key_treats_dmjss_as_dmjss_not_forest_loss(self) -> None:
        self.assertEqual(
            raster_semantic_key("UDefA_Ct_DMJSS_2009_2018_EPSG_5880_30m"),
            "ct_dmjss",
        )
        self.assertEqual(
            raster_semantic_key("UDefA_MB_DMJSS_2009_2018_EPSG_5880_30m"),
            "mb_dmjss",
        )
        self.assertNotEqual(
            raster_semantic_key("Change_ForestToNonForest_CTrees_DMJSS_EPSG_5880_30m"),
            "ct_dmjss_forest_loss",
        )

    def test_raster_semantic_key_keeps_ctrees_and_mapbiomas_sources_separate(self) -> None:
        self.assertEqual(
            raster_semantic_key("CTrees_FCBM2_2013_30m_EPSG_5880_30m"),
            "ct_forest_2013",
        )
        self.assertEqual(
            raster_semantic_key("UDefA_MB_Forest_Input_T2_2013_EPSG_5880_30m"),
            "mb_forest_2013",
        )
        self.assertEqual(
            raster_semantic_key("UDefA_CTrees_NonForest_T2_2013_30m_EPSG_5880_30m"),
            "ct_nonforest_T2_2013",
        )
        self.assertEqual(
            raster_semantic_key("UDefA_MB_NonForest_Input_T2_2013_EPSG_5880_30m"),
            "mb_nonforest_T2_2013",
        )
        self.assertEqual(
            raster_semantic_key("UDefA_Ct_ForestChange4_Cal_2009_2013_EPSG_5880_30m"),
            "ct_forest_change4_2009_2013",
        )
        self.assertEqual(
            raster_semantic_key("UDefA_MB_ForestChange4_Cal_2009_2013_EPSG_5880_30m"),
            "mb_forest_change4_2009_2013",
        )


if __name__ == "__main__":
    unittest.main()
