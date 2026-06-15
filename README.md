# UDef-ARP

[![Python](https://img.shields.io/badge/python-3.9--3.10-blue.svg)](https://www.python.org/)
[![CI](https://github.com/abrandaojr/udefa/actions/workflows/ci.yml/badge.svg)](https://github.com/abrandaojr/udefa/actions/workflows/ci.yml)
[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-green.svg)](LICENSE)

This repository is a maintained fork of
[ClarkCGA/UDef-ARP](https://github.com/ClarkCGA/UDef-ARP), the original
UDef-ARP repository developed by Clark Labs in collaboration with TerraCarbon.
This fork preserves the original GPLv3 license, credits, documentation assets,
and application structure, while adding repository polish, CI checks, and a
documented fix for modeling region ID overflow.

Unplanned Deforestation Allocated Risk Modeling and Mapping Procedure
(UDef-ARP) is a Windows desktop application for implementing the modeling and
mapping workflow associated with Verra's VT0007 Unplanned Deforestation
Allocation tool:
[VT0007 Unplanned Deforestation Allocation v1.0](https://verra.org/wp-content/uploads/2024/02/VT0007-Unplanned-Deforestation-Allocation-v1.0.pdf).

UDef-ARP was developed by Clark Labs in collaboration with TerraCarbon. It is
used together with a raster-capable GIS for preparing inputs and reviewing
outputs. The final output is an expected forest-loss risk map expressed in
hectares per pixel per year.

<p align="center">
  <img src="data/stage.PNG" alt="Fitting and prediction phases for the VT0007 workflow">
  <br>
  <em>Fitting and prediction phases, testing stages, and application stages from the VT0007 workflow.</em>
</p>

## What The Application Does

- Builds 30-class vulnerability maps from forest-edge distance or alternative model outputs
- Allocates unplanned deforestation risk across administrative divisions nested within a jurisdiction
- Supports Calibration Period (CAL), Confirmation Period (CNF), Historical Reference Period (HRP), and Validity Period (VP) workflows
- Produces maps in GeoTIFF or TerrSet raster formats
- Provides tools for comparing the benchmark procedure against alternative empirical models
- Includes PDF documentation for each major workflow stage

The benchmark model is intentionally simple. It uses distance from the forest
edge and a map of administrative divisions to estimate expected deforestation
density with a relative-frequency approach. Alternative empirical models can be
tested and used when they outperform the benchmark according to the VT0007
procedure.

## Project Status

- Platform: Windows only
- Interface: PyQt5 desktop GUI
- Raster engine: GDAL
- Primary environment: Conda
- Development status: active, with updates expected

Only limited input bulletproofing has been implemented. Users should read the
VT0007 documentation carefully and verify all input rasters before running the
workflow.

## Source And Attribution

- Original source repository:
  [ClarkCGA/UDef-ARP](https://github.com/ClarkCGA/UDef-ARP)
- Original developers: Clark Labs, Clark University
- Collaborating organization: TerraCarbon
- Upstream project: Unplanned Deforestation Allocated Risk Modeling and Mapping Procedure (UDef-ARP)
- Upstream reference checked for this fork: `ClarkCGA/UDef-ARP` tag `v2.14.1`, commit `14acdaf`
- Protocol reference:
  [Verra VT0007 Unplanned Deforestation Allocation v1.0](https://verra.org/wp-content/uploads/2024/02/VT0007-Unplanned-Deforestation-Allocation-v1.0.pdf)
- License: GNU General Public License v3

All core application code, GUI assets, fonts, documentation PDFs, logos, and
sample workflow materials originate from the upstream UDef-ARP project unless
otherwise stated. Changes made in this fork are intended to preserve provenance
and improve maintainability, documentation, and reproducibility.

To compare this fork with the upstream source:

```powershell
git remote add upstream https://github.com/ClarkCGA/UDef-ARP.git
git fetch upstream --tags
git log --oneline upstream/main..main
git diff upstream/main...main
```

## Important Fix In This Fork

### Modeling Region ID Overflow

Modeling region IDs are computed as:

```text
vulnerability_class * 1000 + admin_division_id
```

The original implementation cast these arrays to `numpy.int16`, which can
silently overflow above `32,767`. For jurisdictions with many administrative
divisions, this can corrupt the relative-frequency table and downstream outputs.

This fork casts `tabulation_bin_id_HRP` and `tabulation_bin_id_VP` to
`numpy.int32` and writes `GDT_Int32` rasters. That preserves valid modeling
region IDs for much larger jurisdictions.

## Repository Layout

```text
UDef-ARP.py                 Main PyQt5 desktop application
udef_auto.py                YAML-driven automation runner
allocation_tool.py          Allocation and relative-frequency routines
vulnerability_map.py        Vulnerability map generation routines
model_evaluation.py         Model comparison and evaluation routines
UDef-ARP_conda_env.yml      Conda environment definition
data/                       UI files, images, icons, and logos
doc/                        Workflow documentation PDFs
font/                       Application font assets
examples/                   Example automation configuration files
```

## Requirements

- Windows
- Anaconda or Miniconda
- Python 3.9 to 3.10
- GDAL
- PyQt5
- NumPy
- pandas
- GeoPandas
- SciPy
- Shapely
- Matplotlib
- PyYAML
- Raster inputs in an equal-area projection

Large jurisdictions can require substantial RAM because raster inputs are held
in memory during processing. A minimum display resolution of 1920 x 1080 is
recommended; 4K is preferred.

## Installation

Open Anaconda Prompt on Windows:

```powershell
git clone https://github.com/abrandaojr/udefa.git
cd udefa
conda env create -f UDef-ARP_conda_env.yml
conda activate udefarp
```

Run the application:

```powershell
python UDef-ARP.py
```

The GUI can also be launched from a Python IDE after activating the `udefarp`
environment.

## Automatic Workflow Mode

This fork adds a configuration-driven runner for users who want to enter all
inputs once and let the system generate the selected outputs automatically.
The original GUI is unchanged.

Copy the example configuration:

```powershell
copy examples\auto_config.yml my_project.yml
notepad my_project.yml
```

Fill in `working_directory`, raster inputs, expected deforestation, output
names, and the stages you want to run. Paths can be absolute or relative to
`working_directory`.

Run a validation pass first:

```powershell
python udef_auto.py my_project.yml --dry-run
```

Run the full automated workflow:

```powershell
python udef_auto.py my_project.yml --summary my_project_summary.json
```

Supported automation stages:

- `nrt`
- `vulnerability_distance`
- `vulnerability_alternative`
- `fit`
- `cnf`
- `vp`
- `model_evaluation`

The runner uses the same processing classes as the GUI: `VulnerabilityMap`,
`AllocationTool`, and `ModelEvaluation`.

When stages are run in sequence, the runner can reuse generated values. For
example, `vulnerability_distance` can use the NRT calculated by a previous
`nrt` stage, and `cnf` / `vp` can use the relative-frequency CSV generated by a
previous `fit` stage.

## Input Data Rules

UDef-ARP accepts raster inputs in:

- GeoTIFF `.tif`
- TerrSet `.rst`

All map inputs must:

- Use an equal-area projection
- Be co-registered
- Use the same spatial resolution
- Have the same number of rows and columns
- Follow the required binary or class-value conventions described in the VT0007 workflow documentation

<p align="center">
  <img src="data/intro_screen.png" alt="UDef-ARP graphical user interface">
</p>

## Documentation

Detailed PDF guides are included in `doc/`:

- `UDef-ARP_Introduction.pdf`
- Calibration fitting guides
- Confirmation prediction guides
- Historical reference period fitting guides
- Validity period application guides

The GUI also links to the relevant documentation from each workflow screen.

## Quality Checks

The repository includes a lightweight GitHub Actions workflow that checks Python
syntax without launching the GUI:

```powershell
python -m compileall -q UDef-ARP.py udef_auto.py allocation_tool.py vulnerability_map.py model_evaluation.py
python udef_auto.py --help
```

Runtime validation still requires the full Windows Conda environment and
representative raster inputs.

## License

This project is distributed under the GNU General Public License v3. See
[`LICENSE`](LICENSE).

See [`NOTICE.md`](NOTICE.md) for upstream attribution and fork-specific notes.
