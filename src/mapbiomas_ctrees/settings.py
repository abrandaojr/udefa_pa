"""Configuration loading for the analytical workflow."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class Scenario:
    """A MapBiomas forest-persistence scenario."""

    identifier: str
    threshold_percent: float
    start_year: int
    end_year: int

    @property
    def label(self) -> str:
        """Return the scenario label used in exported file names and reports."""
        return (
            f"{self.identifier}_{self.threshold_percent:g}pct_"
            f"{self.start_year}-{self.end_year}"
        )


@dataclass(frozen=True)
class ProjectSettings:
    """Validated project settings."""

    path: Path
    raw: dict[str, Any]
    scenarios: list[Scenario]
    output_root: Path
    output_directories: dict[str, Path]


def load_settings(path: str | Path, output_root: str | Path | None = None) -> ProjectSettings:
    """Load the YAML settings file and validate required analytical fields."""
    settings_path = Path(path)
    if not settings_path.exists():
        raise FileNotFoundError(f"Settings file not found: {settings_path}")

    raw = yaml.safe_load(settings_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("The settings file must contain a YAML mapping.")

    for key in (
        "project.name",
        "earth_engine.project",
        "earth_engine.aoi.states_feature_collection",
        "earth_engine.mapbiomas_image",
        "grid.crs",
        "grid.scale_m",
        "grid.anchor_asset",
        "grid.boundary_asset",
        "grid.resampling_method",
        "analysis.years.start",
        "analysis.years.end",
        "analysis.forest_codes",
        "analysis.excluded_mapbiomas_codes",
        "analysis.spatial_units.asset",
        "analysis.spatial_units.id_property",
        "analysis.spatial_units.name_property",
        "analysis.scenarios",
        "references",
        "udef_a.ctrees_fcbm_reference",
        "udef_a.ctrees_snapshot_references.t1",
        "udef_a.ctrees_snapshot_references.t2",
        "udef_a.ctrees_snapshot_references.t3",
        "google.presentation_id",
        "outputs.root",
        "outputs.directories",
    ):
        _require(raw, key)

    start_year = int(raw["analysis"]["years"]["start"])
    end_year = int(raw["analysis"]["years"]["end"])
    if start_year > end_year:
        raise ValueError("analysis.years.start must be less than or equal to analysis.years.end.")
    if int(raw["grid"]["scale_m"]) != int(raw["earth_engine"]["scale_native_m"]):
        raise ValueError("grid.scale_m must match earth_engine.scale_native_m.")
    if str(raw["grid"]["resampling_method"]) != "nearestNeighbor":
        raise ValueError("grid.resampling_method must be nearestNeighbor for categorical rasters.")

    scenarios = [_load_scenario(item, start_year, end_year) for item in raw["analysis"]["scenarios"]]

    root = Path(output_root) if output_root else Path(raw["outputs"]["root"])
    output_names = raw["outputs"]["directories"]
    output_directories = {name: root / subdir for name, subdir in output_names.items()}

    return ProjectSettings(
        path=settings_path,
        raw=raw,
        scenarios=scenarios,
        output_root=root,
        output_directories=output_directories,
    )


def ensure_output_directories(settings: ProjectSettings) -> None:
    """Create all configured output directories."""
    for path in settings.output_directories.values():
        path.mkdir(parents=True, exist_ok=True)


def _load_scenario(item: dict[str, Any], start_year: int, end_year: int) -> Scenario:
    scenario = Scenario(
        identifier=str(item["id"]),
        threshold_percent=float(item["threshold_percent"]),
        start_year=int(item["start_year"]),
        end_year=int(item["end_year"]),
    )
    if not 0 < scenario.threshold_percent <= 100:
        raise ValueError(f"Scenario {scenario.identifier} has an invalid threshold.")
    if scenario.start_year < start_year or scenario.end_year > end_year:
        raise ValueError(f"Scenario {scenario.identifier} falls outside the study period.")
    if scenario.start_year > scenario.end_year:
        raise ValueError(f"Scenario {scenario.identifier} has an invalid year range.")
    return scenario


def _require(raw: dict[str, Any], dotted_key: str) -> None:
    node: Any = raw
    for part in dotted_key.split("."):
        if not isinstance(node, dict) or part not in node:
            raise ValueError(f"Missing required setting: {dotted_key}")
        node = node[part]
