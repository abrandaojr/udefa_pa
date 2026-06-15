#!/usr/bin/env python3
"""
Configuration-driven automation runner for UDef-ARP.

The GUI remains the primary upstream interface. This runner lets users define
all paths and numeric inputs once in a YAML file, then execute the selected
workflow stages without clicking through each screen.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import yaml


DEFAULT_MAX_ITERATIONS = 25

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
        expected_deforestation = float(
            block.get("expected_deforestation")
            or self.config.get("expected_deforestation")
            or require(block, "expected_deforestation")
        )
        modeling_region = self.output(block.get("modeling_region_output"), "modeling_region_vp")
        density = self.output(block.get("density_output"), "density_vp")
        max_iterations = int(block.get("max_iterations", DEFAULT_MAX_ITERATIONS))

        result: dict[str, Any] = {
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
            result["missing_modeling_region_ids"] = [int(x) for x in missing_ids.tolist()]
            result["iterations"] = int(iterations)
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
