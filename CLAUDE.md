# CLAUDE.md

Autonomous GeoAI risk analyst for the OASIS @ ACM SIGSPATIAL 2026 Student
Challenge, **Track A: Disaster Resilience & Vulnerability Analysis**.
Submission (short paper + this repository) is due **4 Sep 2026, 11:59 PM AoE**.

The agent takes a natural-language question about hurricane risk, autonomously
retrieves and aligns live public geospatial data, reasons over it with a mix of
verified tools and self-written code, and reports priority communities under a
user-chosen weighting. It is decision support. It does not act.

---

## How this repository is judged

Four equally weighted criteria, reviewed by a program committee. There is no
accuracy leaderboard, so **every point is won by something we chose to build**.
When a design decision is ambiguous, resolve it toward whichever criterion it
serves.

| | Criterion | What it demands of the code |
| --- | --- | --- |
| **TU** | Tool-Use Rigor & Autonomy | Retrieval and cleaning happen in code, never by hand. The agent can write, run and debug its own spatial analysis. User preferences are parameters. |
| **RB** | Robustness & Generalizability | Survives timeouts, missing data, wrong CRS, mismatched granularity. Runs on a second county with no code change. |
| **SG** | Social Good Alignment | Vulnerability / equity / optimality are operationalized functions with stated rationale and stated weight provenance. Trade-offs are reported, including who loses. |
| **IR** | Innovation & Reflection | Two feedback cycles (code repair, critic revision). Honest failure reporting. |

---

## Hard invariants — never violate these

1. **No manual data cleaning, ever.** If a dataset is malformed, fix it in
   `align.py` so the agent fixes it at run time. Never hand-edit a snapshot, a
   GeoJSON, or a CSV. Never hardcode a value that should have been retrieved.
   This is the single most expensive rule to break: criterion TU says
   "without manual low-level data-cleaning" in as many words.
2. **All area, distance, buffer, centroid and zonal operations run in the
   metric CRS.** Route every one through the helper in `align.py`. Geographic
   CRS operations return degree-based values silently, with no error and no
   warning. This has already bitten this project once.
3. **Geometry never goes into a model message.** Tools return small scalar
   summaries. If a tool result could contain a coordinate list, it is wrong.
4. **Tool argument schemas use scalar types only** (`str`, `float`, `int`,
   `bool`). Nested pydantic models emit `$ref`/`$defs`, which some
   OpenAI-compatible servers reject.
5. **Nothing is hardcoded to one county.** Study area is a parameter (FIPS or
   place name) threaded from config. A literal "Charleston" or "45019" outside
   config is a bug — the transfer run is worth a quarter of criterion RB.
6. **Every dataset carries a `Provenance` record** — source URL, retrieval
   timestamp, declared CRS, working CRS, vintage, feature count, license.
   A dataset without one does not enter the registry.
7. **Every network call has an explicit timeout and a bounded retry.**
   API timeouts are named in criterion RB; an unbounded `requests.get` is a
   lost point, not just a hang.
8. **Every number the agent reports must be traceable to a logged tool
   result.** That is what `critic.py` enforces. Do not weaken it.

## Verification rules

- **A test that mocks the thing under test proves nothing.** This project has
  already shipped two green test suites that both stubbed the LLM client, so no
  real SDK code had ever executed and a genuine bug survived. Any change to
  `llm_client.py`, `acquire.py` or `sandbox.py` needs at least one check that
  exercises the real boundary.
- Offline loop mechanics (id matching, message ordering, iteration bounds) are
  tested with a stub LLM — that part is correct and cheap. Keep it.
- Spatial results are verified against an independently computed value, not
  against a previous run of the same code.
- When something breaks for real, append it to `docs/failures.md` with the date
  and what happened. Those entries become a paper section; do not clean them up
  and do not invent them later.

---

## Architecture

Two feedback cycles distinguish this system: **code repair** around the
sandbox, and **critic revision** around the answer.

```
question -> orchestrator (LLM) -> tools --------------------> answer
                  ^                 |                            |
                  |                 +-- sandbox: write, run,      |
                  |                     read traceback, repair    |
                  |                                               v
                  +---------------- critic: numbers vs tool log --+
```

| Module | Responsibility |
| --- | --- |
| `src/config.py` | settings, paths, CRS constants, **study area parameter** |
| `src/provenance.py` | `Provenance` dataclass, manifest read/write |
| `src/registry.py` | named dataset registry; layers only reachable through it |
| `src/acquire.py` | live retrieval: ArcGIS REST, Census API, Overpass, raster |
| `src/align.py` | CRS resolution, geometry repair, sentinel scrub, GEOID audit, granularity apportionment, raster→vector zonal stats |
| `src/faults.py` | injectable timeouts / 5xx / empty / wrong-CRS, for the robustness experiment |
| `src/vulnerability.py` | percentile index, weights as parameters |
| `src/hazard.py` | exposure from raster and vector hazard layers |
| `src/experiments/transfer.py` | isolated second-area acquisition/pipeline evidence; never model-visible |
| `src/risk.py` | hazard × exposure × vulnerability × resilience, components reported separately |
| `src/pipeline.py` / `src/risk.py` | scenario and weighting sweep; `ScenarioRow` trade-off table |
| `src/sandbox.py` | subprocess execution of model-written code, traceback return |
| `src/critic.py` | numeric traceability + domain invariants + revision cycle |
| `src/schemas.py` | pydantic arg models → tool specs (flat, no `$ref`) |
| `src/llm_client.py` | **the only file that talks to a model or parses tool calls** |
| `src/agent.py` | the loop, logging, counters |
| `src/trace.py` | terminal formatting (demo asset — finalists present in November) |

Two model backends exist: the Clemson proxy (`gpt-5`) and Vertex AI
(`gemini-2.5-pro`). Both stay behind `llm_client`. Do not let backend details
leak into `agent.py`.

---

## Conventions

- Python 3.11, **type hints on every function**, `dataclass` for records.
- No agent framework. No LangChain, LangGraph, CrewAI, MCP, vector DB, RAG,
  Streamlit, Docker, ArcPy. The loop being ~200 readable lines is a feature.
- Tool functions return small JSON-serialisable dicts. Errors return
  `{"error": ..., "detail": ...}` rather than raising, so the model can recover.
- Keep the LLM-visible tool count at or under a dozen. If you want another
  tool, it usually belongs as a parameter on an existing one.
- One module per commit. Commit messages: lowercase, imperative, no body needed.

## Commands

```powershell
.\.venv\Scripts\Activate.ps1
python -m src.test_api          # does the endpoint work, and does it tool-call
python -m src.acquire           # live retrieval -> data/snapshot/ + manifest.json
python -m src.acquire --check   # exercise the real endpoints: paging, CRS, error bodies
python -m src.demo              # run the agent on the built-in questions
python -m src.demo "question"
python -m src.experiments.faults    # robustness runs
python -m src.experiments.transfer  # second-county run
```

Run as a module from the project root; `python src/demo.py` fails on imports.
Every run writes `logs/run_<id>.jsonl` (one object per step, untruncated) and
`outputs/run_<id>.json` (messages + steps).

## Secrets

`.env` and `application_default_credentials.json` are git-ignored and must stay
that way. Never write a key into `.env.example`, a test, a log, or a commit.
Before any push: `git log --all -S<key-fragment>` must return nothing.

## Deliberately out of scope

Multi-hazard beyond hurricane surge and coastal flood. Real-time observed
conditions. Raster processing beyond zonal statistics. Exact optimization
solvers. Area-weighted apportionment where centroid containment is stated as a
limitation. Say so in the paper's limitations rather than building it.

---

# Build contract (added 27 Aug — four-day plan)

## `src/contracts.py` is frozen

It holds every dataclass, protocol signature, column name and tool name the
system uses. Implement against it.

- Do **not** change a field or a signature there to make an implementation
  compile. Change the implementation.
- If a signature genuinely has to change: change `contracts.py` first, in its
  own commit, with a one-line reason, then update every caller in that commit.
- Never write a column name as a string literal. Use `contracts.Col`. A typo in
  a column name is the most common way a multi-session build breaks silently.

This exists because the system is built across many short sessions. Without a
frozen contract, session 6 quietly renames what session 2 produced.

## The other two documents

- `docs/DATA.md` — every endpoint, its exact parameters, its quirks, and an
  honest VERIFIED / UNVERIFIED status. Paste the relevant section into any
  acquisition prompt; the `outSR` trap in §1 costs an afternoon if it is not in
  front of you.
- `docs/BUILD-PLAN.md` — fourteen sessions across four days, each with a
  prompt, an acceptance gate and a cut line decided in advance.

## Session discipline

One module per session, then `/clear`. End every session with the
`invariant-reviewer` subagent on the diff, then `/gate <module>`, then a commit.
`/rubric` at the end of each day — whatever it names as weakest is the next
morning's first hour. `/failure` the moment something breaks, with the real
error text.

## Scope changes for the four-day build

- **`src/accessibility.py` is cut.** Network travel time is Track B's named
  challenge and the largest available time sink. Distance-based accessibility,
  if it appears at all, is a supporting measure computed in `risk.py`.
- **Six datasets, one of them a raster.** Tracts, block groups, ACS, 3DEP
  elevation, OSM facilities, and NFHL flood zones as an optional layer that is
  allowed to fail. Rationale and endpoints in `docs/DATA.md`.
- **Hazard is a bathtub inundation model** over 3DEP elevation:
  `depth = max(0, surge_height_m - elevation_m)`. A deliberate simplification,
  stated in the limitations, and it delivers the raster-to-vector multiscale
  join that Track A actually names.
- **`src/robustness.py` survives** as `src/experiments/behaviour.py`. Its four
  adversarial scenarios port to real data. Keep the prompt-injection-via-data
  case in particular: injection through a data attribute is a genuine and
  under-reported failure mode for GeoAI agents and one of the more original
  things in this repository.

## Known local gotcha

`.env` currently begins with a UTF-8 BOM, so the first key parses as
`﻿GOOGLE_CLOUD_PROJECT` under some dotenv versions and reads back empty. If
a setting is mysteriously blank, that is the first thing to check — save `.env`
as UTF-8 without BOM, or load it with `encoding="utf-8-sig"`.
