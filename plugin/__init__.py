"""Hermes agent-task plugin registration."""

from __future__ import annotations

from .agent_task import AGENT_TASK_SCHEMA, agent_task_reply_hook, agent_task_tool, check_agent_task_available


def register(ctx) -> None:
    ctx.register_tool(
        name="agent_task",
        toolset="agent_task",
        schema=AGENT_TASK_SCHEMA,
        handler=agent_task_tool,
        check_fn=check_agent_task_available,
        description="Background tasks with automatic reply context restoration.",
        emoji="📬",
    )
    ctx.register_hook("pre_gateway_dispatch", agent_task_reply_hook)
