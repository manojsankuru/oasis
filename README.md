# OASIS GeoAI risk analyst

Autonomous GeoAI risk analyst for the OASIS @ ACM SIGSPATIAL 2026 Student Challenge,
**Track A: Disaster Resilience & Vulnerability Analysis**.

The agent takes a natural-language question about hurricane risk, autonomously retrieves
and aligns live public geospatial data, reasons over it with a mix of verified tools and
self-written code, and reports priority communities under a user-chosen weighting. It is
decision support. It does not act.

See `CLAUDE.md` for the hard invariants, `src/contracts.py` for the frozen interfaces,
`docs/DATA.md` for endpoints and `docs/BUILD-PLAN.md` for the session plan.

## Status

Session S9 of 14 complete. **The agent answers a question.** On top of the deterministic
spine there is now a tool surface: the eleven names in `contracts.TOOL_NAMES`, each
returning a small JSON result, and flat scalar argument schemas with no `$ref`, `$defs`
or `anyOf` anywhere in the emitted JSON.

```powershell
python -m src.pipeline          # writes outputs/risk_*.csv and outputs/tradeoff.csv
python -m src.demo              # ask the agent; needs .env and a model
python -m src.tools             # the tool surface, printed
python -m src.schemas           # the emitted specs, printed
```

The deterministic spine still runs end to end with **no API key and no model**: live
retrieval, cleaning, a bathtub inundation model over 3DEP elevation, a weighted
percentile vulnerability index, and a four-component risk table with a trade-off report
naming who each weighting drops. Every number the agent quotes comes from there.

**No tool computes anything.** `pipeline.run()` takes about 40 seconds for three
scenarios; one result is built per process and every tool reads from it. A tool that
recomputed a number would be a second answer to one question, and `critic.py` in S11
traces every reported number back to a logged tool result. A check asserts on the real
county that the tool route and the pipeline route return the same units in the same
order.

Two of the eleven are backed by modules that do not exist yet — `run_spatial_code` by
`src/sandbox.py` (S10) and `validate_answer` by `src/critic.py` (S11). They are probed
for at run time, advertised to the model as unavailable so no turn is spent discovering
it, and return a refusal naming the missing module. Nothing about that state is written
down: the day the module lands, the tool works.

Every module carries its own `--check` that verifies its results against an
independently computed value, and `python mutate.py` breaks each of those checks on
purpose and reports any that did not notice. As of S9: **525 checks across seven
modules, all passing, and 166 mutations with no survivors.** What that number does not
reach is stated in `docs/failures.md` rather than left implied.

## Setup

```powershell
cd C:\Projects\oasis\geo-agent
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Then fill in `.env`. Run as a module from the project root; `python src/demo.py` fails on
imports. Nothing under **Analysis** below needs `.env` or a network connection once
`data/snapshot/` exists.

## Providers

Setting `GOOGLE_CLOUD_PROJECT` selects Vertex AI; otherwise the Clemson settings are used.
Switching is a `.env` edit, nothing else. Both stay behind `src/llm_client.py`.

| variable | meaning |
| --- | --- |
| `GOOGLE_CLOUD_PROJECT` | GCP project id — its presence selects Vertex |
| `GOOGLE_CLOUD_LOCATION` | region, e.g. `us-central1` |
| `GEMINI_MODEL` | e.g. `gemini-2.5-pro` |
| `CENSUS_API_KEY` | Census API key; keyless use works up to a daily cap |
| `CLEMSON_API_BASE_URL` / `OPENAI_PROXY_URL` | the Clemson endpoints |
| `CLEMSON_API_KEY` / `CLEMSON_MODEL` | the Clemson key and model id |

Vertex needs no API key — auth is Application Default Credentials, and `llm_client.py`
mints a short-lived OAuth token per client. `gcloud` need not be installed if ADC already
exists at `%APPDATA%\gcloud\`.

Vertex is reached through its **OpenAI-compatible endpoint**, so the `openai` SDK and the
whole agent loop are unchanged. Two quirks, handled in `config.py`: the model id needs a
`google/` prefix (`google/gemini-2.5-pro`; the bare name 400s with *"Malformed publisher
model"*), and there is no `/models` endpoint, so `test_api` check 1 verifies credentials
instead.

On the Clemson proxy, `GET /models` lists far more than it will serve. Verified working:
`gpt-5`, `gpt-5-mini`, `gpt-4.1`, `gpt-4.1-mini`, `gpt-4o`, `gpt-4o-mini`,
`gpt-3.5-turbo`. Listed but refused: `gpt-5.1`, `gpt-5.1-chat-latest`,
`gpt-5-chat-latest`.

## Secrets

`.env` and `application_default_credentials.json` are git-ignored and must stay that way.
Never write a key into `.env.example`, a test, a log, or a commit. Before any push,
`git log --all -S<key-fragment>` must return nothing.

## Commands

### Retrieval and the agent

```powershell
python -m src.test_api          # does the endpoint work, and does it tool-call
python -m src.acquire           # live retrieval -> data/snapshot/ + manifest.json   (S5)
python -m src.demo              # run the agent on the built-in questions            (S9)
python -m src.demo "question"   # or on one of your own
python -m src.experiments.faults    # robustness runs                                (S12)
python -m src.experiments.transfer  # second-county run                              (S13)
```

### Analysis — no model, no key, no network

```powershell
python -m src.align             # what cleaning did, with denominators
python -m src.hazard            # bathtub inundation per tract, per scenario
python -m src.vulnerability     # the index and every weight preset's provenance
python -m src.risk              # the four components, the score, and who loses
python -m src.pipeline          # all of it -> outputs/
python -m src.tools             # the tool surface and what is pending a module
python -m src.schemas           # the emitted tool specs, as sent to the model
```

### Verification

```powershell
python -m src.align --check     # and --check on hazard, vulnerability, risk,
                                #   pipeline, schemas, tools
python mutate.py                # break every check on purpose; survivors are reported
python mutate.py tools schemas  # one or more modules at a time
```

A `--check` verifies against a value computed a different way — a hand-built synthetic
raster whose expected statistics are arithmetic stated in the check, or a cell-centre
point-in-polygon pass written with shapely and numpy that never calls the function under
test. A spatial result checked only against a previous run of the same code is not
checked.

Every run writes `logs/run_<id>.jsonl` (one object per step, untruncated) and
`outputs/run_<id>.json` (messages + steps). The terminal trace is for watching; the JSONL
is for auditing.

## Coordinate systems

Data is stored in EPSG:4326 and every area, distance, buffer, centroid and zonal
operation runs in the projected working CRS (EPSG:5070 by default, a field on
`StudyArea`). In a geographic CRS those operations return degree-based values silently,
with no error and no warning. `StudyArea` rejects a geographic `working_crs` for that
reason.

## Layout

| path | purpose |
| --- | --- |
| `src/contracts.py` | frozen dataclasses, protocols, column names, tool names |
| `src/config.py` | settings, paths, CRS constants, the study-area parameter |
| `src/llm_client.py` | the only file that talks to a model or parses tool calls |
| `src/agent.py` | the loop, logging, counters |
| `src/align.py` | CRS resolution, geometry repair, sentinel scrub, GEOID audit, apportionment, zonal stats |
| `src/hazard.py` | bathtub inundation surfaces and the scenarios |
| `src/vulnerability.py` | the percentile index, the indicators' rationale, the weight presets |
| `src/risk.py` | the four components, the score, the trade-off table |
| `src/pipeline.py` | the deterministic spine: snapshot in, risk table out |
| `src/verify.py` | check helpers shared by every module's `--check` |
| `src/tools.py` | the eleven LLM-visible tools; reports the pipeline's numbers, computes none |
| `src/schemas.py` | flat scalar pydantic arg models to tool specs, no `$ref` |
| `src/trace.py` | terminal formatting |
| `src/robustness.py` | four adversarial scenarios (ported to `experiments/` in S12) |
| `mutate.py` | applies one wrong edit at a time and reports any check that did not notice |
| `docs/` | `DATA.md`, `BUILD-PLAN.md`, `failures.md`, `RUNBOOK.md` |
| `test_demo/` | parked pre-rewrite scaffolding, git-ignored |

## License

MIT. See `LICENSE`.
