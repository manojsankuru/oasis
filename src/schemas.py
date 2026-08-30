"""Tool argument models, and the specs the model is offered.

One flat pydantic model per name in `contracts.TOOL_NAMES`, and the OpenAI-shaped
specs built from them. Nothing here executes anything: `tools.py` holds the
functions, `agent.execute_tool` validates arguments here and then calls there,
and `tools.surface_faults` is what makes a disagreement between the two loud at
startup rather than at the sixth iteration of a run.

**Invariant 4, and why it is stricter here than it reads.** The rule is that a
tool argument is `str`, `float`, `int` or `bool`, because a nested pydantic model
emits `$ref` and `$defs` into `model_json_schema()` and some OpenAI-compatible
servers reject those. An optional scalar is the same failure in smaller clothes:
`float | None` emits

    {"anyOf": [{"type": "number"}, {"type": "null"}]}

with no plain `type` key at all, and a server strict enough to reject a `$ref` is
strict enough to reject that. So every field here is a plain scalar with a
default, and "the caller did not set this" is carried by a sentinel value rather
than by a union -- `UNSET_WEIGHT` for a weight, the empty string for a name. The
check asserts the absence of all three of `$ref`, `$defs` and `anyOf` on the real
serialised JSON, not on the dict, because the string is what crosses the wire.

**Nothing here is written down twice.** The eleven names come from
`contracts.TOOL_NAMES`, the weight arguments from
`contracts.VULNERABILITY_INDICATORS`, the scenario names from
`hazard.HAZARD_SCENARIOS` and the preset names from
`vulnerability.WEIGHT_PRESETS`. A sixth indicator added to the contract grows a
sixth weight argument with no edit here, and a description that lists the legal
values cannot drift from the values that are actually legal. The previous version
of this file advertised four tools whose names were in none of those places and
which nothing could run.
"""

from __future__ import annotations

import json
import sys
from typing import Any, Iterable

from pydantic import BaseModel, Field, create_model

from . import verify, vulnerability
from .contracts import TOOL_NAMES, VULNERABILITY_INDICATORS
from .hazard import HAZARD_SCENARIOS
from .vulnerability import DEFAULT_PRESET, INDICATOR_RATIONALE, WEIGHT_PRESETS

SCALAR_TYPES: frozenset[str] = frozenset({"string", "number", "integer", "boolean"})
"""The four JSON types invariant 4 permits, as `model_json_schema()` names them."""

FORBIDDEN_KEYWORDS: tuple[str, ...] = ("$ref", "$defs", "anyOf", "allOf", "oneOf")
"""Every schema keyword that means "this type is not one plain scalar".

`$ref` and `$defs` are the two the invariant names. The three combinators are the
same shape of value arriving by a different route -- a union, an intersection or a
choice -- and a property carrying one of them has no plain `type` for a server to
read. They are listed here rather than discovered so that the check tests for
them by name and reports which one appeared.
"""

DEFAULT_TOP_N = 10
MAX_TOP_N = 50
"""How many units a ranking tool returns, and the most it will return.

Bounded because the result goes into a model message: a ranking of every unit in
a large county would crowd out the run's remaining turns, and the tools report
how many units they omitted rather than trying to fit them all in.
"""

UNSET_WEIGHT = -1.0
"""What a weight argument means when the caller left it alone.

`vulnerability.normalised_weights` refuses a negative weight outright -- whether
more of something is better or worse is a direction, and it is stated in
`INDICATOR_DIRECTION` where it can be read rather than hidden in the sign of a
weight -- so a negative can never be a weighting anybody meant. That refusal is
what makes a negative usable as "leave this one at the preset's value" without
colliding with a real preference, and the check proves the refusal rather than
asserting the convention.
"""

UNSET_NAME = ""
"""What a name argument means when the caller left it alone: use the default."""

WEIGHT_ARG_PREFIX = "weight_"


def weight_argument(indicator: str) -> str:
    """The argument name that overrides one indicator's weight."""
    return f"{WEIGHT_ARG_PREFIX}{indicator}"


def weighted_indicator(argument: str) -> str:
    """The indicator an override argument names, read back off the argument."""
    return argument[len(WEIGHT_ARG_PREFIX):]


def weight_arguments() -> tuple[str, ...]:
    """One override argument per indicator, enumerated from the contract."""
    return tuple(weight_argument(name) for name in VULNERABILITY_INDICATORS)


SCENARIO_NAMES: tuple[str, ...] = tuple(item.name for item in HAZARD_SCENARIOS)
PRESET_NAMES: tuple[str, ...] = tuple(item.name for item in WEIGHT_PRESETS)


def _named(values: Iterable[str]) -> str:
    return ", ".join(values)


_SCENARIO_HELP = (
    f"Name of a surge scenario. The ones this study area carries are "
    f"{_named(SCENARIO_NAMES)}. Leave empty for the deepest one that is not the "
    "only one, or call hazard_exposure with an empty name to see the list."
)

_PRESET_HELP = (
    f"Name of a named weighting. This project defines {_named(PRESET_NAMES)}; "
    f"{DEFAULT_PRESET.name} is the default and is the one with a published origin. "
    "Leave empty to use the default. Call ask_user_preferences to see where each "
    "weighting came from before choosing one."
)

_TOP_N_HELP = (
    f"How many units to return, highest first, between 1 and {MAX_TOP_N}. "
    "The result always says how many units it omitted and why."
)


# ---------------------------------------------------------------------------
# one flat model per tool
# ---------------------------------------------------------------------------


class ListDatasetsArgs(BaseModel):
    """No arguments. The registry is a property of the run, not of the question."""


class AcquireDatasetArgs(BaseModel):
    name: str = Field(
        description=(
            "Name of the dataset to retrieve, exactly as list_datasets reports it. "
            "This performs a live request to the publishing service and rewrites "
            "that layer in the snapshot; it is not a read of what is already there."
        )
    )
    timeout_s: float = Field(
        default=60.0,
        ge=1.0,
        le=600.0,
        description=(
            "Seconds to wait for the service before giving up. Every network call "
            "in this system carries one; there is no unbounded wait."
        ),
    )


class DescribeLayerArgs(BaseModel):
    name: str = Field(
        description="Name of the dataset to describe, exactly as list_datasets reports it."
    )
    max_columns: int = Field(
        default=30,
        ge=1,
        le=200,
        description="How many columns to report. The result says how many it left out.",
    )


class DescribeAlignmentArgs(BaseModel):
    """No arguments. One snapshot was cleaned; this reports what that did."""


class HazardExposureArgs(BaseModel):
    scenario: str = Field(default=UNSET_NAME, description=_SCENARIO_HELP)
    top_n: int = Field(default=DEFAULT_TOP_N, ge=1, le=MAX_TOP_N, description=_TOP_N_HELP)


class RiskScenarioArgs(BaseModel):
    scenario: str = Field(default=UNSET_NAME, description=_SCENARIO_HELP)
    preset: str = Field(default=UNSET_NAME, description=_PRESET_HELP)
    top_n: int = Field(default=DEFAULT_TOP_N, ge=1, le=MAX_TOP_N, description=_TOP_N_HELP)


class CompareScenariosArgs(BaseModel):
    scenario: str = Field(default=UNSET_NAME, description=_SCENARIO_HELP)
    priority_units: int = Field(
        default=DEFAULT_TOP_N,
        ge=1,
        le=MAX_TOP_N,
        description=(
            "How long each weighting's priority list is. The comparison is over "
            "lists of this length, so who a weighting drops depends on it."
        ),
    )


class AskUserPreferencesArgs(BaseModel):
    question: str = Field(
        description=(
            "The preference you need settled before you can decide, in one sentence. "
            "Ask this BEFORE producing a priority ordering, not after: which units "
            "come first is a value judgement and the weighting is where it is made."
        )
    )


class RunSpatialCodeArgs(BaseModel):
    code: str = Field(
        description=(
            "Python source to execute against the cleaned layers. Use this only for "
            "a question the other tools cannot answer; anything they do answer is "
            "already verified and a second computation of it is a second answer."
        )
    )
    timeout_s: float = Field(
        default=60.0,
        ge=1.0,
        le=300.0,
        description="Seconds the code may run before it is killed.",
    )


class ValidateAnswerArgs(BaseModel):
    answer: str = Field(
        description=(
            "The draft answer to check. Every number in it is traced back to a "
            "logged tool result; the ones that cannot be traced come back as findings."
        )
    )


VulnerabilityIndexArgs = create_model(
    "VulnerabilityIndexArgs",
    __doc__=(
        "The weighted percentile index. Weights are arguments, one per indicator, "
        "generated from contracts.VULNERABILITY_INDICATORS so that the contract and "
        "the model-visible surface cannot disagree about what is weightable."
    ),
    preset=(str, Field(default=UNSET_NAME, description=_PRESET_HELP)),
    top_n=(int, Field(default=DEFAULT_TOP_N, ge=1, le=MAX_TOP_N, description=_TOP_N_HELP)),
    **{
        weight_argument(name): (
            float,
            Field(
                default=UNSET_WEIGHT,
                description=(
                    f"Relative weight on {name}. {INDICATOR_RATIONALE[name]} "
                    f"Weights are relative and are scaled to sum to one, so 2 and 1 "
                    f"mean the same as 0.5 and 0.25. Leave at {UNSET_WEIGHT} to keep "
                    "the chosen weighting's own value for this indicator; a negative "
                    "weight is refused by the index itself, which is why one can mean "
                    "'unset' here."
                ),
            ),
        )
        for name in VULNERABILITY_INDICATORS
    },
)


TOOL_ARG_MODELS: dict[str, type[BaseModel]] = {
    "list_datasets": ListDatasetsArgs,
    "acquire_dataset": AcquireDatasetArgs,
    "describe_layer": DescribeLayerArgs,
    "describe_alignment": DescribeAlignmentArgs,
    "hazard_exposure": HazardExposureArgs,
    "vulnerability_index": VulnerabilityIndexArgs,
    "risk_scenario": RiskScenarioArgs,
    "compare_scenarios": CompareScenariosArgs,
    "ask_user_preferences": AskUserPreferencesArgs,
    "run_spatial_code": RunSpatialCodeArgs,
    "validate_answer": ValidateAnswerArgs,
}
"""Argument model per tool, in `contracts.TOOL_NAMES` order.

Written out rather than generated, because the mapping from a name to its
arguments is the thing this file exists to state. What is checked, rather than
trusted, is that the keys are exactly `TOOL_NAMES` and in that order -- a tool
added here and not to the contract, or the reverse, fails the check rather than
reaching the model."""


TOOL_DESCRIPTIONS: dict[str, str] = {
    "list_datasets": (
        "List every retrieved dataset with its source URL, retrieval timestamp, "
        "vintage, feature count, licence, declared and working CRS, and whether "
        "its retrieval was degraded. Call this first: it is the only way to learn "
        "which layer names the other tools accept, and it is where the citations "
        "for your answer come from."
    ),
    "acquire_dataset": (
        "Retrieve one dataset again, live, from the service that publishes it, and "
        "re-register it with fresh provenance. Use this when list_datasets shows a "
        "layer as degraded or missing, or when the data may be stale. This makes a "
        "network request and takes time; it is not needed to read data that is "
        "already there."
    ),
    "describe_layer": (
        "Report one layer's columns, dtypes, null counts, CRS, feature count and "
        "provenance. Columns whose names could carry a coordinate are withheld and "
        "counted rather than listed. Use this before assuming a column exists."
    ),
    "describe_alignment": (
        "Report what the cleaning stage did to the snapshot: what was reprojected "
        "and from what to what, how many geometries were repaired or dropped, which "
        "sentinel codes were removed and how many of each, which GEOIDs matched and "
        "which did not, how coarse and fine granularities were reconciled and with "
        "what error against the published totals, and every warning raised. Each "
        "count comes with the denominator it was taken over, so a zero can be told "
        "apart from a step that never ran."
    ),
    "hazard_exposure": (
        "Report inundation per unit for one surge scenario: the flooded fraction, "
        "mean and maximum depth, and the exposed resident count, ranked worst first. "
        "Also reports the county totals at both granularities the estimate was built "
        "from, and how far apart they are."
    ),
    "vulnerability_index": (
        "Compute the weighted percentile vulnerability index over the census "
        "indicators and return the highest-scoring units. The weighting is an "
        "argument: name a preset, or override any indicator's weight directly. "
        "Reports which units could not be scored and why."
    ),
    "risk_scenario": (
        "Combine hazard, exposed population, vulnerability and resilience into one "
        "priority ranking for a named scenario under a named weighting. Every "
        "component is reported as its own number beside the score, and the score is "
        "county-relative: it says which units here are worst, never whether here is bad."
    ),
    "compare_scenarios": (
        "Run every named weighting against one scenario and report the trade-off: "
        "who each weighting prioritises, how many residents and how many vulnerable "
        "residents that covers, and WHICH UNITS EACH WEIGHTING DROPS that another "
        "would have prioritised. Use this whenever the answer depends on a value "
        "judgement, which is whenever it is a priority ordering."
    ),
    "ask_user_preferences": (
        "Ask the human which weighting to use, and get back every available "
        "weighting with its weights, where it came from, and the URL of its source. "
        "Call this BEFORE deciding a priority order, not after: which communities "
        "come first depends on a value judgement that is not yours to make."
    ),
    "run_spatial_code": (
        "Write and execute Python against the cleaned layers, and get back stdout, "
        "stderr and the full traceback on failure. For questions the other tools do "
        "not answer. Do not use it to recompute a number another tool already returned."
    ),
    "validate_answer": (
        "Check a draft answer before you give it: every number in it is traced back "
        "to a logged tool result and the untraceable ones come back as findings. Run "
        "this on your final answer."
    ),
}


PENDING_SUFFIX = (
    " NOT AVAILABLE ON THIS RUN -- the module that backs it has not been built yet. "
    "Calling it returns an error and wastes a turn. Answer without it."
)
"""Appended to a pending tool's description so the model learns it from the spec.

The alternative -- letting the model discover it by calling the tool -- costs one
of a small number of iterations to learn something the spec could have said.
Which tools are pending is decided in `tools.pending_tools()` by probing for the
backing module, never by a list written down anywhere."""


# ---------------------------------------------------------------------------
# specs
# ---------------------------------------------------------------------------


def parameters(model: type[BaseModel]) -> dict[str, Any]:
    """One model's JSON Schema, stripped of the titles pydantic adds.

    The titles are removed because they are noise in a model message -- a `title`
    of "Top N" beside a `description` that says the same thing in a sentence -- and
    because their absence makes the emitted schema short enough to read in the gate
    output, which is where anybody checks it for a `$ref`.
    """
    schema = model.model_json_schema()
    schema.pop("title", None)
    schema.pop("description", None)
    schema.setdefault("properties", {})
    for prop in schema["properties"].values():
        prop.pop("title", None)
    return schema


def build_tool_specs(pending: Iterable[str] = ()) -> list[dict[str, Any]]:
    """The specs the model is offered, one per advertised tool.

    `pending` names tools whose backing module is absent on this run; their
    description says so. It is a parameter rather than a lookup because this
    module must not import `tools`, which imports this one.
    """
    unavailable = set(pending)
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": TOOL_DESCRIPTIONS[name]
                + (PENDING_SUFFIX if name in unavailable else ""),
                "parameters": parameters(model),
            },
        }
        for name, model in TOOL_ARG_MODELS.items()
    ]


TOOL_SPECS: list[dict[str, Any]] = build_tool_specs()
"""The specs with nothing marked pending. `tools.tool_specs()` is what the agent
sends; this is the base form, and the check holds both to the same rules."""


def serialised(specs: list[dict[str, Any]]) -> str:
    """The specs as the string that crosses the wire.

    Invariant 4 is checked on this rather than on the dict, because a `$ref`
    introduced by a pydantic version change would be a nested dict that a shallow
    scan of `properties` would miss and that `json.dumps` cannot hide.
    """
    return json.dumps(specs, sort_keys=True)


def forbidden_in(text: str) -> list[str]:
    """Which forbidden schema keywords appear in this serialised schema."""
    return [word for word in FORBIDDEN_KEYWORDS if word in text]


def properties_of(specs: list[dict[str, Any]]) -> list[tuple[str, str, dict[str, Any]]]:
    """(tool, argument, schema) for every argument of every spec."""
    return [
        (spec["function"]["name"], argument, schema)
        for spec in specs
        for argument, schema in spec["function"]["parameters"]["properties"].items()
    ]


def format_specs(specs: list[dict[str, Any]] | None = None) -> str:
    """Every spec, printed, for the acceptance gate to paste."""
    chosen = TOOL_SPECS if specs is None else specs
    return json.dumps(chosen, indent=2)


def main() -> int:
    print(format_specs())
    return 0


# ---------------------------------------------------------------------------
# self check
# ---------------------------------------------------------------------------


def _surface_checks() -> list[tuple[str, bool]]:
    """The advertised surface against the frozen one."""
    advertised = tuple(TOOL_ARG_MODELS)
    described = tuple(TOOL_DESCRIPTIONS)
    print(f"advertised: {len(advertised)} tool(s)")
    for name in advertised:
        model = TOOL_ARG_MODELS[name]
        print(f"  {name:<22} {len(model.model_fields):>2} argument(s)")
    return [
        ("every frozen tool name has an argument model, in contract order",
         advertised == TOOL_NAMES),
        ("every frozen tool name has a description, in contract order",
         described == TOOL_NAMES),
        ("no tool is advertised that the frozen contract does not name",
         not set(advertised) - set(TOOL_NAMES) and not set(described) - set(TOOL_NAMES)),
        ("every description is a real sentence rather than a placeholder",
         all(len(TOOL_DESCRIPTIONS[name]) > 60 for name in described)),
    ]


def _flatness_checks() -> list[tuple[str, bool]]:
    """Invariant 4, asserted on the real serialised JSON.

    Both spec forms are tested. The pending suffix only touches a description, but
    "only touches a description" is a claim about a code path, and the code path is
    cheap to run.
    """
    forms = {
        "plain": TOOL_SPECS,
        "with every tool marked pending": build_tool_specs(TOOL_NAMES),
    }
    found: dict[str, list[str]] = {}
    for label, specs in forms.items():
        text = serialised(specs)
        found[label] = forbidden_in(text)
        print(f"  {label}: {len(text):,} chars, forbidden keywords {found[label] or 'none'}")

    # A scan over a clean surface returns nothing whether or not it is looking, so
    # it is run against a schema that carries each forbidden keyword. Without this
    # an emptied keyword list reads exactly like a flat set of specs.
    nested = json.dumps(
        {
            "a": {"$ref": "#/$defs/Nested"},
            "b": {"anyOf": [{"type": "number"}, {"type": "null"}]},
            "c": {"allOf": [{"type": "string"}]},
            "d": {"oneOf": [{"type": "integer"}]},
        }
    )
    caught = forbidden_in(nested)
    print(f"  against a schema built to violate it, the scan finds {caught}")

    props = properties_of(TOOL_SPECS)
    typed = [(tool, argument) for tool, argument, schema in props if "type" not in schema]
    wrong = [
        (tool, argument, schema.get("type"))
        for tool, argument, schema in props
        if schema.get("type") not in SCALAR_TYPES
    ]
    undescribed = [
        (tool, argument) for tool, argument, schema in props if not schema.get("description")
    ]
    kinds = sorted({str(schema.get("type")) for _, _, schema in props})
    print(f"  {len(props)} argument(s) across {len(TOOL_SPECS)} spec(s); types seen: {kinds}")

    return [
        ("no forbidden schema keyword appears in the serialised specs, in either form",
         not any(found.values())),
        ("every argument carries a plain type rather than a combinator",
         not typed),
        ("every argument's type is one of the four scalars invariant 4 permits",
         not wrong),
        ("every argument carries a description the model can read",
         not undescribed),
        ("the scan found arguments to scan",
         len(props) > 0),
        ("the scan finds every forbidden keyword in a schema built to carry them",
         sorted(caught) == sorted(FORBIDDEN_KEYWORDS) and len(FORBIDDEN_KEYWORDS) > 0),
    ]


def _sentinel_checks() -> list[tuple[str, bool]]:
    """That an unset scalar cannot be mistaken for a value somebody meant.

    The convention is only safe because the index refuses a negative weight, so
    the refusal is exercised here rather than described. Asserting the convention
    against itself would pass whatever the index did.
    """
    unset = {name: UNSET_WEIGHT for name in VULNERABILITY_INDICATORS}
    real = {name: 1.0 for name in VULNERABILITY_INDICATORS}
    one_unset = {**real, VULNERABILITY_INDICATORS[0]: UNSET_WEIGHT}

    arguments = set(weight_arguments())
    model_fields = set(VulnerabilityIndexArgs.model_fields)
    round_trip = all(
        weighted_indicator(weight_argument(name)) == name
        for name in VULNERABILITY_INDICATORS
    )
    print(f"  weight arguments generated from the contract: {sorted(arguments)}")
    print(f"  the unset sentinel is {UNSET_WEIGHT}, and the index refuses it as a weight")

    return [
        ("one weight argument exists per indicator in the contract, and no more",
         arguments == model_fields - {"preset", "top_n"} and len(arguments) == len(VULNERABILITY_INDICATORS)),
        ("an argument name reads back as the indicator it weights",
         round_trip),
        ("the index refuses the unset sentinel as a weight, so it cannot be one",
         verify.refuses(
             lambda: vulnerability.normalised_weights(unset, VULNERABILITY_INDICATORS),
             ValueError,
             "negative weight",
         )),
        ("the index refuses a single unset weight among real ones",
         verify.refuses(
             lambda: vulnerability.normalised_weights(one_unset, VULNERABILITY_INDICATORS),
             ValueError,
             "negative weight",
         )),
        ("a weighting of real weights is accepted, so the refusal is about the sign",
         abs(sum(vulnerability.normalised_weights(real, VULNERABILITY_INDICATORS).values()) - 1.0)
         < 1e-12),
    ]


_PLACEHOLDERS: dict[type, Any] = {str: "x", float: 1.0, int: 1, bool: True}


def _from_defaults(model: type[BaseModel]) -> BaseModel:
    """Build one model from its own defaults, filling required fields minimally.

    A required field has no default to test, so it gets a placeholder of its
    declared type. What is under test is that every DEFAULT this file writes down
    is a value the field would accept -- a bound tightened without moving the
    default beside it is otherwise invisible until a model calls the tool.
    """
    supplied: dict[str, Any] = {}
    for argument, field in model.model_fields.items():
        if field.is_required():
            supplied[argument] = _PLACEHOLDERS[field.annotation]
        else:
            supplied[argument] = field.default
    return model(**supplied)


def _validation_checks() -> list[tuple[str, bool]]:
    """That the models reject what `agent.execute_tool` relies on them rejecting."""
    required = {
        name: sorted(
            argument
            for argument, field in model.model_fields.items()
            if field.is_required()
        )
        for name, model in TOOL_ARG_MODELS.items()
    }
    declared = {
        spec["function"]["name"]: sorted(
            spec["function"]["parameters"].get("required", [])
        )
        for spec in TOOL_SPECS
    }
    print(f"  required arguments per tool: { {k: v for k, v in required.items() if v} }")

    missing_required = verify.refuses(
        lambda: TOOL_ARG_MODELS["describe_layer"](),
        Exception,
        "name",
    )
    out_of_range = verify.refuses(
        lambda: TOOL_ARG_MODELS["hazard_exposure"](top_n=MAX_TOP_N + 1),
        Exception,
        "less than or equal",
    )
    unknown_ignored = TOOL_ARG_MODELS["hazard_exposure"](scenario="x", nonsense=1)

    return [
        ("the spec's required list matches the model's required fields, tool by tool",
         required == declared),
        ("at least one tool requires an argument, so the comparison is not vacuous",
         any(required.values())),
        ("a missing required argument is refused, naming the argument",
         missing_required),
        ("an out-of-range bound is refused, naming the bound",
         out_of_range),
        ("an unknown argument is dropped rather than crashing the call",
         unknown_ignored.scenario == "x" and not hasattr(unknown_ignored, "nonsense")),
        ("every declared default validates, so an optional argument is really optional",
         all(_from_defaults(model) is not None for model in TOOL_ARG_MODELS.values())),
        ("a tool with no required argument really can be called with none",
         all(
             TOOL_ARG_MODELS[name]() is not None
             for name, wanted in required.items()
             if not wanted
         )
         and any(not wanted for wanted in required.values())),
    ]


def _pending_checks() -> list[tuple[str, bool]]:
    """That marking a tool pending changes what the model is told, and only that."""
    one = TOOL_NAMES[-1]
    marked = build_tool_specs([one])
    by_name = {spec["function"]["name"]: spec for spec in marked}
    plain = {spec["function"]["name"]: spec for spec in TOOL_SPECS}
    others = [name for name in TOOL_NAMES if name != one]
    print(f"  marking {one!r} pending changed {sum(1 for n in TOOL_NAMES if by_name[n] != plain[n])} spec(s)")

    # The "no other tool changed" comparison is against `TOOL_SPECS`, which is
    # built by the same function -- so a build that marks every tool marks both
    # sides and the comparison passes. The two absolute assertions below are what
    # actually pin it: nothing is marked when nothing is pending, and only the one
    # named tool is marked when one is.
    return [
        ("a pending tool's description says so",
         PENDING_SUFFIX in by_name[one]["function"]["description"]),
        ("no tool is marked pending when nothing is pending",
         not any(
             PENDING_SUFFIX in spec["function"]["description"]
             for spec in build_tool_specs()
         )),
        ("only the named tool is marked, not every tool",
         all(
             PENDING_SUFFIX not in by_name[name]["function"]["description"]
             for name in others
         )
         and len(others) > 0),
        ("no other tool's description changes",
         all(by_name[name] == plain[name] for name in others) and len(others) > 0),
        ("a pending tool keeps its arguments, so a caller is refused rather than confused",
         by_name[one]["function"]["parameters"] == plain[one]["function"]["parameters"]),
        ("marking nothing pending marks nothing",
         build_tool_specs() == TOOL_SPECS),
    ]


def _derivation_checks() -> list[tuple[str, bool]]:
    """That the values named in a description are the values that are legal.

    A description listing the scenarios or the presets is the model's only source
    for what to pass. Written by hand it drifts, and the drift is invisible until
    the model calls a tool with a name that no longer exists.
    """
    text = serialised(TOOL_SPECS)
    scenarios_named = [item.name for item in HAZARD_SCENARIOS if item.name in text]
    presets_named = [item.name for item in WEIGHT_PRESETS if item.name in text]
    print(f"  scenario names reaching the specs: {scenarios_named}")
    print(f"  preset names reaching the specs:   {presets_named}")

    # Compared against the source tuples rather than against SCENARIO_NAMES and
    # PRESET_NAMES. Comparing a derived list to the thing it was derived from
    # passes whatever that thing holds, including a name somebody pasted in.
    return [
        ("every scenario the study area defines is named in the specs",
         len(scenarios_named) == len(HAZARD_SCENARIOS) and len(HAZARD_SCENARIOS) > 0),
        ("every weighting the project defines is named in the specs",
         len(presets_named) == len(WEIGHT_PRESETS) and len(WEIGHT_PRESETS) > 0),
        # Asserted as the phrase, not as the name. Every preset name already
        # appears in the list of legal values, so "the default's name is in the
        # help" is satisfied by that list and stays true after the sentence
        # saying WHICH one is the default has been deleted.
        ("the specs say which weighting is used when none is named",
         f"{DEFAULT_PRESET.name} is the default" in text),
        ("every indicator in the contract reaches the specs as an argument",
         all(weight_argument(name) in text for name in VULNERABILITY_INDICATORS)),
    ]


def _self_check() -> int:
    print("SCHEMAS -- flat scalar argument models, no $ref anywhere\n")
    checks = _surface_checks()
    print()
    checks += _flatness_checks()
    print()
    checks += _sentinel_checks()
    print()
    checks += _validation_checks()
    print()
    checks += _pending_checks()
    print()
    checks += _derivation_checks()
    print()
    checks += verify.discipline_checks(sys.modules[__name__])
    return verify.report(checks)


if __name__ == "__main__":
    raise SystemExit(_self_check() if "--check" in sys.argv[1:] else main())
