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
    status: str = "Waiting"
    progress: str = "-"
    detail: str = ""
    updated_at: str = "-"
    update_order: int = 0


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
        "Audit",
        "Google Credentials",
        "CSV exports",
        "Sync rasters",
        "Download raster",
        "GeoTIFF mosaics",
        "IDRISI conversion",
        "Earth Engine",
        "Local analysis",
        "Reports",
    )

    def __init__(self, path: Path, print_to_terminal: bool = True) -> None:
        self.path = path
        self.print_to_terminal = print_to_terminal
        self.rows: OrderedDict[str, ProgressRow] = OrderedDict(
            (step, ProgressRow(step=step)) for step in self.DEFAULT_STEPS
        )
        self.audit_panel = AuditPanel()
        self._update_counter = 0
        self.write(print_to_terminal=False)

    def update(self, step: str, status: str, detail: str = "", progress: str = "-") -> None:
        step = _translate_step(step)
        if step not in self.rows:
            self.rows[step] = ProgressRow(step=step)
        row = self.rows[step]
        row.status = _translate_status(status)
        row.progress = progress
        row.detail = _translate_text(detail)
        row.updated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._update_counter += 1
        row.update_order = self._update_counter
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
            issues=[_translate_text(issue) for issue in audit.issues[:20]],
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
            "# Workflow Progress",
            "",
            "Open this file in VS Code to monitor the workflow from a single location.",
            "",
            "| Step | Status | Progress | Detail | Updated |",
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
                "## Data Readiness",
                "",
                f"Updated at: {_cell(panel.updated_at)}",
                "",
                "| Indicator | Value |",
                "|---|---:|",
            ]
        )
        for key in ("total", "required", "required_not_ready", "ready", "partial", "missing", "invalid", "optional_missing", "observed"):
            if key in panel.summary:
                lines.append(f"| {_cell(_summary_label(key))} | {panel.summary[key]} |")
        lines.extend(
            [
                "",
                "| File | Path |",
                "|---|---|",
                f"| State JSON | {_cell(panel.state_path)} |",
                f"| Detailed audit | {_cell(panel.audit_path)} |",
            ]
        )
        if panel.issues:
            lines.extend(["", "## Active Issues", "", "| Issue |", "|---|"])
            for issue in panel.issues:
                lines.append(f"| {_cell(issue)} |")
        if panel.required_rows:
            lines.extend(
                [
                    "",
                    "## Required Products",
                    "",
                    "| Product | Type | Status | Stages |",
                    "|---|---|---:|---|",
                ]
            )
            for name, kind, status, stages in panel.required_rows:
                lines.append(f"| {_cell(name)} | {_cell(kind)} | {_cell(_translate_status(status))} | {_cell(stages)} |")

    def _terminal_lines(self) -> list[str]:
        width = max(80, shutil.get_terminal_size((120, 20)).columns)
        current = self._current_row()
        completed = sum(1 for row in self.rows.values() if row.status == "Ready")
        detail = _terminal_text(current.detail or current.updated_at, max(24, width - 21))
        lines = [
            "",
            "Workflow Update",
            f"Current: {current.step} - {current.status}",
            f"Detail: {detail}",
            f"Progress: {completed}/{len(self.rows)} steps complete",
        ]
        lines.extend(self._terminal_audit_lines(width))
        lines.append(f"Details: {self.path}")
        return lines

    def _current_row(self) -> ProgressRow:
        updated = [row for row in self.rows.values() if row.update_order]
        if not updated:
            return next(iter(self.rows.values()))
        return max(updated, key=lambda row: row.update_order)

    def _terminal_audit_lines(self, width: int) -> list[str]:
        panel = self.audit_panel
        if not panel.summary and not panel.issues and not panel.required_rows:
            return []
        lines: list[str] = []
        required = panel.summary.get("required", 0)
        pending = panel.summary.get("required_not_ready", 0)
        ready = max(0, required - pending)
        if required:
            lines.append(f"Required outputs: {ready}/{required} ready; {pending} still pending")
        elif panel.summary:
            lines.append(f"Products observed: {panel.summary.get('total', 0)}")
        if panel.issues:
            first_issue = _terminal_text(panel.issues[0], max(40, width - 20))
            lines.append(f"Active issues: {len(panel.issues)}; first issue: {first_issue}")
        if panel.required_rows:
            pending_note = _pending_note(panel.required_rows)
            if pending_note:
                lines.append(f"Next required work: {pending_note}")
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
    text = _cell(_translate_text(value))
    if len(text) <= width:
        return text
    return textwrap.shorten(text, width=width, placeholder="...")


def _stage_summary(stages: dict[str, Any]) -> str:
    parts: list[str] = []
    for name, state in stages.items():
        parts.append(f"{_stage_label(name)}={_translate_status(getattr(state, 'status', '-'))}")
    return "; ".join(parts)


def _translate_step(value: str) -> str:
    return {
        "Auditoria": "Audit",
        "Credenciais Google": "Google Credentials",
        "Mosaicos GeoTIFF": "GeoTIFF mosaics",
        "Conversao IDRISI": "IDRISI conversion",
        "Analise local": "Local analysis",
        "Relatorios": "Reports",
    }.get(value, value)


def _translate_status(value: str) -> str:
    return {
        "Aguardando": "Waiting",
        "Autenticando": "Authenticating",
        "Carregando": "Loading",
        "Pronto": "Ready",
        "Inicializado": "Initialized",
        "Submetendo": "Submitting",
        "Na fila": "Queued",
        "Baixando": "Downloading",
        "Baixado": "Downloaded",
        "Comprimindo": "Compressing",
        "Gerando": "Generating",
        "Convertendo": "Converting",
        "Verificando": "Checking",
        "Sem tiles": "No tiles",
        "Faltando": "Missing",
        "Rodando": "Running",
        "Status": "Status",
        "Drive": "Drive",
        "Compressing": "Compressing",
        "Downloaded": "Downloaded",
        "ready": "ready",
        "partial": "partial",
        "missing": "missing",
        "invalid": "invalid",
        "observed": "observed",
        "optional_missing": "optional missing",
    }.get(str(value), str(value))


def _translate_text(value: str) -> str:
    text = str(value)
    replacements = {
        "Artefato ambiguo": "Ambiguous artifact",
        "Artefato invalido": "Invalid artifact",
        "Arquivo temporario residual": "Residual temporary file",
        "arquivo(s)": "file(s)",
        "em ": "at ",
        "CSV ausente": "Missing CSV",
        "GeoTIFF tile ausente": "Missing GeoTIFF tile",
        "Mosaico ausente": "Missing mosaic",
        "IDRISI .rst ausente": "Missing IDRISI .rst",
        "IDRISI .rdc ausente": "Missing IDRISI .rdc",
        "IDRISI .pal ausente": "Missing IDRISI .pal",
        "arquivo vazio": "empty file",
        "CSV ilegivel": "Unreadable CSV",
        "CSV sem cabecalho": "CSV without a header",
        "tamanho total zero": "zero total size",
        "Preparando credenciais Google": "Preparing Google credentials",
        "Credenciais Google OK": "Google credentials are ready",
        "Inicializando Drive": "Initializing Drive",
        "Drive inicializado": "Drive initialized",
        "produto(s)": "product(s)",
        "obrigatorio(s) pendente(s)": "required pending",
        "problema(s)": "issue(s)",
        "submetidos": "submitted",
        "submetido": "submitted",
        "finalizados": "completed",
        "finalizado": "completed",
        "CSV locais": "local CSV files",
        "obrigatorios OK": "required products ready",
        "Recriando mosaicos": "Rebuilding mosaics",
        "mosaicos": "mosaics",
        "mosaico(s)": "mosaic(s)",
        "Cache local completo": "Local cache is complete",
        "convertidos agora": "converted during this run",
        "Verificando raster exports": "Checking raster exports",
        "Submetendo exports raster": "Submitting raster exports",
        "Preparando rasters faltantes": "Preparing missing rasters",
        "Atualizando tabela de status": "Updating the status table",
        "Computando tabelas finais": "Computing final tables",
        "Resultados, figuras e tabelas": "Results, figures, and tables",
        "Analise finalizada": "Analysis completed",
        "Docs, Word, Excel e Slides atualizados": "Docs, Word, Excel, and Slides updated",
    }
    for source, target in replacements.items():
        text = text.replace(source, target)
    return text


def _summary_label(value: str) -> str:
    return {
        "total": "total",
        "required": "required",
        "required_not_ready": "required not ready",
        "ready": "ready",
        "partial": "partial",
        "missing": "missing",
        "invalid": "invalid",
        "optional_missing": "optional missing",
        "observed": "observed",
    }.get(value, value)


def _stage_label(value: str) -> str:
    return {
        "download": "download",
        "mosaic": "mosaic",
        "idrisi_rst": "IDRISI .rst",
        "idrisi_rdc": "IDRISI .rdc",
        "idrisi_pal": "IDRISI .pal",
    }.get(value, value)


def _pending_stage_summary(stages: str) -> str:
    pending = [
        item.strip()
        for item in stages.split(";")
        if "missing" in item.lower() or "invalid" in item.lower()
    ]
    if pending:
        return "pending: " + "; ".join(pending)
    return stages


def _pending_note(required_rows: list[tuple[str, str, str, str]]) -> str:
    stages = " ".join(row[3].lower() for row in required_rows)
    notes: list[str] = []
    if "mosaic=missing" in stages:
        notes.append("build GeoTIFF mosaics")
    if "idrisi .rst=missing" in stages or "idrisi .rdc=missing" in stages:
        notes.append("convert GeoTIFF mosaics to IDRISI")
    if "download=missing" in stages:
        notes.append("download missing raster exports")
    if "csv=missing" in stages or "csv=invalid" in stages:
        notes.append("repair or download required CSV tables")
    if not notes:
        return ""
    if len(notes) == 1:
        return notes[0]
    if len(notes) == 2:
        return " and ".join(notes)
    return ", ".join(notes[:-1]) + ", and " + notes[-1]
