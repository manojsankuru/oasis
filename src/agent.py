"""The loop: ask, call tools, log every step, answer.

Two hundred readable lines and no framework, which is a stated feature of this
project rather than an omission. Everything model-shaped lives in `llm_client`;
everything data-shaped lives in `tools`; this file owns the turn structure, the
counters and the log.

**The system prompt is generated, not written.** It used to name four tools that
no longer existed, describe shelters this system does not model, and promise that
every distance it returned was in kilometres when it returns no distance at all.
That drift is not a mistake somebody made once -- it is what happens to a hand-
written inventory of a surface that changes in another file. `system_prompt()`
builds the inventory from `contracts.TOOL_NAMES` and the descriptions the model
is already being sent, so the prose and the specs cannot disagree, and a tool
whose backing module is missing is named as unavailable rather than discovered by
spending one of a small number of turns on it.
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Any

from pydantic import ValidationError

from . import config, llm_client, schemas, tools, trace
from .contracts import TOOL_NAMES, ToolResult

ROLE = """You are an autonomous geospatial risk analyst supporting hurricane
evacuation and mitigation planning. You answer questions about which communities
should be prioritised, using data this system retrieved from public services and
cleaned in code.

You cannot see the data. The only way to learn anything about it is to call the
tools below, and every number you report must come from a tool result. Never
estimate one, never carry one over from general knowledge, and never round a
number into a different number."""

RULES = """How to work:

- Call list_datasets first if you do not already know which layers exist and what
  they are called. It carries the source URL, vintage and licence of every layer,
  which is what you cite.
- A priority ordering is a value judgement, not a fact. Call ask_user_preferences
  BEFORE you decide one, use the weighting it names, and say which weighting you
  used and where it came from.
- Report who loses. compare_scenarios names the units that another weighting
  would have prioritised and yours does not. An answer that lists winners and not
  losers has hidden the trade-off it was asked about.
- Say what was left out. Every ranking tool reports how many units it could not
  score and why; carry that into your answer rather than presenting a top ten as
  though it were the whole county.
- Depths and surge heights are in metres. Distances used inside the tools are in
  metres too, measured in a projected coordinate system, so they are real ground
  measurements. No tool returns kilometres.
- The risk score is county-relative: it says which units here are worst, never
  whether here is bad in absolute terms. Say so when you quote it.
- Keep the final answer short. Name the tools you used, quote the exact numbers
  they returned, and state the scenario and weighting the answer is conditional on."""


def tool_inventory(pending: tuple[str, ...] = ()) -> str:
    """The tool list as prose, built from the frozen names and their descriptions."""
    lines: list[str] = []
    for name in TOOL_NAMES:
        mark = "  [UNAVAILABLE ON THIS RUN] " if name in pending else "  "
        lines.append(f"{mark}{name}: {schemas.TOOL_DESCRIPTIONS[name]}")
    return "\n".join(lines)


def system_prompt() -> str:
    """The whole system message, with the inventory generated from the surface."""
    pending = tools.pending_tools()
    parts = [ROLE, "", "Tools available to you:", "", tool_inventory(pending)]
    if pending:
        parts += [
            "",
            f"{len(pending)} tool(s) above are marked unavailable because the module "
            "that runs them is not built in this checkout. Do not call them; answer "
            "with the ones that work and say what you could not do.",
        ]
    parts += ["", RULES]
    return "\n".join(parts)


SYSTEM_PROMPT: str = system_prompt()
"""Built once at import for anything that wants to read it. `run_agent` rebuilds
it per run, so a tool surface that changed since import is the one described."""


class RunLogger:
    """One JSONL line per step, untruncated, written as it happens."""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self.path = config.LOGS_DIR / f"run_{run_id}.jsonl"
        self.steps: list[dict[str, Any]] = []

    def log(
        self, step_type: str, payload: dict[str, Any], iteration: int | None = None
    ) -> dict[str, Any]:
        record = {
            "run_id": self.run_id,
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "iteration": iteration,
            "step": step_type,
            "payload": payload,
        }
        self.steps.append(record)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, default=str) + "\n")
        return record


def execute_tool(name: str, arguments: dict[str, Any]) -> ToolResult:
    """Validate the arguments, run the tool, and turn any failure into a result.

    The `KeyError` from a name present in `schemas.TOOL_ARG_MODELS` and absent
    from `tools.TOOL_FUNCTIONS` would arrive here as `{"error": "KeyError"}` --
    the type of the mistake rather than the mistake. `tools.surface_faults()` is
    what makes that disagreement loud at startup instead, which is why the bare
    handler below is safe to keep: it is for the failures nobody predicted, not
    for the one that was predictable.
    """
    model = schemas.TOOL_ARG_MODELS.get(name)
    if model is None:
        return {
            "error": f"unknown tool '{name}'",
            "available_tools": sorted(schemas.TOOL_ARG_MODELS),
        }
    try:
        validated = model(**arguments)
    except (ValidationError, TypeError) as exc:
        return {"error": "invalid arguments", "detail": str(exc)}
    try:
        return tools.TOOL_FUNCTIONS[name](**validated.model_dump())
    except Exception as exc:
        return {"error": type(exc).__name__, "detail": str(exc)}


def run_agent(
    question: str,
    model: str | None = None,
    max_iterations: int = config.MAX_ITERATIONS,
    verbose: bool = True,
) -> dict[str, Any]:
    """One question, up to `max_iterations` turns, everything logged."""
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    config.ensure_dirs()
    logger = RunLogger(run_id)
    tracer = trace.Tracer(verbose)
    client = llm_client.make_client()
    model = model or config.MODEL
    specs = tools.tool_specs()
    tools.forget_calls()

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt()},
        {"role": "user", "content": question},
    ]
    logger.log(
        "run_start",
        {
            "question": question,
            "model": model,
            "base_url": config.API_BASE_URL,
            "tools_offered": [spec["function"]["name"] for spec in specs],
            "tools_pending": list(tools.pending_tools()),
        },
    )
    tracer.question(question, model, config.API_BASE_URL)

    final_answer: str | None = None
    stop_reason = "max_iterations"
    llm_calls = 0
    tool_calls_made = 0
    run_started = time.time()

    for iteration in range(1, max_iterations + 1):
        started = time.time()
        response = llm_client.chat(client, messages, tools=specs, model=model)
        message = response.choices[0].message
        tool_calls = llm_client.parse_tool_calls(message)
        llm_elapsed = time.time() - started
        llm_calls += 1
        tracer.llm_step(iteration, tool_calls, message.content, llm_elapsed)

        logger.log(
            "llm_response",
            {
                "content": message.content,
                "tool_calls": [
                    {"name": c["name"], "arguments": c["arguments"]} for c in tool_calls
                ],
                "finish_reason": response.choices[0].finish_reason,
                "elapsed_seconds": round(time.time() - started, 2),
            },
            iteration,
        )

        messages.append(llm_client.message_to_dict(message))

        if not tool_calls:
            final_answer = message.content
            stop_reason = "final_answer"
            logger.log("final_answer", {"content": final_answer}, iteration)
            tracer.final(final_answer)
            break

        for call in tool_calls:
            tool_started = time.time()
            result = execute_tool(call["name"], call["arguments"])
            tool_elapsed = time.time() - tool_started
            tool_calls_made += 1
            tracer.tool_result(iteration, call["name"], result, tool_elapsed)
            logger.log(
                "tool_result",
                {
                    "tool": call["name"],
                    "arguments": call["arguments"],
                    "result": result,
                    "elapsed_seconds": round(tool_elapsed, 2),
                },
                iteration,
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": json.dumps(result, default=str),
                }
            )

    duration = time.time() - run_started
    if stop_reason == "max_iterations":
        tracer.stopped_early(max_iterations)

    totals = {
        "llm_calls": llm_calls,
        "gis_tool_calls": tool_calls_made,
        "duration_seconds": round(duration, 2),
    }
    logger.log("run_end", {"stop_reason": stop_reason, **totals})

    transcript_path = config.OUTPUTS_DIR / f"run_{run_id}.json"
    transcript = {
        "run_id": run_id,
        "question": question,
        "model": model,
        "stop_reason": stop_reason,
        "final_answer": final_answer,
        "totals": totals,
        "messages": messages,
        "steps": logger.steps,
    }
    transcript_path.write_text(
        json.dumps(transcript, indent=2, default=str), encoding="utf-8"
    )

    tracer.summary(llm_calls, tool_calls_made, duration, logger.path, transcript_path)

    return {
        "run_id": run_id,
        "final_answer": final_answer,
        "stop_reason": stop_reason,
        "log_path": str(logger.path),
        "transcript_path": str(transcript_path),
        "steps": logger.steps,
        **totals,
    }
