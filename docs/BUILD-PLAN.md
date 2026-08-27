# Four-day build plan

Thu 27 Aug -> Sun 30 Aug. Paper Mon 31 - Wed 2 Sep. Submit Wed 3 Sep.
Deadline Fri 4 Sep 11:59 PM AoE, kept empty as margin.

Fourteen sessions. One module per session. Each has a paste-ready prompt, an
acceptance gate, and a cut line decided in advance so that a slip at 6pm is a
plan and not a panic.

---

## How to drive Claude Code through this

1. **One module per session, then `/clear`.** Drift starts in long contexts. A
   session that has already built three modules will happily rename the second
   one to make the fourth compile.
2. **`src/contracts.py` is frozen.** If a session wants to change a field or a
   signature there, that is a separate commit with a one-line reason, and every
   caller updates with it. Say this out loud in the prompt when you sense a
   session drifting toward "I'll just adjust the dataclass".
3. **End every session the same way:** run the `invariant-reviewer` subagent on
   the diff, then `/gate <module>`, then commit. Do not skip the reviewer
   because the change looks small — hardcoded study areas and CRS bypasses
   arrive in small changes.
4. **`/rubric` at the end of each day.** It names the weakest criterion. That is
   the next morning's first hour, not a nice-to-have.
5. **`/failure` the moment something breaks**, with the real error text. You
   will not remember it on Tuesday, and §3.7 of the paper is made of these.
6. **Paste the relevant part of `docs/DATA.md` into acquisition prompts.** The
   endpoint quirks are the part Claude Code cannot guess, and the `outSR` trap
   in particular will cost you an afternoon if it is not in front of it.

---

# Day 1 — Thursday 27 August

Foundation and live retrieval. This is the day with the most unknowns in it, so
it is deliberately front-loaded.

## S1 — Demolition and study-area parameter  *(45 min)*

> Read `CLAUDE.md` and `src/contracts.py` first; `contracts.py` is frozen and
> you implement against it.
>
> Remove `src/create_sample_data.py` and every file in `data/`. They are
> synthetic and they are what disqualifies the live-data claim.
>
> Rewrite `src/config.py` around a `StudyArea` dataclass holding `name`,
> `state_fips`, `county_fips` and `working_crs`. Define `STUDY_AREA` and
> `TRANSFER_AREA`. The study-area bounding box must be *derived* from the
> retrieved tract layer at run time — there must be no literal bbox anywhere.
> Add `CENSUS_API_KEY` to settings and to `.env.example` as a placeholder.
>
> Update `requirements.txt` to add `requests`, `tenacity`, `rasterio`, `numpy`,
> `pandas`, `matplotlib`. Install and confirm they import alongside the existing
> stack in the same interpreter.
>
> Then `git init`, add an MIT `LICENSE`, and make the first commit. Before
> committing, confirm that `.env`, `application_default_credentials.json` and
> anything matching `*credentials*.json` are ignored, and that `git status`
> shows none of them.

**Gate.** `git log` shows one commit. `data/` contains only `.gitkeep`.
`grep -rniE "charleston|45019|-80\.[0-9]" src/` returns hits in `config.py` only.
`git status` lists no credential file.

**Cut line.** None. This session cannot slip; everything else depends on it.

## S2 — Provenance and the dataset registry  *(1 h)*

> Implement `src/provenance.py` and `src/registry.py` against the `Provenance`
> and `DatasetRecord` contracts.
>
> `provenance.py`: serialise to and from JSON, and read/write
> `data/snapshot/manifest.json` as a list of records. Timestamps are ISO-8601
> with timezone.
>
> `registry.py`: datasets are reachable *only* by name through the registry, and
> registering one without a `Provenance` raises. Loading a vector or table
> dataset returns it already in the working CRS. Cache the projected frame, not
> just the raw one.
>
> Write a check that round-trips a manifest through disk and asserts equality.

**Gate.** `/gate registry`. No dataset reachable without provenance.

**Cut line.** None. Every later session writes through this.

## S3 — ArcGIS vector retrieval  *(1.5 h)*

> Implement `discover_arcgis_layers` and `fetch_arcgis_vector` in
> `src/acquire.py` against the `Acquirer` protocol. Read
> `docs/DATA.md` sections 1, 2 and 6 first and follow them exactly, including
> the `outSR` trap.
>
> Requirements: explicit timeout on every call; bounded retry with backoff via
> `tenacity`; `resultOffset` paging when the response is truncated; assert the
> CRS actually received matches the CRS requested and record both in
> `Provenance`; a JSON error body returned with HTTP 200 must be detected and
> raised rather than written to disk.
>
> Then retrieve tracts and block groups for `STUDY_AREA` for real. Print the
> feature counts and the provenance records. Do not mock the network anywhere in
> this session — a mocked retrieval test proves nothing here.

**Gate.** `/gate acquire`. Real feature counts printed, both layers, real CRS
assertion, provenance written.

**Cut line.** If TIGERweb paging fights you, retrieve without paging, assert the
count is under `maxRecordCount`, and log the limitation. Fix it on Day 4 only if
the transfer county needs it.

## S4 — ACS retrieval with run-time variable resolution  *(1.5 h)*

> Implement `resolve_acs_variables` and `fetch_acs`. Read `docs/DATA.md` §3.
>
> `resolve_acs_variables` fetches the ACS variables catalogue and matches
> canonical column names (from `contracts.VULNERABILITY_INDICATORS`) to variable
> ids by concept and label. **No variable id is hardcoded anywhere.** Log every
> resolution — which pattern matched which id — into provenance notes, because
> that log is the evidence for the autonomy criterion.
>
> Fetch estimates and their margins of error, at tract level and at block-group
> level for the one attribute we will apportion.
>
> Sentinel handling belongs in `align.py`, not here; retrieval returns what the
> API returned.

**Gate.** `/gate acquire`. Real rows returned. Print the resolved id for each
canonical column and eyeball that they are the variables you meant.

**Cut line.** If concept matching is unreliable by mid-afternoon, resolve a
*shortlist* of candidate ids automatically and pick from it with a documented
tie-break — still no hardcoded ids, but a smaller search. Do not fall back to
pasting variable codes; that is the manual-cleaning failure in another costume.

## S5 — Raster, facilities, and the snapshot  *(1.5 h)*

> Implement `fetch_arcgis_raster` and `fetch_osm`. Read `docs/DATA.md` §4, §5.
>
> Raster: compute `size` from the derived bbox and a target cell size, cap each
> dimension at 8000 and coarsen if needed, request `imageSR` as the working CRS,
> verify `Content-Type` is an image before writing the GeoTIFF, and open it with
> rasterio to confirm the transform and CRS.
>
> OSM: convert the 4326 bbox to Overpass `south,west,north,east` order in this
> function and nowhere else. Handle HTTP 429 as a retryable outcome.
>
> Then write `src/acquire.py`'s `__main__` so that `python -m src.acquire`
> retrieves all six datasets, writes `data/snapshot/` and `manifest.json`, and
> prints a summary table.

**Gate.** `/gate acquire`. `python -m src.acquire` completes for `STUDY_AREA`;
manifest has six entries with real timestamps; the GeoTIFF opens in rasterio at
the expected CRS.

**Cut line.** If the raster export fights the size cap, drop the target cell size
to 30 m and move on. Resolution is not what is being scored; having a raster is.

**End of Day 1.** `python -m src.acquire` produces a real snapshot. If it does
not, tomorrow starts here and Day 4 loses the transfer run — say so out loud
rather than sliding.

---

# Day 2 — Friday 28 August

Alignment and the risk model. This is the day criterion RB is won.

## S6 — Alignment core  *(2 h)*

> Implement `to_working_crs`, `repair_geometry`, `scrub_sentinels` and
> `join_on_geoid` in `src/align.py` against the `Aligner` protocol, populating
> `AlignmentReport` as you go.
>
> Every field of `AlignmentReport` must be filled by real work: which datasets
> were reprojected and from what, how many geometries were repaired and how many
> dropped, how many sentinels were removed per column, which GEOIDs are unmatched
> on each side of the join, and the temporal span of the inputs.
>
> Unmatched GEOIDs are reported, never silently dropped. Sentinel scrubbing
> covers the full family of Census negative codes, not just `-666666666`.

**Gate.** `/gate align`. Print the full `AlignmentReport` for the real snapshot.
Every non-zero number in it should be one you can explain.

**Cut line.** None — this section *is* a rubric criterion.

## S7 — Zonal statistics and granularity apportionment  *(2 h)*

> Implement `zonal_stats` and `apportion`.
>
> `zonal_stats`: use `rasterio.mask` plus numpy directly — do not add
> `rasterstats` as a dependency, it is a Windows install risk for thirty lines
> of code. Return min, mean, max and cell count per polygon, indexed by GEOID.
> Polygons covering fewer than `contracts.MIN_RASTER_CELLS` cells are counted
> into `AlignmentReport.units_below_cell_threshold` — their statistics are not
> trustworthy and the paper says so.
>
> `apportion`: aggregate the block-group attribute up to tracts by population
> weight, compare against the directly published tract figure, and record the
> percentage discrepancy in `AlignmentReport.apportionment_error`. That
> discrepancy is a result, not a bug — do not tune it away.

**Gate.** `/gate align`. Verify one polygon's zonal mean by hand against the
raster; a spatial result checked only against a previous run of the same code is
not checked.

**Cut line.** If population-weighted apportionment is fiddly, use simple sum
and report the larger error honestly. The reported error is the deliverable.

## S8 — Hazard, vulnerability, risk  *(2.5 h)*

> Implement `src/hazard.py`, `src/vulnerability.py` and `src/risk.py`.
>
> `hazard.py`: bathtub inundation `depth = max(0, surge_height_m - elevation_m)`
> per cell for each `HazardScenario`, summarised per tract through `zonal_stats`.
> Define three scenarios in config with the category-to-height assumption
> written into `HazardScenario.assumption_note`.
>
> `vulnerability.py`: percentile-rank each indicator in
> `contracts.VULNERABILITY_INDICATORS`, combine under a `WeightPreset`. Weights
> are arguments with defaults, never constants in the function body. Every
> indicator gets a docstring sentence saying why it is in the index.
>
> `risk.py`: report hazard, exposure, vulnerability and resilience **as separate
> columns** before any combined score, then combine under a preset. A single
> collapsed number without its components is a criterion SG failure.
>
> Define at least three `WeightPreset`s, one of which has
> `origin="published_plan"` or `"published_index"` with a real `origin_url`.
>
> Then write `src/pipeline.py`: snapshot in, tract-level risk table out, no LLM
> involved. This is the deterministic spine everything else calls.

**Gate.** `/gate risk`. `python -m src.pipeline` writes a real table. Sanity:
exposed population never exceeds tract population; inundated fraction lies in
[0, 1]; the ranking changes when you change a weight.

**Cut line.** If the four-component model misbehaves, report the components side
by side without combining them. Refusing to collapse incommensurable quantities
into one score is a defensible position, and a better paper than a fudged index.

**End of Day 2.** A real risk table for a real county, produced with no LLM and
no hand-editing.

---

# Day 3 — Saturday 29 August

The agent surface. This is the day criteria TU and IR are won.

## S9 — Tools and schemas  *(2 h)*

> Replace `src/spatial_tools.py` with `src/tools.py` implementing the eleven
> names in `contracts.TOOL_NAMES`, each returning a small `ToolResult` dict.
> Rewrite `src/schemas.py` to build specs from flat scalar pydantic models — no
> nested models, no `$ref` in the emitted schema; assert that in a check.
>
> `list_datasets` and `describe_layer` surface provenance so the model can cite
> sources. `describe_alignment` returns the `AlignmentReport`. `acquire_dataset`
> triggers a live retrieval by name — that tool is the autonomy showcase and it
> belongs in the trace figure.
>
> No tool returns geometry. Confirm by asserting that no serialised result
> contains a coordinate list.

**Gate.** `/gate tools`. Print the emitted tool specs and grep them for `$ref`.

**Cut line.** Ship nine tools rather than eleven if time is short — drop
`compare_scenarios` and `validate_answer` from the model-visible surface and
call them from the harness instead. The analysis still happens; only the model's
access to it changes.

## S10 — The sandbox and the repair loop  *(2.5 h)*

> Implement `src/sandbox.py` against the `Sandbox` protocol. This is the core of
> criterion TU: *"autonomously write, execute, and debug technical spatial
> analysis logic."*
>
> `run()` executes model-written Python in a subprocess with a timeout and a
> restricted working directory, with the aligned layers available by name
> through a small preamble the sandbox injects. Return a populated `CodeRun`
> including the full traceback on failure.
>
> `repair_loop()` hands the traceback back to the model, bounded at three
> attempts, accumulating a `CodeSession`.
>
> Instrument all of it: attempts per request, first-run failure rate, repair
> rate, and an error taxonomy (`AttributeError`, `CRSError`, shape mismatch,
> timeout). **Which errors it makes is more interesting than how many**, so
> classify them.
>
> Be explicit in the docstring that this is a timeout-and-working-directory
> boundary, not a security boundary. That honesty belongs in the paper too.

**Gate.** `/gate sandbox`. Give it three questions the curated tools cannot
answer and paste the real transcripts, including at least one repair.

**Cut line.** If the loop is unreliable by evening, **keep it and report the
failure rate**. A measured 40% success rate is a result; silence is a missing
criterion.

## S11 — Critic, preferences, and the loop  *(2.5 h)*

> Implement `src/critic.py` against the `Critic` protocol, and rewire
> `src/agent.py` around it.
>
> `check()` extracts every numeral from a draft answer and confirms each appears
> in a logged tool result within tolerance, producing `CriticFinding`s for the
> ones that do not. `invariants()` checks the domain rules from S8. Findings go
> back to the model for revision, bounded at two cycles, every firing logged.
>
> Add `ask_user_preferences` as a real interaction: before computing a priority
> ordering the agent elicits the weighting, and recomputes when it changes. This
> must be behaviour, not a paragraph — criterion TU names human-in-the-loop
> interaction, and a policy statement scores nothing.
>
> Raise `MAX_ITERATIONS` to 15. Keep the existing stub-LLM loop tests; they
> cover id matching and message ordering correctly and cheaply.

**Gate.** `/gate critic`. Show one run where the critic fires and the revision
fixes the answer. If it never fires, feed it a deliberately wrong draft and
prove it catches that.

**Cut line.** If the revision cycle is unstable, run the critic in report-only
mode and publish its findings as a results table. Detection without automated
revision is still a real contribution.

**End of Day 3.** The agent answers a real question about real data, writes and
repairs code at least once, and the critic has fired at least once.

---

# Day 4 — Sunday 30 August

Experiments and artifacts. Nothing new is built after today.

## S12 — Fault injection  *(2 h)*

> Implement `src/faults.py` and `src/experiments/faults.py` against
> `FaultConfig` and `FaultEvent`.
>
> `faults.py` wraps the retrieval layer so a seeded fraction of calls time out,
> return 500, return an empty result, return a layer in the wrong CRS, or return
> a truncated page. The seed makes runs reproducible.
>
> `experiments/faults.py` runs the evaluation suite clean and under each fault
> kind at two rates, and writes a table: fault kind, rate, runs completed, runs
> completed with correct numbers, mean extra turns, recovery rate.
>
> Also port `src/robustness.py` to `src/experiments/behaviour.py` so its four
> adversarial scenarios run against the real data. **Keep the prompt-injection
> scenario** — injection through a data attribute is a genuine and
> under-reported failure mode for GeoAI agents, it is one of the more original
> things in this repository, and it belongs in the paper.

**Gate.** `/gate faults`. The table exists with real numbers in it.

**Cut line.** Two fault kinds at one rate is enough if time is short. Timeout
and wrong-CRS are the two the rubric names.

## S13 — Scenario sweep and transfer  *(2.5 h)*

> Implement `src/scenarios.py` and `src/experiments/transfer.py`.
>
> `scenarios.py` sweeps every `WeightPreset` against every `HazardScenario`,
> emitting `ScenarioRow`s. Fill `displaced_geoids` — the units another preset
> prioritises and this one does not. **Reporting only who gains is the most
> common way to fail criterion SG.**
>
> `experiments/transfer.py` runs the entire pipeline on `TRANSFER_AREA` with no
> code change. Record what worked, what broke, and what the agent recovered from
> unaided. A partial failure reported honestly beats a success you did not
> attempt — do not spend the day making the second county work.

**Gate.** `/gate scenarios`. Trade-off table and transfer report both written to
`outputs/paper/`.

**Cut line.** If transfer breaks early, spend one hour making the failure legible
and stop. The failure mode is the finding.

## S14 — Figures and the numbers file  *(2 h)*

> Write `src/figures.py` producing exactly three PDFs into `paper/figs/`:
> architecture (hand-draw this one, do not generate it), the risk surface with
> the raster-to-vector join visible, and the trade-off curve.
>
> Write `src/experiments/report.py` emitting `outputs/paper/numbers.json`:
> every count, rate and total the paper will cite, each keyed by the section
> that uses it. When you write the paper you cite this file, so that no number
> reaches the paper without a tool result behind it.
>
> Then run `/rubric` one final time and fix only what it names.

**Gate.** `/gate figures`. `outputs/paper/numbers.json` exists and every
`\TD{n}` in `paper/main.tex` has a corresponding key.

**Cut line.** Ugly figures beat unfinished figures. Restyle only if Day 4 ends
early, which it will not.

**End of Day 4.** Every number and figure the paper needs exists on disk. From
Monday you are writing, not building.

---

## What is deliberately not built

Road network and travel time (Track B's named challenge, and the largest
available time sink). Exact optimization solvers. Multi-hazard. Real-time
conditions. Area-weighted apportionment beyond the one demonstration case.
Raster processing beyond zonal statistics. Each of these goes in the paper's
limitations, where a stated cut reads as judgement rather than omission.
