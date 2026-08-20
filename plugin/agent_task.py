"""
Agent task management tool for Hermes.

This is the agent-oriented alternative to legacy cronjob for scheduled/background
work. It manages task definitions and triggers collector-based runs that write
structured result.json files under ~/.hermes/agent_runs/<short_id>/<run_id>/.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from hermes_constants import get_hermes_home
from typing import Any, Dict


_TASK_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_CRON_FIELD_RE = re.compile(r"^[A-Za-z0-9*/?,#LW-]+$")
_DELIVERY_TARGETS = {"local", "none", "origin", "telegram"}
_TIMEZONE_RE = re.compile(r"^[A-Za-z0-9_+./-]{1,64}$")


def _scripts_dir() -> Path:
    return get_hermes_home() / "scripts"


def _runner_path() -> Path:
    return _scripts_dir() / "agent_task_runner.py"


def _managed_python_path() -> Path:
    return get_hermes_home() / "hermes-agent" / "venv" / "bin" / "python"


def _runner_python() -> Path:
    managed = _managed_python_path()
    return managed if managed.is_file() else Path(sys.executable)


def _run_runner(args: list[str], timeout: int = 180) -> Dict[str, Any]:
    runner = _runner_path()
    if not runner.exists():
        return {"success": False, "error": f"agent_task_runner.py not found: {runner}"}
    try:
        result = subprocess.run(
            [str(_runner_python()), str(runner)] + args,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(runner.parent),
        )
        return {
            "success": result.returncode == 0,
            "exit_code": result.returncode,
            "stdout": (result.stdout or "").strip(),
            "stderr": (result.stderr or "").strip(),
        }
    except subprocess.TimeoutExpired:
        return {"success": False, "error": f"agent_task runner timed out after {timeout}s"}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


def _load_json_if_possible(text: str) -> Any:
    try:
        return json.loads(text)
    except Exception:
        return text


def _crontab_read() -> str:
    """Read current user crontab. Return empty string if no crontab exists."""
    result = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=10)
    if result.returncode != 0:
        if "no crontab" in (result.stderr or "").lower():
            return ""
        raise RuntimeError((result.stderr or result.stdout or "crontab -l failed").strip())
    return result.stdout or ""


def _crontab_write(content: str) -> None:
    """Write current user crontab from string content."""
    result = subprocess.run(["crontab", "-"], input=content, capture_output=True, text=True, timeout=10)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "crontab write failed").strip())


def _cron_marker(task_id: str) -> str:
    return f"# agent-task:{task_id}"


def _split_schedule_timezone(schedule: str, timezone_name: str | None = None) -> tuple[str, str | None]:
    fields = schedule.split()
    if len(fields) == 6 and not timezone_name:
        timezone_name = fields.pop()
    schedule = " ".join(fields)
    if timezone_name:
        if not _TIMEZONE_RE.fullmatch(timezone_name):
            raise ValueError(f"Invalid timezone: {timezone_name!r}")
        try:
            ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"Unknown timezone: {timezone_name!r}") from exc
    return schedule, timezone_name


def _validate_cron_inputs(task_id: str, schedule: str, deliver: str) -> None:
    if not _TASK_ID_RE.fullmatch(task_id):
        raise ValueError(f"Invalid task_id: {task_id!r}")
    fields = schedule.split()
    if len(fields) != 5 or not all(_CRON_FIELD_RE.fullmatch(field) for field in fields):
        raise ValueError(f"Invalid five-field cron schedule: {schedule!r}")
    if deliver not in _DELIVERY_TARGETS:
        raise ValueError(f"Invalid delivery target: {deliver!r}")


def _task_cron_line(
    task_id: str,
    schedule: str,
    *,
    deliver: str = "telegram",
    timezone_name: str | None = None,
) -> str:
    schedule, timezone_name = _split_schedule_timezone(schedule, timezone_name)
    _validate_cron_inputs(task_id, schedule, deliver)
    home = get_hermes_home()
    python = _managed_python_path()
    runner = _runner_path()
    log_path = home / "logs" / "agent-task" / f"{task_id}.log"
    command_guard = ""
    if timezone_name:
        minute, hour, day_of_month, month, day_of_week = schedule.split()
        if not hour.isdigit() or not 0 <= int(hour) <= 23:
            raise ValueError("Timezone-aware schedules currently require one numeric hour")
        schedule = f"{minute} * {day_of_month} {month} {day_of_week}"
        local_hour = f"{int(hour):02d}"
        command_guard = f'[ "$(TZ={shlex.quote(timezone_name)} date +\\%H)" = "{local_hour}" ] && '
    return (
        f"{schedule} cd {shlex.quote(str(home))} && umask 077 && "
        f"{command_guard}{shlex.quote(str(python))} {shlex.quote(str(runner))} run {shlex.quote(task_id)} "
        f"--deliver {shlex.quote(deliver)} --skip-if-empty --retry-failed --prune "
        f">>{shlex.quote(str(log_path))} 2>&1"
    )


def _schedule_task_in_crontab(
    task_id: str,
    schedule: str,
    *,
    deliver: str = "telegram",
    timezone_name: str | None = None,
) -> dict:
    """Add/update a system crontab entry for an agent-task."""
    schedule, timezone_name = _split_schedule_timezone(schedule, timezone_name)
    _validate_cron_inputs(task_id, schedule, deliver)
    log_dir = get_hermes_home() / "logs" / "agent-task"
    log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    existing = _crontab_read().splitlines()
    marker = _cron_marker(task_id)
    new_lines = []
    skip_next = False
    removed = False
    for line in existing:
        if skip_next:
            skip_next = False
            removed = True
            continue
        if line.strip() == marker:
            skip_next = True
            removed = True
            continue
        if f"agent_task_runner.py run {task_id}" in line:
            removed = True
            continue
        new_lines.append(line)
    if new_lines and new_lines[-1].strip():
        new_lines.append("")
    new_lines.extend(
        [
            marker,
            _task_cron_line(task_id, schedule, deliver=deliver, timezone_name=timezone_name),
        ]
    )
    _crontab_write("\n".join(new_lines).rstrip() + "\n")
    return {
        "success": True,
        "task_id": task_id,
        "schedule": schedule,
        "timezone": timezone_name,
        "updated_existing": removed,
    }


def _unschedule_task_in_crontab(task_id: str) -> dict:
    """Remove agent-task crontab entry for task_id."""
    existing = _crontab_read().splitlines()
    marker = _cron_marker(task_id)
    new_lines = []
    skip_next = False
    removed = False
    for line in existing:
        if skip_next:
            skip_next = False
            removed = True
            continue
        if line.strip() == marker:
            skip_next = True
            removed = True
            continue
        if f"agent_task_runner.py run {task_id}" in line:
            removed = True
            continue
        new_lines.append(line)
    _crontab_write("\n".join(new_lines).rstrip() + ("\n" if new_lines else ""))
    return {"success": True, "task_id": task_id, "removed": removed}


def agent_task_tool(args: Dict[str, Any], **kwargs) -> str:
    """Compressed agent_task tool handler."""
    action = str(args.get("action") or "list").strip().lower()
    task_id = args.get("task_id")
    run_id = args.get("run_id")

    if action == "list":
        r = _run_runner(["list"])
        return json.dumps(r, indent=2, ensure_ascii=False)

    if action == "info":
        if not task_id:
            return json.dumps({"success": False, "error": "task_id is required for info"})
        r = _run_runner(["info", str(task_id)])
        if r.get("success") and r.get("stdout"):
            r["task"] = _load_json_if_possible(r["stdout"])
        return json.dumps(r, indent=2, ensure_ascii=False)

    if action == "runs":
        if not task_id:
            return json.dumps({"success": False, "error": "task_id is required for runs"})
        r = _run_runner(["list-runs", str(task_id)])
        return json.dumps(r, indent=2, ensure_ascii=False)

    if action == "health":
        r = _run_runner(["doctor"])
        if r.get("stdout"):
            r["health"] = _load_json_if_possible(r["stdout"])
        return json.dumps(r, indent=2, ensure_ascii=False)

    if action == "prune":
        runner_args = ["prune"]
        if task_id:
            runner_args.extend(["--task-id", str(task_id)])
        if args.get("retention_days") is not None:
            runner_args.extend(["--days", str(args["retention_days"])])
        if args.get("max_runs") is not None:
            runner_args.extend(["--max-runs", str(args["max_runs"])])
        if args.get("apply"):
            runner_args.append("--apply")
        r = _run_runner(runner_args)
        if r.get("stdout"):
            r["result"] = _load_json_if_possible(r["stdout"])
        return json.dumps(r, indent=2, ensure_ascii=False)

    if action == "retry_failed":
        if not task_id:
            return json.dumps({"success": False, "error": "task_id is required for retry_failed"})
        r = _run_runner(["retry-failed", str(task_id)])
        if r.get("stdout"):
            r["result"] = _load_json_if_possible(r["stdout"])
        return json.dumps(r, indent=2, ensure_ascii=False)

    if action == "create":
        if not task_id:
            return json.dumps({"success": False, "error": "task_id is required for create"})
        short_task_id = args.get("short_task_id") or args.get("short")
        description = args.get("description") or args.get("desc")
        if not short_task_id or not description:
            return json.dumps({"success": False, "error": "short_task_id and description are required for create"})
        runner_args = ["create", str(task_id), "--short", str(short_task_id), "--desc", str(description)]
        if args.get("schedule"):
            runner_args.extend(["--schedule", str(args.get("schedule"))])
        if args.get("collector"):
            runner_args.extend(["--collector", str(args.get("collector"))])
        r = _run_runner(runner_args)
        return json.dumps(r, indent=2, ensure_ascii=False)

    if action == "schedule":
        if not task_id:
            return json.dumps({"success": False, "error": "task_id is required for schedule"})
        schedule = args.get("schedule")
        if not schedule:
            info = _run_runner(["info", str(task_id)])
            task = _load_json_if_possible(info.get("stdout", "")) if info.get("success") else None
            if isinstance(task, dict):
                schedule = task.get("schedule")
        if not schedule:
            return json.dumps({"success": False, "error": "schedule is required and task has no schedule"})
        try:
            result = _schedule_task_in_crontab(
                str(task_id),
                str(schedule),
                deliver=str(args.get("deliver") or "telegram"),
                timezone_name=str(args["timezone"]) if args.get("timezone") else None,
            )
            return json.dumps(result, indent=2, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"success": False, "error": str(exc)}, indent=2, ensure_ascii=False)

    if action == "unschedule":
        if not task_id:
            return json.dumps({"success": False, "error": "task_id is required for unschedule"})
        try:
            result = _unschedule_task_in_crontab(str(task_id))
            return json.dumps(result, indent=2, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"success": False, "error": str(exc)}, indent=2, ensure_ascii=False)

    if action == "crontab":
        try:
            return json.dumps({"success": True, "crontab": _crontab_read()}, indent=2, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"success": False, "error": str(exc)}, indent=2, ensure_ascii=False)

    if action == "set_delivery":
        if not task_id:
            return json.dumps({"success": False, "error": "task_id is required for set_delivery"})
        if not args.get("chat_id"):
            return json.dumps({"success": False, "error": "chat_id is required for set_delivery"})
        runner_args = ["set-delivery", str(task_id), "--chat-id", str(args.get("chat_id"))]
        if args.get("thread_id"):
            runner_args.extend(["--thread-id", str(args.get("thread_id"))])
        if args.get("topic_name"):
            runner_args.extend(["--topic-name", str(args.get("topic_name"))])
        if args.get("create_topic"):
            runner_args.append("--create-topic")
        r = _run_runner(runner_args)
        if r.get("stdout"):
            r["result"] = _load_json_if_possible(r["stdout"])
        return json.dumps(r, indent=2, ensure_ascii=False)

    if action == "deliver":
        if not task_id or not run_id:
            return json.dumps({"success": False, "error": "task_id and run_id are required for deliver"})
        runner_args = ["deliver", str(task_id), str(run_id)]
        to = str(args.get("deliver") or args.get("to") or "telegram")
        if to:
            runner_args.extend(["--to", to])
        if args.get("chat_id"):
            runner_args.extend(["--chat-id", str(args.get("chat_id"))])
        if args.get("thread_id"):
            runner_args.extend(["--thread-id", str(args.get("thread_id"))])
        if args.get("skip_if_empty"):
            runner_args.append("--skip-if-empty")
        r = _run_runner(runner_args)
        if r.get("stdout"):
            r["result"] = _load_json_if_possible(r["stdout"])
        return json.dumps(r, indent=2, ensure_ascii=False)

    if action == "run":
        if not task_id:
            return json.dumps({"success": False, "error": "task_id is required for run"})
        runner_args = ["run", str(task_id)]
        deliver = str(args.get("deliver") or "local")
        if deliver and deliver != "local":
            runner_args.extend(["--deliver", deliver])
        if args.get("chat_id"):
            runner_args.extend(["--chat-id", str(args.get("chat_id"))])
        if args.get("thread_id"):
            runner_args.extend(["--thread-id", str(args.get("thread_id"))])
        if args.get("skip_if_empty"):
            runner_args.append("--skip-if-empty")
        timeout = int(args.get("timeout") or 480)
        if not 1 <= timeout <= 1200:
            return json.dumps({"success": False, "error": "timeout must be between 1 and 1200 seconds"})
        r = _run_runner(runner_args, timeout=timeout)
        if r.get("stdout"):
            parsed = _load_json_if_possible(r["stdout"])
            r["collector_output"] = parsed
            if isinstance(parsed, dict):
                r["run_info"] = parsed.get("run_info")
                r["prompt_context"] = parsed.get("prompt_context")
        return json.dumps(r, indent=2, ensure_ascii=False)

    if action == "read":
        if not task_id or not run_id:
            return json.dumps({"success": False, "error": "task_id and run_id are required for read"})
        # Use deterministic path from task info short_task_id + run_id
        info = _run_runner(["info", str(task_id)])
        if not info.get("success"):
            return json.dumps(info, indent=2, ensure_ascii=False)
        task = _load_json_if_possible(info.get("stdout", ""))
        if not isinstance(task, dict):
            return json.dumps({"success": False, "error": "failed to parse task info", "raw": info}, indent=2)
        home = get_hermes_home()
        run_dir = home / "agent_runs" / str(task.get("short_task_id")) / str(run_id)
        result_file = run_dir / "result.json"
        run_file = run_dir / "run.json"
        if not run_file.exists():
            return json.dumps({"success": False, "error": f"run not found: {run_dir}"}, indent=2)

        def read_json(path: Path):
            try:
                return json.loads(path.read_text())
            except Exception as exc:
                return {"_error": str(exc)}

        return json.dumps(
            {
                "success": True,
                "task": task,
                "run": read_json(run_file),
                "result": read_json(result_file) if result_file.exists() else None,
                "run_dir": str(run_dir),
            },
            indent=2,
            ensure_ascii=False,
        )

    return json.dumps({"success": False, "error": f"unknown action: {action}"})


AGENT_TASK_SCHEMA = {
    "name": "agent_task",
    "description": "Manage and run agent-oriented scheduled/background tasks. Preferred over legacy cronjob for new tasks. Includes health, retention, locking, and failed-delivery retry.",
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "create",
                    "schedule",
                    "unschedule",
                    "crontab",
                    "list",
                    "info",
                    "runs",
                    "run",
                    "read",
                    "set_delivery",
                    "deliver",
                    "health",
                    "prune",
                    "retry_failed",
                ],
                "description": "Action to perform. Use create for task definition, schedule/unschedule for system crontab, run to trigger collector, set_delivery to configure destination, deliver to send an existing run.",
            },
            "task_id": {"type": "string", "description": "Task ID, e.g. package-tracker"},
            "short_task_id": {"type": "string", "description": "Short ID for create action, e.g. pkg"},
            "description": {"type": "string", "description": "Task description for create action"},
            "schedule": {
                "type": "string",
                "description": "Cron schedule for create/schedule action, e.g. '0 9,18 * * *'",
            },
            "timezone": {
                "type": "string",
                "description": "Optional IANA timezone for a schedule with one numeric hour",
            },
            "collector": {
                "type": "string",
                "description": "Collector script filename under ~/.hermes/scripts for create action",
            },
            "run_id": {"type": "string", "description": "Run ID for read action"},
            "timeout": {"type": "integer", "description": "Timeout seconds for run action"},
            "deliver": {
                "type": "string",
                "enum": ["local", "none", "origin", "telegram"],
                "description": "Optional delivery target for run. Default: local/stdout only.",
            },
            "chat_id": {"type": "string", "description": "Explicit Telegram chat ID for delivery or set_delivery"},
            "thread_id": {"type": "string", "description": "Telegram message_thread_id/forum topic ID"},
            "topic_name": {"type": "string", "description": "Topic name to store/create for set_delivery"},
            "create_topic": {
                "type": "boolean",
                "description": "If true, create Telegram forum topic using topic_name and store returned thread_id",
            },
            "to": {"type": "string", "enum": ["telegram", "origin"], "description": "Destination for deliver action"},
            "skip_if_empty": {
                "type": "boolean",
                "description": "For run/deliver: pass --skip-if-empty so collector skip_delivery=true suppresses delivery",
            },
            "retention_days": {"type": "integer", "description": "Retention age for prune; defaults to 45 days"},
            "max_runs": {"type": "integer", "description": "Maximum retained runs per task for prune; defaults to 200"},
            "apply": {"type": "boolean", "description": "For prune: delete candidates. Default is a dry-run."},
        },
        "required": ["action"],
    },
}


def check_agent_task_available() -> bool:
    return _runner_path().exists()
