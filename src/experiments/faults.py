"""The robustness table: retrieval under each injected fault kind, at two rates.

    python -m src.experiments.faults           # the fixture grid
    python -m src.experiments.faults --live    # add rows from the real service

WHAT A ROW MEASURES
-------------------
One row is `SEEDS` runs of one retrieval under one `FaultConfig`. Every run goes
through `acquire.fetch_arcgis_vector`, so the retry policy, the CRS assertion and
the paging loop are the shipped ones. `src/faults.py` says where the fault is
injected and why; this module only decides what to count.

Four columns exist because "it ran" and "it was right" are different questions
and criterion RB asks the second one:

* `completed`   -- the call returned instead of raising.
* `correct`     -- it returned AND the feature count and declared CRS match the
                   clean run. `empty` and `truncated` complete without raising
                   and are wrong, which is the whole reason they are in the
                   suite. Recovery rate is computed on this column.
* `extra calls` -- mean network attempts beyond the clean run's attempt count,
                   which is measured from a clean run rather than assumed.
* `injected`    -- faults actually fired, so a row that injected nothing is
                   visible as a row that proves nothing.

WHICH ROWS ARE LIVE
-------------------
The default grid runs against `faults.local_service()` -- a real HTTP server on
loopback serving a synthetic Esri JSON FeatureSet. It is a stub SERVICE, not a
stub of the code under test: the socket, the status line, `requests`, `_request`,
the tenacity retry, `_query_features`, `_received_crs` and the ESRIJSON driver
all run for real against it. It is here because two hundred live runs against a public
service to measure a rate is an abuse of somebody else's endpoint, and because a
grid whose denominator changes when a service has a bad afternoon is not a grid.

`--live` adds rows for the real tract layer, retrieved from the service the
snapshot came from, with the expected feature count read from the snapshot's own
provenance rather than typed in. Those rows are labelled `live` in the table and
every other row is labelled `fixture`. The gate quotes both labels.

The suite never writes to `data/snapshot/`. `fetch_arcgis_vector` returns a frame
and a `Provenance` and stores nothing; registration is `acquire.acquire_*`'s job
and none of those are called here.
"""

from __future__ import annotations

import json
import statistics
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .. import acquire, config, faults
from ..contracts import FaultConfig, FaultEvent
from ..registry import Registry

RATES: tuple[float, ...] = (0.25, 0.5)
"""Two rates, per network attempt. 0.25 is a service having a bad minute; 0.5 is
one that is barely up. Both are below 1.0 on purpose -- a rate of 1.0 measures
the failure path only, and recovery is the number criterion RB asks for."""

SEEDS: int = 20
"""Runs per cell. Small, and the table prints the attempt count beside every rate
so nobody reads twenty runs as a confidence interval."""

LIVE_SEEDS: int = 6
"""Runs per live cell. Six rather than three because a content fault is only
eligible on the query call, so at rate 0.5 three seeds miss entirely about one
time in eight -- and a row that injected nothing is a row that proves nothing,
however green it looks."""

FIXTURE = "fixture"
LIVE = "live"

OUTPUT_DIR = config.OUTPUTS_DIR
TABLE_NAME = "faults.md"
RECORD_NAME = "faults.json"


def clean_attempt_count(service_url: str, *, where: str, timeout_s: float) -> int:
    """How many network calls one clean retrieval of this layer costs.

    Measured by watching a real run through a rate-0.0 plan rather than assuming
    two. `fetch_arcgis_vector` costs one metadata call plus one page, and a layer
    that needs a second page would make a hardcoded two wrong in the direction
    that flatters the harness -- every faulted run would report a spare attempt
    it never spent.
    """
    with faults.injecting(FaultConfig(kind="timeout", rate=0.0, seed=0)) as plan:
        acquire.fetch_arcgis_vector(
            service_url, 0, where=where, out_sr=4326, timeout_s=timeout_s
        )
    return plan.attempts


def run_cell(
    service_url: str,
    kind: str,
    rate: float,
    *,
    dataset: str,
    layer_id: int,
    where: str,
    expect_features: int,
    expect_crs: str,
    clean_attempts: int,
    timeout_s: float,
) -> list[faults.RunOutcome]:
    """Every seeded run for one (kind, rate) cell."""
    return [
        faults.run_retrieval(
            service_url,
            FaultConfig(kind=kind, rate=rate, seed=seed),  # type: ignore[arg-type]
            dataset=dataset,
            expect_features=expect_features,
            expect_crs=expect_crs,
            clean_attempts=clean_attempts,
            layer_id=layer_id,
            where=where,
            timeout_s=timeout_s,
        )
        for seed in range(SEEDS)
    ]


def summarise(source: str, kind: str, rate: float, runs: list[faults.RunOutcome]) -> dict[str, Any]:
    """One table row, with the denominators it was computed against."""
    completed = [run for run in runs if run.completed]
    correct = [run for run in runs if run.correct]
    faulted = [run for run in runs if run.injected > 0]
    faulted_correct = [run for run in faulted if run.correct]
    errors = sorted({run.error.split(":")[0] for run in runs if run.error})
    return {
        "source": source,
        "kind": kind,
        "rate": rate,
        "runs": len(runs),
        "completed": len(completed),
        "correct": len(correct),
        "recovery_rate": round(len(correct) / len(runs), 3) if runs else 0.0,
        "recovery_rate_given_faulted": round(len(faulted_correct) / len(faulted), 3)
        if faulted
        else None,
        "runs_with_an_injected_fault": len(faulted),
        "injected_faults": sum(run.injected for run in runs),
        "not_applicable": sum(run.not_applicable for run in runs),
        "attempts": sum(run.attempts for run in runs),
        "mean_extra_calls": round(statistics.fmean(run.extra_turns for run in runs), 2)
        if runs
        else 0.0,
        "features_seen": sorted({run.features for run in runs}),
        "every_call_had_a_timeout": all(run.timeouts_ok for run in runs),
        "failure_kinds": errors,
    }


def events(runs: Iterable[faults.RunOutcome]) -> list[FaultEvent]:
    """The frozen record for every run, for anything that wants the raw events."""
    return [run.event() for run in runs]


def render(rows: list[dict[str, Any]], clean: dict[str, Any]) -> str:
    """The table, as markdown, with the clean run above it as the baseline."""
    lines: list[str] = []
    lines.append("| source | fault | rate | runs | completed | correct | recovery | "
                 "recovery when faulted | mean extra calls | calls | injected | "
                 "not applicable | failure |")
    lines.append("| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | "
                 "---: | ---: | --- |")
    lines.append(
        f"| {clean['source']} | none (baseline) | 0.00 | {clean['runs']} | "
        f"{clean['completed']} | {clean['correct']} | "
        f"{clean['recovery_rate']:.0%} | n/a | {clean['mean_extra_calls']:.2f} | "
        f"{clean['attempts']} | 0 | 0 | - |"
    )
    for row in rows:
        failure = ", ".join(row["failure_kinds"]) or "-"
        conditional = row["recovery_rate_given_faulted"]
        given = "n/a" if conditional is None else f"{conditional:.0%}"
        lines.append(
            f"| {row['source']} | `{row['kind']}` | {row['rate']:.2f} | {row['runs']} | "
            f"{row['completed']} | {row['correct']} | {row['recovery_rate']:.0%} | "
            f"{given} | {row['mean_extra_calls']:.2f} | {row['attempts']} | "
            f"{row['injected_faults']} | {row['not_applicable']} | {failure} |"
        )
    return "\n".join(lines)


def fixture_suite(timeout_s: float = 10.0) -> tuple[dict[str, Any], list[dict[str, Any]], list[FaultEvent]]:
    """The whole grid against the loopback fixture service."""
    all_runs: list[faults.RunOutcome] = []
    rows: list[dict[str, Any]] = []
    with faults.local_service() as url:
        attempts = clean_attempt_count(url, where="1=1", timeout_s=timeout_s)
        baseline = [
            faults.run_retrieval(
                url,
                None,
                dataset=FIXTURE,
                expect_features=faults.SAMPLE_FEATURE_COUNT,
                expect_crs="EPSG:4326",
                clean_attempts=attempts,
                timeout_s=timeout_s,
            )
            for _ in range(SEEDS)
        ]
        clean = summarise(FIXTURE, "none", 0.0, baseline)
        all_runs.extend(baseline)
        for kind in faults.ALL_KINDS:
            for rate in RATES:
                runs = run_cell(
                    url,
                    kind,
                    rate,
                    dataset=FIXTURE,
                    layer_id=0,
                    where="1=1",
                    expect_features=faults.SAMPLE_FEATURE_COUNT,
                    expect_crs="EPSG:4326",
                    clean_attempts=attempts,
                    timeout_s=timeout_s,
                )
                all_runs.extend(runs)
                rows.append(summarise(FIXTURE, kind, rate, runs))
                row = rows[-1]
                print(
                    f"  {kind:<13} rate {rate:.2f}  "
                    f"completed {row['completed']}/{row['runs']}  "
                    f"correct {row['correct']}/{row['runs']}  "
                    f"extra calls {row['mean_extra_calls']:.2f}  "
                    f"recovery-when-faulted {row['recovery_rate_given_faulted']}  "
                    f"injected {row['injected_faults']}/{row['attempts']} calls  "
                    f"n/a {row['not_applicable']}"
                )
    return clean, rows, events(all_runs)


def live_target() -> dict[str, Any]:
    """The real tract layer, discovered from the service, plus what to expect.

    The expected feature count and CRS come from the stored provenance for the
    snapshot's own tract layer, so the live rows are compared against what this
    project actually retrieved rather than against a number typed here. Invariant
    5: the study area is threaded from config and the layer id is discovered.
    """
    area = config.STUDY_AREA
    found = Registry()
    found.load_manifest()
    record = found.record(acquire.DATASET_TRACTS)
    layers = acquire.discover_arcgis_layers(
        acquire.TIGERWEB_TRACTS_BLOCKS_URL, timeout_s=config.REQUEST_TIMEOUT_S
    )
    layer = acquire.select_layer(
        layers, acquire.TRACTS_LAYER_NAME, geometry_type=acquire.POLYGON_GEOMETRY
    )
    return {
        "service_url": layer["service_url"],
        "layer_id": int(layer["id"]),
        "where": acquire.tigerweb_county_where(area),
        "expect_features": int(record.provenance.feature_count),
        "expect_crs": record.provenance.declared_crs,
        "dataset": acquire.DATASET_TRACTS,
    }


def live_suite(kinds: tuple[str, ...], rate: float) -> tuple[dict[str, Any], list[dict[str, Any]], list[FaultEvent]]:
    """A small grid against the real service.

    Deliberately small. Every run here is a real request to somebody else's
    endpoint, so this takes one rate and however many kinds the caller asks for,
    and the table says so beside the numbers.
    """
    target = live_target()
    timeout_s = config.REQUEST_TIMEOUT_S
    print(
        f"  live target: layer {target['layer_id']} at {target['service_url']}, "
        f"expecting {target['expect_features']} feature(s) in {target['expect_crs']}"
    )
    attempts = clean_attempt_count(
        target["service_url"], where=target["where"], timeout_s=timeout_s
    )
    baseline = [
        faults.run_retrieval(
            target["service_url"],
            None,
            dataset=target["dataset"],
            expect_features=target["expect_features"],
            expect_crs=target["expect_crs"],
            clean_attempts=attempts,
            layer_id=target["layer_id"],
            where=target["where"],
            timeout_s=timeout_s,
        )
    ]
    clean = summarise(LIVE, "none", 0.0, baseline)
    all_runs = list(baseline)
    rows: list[dict[str, Any]] = []
    for kind in kinds:
        runs = [
            faults.run_retrieval(
                target["service_url"],
                FaultConfig(kind=kind, rate=rate, seed=seed),  # type: ignore[arg-type]
                dataset=target["dataset"],
                expect_features=target["expect_features"],
                expect_crs=target["expect_crs"],
                clean_attempts=attempts,
                layer_id=target["layer_id"],
                where=target["where"],
                timeout_s=timeout_s,
            )
            for seed in range(LIVE_SEEDS)
        ]
        all_runs.extend(runs)
        rows.append(summarise(LIVE, kind, rate, runs))
        row = rows[-1]
        if row["injected_faults"] == 0:
            print(f"  WARNING: {kind} injected nothing in {LIVE_SEEDS} live seed(s); "
                  "this row measures the dice, not the system")
        print(
            f"  {kind:<13} rate {rate:.2f}  live  "
            f"completed {row['completed']}/{row['runs']}  "
            f"correct {row['correct']}/{row['runs']}  "
            f"injected {row['injected_faults']}  features {row['features_seen']}"
        )
    return clean, rows, events(all_runs)


def write(clean: dict[str, Any], rows: list[dict[str, Any]], recorded: list[FaultEvent]) -> Path:
    """Write the table and the raw rows, and return the table's path."""
    config.ensure_dirs()
    table = render(rows, clean)
    stamp = datetime.now().isoformat(timespec="seconds")
    document = (
        "# Robustness under injected retrieval faults\n\n"
        f"Generated {stamp} by `python -m src.experiments.faults`.\n\n"
        f"`rate` is per network attempt, not per dataset -- see the module docstring "
        f"in `src/faults.py`. Each fixture cell is {SEEDS} seeded runs; each live cell "
        f"is {LIVE_SEEDS}. `correct` means the run returned the same feature count and declared "
        f"CRS as the clean run; `completed` means only that it returned.\n\n"
        f"{table}\n\n"
        f"Rows marked `{FIXTURE}` ran against a real HTTP server on loopback serving a "
        f"synthetic FeatureSet; rows marked `{LIVE}` ran against the service the "
        f"snapshot came from. Nothing here writes to `data/snapshot/`.\n"
    )
    path = OUTPUT_DIR / TABLE_NAME
    path.write_text(document, encoding="utf-8")
    (OUTPUT_DIR / RECORD_NAME).write_text(
        json.dumps(
            {
                "generated": stamp,
                "rates": list(RATES),
                "seeds": SEEDS,
                "max_retries": config.MAX_RETRIES,
                "request_timeout_s": config.REQUEST_TIMEOUT_S,
                "clean": clean,
                "rows": rows,
                "events": [asdict(event) for event in recorded],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return path


def main() -> int:
    live = "--live" in sys.argv[1:]
    print("robustness suite: retrieval under injected faults\n")
    print(f"  rates {list(RATES)} per network attempt, {SEEDS} seed(s) per cell, "
          f"retry bound {config.MAX_RETRIES}\n")
    clean, rows, recorded = fixture_suite()

    if live:
        print("\n  --live: real requests to the publishing service\n")
        live_clean, live_rows, live_events = live_suite(("timeout", "wrong_crs"), 0.5)
        rows = rows + live_rows
        recorded = recorded + live_events
        print(f"\n  live baseline: {live_clean['features_seen']} feature(s), "
              f"correct {live_clean['correct']}/{live_clean['runs']}")

    path = write(clean, rows, recorded)
    print("\n" + render(rows, clean))
    print(f"\nwritten to {path}")
    print(f"every call carried a timeout: "
          f"{all(row['every_call_had_a_timeout'] for row in rows)}")
    print(f"the injector is armed after the suite: {faults.armed()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
