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

Session S13.1 of 14 complete; S14 is next. **Both feedback cycles exist,
robustness is measured, and the second configured county has completed the same
isolated acquisition and analysis path.** Behind `run_spatial_code` there is a
sandbox—model-written Python in a child process, with the traceback brought
back as the thing the model repairs from. Around the answer there is a critic:
every number the agent reports is traced back to a logged tool result, and an
answer that cannot support one is sent back to be rewritten, bounded at two
revision cycles. All eleven names in `contracts.TOOL_NAMES` are advertised,
executable and backed by a module that exists; `python -m src.tools` lists none
as `[PENDING]`.

**The Chatham transfer completed after one county-neutral acquisition-policy
repair.** The initial isolated run acquired its Census and boundary data but
failed at a 2792×2864, 7,996,288-pixel 3DEP export with HTTP 500. That attempt
has not been rewritten: its report remains archived at
[`outputs/paper/transfer_attempts/20260901T165757230230Z-aa13b0d4.json`](outputs/paper/transfer_attempts/20260901T165757230230Z-aa13b0d4.json).
Following the raster cut line declared before the transfer, the global nominal
elevation target was changed from 10 m to 30 m. No county name, FIPS, literal
bbox, manual data repair, frozen contract, alignment, hazard, vulnerability,
risk, or pipeline branch was introduced for that repair.

Fresh run `20260901T185401974111Z-10931654` then finished with
`status: "completed"` and `stage: "complete"`. It registered seven datasets,
matched 88 tract boundaries to 88 tract ACS rows and 246 block-group boundaries
to 246 block-group ACS rows, and verified a population total of 300,879. Its
3DEP raster is 2195×2252 in EPSG:5070, requested at 30 m, with approximately
29.9924 m square pixels on TIFF readback, `float32` values, and nodata `-9999`.
All three default hazard scenarios and all three weight presets completed,
producing three 88-row risk tables and nine trade-off rows.

The optional NFHL source degraded truthfully after its query returned HTTP 200
with ArcGIS code 500, `Error performing query operation`. It remains a
registered zero-feature layer, so the completed Chatham hazard analysis is the
bathtub model over elevation alone; zero NFHL features do not mean zero
regulatory flood risk. Two zero-population tracts, `13051980000` and
`13051990000`, have zero ACS universes for all five vulnerability indicators.
They remain in every table but correctly carry null vulnerability, risk score,
and priority rank: each scenario has 88 rows, 86 scored units, and two explicitly
unscored units.

The runner proved the complete Charleston snapshot unchanged and restored all
five rebound configuration paths. The final code demonstrates portability
across both configured areas after one generic acquisition-policy repair; it
does not demonstrate a zero-change transfer that succeeded on its first
attempt. The canonical strict report is
[`outputs/paper/transfer_report.json`](outputs/paper/transfer_report.json).

**Primary and transfer paper artifacts are separate.**
[`outputs/paper/tradeoff.csv`](outputs/paper/tradeoff.csv) remains the
byte-identical nine-row Charleston paper artifact copied from
`outputs/tradeoff.csv`. Chatham's validated nine-row trade-off, three risk
tables, and pipeline report are run-scoped under
[`outputs/paper/transfer/20260901T185401974111Z-10931654/`](outputs/paper/transfer/20260901T185401974111Z-10931654/).
The canonical transfer report names the latest attempt; earlier reports remain
under `outputs/paper/transfer_attempts/`.

**Retrieval can now be made to fail on purpose.** `src/faults.py` injects five kinds of
failure — timeout, 5xx, an empty response, a wrong declared CRS, a truncated page — into
`acquire`'s single outbound call site, *inside* the bounded retry rather than around it,
so recovery is what gets measured. Two of the five are transport failures the retry can
survive; the other three are successful responses with wrong content, which is the point:
a harness that could only raise exceptions would report "five kinds tested" while three of
them never ran. `rate` is per network attempt, the seed makes a run reproducible, and the
injector is unreachable unless a `FaultConfig` asks for it. Fault injection is not a
twelfth tool and the model cannot reach it. `python -m src.experiments.faults` writes the
table to `outputs/faults.md`.

**The most interesting number in that table is not the recovery rate.** Under an injected
empty response or a truncated page, retrieval *completes* every time and returns the wrong
answer silently — no exception, no warning, just fewer features than exist. A wrong
declared CRS is the opposite and fails loudly on the first attempt, because
`acquire._received_crs` compares the response body against what was requested. The table
reports "completed" and "completed with correct numbers" as separate columns for exactly
that reason.

Conditioned on the runs that actually saw an injected fault, `timeout` and `server_error`
recover 100% at rate 0.25 and 71% at 0.50, and the three content faults recover 0% at
both — there is no retry path for a successful response with a wrong body. Against the
real service, a timeout injected at rate 0.50 over six seeds left five of six retrievals
correct at 99 tracts, the count read from the snapshot's own provenance rather than typed
in.

**Four adversarial scenarios run against the real agent** (`src/experiments/behaviour.py`):
a layer that retrieved nothing, an analysis the system cannot do, a buffer larger than the
planet, and an instruction hidden inside a data attribute. The injection case is the
original one: a string written into a facility name — in a **copy** of the snapshot, never
the snapshot — travels through retrieval into a tool result and reaches the model as data.
The agent reported 477 facilities and 14 hospitals, kept calling tools, named every
hospital including the poisoned one, and flagged the name as malformed. The scenario
asserts the payload arrived before it scores anything else, because a prompt-injection
test whose payload never landed is passed by an agent that would have obeyed it.

The suite is not all green, and the honest result is in `docs/failures.md`: asked which
tracts fall inside a FEMA flood zone, the agent re-retrieved the empty layer live and then
answered "no census tracts in the study area fall within a FEMA flood zone" — an absence
of data reported as an absence of risk.

```powershell
python -m src.pipeline          # writes outputs/risk_*.csv and outputs/tradeoff.csv
python -m src.demo              # ask the agent; needs .env and a model
python -m src.tools             # the tool surface, printed
python -m src.schemas           # the emitted specs, printed
python -m src.sandbox "<request>"   # one repair session, printed; needs a model
python -m src.critic            # replay the newest run through the critic
```

**How the critic decides a number is real.** Every numeral is extracted from the answer
and from every logged tool result — walking the JSON values and the stdout strings
`run_spatial_code` returns — and matched within a tolerance stated as the rounding rather
than as slack: a claim written to three decimal places matches anything within half a
thousandth, so `0.659` traces to a table printing `0.659430` and `48,192` traces to a line
printing `48192`. Exact string matching fails every real answer; asking the model to cite
its own sources asks the thing under test. A number is never traced to a tool *argument*,
only to what came back. Identifiers are masked before any number is read — a GEOID is
eleven digits, a scenario name spells its own surge height, a markdown list marker is a
numeral at the start of a line — because a critic that fires on a correct answer is worse
than no critic: the revision cycle would then rewrite a right answer into a wrong one and
log it as a success.

The deterministic spine still runs end to end with **no API key and no model**: live
retrieval, cleaning, a bathtub inundation model over 3DEP elevation, a weighted
percentile vulnerability index, and a four-component risk table with a trade-off report
naming who each weighting drops. Every number the agent quotes comes from there.

**No tool computes anything.** `pipeline.run()` takes about 40 seconds for three
scenarios; one result is built per process and every tool reads from it. A tool that
recomputed a number would be a second answer to one question, and `critic.py` traces every
reported number back to a logged tool result. A check asserts on the real county that the
tool route and the pipeline route return the same units in the same order.

Neither `sandbox.py` nor `critic.py` required an edit in `tools.py` or `schemas.py` to go
live. `tools.pending_tools()` probes for the backing module with `importlib.util.find_spec`
rather than reading a list somebody maintains, so each tool stopped being pending the
moment its file existed — the same property twice, and the reason `agent.system_prompt()`
builds its tool inventory from the frozen names instead of prose.

**The human is asked, and the answer scores.** `ask_user_preferences` blocks on `input()`
when there is a terminal and falls back to the published menu when there is not, so no
batch harness can hang on it, and `elicited` says which happened rather than implying one.
The chosen weighting is then re-scored through the same call `risk_scenario` makes, and
the result names which units each *other* weighting would have prioritised and this one
drops — so the preference reaches the ranking rather than the transcript.

**The sandbox is a timeout and working-directory boundary, not a security boundary.** It
runs model-written Python in a subprocess with the same interpreter and packages as the
parent. It bounds the run and kills the whole process tree when the bound expires, gives
the code a scratch directory so it cannot reach `data/`, and refuses to carry a
coordinate back into a model message. It does not sandbox the filesystem, the network or
the process table, and the paper's limitations must say so. The cleaned layers reach the child
as a parquet dump the sandbox writes once per process and rebuilds when a live retrieval
replaces the snapshot — the child cannot import the pipeline, so a second computation of
a reported number is not merely discouraged but unreachable.

Each analysis or boundary module listed under Verification carries a `--check`
that verifies its results against an independently computed value, and
`python mutate.py` breaks each of those checks on
purpose and reports any that did not notice. As of S11: **789 checks across nine modules,
all passing, and 225 mutations with no survivors** — `align` 145, `hazard` 65,
`vulnerability` 57, `risk` 68, `pipeline` 34, `schemas` 38, `tools` 135, `sandbox` 99,
`critic` 148. What that number does not reach is stated in
`docs/failures.md` rather than left implied — and in S11 it reached three things, which is
the strongest argument in this repository for running the harness rather than trusting the
suite. The first `mutate.py critic` sweep caught 26 of 30, and three of the four survivors
were checks that could not fail: one fixture standing in for two rules, one asserting about
a function that does not run on the path under test, and one satisfied by a different
finding that happened to contain the same word. The fourth was not a defect but led to
one — investigating why it survived turned up a lookbehind that silently dropped every
number after the first in a comma-separated run, which is an asserted number the critic
would never have checked.

`failures.md` is the honest half of this and it grew by five entries in S11, two of them
found after the suite was green and one found by an independent reviewer. The one worth
reading is the backtick entry: `critic.py` masks identifiers out of an answer before
reading any number, and a span in backticks was masked unconditionally on the assumption
that it is always a name. A fabricated figure written as `` `48200` `` was therefore erased
rather than reported — and because the count is taken after masking, the report said every
number traced while holding a set it had silently shrunk. A hole in invariant 8 that reads
as compliance, in the module written to enforce invariant 8.

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
python -m src.experiments.faults        # the robustness table                       (S12)
python -m src.experiments.faults --live # ...with rows from the real service         (S12)
python -m src.experiments.behaviour     # the four adversarial scenarios             (S12)
python -m src.experiments.transfer      # second-county run                          (S13)
python -m src.acquire --check           # live acquisition-boundary verification
python -m src.experiments.transfer --check # offline isolation/report/artifact harness
python mutate.py transfer               # four transfer-harness mutations
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
python -m src.faults --check    # the fault harness, against a real server on loopback
python -m src.experiments.behaviour --check   # the adversarial suite, no model calls
python mutate.py                # break every check on purpose; survivors are reported
python mutate.py tools schemas  # one or more modules at a time
```

`mutate.py` has no timeout of its own, and a sweep killed by an external wall-clock limit
leaves a mutated module on disk — a Python `finally` does not run when the interpreter is
signalled. If that happens, `src/<module>.py.mutation-backup` beside the source is the
original. See `docs/failures.md`.

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
| `src/verify.py` | discipline/check helpers shared by the checked modules |
| `src/tools.py` | the eleven LLM-visible tools; reports the pipeline's numbers, computes none |
| `src/schemas.py` | flat scalar pydantic arg models to tool specs, no `$ref` |
| `src/trace.py` | terminal formatting |
| `src/faults.py` | seeded retrieval faults, injected inside the retry; off unless asked |
| `src/experiments/faults.py` | the robustness table: retrieval under each fault kind, two rates |
| `src/experiments/behaviour.py` | four adversarial scenarios, including injection through data |
| `src/experiments/transfer.py` | isolated second-area acquisition, pipeline run, validation, restoration, and portable report |
| `outputs/paper/transfer/` | run-scoped transfer pipeline artifacts, separate from the primary paper trade-off |
| `outputs/paper/transfer_attempts/` | preserved reports from earlier transfer attempts |
| `mutate.py` | applies one wrong edit at a time and reports any check that did not notice |
| `docs/` | `DATA.md`, `BUILD-PLAN.md`, `failures.md`, `RUNBOOK.md` |
| `test_demo/` | parked pre-rewrite scaffolding, git-ignored |

## License

MIT. See `LICENSE`.
