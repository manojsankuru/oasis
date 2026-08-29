# Runbook — how to drive the four-day build

`BUILD-PLAN.md` says *what* each session does. This says *what you type*, in what
order, and what to do when a session runs long. Read the session's full prompt
from `BUILD-PLAN.md`; the prompts below are the short form plus the ritual.

---

## Where you actually are

Checked Sat 29 Aug 11:50, at the end of S6.

| | |
| --- | --- |
| Plan | Day 3 = Sat 29 Aug, S9-S11. |
| Reality | S1-S6 committed. S6 finished Sat 29 Aug 11:50. |
| Built | `config`, `contracts`, `schemas`, `llm_client`, `agent`, `trace`, `tools` (empty), `provenance`, `registry`, `acquire`, `align` |
| Missing | `hazard`, `vulnerability`, `risk`, `sandbox`, `critic`, `scenarios`, `faults`, `figures` |

**You are two calendar days behind, on the morning of Day 3.** Eight sessions and
18 hours remain (S7 2, S8 2.5, S9 2, S10 2.5, S11 2.5, S12 2, S13 2.5, S14 2),
against 1.5 days to Sun 30 Aug. That does not fit, and the plan says to decide
rather than slide.

**Decision: spend one paper day. Mon 31 Aug becomes a build day.** Compressing 18
hours into 1.5 days is not real, and cutting a session is worse -- S6 and S7 are
criterion RB and the plan gives them no cut line, S13's transfer run is a quarter
of RB on its own, and S12's fault runs are criterion IR. The paper is a short
paper and two days is enough for it.

| Day | Sessions | Hours |
| --- | --- | --- |
| Sat 29 (from 12:00) | S7, S8, S9 | 6.5 |
| Sun 30 | S10, S11, S12 | 7 |
| Mon 31 (was paper day 1) | S13, S14 | 4.5 |
| Tue 1 - Wed 2 Sep | paper | -- |
| Wed 3 Sep | submit | -- |

Deadline is Fri 4 Sep 11:59 PM AoE, so submitting Wed 3 keeps a full day of
margin. **The next thing that dies is S14's figures, not S13's transfer run** --
figures can be cut to the two the paper cannot do without. Decide that on Monday
morning, not Monday night.

---

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
