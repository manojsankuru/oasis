import json
import time
from datetime import datetime

from pydantic import ValidationError

from . import config, llm_client, schemas, tools, trace

SYSTEM_PROMPT = """You are a spatial analysis assistant for emergency shelter planning.

You cannot see the data directly. The only way to learn anything about it is to call
the provided tools. Never guess a number, a shelter name, or a distance.

Rules:
- If you do not know which layers or attributes exist, call list_layers first.
- Call one or more tools, read their results, then answer.
- All distances returned by the tools are in kilometers and were computed in a
  projected coordinate system, so they are real ground distances.
- When you have enough information, give a short final answer that cites the exact
  numbers the tools returned and names the tools you used.
- Every number you report must come from a tool result. Never estimate one.
"""


class RunLogger:
    def __init__(self, run_id):
        self.run_id = run_id
        self.path = config.LOGS_DIR / f"run_{run_id}.jsonl"
        self.steps = []

    def log(self, step_type, payload, iteration=None):
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


def execute_tool(name, arguments):
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


def run_agent(question, model=None, max_iterations=config.MAX_ITERATIONS, verbose=True):
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    config.ensure_dirs()
    logger = RunLogger(run_id)
    tracer = trace.Tracer(verbose)
    client = llm_client.make_client()
    model = model or config.MODEL

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": question},
    ]
    logger.log("run_start", {"question": question, "model": model, "base_url": config.API_BASE_URL})
    tracer.question(question, model, config.API_BASE_URL)

    final_answer = None
    stop_reason = "max_iterations"
    llm_calls = 0
    tool_calls_made = 0
    run_started = time.time()

    for iteration in range(1, max_iterations + 1):
        started = time.time()
        response = llm_client.chat(client, messages, tools=schemas.TOOL_SPECS, model=model)
        message = response.choices[0].message
        tool_calls = llm_client.parse_tool_calls(message)
        llm_elapsed = time.time() - started
        llm_calls += 1
        tracer.llm_step(iteration, tool_calls, message.content, llm_elapsed)

        logger.log(
            "llm_response",
            {
                "content": message.content,
                "tool_calls": [{"name": c["name"], "arguments": c["arguments"]} for c in tool_calls],
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
    transcript_path.write_text(json.dumps(transcript, indent=2, default=str), encoding="utf-8")

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
