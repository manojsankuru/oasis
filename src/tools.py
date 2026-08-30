"""The LLM-visible tool surface.

Empty until S9 implements the eleven names in contracts.TOOL_NAMES against the
registry. The synthetic-data tools that used to live in spatial_tools.py were
removed in S1; nothing here may reach a hand-authored layer.

Until then `surface_faults` exists so that emptiness is loud. `schemas.py` still
advertises four prototype tools to the model, none of which is in
`contracts.TOOL_NAMES` and none of which anything here can execute. Without a
guard, `python -m src.demo` runs and exits 0 -- not because the system works, but
because the model happened not to call one of them. A reviewer running the one
command in CLAUDE.md would see a system that looks finished and is not.
"""

from __future__ import annotations

from . import schemas
from .contracts import TOOL_NAMES, ToolFn

TOOL_FUNCTIONS: dict[str, ToolFn] = {}


def surface_faults() -> list[str]:
    """Every way the advertised tool surface and the executable one disagree.

    Three separate faults, because they fail differently. A name the model is
    offered but nothing can run wastes a turn and returns an error the model then
    has to reason about. A name outside `contracts.TOOL_NAMES` is drift from the
    frozen contract. A name that is implemented and never advertised is dead code
    the model cannot reach.
    """
    advertised = set(schemas.TOOL_ARG_MODELS)
    executable = set(TOOL_FUNCTIONS)
    frozen = set(TOOL_NAMES)

    faults: list[str] = []
    unrunnable = sorted(advertised - executable)
    if unrunnable:
        faults.append(
            f"{len(unrunnable)} tool(s) are offered to the model and cannot be executed: "
            f"{unrunnable}. TOOL_FUNCTIONS holds {sorted(executable) or 'nothing'}"
        )
    undeclared = sorted(advertised - frozen)
    if undeclared:
        faults.append(
            f"{len(undeclared)} advertised tool(s) are not in contracts.TOOL_NAMES: "
            f"{undeclared}. The frozen surface is {list(TOOL_NAMES)}"
        )
    unreachable = sorted(executable - advertised)
    if unreachable:
        faults.append(
            f"{len(unreachable)} implemented tool(s) are never advertised, so the model "
            f"cannot call them: {unreachable}"
        )
    return faults
