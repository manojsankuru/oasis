"""The LLM-visible tool surface: the eleven names in `contracts.TOOL_NAMES`.

Every tool here **reports** a number that something else computed. None of them
computes one. That is the rule the module is built around and it is worth stating
plainly, because it looks like a restriction and is actually the point.

**Why no tool recomputes.** `pipeline.run()` is the only thing in this system that
computes anything, and one run takes 41 seconds for three scenarios: it derives
three inundation surfaces over an eight-million-cell grid, measures every polygon
of two granularities against each, and builds four components per unit. A tool
that called it per invocation would make a six-iteration agent loop take four
minutes of wall clock, and would do it while producing exactly the same numbers
every time. So one `PipelineResult` is built on first need and every tool reads
from it -- see `analysis()`.

The second reason matters more than the speed. `critic.py` in S11 traces every
number in an answer back to a logged tool result, and a tool that arrived at its
number by its own route would be a second computation path: two answers to one
question, agreeing today and free to disagree after any edit. Recombining a
weighting through `Vulnerability.index` and `Risk.combine` -- the same two
functions `pipeline` itself calls -- is not a second path, and `_agreement_checks`
asserts on the real county that the tool route and the pipeline route return the
same units in the same order.

**What is not in the shared run.** `list_datasets`, `describe_layer` on a vector,
`acquire_dataset` and `ask_user_preferences` read the registry and the manifest
only, so the first thing a model does costs no analysis at all.

**Invariant 3 is checked on the serialised result, not on the intention.**
`coordinate_faults` walks the JSON a tool would send and refuses three shapes: a
key that could name a coordinate, a bare token in a VALUE that could name one --
`describe_layer` reporting `INTPTLAT` in a list of column names is a coordinate
reaching a model message even though no number moved -- and any list holding a
number or a list, which is the shape of a coordinate list itself. The pattern is
`pipeline.COORDINATE_PATTERN`, imported rather than rewritten, because TIGERweb
really does ship `CENTLAT`, `CENTLON`, `INTPTLAT` and `INTPTLON` as ordinary text
attributes on the layer `describe_layer` is most likely to be asked about, and
one regular expression that both the deliverable and the tool surface are held to
is one that cannot drift apart.
"""

from __future__ import annotations

import functools
import importlib
import importlib.util
import io
import json
import math
import os
import re
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

import pandas as pd

from . import acquire, align, config, pipeline, schemas, verify, vulnerability
from .contracts import (
    TOOL_NAMES,
    VULNERABILITY_INDICATORS,
    Col,
    HazardScenario,
    ScenarioRow,
    ToolFn,
    ToolResult,
    WeightPreset,
)
from .hazard import HAZARD_SCENARIOS
from .registry import Registry
from .risk import DEFAULT_PRIORITY_UNITS, Risk, vulnerable_label
from .vulnerability import DEFAULT_PRESET, WEIGHT_PRESETS, PRESETS

TOOL_FUNCTIONS: dict[str, ToolFn] = {}
"""Name to implementation, filled by the `@tool` decorator.

The key is the function's own `__name__`, so the executable name and the source
name are the same string rather than two strings somebody keeps in step. What is
still checked, because it is the thing that breaks, is that this set equals
`contracts.TOOL_NAMES` and equals what `schemas.py` advertises."""

CALLS: list[dict[str, Any]] = []
"""Every tool result this process produced, newest last.

Invariant 8 says every number the agent reports is traceable to a logged tool
result. `agent.py` writes the run log to disk for the transcript; this is the
in-process copy `validate_answer` hands the critic, so the tool that checks
traceability does not have to reach back into the loop that called it."""

MAX_CALLS = 200
"""How many results are kept. A bound rather than a growing list, because this
lives for the life of the process and an agent loop is bounded at a dozen turns."""

MAX_LIST = 25
"""Longest list of identifiers any result carries. Longer lists are cut and the
result says how many it left out -- a truncation nobody counts reads as an answer."""

MAX_NOTES = 6
"""Longest run of provenance notes a detail view carries, for the same reason."""

COORDINATE_PATTERN = pipeline.COORDINATE_PATTERN
"""Imported, never rewritten. `pipeline.reported` refuses to WRITE a column whose
name could carry a coordinate; this refuses to SEND one. Two rules with one
pattern cannot disagree about what a coordinate looks like."""

BARE_TOKEN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
"""A string value that could be a column name rather than prose.

The coordinate pattern is written for column names and matches on underscore
boundaries, so applying it to a sentence produces false positives. Applying it to
values that look like identifiers catches the case that matters -- a column name
travelling inside a list -- and leaves prose alone."""

COORDINATE_TEXT = re.compile(r"-?\d{1,3}\.\d{4,}[\s,]+-?\d{1,3}\.\d{4,}")
"""Two decimal degrees side by side anywhere in a sentence.

The third route a coordinate takes, and the one that got through. A `Provenance`
note is free text, written by the retrieval to record what it had to work around,
and `acquire.fetch_osm` records the study extent by printing it -- the bounding
box in both coordinate orders, to fifteen decimal places, in prose. It is neither
a key, nor a bare token, nor a list of numbers, so the two rules above see nothing
and eight coordinates reach the model inside a paragraph about coordinate order.

Written narrowly on purpose: a signed number with at least four decimal places,
next to another one. Populations, depths, percentages and fractions in this
project do not take that shape, and a pair of them is a location in a way that a
single number is not."""

BACKING_MODULES: dict[str, str] = {
    "run_spatial_code": "sandbox",
    "validate_answer": "critic",
}
"""Which module implements the two tools this session does not build.

A mapping from a tool to the module that will run it, which is a fact about the
design. Which of them are PENDING is never written down: `pending_tools()` probes
for the module, so the day `src/sandbox.py` lands the list shortens with no edit
here. A hardcoded pending set is the same defect as a harness hardcoded to one
county's tract count -- correct until the thing it describes changes, and silent
when it does."""

TRACT_KEY = f"{acquire.DATASET_TRACTS}{align.JOINED_SUFFIX}"
GROUP_KEY = f"{acquire.DATASET_BLOCK_GROUPS}{align.JOINED_SUFFIX}"

SCENARIOS: dict[str, HazardScenario] = {item.name: item for item in HAZARD_SCENARIOS}
DEFAULT_SCENARIO: HazardScenario = HAZARD_SCENARIOS[0]
"""The scenario a caller gets by leaving the argument empty: the first the study
area declares, which is the shallowest surge and so the most conservative claim."""


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------


def tool(fn: ToolFn) -> ToolFn:
    """Register a function as the tool it is named after, and log what it returns.

    Registration by `__name__` rather than by a table means the name the model
    calls and the name in the source are one string. The log is here rather than
    at eleven call sites for the same reason.
    """

    @functools.wraps(fn)
    def logged(**kwargs: Any) -> ToolResult:
        result = fn(**kwargs)
        CALLS.append(
            {"tool": fn.__name__, "arguments": dict(kwargs), "result": result}
        )
        del CALLS[:-MAX_CALLS]
        return result

    TOOL_FUNCTIONS[fn.__name__] = logged
    return logged


def logged_calls() -> list[dict[str, Any]]:
    """Every tool result this process produced, for the critic to trace against."""
    return list(CALLS)


QUESTION = ""
"""The question this run was asked, as `agent.run_agent` passed it to `forget_calls`.

Held so a weighting built from a person's words can be traced to somewhere they
said them. A tool never sees the conversation; this one string is the part of it
`preference_trace` may read."""


def forget_calls(question: str = "") -> None:
    """Drop the call log and start a new question. A new question is a new
    traceability question.

    The scripted-reply cursor goes with it, so each question a harness asks starts
    at the first reply its script holds rather than wherever the last one stopped.
    """
    global _SCRIPT_POSITION, QUESTION
    CALLS.clear()
    _SCRIPT_POSITION = 0
    QUESTION = question


# ---------------------------------------------------------------------------
# the shared analysis
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Analysis:
    """One `pipeline.run()`, shared by every tool for the life of the process."""

    result: pipeline.PipelineResult
    engine: Risk
    units: Any
    built_at: str
    seconds: float


_ANALYSIS: Analysis | None = None


def analysis() -> Analysis:
    """The shared run, built on first need.

    `Risk()` is constructed here rather than reached for per call because
    `Risk.combine` and `Vulnerability.index` are pure frame operations -- they
    touch no file and no service -- so a caller recombining a weighting is calling
    the same two functions the pipeline called, on the same frame, and not opening
    a second route to the answer.
    """
    global _ANALYSIS
    if _ANALYSIS is None:
        started = time.time()
        result = pipeline.run()
        _ANALYSIS = Analysis(
            result=result,
            engine=Risk(),
            units=result.snapshot.frames[TRACT_KEY],
            built_at=datetime.now().isoformat(timespec="seconds"),
            seconds=round(time.time() - started, 2),
        )
    return _ANALYSIS


def invalidate() -> None:
    """Forget the shared run. Called after a live retrieval rewrites the snapshot."""
    global _ANALYSIS
    _ANALYSIS = None


def analysis_built() -> bool:
    """Is a shared run currently held?

    Exposed so that "a live retrieval invalidates the analysis" can be checked
    without paying to rebuild it. Without this the only observable of a failed
    invalidation is a stale answer after a retrieval, which no offline check can
    reach and which would show up as a wrong number rather than as a failure.
    """
    return _ANALYSIS is not None


def registry() -> Registry:
    """A registry with the manifest freshly read.

    Read per call rather than cached, so a layer `acquire_dataset` has just
    rewritten is the layer the next `list_datasets` reports.
    """
    found = Registry()
    found.load_manifest()
    return found


# ---------------------------------------------------------------------------
# invariant 3: what a result may not carry
# ---------------------------------------------------------------------------


def coordinate_faults(payload: Any, path: str = "result") -> list[str]:
    """Every way this payload could carry a coordinate into a model message.

    Three shapes, because they arrive by three routes. A KEY that names a
    coordinate is the obvious one. A bare-token VALUE that names one is the route
    `describe_layer` opens by reporting the column names of a layer TIGERweb ships
    with `INTPTLAT` on it. A LIST holding a number or another list is the shape of
    a coordinate list itself, and is refused whatever it is called -- which is why
    every sequence of numbers this module returns is a list of named records
    instead.
    """
    faults: list[str] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            here = f"{path}.{key}"
            if COORDINATE_PATTERN.search(str(key)):
                faults.append(f"{here}: key could name a coordinate")
            faults.extend(coordinate_faults(value, here))
    elif isinstance(payload, list):
        for index, value in enumerate(payload):
            here = f"{path}[{index}]"
            if isinstance(value, bool):
                continue
            if isinstance(value, (int, float)):
                faults.append(f"{here}: list holds a number, which is a coordinate shape")
            elif isinstance(value, list):
                faults.append(f"{here}: list holds a list, which is a coordinate shape")
            faults.extend(coordinate_faults(value, here))
    elif isinstance(payload, str):
        if BARE_TOKEN.match(payload) and COORDINATE_PATTERN.search(payload):
            faults.append(f"{path}: value {payload!r} could name a coordinate")
        if COORDINATE_TEXT.search(payload):
            faults.append(
                f"{path}: value carries a coordinate pair in prose: "
                f"{COORDINATE_TEXT.search(payload).group(0)!r}"
            )
    return faults


def as_sent(result: ToolResult) -> Any:
    """The result as the model would receive it: serialised and read back.

    Checked on the round trip rather than on the dict, because what a tool returns
    and what `json.dumps(..., default=str)` makes of it are not the same object --
    a tuple becomes a list, a pandas scalar becomes a string -- and the second one
    is what crosses into the message.
    """
    return json.loads(json.dumps(result, default=str))


def reportable_columns(columns: Iterable[Any]) -> tuple[list[str], list[str]]:
    """Split column names into the ones a result may carry and the ones it may not."""
    kept: list[str] = []
    withheld: list[str] = []
    for column in columns:
        name = str(column)
        (withheld if COORDINATE_PATTERN.search(name) else kept).append(name)
    return kept, withheld


# ---------------------------------------------------------------------------
# small shaping helpers -- formatting only, never arithmetic
# ---------------------------------------------------------------------------


def _scalar(value: Any) -> Any:
    """One frame value as JSON, with a missing value stated rather than dropped."""
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, bool):
        return bool(value)
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, float):
        return round(value, 6)
    if isinstance(value, (int, str)):
        return value
    return str(value)


def _capped(items: Iterable[Any], limit: int = MAX_LIST) -> dict[str, Any]:
    """A list cut to a bound, with the number cut named beside it."""
    listed = [str(item) for item in items]
    return {
        "listed": listed[:limit],
        "total": len(listed),
        "not_listed": max(0, len(listed) - limit),
    }


def _numbers(values: dict[str, Any]) -> dict[str, Any]:
    return {key: _scalar(value) for key, value in values.items()}


def _ranking(
    frame: pd.DataFrame, *, by: str, columns: tuple[str, ...], top_n: int
) -> tuple[list[dict[str, Any]], int, int]:
    """The top `top_n` rows by one column, as records, plus what was left out.

    Sorting and slicing read values; nothing here derives one. The two counts
    returned are the reason: a ranking that does not say how many units it dropped
    for having no value, and how many it simply did not show, is a list of ten
    units presented as if it were the county.
    """
    usable = frame.dropna(subset=[by])
    ordered = usable.sort_values(by, ascending=False)
    rows = [
        {column: _scalar(row[column]) for column in columns}
        for _, row in ordered.head(top_n).iterrows()
    ]
    return rows, len(frame) - len(usable), max(0, len(usable) - len(rows))


def _omission(evidence_units: int, unscored: int, unscored_geoids: Iterable[str]) -> dict[str, Any]:
    """Who a ranking left out and why, read from the evidence that recorded it."""
    return {
        "units": evidence_units,
        "units_scored": evidence_units - unscored,
        "units_unscored": unscored,
        "unscored_geoids": _capped(unscored_geoids),
        "why_unscored": (
            "a unit is not scored when any indicator or component is undefined for "
            "it; weighting the ones it does publish would answer a different "
            "question for that unit than for every other"
        ),
    }


def _provenance(record: Any, *, with_notes: bool = True) -> dict[str, Any]:
    """One dataset's provenance as JSON. Invariant 6, made citable.

    The notes are where retrieval records what it had to work around -- a service
    that paged short, a variable resolved by matching a label -- and across seven
    datasets they run to tens of kilobytes, which is most of a model's useful
    context spent before it has asked anything. `list_datasets` is an index and
    takes the count; `describe_layer` is the detail view and takes the notes.
    """
    source = record.provenance
    described = {
        "source_url": source.source_url,
        "retrieved_at": source.retrieved_at.isoformat(),
        "declared_crs": source.declared_crs,
        "working_crs": source.working_crs,
        "vintage": source.vintage,
        "feature_count": source.feature_count,
        "license": source.license,
        "degraded": align.is_degraded(source),
    }
    if with_notes:
        safe = [note for note in source.notes if not COORDINATE_TEXT.search(note)]
        described["notes"] = _capped(safe, MAX_NOTES)
        described["notes_withheld"] = len(source.notes) - len(safe)
        described["why_withheld_notes"] = (
            "a retrieval note that spells out a coordinate is withheld here and "
            "kept in the manifest on disk. Invariant 3 is about what reaches a "
            "model message, not about what the provenance record holds, and the "
            "study extent is derived from the boundary layer at run time rather "
            "than read from a note"
        )
    else:
        described["note_count"] = len(source.notes)
    return described


def _preset(name: str) -> WeightPreset:
    if not name:
        return DEFAULT_PRESET
    return vulnerability.preset_named(name)


def _scenario(name: str) -> HazardScenario:
    if not name:
        return DEFAULT_SCENARIO
    try:
        return SCENARIOS[name]
    except KeyError:
        raise KeyError(
            f"no hazard scenario named {name!r}; this study area carries "
            f"{sorted(SCENARIOS)}"
        ) from None


def _refusal(exc: Exception, kind: str) -> ToolResult:
    """A refusal the model can act on, rather than an exception it has to parse."""
    return {"error": kind, "detail": str(exc).strip("'\"")}


# ---------------------------------------------------------------------------
# recombination -- the same two functions the pipeline called
# ---------------------------------------------------------------------------


def scored(
    scenario: HazardScenario,
    preset: WeightPreset,
    weights: dict[str, float] | None = None,
) -> tuple[pd.DataFrame, Any, Any]:
    """One scenario's table under one weighting.

    A preset carries two halves: weights over the indicators, which decide
    `Col.VULNERABILITY`, and weights over the objective terms, which decide how
    that column trades off against the other three. Recomputing only the index and
    re-ranking is what makes both halves vary; it re-reads no raster, because the
    hazard columns in the frame do not depend on the weighting.

    Run for the pipeline's own preset too, with no branch. A branch here would
    mean the default answer and every other answer came from different code, and
    the check that the two agree would only ever exercise one of them.
    """
    state = analysis()
    table = state.result.tables[scenario.name]
    index, index_evidence = state.engine.index.index(
        state.units, preset=preset, weights=weights, dataset=TRACT_KEY
    )
    frame = table.frame.copy()
    frame[Col.VULNERABILITY] = pd.Series(
        index.to_numpy(), index=table.frame.index, dtype="Float64"
    )
    combined, risk_evidence = state.engine.combine(
        frame, scenario=scenario, preset=preset, dataset=TRACT_KEY
    )
    return combined, risk_evidence, index_evidence


def overridden_weights(preset: WeightPreset, overrides: dict[str, float]) -> tuple[dict[str, float] | None, dict[str, float]]:
    """The preset's weights with the caller's overrides applied, and which applied.

    `schemas.UNSET_WEIGHT` is negative and the index refuses a negative weight, so
    an unset argument cannot be confused with a preference. `None` back means the
    caller overrode nothing and the preset's own weights are used unchanged, which
    keeps the evidence able to say the weighting came from its stated origin.
    """
    applied = {
        name: value
        for name, value in overrides.items()
        if value != schemas.UNSET_WEIGHT
    }
    if not applied:
        return None, {}
    return {**preset.weights, **applied}, applied


# ---------------------------------------------------------------------------
# a weighting built from a person's own words
# ---------------------------------------------------------------------------


HEMISPHERE_PAIR = re.compile(
    r"[NSEWnsew]?\s*-?\d+\.\d+\s*°?\s*[NSEWnsew]?"
    r"\s*(?:[,;/|\s()\[\]]|\s-\s)+\s*"
    r"[NSEWnsew]?\s*-?\d+\.\d+"
)
"""Two decimal numbers with a hemisphere letter, a degree sign, a slash, a bar, a
semicolon or a spaced dash beside them: `20.6543 N 10.1234 W`, `N20.6543
W10.1234`, `20.65°N, 10.12°W`, `20.6543/-10.1234`, `20.6543 - 10.1234`.

The shapes `sandbox.output_faults` does not reach, because its pair rule allows
only space, comma and brackets between the two numbers. Narrow on purpose: two
weights a person states as "age 0.4 and car 0.4" have words between them and
pass, and an unspaced dash is a range, as in "1.5-3.0 m"."""

CARRIES_COORDINATE = "the words could carry a coordinate"
GUARD_UNAVAILABLE = (
    "the coordinate guard in sandbox.py could not be loaded, so no free text can be "
    "checked and none is quoted back"
)


def coordinate_refusal(text: str) -> str:
    """Why these free-text words may not be quoted into a model message, or "".

    A person's words are the one free text this module quotes back, so they are
    held to the widest rule this project has: `coordinate_faults` on the string,
    `HEMISPHERE_PAIR` for the separators other rules do not allow, and
    `sandbox.output_faults` on every line, which covers WKT, GeoJSON, labelled
    pairs and pairs rounded to three places. `sandbox` imports this module, so it
    is imported here, at call time. If it cannot be imported every reply is
    refused, and the reason says so rather than blaming the words: a guard that
    fails open is not a guard, and one that fails with a false reason misleads.

    Not covered, stated rather than implied: degrees, minutes and seconds written
    as integers, a single coordinate standing alone, and a pair with a word
    between, as in "20.6543 and -10.1234" -- matching that would refuse "the 1.5
    and 3.0 m scenarios". Refused though harmless: replies that use the word
    "coordinates", or put a decimal right after a coordinate-like label such as x.
    The prompt asks once more when it refuses a reply.
    """
    try:
        from . import sandbox
    except ImportError:
        return GUARD_UNAVAILABLE
    if coordinate_faults({"stated_preference": text}) or HEMISPHERE_PAIR.search(text):
        return CARRIES_COORDINATE
    if any(sandbox.output_faults(line) for line in text.splitlines() or [text]):
        return CARRIES_COORDINATE
    return ""


def requested_weights(arguments: dict[str, Any]) -> tuple[dict[str, float], list[str]]:
    """The weights a risk tool call set, by weightable name, and any misspelled.

    An argument left at `schemas.UNSET_WEIGHT` was not set. One that starts
    `weight_` and names no weightable key comes back as misspelled, so the tool can
    refuse it by name. Any other unknown argument is ignored, as every tool
    ignores one.
    """
    legal = set(schemas.weight_arguments(schemas.RISK_WEIGHT_KEYS))
    chosen: dict[str, float] = {}
    misspelled: list[str] = []
    for argument, value in arguments.items():
        if not argument.startswith(schemas.WEIGHT_ARG_PREFIX):
            continue
        if argument not in legal:
            misspelled.append(argument)
        elif value != schemas.UNSET_WEIGHT:
            chosen[schemas.weighted_indicator(argument)] = float(value)
    return chosen, sorted(misspelled)


def user_weighting(
    base: WeightPreset, arguments: dict[str, Any], preference: str
) -> WeightPreset | None:
    """The weighting a person's words describe, or None when no weight was set.

    A weight the call left unset keeps `base`'s value, the rule
    `vulnerability_index` already applies to its overrides. What comes back is a
    `WeightPreset` with `origin="user"` and the person's sentence as its
    `origin_note`, which the frozen contract already allows, so it travels through
    `scored` and `Risk.compare_presets` exactly as a published weighting does.

    Four refusals, each a way a ranking could claim to follow somebody it does not.
    A misspelled weight drops part of what they said. Weights with no sentence are
    a preference nobody stated. A sentence with no weight ranks under the default
    while reading as theirs. A negative or all-zero half is refused by
    `normalised_weights` before any unit is scored.
    """
    chosen, misspelled = requested_weights(arguments)
    if misspelled:
        raise KeyError(
            f"{misspelled} name no weightable key; the weight arguments are "
            f"{list(schemas.weight_arguments(schemas.RISK_WEIGHT_KEYS))}. A misspelled "
            "weight is part of what the person said, dropped in silence"
        )
    unreadable = {name: value for name, value in chosen.items() if not math.isfinite(value)}
    if unreadable:
        raise ValueError(
            f"the weights {unreadable} are not finite numbers. Python's JSON reader "
            "accepts NaN and Infinity, and either one would rank every unit on a "
            "number that means nothing"
        )
    stated = preference.strip()
    if not chosen:
        if stated:
            raise ValueError(
                "a stated preference arrived with no weight set, so the ranking would "
                "be the named weighting's while reading as the person's. Map their "
                "words to weight_* arguments first, or leave preference empty"
            )
        return None
    if not stated:
        raise ValueError(
            f"weights for {sorted(chosen)} were set with no preference. A weighting "
            f"named '{schemas.USER_WEIGHTING}' carries the sentence it was built from: "
            "pass the person's words unchanged as preference, or use a named weighting"
        )
    refusal = coordinate_refusal(stated)
    if refusal:
        raise ValueError(
            f"the preference cannot be echoed into this result: {refusal}. "
            "Invariant 3 keeps geometry out of every model message. Pass the "
            "person's words without the location in them"
        )
    weights = {**base.weights, **chosen}
    vulnerability.normalised_weights(weights, VULNERABILITY_INDICATORS)
    vulnerability.normalised_weights(weights, vulnerability.OBJECTIVE_TERMS)
    return WeightPreset(
        name=schemas.USER_WEIGHTING,
        weights=weights,
        origin=schemas.USER_WEIGHTING,
        origin_note=stated,
    )


def _comparable(text: str) -> str:
    """Words as compared for a trace: case folded and runs of whitespace collapsed."""
    return " ".join(text.casefold().split())


MIN_QUOTED_WORDS = 3
"""The fewest words a preference may quote from the question.

A single character or a single word of a question states nothing -- "?" and
"first" are in almost every question -- and a trace that accepted them would let
any weights rank under a label crediting whoever wrote the question."""


def _quoted_from_question(stated: str) -> bool:
    """Do these words appear in the question, whole and on word boundaries?

    What this cannot check, stated rather than implied: that the weights follow
    the words. That mapping is the model's, made in the open as `weight_*`
    arguments beside the quoted words, where a reader can disagree with it.
    """
    if len(stated.split()) < MIN_QUOTED_WORDS:
        return False
    pattern = rf"(?<!\w){re.escape(stated)}(?!\w)"
    return re.search(pattern, _comparable(QUESTION)) is not None


def preference_trace(preference: str) -> dict[str, Any]:
    """Who stated the sentence behind a user weighting, as far as this run shows.

    A tool never sees the conversation, so two places count. A reply that
    `ask_user_preferences` returned on this question, which says whether a person
    or a script gave it. Or the question itself, as `forget_calls` was given it, when
    the words appear in it unchanged. Anywhere else, `answered_by` is None, and
    `stated_weighting` refuses to rank under words nobody on this question said.
    """
    stated = _comparable(preference)
    for call in reversed(logged_calls()):
        result = call.get("result") or {}
        if (
            call.get("tool") == "ask_user_preferences"
            and stated
            and _comparable(str(result.get("stated_preference") or "")) == stated
        ):
            return {
                "answered_by": "person" if result.get("elicited") is True else "script",
                "traced_to": "ask_user_preferences",
                "elicited": result.get("elicited"),
                "channel": result.get("channel"),
            }
    if _quoted_from_question(stated):
        return {
            "answered_by": "question",
            "traced_to": "the question this run was asked",
            "note": (
                "these words appear unchanged in the question, so whoever wrote the "
                "question stated them; no ask_user_preferences reply did"
            ),
        }
    return {
        "answered_by": None,
        "traced_to": None,
        "note": (
            "neither an ask_user_preferences reply on this question nor the question "
            "itself holds these words, so they are not verified as anybody's"
        ),
    }


class UnstatedPreference(ValueError):
    """A weighting called the person's, built from words nobody on this question said."""


def stated_weighting(
    base: WeightPreset, arguments: dict[str, Any], preference: str
) -> tuple[WeightPreset | None, dict[str, Any]]:
    """`user_weighting`, refused unless somebody on this question said the words.

    The rule "never invent a preference the person did not state" is written in
    `agent.RULES`, and a rule a model is only asked to follow is a rule it can
    break. This is where it is enforced: a weighting called the person's has to
    trace to a reply or to the question, or nothing is ranked under it.
    """
    user = user_weighting(base, arguments, preference)
    if user is None:
        return None, {}
    trace = preference_trace(user.origin_note)
    if trace["answered_by"] is None:
        raise UnstatedPreference(
            "no ask_user_preferences reply on this question and no part of the "
            "question holds these words, so ranking under them would credit a "
            "preference to somebody who never stated it. Pass the person's words "
            "unchanged, or use a named weighting and say nobody chose it"
        )
    return user, trace


def user_fields(
    user: WeightPreset, base: WeightPreset, arguments: dict[str, Any], trace: dict[str, Any]
) -> dict[str, Any]:
    """What a ranking under a person's words adds to a result: who said the words,
    the words, and the weights as set and as used.

    The weights used are the two halves scaled by `normalised_weights`, the call
    the index and the score make themselves, so the numbers the model is told to
    state are numbers a tool returned -- from either risk tool, not only one.
    """
    chosen, _ = requested_weights(arguments)
    return {
        "answered_by": trace["answered_by"],
        "stated_preference": user.origin_note,
        "preference_trace": trace,
        "weights_set": _numbers(chosen),
        "unset_weights_from": base.name,
        "indicator_weights_used": _numbers(
            vulnerability.normalised_weights(user.weights, VULNERABILITY_INDICATORS)
        ),
        "component_weights": _numbers(
            vulnerability.normalised_weights(user.weights, vulnerability.OBJECTIVE_TERMS)
        ),
    }


def pairs_against(rows: list[ScenarioRow], name: str) -> list[dict[str, Any]]:
    """One weighting's priority list against each other weighting's, a pair at a time.

    Set differences over the GEOIDs `Risk.compare_presets` already chose, each kept
    in its own list's rank order. No unit is scored here. `ScenarioRow.
    displaced_geoids` pools every other weighting into one set, which says who a
    weighting drops but not whose list they came from; this says whose.
    """
    mine = [row for row in rows if row.preset == name][0].top_geoids
    pairs: list[dict[str, Any]] = []
    for row in rows:
        if row.preset == name:
            continue
        dropped = [geoid for geoid in row.top_geoids if geoid not in mine]
        added = [geoid for geoid in mine if geoid not in row.top_geoids]
        pairs.append(
            {
                "against": row.preset,
                "dropped": _capped(dropped),
                "dropped_count": len(dropped),
                "added": _capped(added),
                "added_count": len(added),
                "shared_count": len(mine) - len(added),
            }
        )
    return pairs


# ---------------------------------------------------------------------------
# the eleven tools
# ---------------------------------------------------------------------------


@tool
def list_datasets() -> ToolResult:
    """Every registered dataset with the provenance needed to cite it."""
    found = registry()
    records = found.records()
    return {
        "study_area": found.study_area.name,
        "working_crs": found.working_crs,
        "storage_crs": config.STORAGE_CRS,
        "dataset_count": len(records),
        "datasets": [
            {"name": record.name, "kind": record.kind, **_provenance(record, with_notes=False)}
            for record in records
        ],
        "degraded": [
            record.name for record in records if align.is_degraded(record.provenance)
        ],
        "note": (
            "every layer here was retrieved by this system and carries the URL it "
            "came from, the moment it was retrieved and the licence it is published "
            "under. Nothing was edited by hand. Call describe_layer for a layer's "
            "retrieval notes, which say what the service made this system work around."
        ),
    }


@tool
def acquire_dataset(name: str, timeout_s: float = config.REQUEST_TIMEOUT_S) -> ToolResult:
    """Retrieve one dataset live from the service that publishes it.

    The autonomy showcase: the model names a layer and the system performs the
    real request, re-derives the study extent from the retrieved boundaries when
    the layer needs one, and re-registers it with fresh provenance. Every call
    carries the explicit timeout invariant 7 requires, taken from the argument
    rather than from a constant here, so the bound is visible to the caller.
    """
    groups = _retrievers()
    match = [group for group in groups if name in group.datasets]
    if not match:
        return {
            "error": "unknown dataset",
            "detail": f"nothing retrievable is named {name!r}",
            "retrievable": sorted(item for group in groups for item in group.datasets),
        }
    group = match[0]
    found = registry()
    area = found.study_area
    started = time.time()
    if group.needs_extent:
        boundaries = found.load(acquire.DATASET_TRACTS)
        extent = config.derive_bbox(boundaries)
        produced = group.call(area, found, extent, timeout_s=timeout_s)
    else:
        produced = group.call(area, found, timeout_s=timeout_s)
    records = list(produced) if isinstance(produced, list) else [produced]
    manifest = found.save_manifest()
    invalidate()
    return {
        "requested": name,
        "timeout_s": timeout_s,
        "seconds": round(time.time() - started, 2),
        "retrieved": [
            {"name": record.name, "kind": record.kind, **_provenance(record)}
            for record in records
        ],
        "manifest": manifest.name,
        "analysis_invalidated": True,
        "note": (
            "this was a live request to the publishing service, not a read of the "
            "stored snapshot. The analysis will be rebuilt from the new data on the "
            "next tool call that needs it."
        ),
    }


@tool
def describe_layer(name: str, max_columns: int = 30) -> ToolResult:
    """One layer's shape, columns, null counts, CRS and provenance.

    Columns whose names could carry a coordinate are counted and withheld rather
    than listed. On this study area that is not hypothetical: TIGERweb ships four
    of them on the tract layer as ordinary text attributes, and a list of column
    names is a model message like any other.
    """
    found = registry()
    try:
        record = found.record(name)
    except KeyError as exc:
        return {**_refusal(exc, "unknown dataset"), "datasets": found.names()}

    described: dict[str, Any] = {
        "name": record.name,
        "kind": record.kind,
        **_provenance(record),
    }
    if record.kind == "raster":
        described.update(_raster_facts(record.name))
        return described

    frame = found.load(record.name)
    kept, withheld = reportable_columns(frame.columns)
    described.update(
        {
            "rows": len(frame),
            "column_count": len(frame.columns),
            "columns_withheld": len(withheld),
            "columns_not_listed": max(0, len(kept) - max_columns),
            "columns": [
                {
                    "name": column,
                    "dtype": str(frame[column].dtype),
                    "nulls": int(frame[column].isna().sum()),
                }
                for column in kept[:max_columns]
            ],
            "why_withheld": (
                "a column whose name could carry a coordinate is not reported; "
                "geometry never reaches a model message, and a coordinate wearing a "
                "column name is still a coordinate"
            ),
        }
    )
    if hasattr(frame, "crs") and frame.crs is not None:
        described["crs_on_load"] = frame.crs.to_string()
    return described


def _raster_facts(name: str) -> dict[str, Any]:
    """A raster's grid, read from the evidence the hazard stage already recorded.

    A raster cannot be handed to `Registry.load`, and opening it here would be a
    second reader of a file `align.zonal_stats` already owns. The grid facts below
    were counted once, while the inundation surface was derived from this raster,
    and are quoted from there.
    """
    state = analysis()
    surfaces = [
        surface
        for surface in state.result.hazard.surfaces.values()
        if surface.evidence.source_raster == name
    ]
    if not surfaces:
        return {
            "grid": None,
            "note": (
                f"{name} is a raster and no inundation surface was derived from it "
                "on this run, so no grid statistics were counted for it"
            ),
        }
    grid = surfaces[0].evidence
    return {
        "grid": _numbers(
            {
                "crs": grid.crs,
                "cell_size_m": grid.cell_size_m,
                "cells": grid.cells,
                "usable_cells": grid.usable_cells,
                "nodata_cells": grid.nodata_cells,
                "non_finite_cells": grid.non_finite_cells,
                "elevation_min_m": grid.elevation_min_m,
                "elevation_max_m": grid.elevation_max_m,
            }
        ),
        "note": (
            "counted once while the inundation surface was derived from this raster. "
            "A nodata count of zero means zero cells of the whole grid were missing, "
            "not that nothing was checked -- the denominator is beside it."
        ),
    }


@tool
def describe_alignment() -> ToolResult:
    """What the cleaning stage did, with the denominator beside every count.

    `AlignmentReport` is frozen and carries numerators only. Reporting those alone
    would make "nothing needed repairing" and "repair never ran" the same result,
    so every count here is paired with what it was counted over, from
    `AlignmentEvidence`.
    """
    state = analysis()
    report = state.result.snapshot.report
    evidence = state.result.snapshot.evidence
    return {
        "study_area": state.result.study_area.name,
        "working_crs": state.result.study_area.working_crs,
        "datasets_cleaned": list(evidence.datasets),
        "layers_produced": sorted(state.result.snapshot.frames),
        "reprojected": dict(report.reprojected),
        "crs_observed": dict(evidence.crs_observed),
        "repairs": _numbers(
            {
                "examined": evidence.geometries_examined,
                "invalid_before": evidence.geometries_invalid,
                "repaired": report.geometries_repaired,
                "dropped": report.geometries_dropped,
            }
        ),
        "sentinels": {
            "values_examined": evidence.values_examined,
            "columns_examined": evidence.scrub_columns,
            "non_numeric": evidence.non_numeric,
            "removed": dict(report.sentinels_removed),
            "codes_seen": evidence.codes_seen(),
        },
        "geoid_audit": {
            "compared": evidence.geoids_compared,
            "matched": evidence.geoids_matched,
            "unmatched_boundary_side": _capped(report.unmatched_left),
            "unmatched_attribute_side": _capped(report.unmatched_right),
        },
        "granularity": {
            "units_apportioned": evidence.units_apportioned,
            "method": dict(report.apportioned),
            "error_pct_against_published": _numbers(report.apportionment_error),
        },
        "raster_join": {
            "polygons_measured": evidence.polygons_measured,
            "smallest_polygon_cells": evidence.smallest_polygon_cells,
            "units_below_cell_threshold": report.units_below_cell_threshold,
        },
        "degraded": dict(evidence.degraded),
        "derived_columns": dict(evidence.derived),
        "temporal_span": dict(report.temporal_span),
        "warnings": list(report.warnings),
        "note": (
            "no dataset was edited by hand. Every number here is a count of "
            "something the cleaning stage did at run time, beside the number of "
            "things it looked at to do it."
        ),
    }


@tool
def hazard_exposure(scenario: str = "", top_n: int = schemas.DEFAULT_TOP_N) -> ToolResult:
    """Inundation and exposed population per unit, for one surge scenario."""
    try:
        chosen = _scenario(scenario)
    except KeyError as exc:
        return {**_refusal(exc, "unknown scenario"), "scenarios": sorted(SCENARIOS)}

    state = analysis()
    table = state.result.tables[chosen.name]
    exposure = table.exposure
    measure = [
        item for item in state.result.hazard.measures if item.scenario == chosen.name
    ][0]
    rows, without_value, not_shown = _ranking(
        table.frame,
        by=Col.EXPOSED_POPULATION,
        columns=(
            Col.GEOID,
            Col.POPULATION,
            Col.INUNDATED_FRACTION,
            Col.INUNDATION_MEAN_M,
            Col.INUNDATION_MAX_M,
            Col.EXPOSED_POPULATION,
        ),
        top_n=top_n,
    )
    return {
        "scenario": chosen.name,
        "surge_height_m": chosen.surge_height_m,
        "surge_source": chosen.source,
        "assumption": chosen.assumption_note,
        "model": "bathtub inundation: depth = max(0, surge height - ground elevation)",
        "county_totals": _numbers(
            {
                "population": exposure.population_total,
                "exposed_population": exposure.fine_total,
                "exposed_population_coarse_estimate": exposure.coarse_total,
                "granularity_gap_worst_unit": exposure.max_abs_difference,
                "granularity_error_pct": exposure.relative_error_pct,
            }
        ),
        "granularity": {
            "reported_estimate": exposure.fine,
            "compared_against": exposure.coarse,
            "fine_units": exposure.fine_units,
            "coarse_units": exposure.coarse_units,
            "units_compared": exposure.units_compared,
            "method": exposure.method_note,
            "note": (
                "the reported figure sums block groups; the second multiplies each "
                "tract's population by its own flooded fraction. The gap between "
                "them is a result about granularity, not an error either made."
            ),
        },
        "units": _numbers(
            {
                "measured": measure.units_measured,
                "without_raster_cells": measure.units_without_cells,
                "fully_wet": measure.units_fully_wet,
                "fully_dry": measure.units_fully_dry,
                "fraction_min": measure.fraction_min,
                "fraction_max": measure.fraction_max,
                "deepest_unit": measure.deepest_unit,
                "deepest_m": measure.deepest_m,
            }
        ),
        "ranking": rows,
        "ranked_by": Col.EXPOSED_POPULATION,
        "units_without_a_value": without_value,
        "units_not_shown": not_shown,
        "vector_hazard": dict(state.result.hazard.vector_hazard),
        "warnings": list(measure.warnings) + list(exposure.warnings),
    }


@tool
def vulnerability_index(
    preset: str = "",
    top_n: int = schemas.DEFAULT_TOP_N,
    **overrides: Any,
) -> ToolResult:
    """The weighted percentile index, under a named weighting or an overridden one.

    A `weight_*` argument naming no indicator is refused by name, as the risk
    tools refuse one. The index weights indicators only, so an objective term such
    as `weight_exposed_population` -- legal on `risk_scenario` -- is refused here
    with a pointer there, rather than dropped while the index ranks as though the
    person had said nothing.
    """
    try:
        chosen = _preset(preset)
    except KeyError as exc:
        return {**_refusal(exc, "unknown preset"), "presets": sorted(PRESETS)}
    unknown = sorted(
        argument
        for argument in overrides
        if argument.startswith(schemas.WEIGHT_ARG_PREFIX)
        and argument not in schemas.weight_arguments()
    )
    if unknown:
        return {
            "error": "invalid weighting",
            "detail": (
                f"{unknown} name no indicator; the index weights only "
                f"{list(schemas.weight_arguments())}. The objective terms are weighted "
                "on risk_scenario and compare_scenarios"
            ),
            "preset": chosen.name,
        }

    weights, applied = overridden_weights(
        chosen,
        {
            schemas.weighted_indicator(argument): value
            for argument, value in overrides.items()
            if argument in schemas.weight_arguments()
        },
    )
    state = analysis()
    try:
        index, evidence = state.engine.index.index(
            state.units, preset=chosen, weights=weights, dataset=TRACT_KEY
        )
    except (KeyError, ValueError) as exc:
        return {**_refusal(exc, "invalid weighting"), "preset": chosen.name}

    frame = state.units.copy()
    frame[Col.VULNERABILITY] = pd.Series(
        index.to_numpy(), index=state.units.index, dtype="Float64"
    )
    rows, without_value, not_shown = _ranking(
        frame,
        by=Col.VULNERABILITY,
        columns=(Col.GEOID, Col.POPULATION, Col.VULNERABILITY, *VULNERABILITY_INDICATORS),
        top_n=top_n,
    )
    return {
        "preset": evidence.preset,
        "weights_origin": evidence.origin,
        "weights_origin_note": chosen.origin_note,
        "weights_origin_url": evidence.origin_url,
        "weights_overridden": _numbers(applied),
        "weights_used": _numbers(evidence.weights),
        "indicators": list(evidence.indicators),
        "directions": dict(evidence.directions),
        "published_per_indicator": dict(evidence.published),
        "missing_per_indicator": dict(evidence.missing),
        "rank_denominator": dict(evidence.rank_denominator),
        "index_min": _scalar(evidence.index_min),
        "index_max": _scalar(evidence.index_max),
        "omitted": _omission(evidence.units, evidence.units_unscored, evidence.unscored_geoids),
        "ranking": rows,
        "ranked_by": Col.VULNERABILITY,
        "units_without_a_value": without_value,
        "units_not_shown": not_shown,
        "note": (
            "each indicator is percentile-ranked against the units that publish it, "
            "the CDC/ATSDR rule, so a unit with no value is not treated as the least "
            "vulnerable -- it is not ranked at all"
        ),
        "warnings": list(evidence.warnings),
    }


@tool
def risk_scenario(
    scenario: str = "",
    preset: str = "",
    top_n: int = schemas.DEFAULT_TOP_N,
    preference: str = "",
    export_map: bool = False,
    **weights: Any,
) -> ToolResult:
    """Hazard, exposure, vulnerability and resilience combined into a priority order.

    With no weight set this is the named weighting's ranking, unchanged and with
    no field added. With weights set it ranks under the weighting `user_weighting`
    builds from them and from the person's sentence, and every origin field says so.
    With `export_map` the same ranking is also written as a map, and only the two
    file paths are added to the result.
    """
    try:
        chosen_scenario = _scenario(scenario)
    except KeyError as exc:
        return {**_refusal(exc, "unknown scenario"), "scenarios": sorted(SCENARIOS)}
    try:
        chosen_preset = _preset(preset)
    except KeyError as exc:
        return {**_refusal(exc, "unknown preset"), "presets": sorted(PRESETS)}
    try:
        user, trace = stated_weighting(chosen_preset, weights, preference)
    except UnstatedPreference as exc:
        return _refusal(exc, "unstated preference")
    except (KeyError, ValueError) as exc:
        return {**_refusal(exc, "invalid weighting"), "preset": chosen_preset.name}
    weighting = user or chosen_preset

    frame, risk, index = scored(chosen_scenario, weighting)
    state = analysis()
    table = state.result.tables[chosen_scenario.name]
    rows, without_value, not_shown = _ranking(
        frame,
        by=Col.RISK_SCORE,
        columns=(
            Col.GEOID,
            Col.PRIORITY_RANK,
            Col.RISK_SCORE,
            Col.POPULATION,
            Col.INUNDATED_FRACTION,
            Col.EXPOSED_POPULATION,
            Col.VULNERABILITY,
            Col.RESILIENCE,
        ),
        top_n=top_n,
    )
    result = {
        "scenario": chosen_scenario.name,
        "surge_height_m": chosen_scenario.surge_height_m,
        "preset": risk.preset,
        "weights_origin": risk.origin,
        "weights_origin_note": weighting.origin_note,
        "weights_origin_url": risk.origin_url,
        "components": list(risk.components),
        "component_weights": _numbers(risk.weights),
        "component_directions": dict(risk.directions),
        "component_published": dict(risk.component_published),
        "rank_denominator": dict(risk.rank_denominator),
        "score_min": _scalar(risk.score_min),
        "score_max": _scalar(risk.score_max),
        "omitted": _omission(risk.units, risk.units_unscored, risk.unscored_geoids),
        "vulnerability_omitted": _omission(
            index.units, index.units_unscored, index.unscored_geoids
        ),
        "ranking": rows,
        "ranked_by": Col.RISK_SCORE,
        "units_without_a_value": without_value,
        "units_not_shown": not_shown,
        "resilience": _numbers(
            {
                "facilities": table.resilience.facilities,
                "radius_m": table.resilience.radius_m,
                "tag_key": table.resilience.tag_key,
                "tag_source": table.resilience.tag_source,
                "units_reaching_none": table.resilience.units_with_none,
                "median_count": table.resilience.median_count,
            }
        ),
        "warnings": list(risk.warnings) + list(index.warnings),
    }
    if user is not None:
        result.update(user_fields(user, chosen_preset, weights, trace))
    if export_map is True:
        try:
            result["map_files"] = export_ranked_map(
                frame,
                state.units,
                state.engine.aligner,
                stem=f"{RISK_TABLE_PREFIX}_{chosen_scenario.name}_{weighting.name}",
                outlined=tuple(str(row[Col.GEOID]) for row in rows),
                title=f"{chosen_scenario.name} under {weighting.name}",
            )
        except Exception as exc:  # noqa: BLE001 -- a failed map must not lose the ranking
            result["map_error"] = _refusal(exc, "map export failed")
    return result


MAP_DIRECTORY = config.OUTPUTS_DIR / "maps"
"""Where `export_map` writes. Git-ignored, like every output that is not paper evidence."""
RISK_TABLE_PREFIX = pipeline.TABLE_PREFIX
MAP_COLUMNS: tuple[str, ...] = (
    Col.GEOID,
    Col.PRIORITY_RANK,
    Col.RISK_SCORE,
    Col.POPULATION,
    Col.INUNDATED_FRACTION,
    Col.EXPOSED_POPULATION,
    Col.VULNERABILITY,
    Col.RESILIENCE,
)


def export_ranked_map(
    frame: pd.DataFrame,
    units: Any,
    aligner: align.Alignment,
    *,
    stem: str,
    outlined: tuple[str, ...],
    title: str,
    directory: Path | None = None,
) -> dict[str, str]:
    """Write a ranking as a GeoPackage and a PNG, and return only their two paths.

    The shapes come from the tract layer the ranking was computed over, joined on
    the shared index, and pass through `to_working_crs` like every other spatial
    step. A frame whose rows are not exactly those tracts, in that order and with
    those GEOIDs, is refused rather than joined by position, which would draw one
    tract's score on another's shape. `outlined` is the GEOIDs the tool returned
    as its ranking, so the map outlines exactly the list the model was given,
    ties included. The paths are the whole return value: a shape never reaches
    the model.
    """
    if not frame.index.equals(units.index) or not frame[Col.GEOID].astype(str).reset_index(
        drop=True
    ).equals(units[Col.GEOID].astype(str).reset_index(drop=True)):
        raise ValueError(
            "the ranking and the tract layer do not hold the same rows in the same "
            "order, so no score can be put on a shape"
        )
    absent = [column for column in MAP_COLUMNS if column not in frame.columns]
    if absent:
        raise KeyError(f"the ranking carries no {absent} to map")
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in stem)
    target = Path(directory) if directory is not None else MAP_DIRECTORY
    target.mkdir(parents=True, exist_ok=True)

    import geopandas as gpd

    shapes = aligner.to_working_crs(units)
    ranked = gpd.GeoDataFrame(
        frame[list(MAP_COLUMNS)].copy(), geometry=shapes.geometry, crs=shapes.crs
    )
    ranked[Col.GEOID] = ranked[Col.GEOID].astype(str)
    for column in MAP_COLUMNS[1:]:
        ranked[column] = pd.to_numeric(ranked[column], errors="coerce").astype(float)
    geopackage = target / f"{safe}.gpkg"
    if geopackage.exists():
        geopackage.unlink()
    ranked.to_file(geopackage, layer=safe, driver="GPKG")

    picture = target / f"{safe}.png"
    _draw_ranked_map(ranked, picture, outlined=outlined, title=title)
    return {
        "geopackage": pipeline.shown_path(geopackage),
        "png": pipeline.shown_path(picture),
        "note": "files only; open them in a GIS or an image viewer",
    }


def _draw_ranked_map(ranked: Any, path: Path, *, outlined: tuple[str, ...], title: str) -> None:
    """Risk score as a choropleth, the returned tracts outlined, unscored hatched."""
    os.environ.setdefault(
        "MPLCONFIGDIR", str(Path(tempfile.gettempdir()) / "geo-agent-matplotlib-cache")
    )
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    figure, axis = plt.subplots(figsize=(6.0, 6.0))
    try:
        ranked.plot(
            column=Col.RISK_SCORE,
            ax=axis,
            cmap="cividis",
            vmin=0.0,
            vmax=1.0,
            edgecolor="white",
            linewidth=0.2,
            legend=True,
            legend_kwds={"label": "risk score (county-relative, 0-1)", "shrink": 0.6},
            missing_kwds={"color": "#BDBDBD", "hatch": "///", "label": "unscored"},
        )
        top = ranked[ranked[Col.GEOID].isin(set(outlined))]
        if len(top):
            top.plot(ax=axis, facecolor="none", edgecolor="#D55E00", linewidth=1.2)
        axis.set_title(f"{title}: the {len(top)} tracts listed are outlined", fontsize=10)
        axis.set_axis_off()
        figure.savefig(path, format="png", dpi=150, bbox_inches="tight")
    finally:
        plt.close(figure)


@tool
def compare_scenarios(
    scenario: str = "",
    priority_units: int = DEFAULT_PRIORITY_UNITS,
    preset: str = "",
    preference: str = "",
    **weights: Any,
) -> ToolResult:
    """Every weighting against one scenario, including who each one drops.

    `units=` is passed and is load-bearing. Without it the comparison recombines
    only the objective half of each weighting, and two presets that differ only in
    their indicator weights return byte-identical rows while the table presents
    them as compared. That was a real defect in this project, found by reading
    rather than by a check, and `_tradeoff_checks` now asserts the difference it
    should produce.

    With weights set, the person's weighting joins the comparison as one more row,
    and `pairs_against` sets it beside each named weighting in turn. With none set
    the comparison is the named weightings alone, unchanged and with no field added.
    """
    try:
        chosen = _scenario(scenario)
    except KeyError as exc:
        return {**_refusal(exc, "unknown scenario"), "scenarios": sorted(SCENARIOS)}
    try:
        base = _preset(preset)
    except KeyError as exc:
        return {**_refusal(exc, "unknown preset"), "presets": sorted(PRESETS)}
    try:
        user, trace = stated_weighting(base, weights, preference)
    except UnstatedPreference as exc:
        return _refusal(exc, "unstated preference")
    except (KeyError, ValueError) as exc:
        return {**_refusal(exc, "invalid weighting"), "preset": base.name}
    compared = WEIGHT_PRESETS if user is None else (user, *WEIGHT_PRESETS)
    by_name = {item.name: item for item in compared}

    state = analysis()
    table = state.result.tables[chosen.name]
    try:
        rows = state.engine.compare_presets(
            table.frame,
            scenario=chosen,
            units=state.units,
            presets=compared,
            priority_units=priority_units,
        )
    except ValueError as exc:
        return _refusal(exc, "invalid comparison")

    result = {
        "scenario": chosen.name,
        "surge_height_m": chosen.surge_height_m,
        "priority_units": priority_units,
        "weightings_compared": len(rows),
        "rows": [
            {
                "preset": row.preset,
                "origin": by_name[row.preset].origin,
                "origin_url": by_name[row.preset].origin_url,
                "top_geoids": _capped(row.top_geoids),
                "population_in_priority": row.population_in_priority,
                "vulnerable_population_in_priority": row.vulnerable_population_in_priority,
                "mean_inundation_m": _scalar(row.mean_inundation_m),
                "displaced_geoids": _capped(row.displaced_geoids),
                "displaced_count": len(row.displaced_geoids),
            }
            for row in rows
        ],
        "vulnerable_population_means": (
            f"vulnerable_population_in_priority is the {vulnerable_label()}. Call it "
            "that, not the vulnerable population: the cut is a point on this county's "
            "own distribution, so a share of tracts qualifies by construction"
        ),
        "who_loses": (
            "displaced_geoids names the units that ANOTHER weighting in this "
            "comparison prioritises and this one does not -- the people whose "
            "priority depends on a value judgement rather than on the data"
        ),
        "note": (
            "both halves of each weighting vary here: the index is recomputed per "
            "weighting from the indicator weights, then re-ranked under the "
            "objective weights. Comparing only the second half would report three "
            "answers where two of them were the same answer."
        ),
    }
    if user is not None:
        result.update(user_fields(user, base, weights, trace))
        for row in result["rows"]:
            if row["preset"] == user.name:
                row["answered_by"] = trace["answered_by"]
        result["user_against_each_weighting"] = pairs_against(rows, user.name)
        result["pairs_note"] = (
            f"each pair sets the '{user.name}' weighting beside one named weighting. "
            "dropped names the units the named weighting prioritises and the person's "
            "does not -- who loses if their words decide -- and added names the units "
            "their words bring in instead"
        )
    return result


ELICIT = True
"""Whether `ask_user_preferences` may block on `input()`.

A second guard beside `isatty`, and it exists because of where the tool is
called. `SAMPLE_ARGUMENTS` runs every tool in the check suite, and a check suite
run from a real terminal would sit on `input()` forever -- green when nobody is
watching and hung when somebody is. `_self_check` closes the channel around
`_every_result` and reopens it for the one check that drives the real prompt with
a scripted reader, so the interactive path is exercised rather than skipped."""

PROMPT_LABEL = "[ask_user_preferences]"
NO_CHANNEL = "this run has no interactive channel, so nobody was asked"

SCRIPTED_REPLIES_ENV = "OASIS_SCRIPTED_REPLIES"
"""The environment variable a harness sets to answer `ask_user_preferences`.

It names a JSON file holding a list of reply strings, taken in order, one per
`ask_user_preferences` call within a question. The cursor restarts at the first
reply on each new question, so a harness running several questions points this at
one file per question. A result answered this way says `elicited="scripted"` and
`channel="script"`, so no harness result reads as a person's answer. Read at call
time rather than at import, so a harness can point each run at its own file."""

SCRIPTED = "scripted"
"""The value `elicited` takes when a script answered: truthy, and never `True`."""

_SCRIPT_POSITION = 0
"""How many scripted replies this question has used. `forget_calls` resets it."""


def interactive_channel() -> bool:
    """Is there a person at a terminal to put the choice to?

    Both guards are needed and neither is redundant. `isatty` is false in a batch
    run, under `mutate.py` and in CI, which is the case that would hang. `ELICIT`
    is what lets a check suite running in a real terminal stay non-interactive
    without lying about the channel.
    """
    if not ELICIT:
        return False
    try:
        return bool(sys.stdin is not None and sys.stdin.isatty())
    except (AttributeError, ValueError, OSError):
        return False


def scripted_replies() -> list[str] | None:
    """The replies a harness scripted for this process, or None when none is named.

    A file that is missing or holds anything but a list of strings raises, and
    `ask_user_preferences` turns that into a refusal. A harness pointed at a
    broken script should fail loudly on its first question, not run every
    question on the default as though nobody had been scripted.
    """
    named = os.environ.get(SCRIPTED_REPLIES_ENV, "").strip()
    if not named:
        return None
    loaded = json.loads(Path(named).read_text(encoding="utf-8-sig"))
    if not isinstance(loaded, list) or not all(isinstance(item, str) for item in loaded):
        raise ValueError(
            f"{SCRIPTED_REPLIES_ENV} names {named}, which holds "
            f"{type(loaded).__name__}; it must hold a JSON list of reply strings, one "
            "per ask_user_preferences call within a question, restarting at the first "
            "reply on each new question"
        )
    return loaded


def next_scripted_reply(replies: list[str]) -> str | None:
    """The next unused scripted reply on this question, or None when they ran out."""
    global _SCRIPT_POSITION
    if _SCRIPT_POSITION >= len(replies):
        return None
    reply = replies[_SCRIPT_POSITION]
    _SCRIPT_POSITION += 1
    return reply


def read_reply(
    reply: str, options: list[str], default: str, who: str
) -> tuple[str, str, str, bool]:
    """What one reply means: (weighting chosen, words stated, how, whether it was used).

    A menu pick, by number or by name, chooses that weighting. An empty reply
    accepts the default. Anything else is the person's own words, returned
    unchanged as the second value for the model to map onto weights.

    The last value is False when the reply could not be used and the default was
    imposed rather than chosen: a number off the menu, or words `coordinate_refusal`
    withholds. S10's coordinate guard once leaked through its own refusal message
    by quoting what it refused, so free text that is quoted back is held to the
    widest rule this project has -- and the result says the default was imposed,
    never that the person chose it.
    """
    text = reply.strip()
    if not text:
        return default, "", f"{who} accepted the default", True
    if text.isdigit():
        if 1 <= int(text) <= len(options):
            return options[int(text) - 1], "", f"{who} chose by number", True
        return default, "", f"{who} gave a number off the menu, so the default stands", False
    for name in options:
        if text.lower() == name.lower():
            return name, "", f"{who} chose by name", True
    refusal = coordinate_refusal(text)
    if refusal:
        return (
            default,
            "",
            f"{who} answered in plain words, but {refusal}; they are withheld and "
            "the default stands",
            False,
        )
    return (
        schemas.USER_WEIGHTING,
        text,
        f"{who} answered in plain words, not a menu pick",
        True,
    )


RETRY_HINT = (
    "Try once more: pick a number, press enter for the default, or say it in words "
    "with each number labelled and no location, for example 'age 0.6, no car 0.4'."
)


def elicit(question: str, options: list[str], default: str) -> tuple[str, str, str, bool]:
    """Put the choice to the person and read what they say back.

    A menu pick comes back as a name. Anything else they type comes back word for
    word as the second value, because a person's own words are the preference and
    the menu is only a shortcut to one. `read_reply` decides which.

    A reply that cannot be used is named at the prompt and asked once more, so
    the person learns their words were not used instead of finding the default in
    their place. Once, not until it works: a prompt that loops is a hung harness.
    """
    print()
    print(f"{PROMPT_LABEL} {question}")
    for position, name in enumerate(options, start=1):
        print(f"  {position}. {name}")
    print(f"  press enter for the default ({default}), or say what matters most in your own words")
    for attempt in (1, 2):
        try:
            reply = input("  weighting> ").strip()
        except (EOFError, KeyboardInterrupt, OSError):
            return default, "", "the prompt was closed without a reply, so the default stands", False
        chosen, stated, how, used = read_reply(reply, options, default, "the person was asked and")
        if used:
            return chosen, stated, how, used
        print(f"  that reply was not used: {how}.")
        if attempt == 1:
            print(f"  {RETRY_HINT}")
    return chosen, stated, how, used


def answer_note(elicited: bool | str, channel: str, stated: str, used: bool) -> str:
    """One sentence saying who answered, and what the model should do with it.

    `used` is False when a reply came back but could not be used, so the default
    was imposed. That is said in as many words: a default nobody chose is never
    reported as somebody's choice.
    """
    words = (
        " Their words are stated_preference: map them onto weight_* arguments and "
        "pass them unchanged as preference to risk_scenario and compare_scenarios."
    )
    imposed = (
        " The default stands and is not a preference anybody expressed."
    )
    if elicited is True:
        if not used:
            return "a person at a terminal was asked, but no usable reply came back." + imposed
        if stated:
            return "a person at a terminal answered this in their own words." + words
        return (
            "a person at a terminal answered this and the county was re-scored under "
            "what they chose"
        )
    if elicited == SCRIPTED:
        lead = (
            f"a scripted reply from the file named by {SCRIPTED_REPLIES_ENV} answered "
            "this, not a person."
        )
        if not used:
            return lead + " Its reply could not be used." + imposed
        if stated:
            return lead + words.replace("Their", "Its")
        return lead + " The county was re-scored under what it chose."
    if channel == "script":
        return (
            "the script held no reply for this question, so nobody answered. The "
            "choice is the default, not a preference somebody expressed."
        )
    return f"{NO_CHANNEL}. The choice is the default, not a preference somebody expressed."


def weighting_effect(name: str) -> dict[str, Any]:
    """What the chosen weighting does to the ranking, computed rather than promised.

    This is the half of a preference that scores. A tool that recorded a choice
    and returned a menu would have elicited a preference and left it in the
    transcript; this runs the choice through the same `scored` call
    `risk_scenario` makes, on the same shared analysis, so the weighting reaches
    the score rather than the prose. Reported beside it: which units another
    weighting would have prioritised and this one drops, which is who loses.
    """
    scenario = DEFAULT_SCENARIO
    chosen = _preset(name)
    frame, risk, _ = scored(scenario, chosen)
    rows, _, _ = _ranking(
        frame,
        by=Col.RISK_SCORE,
        columns=(Col.GEOID, Col.PRIORITY_RANK, Col.RISK_SCORE),
        top_n=DEFAULT_PRIORITY_UNITS,
    )
    mine = [str(row[Col.GEOID]) for row in rows]
    dropped: dict[str, Any] = {}
    for other in WEIGHT_PRESETS:
        if other.name == chosen.name:
            continue
        other_frame, _, _ = scored(scenario, other)
        other_rows, _, _ = _ranking(
            other_frame, by=Col.RISK_SCORE, columns=(Col.GEOID,), top_n=DEFAULT_PRIORITY_UNITS
        )
        theirs = [str(row[Col.GEOID]) for row in other_rows]
        dropped[other.name] = _capped([unit for unit in theirs if unit not in mine])
    return {
        "recomputed": True,
        "scenario": scenario.name,
        "priority_units": DEFAULT_PRIORITY_UNITS,
        "ranking": rows,
        "weights_used": _numbers(risk.weights),
        "weights_origin": risk.origin,
        "dropped_against_each_other_weighting": dropped,
        "note": (
            "these units were re-scored under the weighting named above, through the "
            "same call risk_scenario makes. Pass that name as the preset argument to "
            "risk_scenario to work with the full table."
        ),
    }


@tool
def ask_user_preferences(question: str) -> ToolResult:
    """Put a weighting choice to the human, and score the county under their answer.

    Three channels, tried in this order. A harness that names a script in
    `SCRIPTED_REPLIES_ENV` is answered from it, and the result says
    `elicited="scripted"`. A person at a terminal is asked for real. With neither,
    nobody is asked, because a tool that blocked on `input()` would hang every
    harness that runs the agent without one -- `mutate.py` among them, which still
    has no timeout of its own. `elicited` tells the truth in every case: `True`
    only when a person answered, never merely because a choice was made.

    The person may pick from the menu or answer in their own words. Their words
    come back unchanged as `stated_preference`, and mapping them onto weights is
    the model's job, done in the open as `weight_*` arguments on the risk tools.

    What it does NOT do is invent an answer and present it as the human's. The
    fallback is the default, which is a published weighting with a citable origin
    rather than this system's opinion, and the result says so.
    """
    names = [item.name for item in WEIGHT_PRESETS]
    try:
        script = scripted_replies()
    except (OSError, ValueError) as exc:
        return {**_refusal(exc, "scripted replies unreadable"), "variable": SCRIPTED_REPLIES_ENV}

    asked: bool | str
    if script is not None:
        channel = "script"
        reply = next_scripted_reply(script)
        if reply is None:
            asked = False
            chosen, stated, how, used = (
                DEFAULT_PRESET.name,
                "",
                "the script held no reply for this question, so the default stands",
                False,
            )
        else:
            asked = SCRIPTED
            chosen, stated, how, used = read_reply(
                reply, names, DEFAULT_PRESET.name, "the scripted reply"
            )
    elif interactive_channel():
        channel, asked = "terminal", True
        chosen, stated, how, used = elicit(question, names, DEFAULT_PRESET.name)
    else:
        channel, asked = "none", False
        chosen, stated, how, used = DEFAULT_PRESET.name, "", NO_CHANNEL, False

    if stated:
        effect: dict[str, Any] = {
            "recomputed": False,
            "why": (
                "a sentence is not a weighting until it is mapped onto weights. Set "
                "the weight_* arguments its words name on risk_scenario and "
                "compare_scenarios, and pass it unchanged as preference; those tools "
                "score the county under it"
            ),
        }
    else:
        try:
            effect = weighting_effect(chosen)
        except Exception as exc:
            effect = {"recomputed": False, **_refusal(exc, "could not score under this weighting")}
    return {
        "question": question,
        "elicited": asked,
        "channel": channel,
        "chosen": chosen,
        "stated_preference": stated,
        "how_it_was_chosen": how,
        "why": answer_note(asked, channel, stated, used),
        "effect": effect,
        "default": DEFAULT_PRESET.name,
        "options": [
            {
                "name": item.name,
                "origin": item.origin,
                "origin_note": item.origin_note,
                "origin_url": item.origin_url,
                "weights": _numbers(item.weights),
            }
            for item in WEIGHT_PRESETS
        ],
        "how_to_use": (
            "pass one of these names as the preset argument to risk_scenario or "
            "vulnerability_index, or override an individual indicator weight "
            f"directly. When chosen is '{schemas.USER_WEIGHTING}' there is no name to "
            "pass: stated_preference holds the person's words instead, so set the "
            "weight_* arguments those words name on risk_scenario and "
            "compare_scenarios, leave every other weight unset, and pass the words "
            "unchanged as preference. Report which weighting you used and where it "
            "came from."
        ),
        "note": (
            "two of these come from one published index by two of its own published "
            "rules and differ only in how they weight the indicators; the third is "
            "the authors' judgement and says so. Call compare_scenarios to see who "
            "each choice drops."
        ),
    }


@tool
def run_spatial_code(code: str, timeout_s: float = 60.0) -> ToolResult:
    """Execute model-written Python against the cleaned layers."""
    module = _backing("run_spatial_code")
    if isinstance(module, dict):
        return module
    run = module.Sandbox().run(code, timeout_s=timeout_s)
    return {
        "exit_code": run.exit_code,
        "stdout": run.stdout,
        "stderr": run.stderr,
        "duration_s": run.duration_s,
        "error_type": run.error_type,
    }


@tool
def validate_answer(answer: str) -> ToolResult:
    """Trace every number in a draft answer back to a logged tool result."""
    module = _backing("validate_answer")
    if isinstance(module, dict):
        return module
    report = module.Critic().check(answer, logged_calls(), 1)
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
# retrieval dispatch and the pending probe
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Retriever:
    """One retrieval call and the dataset names it produces."""

    datasets: tuple[str, ...]
    call: Callable[..., Any]
    needs_extent: bool


def _retrievers() -> tuple[Retriever, ...]:
    """Which live retrieval produces which registered dataset.

    The names come from `acquire`'s own constants, so a dataset renamed there is
    renamed here. `needs_extent` marks the calls that take the study extent, which
    is derived from the retrieved boundary layer at run time and never written
    down -- invariant 5.
    """
    return (
        Retriever(
            (acquire.DATASET_TRACTS, acquire.DATASET_BLOCK_GROUPS),
            acquire.acquire_boundaries,
            False,
        ),
        Retriever(
            (acquire.DATASET_ACS, acquire.DATASET_ACS_BLOCK_GROUPS),
            acquire.acquire_acs,
            False,
        ),
        Retriever((acquire.DATASET_ELEVATION,), acquire.acquire_elevation, True),
        Retriever((acquire.DATASET_FACILITIES,), acquire.acquire_facilities, True),
        Retriever((acquire.DATASET_FLOOD_ZONES,), acquire.acquire_flood_zones, True),
    )


def module_present(name: str) -> bool:
    """Is `src/<name>.py` importable in this checkout?"""
    try:
        return importlib.util.find_spec(f"{__package__}.{name}") is not None
    except (ImportError, ValueError):
        return False


def pending_tools() -> tuple[str, ...]:
    """Advertised tools whose backing module has not been built yet.

    Probed, never listed. The day `src/sandbox.py` exists, `run_spatial_code`
    drops off this list with no edit anywhere -- which is the property a written
    list cannot have and the reason the S8 review found five assertions frozen to
    one county's tract count.
    """
    return tuple(
        name
        for name, module in BACKING_MODULES.items()
        if not module_present(module)
    )


def _backing(name: str) -> Any:
    """The module that runs this tool, or the refusal to hand back instead."""
    module = BACKING_MODULES[name]
    if not module_present(module):
        return {
            "error": "tool_unavailable",
            "detail": (
                f"{name} is backed by src/{module}.py, which does not exist in this "
                "checkout, so the capability is not built rather than broken"
            ),
            "backing_module": f"src/{module}.py",
            "advertised_as_pending": name in pending_tools(),
        }
    return importlib.import_module(f"{__package__}.{module}")


def tool_specs() -> list[dict[str, Any]]:
    """The specs the agent sends, with any pending tool marked as such."""
    return schemas.build_tool_specs(pending=pending_tools())


def faults_between(
    advertised: set[str], executable: set[str], frozen: set[str]
) -> list[str]:
    """The three disagreements, taken over sets rather than over module state.

    Separated from `surface_faults` so the guard can be exercised on sets that
    disagree. On a wired-up surface the real sets are equal, so a mutation that
    stops one of these branches firing produces the same empty list and reads as
    caught by nothing -- which is the shape of a guard that cannot fail. The
    fixtures in `_fault_checks` are the only thing that reaches these branches.
    """
    faults: list[str] = []
    unrunnable = sorted(advertised - executable)
    if unrunnable:
        faults.append(
            f"{len(unrunnable)} tool(s) are offered to the model and cannot be executed: "
            f"{unrunnable}. TOOL_FUNCTIONS holds {sorted(executable) or 'nothing'}"
        )
    undeclared = sorted(advertised - frozen)
    if undeclared:
        faults.append(
            f"{len(undeclared)} advertised tool(s) are not in contracts.TOOL_NAMES: "
            f"{undeclared}. The frozen surface is {list(TOOL_NAMES)}"
        )
    unreachable = sorted(executable - advertised)
    if unreachable:
        faults.append(
            f"{len(unreachable)} implemented tool(s) are never advertised, so the model "
            f"cannot call them: {unreachable}"
        )
    return faults


def surface_faults() -> list[str]:
    """Every way the advertised tool surface and the executable one disagree.

    Three separate faults, because they fail differently. A name the model is
    offered but nothing can run wastes a turn and returns an error the model then
    has to reason about. A name outside `contracts.TOOL_NAMES` is drift from the
    frozen contract. A name that is implemented and never advertised is dead code
    the model cannot reach.

    Deliberately blind to `pending_tools()`. A pending tool is advertised,
    executable and frozen -- it runs, and what it returns is a refusal naming the
    module that is missing. Folding "pending" in here would make this guard go
    quiet for a state it was not written to describe, which is how a guard stops
    being able to fail.
    """
    return faults_between(
        set(schemas.TOOL_ARG_MODELS), set(TOOL_FUNCTIONS), set(TOOL_NAMES)
    )


def format_surface() -> str:
    """The tool surface, printed, for the acceptance gate to paste."""
    pending = pending_tools()
    lines = [
        f"TOOL SURFACE -- {len(TOOL_FUNCTIONS)} tool(s), "
        f"{len(pending)} pending a module that is not built yet",
        "",
    ]
    for name in TOOL_NAMES:
        model = schemas.TOOL_ARG_MODELS[name]
        arguments = ", ".join(model.model_fields) or "(none)"
        mark = "PENDING" if name in pending else "ready  "
        lines.append(f"  [{mark}] {name}")
        lines.append(f"            args: {arguments}")
    lines.append("")
    lines.append(f"  faults: {surface_faults() or 'none'}")
    if pending:
        for name in pending:
            lines.append(f"  {name} is backed by src/{BACKING_MODULES[name]}.py, not built")
    return "\n".join(lines)


def main() -> int:
    print(format_surface())
    return 0


# ---------------------------------------------------------------------------
# self check
# ---------------------------------------------------------------------------


SAMPLE_ARGUMENTS: dict[str, dict[str, Any]] = {
    "list_datasets": {},
    "acquire_dataset": {"name": "not-a-dataset"},
    "describe_layer": {"name": acquire.DATASET_TRACTS},
    "describe_alignment": {},
    "hazard_exposure": {},
    "vulnerability_index": {},
    "risk_scenario": {},
    "compare_scenarios": {},
    "ask_user_preferences": {"question": "which weighting should decide priority?"},
    "run_spatial_code": {"code": "print(1)"},
    "validate_answer": {"answer": "a draft"},
}
"""Arguments to call each tool with in the checks.

`acquire_dataset` is called with a name nothing retrieves, so the checks perform
no network request: the real endpoints are exercised by `acquire.py --check`,
which is the module that owns them, and by `python -m src.demo`. A tool added
without an entry here fails the check rather than going unexercised."""

SAMPLE_PREFERENCE = "older residents and households without a car matter most"
"""The sentence the checks map onto weights by hand. Whether a model maps it the
same way is a question for a live run with this as its scripted reply, and only
that run's log can answer it -- these checks cannot."""

PINNED_REPLY_COORDINATE = "-10.123456, 20.654321"
"""A coordinate pair a reply could carry. Placed nowhere near any study area on
purpose: what is under test is the shape, not a place."""


def _every_result() -> dict[str, ToolResult]:
    """Call every registered tool once, enumerated from the registry itself."""
    return {
        name: TOOL_FUNCTIONS[name](**SAMPLE_ARGUMENTS[name])
        for name in sorted(TOOL_FUNCTIONS)
    }


def _surface_checks(results: dict[str, ToolResult]) -> list[tuple[str, bool]]:
    """The three things `surface_faults` watches, plus the pending probe."""
    faults = surface_faults()
    for line in faults:
        print(f"  surface fault: {line}")
    pending = pending_tools()
    print(f"  {len(TOOL_FUNCTIONS)} executable, {len(schemas.TOOL_ARG_MODELS)} advertised, "
          f"{len(TOOL_NAMES)} frozen; pending {list(pending)}")
    print(f"  probe: a module that exists reads present, one that does not reads absent")

    specs = tool_specs()
    marked = {
        spec["function"]["name"]
        for spec in specs
        if schemas.PENDING_SUFFIX in spec["function"]["description"]
    }
    return [
        ("the advertised surface and the executable one agree", not faults),
        ("every frozen tool name is executable", set(TOOL_FUNCTIONS) == set(TOOL_NAMES)),
        ("every tool was covered by the checks, none skipped for lack of arguments",
         set(SAMPLE_ARGUMENTS) == set(TOOL_FUNCTIONS) and set(results) == set(TOOL_NAMES)),
        ("the pending probe finds a module that exists",
         module_present("pipeline") and module_present("schemas")),
        ("the pending probe reports a module that does not exist as absent",
         not module_present("a_module_nobody_wrote")),
        ("pending is derived from the probe, not from a list written down",
         set(pending) == {
             name for name, module in BACKING_MODULES.items() if not module_present(module)
         }),
        ("the specs the agent sends mark exactly the pending tools", marked == set(pending)),
        ("every implemented tool is a callable the loop can reach",
         all(callable(TOOL_FUNCTIONS[name]) for name in TOOL_NAMES)),
    ]


def _serialisation_checks(results: dict[str, ToolResult]) -> list[tuple[str, bool]]:
    """Invariant 3, on every tool's real result, enumerated from the registry."""
    faults: dict[str, list[str]] = {}
    sizes: dict[str, int] = {}
    for name, result in results.items():
        payload = json.dumps(result, default=str)
        sizes[name] = len(payload)
        found = coordinate_faults(as_sent(result))
        if found:
            faults[name] = found
    for name in sorted(sizes):
        print(f"  {name:<22} {sizes[name]:>7,} bytes serialised  "
              f"{len(faults.get(name, [])) or 'no'} coordinate fault(s)")
    for name, found in faults.items():
        for line in found[:3]:
            print(f"    {name}: {line}")

    return [
        ("every tool returns a dict", all(isinstance(item, dict) for item in results.values())),
        ("every result serialises to JSON", len(sizes) == len(results)),
        ("no serialised result carries a coordinate, by key, by value or by shape",
         not faults),
        ("the coordinate scan had results to scan", len(results) == len(TOOL_NAMES)),
        ("no result is large enough to crowd the run's remaining turns",
         max(sizes.values()) < 24_000),
    ]


def _coordinate_guard_checks() -> list[tuple[str, bool]]:
    """That the guard can fail, on each of the three shapes it refuses.

    This study area's tract layer really does carry coordinate columns, so the
    withholding is falsifiable on real data as well -- but the synthetic payloads
    are what prove each branch fires, because a county whose boundary layer
    happened to ship none would leave every branch untested.
    """
    by_key = {"INTPTLAT": "+32.7"}
    by_value = {"columns": ["GEOID", "CENTLON"]}
    by_shape = {"outline": [1.0, 2.0]}
    by_nesting = {"outline": [[1.0, 2.0]]}
    clean = {
        "scenario": "surge_3_0m",
        "rows": [{"geoid": "01001020100", "population": 1}],
        "count": 2,
    }

    by_prose = {
        "note": (
            "bbox converted from (min_lon, min_lat, max_lon, max_lat) "
            "(-80.45355300027266, 32.48256499980578) to south,west order"
        )
    }

    found = registry()
    frame = found.load(acquire.DATASET_TRACTS)
    kept, withheld = reportable_columns(frame.columns)
    described = describe_layer(name=acquire.DATASET_TRACTS)

    # Every registered layer, enumerated from the registry rather than named here.
    # The version of this check that named one layer passed while a second layer
    # was sending the study bounding box to the model in a provenance note: the
    # scan is only as wide as the set it is pointed at.
    every = {name: describe_layer(name=name) for name in found.names()}
    leaking = {
        name: coordinate_faults(as_sent(result))
        for name, result in every.items()
        if coordinate_faults(as_sent(result))
    }
    hidden = {
        name: result.get("notes_withheld", 0)
        for name, result in every.items()
        if result.get("notes_withheld", 0)
    }
    print(f"  {acquire.DATASET_TRACTS}: {len(kept)} column(s) reportable, "
          f"{len(withheld)} withheld -> {withheld}")
    print(f"  described all {len(every)} registered layer(s); "
          f"coordinate faults {leaking or 'none'}")
    print(f"  retrieval notes withheld for carrying a coordinate: {hidden or 'none'}")

    return [
        ("a key that names a coordinate is refused", bool(coordinate_faults(by_key))),
        ("a column name that names a coordinate, inside a value, is refused",
         bool(coordinate_faults(by_value))),
        ("a list of numbers is refused whatever it is called",
         bool(coordinate_faults(by_shape))),
        ("a list of lists is refused whatever it is called",
         bool(coordinate_faults(by_nesting))),
        ("a coordinate spelled out inside a sentence is refused",
         bool(coordinate_faults(by_prose))),
        ("a clean result raises nothing, so the guard is not refusing everything",
         not coordinate_faults(clean)),
        ("prose is not mistaken for a column name",
         not coordinate_faults({"note": "the x and y of the matter, longitude aside"})),
        ("ordinary numbers in prose are not mistaken for a coordinate",
         not coordinate_faults(
             {"note": "99,037 exposed of 420,264 at 3.0 m; error 0.0 pct over 261 units"}
         )),
        ("this study area's boundary layer really does carry coordinate columns",
         len(withheld) > 0),
        ("describe_layer withholds them and says how many",
         described["columns_withheld"] == len(withheld)
         and not any(item["name"] in withheld for item in described["columns"])),
        ("describe_layer still reports the columns that are not coordinates",
         len(described["columns"]) > 0 and Col.GEOID in {item["name"] for item in described["columns"]}),
        ("every registered layer was described, not one chosen by hand",
         set(every) == set(found.names()) and len(every) > 1),
        ("no layer's description carries a coordinate, by key, value, shape or prose",
         not leaking),
        ("a withheld note is counted rather than dropped in silence",
         all(
             every[name]["notes"]["total"] + every[name]["notes_withheld"]
             == len(found.record(name).provenance.notes)
             for name in every
         )),
    ]


def _agreement_checks() -> list[tuple[str, bool]]:
    """That the tool route and the pipeline route are one route.

    Every number a tool reports has to be the number the pipeline computed, or
    there are two answers to one question and the critic's traceability check is
    tracing to the wrong one. These compare the tools' output against
    `PipelineResult` on the real county rather than against a fixture, because the
    thing at risk is agreement on real data.
    """
    state = analysis()
    scenario = DEFAULT_SCENARIO
    table = state.result.tables[scenario.name]

    from_tool = risk_scenario(scenario=scenario.name, preset=DEFAULT_PRESET.name, top_n=10)
    tool_geoids = [row[Col.GEOID] for row in from_tool["ranking"]]
    pipeline_geoids = (
        table.frame.dropna(subset=[Col.RISK_SCORE])
        .sort_values(Col.RISK_SCORE, ascending=False)[Col.GEOID]
        .head(10)
        .tolist()
    )

    exposure = hazard_exposure(scenario=scenario.name)
    totals = exposure["county_totals"]

    compared = compare_scenarios(scenario=scenario.name, priority_units=DEFAULT_PRIORITY_UNITS)
    tool_rows = {row["preset"]: row for row in compared["rows"]}
    pipeline_rows = {
        row.preset: row for row in state.result.tradeoff if row.scenario == scenario.name
    }

    print(f"  shared run: {state.seconds:.1f}s for {len(state.result.tables)} scenario(s), "
          f"built {state.built_at}")
    print(f"  {scenario.name}: tool top-10 == pipeline top-10: {tool_geoids == pipeline_geoids}")
    print(f"  exposed population reported {totals['exposed_population']:,.0f} "
          f"against the tract-uniform estimate {totals['exposed_population_coarse_estimate']:,.0f}")

    return [
        ("the tool's priority order under the pipeline's own weighting is the pipeline's",
         tool_geoids == pipeline_geoids and len(tool_geoids) == 10),
        ("the exposed population reported is the evidence's, not a recomputation",
         totals["exposed_population"] == round(table.exposure.fine_total, 6)),
        ("the coarse estimate reported is the evidence's too",
         totals["exposed_population_coarse_estimate"] == round(table.exposure.coarse_total, 6)),
        ("the population denominator reported is the evidence's",
         totals["population"] == round(table.exposure.population_total, 6)),
        ("the trade-off rows are the pipeline's rows, weighting by weighting",
         set(tool_rows) == set(pipeline_rows)
         and all(
             tool_rows[name]["top_geoids"]["listed"] == list(pipeline_rows[name].top_geoids)
             and tool_rows[name]["displaced_geoids"]["listed"]
             == list(pipeline_rows[name].displaced_geoids)[:MAX_LIST]
             and tool_rows[name]["population_in_priority"]
             == pipeline_rows[name].population_in_priority
             for name in tool_rows
         )
         and len(tool_rows) > 1),
        ("the comparison covered every weighting the project defines",
         compared["weightings_compared"] == len(WEIGHT_PRESETS)),
    ]


def _tradeoff_checks() -> list[tuple[str, bool]]:
    """That both halves of a weighting reach the comparison.

    Asserted as a difference that must EXIST. `svi_equal` and `svi_themes` carry
    identical objective weights and differ only in how they weight the indicators,
    so if the `units=` argument were dropped they would return the same rows and
    this would fail. The same property asserted the other way round -- that the
    presets agree -- would pass without the argument and prove nothing.
    """
    scenario = DEFAULT_SCENARIO
    compared = compare_scenarios(scenario=scenario.name)
    rows = {row["preset"]: row for row in compared["rows"]}
    same_objective = [
        (a.name, b.name)
        for i, a in enumerate(WEIGHT_PRESETS)
        for b in WEIGHT_PRESETS[i + 1:]
        if {k: a.weights[k] for k in vulnerability.OBJECTIVE_TERMS}
        == {k: b.weights[k] for k in vulnerability.OBJECTIVE_TERMS}
        and {k: a.weights[k] for k in VULNERABILITY_INDICATORS}
        != {k: b.weights[k] for k in VULNERABILITY_INDICATORS}
    ]
    differing = [
        (a, b)
        for a, b in same_objective
        if rows[a]["top_geoids"]["listed"] != rows[b]["top_geoids"]["listed"]
    ]
    displaced = sum(row["displaced_count"] for row in rows.values())
    print(f"  preset pairs differing ONLY in indicator weights: {same_objective}")
    print(f"  of those, pairs whose priority lists differ here: {differing}")
    print(f"  displacements recorded across the comparison: {displaced}")

    return [
        ("a preset pair differing only in indicator weights exists to test with",
         len(same_objective) >= 1),
        ("that pair ranks differently, which only happens if the index was recomputed",
         len(differing) == len(same_objective)),
        ("the comparison names units each weighting drops rather than reporting none",
         displaced > 0),
        ("every weighting reports where it came from",
         all(row["origin"] for row in rows.values())),
    ]


def _weighting_checks() -> list[tuple[str, bool]]:
    """That a weight argument is a parameter and not decoration."""
    default = vulnerability_index()
    heavy = vulnerability_index(**{
        schemas.weight_argument(VULNERABILITY_INDICATORS[0]): 10.0
    })
    both = [
        [row[Col.GEOID] for row in default["ranking"]],
        [row[Col.GEOID] for row in heavy["ranking"]],
    ]
    named = vulnerability_index(preset=WEIGHT_PRESETS[1].name)

    # Two presets, chosen for what they hold constant. `indicator_only` differs
    # from the default in its indicator weights alone, so the score can only move
    # if the index was recomputed under it -- the objective weights are identical
    # and would produce the same ranking on their own. `objective_too` also moves
    # the objective terms. Comparing the default against one preset would let
    # either half of a weighting explain any difference, which is how a comparison
    # of three weightings once reported two that had not been varied.
    terms = {name: DEFAULT_PRESET.weights[name] for name in vulnerability.OBJECTIVE_TERMS}
    indicator_only = [
        item
        for item in WEIGHT_PRESETS
        if item.name != DEFAULT_PRESET.name
        and {name: item.weights[name] for name in vulnerability.OBJECTIVE_TERMS} == terms
    ][0]
    objective_too = [
        item
        for item in WEIGHT_PRESETS
        if {name: item.weights[name] for name in vulnerability.OBJECTIVE_TERMS} != terms
    ][0]
    risk_default = risk_scenario(preset=DEFAULT_PRESET.name)
    risk_indicator = risk_scenario(preset=indicator_only.name)
    risk_objective = risk_scenario(preset=objective_too.name)
    print(f"  overriding {VULNERABILITY_INDICATORS[0]} moved "
          f"{len(set(both[0]) ^ set(both[1]))} unit(s) in or out of the top "
          f"{len(both[0])}")
    print(f"  risk under {DEFAULT_PRESET.name} against {indicator_only.name} "
          f"(indicator weights only): rankings differ "
          f"{risk_default['ranking'] != risk_indicator['ranking']}")
    print(f"  against {objective_too.name} (objective weights too): component weights differ "
          f"{risk_default['component_weights'] != risk_objective['component_weights']}")

    return [
        ("an overridden weight changes the ranking", both[0] != both[1]),
        ("a weighting that differs only in its indicator weights still moves the score",
         risk_default["ranking"] != risk_indicator["ranking"]
         and risk_indicator["preset"] == indicator_only.name
         and risk_default["component_weights"] == risk_indicator["component_weights"]),
        ("a weighting that also moves the objective terms reports the moved weights",
         risk_default["component_weights"] != risk_objective["component_weights"]
         and risk_objective["preset"] == objective_too.name),
        ("the override is reported rather than applied silently",
         bool(heavy["weights_overridden"]) and not default["weights_overridden"]),
        ("an untouched weighting keeps its stated origin",
         default["weights_origin"] == DEFAULT_PRESET.origin
         and default["weights_origin_url"] == DEFAULT_PRESET.origin_url),
        ("naming a different preset changes the weights used",
         named["weights_used"] != default["weights_used"]),
        # The tolerance is the rounding this module applies for the message, not
        # slack in the index: five weights rounded to six decimal places can sum
        # to 1.000002, and asserting exact equality would be asserting that the
        # reported number is the unrounded one, which it deliberately is not.
        ("the weights that were used are reported, whatever their source",
         abs(sum(default["weights_used"].values()) - 1.0) < 1e-5
         and abs(sum(heavy["weights_used"].values()) - 1.0) < 1e-5
         and len(heavy["weights_used"]) == len(VULNERABILITY_INDICATORS)),
        ("the unset sentinel is not mistaken for a weight of its own",
         vulnerability_index(**{
             schemas.weight_argument(name): schemas.UNSET_WEIGHT
             for name in VULNERABILITY_INDICATORS
         })["weights_used"] == default["weights_used"]),
    ]


def _omission_checks() -> list[tuple[str, bool]]:
    """That every ranking says how many units it left out, and why.

    The county number is printed and not asserted. One unscored tract here is a
    property of this county's water tract, not of the code, and asserting it would
    stop the harness working on the transfer county with no defect present.
    """
    index = vulnerability_index()
    risk = risk_scenario()
    exposure = hazard_exposure()
    print(f"  vulnerability: {index['omitted']['units_scored']} scored, "
          f"{index['omitted']['units_unscored']} unscored of {index['omitted']['units']}")
    print(f"  risk:          {risk['omitted']['units_scored']} scored, "
          f"{risk['omitted']['units_unscored']} unscored of {risk['omitted']['units']}")
    print(f"  unscored here: {index['omitted']['unscored_geoids']['listed']}")

    return [
        ("the index accounts for every unit, scored plus unscored",
         index["omitted"]["units_scored"] + index["omitted"]["units_unscored"]
         == index["omitted"]["units"] and index["omitted"]["units"] > 0),
        ("the risk table accounts for every unit too",
         risk["omitted"]["units_scored"] + risk["omitted"]["units_unscored"]
         == risk["omitted"]["units"] and risk["omitted"]["units"] > 0),
        ("every ranking says how many units it did not show",
         all("units_not_shown" in item for item in (index, risk, exposure))),
        ("an unscored unit is named rather than merely counted",
         index["omitted"]["unscored_geoids"]["total"]
         == index["omitted"]["units_unscored"]),
        ("the reason a unit is unscored travels with the count",
         bool(index["omitted"]["why_unscored"]) and bool(risk["omitted"]["why_unscored"])),
        ("the risk table reports the vulnerability omissions separately from its own",
         "vulnerability_omitted" in risk),
        ("a unit with no score is dropped from the ranking and counted, not ranked",
         risk["units_without_a_value"] == risk["omitted"]["units_unscored"]
         and index["units_without_a_value"] == index["omitted"]["units_unscored"]),
        ("the ranking, the units not shown and the units with no value account for all",
         len(risk["ranking"]) + risk["units_not_shown"] + risk["units_without_a_value"]
         == risk["omitted"]["units"]),
    ]


def _refusal_checks() -> list[tuple[str, bool]]:
    """What a tool refuses, and whether it refuses recoverably.

    A tool that raises makes `agent.execute_tool` produce `{"error": "KeyError"}`,
    which tells the model the type of the mistake and not the mistake. These
    return the legal values instead, so the next turn can be right.
    """
    unknown_scenario = hazard_exposure(scenario="surge_99m")
    unknown_preset = risk_scenario(preset="nobody_wrote_this")
    unknown_dataset = describe_layer(name="not-a-layer")
    unknown_retrieval = acquire_dataset(name="not-a-layer")
    print(f"  unknown scenario -> {unknown_scenario['error']!r}, "
          f"offering {unknown_scenario['scenarios']}")
    print(f"  unknown preset   -> {unknown_preset['error']!r}, "
          f"offering {unknown_preset['presets']}")

    return [
        ("an unknown scenario is refused with the legal names",
         unknown_scenario.get("error") == "unknown scenario"
         and sorted(SCENARIOS) == unknown_scenario["scenarios"]),
        ("an unknown preset is refused with the legal names",
         unknown_preset.get("error") == "unknown preset"
         and sorted(PRESETS) == unknown_preset["presets"]),
        ("an unknown layer is refused with the registered names",
         unknown_dataset.get("error") == "unknown dataset"
         and acquire.DATASET_TRACTS in unknown_dataset["datasets"]),
        ("an unretrievable name is refused with the retrievable ones",
         unknown_retrieval.get("error") == "unknown dataset"
         and acquire.DATASET_ELEVATION in unknown_retrieval["retrievable"]),
        ("no refusal raised instead of returning, so the loop can continue",
         all(isinstance(item, dict) for item in
             (unknown_scenario, unknown_preset, unknown_dataset, unknown_retrieval))),
        ("a refusal names what to do next rather than only what went wrong",
         all("detail" in item for item in (unknown_dataset, unknown_scenario))),
    ]


def _pending_checks(results: dict[str, ToolResult]) -> list[tuple[str, bool]]:
    """That a tool with no module behind it says so rather than pretending."""
    pending = pending_tools()
    refusals = {
        name: results[name] for name in pending
    }
    ready = [name for name in TOOL_NAMES if name not in pending]
    for name, result in refusals.items():
        print(f"  {name}: {result.get('detail', '')[:96]}")

    return [
        ("every pending tool returns a refusal naming its missing module",
         all(item.get("error") == "tool_unavailable" and item.get("backing_module")
             for item in refusals.values())),
        ("a pending tool is advertised as pending, so no turn is spent discovering it",
         all(item.get("advertised_as_pending") for item in refusals.values())),
        ("no tool that has a module behind it returns that refusal",
         not any(results[name].get("error") == "tool_unavailable" for name in ready)),
        ("the tools with no module are exactly the ones the mapping names",
         set(pending) <= set(BACKING_MODULES)),
        ("at least one tool is backed by a named module, so the probe has work to do",
         len(BACKING_MODULES) > 0),
    ]


def _provenance_checks(results: dict[str, ToolResult]) -> list[tuple[str, bool]]:
    """That what a tool reports about a dataset is what the registry holds.

    Invariant 6 is what makes the answer citable, and the failure mode is not a
    missing record -- it is a plausible one. A source URL written here rather than
    quoted from the retrieval would give the model something to cite that nothing
    retrieved, and every mechanical check about provenance existing would pass.
    So each field is compared against the record, not merely required to be there.
    """
    found = registry()
    listed = {item["name"]: item for item in results["list_datasets"]["datasets"]}
    records = {record.name: record for record in found.records()}
    # Read off the frozen `Provenance` dataclass, NOT back through `_provenance`.
    # Comparing the tool's output to the helper that produced it compares the
    # helper to itself: a source URL invented inside `_provenance` appears
    # identically on both sides and the assertion passes. That is exactly how a
    # mutation replacing the URL with a literal survived the first sweep.
    quoted = all(
        listed[name]["source_url"] == records[name].provenance.source_url
        and listed[name]["vintage"] == records[name].provenance.vintage
        and listed[name]["license"] == records[name].provenance.license
        and listed[name]["feature_count"] == records[name].provenance.feature_count
        and listed[name]["declared_crs"] == records[name].provenance.declared_crs
        and listed[name]["working_crs"] == records[name].provenance.working_crs
        and listed[name]["retrieved_at"]
        == records[name].provenance.retrieved_at.isoformat()
        for name in records
    )
    shaped = all(
        set(listed[name])
        == {"name", "kind", *_provenance(records[name], with_notes=False)}
        for name in records
    )
    detailed = describe_layer(name=acquire.DATASET_TRACTS)
    fields = ("source_url", "retrieved_at", "vintage", "license", "declared_crs")
    populated = {
        field_name: sum(1 for item in listed.values() if item.get(field_name))
        for field_name in fields
    }
    degraded = results["list_datasets"]["degraded"]
    print(f"  {len(listed)} dataset(s) listed; fields populated {populated}")
    print(f"  degraded, read through align.is_degraded: {degraded}")

    return [
        ("every registered dataset is listed", set(listed) == set(records) and len(listed) > 0),
        ("every value is read off the record rather than written here", quoted),
        ("the reported fields are the ones the provenance record carries", shaped),
        ("every dataset carries a source URL, a timestamp, a vintage and a licence",
         all(count == len(listed) for count in populated.values())),
        ("degradation is reported through the provenance flag, not inferred here",
         degraded == [
             name for name, record in records.items()
             if align.is_degraded(record.provenance)
         ]),
        ("the working CRS the layers were put into is reported beside them",
         results["list_datasets"]["working_crs"] == found.working_crs),
        ("the index reports how many retrieval notes a layer has without carrying them",
         all("note_count" in item and "notes" not in item for item in listed.values())),
        ("the detail view carries the notes the index only counted",
         detailed["notes"]["total"] == listed[acquire.DATASET_TRACTS]["note_count"]),
    ]


def _fault_checks() -> list[tuple[str, bool]]:
    """That each of `faults_between`'s three branches can fire.

    On a wired surface the three sets are equal, so every branch returns nothing
    and a deleted branch is indistinguishable from a satisfied one. These feed it
    sets that disagree in one way at a time, which is the only way to observe that
    the guard still works -- the same lesson as the S8 fixtures for rules this
    county cannot violate.
    """
    everything = {"a", "b"}
    unrunnable = faults_between({"a", "b"}, {"a"}, {"a", "b"})
    undeclared = faults_between({"a", "b"}, {"a", "b"}, {"a"})
    unreachable = faults_between({"a"}, {"a", "b"}, {"a", "b"})
    quiet = faults_between(everything, everything, everything)
    print(f"  advertised but not executable -> {len(unrunnable)} fault(s)")
    print(f"  advertised but not frozen     -> {len(undeclared)} fault(s)")
    print(f"  executable but not advertised -> {len(unreachable)} fault(s)")

    return [
        ("a tool offered to the model that nothing can run is reported",
         len(unrunnable) == 1 and "cannot be executed" in unrunnable[0]),
        ("a tool advertised outside the frozen contract is reported",
         len(undeclared) == 1 and "not in contracts.TOOL_NAMES" in undeclared[0]),
        ("a tool implemented and never advertised is reported",
         len(unreachable) == 1 and "never advertised" in unreachable[0]),
        ("three sets that agree produce no fault, so the guard is not always firing",
         quiet == []),
        ("the real surface routes through the same function the fixtures exercised",
         surface_faults() == faults_between(
             set(schemas.TOOL_ARG_MODELS), set(TOOL_FUNCTIONS), set(TOOL_NAMES)
         )),
    ]


def _shaping_checks() -> list[tuple[str, bool]]:
    """That the two shaping helpers state what they dropped.

    Neither can be observed on the real county: no list this study area produces
    is longer than the cap, and a missing value reaches `_scalar` only through the
    one unscored tract. Both are cheap to force and expensive to get wrong -- a
    truncation nobody counts, or a null rendered as a zero, is a wrong number that
    looks like a right one.
    """
    long = _capped([f"unit{index}" for index in range(MAX_LIST + 7)])
    short = _capped(["a", "b"])
    missing = [_scalar(value) for value in (None, float("nan"), pd.NA)]
    real = [_scalar(value) for value in (3, "x", True, 1.5)]
    print(f"  a list of {MAX_LIST + 7} was cut to {len(long['listed'])} "
          f"with {long['not_listed']} named as not listed")

    return [
        ("a list longer than the cap is cut", len(long["listed"]) == MAX_LIST),
        ("the number cut is reported rather than silently dropped",
         long["not_listed"] == 7 and long["total"] == MAX_LIST + 7),
        ("a list under the cap is untouched and reports nothing cut",
         short["listed"] == ["a", "b"] and short["not_listed"] == 0),
        ("a missing value is reported as missing, never as a zero",
         missing == [None, None, None]),
        ("a real value survives shaping unchanged", real == [3, "x", True, 1.5]),
    ]


def _cache_checks() -> list[tuple[str, bool]]:
    """That a live retrieval forgets the shared run, through the real call site.

    Last, because it drops the analysis every earlier check was reading. If the
    invalidation did nothing, a tool called after `acquire_dataset` would answer
    from the snapshot that retrieval replaced -- a stale number presented as a
    fresh one, which is the worst failure shape this module has.

    An earlier version called `invalidate()` directly, which proved the function
    works and never touched the line in `acquire_dataset` that calls it: deleting
    that line left the whole suite green. So this drives the real tool, with the
    retrieval and the registry replaced by stubs. Nothing here reaches the network
    and nothing here writes to `data/`: the scratch registry re-registers the
    records already on disk and saves its manifest into a temporary directory,
    which is what lets the call site be exercised offline without a snapshot being
    rewritten by a check.
    """
    before = analysis_built()
    real = registry()
    record = real.records()[0]
    original_registry, original_retrievers = registry, _retrievers
    snapshot_stamp = config.MANIFEST_PATH.stat().st_mtime_ns

    with tempfile.TemporaryDirectory() as folder:
        scratch = Registry(manifest_path=Path(folder) / config.MANIFEST_PATH.name)
        for existing in real.records():
            scratch.register(
                existing.name, existing.kind, existing.path, existing.provenance
            )

        def again(area: Any, found: Registry, *, timeout_s: float) -> Any:
            return found.register(
                record.name, record.kind, record.path, record.provenance
            )

        globals()["registry"] = lambda: scratch
        globals()["_retrievers"] = lambda: (Retriever((record.name,), again, False),)
        try:
            result = acquire_dataset(name=record.name)
        finally:
            globals()["registry"] = original_registry
            globals()["_retrievers"] = original_retrievers

    after = analysis_built()
    untouched = config.MANIFEST_PATH.stat().st_mtime_ns == snapshot_stamp
    print(f"  a run was held: {before}; after retrieving {record.name!r}: {after}")
    print(f"  the retrieval reported {len(result.get('retrieved', []))} record(s) "
          f"and a manifest written to a temporary directory")

    return [
        ("a shared run was held, so the state under test is not vacuous", before),
        ("a retrieval says it invalidated the analysis",
         result.get("analysis_invalidated") is True),
        ("and the run really is forgotten, through acquire_dataset's own call site",
         not after),
        ("the retrieval reported the provenance of what it re-registered",
         len(result.get("retrieved", [])) == 1
         and result["retrieved"][0]["source_url"] == record.provenance.source_url),
        ("the bound the caller passed is reported back rather than assumed",
         result.get("timeout_s") == config.REQUEST_TIMEOUT_S),
        ("the snapshot manifest was not rewritten by a check",
         untouched),
        ("the stubs were put back, so later callers get the real registry",
         registry is original_registry and _retrievers is original_retrievers),
    ]


def _log_checks() -> list[tuple[str, bool]]:
    """That every tool result is recorded, which is what invariant 8 traces to."""
    before = len(logged_calls())
    result = list_datasets()
    after = logged_calls()
    print(f"  call log holds {len(after)} result(s), bounded at {MAX_CALLS}")

    return [
        ("a tool call is recorded as it returns",
         len(after) == before + 1 and after[-1]["tool"] == "list_datasets"),
        ("the recorded result is the result that was returned",
         after[-1]["result"] == result),
        ("the log records the arguments too, so a number can be traced to a question",
         "arguments" in after[-1]),
        ("the log is bounded rather than growing for the life of the process",
         len(after) <= MAX_CALLS),
        ("every tool is recorded, not only the ones that read the analysis",
         {item["tool"] for item in after} == set(TOOL_NAMES)),
    ]


def _elicitation_checks() -> list[tuple[str, bool]]:
    """That the human-in-the-loop tool really asks, and that the answer scores.

    Driven through the real `input()` with a scripted reader rather than by
    calling `elicit` with a string, because the thing that can break is the
    channel: a guard that reads the wrong stream, or a prompt nobody sees. Only
    the person is replaced, which is the same shape as the sandbox's scripted
    author.

    The batch path is checked in the same breath. A tool that asked for real and
    hung a harness would be a worse tool than the menu it replaced.
    """
    global ELICIT
    names = [item.name for item in WEIGHT_PRESETS]
    other = [name for name in names if name != DEFAULT_PRESET.name][0]
    real_stdin, real_elicit = sys.stdin, ELICIT

    def scripted(reply: str, tty: bool) -> io.StringIO:
        stream = io.StringIO(reply)
        stream.isatty = lambda: tty  # type: ignore[method-assign]
        return stream

    def answered(reply: str, newline: bool = True) -> ToolResult:
        global ELICIT
        sys.stdin = scripted(reply + "\n" if newline else reply, True)
        ELICIT = True
        try:
            return ask_user_preferences(question="which weighting should decide priority?")
        finally:
            sys.stdin = real_stdin

    def channel_under(flag: bool, tty: bool) -> bool:
        global ELICIT
        sys.stdin, ELICIT = scripted("", tty), flag
        try:
            return interactive_channel()
        finally:
            sys.stdin, ELICIT = real_stdin, real_elicit

    try:
        by_name = answered(other)
        by_number = answered(str(names.index(other) + 1))
        by_default = answered("")
        off_menu = answered(str(len(names) + 1))
        pinned = answered(f"start with {PINNED_REPLY_COORDINATE} please")
        # No trailing newline, so `input()` really hits EOF and raises. With one
        # appended this was byte-identical to the empty-reply case above, and the
        # except branch that survives a closed prompt had no coverage at all --
        # a real closed stdin would have raised EOFError out of a tool call.
        closed = answered("", newline=False)
        ELICIT = False
        batch = ask_user_preferences(question="which weighting should decide priority?")
    finally:
        sys.stdin, ELICIT = real_stdin, real_elicit

    # The scripted channel's two scoring paths: a menu pick, which re-scores the
    # county, and a script that has run out, which is nobody answering. Both
    # build the analysis, which is why they are here rather than with the
    # scripted checks that run without one.
    saved_env, saved_calls = os.environ.get(SCRIPTED_REPLIES_ENV), list(CALLS)
    with tempfile.TemporaryDirectory() as folder:
        script = Path(folder) / "replies.json"
        script.write_text(json.dumps([other]), encoding="utf-8")
        os.environ[SCRIPTED_REPLIES_ENV] = str(script)
        try:
            forget_calls()
            scripted_pick = ask_user_preferences(question="which weighting?")
            ran_out = ask_user_preferences(question="and which now?")
        finally:
            forget_calls()
            CALLS.extend(saved_calls)
            if saved_env is None:
                os.environ.pop(SCRIPTED_REPLIES_ENV, None)
            else:
                os.environ[SCRIPTED_REPLIES_ENV] = saved_env

    scored_here = weighting_effect(other)
    scored_default = weighting_effect(DEFAULT_PRESET.name)
    print(f"  a person naming {other!r}: elicited={by_name['elicited']} "
          f"chosen={by_name['chosen']} channel={by_name['channel']}")
    print(f"  by number: chosen={by_number['chosen']}; "
          f"empty reply: chosen={by_default['chosen']}; "
          f"number off the menu: chosen={off_menu['chosen']}")
    print(f"  with no terminal: elicited={batch['elicited']} chosen={batch['chosen']}")
    print(f"  a script naming {other!r}: elicited={scripted_pick['elicited']} "
          f"chosen={scripted_pick['chosen']}; once it ran out: "
          f"elicited={ran_out['elicited']} chosen={ran_out['chosen']}")
    print(f"  the two weightings rank differently: "
          f"{scored_here['ranking'] != scored_default['ranking']}")

    return [
        ("a person at a terminal is really asked", by_name["elicited"] is True),
        ("the channel is named rather than assumed", by_name["channel"] == "terminal"),
        ("a weighting named by the person is the one chosen",
         by_name["chosen"] == other),
        ("a weighting chosen by number is the same choice",
         by_number["chosen"] == other),
        ("an empty reply is the person accepting the default, and says so",
         by_default["chosen"] == DEFAULT_PRESET.name
         and by_default["elicited"] is True
         and "accepted the default" in by_default["how_it_was_chosen"]),
        ("a number off the menu falls back to the default rather than failing",
         off_menu["chosen"] == DEFAULT_PRESET.name and off_menu["elicited"] is True
         and off_menu["stated_preference"] == ""),
        # The rule this replaced was "never quote a reply back", written after S10's
        # guard leaked a coordinate by quoting what it refused. A person's own words
        # are now the preference and come back by design, so the rule is narrowed
        # to the thing it was for: words that carry a coordinate never come back.
        ("words that carry a coordinate are never quoted back into the result",
         PINNED_REPLY_COORDINATE not in json.dumps(pinned, default=str)
         and pinned["chosen"] == DEFAULT_PRESET.name
         and not coordinate_faults(as_sent(pinned))),
        ("a scripted menu pick is the script's choice, marked scripted, and it scores",
         scripted_pick["elicited"] == SCRIPTED
         and scripted_pick["chosen"] == other
         and scripted_pick["effect"]["recomputed"] is True),
        ("a script that has run out is nobody answering, and the default stands",
         ran_out["elicited"] is False
         and ran_out["channel"] == "script"
         and ran_out["chosen"] == DEFAULT_PRESET.name
         and "nobody answered" in ran_out["why"]),
        ("a closed prompt is survivable and took the branch written for it",
         closed["chosen"] == DEFAULT_PRESET.name
         and "closed without a reply" in closed["how_it_was_chosen"]),
        ("with no terminal nothing blocks and elicited tells the truth",
         batch["elicited"] is False and batch["channel"] == "none"),
        ("the fallback is the default, not a preference nobody expressed",
         batch["chosen"] == DEFAULT_PRESET.name and NO_CHANNEL in batch["why"]),
        ("the chosen weighting reaches the score rather than the transcript",
         by_name["effect"]["recomputed"] is True
         and by_name["effect"]["ranking"] == scored_here["ranking"]),
        ("a different weighting really produces a different ranking",
         scored_here["ranking"] != scored_default["ranking"]),
        ("the recomputation runs the same call risk_scenario runs",
         [row[Col.GEOID] for row in scored_here["ranking"]]
         == [row[Col.GEOID] for row in _ranking(
             scored(DEFAULT_SCENARIO, _preset(other))[0],
             by=Col.RISK_SCORE,
             columns=(Col.GEOID,),
             top_n=DEFAULT_PRIORITY_UNITS)[0]]),
        ("the result names who each other weighting would have prioritised instead",
         set(by_name["effect"]["dropped_against_each_other_weighting"])
         == {item.name for item in WEIGHT_PRESETS} - {other}),
        ("both guards open together", channel_under(True, True)),
        ("closing the flag closes the channel even at a terminal",
         not channel_under(False, True)),
        ("no terminal closes the channel even with the flag open",
         not channel_under(True, False)),
    ]


def _user_weighting_checks() -> list[tuple[str, bool]]:
    """That a person's words become a validated weighting, and nothing else does.

    Pure: no analysis is built, so this runs on a checkout with no snapshot. The
    expected weights are worked out here by hand from the default preset's own
    values, not through `normalised_weights`, which is the function under test.
    """
    age, car = Col.PCT_AGE_65_PLUS, Col.PCT_NO_VEHICLE
    named = {schemas.weight_argument(age): 2.0, schemas.weight_argument(car): 2.0}
    before = dict(DEFAULT_PRESET.weights)
    built = user_weighting(DEFAULT_PRESET, named, SAMPLE_PREFERENCE)
    others = [name for name in VULNERABILITY_INDICATORS if name not in (age, car)]
    total = 2.0 + 2.0 + sum(before[name] for name in others)
    expected = {
        name: (2.0 if name in (age, car) else before[name]) / total
        for name in VULNERABILITY_INDICATORS
    }
    used = (
        vulnerability.normalised_weights(built.weights, VULNERABILITY_INDICATORS)
        if built is not None
        else {}
    )
    every_unset = {
        argument: schemas.UNSET_WEIGHT
        for argument in schemas.weight_arguments(schemas.RISK_WEIGHT_KEYS)
    }
    on_exposure = user_weighting(
        DEFAULT_PRESET,
        {schemas.weight_argument(Col.EXPOSED_POPULATION): 3.0},
        SAMPLE_PREFERENCE,
    )
    all_zero = {schemas.weight_argument(name): 0.0 for name in VULNERABILITY_INDICATORS}
    # Caught rather than left to raise, so a broken sentinel reads as this check
    # failing instead of as the whole suite crashing on an unrelated line.
    try:
        nothing: tuple[Any, ...] = (
            user_weighting(DEFAULT_PRESET, every_unset, ""),
            user_weighting(DEFAULT_PRESET, {}, ""),
        )
    except (KeyError, ValueError) as exc:
        nothing = (exc,)
    print(f"  {SAMPLE_PREFERENCE!r} as weight 2 on {age} and {car}:")
    print(f"    normalised          {({name: round(value, 4) for name, value in used.items()})}")
    print(f"    worked out by hand  {({name: round(value, 4) for name, value in expected.items()})}")

    return [
        ("a weight the person's words name is applied, the rest keep the named weighting's",
         built is not None and built.weights == {**before, age: 2.0, car: 2.0}),
        ("the indicator weights normalise to the values worked out by hand",
         bool(used)
         and all(abs(used[name] - expected[name]) < 1e-12 for name in VULNERABILITY_INDICATORS)),
        ("the two indicators the words name outweigh every other",
         bool(used) and min(used[age], used[car]) > max(used[name] for name in others)),
        ("the objective half is untouched when the words name only indicators",
         built is not None
         and all(built.weights[name] == before[name] for name in vulnerability.OBJECTIVE_TERMS)),
        ("an objective term can be weighted from the person's words too",
         on_exposure is not None and on_exposure.weights[Col.EXPOSED_POPULATION] == 3.0),
        ("the weighting is named, and sourced, as the person's sentence",
         built is not None
         and built.name == schemas.USER_WEIGHTING
         and built.origin == schemas.USER_WEIGHTING
         and built.origin_note == SAMPLE_PREFERENCE
         and built.origin_url == ""),
        ("building on the named weighting leaves it unchanged",
         DEFAULT_PRESET.weights == before),
        ("no weight set builds no weighting, so the tools rank as before",
         nothing == (None, None)),
        ("a misspelled weight is refused by name rather than dropped",
         verify.refuses(
             lambda: user_weighting(
                 DEFAULT_PRESET, {**named, "weight_pct_elderly": 2.0}, SAMPLE_PREFERENCE
             ),
             KeyError,
             "weight_pct_elderly",
         )),
        ("a negative weight is refused",
         verify.refuses(
             lambda: user_weighting(
                 DEFAULT_PRESET, {schemas.weight_argument(age): -0.5}, SAMPLE_PREFERENCE
             ),
             ValueError,
             "negative weight",
         )),
        ("a weighting whose indicators all weigh zero is refused",
         verify.refuses(
             lambda: user_weighting(DEFAULT_PRESET, all_zero, SAMPLE_PREFERENCE),
             ValueError,
             "sum to",
         )),
        ("a weight that is not a finite number is refused, NaN and Infinity alike",
         all(
             verify.refuses(
                 lambda value=value: user_weighting(
                     DEFAULT_PRESET, {schemas.weight_argument(age): value}, SAMPLE_PREFERENCE
                 ),
                 ValueError,
                 "not finite",
             )
             for value in (float("nan"), float("inf"))
         )),
        ("weights with no sentence are refused, so nobody's preference is invented",
         verify.refuses(
             lambda: user_weighting(DEFAULT_PRESET, named, "  "), ValueError, "no preference"
         )),
        ("a sentence with no weight is refused, so the default never reads as theirs",
         verify.refuses(
             lambda: user_weighting(DEFAULT_PRESET, every_unset, SAMPLE_PREFERENCE),
             ValueError,
             "no weight set",
         )),
        ("an unknown argument that is not a weight is ignored, as every tool ignores one",
         user_weighting(DEFAULT_PRESET, {**named, "nonsense": 1}, SAMPLE_PREFERENCE)
         is not None),
        ("a preference carrying a coordinate is refused, and the refusal does not quote it",
         verify.refuses(
             lambda: user_weighting(
                 DEFAULT_PRESET, named, f"{SAMPLE_PREFERENCE} near {PINNED_REPLY_COORDINATE}"
             ),
             ValueError,
             "could carry a coordinate",
         )
         and not verify.refuses(
             lambda: user_weighting(
                 DEFAULT_PRESET, named, f"{SAMPLE_PREFERENCE} near {PINNED_REPLY_COORDINATE}"
             ),
             ValueError,
             PINNED_REPLY_COORDINATE,
         )),
        ("the person's weighting cannot collide with a named one",
         schemas.USER_WEIGHTING not in PRESETS),
    ]


def _free_text_checks() -> list[tuple[str, bool]]:
    """That a person who types a sentence at the terminal gets their words back.

    Driven through the real `input()` with a scripted reader, as
    `_elicitation_checks` is. Only the sentence path runs end to end here, because
    it is the one reply that scores nothing until it is mapped; `read_reply` is
    called directly for the rest, so this also runs with no snapshot.
    """
    global ELICIT
    names = [item.name for item in WEIGHT_PRESETS]
    real_stdin, real_elicit = sys.stdin, ELICIT
    pinned_reply = f"flood {PINNED_REPLY_COORDINATE} first"

    def typed_in(*lines: str) -> ToolResult:
        global ELICIT
        stream = io.StringIO("".join(line + "\n" for line in lines))
        stream.isatty = lambda: True  # type: ignore[method-assign]
        sys.stdin, ELICIT = stream, True
        try:
            return ask_user_preferences(question="what matters most to you?")
        finally:
            sys.stdin, ELICIT = real_stdin, real_elicit

    typed = typed_in(SAMPLE_PREFERENCE)
    retried = typed_in(pinned_reply, SAMPLE_PREFERENCE)
    # Two refused replies: the default is imposed, and that result scores the
    # county under it -- with no snapshot the scoring fails and is reported in
    # `effect`, which is not under test here. `why` and `chosen` are.
    twice = typed_in(pinned_reply, f"then {PINNED_REPLY_COORDINATE}")

    # The guard made unloadable, the way a checkout without src/sandbox.py has it.
    package = sys.modules[__package__]
    held = getattr(package, "sandbox", None)
    loaded = sys.modules.get(f"{__package__}.sandbox")
    try:
        if held is not None:
            delattr(package, "sandbox")
        sys.modules[f"{__package__}.sandbox"] = None  # type: ignore[assignment]
        unguarded = read_reply(SAMPLE_PREFERENCE, names, DEFAULT_PRESET.name, "the person")
    finally:
        if loaded is None:
            sys.modules.pop(f"{__package__}.sandbox", None)
        else:
            sys.modules[f"{__package__}.sandbox"] = loaded
        if held is not None:
            setattr(package, "sandbox", held)

    who = "the person was asked and"
    padded = read_reply(f"  {SAMPLE_PREFERENCE}  ", names, DEFAULT_PRESET.name, who)
    pinned = read_reply(pinned_reply, names, DEFAULT_PRESET.name, who)
    off_menu = read_reply(str(len(names) + 1), names, DEFAULT_PRESET.name, who)
    by_name = read_reply(names[1].upper(), names, DEFAULT_PRESET.name, who)
    by_number = read_reply("2", names, DEFAULT_PRESET.name, who)
    empty = read_reply("", names, DEFAULT_PRESET.name, who)
    # Each shape a reviewer got through the first version of this guard, which
    # read the string rule only. Every one came back word for word.
    shapes = {
        "GeoJSON": '{"type":"Polygon","coordinates":[[[-10.12,20.65],[-10.1,20.6]]]}',
        "WKT": "POLYGON ((-10.12 20.65, -10.1 20.6, -10.12 20.65))",
        "labelled degrees": "start at lat 20.6543 lon -10.1234",
        "three decimal places": "start at 20.654, -10.123",
        "easting and northing": "start at easting 1613477.7, northing 1234567.9",
        "slash between": "start at 20.6543/-10.1234",
        "semicolon between": "start at 20.6543;-10.1234",
        "hemisphere letters": "start at 20.6543 N 10.1234 W",
        "degree signs": "start at 20.65°N, 10.12°W",
    }
    withheld = {
        label: read_reply(text, names, DEFAULT_PRESET.name, who)[1] == ""
        for label, text in shapes.items()
    }
    stated_weights = "older residents 0.4 and households without a car 0.4"
    numeric = read_reply(stated_weights, names, DEFAULT_PRESET.name, who)
    print(f"  typed {SAMPLE_PREFERENCE!r}: elicited={typed['elicited']} "
          f"chosen={typed['chosen']} stated={typed['stated_preference']!r}")
    print(f"  words carrying a coordinate -> chosen={pinned[0]} stated={pinned[1]!r}")
    print(f"  withheld by shape: {withheld}")

    return [
        ("a person typing a sentence is really asked, at the terminal",
         typed["elicited"] is True and typed["channel"] == "terminal"),
        ("their sentence comes back word for word",
         typed["stated_preference"] == SAMPLE_PREFERENCE),
        ("a sentence is named as the person's own weighting, not a menu pick",
         typed["chosen"] == schemas.USER_WEIGHTING
         and "plain words" in typed["how_it_was_chosen"]),
        ("a sentence scores nothing until it is mapped onto weights, and says so",
         typed["effect"]["recomputed"] is False
         and "weight_" in typed["effect"].get("why", "")),
        ("the result tells the model to map the words and pass them on unchanged",
         "stated_preference" in typed["why"] and "preference" in typed["how_to_use"]),
        ("surrounding spaces are trimmed and nothing else is changed",
         padded[1] == SAMPLE_PREFERENCE),
        ("words carrying a coordinate are withheld and the default stands",
         pinned[0] == DEFAULT_PRESET.name and pinned[1] == "" and "withheld" in pinned[2]),
        ("withheld words are marked as not used, so the default is not called a choice",
         pinned[3] is False and off_menu[3] is False and padded[3] is True),
        ("a number off the menu keeps the default rather than becoming a sentence",
         off_menu[0] == DEFAULT_PRESET.name and off_menu[1] == ""),
        ("a refused reply is asked once more, and the second answer is the one used",
         retried["stated_preference"] == SAMPLE_PREFERENCE and retried["elicited"] is True),
        ("two refused replies impose the default, and the result says nobody chose it",
         twice["chosen"] == DEFAULT_PRESET.name
         and twice["stated_preference"] == ""
         and "no usable reply" in twice["why"]
         and "not a preference anybody expressed" in twice["why"]),
        ("with the guard unloadable every reply is refused, and the reason names the guard",
         unguarded[1] == ""
         and unguarded[3] is False
         and "could not be loaded" in unguarded[2]
         and CARRIES_COORDINATE not in unguarded[2]),
        ("a menu name in any case is a menu pick, not a sentence",
         by_name[0] == names[1] and by_name[1] == ""),
        ("a menu number is a menu pick",
         by_number == (names[1], "", f"{who} chose by number", True)),
        ("an empty reply accepts the default",
         empty[0] == DEFAULT_PRESET.name and empty[1] == ""),
        ("every coordinate shape a reviewer got through is now withheld",
         all(withheld.values()) and len(withheld) == len(shapes)),
        ("weights a person states as numbers, with words between them, still pass",
         numeric[1] == stated_weights),
        ("the sentence result carries no coordinate, by the widest rule",
         not coordinate_faults(as_sent(typed))
         and not coordinate_refusal(typed["stated_preference"])),
    ]


def _scripted_checks() -> list[tuple[str, bool]]:
    """That a harness can answer for a person without posing as one.

    A real file, named by the real environment variable, read by the real tool. A
    terminal is open at the same time holding a different reply, to show the
    script wins and `input()` is never reached: a harness started from a terminal
    must not stop and wait on somebody who is not there. Runs with no snapshot.

    The call log is put back afterwards, because `forget_calls` is part of what is
    under test and `_log_checks` later reads the log this suite built.

    Every reply here is a sentence, the terminal's included, because a sentence is
    the one reply that scores nothing. A mutant that reads the wrong channel then
    fails a check here rather than crashing on a missing snapshot, and the script
    holds two different sentences so that a cursor nobody reset is told apart from
    one that was.
    """
    global ELICIT
    real_stdin, real_elicit = sys.stdin, ELICIT
    saved_env, saved_calls = os.environ.get(SCRIPTED_REPLIES_ENV), list(CALLS)
    saved_question = QUESTION
    terminal = io.StringIO("words typed at the terminal\n")
    terminal.isatty = lambda: True  # type: ignore[method-assign]

    def ask() -> ToolResult:
        return ask_user_preferences(question="what matters most to you?")

    with tempfile.TemporaryDirectory() as folder:
        good = Path(folder) / "replies.json"
        good.write_text(
            json.dumps([SAMPLE_PREFERENCE, "a second scripted reply"]), encoding="utf-8"
        )
        wrong = Path(folder) / "wrong.json"
        wrong.write_text(json.dumps({"reply": SAMPLE_PREFERENCE}), encoding="utf-8")
        missing = Path(folder) / "absent.json"
        try:
            sys.stdin, ELICIT = terminal, True
            os.environ[SCRIPTED_REPLIES_ENV] = str(good)
            forget_calls()
            first = ask()
            unread = terminal.tell() == 0
            traced = preference_trace(SAMPLE_PREFERENCE)
            untraced = preference_trace("words nobody said")
            forget_calls()
            again = ask()
            os.environ[SCRIPTED_REPLIES_ENV] = str(wrong)
            malformed = ask()
            os.environ[SCRIPTED_REPLIES_ENV] = str(missing)
            absent = ask()
            forget_calls(f"Which tracts first? For me, {SAMPLE_PREFERENCE}.")
            from_question = preference_trace(SAMPLE_PREFERENCE.upper())
            paraphrased = preference_trace("elderly people and the carless come first")
            fragments = {
                text: preference_trace(text)["answered_by"]
                for text in ("?", "first", "me, older", "ch tracts first")
            }
        finally:
            sys.stdin, ELICIT = real_stdin, real_elicit
            forget_calls(saved_question)
            CALLS.extend(saved_calls)
            if saved_env is None:
                os.environ.pop(SCRIPTED_REPLIES_ENV, None)
            else:
                os.environ[SCRIPTED_REPLIES_ENV] = saved_env

    print(f"  scripted {SAMPLE_PREFERENCE!r}: elicited={first['elicited']!r} "
          f"channel={first['channel']} stated={first['stated_preference']!r}")
    print(f"  a terminal was open and left unread: {unread}; traced to "
          f"{traced['traced_to']} (elicited={traced.get('elicited')!r})")
    print(f"  a script holding a dict -> {malformed.get('error')!r}; "
          f"a missing script -> {absent.get('error')!r}")

    return [
        ("a scripted reply is marked scripted, never as a person's answer",
         first["elicited"] == SCRIPTED
         and first["elicited"] is not True
         and first["channel"] == "script"),
        ("the scripted words come back word for word",
         first["stated_preference"] == SAMPLE_PREFERENCE),
        ("the result says a script answered, not a person", "not a person" in first["why"]),
        ("the script wins over an open terminal, which is never read", unread),
        ("a sentence the tool returned is traced to it, and says a script answered",
         traced["traced_to"] == "ask_user_preferences"
         and traced["elicited"] == SCRIPTED
         and traced["answered_by"] == "script"),
        ("words no reply returned and the question does not hold are not traced",
         untraced["answered_by"] is None and "not verified" in untraced["note"]),
        ("words the question holds unchanged are traced to the question, in any case",
         from_question["answered_by"] == "question"),
        ("a paraphrase of the question is not the question's words",
         paraphrased["answered_by"] is None),
        ("a character, a word, two words or a broken word of the question trace to nobody",
         all(value is None for value in fragments.values()) and len(fragments) == 4),
        ("a new question starts the script again from its first reply",
         again["stated_preference"] == SAMPLE_PREFERENCE and again["elicited"] == SCRIPTED),
        ("a script that is not a list of replies is refused, not read as silence",
         malformed.get("error") == "scripted replies unreadable"
         and malformed.get("variable") == SCRIPTED_REPLIES_ENV),
        ("a script that is missing is refused too",
         absent.get("error") == "scripted replies unreadable"),
        ("the checks put the environment and the question back as they found them",
         os.environ.get(SCRIPTED_REPLIES_ENV) == saved_env and QUESTION == saved_question),
    ]


def _boundary_refusal_checks() -> list[tuple[str, bool]]:
    """The refusals, driven through `agent.execute_tool`, the path a model's call takes.

    Called through the loop's own executor rather than the tool function, because
    the thing that can break is between them: if `execute_tool` ever kept only the
    declared fields, a misspelled weight would be dropped before the tool could
    refuse it, and every direct call here would still pass. Each of these is
    refused before the analysis is built, so this runs with no snapshot.

    **Everything here goes through `agent.tools`, not this module's globals.** Run
    as `python -m src.tools --check`, this file is `__main__`, and `agent` imports a
    second copy of it as `src.tools`. A question set on the first copy is a
    question the executor never sees -- which is how an earlier version of this
    check asserted on state the call it made could not reach.

    The last part runs `agent.run_agent` itself, with the model replaced by
    `critic.scripted_run`'s fixed drafts, so the line in the loop that hands the
    question to the tools is exercised. Without it, every preference quoted from a
    question would be refused in production and no check would notice.
    """
    from . import agent, critic

    target = agent.tools
    saved_calls, saved_question = list(target.CALLS), target.QUESTION
    age = schemas.weight_argument(Col.PCT_AGE_65_PLUS)
    located = "flood POLYGON ((1.5 2.5, 3.5 4.5, 1.5 2.5)) first"
    try:
        target.forget_calls("which communities should come first under a three metre surge?")
        misspelled = {
            name: agent.execute_tool(
                name, {"weight_pct_elderly": 2.0, "preference": SAMPLE_PREFERENCE}
            )
            for name in (*schemas.RISK_WEIGHT_TOOLS, "vulnerability_index")
        }
        objective_on_index = agent.execute_tool(
            "vulnerability_index",
            {schemas.weight_argument(Col.EXPOSED_POPULATION): 3.0},
        )
        unstated = {
            name: agent.execute_tool(name, {age: 2.0, "preference": SAMPLE_PREFERENCE})
            for name in schemas.RISK_WEIGHT_TOOLS
        }
        pinned = agent.execute_tool("risk_scenario", {age: 2.0, "preference": located})
        run = critic.scripted_run(["no number in this answer"], 0)
        asked = str(run["steps"][0]["payload"].get("question", ""))
        reached = target.QUESTION
    finally:
        target.forget_calls(saved_question)
        target.CALLS.extend(saved_calls)
    print(f"  through execute_tool: misspelled -> "
          f"{ {name: item.get('error') for name, item in misspelled.items()} }")
    print(f"  words nobody on this question said -> "
          f"{ {name: item.get('error') for name, item in unstated.items()} }")
    print(f"  run_agent asked {asked!r}; the tools it calls hold {reached!r}")

    return [
        ("a misspelled weight survives validation and is refused by name, in all three",
         all(
             item.get("error") == "invalid weighting"
             and "weight_pct_elderly" in item.get("detail", "")
             for item in misspelled.values()
         )
         and len(misspelled) == 3),
        ("an objective term passed to the index is refused, with a pointer to the risk tools",
         objective_on_index.get("error") == "invalid weighting"
         and "risk_scenario" in objective_on_index.get("detail", "")),
        ("words neither a reply nor the question holds rank nothing, in both risk tools",
         all(item.get("error") == "unstated preference" for item in unstated.values())),
        ("a preference carrying a coordinate is refused without being quoted back",
         pinned.get("error") == "invalid weighting"
         and "1.5 2.5" not in json.dumps(pinned, default=str)),
        ("run_agent hands its question to the tools it calls",
         bool(asked) and reached == asked),
        ("the checks put the executor's call log and question back",
         target.QUESTION == saved_question and len(target.CALLS) == len(saved_calls)),
    ]


def _live_map_checks() -> list[tuple[str, bool]]:
    """`risk_scenario(export_map=True)` on the real analysis, written to a temporary folder."""
    global MAP_DIRECTORY
    kept = MAP_DIRECTORY
    with tempfile.TemporaryDirectory(prefix="oasis-live-map-") as temporary:
        MAP_DIRECTORY = Path(temporary)
        try:
            plain = risk_scenario(scenario=HAZARD_SCENARIOS[0].name)
            mapped = risk_scenario(scenario=HAZARD_SCENARIOS[0].name, export_map=True)
            files = mapped.get("map_files", {})
            written = sorted(path.name for path in MAP_DIRECTORY.iterdir())
        finally:
            MAP_DIRECTORY = kept
    print(f"  live map export wrote {written}; result carries {files}")
    return [
        ("risk_scenario without export_map adds no map field",
         "map_files" not in plain and "map_error" not in plain),
        ("risk_scenario with export_map writes both files and names them",
         isinstance(files, dict) and len(written) == 2
         and {Path(files.get("geopackage", "")).name, Path(files.get("png", "")).name} == set(written)),
        ("export_map changes nothing in the ranking it maps",
         mapped.get("ranking") == plain.get("ranking")),
        ("the mapped result still carries no coordinate", not coordinate_faults(mapped)),
    ]


def _map_fixture() -> tuple[pd.DataFrame, Any]:
    """Four square tracts in geographic degrees, one of them unscored.

    Degrees rather than the working CRS, so the export has to reproject them and
    the check that the GeoPackage is in the working CRS can fail.
    """
    import geopandas as gpd
    from shapely.geometry import box

    side = 0.01
    west, south = -90.0, 35.0
    shapes = [
        box(west + column * side, south, west + (column + 1) * side, south + side)
        for column in range(4)
    ]
    units = gpd.GeoDataFrame(
        {Col.GEOID: [f"0000000000{index}" for index in range(4)]},
        geometry=shapes,
        crs="EPSG:4326",
    )
    frame = pd.DataFrame(index=units.index)
    frame[Col.GEOID] = units[Col.GEOID].astype("string")
    frame[Col.PRIORITY_RANK] = pd.Series([1, 2, 3, pd.NA], index=units.index, dtype="Int64")
    frame[Col.RISK_SCORE] = pd.Series([0.9, 0.6, 0.3, pd.NA], index=units.index, dtype="Float64")
    for column in MAP_COLUMNS[3:]:
        frame[column] = pd.Series([0.5, 0.4, 0.3, 0.2], index=units.index, dtype="Float64")
    return frame, units


def _map_export_checks() -> list[tuple[str, bool]]:
    """That `export_map` writes a real GeoPackage and PNG and hands back only paths.

    Runs on a fixture, with the real geopandas driver and the real matplotlib
    backend, so a missing driver fails here rather than in a demo. The read-back is
    of the file, not of the frame that was written.
    """
    import geopandas as gpd

    frame, units = _map_fixture()
    aligner = align.Alignment()
    with tempfile.TemporaryDirectory(prefix="oasis-map-check-") as temporary:
        directory = Path(temporary)
        try:
            paths = export_ranked_map(
                frame, units, aligner, stem="risk_fixture_user",
                outlined=("00000000000", "00000000001"), title="fixture", directory=directory,
            )
            again = export_ranked_map(
                frame, units, aligner, stem="risk_fixture_user",
                outlined=("00000000000", "00000000001"), title="fixture", directory=directory,
            )
            geopackage = directory / "risk_fixture_user.gpkg"
            picture = directory / "risk_fixture_user.png"
            read = gpd.read_file(geopackage)
            ranks = dict(zip(read[Col.GEOID].astype(str), read[Col.PRIORITY_RANK]))
            written = True
        except Exception as exc:  # noqa: BLE001 -- the check reports, it does not crash
            print(f"  map export failed: {type(exc).__name__}: {exc}")
            paths, again, ranks, read, written = {}, {}, {}, None, False
            geopackage = picture = directory / "missing"
        checks = [
            ("export_map writes a GeoPackage and a PNG", written and geopackage.is_file() and picture.is_file()),
            ("the PNG is a real PNG", written and picture.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"),
            ("the GeoPackage reads back every tract with its rank, the unscored one blank",
             written and len(read) == len(units)
             and ranks.get("00000000000") == 1 and ranks.get("00000000002") == 3
             and pd.isna(ranks.get("00000000003"))),
            ("the GeoPackage is in the working CRS",
             written and str(read.crs) == str(gpd.GeoSeries([], crs=config.STUDY_AREA.working_crs).crs)),
            ("a second export replaces the first rather than failing", written and again == paths),
            ("the result is file paths and a note, all strings, with no coordinate",
             written and set(paths) == {"geopackage", "png", "note"}
             and all(isinstance(value, str) for value in paths.values())
             and not coordinate_faults(paths)),
        ]
    shuffled = frame.iloc[::-1]
    relabelled = frame.copy()
    relabelled[Col.GEOID] = relabelled[Col.GEOID].iloc[::-1].to_numpy()
    for name, wrong in (("reordered", shuffled), ("relabelled", relabelled)):
        checks.append(
            (f"a {name} ranking is refused, not joined by position",
             verify.refuses(
                 lambda wrong=wrong: export_ranked_map(
                     wrong, units, aligner, stem="x", outlined=(), title="x",
                     directory=Path(tempfile.gettempdir()),
                 ),
                 ValueError,
                 "same rows",
             ))
        )
    return checks


def _user_ranking_checks() -> list[tuple[str, bool]]:
    """That a ranking under the person's words follows them, on the real county.

    Three parts of one promise. With no weight set, both risk tools add no field,
    and every weight left at the sentinel is the same call as no weight argument.
    With weights set, the ranking is made under the person's weighting and every
    origin field says so. And the comparison sets that weighting beside each named
    one, pair by pair, with the pairs recomputed here from the rows.

    What these cannot show is that the no-weights output equals the output of the
    code before this change: both sides of every comparison here run the new code.
    That is a one-off comparison against the previous commit, run once and
    recorded, not a check that can live in the module it compares. The sentence is
    put in the question, as `agent.run_agent` would, so the words trace to it.
    """
    age, car = Col.PCT_AGE_65_PLUS, Col.PCT_NO_VEHICLE
    named = {schemas.weight_argument(age): 2.0, schemas.weight_argument(car): 2.0}
    every_unset = {
        argument: schemas.UNSET_WEIGHT
        for argument in schemas.weight_arguments(schemas.RISK_WEIGHT_KEYS)
    }
    saved_calls, saved_question = list(CALLS), QUESTION
    forget_calls(f"Which tracts come first? {SAMPLE_PREFERENCE}.")
    try:
        return _user_ranking_results(named, every_unset)
    finally:
        forget_calls(saved_question)
        CALLS.extend(saved_calls)


def _user_ranking_results(
    named: dict[str, float], every_unset: dict[str, float]
) -> list[tuple[str, bool]]:
    """The body of `_user_ranking_checks`, run with the question in place."""
    age, car = Col.PCT_AGE_65_PLUS, Col.PCT_NO_VEHICLE
    added = (
        "answered_by", "stated_preference", "preference_trace", "weights_set",
        "unset_weights_from", "indicator_weights_used",
        "user_against_each_weighting", "pairs_note",
    )

    plain = risk_scenario()
    unset = risk_scenario(**every_unset)
    theirs = risk_scenario(preference=SAMPLE_PREFERENCE, **named)
    misspelled = risk_scenario(preference=SAMPLE_PREFERENCE, weight_pct_elderly=2.0)
    negative = risk_scenario(
        preference=SAMPLE_PREFERENCE, **{schemas.weight_argument(age): -0.5}
    )
    unworded = risk_scenario(**named)

    compared_plain = compare_scenarios()
    compared_unset = compare_scenarios(**every_unset)
    compared = compare_scenarios(preference=SAMPLE_PREFERENCE, **named)
    rows = {row["preset"]: row for row in compared.get("rows", [])}
    plain_rows = {row["preset"]: row for row in compared_plain["rows"]}
    pairs = {item["against"]: item for item in compared.get("user_against_each_weighting", [])}
    user_top = rows.get(schemas.USER_WEIGHTING, {}).get("top_geoids", {}).get("listed", [])

    used = theirs.get("indicator_weights_used", {})
    others = [name for name in VULNERABILITY_INDICATORS if name not in (age, car)]
    sizes = {
        "risk_scenario": len(json.dumps(theirs, default=str)),
        "compare_scenarios": len(json.dumps(compared, default=str)),
    }
    print(f"  under {SAMPLE_PREFERENCE!r}: preset={theirs.get('preset')} "
          f"origin={theirs.get('weights_origin')}")
    print(f"  indicator weights used: {used}")
    print(f"  top {len(user_top)} under the person's words: {user_top}")
    for name, item in pairs.items():
        print(f"    against {name}: drops {item['dropped_count']}, adds "
              f"{item['added_count']}, shares {item['shared_count']}")
    print(f"  serialised sizes: {sizes}")

    return [
        ("every weight at the sentinel is the same risk_scenario call as no weight at all",
         json.dumps(unset, default=str) == json.dumps(plain, default=str)),
        ("with no weight set, risk_scenario adds no field",
         not any(key in plain for key in added)),
        ("every weight at the sentinel is the same compare_scenarios call as none",
         json.dumps(compared_unset, default=str) == json.dumps(compared_plain, default=str)),
        ("with no weight set, compare_scenarios compares the named weightings alone",
         compared_plain["weightings_compared"] == len(WEIGHT_PRESETS)
         and not any(key in compared_plain for key in added)),
        ("under the person's words the ranking is the person's weighting, sourced to them",
         theirs.get("preset") == schemas.USER_WEIGHTING
         and theirs.get("weights_origin") == schemas.USER_WEIGHTING
         and theirs.get("weights_origin_note") == SAMPLE_PREFERENCE
         and theirs.get("stated_preference") == SAMPLE_PREFERENCE),
        ("the result says at its top level who stated the words",
         theirs.get("answered_by") == "question"
         and compared.get("answered_by") == "question"
         and rows.get(schemas.USER_WEIGHTING, {}).get("answered_by") == "question"),
        ("both risk tools report the same weights used, so either one can be quoted",
         compared.get("indicator_weights_used") == used
         and compared.get("component_weights") == theirs.get("component_weights")),
        ("the two indicators their words name carry the most weight in the index",
         bool(used) and min(used[age], used[car]) > max(used[name] for name in others)),
        ("the objective weights stay the named weighting's, as their words named none",
         theirs.get("component_weights") == plain["component_weights"]
         and theirs.get("unset_weights_from") == DEFAULT_PRESET.name),
        ("their weighting reaches the ranking rather than the prose",
         theirs.get("ranking") != plain["ranking"]),
        ("the weights they set are reported as set",
         theirs.get("weights_set") == {age: 2.0, car: 2.0}),
        ("a misspelled weight is refused by name rather than ranked without it",
         misspelled.get("error") == "invalid weighting"
         and "weight_pct_elderly" in misspelled.get("detail", "")),
        ("a negative weight is refused before anything is ranked",
         negative.get("error") == "invalid weighting"
         and "negative weight" in negative.get("detail", "")),
        ("weights with no sentence are refused, so no ranking is credited to nobody",
         unworded.get("error") == "invalid weighting"),
        ("the comparison adds the person's weighting as one more row, sourced to them",
         compared.get("weightings_compared") == len(WEIGHT_PRESETS) + 1
         and rows.get(schemas.USER_WEIGHTING, {}).get("origin") == schemas.USER_WEIGHTING),
        ("it is set beside every named weighting, one pair at a time",
         set(pairs) == set(PRESETS)),
        ("each pair's dropped units are the named list minus the person's, in rank order",
         all(
             pairs[name]["dropped"]["listed"]
             == [geoid for geoid in rows[name]["top_geoids"]["listed"] if geoid not in user_top]
             and pairs[name]["added"]["listed"]
             == [geoid for geoid in user_top if geoid not in rows[name]["top_geoids"]["listed"]]
             for name in pairs
         )
         and len(pairs) > 0),
        ("dropped plus shared accounts for each named weighting's whole list",
         all(
             pairs[name]["dropped_count"] + pairs[name]["shared_count"]
             == len(rows[name]["top_geoids"]["listed"])
             for name in pairs
         )),
        ("adding the person's row changes no named weighting's own list",
         all(
             rows[name]["top_geoids"] == plain_rows[name]["top_geoids"]
             for name in plain_rows
         )),
        ("no result under the person's words carries a coordinate",
         not coordinate_faults(as_sent(theirs)) and not coordinate_faults(as_sent(compared))),
        ("no result under the person's words is large enough to crowd the run",
         max(sizes.values()) < 24_000),
    ]


def _self_check() -> int:
    print("TOOLS -- the eleven names in contracts.TOOL_NAMES\n")

    # A harness's scripted replies are set aside for the whole suite: a shell that
    # still names one would otherwise answer the suite's own questions from it.
    # `_scripted_checks` names a script of its own and puts this one back.
    scripted = os.environ.pop(SCRIPTED_REPLIES_ENV, None)
    try:
        return _run_checks()
    finally:
        if scripted is not None:
            os.environ[SCRIPTED_REPLIES_ENV] = scripted


def _run_checks() -> int:
    """Every check, in order, with the environment `_self_check` prepared."""
    global ELICIT
    # `SAMPLE_ARGUMENTS` calls every tool, `ask_user_preferences` among them, and
    # this suite is run from a real terminal as often as not. Closed here and
    # reopened by `_elicitation_checks`, which drives the prompt with a scripted
    # reader -- so the interactive path is exercised rather than merely skipped.
    ELICIT = False
    try:
        results = _every_result()
    finally:
        ELICIT = True

    checks = _surface_checks(results)
    print()
    checks += _fault_checks()
    print()
    checks += _serialisation_checks(results)
    print()
    checks += _coordinate_guard_checks()
    print()
    checks += _provenance_checks(results)
    print()
    checks += _shaping_checks()
    print()
    checks += _agreement_checks()
    print()
    checks += _tradeoff_checks()
    print()
    checks += _weighting_checks()
    print()
    checks += _elicitation_checks()
    print()
    checks += _user_weighting_checks()
    print()
    checks += _free_text_checks()
    print()
    checks += _scripted_checks()
    print()
    checks += _boundary_refusal_checks()
    print()
    checks += _user_ranking_checks()
    checks += _map_export_checks()
    checks += _live_map_checks()
    print()
    checks += _omission_checks()
    print()
    checks += _refusal_checks()
    print()
    checks += _pending_checks(results)
    print()
    checks += _log_checks()
    print()
    checks += _cache_checks()
    print()
    checks += verify.discipline_checks(sys.modules[__name__])

    # The three modules between a question and these tools carry no `--check` of
    # their own: `agent` is the loop, `llm_client` is the only file that talks to a
    # model, and `trace` renders what the loop logged. They are held to the same
    # scans here rather than nowhere, because an annotation nothing scans is an
    # annotation that rots -- which is how all three came to have none.
    from . import agent, llm_client, trace

    for module in (agent, llm_client, trace):
        print()
        print(f"the loop's collaborators -- {module.__name__}:")
        checks += verify.discipline_checks(module)
    return verify.report(checks)


if __name__ == "__main__":
    raise SystemExit(_self_check() if "--check" in sys.argv[1:] else main())
