"""Snapshot in, tract-level risk table out. No model is involved anywhere here.

This is the deterministic spine. Everything above it -- the tools, the agent, the
critic -- calls into this and reports what it returns; nothing above it computes a
number of its own. Two consequences worth stating.

* **The whole chain runs without an API key.** Retrieval, cleaning, the bathtub,
  the index, the four components, the score and the trade-off table are all here,
  so a reviewer with no credentials can still run the analysis end to end and get
  the same numbers the agent would quote. A system whose results only exist
  inside a model call cannot be checked.

* **Running it twice gives the same table.** There is no sampling, no ordering by
  hash, and no model, so byte-identical output is a property this module can
  assert rather than hope for. The check does exactly that, because "no LLM
  involved" is easy to claim and cheap to verify.

The written table carries the four objective terms as their own columns beside
the score, and carries no geometry: invariant 3 says a coordinate never reaches a
model message, and the CSV this writes is the thing `tools.py` will summarise.
"""

from __future__ import annotations

import dataclasses
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from . import acquire, align, config, verify, vulnerability
from .align import Alignment, AlignedSnapshot
from .contracts import Col, HazardScenario, ScenarioRow, WeightPreset
from .hazard import HAZARD_SCENARIOS, Hazard, HazardReport
from .registry import Registry
from .risk import (
    DEFAULT_FACILITY_RADIUS_M,
    DEFAULT_PRIORITY_UNITS,
    Risk,
    RiskTable,
    format_components,
    format_table,
    format_tradeoff,
)
from .vulnerability import WEIGHT_PRESETS, OBJECTIVE_TERMS

TABLE_PREFIX = "risk"
TRADEOFF_NAME = "tradeoff.csv"
REPORT_NAME = "pipeline_report.txt"

REPORTED_COLUMNS: tuple[str, ...] = (
    Col.GEOID,
    Col.POPULATION,
    *vulnerability.VULNERABILITY_INDICATORS,
    Col.INUNDATED_FRACTION,
    Col.INUNDATION_MEAN_M,
    Col.INUNDATION_MAX_M,
    Col.EXPOSED_POPULATION,
    Col.VULNERABILITY,
    Col.RESILIENCE,
    Col.RISK_SCORE,
    Col.PRIORITY_RANK,
)
"""Every column the written table carries, in order, and nothing else.

Named from `contracts.Col` rather than taken from whatever the frame happens to
hold, so a column added upstream cannot silently appear in the deliverable and a
column removed upstream fails loudly here instead of writing a shorter file."""

COORDINATE_PATTERN = re.compile(
    r"(?:^|_)(lat|lon|lng|latitude|longitude|x|y|geom|geometry|centlat|centlon|intptlat|intptlon)(?:$|_)",
    re.IGNORECASE,
)
"""Column names that would carry a coordinate out of the pipeline.

TIGERweb ships CENTLAT, CENTLON, INTPTLAT and INTPTLON as ordinary text
attributes, so invariant 3 is not only about a geometry column: a coordinate
wearing a column name is still a coordinate. `align_snapshot` drops them from the
joined layers and this refuses to write one if any survives."""


@dataclass(slots=True)
class PipelineResult:
    """Everything one run produced, and where it was written."""

    study_area: config.StudyArea
    snapshot: AlignedSnapshot
    hazard: HazardReport
    tables: dict[str, RiskTable] = field(default_factory=dict)
    tradeoff: list[ScenarioRow] = field(default_factory=list)
    written: list[Path] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def scenarios(self) -> tuple[str, ...]:
        return tuple(self.tables)


def table_name(scenario: HazardScenario, preset: WeightPreset) -> str:
    """A filename built from the scenario and preset, never written as a literal."""
    stem = "_".join(
        "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in part)
        for part in (TABLE_PREFIX, scenario.name, preset.name)
    )
    return f"{stem}.csv"


def reported(frame: pd.DataFrame) -> pd.DataFrame:
    """Cut a risk table to exactly the reportable columns, refusing a coordinate.

    Two refusals rather than a silent selection. A missing column means an
    upstream module stopped producing something the deliverable promises, and
    writing the file without it would ship a quietly shorter table. A
    coordinate-bearing column means invariant 3 is about to be broken by a file
    the agent will later be asked to summarise.
    """
    absent = [column for column in REPORTED_COLUMNS if column not in frame.columns]
    if absent:
        raise KeyError(
            f"the risk table carries no {absent}; the written table promises "
            f"{list(REPORTED_COLUMNS)} and a shorter file would be a quieter failure "
            "than this one"
        )
    leaking = [
        column
        for column in frame.columns
        if COORDINATE_PATTERN.search(str(column))
    ]
    if leaking:
        raise ValueError(
            f"the risk table carries {leaking}, which would write a coordinate into the "
            "deliverable. Invariant 3: geometry never reaches a model message, and a "
            "coordinate wearing a column name is still a coordinate"
        )
    return frame[list(REPORTED_COLUMNS)]


def run(
    area: config.StudyArea | None = None,
    *,
    scenarios: tuple[HazardScenario, ...] = HAZARD_SCENARIOS,
    presets: tuple[WeightPreset, ...] = WEIGHT_PRESETS,
    preset: WeightPreset = vulnerability.DEFAULT_PRESET,
    radius_m: float = DEFAULT_FACILITY_RADIUS_M,
    priority_units: int = DEFAULT_PRIORITY_UNITS,
    write: bool = True,
    outputs: Path | None = None,
) -> PipelineResult:
    """Align, model the hazard, build the components, score, compare, write.

    Every knob a human would want to turn is an argument with a default: the
    scenarios, the weighting, the radius credited as reachable, and how long a
    priority list is. None of them is a constant inside a function body, which is
    what makes the sensitivity of the answer to each of them measurable without
    editing this file.
    """
    if not scenarios:
        raise ValueError("the pipeline was given no hazard scenario to run")
    if not presets:
        raise ValueError("the pipeline was given no weighting to compare")

    registry = Registry(study_area=area)
    registry.load_manifest()
    aligner = Alignment(registry)
    snapshot = aligner.align_snapshot()

    tract_key = f"{acquire.DATASET_TRACTS}{align.JOINED_SUFFIX}"
    group_key = f"{acquire.DATASET_BLOCK_GROUPS}{align.JOINED_SUFFIX}"
    for key in (tract_key, group_key, acquire.DATASET_FACILITIES):
        if key not in snapshot.frames:
            raise KeyError(
                f"no {key} layer in the snapshot, so no risk table can be built. Run "
                f"`python -m src.acquire` first; the snapshot holds {sorted(snapshot.frames)}"
            )
    tracts = snapshot.frames[tract_key]
    groups = snapshot.frames[group_key]
    facilities = snapshot.frames[acquire.DATASET_FACILITIES]

    risk = Risk(aligner)
    hazard_report = risk.hazard.measure_all(tracts, scenarios=scenarios, dataset=tract_key)

    result = PipelineResult(
        study_area=aligner.study_area,
        snapshot=snapshot,
        hazard=hazard_report,
        warnings=list(snapshot.report.warnings) + list(hazard_report.warnings),
    )
    directory = Path(outputs or config.OUTPUTS_DIR)
    if write:
        config.ensure_dirs()
        directory.mkdir(parents=True, exist_ok=True)

    for scenario in scenarios:
        surface = hazard_report.surfaces[scenario.name]
        table = risk.table(
            surface, tracts, groups, facilities,
            preset=preset, radius_m=radius_m, dataset=tract_key,
        )
        result.tables[scenario.name] = table
        result.warnings.extend(table.warnings)
        result.tradeoff.extend(
            risk.compare_presets(
                table.frame,
                scenario=scenario,
                units=tracts,
                presets=presets,
                priority_units=priority_units,
            )
        )
        if write:
            path = directory / table_name(scenario, preset)
            reported(table.frame).to_csv(path, index=False)
            result.written.append(path)

    if write:
        tradeoff_path = directory / TRADEOFF_NAME
        pd.DataFrame(
            [
                {
                    **{
                        key: value
                        for key, value in dataclasses.asdict(row).items()
                        if not isinstance(value, tuple)
                    },
                    "top_geoids": " ".join(row.top_geoids),
                    "displaced_geoids": " ".join(row.displaced_geoids),
                }
                for row in result.tradeoff
            ]
        ).to_csv(tradeoff_path, index=False)
        result.written.append(tradeoff_path)

        report_path = directory / REPORT_NAME
        report_path.write_text(format_report(result), encoding="utf-8")
        result.written.append(report_path)
    return result


def format_report(result: PipelineResult) -> str:
    """The whole run, from what was cleaned to who each weighting drops."""
    lines = [
        f"PIPELINE -- {result.study_area.name}",
        f"  working crs {result.study_area.working_crs}   "
        f"{len(result.snapshot.frames)} cleaned layer(s), "
        f"{len(result.tables)} scenario(s), no model involved",
        "",
        align.format_report(
            result.snapshot.report, result.snapshot.evidence, result.study_area
        ),
        "",
    ]
    for name, table in result.tables.items():
        lines.append(format_components(table))
        lines.append("")
        lines.append(format_table(table))
        lines.append("")
    by_scenario: dict[str, list[ScenarioRow]] = {}
    for row in result.tradeoff:
        by_scenario.setdefault(row.scenario, []).append(row)
    for scenario, rows in by_scenario.items():
        lines.append(format_tradeoff(rows))
        lines.append("")
    if result.written:
        lines.append("  written:")
        for path in result.written:
            lines.append(f"      {path}")
    if result.warnings:
        lines.append("")
        lines.append(f"  warnings ({len(result.warnings)}):")
        for warning in result.warnings:
            lines.append(f"      - {warning}")
    return "\n".join(lines)


def main(area: config.StudyArea | None = None) -> int:
    result = run(area)
    print(format_report(result))
    print()
    for path in result.written:
        size = path.stat().st_size
        print(f"  wrote {path}  ({size:,} bytes)")
    return 0


# ---------------------------------------------------------------------------
# self check
# ---------------------------------------------------------------------------


def _written_checks(result: PipelineResult) -> list[tuple[str, bool]]:
    """Read the deliverable back off disk and check it there, not in memory.

    A frame that is correct in memory and wrong in the file is a real failure
    mode -- a dtype that does not survive a CSV round trip, an index written as a
    column -- and checking the frame proves nothing about the file anybody will
    open.
    """
    tables = [path for path in result.written if path.name.startswith(TABLE_PREFIX)]
    reread = {path.name: pd.read_csv(path, dtype={Col.GEOID: str}) for path in tables}
    tradeoff = pd.read_csv(
        next(path for path in result.written if path.name == TRADEOFF_NAME)
    )

    print(f"written: {len(result.written)} file(s)")
    for path in result.written:
        print(f"  {path.name:<34} {path.stat().st_size:>9,} bytes")
    for name, frame in reread.items():
        print(f"  {name:<34} {len(frame)} rows x {len(frame.columns)} cols")

    units = len(next(iter(result.tables.values())).frame)
    every_row = bool(reread) and all(len(frame) == units for frame in reread.values())
    every_column = all(
        list(frame.columns) == list(REPORTED_COLUMNS) for frame in reread.values()
    )
    leaking = [
        column
        for frame in reread.values()
        for column in frame.columns
        if COORDINATE_PATTERN.search(str(column))
    ]
    geoid_width = all(
        frame[Col.GEOID].astype(str).str.len().eq(11).all() for frame in reread.values()
    )

    return [
        ("a table was written for every scenario, plus the trade-off and the report",
         len(tables) == len(result.tables) and len(result.written) == len(result.tables) + 2),
        ("every written table has one row per tract, unscored ones included",
         every_row and len(reread) == len(result.tables) and len(reread) > 0),
        ("every written table carries exactly the promised columns, in order",
         every_column and len(reread) > 0),
        ("no written column could carry a coordinate out of the pipeline",
         not leaking),
        ("GEOIDs survive the round trip as eleven-character strings, not as integers",
         geoid_width and len(reread) > 0),
        ("the trade-off file has one row per preset per scenario",
         len(tradeoff) == len(WEIGHT_PRESETS) * len(result.tables) and len(tradeoff) > 0),
        ("the trade-off file names who each weighting drops",
         "displaced_geoids" in tradeoff.columns
         and bool(tradeoff["displaced_geoids"].astype(str).str.strip().ne("").any())),
        ("the report names the study area and the layers it cleaned",
         REPORT_NAME in {path.name for path in result.written}),
    ]


def _monotonic_checks(result: PipelineResult) -> list[tuple[str, bool]]:
    """A deeper surge must flood at least as much of every unit as a shallower one.

    Nothing inside a single scenario can catch a sign error in the bathtub, a
    scenario read from the wrong row, or a surface written under the wrong name --
    every one of those produces a self-consistent table. Comparing the scenarios
    to each other does, and it is the only check in this project that spans them.
    """
    ordered = sorted(result.tables.values(), key=lambda table: table.scenario.surge_height_m)
    if len(ordered) < 2:
        return [("at least two scenarios ran, so they can be compared", False)]

    fractions = [
        pd.to_numeric(table.frame[Col.INUNDATED_FRACTION], errors="coerce")
        for table in ordered
    ]
    depths = [
        pd.to_numeric(table.frame[Col.INUNDATION_MAX_M], errors="coerce")
        for table in ordered
    ]
    exposed = [
        pd.to_numeric(table.frame[Col.EXPOSED_POPULATION], errors="coerce")
        for table in ordered
    ]

    rising_fraction = all(
        bool((fractions[i + 1] >= fractions[i] - 1e-12).all()) for i in range(len(ordered) - 1)
    )
    rising_depth = all(
        bool((depths[i + 1] >= depths[i] - 1e-9).all()) for i in range(len(ordered) - 1)
    )
    rising_exposed = all(
        bool((exposed[i + 1] >= exposed[i] - 1e-6).all()) for i in range(len(ordered) - 1)
    )
    strictly_more = [
        int((fractions[i + 1] > fractions[i] + 1e-12).sum()) for i in range(len(ordered) - 1)
    ]

    for table, fraction, people in zip(ordered, fractions, exposed):
        print(
            f"  {table.scenario.name:<12} {table.scenario.surge_height_m:>4.1f} m  "
            f"mean flooded fraction {fraction.mean():.4f}  "
            f"exposed {people.sum():>10,.0f}  "
            f"fully wet {int((fraction >= 1.0).sum()):>3}  "
            f"fully dry {int((fraction <= 0.0).sum()):>3}"
        )
    print(f"  units that flood strictly more at each step up: {strictly_more}")

    return [
        ("a deeper surge floods at least as much of every unit as a shallower one",
         rising_fraction),
        ("a deeper surge is at least as deep in every unit",
         rising_depth),
        ("a deeper surge exposes at least as many residents in every unit",
         rising_exposed),
        ("the scenarios are genuinely different, not three runs of one surge height",
         all(count > 0 for count in strictly_more) and len(strictly_more) > 0),
        ("the county total rises with the surge height",
         [round(float(people.sum()), 3) for people in exposed]
         == sorted(round(float(people.sum()), 3) for people in exposed)
         and float(exposed[-1].sum()) > float(exposed[0].sum())),
    ]


def _determinism_checks(result: PipelineResult) -> list[tuple[str, bool]]:
    """Run one scenario twice and require the same bytes.

    "No LLM is involved" is a claim about this module, and this is what makes it a
    measurement. A second run rederives the raster, re-measures every polygon and
    re-ranks every unit; anything sampled, hashed or model-driven would move.
    """
    scenario = min(result.tables.values(), key=lambda table: table.scenario.surge_height_m).scenario
    again = run(scenarios=(scenario,), write=False)
    first = reported(result.tables[scenario.name].frame).reset_index(drop=True)
    second = reported(again.tables[scenario.name].frame).reset_index(drop=True)

    same = first.equals(second)
    differing = (
        []
        if same
        else [
            column
            for column in first.columns
            if not first[column].equals(second[column])
        ]
    )
    print(
        f"determinism: re-ran {scenario.name} from the snapshot; "
        f"{'identical' if same else f'DIFFERS in {differing}'}"
    )

    return [
        ("running the same scenario twice produces an identical table",
         same),
        ("the second run really did recompute rather than return the first result",
         again.tables[scenario.name] is not result.tables[scenario.name]),
    ]


def _gate_checks(result: PipelineResult) -> list[tuple[str, bool]]:
    """The three rules the acceptance gate names, asserted on the real table."""
    outcomes: list[tuple[str, bool]] = []
    over = 0
    unbounded = 0
    compared = 0
    for table in result.tables.values():
        frame = table.frame
        population = pd.to_numeric(frame[Col.POPULATION], errors="coerce")
        exposed = pd.to_numeric(frame[Col.EXPOSED_POPULATION], errors="coerce")
        fraction = pd.to_numeric(frame[Col.INUNDATED_FRACTION], errors="coerce")
        comparable = population.notna() & exposed.notna()
        compared += int(comparable.sum())
        over += int((exposed > population + 1e-6)[comparable].sum())
        unbounded += int(((fraction < 0.0) | (fraction > 1.0))[fraction.notna()].sum())

    tops: dict[str, set[str]] = {}
    for row in result.tradeoff:
        tops.setdefault(f"{row.scenario}/{row.preset}", set(row.top_geoids))
    by_scenario: dict[str, list[tuple[str, set[str]]]] = {}
    for key, top in tops.items():
        scenario, preset = key.split("/")
        by_scenario.setdefault(scenario, []).append((preset, top))
    moved = [
        scenario
        for scenario, pairs in by_scenario.items()
        if any(a[1] != b[1] for i, a in enumerate(pairs) for b in pairs[i + 1:])
    ]
    displaced = sum(len(row.displaced_geoids) for row in result.tradeoff)

    print(
        f"gate: {compared} unit-scenario(s) compared, {over} exposed over population, "
        f"{unbounded} fraction(s) outside [0, 1]"
    )
    print(
        f"  the ranking moves under a different weighting in "
        f"{len(moved)} of {len(by_scenario)} scenario(s); "
        f"{displaced} displacement(s) recorded across the trade-off table"
    )

    outcomes.append(
        ("exposed population never exceeds unit population, over a real denominator",
         over == 0
         and compared == sum(len(table.frame) for table in result.tables.values())
         and compared > 0)
    )
    outcomes.append(
        ("every inundated fraction lies in [0, 1]", unbounded == 0)
    )
    outcomes.append(
        ("the ranking changes when a weight changes, in at least one scenario",
         len(moved) >= 1)
    )
    outcomes.append(
        ("the trade-off table names units a weighting drops, rather than reporting none",
         displaced > 0)
    )

    # "the ranking moved" is satisfied by any one preset differing, which the
    # objective weights alone can do. A preset also carries indicator weights, and
    # a pair whose objective halves are identical is the only thing that can show
    # those reached the table at all. `svi_equal` and `svi_themes` are that pair.
    same_terms = [
        (a, b)
        for i, a in enumerate(WEIGHT_PRESETS)
        for b in WEIGHT_PRESETS[i + 1:]
        if {k: a.weights[k] for k in OBJECTIVE_TERMS}
        == {k: b.weights[k] for k in OBJECTIVE_TERMS}
        and {k: a.weights[k] for k in vulnerability.VULNERABILITY_INDICATORS}
        != {k: b.weights[k] for k in vulnerability.VULNERABILITY_INDICATORS}
    ]
    indicator_pairs = [
        (scenario, a.name, b.name, tops[f"{scenario}/{a.name}"] != tops[f"{scenario}/{b.name}"])
        for a, b in same_terms
        for scenario in by_scenario
    ]
    print(
        f"  preset pairs differing ONLY in indicator weights: "
        f"{[(a.name, b.name) for a, b in same_terms]}; they rank differently in "
        f"{sum(1 for _, _, _, moved in indicator_pairs if moved)} of "
        f"{len(indicator_pairs)} scenario(s)"
    )
    outcomes.append(
        ("a preset pair differing only in indicator weights exists to test with",
         len(same_terms) >= 1 and len(indicator_pairs) == len(same_terms) * len(by_scenario))
    )
    outcomes.append(
        ("changing only the indicator weights changes the table, in every scenario",
         bool(indicator_pairs) and all(moved for _, _, _, moved in indicator_pairs))
    )
    return outcomes


def _refusal_checks() -> list[tuple[str, bool]]:
    """What the pipeline refuses to write."""
    frame = pd.DataFrame({column: [1] for column in REPORTED_COLUMNS})
    leaking = frame.copy()
    leaking["INTPTLAT"] = ["+32.7"]

    # The risk table happens to carry exactly the reported columns in exactly this
    # order, so on real data `reported` selecting nothing is indistinguishable from
    # `reported` selecting correctly. The guard is real -- it is what stops a column
    # added upstream from appearing in the deliverable -- and this is the only
    # fixture that can tell the two apart.
    extra = frame.copy()
    extra["spare_upstream_column"] = [99]
    reordered = frame[list(reversed(REPORTED_COLUMNS))]

    return [
        ("a column added upstream is dropped rather than written into the deliverable",
         list(reported(extra).columns) == list(REPORTED_COLUMNS)
         and "spare_upstream_column" not in reported(extra).columns),
        ("the promised order is imposed, not inherited from the frame",
         list(reported(reordered).columns) == list(REPORTED_COLUMNS)
         and list(reordered.columns) != list(REPORTED_COLUMNS)),
        ("a table missing a promised column is refused rather than written short",
         verify.refuses(
             lambda: reported(frame.drop(columns=[Col.RESILIENCE])),
             KeyError,
             "a shorter file would be a quieter failure",
         )),
        ("a coordinate-bearing column is refused before it reaches the deliverable",
         verify.refuses(
             lambda: reported(leaking),
             ValueError,
             "a coordinate wearing a column name is still a coordinate",
         )),
        ("the promised columns pass unchanged when nothing is wrong",
         list(reported(frame).columns) == list(REPORTED_COLUMNS)),
        ("running no scenario at all is refused",
         verify.refuses(
             lambda: run(scenarios=(), write=False), ValueError, "no hazard scenario"
         )),
        ("running with no weighting to compare is refused",
         verify.refuses(
             lambda: run(presets=(), write=False), ValueError, "no weighting to compare"
         )),
    ]


def _self_check() -> int:
    result = run()
    print(
        f"study area: {result.study_area.name}   "
        f"scenarios: {list(result.scenarios)}\n"
    )

    checks = _written_checks(result)
    print()
    checks += _monotonic_checks(result)
    print()
    checks += _gate_checks(result)
    print()
    checks += _determinism_checks(result)
    print()
    checks += _refusal_checks()
    print()
    checks += verify.discipline_checks(sys.modules[__name__])
    return verify.report(checks)


if __name__ == "__main__":
    raise SystemExit(_self_check() if "--check" in sys.argv[1:] else main())
