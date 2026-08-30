"""Terminal formatting for a run. A demo asset: finalists present in November.

Nothing here decides anything. It renders what `agent.py` already logged, so a
change to this file cannot change an answer -- which is why the JSONL log rather
than this is what an auditor reads.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

WIDTH = 68
MAX_LIST_ITEMS = 8
MAX_TEXT = 400


def _fmt(value: Any) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:g}"
    text = str(value)
    if len(text) > MAX_TEXT:
        return text[:MAX_TEXT] + " ..."
    return text


def _render_list(key: str, items: list[Any], indent: int) -> list[str]:
    pad = "  " * indent
    if not items:
        return [f"{pad}{key}: (none)"]
    if all(isinstance(item, dict) for item in items):
        return [f"{pad}{key}: {len(items)} rows"]
    shown = ", ".join(_fmt(item) for item in items[:MAX_LIST_ITEMS])
    extra = len(items) - MAX_LIST_ITEMS
    suffix = f" (+{extra} more)" if extra > 0 else ""
    return [f"{pad}{key}: {shown}{suffix}"]


def render(value: Any, indent: int = 0) -> list[str]:
    pad = "  " * indent
    if not isinstance(value, dict):
        return [f"{pad}{_fmt(value)}"]
    lines: list[str] = []
    for key, item in value.items():
        if isinstance(item, dict):
            lines.append(f"{pad}{key}:")
            lines.extend(render(item, indent + 1))
        elif isinstance(item, list):
            lines.extend(_render_list(key, item, indent))
        else:
            lines.append(f"{pad}{key}: {_fmt(item)}")
    return lines


class Tracer:
    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled

    def _out(self, text: str = "") -> None:
        if self.enabled:
            print(text)

    def question(self, text: str, model: str, endpoint: str) -> None:
        self._out("=" * WIDTH)
        self._out("USER")
        self._out(text)
        self._out("")
        self._out(f"model: {model}    endpoint: {endpoint}")
        self._out("=" * WIDTH)
        self._out()

    def llm_step(
        self, step: int, calls: list[dict[str, Any]], content: str | None, elapsed: float
    ) -> None:
        self._out(f"STEP {step} — LLM  ({elapsed:.1f}s)")
        if calls and content and content.strip():
            self._out(f"Says: {_fmt(content.strip())}")
        if not calls:
            self._out("Action: none, replying with a final answer")
        for call in calls:
            self._out(f"Action: {call['name']}")
            for key, value in call["arguments"].items():
                self._out(f"  {key}: {_fmt(value)}")
        self._out()

    def tool_result(
        self, step: int, name: str, result: dict[str, Any], elapsed: float
    ) -> None:
        failed = isinstance(result, dict) and "error" in result
        label = "TOOL ERROR" if failed else "TOOL RESULT"
        self._out(f"STEP {step} — {label}  ({elapsed:.2f}s)")
        self._out(f"Tool: {name}")
        for line in render(result):
            self._out(line)
        self._out()

    def final(self, answer: str | None) -> None:
        self._out("FINAL ANSWER")
        self._out(answer.strip() if answer else "(the model returned no answer)")
        self._out()

    def stopped_early(self, max_iterations: int) -> None:
        self._out(f"STOPPED — hit the {max_iterations} step limit without a final answer")
        self._out()

    def summary(
        self,
        llm_calls: int,
        tool_calls: int,
        duration: float,
        log_path: Path,
        transcript_path: Path,
    ) -> None:
        self._out("-" * WIDTH)
        self._out(f"TOTAL LLM CALLS:      {llm_calls}")
        self._out(f"TOTAL GIS TOOL CALLS: {tool_calls}")
        self._out(f"TOTAL DURATION:       {duration:.1f}s")
        self._out(f"JSONL LOG:            {log_path}")
        self._out(f"TRANSCRIPT:           {transcript_path}")
        self._out("-" * WIDTH)
        self._out()
