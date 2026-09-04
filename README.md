## Reproducibility

This repository implements an autonomous GeoAI risk analyst for the OASIS @ ACM SIGSPATIAL 2026 Student Challenge, Track A: Disaster Resilience and Vulnerability Analysis.

The system combines an LLM orchestrator with deterministic geospatial tools. Public-data acquisition, coordinate-system handling, hazard modeling, vulnerability calculation, risk ranking, sensitivity analysis, output validation, and provenance recording are implemented as executable Python modules. The LLM selects and coordinates tools but does not invent geometries, Census values, risk scores, or optimization results.

The system is decision support. It does not make operational evacuation decisions.

### Demonstration video

[Watch the 10-minute agent demonstration](YOUR_UNLISTED_VIDEO_URL)

Replace `YOUR_UNLISTED_VIDEO_URL` with the final YouTube, institutional-media, or shareable Drive URL.

### Tested environment

The development environment used:

- Windows with PowerShell
- Python 3.11.9
- projected working CRS `EPSG:5070`
- storage CRS `EPSG:4326`

The deterministic pipeline does not require an LLM API key. Live data acquisition requires internet access and should use a Census API key. The conversational agent additionally requires one configured model provider.

### Fresh installation on Windows

```powershell
git clone https://github.com/manojsankuru/oasis.git geo-agent
cd geo-agent

py -3.11 -m venv .venv

$ProjectPython = ".\.venv\Scripts\python.exe"

& $ProjectPython --version
& $ProjectPython -m pip install --upgrade pip
& $ProjectPython -m pip install -r requirements.txt
```

Do not install into the system Python environment. Do not run files directly as `python src/file.py`; run them as modules from the repository root.

### Fresh installation on Linux or macOS

```bash
git clone https://github.com/manojsankuru/oasis.git geo-agent
cd geo-agent

python3.11 -m venv .venv
ProjectPython="./.venv/bin/python"

"$ProjectPython" --version
"$ProjectPython" -m pip install --upgrade pip
"$ProjectPython" -m pip install -r requirements.txt
```

### Secrets and environment configuration

`.env`, credentials, service-account files, private keys, logs, acquired snapshots, and transfer-work directories are intentionally excluded from version control.

For deterministic offline checks, no `.env` file is required.

For live public-data acquisition, create `.env` containing:

```dotenv
CENSUS_API_KEY=YOUR_CENSUS_API_KEY
```

Request a Census API key at:

https://api.census.gov/data/key_signup.html

For a Vertex AI-backed agent run:

```dotenv
CENSUS_API_KEY=YOUR_CENSUS_API_KEY
GOOGLE_CLOUD_PROJECT=YOUR_REAL_PROJECT_ID
GOOGLE_CLOUD_LOCATION=us-central1
GOOGLE_GENAI_USE_VERTEXAI=true
GEMINI_MODEL=gemini-2.5-pro
```

The executing machine must have valid Google Application Default Credentials.


Never commit `.env`. Verify that it is ignored:

```powershell
git status --short --ignored .env
```

### Primary Charleston reproduction

The primary configured study area is Charleston County, South Carolina, with state FIPS `45` and county FIPS `019`.

Retrieve the live public datasets:

```powershell
& $ProjectPython -m src.acquire
```

This writes the acquired snapshot and provenance manifest under:

```text
data/snapshot/
```

Run the deterministic analysis:

```powershell
& $ProjectPython -m src.pipeline
```

This writes three scenario risk tables, a nine-row weighting trade-off table, and a pipeline report under:

```text
outputs/
```

The deterministic pipeline includes:

1. tract and block-group acquisition;
2. ACS indicator acquisition;
3. elevation acquisition;
4. facility acquisition;
5. optional NFHL acquisition;
6. CRS validation and reprojection;
7. geometry repair and join auditing;
8. block-group-to-tract apportionment;
9. raster zonal statistics;
10. flood-depth scenarios;
11. vulnerability scoring;
12. facility-access scoring;
13. risk ranking;
14. weighting sensitivity analysis;
15. artifact and provenance reporting.

A missing optional source is reported as degraded. Missing flood-zone data is never interpreted as proof of zero flood risk.

### Tool surface

Inspect the eleven model-visible tools:

```powershell
& $ProjectPython -m src.tools
```

Inspect the schemas sent to the model:

```powershell
& $ProjectPython -m src.schemas
```

These commands should report no missing backing modules.

### Model-backed agent demonstration

Verify the configured model endpoint:

```powershell
& $ProjectPython -m src.test_api
```

Run one question:

```powershell
& $ProjectPython -m src.demo "Which communities in this county should be prioritised for evacuation support under a three metre storm surge, and who does that choice leave out?"
```

The agent writes full traces under:

```text
logs/run_<id>.jsonl
```

and structured run results under:

```text
outputs/run_<id>.json
```

Every numeric claim in the final answer is checked against returned tool evidence by the critic. Geometry and coordinate arrays are not sent to the model.

### Reproduce the registered transfer experiment

The checked-in transfer area is Chatham County, Georgia, state FIPS `13`, county FIPS `051`.

Run the offline transfer safety harness:

```powershell
& $ProjectPython -m src.experiments.transfer --check
```

Run live isolated transfer acquisition and analysis:

```powershell
& $ProjectPython -m src.experiments.transfer
```

The transfer runner writes live intermediate data only under:

```text
outputs/transfer-work/<run-id>/
```

and validated paper artifacts under:

```text
outputs/paper/transfer/<run-id>/
```

The canonical structured result is:

```text
outputs/paper/transfer_report.json
```

A successful transfer report must show:

- `status: "completed"`
- `stage: "complete"`
- the requested study area
- restored configuration paths
- unchanged primary snapshot
- correct county GEOID prefixes
- all required datasets registered
- all configured scenarios completed
- validated risk-table row counts
- nine trade-off rows
- explicit missingness and warnings

Earlier failed and completed reports are retained under:

```text
outputs/paper/transfer_attempts/
```

### Test an unseen county

For a new county, edit only `TRANSFER_AREA` in `src/config.py`.

Example:

```python
TRANSFER_AREA = StudyArea(
    name="New Hanover County, North Carolina",
    state_fips="37",
    county_fips="129",
)
```

The official county GEOID is `37129`. No bounding box is required; the implementation derives the extent from the retrieved Census tract polygons.

Then run:

```powershell
& $ProjectPython -m src.experiments.transfer --check
& $ProjectPython -m src.experiments.transfer
```

Do not introduce county-specific branches or manually repair acquired files. If a public service fails, preserve the structured failure report.

### Robustness experiments

Run the deterministic fault experiment:

```powershell
& $ProjectPython -m src.experiments.faults
```

Run fault injection against a live service:

```powershell
& $ProjectPython -m src.experiments.faults --live
```

Run the adversarial behavior scenarios:

```powershell
& $ProjectPython -m src.experiments.behaviour
```
The repository intentionally distinguishes:

- successful command completion;
- correct returned content;
- recovered transport faults;
- unrecovered content faults;
- degraded optional data;
- unscored geographic units;
- and complete validated analysis.

### Mutation testing

Mutation testing deliberately changes one implementation detail at a time and checks whether the independent verification detects it.

Run it in a disposable clone:

```powershell
& $ProjectPython mutate.py
```

Do not terminate the process externally. If a process is killed, inspect the repository for:

```text
*.mutation-backup
```

A mutation sweep is successful only when every deliberate defect is detected and no mutation-backup file remains.

### Paper artifacts

Generate exactly the three manuscript figures:

```powershell
& $ProjectPython -m src.figures
```

Generate the source-fingerprinted quantitative evidence:

```powershell
& $ProjectPython -m src.experiments.report
```

Verify both:

```powershell
& $ProjectPython -m src.figures --check
& $ProjectPython -m src.experiments.report --check
```

Expected figures:

```text
paper/figs/architecture.pdf
paper/figs/risk_surface.pdf
paper/figs/tradeoff.pdf
```

Expected metric record:

```text
outputs/paper/numbers.json
```

Build the paper from `paper/`:

```powershell
pdflatex -interaction=nonstopmode main.tex
bibtex main
pdflatex -interaction=nonstopmode main.tex
pdflatex -interaction=nonstopmode main.tex
```

The build requires the ACM `acmart` class. The final PDF must be no more than four pages, including references.

### Expected limitations

The current evidence supports Charleston and Chatham, and the architecture is parameterized for other counties. It does not establish universal geographic transferability.

Known limitations include:

- the default `EPSG:5070` working CRS is intended for the contiguous United States;
- Alaska, Hawaii, Puerto Rico, and territories require separate CRS and endpoint validation;
- the flood-depth model is a screening-level bathtub model, not a hydrodynamic simulation;
- NFHL availability and service behavior differ by county;
- ACS estimates can be suppressed or have zero universes;
- public APIs can time out, truncate, or return successful HTTP responses with incorrect content;
- tract-level scores do not describe individuals;
- vulnerability and equity weights are normative choices;
- the system supports public deliberation but does not replace community engagement or emergency-management authority;
- the model-written-code subprocess has timeout and working-directory controls but is not a complete operating-system security sandbox.

### Repository map

| Path | Purpose |
|---|---|
| `src/config.py` | Study-area, path, provider, and CRS configuration |
| `src/acquire.py` | Public-data acquisition and provenance |
| `src/align.py` | CRS, geometry, missingness, joins, apportionment, and raster alignment |
| `src/hazard.py` | Flood-depth scenarios and zonal hazard measurements |
| `src/vulnerability.py` | Vulnerability indicators and weighting presets |
| `src/risk.py` | Risk components, rankings, and trade-off analysis |
| `src/pipeline.py` | Deterministic end-to-end analysis |
| `src/tools.py` | Model-visible tool execution surface |
| `src/agent.py` | LLM orchestration loop |
| `src/critic.py` | Numeric claim-to-tool-evidence verification |
| `src/sandbox.py` | Bounded subprocess for model-written spatial code |
| `src/faults.py` | Seeded retrieval fault injection |
| `src/experiments/faults.py` | Robustness experiments |
| `src/experiments/behaviour.py` | Adversarial agent scenarios |
| `src/experiments/transfer.py` | Isolated cross-county transfer harness |
| `src/figures.py` | Deterministic manuscript figures |
| `src/experiments/report.py` | Source-fingerprinted paper metrics |
| `docs/DATA.md` | Data endpoints and source details |
| `docs/RUNBOOK.md` | Development and operational record |
| `docs/failures.md` | Honest failure and limitation record |
| `paper/` | ACM manuscript and generated figures |

### License

This project is released under the MIT License. See `LICENSE`.