"""Persistent local state and data-quality audit for workflow artifacts."""

from __future__ import annotations

import csv
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from .catalog import ProductSpec
from .raster_naming import raster_product_stem, raster_semantic_key


@dataclass
class ArtifactState:
    """Observed state for one artifact stage."""

    status: str
    path: str | None = None
    size_bytes: int | None = None
    modified_at: str | None = None
    detail: str = ""


@dataclass
class ProductState:
    """Expected product plus its local artifact stages."""

    name: str
    kind: str
    required: bool
    description: str
    status: str
    stages: dict[str, ArtifactState] = field(default_factory=dict)


@dataclass
class PipelineAudit:
    """Summary of expected and observed workflow state."""

    generated_at: str
    products: dict[str, ProductState]
    issues: list[str]
    summary: dict[str, int]

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "summary": self.summary,
            "issues": self.issues,
            "products": {
                name: {
                    "name": product.name,
                    "kind": product.kind,
                    "required": product.required,
                    "description": product.description,
                    "status": product.status,
                    "stages": {stage: asdict(state) for stage, state in product.stages.items()},
                }
                for name, product in sorted(self.products.items())
            },
        }


def audit_pipeline_state(
    *,
    expected_csvs: list[ProductSpec],
    expected_rasters: list[ProductSpec],
    table_directory: Path,
    geotiff_tile_directory: Path,
    geotiff_directory: Path,
    idrisi_directory: Path,
) -> PipelineAudit:
    """Build a local audit without requiring Google Drive or Earth Engine."""
    products: dict[str, ProductState] = {}
    issues: list[str] = []

    for spec in expected_csvs:
        path = _find_single_artifact(table_directory, spec.name, ".csv", issues)
        stage = _csv_artifact_state(path, missing_detail="Missing CSV")
        products[spec.name] = ProductState(
            name=spec.name,
            kind=spec.kind,
            required=spec.required,
            description=spec.description,
            status=_product_status(spec.required, [stage]),
            stages={"csv": stage},
        )

    for spec in expected_rasters:
        tile_paths = _find_geotiff_group(geotiff_tile_directory, spec.name)
        if not tile_paths:
            tile_paths = _find_equivalent_geotiff_group(geotiff_tile_directory, spec.name)
        mosaic_path = _find_equivalent_single_raster(
            geotiff_directory,
            spec.name,
            {".tif", ".tiff"},
            preferred_exact=geotiff_directory / f"{spec.name}.tif",
        )
        rst_path = _find_equivalent_single_raster(
            idrisi_directory,
            spec.name,
            {".rst"},
            preferred_exact=idrisi_directory / f"{spec.name}.rst",
        )
        rdc_path = _find_equivalent_single_raster(
            idrisi_directory,
            spec.name,
            {".rdc"},
            preferred_exact=idrisi_directory / f"{spec.name}.rdc",
        )
        pal_path = _find_equivalent_single_raster(
            idrisi_directory,
            spec.name,
            {".pal"},
            preferred_exact=idrisi_directory / f"{spec.name}.pal",
        )
        stages = {
            "download": _multi_artifact_state(tile_paths, missing_detail="Missing GeoTIFF tile"),
            "mosaic": _artifact_state(mosaic_path, missing_detail="Missing mosaic"),
            "idrisi_rst": _artifact_state(rst_path, missing_detail="Missing IDRISI .rst"),
            "idrisi_rdc": _artifact_state(rdc_path, missing_detail="Missing IDRISI .rdc"),
            "idrisi_pal": _artifact_state(pal_path, missing_detail="Missing IDRISI .pal"),
        }
        products[spec.name] = ProductState(
            name=spec.name,
            kind=spec.kind,
            required=spec.required,
            description=spec.description,
            status=_product_status(
                spec.required,
                [
                    stages["download"],
                    stages["mosaic"],
                    stages["idrisi_rst"],
                    stages["idrisi_rdc"],
                    stages["idrisi_pal"],
                ],
            ),
            stages=stages,
        )

    _add_observed_csvs(products, table_directory)
    _add_observed_rasters(products, geotiff_tile_directory, geotiff_directory, idrisi_directory)
    _add_residual_file_issues(issues, geotiff_tile_directory, geotiff_directory, idrisi_directory)
    _add_invalid_artifact_issues(issues, products)

    summary = _summarize(products)
    return PipelineAudit(
        generated_at=datetime.now(timezone.utc).isoformat(),
        products=products,
        issues=issues,
        summary=summary,
    )


def write_pipeline_state(audit: PipelineAudit, path: Path) -> Path:
    """Write the audit as formatted JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(audit.to_dict(), indent=2, sort_keys=True), encoding="utf-8")
    return path


def write_pipeline_audit_markdown(audit: PipelineAudit, path: Path) -> Path:
    """Write a compact Markdown table for VS Code."""
    path.parent.mkdir(parents=True, exist_ok=True)
    required = sorted(
        (product for product in audit.products.values() if product.required),
        key=lambda item: (item.status == "ready", item.kind, item.name),
    )
    optional_pending = sorted(
        (
            product
            for product in audit.products.values()
            if not product.required and product.status in {"optional_missing", "partial", "invalid"}
        ),
        key=lambda item: (item.kind, item.name),
    )
    observed = sorted(
        (product for product in audit.products.values() if product.status == "observed"),
        key=lambda item: (item.kind, item.name),
    )
    lines = [
        "# Pipeline Audit",
        "",
        f"Generated at: {audit.generated_at}",
        "",
        "| Metric | Value |",
        "|---|---:|",
    ]
    for key, value in sorted(audit.summary.items()):
        lines.append(f"| {key} | {value} |")
    if audit.issues:
        lines.extend(["", "## Issues", ""])
        lines.extend(f"- {issue}" for issue in audit.issues)
    lines.extend(
        [
            "",
            "## Required Products",
            "",
            "| Product | Type | Status | Stages |",
            "|---|---|---:|---|",
        ]
    )
    for product in required:
        lines.append(
            f"| {_cell(product.name)} | {_cell(product.kind)} | {_cell(product.status)} | "
            f"{_cell(_stage_summary(product.stages))} |"
        )
    if optional_pending:
        lines.extend(
            [
                "",
                "## Pending Optional Products",
                "",
                "| Product | Type | Status | Stages |",
                "|---|---|---:|---|",
            ]
        )
        for product in optional_pending[:100]:
            lines.append(
                f"| {_cell(product.name)} | {_cell(product.kind)} | {_cell(product.status)} | "
                f"{_cell(_stage_summary(product.stages))} |"
            )
        if len(optional_pending) > 100:
            lines.append(f"| ... | ... | ... | {len(optional_pending) - 100} product(s) omitted |")
    if observed:
        lines.extend(
            [
                "",
                "## Observed Artifacts Outside the Catalog",
                "",
                "| Product | Type | Stages |",
                "|---|---|---|",
            ]
        )
        for product in observed[:100]:
            lines.append(f"| {_cell(product.name)} | {_cell(product.kind)} | {_cell(_stage_summary(product.stages))} |")
        if len(observed) > 100:
            lines.append(f"| ... | ... | {len(observed) - 100} artifact(s) omitted; see pipeline_state.json |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _find_single_artifact(directory: Path, stem: str, suffix: str, issues: list[str]) -> Path | None:
    matches = sorted(directory.glob(f"{stem}*{suffix}")) if directory.exists() else []
    if len(matches) > 1:
        issues.append(f"Ambiguous artifact for {stem}: {len(matches)} file(s) in {directory}")
    return matches[0] if matches else None


def _find_geotiff_group(directory: Path, product_name: str) -> list[Path]:
    if not directory.exists():
        return []
    return sorted(
        path
        for path in directory.iterdir()
        if path.is_file()
        and path.suffix.lower() in {".tif", ".tiff"}
        and (path.stem == product_name or path.stem.startswith(f"{product_name}-") or path.stem.startswith(f"{product_name}_"))
    )


def _artifact_state(path: Path | None, *, missing_detail: str) -> ArtifactState:
    if path is None:
        return ArtifactState(status="missing", detail=missing_detail)
    stat = path.stat()
    if stat.st_size <= 0:
        return ArtifactState(
            status="invalid",
            path=str(path),
            size_bytes=stat.st_size,
            modified_at=_mtime(path),
            detail="empty file",
        )
    return ArtifactState(status="ready", path=str(path), size_bytes=stat.st_size, modified_at=_mtime(path))


def _csv_artifact_state(path: Path | None, *, missing_detail: str) -> ArtifactState:
    state = _artifact_state(path, missing_detail=missing_detail)
    if path is None or state.status != "ready":
        return state
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.reader(handle)
            header = next(reader, None)
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        state.status = "invalid"
        state.detail = f"Unreadable CSV: {exc}"
        return state
    if not header or not any(str(column).strip() for column in header):
        state.status = "invalid"
        state.detail = "CSV without a header"
    return state


def _multi_artifact_state(paths: list[Path], *, missing_detail: str) -> ArtifactState:
    if not paths:
        return ArtifactState(status="missing", detail=missing_detail)
    total_size = sum(path.stat().st_size for path in paths)
    status = "invalid" if total_size <= 0 else "ready"
    detail = f"{len(paths)} file(s)"
    if status == "invalid":
        detail = f"{detail}; zero total size"
    newest = max(paths, key=lambda path: path.stat().st_mtime)
    return ArtifactState(
        status=status,
        path=str(paths[0]) if len(paths) == 1 else str(paths[0].parent),
        size_bytes=total_size,
        modified_at=_mtime(newest),
        detail=detail,
    )


def _product_status(required: bool, stages: list[ArtifactState]) -> str:
    if any(stage.status == "invalid" for stage in stages):
        return "invalid"
    if required and any(stage.status == "missing" for stage in stages[:1]):
        return "missing"
    if all(stage.status == "ready" for stage in stages):
        return "ready"
    if any(stage.status == "ready" for stage in stages):
        return "partial"
    return "missing" if required else "optional_missing"


def _add_observed_csvs(products: dict[str, ProductState], table_directory: Path) -> None:
    if not table_directory.exists():
        return
    known = set(products)
    for path in sorted(table_directory.glob("*.csv")):
        if any(path.stem == name or path.stem.startswith(f"{name}_") for name in known):
            continue
        products[f"observed:{path.stem}"] = ProductState(
            name=path.stem,
            kind="csv",
            required=False,
            description="CSV observed outside the initial catalog",
            status="observed",
            stages={"csv": _csv_artifact_state(path, missing_detail="")},
        )


def _find_equivalent_geotiff_group(directory: Path, expected_name: str) -> list[Path]:
    if not directory.exists():
        return []
    expected_key = raster_semantic_key(expected_name)
    return sorted(
        path
        for path in directory.iterdir()
        if path.is_file()
        and path.suffix.lower() in {".tif", ".tiff"}
        and raster_semantic_key(raster_product_stem(path)) == expected_key
    )


def _find_equivalent_single_raster(
    directory: Path,
    expected_name: str,
    suffixes: set[str],
    *,
    preferred_exact: Path,
) -> Path | None:
    if preferred_exact.exists():
        return preferred_exact
    if not directory.exists():
        return None
    expected_key = raster_semantic_key(expected_name)
    matches = sorted(
        path
        for path in directory.iterdir()
        if path.is_file()
        and path.suffix.lower() in suffixes
        and raster_semantic_key(path.stem) == expected_key
    )
    return matches[0] if matches else None


def _add_observed_rasters(
    products: dict[str, ProductState],
    geotiff_tile_directory: Path,
    geotiff_directory: Path,
    idrisi_directory: Path,
) -> None:
    known = set(products)
    observed: dict[str, ProductState] = {}
    for directory, stage_name, suffixes in (
        (geotiff_tile_directory, "download", {".tif", ".tiff"}),
        (geotiff_directory, "mosaic", {".tif", ".tiff"}),
        (idrisi_directory, "idrisi_rst", {".rst"}),
        (idrisi_directory, "idrisi_rdc", {".rdc"}),
        (idrisi_directory, "idrisi_pal", {".pal"}),
    ):
        if not directory.exists():
            continue
        for path in sorted(directory.iterdir()):
            if not path.is_file() or path.suffix.lower() not in suffixes:
                continue
            if any(path.stem == name or path.stem.startswith(f"{name}-") or path.stem.startswith(f"{name}_") for name in known):
                continue
            product = observed.setdefault(
                path.stem,
                ProductState(
                    name=path.stem,
                    kind="raster",
                    required=False,
                    description="Raster observed outside the initial catalog",
                    status="observed",
                    stages={},
                ),
            )
            product.stages[stage_name] = _artifact_state(path, missing_detail="")
    for name, product in sorted(observed.items()):
        products[f"observed:raster:{name}"] = product


def _add_residual_file_issues(issues: list[str], *directories: Path) -> None:
    for directory in directories:
        if not directory.exists():
            continue
        for path in sorted(directory.iterdir()):
            if path.is_file() and path.name.lower().endswith((".download", ".tmp")):
                issues.append(f"Residual temporary file: {path} ({path.stat().st_size} bytes)")


def _add_invalid_artifact_issues(issues: list[str], products: dict[str, ProductState]) -> None:
    for product in sorted(products.values(), key=lambda item: (item.kind, item.name)):
        for stage_name, state in product.stages.items():
            if state.status != "invalid":
                continue
            location = f" in {state.path}" if state.path else ""
            detail = f": {state.detail}" if state.detail else ""
            issues.append(f"Invalid artifact: {product.name} [{stage_name}]{location}{detail}")


def _summarize(products: dict[str, ProductState]) -> dict[str, int]:
    summary: dict[str, int] = {"total": len(products)}
    for product in products.values():
        summary[product.status] = summary.get(product.status, 0) + 1
        if product.required:
            summary["required"] = summary.get("required", 0) + 1
            if product.status != "ready":
                summary["required_not_ready"] = summary.get("required_not_ready", 0) + 1
    return summary


def _mtime(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()


def _cell(value: str) -> str:
    return str(value).replace("|", "/").replace("\r", " ").replace("\n", " ").strip()


def _stage_summary(stages: dict[str, ArtifactState]) -> str:
    return "; ".join(f"{name}={state.status}" for name, state in stages.items())
