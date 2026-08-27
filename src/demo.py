import sys

from . import agent, config

QUESTIONS = [
    "Find schools within 3 km of hospitals.",
]


def main():
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
