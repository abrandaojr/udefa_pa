"""Expected workflow products that can be known without running Earth Engine."""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any

from .constants import CROSS_TABULATION_YEARS, PRIMARY_MAPBIOMAS_YEARS
from .settings import Scenario


@dataclass(frozen=True)
class ProductSpec:
    """One expected pipeline product."""

    name: str
    kind: str
    required: bool
    description: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_YEAR_TO_TX: dict[int, str] = {
    1985: "T0_1985",
    2009: "T1_2009",
    2013: "T2_2013",
    2018: "T3_2018",
    2024: "T4_2024",
}

_PERIOD_LABEL: dict[tuple[int, int], str] = {
    (1985, 2009): "Hist_1985_2009",
    (2009, 2013): "Cal_2009_2013",
    (2013, 2018): "Con_2013_2018",
    (2018, 2024): "T3T4_2018_2024",
    (1985, 2024): "T0T4_1985_2024",
}


def expected_csv_exports(scenarios: list[Scenario], reference_names: list[str]) -> list[ProductSpec]:
    """Return required CSV exports consumed by local analysis."""
    specs: list[ProductSpec] = []
    for scenario in scenarios:
        specs.append(
            ProductSpec(
                name=f"Area_30m_{scenario.label}",
                kind="csv",
                required=True,
                description=f"Area by persistence class for scenario {scenario.label}",
            )
        )
        for reference_name in reference_names:
            specs.append(
                ProductSpec(
                    name=f"XTab_30m_{scenario.label}_x_{reference_name}",
                    kind="csv",
                    required=True,
                    description=f"Cross-tabulation for scenario {scenario.label} and {reference_name}",
                )
            )

    for year in CROSS_TABULATION_YEARS:
        specs.extend(
            [
                ProductSpec(
                    name=f"AllClass_XTab_30m_CTrees_{year}_x_MapBiomas_LULC_{year}",
                    kind="csv",
                    required=False,
                    description=f"All-class CTrees x MapBiomas cross-tabulation for {year}",
                ),
                ProductSpec(
                    name=f"Binary_XTab_30m_CTrees_{year}_x_MapBiomas_Binary_{year}",
                    kind="csv",
                    required=False,
                    description=f"Binary CTrees x MapBiomas cross-tabulation for {year}",
                ),
                ProductSpec(
                    name=f"SpatialDisagreement_30m_CTrees_{year}_x_MapBiomas_Binary_{year}",
                    kind="csv",
                    required=False,
                    description=f"Spatial disagreement by unit for {year}",
                ),
            ]
        )

    for start_year, end_year in zip(CROSS_TABULATION_YEARS, CROSS_TABULATION_YEARS[1:]):
        specs.extend(
            [
                ProductSpec(
                    name=f"ChangeAgreement_30m_CTrees_{start_year}_{end_year}_x_MapBiomas_{start_year}_{end_year}",
                    kind="csv",
                    required=False,
                    description=f"Forest-change agreement for {start_year}-{end_year}",
                ),
                ProductSpec(
                    name=f"ChangeAreaTimeSeries_30m_CTrees_x_MapBiomas_{start_year}_{end_year}",
                    kind="csv",
                    required=False,
                    description=f"Forest-loss area time series for {start_year}-{end_year}",
                ),
            ]
        )

    specs.append(
        ProductSpec(
            name="TemporalReversal_30m_CTrees_x_MapBiomas",
            kind="csv",
            required=False,
            description="Temporal reversal area table",
        )
    )
    specs.append(
        ProductSpec(
            name="ChangeArea_ForestToNonForest_30m",
            kind="csv",
            required=False,
            description="Forest-to-nonforest change-area export",
        )
    )
    return specs


def expected_raster_products(settings: dict[str, Any], scenarios: list[Scenario]) -> list[ProductSpec]:
    """Return raster products whose names can be known without EE images."""
    specs: list[ProductSpec] = [
        ProductSpec(
            name=_with_projection_suffix("UDefA_ValidMask", settings),
            kind="raster",
            required=False,
            description="Valid analysis area mask",
        )
    ]

    for year in PRIMARY_MAPBIOMAS_YEARS:
        tx = _YEAR_TO_TX.get(year, f"T{year}")
        specs.extend(
            [
                ProductSpec(
                    name=_with_projection_suffix(f"UDefA_MB_LULC_{tx}", settings),
                    kind="raster",
                    required=True,
                    description=f"MapBiomas land-cover classes, {year}",
                ),
                ProductSpec(
                    name=_with_projection_suffix(f"UDefA_MB_Forest_{tx}", settings),
                    kind="raster",
                    required=False,
                    description=f"MapBiomas binary forest/non-forest, {year}",
                ),
            ]
        )

    for scenario in scenarios:
        specs.append(
            ProductSpec(
                name=_with_projection_suffix(f"UDefA_MB_Persistence_Scen{_safe_name(scenario.label)}", settings),
                kind="raster",
                required=True,
                description=f"MapBiomas forest-persistence scenario {scenario.label}",
            )
        )

    for start_year, end_year in zip(PRIMARY_MAPBIOMAS_YEARS, PRIMARY_MAPBIOMAS_YEARS[1:]):
        period = _PERIOD_LABEL.get((start_year, end_year), f"{start_year}_{end_year}")
        specs.append(
            ProductSpec(
                name=_with_projection_suffix(f"UDefA_MB_ForestChange4_{period}", settings),
                kind="raster",
                required=False,
                description=f"MapBiomas 4-class forest change, {start_year}-{end_year}",
            )
        )

    for start_year, end_year in zip(CROSS_TABULATION_YEARS, CROSS_TABULATION_YEARS[1:]):
        period = _PERIOD_LABEL.get((start_year, end_year), f"{start_year}_{end_year}")
        specs.extend(
            [
                ProductSpec(
                    name=_with_projection_suffix(f"UDefA_Ct_ForestChange4_{period}", settings),
                    kind="raster",
                    required=False,
                    description=f"CTrees 4-class forest change, {start_year}-{end_year}",
                ),
                ProductSpec(
                    name=_with_projection_suffix(f"UDefA_Ct_MB_Agreement_{period}", settings),
                    kind="raster",
                    required=False,
                    description=f"CTrees vs MapBiomas forest-loss agreement, {start_year}-{end_year}",
                ),
            ]
        )
    return specs


def _projection_suffix(settings: dict[str, Any]) -> str:
    grid = settings.get("grid", {})
    earth_engine = settings.get("earth_engine", {})
    crs = str(grid.get("crs") or earth_engine.get("crs") or "unknown_crs")
    scale = grid.get("scale_m") or earth_engine.get("scale_native_m") or "unknown"
    return f"{crs.replace(':', '_').replace('/', '_')}_{scale}m"


def _with_projection_suffix(name: str, settings: dict[str, Any]) -> str:
    suffix = _projection_suffix(settings)
    clean_name = _safe_name(name)
    if clean_name.endswith(suffix):
        return clean_name
    return f"{clean_name}_{suffix}"


def _safe_name(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in str(value))
