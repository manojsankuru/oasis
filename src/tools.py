"""The LLM-visible tool surface.

Empty until S9 implements the eleven names in contracts.TOOL_NAMES against the
registry. The synthetic-data tools that used to live in spatial_tools.py were
removed in S1; nothing here may reach a hand-authored layer.
"""

from __future__ import annotations

from .contracts import ToolFn

TOOL_FUNCTIONS: dict[str, ToolFn] = {}
