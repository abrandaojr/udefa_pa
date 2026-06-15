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

    def require_file(self, value: str | Path, key: str) -> str:
        path = self.path(value)
        if not Path(path).exists():
            raise FileNotFoundError(f"{key} does not exist: {path}")
        return path

    def record(self, stage: str, outputs: dict[str, Any]) -> None:
        self.summary.append({"stage": stage, "outputs": outputs})

    def announce(self, message: str) -> None:
        print(f"[UDef-ARP auto] {message}")

    def run(self) -> list[dict[str, Any]]:
        if not self.working_directory.exists():
            raise FileNotFoundError(f"working_directory does not exist: {self.working_directory}")

        self.announce(f"Working directory: {self.working_directory}")

        for stage in as_list(self.config.get("stages")):
            self.run_stage(stage)

        if not self.config.get("stages"):
            self.run_legacy_blocks()

        return self.summary

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
        distance = self.require_file(require(block, "distance"), "nrt.distance")
        deforestation_hrp = self.require_file(
            require(block, "deforestation_hrp"),
            "nrt.deforestation_hrp",
        )
        mask = self.require_file(require(block, "mask"), "nrt.mask")

        if self.dry_run:
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
        distance = self.require_file(require(block, "distance"), "vulnerability_distance.distance")
        mask = self.require_file(require(block, "mask"), "vulnerability_distance.mask")
        nrt_value = block.get("nrt", self.last_nrt)
        if nrt_value in (None, ""):
            raise ValueError(
                "Missing required config value: vulnerability_distance.nrt "
                "(or run an nrt stage before this stage)"
            )
        nrt = int(nrt_value)
        n_classes = int(block.get("n_classes", 29))
        output = self.output(block.get("output"), "vulnerability_distance")

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
        empirical = self.require_file(require(block, "empirical"), "vulnerability_alternative.empirical")
        mask = self.require_file(require(block, "mask"), "vulnerability_alternative.mask")
        forest_mask = self.require_file(require(block, "forest_mask"), "vulnerability_alternative.forest_mask")
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
        risk30_hrp = self.require_file(require(block, "risk30_hrp"), "fit.risk30_hrp")
        municipality = self.require_file(require(block, "municipality"), "fit.municipality")
        deforestation_hrp = self.require_file(require(block, "deforestation_hrp"), "fit.deforestation_hrp")
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
        municipality = self.require_file(require(block, "municipality"), "cnf.municipality")
        deforestation_cnf = self.require_file(require(block, "deforestation_cnf"), "cnf.deforestation_cnf")
        risk30_vp = self.require_file(require(block, "risk30_vp"), "cnf.risk30_vp")
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
        municipality = self.require_file(require(block, "municipality"), "vp.municipality")
        risk30_vp = self.require_file(require(block, "risk30_vp"), "vp.risk30_vp")
        expected_deforestation = float(require(block, "expected_deforestation"))
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
        mask = self.require_file(require(block, "mask"), "model_evaluation.mask")
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
                require(block, "forest_mask"),
                "model_evaluation.forest_mask",
            )
            deforestation_cal = self.require_file(
                require(block, "deforestation_cal"),
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
