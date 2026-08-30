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

from . import agent, config, tools

QUESTIONS = [
    "Which communities in this county should be prioritised for evacuation support "
    "under a three metre storm surge, and who does that choice leave out?",
    "What data is this analysis built on, where did each layer come from, and what "
    "did the cleaning stage have to fix before it could be used?",
]


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

    config.ensure_dirs()
    questions = sys.argv[1:] or QUESTIONS
    results = [agent.run_agent(question) for question in questions]

    if len(results) > 1:
        print("=" * 68)
        print("RUN TOTALS")
        print(f"QUESTIONS ASKED:      {len(results)}")
        print(f"TOTAL LLM CALLS:      {sum(r['llm_calls'] for r in results)}")
        print(f"TOTAL GIS TOOL CALLS: {sum(r['gis_tool_calls'] for r in results)}")
        print(f"TOTAL DURATION:       {sum(r['duration_seconds'] for r in results):.1f}s")
        print("=" * 68)

    return 0 if all(r["stop_reason"] == "final_answer" for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
