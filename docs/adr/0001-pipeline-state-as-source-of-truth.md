# ADR 0001: Pipeline State As Source Of Truth

## Status

Accepted

## Context

The workflow manages remote Earth Engine exports, Google Drive downloads, local GeoTIFF mosaics, IDRISI conversions, CSV analysis, and publication outputs. Previously, progress and readiness were inferred mostly from files present in output folders.

That made reruns fragile: partial downloads, stale rasters, duplicated CSVs, and partially written IDRISI files could be mistaken for valid data.

## Decision

The pipeline will maintain an explicit local state inventory at `outputs/pipeline_state.json`.

The inventory records expected products, observed artifacts, local paths, stage status, sizes, timestamps, and data-quality issues. Human-readable status is rendered to `outputs/logs/pipeline_audit.md`.

Workflow stages should increasingly make decisions from this state rather than from ad hoc folder scans.

## Consequences

- Pipeline runs become easier to audit and resume.
- Data-quality checks can fail early with clear messages.
- Expensive stages such as IDRISI conversion can be skipped when outputs are current.
- Future refactors should preserve the state schema or migrate it explicitly.
