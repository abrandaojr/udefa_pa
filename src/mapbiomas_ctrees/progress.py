"""Single-file workflow progress table for VS Code."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
import shutil
import textwrap
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
    """Write and print a compact progress table after each workflow update."""

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

    def __init__(self, path: Path, print_to_terminal: bool = True) -> None:
        self.path = path
        self.print_to_terminal = print_to_terminal
        self.rows: OrderedDict[str, ProgressRow] = OrderedDict(
            (step, ProgressRow(step=step)) for step in self.DEFAULT_STEPS
        )
        self.audit_panel = AuditPanel()
        self.write(print_to_terminal=False)

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

    def write(self, print_to_terminal: bool = True) -> None:
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
        if self.print_to_terminal and print_to_terminal:
            print("\n".join(self._terminal_lines()), flush=True)

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

    def _terminal_lines(self) -> list[str]:
        width = max(80, shutil.get_terminal_size((120, 20)).columns)
        detail_width = max(24, width - 68)
        separator = (
            "+"
            + "-" * 22
            + "+"
            + "-" * 14
            + "+"
            + "-" * 12
            + "+"
            + "-" * detail_width
            + "+"
        )
        lines = [
            "",
            "Progresso do workflow",
            separator,
            _terminal_row(("Etapa", "Status", "Progresso", "Detalhe"), detail_width),
            separator,
        ]
        for row in self.rows.values():
            lines.append(
                _terminal_row(
                    (
                        row.step,
                        row.status,
                        row.progress,
                        row.detail or row.updated_at,
                    ),
                    detail_width,
                )
            )
        lines.append(separator)
        lines.extend(self._terminal_audit_lines(width))
        return lines

    def _terminal_audit_lines(self, width: int) -> list[str]:
        panel = self.audit_panel
        if not panel.summary and not panel.issues and not panel.required_rows:
            return []
        lines = ["Qualidade dos dados"]
        summary_keys = ("total", "required", "required_not_ready", "ready", "partial", "missing", "invalid", "optional_missing", "observed")
        summary = [
            f"{key}={panel.summary[key]}"
            for key in summary_keys
            if key in panel.summary
        ]
        if summary:
            lines.append("  " + ", ".join(summary))
        if panel.issues:
            lines.append("Problemas ativos:")
            for issue in panel.issues[:5]:
                lines.append("  - " + _terminal_text(issue, max(40, width - 6)))
            if len(panel.issues) > 5:
                lines.append(f"  - ... mais {len(panel.issues) - 5} problema(s)")
        if panel.required_rows:
            lines.append("Produtos obrigatorios pendentes/principais:")
            for name, kind, status, stages in panel.required_rows[:8]:
                detail = f"{name} [{kind}/{status}] {stages}"
                lines.append("  - " + _terminal_text(detail, max(40, width - 6)))
            if len(panel.required_rows) > 8:
                lines.append(f"  - ... mais {len(panel.required_rows) - 8} produto(s)")
        if panel.state_path or panel.audit_path:
            lines.append(f"Arquivos: estado={panel.state_path} auditoria={panel.audit_path}")
        return lines


def _cell(value: str) -> str:
    return str(value).replace("|", "/").replace("\r", " ").replace("\n", " ").strip()


def _terminal_row(values: tuple[str, str, str, str], detail_width: int) -> str:
    step, status, progress, detail = values
    return (
        "| "
        + _terminal_text(step, 20).ljust(20)
        + " | "
        + _terminal_text(status, 12).ljust(12)
        + " | "
        + _terminal_text(progress, 10).rjust(10)
        + " | "
        + _terminal_text(detail, detail_width - 2).ljust(detail_width - 2)
        + " |"
    )


def _terminal_text(value: str, width: int) -> str:
    text = _cell(value)
    if len(text) <= width:
        return text
    return textwrap.shorten(text, width=width, placeholder="...")


def _stage_summary(stages: dict[str, Any]) -> str:
    parts: list[str] = []
    for name, state in stages.items():
        parts.append(f"{name}={getattr(state, 'status', '-')}")
    return "; ".join(parts)
