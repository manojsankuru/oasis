# Runbook — how to drive the four-day build

`BUILD-PLAN.md` says *what* each session does. This says *what you type*, in what
order, and what to do when a session runs long. Read the session's full prompt
from `BUILD-PLAN.md`; the prompts below are the short form plus the ritual.

---

## Where you actually are

Checked Sun 30 Aug, at the end of S10.

| | |
| --- | --- |
| Plan | Mon 31 Aug converted from a paper day to a build day: S8-S9 Sat 29, S10-S12 Sun 30, S13-S14 Mon 31, paper Tue 1 - Wed 2, submit Wed 3, deadline Fri 4. |
| Reality | S1-S10 done. |
| Built | `config`, `contracts`, `provenance`, `registry`, `acquire`, `align`, `verify`, `hazard`, `vulnerability`, `risk`, `pipeline`, `schemas`, `tools`, `agent`, `llm_client`, `trace`, `sandbox` |
| Missing | `critic` (S11), `scenarios`, `faults`, `figures` |

**The agent writes and repairs its own code.** `python -m src.demo` runs rather
than refuses, the numbers in its answer match `outputs/tradeoff.csv` line for
line, and `run_spatial_code` now executes rather than returning a refusal.

What S11 inherits:

- **`src/sandbox.py` is built and `run_spatial_code` is live.** `tools.py` and
  `schemas.py` were not edited: `pending_tools()` probes for the module, so the
  tool started working the moment the file existed. The one remaining pending
  tool is `validate_answer`, backed by `src/critic.py` — S11's job, and the same
  probe will pick it up. What it must provide is a class named `Critic`,
  constructible with no arguments, with
  `check(answer, steps, cycle) -> CriticReport`. `tools.logged_calls()` is the
  in-process list of every tool result, which is what the critic traces against.
- **The sandbox is a timeout and working-directory boundary, not a security
  boundary**, and says so in its own docstring. Model-written Python runs in a
  child with a deadline; the whole process tree is killed when it expires. Every
  subprocess writes stdout and stderr to FILES rather than pipes, because a
  killed child's surviving grandchild holding an inherited pipe is what hangs
  `mutate.py` — whose `run_check` still has no timeout of its own.
- **How the layers reach model-written code**, decided in S10 and written into
  the module docstring: the parent dumps `tools.analysis()`'s frames to parquet
  in a temp directory it owns, once per process, and the child reads them back by
  name. The dump is keyed on the identity of the `Analysis` object, so a live
  retrieval that calls `tools.invalidate()` invalidates the dump too, with no
  edit in `tools.py`. The child cannot import `src`, so rebuilding the pipeline
  in the child is unreachable rather than merely discouraged.
- **`CodeRun.error_type` carries the taxonomy.** `Timeout`, `SyntaxError`,
  `NonZeroExit`, `ShapeMismatch`, `CRSError`, `GeometryInOutput` and whatever the
  traceback ends with. `sandbox.metrics()` reports attempts per request,
  first-run failure rate, repair rate and the taxonomy; `format_metrics()` prints
  it. That is the instrumentation criterion IR asks for and the critic's half of
  it is S11.
- **`repair_loop` is not reachable from the tool surface.** `run_spatial_code`
  calls `run` only, and the agent's own loop is the repair there. Drive the
  bounded loop directly with `python -m src.sandbox "<request>"`, which takes an
  optional `author` on `Sandbox(...)` so the loop mechanics can be checked with a
  scripted author offline.
- **A failed run tells the model what names it had.** `sandbox.available_names()`
  is generated from the dump and appended to the stderr of any non-zero run. It
  is there because the first demo run had the model invent `CONTEXT.layers`, get
  a true and useless `NameError`, and report the tool as broken. If you add a
  tool that hands the model a capability, check that something tells it the
  interface — 95 green checks did not. See `failures.md`.
- **`MAX_ITERATIONS` is still 6.** S11 raises it. A question that needs a tool
  call, a preference, a failed code run and a repair uses five of the six.

What S10 inherited from S9:

- **The eleven names in `contracts.TOOL_NAMES` are implemented** in `src/tools.py`
  and advertised by `src/schemas.py`. `tools.surface_faults()` returns nothing;
  the guard was kept and its reason removed.
- **No tool computes anything.** `tools.analysis()` builds one `PipelineResult`
  per process -- about 40 s for three scenarios -- and every tool reads from it.
  A tool that recomputed a number would be a second answer for `critic.py` to
  trace to. `_agreement_checks` asserts on the real county that the tool route
  and the pipeline route return the same units in the same order.
- **`run_spatial_code` is already wired for you.** It is advertised, executable,
  and returns a refusal naming `src/sandbox.py`. `tools.BACKING_MODULES` maps the
  tool to the module and `tools.pending_tools()` probes for it with
  `importlib.util.find_spec`, so **the day `src/sandbox.py` exists the tool stops
  being pending with no edit in `tools.py` or `schemas.py`**. What S10 must
  provide is a class named `Sandbox` with `run(source, *, timeout_s) -> CodeRun`,
  which is the frozen `contracts.Sandbox` protocol. Same for `validate_answer`
  and `src/critic.py` in S11: a `Critic` with
  `check(answer, steps, cycle) -> CriticReport`. `tools.logged_calls()` is the
  in-process list of every tool result, which is what the critic traces against.
- **Argument schemas are flat scalars with sentinel defaults**, never
  `float | None`: an Optional emits `anyOf` with no plain `type`, and a server
  strict enough to reject `$ref` rejects that too. `schemas.UNSET_WEIGHT` is
  `-1.0` and works as "unset" only because `vulnerability.normalised_weights`
  refuses a negative weight, which a check proves rather than assumes.
- **`agent.system_prompt()` is generated** from `TOOL_NAMES` and
  `schemas.TOOL_DESCRIPTIONS`. Do not hand-write a tool list into it again; the
  old one still named `list_layers` and shelters eight sessions after they were
  deleted.
- **`MAX_ITERATIONS` is still 6.** S11 raises it to 15. The demo's two questions
  finish in 3 and 2 LLM calls.

Measured on this county, S9:

- 420,264 residents in 99 tracts and 261 block groups. Exposed population
  99,037 / 187,349 / 303,839 at 1.5 / 3.0 / 5.0 m of surge -- the block-group
  rollup; the tract-uniform estimate at 3.0 m is 125,533 and the gap is reported
  as a granularity result, not tuned away.
- 98 of 99 tracts scored. The unscored one is `45019990100`, a 9900-series water
  tract with no residents. Every ranking tool names it and says why.
- The raw `tracts` layer carries `CENTLAT`, `CENTLON`, `INTPTLAT`, `INTPTLON` and
  a geometry column. `describe_layer` withholds all five and reports the count.
- 525 checks across seven modules, all PASS. 166 mutations, zero survivors --
  after four survived the first S9 sweep, three of them because a check compared
  a function against itself. See `failures.md`.
- The `facilities` provenance carries a note spelling the study extent out in
  prose. `describe_layer` withholds it and counts it; `tools.COORDINATE_TEXT` is
  the rule, and the reviewer found it because no check looked at prose. Any new
  tool that forwards free text needs to go through that filter.
- `flood_zones` is still DEGRADED. `hazard.vector_hazard_status` reports the
  hazard as elevation-only rather than as an absence of flood risk.

Measured in S10, on the real model (`google/gemini-2.5-pro` through Vertex):

- Seven `repair_loop` requests the eleven tools cannot answer. Five succeeded on
  the first attempt; two failed and both repaired on the second. First-attempt
  failure rate 29%, repair rate 100%, mean 1.29 attempts.
- Three agent questions the eleven tools cannot answer, through
  `python -m src.demo`. All three answered, in 3, 6 and 4 LLM calls of the six
  allowed. One shows the repair in the agent's own loop: `KeyError` on `tracts`,
  then `tracts_joined`, then the answer. Two of the three answers match a
  `repair_loop` session run separately — 0.659 and 15 tracts / 48,192 residents.
- The two real failures were `TypeError` — `AREALAND` arrives as an Arrow string
  under pandas 3, so `area - AREALAND` raises, and the fix is `pd.to_numeric` —
  and `ModuleNotFoundError` for `libpysal`, which the model replaced with a
  hand-written contiguity matrix. Neither was staged; both transcripts are the
  trace-figure material for criterion TU.
- A run costs about 0.6 s. Layers are bound lazily in the child, so a program
  that touches no layer pays interpreter start only, and one that touches all
  eight pays about half a second of parquet.
- The output guard withheld a coordinate on every one of eight shapes tried
  against the real layers, including a layer projected back to EPSG:4326 and
  printed at three decimal places — the shape the reviewer found and no rule in
  the first version could see.

## The ritual — identical every session

```
1. /clear
2. paste the session prompt (short form below, full text in BUILD-PLAN.md)
3. when it says it is done:
      use the invariant-reviewer subagent to review the diff against CLAUDE.md
4. /gate <module>
5. fix whatever the gate names, then commit — one line, lowercase
6. /clear
```

Step 3 is the one you will want to skip. Don't. Hardcoded study areas and CRS
bypasses arrive in three-line changes, which is exactly when skipping feels safe.

**The sentence to keep ready.** The moment a session says anything resembling
"I'll adjust the dataclass" or "the contract needs a small change":

> contracts.py is frozen. Change the implementation, not the contract. If the
> signature genuinely has to change, that is its own commit with a one-line
> reason and every caller updated in the same commit.

**When something breaks**, before you debug it:

```
/failure <one line about what broke>
```

Paste the real error text. These become §3.7 of the paper, and you will not
remember Thursday's traceback on Tuesday.

**Acquisition sessions (S3, S4, S5)** — open `docs/DATA.md`, copy the named
section, and paste it into the prompt. The endpoint quirks are the part the model
cannot guess, and the `outSR` trap in §1 costs an afternoon if it is not in front
of it.

---

# Day 2 — today

## S2 — provenance + registry · 1 h · no cut line

```
Read CLAUDE.md and src/contracts.py first; contracts.py is frozen.
Implement src/provenance.py and src/registry.py against the Provenance and
DatasetRecord contracts. provenance.py serialises to/from JSON and reads/writes
data/snapshot/manifest.json as a list of records, timestamps ISO-8601 with
timezone. registry.py makes datasets reachable ONLY by name; registering one
without a Provenance raises; loading a vector or table returns it already in the
working CRS; cache the projected frame, not the raw one. Write a check that
round-trips a manifest through disk and asserts equality.
```

**Gate:** `/gate registry` — no dataset reachable without provenance.
**Why no cut line:** every later session writes through this.

## S3 — ArcGIS vector retrieval · 1.5 h

Paste **DATA.md §1, §2, §6** into the prompt.

```
Implement discover_arcgis_layers and fetch_arcgis_vector in src/acquire.py
against the Acquirer protocol. Follow the pasted DATA.md sections exactly,
including the outSR trap. Explicit timeout on every call; bounded retry with
backoff via tenacity; resultOffset paging when truncated; assert the CRS received
matches the CRS requested and record both in Provenance; a JSON error body
returned with HTTP 200 must raise, not be written to disk.
Then retrieve tracts and block groups for STUDY_AREA for real and print the
feature counts and provenance. Do not mock the network anywhere in this session.
```

**Gate:** `/gate acquire` — real feature counts for both layers, real CRS
assertion, provenance written.
**Cut:** if TIGERweb paging fights you, retrieve without paging, assert the count
is under `maxRecordCount`, log the limitation. Revisit on Day 4 only if the
transfer county needs it.

## S4 — ACS with run-time variable resolution · 1.5 h

Paste **DATA.md §3**.

```
Implement resolve_acs_variables and fetch_acs. resolve_acs_variables fetches the
ACS variables catalogue and matches contracts.VULNERABILITY_INDICATORS to
variable ids by concept and label. No variable id is hardcoded anywhere. Log
every resolution — which pattern matched which id — into provenance notes; that
log is the evidence for the autonomy criterion. Fetch estimates and margins of
error at tract level, and at block-group level for the one attribute we
apportion. Sentinel handling belongs in align.py; retrieval returns what the API
returned.
```

**Gate:** `/gate acquire` — real rows. Read the resolved id for each canonical
column and confirm they are the variables you meant.
**Cut:** if concept matching is unreliable by mid-afternoon, resolve a *shortlist*
automatically and pick with a documented tie-break. Still no hardcoded ids.
Pasting variable codes is the manual-cleaning failure wearing a different hat.

## S5 — raster, facilities, snapshot · 1.5 h

Paste **DATA.md §4, §5**.

```
Implement fetch_arcgis_raster and fetch_osm. Raster: compute size from the
derived bbox and a target cell size, cap each dimension at 8000 and coarsen if
needed, request imageSR as the working CRS, verify Content-Type is an image
before writing the GeoTIFF, open it with rasterio to confirm transform and CRS.
OSM: convert the 4326 bbox to Overpass south,west,north,east order in this
function and nowhere else; treat HTTP 429 as retryable.
Then write acquire.py's __main__ so python -m src.acquire retrieves all six
datasets, writes data/snapshot/ and manifest.json, and prints a summary table.
```

**Gate:** `/gate acquire` — `python -m src.acquire` completes, manifest has six
entries with real timestamps, the GeoTIFF opens in rasterio at the expected CRS.
**Cut:** if the raster export fights the size cap, drop to 30 m cell size and move
on. Resolution is not scored; having a raster is.

## S6 — alignment core · 2 h · no cut line

```
Implement to_working_crs, repair_geometry, scrub_sentinels and join_on_geoid in
src/align.py against the Aligner protocol, populating AlignmentReport as you go.
Every field must be filled by real work: which datasets were reprojected and from
what, how many geometries repaired and dropped, sentinels removed per column,
unmatched GEOIDs on each side, temporal span. Unmatched GEOIDs are reported,
never silently dropped. Sentinel scrubbing covers the full family of Census
negative codes, not just -666666666.
```

**Gate:** `/gate align` — print the full `AlignmentReport` for the real snapshot.
Every non-zero number in it must be one you can explain.

**End of today:** `python -m src.acquire` produces a real snapshot. If it does
not, tomorrow starts here and S13 is the first thing to cut.

Then: `/rubric`. Whatever it names as weakest is Saturday's first hour.

---

# Day 3 — Saturday

## S7 — zonal stats + apportionment · 2 h

```
Implement zonal_stats and apportion. zonal_stats uses rasterio.mask plus numpy
directly — do not add rasterstats, it is a Windows install risk for thirty lines
of code. Return min, mean, max, cell count per polygon indexed by GEOID.
Polygons under contracts.MIN_RASTER_CELLS go into
AlignmentReport.units_below_cell_threshold. apportion aggregates the block-group
attribute up to tracts by population weight, compares against the published
tract figure, and records the percentage discrepancy in
AlignmentReport.apportionment_error. That discrepancy is a result, not a bug.
```

**Gate:** `/gate align` — verify one polygon's zonal mean **by hand** against the
raster. A spatial result checked only against a previous run of the same code is
not checked.
**Cut:** if population-weighted apportionment is fiddly, use simple sum and report
the larger error honestly. The reported error is the deliverable.

## S8 — hazard, vulnerability, risk · 2.5 h

```
Implement src/hazard.py, src/vulnerability.py, src/risk.py.
hazard: bathtub inundation depth = max(0, surge_height_m - elevation_m) per cell
per HazardScenario, summarised per tract through zonal_stats. Three scenarios in
config with the category-to-height assumption in HazardScenario.assumption_note.
vulnerability: percentile-rank each indicator in VULNERABILITY_INDICATORS,
combine under a WeightPreset. Weights are arguments with defaults, never
constants in the function body. Each indicator gets a docstring sentence saying
why it is in the index.
risk: report hazard, exposure, vulnerability and resilience as SEPARATE COLUMNS
before any combined score, then combine under a preset.
At least three WeightPresets, one with origin="published_plan" or
"published_index" and a real origin_url.
Then src/pipeline.py: snapshot in, tract-level risk table out, no LLM.
```

**Gate:** `/gate risk` — `python -m src.pipeline` writes a real table. Sanity:
exposed population never exceeds tract population; inundated fraction in [0,1];
the ranking changes when you change a weight.
**Cut:** if the four-component model misbehaves, report the components side by
side without combining. Refusing to collapse incommensurable quantities is a
defensible position and a better paper than a fudged index.

## S9 — tools and schemas · 2 h

```
Fill src/tools.py with the eleven names in contracts.TOOL_NAMES, each returning a
small ToolResult dict. Rewrite src/schemas.py to build specs from flat scalar
pydantic models — no nested models, no $ref in the emitted schema; assert that in
a check. list_datasets and describe_layer surface provenance so the model can
cite sources. describe_alignment returns the AlignmentReport. acquire_dataset
triggers a live retrieval by name — that tool is the autonomy showcase and
belongs in the trace figure. No tool returns geometry; assert no serialised
result contains a coordinate list. Rewire src/demo.py onto the registry.
```

**Gate:** `/gate tools` — print the emitted tool specs and grep them for `$ref`.
**Cut:** ship nine tools, dropping `compare_scenarios` and `validate_answer` from
the model-visible surface and calling them from the harness. The analysis still
happens; only the model's access changes.

## S10 — sandbox and repair loop · 2.5 h

```
Implement src/sandbox.py against the Sandbox protocol. run() executes
model-written Python in a subprocess with a timeout and a restricted working
directory, aligned layers available by name through a preamble the sandbox
injects. Return a populated CodeRun including the full traceback on failure.
repair_loop() hands the traceback back to the model, bounded at three attempts,
accumulating a CodeSession. Instrument all of it: attempts per request, first-run
failure rate, repair rate, and an error taxonomy — which errors it makes is more
interesting than how many, so classify them. State in the docstring that this is
a timeout-and-working-directory boundary, not a security boundary.
```

**Gate:** `/gate sandbox` — give it three questions the curated tools cannot
answer and paste the real transcripts, including at least one repair.
**Cut:** if it is unreliable by evening, **keep it and report the failure rate.** A
measured 40% success rate is a result. Silence is a missing criterion.

Then: `/rubric`.

---

# Day 4 — Sunday

## S11 — critic, preferences, loop · 2.5 h

```
Implement src/critic.py against the Critic protocol and rewire src/agent.py
around it. check() extracts every numeral from a draft answer and confirms each
appears in a logged tool result within tolerance, producing CriticFindings for
those that do not. invariants() checks the domain rules from S8. Findings go back
to the model for revision, bounded at two cycles, every firing logged.
Add ask_user_preferences as real interaction: elicit the weighting before
computing a priority ordering, and recompute when it changes. Behaviour, not a
paragraph. Raise MAX_ITERATIONS to 15. Keep the stub-LLM loop tests.
```

**Gate:** `/gate critic` — show one run where the critic fires and the revision
fixes the answer. If it never fires, feed it a deliberately wrong draft and prove
it catches that.
**Cut:** run the critic in report-only mode and publish its findings as a results
table. Detection without automated revision is still a real contribution.

## S12 — fault injection · 2 h

```
Implement src/faults.py and src/experiments/faults.py against FaultConfig and
FaultEvent. faults.py wraps retrieval so a seeded fraction of calls time out,
return 500, return empty, return the wrong CRS, or return a truncated page. The
seed makes runs reproducible. experiments/faults.py runs the suite clean and
under each fault kind at two rates, and writes a table: fault kind, rate, runs
completed, runs completed with correct numbers, mean extra turns, recovery rate.
Also port src/robustness.py to src/experiments/behaviour.py so its four
adversarial scenarios run against real data. Keep the prompt-injection scenario.
```

**Gate:** `/gate faults` — the table exists with real numbers in it.
**Cut:** two fault kinds at one rate. Timeout and wrong-CRS are the two the rubric
names.

## S13 — scenario sweep and transfer · 2.5 h

```
Implement src/scenarios.py and src/experiments/transfer.py. scenarios.py sweeps
every WeightPreset against every HazardScenario, emitting ScenarioRows. Fill
displaced_geoids — the units another preset prioritises and this one does not.
Reporting only who gains is the most common way to fail criterion SG.
experiments/transfer.py runs the entire pipeline on TRANSFER_AREA with no code
change. Record what worked, what broke, and what the agent recovered from
unaided.
```

**Gate:** `/gate scenarios` — trade-off table and transfer report both written to
`outputs/paper/`.
**Cut:** if transfer breaks early, spend one hour making the failure legible and
stop. The failure mode is the finding. Do not spend the day making the second
county work.

## S14 — figures and the numbers file · 2 h

```
Write src/figures.py producing exactly three PDFs into paper/figs/: architecture
(hand-draw this one, do not generate it), the risk surface with the
raster-to-vector join visible, and the trade-off curve.
Write src/experiments/report.py emitting outputs/paper/numbers.json: every count,
rate and total the paper will cite, keyed by the section that uses it.
```

**Gate:** `/gate figures` — `outputs/paper/numbers.json` exists and every `\TD{n}`
in `paper/main.tex` has a corresponding key.
**Cut:** ugly figures beat unfinished figures.

Then: `/rubric` one final time, and fix only what it names.

---

## Standing decisions

- **`paper/` currently sits outside the repository**, at `oasis/paper/`. S14's gate
  reads `paper/main.tex` and writes `paper/figs/`, both relative to the project
  root. Move it inside before Sunday or that gate cannot run.
- **A cut line is a decision, not a failure.** Each one above was chosen in advance
  precisely so that a slip at 6pm is a plan. Take the cut and log it.
- **Never delete a `failures.md` entry** because it turned out embarrassing. Add a
  new dated entry if the same thing recurs.
