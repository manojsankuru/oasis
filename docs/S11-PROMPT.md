# S11 — The critic and the revision cycle · 2.5 h

FIRST: start with cwd = `C:\Projects\oasis\geo-agent`, not the parent oasis folder. The
`invariant-reviewer` subagent and the `/gate`, `/failure`, `/rubric` commands live in
`geo-agent/.claude` and are invisible from the parent. This bit S6, S8 twice, S9 and S10.

Three things about the reviewer, established in S8 and confirmed in S9 and S10, so you do
not rediscover them:

- **The subagent cannot be found by name.** `subagent_type: "invariant-reviewer"` returns
  "Agent type not found". Read `.claude/agents/invariant-reviewer.md` and run a
  **general-purpose** agent with that file's text pasted in as its standing instructions.
- **Run it on sonnet.** The S8 attempt on opus died mid-review with a 429 session limit
  and returned nothing. Sonnet completed S9's review in 10 minutes and S10's in 8.
- **Give it the context it would otherwise re-derive**, and tell it which invariant is
  most at risk in this module. S10's review was pointed at invariant 3 and returned two
  real violations in ten minutes; an unpointed review spends its budget re-reading
  `align.py`.

Do not skip it. It found 5 defects in S6, 7 in S7, 3 in S8, 2 in S9 and 2 in S10 — and
S10's pair were both live invariant-3 violations sitting behind 94 green checks and a
zero-survivor mutation sweep.

## State of the tree

**S10 is finished and gate-verified but NOT COMMITTED.** The proposed message is
`sandbox and repair loop`. Commit it before you start, in its own commit, or S11's diff
will contain two sessions and the reviewer will review both.

`git status` shows:

```
 M README.md
 M docs/RUNBOOK.md
 M docs/failures.md
 M mutate.py
?? docs/S9-PROMPT.md
?? docs/S10-PROMPT.md
?? docs/S11-PROMPT.md
?? src/sandbox.py
```

- `data/snapshot/` — the retrieved snapshot. **Never hand-edit it.** Last written 28 Aug.
- `data/derived/` — inundation rasters, gitignored, **recomputed on every run**. Not an
  input. Reach it through `HazardSurface`, never by path.
- `data/snapshot-backup/` — gitignored safety copy. Not an input.

## Schedule, already decided, do not re-litigate

S10 finished late. S11 and S12 now sit on Mon 31 with S13 and S14, paper Tue 1 – Wed 2,
submit Wed 3, deadline Fri 4. S11 is 2.5 h. If it runs past 3 h, take the cut line below
rather than eating S12 — and read the cut line now, not at the three-hour mark, because
it changes what you build rather than what you finish.

## Read first, in this order

`CLAUDE.md`, then `src/contracts.py` (the `Critic` protocol, `CriticReport`,
`CriticFinding`), then `src/tools.py` — specifically `validate_answer`, `logged_calls()`,
`CALLS`, `forget_calls()` and `MAX_CALLS` — then `src/agent.py` in full, because this is
the session that rewires it. Then `src/pipeline.py`'s `_monotonic_checks`, which is where
the domain rules already live. Then the **last three entries** of `docs/failures.md`;
those are S10's, and the last of them is the one that matters most to this session. Then
`docs/RUNBOOK.md`, the section headed "Where you actually are", which was written for you.

`contracts.py` is frozen and has not been touched since S1. Keep it that way.

## What S11 is

Two halves. Build both. They are not the same thing and each one alone reads as done.

**Half one — `src/critic.py`**, against the frozen `Critic` protocol:

```python
def check(self, answer: str, steps: list[dict[str, Any]], cycle: int) -> CriticReport: ...
def invariants(self, frame: Any) -> list[CriticFinding]: ...
```

**`src/tools.py` already calls it, and is committed with green checks. Do not change
that call.** This is the exact shape it expects:

```python
report = module.Critic().check(answer, logged_calls(), 1)
# then reads report.cycle, report.passed, report.numbers_checked,
# report.numbers_traceable, and report.findings[i].kind / .detail / .evidence
```

So: a class named `Critic`, in `src/critic.py`, **constructible with no arguments**. The
moment that file exists and imports cleanly, `tools.pending_tools()` returns empty, the
model stops being told `validate_answer` is unavailable, and it works. **You should not
need to edit `tools.py` or `schemas.py` for this half.**

**Half two — the revision cycle in `src/agent.py`.** When the model produces a final
answer, the critic runs on it. If there are findings, they go back as a turn and the model
revises. Bounded at **two revision cycles**, every firing logged as its own step type so
`trace.py` and the paper can count them. Raise `MAX_ITERATIONS` to 15 in `config.py`.

`validate_answer` is a tool the model *may* call. The revision cycle is something the loop
*always* does. Building only the tool means it may never fire; building only the cycle
means the advertised tool still returns a refusal. Criterion IR names both.

## Step 0, before writing any code

Run the eight self-checks and confirm **624 PASS, exit 0** on each. About twelve minutes.

```
python -m src.align --check          # 145
python -m src.hazard --check         #  65
python -m src.vulnerability --check  #  57
python -m src.risk --check           #  68
python -m src.pipeline --check       #  34
python -m src.schemas --check        #  38
python -m src.tools --check          # 118
python -m src.sandbox --check        #  99
```

`sandbox` alone takes about ninety seconds: its timeout fixture really waits out a
deadline and then waits again to prove the child is gone. That is not a hang.

**Do not run the bare `python mutate.py`.** It is now 192 mutations across eight modules
and takes well over an hour. Run `python mutate.py critic` at the end, once you have
written the entries.

Then run `python -m src.demo` and read the transcript. It works today with all eleven
tools, ten of which execute. Note what it does **not** do: nothing checks whether the
numbers in the final answer came from anywhere. That gap is what S11 closes.

## The regression signal S10 wishes it had used earlier

**The moment `src/critic.py` imports cleanly, run `python -m src.tools --check` before
writing a single check of your own.** Its behaviour changes the instant that file exists:
`validate_answer` stops returning a refusal and really runs your critic, `pending_tools()`
returns empty, and the coordinate scan and the 24 kB size cap now apply to whatever your
`CriticReport` serialises to. Getting those 118 green first is far cheaper than untangling
them after forty new checks land. S10 did this and it saved an hour.

While you are there, notice what happens to `tools._pending_checks`. With nothing pending,
`refusals` is an empty dict and three of its five assertions pass over an empty set. They
are not wrong, they are **vacuous**, and a check that cannot fail reads as coverage. Say
so in the gate rather than quoting five green checks that tested nothing.

## Measured, do not re-derive

- **624 checks across eight modules, all PASS. 192 mutations, zero survivors.**
  align 145, hazard 65, vulnerability 57, risk 68, pipeline 34, schemas 38, tools 118,
  sandbox 99.
- 420,264 residents in 99 tracts and 261 block groups. Exposed population
  99,037 / 187,349 / 303,839 at 1.5 / 3.0 / 5.0 m of surge — the block-group rollup; the
  tract-uniform estimate at 3.0 m is 125,533.
- 98 of 99 tracts scored. The unscored one is a water tract with no residents. Every
  ranking tool names it and says why. **Do not write its GEOID into `critic.py`** —
  `verify.study_area_tokens` scans for the county prefix and will fail the module.
- `pipeline.run()` is the only thing that computes anything: 17–41 s. `tools.analysis()`
  caches one `PipelineResult` per process. **Use it. Do not call `pipeline.run()`
  yourself.**
- Real numbers a real answer contained, from S10's transcripts, which are what your
  tolerance has to survive: `0.659` traced from a stdout table printing `0.659430`;
  `48,192` traced from a stdout line printing `48192`; `22` traced from a stdout line
  printing `22`; `0.829855` traced from a structured `risk_scenario` result.
- The demo's three S10 questions finish in 3, 6 and 4 LLM calls of the six allowed.

## The decision you have to make deliberately and write down

**How does a number in the answer get matched to a number in a tool result?**

Three options. The one you pick belongs in the module docstring with its reason:

1. Exact string match against the serialised tool results. Brittle to the point of
   useless: `0.659` does not appear in `0.659430`, and `48,192` does not appear in
   `48192`. Every real answer would fail.
2. Extract every numeral from the answer and every numeral from each logged result —
   walking the JSON values **and the stdout strings `run_spatial_code` returns** —
   normalise both, and match within a tolerance that **rounding alone can explain**,
   stated as the rounding rather than as slack.
3. Ask the model to cite its own sources. Circular: the thing under test is whether the
   model's numbers are real, and this asks the model.

(2) is almost certainly right. Whatever you choose, say why — S9's equivalent decision
(one `PipelineResult` per process) and S10's (a parquet dump the child reads, keyed on the
shared run) are both written down for the same reason.

**The half of (2) that will bite you.** `run_spatial_code` returns up to 6,000 characters
of stdout as an unstructured string. A critic that accepts any numeral appearing anywhere
in that blob will trace almost anything, including a number the model invented that
happens to be a substring of a correlation matrix. That is a guard that cannot fail, which
this project has shipped four times. Decide what a *match inside a text blob* means —
whole-number boundaries, at minimum — and write a fixture where a number is present as a
substring and must NOT be traced.

## The second decision, which the contract forces

**`check(answer, steps, cycle)` will be handed two different shapes.**

- `tools.validate_answer` passes `logged_calls()`, which is
  `[{"tool": ..., "arguments": {...}, "result": {...}}, ...]`.
- `agent.py`'s own log is `logger.steps`, which is
  `[{"run_id", "timestamp", "iteration", "step", "payload"}, ...]` where a tool result
  lives at `payload.result` and only when `step == "tool_result"`.

One signature, two callers, two shapes. If you write the critic against one and wire the
loop with the other, every number becomes untraceable and the critic fires on a correct
answer — which looks exactly like the critic working. Normalise at the top of `check()`,
accept both, and **write a fixture for each shape** asserting the same answer gets the
same report. This is the hour S11 loses if you skip it.

## Contract notes, decided earlier, do not undo

- **`TOOL_NAMES` is frozen at eleven** and `validate_answer` is already one of them. Do
  not add a twelfth. A twelfth idea is a parameter on an existing tool.
- **`CriticFinding.kind` is a `Literal` of exactly three values** —
  `"untraceable_number"`, `"invariant_violation"`, `"unsupported_claim"`. Do not invent a
  fourth. If a finding does not fit one of the three, work out which one it actually is.
- **`CriticReport.passed` is a property on the frozen dataclass** and is
  `not self.findings`. Do not shadow it with a field.
- **Invariant 3 applies to the report.** `validate_answer` returns findings to the model,
  and `CriticFinding.evidence` is free text quoted from somewhere. If it quotes a tool
  result, it can quote a coordinate. `tools.coordinate_faults` and
  `sandbox.output_faults` both exist and both refuse text; use one rather than writing a
  third. S10's own guard leaked through its own refusal message — read that entry.
- **Errors return `{"error": ..., "detail": ...}` rather than raising**, so the model can
  recover. A finding is a *result*, not an exception.
- Type hints on every function. `verify.discipline_checks(sys.modules[__name__])` gives
  six checks for one line.
- Never write a column name as a string literal. Use `contracts.Col`.

## `invariants(frame)` — the half that is easy to fake

The domain rules already exist, in `pipeline._monotonic_checks`: a deeper surge floods at
least as much of every unit, is at least as deep, and exposes at least as many residents;
the scenarios are genuinely different; the county total rises with surge height. Add the
within-unit ones the pipeline does not assert — exposed population never exceeds
population, an index that claims to be a percentile lies in [0, 1], a rank is dense over
the scored units and not over all of them.

**The trap.** On the real county every one of these already holds, so an `invariants()`
that returns `[]` and an `invariants()` that returns `[]` because it checks nothing are
indistinguishable. Every rule needs a **fixture frame that violates it**, built by hand,
asserted to produce exactly one finding of exactly the right kind. This is the S8 lesson
about rules this county cannot break, and it is now four sessions old.

And do not implement `invariants()` by calling `pipeline._monotonic_checks`. That compares
the pipeline against itself — the exact defect that let three S9 mutations survive.

## The trap that will cost you an hour if you do not read this

**A critic that fires on a correct answer is worse than no critic**, because the revision
cycle will then rewrite a right answer into a wrong one and log it as a success. Two
specific ways this happens here:

1. **Identifiers are not claims.** A GEOID is eleven digits. "the five highest-risk
   tracts" contains a numeral. "2019-2023" is a vintage. A naive numeral scan produces a
   dozen untraceable numbers on a perfect answer. Decide what a *claim* is before you
   decide what a *match* is.
2. **The call log is per-question.** `tools.forget_calls()` clears it and `agent.py` calls
   it at the start of every run. If you run the critic against a log that was cleared, or
   one belonging to a previous question, everything is untraceable. Check the count you
   traced against, and make `numbers_checked` and `numbers_traceable` both reportable so a
   zero-length log is visible rather than silent.

Prove the critic can fail **and** that it stays quiet: feed it one deliberately wrong
draft with a number nothing produced, and one real answer from `outputs/run_*.json` that
should pass clean. Both fixtures, both asserted.

## Read docs/failures.md before you write checks

The last three entries are S10's. Five lessons will bite again:

- **A guard and its fixture written from the same picture of the channel can only confirm
  each other.** S10's coordinate rules were calibrated to a precision no model would ever
  print, and every fixture used that same precision, so the suite proved nothing. Ask what
  your critic's fixtures assume about the answers it will see, then go and read a real
  `outputs/run_*.json`.
- **A check that exempts the thing under test cannot see it fail.** Two S10 checks skipped
  any line containing `withheld`, which was the only line the leak could be on.
- **A capability can be complete, correct, verified and unusable.** S10 shipped a sandbox
  with 95 green checks that the model could not use, because nothing told it the
  interface. Your equivalent: does the model know what `validate_answer` returns and what
  to do with a finding? Run the demo and read what it does with one.
- **A check that compares a function against itself passes whatever that function does.**
- **A mutation that produces identical answers is not a mutation.** Quote no mutation
  score without naming what it cannot reach.

## `ask_user_preferences` — the third thing S11 owes

It currently returns `{"elicited": False, "channel": "none", ...}` — an honest menu, and
honestly not an interaction. Criterion TU names human-in-the-loop interaction and a
policy statement scores nothing.

Make it real **without breaking batch runs**: a tool that blocks on `input()` hangs every
harness that runs the agent without a terminal, including `mutate.py`, which still has no
timeout of its own. Guard on `sys.stdin.isatty()`, fall back to the menu when there is no
terminal, and keep `elicited` telling the truth either way. The recomputation when the
weighting changes is the part that scores — the preset must reach `risk_scenario`, not
just the transcript.

## What S11 is worth, by criterion

`/rubric` at the end of S10 named **RB the weakest** — the transfer run has still never
been executed — with **IR close behind**, because one of the two feedback loops now exists
and one does not. S11 builds the second one.

- **IR** — this is the criterion's own wording: two feedback cycles. After S11 both exist.
  **Instrument all of it**: cycles run, findings per cycle, findings by kind, how many
  answers changed after revision, and how many revisions made the answer *worse*. That
  last number is the one a reviewer will believe you on.
- **TU** — `validate_answer` executing, plus `ask_user_preferences` actually eliciting and
  the elicited weighting actually reaching the score.
- **SG** — a critic that catches an unsupported claim about *who loses* is the trade-off
  reporting the criterion asks for, enforced rather than promised.

## Gate

`/gate critic`. Show **one run where the critic fires and the revision fixes the answer**,
with the real transcript. If it never fires on a real question, feed it a deliberately
wrong draft and prove it catches that — and say plainly which one you are showing.

Also prove, with real output:

- a correct answer passes clean, with `numbers_checked` greater than zero
- a number nothing produced is reported as `untraceable_number` with its evidence
- a violated domain rule is reported as `invariant_violation`, from a fixture frame
- the same answer and the same run produce the same report through both step shapes
- a finding's `evidence` carries no coordinate
- `python -m src.tools` lists no tool as `[PENDING]`, and `tools.surface_faults()` is
  still empty
- the revision cycle is bounded: a critic that never passes stops after two cycles

## Cut line, decided in advance

If the revision cycle is unstable by evening, **run the critic in report-only mode and
publish its findings as a results table.** Detection without automated revision is still a
real contribution and a real number for the paper. Ship `check()` working and the cycle
bounded at zero rather than shipping neither.

## End the session the way the plan says, in this order

1. Run the invariant-reviewer (general-purpose agent, sonnet, with the agent file's text).
   Point it at **invariant 8 and invariant 3** — 8 because it is what this module exists
   to enforce, 3 because `CriticFinding.evidence` is free text going into a model message
   and that is exactly how S9 and S10 both leaked.
2. Add a `"critic"` entry to `mutate.py`'s `TARGETS` and run `python mutate.py critic` to
   zero survivors. A module listed with an empty list prints `0/0 caught`. Prefer
   mutations that fail fast: `mutate.py`'s `run_check` still has no timeout of its own.
3. `/gate critic`. Print the real output, not a description of it.
4. Propose a one-line lowercase commit message and wait for approval. S7 used "alignment
   complete", S8 "hazard vulnerability risk", S9 "tools and schemas", S10 "sandbox and
   repair loop".
5. `/failure` the moment anything breaks, with the real error text. S10 added three
   entries and two of them were found after the suite was green.
6. Update `docs/RUNBOOK.md` "Where you actually are" and README's Status block. S10 wrote
   both in a format S11 can follow.
7. `/rubric` before you stop.

## Three things already costing points, cheap to fix if S11 finishes early

- **The transfer run has never been executed.** "Runs on a second county with no code
  change" is a quarter of criterion RB and is currently a claim, not a measurement.
  `src/experiments/` does not exist. Pointing the pipeline at `config.TRANSFER_AREA` —
  even if it fails — turns it into a result. Needs a live acquire, so budget 45 minutes.
  **This has been the weakest criterion for two sessions running.**
- **`acquire_dataset` has never been triggered by the model.** It is exercised offline
  only, with a stubbed retriever. One demo question that forces a real retrieval, kept as
  a transcript, is the autonomy showcase the trace figure needs.
- **`src/robustness.py`** — 2 of 13 functions annotated, imported by nothing, still not
  ported to `src/experiments/behaviour.py` (S12). A reviewer browsing `src/` finds the
  file and reads it as abandoned.
