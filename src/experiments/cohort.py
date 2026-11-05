"""The declared agent cohort: fixed questions, three conditions, and every cell kept.

    python -m src.experiments.cohort --declare   # write the manifest from the questions
    python -m src.experiments.cohort --run       # run every cell, in the declared order
    python -m src.experiments.cohort --resume    # continue after an interruption
    python -m src.experiments.cohort --check     # offline, on a stub cohort in a temporary directory

The questions and the scripted replies are committed with the manifest before any
run, which is the pre-registration. A failed run is a result: it is recorded and
never retried. Each transcript goes through `experiments.redact` into
`outputs/paper/cohort/`, so the evidence `report.py` computes from can be committed.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Iterator, Mapping

from .. import agent, config, llm_client, sandbox, tools, verify
from . import redact

QUESTIONS_NAME = "cohort_questions.json"
REPLIES_NAME = "cohort_replies.json"
MANIFEST_NAME = "agent_evaluation_manifest.json"
TRANSCRIPTS_NAME = "cohort"
SCHEMA_VERSION = 1
AREAS = ("primary", "transfer")
FINISHED = ("completed", "error")
SCRIPT_VARIABLE = tools.SCRIPTED_REPLIES_ENV


@dataclass(frozen=True, slots=True)
class Condition:
    name: str
    use_critic: bool
    code_attempts: int | None
    definition: str


CONDITIONS: tuple[Condition, ...] = (
    Condition(
        "full", True, None,
        "the shipped loop: the critic checks every draft and asks for up to "
        f"{agent.MAX_REVISIONS} revisions, validate_answer answers, and run_spatial_code "
        "runs every call, bounded only by the iteration bound",
    ),
    Condition(
        "critic_off", False, None,
        "no critic pass over any draft, so the first draft is the final answer, and "
        "validate_answer refuses with error critic_off; run_spatial_code as in full",
    ),
    Condition(
        "repair_off", True, 1,
        "the critic as in full; run_spatial_code runs a call until one code run fails, "
        "then refuses every later call in the run with error repair_off, so no "
        "traceback is ever repaired",
    ),
)
"""The three conditions, defined by what the loop does rather than by name."""

FROZEN_FILES: tuple[str, ...] = (
    "src/agent.py",
    "src/critic.py",
    "src/tools.py",
    "src/schemas.py",
    "src/llm_client.py",
    "src/config.py",
    "src/sandbox.py",
    "src/contracts.py",
)
"""Fingerprinted at declaration and checked before every cell, with the scripted replies."""

MEASURES: dict[str, str] = {
    "question_count": "questions with a finished run in the condition",
    "llm_calls": "sum of totals.llm_calls over the condition's completed runs",
    "gis_tool_calls": "sum of totals.gis_tool_calls, every tool call the loop executed",
    "generated_code_calls": "run_spatial_code results logged, refusals included",
    "successful_code_runs": "run_spatial_code results with exit_code 0",
    "failed_code_runs": "run_spatial_code results with a nonzero exit_code",
    "error_taxonomy": "failed code runs counted by error_type",
    "repair_attempts": "code runs that follow a failed code run in the same run",
    "repair_successes": "code sessions whose first run failed and a later run succeeded; a session ends at a success",
    "critic_cycles": "critic_report steps logged",
    "critic_firings": "critic_report steps that did not pass",
    "revisions_requested": "revision_request steps logged",
    "first_draft_pass_rate": "share of completed runs whose first answer draft is not empty and passes the declared critic, re-run offline on the steps logged before that draft",
    "final_draft_pass_rate": "share of completed runs whose final answer is not empty and passes the declared critic, re-run offline on every logged step",
    "numbers_checked": "numbers the declared critic checked in the final answers",
    "numbers_traceable": "of those, the numbers that traced to a logged tool result",
    "mean_turns": "mean totals.llm_calls per completed run",
    "mean_duration": "mean totals.duration_seconds per completed run",
    "numerals_seen": "numerals in the final answers, checked or masked",
    "numerals_masked_by_reason": "numerals the masks removed from the final answers, by mask",
    "who_loses_named_displaced_unit_rate": "of completed runs that called compare_scenarios, the share whose final answer names at least one GEOID a compare_scenarios result listed in displaced_geoids",
    "who_loses_gate_pass_rate": "of the same runs, the share whose final answer is not empty and draws no 'does not say who loses' finding from the declared critic",
    "tool_credit_errors": "completed runs whose final answer names a tool, in backticks or as 'name tool', that the run never called",
    "weights_before_ranking_rate": "of completed runs whose final answer states a nonzero weight a ranking tool reported, in a sentence that names that weight's indicator or component, and names a GEOID that tool ranked, the share where the first such weight comes before the first such GEOID",
    "validate_answer_calls": "validate_answer calls the model made, refusals included",
    "harness_errors": "cells whose run raised instead of finishing, recorded and never retried",
    "stop_reasons": "completed runs counted by stop_reason",
}
"""Every measure the report computes, declared with its definition before any run."""

ISOLATION = (
    "each primary-county cell runs on its own copy of the committed snapshot, with its "
    "build outputs inside that copy and the analysis built before the timed run, so a "
    "retrieval in one cell never reaches another; behaviour scenario C poisons a copy of "
    "that copy; cold-start cells run in demo.cold_start's empty workspace; the committed "
    "snapshot's fingerprint is compared before and after every cell, and a change stops "
    "the cohort; ask_user_preferences never prompts a terminal, so an unscripted cell "
    "proceeds on the default weighting"
)

SNAPSHOT_SETTINGS: tuple[str, ...] = (
    "PROJECT_ROOT", "DATA_DIR", "SNAPSHOT_DIR", "DERIVED_DIR", "MANIFEST_PATH", "OUTPUTS_DIR",
)
"""The `config` paths a primary cell moves into its own copy of the snapshot."""

Runner = Callable[[dict[str, Any], Condition, str, list[str] | None], dict[str, Any]]
GitState = Callable[[], tuple[str, list[str]]]


@dataclass(frozen=True, slots=True)
class CohortPaths:
    root: Path
    paper: Path

    @property
    def questions(self) -> Path:
        return self.paper / QUESTIONS_NAME

    @property
    def replies(self) -> Path:
        return self.paper / REPLIES_NAME

    @property
    def manifest(self) -> Path:
        return self.paper / MANIFEST_NAME

    @property
    def transcripts(self) -> Path:
        return self.paper / TRANSCRIPTS_NAME

    def shown(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.root.resolve()).as_posix()
        except ValueError:
            return path.as_posix()


def default_paths() -> CohortPaths:
    return CohortPaths(root=config.PROJECT_ROOT, paper=config.PAPER_DIR)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def fingerprint(path: Path) -> str:
    """The sha256 of a file's bytes with CRLF read as LF, so both checkouts agree."""
    return hashlib.sha256(verify.lf_normalised(path.read_bytes())).hexdigest()


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(redact.serialised(payload))


def git_state(root: Path) -> tuple[str, list[str]]:
    """HEAD, and every status line that says the checkout is not that commit."""
    head = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    status = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain", "--untracked-files=all"],
        capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    return head, status


def blocking_lines(status: list[str], allowed: set[str]) -> list[str]:
    """Status lines that make HEAD a false description of the code that would run.

    A tracked change anywhere does. An untracked file does only under `src/`, where
    it is code. The declaration's own files are allowed, since they are what the
    declaration commit adds."""
    blocking: list[str] = []
    for line in status:
        path = line[3:].strip().strip('"')
        if path in allowed:
            continue
        if line.startswith("??") and not path.startswith("src/"):
            continue
        blocking.append(line)
    return blocking


def load_questions(path: Path) -> dict[str, Any]:
    document = json.loads(path.read_text(encoding="utf-8"))
    questions = document.get("questions")
    if not isinstance(questions, list) or not questions:
        raise ValueError(f"{path.name} holds no questions list")
    seen: set[str] = set()
    for item in questions:
        identifier = item.get("id")
        if not isinstance(identifier, str) or not identifier or identifier in seen:
            raise ValueError(f"{path.name}: question id {identifier!r} is missing or repeated")
        seen.add(identifier)
        if not str(item.get("text", "")).strip():
            raise ValueError(f"{path.name}: question {identifier} has no text")
        if item.get("area") not in AREAS:
            raise ValueError(f"{path.name}: question {identifier} names area {item.get('area')!r}, not one of {AREAS}")
        if not str(item.get("category", "")).strip():
            raise ValueError(f"{path.name}: question {identifier} has no category")
        scenario = item.get("behaviour_scenario")
        if scenario is not None and behaviour_scenario(scenario).question != item["text"]:
            raise ValueError(
                f"{path.name}: question {identifier} names behaviour scenario {scenario}, "
                "but its text is not that scenario's question"
            )
    return document


def behaviour_scenario(identifier: str) -> Any:
    from . import behaviour

    found = [item for item in behaviour.SCENARIOS if item.id == identifier]
    if not found:
        raise ValueError(f"behaviour.py has no scenario {identifier!r}")
    return found[0]


def load_replies(path: Path, questions: list[dict[str, Any]]) -> dict[str, list[str]]:
    replies = json.loads(path.read_text(encoding="utf-8"))
    known = {item["id"] for item in questions}
    if not isinstance(replies, dict):
        raise ValueError(f"{path.name} must map question ids to lists of reply strings")
    for identifier, listed in replies.items():
        if identifier not in known:
            raise ValueError(f"{path.name} scripts a reply for {identifier}, which is not a declared question")
        if not isinstance(listed, list) or not listed or not all(isinstance(item, str) for item in listed):
            raise ValueError(f"{path.name}: the replies for {identifier} must be a non-empty list of strings")
    return replies


def model_settings(secrets: Mapping[str, str]) -> dict[str, Any]:
    """What the runs will call, with the endpoint redacted."""
    endpoint = redact.redact(config.API_BASE_URL, secrets).payload
    return {
        "provider": config.PROVIDER,
        "model": config.MODEL,
        "location": config.GOOGLE_CLOUD_LOCATION if config.PROVIDER == "vertex" else None,
        "endpoint": endpoint,
        "tool_choice": "auto",
        "temperature": "provider default; llm_client sets none",
    }


def loop_bounds() -> dict[str, Any]:
    return {
        "max_iterations": config.MAX_ITERATIONS,
        "max_revisions": agent.MAX_REVISIONS,
        "sandbox_timeout_s": sandbox.DEFAULT_TIMEOUT_S,
        "request_timeout_s": config.REQUEST_TIMEOUT_S,
        "retry_bound": config.MAX_RETRIES,
    }


def declaration(
    paths: CohortPaths, head: str, secrets: Mapping[str, str], now: str
) -> dict[str, Any]:
    document = load_questions(paths.questions)
    questions = document["questions"]
    replies = load_replies(paths.replies, questions)
    cells = [
        {"question_id": item["id"], "condition": condition.name}
        for item in questions
        for condition in CONDITIONS
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "declared agent evaluation cohort",
        "declared_at": now,
        "git_sha": head,
        "model": model_settings(secrets),
        "bounds": loop_bounds(),
        "isolation": ISOLATION,
        "frozen_files": {
            name: fingerprint(paths.root / name) for name in FROZEN_FILES
        },
        "questions_file": {"path": paths.shown(paths.questions), "sha256": fingerprint(paths.questions)},
        "replies_file": {"path": paths.shown(paths.replies), "sha256": fingerprint(paths.replies)},
        "conditions": [
            {"name": item.name, "use_critic": item.use_critic, "code_attempts": item.code_attempts,
             "definition": item.definition}
            for item in CONDITIONS
        ],
        "questions": questions,
        "excluded_questions": document.get("excluded", []),
        "scripted_replies": replies,
        "order": "question by question, each question under full, then critic_off, then repair_off",
        "cells": cells,
        "cell_count": len(cells),
        "measures": dict(MEASURES),
        "transcripts": paths.shown(paths.transcripts),
        "runs": [],
    }


def declare(
    paths: CohortPaths,
    *,
    state: GitState | None = None,
    secrets: Mapping[str, str] | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    """Write the manifest, or refuse: a dirty checkout, or a manifest that holds runs."""
    secrets = redact.secrets_from(os.environ) if secrets is None else secrets
    head, status = (state or (lambda: git_state(paths.root)))()
    allowed = {paths.shown(item) for item in (paths.questions, paths.replies, paths.manifest)}
    blocking = blocking_lines(status, allowed)
    if blocking:
        raise ValueError(
            "the checkout is dirty, so HEAD would not describe the code that runs: "
            + "; ".join(blocking[:5])
        )
    if paths.manifest.is_file():
        existing = json.loads(paths.manifest.read_text(encoding="utf-8"))
        if existing.get("runs"):
            raise ValueError(
                f"{paths.manifest.name} already holds {len(existing['runs'])} run(s); a declared "
                "cohort with results is never declared again"
            )
    manifest = declaration(paths, head, secrets, now or utc_now())
    remaining = redact.leaks(manifest, secrets)
    if remaining:
        raise ValueError(f"the manifest would carry {', '.join(remaining)}; nothing was written")
    write_json(paths.manifest, manifest)
    return manifest


def frozen_problems(
    manifest: dict[str, Any], paths: CohortPaths, secrets: Mapping[str, str] | None = None
) -> list[str]:
    """Every way the checkout differs from the one the cohort was declared on."""
    secrets = redact.secrets_from(os.environ) if secrets is None else secrets
    problems = [
        f"{name} changed since the declaration"
        for name, digest in manifest.get("frozen_files", {}).items()
        if not (paths.root / name).is_file() or fingerprint(paths.root / name) != digest
    ]
    for key, path in (("questions_file", paths.questions), ("replies_file", paths.replies)):
        if not path.is_file() or fingerprint(path) != manifest.get(key, {}).get("sha256"):
            problems.append(f"{path.name} changed since the declaration")
    if paths.questions.is_file() and paths.replies.is_file():
        document = load_questions(paths.questions)
        if manifest.get("questions") != document["questions"]:
            problems.append(f"the manifest's questions differ from {paths.questions.name}")
        if manifest.get("scripted_replies") != load_replies(paths.replies, document["questions"]):
            problems.append(f"the manifest's scripted replies differ from {paths.replies.name}")
    declared = manifest.get("model", {})
    current = model_settings(secrets)
    if declared != current:
        changed = sorted(key for key in set(declared) | set(current) if declared.get(key) != current.get(key))
        problems.append(f"the model settings differ from the declaration in {changed}")
    if manifest.get("bounds") != loop_bounds():
        problems.append("the loop bounds differ from the declaration")
    return problems


def code_problems(manifest: dict[str, Any], paths: CohortPaths) -> list[str]:
    """Code under `src/` that differs from the declared commit, tracked or not."""
    sha = str(manifest.get("git_sha", ""))
    changed = subprocess.run(
        ["git", "-C", str(paths.root), "diff", "--name-only", sha, "--", "src"],
        capture_output=True, text=True,
    )
    if changed.returncode != 0:
        return [f"the declared commit {sha[:12]} is not in this checkout"]
    untracked = subprocess.run(
        ["git", "-C", str(paths.root), "status", "--porcelain", "--untracked-files=all", "--", "src"],
        capture_output=True, text=True,
    ).stdout.splitlines()
    return [f"{name} differs from the declared commit" for name in changed.stdout.split()] + [
        f"{line[3:]} is untracked code" for line in untracked if line.startswith("??")
    ]


def primary_fingerprint() -> dict[str, Any]:
    """The committed primary snapshot, as `transfer.fingerprint_primary` reads it."""
    from . import transfer

    return transfer.fingerprint_primary(
        snapshot_dir=config.SNAPSHOT_DIR,
        manifest_path=config.MANIFEST_PATH,
        project_root=config.PROJECT_ROOT,
        area=config.STUDY_AREA,
    )


@contextlib.contextmanager
def snapshot_copy() -> Iterator[Path]:
    """Run inside a copy of the committed snapshot, then put every path back."""
    originals = {name: getattr(config, name) for name in SNAPSHOT_SETTINGS}
    with tempfile.TemporaryDirectory(prefix="cohort-snapshot-", ignore_cleanup_errors=True) as folder:
        copy = Path(folder)
        snapshot = copy / "data" / "snapshot"
        shutil.copytree(originals["SNAPSHOT_DIR"], snapshot)
        try:
            config.PROJECT_ROOT = copy
            config.DATA_DIR = copy / "data"
            config.SNAPSHOT_DIR = snapshot
            config.DERIVED_DIR = copy / "data" / "derived"
            config.MANIFEST_PATH = snapshot / Path(originals["MANIFEST_PATH"]).name
            config.OUTPUTS_DIR = copy / "outputs"
            config.DERIVED_DIR.mkdir(parents=True, exist_ok=True)
            config.OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
            tools.invalidate()
            yield copy
        finally:
            for name, value in originals.items():
                setattr(config, name, value)
            tools.invalidate()
            sandbox.discard_workspace()


def condition_named(name: str) -> Condition:
    return next(item for item in CONDITIONS if item.name == name)


def finished_cells(manifest: dict[str, Any]) -> set[tuple[str, str]]:
    return {
        (entry["question_id"], entry["condition"])
        for entry in manifest.get("runs", [])
        if entry.get("status") in FINISHED
    }


def run_cells(
    paths: CohortPaths,
    *,
    resume: bool,
    runner: Runner | None = None,
    checks: Callable[[dict[str, Any], CohortPaths], list[str]] | None = None,
    secrets: Mapping[str, str] | None = None,
    snapshot: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    """Run every unfinished cell in the declared order, saving the manifest after each.

    The declaration is checked again before every cell, and the committed snapshot
    is fingerprinted around every cell: a change stops the cohort."""
    secrets = redact.secrets_from(os.environ) if secrets is None else secrets
    checks = checks or (lambda found, where: frozen_problems(found, where) + code_problems(found, where))
    snapshot = snapshot or primary_fingerprint
    manifest = json.loads(paths.manifest.read_text(encoding="utf-8"))
    if manifest.get("runs") and not resume:
        raise ValueError(f"{paths.manifest.name} already holds runs; continue them with --resume")
    if resume and not manifest.get("runs"):
        raise ValueError(f"{paths.manifest.name} holds no runs to resume; start them with --run")
    problems = checks(manifest, paths)
    if problems:
        raise ValueError("refusing to run: " + "; ".join(problems))
    for entry in manifest["runs"]:
        if entry.get("status") == "started":
            entry["status"] = "interrupted"
    questions = {item["id"]: item for item in manifest["questions"]}
    replies: dict[str, list[str]] = manifest.get("scripted_replies", {})
    done = finished_cells(manifest)
    for cell in manifest["cells"]:
        key = (cell["question_id"], cell["condition"])
        if key in done:
            continue
        problems = checks(manifest, paths)
        if problems:
            write_json(paths.manifest, manifest)
            raise ValueError("refusing to run: " + "; ".join(problems))
        condition = condition_named(cell["condition"])
        run_id = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}_{cell['question_id']}_{condition.name}"
        entry: dict[str, Any] = {
            "question_id": cell["question_id"],
            "condition": condition.name,
            "run_id": run_id,
            "status": "started",
            "started_at": utc_now(),
        }
        manifest["runs"].append(entry)
        write_json(paths.manifest, manifest)
        before = snapshot()
        try:
            outcome = (runner or run_cell)(questions[cell["question_id"]], condition, run_id, replies.get(cell["question_id"]))
            entry.update(collect(outcome, paths, secrets))
        except Exception as exc:
            entry.update({
                "status": "error",
                "error": {"type": type(exc).__name__, "message": redact.redact(str(exc), secrets).payload},
            })
        entry["finished_at"] = utc_now()
        entry["primary_snapshot_unchanged"] = snapshot() == before
        write_json(paths.manifest, manifest)
        if not entry["primary_snapshot_unchanged"]:
            raise RuntimeError(
                f"cell {cell['question_id']} {condition.name} changed the committed primary "
                "snapshot; the cohort stops so no later cell runs on other data"
            )
    return manifest


def collect(outcome: dict[str, Any], paths: CohortPaths, secrets: Mapping[str, str]) -> dict[str, Any]:
    """The finished run's record, with its transcript redacted into the committed folder."""
    source = Path(str(outcome.get("transcript_path", "")))
    if not source.is_file():
        return {"status": "error", "error": {"type": "MissingTranscript", "message": "the run wrote no transcript"}}
    destination = paths.transcripts / source.name
    replaced = redact.redact_file(source, destination, secrets)
    record: dict[str, Any] = {
        "status": "completed",
        "stop_reason": outcome.get("stop_reason"),
        "transcript": paths.shown(destination),
        "redacted": replaced,
    }
    if "isolation_held" in outcome:
        record["isolation_held"] = bool(outcome["isolation_held"])
    if outcome.get("poisoned_copy"):
        record["poisoned_copy"] = True
    if outcome.get("snapshot_copy"):
        record["snapshot_copy"] = True
    return record


def run_cell(
    question: dict[str, Any], condition: Condition, run_id: str, replies: list[str] | None
) -> dict[str, Any]:
    """One live run: the declared question under one condition, with its scripted replies."""
    from .. import demo

    saved = os.environ.get(tools.SCRIPTED_REPLIES_ENV)
    elicit = tools.ELICIT
    keep = Path(config.OUTPUTS_DIR)
    with tempfile.TemporaryDirectory(prefix="cohort-replies-") as folder:
        if replies:
            script = Path(folder) / "replies.json"
            script.write_text(json.dumps(replies), encoding="utf-8")
            os.environ[tools.SCRIPTED_REPLIES_ENV] = str(script)
        else:
            os.environ.pop(tools.SCRIPTED_REPLIES_ENV, None)
        options = {
            "verbose": False,
            "use_critic": condition.use_critic,
            "code_attempts": condition.code_attempts,
            "run_id": run_id,
        }
        tools.ELICIT = False
        try:
            if question["area"] == "transfer":
                results, held = demo.cold_start([question["text"]], **options)
                return {**results[0], "isolation_held": held}
            scenario = question.get("behaviour_scenario")
            with snapshot_copy():
                extra: dict[str, Any] = {}
                with contextlib.ExitStack() as stack:
                    if scenario is not None and behaviour_scenario(scenario).poisoned:
                        from . import behaviour

                        poison = stack.enter_context(behaviour.poisoned_snapshot())
                        extra = {"poisoned_copy": True, "poisoned_features": poison["features"]}
                    tools.analysis()
                    result = agent.run_agent(question["text"], **options)
                source = Path(result["transcript_path"])
                keep.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, keep / source.name)
                return {**result, **extra, "transcript_path": str(keep / source.name), "snapshot_copy": True}
        finally:
            tools.ELICIT = elicit
            if saved is None:
                os.environ.pop(tools.SCRIPTED_REPLIES_ENV, None)
            else:
                os.environ[tools.SCRIPTED_REPLIES_ENV] = saved


def _stub_paths(root: Path) -> CohortPaths:
    paths = CohortPaths(root=root, paper=root / "outputs" / "paper-stub")
    for name in FROZEN_FILES:
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((config.PROJECT_ROOT / name).read_bytes())
    write_json(paths.questions, {
        "stub": True,
        "questions": [
            {"id": "s1", "category": "stub", "area": "primary", "text": "stub question one", "expected_tools": []},
            {"id": "s2", "category": "stub", "area": "primary", "text": "stub question two", "expected_tools": []},
        ],
        "excluded": [{"id": "s3", "text": "a stub question left out", "reason": "stub"}],
    })
    write_json(paths.replies, {"s1": ["a stub reply"]})
    return paths


STUB_ENDPOINT = (
    "https://us-central1-aiplatform.googleapis.com/v1beta1/"
    f"projects/{redact.FAKE_PROJECT}/locations/us-central1/endpoints/openapi"
)


def _stub_runner(calls: list[tuple[str, str, str | None]], folder: Path, fail: set[tuple[str, str]]) -> Runner:
    def runner(question: dict[str, Any], condition: Condition, run_id: str, replies: list[str] | None) -> dict[str, Any]:
        calls.append((question["id"], condition.name, replies[0] if replies else None))
        if (question["id"], condition.name) in fail:
            raise RuntimeError(f"stub failure calling {STUB_ENDPOINT} for {run_id}")
        transcript = folder / f"run_{run_id}.json"
        transcript.write_text(json.dumps({
            "run_id": run_id,
            "final_answer": "stub answer",
            "steps": [{"step": "run_start", "payload": {
                "base_url": STUB_ENDPOINT,
                "use_critic": condition.use_critic, "code_attempts": condition.code_attempts}}],
        }), encoding="utf-8")
        return {"transcript_path": str(transcript), "stop_reason": "final_answer"}

    return runner


def _harness_checks() -> list[tuple[str, bool]]:
    secrets = {redact.FAKE_KEY: "CENSUS_API_KEY"}
    clean_state: GitState = lambda: ("0" * 40, ["?? docs/notes.md", "?? outputs/paper-stub/cohort_questions.json"])
    with tempfile.TemporaryDirectory(prefix="cohort-check-") as folder:
        root = Path(folder)
        paths = _stub_paths(root)
        stubs = root / "stub-transcripts"
        stubs.mkdir()
        try:
            declare(paths, state=lambda: ("0" * 40, [" M src/tools.py"]), secrets=secrets)
            dirty_refused = False
        except ValueError as exc:
            dirty_refused = "dirty" in str(exc)
        try:
            declare(paths, state=lambda: ("0" * 40, ["?? src/new_module.py"]), secrets=secrets)
            untracked_refused = False
        except ValueError as exc:
            untracked_refused = "dirty" in str(exc)
        manifest = declare(paths, state=clean_state, secrets=secrets, now="2026-11-04T00:00:00+00:00")
        on_disk = json.loads(paths.manifest.read_text(encoding="utf-8"))

        calls: list[tuple[str, str, str | None]] = []
        no_checks: Callable[[dict[str, Any], CohortPaths], list[str]] = lambda found, where: []
        first = run_cells(paths, resume=False, runner=_stub_runner(calls, stubs, {("s1", "critic_off")}),
                          checks=no_checks, secrets=secrets)
        statuses = [(entry["question_id"], entry["condition"], entry["status"]) for entry in first["runs"]]
        written = sorted(item.name for item in paths.transcripts.iterdir()) if paths.transcripts.is_dir() else []
        redacted = [(root / entry["transcript"]).read_text(encoding="utf-8")
                    for entry in first["runs"] if entry["status"] == "completed"]
        failed = next(entry for entry in first["runs"] if entry["status"] == "error")

        try:
            run_cells(paths, resume=False, runner=_stub_runner([], stubs, set()), checks=no_checks, secrets=secrets)
            rerun_refused = False
        except ValueError as exc:
            rerun_refused = "--resume" in str(exc)
        try:
            declare(paths, state=clean_state, secrets=secrets)
            redeclare_refused = False
        except ValueError as exc:
            redeclare_refused = "already holds" in str(exc)

        interrupted = json.loads(paths.manifest.read_text(encoding="utf-8"))
        interrupted["runs"] = interrupted["runs"][:2]
        interrupted["runs"][1]["status"] = "started"
        write_json(paths.manifest, interrupted)
        again: list[tuple[str, str, str | None]] = []
        resumed = run_cells(paths, resume=True, runner=_stub_runner(again, stubs, set()), checks=no_checks, secrets=secrets)

        frozen = json.loads(paths.manifest.read_text(encoding="utf-8"))
        frozen_clean = frozen_problems(frozen, paths, secrets)
        moved_model = {**frozen, "model": {**frozen["model"], "location": "elsewhere-1"}}
        moved_bounds = {**frozen, "bounds": {**frozen["bounds"], "max_iterations": 99}}
        moved_question = {**frozen, "questions": [{**frozen["questions"][0], "text": "an edited copy"}] + frozen["questions"][1:]}
        moved_reply = {**frozen, "scripted_replies": {"s1": ["an edited reply"]}}
        branch_problems = {
            "model": frozen_problems(moved_model, paths, secrets),
            "bounds": frozen_problems(moved_bounds, paths, secrets),
            "question": frozen_problems(moved_question, paths, secrets),
            "reply": frozen_problems(moved_reply, paths, secrets),
        }
        (root / "src" / "critic.py").write_bytes((root / "src" / "critic.py").read_bytes() + b"\n")
        frozen_edited = frozen_problems(frozen, paths, secrets)
        try:
            run_cells(paths, resume=True, runner=_stub_runner([], stubs, set()), secrets=secrets,
                      checks=lambda found, where: frozen_problems(found, where, secrets))
            edited_refused = False
        except ValueError as exc:
            edited_refused = "critic.py changed" in str(exc)
        (root / "src" / "critic.py").write_bytes((config.PROJECT_ROOT / "src" / "critic.py").read_bytes())
        replies_edited = json.loads(paths.replies.read_text(encoding="utf-8"))
        write_json(paths.replies, {"s1": ["a reply edited after the declaration"]})
        replies_problems = frozen_problems(frozen, paths, secrets)
        write_json(paths.replies, replies_edited)

        fresh = CohortPaths(root=root / "fresh", paper=root / "fresh" / "outputs" / "paper-stub")
        for name in FROZEN_FILES:
            (fresh.root / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(root / name, fresh.root / name)
        shutil.copytree(paths.paper, fresh.paper, ignore=shutil.ignore_patterns(TRANSCRIPTS_NAME, MANIFEST_NAME))
        declare(fresh, state=clean_state, secrets=secrets)
        verdicts = iter([[], []])
        mid_calls: list[tuple[str, str, str | None]] = []
        try:
            run_cells(fresh, resume=False, runner=_stub_runner(mid_calls, stubs, set()), secrets=secrets,
                      checks=lambda found, where: next(verdicts, ["src/tools.py changed since the declaration"]))
            mid_refused = False
        except ValueError as exc:
            mid_refused = "tools.py changed" in str(exc)
        mid_runs = json.loads(fresh.manifest.read_text(encoding="utf-8"))["runs"]

        moved = CohortPaths(root=root / "moved", paper=root / "moved" / "outputs" / "paper-stub")
        for name in FROZEN_FILES:
            (moved.root / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(root / name, moved.root / name)
        shutil.copytree(paths.paper, moved.paper, ignore=shutil.ignore_patterns(TRANSCRIPTS_NAME, MANIFEST_NAME))
        declare(moved, state=clean_state, secrets=secrets)
        readings = iter(["before", "after"])
        try:
            run_cells(moved, resume=False, runner=_stub_runner([], stubs, set()), secrets=secrets,
                      checks=no_checks, snapshot=lambda: next(readings, "after"))
            moved_stopped = False
        except RuntimeError as exc:
            moved_stopped = "changed the committed primary snapshot" in str(exc)
        moved_runs = json.loads(moved.manifest.read_text(encoding="utf-8"))["runs"]

        repository = root / "repository"
        (repository / "src").mkdir(parents=True)
        (repository / "src" / "kept.py").write_text("VALUE = 1\n", encoding="utf-8")
        identity = ["-c", "user.name=cohort check", "-c", "user.email=cohort-check@localhost"]
        for command in (["init", "-q"], ["add", "src/kept.py"], [*identity, "commit", "-q", "-m", "stub"]):
            subprocess.run(["git", "-C", str(repository), *command], capture_output=True, check=True, timeout=120)
        sha = subprocess.run(["git", "-C", str(repository), "rev-parse", "HEAD"],
                             capture_output=True, text=True, check=True, timeout=120).stdout.strip()
        at_sha = CohortPaths(root=repository, paper=repository / "paper")
        code_clean = code_problems({"git_sha": sha}, at_sha)
        (repository / "src" / "kept.py").write_text("VALUE = 2\n", encoding="utf-8")
        code_edited = code_problems({"git_sha": sha}, at_sha)
        (repository / "src" / "kept.py").write_text("VALUE = 1\n", encoding="utf-8")
        (repository / "src" / "added.py").write_text("NEW = 1\n", encoding="utf-8")
        code_untracked = code_problems({"git_sha": sha}, at_sha)
        code_unknown = code_problems({"git_sha": "f" * 40}, at_sha)

    cell_order = [(cell["question_id"], cell["condition"]) for cell in manifest["cells"]]
    print(f"  declared {manifest['cell_count']} cells in order {cell_order}")
    print(f"  first pass: {statuses}")
    print(f"  resume ran {again}; interrupted entries kept: "
          f"{sum(entry['status'] == 'interrupted' for entry in resumed['runs'])}")
    print(f"  with critic.py edited: {frozen_edited}")
    expected_order = [(q, c.name) for q in ("s1", "s2") for c in CONDITIONS]
    return [
        ("a checkout with a tracked change is refused a declaration", dirty_refused),
        ("so is one with untracked code under src/", untracked_refused),
        ("a clean checkout declares every question under every condition, question by question",
         cell_order == expected_order and manifest["cell_count"] == 6),
        ("each condition is declared by what the loop does, with its switches",
         [(item["name"], item["use_critic"], item["code_attempts"]) for item in manifest["conditions"]]
         == [("full", True, None), ("critic_off", False, None), ("repair_off", True, 1)]
         and all(item["definition"] for item in manifest["conditions"])),
        ("every measure the report computes is declared with its definition",
         set(manifest["measures"]) == set(MEASURES) and all(manifest["measures"].values())),
        ("the declaration records HEAD, the frozen files, the replies and the excluded questions",
         manifest["git_sha"] == "0" * 40 and set(manifest["frozen_files"]) == set(FROZEN_FILES)
         and manifest["scripted_replies"] == {"s1": ["a stub reply"]}
         and [item["id"] for item in manifest["excluded_questions"]] == ["s3"]),
        ("the manifest on disk is the one returned, with no runs yet", on_disk == manifest and on_disk["runs"] == []),
        ("every cell ran once, in order, and a run that raised is a recorded result",
         [(q, c) for q, c, _ in statuses] == expected_order and len(calls) == 6
         and [s for q, c, s in statuses if (q, c) == ("s1", "critic_off")] == ["error"]),
        ("the error is kept with its type and its message redacted",
         failed["error"]["type"] == "RuntimeError" and redact.FAKE_PROJECT not in failed["error"]["message"]),
        ("the scripted replies reach their own question and no other",
         {q: r for q, _, r in calls} == {"s1": "a stub reply", "s2": None}),
        ("every completed transcript was redacted into the committed folder",
         len(written) == 5 and all(redact.FAKE_PROJECT not in item and redact.PROJECT_PLACEHOLDER in item
                                   for item in redacted)),
        ("a second --run on a manifest with runs is refused and points at --resume", rerun_refused),
        ("a cohort with runs is never declared again", redeclare_refused),
        ("--resume reruns only unfinished cells and keeps the interrupted entry",
         [(q, c) for q, c, _ in again] == expected_order[1:2] + expected_order[2:]
         and sum(entry["status"] == "interrupted" for entry in resumed["runs"]) == 1),
        ("a checkout matching the declaration raises no frozen-file problem", frozen_clean == []),
        ("an edited frozen file is named, and the run is refused", frozen_edited == ["src/critic.py changed since the declaration"] and edited_refused),
        ("a changed model setting is named by key", branch_problems["model"] == ["the model settings differ from the declaration in ['location']"]),
        ("changed loop bounds are refused", branch_problems["bounds"] == ["the loop bounds differ from the declaration"]),
        ("a manifest whose question copy was edited is refused",
         branch_problems["question"] == ["the manifest's questions differ from cohort_questions.json"]),
        ("a manifest whose reply copy was edited is refused",
         branch_problems["reply"] == ["the manifest's scripted replies differ from cohort_replies.json"]),
        ("an edited reply file is refused, both as a file and against the manifest's copy",
         replies_problems == ["cohort_replies.json changed since the declaration",
                              "the manifest's scripted replies differ from cohort_replies.json"]),
        ("the declaration is checked again before every cell, so an edit mid-run stops the next cell",
         mid_refused and len(mid_calls) == 1 and [entry["status"] for entry in mid_runs] == ["completed"]),
        ("a cell that moves the committed snapshot is recorded and stops the cohort",
         moved_stopped and len(moved_runs) == 1 and moved_runs[0]["primary_snapshot_unchanged"] is False),
        ("code at the declared commit raises no code problem", code_clean == []),
        ("an edited file under src/ is named against the declared commit", code_edited == ["src/kept.py differs from the declared commit"]),
        ("untracked code under src/ is named", code_untracked == ["src/added.py is untracked code"]),
        ("a declared commit this checkout does not hold is refused",
         code_unknown == [f"the declared commit {'f' * 12} is not in this checkout"]),
    ]


def _live_cell_checks() -> list[tuple[str, bool]]:
    """Real cells through `run_cell` and `collect`, with only the model stubbed."""
    replies = ["stub reply"]
    seen: list[str | None] = []
    during: list[tuple[str, bool]] = []

    def scripted_client() -> Any:
        return object()

    def scripted_chat(client: Any, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None,
                      model: str | None = None) -> Any:
        named = os.environ.get(SCRIPT_VARIABLE)
        seen.append(Path(named).read_text(encoding="utf-8") if named and Path(named).is_file() else named)
        during.append((Path(config.SNAPSHOT_DIR).resolve().as_posix(), agent.tools.ELICIT))
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content="No figure is reported here.", tool_calls=None),
            finish_reason="stop")])

    real_client, real_chat = llm_client.make_client, llm_client.chat
    llm_client.make_client, llm_client.chat = scripted_client, scripted_chat
    secrets = redact.secrets_from(os.environ)
    committed = Path(config.SNAPSHOT_DIR).resolve().as_posix()
    settings = {name: getattr(config, name) for name in SNAPSHOT_SETTINGS}
    elicit_before = tools.ELICIT
    try:
        with tempfile.TemporaryDirectory(prefix="cohort-cell-") as folder:
            paths = CohortPaths(root=Path(folder), paper=Path(folder) / "paper-stub")
            outcome = run_cell({"id": "s1", "area": "primary", "text": "stub question"},
                               condition_named("critic_off"), "cohort-check-cell", replies)
            record = collect(outcome, paths, secrets)
            payload = json.loads((Path(folder) / record["transcript"]).read_text(encoding="utf-8"))
            os.environ[SCRIPT_VARIABLE] = str(Path(folder) / "a-script-left-over.json")
            run_cell({"id": "s2", "area": "primary", "text": "stub question two"},
                     condition_named("full"), "cohort-check-cell-two", None)
            left_over = os.environ.get(SCRIPT_VARIABLE)
    finally:
        llm_client.make_client, llm_client.chat = real_client, real_chat
        os.environ.pop(SCRIPT_VARIABLE, None)
    start = next(step["payload"] for step in payload["steps"] if step.get("step") == "run_start")
    kinds = [step.get("step") for step in payload["steps"]]
    print(f"  a real cell under critic_off: {record['status']}, steps {kinds}, redacted {record['redacted']}")
    return [
        ("a real cell finishes and its transcript lands in the cohort folder",
         record["status"] == "completed" and record["transcript"].startswith("paper-stub/cohort/")),
        ("the transcript names its condition in run_start",
         start.get("use_critic") is False and start.get("code_attempts") is None),
        ("critic_off logs no critic pass", "critic_report" not in kinds),
        ("the redacted transcript names no project and holds no secret this environment knows",
         redact.project_ids(payload) == set() and redact.leaks(payload, secrets) == []),
        ("a cell with scripted replies runs with exactly its own replies",
         seen[:1] == [json.dumps(replies)]),
        ("a cell with none runs with no script, even when one was set before it", seen[1:] == [None]),
        ("and the variable is put back as the cell found it", left_over is not None and left_over.endswith("a-script-left-over.json")),
        ("each primary cell runs on its own copy of the snapshot, never the committed one",
         len(during) == 2 and all(where != committed for where, _ in during) and during[0][0] != during[1][0]),
        ("and no cell can prompt a terminal", all(flag is False for _, flag in during)),
        ("the snapshot paths and the prompt setting are put back after each cell",
         {name: getattr(config, name) for name in SNAPSHOT_SETTINGS} == settings and tools.ELICIT == elicit_before),
        ("the raw transcript is kept outside the copy, which is gone",
         Path(outcome["transcript_path"]).parent == Path(config.OUTPUTS_DIR) and outcome.get("snapshot_copy") is True),
    ]


def _run_checks(scratch: Path) -> list[tuple[str, bool]]:
    checks = _harness_checks()
    print()
    checks += _live_cell_checks()
    print()
    checks += verify.discipline_checks(sys.modules[__name__])
    return checks


def _self_check() -> int:
    print("COHORT -- declared questions, three conditions, every cell a result\n")
    saved = os.environ.pop(tools.SCRIPTED_REPLIES_ENV, None)
    try:
        checks = verify.checked_in_scratch("geoagent-cohort-check-", _run_checks)
    finally:
        if saved is not None:
            os.environ[tools.SCRIPTED_REPLIES_ENV] = saved
    return verify.report(checks)


def main(argv: list[str]) -> int:
    paths = default_paths()
    if "--declare" in argv:
        manifest = declare(paths)
        print(f"declared {manifest['cell_count']} cells over {len(manifest['questions'])} questions "
              f"and {len(manifest['conditions'])} conditions at {manifest['git_sha'][:12]} "
              f"into {paths.shown(paths.manifest)}")
        return 0
    if "--run" in argv or "--resume" in argv:
        manifest = run_cells(paths, resume="--resume" in argv)
        finished = finished_cells(manifest)
        print(f"{len(finished)} of {manifest['cell_count']} cells finished; manifest {paths.shown(paths.manifest)}")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(_self_check() if "--check" in sys.argv[1:] else main(sys.argv[1:]))
