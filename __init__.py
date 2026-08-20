"""Native Hermes agent-task plugin registration."""

from __future__ import annotations


def _components():
    """Load Hermes-dependent components only when the plugin is registered."""
    if __package__:
        from .agent_task import (
            AGENT_TASK_SCHEMA,
            agent_task_reply_hook,
            agent_task_tool,
            check_agent_task_available,
        )
    else:  # Direct source-tree loading by lightweight plugin inspectors.
        from agent_task import (
            AGENT_TASK_SCHEMA,
            agent_task_reply_hook,
            agent_task_tool,
            check_agent_task_available,
        )

    return AGENT_TASK_SCHEMA, agent_task_reply_hook, agent_task_tool, check_agent_task_available


def register(ctx) -> None:
    schema, reply_hook, tool, availability_check = _components()
    ctx.register_tool(
        name="agent_task",
        toolset="agent_task",
        schema=schema,
        handler=tool,
        check_fn=availability_check,
        description="Background tasks with automatic reply context restoration.",
        emoji="📬",
    )
    ctx.register_hook("pre_gateway_dispatch", reply_hook)
