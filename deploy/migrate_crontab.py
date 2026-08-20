#!/usr/bin/env python3
"""Render or apply managed agent-task crontab entries from live task definitions."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import types
from pathlib import Path


def load_plugin(repo_root: Path, hermes_home: Path):
    try:
        import hermes_constants  # noqa: F401
    except ModuleNotFoundError:
        constants = types.ModuleType("hermes_constants")
        constants.get_hermes_home = lambda: hermes_home
        sys.modules["hermes_constants"] = constants

    path = repo_root / "plugin" / "agent_task.py"
    spec = importlib.util.spec_from_file_location("agent_task_plugin_for_migration", path)
    if not spec or not spec.loader:
        raise RuntimeError(f"Cannot load plugin: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def task_definitions(hermes_home: Path) -> list[dict]:
    tasks = []
    for path in sorted((hermes_home / "agent_tasks").glob("*/task.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("task_id") and data.get("schedule"):
            tasks.append(data)
    return tasks


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Write the current user crontab")
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[1]
    hermes_home = Path(os.path.expanduser(os.environ.get("HERMES_HOME", "~/.hermes")))
    plugin = load_plugin(repo_root, hermes_home)
    tasks = task_definitions(hermes_home)
    rendered = []
    for task in tasks:
        task_id = str(task["task_id"])
        schedule = str(task["schedule"])
        deliver = str(task.get("deliver") or "telegram")
        line = plugin._task_cron_line(task_id, schedule, deliver=deliver)
        rendered.append({"task_id": task_id, "line": line})
        if args.apply:
            plugin._schedule_task_in_crontab(task_id, schedule, deliver=deliver)

    print(json.dumps({"success": True, "applied": args.apply, "tasks": rendered}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
