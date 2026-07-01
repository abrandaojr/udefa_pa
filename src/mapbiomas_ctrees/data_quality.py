"""Data-quality gates for local workflow execution."""

from __future__ import annotations

from dataclasses import dataclass

from .pipeline_state import PipelineAudit, ProductState


@dataclass(frozen=True)
class QualityGateResult:
    """Result of one data-quality gate."""

    ok: bool
    name: str
    failures: list[str]

    @property
    def summary(self) -> str:
        if self.ok:
            return f"{self.name}: OK"
        return f"{self.name}: {len(self.failures)} failure(s): " + "; ".join(self.failures[:10])


def required_csv_gate(audit: PipelineAudit) -> QualityGateResult:
    """Ensure required CSV inputs for local analysis are present and valid."""
    failures = [
        _product_failure(product)
        for product in audit.products.values()
        if product.kind == "csv" and product.required and product.status != "ready"
    ]
    failures.extend(issue for issue in audit.issues if "Artefato ambiguo" in issue)
    return QualityGateResult(ok=not failures, name="required_csv", failures=failures)


def required_raster_gate(audit: PipelineAudit) -> QualityGateResult:
    """Ensure required raster products reached the final local GeoTIFF/IDRISI state."""
    failures = [
        _product_failure(product)
        for product in audit.products.values()
        if product.kind == "raster" and product.required and product.status != "ready"
    ]
    return QualityGateResult(ok=not failures, name="required_raster", failures=failures)


def blocking_residual_files_gate(audit: PipelineAudit) -> QualityGateResult:
    """Surface temporary files that indicate an interrupted or active artifact write."""
    failures = [issue for issue in audit.issues if "Arquivo temporario residual" in issue]
    return QualityGateResult(ok=not failures, name="blocking_residual_files", failures=failures)


def assert_gate(result: QualityGateResult) -> None:
    if not result.ok:
        raise RuntimeError(result.summary)


def _product_failure(product: ProductState) -> str:
    stage_details = ", ".join(
        f"{stage}={state.status} ({state.detail})"
        for stage, state in product.stages.items()
        if state.status != "ready"
    )
    return f"{product.name}: {product.status}; {stage_details}"
