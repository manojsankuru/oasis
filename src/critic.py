"""The critic: every number the answer reports, traced back to a logged tool result.

Invariant 8 in one module. `validate_answer` hands it a draft and a call log;
`agent.py` hands it a final answer and the run log, and revises when it finds
something. Both routes come through `check()`.

**How a number in the answer is matched to a number in a tool result.** Three
options were open and the middle one is implemented:

1. Exact string match against the serialised results. Useless here: `0.659` does
   not appear in `0.659430` and `48,192` does not appear in `48192`, so every
   real answer would fail.
2. Extract every numeral from the answer and every numeral from each logged
   result -- walking the JSON values and the stdout strings `run_spatial_code`
   returns -- normalise both, and match within a tolerance that rounding alone
   explains.
3. Ask the model to cite its own sources. Circular: the thing under test is
   whether the model's numbers are real, and this asks the model.

(2), and the tolerance is stated as the rounding rather than as slack. A claim
written to `d` decimal places matches a value within half of `10**-d`, because
that is exactly the set of values that round to it. An integer written with three
or more trailing zeros is read as rounded to that power of ten -- `420,000`
matches `420,264` -- and an integer written without them is read as exact.
Nothing here is a tunable margin; every accepted difference is a rounding a
reader could have performed.

**What a match inside a text blob means.** `run_spatial_code` returns up to six
thousand characters of unstructured stdout. A critic that accepted any numeral
appearing anywhere in that string would trace almost anything, including a number
the model invented that happens to sit inside a correlation matrix. Numbers are
extracted from a blob with digit boundaries on both sides, so `192` does not
trace to a line printing `48192`, and a fixture asserts exactly that.

**What is NOT a claim.** A naive numeral scan produces a dozen untraceable
numbers on a perfect answer, and a critic that fires on a correct answer is worse
than no critic: the revision cycle then rewrites a right answer into a wrong one
and logs it as a success. So identifiers are masked out before any number is
read -- a GEOID is eleven digits, a scenario name carries its surge height, a
markdown list marker is a numeral at the start of a line, a vintage is a year
range, and a backtick span is a name rather than a quantity.

**Two callers, two step shapes.** `tools.validate_answer` passes
`logged_calls()`, which is `[{"tool", "arguments", "result"}, ...]`. `agent.py`
passes `logger.steps`, which is `[{"run_id", "timestamp", "iteration", "step",
"payload"}, ...]` with the result under `payload.result` and only where `step ==
"tool_result"`. `normalise_steps` accepts both at the top of `check()`, and a
fixture runs one real transcript through both shapes and asserts the same report.
Writing the critic against one shape and wiring the loop with the other makes
every number untraceable and fires the critic on a correct answer, which looks
exactly like the critic working.

**A number is never traced to a tool ARGUMENT.** The arguments are what the model
asked for; tracing to them would be option (3) wearing option (2)'s clothes.
Only what a tool returned counts as evidence.

**Invariant 3 applies to this report.** `detail` and `evidence` are free text
going into a model message, and evidence quoted from an answer or a frame can
carry a coordinate. Both fields go through `sandbox.guard_stream`, which is the
rule this project already has rather than a third copy of it, and the check that
asserts they are clean does not exempt the guard's own marker -- that exemption
is how S10's leak survived two checks written to catch it.
"""

from __future__ import annotations

import functools
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

import pandas as pd

from . import config, contracts, sandbox, tools, verify
from .contracts import Col, CriticFinding, CriticReport

# ---------------------------------------------------------------------------
# what a claim is: the masks that run before any number is read
# ---------------------------------------------------------------------------

MASK = "\x00"
"""Placeholder written over a masked span, one character per character.

Same length as what it replaces, so every offset in the answer still points at
the same character and a finding can quote the text around a number."""

URL_SPAN = re.compile(r"https?://\S+")
CODE_SPAN = re.compile(r"`[^`\n]*[A-Za-z][^`\n]*`")
"""A backtick span that contains a letter is a name -- a tool, a layer, a
scenario, a preset. Real answers write `surge_1_5m` and `tracts_joined` that way.

**The letter is the whole rule.** An earlier version masked every backtick span,
on the assumption that a span in backticks is always a name. Nothing checked the
assumption, and a model that writes a fabricated figure as `48200` -- the same
model already backticks numeric-looking tokens throughout its real answers -- had
that number erased before a claim could be extracted from it. Not reported as
untraceable: never counted at all, because `numbers_checked` is taken after the
masks run, so the report would say every number traced while quietly holding a
smaller set than the answer asserts. A hole in invariant 8 that reads as
compliance is the worst shape this module can have."""

YEAR_RANGE = re.compile(r"\b(?:19|20)\d{2}\s*[-‐-―]\s*(?:19|20)\d{2}\b")
"""A vintage. `ACS 2019-2023` is a dataset name, not two quantities."""

LONG_DIGITS = re.compile(r"(?<![\d.])\d{10,}(?!\d)")
"""An identifier. A tract GEOID is eleven digits and a block group is twelve.

**A run of digits after a decimal point is not an identifier, it is precision.**
The lookbehind read `(?<!\\d)` once, so `0.1234567890123` had its fractional part
masked as though it were a GEOID and the claim became `0` -- not the number the
answer asserts, and one that traces to almost any result, since a zero appears in
nearly every one. That is the backtick hole in a second costume: a mask that
swallows a quantity does not report it as untraceable, it replaces it with a
different number and passes."""

WORD_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
"""Scanned for tokens that mix letters and digits, which are names carrying a
number rather than numbers: a scenario name spells its own surge height."""

LIST_MARKER = re.compile(r"(?m)^[ \t]*(?:[-*+][ \t]*)?\d+[.)](?=[ \t])")
"""An ordered-list marker. Every ranked answer this project has produced is a
numbered list, so this is the single largest source of false claims."""

MASKS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("a URL", URL_SPAN),
    ("a name in backticks", CODE_SPAN),
    ("a dataset vintage", YEAR_RANGE),
    ("an identifier of ten digits or more", LONG_DIGITS),
    ("a list marker", LIST_MARKER),
)

NUMBER = re.compile(r"(?<![\d.])-?\d[\d,]*(?:\.\d+)?(?!\d)(?!\.\d)")
"""One number, with digit boundaries on both sides.

`48192` yields one number and never `192`, `8192` or `4819`. That guarantee comes
from `finditer` scanning left to right without overlapping: the pattern consumes a
run of digits greedily, so the scan resumes after it rather than inside it. The
lookarounds are for the case the scan alone cannot settle -- a number that ends
where another begins. `(?!\\.\\d)` rather than `(?![.])` so a number at the end of a
sentence still matches while `1.5.3` matches nothing, because a version is not a
quantity.

**The lookbehind excludes `.` and not `,` on purpose.** It read `(?<![\\d.,])`
once, which looked symmetrical and silently dropped every number after the first
in a comma-separated run: `values 0.5,0.75,0.9` yielded `0.5` alone. On the answer
side that is a hole in invariant 8 -- an invented number never checked. On the
result side it is worse than a hole, because a real number that no longer appears
as a candidate makes a correct answer fail. A comma inside a number is consumed by
`[\\d,]*` before the scan can resume on it, so excluding it bought nothing."""

CONTEXT_CHARS = 48
"""How much of the answer a finding quotes around an untraceable number. Narrow
on purpose: a wider window usually catches a second number, and two decimals side
by side are a coordinate shape that the output guard then withholds entirely."""


# ---------------------------------------------------------------------------
# bounds -- a report is a model message and shares the size cap every tool has
# ---------------------------------------------------------------------------

MAX_FINDINGS = 20
MAX_EVIDENCE = 240
MAX_CALLS = 400
MAX_CANDIDATES = 60_000
MAX_TEXT_SCAN = 20_000
"""Longest string value a candidate scan reads. `run_spatial_code` bounds its own
stdout at six thousand characters; this is the bound for everything else."""


# ---------------------------------------------------------------------------
# tolerance -- stated as the rounding, never as a margin
# ---------------------------------------------------------------------------

SIGNIFICANT_ZEROS = 3
"""Trailing zeros before an integer is read as rounded rather than exact.

`420,000` has four and matches `420,264`; `100` has two and must match `100`
exactly. Set at three because two would let a claim of `100` trace to anything
between `50` and `150`, which is a guard that has stopped being one."""

FLOAT_SLACK = 1e-9
"""Binary floating point only. Never widens a rounding by anything a reader
would notice."""

PERCENT_SCALE = 100.0
"""A claim written with a percent sign also matches the same quantity stored as a
fraction. `23.4%` traces to a result holding `0.234`, because those are the same
measurement in two units and refusing the second would fire on a correct answer."""


# ---------------------------------------------------------------------------
# what a priority ordering owes, which is criterion SG enforced rather than promised
# ---------------------------------------------------------------------------

ORDERING_LANGUAGE = re.compile(
    r"\b(?:prioriti\w*|priority|highest[- ]risk|top\s+(?:\d+|three|five|ten)|rank\w*|"
    r"worst[- ]affected|most\s+at\s+risk)\b",
    re.IGNORECASE,
)
WEIGHTING_LANGUAGE = re.compile(
    r"\b(?:weight\w*|weighting|preset|equal\w*|value\s+judg\w*)\b", re.IGNORECASE
)
TRADEOFF_LANGUAGE = re.compile(
    r"\b(?:loses?|losing|who\s+loses|drop\w*|displac\w*|not\s+prioriti\w*|"
    r"another\s+weighting|a\s+different\s+weighting|trade[- ]?offs?)\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# the two step shapes
# ---------------------------------------------------------------------------

TOOL_RESULT_STEP = "tool_result"

CRITIC_FIXTURE_STEP = "a_step_type_that_carries_a_result"
"""A step that holds a `result` and is not a tool result, used by one fixture.

The step-type filter would otherwise be belt over braces: today no other step in
`agent.py`'s log carries a `result` key, so removing the filter changes nothing
and reads as a check that cannot fail. The day one does -- a cached result, a
replayed step, a step type S12 adds -- its contents would silently become
evidence a number could be traced to. This is the fixture for that day."""


def normalise_steps(steps: Any) -> list[dict[str, Any]]:
    """Both callers' shapes as one list of `{"tool", "arguments", "result"}`.

    An agent step is recognised by carrying a `payload`, which the tool log's own
    entries never do, and only a `tool_result` step holds a result. Anything else
    is dropped rather than guessed at, because a shape nobody planned for reads
    better as zero traceable numbers than as a silently different answer.
    """
    calls: list[dict[str, Any]] = []
    if not isinstance(steps, list):
        return calls
    for step in steps:
        if not isinstance(step, dict):
            continue
        if "payload" in step and "step" in step:
            if step.get("step") != TOOL_RESULT_STEP:
                continue
            payload = step.get("payload")
        else:
            payload = step
        if not isinstance(payload, dict) or "result" not in payload:
            continue
        calls.append(
            {
                "tool": str(payload.get("tool", "")),
                "arguments": payload.get("arguments") or {},
                "result": payload.get("result"),
            }
        )
    return calls[-MAX_CALLS:]


# ---------------------------------------------------------------------------
# claims
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Claim:
    """One number the answer asserts, and where in the answer it sits."""

    text: str
    value: float
    start: int
    end: int
    percent: bool

    def context(self, answer: str) -> str:
        left = max(0, self.start - CONTEXT_CHARS)
        right = min(len(answer), self.end + CONTEXT_CHARS)
        return answer[left:right].replace("\n", " ").strip()


def masked(answer: str) -> str:
    """The answer with every identifier written over, offsets preserved."""
    text = answer
    for _, rule in MASKS:
        text = rule.sub(lambda found: MASK * len(found.group(0)), text)
    for found in WORD_TOKEN.finditer(text):
        token = found.group(0)
        if any(character.isdigit() for character in token):
            text = text[: found.start()] + MASK * len(token) + text[found.end() :]
    return text


def as_value(text: str) -> float | None:
    try:
        return float(text.replace(",", ""))
    except ValueError:
        return None


def claims(answer: str) -> list[Claim]:
    """Every number the answer asserts as a quantity."""
    if not answer:
        return []
    scan = masked(answer)
    found: list[Claim] = []
    for match in NUMBER.finditer(scan):
        value = as_value(match.group(0))
        if value is None:
            continue
        after = scan[match.end() : match.end() + 1]
        found.append(
            Claim(
                text=match.group(0),
                value=value,
                start=match.start(),
                end=match.end(),
                percent=after == "%",
            )
        )
    return found


# ---------------------------------------------------------------------------
# candidates: every number a tool really returned
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Candidate:
    """One number a logged tool result carried, and where it came from."""

    value: float
    tool: str
    path: str
    from_text: bool


def numbers_in_text(text: str) -> list[float]:
    """Every number in a string, with digit boundaries on both sides."""
    values: list[float] = []
    for match in NUMBER.finditer(text[:MAX_TEXT_SCAN]):
        value = as_value(match.group(0))
        if value is not None:
            values.append(value)
    return values


def walk(payload: Any, path: str) -> Iterator[tuple[float, str, bool]]:
    """Every number under this result, from the values and from the strings.

    Keys are not walked: a key is a name. Booleans are skipped before the numeric
    branch because `bool` is a subclass of `int` and `True` is not a measurement.
    """
    if isinstance(payload, bool):
        return
    if isinstance(payload, dict):
        for key, value in payload.items():
            yield from walk(value, f"{path}.{key}")
    elif isinstance(payload, (list, tuple)):
        for index, value in enumerate(payload):
            yield from walk(value, f"{path}[{index}]")
    elif isinstance(payload, (int, float)):
        yield float(payload), path, False
    elif isinstance(payload, str):
        for value in numbers_in_text(payload):
            yield value, path, True


def candidates(calls: list[dict[str, Any]]) -> tuple[list[Candidate], bool]:
    """Every number the logged results carried, and whether the scan hit its bound."""
    found: list[Candidate] = []
    for call in calls:
        tool = str(call.get("tool", ""))
        for value, path, from_text in walk(call.get("result"), "result"):
            if len(found) >= MAX_CANDIDATES:
                return found, True
            found.append(Candidate(value=value, tool=tool, path=path, from_text=from_text))
    return found, False


# ---------------------------------------------------------------------------
# matching
# ---------------------------------------------------------------------------


def rounding_unit(text: str) -> float:
    """The size of the step this number was rounded to, read off how it is written.

    `0.659` is written to three decimal places, so it stands for anything within
    half a thousandth. `48192` is written exactly, so it stands for itself. The
    unit is a property of the text, never a knob.
    """
    digits = text.replace(",", "").lstrip("+-")
    if "." in digits:
        return 10.0 ** -len(digits.split(".", 1)[1])
    stripped = digits.rstrip("0")
    zeros = len(digits) - len(stripped)
    return 10.0**zeros if zeros >= SIGNIFICANT_ZEROS and stripped else 1.0


def rounds_to(claim: Claim, value: float) -> bool:
    """Would a reader rounding `value` the way this claim is written have written it?"""
    unit = rounding_unit(claim.text)
    tolerance = 0.5 * unit + FLOAT_SLACK * max(1.0, abs(claim.value))
    if abs(value - claim.value) <= tolerance:
        return True
    return claim.percent and abs(value * PERCENT_SCALE - claim.value) <= tolerance


def trace(claim: Claim, pool: list[Candidate]) -> Candidate | None:
    """The logged number this claim came from, preferring a structured one.

    A structured value is a field a tool decided to report; a number inside a
    stdout blob is whatever the model's own program happened to print. Both are
    real evidence, and when the same claim matches both the structured one is the
    better thing to name in a finding.
    """
    fallback: Candidate | None = None
    for item in pool:
        if not rounds_to(claim, item.value):
            continue
        if not item.from_text:
            return item
        fallback = fallback or item
    return fallback


# ---------------------------------------------------------------------------
# invariant 3: what a finding may not carry
# ---------------------------------------------------------------------------

GUARD_STREAM = "critic"


def guarded(text: str) -> str:
    """One field of a finding, through the guard every model message goes through.

    `sandbox.guard_stream` rather than a third copy of the rule: this project has
    two enforcement points for invariant 3 already and a third would be the copy
    that drifts. It replaces an offending line with a marker that names the rule
    and never quotes what it withheld.
    """
    return sandbox.guard_stream(text[:MAX_EVIDENCE], GUARD_STREAM).strip()


def finding(kind: str, detail: str, evidence: str = "") -> CriticFinding:
    """A finding with both free-text fields guarded, because both reach the model."""
    return CriticFinding(
        kind=kind,  # type: ignore[arg-type]
        detail=guarded(detail),
        evidence=guarded(evidence) if evidence else "",
    )


# ---------------------------------------------------------------------------
# domain invariants over one frame
# ---------------------------------------------------------------------------

UNIT_SLACK = 1e-9
POPULATION_SLACK = 0.5
"""Apportionment writes fractional residents and a report rounds them, so a unit
whose exposed count sits half a person above its population is arithmetic rather
than a violation. Anything larger is a violation."""

UNIT_INTERVAL_COLUMNS: tuple[str, ...] = (
    Col.VULNERABILITY,
    Col.RESILIENCE,
    Col.RISK_SCORE,
    Col.INUNDATED_FRACTION,
)
"""Every column whose name claims a percentile, an index or a fraction. Each one
says of itself that it lies in [0, 1]; none of them is asserted to by the
pipeline."""

NON_NEGATIVE_COLUMNS: tuple[str, ...] = (Col.POPULATION, Col.EXPOSED_POPULATION)

MAX_OFFENDERS_NAMED = 2


def numeric(frame: pd.DataFrame, column: str) -> pd.Series:
    return pd.to_numeric(frame[column], errors="coerce")


def unit_label(frame: pd.DataFrame, position: int) -> str:
    if Col.GEOID in frame.columns:
        return str(frame[Col.GEOID].iloc[position])
    return f"row {position}"


def offending_units(frame: pd.DataFrame, flags: pd.Series, describe: str) -> list[str]:
    """Which rows tripped a rule.

    The fill is not cosmetic. The real tables carry pandas' nullable dtypes, so a
    comparison against a missing value is `pd.NA` rather than `False`, and reading
    one as a boolean raises rather than answering. A unit with no value for a
    column has not broken the rule -- it has not been measured -- and that is what
    the fill says.
    """
    settled = flags.fillna(False).astype(bool).to_numpy()
    return [
        f"unit {unit_label(frame, at)} {describe}"
        for at, bad in enumerate(settled)
        if bool(bad)
    ]


def exposure_over_population(frame: pd.DataFrame) -> list[str]:
    """Exposed residents never outnumber residents.

    Held only where the population is itself non-negative. A negative population
    is its own rule's business, and letting it trip this one too would report one
    defect as two findings and make the fixture for the other rule impossible to
    write.
    """
    exposed = numeric(frame, Col.EXPOSED_POPULATION)
    population = numeric(frame, Col.POPULATION)
    return offending_units(
        frame,
        (exposed > population + POPULATION_SLACK) & (population >= 0.0),
        "exposes more residents than it holds",
    )


def outside_unit_interval(frame: pd.DataFrame, column: str) -> list[str]:
    values = numeric(frame, column)
    return offending_units(
        frame,
        (values < -UNIT_SLACK) | (values > 1.0 + UNIT_SLACK),
        f"has {column} outside [0, 1]",
    )


def negative_count(frame: pd.DataFrame, column: str) -> list[str]:
    return offending_units(frame, numeric(frame, column) < 0.0, f"has a negative {column}")


def mean_over_max_depth(frame: pd.DataFrame) -> list[str]:
    mean = numeric(frame, Col.INUNDATION_MEAN_M)
    peak = numeric(frame, Col.INUNDATION_MAX_M)
    return offending_units(
        frame, mean > peak + UNIT_SLACK, "floods deeper on average than at its deepest"
    )


def minimum_over_mean_elevation(frame: pd.DataFrame) -> list[str]:
    lowest = numeric(frame, Col.ELEV_MIN_M)
    mean = numeric(frame, Col.ELEV_MEAN_M)
    return offending_units(
        frame, lowest > mean + UNIT_SLACK, "sits lower on average than at its lowest point"
    )


def rank_not_dense_over_scored(frame: pd.DataFrame) -> list[str]:
    """A rank is dense over the units that were scored, not over all of them.

    98 of 99 units score here and one does not, so a rank running to 99 would mean
    the unscored unit had been given a position, and a rank running to 98 with a
    gap in it would mean two units share one. Both are invisible to any check that
    only looks at the top of the table.
    """
    ranks = numeric(frame, Col.PRIORITY_RANK).dropna()
    scored = int(numeric(frame, Col.RISK_SCORE).notna().sum())
    faults: list[str] = []
    if len(ranks) != scored:
        faults.append(f"{len(ranks)} ranks were issued over {scored} scored units")
    expected = set(range(1, scored + 1))
    seen = {int(value) for value in ranks.to_numpy() if float(value).is_integer()}
    if seen != expected:
        missing = sorted(expected - seen)[:MAX_OFFENDERS_NAMED]
        unexpected = sorted(seen - expected)[:MAX_OFFENDERS_NAMED]
        faults.append(
            f"the ranks are not 1..{scored}: missing {missing or 'nothing'}, "
            f"unexpected {unexpected or 'nothing'}"
        )
    return faults


@dataclass(frozen=True, slots=True)
class Rule:
    """One domain rule, the columns it needs, and what a violation looks like."""

    name: str
    columns: tuple[str, ...]
    offenders: Callable[[pd.DataFrame], list[str]]


def _interval_rule(column: str) -> Rule:
    return Rule(
        name=f"{column} claims to be an index or a fraction and must lie in [0, 1]",
        columns=(column,),
        offenders=functools.partial(outside_unit_interval, column=column),
    )


def _non_negative_rule(column: str) -> Rule:
    return Rule(
        name=f"{column} counts residents and cannot be negative",
        columns=(column,),
        offenders=functools.partial(negative_count, column=column),
    )


RULES: tuple[Rule, ...] = (
    Rule(
        name="a unit never exposes more residents than it holds",
        columns=(Col.EXPOSED_POPULATION, Col.POPULATION),
        offenders=exposure_over_population,
    ),
    *(_interval_rule(column) for column in UNIT_INTERVAL_COLUMNS),
    *(_non_negative_rule(column) for column in NON_NEGATIVE_COLUMNS),
    Rule(
        name="a unit never floods deeper on average than at its deepest point",
        columns=(Col.INUNDATION_MEAN_M, Col.INUNDATION_MAX_M),
        offenders=mean_over_max_depth,
    ),
    Rule(
        name="a unit is never lower on average than at its lowest point",
        columns=(Col.ELEV_MIN_M, Col.ELEV_MEAN_M),
        offenders=minimum_over_mean_elevation,
    ),
    Rule(
        name="the priority rank is dense over the scored units and not over all of them",
        columns=(Col.PRIORITY_RANK, Col.RISK_SCORE),
        offenders=rank_not_dense_over_scored,
    ),
)
"""The within-unit rules, which are the ones `pipeline._monotonic_checks` does not
assert.

The cross-scenario rules are deliberately absent and this is the reason. They
compare one scenario's table against another's, so a single frame cannot reach
them -- and implementing them here by calling `pipeline._monotonic_checks` would
compare the pipeline against itself, which is exactly the defect that let three
S9 mutations survive. The pipeline owns the rules that span its own runs. This
module owns the rules that hold inside one row, and every one of them has a
fixture frame built by hand that breaks it, because on the real county all of
them already hold and a rule that checks nothing is indistinguishable from a rule
that passes."""


def real_frames(state: Any) -> dict[str, pd.DataFrame]:
    """Every frame of the shared analysis the rules can be held to.

    Both the joined units and each scenario's risk table, because the rules do not
    all live on one frame: the elevation pair is on the joined layer and never
    reaches a risk table, while the rank and the score exist only on the risk
    table. Holding one frame to every rule would have meant either dropping a real
    rule or asserting a column into existence.
    """
    frames: dict[str, pd.DataFrame] = {tools.TRACT_KEY: state.units}
    for name, table in state.result.tables.items():
        frames[name] = table.frame
    return frames


def applicable(frame: Any) -> tuple[list[str], list[str]]:
    """Which rules this frame can be held to, and which it cannot.

    Returned so that a skipped rule is reportable. A rule silently skipped for a
    missing column is a check that cannot fail, which is the shape this project
    has shipped four times.
    """
    if not isinstance(frame, pd.DataFrame):
        return [], [rule.name for rule in RULES]
    present = set(frame.columns)
    ran = [rule.name for rule in RULES if present.issuperset(rule.columns)]
    skipped = [rule.name for rule in RULES if not present.issuperset(rule.columns)]
    return ran, skipped


# ---------------------------------------------------------------------------
# the critic
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Critic:
    """Implements the frozen `contracts.Critic` protocol. No arguments to build.

    `tools.validate_answer` constructs one per call, so anything expensive would
    be paid per call. Nothing here is expensive: the shared analysis is reached
    through `tools.analysis()` only when one is already built, and it is never
    built here, because a critic that triggered a forty-second pipeline run inside
    a model turn would be a second route to the answer it is checking.
    """

    traced: list[tuple[Claim, Candidate]] = field(default_factory=list)
    untraced: list[Claim] = field(default_factory=list)

    def check(
        self, answer: str, steps: list[dict[str, Any]], cycle: int
    ) -> CriticReport:
        calls = normalise_steps(steps)
        pool, truncated = candidates(calls)
        found = claims(answer or "")
        self.traced = []
        self.untraced = []

        findings: list[CriticFinding] = []
        if found and not calls:
            findings.append(
                finding(
                    "untraceable_number",
                    f"the answer reports {len(found)} number(s) and no tool result was "
                    "logged for this question at all, so none of them can be traced. "
                    "The call log is cleared at the start of every run.",
                )
            )
            self.untraced = list(found)
        else:
            for claim in found:
                source = trace(claim, pool)
                if source is None:
                    self.untraced.append(claim)
                else:
                    self.traced.append((claim, source))
            findings.extend(self._number_findings(answer or "", calls, truncated))

        findings.extend(self._claim_findings(answer or "", calls))
        findings.extend(self._frame_findings())

        return CriticReport(
            cycle=cycle,
            findings=self._bounded(findings),
            numbers_checked=len(found),
            numbers_traceable=len(self.traced),
        )

    def _number_findings(
        self, answer: str, calls: list[dict[str, Any]], truncated: bool
    ) -> list[CriticFinding]:
        note = (
            f" The scan read {len(calls)} logged tool result(s)"
            + (", and hit its candidate bound." if truncated else ".")
        )
        return [
            finding(
                "untraceable_number",
                f"the answer reports {claim.text} and no logged tool result produced "
                f"a number that rounds to it." + note,
                claim.context(answer),
            )
            for claim in self.untraced
        ]

    def _claim_findings(
        self, answer: str, calls: list[dict[str, Any]]
    ) -> list[CriticFinding]:
        """What a priority ordering owes beyond its numbers.

        Criterion SG asks that trade-offs be reported, including who loses. An
        answer that orders communities is making a value judgement, and one that
        neither names the weighting it used nor names the units another weighting
        would have prioritised has hidden the trade-off it was asked about. Gated
        on the answer actually ordering something, so an answer to a counting
        question is never asked for a weighting it had no reason to choose.
        """
        if not ORDERING_LANGUAGE.search(answer):
            return []
        findings: list[CriticFinding] = []
        if not WEIGHTING_LANGUAGE.search(answer):
            findings.append(
                finding(
                    "unsupported_claim",
                    "the answer orders communities by priority and names no weighting. "
                    "Which units come first is a value judgement, not a fact; say which "
                    "weighting decided it and where that weighting came from.",
                )
            )
        if not TRADEOFF_LANGUAGE.search(answer):
            findings.append(
                finding(
                    "unsupported_claim",
                    "the answer orders communities by priority and does not say who "
                    "loses. Call compare_scenarios and name the units another weighting "
                    "would have prioritised and this one does not.",
                )
            )
        return findings

    def _frame_findings(self) -> list[CriticFinding]:
        """The domain rules, run over the shared analysis when one already exists.

        Never built here. `tools.analysis_built()` answers whether a run is held
        without paying to build one, so a critic called before any tool has
        computed anything reports on the answer and says nothing about a frame it
        has not seen.
        """
        if not tools.analysis_built():
            return []
        seen: set[str] = set()
        findings: list[CriticFinding] = []
        for frame in real_frames(tools.analysis()).values():
            for item in self.invariants(frame):
                if item.detail in seen:
                    continue
                seen.add(item.detail)
                findings.append(item)
        return findings

    def invariants(self, frame: Any) -> list[CriticFinding]:
        """Every within-unit domain rule this frame can be held to.

        One finding per violated rule, naming how many units broke it and up to
        two of them, so a rule broken by every unit costs one finding rather than
        ninety-nine.
        """
        if not isinstance(frame, pd.DataFrame) or frame.empty:
            return []
        present = set(frame.columns)
        findings: list[CriticFinding] = []
        for rule in RULES:
            if not present.issuperset(rule.columns):
                continue
            offenders = rule.offenders(frame)
            if not offenders:
                continue
            findings.append(
                finding(
                    "invariant_violation",
                    f"{rule.name} -- broken by {len(offenders)} unit(s)",
                    "; ".join(offenders[:MAX_OFFENDERS_NAMED]),
                )
            )
        return findings

    def _bounded(self, findings: list[CriticFinding]) -> list[CriticFinding]:
        if len(findings) <= MAX_FINDINGS:
            return findings
        kept = findings[: MAX_FINDINGS - 1]
        kept.append(
            finding(
                "untraceable_number",
                f"{len(findings) - len(kept)} further finding(s) are not listed: a "
                "report is a model message and shares the size bound every tool result "
                "is held to.",
            )
        )
        return kept


# ---------------------------------------------------------------------------
# what the model is told to do with a finding
# ---------------------------------------------------------------------------


def revision_request(report: CriticReport) -> str:
    """The turn the loop sends back when the critic finds something.

    Written as instructions rather than as a report, because S10's lesson is that
    a capability the model cannot use is not a capability: the sandbox was correct
    for ninety-five checks and unusable because nothing told the model its
    interface. A finding the model cannot act on is the same defect one module
    over.
    """
    lines = [
        f"Your draft answer did not pass the critic (revision cycle {report.cycle}).",
        f"{report.numbers_traceable} of {report.numbers_checked} number(s) in it "
        "traced back to a logged tool result.",
        "",
        "Findings:",
    ]
    for position, item in enumerate(report.findings, start=1):
        lines.append(f"{position}. [{item.kind}] {item.detail}")
        if item.evidence:
            lines.append(f"   in your answer: {item.evidence}")
    lines += [
        "",
        "Fix every finding and reply with the corrected answer. For a number you "
        "cannot support, call the tool that produces it rather than restating it, or "
        "drop the claim. Do not change a number that was already traced.",
    ]
    return "\n".join(lines)


def as_result(report: CriticReport) -> dict[str, Any]:
    """The report as `validate_answer` sends it, for anything that wants to log one."""
    return {
        "cycle": report.cycle,
        "passed": report.passed,
        "numbers_checked": report.numbers_checked,
        "numbers_traceable": report.numbers_traceable,
        "findings": [
            {"kind": item.kind, "detail": item.detail, "evidence": item.evidence}
            for item in report.findings
        ],
    }


# ---------------------------------------------------------------------------
# fixture frames -- every rule needs one that breaks it
# ---------------------------------------------------------------------------

FIXTURE_GEOIDS: tuple[str, ...] = ("99001000100", "99001000200", "99001000300")
"""Synthetic identifiers. A real GEOID carries the study area's county prefix and
`verify.study_area_tokens` scans this module's source for it, so a fixture built
from a real answer would fail the module rather than test it."""


def clean_frame() -> pd.DataFrame:
    """A small frame that breaks no rule. Every violating fixture is this, edited."""
    return pd.DataFrame(
        {
            Col.GEOID: list(FIXTURE_GEOIDS),
            Col.POPULATION: [1000.0, 2000.0, 3000.0],
            Col.EXPOSED_POPULATION: [100.0, 400.0, 900.0],
            Col.VULNERABILITY: [0.10, 0.50, 0.90],
            Col.RESILIENCE: [0.20, 0.40, 0.60],
            Col.RISK_SCORE: [0.30, 0.60, 0.90],
            Col.INUNDATED_FRACTION: [0.10, 0.20, 0.30],
            Col.INUNDATION_MEAN_M: [0.50, 1.00, 1.50],
            Col.INUNDATION_MAX_M: [1.00, 2.00, 3.00],
            Col.ELEV_MIN_M: [1.00, 2.00, 3.00],
            Col.ELEV_MEAN_M: [4.00, 5.00, 6.00],
            Col.PRIORITY_RANK: [3.0, 2.0, 1.0],
        }
    )


def broken(column: str, position: int, value: Any) -> pd.DataFrame:
    frame = clean_frame()
    frame.loc[position, column] = value
    return frame


VIOLATIONS: tuple[tuple[str, pd.DataFrame, str], ...] = (
    (
        "a unit exposing more residents than it holds",
        broken(Col.EXPOSED_POPULATION, 0, 1500.0),
        "a unit never exposes more residents than it holds",
    ),
    (
        "a percentile index above one",
        broken(Col.VULNERABILITY, 1, 1.4),
        f"{Col.VULNERABILITY} claims to be an index or a fraction and must lie in [0, 1]",
    ),
    (
        "a resilience index below zero",
        broken(Col.RESILIENCE, 2, -0.2),
        f"{Col.RESILIENCE} claims to be an index or a fraction and must lie in [0, 1]",
    ),
    (
        "a risk score above one",
        broken(Col.RISK_SCORE, 0, 1.9),
        f"{Col.RISK_SCORE} claims to be an index or a fraction and must lie in [0, 1]",
    ),
    (
        "a flooded fraction above one",
        broken(Col.INUNDATED_FRACTION, 1, 1.2),
        f"{Col.INUNDATED_FRACTION} claims to be an index or a fraction and must lie in [0, 1]",
    ),
    (
        "a negative population",
        broken(Col.POPULATION, 2, -50.0),
        f"{Col.POPULATION} counts residents and cannot be negative",
    ),
    (
        "a negative exposed population",
        broken(Col.EXPOSED_POPULATION, 1, -5.0),
        f"{Col.EXPOSED_POPULATION} counts residents and cannot be negative",
    ),
    (
        "a mean depth deeper than the maximum",
        broken(Col.INUNDATION_MEAN_M, 0, 2.5),
        "a unit never floods deeper on average than at its deepest point",
    ),
    (
        "a minimum elevation above the mean",
        broken(Col.ELEV_MIN_M, 2, 9.0),
        "a unit is never lower on average than at its lowest point",
    ),
    (
        "a rank issued to a unit that was not scored",
        broken(Col.RISK_SCORE, 2, None),
        "the priority rank is dense over the scored units and not over all of them",
    ),
    (
        "two units sharing one rank, leaving a gap",
        broken(Col.PRIORITY_RANK, 0, 2.0),
        "the priority rank is dense over the scored units and not over all of them",
    ),
)
"""One violating frame per rule, built by hand and asserted to produce exactly one
finding of exactly the right kind. On the real county every rule already holds, so
without these an `invariants()` that returns nothing and an `invariants()` that
checks nothing are the same observation."""


# ---------------------------------------------------------------------------
# fixture answers and logs
# ---------------------------------------------------------------------------

SUBSTRING_STDOUT = "count 48192\nmean 0.659430\nrows 22\n"
"""A stdout blob for the fixture that matters most: `192` is present inside
`48192` as a substring and must NOT trace, while `0.659` must."""


def stdout_call(text: str) -> dict[str, Any]:
    return {
        "tool": "run_spatial_code",
        "arguments": {"code": "print(1)"},
        "result": {"exit_code": 0, "stdout": text, "stderr": "", "error_type": None},
    }


def structured_call(payload: dict[str, Any]) -> dict[str, Any]:
    return {"tool": "risk_scenario", "arguments": {}, "result": payload}


def agent_shape(calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The same calls as `agent.py` logs them, with the noise around them.

    The two non-tool steps are the point: the agent's log is not a call log with
    extra keys, it is a log of everything, and a normaliser that took every step's
    payload would trace numbers to the question and to the model's own prose.
    """
    steps: list[dict[str, Any]] = [
        {
            "run_id": "fixture",
            "timestamp": "",
            "iteration": None,
            "step": "run_start",
            "payload": {"question": "how many units flood at 3 metres?", "model": "x"},
        }
    ]
    for position, call in enumerate(calls, start=1):
        steps.append(
            {
                "run_id": "fixture",
                "timestamp": "",
                "iteration": position,
                "step": TOOL_RESULT_STEP,
                "payload": {**call, "elapsed_seconds": 0.1},
            }
        )
    steps.append(
        {
            "run_id": "fixture",
            "timestamp": "",
            "iteration": len(calls) + 1,
            "step": "final_answer",
            "payload": {"content": "77777 is not a tool result"},
        }
    )
    steps.append(
        {
            "run_id": "fixture",
            "timestamp": "",
            "iteration": len(calls) + 1,
            "step": CRITIC_FIXTURE_STEP,
            "payload": {"cycle": 1, "result": {"invented": 424242}},
        }
    )
    return steps


def transcripts() -> list[Path]:
    """Every run transcript on disk, newest first."""
    if not config.OUTPUTS_DIR.exists():
        return []
    return sorted(config.OUTPUTS_DIR.glob("run_*.json"), reverse=True)


def real_run() -> tuple[str, list[dict[str, Any]], Path] | None:
    """The newest transcript that carries both an answer and a tool result.

    Read from disk rather than pasted in, for two reasons. A real answer names
    real units, and a real GEOID carries the county prefix that
    `verify.study_area_tokens` refuses in this module's source. And a fixture
    written from the same picture of the channel as the rule it tests can only
    confirm the rule -- which is the S10 entry this session was told to read.
    """
    for path in transcripts():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        answer = payload.get("final_answer")
        steps = payload.get("steps")
        if not isinstance(answer, str) or not answer.strip():
            continue
        if not normalise_steps(steps):
            continue
        return answer, steps, path
    return None


def swap_first_number(answer: str) -> tuple[str, str]:
    """A real answer with one number replaced by one nothing produced.

    The deliberately wrong draft, built from a real answer rather than written
    from scratch, so the number that must be caught sits in prose the model
    really wrote.
    """
    invented = "77777.7"
    for claim in claims(answer):
        return answer[: claim.start] + invented + answer[claim.end :], invented
    return answer + f" The total is {invented}.", invented


# ---------------------------------------------------------------------------
# the loop, with only the model stubbed
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ScriptedMessage:
    content: str
    tool_calls: None = None


@dataclass(slots=True)
class ScriptedChoice:
    message: ScriptedMessage
    finish_reason: str = "stop"


@dataclass(slots=True)
class ScriptedResponse:
    choices: list[ScriptedChoice]


def scripted_run(drafts: list[str], max_revisions: int) -> dict[str, Any]:
    """One real `run_agent`, with the model replaced by a fixed list of drafts.

    Everything the revision cycle owns runs for real: the critic, the bound, the
    step types, the counters and the log. Only the model is stubbed, which is the
    one stub this project's verification rules allow -- loop mechanics are cheap
    to test offline and a stubbed critic would be testing nothing.

    The log and transcript go to a directory this function owns, so a fixture run
    never lands in `outputs/` where `real_run` would later read it back as an
    answer the system produced.
    """
    from tempfile import TemporaryDirectory

    from . import agent, llm_client

    replies = list(drafts)

    def scripted_client() -> Any:
        return object()

    def scripted_chat(
        client: Any,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
    ) -> Any:
        text = replies.pop(0) if len(replies) > 1 else replies[0]
        return ScriptedResponse([ScriptedChoice(ScriptedMessage(text))])

    real_client, real_chat = llm_client.make_client, llm_client.chat
    real_logs, real_outputs = config.LOGS_DIR, config.OUTPUTS_DIR
    llm_client.make_client, llm_client.chat = scripted_client, scripted_chat
    try:
        with TemporaryDirectory(prefix="geoagent-critic-") as where:
            config.LOGS_DIR = Path(where)
            config.OUTPUTS_DIR = Path(where)
            return agent.run_agent(
                "how many residents are exposed?",
                verbose=False,
                max_revisions=max_revisions,
            )
    finally:
        llm_client.make_client, llm_client.chat = real_client, real_chat
        config.LOGS_DIR, config.OUTPUTS_DIR = real_logs, real_outputs


# ---------------------------------------------------------------------------
# self-checks
# ---------------------------------------------------------------------------


def _one(kind: str, findings: list[CriticFinding]) -> bool:
    return len(findings) == 1 and findings[0].kind == kind


def _claim_checks() -> list[tuple[str, bool]]:
    """What the scan reads as a quantity, and what it refuses to."""
    ranked = (
        "Under the s_1_5m scenario the five highest-scoring units are:\n"
        "1. Unit 99001000100: 0.829855\n"
        "2. Unit 99001000200: 0.806612\n"
        "Of the 477 mapped facilities, 22 lie inside them. Data: ACS 2019-2023.\n"
        "See https://example.org/a/1234 for the source."
    )
    values = [item.value for item in claims(ranked)]
    print(f"  a ranked answer with markers, GEOIDs, a vintage and a URL yields {values}")

    # The scenario name above is deliberately NOT in backticks. It was once, and
    # `CODE_SPAN` erased the whole span before the digit-bearing-token mask could
    # see it -- so the assertion below was satisfied by a rule it was not written
    # for, and deleting the mask it does test changed nothing. One fixture cannot
    # prove two rules; each of these two now has text only it can reach.
    named = [item.value for item in claims("scored under `svi_equal` and s_1_5m, 477 units")]
    print(f"  a bare name and a backticked one together yield {named}")

    percent = claims("poverty is 23.4% of households")
    listed = [item.value for item in claims("the fractions are 0.5,0.75,0.9 in order")]
    quoted = [item.value for item in claims("the total is `48200` residents")]
    version = [item.value for item in claims("shapely 1.5.3 raised it")]
    precise = [item.text for item in claims("the ratio is 0.1234567890123 exactly")]
    print(f"  a number with a thirteen-digit fraction yields {precise}")
    print(f"  a percent claim: {[(c.text, c.percent) for c in percent]}")
    print(f"  a comma-separated run yields {listed}; a backticked number yields "
          f"{quoted}; a version string yields {version}")

    return [
        ("a markdown list marker is not a claim", 1.0 not in values and 2.0 not in values),
        ("an eleven-digit identifier is not a claim",
         not any(value > 1e9 for value in values)),
        ("a name carrying a number is not a claim", 1.5 not in values and 5.0 not in values),
        ("a bare name carrying a number is masked by the token rule, not by backticks",
         named == [477.0]),
        ("a dataset vintage is not a claim", 2019.0 not in values and 2023.0 not in values),
        ("a URL is not a claim", 1234.0 not in values),
        ("the real quantities in it are claims",
         {0.829855, 0.806612, 477.0, 22.0}.issubset(set(values))),
        ("a percent sign is read off the claim", len(percent) == 1 and percent[0].percent),
        # Every number after the first in a comma-separated run was once dropped,
        # which is a number the answer asserts and the critic never checks.
        ("every number in a comma-separated run is a claim", listed == [0.5, 0.75, 0.9]),
        # A number in backticks is still a number. A NAME in backticks is not.
        ("a number in backticks is still checked", quoted == [48200.0]),
        ("a version string is not two quantities", version == []),
        # Not "is a claim" but "is THIS claim". The failure this guards against
        # replaced the number with its own integer part, which is a different
        # number that traces to nearly anything.
        ("a long fraction is precision, not an identifier, and survives whole",
         precise == ["0.1234567890123"]),
        ("an answer with no numbers yields no claims", claims("no numbers here") == []),
        ("an empty answer yields no claims", claims("") == []),
    ]


def _boundary_checks() -> list[tuple[str, bool]]:
    """The guard that stops a text blob tracing anything: whole-number boundaries."""
    pool, _ = candidates([stdout_call(SUBSTRING_STDOUT)])
    values = sorted({item.value for item in pool if item.from_text})
    inside = claims("192")[0]
    rounded = claims("0.659")[0]
    whole = claims("48,192")[0]
    partial = claims("8192")[0]
    print(f"  the blob {SUBSTRING_STDOUT!r} yields {values} from its text, "
          f"and {sorted({item.value for item in pool if not item.from_text})} "
          "from the fields around it")

    asked_for = {
        "tool": "hazard_exposure",
        "arguments": {"top_n": 61616, "scenario": ""},
        "result": {"scenario": "", "measured": True, "units": 99},
    }
    # Through `normalise_steps`, not straight into `candidates`. The normaliser is
    # what decides that a call's arguments are not evidence, so a fixture that
    # skipped it was asserting about a function that never runs on this path --
    # and a mutation putting the arguments back survived the whole suite.
    argument_pool, _ = candidates(normalise_steps([asked_for]))
    print(f"  a call whose ARGUMENTS hold 61616 yields "
          f"{sorted({item.value for item in argument_pool})}")

    return [
        ("a blob yields only its whole numbers", values == [0.65943, 22.0, 48192.0]),
        ("a number present only as a substring does not trace",
         trace(inside, pool) is None),
        ("a longer substring does not trace either", trace(partial, pool) is None),
        ("the whole number does trace", trace(whole, pool) is not None),
        ("a rounded number traces to the value it rounds from",
         trace(rounded, pool) is not None),
        ("a number nothing printed does not trace",
         trace(claims("31337")[0], pool) is None),
        # Tracing a number to what the model ASKED for rather than to what came
        # back is option (3) in the module docstring wearing option (2)'s clothes:
        # the answer would be checked against the model's own input.
        ("a number the model passed as an argument is not evidence for it",
         trace(claims("61616")[0], argument_pool) is None),
        ("the result of that same call still traces",
         trace(claims("99")[0], argument_pool) is not None),
        ("a boolean in a result is not a number",
         1.0 not in {item.value for item in argument_pool}),
    ]


def _tolerance_checks() -> list[tuple[str, bool]]:
    """That every accepted difference is a rounding rather than a margin."""
    print(f"  rounding unit of 0.659 is {rounding_unit('0.659')}, "
          f"of 48192 is {rounding_unit('48192')}, of 420,000 is {rounding_unit('420,000')}, "
          f"of 100 is {rounding_unit('100')}")
    pool = [Candidate(value=0.65943, tool="t", path="p", from_text=False)]
    percent_pool = [Candidate(value=0.234, tool="t", path="p", from_text=False)]

    return [
        ("a claim to three places stands for half a thousandth",
         rounding_unit("0.659") == 0.001),
        ("an exact integer stands for itself", rounding_unit("48192") == 1.0),
        ("an integer with three trailing zeros is read as rounded",
         rounding_unit("420,000") == 10_000.0),
        ("an integer with two trailing zeros is not", rounding_unit("100") == 1.0),
        ("0.659 traces to 0.659430", trace(claims("0.659")[0], pool) is not None),
        ("0.660 does not trace to 0.659430", trace(claims("0.660")[0], pool) is None),
        ("420,000 traces to 420,264",
         rounds_to(claims("420,000")[0], 420_264.0)),
        ("420,000 does not trace to 480,000",
         not rounds_to(claims("420,000")[0], 480_000.0)),
        ("a percent claim traces to the same quantity stored as a fraction",
         trace(claims("23.4%")[0], percent_pool) is not None),
        ("a percent claim does not trace to an unrelated fraction",
         trace(claims("23.4%")[0],
               [Candidate(value=0.9, tool="t", path="p", from_text=False)]) is None),
    ]


def _shape_checks() -> list[tuple[str, bool]]:
    """One answer, one run, two step shapes, one report."""
    calls = [
        stdout_call(SUBSTRING_STDOUT),
        structured_call({"ranking": [{"score": 0.829855}], "facilities": 477}),
    ]
    answer = "The score is 0.829855 across 477 facilities and 22 units."
    tool_shape = Critic().check(answer, calls, 1)
    log_shape = Critic().check(answer, agent_shape(calls), 1)
    print(f"  tool-log shape: {as_result(tool_shape)}")
    print(f"  agent-log shape: {as_result(log_shape)}")

    real = real_run()
    if real is None:
        print("  NO TRANSCRIPT ON DISK -- the real-run shape check cannot run")
        real_same = False
        real_where = "none"
    else:
        real_answer, real_steps, path = real
        real_where = path.name
        from_log = Critic().check(real_answer, real_steps, 1)
        from_calls = Critic().check(real_answer, normalise_steps(real_steps), 1)
        real_same = as_result(from_log) == as_result(from_calls)
        print(f"  a real transcript ({real_where}) through both shapes: "
              f"{from_log.numbers_traceable}/{from_log.numbers_checked} traced, "
              f"identical reports: {real_same}")

    return [
        ("the two shapes give the same report",
         as_result(tool_shape) == as_result(log_shape)),
        ("the agent shape really was the longer one",
         len(agent_shape(calls)) > len(calls)),
        ("a non-tool step contributes no candidate",
         77777.0 not in {item.value for item in candidates(
             normalise_steps(agent_shape(calls)))[0]}),
        ("a step that carries a result and is not a tool result contributes none either",
         424242.0 not in {item.value for item in candidates(
             normalise_steps(agent_shape(calls)))[0]}),
        ("the normaliser reduces both shapes to the same calls",
         normalise_steps(agent_shape(calls)) == normalise_steps(calls)),
        ("a real transcript gives the same report through both shapes", real_same),
        (f"the real-run fixture came from a transcript on disk ({real_where})",
         real is not None),
        ("a shape nobody planned for yields nothing rather than guessing",
         normalise_steps([{"nonsense": 1}, "not a dict", None]) == []),
        ("a non-list is not a log", normalise_steps("steps") == []),
    ]


def _answer_checks() -> list[tuple[str, bool]]:
    """That the critic can fail, and that it stays quiet on a correct answer."""
    real = real_run()
    if real is None:
        print("  NO TRANSCRIPT ON DISK -- the real-answer checks cannot run")
        return [("a real answer was available to check against", False)]

    answer, steps, path = real
    clean = Critic().check(answer, steps, 1)
    wrong, invented = swap_first_number(answer)
    caught = Critic().check(wrong, steps, 1)
    kinds = {item.kind for item in caught.findings}
    print(f"  real answer from {path.name}: "
          f"{clean.numbers_traceable}/{clean.numbers_checked} traced, "
          f"{len(clean.findings)} finding(s)")
    print(f"  the same answer with one number replaced by {invented}: "
          f"{caught.numbers_traceable}/{caught.numbers_checked} traced, "
          f"{len(caught.findings)} finding(s)")
    for item in caught.findings:
        print(f"    [{item.kind}] {item.detail}")

    empty = Critic().check(answer, [], 1)
    print(f"  the same answer against an empty log: {as_result(empty)}")

    numbers = [item for item in caught.findings if item.kind == "untraceable_number"]
    return [
        ("a real answer traces at least one number", clean.numbers_checked > 0),
        ("a real answer traces every number it reports",
         clean.numbers_traceable == clean.numbers_checked),
        ("a real answer raises no untraceable_number finding",
         not any(item.kind == "untraceable_number" for item in clean.findings)),
        ("a number nothing produced is caught", "untraceable_number" in kinds),
        ("exactly one number went missing, so exactly one is reported",
         len(numbers) == 1),
        ("the finding names the number rather than the tool that did not produce it",
         invented in numbers[0].detail),
        ("the finding carries evidence from the answer", bool(numbers[0].evidence)),
        ("a cleared log is visible rather than silent",
         empty.numbers_checked > 0 and empty.numbers_traceable == 0
         and not empty.passed),
        ("an empty log costs one finding rather than one per number",
         len([item for item in empty.findings
              if item.kind == "untraceable_number"]) == 1),
        ("the cycle the caller passed is the cycle reported",
         Critic().check(answer, steps, 3).cycle == 3),
        ("passed is the property on the frozen dataclass, not a field",
         isinstance(type(clean).passed, property)),
    ]


def _invariant_checks() -> list[tuple[str, bool]]:
    """Every rule, against a frame built by hand to break it."""
    critic = Critic()
    clean = critic.invariants(clean_frame())
    ran, skipped = applicable(clean_frame())
    print(f"  the clean fixture frame raises {len(clean)} finding(s)")
    print(f"  {len(ran)} rule(s) apply to it, {len(skipped)} skipped: {skipped or 'none'}")

    results: list[tuple[str, bool]] = [
        ("a frame that breaks no rule raises nothing", clean == []),
        ("every rule applies to the clean fixture frame", not skipped),
        ("there is a violating fixture for every rule",
         len({name for _, _, name in VIOLATIONS}) == len(RULES)),
        ("a frame with no columns skips every rule rather than passing them",
         applicable(pd.DataFrame({"x": [1]}))[1] == [rule.name for rule in RULES]),
        ("a non-frame raises nothing", critic.invariants(None) == []),
        ("an empty frame raises nothing", critic.invariants(pd.DataFrame()) == []),
    ]
    for label, frame, rule_name in VIOLATIONS:
        found = critic.invariants(frame)
        print(f"  {label}: {[item.detail for item in found]}")
        results.append(
            (f"{label} raises exactly one invariant_violation", _one("invariant_violation", found))
        )
        results.append(
            (f"{label} names the rule it broke",
             len(found) == 1 and found[0].detail.startswith(rule_name))
        )
    return results


def _reach_checks() -> list[tuple[str, bool]]:
    """That `check()` really carries the domain rules, not only `invariants()` does.

    Without this the frame half of `check()` is unreachable from any check: on the
    real county every rule holds, so a `check()` that consulted no frame at all
    would return exactly what a working one returns. The shared analysis is
    replaced with a violating frame for the length of one call -- the only stub in
    this module's suite that is not the model, and it is here because the
    alternative is a branch nothing can observe.
    """
    from types import SimpleNamespace

    keep = tools._ANALYSIS
    quiet = Critic().check("The score is 0.5.", [structured_call({"score": 0.5})], 1)
    try:
        tools._ANALYSIS = SimpleNamespace(
            units=broken(Col.VULNERABILITY, 1, 1.4),
            result=SimpleNamespace(tables={}),
        )
        built = tools.analysis_built()
        loud = Critic().check("The score is 0.5.", [structured_call({"score": 0.5})], 1)
    finally:
        tools._ANALYSIS = keep

    print(f"  with no analysis held, check() raises {len(quiet.findings)} finding(s)")
    print(f"  with a violating frame held, check() raises "
          f"{[item.kind for item in loud.findings]}")

    return [
        ("a violating frame was actually reachable through the shared analysis", built),
        ("check() carries the domain rules, not only invariants() does",
         any(item.kind == "invariant_violation" for item in loud.findings)),
        ("the number in the answer still traced while the frame failed",
         loud.numbers_traceable == loud.numbers_checked == 1),
        ("a critic with no analysis to read says nothing about a frame",
         not any(item.kind == "invariant_violation" for item in quiet.findings)),
    ]


def _real_frame_checks() -> list[tuple[str, bool]]:
    """The rules, against the county the pipeline really produced.

    A rule that fires here is either a real defect upstream or a rule written
    wrong, and both are worth knowing. This is also the only check that proves the
    rules are reachable on a frame nobody built to be checked.
    """
    state = tools.analysis()
    critic = Critic()
    frames = real_frames(state)
    covered: set[str] = set()
    reported: list[tuple[str, bool]] = []
    for name, frame in frames.items():
        ran, skipped = applicable(frame)
        covered.update(ran)
        found = critic.invariants(frame)
        print(f"  {name}: {len(frame)} units, {len(ran)} rule(s) applied, "
              f"{len(skipped)} skipped, {len(found)} finding(s)")
        for item in found:
            print(f"    [{item.kind}] {item.detail}")
        reported.append((f"the real {name} frame breaks no rule", not found))

    unreached = [rule.name for rule in RULES if rule.name not in covered]
    print(f"  rules no real frame could be held to: {unreached or 'none'}")
    reported.append(("every rule reaches at least one real frame", not unreached))
    reported.append(("the real frames were not empty",
                     bool(frames) and all(len(frame) > 0 for frame in frames.values())))
    return reported


def _guard_checks() -> list[tuple[str, bool]]:
    """Invariant 3 on the report, without exempting the guard's own marker.

    Two S10 checks skipped any line containing `withheld`, which was the only line
    the leak could be on. Nothing here is skipped: the scan reads every character
    of every finding, markers included.
    """
    pair = "-80.4535530002726 32.4825649998057"
    calls = [stdout_call("count 48192\n")]
    answer = f"The centre of the study area is at {pair} and 31337 residents live there."
    report = Critic().check(answer, calls, 1)
    serialised = json.dumps(as_result(report), default=str)
    faults = tools.coordinate_faults(as_result(report))
    text_faults = [
        line
        for item in report.findings
        for field_text in (item.detail, item.evidence)
        for line in sandbox.output_faults(field_text)
    ]
    quoted = [item for item in report.findings if item.evidence]
    print(f"  a report on an answer carrying a coordinate pair the log never produced: "
          f"{len(report.findings)} finding(s), {len(serialised)} bytes")
    for item in report.findings:
        print(f"    detail:   {item.detail}")
        print(f"    evidence: {item.evidence or '(none)'}")

    long_report = Critic().check(
        " ".join(f"{value}.5" for value in range(400, 500)), [stdout_call("nothing\n")], 1
    )
    long_bytes = len(json.dumps(as_result(long_report), default=str))
    print(f"  a hundred untraceable numbers serialise to {long_bytes} bytes in "
          f"{len(long_report.findings)} finding(s)")

    return [
        ("the answer really did produce findings for the scan to read",
         len(report.findings) > 0 and len(quoted) > 0),
        ("a coordinate pair in the answer never reaches a finding", not text_faults),
        ("the serialised report carries no coordinate shape", not faults),
        ("the pair is not quoted back anywhere in the report", pair not in serialised),
        ("the scan covered the guard's own markers too",
         all(sandbox.output_faults(item.evidence) == [] for item in quoted)),
        ("the report still says something rather than withholding everything",
         any(item.detail.strip() for item in report.findings)),
        ("a report is bounded in findings", len(long_report.findings) <= MAX_FINDINGS),
        ("the truncation is stated rather than silent",
         "not listed" in long_report.findings[-1].detail),
        ("a report stays under the size bound every tool result is held to",
         long_bytes < 24_000),
        ("the count reported is the count checked, not the count listed",
         long_report.numbers_checked == 100),
    ]


def _ordering_checks() -> list[tuple[str, bool]]:
    """The trade-off rule: what a priority ordering owes beyond its numbers."""
    calls = [structured_call({"ranking": [{"score": 0.5}]})]
    bare = Critic().check("The highest-risk units score 0.5.", calls, 1)
    complete = Critic().check(
        "Under the equal weighting the highest-risk units score 0.5. A different "
        "weighting would have prioritised other units, which this one drops.",
        calls,
        1,
    )
    counting = Critic().check("There are 0.5 of them.", calls, 1)
    print(f"  a bare ordering: {[item.detail for item in bare.findings]}")
    print(f"  a complete ordering: {len(complete.findings)} finding(s)")
    print(f"  a counting answer: {len(counting.findings)} finding(s)")

    # Matched on a phrase that appears in one finding and not the other. Both
    # findings mention the word "weighting" -- the who-loses one tells the model to
    # name what another weighting would have prioritised -- so testing for that
    # word let the who-loses finding satisfy the assertion about the weighting one,
    # and deleting the weighting rule outright changed nothing.
    return [
        ("an ordering that names no weighting is an unsupported_claim",
         any(item.kind == "unsupported_claim" and "names no weighting" in item.detail
             for item in bare.findings)),
        ("an ordering that does not say who loses is an unsupported_claim",
         any(item.kind == "unsupported_claim" and "does not say who loses" in item.detail
             for item in bare.findings)),
        ("the two are separate findings, not one counted twice",
         len([item for item in bare.findings
              if item.kind == "unsupported_claim"]) == 2),
        ("an ordering that does both raises neither", complete.passed),
        ("an answer that orders nothing is never asked for a weighting",
         counting.passed),
        ("the numbers in all three still traced",
         bare.numbers_traceable == bare.numbers_checked == 1),
    ]


def _surface_checks() -> list[tuple[str, bool]]:
    """That the tool this module backs has stopped being pending."""
    pending = tools.pending_tools()
    faults = tools.surface_faults()
    result = tools.validate_answer(answer="The score is 0.5.")
    print(f"  pending tools now: {list(pending)}")
    print(f"  surface faults: {faults or 'none'}")
    print(f"  validate_answer returned: {result}")

    return [
        ("validate_answer is no longer pending", "validate_answer" not in pending),
        ("the tool surface still has no faults", not faults),
        ("the tool runs the critic rather than returning a refusal",
         result.get("error") is None and result["numbers_checked"] == 1),
        ("the tool reports the five fields the contract gives it",
         set(result) == {"cycle", "passed", "numbers_checked", "numbers_traceable",
                         "findings"}),
        ("this module satisfies the frozen Critic protocol",
         isinstance(Critic(), contracts.Critic)),
        ("a Critic is constructible with no arguments, which is how the tool builds one",
         isinstance(Critic().check("", [], 1), CriticReport)),
        ("the tool surface is still the eleven frozen names",
         set(tools.TOOL_FUNCTIONS) == set(contracts.TOOL_NAMES)),
        ("contracts.py was not edited to make this compile",
         [f.name for f in __import__("dataclasses").fields(CriticReport)]
         == ["cycle", "findings", "numbers_checked", "numbers_traceable"]),
    ]


def _revision_checks() -> list[tuple[str, bool]]:
    """That a finding tells the model what to do with it."""
    report = Critic().check(
        "The total is 31337 residents.", [stdout_call("count 48192\n")], 2
    )
    request = revision_request(report)
    print("  the turn the loop sends back:")
    for line in request.splitlines():
        print(f"    {line}")

    return [
        ("the request names the cycle it is on", "cycle 2" in request),
        ("the request reports how much traced", "0 of 1" in request),
        ("the request names every finding",
         all(item.detail in request for item in report.findings)),
        ("the request says what to do rather than only what is wrong",
         "call the tool that produces it" in request),
        ("the request tells the model not to disturb what traced",
         "already traced" in request),
    ]


def _loop_checks() -> list[tuple[str, bool]]:
    """The revision cycle in `agent.py`: that it fires, that it stops, that it counts.

    A critic that never passes is the only honest way to test the bound, because a
    cycle that happens to converge proves the loop terminated, not that it was
    bounded. The stubborn draft below carries a number nothing produced and
    repeats it, so every cycle finds the same thing and only the bound can end it.
    """
    from . import agent

    bound = agent.MAX_REVISIONS
    stubborn = "The exposed population is 31337 residents."
    never = scripted_run([stubborn], bound)
    report_only = scripted_run([stubborn], 0)
    clean = scripted_run(["No numbers were available for this question."], bound)
    fixed = scripted_run([stubborn, "I cannot support that figure, so I withdraw it."],
                         bound)
    # A revision that adds findings rather than removing them. Without it the
    # `made_worse` comparison is only ever exercised in its false direction, and a
    # flipped comparison would survive every other scenario here -- which is the
    # one number a reviewer is least likely to take on trust.
    #
    # The second draft adds an ordering rather than more numbers, because these
    # runs log no tool call at all: an empty call log costs one finding however
    # many numbers the answer carries, so piling on numbers would have left the
    # count flat and the fixture would have tested nothing.
    worse = scripted_run(
        [stubborn,
         "The highest-risk communities are ranked first; the total is 31337 residents."],
        bound,
    )

    for label, run in (("never passes", never), ("report-only", report_only),
                       ("passes first time", clean), ("revised and fixed", fixed),
                       ("revised and made worse", worse)):
        print(f"  {label}: {run['llm_calls']} llm call(s), stop_reason "
              f"{run['stop_reason']}, {run['revision']}")

    steps = [step["step"] for step in never["steps"]]
    return [
        ("a critic that never passes stops after two revision cycles",
         never["revision"]["revisions_requested"] == bound),
        ("it ran one more critic cycle than it requested revisions",
         never["revision"]["cycles_run"] == bound + 1),
        ("it stopped because of the bound and says so",
         never["stop_reason"] == "revision_limit"),
        ("it did not spend the iteration budget getting there",
         never["llm_calls"] == bound + 1
         and never["llm_calls"] < config.MAX_ITERATIONS),
        ("every critic firing is its own step type",
         steps.count("critic_report") == bound + 1),
        ("every revision is its own step type",
         steps.count("revision_request") == bound),
        ("the run reports findings per cycle rather than a total",
         len(never["revision"]["findings_per_cycle"]) == bound + 1),
        ("the run reports findings by kind",
         never["revision"]["findings_by_kind"].get("untraceable_number", 0) > 0),
        ("an unchanged rewrite is not counted as an answer that changed",
         never["revision"]["answers_changed_after_revision"] == 0),
        ("no revision made it worse, and the count is reported either way",
         never["revision"]["revisions_that_made_it_worse"] == 0),
        ("a revision that adds findings is counted as having made it worse",
         worse["revision"]["revisions_that_made_it_worse"] == 1),
        ("that run really did add findings rather than merely change the answer",
         worse["revision"]["findings_per_cycle"][1]
         > worse["revision"]["findings_per_cycle"][0]),
        ("report-only runs the critic and revises nothing",
         report_only["revision"]["cycles_run"] == 1
         and report_only["revision"]["revisions_requested"] == 0),
        ("report-only still publishes the findings it found",
         report_only["revision"]["findings_per_cycle"] == [1]),
        ("an answer that passes costs one cycle and no revision",
         clean["revision"]["cycles_run"] == 1
         and clean["revision"]["revisions_requested"] == 0
         and clean["stop_reason"] == "final_answer"),
        ("a draft that passes is reported as having passed",
         clean["revision"]["first_draft_passed"] is True),
        ("a revised answer that changes is counted as changed",
         fixed["revision"]["answers_changed_after_revision"] == 1),
        ("a revision that fixes the answer ends the loop before the bound",
         fixed["revision"]["revisions_requested"] == 1
         and fixed["stop_reason"] == "final_answer"),
        ("the first draft failed and the last one passed",
         fixed["revision"]["first_draft_passed"] is False
         and fixed["revision"]["final_draft_passed"] is True),
        ("the revised answer is the one that was kept",
         "withdraw" in (fixed["final_answer"] or "")),
        ("the loop reports the critic as available",
         never["revision"]["critic_available"] is True),
        ("the iteration bound was raised for the cycle",
         config.MAX_ITERATIONS == 15),
    ]


def _self_check() -> int:
    print("CRITIC -- every reported number traced to a logged tool result\n")

    checks = _claim_checks()
    print()
    checks += _boundary_checks()
    print()
    checks += _tolerance_checks()
    print()
    checks += _shape_checks()
    print()
    checks += _answer_checks()
    print()
    checks += _invariant_checks()
    print()
    checks += _ordering_checks()
    print()
    checks += _guard_checks()
    print()
    checks += _revision_checks()
    print()
    checks += _loop_checks()
    print()
    checks += _reach_checks()
    print()
    checks += _real_frame_checks()
    print()
    checks += _surface_checks()
    print()
    checks += verify.discipline_checks(sys.modules[__name__])
    return verify.report(checks)


def main() -> int:
    """Replay a transcript through the critic and print the report.

    With no argument it reads the newest run on disk, which is the cheapest way to
    see what the critic makes of an answer the system really produced.
    """
    wanted = [item for item in sys.argv[1:] if not item.startswith("-")]
    if wanted:
        payload = json.loads(Path(wanted[0]).read_text(encoding="utf-8"))
        answer, steps = payload.get("final_answer") or "", payload.get("steps") or []
        where = wanted[0]
    else:
        real = real_run()
        if real is None:
            print("no transcript on disk; run python -m src.demo first")
            return 1
        answer, steps, path = real
        where = str(path)

    report = Critic().check(answer, steps, 1)
    print(f"transcript: {where}\n")
    print(answer.strip())
    print()
    print(json.dumps(as_result(report), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_self_check() if "--check" in sys.argv[1:] else main())
