"""Check helpers shared by every module that carries a `--check`.

Three of these scans existed once in `align.py`, written against a
hand-maintained tuple of the parts to look at. The S7 review found the hole that
shape always has: a scan built from an inclusion list exempts whatever nobody
remembers to add, and it fails silently when that happens. These versions
enumerate what they scan from the module object itself, so a function added
tomorrow is covered without anyone choosing to cover it.

The reason they live here rather than being copied into four modules is the same
lesson one level up. Four copies of a guard drift, and the copy that drifts is
the one nobody re-reads.
"""

from __future__ import annotations

import contextlib
import inspect
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Iterator

from . import acquire, config

METRIC_OPERATIONS: dict[str, str] = {
    "to_crs": r"\.to_crs\(",
    "area": r"\.area\b",
    "buffer": r"\.buffer\(",
    "centroid": r"\.centroid\b",
    "distance": r"\.distance\(",
    "length": r"\.length\b",
}
"""Every call invariant 2 covers. Each one answers in degrees on a geographic
frame, silently, with no error and no warning."""

CRS_HELPER = "to_working_crs"
"""The single choke point invariant 2 names. A function that performs a metric
operation has to route its input through this first; a function that does not
perform one has no reason to mention it."""


def defined_here(module: ModuleType) -> list[tuple[str, Any]]:
    """Every function, method and property the module itself defines.

    Enumerated from the module object rather than from a list of things to look
    at, including the methods and properties of every class defined here. An
    imported function is skipped by comparing `__module__`, so a helper pulled in
    from `align` is that module's responsibility and is scanned there.
    """
    found: list[tuple[str, Any]] = []
    for name, member in vars(module).items():
        if inspect.isfunction(member) and member.__module__ == module.__name__:
            found.append((name, member))
        elif inspect.isclass(member) and member.__module__ == module.__name__:
            for attribute, value in vars(member).items():
                if attribute.startswith("__"):
                    continue
                if isinstance(value, property):
                    value = value.fget
                elif isinstance(value, (staticmethod, classmethod)):
                    value = value.__func__
                if inspect.isfunction(value):
                    found.append((f"{name}.{attribute}", value))
    return found


def unannotated(module: ModuleType) -> list[str]:
    """Functions missing a hint on a parameter or on the return."""
    missing: list[str] = []
    for name, fn in defined_here(module):
        signature = inspect.signature(fn)
        gaps = [
            item.name
            for item in signature.parameters.values()
            if item.name not in ("self", "cls")
            and item.annotation is inspect.Signature.empty
        ]
        if signature.return_annotation is inspect.Signature.empty:
            gaps.append("-> return")
        if gaps:
            missing.append(f"{name}: {gaps}")
    return missing


def metric_bypasses(module: ModuleType) -> list[str]:
    """Functions that perform a metric operation without routing through the helper.

    The rule is per function rather than per module. `align.py` can assert that
    exactly one `.to_crs(` exists in its implementation because it owns the
    helper; a module that legitimately buffers or measures a distance cannot,
    and counting metric calls to zero would only push the work somewhere the scan
    does not look.

    So: a function whose body contains one of these calls must also call
    `to_working_crs`. The helper is idempotent, so routing an already-projected
    frame through it costs nothing, which is what makes the rule cheap enough to
    hold everywhere rather than argue about case by case.

    The scan reads source rather than bytecode, so a docstring that spells one of
    these calls out trips it. That is a false positive worth keeping: prose can
    name an operation in words without writing the call, and the alternative --
    excluding docstrings -- would let a real call hide inside a string literal.
    This function's own docstring is written that way for exactly that reason.
    """
    offenders: list[str] = []
    for name, fn in defined_here(module):
        try:
            source = inspect.getsource(fn)
        except OSError:
            continue
        used = [
            call
            for call, pattern in METRIC_OPERATIONS.items()
            if re.search(pattern, source)
        ]
        if used and CRS_HELPER not in source:
            offenders.append(f"{name}: {used} without {CRS_HELPER}")
    return offenders


def reprojections(module: ModuleType) -> int:
    """How many times this module calls `.to_crs(` directly.

    Must be zero everywhere except `align.py`, which owns `to_working_crs`. A
    module that reprojects for itself has taken the choke point out of the path
    while still looking, to `metric_bypasses`, as though it had not: a function
    can call both.
    """
    source = inspect.getsource(module)
    return len(re.findall(METRIC_OPERATIONS["to_crs"], source))


def study_area_tokens(module: ModuleType) -> list[str]:
    """Tokens of either configured study area that appear in this module's source.

    Invariant 5: a literal county name, FIPS pair or state code outside
    `config.py` breaks the transfer run. Both configured areas are scanned, not
    just the active one, so a module hardcoded to the transfer county is caught
    by a run pointed at the primary.
    """
    tokens = {
        token
        for area in (config.STUDY_AREA, config.TRANSFER_AREA)
        for token in (
            area.county_geoid,
            area.state_fips + area.county_fips,
            *re.findall(r"[A-Z][a-z]+", area.name.split(",")[0]),
        )
        if token not in acquire._PLACE_WORDS
    }
    source = inspect.getsource(module)
    return sorted(token for token in tokens if token in source)


def discipline_checks(module: ModuleType) -> list[tuple[str, bool]]:
    """The scans every module in this project has to pass, run as one block.

    The last one turns the scanner on itself. A guard nothing audits is the same
    shape of hole as a list nobody maintains: this module could grow an
    unannotated helper, or a hardcoded county, and every module that trusts it
    would still report a clean sweep.
    """
    bypasses = metric_bypasses(module)
    for line in bypasses:
        print(f"  metric operation outside the CRS helper: {line}")
    reprojected = reprojections(module)
    print(f"  direct .to_crs( calls in {module.__name__}: {reprojected}")

    missing = unannotated(module)
    for line in missing:
        print(f"  unannotated: {line}")

    tokens = study_area_tokens(module)
    print(f"  source scan for a hardcoded study area: {tokens or 'none'}")

    scanned = len(defined_here(module))
    here = sys.modules[__name__]
    own = unannotated(here) + study_area_tokens(here) + metric_bypasses(here)
    print(
        f"  {scanned} function(s) and method(s) scanned, enumerated from the module; "
        f"{len(defined_here(here))} in the scanner itself, {len(own)} fault(s) there"
    )
    return [
        ("every metric operation routes through to_working_crs", not bypasses),
        ("the module never reprojects for itself", reprojected == 0),
        ("every function and method carries type hints", not missing),
        ("no token of either configured study area appears in this module", not tokens),
        ("the discipline scan found something to scan", scanned > 0),
        ("the scan module holds itself to the same rules it applies", not own),
    ]


def refuses(call: Any, kind: type[BaseException], phrase: str) -> bool:
    """Did this call refuse for the stated reason, rather than merely raise?

    The phrase is not decoration. `except ValueError: return True` tests that
    something went wrong, not that the right thing went wrong -- three S7
    mutations survived by raising the correct exception type from the wrong
    place, and matching the message is what closed that.
    """
    try:
        call()
    except kind as exc:
        return phrase in str(exc)
    except Exception:
        return False
    return False


SCRATCH_SETTINGS: tuple[str, ...] = ("OUTPUTS_DIR", "DERIVED_DIR", "LOGS_DIR")
"""The `config` directories an analysis build or an agent run writes into.

Six committed files live under the first two. Rebuilt on Linux, the tables come
back with LF where the commits hold CRLF and the raster deflates to different
bytes, so a check that rebuilt them in place dirtied the checkout with identical
content."""

WATCHED_DIRECTORIES: tuple[str, ...] = ("OUTPUTS_DIR", "DERIVED_DIR", "SNAPSHOT_DIR")
"""Where the committed outputs and data live, read off `config` at call time."""


@contextlib.contextmanager
def scratch_builds(prefix: str) -> Iterator[Path]:
    """Send every build inside the block to a temporary directory, then restore."""
    saved = {name: getattr(config, name) for name in SCRATCH_SETTINGS}
    with tempfile.TemporaryDirectory(prefix=prefix, ignore_cleanup_errors=True) as where:
        scratch = Path(where)
        try:
            for name, value in saved.items():
                setattr(config, name, scratch / value.name)
            yield scratch
        finally:
            for name, value in saved.items():
                setattr(config, name, value)


def file_stamps(paths: list[Path]) -> dict[str, tuple[int, int]]:
    """Size and modification time of each of these files that exists."""
    return {
        path.as_posix(): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in paths
        if path.is_file()
    }


def files_under(*directories: Path) -> list[Path]:
    return [
        path
        for directory in directories
        if directory.is_dir()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    ]


def git_listing(root: Path, *arguments: str) -> list[str] | None:
    """One NUL-separated git listing under `root`, or None when git cannot answer."""
    try:
        done = subprocess.run(
            ["git", "-C", str(root), *arguments], capture_output=True, check=True, timeout=120
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return [item for item in done.stdout.decode("utf-8", "replace").split("\0") if item]


def git_ignores(root: Path, path: Path) -> bool | None:
    """Do git's ignore rules exclude this path? None when git cannot answer."""
    try:
        done = subprocess.run(
            ["git", "-C", str(root), "check-ignore", "-q", "--no-index", str(path)],
            capture_output=True, timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return {0: True, 1: False}.get(done.returncode)


def committed_state(directories: tuple[Path, ...]) -> tuple[dict[str, tuple[int, int]], list[str]] | None:
    """Stamps of the files git tracks under these directories, and git's status for them.

    Tracked files only, so a live run writing its own ignored transcript while a
    check runs is not a change; the status lines catch an unignored file appearing."""
    project = config.PROJECT_ROOT
    try:
        scopes = [directory.resolve().relative_to(project.resolve()).as_posix() for directory in directories]
    except ValueError:
        return None
    tracked = git_listing(project, "ls-files", "-z", "--", *scopes)
    status = git_listing(
        project, "--no-optional-locks", "status", "--porcelain", "-z", "--untracked-files=all", "--", *scopes
    )
    if tracked is None or status is None:
        return None
    return file_stamps([project / name for name in tracked]), status


def checked_in_scratch(
    prefix: str,
    run: Callable[[Path], list[tuple[str, bool]]],
    watched: tuple[Path, ...] | None = None,
) -> list[tuple[str, bool]]:
    """Run a suite with its builds in scratch, and check that no committed file moved."""
    if watched is None:
        directories = tuple(getattr(config, name) for name in WATCHED_DIRECTORIES)
        state: Callable[[], tuple[dict[str, tuple[int, int]], list[str]] | None] = (
            lambda: committed_state(directories)
        )
    else:
        directories = watched
        state = lambda: (file_stamps(files_under(*watched)), [])
    first = state()
    if first is None:
        directories = watched or directories
        state = lambda: (file_stamps(files_under(*directories)), [])
        first = state()
    before, status_before = first
    with scratch_builds(prefix) as scratch:
        checks = run(scratch)
        loaded = sys.modules.get(f"{__package__}.tools")
        if loaded is not None:
            loaded.invalidate()
    after, status_after = state() or ({}, [])
    moved = sorted(name for name in before.keys() | after.keys() if before.get(name) != after.get(name))
    appeared = sorted(set(status_after) - set(status_before))
    print()
    print(f"  {len(before)} watched file(s) under {', '.join(item.name for item in directories)}; "
          f"changed while the checks ran: {len(moved)}; new status lines: {len(appeared)}")
    for name in (moved + appeared)[:10]:
        print(f"    {name}")
    return checks + [
        ("no tracked file under outputs/, data/derived/ or data/snapshot/ changed while the checks ran",
         bool(before) and not moved and not appeared),
    ]


def lf_normalised(content: bytes) -> bytes:
    """Text bytes with CRLF read as LF. A NUL in the first 8000 bytes marks binary, kept as is."""
    return content if b"\0" in content[:8000] else content.replace(b"\r\n", b"\n")


def report(checks: list[tuple[str, bool]]) -> int:
    """Print every check and return the exit code the module should use."""
    print()
    failed = 0
    for label, ok in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
        failed += 0 if ok else 1
    print("\nall checks passed" if failed == 0 else f"\n{failed} check(s) failed")
    return 0 if failed == 0 else 1
