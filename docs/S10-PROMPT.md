# S10 — The sandbox and the repair loop · 2.5 h

FIRST: start with cwd = `C:\Projects\oasis\geo-agent`, not the parent oasis folder. The
`invariant-reviewer` subagent and the `/gate`, `/failure`, `/rubric` commands live in
`geo-agent/.claude` and are invisible from the parent. This bit S6, S8 twice, and S9.

Two things about the reviewer, established in S8 and confirmed in S9, so you do not
rediscover them:

- **The subagent cannot be found by name.** `subagent_type: "invariant-reviewer"` returns
  "Agent type not found". Read `.claude/agents/invariant-reviewer.md` and run a
  **general-purpose** agent with that file's text pasted in as its standing instructions.
- **Run it on sonnet.** The S8 attempt on opus died mid-review with a 429 session limit
  and returned nothing. Sonnet completed S9's review in 10 minutes.

Do not skip it. It found 5 defects in S6, 7 in S7, 3 in S8 and 2 in S9 — and one of the
S9 pair was a live invariant-3 violation sitting behind 108 green checks and a
zero-survivor mutation sweep.

## State of the tree

S9 is finished, gate-verified and committed: `3c11821 "tools and schemas"`.
`git status` shows only `docs/S9-PROMPT.md` and this file as untracked.

- `data/snapshot/` — the retrieved snapshot. **Never hand-edit it.** Last written 28 Aug.
- `data/derived/` — inundation rasters, gitignored, **recomputed on every run**. Not an
  input. Reach it through `HazardSurface`, never by path.
- `data/snapshot-backup/` — gitignored safety copy. Not an input.

## Schedule, already decided, do not re-litigate

S10–S12 Sun 30, S13–S14 Mon 31, paper Tue 1 – Wed 2, submit Wed 3, deadline Fri 4.
S10 is 2.5 h. If it runs past 3 h, take the cut line below rather than eating S11 — the
critic is the other half of criterion IR and neither half exists yet.

## Read first, in this order

`CLAUDE.md`, then `src/contracts.py` (the `Sandbox` protocol, `CodeRun`, `CodeSession`),
then `src/tools.py` — specifically `run_spatial_code`, `BACKING_MODULES`,
`pending_tools()` and `analysis()` — then `src/verify.py`. Then the **last five entries**
of `docs/failures.md`; those are S9's. Then `docs/RUNBOOK.md`, the section headed
"Where you actually are", which was written for you.

`contracts.py` is frozen and has not been touched since S1. Keep it that way.

## What S10 is

Implement `src/sandbox.py` against the frozen `Sandbox` protocol:

```python
def run(self, source: str, *, timeout_s: float = 60.0) -> CodeRun: ...
def repair_loop(self, request: str, *, max_attempts: int = 3) -> CodeSession: ...
```

**`src/tools.py` already calls it, and is committed with green checks. Do not change
that call.** This is the exact shape it expects:

```python
run = sandbox.Sandbox().run(code, timeout_s=timeout_s)
# then reads run.exit_code, run.stdout, run.stderr, run.duration_s, run.error_type
```

So: a class named `Sandbox`, in `src/sandbox.py`, **constructible with no arguments**.

The moment that file exists and imports cleanly, `tools.pending_tools()` stops naming
`run_spatial_code`, the model stops being told the tool is unavailable, and it works.
**You should not need to edit `tools.py` or `schemas.py` at all.** If you find yourself
wanting to, stop and work out whether the protocol is the thing that is wrong — and if it
genuinely is, change `contracts.py` first, in its own commit, with a one-line reason.

## Step 0, before writing any code

Run the seven self-checks and confirm **525 PASS, exit 0** on each. About ten minutes.

```
python -m src.align --check          # 145
python -m src.hazard --check         #  65
python -m src.vulnerability --check  #  57
python -m src.risk --check           #  68
python -m src.pipeline --check       #  34
python -m src.schemas --check        #  38
python -m src.tools --check          # 118
```

**Do not run the bare `python mutate.py`.** It is now 166 mutations across seven modules
and takes about an hour, and nothing you are about to touch changes any of them. Run
`python mutate.py sandbox` at the end, once you have written the entries.

Then run `python -m src.demo` and read the transcript. It works today with nine of the
eleven tools. Note what it does **not** do: it never writes a line of code, because the
tool that would let it reports itself as not built. That refusal is what S10 deletes.

## Measured, do not re-derive

- **`pipeline.run()` is the only thing that computes anything.** 17–41 s for three
  scenarios, depending on the OS file cache. `tools.analysis()` caches one
  `PipelineResult` per process. **Use it. Do not call `pipeline.run()` yourself** — a
  second run is a second answer, and `critic.py` in S11 traces every reported number back
  to one logged tool result.
- The cleaned layers the sandbox has to expose, and their shapes:

  | name | rows × cols | type |
  |---|---|---|
  | `tracts_joined` | 99 × 12 | GeoDataFrame |
  | `block_groups_joined` | 261 × 7 | GeoDataFrame |
  | `tracts` | 99 × 18 | GeoDataFrame |
  | `block_groups` | 261 × 19 | GeoDataFrame |
  | `facilities` | 477 × 97 | GeoDataFrame |
  | `flood_zones` | 0 × 1 | GeoDataFrame (DEGRADED — an absence of data, not of hazard) |
  | `acs` | 99 × 124 | DataFrame |
  | `acs_block_groups` | 261 × 10 | DataFrame |

- Working CRS is **EPSG:5070**. Everything in `*_joined` is already projected, so model
  code measuring a distance on those frames gets metres.
- 99 tracts, 261 block groups, 420,264 residents. 98 tracts scored, 1 unscored —
  `45019990100`, a water tract with no residents.
- The interpreter is `.venv\Scripts\python.exe`. `mutate.py` already falls back to
  `sys.executable` when it is missing; do the same rather than hardcoding a path.

## The decision you have to make deliberately and write down

**How does model-written code reach the layers?**

Three options. The one you pick belongs in the module docstring with its reason:

1. The child imports `src.pipeline` and rebuilds. 17–41 s per attempt, so a three-attempt
   repair loop costs two minutes of wall clock before the model sees anything. Wrong.
2. The parent dumps `tools.analysis()`'s frames to parquet in a temp directory once, and
   injects a small preamble that reads them back by name. Fast, and the child never opens
   anything under `data/`.
3. Pipe the frames through stdin. Geometry does not survive it.

(2) is almost certainly right. Whatever you choose, say why — S9's equivalent decision
(one `PipelineResult` per process, every tool reads from it) is the thing that made the
tool surface fast enough to use, and it is written down for the same reason.

**Never write into `data/`.** The dump goes into a temp directory the sandbox owns and
removes.

## Contract notes, decided earlier, do not undo

- **`TOOL_NAMES` is frozen at eleven** and `run_spatial_code` is already one of them. Do
  not add a twelfth. A twelfth idea is a parameter on an existing tool.
- **Invariant 3 now has a channel no existing check covers.** `run_spatial_code` returns
  the child's **stdout straight to the model**. Model-written code that prints a geometry,
  a coordinate pair, or a whole GeoDataFrame breaks invariant 3 — and no S9 check can see
  it, because with `sandbox.py` absent that tool returns a refusal and the coordinate scan
  never meets real output. **Decide what the sandbox does about this and prove it with a
  fixture.** `tools.coordinate_faults(payload)` already exists, takes any JSON-shaped
  value, and refuses four shapes including a coordinate pair spelled out in prose. This is
  the single most likely defect in S10.
- **Say in the docstring that this is a timeout-and-working-directory boundary, not a
  security boundary.** It executes model-written Python in a subprocess. That honesty
  belongs in the paper's limitations too.
- Type hints on every function. `verify.discipline_checks(sys.modules[__name__])` gives
  six checks for one line.
- Never write a column name as a string literal. Use `contracts.Col`.
- Errors return `{"error": ..., "detail": ...}` rather than raising, so the model can
  recover. A traceback is a *result*, not an exception to propagate.

## The trap that will cost you an hour if you do not read this

`mutate.py`'s `run_check` calls `subprocess.run(..., capture_output=True)` **with no
timeout of its own.** If `python -m src.sandbox --check` spawns a child that hangs, the
mutation harness hangs forever, silently, with no output — and you will not be able to
tell it from a slow run.

Every subprocess the sandbox starts must carry a timeout **and be killed when it
expires**. On Windows a `subprocess.run(timeout=...)` that fires leaves the child alive
unless you kill it. Test that path explicitly with a fixture that sleeps past its own
deadline, and assert the process is gone afterwards.

## Read docs/failures.md before you write checks

The last five entries are S9's. Five lessons will bite again:

- **A check that compares a function against itself passes whatever that function does.**
  Three of the four S9 mutation survivors were exactly this. If a check reads the
  sandbox's output and compares it against something the sandbox produced, it proves two
  call sites agree — which they always will — not that either is right. Compare against
  source you wrote by hand and an outcome you stated before the call.
- **Test the call site, not only the function.** Deleting the `invalidate()` call inside
  `acquire_dataset` left all 108 checks green, because the check called `invalidate()`
  directly. `repair_loop` will call `run`; check the loop, not just `run`.
- **A guard whose triggering condition cannot occur here is untested unless a fixture
  forces it.** Do not wait for model-written code to fail in the right way. Write the
  failing source yourself: a `NameError`, a timeout, a non-zero exit with no traceback, a
  `SyntaxError` that never runs, and code that writes to stdout and stderr at once.
- **A mutation that produces identical answers is not a mutation.** Quote no mutation
  score without naming what it cannot reach.
- **Enumerating the shapes a violation can take is not the same as covering them.** The S9
  coordinate guard listed three shapes and the reviewer found a fourth, live, in a
  provenance note. Ask what shape you have not thought of, then go looking for it.

## What S10 is worth, by criterion

`/rubric` at the end of S9 named **IR the weakest**, for the same reason it did after S8:
the architecture diagram in `CLAUDE.md` shows two feedback loops and neither exists. S10
builds the first one.

- **TU** — this is the criterion's own wording: *"autonomously write, execute, and debug
  technical spatial analysis logic."* A real repair — first attempt fails, traceback goes
  back, second attempt works — is the highest-value artifact left in the build and belongs
  in the trace figure.
- **IR** — one of the two feedback cycles. **Instrument all of it**: attempts per request,
  first-run failure rate, repair rate, and an **error taxonomy**. *Which* errors the model
  makes is more interesting than how many, so classify them: `NameError`,
  `AttributeError`, `KeyError`, `CRSError`, shape mismatch, timeout. `CodeRun.error_type`
  is the field the contract already gives you for it.

## Gate

`/gate sandbox`. Give it **three questions the curated tools cannot answer** and paste the
**real transcripts**, including **at least one repair**. If the model never fails on its
own, hand it a genuinely harder request rather than faking a failure.

Also prove, with real output:

- the timeout fires, and the child is dead afterwards
- a non-zero exit is reported rather than swallowed
- a full traceback reaches `CodeRun.stderr`, not a truncated one
- what the sandbox does with stdout that contains a coordinate
- `python -m src.tools` no longer lists `run_spatial_code` as `[PENDING]`, and
  `tools.surface_faults()` is still empty

## Cut line, decided in advance

If the loop is unreliable by evening, **keep it and report the failure rate.** A measured
40% success rate is a result; silence is a missing criterion. Ship `run()` working and
`repair_loop()` bounded at one attempt rather than shipping neither.

## End the session the way the plan says, in this order

1. Run the invariant-reviewer (general-purpose agent, sonnet, with the agent file's text).
   Do not skip it because the module is small — S9's diff was two files and it found two
   real defects, one of them a live invariant violation.
2. Add a `"sandbox"` entry to `mutate.py`'s `TARGETS` and run `python mutate.py sandbox`
   to zero survivors. A module listed with an empty list prints `0/0 caught`, which is the
   harness telling you its checks have never been broken on purpose.
3. `/gate sandbox`. Print the real output, not a description of it.
4. Propose a one-line lowercase commit message and wait for approval. S7 used
   "alignment complete", S8 "hazard vulnerability risk", S9 "tools and schemas".
5. `/failure` the moment anything breaks, with the real error text.
6. Update `docs/RUNBOOK.md` "Where you actually are" and README's Status block. S9 wrote
   both in a format S10 can follow.
7. `/rubric` before you stop.

## Three things already costing points, cheap to fix if S10 finishes early

- **`src/robustness.py`** — 2 of 13 functions annotated, imported by nothing, still not
  ported to `src/experiments/behaviour.py` (S12). Its prompt-injection-via-data case is
  one of the more original things in this repository, and a reviewer browsing `src/` finds
  the file and reads it as abandoned.
- **The transfer run has never been executed.** "Runs on a second county with no code
  change" is a quarter of criterion RB and is currently a claim, not a measurement.
  `src/experiments/` does not exist. Pointing the pipeline at `config.TRANSFER_AREA` —
  even if it fails — turns it into a result. Needs a live acquire for the second county,
  so budget 45 minutes.
- **`acquire_dataset` has never actually been triggered by the model.** It is exercised
  offline only, with a stubbed retriever. One demo question that forces a real retrieval,
  kept as a transcript, is the autonomy showcase the trace figure needs — and it is the
  one TU claim in S9 that is built but undemonstrated.
