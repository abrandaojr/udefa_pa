"""Single-file workflow progress table for VS Code."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass
class ProgressRow:
    step: str
    status: str = "Aguardando"
    progress: str = "-"
    detail: str = ""
    updated_at: str = "-"


@dataclass
class AuditPanel:
    summary: dict[str, int] = field(default_factory=dict)
    issues: list[str] = field(default_factory=list)
    required_rows: list[tuple[str, str, str, str]] = field(default_factory=list)
    state_path: str = ""
    audit_path: str = ""
    updated_at: str = "-"


class WorkflowProgress:
    """Write a compact Markdown progress table after each workflow update."""

    DEFAULT_STEPS = (
        "Auditoria",
        "Credenciais Google",
        "CSV exports",
        "Sync rasters",
        "Download raster",
        "Mosaicos GeoTIFF",
        "Conversao IDRISI",
        "Earth Engine",
        "Analise local",
        "Relatorios",
    )

    def __init__(self, path: Path) -> None:
        self.path = path
        self.rows: OrderedDict[str, ProgressRow] = OrderedDict(
            (step, ProgressRow(step=step)) for step in self.DEFAULT_STEPS
        )
        self.audit_panel = AuditPanel()
        self.write()

    def update(self, step: str, status: str, detail: str = "", progress: str = "-") -> None:
        if step not in self.rows:
            self.rows[step] = ProgressRow(step=step)
        row = self.rows[step]
        row.status = status
        row.progress = progress
        row.detail = detail
        row.updated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.write()

    def update_audit(self, audit: Any, state_path: Path, audit_path: Path) -> None:
        required_products = [
            product
            for product in audit.products.values()
            if product.required
        ]
        pending = [product for product in required_products if product.status != "ready"]
        rows = pending or required_products
        rows = sorted(rows, key=lambda product: (product.kind, product.status, product.name))[:40]
        self.audit_panel = AuditPanel(
            summary=dict(audit.summary),
            issues=list(audit.issues[:20]),
            required_rows=[
                (
                    product.name,
                    product.kind,
                    product.status,
                    _stage_summary(product.stages),
                )
                for product in rows
            ],
            state_path=str(state_path),
            audit_path=str(audit_path),
            updated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )
        self.write()

    def write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            "# Progresso do workflow",
            "",
            "Abra este arquivo no VS Code para acompanhar a execucao em um unico lugar.",
            "",
            "| Etapa | Status | Progresso | Detalhe | Atualizado |",
            "|---|---:|---:|---|---:|",
        ]
        for row in self.rows.values():
            lines.append(
                "| "
                + " | ".join(
                    (
                        _cell(row.step),
                        _cell(row.status),
                        _cell(row.progress),
                        _cell(row.detail),
                        _cell(row.updated_at),
                    )
                )
                + " |"
            )
        self._append_audit_panel(lines)
        self.path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _append_audit_panel(self, lines: list[str]) -> None:
        panel = self.audit_panel
        if not panel.summary and not panel.issues and not panel.required_rows:
            return
        lines.extend(
            [
                "",
                "## Qualidade dos dados",
                "",
                f"Atualizado em: {_cell(panel.updated_at)}",
                "",
                "| Indicador | Valor |",
                "|---|---:|",
            ]
        )
        for key in ("total", "required", "required_not_ready", "ready", "partial", "missing", "invalid", "optional_missing", "observed"):
            if key in panel.summary:
                lines.append(f"| {_cell(key)} | {panel.summary[key]} |")
        lines.extend(
            [
                "",
                "| Arquivo | Caminho |",
                "|---|---|",
                f"| Estado JSON | {_cell(panel.state_path)} |",
                f"| Auditoria detalhada | {_cell(panel.audit_path)} |",
            ]
        )
        if panel.issues:
            lines.extend(["", "## Problemas ativos", "", "| Problema |", "|---|"])
            for issue in panel.issues:
                lines.append(f"| {_cell(issue)} |")
        if panel.required_rows:
            lines.extend(
                [
                    "",
                    "## Produtos obrigatorios",
                    "",
                    "| Produto | Tipo | Status | Etapas |",
                    "|---|---|---:|---|",
                ]
            )
            for name, kind, status, stages in panel.required_rows:
                lines.append(f"| {_cell(name)} | {_cell(kind)} | {_cell(status)} | {_cell(stages)} |")


def _cell(value: str) -> str:
    return str(value).replace("|", "/").replace("\r", " ").replace("\n", " ").strip()


def _stage_summary(stages: dict[str, Any]) -> str:
    parts: list[str] = []
    for name, state in stages.items():
        parts.append(f"{name}={getattr(state, 'status', '-')}")
    return "; ".join(parts)
