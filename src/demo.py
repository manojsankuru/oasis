"""Run the agent on the built-in questions, or on one given on the command line.

This command refused to start for eight sessions, and the refusal was the right
behaviour at the time: `schemas.py` advertised four tools nothing could execute,
so the command exited 0 whenever the model happened not to call one, and a
reviewer running the one command in CLAUDE.md would have seen a system that
looked finished and was not. `tools.surface_faults()` is what made that loud, and
it is still called below -- the guard was kept and its reason was removed.

What replaced it is a banner rather than a refusal. A tool whose backing module
is not built yet is advertised as unavailable and returns a refusal naming the
module, so the run continues and the answer says what it could not do.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from . import agent, config, sandbox, tools
from .experiments import transfer
from .registry import Registry

QUESTIONS = [
    "Which communities in this county should be prioritised for evacuation support "
    "under a three metre storm surge, and who does that choice leave out?",
    "What data is this analysis built on, where did each layer come from, and what "
    "did the cleaning stage have to fix before it could be used?",
]

AREA_SETTINGS: tuple[str, ...] = ("STUDY_AREA", "OUTPUTS_DIR", "PAPER_DIR", "LOGS_DIR")


def cold_start(questions: list[str], **options: Any) -> tuple[list[dict[str, Any]], bool]:
    originals = transfer.capture_config_paths()
    extras = {name: getattr(config, name) for name in AREA_SETTINGS}
    root = Path(originals["PROJECT_ROOT"]).resolve()
    primary = config.STUDY_AREA
    before = transfer.fingerprint_primary(
        snapshot_dir=originals["SNAPSHOT_DIR"],
        manifest_path=originals["MANIFEST_PATH"],
        project_root=root,
        area=primary,
    )
    run_id = transfer.new_run_id()
    area_root = Path(config.OUTPUTS_DIR) / "agent-area" / run_id
    paths = transfer.build_paths(run_id, outputs_dir=area_root, paper_dir=area_root / "paper")
    work = paths.work_root
    print(f"cold start on {config.TRANSFER_AREA.name}: empty workspace {work}")
    results: list[dict[str, Any]] = []
    maps = tools.MAP_DIRECTORY
    try:
        config.STUDY_AREA = config.TRANSFER_AREA
        config.OUTPUTS_DIR = work / "outputs"
        tools.MAP_DIRECTORY = work / "outputs" / "maps"
        config.PAPER_DIR = work / "outputs" / "paper"
        config.LOGS_DIR = work / "logs"
        with transfer.isolated_config(paths, originals):
            tools.invalidate()
            Registry(config.STUDY_AREA).save_manifest()
            config.ensure_dirs()
            results = [agent.run_agent(question, **options) for question in questions]
    finally:
        for name, value in extras.items():
            setattr(config, name, value)
        tools.MAP_DIRECTORY = maps
        tools.invalidate()
        sandbox.discard_workspace()
        after = transfer.fingerprint_primary(
            snapshot_dir=originals["SNAPSHOT_DIR"],
            manifest_path=originals["MANIFEST_PATH"],
            project_root=root,
            area=primary,
        )
        restored = (
            transfer.capture_config_paths() == originals
            and all(getattr(config, name) == value for name, value in extras.items())
            and tools.MAP_DIRECTORY == maps
        )
        print(f"primary snapshot fingerprint unchanged: {before == after}; settings restored: {restored}")
        print(f"run log and workspace kept under {area_root}")
    return results, before == after and restored


def main() -> int:
    faults = tools.surface_faults()
    if faults:
        print("REFUSING TO RUN -- the advertised tool surface and the executable one disagree.")
        for fault in faults:
            print(f"  - {fault}")
        return 1

    missing = config.missing_settings()
    if missing:
        print("Missing settings: " + ", ".join(missing))
        print("Copy .env.example to .env and fill it in.")
        return 1

    pending = tools.pending_tools()
    print(f"tool surface: {len(tools.TOOL_FUNCTIONS)} tool(s), no fault between what is "
          f"advertised and what can run")
    if pending:
        print(f"  {len(pending)} not built in this checkout, advertised as unavailable:")
        for name in pending:
            print(f"    {name} -> src/{tools.BACKING_MODULES[name]}.py")
    for warning in config.setting_warnings():
        print(f"  warning: {warning}")
    print()

    arguments = sys.argv[1:]
    isolated = True
    if arguments[:1] == ["--area"]:
        if arguments[1:2] != ["transfer"]:
            print("--area takes one value, transfer, which runs on config.TRANSFER_AREA")
            return 2
        results, isolated = cold_start(arguments[2:] or QUESTIONS[:1])
    else:
        config.ensure_dirs()
        questions = arguments or QUESTIONS
        results = [agent.run_agent(question) for question in questions]

    if len(results) > 1:
        print("=" * 68)
        print("RUN TOTALS")
        print(f"QUESTIONS ASKED:      {len(results)}")
        print(f"TOTAL LLM CALLS:      {sum(r['llm_calls'] for r in results)}")
        print(f"TOTAL GIS TOOL CALLS: {sum(r['gis_tool_calls'] for r in results)}")
        print(f"TOTAL DURATION:       {sum(r['duration_seconds'] for r in results):.1f}s")
        print("=" * 68)

    finished = bool(results) and all(r["stop_reason"] == "final_answer" for r in results)
    return 0 if finished and isolated else 1


if __name__ == "__main__":
    sys.exit(main())
