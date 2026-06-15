#!/usr/bin/env python3
"""
Configuration-driven automation runner for UDef-ARP.

The GUI remains the primary upstream interface. This runner lets users define
all paths and numeric inputs once in a YAML file, then execute the selected
workflow stages without clicking through each screen.
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
import re
from typing import Any

import yaml


DEFAULT_MAX_ITERATIONS = 25
SCENARIO_MULTIPLIERS = {
    "bau": 1.0,
    "low": 0.9,
    "medium": 1.1,
    "high": 1.2,
}

DEFAULT_INPUT_FILES = {
    "admin_divisions": "admin_divisions.tif",
    "jurisdiction_mask": "jurisdiction_mask.tif",
    "forest_mask_cal": "forest_mask_cal.tif",
    "distance_to_non_forest_cal": "distance_to_non_forest_cal.tif",
    "distance_to_non_forest_hrp": "distance_to_non_forest_hrp.tif",
    "distance_to_non_forest_vp": "distance_to_non_forest_vp.tif",
    "deforestation_cal": "deforestation_cal.tif",
    "deforestation_hrp": "deforestation_hrp.tif",
    "deforestation_cnf": "deforestation_cnf.tif",
    "empirical_vulnerability": "empirical_vulnerability_0_1.tif",
}

LEGACY_BLOCK_NAMES = {
    "nrt",
    "vulnerability_distance",
    "vulnerability_alternative",
    "fit",
    "cnf",
    "vp",
    "model_evaluation",
    "empirical_vulnerability_comparison",
}


def load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}
    if not isinstance(config, dict):
        raise ValueError("The automation config must be a YAML mapping.")
    return config


def require(config: dict[str, Any], key: str) -> Any:
    value = config.get(key)
    if value in (None, ""):
        raise ValueError(f"Missing required config value: {key}")
    return value


def as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def slugify(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "_", Path(value).stem).strip("_").lower()
    return slug or "empirical"


class AutoRunner:
    def __init__(self, config: dict[str, Any], dry_run: bool = False) -> None:
        self.config = config
        self.dry_run = dry_run
        self.working_directory = Path(require(config, "working_directory")).expanduser().resolve()
        self.output_prefix = str(config.get("output_prefix", "udef"))
        self.default_extension = str(config.get("output_extension", ".tif"))
        if not self.default_extension.startswith("."):
            self.default_extension = f".{self.default_extension}"
        self.summary: list[dict[str, Any]] = []
        self.planned_outputs: set[str] = set()
        self.last_nrt: int | None = None
        self.last_relative_frequency_csv: str | None = None
        self.expected_deforestation_scenarios: dict[str, float] | None = None

    def path(self, value: str | Path) -> str:
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = self.working_directory / candidate
        return str(candidate.resolve())

    def output(self, value: str | Path | None, suffix: str) -> str:
        if value:
            return self.path(value)
        return self.path(f"{self.output_prefix}_{suffix}{self.default_extension}")

    def csv_output(self, value: str | Path | None, suffix: str) -> str:
        if value:
            return self.path(value)
        return self.path(f"{self.output_prefix}_{suffix}.csv")

    def configured_input(self, block: dict[str, Any], key: str, default_name: str) -> str:
        return str(block.get(key) or self.config.get("inputs", {}).get(key) or default_name)

    def default_input(self, key: str) -> str:
        return str(self.config.get("inputs", {}).get(key) or DEFAULT_INPUT_FILES[key])

    def require_file(self, value: str | Path, key: str) -> str:
        path = self.path(value)
        if not Path(path).exists() and path not in self.planned_outputs:
            defaults = "\n".join(
                f"  - {name}: {filename}"
                for name, filename in sorted(DEFAULT_INPUT_FILES.items())
            )
            raise FileNotFoundError(
                f"{key} does not exist: {path}\n\n"
                "Use the exact default input filenames in working_directory, "
                "or override them under the YAML 'inputs:' block.\n\n"
                f"Default input filenames:\n{defaults}"
            )
        return path

    def historical_period_years(self, block: dict[str, Any] | None = None) -> float:
        block = block or {}
        value = (
            block.get("historical_period_years")
            or self.config.get("historical_period_years")
            or self.config.get("hrp_years")
        )
        if value in (None, ""):
            raise ValueError(
                "Missing required value: historical_period_years. "
                "This is needed to calculate BAU expected deforestation from "
                "the historical-period deforestation map."
            )
        years = float(value)
        if years <= 0:
            raise ValueError("historical_period_years must be greater than zero.")
        return years

    def calculate_binary_deforestation_area_ha(self, raster_path: str) -> float:
        from osgeo import gdal
        import numpy as np

        dataset = gdal.Open(raster_path)
        if dataset is None:
            raise FileNotFoundError(f"Could not open raster: {raster_path}")
        band = dataset.GetRasterBand(1)
        array = band.ReadAsArray()
        nodata = band.GetNoDataValue()
        valid = np.isfinite(array)
        if nodata is not None:
            valid &= array != nodata
        deforested_pixels = (array == 1) & valid
        pixel_area_ha = abs(dataset.GetGeoTransform()[1] * dataset.GetGeoTransform()[5]) / 10000
        return float(deforested_pixels.sum() * pixel_area_ha)

    def get_expected_deforestation_scenarios(
        self,
        block: dict[str, Any] | None = None,
    ) -> dict[str, float]:
        block = block or {}
        if self.expected_deforestation_scenarios is not None:
            return self.expected_deforestation_scenarios

        explicit_scenarios = block.get("expected_deforestation_scenarios") or self.config.get(
            "expected_deforestation_scenarios"
        )
        if explicit_scenarios:
            scenarios = {str(key): float(value) for key, value in explicit_scenarios.items()}
            self.expected_deforestation_scenarios = scenarios
            return scenarios

        explicit = block.get("expected_deforestation") or self.config.get("expected_deforestation")
        if explicit not in (None, ""):
            bau = float(explicit)
        else:
            years = self.historical_period_years(block)
            if self.dry_run:
                bau = float(block.get("dry_run_historical_deforestation_ha", 100000)) / years
            else:
                deforestation_hrp = self.require_file(
                    self.configured_input(
                        block,
                        "deforestation_hrp",
                        self.default_input("deforestation_hrp"),
                    ),
                    "expected_deforestation.deforestation_hrp",
                )
                bau = self.calculate_binary_deforestation_area_ha(deforestation_hrp) / years

        scenarios = {
            name: float(bau * multiplier)
            for name, multiplier in SCENARIO_MULTIPLIERS.items()
        }
        self.expected_deforestation_scenarios = scenarios
        return scenarios

    def record(self, stage: str, outputs: dict[str, Any]) -> None:
        self.summary.append({"stage": stage, "outputs": outputs})
        for value in outputs.values():
            if isinstance(value, str) and value not in {"<dry-run>"}:
                self.planned_outputs.add(value)

    def announce(self, message: str) -> None:
        print(f"[UDef-ARP auto] {message}")

    def run(self) -> list[dict[str, Any]]:
        if not self.working_directory.exists():
            raise FileNotFoundError(f"working_directory does not exist: {self.working_directory}")

        self.announce(f"Working directory: {self.working_directory}")

        stages = as_list(self.config.get("stages"))
        if not stages and not any(name in self.config for name in LEGACY_BLOCK_NAMES):
            stages = self.default_stages()

        for stage in stages:
            self.run_stage(stage)

        if not stages and any(name in self.config for name in LEGACY_BLOCK_NAMES):
            self.run_legacy_blocks()

        return self.summary

    def default_stages(self) -> list[dict[str, Any]]:
        return [
            {"name": "nrt"},
            {"name": "vulnerability_distance", "period": "hrp"},
            {"name": "fit"},
            {"name": "vulnerability_distance", "period": "vp"},
            {"name": "cnf"},
            {"name": "vp"},
        ]

    def run_legacy_blocks(self) -> None:
        for key, handler in [
            ("nrt", self.run_nrt),
            ("vulnerability_distance", self.run_vulnerability_distance),
            ("vulnerability_alternative", self.run_vulnerability_alternative),
            ("fit", self.run_fit),
            ("cnf", self.run_cnf),
            ("vp", self.run_vp),
            ("model_evaluation", self.run_model_evaluation),
            ("empirical_vulnerability_comparison", self.run_empirical_vulnerability_comparison),
        ]:
            block = self.config.get(key)
            if isinstance(block, dict) and block.get("enabled", True):
                handler(block)

    def run_stage(self, stage: dict[str, Any]) -> None:
        if not isinstance(stage, dict):
            raise ValueError("Each item in stages must be a mapping.")
        name = require(stage, "name")
        handlers = {
            "nrt": self.run_nrt,
            "vulnerability_distance": self.run_vulnerability_distance,
            "vulnerability_alternative": self.run_vulnerability_alternative,
            "fit": self.run_fit,
            "cnf": self.run_cnf,
            "vp": self.run_vp,
            "model_evaluation": self.run_model_evaluation,
            "empirical_vulnerability_comparison": self.run_empirical_vulnerability_comparison,
        }
        if name not in handlers:
            raise ValueError(f"Unsupported stage: {name}")
        if stage.get("enabled", True):
            handlers[name](stage)

    def run_nrt(self, block: dict[str, Any]) -> None:
        self.announce("Calculating NRT")
        distance = self.require_file(
            self.configured_input(
                block,
                "distance",
                self.default_input("distance_to_non_forest_cal"),
            ),
            "nrt.distance",
        )
        deforestation_hrp = self.require_file(
            self.configured_input(
                block,
                "deforestation_hrp",
                self.default_input("deforestation_hrp"),
            ),
            "nrt.deforestation_hrp",
        )
        mask = self.require_file(
            self.configured_input(block, "mask", self.default_input("jurisdiction_mask")),
            "nrt.mask",
        )

        if self.dry_run:
            self.last_nrt = int(block.get("dry_run_nrt", 1))
            self.record("nrt", {"value": "<dry-run>"})
            return

        from vulnerability_map import VulnerabilityMap

        nrt = VulnerabilityMap().nrt_calculation(distance, deforestation_hrp, mask)
        self.last_nrt = int(nrt)
        output_txt = block.get("output_txt")
        outputs: dict[str, Any] = {"value": int(nrt)}
        if output_txt:
            output_path = self.path(output_txt)
            Path(output_path).write_text(f"{nrt}\n", encoding="utf-8")
            outputs["output_txt"] = output_path
        self.record("nrt", outputs)
        self.announce(f"NRT = {nrt}")

    def run_vulnerability_distance(self, block: dict[str, Any]) -> None:
        self.announce("Generating distance-based vulnerability map")
        period = str(block.get("period", "hrp")).lower()
        distance_defaults = {
            "cal": "distance_to_non_forest_cal",
            "hrp": "distance_to_non_forest_hrp",
            "vp": "distance_to_non_forest_vp",
            "cnf": "distance_to_non_forest_vp",
        }
        if period not in distance_defaults:
            raise ValueError("vulnerability_distance.period must be one of: cal, hrp, cnf, vp")
        distance = self.require_file(
            self.configured_input(
                block,
                "distance",
                self.default_input(distance_defaults[period]),
            ),
            "vulnerability_distance.distance",
        )
        mask = self.require_file(
            self.configured_input(block, "mask", self.default_input("jurisdiction_mask")),
            "vulnerability_distance.mask",
        )
        nrt_value = block.get("nrt", self.last_nrt)
        if nrt_value in (None, ""):
            raise ValueError(
                "Missing required config value: vulnerability_distance.nrt "
                "(or run an nrt stage before this stage)"
            )
        nrt = int(nrt_value)
        n_classes = int(block.get("n_classes", 29))
        output = self.output(block.get("output"), f"vulnerability_{period}")

        if not self.dry_run:
            from osgeo import gdal
            from vulnerability_map import VulnerabilityMap

            tool = VulnerabilityMap()
            tool.set_working_directory(str(self.working_directory))
            data = tool.geometric_classification(distance, nrt, n_classes, mask)
            tool.array_to_image(distance, output, data, gdal.GDT_Int16, -1)
            tool.replace_ref_system(distance, output)
        self.record("vulnerability_distance", {"output": output})

    def run_vulnerability_alternative(self, block: dict[str, Any]) -> None:
        self.announce("Generating alternative vulnerability map")
        empirical = self.require_file(
            self.configured_input(
                block,
                "empirical",
                self.default_input("empirical_vulnerability"),
            ),
            "vulnerability_alternative.empirical",
        )
        mask = self.require_file(
            self.configured_input(block, "mask", self.default_input("jurisdiction_mask")),
            "vulnerability_alternative.mask",
        )
        forest_mask = self.require_file(
            self.configured_input(block, "forest_mask", self.default_input("forest_mask_cal")),
            "vulnerability_alternative.forest_mask",
        )
        n_classes = int(block.get("n_classes", 30))
        output = self.output(block.get("output"), "vulnerability_alternative")

        if not self.dry_run:
            from osgeo import gdal
            from vulnerability_map import VulnerabilityMap

            tool = VulnerabilityMap()
            tool.set_working_directory(str(self.working_directory))
            data = tool.geometric_classification_alternative(empirical, n_classes, mask, forest_mask)
            tool.array_to_image(empirical, output, data, gdal.GDT_Int16, -1)
            tool.replace_ref_system(empirical, output)
        self.record("vulnerability_alternative", {"output": output})

    def run_fit(self, block: dict[str, Any]) -> None:
        self.announce("Running fitting workflow")
        risk30_hrp = self.require_file(
            self.configured_input(
                block,
                "risk30_hrp",
                f"{self.output_prefix}_vulnerability_hrp{self.default_extension}",
            ),
            "fit.risk30_hrp",
        )
        municipality = self.require_file(
            self.configured_input(block, "municipality", self.default_input("admin_divisions")),
            "fit.municipality",
        )
        deforestation_hrp = self.require_file(
            self.configured_input(
                block,
                "deforestation_hrp",
                self.default_input("deforestation_hrp"),
            ),
            "fit.deforestation_hrp",
        )
        csv_name = self.csv_output(block.get("relative_frequency_csv"), "relative_frequency_hrp")
        modeling_region = self.output(block.get("modeling_region_output"), "modeling_region_hrp")
        density = self.output(block.get("density_output"), "density_hrp")

        if not self.dry_run:
            from allocation_tool import AllocationTool

            AllocationTool().execute_workflow_fit(
                str(self.working_directory),
                risk30_hrp,
                municipality,
                deforestation_hrp,
                csv_name,
                modeling_region,
                density,
            )
        self.record(
            "fit",
            {
                "relative_frequency_csv": csv_name,
                "modeling_region_output": modeling_region,
                "density_output": density,
            },
        )
        self.last_relative_frequency_csv = csv_name

    def run_cnf(self, block: dict[str, Any]) -> None:
        self.announce("Running confirmation prediction workflow")
        relative_frequency_csv_value = block.get(
            "relative_frequency_csv",
            self.last_relative_frequency_csv,
        )
        if relative_frequency_csv_value in (None, ""):
            raise ValueError(
                "Missing required config value: cnf.relative_frequency_csv "
                "(or run a fit stage before this stage)"
            )
        relative_frequency_csv = self.require_file(relative_frequency_csv_value, "cnf.relative_frequency_csv")
        municipality = self.require_file(
            self.configured_input(block, "municipality", self.default_input("admin_divisions")),
            "cnf.municipality",
        )
        deforestation_cnf = self.require_file(
            self.configured_input(
                block,
                "deforestation_cnf",
                self.default_input("deforestation_cnf"),
            ),
            "cnf.deforestation_cnf",
        )
        risk30_vp = self.require_file(
            self.configured_input(
                block,
                "risk30_vp",
                f"{self.output_prefix}_vulnerability_vp{self.default_extension}",
            ),
            "cnf.risk30_vp",
        )
        modeling_region = self.output(block.get("modeling_region_output"), "modeling_region_cnf")
        density = self.output(block.get("density_output"), "density_cnf")
        max_iterations = int(block.get("max_iterations", DEFAULT_MAX_ITERATIONS))

        result: dict[str, Any] = {
            "modeling_region_output": modeling_region,
            "density_output": density,
        }
        if not self.dry_run:
            from allocation_tool import AllocationTool

            missing_ids, iterations = AllocationTool().execute_workflow_cnf(
                str(self.working_directory),
                max_iterations,
                relative_frequency_csv,
                municipality,
                deforestation_cnf,
                risk30_vp,
                modeling_region,
                density,
            )
            result["missing_modeling_region_ids"] = [int(x) for x in missing_ids.tolist()]
            result["iterations"] = int(iterations)
        self.record("cnf", result)

    def run_vp(self, block: dict[str, Any]) -> None:
        self.announce("Running validity-period prediction workflow")
        relative_frequency_csv_value = block.get(
            "relative_frequency_csv",
            self.last_relative_frequency_csv,
        )
        if relative_frequency_csv_value in (None, ""):
            raise ValueError(
                "Missing required config value: vp.relative_frequency_csv "
                "(or run a fit stage before this stage)"
            )
        relative_frequency_csv = self.require_file(relative_frequency_csv_value, "vp.relative_frequency_csv")
        municipality = self.require_file(
            self.configured_input(block, "municipality", self.default_input("admin_divisions")),
            "vp.municipality",
        )
        risk30_vp = self.require_file(
            self.configured_input(
                block,
                "risk30_vp",
                f"{self.output_prefix}_vulnerability_vp{self.default_extension}",
            ),
            "vp.risk30_vp",
        )
        scenarios = self.get_expected_deforestation_scenarios(block)
        scenario_csv = self.csv_output(
            block.get("scenario_csv"),
            "expected_deforestation_scenarios",
        )
        if not self.dry_run:
            self.write_scenario_csv(scenarios, scenario_csv)
        max_iterations = int(block.get("max_iterations", DEFAULT_MAX_ITERATIONS))

        result: dict[str, Any] = {"scenario_csv": scenario_csv, "scenarios": {}}
        for scenario_name, expected_deforestation in scenarios.items():
            modeling_region = self.output(
                block.get(f"{scenario_name}_modeling_region_output"),
                f"modeling_region_vp_{scenario_name}",
            )
            density = self.output(
                block.get(f"{scenario_name}_density_output"),
                f"density_vp_{scenario_name}",
            )
            scenario_result: dict[str, Any] = {
                "expected_deforestation_ha_per_year": expected_deforestation,
                "modeling_region_output": modeling_region,
                "density_output": density,
            }
            if not self.dry_run:
                from allocation_tool import AllocationTool

                missing_ids, iterations = AllocationTool().execute_workflow_vp(
                    str(self.working_directory),
                    max_iterations,
                    relative_frequency_csv,
                    municipality,
                    expected_deforestation,
                    risk30_vp,
                    modeling_region,
                    density,
                )
                scenario_result["missing_modeling_region_ids"] = [
                    int(x) for x in missing_ids.tolist()
                ]
                scenario_result["iterations"] = int(iterations)
            result["scenarios"][scenario_name] = scenario_result
        self.record("vp", result)

    def run_model_evaluation(self, block: dict[str, Any]) -> None:
        self.announce("Running model evaluation")
        mask = self.require_file(
            self.configured_input(block, "mask", self.default_input("jurisdiction_mask")),
            "model_evaluation.mask",
        )
        density = self.require_file(require(block, "density"), "model_evaluation.density")
        deforestation = self.require_file(require(block, "deforestation"), "model_evaluation.deforestation")
        grid_area = int(require(block, "grid_area"))
        title = str(block.get("title", "Model Evaluation"))
        plot_output = self.path(block.get("plot_output", f"{self.output_prefix}_model_evaluation.png"))
        raster_output = self.output(block.get("raster_output"), "model_evaluation_grid")
        combined_deforestation_output = block.get("combined_deforestation_output")
        forest_mask = None
        deforestation_cal = None
        combined_output = None
        if combined_deforestation_output:
            forest_mask = self.require_file(
                self.configured_input(block, "forest_mask", self.default_input("forest_mask_cal")),
                "model_evaluation.forest_mask",
            )
            deforestation_cal = self.require_file(
                self.configured_input(
                    block,
                    "deforestation_cal",
                    self.default_input("deforestation_cal"),
                ),
                "model_evaluation.deforestation_cal",
            )
            combined_output = self.output(
                combined_deforestation_output,
                "combined_deforestation_reference",
            )
        xmax = block.get("xmax", "default")
        ymax = block.get("ymax", "default")

        if not self.dry_run:
            from model_evaluation import ModelEvaluation

            tool = ModelEvaluation()
            tool.set_working_directory(str(self.working_directory))
            tool.create_mask_polygon(mask)
            clipped_gdf = tool.create_thiessen_polygon(
                grid_area,
                mask,
                density,
                deforestation,
                plot_output,
                raster_output,
            )
            tool.replace_ref_system(mask, raster_output)
            if combined_output:
                tool.create_deforestation_map(
                    forest_mask,
                    deforestation_cal,
                    deforestation,
                    combined_output,
                )
                tool.replace_ref_system(forest_mask, combined_output)
                tool.replace_legend(combined_output)
            tool.create_plot(grid_area, clipped_gdf, title, plot_output, xmax, ymax)
            tool.remove_temp_files()
        outputs = {"plot_output": plot_output, "raster_output": raster_output}
        if combined_output:
            outputs["combined_deforestation_output"] = combined_output
        self.record("model_evaluation", outputs)

    def empirical_map_specs(self, block: dict[str, Any]) -> list[dict[str, str]]:
        raw_maps = (
            block.get("empirical_maps")
            or self.config.get("empirical_vulnerability_maps")
            or self.config.get("inputs", {}).get("empirical_vulnerability_maps")
        )
        specs: list[dict[str, str]] = []

        if raw_maps:
            for item in as_list(raw_maps):
                if isinstance(item, dict):
                    path_value = item.get("path") or item.get("file") or item.get("empirical")
                    if not path_value:
                        raise ValueError(
                            "Each empirical map object must define 'path', 'file', or 'empirical'."
                        )
                    specs.append(
                        {
                            "path": str(path_value),
                            "label": str(item.get("label") or slugify(str(path_value))),
                            "output": str(item.get("output") or ""),
                        }
                    )
                else:
                    specs.append(
                        {
                            "path": str(item),
                            "label": slugify(str(item)),
                            "output": "",
                        }
                    )
        else:
            discovered = sorted(self.working_directory.glob("empirical_vulnerability_*.tif"))
            legacy = self.working_directory / DEFAULT_INPUT_FILES["empirical_vulnerability"]
            if legacy.exists() and legacy not in discovered:
                discovered.append(legacy)
            specs = [
                {"path": path.name, "label": slugify(path.name), "output": ""}
                for path in discovered
            ]

        if not specs:
            raise ValueError(
                "At least one empirical vulnerability map is required for "
                "empirical_vulnerability_comparison. Provide files named "
                "empirical_vulnerability_*.tif in working_directory or list them "
                "under empirical_vulnerability_maps in the YAML."
            )

        labels = [spec["label"] for spec in specs]
        if len(labels) != len(set(labels)):
            raise ValueError("Empirical vulnerability map labels must be unique.")
        return specs

    def raster_class_summary_rows(
        self,
        maps: list[dict[str, str]],
    ) -> list[dict[str, Any]]:
        from osgeo import gdal
        import numpy as np

        rows: list[dict[str, Any]] = []
        for item in maps:
            raster_path = item["path"]
            dataset = gdal.Open(raster_path)
            if dataset is None:
                raise FileNotFoundError(f"Could not open raster: {raster_path}")
            band = dataset.GetRasterBand(1)
            array = band.ReadAsArray()
            nodata = band.GetNoDataValue()
            valid = np.isfinite(array)
            if nodata is not None:
                valid &= array != nodata
            valid &= array > 0
            valid_values = array[valid]
            pixel_count_total = int(valid_values.size)
            pixel_area_ha = abs(dataset.GetGeoTransform()[1] * dataset.GetGeoTransform()[5]) / 10000
            if pixel_count_total == 0:
                rows.append(
                    {
                        "map_label": item["label"],
                        "map_type": item["map_type"],
                        "raster_path": raster_path,
                        "class_value": "",
                        "pixel_count": 0,
                        "area_ha": 0,
                        "share_of_valid_pixels": 0,
                    }
                )
                continue
            classes, counts = np.unique(valid_values.astype(int), return_counts=True)
            for class_value, count in zip(classes.tolist(), counts.tolist()):
                rows.append(
                    {
                        "map_label": item["label"],
                        "map_type": item["map_type"],
                        "raster_path": raster_path,
                        "class_value": int(class_value),
                        "pixel_count": int(count),
                        "area_ha": float(count * pixel_area_ha),
                        "share_of_valid_pixels": float(count / pixel_count_total),
                    }
                )
        return rows

    def write_comparison_csv(self, rows: list[dict[str, Any]], output_csv: str) -> None:
        fieldnames = [
            "map_label",
            "map_type",
            "raster_path",
            "class_value",
            "pixel_count",
            "area_ha",
            "share_of_valid_pixels",
        ]
        with Path(output_csv).open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

    def write_scenario_csv(self, scenarios: dict[str, float], output_csv: str) -> None:
        with Path(output_csv).open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=["scenario", "expected_deforestation_ha_per_year"],
            )
            writer.writeheader()
            for scenario, value in scenarios.items():
                writer.writerow(
                    {
                        "scenario": scenario,
                        "expected_deforestation_ha_per_year": value,
                    }
                )

    def run_empirical_vulnerability_comparison(self, block: dict[str, Any]) -> None:
        self.announce("Running empirical vulnerability comparison")
        specs = self.empirical_map_specs(block)
        mask = self.require_file(
            self.configured_input(block, "mask", self.default_input("jurisdiction_mask")),
            "empirical_vulnerability_comparison.mask",
        )
        forest_mask = self.require_file(
            self.configured_input(block, "forest_mask", self.default_input("forest_mask_cal")),
            "empirical_vulnerability_comparison.forest_mask",
        )
        n_classes = int(block.get("n_classes", 30))
        benchmark = self.require_file(
            self.configured_input(
                block,
                "benchmark_vulnerability",
                f"{self.output_prefix}_vulnerability_vp{self.default_extension}",
            ),
            "empirical_vulnerability_comparison.benchmark_vulnerability",
        )
        comparison_csv = self.csv_output(
            block.get("comparison_csv"),
            "empirical_vulnerability_comparison",
        )
        run_fit = bool(block.get("run_fit", True))
        run_cnf = bool(block.get("run_cnf", True))
        run_vp = bool(block.get("run_vp", self.config.get("expected_deforestation") is not None))

        generated_maps: list[dict[str, str]] = [
            {
                "label": "benchmark_distance",
                "map_type": "benchmark",
                "path": benchmark,
            }
        ]
        empirical_outputs: list[dict[str, Any]] = []

        if not self.dry_run:
            from osgeo import gdal
            from vulnerability_map import VulnerabilityMap

            tool = VulnerabilityMap()
            tool.set_working_directory(str(self.working_directory))
        else:
            tool = None
            gdal = None

        for spec in specs:
            empirical = self.require_file(
                spec["path"],
                f"empirical_vulnerability_comparison.empirical_maps.{spec['label']}",
            )
            output = (
                self.path(spec["output"])
                if spec["output"]
                else self.output(None, f"vulnerability_empirical_{spec['label']}")
            )
            if not self.dry_run:
                data = tool.geometric_classification_alternative(
                    empirical,
                    n_classes,
                    mask,
                    forest_mask,
                )
                tool.array_to_image(empirical, output, data, gdal.GDT_Int16, -1)
                tool.replace_ref_system(empirical, output)

            generated_maps.append(
                {
                    "label": spec["label"],
                    "map_type": "empirical",
                    "path": output,
                }
            )
            self.planned_outputs.add(output)

            workflow_outputs: dict[str, Any] = {"vulnerability_output": output}
            if run_fit:
                fit_block = {
                    "risk30_hrp": output,
                    "relative_frequency_csv": f"{self.output_prefix}_{spec['label']}_relative_frequency_hrp.csv",
                    "modeling_region_output": f"{self.output_prefix}_{spec['label']}_modeling_region_hrp{self.default_extension}",
                    "density_output": f"{self.output_prefix}_{spec['label']}_density_hrp{self.default_extension}",
                }
                self.run_fit(fit_block)
                workflow_outputs["fit"] = self.summary[-1]["outputs"]
                empirical_csv = workflow_outputs["fit"]["relative_frequency_csv"]
            else:
                empirical_csv = block.get("relative_frequency_csv")

            if run_cnf:
                cnf_block = {
                    "relative_frequency_csv": empirical_csv,
                    "risk30_vp": output,
                    "modeling_region_output": f"{self.output_prefix}_{spec['label']}_modeling_region_cnf{self.default_extension}",
                    "density_output": f"{self.output_prefix}_{spec['label']}_density_cnf{self.default_extension}",
                    "max_iterations": block.get("max_iterations", DEFAULT_MAX_ITERATIONS),
                }
                self.run_cnf(cnf_block)
                workflow_outputs["cnf"] = self.summary[-1]["outputs"]

            if run_vp:
                vp_block = {
                    "relative_frequency_csv": empirical_csv,
                    "risk30_vp": output,
                    "max_iterations": block.get("max_iterations", DEFAULT_MAX_ITERATIONS),
                }
                for scenario_name in SCENARIO_MULTIPLIERS:
                    vp_block[
                        f"{scenario_name}_modeling_region_output"
                    ] = (
                        f"{self.output_prefix}_{spec['label']}_modeling_region_vp_"
                        f"{scenario_name}{self.default_extension}"
                    )
                    vp_block[f"{scenario_name}_density_output"] = (
                        f"{self.output_prefix}_{spec['label']}_density_vp_"
                        f"{scenario_name}{self.default_extension}"
                    )
                if block.get("expected_deforestation_scenarios") is not None:
                    vp_block["expected_deforestation_scenarios"] = block[
                        "expected_deforestation_scenarios"
                    ]
                self.run_vp(vp_block)
                workflow_outputs["vp"] = self.summary[-1]["outputs"]

            empirical_outputs.append(
                {
                    "label": spec["label"],
                    "input": empirical,
                    "outputs": workflow_outputs,
                }
            )

        if not self.dry_run:
            rows = self.raster_class_summary_rows(generated_maps)
            self.write_comparison_csv(rows, comparison_csv)

        self.record(
            "empirical_vulnerability_comparison",
            {
                "benchmark_vulnerability": benchmark,
                "empirical_map_count": len(specs),
                "comparison_csv": comparison_csv,
                "empirical_outputs": empirical_outputs,
            },
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run UDef-ARP workflows automatically from a YAML config file."
    )
    parser.add_argument("config", type=Path, help="Path to the automation YAML file")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate inputs and print planned outputs without running raster processing",
    )
    parser.add_argument(
        "--summary",
        type=Path,
        help="Optional path to write a JSON summary of executed stages",
    )
    args = parser.parse_args()

    runner = AutoRunner(load_config(args.config), dry_run=args.dry_run)
    summary = runner.run()

    print(json.dumps(summary, indent=2))
    if args.summary:
        args.summary.write_text(json.dumps(summary, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
