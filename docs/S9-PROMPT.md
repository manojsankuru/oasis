# S9 — Tools and schemas · 2 h

FIRST: start with cwd = `C:\Projects\oasis\geo-agent`, not the parent oasis folder. The
`invariant-reviewer` subagent and the `/gate`, `/failure`, `/rubric` commands live in
`geo-agent/.claude` and are invisible from the parent. This bit S6 and it bit S8 twice —
once because the session resumed with cwd reset to the parent and a script died with
`FileNotFoundError: mutate.py`.

Two things S8 established about the reviewer, so you do not rediscover them:

- **The subagent cannot be found by name.** `subagent_type: "invariant-reviewer"` returns
  "Agent type not found". Read `.claude/agents/invariant-reviewer.md` and run a
  **general-purpose** agent with that file's text pasted in as its standing instructions.
  Do not skip the review — it found three defects in S8 behind 366 green checks and a
  125-of-125 mutation sweep, and one of them was a silent-wrong-answer bug.
- **Run it on sonnet.** The S8 attempt on opus died mid-review with a 429 session limit
  and returned nothing. Sonnet completed the same review in 8 minutes.

## State of the tree

S8 is finished, gate-verified and committed: `0ddc5c4 "hazard vulnerability risk"`.
`git status` should be clean.

- `data/snapshot/` — the retrieved snapshot. **Never hand-edit it.** Last written 28 Aug.
- `data/derived/` — 17 MB of inundation rasters, gitignored, **recomputed on every run**.
  Not an input. Do not read from it directly; reach it through `HazardSurface`.
- `data/snapshot-backup/` — 39 MB safety copy, gitignored. Not an input.

## Schedule, already decided, do not re-litigate

Mon 31 Aug was converted from a paper day into a build day. S8–S9 Sat 29, S10–S12 Sun 30,
S13–S14 Mon 31, paper Tue 1 – Wed 2, submit Wed 3, deadline Fri 4. S9 is 2 h. If it runs
past 2.5 h, take the cut line below rather than eating S10's slot — S10 is the sandbox and
it is the core of criterion TU.

## Read first, in this order

`CLAUDE.md`, then `src/contracts.py` (`TOOL_NAMES`, `ToolResult`, `Col`), then
`src/tools.py` (45 lines — read `surface_faults` and understand what it is telling you),
then `src/schemas.py` (the file you are replacing), then `agent.execute_tool` and
`agent.SYSTEM_PROMPT`, then `src/verify.py`. Then the **last eight entries** of
`docs/failures.md` — those are S8's, and two of them are about guards that could not fail.

`contracts.py` is frozen and has not been touched since S1. Keep it that way.

## What S9 is

Implement the **eleven names in `contracts.TOOL_NAMES`** in `src/tools.py`, each returning
a small `ToolResult` dict, and rewrite `src/schemas.py` to build specs from flat scalar
pydantic models with no `$ref` anywhere in the emitted JSON.

This is the session that turns a working analysis into an agent that can be asked a
question. Everything the tools report already exists and is verified; **a tool that
recomputes a number is a bug**, because `critic.py` in S11 traces every reported number
back to a logged tool result and a second computation path is a second answer.

## Step 0, before writing any code

Run the five self-checks and confirm 369 PASS, exit 0 on each:

```
python -m src.align --check          # 145
python -m src.hazard --check         #  65
python -m src.vulnerability --check  #  57
python -m src.risk --check           #  68
python -m src.pipeline --check       #  34
```

Then `python -m src.pipeline` and read the report. Then start `python mutate.py` — it takes
**~40 minutes** for 128 mutations across five modules and must report 128/128 caught. Start
it and read `tools.py`/`schemas.py`/`agent.py` while it runs.

Then run `python -m src.demo`. **It refuses to start and tells you why.** That refusal is
the thing S9 deletes. Read it first; it is the specification.

## Measured, do not re-derive

- **`pipeline.run()` is the only thing that computes anything**, and it takes **41 s** for
  three scenarios. A tool that calls it per invocation makes a six-iteration agent loop
  take four minutes. Build one `PipelineResult` per run and have every tool read from it.
  Decide this deliberately and write down why.
- 99 tracts, 261 block groups, 420,264 residents. **98 tracts scored, 1 unscored** —
  `45019990100`, a 9900-series water tract with zero residents and no vulnerability index.
  Every tool that returns a ranking must say how many units it omitted and why.
- Exposed population: **99,037 / 187,349 / 303,839** at 1.5 / 3.0 / 5.0 m of surge.
- Three scenarios, `hazard.HAZARD_SCENARIOS`: `surge_1_5m`, `surge_3_0m`, `surge_5_0m`.
- Three presets, `vulnerability.WEIGHT_PRESETS`: `svi_equal` and `svi_themes` (both
  `origin="published_index"`, verified CDC/ATSDR SVI url) and `evacuation_capacity`
  (`origin="authors"`). `svi_equal` is `DEFAULT_PRESET`.
- `flood_zones` is **DEGRADED** — FEMA answered HTTP 200 with an ArcGIS error body. Test it
  with `align.is_degraded`, never a row count. `hazard.vector_hazard_status()` already
  produces the sentence a tool should surface.
- The elevation raster carries **zero nodata cells** of 7,997,535. Any rule about holes is
  unfalsifiable on this county and needs a synthetic fixture.
- Written artifacts: `outputs/risk_<scenario>_<preset>.csv` (99 rows × 15 cols),
  `outputs/tradeoff.csv` (9 rows), `outputs/pipeline_report.txt`.

## Contract notes, decided earlier, do not undo

- **`TOOL_NAMES` is frozen at eleven.** Do not add a twelfth. A twelfth idea is a parameter
  on an existing tool.
- **`ToolResult` is a small JSON-serialisable dict and never contains geometry.** Invariant
  3. `pipeline.COORDINATE_PATTERN` already names the trap: TIGERweb ships `CENTLAT`,
  `CENTLON`, `INTPTLAT`, `INTPTLON` as ordinary text attributes, so a coordinate wearing a
  column name is still a coordinate. Reuse that pattern rather than writing a second one.
- **Invariant 4: scalar arg types only** — `str`, `float`, `int`, `bool`. A nested pydantic
  model emits `$ref`/`$defs`, which some OpenAI-compatible servers reject. Assert the
  absence of `$ref` in the emitted spec, in a check, on the real serialised JSON.
- **Never write a column name as a string literal.** Use `contracts.Col`.
- `agent.execute_tool` validates arguments with `schemas.TOOL_ARG_MODELS[name]` and then
  calls `tools.TOOL_FUNCTIONS[name]`. A name present in one and absent from the other
  currently raises `KeyError`, which a bare `except Exception` turns into an error dict the
  model then has to reason about. `tools.surface_faults()` is what makes that loud at
  startup. **Keep the guard; delete its reason.**
- `agent.SYSTEM_PROMPT` still names `list_layers` and talks about shelters and a prototype
  that no longer exists. Rewrite it for the real tool names, or the model will call a tool
  that is not there. It also still says "All distances returned by the tools are in
  kilometers" — check that against what you actually return.

## Patterns to reuse, not reinvent

- `verify.discipline_checks(sys.modules[__name__])` gives you six checks for one line:
  metric-CRS discipline, no self-reprojection, type hints, no hardcoded study area, a
  non-empty scan, and the scanner auditing itself.
- `verify.refuses(call, kind, phrase)` — **match the message, not just the exception type.**
  Three S7 mutations survived by raising the right type from the wrong place.
- `_self_check() -> list[tuple[str, bool]]` ending in `verify.report(checks)`, run as
  `python -m src.tools --check`, non-zero exit on failure.
- Report what was **examined** beside what was **changed**. A field that is only ever zero
  cannot be told apart from one that was never implemented.
- **Add mutations for every new module.** `mutate.py` now has a `TARGETS` dict keyed by
  module name — add `"tools"` and `"schemas"` entries and keep it at zero survivors. A
  module listed with an empty list prints `0/0 caught`, which is the harness telling you
  its checks have never been broken on purpose.

## Read docs/failures.md before you write checks

The last eight entries are S8's. Four will bite again:

- **8 of 52 mutations survived the first S8 sweep.** Six survived because this county
  cannot express the error — its raster has no holes, its block-group populations are
  Census-controlled to agree exactly, its one degraded layer is also empty. A guard whose
  triggering condition cannot occur here is untested unless a fixture forces it.
- **`verify.metric_bypasses` is satisfied by one mention of `to_working_crs`.** It asks
  whether a function that does a metric operation mentions the helper *anywhere*. A
  function projecting two frames has two obligations and one observable. If anything in
  `tools.py` projects a frame, that hole is still open — write the fixture.
- **The harness was hardcoded to this county's 99 tracts** in five places, which would have
  broken the S13 transfer run's verification with no defect present. Derive every
  denominator from the loaded frame; print the county-specific number, do not assert it.
- **A mutation that produces identical answers is not a mutation.** Deleting a
  `.where(complete)` beside a Float64 sum is a no-op, because `pd.NA` already propagates.
  Quote no mutation score without naming what it cannot reach.

## What S9 is worth, by criterion

`/rubric` at the end of S8 named **IR the weakest** — both feedback cycles are still
unbuilt (sandbox S10, critic S11). S9 does not fix IR directly, but it is the gate to both:
neither the sandbox nor the critic has anything to attach to until the tool surface exists.

- **TU** — `acquire_dataset` triggering a live retrieval by name is the autonomy showcase
  and belongs in the trace figure. `list_datasets` and `describe_layer` must surface
  `Provenance` so the model can cite its sources; `describe_alignment` returns the
  `AlignmentReport`, which is the "no manual cleaning" claim made legible to a reviewer.
- **SG** — `ask_user_preferences` is the HITL tool and the only one that elicits a
  weighting before deciding. `compare_scenarios` is where "who loses" reaches the model:
  `risk.compare_presets(..., units=tracts)` already returns `ScenarioRow.displaced_geoids`
  and **the `units=` argument is load-bearing** — without it two of the three presets return
  identical rows. That was an S8 defect; do not reintroduce it by dropping the argument.

## Gate

`/gate tools`. Print the emitted tool specs and grep them for `$ref` — paste the real
output, not a description. Assert in a check that no serialised tool result contains a
coordinate list. `python -m src.demo` must **run** rather than refuse, and produce a real
answer citing real numbers from real tool results. For every reported number, state whether
it is zero because nothing needed doing, non-zero because something did, or not yet
implemented — and how you know.

## Cut line, decided in advance

Ship **nine** tools rather than eleven if time is short: drop `compare_scenarios` and
`validate_answer` from the model-visible surface and call them from the harness instead.
The analysis still happens and the trade-off table is still produced; only the model's
access to it changes. Say so in the limitations rather than shipping eleven half-built ones.

## End the session the way the plan says, in this order

1. Run the invariant-reviewer (general-purpose agent, sonnet, with the agent file's text).
   Do not skip it because the change looks small — 5 defects in S6, 7 in S7, 3 in S8.
2. `/gate tools`. Print the real output.
3. Propose a one-line lowercase commit message and wait for approval. S6 used
   "alignment core", S7 "alignment complete", S8 "hazard vulnerability risk".
4. `/failure` the moment anything breaks, with the real error text.
5. `/rubric` before you stop.

## Three things already costing points, cheap to fix if S9 finishes early

- **Type hints.** CLAUDE.md says every function. Measured today: `agent.py` 0 of 3,
  `llm_client.py` 0 of 5, `schemas.py` 0 of 2, `trace.py` 0 of 10, `robustness.py` 2 of 13.
  `schemas.py` and `agent.py` are both inside S9's blast radius — annotate them while you
  are there, and add `verify.discipline_checks` to whatever check covers them.
- **README.md** "Status" says Session S8 of 14. Update it, and delete the paragraph saying
  `python -m src.demo` refuses to start once it no longer does.
- **`src/robustness.py`** still needs porting to `src/experiments/behaviour.py` (S12). Its
  prompt-injection-via-data case is one of the more original things in this repository —
  keep it in particular.
