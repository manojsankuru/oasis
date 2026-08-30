import sys

from . import agent, config, tools

QUESTIONS = [
    "Find schools within 3 km of hospitals.",
]


def main() -> int:
    faults = tools.surface_faults()
    if faults:
        print("REFUSING TO RUN -- the tool surface is not wired up yet.")
        for fault in faults:
            print(f"  - {fault}")
        print()
        print(
            "Session S9 implements contracts.TOOL_NAMES in src/tools.py and rewrites\n"
            "src/schemas.py to match. Until then this command would start an agent that\n"
            "can be offered tools nothing can run, and would exit 0 whenever the model\n"
            "declined to call one -- which reads as a working system."
        )
        print()
        print("What does work today, with no API key and no model:")
        print("  python -m src.align --check")
        print("  python -m src.hazard --check")
        print("  python -m src.vulnerability --check")
        print("  python -m src.risk --check")
        print("  python -m src.pipeline          # writes the real risk table")
        return 1

    missing = config.missing_settings()
    if missing:
        print("Missing settings: " + ", ".join(missing))
        print("Copy .env.example to .env and fill it in.")
        return 1

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
