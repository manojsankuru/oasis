"""Model-written Python, executed in a child process, with the traceback brought back.

This is the first of the two feedback cycles the architecture names: the model
writes code, the code fails, the traceback comes back as text, the model fixes it.
`repair_loop` runs that cycle here; `tools.run_spatial_code` exposes a single
`run` to the agent and lets the agent's own loop be the repair.

**How model-written code reaches the layers, and why.** Three routes were
available and the module takes the second.

1. *The child imports the pipeline and rebuilds.* One rebuild is 17-41 seconds,
   so a three-attempt repair costs two minutes of wall clock before the model
   sees anything, and each attempt would be a second computation of numbers a
   tool already reported -- two answers to one question, which is exactly what
   `critic.py` traces against. Rejected.
2. *The parent dumps the cleaned frames to parquet once, in a temporary directory
   it owns, and the child reads them back by name.* One dump per process, keyed
   on the identity of `tools.analysis()`, so a live retrieval that invalidates
   the shared run invalidates this too with no edit in `tools.py`. The child pays
   a parquet read, not a pipeline run, and it never opens anything under `data/`.
   **Taken.**
3. *Pipe the frames through stdin.* Geometry does not survive it. Rejected.

The reason is the same one that made the tool surface fast enough to use in S9:
one `PipelineResult` per process, and everything downstream reads from it. A
sandbox that recomputed would be a third route to the same numbers.

**This is a timeout and working-directory boundary, not a security boundary.**
It executes model-written Python in a subprocess with the same interpreter, the
same packages and the same user as the parent. It bounds how long that code may
run, kills the process tree when the bound expires, gives the code a scratch
directory to work in so it cannot reach `data/`, and refuses to carry a
coordinate back into a model message. It does not sandbox the filesystem, the
network, or the process table. Code that wants to delete this repository can.
That honesty belongs in the paper's limitations and is stated here so nobody
reads the word "sandbox" as more than it is.

**What the output guard can and cannot reach.** Invariant 3 says geometry never
enters a model message, and this tool returns the child's stdout to the model
verbatim, which is a channel no earlier check could see. Two enforcement points,
one rule set: the child raises `GeometryInOutput` before writing a line that
could carry a coordinate, so the model gets a repairable error rather than
mangled output; the parent redacts the same shapes line by line on the way out,
because the child guard is bypassable with `sys.stdout.write`.

`tools.COORDINATE_TEXT` was written for degrees at full precision: at most three
digits before the point, at least four after it. Both bounds fail on this
channel, and the first version of this module inherited both.

* The ceiling hides a projected coordinate, which is seven digits.
* The floor hides a coordinate rounded to three places -- which is what these
  instructions tell the model to print -- and the layers are ordinary
  GeoDataFrames, so nothing stops a program projecting one back to degrees
  before printing it.
* A tight separator hides a labelled pair: `easting 1613477.7, northing
  1234567.9` is two numbers with a word between them.

`BARE_PAIR`, `LABELLED_PAIR` and `LABELLED_NUMBER` are the three answers, and
`GEOGRAPHIC_SOURCE` forces the first of them with real code that really does
reproject. All three were found by the invariant reviewer, in a module whose
ninety-four checks were green -- the fifth session running in which that
happened, and the second in which the defect was in a guard written that session
to enforce the invariant it then failed to enforce.

What remains uncovered, stated rather than implied: a single coordinate printed
alone on its own line, because one number is not a location and refusing every
large decimal would refuse a population and every distance in metres this
project reports; and a pair split across two `print` calls. The instructions
tell the model to label every number and not to project back to degrees, which
narrows both without closing either.
"""

from __future__ import annotations

import atexit
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from . import acquire, align, config, contracts, llm_client, tools, verify
from .contracts import CodeRun, CodeSession, Col

# ---------------------------------------------------------------------------
# where the child comes from
# ---------------------------------------------------------------------------

INTERPRETER: Path = config.PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
if not INTERPRETER.exists():
    INTERPRETER = config.PROJECT_ROOT / ".venv" / "bin" / "python"
if not INTERPRETER.exists():
    INTERPRETER = Path(sys.executable)
"""The interpreter the child runs under, falling back the way `mutate.py` does.

A hardcoded path is a machine-specific constant, which is the same defect as a
hardcoded county: correct here and silent everywhere else."""

RUNNER_NAME = "_runner.py"
CONFIG_NAME = "_config.json"
SOURCE_NAME = "analysis.py"
STDOUT_NAME = "stdout.txt"
STDERR_NAME = "stderr.txt"
WORKSPACE_PREFIX = "geoagent-sandbox-"

POLL_INTERVAL_S = 0.05
KILL_TIMEOUT_S = 10.0
DEFAULT_TIMEOUT_S = 60.0

TIMEOUT_EXIT_CODE = 124
"""Reported when a killed child leaves a zero return code behind.

The conventional shell code for a command that ran out of time. A timeout that
reported exit 0 would read as a success in every count that follows."""

USER_ERROR_EXIT_CODE = 1
START_FAILED_EXIT_CODE = 127

STREAM_LIMIT = 6_000
"""Characters of stdout, and of stderr, that come back to the model.

`tools._serialisation_checks` refuses any tool result over 24 kB, and a model
that prints a frame produces megabytes. Cut in the middle rather than at the end,
because for a traceback the last lines are the ones that matter."""

CHILD_OUTPUT_LIMIT = 200_000
"""What the child will write before it refuses, so a print loop cannot fill a
disk inside its timeout."""

READ_LIMIT = 2_000_000
"""How much of an output file the parent reads. A bound on the parent's memory,
independent of the bound on what it forwards."""

MAX_RUNS = 200
"""How many `CodeRun` records this process keeps for the instrumentation."""

TIMEOUT_ERROR = "Timeout"
NON_ZERO_EXIT_ERROR = "NonZeroExit"
SHAPE_MISMATCH_ERROR = "ShapeMismatch"
CRS_ERROR = "CRSError"
START_FAILED_ERROR = "StartFailed"
WORKSPACE_ERROR = "WorkspaceUnavailable"
AUTHOR_ERROR = "AuthorUnavailable"


# ---------------------------------------------------------------------------
# invariant 3, on a stream of text rather than on a JSON payload
# ---------------------------------------------------------------------------

COORDINATE_NAME = tools.COORDINATE_PATTERN
"""Imported through `tools`, which imports it from `pipeline`. Three rules with
one pattern cannot disagree about what a coordinate column is called."""

BARE_TOKEN = tools.BARE_TOKEN

GEOMETRY_TEXT = re.compile(
    r"\b(?:POINT|LINESTRING|LINEARRING|POLYGON|MULTIPOINT|MULTILINESTRING"
    r"|MULTIPOLYGON|GEOMETRYCOLLECTION)\s*[ZM]{0,2}\s*\("
)
"""Well-known text, and the shapely repr that wraps it.

Uppercase only, deliberately. `str(geometry)`, `repr(geometry)`, `.wkt` and a
GeoDataFrame's own repr all emit the keyword in capitals; matching case
insensitively would refuse the word "polygon" in a sentence, and a guard that
refuses prose gets exempted rather than fixed."""

BARE_PAIR = re.compile(r"-?\d+\.\d+[\s,()\[\]]+-?\d+\.\d+")
"""Two decimal numbers with nothing but space, comma or bracket between them.

`tools.COORDINATE_TEXT` is this rule with a floor of four decimal places and a
ceiling of three integer digits -- it sees a degree printed at full precision
and nothing else. Both bounds were wrong for this channel. The ceiling hides a
projected metre coordinate, which is seven digits. The floor hides a degree
rounded to three places, which is exactly what `CODE_RULES` below tells the
model to print. Dropping both leaves the separator as the only discriminator,
which is why the instructions say to label every number: `1.234 m, max 5.678 m`
has words between its numbers and survives, `1.234 5.678` does not."""

LABELLED_PAIR = re.compile(r"-?\d{4,}\.\d+[^\d\n]{0,24}-?\d{4,}\.\d+")
"""Two large decimal numbers with a label between them.

The shape a tight separator cannot see: `easting 1613477.7, northing 1234567.9`.
Here the magnitude carries the discrimination rather than the spacing -- this
project reports populations as integers and fractions, depths and percentages as
small numbers -- so a label between the two costs nothing."""

LABELLED_NUMBER = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*[:=]?\s*-?\d+\.\d+")
"""A name immediately in front of a decimal number, so the name can be tested.

`lon -80.454 lat 32.483` is a coordinate that neither pair rule can reach: the
separator carries words and the magnitude is small. What gives it away is the
label, and `COORDINATE_PATTERN` already knows which labels name a coordinate.
Bound to the name directly in front of a number rather than to every word on the
line, because a traceback frame under `.../shapely/geometry/base.py` beside a
version number would otherwise be redacted out of a real traceback."""

COORDINATE_WORD = re.compile(r"\b(?:coordinates|__geo_interface__|geo_interface)\b")
"""The containers a coordinate travels inside when it is serialised."""

QUOTED_NAME = re.compile(r"['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]")
"""A quoted identifier, which is how a column name appears in `print(df.columns)`."""

LINE_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("well-known text geometry", GEOMETRY_TEXT),
    ("two decimal numbers side by side", BARE_PAIR),
    ("two large decimal numbers with a label between them", LABELLED_PAIR),
    ("a word that names a coordinate container", COORDINATE_WORD),
)

WITHHELD_TEMPLATE = (
    "[{stream} line {number} withheld: {reason}. Invariant 3 -- geometry never "
    "reaches a model message. Print labelled scalars instead.]"
)
CUT_TEMPLATE = "\n[... {count} characters of {stream} withheld: over the {limit}-character bound ...]\n"


def output_faults(line: str) -> list[str]:
    """Every way one line of child output could carry a coordinate.

    Per line rather than per stream, so one offending line costs one line and the
    rest of a useful run survives. The rule set is a superset of the string half
    of `tools.coordinate_faults`, which a check asserts rather than assumes.

    **A fault names the rule and never quotes the text.** The first version
    echoed the match so the model would know what to fix, which put the refused
    coordinate into the refusal -- the guard leaking through its own message. A
    reason a reader can act on does not have to repeat the thing being withheld.
    """
    faults: list[str] = []
    for label, rule in LINE_RULES:
        if rule.search(line):
            faults.append(label)
    for name in QUOTED_NAME.findall(line):
        if COORDINATE_NAME.search(name):
            faults.append("a quoted name that could be a coordinate column")
            break
    for name in LABELLED_NUMBER.findall(line):
        if COORDINATE_NAME.search(name):
            faults.append("a name that could label a coordinate, in front of a number")
            break
    stripped = line.strip()
    if BARE_TOKEN.match(stripped) and COORDINATE_NAME.search(stripped):
        faults.append("a bare name that could be a coordinate column")
    return faults


def guard_stream(text: str, stream: str) -> str:
    """One stream, with every offending line replaced and the whole thing bounded.

    Replaced rather than dropped: a line that vanishes reads as a line that was
    never printed, and the model then repairs the wrong thing.
    """
    kept: list[str] = []
    for number, line in enumerate(text.splitlines(), start=1):
        faults = output_faults(line)
        if faults:
            kept.append(
                WITHHELD_TEMPLATE.format(
                    stream=stream, number=number, reason="; ".join(faults[:2])
                )
            )
        else:
            kept.append(line)
    joined = "\n".join(kept)
    if text.endswith("\n") and joined:
        joined += "\n"
    if len(joined) <= STREAM_LIMIT:
        return joined
    half = STREAM_LIMIT // 2
    cut = len(joined) - STREAM_LIMIT
    return (
        joined[:half]
        + CUT_TEMPLATE.format(count=cut, stream=stream, limit=STREAM_LIMIT)
        + joined[-half:]
    )


# ---------------------------------------------------------------------------
# the workspace: one dump per shared run
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Workspace:
    """A temporary directory holding the cleaned layers and the child's runner."""

    directory: Path
    runner: Path
    layers: dict[str, dict[str, Any]]
    unavailable: dict[str, str]
    built_from: Any
    seconds: float


_WORKSPACE: Workspace | None = None


def _frame_spec(name: str, frame: Any, directory: Path) -> dict[str, Any]:
    """Write one frame to parquet and describe what the child will find there."""
    spatial = hasattr(frame, "geometry") and hasattr(frame, "crs")
    path = directory / f"{name}.parquet"
    frame.to_parquet(path)
    kept, withheld = tools.reportable_columns(frame.columns)
    return {
        "path": str(path),
        "geometry": bool(spatial),
        "rows": int(len(frame)),
        "columns": int(len(frame.columns)),
        "reportable": kept,
        "columns_withheld": len(withheld),
    }


def _degraded_names() -> set[str]:
    """Registered datasets whose retrieval was degraded, read through `align`."""
    found = tools.registry()
    return {
        record.name
        for record in found.records()
        if align.is_degraded(record.provenance)
    }


def build_workspace() -> Workspace:
    """Dump the shared run's frames into a directory this module owns.

    Nothing here is named: the layer names come from the aligned snapshot, which
    built them from `acquire`'s dataset constants and `align.JOINED_SUFFIX`. A
    layer added upstream appears to the model with no edit here.
    """
    started = time.monotonic()
    state = tools.analysis()
    directory = Path(tempfile.mkdtemp(prefix=WORKSPACE_PREFIX))
    layers: dict[str, dict[str, Any]] = {}
    unavailable: dict[str, str] = {}
    degraded = _degraded_names()
    for name, frame in state.result.snapshot.frames.items():
        try:
            layers[name] = _frame_spec(name, frame, directory)
        except Exception as exc:
            unavailable[name] = f"{type(exc).__name__}: {exc}"
            continue
        base = name[: -len(align.JOINED_SUFFIX)] if name.endswith(align.JOINED_SUFFIX) else name
        layers[name]["degraded"] = base in degraded
    runner = directory / RUNNER_NAME
    runner.write_text(RUNNER_SOURCE, encoding="utf-8")
    return Workspace(
        directory=directory,
        runner=runner,
        layers=layers,
        unavailable=unavailable,
        built_from=state,
        seconds=round(time.monotonic() - started, 2),
    )


def workspace() -> Workspace:
    """The dump, built on first need and rebuilt when the shared run changes.

    Keyed on the identity of the `Analysis` object rather than on a timestamp or
    a flag. `acquire_dataset` calls `tools.invalidate()` after a live retrieval,
    so the next `tools.analysis()` is a different object and this dump is
    rebuilt -- without `tools.py` knowing this module exists. A dump keyed on
    nothing would serve the model the snapshot a retrieval had just replaced,
    which is the stale-answer failure `_cache_checks` was written for one level
    up.
    """
    global _WORKSPACE
    state = tools.analysis()
    if _WORKSPACE is None or _WORKSPACE.built_from is not state:
        discard_workspace()
        _WORKSPACE = build_workspace()
    return _WORKSPACE


def discard_workspace() -> None:
    """Forget and remove the dump. Registered at exit, and used between rebuilds."""
    global _WORKSPACE
    if _WORKSPACE is not None:
        shutil.rmtree(_WORKSPACE.directory, ignore_errors=True)
        _WORKSPACE = None


atexit.register(discard_workspace)


# ---------------------------------------------------------------------------
# the child
# ---------------------------------------------------------------------------

RUNNER_SOURCE = '''"""Written by src/sandbox.py into the workspace it owns. Not imported by anything.

Three jobs, in order: bind the cleaned layers, install the output guard, and run
the model's file under its own name so the traceback carries the model's own line
numbers rather than this file's.
"""

import builtins
import json
import sys
import traceback

SETTINGS = json.loads(open(sys.argv[1], encoding="utf-8").read())

LINE_RULES = [(label, __import__("re").compile(pattern))
              for label, pattern in SETTINGS["line_rules"]]
QUOTED_NAME = __import__("re").compile(SETTINGS["quoted_name"])
LABELLED_NUMBER = __import__("re").compile(SETTINGS["labelled_number"])
BARE_TOKEN = __import__("re").compile(SETTINGS["bare_token"])
COORDINATE_NAME = __import__("re").compile(SETTINGS["coordinate_name"], 2)
OUTPUT_LIMIT = SETTINGS["output_limit"]
SPATIAL_TYPES = ("GeoDataFrame", "GeoSeries")


class GeometryInOutput(Exception):
    """Printed text that could carry a coordinate into a model message."""


class OutputTooLarge(Exception):
    """More output than the sandbox will carry back to the model."""


def faults(text):
    found = []
    for line in text.splitlines():
        for label, rule in LINE_RULES:
            if rule.search(line):
                found.append(label)
        for name in QUOTED_NAME.findall(line):
            if COORDINATE_NAME.search(name):
                found.append("a quoted name that could be a coordinate column")
                break
        for name in LABELLED_NUMBER.findall(line):
            if COORDINATE_NAME.search(name):
                found.append("a name that could label a coordinate, in front of a number")
                break
        stripped = line.strip()
        if BARE_TOKEN.match(stripped) and COORDINATE_NAME.search(stripped):
            found.append("a bare name that could be a coordinate column")
    return found


WRITTEN = [0]
REAL_PRINT = builtins.print


def guarded_print(*args, **kwargs):
    for item in args:
        kind = type(item)
        if kind.__module__.split(".")[0] == "shapely" or kind.__name__ in SPATIAL_TYPES:
            raise GeometryInOutput(
                "printing a " + kind.__name__ + " would put geometry into a model "
                "message. Print a count, a name, or a rounded scalar instead."
            )
    rendered = str(kwargs.get("sep", " ")).join(str(item) for item in args)
    found = faults(rendered)
    if found:
        raise GeometryInOutput(
            "this output could carry a coordinate into a model message ("
            + "; ".join(found[:3])
            + "). Print labelled scalars, at most three decimal places, one per line."
        )
    WRITTEN[0] += len(rendered) + 1
    if WRITTEN[0] > OUTPUT_LIMIT:
        raise OutputTooLarge(
            "this run printed more than " + str(OUTPUT_LIMIT)
            + " characters. Summarise instead of printing rows."
        )
    REAL_PRINT(*args, **kwargs)


builtins.print = guarded_print


class Namespace(dict):
    """The globals the model's program runs in, with the layers loaded on demand.

    A dict subclass rather than eight eager reads: a program that asks for one
    layer pays for one, and a program that asks for none -- which is most of
    them, most of the time -- starts in the time the interpreter takes. A name
    that is not a layer raises KeyError, which is what makes an ordinary typo
    come back as an ordinary NameError.
    """

    def __missing__(self, key):
        if key == "pd":
            import pandas
            self[key] = pandas
            return pandas
        if key == "np":
            import numpy
            self[key] = numpy
            return numpy
        if key == "gpd":
            import geopandas
            self[key] = geopandas
            return geopandas
        if key == "LAYERS":
            loaded = {name: self[name] for name in SETTINGS["layers"]}
            self[key] = loaded
            return loaded
        spec = SETTINGS["layers"].get(key)
        if spec is None:
            raise KeyError(key)
        if spec["geometry"]:
            import geopandas
            frame = geopandas.read_parquet(spec["path"])
        else:
            import pandas
            frame = pandas.read_parquet(spec["path"])
        self[key] = frame
        return frame


def main():
    namespace = Namespace({
        "__name__": "__main__",
        "__file__": SETTINGS["source_path"],
        "__builtins__": builtins,
    })
    sys.argv = [SETTINGS["source_path"]]
    text = open(SETTINGS["source_path"], encoding="utf-8").read()
    try:
        compiled = compile(text, SETTINGS["source_path"], "exec")
    except SyntaxError:
        kind, value, _ = sys.exc_info()
        sys.stderr.write("".join(traceback.format_exception_only(kind, value)))
        return SETTINGS["user_error_exit"]
    try:
        exec(compiled, namespace)
    except SystemExit as stop:
        if stop.code is None:
            return 0
        return stop.code if isinstance(stop.code, int) else 1
    except BaseException:
        kind, value, tb = sys.exc_info()
        sys.stderr.write("".join(traceback.format_exception(kind, value, tb.tb_next or tb)))
        return SETTINGS["user_error_exit"]
    return 0


if __name__ == "__main__":
    try:
        STATUS = main()
    finally:
        sys.stdout.flush()
        sys.stderr.flush()
    raise SystemExit(STATUS)
'''


def runner_settings(space: Workspace, source_path: Path) -> dict[str, Any]:
    """What the child is told, serialised beside it.

    The guard patterns travel rather than being written twice. Two copies of a
    rule drift, and the copy that drifts is the one nobody re-reads -- the reason
    `verify.py` exists at all.
    """
    return {
        "layers": {
            name: {"path": spec["path"], "geometry": spec["geometry"]}
            for name, spec in space.layers.items()
        },
        "line_rules": [[label, rule.pattern] for label, rule in LINE_RULES],
        "quoted_name": QUOTED_NAME.pattern,
        "labelled_number": LABELLED_NUMBER.pattern,
        "bare_token": BARE_TOKEN.pattern,
        "coordinate_name": COORDINATE_NAME.pattern,
        "output_limit": CHILD_OUTPUT_LIMIT,
        "source_path": str(source_path),
        "user_error_exit": USER_ERROR_EXIT_CODE,
    }


def child_environment() -> dict[str, str]:
    """The child's environment: UTF-8 in both directions, and no project on the path.

    `PYTHONPATH` is dropped so the child cannot import `src.pipeline` and rebuild
    the analysis -- route (1) in the module docstring, made unreachable rather
    than merely discouraged. UTF-8 is forced because a traceback carrying a
    non-ASCII character, read back under the Windows ANSI code page, raises
    `UnicodeDecodeError` in the parent, which would turn a model's mistake into
    the sandbox's crash.
    """
    environment = dict(os.environ)
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["PYTHONUTF8"] = "1"
    environment.pop("PYTHONPATH", None)
    return environment


def kill_tree(process: subprocess.Popen[bytes]) -> None:
    """Kill the child and everything it started, then reap it.

    The direct child is not enough. `mutate.py`'s `run_check` calls
    `subprocess.run` with no timeout of its own, so a grandchild that survives
    and holds an inherited handle hangs the mutation harness silently and
    forever. On Windows only `taskkill /T` walks the tree; its failure is
    ignored because the commonest reason for it is that the child already
    exited.
    """
    if process.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                capture_output=True,
                timeout=KILL_TIMEOUT_S,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        process.kill()
    except OSError:
        pass
    try:
        process.wait(timeout=KILL_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        pass


def read_capped(path: Path) -> str:
    """One output file, read with a bound on what the parent holds in memory."""
    try:
        size = path.stat().st_size
    except OSError:
        return ""
    with path.open("rb") as handle:
        if size <= READ_LIMIT:
            raw = handle.read()
        else:
            head = handle.read(READ_LIMIT // 2)
            handle.seek(size - READ_LIMIT // 2)
            raw = head + b"\n...\n" + handle.read()
    return raw.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# the error taxonomy -- criterion IR asks which errors, not how many
# ---------------------------------------------------------------------------

TRACEBACK_TAIL = re.compile(r"^([A-Za-z_][A-Za-z0-9_.]*)\s*(?::|$)")
SHAPE_MISMATCH = re.compile(
    r"does not match length|Length mismatch|Lengths must match|not aligned"
    r"|could not be broadcast|Unalignable|cannot reindex|shape mismatch",
    re.IGNORECASE,
)


def exception_name(stderr: str) -> str | None:
    """The exception a traceback ends with, or None if it does not end with one."""
    for line in reversed(stderr.splitlines()):
        stripped = line.strip()
        if not stripped or line[:1].isspace():
            continue
        found = TRACEBACK_TAIL.match(stripped)
        if found:
            return found.group(1)
        return None
    return None


def classify(exit_code: int, stderr: str, timed_out: bool) -> str | None:
    """What went wrong, as one word the instrumentation can count.

    `CodeRun.error_type` is the field the contract already provides for this, and
    the taxonomy is the interesting half of criterion IR: which mistakes a model
    makes writing spatial code is a result, and a count of failures is not.
    """
    if timed_out:
        return TIMEOUT_ERROR
    if exit_code == 0:
        return None
    name = exception_name(stderr)
    if name is None:
        return NON_ZERO_EXIT_ERROR
    short = name.rsplit(".", 1)[-1]
    if short.endswith(CRS_ERROR):
        return CRS_ERROR
    if short == "ValueError" and SHAPE_MISMATCH.search(stderr):
        return SHAPE_MISMATCH_ERROR
    return short


# ---------------------------------------------------------------------------
# instrumentation
# ---------------------------------------------------------------------------

RUNS: list[CodeRun] = []
SESSIONS: list[CodeSession] = []


def record(run: CodeRun) -> CodeRun:
    """Keep one run for the metrics, bounded the way `tools.CALLS` is."""
    RUNS.append(run)
    del RUNS[:-MAX_RUNS]
    return run


def forget_runs() -> None:
    """Drop the instrumentation. A new question is a new measurement."""
    RUNS.clear()
    SESSIONS.clear()


def metrics(
    runs: list[CodeRun] | None = None, sessions: list[CodeSession] | None = None
) -> dict[str, Any]:
    """Attempts, first-run failure rate, repair rate and the error taxonomy.

    Takes its inputs so a check can measure a set of runs whose outcome it stated
    by hand, rather than measuring the runs the module itself just produced and
    comparing them against the module.
    """
    runs = RUNS if runs is None else runs
    sessions = SESSIONS if sessions is None else sessions
    failed = [item for item in runs if item.exit_code != 0]
    taxonomy: dict[str, int] = {}
    for item in failed:
        key = item.error_type or NON_ZERO_EXIT_ERROR
        taxonomy[key] = taxonomy.get(key, 0) + 1
    first_failed = [
        item for item in sessions if item.runs and item.runs[0].exit_code != 0
    ]
    repaired = [item for item in first_failed if item.succeeded]
    attempts = [len(item.runs) for item in sessions]
    return {
        "runs": len(runs),
        "runs_failed": len(failed),
        "run_failure_rate": round(len(failed) / len(runs), 3) if runs else 0.0,
        "sessions": len(sessions),
        "sessions_succeeded": sum(1 for item in sessions if item.succeeded),
        "first_attempt_failures": len(first_failed),
        "first_run_failure_rate": (
            round(len(first_failed) / len(sessions), 3) if sessions else 0.0
        ),
        "repaired": len(repaired),
        "repair_rate": (
            round(len(repaired) / len(first_failed), 3) if first_failed else 0.0
        ),
        "attempts_total": sum(attempts),
        "attempts_mean": round(sum(attempts) / len(attempts), 2) if attempts else 0.0,
        "error_taxonomy": dict(sorted(taxonomy.items())),
    }


def format_metrics(measured: dict[str, Any] | None = None) -> str:
    """The instrumentation, printed, for the gate and for the paper."""
    measured = metrics() if measured is None else measured
    lines = [
        f"SANDBOX -- {measured['runs']} run(s), {measured['runs_failed']} failed "
        f"({measured['run_failure_rate']:.0%})",
        f"  sessions {measured['sessions']}, succeeded {measured['sessions_succeeded']}, "
        f"mean {measured['attempts_mean']} attempt(s)",
        f"  first attempt failed in {measured['first_attempt_failures']} session(s) "
        f"({measured['first_run_failure_rate']:.0%}); repaired {measured['repaired']} "
        f"({measured['repair_rate']:.0%} of those)",
        "  error taxonomy:",
    ]
    if measured["error_taxonomy"]:
        for name, count in measured["error_taxonomy"].items():
            lines.append(f"      {name:<20} {count}")
    else:
        lines.append("      (nothing failed)")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# what the model is told before it writes anything
# ---------------------------------------------------------------------------

CODE_RULES = """How to answer:

- Reply with one complete Python program in a ```python fenced block, and nothing
  else. It is written to a file and executed; it is not a snippet appended to
  anything.
- The layers listed above are already bound as module-level names. Do not read a
  file, do not import the project, and do not make a network request; none of
  them will work.
- The frames are already in the working coordinate system named above, which is
  projected, so a measurement on them is in metres.
- Print labelled scalars: a name, then one rounded number, one per line, at most
  three decimal places. Printing a geometry, a coordinate, a whole frame or two
  bare numbers side by side raises GeometryInOutput -- geometry never reaches a
  model message.
- Do not convert a layer to a geographic coordinate system and do not print a
  longitude or a latitude under any name. Report a distance, a count or a share
  instead; a location is the one thing this channel may not carry.
- If the program fails you will be shown the traceback and asked to fix it.
  Return the whole corrected program, not a patch."""


def available_names(space: Workspace | None = None) -> str:
    """What is bound inside the child, generated from the dump itself.

    The one inventory, used in two places: the system message `repair_loop`
    sends, and the stderr of any run that failed. It exists in the second place
    because of what the S10 demo did.

    `run_spatial_code` is advertised to the model as "write and execute Python
    against the cleaned layers" and nothing anywhere told it what the layers are
    CALLED. The model invented `CONTEXT.layers.acs()`, got back
    `NameError: name 'CONTEXT' is not defined` -- true, complete, and useless --
    and answered that the tool was broken. A capability whose interface cannot be
    discovered is not a capability, and the channel to say so was already there:
    a traceback is a result, so the names travel back with it.

    The tool's description in `schemas.py` was NOT edited to carry this. A
    hand-written inventory of a surface that changes in another file is the drift
    `agent.system_prompt()` exists to prevent, and this list is built from the
    frames that were actually dumped, so a layer the snapshot stops producing
    stops being offered.
    """
    space = workspace() if space is None else space
    state = space.built_from
    lines = [
        f"Working coordinate system: {state.result.study_area.working_crs} "
        "(projected -- distances and areas come out in metres).",
        "",
        "Names already bound, with pandas as pd, geopandas as gpd, numpy as np:",
        "",
    ]
    for name, spec in space.layers.items():
        kind = "GeoDataFrame" if spec["geometry"] else "DataFrame"
        mark = " DEGRADED -- an absence of data, not of hazard" if spec.get("degraded") else ""
        shown = spec["reportable"][: tools.MAX_LIST]
        rest = len(spec["reportable"]) - len(shown)
        lines.append(
            f"  {name}: {kind}, {spec['rows']} rows x {spec['columns']} columns"
            f"{mark}"
        )
        lines.append(
            f"      columns: {', '.join(shown)}"
            + (f" (+{rest} more)" if rest else "")
            + (
                f"; {spec['columns_withheld']} coordinate column(s) not listed"
                if spec["columns_withheld"]
                else ""
            )
        )
    for name, reason in space.unavailable.items():
        lines.append(f"  {name}: not bound -- {reason}")
    lines.append("")
    lines.append(f"  LAYERS is a dict of all {len(space.layers)} of them by name.")
    return "\n".join(lines)


def code_instructions(space: Workspace | None = None) -> str:
    """The system message for `repair_loop`: the inventory plus how to answer.

    Generated for the reason the system prompt in `agent.py` is generated: an
    inventory maintained beside the thing it describes drifts from it, and the
    drift is silent.
    """
    return "\n".join(
        [
            "You write Python that runs against cleaned geospatial layers in a "
            "subprocess with no network and no project imports.",
            "",
            available_names(space),
            "",
            CODE_RULES,
        ]
    )


FENCE = re.compile(r"```(?:python|py)?\s*\n(.*?)(?:```|\Z)", re.DOTALL)


def extract_code(reply: str) -> str:
    """The program out of a model reply, fenced or not."""
    found = FENCE.search(reply)
    return (found.group(1) if found else reply).strip() + "\n"


REPAIR_TEMPLATE = """That program failed. This is what the sandbox reported.

exit code: {exit_code}
error type: {error_type}

stdout:
{stdout}

stderr:
{stderr}

Fix it and return the whole corrected program in one ```python block."""


def repair_message(run: CodeRun) -> str:
    """The traceback, handed back as the next turn's question.

    This is the feedback edge the architecture diagram draws. `run.stderr` has
    already been through the output guard, because a traceback quoting a geometry
    is a model message like any other.
    """
    return REPAIR_TEMPLATE.format(
        exit_code=run.exit_code,
        error_type=run.error_type,
        stdout=run.stdout.strip() or "(nothing)",
        stderr=run.stderr.strip() or "(nothing)",
    )


class Author(Protocol):
    """Whatever turns a conversation into a reply. The model, or a script."""

    def __call__(self, messages: list[dict[str, str]]) -> str: ...


@dataclass(slots=True)
class ModelAuthor:
    """The real author: one chat completion, no tools, through `llm_client`."""

    client: Any = None

    def __call__(self, messages: list[dict[str, str]]) -> str:
        if self.client is None:
            self.client = llm_client.make_client()
        response = llm_client.chat(self.client, messages)
        return response.choices[0].message.content or ""


# ---------------------------------------------------------------------------
# the sandbox itself
# ---------------------------------------------------------------------------


class Sandbox:
    """`contracts.Sandbox`: run model-written code, and repair it when it fails.

    Constructible with no arguments, because `tools.run_spatial_code` builds one
    per call and discards it. Everything expensive -- the parquet dump, the
    runner, the shared analysis behind both -- lives at module level and is
    keyed on the shared run, so a per-call instance costs a directory entry.
    """

    def __init__(self, author: Author | None = None) -> None:
        self.author: Author = ModelAuthor() if author is None else author
        self.author_calls: list[list[dict[str, str]]] = []

    def run(self, source: str, *, timeout_s: float = DEFAULT_TIMEOUT_S) -> CodeRun:
        """Execute one program and bring back what it did.

        Never raises. A traceback is a result -- it is the thing the model reads
        to repair -- and an exception here would reach `tools.run_spatial_code`
        as `{"error": "OSError"}`, which tells the model the type of the mistake
        rather than the mistake.

        `duration_s` is the child's clock, started after the dump is ready. The
        first run in a process may wait on the shared analysis behind it, and
        folding that one-time wait into the first attempt would report a repair
        loop as ten times slower than it is.
        """
        opened = time.monotonic()
        try:
            space = workspace()
            run_directory = Path(tempfile.mkdtemp(dir=space.directory, prefix="run_"))
        except Exception as exc:
            return self._failure(source, WORKSPACE_ERROR, exc, opened)

        started = time.monotonic()
        try:
            try:
                source_path = run_directory / SOURCE_NAME
                source_path.write_text(source, encoding="utf-8")
                settings_path = run_directory / CONFIG_NAME
                settings_path.write_text(
                    json.dumps(runner_settings(space, source_path)), encoding="utf-8"
                )
                out_path = run_directory / STDOUT_NAME
                err_path = run_directory / STDERR_NAME
                with out_path.open("wb") as out, err_path.open("wb") as err:
                    process = subprocess.Popen(
                        [str(INTERPRETER), str(space.runner), str(settings_path)],
                        cwd=run_directory,
                        stdin=subprocess.DEVNULL,
                        stdout=out,
                        stderr=err,
                        env=child_environment(),
                    )
            except Exception as exc:
                return self._failure(source, START_FAILED_ERROR, exc, started)

            deadline = time.monotonic() + max(float(timeout_s), 0.0)
            while process.poll() is None and time.monotonic() < deadline:
                time.sleep(POLL_INTERVAL_S)
            timed_out = process.poll() is None
            if timed_out:
                kill_tree(process)
            exit_code = process.returncode if process.returncode is not None else TIMEOUT_EXIT_CODE
            if timed_out and exit_code == 0:
                exit_code = TIMEOUT_EXIT_CODE
            raw_out = read_capped(out_path)
            raw_err = read_capped(err_path)
        finally:
            shutil.rmtree(run_directory, ignore_errors=True)

        if timed_out:
            raw_err = (
                raw_err
                + f"\nTimeout: the sandbox stopped this run after {timeout_s:g} seconds "
                "and killed the process tree.\n"
            )
        # Classified before the inventory is appended, and from the raw stream
        # rather than the guarded one. `exception_name` reads a traceback from
        # its last line backwards; anything added after it becomes the last line
        # and every failure would classify as NonZeroExit.
        failed_as = classify(int(exit_code), raw_err, timed_out)
        if exit_code != 0:
            raw_err = raw_err + "\n" + available_names(space) + "\n"
        return record(
            CodeRun(
                attempt=1,
                source=source,
                exit_code=int(exit_code),
                stdout=guard_stream(raw_out, "stdout"),
                stderr=guard_stream(raw_err, "stderr"),
                duration_s=round(time.monotonic() - started, 3),
                error_type=failed_as,
            )
        )

    def repair_loop(self, request: str, *, max_attempts: int = 3) -> CodeSession:
        """Write, run, read the traceback, rewrite -- bounded at `max_attempts`.

        The bound is the point: an unbounded repair against a model that cannot
        solve the request spends the whole budget rediscovering that. A session
        that fails after three attempts is a measurement, and the failure rate is
        reported rather than hidden.
        """
        session = CodeSession(request=request)
        messages: list[dict[str, str]] = [
            {"role": "system", "content": code_instructions()},
            {"role": "user", "content": request},
        ]
        for attempt in range(1, max(int(max_attempts), 1) + 1):
            self.author_calls.append([dict(item) for item in messages])
            try:
                reply = self.author(messages)
            except Exception as exc:
                session.runs.append(
                    record(
                        CodeRun(
                            attempt=attempt,
                            source="",
                            exit_code=START_FAILED_EXIT_CODE,
                            stdout="",
                            stderr=guard_stream(f"{type(exc).__name__}: {exc}", "stderr"),
                            duration_s=0.0,
                            error_type=AUTHOR_ERROR,
                        )
                    )
                )
                break
            run = self.run(extract_code(reply))
            run.attempt = attempt
            session.runs.append(run)
            if run.exit_code == 0:
                session.succeeded = True
                break
            messages.append({"role": "assistant", "content": reply})
            messages.append({"role": "user", "content": repair_message(run)})
        SESSIONS.append(session)
        return session

    def _failure(
        self, source: str, kind: str, exc: BaseException, started: float
    ) -> CodeRun:
        """A run that never reached the child, reported as a run rather than raised."""
        return record(
            CodeRun(
                attempt=1,
                source=source,
                exit_code=START_FAILED_EXIT_CODE,
                stdout="",
                stderr=guard_stream(f"{type(exc).__name__}: {exc}", "stderr"),
                duration_s=round(time.monotonic() - started, 3),
                error_type=kind,
            )
        )


def format_session(session: CodeSession) -> str:
    """One repair session, printed, for the gate transcript."""
    lines = [
        f"REQUEST: {session.request}",
        f"  {len(session.runs)} attempt(s), succeeded {session.succeeded}",
    ]
    for run in session.runs:
        lines.append("")
        lines.append(
            f"  -- attempt {run.attempt}: exit {run.exit_code}, "
            f"{run.duration_s:.2f}s, error_type {run.error_type}"
        )
        lines.append("     source:")
        for line in run.source.splitlines():
            lines.append(f"       | {line}")
        if run.stdout.strip():
            lines.append("     stdout:")
            for line in run.stdout.splitlines():
                lines.append(f"       | {line}")
        if run.stderr.strip():
            lines.append("     stderr:")
            for line in run.stderr.splitlines():
                lines.append(f"       | {line}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """`python -m src.sandbox "<request>"` -- one real repair session, printed.

    The tool surface only exposes `run`; the agent's own loop is the repair there.
    This is how `repair_loop` is driven against the real model, which is the
    boundary a stub author cannot exercise.
    """
    argv = sys.argv[1:] if argv is None else argv
    requests = [item for item in argv if not item.startswith("-")]
    if not requests:
        print(code_instructions())
        return 0
    box = Sandbox()
    for request in requests:
        session = box.repair_loop(request)
        print(format_session(session))
        print()
    print(format_metrics())
    return 0


# ---------------------------------------------------------------------------
# self check
# ---------------------------------------------------------------------------

CLEAN_SOURCE = "print('sandbox', 'ready')\n"

NAME_ERROR_SOURCE = """value = 1
value = value + 1
print(undefined_name)
"""

NESTED_SOURCE = """def inner():
    raise KeyError('missing layer')


def middle():
    inner()


def outer():
    middle()


outer()
"""

SYNTAX_ERROR_SOURCE = "def broken(:\n    return 1\n"

EXIT_SOURCE = "import sys\nsys.exit(3)\n"

BOTH_STREAMS_SOURCE = """import sys
print('on stdout')
sys.stderr.write('on stderr\\n')
"""

ATTRIBUTE_ERROR_SOURCE = f"print({tools.TRACT_KEY}.no_such_attribute_here)\n"

KEY_ERROR_SOURCE = "print(len(LAYERS['no_such_layer']))\n"

SHAPE_ERROR_SOURCE = """import pandas as pd
frame = pd.DataFrame({'a': [1, 2, 3]})
frame['b'] = [1, 2]
"""

CRS_ERROR_SOURCE = """from pyproj import CRS
CRS.from_user_input('EPSG:not-a-code')
"""

IMPORT_PROJECT_SOURCE = "import src.pipeline\n"

CWD_SOURCE = """import os
open('scratch.txt', 'w').write('x')
print('wrote', os.path.exists('scratch.txt'))
print('cwd', os.getcwd())
"""

BIG_OUTPUT_SOURCE = """import sys
sys.stdout.write(('line of filler text\\n') * 20000)
"""

GEOMETRY_SOURCES: tuple[tuple[str, str], ...] = (
    ("the geometry itself", f"print({tools.TRACT_KEY}.geometry.iloc[0])\n"),
    ("its well-known text", f"print({tools.TRACT_KEY}.geometry.iloc[0].wkt)\n"),
    ("a whole GeoDataFrame", f"print({tools.TRACT_KEY}.head(2))\n"),
    ("the layer extent", f"print({tools.TRACT_KEY}.total_bounds)\n"),
    ("a list of vertices",
     f"print(list({tools.TRACT_KEY}.geometry.iloc[0].exterior.coords)[:3])\n"),
    ("the geo interface", f"print({tools.TRACT_KEY}.geometry.iloc[0].__geo_interface__)\n"),
    ("the column names", f"print(list({acquire.DATASET_TRACTS}.columns))\n"),
)
"""Seven ways real model code puts a coordinate on stdout, run against the real
layers. Enumerating the shapes is not covering them -- these are executed.

The layer names come from `tools.TRACT_KEY` and `acquire`'s own constants rather
than being written here, so a fixture cannot go on testing a layer the snapshot
has stopped producing."""

BYPASS_SOURCE = f"""import sys
first = {tools.TRACT_KEY}.geometry.iloc[0].bounds
sys.stdout.write('%s %s\\n' % (round(first[0], 1), round(first[1], 1)))
sys.stdout.write('easting %s, northing %s\\n' % (round(first[0], 1), round(first[1], 1)))
sys.stdout.write('kept: a plain line\\n')
"""
"""The child guard bypassed on purpose, printing a ROUNDED projected pair twice.

`sys.stdout.write` never reaches the guarded `print`, and one decimal place is
invisible to `tools.COORDINATE_TEXT`. The first line is the shape `BARE_PAIR`
exists for and the second is the shape `LABELLED_PAIR` exists for -- the reviewer
found the second by reading the separator character class, not by running
anything. These are the fixtures that reach the parent's redaction path."""

GEOGRAPHIC_SOURCE = f'''import sys

geographic = {tools.TRACT_KEY}.__REPROJECT__("EPSG:4326")
spot = geographic.geometry.iloc[0].representative_point()
sys.stdout.write("lon {{0}} lat {{1}}\\n".format(round(spot.x, 3), round(spot.y, 3)))
sys.stdout.write("{{0}} {{1}}\\n".format(round(spot.x, 3), round(spot.y, 3)))
sys.stdout.write("kept: a plain line\\n")
'''.replace("__REPROJECT__", "to_" + "crs")
"""Model code that projects a layer back to degrees and prints the result.

The hole the reviewer found. The guard was written on the reasoning that the
working CRS is projected, so a coordinate is seven digits -- true of the frames,
and not true of what a program may do to them. Nothing stops this, and rounded
to the three places the instructions ask for, the pair is invisible to every
rule the first version had.

The reprojection call is assembled rather than written, because
`verify.reprojections` counts that call in this module's source and requires
zero: `align.py` owns the CRS choke point and no other module may reproject for
itself. This module does not; the string describes what the CHILD may do."""

CHILD_STARTED = "child-started.txt"
CHILD_FINISHED = "child-finished.txt"
GRANDCHILD_FINISHED = "grandchild-finished.txt"

SLEEP_SECONDS = 8
TIMEOUT_FIXTURE_S = 2.0
TIMEOUT_SETTLE_S = 12.0
"""The deadline fires at two seconds; the child would finish at eight and its own
child at eight; the check then waits twelve. A child that outlived its deadline
has written its marker well inside that window, so the absence of the marker is
evidence of a kill rather than of a slow machine."""

SLEEP_SOURCE = '''import os
import subprocess
import sys
import time

FOLDER = __FOLDER__
SECONDS = __SECONDS__
open(os.path.join(FOLDER, "child-started.txt"), "w").write("x")
GRANDCHILD = (
    "import os, sys, time\\n"
    "time.sleep(%d)\\n"
    "open(os.path.join(sys.argv[1], 'grandchild-finished.txt'), 'w').write('x')\\n"
) % SECONDS
subprocess.Popen([sys.executable, "-c", GRANDCHILD, FOLDER])
sys.stdout.write("printed before the deadline\\n")
sys.stdout.flush()
time.sleep(SECONDS)
open(os.path.join(FOLDER, "child-finished.txt"), "w").write("x")
'''


def _sleep_source(folder: str) -> str:
    """The timeout fixture, pointed at a directory the check owns and removes.

    It starts a child of its own on purpose. Killing the process this module
    started is not enough: a surviving grandchild holding an inherited handle is
    what hangs `mutate.py`, which runs every `--check` with no timeout.
    """
    return SLEEP_SOURCE.replace("__FOLDER__", repr(folder)).replace(
        "__SECONDS__", repr(SLEEP_SECONDS)
    )


def _guard_checks(box: Sandbox) -> list[tuple[str, bool]]:
    """Invariant 3 on real output from real code against the real layers."""
    runs = {label: box.run(source, timeout_s=90.0) for label, source in GEOMETRY_SOURCES}
    refused = {
        label: (run.error_type is not None or "withheld" in run.stdout)
        for label, run in runs.items()
    }
    # Every line, the withheld markers included. The version of this that skipped
    # them passed while the marker was quoting the coordinate it had just
    # refused: a scan that exempts the guard's own output cannot see the guard
    # leak.
    leaked = {
        label: [line for line in run.stdout.splitlines() if output_faults(line)]
        for label, run in runs.items()
    }
    leaked = {label: lines for label, lines in leaked.items() if lines}

    bypass = box.run(BYPASS_SOURCE, timeout_s=90.0)
    geographic = box.run(GEOGRAPHIC_SOURCE, timeout_s=90.0)
    clean = box.run(CLEAN_SOURCE, timeout_s=90.0)

    for label, run in runs.items():
        print(f"  {label:<24} exit {run.exit_code}  error_type {run.error_type}")
    print(f"  the sys.stdout.write bypass came back as: {bypass.stdout.strip()[:180]!r}")
    print(f"  a layer projected back to degrees came back as: "
          f"{geographic.stdout.strip()[:180]!r}")

    # Every string shape tools.coordinate_faults refuses, this must refuse too --
    # asserted as containment rather than assumed, because the child carries a
    # copy of these rules and a copy that drifts is the one nobody re-reads.
    shared = [
        "INTPTLAT",
        "geometry",
        "bbox from (-80.4535530002726, 32.4825649998057) to south,west order",
    ]
    contained = all(
        not tools.coordinate_faults(line) or output_faults(line) for line in shared
    )

    return [
        ("every way of printing a geometry was refused or withheld",
         all(refused.values()) and len(refused) == len(GEOMETRY_SOURCES)),
        ("no coordinate survived into the stdout the model is handed", not leaked),
        ("printing a geometry raises in the child rather than being mangled",
         all(runs[label].error_type == "GeometryInOutput"
             for label in ("the geometry itself", "its well-known text",
                           "a whole GeoDataFrame", "the layer extent"))),
        ("a rounded projected pair written past the child guard is withheld by the parent",
         bypass.exit_code == 0
         and bypass.stdout.count("withheld") == 2
         and not BARE_PAIR.search(bypass.stdout)
         and not LABELLED_PAIR.search(bypass.stdout)),
        ("a layer really projected back to degrees is withheld at three decimal places",
         geographic.exit_code == 0
         and geographic.stdout.count("withheld") == 2
         and "lon" not in geographic.stdout
         and not any(output_faults(line) for line in geographic.stdout.splitlines())),
        ("the refusal does not quote the coordinate it refused",
         not any(output_faults(line) for line in bypass.stdout.splitlines())
         and not any(
             output_faults(line)
             for run in runs.values()
             for line in run.stderr.splitlines()
         )),
        ("the line beside it survives, so the guard withholds a line and not a stream",
         "kept: a plain line" in bypass.stdout
         and "kept: a plain line" in geographic.stdout),
        ("a clean run is untouched, so the guard is not refusing everything",
         clean.exit_code == 0 and clean.stdout.strip() == "sandbox ready"
         and clean.error_type is None),
        ("every string shape tools.coordinate_faults refuses, this refuses too", contained),
        ("a coordinate rounded to one place, alone on its line, is NOT refused -- "
         "the stated limit of the guard",
         not output_faults("1613477.7")),
        ("the tool result the model actually receives carries no coordinate",
         not tools.coordinate_faults(
             tools.as_sent(tools.run_spatial_code(code=GEOMETRY_SOURCES[0][1], timeout_s=90.0))
         )),
    ]


def _run_checks(box: Sandbox) -> list[tuple[str, bool]]:
    """The real boundary: a real interpreter, a real child, real tracebacks."""
    clean = box.run(CLEAN_SOURCE, timeout_s=90.0)
    named = box.run(NAME_ERROR_SOURCE, timeout_s=90.0)
    nested = box.run(NESTED_SOURCE, timeout_s=90.0)
    broken = box.run(SYNTAX_ERROR_SOURCE, timeout_s=90.0)
    exited = box.run(EXIT_SOURCE, timeout_s=90.0)
    both = box.run(BOTH_STREAMS_SOURCE, timeout_s=90.0)
    imported = box.run(IMPORT_PROJECT_SOURCE, timeout_s=90.0)
    frames = sum(1 for line in nested.stderr.splitlines() if line.strip().startswith("File "))

    print(f"  a clean run: exit {clean.exit_code} in {clean.duration_s:.2f}s, "
          f"stdout {clean.stdout.strip()!r}")
    print(f"  a NameError on line 3: error_type {named.error_type!r}, "
          f"'line 3' in stderr: {'line 3' in named.stderr}")
    print(f"  a three-deep traceback carried {frames} File frame(s)")
    print(f"  sys.exit(3): exit {exited.exit_code}, error_type {exited.error_type!r}, "
          f"stderr {exited.stderr.strip()!r}")

    return [
        ("a program that works reports exit 0 and its own stdout",
         clean.exit_code == 0 and clean.stdout.strip() == "sandbox ready"),
        ("a run is timed and the duration is real",
         clean.duration_s > 0.0 and clean.duration_s < 90.0),
        ("the source that ran is carried back with the result",
         clean.source == CLEAN_SOURCE and named.source == NAME_ERROR_SOURCE),
        ("a NameError is reported as a non-zero exit, not swallowed",
         named.exit_code != 0 and "NameError" in named.stderr),
        ("the error type is classified from the traceback",
         named.error_type == "NameError"),
        ("the traceback names the model's own file and its own line number",
         SOURCE_NAME in named.stderr and "line 3" in named.stderr),
        ("a full traceback comes back, every frame, not a truncated one",
         frames == 4
         and all(name in nested.stderr for name in ("outer", "middle", "inner"))
         and "KeyError" in nested.stderr),
        ("a SyntaxError is reported and the program never runs",
         broken.error_type == "SyntaxError" and not broken.stdout.strip()
         and "line 1" in broken.stderr),
        ("a non-zero exit with no traceback is reported as one",
         exited.exit_code == 3 and exited.error_type == NON_ZERO_EXIT_ERROR
         and "Traceback" not in exited.stderr),
        ("a failed run is told what it had to work with, so a NameError is repairable",
         all(
             name in named.stderr
             for name in (tools.TRACT_KEY, acquire.DATASET_FACILITIES, Col.GEOID)
         )
         and available_names() in named.stderr),
        ("a run that worked is not sent the inventory it did not need",
         available_names() not in clean.stderr and not clean.stderr.strip()),
        ("the error type is read from the traceback, not from what was appended after it",
         named.error_type == "NameError" and exited.error_type == NON_ZERO_EXIT_ERROR
         and nested.error_type == "KeyError"),
        ("stdout and stderr are captured separately when a run writes to both",
         "on stdout" in both.stdout and "on stdout" not in both.stderr
         and "on stderr" in both.stderr and "on stderr" not in both.stdout),
        ("the child cannot import the project and rebuild the analysis",
         imported.exit_code != 0 and imported.error_type == "ModuleNotFoundError"),
    ]


def _taxonomy_checks(box: Sandbox) -> list[tuple[str, bool]]:
    """That each error the criterion asks about is classified as itself."""
    cases = {
        "AttributeError": ATTRIBUTE_ERROR_SOURCE,
        "KeyError": KEY_ERROR_SOURCE,
        SHAPE_MISMATCH_ERROR: SHAPE_ERROR_SOURCE,
        CRS_ERROR: CRS_ERROR_SOURCE,
    }
    got = {name: box.run(source, timeout_s=90.0).error_type for name, source in cases.items()}
    for name, found in got.items():
        print(f"  expected {name:<16} got {found!r}")

    # Classified from text the sandbox did not produce, so the classifier is not
    # being compared against itself.
    written = [
        classify(1, "Traceback (most recent call last):\n  File 'a', line 1\nNameError: x\n", False),
        classify(1, "pyproj.exceptions.CRSError: invalid projection\n", False),
        classify(1, "ValueError: Length of values (2) does not match length of index (3)\n", False),
        classify(1, "", False),
        classify(0, "", False),
        classify(1, "", True),
    ]
    print(f"  hand-written tracebacks classify as {written}")

    return [
        ("an AttributeError on a real layer is classified as one",
         got["AttributeError"] == "AttributeError"),
        ("a KeyError is classified as one", got["KeyError"] == "KeyError"),
        ("a length mismatch is classified as a shape mismatch, not as ValueError",
         got[SHAPE_MISMATCH_ERROR] == SHAPE_MISMATCH_ERROR),
        ("a pyproj CRSError is classified as CRSError, not by its module path",
         got[CRS_ERROR] == CRS_ERROR),
        ("the classifier reads a traceback nothing here produced",
         written[:3] == ["NameError", CRS_ERROR, SHAPE_MISMATCH_ERROR]),
        ("a non-zero exit with no traceback, a success and a timeout are distinguished",
         written[3:] == [NON_ZERO_EXIT_ERROR, None, TIMEOUT_ERROR]),
    ]


def _timeout_checks(box: Sandbox) -> list[tuple[str, bool]]:
    """That the deadline fires, and that nothing is left running behind it.

    The expensive one to get wrong. `mutate.py`'s `run_check` has no timeout of
    its own, so a child that outlives its deadline hangs the mutation harness
    silently and forever -- and a surviving grandchild holding an inherited
    handle does it even after the direct child is killed.
    """
    folder = tempfile.mkdtemp(prefix="geoagent-timeout-")
    try:
        started = time.monotonic()
        run = box.run(_sleep_source(folder), timeout_s=TIMEOUT_FIXTURE_S)
        elapsed = time.monotonic() - started
        time.sleep(TIMEOUT_SETTLE_S)
        started_marker = os.path.exists(os.path.join(folder, CHILD_STARTED))
        finished = os.path.exists(os.path.join(folder, CHILD_FINISHED))
        grandchild = os.path.exists(os.path.join(folder, GRANDCHILD_FINISHED))
    finally:
        shutil.rmtree(folder, ignore_errors=True)

    print(f"  deadline {TIMEOUT_FIXTURE_S}s, run returned after {elapsed:.2f}s, "
          f"exit {run.exit_code}, error_type {run.error_type!r}")
    print(f"  the child started: {started_marker}; "
          f"{TIMEOUT_SETTLE_S}s later it had finished: {finished}; "
          f"its own child had finished: {grandchild}")

    return [
        ("the run really started, so the fixture is not vacuous", started_marker),
        ("the deadline fires and the run comes back", run.error_type == TIMEOUT_ERROR),
        ("a timeout is a non-zero exit", run.exit_code != 0),
        ("the caller is not held past its deadline by more than the kill takes",
         elapsed < TIMEOUT_FIXTURE_S + KILL_TIMEOUT_S),
        ("the child is dead afterwards, not merely abandoned",
         not finished),
        ("the process the child started is dead too, which is what hangs mutate.py",
         not grandchild),
        ("what the child printed before its deadline still comes back",
         "printed before the deadline" in run.stdout),
        ("the timeout says so in stderr rather than returning an empty failure",
         TIMEOUT_ERROR in run.stderr),
    ]


def _workspace_checks(box: Sandbox) -> list[tuple[str, bool]]:
    """That the layers reach the child, from a directory this module owns."""
    space = workspace()
    state = tools.analysis()
    expected = set(state.result.snapshot.frames)
    counts = {
        name: len(frame) for name, frame in state.result.snapshot.frames.items()
    }
    listing = "\n".join(f"print('{name}', len({name}))" for name in sorted(counts)) + "\n"
    seen = box.run(listing, timeout_s=120.0)
    from_child = {}
    for line in seen.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].isdigit():
            from_child[parts[0]] = int(parts[1])

    inside = box.run(CWD_SOURCE, timeout_s=90.0)
    cwd_line = [line for line in inside.stdout.splitlines() if line.startswith("cwd ")]
    owned = os.path.normcase(os.path.realpath(space.directory))
    in_workspace = bool(cwd_line) and os.path.normcase(
        os.path.realpath(cwd_line[0][4:])
    ).startswith(owned)

    print(f"  dumped {len(space.layers)} layer(s) in {space.seconds}s to {space.directory}")
    print(f"  layers the child bound: {sorted(from_child)}")
    print(f"  row counts agree with the shared run: {from_child == counts}")
    print(f"  layers that could not be dumped: {space.unavailable or 'none'}")

    return [
        ("every frame of the shared run reached the child",
         set(from_child) == expected and len(expected) > 1),
        ("the row counts the child read back are the shared run's row counts",
         from_child == counts),
        ("the layer names are the snapshot's, not names written here",
         set(space.layers) | set(space.unavailable) == expected),
        ("the dump is outside the repository, so nothing can be written into data/",
         not str(space.directory).startswith(str(config.PROJECT_ROOT))),
        ("the child's working directory is inside the dump", in_workspace),
        ("a file the child writes lands in its own scratch directory",
         "wrote True" in inside.stdout),
        ("the degraded layer is named to the model as degraded rather than omitted",
         any(spec.get("degraded") for spec in space.layers.values())),
        ("the working coordinate system the child is told about is the shared run's",
         state.result.study_area.working_crs in code_instructions(space)),
        ("the instructions the model is shown carry no coordinate",
         not tools.coordinate_faults(code_instructions(space))
         and not any(output_faults(line) for line in code_instructions(space).splitlines())),
        ("the inventory is one list, sent both before a run and after a failed one",
         available_names(space) in code_instructions(space)
         and all(name in available_names(space) for name in space.layers)),
        ("the identifier the layers are named by is the contract's, not a literal",
         all(Col.GEOID in spec["reportable"]
             for name, spec in space.layers.items()
             if name.endswith(align.JOINED_SUFFIX))
         and any(name.endswith(align.JOINED_SUFFIX) for name in space.layers)),
    ]


def _rebuild_checks() -> list[tuple[str, bool]]:
    """That a retrieval which invalidates the shared run invalidates the dump.

    Driven by moving the key the dump is held on, not by calling the rebuild.
    `tools.invalidate()` would be the honest trigger, but rebuilding the shared
    run costs a whole pipeline pass; what is under test here is that the dump
    notices a different `Analysis`, which is the only thing `tools.invalidate()`
    changes.
    """
    first = workspace()
    first_directory = first.directory
    held = first.built_from
    first.built_from = object()
    second = workspace()
    rebuilt = second.directory != first_directory
    old_gone = not first_directory.exists()
    second.built_from = held
    third = workspace()

    print(f"  after the shared run changed identity: rebuilt {rebuilt}, "
          f"old directory removed {old_gone}")

    return [
        ("a dump built against a different shared run is rebuilt", rebuilt),
        ("the dump it replaces is removed rather than left behind", old_gone),
        ("an unchanged shared run reuses the dump rather than rebuilding it",
         third.directory == second.directory),
        ("the dump is keyed on the shared run itself",
         third.built_from is tools.analysis()),
    ]


SCRIPTED_BROKEN = "print(no_such_name)\n"
SCRIPTED_FIXED = "print('repaired', 2 + 2)\n"


def _repair_loop_checks() -> list[tuple[str, bool]]:
    """LOOP MECHANICS ONLY, with a scripted author in place of the model.

    The model is stubbed here on purpose and this label says so. What is under
    test is the loop: that it calls `run`, that the traceback from attempt one
    reaches the author on attempt two, that attempts are numbered, and that the
    bound holds. The real boundary for `repair_loop` is the model, and it is
    exercised by `python -m src.sandbox "<request>"` in the acceptance gate --
    not here. The subprocess in every run below is real.
    """
    replies = ["```python\n" + SCRIPTED_BROKEN + "```", "```python\n" + SCRIPTED_FIXED + "```"]
    served: list[int] = []

    def scripted(messages: list[dict[str, str]]) -> str:
        served.append(len(messages))
        return replies[min(len(served) - 1, len(replies) - 1)]

    box = Sandbox(author=scripted)
    session = box.repair_loop("print four", max_attempts=3)
    second_turn = box.author_calls[1] if len(box.author_calls) > 1 else []
    fed_back = any(
        "NameError" in message["content"] for message in second_turn
    )

    def always_broken(messages: list[dict[str, str]]) -> str:
        return "```python\n" + SCRIPTED_BROKEN + "```"

    stuck = Sandbox(author=always_broken).repair_loop("never works", max_attempts=3)
    once = Sandbox(author=always_broken).repair_loop("one shot", max_attempts=1)

    def angry(messages: list[dict[str, str]]) -> str:
        raise RuntimeError("no model configured")

    refused = Sandbox(author=angry).repair_loop("no model", max_attempts=3)

    print(f"  scripted repair: {len(session.runs)} attempt(s), succeeded {session.succeeded}, "
          f"author saw {served} message(s) per call")
    print(f"  traceback from attempt 1 reached the author on attempt 2: {fed_back}")
    print(f"  a request that never works: {len(stuck.runs)} attempt(s), "
          f"succeeded {stuck.succeeded}")

    return [
        ("the loop runs again after a failure and stops on the first success",
         len(session.runs) == 2 and session.succeeded and len(served) == 2),
        ("the attempts are numbered in order",
         [run.attempt for run in session.runs] == [1, 2]),
        ("the second attempt is different source, not the first re-run",
         session.runs[0].source != session.runs[1].source
         and session.runs[1].source.strip() == SCRIPTED_FIXED.strip()),
        ("the traceback from the failed attempt is what the author was given next",
         fed_back),
        ("the successful attempt is the one that exits zero",
         session.runs[0].exit_code != 0 and session.runs[1].exit_code == 0),
        ("a request the author never gets right is bounded at max_attempts",
         len(stuck.runs) == 3 and not stuck.succeeded),
        ("the bound is the argument, not a constant",
         len(once.runs) == 1 and not once.succeeded),
        ("an author that raises is recorded as a failed session, not an exception",
         len(refused.runs) == 1 and refused.runs[0].error_type == AUTHOR_ERROR
         and not refused.succeeded),
        ("every session is kept for the instrumentation",
         all(item in SESSIONS for item in (session, stuck, once, refused))),
    ]


def _extraction_checks() -> list[tuple[str, bool]]:
    """That a program is taken out of a reply however the model wrapped it."""
    fenced = extract_code("here you go\n```python\nprint(1)\n```\nhope that helps")
    bare = extract_code("```\nprint(2)\n```")
    naked = extract_code("print(3)")
    unclosed = extract_code("```python\nprint(4)\n")
    print(f"  fenced {fenced!r}  bare {bare!r}  naked {naked!r}  unclosed {unclosed!r}")

    return [
        ("a python fence is unwrapped", fenced == "print(1)\n"),
        ("an unlabelled fence is unwrapped", bare == "print(2)\n"),
        ("a reply with no fence is taken whole", naked == "print(3)\n"),
        ("a fence the model never closed is still usable", unclosed == "print(4)\n"),
        ("the prose around the program is dropped", "hope that helps" not in fenced),
    ]


def _guard_unit_checks() -> list[tuple[str, bool]]:
    """The output rules, on lines written here rather than produced by a run."""
    refused = [
        "POLYGON ((1613477.7123 1234567.8912, 1613480.1 1234570.2))",
        "<POINT (1613477.712 1234567.891)>",
        "-80.4535530002726 32.4825649998057",
        "1613477.7 1234567.9",
        "{'coordinates': [[1.0, 2.0]]}",
        "Index(['GEOID', 'geometry'], dtype='object')",
        "INTPTLAT",
        # Found by the invariant reviewer, all four invisible to the first rule
        # set: a degree pair rounded to the three places the instructions ask
        # for, the same pair with a label, and a projected pair whose separator
        # carries a word.
        "-80.454 32.483",
        "(-80.454, 32.483)",
        "lon -80.454 lat 32.483",
        "easting 1613477.7, northing 1234567.9",
        "centroid_x=1613477.7, centroid_y=1234567.9",
    ]
    allowed = [
        "exposed population 187349 of 420264 residents",
        "mean inundation 1.234 m, max 5.678 m",
        "99 tracts, 261 block groups, 477 facilities",
        "shape (99, 12)",
        "risk 0.87 vulnerability 0.71",
        "the polygon for that tract is small",
        "elapsed 3.0 seconds",
        # Real lines from real repair sessions, and the traceback frames a
        # repair depends on reading. The widened rules have to leave these
        # alone or the guard makes the sandbox unusable rather than safe.
        "Median distance to nearest facility: 890.615",
        "Correlation (pct_no_vehicle, pct_poverty): 0.659",
        "Census Tract 25.03: -1.524",
        "Mean absolute percentage difference: 24.040",
        '  File "/usr/lib/python3.11/site-packages/shapely/geometry/base.py", line 12',
        "    frame = layer.geometry.union_all()",
    ]
    caught = {line: bool(output_faults(line)) for line in refused}
    quiet = {line: bool(output_faults(line)) for line in allowed}
    for line, hit in caught.items():
        print(f"  {'refused' if hit else 'MISSED '}  {line[:64]}")
    for line, hit in quiet.items():
        if hit:
            print(f"  FALSE POSITIVE  {line[:64]} -> {output_faults(line)}")

    long_text = "\n".join(f"line {index} of filler" for index in range(2000))
    cut = guard_stream(long_text, "stdout")
    mixed = guard_stream(
        "keep me\n" + refused[0] + "\nkeep me too\n" + refused[3] + "\n", "stdout"
    )

    return [
        ("every shape a coordinate takes in child output is refused", all(caught.values())),
        ("nothing this project actually reports is refused", not any(quiet.values())),
        ("a stream over the bound is cut and says how much it withheld",
         len(cut) < len(long_text) and "characters of stdout withheld" in cut),
        ("a stream under the bound is untouched",
         guard_stream("short\n", "stdout") == "short\n"),
        ("one bad line costs one line",
         "keep me" in mixed and "keep me too" in mixed and "withheld" in mixed
         and len(mixed.splitlines()) == 4),
        ("the withheld line says which line and why",
         "stdout line 2 withheld" in mixed and "stdout line 4 withheld" in mixed),
        ("the guarded stream is clean when scanned again, markers included",
         not any(output_faults(line) for line in mixed.splitlines())),
    ]


def _metrics_checks() -> list[tuple[str, bool]]:
    """The instrumentation, over sessions whose outcome is stated here by hand."""
    def made(attempt: int, exit_code: int, error: str | None) -> CodeRun:
        return CodeRun(
            attempt=attempt, source="", exit_code=exit_code, stdout="",
            stderr="", duration_s=0.1, error_type=error,
        )

    repaired = CodeSession(
        request="a", runs=[made(1, 1, "NameError"), made(2, 0, None)], succeeded=True
    )
    never = CodeSession(
        request="b",
        runs=[made(1, 1, "KeyError"), made(2, 1, "KeyError"), made(3, 1, TIMEOUT_ERROR)],
    )
    first_time = CodeSession(request="c", runs=[made(1, 0, None)], succeeded=True)
    sessions = [repaired, never, first_time]
    runs = [run for item in sessions for run in item.runs]
    measured = metrics(runs=runs, sessions=sessions)
    print(f"  {measured}")

    return [
        ("every run is counted", measured["runs"] == 6 and measured["runs_failed"] == 4),
        ("the first-attempt failure rate is over sessions, not over runs",
         measured["first_attempt_failures"] == 2
         and measured["first_run_failure_rate"] == round(2 / 3, 3)),
        ("the repair rate is over the sessions that failed first, not over all of them",
         measured["repaired"] == 1 and measured["repair_rate"] == round(1 / 2, 3)),
        ("attempts are reported per request",
         measured["attempts_total"] == 6 and measured["attempts_mean"] == 2.0),
        ("the taxonomy names which errors, not only how many",
         measured["error_taxonomy"] == {"KeyError": 2, "NameError": 1, TIMEOUT_ERROR: 1}),
        ("a run this process really performed is in the module's own record",
         len(RUNS) > 0 and metrics()["runs"] == len(RUNS)),
        ("the report prints the taxonomy it measured",
         "NameError" in format_metrics(measured) and "KeyError" in format_metrics(measured)),
    ]


def _size_checks(box: Sandbox) -> list[tuple[str, bool]]:
    """That a run cannot crowd the turns the model has left."""
    big = box.run(BIG_OUTPUT_SOURCE, timeout_s=90.0)
    result = tools.run_spatial_code(code=BIG_OUTPUT_SOURCE, timeout_s=90.0)
    payload = len(json.dumps(result, default=str))
    print(f"  a run printing 20,000 lines came back as {len(big.stdout):,} characters; "
          f"the tool result serialised to {payload:,} bytes")

    return [
        ("a stream longer than the bound is cut", len(big.stdout) <= STREAM_LIMIT + 200),
        ("the cut is stated rather than silent", "withheld" in big.stdout),
        ("the tool result stays under the size every other tool is held to",
         payload < 24_000),
        ("the run still reports what it was", big.exit_code == 0),
    ]


def _surface_checks() -> list[tuple[str, bool]]:
    """That the tool this module backs has stopped being pending."""
    pending = tools.pending_tools()
    faults = tools.surface_faults()
    result = tools.run_spatial_code(code=CLEAN_SOURCE, timeout_s=90.0)
    print(f"  pending tools now: {list(pending)}")
    print(f"  surface faults: {faults or 'none'}")
    print(f"  run_spatial_code returned: {result}")

    return [
        ("run_spatial_code is no longer pending", "run_spatial_code" not in pending),
        ("the tool surface still has no faults", not faults),
        ("the tool runs code rather than returning a refusal",
         result.get("error") is None and result["exit_code"] == 0
         and "sandbox ready" in result["stdout"]),
        ("the tool reports the five fields the contract gives it",
         set(result) == {"exit_code", "stdout", "stderr", "duration_s", "error_type"}),
        ("this module satisfies the frozen Sandbox protocol",
         isinstance(Sandbox(), contracts.Sandbox)),
        ("a Sandbox is constructible with no arguments, which is how the tool builds one",
         isinstance(Sandbox().author, ModelAuthor)),
        ("the tool surface is still the eleven frozen names",
         set(tools.TOOL_FUNCTIONS) == set(contracts.TOOL_NAMES)),
    ]


def _self_check() -> int:
    print("SANDBOX -- model-written code in a child process\n")
    box = Sandbox()

    checks = _guard_unit_checks()
    print()
    checks += _extraction_checks()
    print()
    checks += _workspace_checks(box)
    print()
    checks += _run_checks(box)
    print()
    checks += _taxonomy_checks(box)
    print()
    checks += _guard_checks(box)
    print()
    checks += _size_checks(box)
    print()
    checks += _timeout_checks(box)
    print()
    checks += _repair_loop_checks()
    print()
    checks += _metrics_checks()
    print()
    checks += _rebuild_checks()
    print()
    checks += _surface_checks()
    print()
    checks += verify.discipline_checks(sys.modules[__name__])
    print()
    print(format_metrics())
    print()
    return verify.report(checks)


if __name__ == "__main__":
    raise SystemExit(_self_check() if "--check" in sys.argv[1:] else main())
