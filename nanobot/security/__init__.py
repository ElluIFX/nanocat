"""Centralized safety infrastructure.

Tools read the ``safety_bypass`` context variable instead of carrying
their own ``safety_check`` attributes.  Sub‑agents inherit the bypass
state automatically via ``asyncio.Task`` context propagation.
"""

from __future__ import annotations

import contextvars

# Set to True by the tool registry when /approve is active.
# All safety‑check functions read this var — a tool does not need its
# own safety_check attribute.
safety_bypass: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "safety_bypass", default=False
)
