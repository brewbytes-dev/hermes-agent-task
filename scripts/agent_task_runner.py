#!/usr/bin/env python3
"""
Agent Task Runner — core module for agent-oriented scheduled tasks.

Provides:
  - Task definition management (create, read, list)
  - Run lifecycle (create_run, save_result, read_run)
  - Callback token management
  - Collector script execution

Directory structure:
  ~/.hermes/agent_tasks/<task_id>/task.json
  ~/.hermes/agent_tasks/<task_id>/prompt.md
  ~/.hermes/agent_runs/<short_task_id>/<run_id>/
  ~/.hermes/agent_runs/<short_task_id>/<run_id>/run.json
  ~/.hermes/agent_runs/<short_task_id>/<run_id>/result.json
  ~/.hermes/agent_runs/<short_task_id>/<run_id>/prompt_context.json
  ~/.hermes/agent_runs/<short_task_id>/<run_id>/stdout.txt
  ~/.hermes/agent_runs/<short_task_id>/<run_id>/stderr.txt
"""

import json
import os
import re
import secrets
import shutil
import string
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import fcntl


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_HERMES_HOME = Path(os.path.expanduser(os.environ.get("HERMES_HOME", "~/.hermes")))
TASKS_DIR = _HERMES_HOME / "agent_tasks"
RUNS_DIR = _HERMES_HOME / "agent_runs"
SCRIPTS_DIR = _HERMES_HOME / "scripts"
LOCKS_DIR = _HERMES_HOME / "agent_task_locks"
LOGS_DIR = _HERMES_HOME / "logs" / "agent-task"

DEFAULT_RETENTION_DAYS = 45
DEFAULT_MAX_RUNS_PER_TASK = 200
DEFAULT_MIN_RUNS_PER_TASK = 10

CALLBACK_PREFIX = "ar:"  # "agent_run" — compact for Telegram 64-char limit

_TASK_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_SHORT_TASK_ID_RE = re.compile(r"^[a-z0-9]{2,8}$")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def _validate_task_id(task_id: str) -> str:
    value = str(task_id)
    if not _TASK_ID_RE.fullmatch(value):
        raise ValueError(f"Invalid task_id: {task_id!r}")
    return value


def _validate_short_task_id(short_task_id: str) -> str:
    value = str(short_task_id)
    if not _SHORT_TASK_ID_RE.fullmatch(value):
        raise ValueError(f"Invalid short_task_id: {short_task_id!r} (2-8 alphanumeric chars)")
    return value


def _safe_child(base: Path, *parts: str) -> Path:
    root = base.resolve()
    candidate = root.joinpath(*parts).resolve()
    if not candidate.is_relative_to(root):
        raise ValueError(f"Path escapes managed directory: {candidate}")
    return candidate


def _read_json(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text()) if path.exists() else None
    except (json.JSONDecodeError, OSError):
        return None


def _write_json(path: Path, data: dict) -> None:
    _ensure_dir(path.parent)
    payload = json.dumps(data, indent=2, ensure_ascii=False, default=str) + "\n"
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _short_id(length: int = 6) -> str:
    """Generate a short alphanumeric ID for run uniqueness."""
    alphabet = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


class TaskAlreadyRunning(RuntimeError):
    """Raised when a second process tries to run the same task concurrently."""


@contextmanager
def task_run_lock(task_id: str):
    """Hold a non-blocking process lock for one task."""
    task_id = _validate_task_id(task_id)
    lock_dir = _ensure_dir(LOCKS_DIR)
    lock_path = _safe_child(lock_dir, f"{task_id}.lock")
    with lock_path.open("a+", encoding="utf-8") as handle:
        os.chmod(lock_path, 0o600)
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise TaskAlreadyRunning(f"Task is already running: {task_id}") from exc
        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps({"task_id": task_id, "pid": os.getpid(), "acquired_at": _utcnow().isoformat()}))
        handle.flush()
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


# ---------------------------------------------------------------------------
# Run ID: self-contained directory key
# ---------------------------------------------------------------------------


def make_run_id() -> str:
    """
    Create a unique run ID in format: YYYYMMDDTHHMMSS-<short_id>
    Example: 20260519T180000Z-a7f3c9

    The run_id IS the directory name under agent_runs/<short_task_id>/.
    No external index needed — path is deterministic from run_id.
    """
    ts = _utcnow().strftime("%Y%m%dT%H%M%S")
    rand = _short_id(6)
    return f"{ts}-{rand}"


def _parse_run_id(run_id: str) -> dict:
    """Validate and parse a run_id into components."""
    m = re.match(r"^(\d{8}T\d{6})-([a-z0-9]{6})$", run_id)
    if not m:
        raise ValueError(f"Invalid run_id format: {run_id!r}")
    return {"timestamp": m.group(1), "rand": m.group(2), "run_id": run_id}


# ---------------------------------------------------------------------------
# Callback token
# ---------------------------------------------------------------------------


def make_callback_data(short_task_id: str, run_id: str) -> str:
    """
    Create a compact callback payload for Telegram inline keyboard.
    Format: ar:<short_task_id>:<run_id>
    Max recommended: < 64 bytes (Telegram callback_data limit)
    Example output: ar:pkg:20260519T180000Z-a7f3c9 (28 chars)
    """
    return f"{CALLBACK_PREFIX}{short_task_id}:{run_id}"


def parse_callback_data(data: str) -> Optional[dict]:
    """
    Parse a callback data string into components.

    Returns:
        {"short_task_id": str, "run_id": str, "run_dir": Path}
        or None if invalid.
    """
    if not data.startswith(CALLBACK_PREFIX):
        return None
    rest = data[len(CALLBACK_PREFIX) :]
    parts = rest.split(":", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return None
    short_task_id = parts[0]
    run_id = parts[1]
    try:
        _parse_run_id(run_id)
    except ValueError:
        return None
    run_dir = RUNS_DIR / short_task_id / run_id
    if not run_dir.exists():
        return None
    return {
        "short_task_id": short_task_id,
        "run_id": run_id,
        "run_dir": run_dir,
    }


# ---------------------------------------------------------------------------
# Task definition management
# ---------------------------------------------------------------------------


def create_task_definition(
    task_id: str,
    short_task_id: str,
    description: str,
    schema: dict,
    schedule: Optional[str] = None,
    collector: Optional[str] = None,
    prompt: Optional[str] = None,
    deliver: str = "origin",
    continue_dialog: bool = False,
) -> dict:
    """
    Create a task definition in ~/.hermes/agent_tasks/<task_id>/task.json.

    Args:
        task_id: Unique task identifier (e.g., "package-tracker")
        short_task_id: Compact ID for callback tokens (max 8 chars, e.g., "pkg")
        description: Human-readable description
        schema: JSON Schema dict for result.json
        schedule: Cron expression or interval (e.g., "0 9,18 * * *")
        collector: Script name (relative to ~/.hermes/scripts/)
        prompt: Default prompt text or path to prompt.md
        deliver: Delivery target (same as cron "deliver")
        continue_dialog: Legacy option for Telegram inline callback buttons. Defaults off; continue by replying to the task message.

    Returns:
        The task definition dict.
    """
    task_id = _validate_task_id(task_id)
    short_task_id = _validate_short_task_id(short_task_id)

    task_config = {
        "task_id": task_id,
        "short_task_id": short_task_id,
        "description": description,
        "schema_id": f"{task_id}/v1",
        "schedule": schedule,
        "collector": collector,
        "default_prompt": prompt,
        "deliver": deliver,
        "continue_dialog": continue_dialog,
        "created_at": _utcnow().isoformat(),
        "updated_at": _utcnow().isoformat(),
    }

    task_dir = _ensure_dir(TASKS_DIR / task_id)
    _write_json(task_dir / "task.json", task_config)

    # Save schema
    schema_path = task_dir / "result.schema.json"
    _write_json(schema_path, schema)

    # Save prompt if provided as text (not a file path)
    if prompt and not prompt.startswith("prompt.md"):
        _ensure_dir(task_dir)
        (task_dir / "prompt.md").write_text(prompt)

    return task_config


def read_task_definition(task_id: str) -> Optional[dict]:
    """Read a task definition by task_id."""
    task_id = _validate_task_id(task_id)
    task_file = _safe_child(TASKS_DIR, task_id, "task.json")
    return _read_json(task_file)


def list_task_definitions() -> list[dict]:
    """List all registered task definitions."""
    tasks = []
    if not TASKS_DIR.exists():
        return tasks
    for d in sorted(TASKS_DIR.iterdir()):
        if d.is_dir():
            cfg = _read_json(d / "task.json")
            if cfg:
                tasks.append(cfg)
    return tasks


def resolve_task_by_short_id(short_task_id: str) -> Optional[dict]:
    """Find a task definition by its short ID."""
    for task in list_task_definitions():
        if task.get("short_task_id") == short_task_id:
            return task
    return None


# ---------------------------------------------------------------------------
# Run lifecycle
# ---------------------------------------------------------------------------


def create_run(task_id: str, collector_result: Optional[str] = None) -> dict:
    """
    Create a new run for a task.

    - Reads task definition
    - Creates run directory ~/.hermes/agent_runs/<short_id>/<run_id>/
    - Creates run.json with metadata
    - Saves collector stdout/stderr if provided
    - Returns run info dict

    Returns:
        {
            "task_id": str,
            "short_task_id": str,
            "run_id": str,
            "run_dir": Path,
            "run_file": Path (run.json),
            "schema_id": str,
        }
    """
    task = read_task_definition(task_id)
    if not task:
        raise ValueError(f"Task not found: {task_id!r}")

    short_task_id = task["short_task_id"]
    run_id = make_run_id()
    run_dir = _ensure_dir(RUNS_DIR / short_task_id / run_id)

    run_data = {
        "task_id": task_id,
        "short_task_id": short_task_id,
        "run_id": run_id,
        "schema_id": task["schema_id"],
        "created_at": _utcnow().isoformat(),
        "status": "created",
        "result_file": "result.json",
        "prompt_context_file": "prompt_context.json",
        "stdout_file": "stdout.txt",
        "stderr_file": "stderr.txt",
    }

    run_file = run_dir / "run.json"
    _write_json(run_file, run_data)

    return {
        "task_id": task_id,
        "short_task_id": short_task_id,
        "run_id": run_id,
        "run_dir": run_dir,
        "run_file": run_file,
        "schema_id": task["schema_id"],
    }


def save_run_result(run_dir: Path, result: dict) -> None:
    """
    Save the result of a run to result.json and update run.json status.

    Args:
        run_dir: Path to the run directory
        result: Result dict (must follow the task's schema)
    """
    run_file = run_dir / "run.json"
    run_data = _read_json(run_file) or {}
    run_data["status"] = result.get("status", "ok")
    run_data["completed_at"] = _utcnow().isoformat()
    _write_json(run_file, run_data)

    result_file = run_dir / "result.json"
    _write_json(result_file, result)


def save_prompt_context(run_dir: Path, context: dict) -> None:
    """
    Save compact prompt context for the initial cron agent message.
    This is a subset of result.json — enough for the agent to generate
    a useful summary without loading the full result.
    """
    _write_json(run_dir / "prompt_context.json", context)


def save_run_stdout_stderr(run_dir: Path, stdout: str, stderr: str) -> None:
    """Save collector script output."""
    if stdout:
        path = run_dir / "stdout.txt"
        path.write_text(stdout, encoding="utf-8")
        path.chmod(0o600)
    if stderr:
        path = run_dir / "stderr.txt"
        path.write_text(stderr, encoding="utf-8")
        path.chmod(0o600)


def read_run(task_id: str, run_id: str) -> Optional[dict]:
    """
    Read a run by task_id and run_id.

    Returns:
        {"run": dict, "result": dict, "task": dict, "run_dir": Path}
        or None if not found.
    """
    _parse_run_id(run_id)
    task = read_task_definition(task_id)
    if not task:
        return None
    short_task_id = task["short_task_id"]
    run_dir = _safe_child(RUNS_DIR, short_task_id, run_id)
    if not run_dir.exists():
        return None
    run_data = _read_json(run_dir / "run.json")
    result = _read_json(run_dir / "result.json")
    if not run_data:
        return None
    return {
        "run": run_data,
        "result": result,
        "task": task,
        "run_dir": run_dir,
    }


def read_run_by_callback(data: str) -> Optional[dict]:
    """
    Resolve a callback token to the full run context.
    Returns the same as read_run().
    """
    parsed = parse_callback_data(data)
    if not parsed:
        return None
    task = resolve_task_by_short_id(parsed["short_task_id"])
    if not task:
        return None
    return read_run(task["task_id"], parsed["run_id"])


def record_delivery_status(
    task_id: str,
    run_id: str,
    *,
    status: str,
    details: Optional[dict] = None,
    error: Optional[str] = None,
) -> dict:
    """Persist delivery state as a small outbox record in run.json."""
    if status not in {"pending", "delivered", "failed", "skipped"}:
        raise ValueError(f"Invalid delivery status: {status!r}")
    run_ctx = read_run(task_id, run_id)
    if not run_ctx:
        raise ValueError(f"Run not found: task_id={task_id} run_id={run_id}")

    run_data = run_ctx["run"] or {}
    previous = run_data.get("delivery") or {}
    attempts = int(previous.get("attempts") or 0)
    if status in {"delivered", "failed"}:
        attempts += 1
    delivery = {
        "status": status,
        "attempts": attempts,
        "updated_at": _utcnow().isoformat(),
    }
    for key in ("target", "thread_id", "message_id", "reason"):
        if details and details.get(key) is not None:
            delivery[key] = details[key]
    if error:
        delivery["error"] = str(error)[:2000]
    run_data["delivery"] = delivery
    _write_json(Path(run_ctx["run_dir"]) / "run.json", run_data)
    return delivery


def _collector_run_id(collector_output: dict) -> Optional[str]:
    run_id = ((collector_output or {}).get("run_info") or {}).get("run_id")
    if not run_id:
        return None
    _parse_run_id(str(run_id))
    return str(run_id)


def list_failed_deliveries(task_id: str, limit: int = 20) -> list[dict]:
    """Return newest runs whose persisted delivery state is failed."""
    task = read_task_definition(task_id)
    if not task:
        return []
    runs_dir = _safe_child(RUNS_DIR, task["short_task_id"])
    if not runs_dir.exists():
        return []
    failed = []
    for run_dir in sorted(runs_dir.iterdir(), reverse=True):
        if not run_dir.is_dir():
            continue
        try:
            _parse_run_id(run_dir.name)
        except ValueError:
            continue
        run_data = _read_json(run_dir / "run.json") or {}
        delivery = run_data.get("delivery") or {}
        if delivery.get("status") == "failed":
            failed.append({"task_id": task_id, "run_id": run_dir.name, "delivery": delivery})
            if len(failed) >= limit:
                break
    return failed


def _run_created_at(run_dir: Path) -> Optional[datetime]:
    try:
        parsed = _parse_run_id(run_dir.name)
        return datetime.strptime(parsed["timestamp"], "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _directory_size(path: Path) -> int:
    total = 0
    for item in path.rglob("*"):
        try:
            if item.is_file() and not item.is_symlink():
                total += item.stat().st_size
        except OSError:
            continue
    return total


def prune_runs(
    *,
    retention_days: int = DEFAULT_RETENTION_DAYS,
    max_runs_per_task: int = DEFAULT_MAX_RUNS_PER_TASK,
    min_runs_per_task: int = DEFAULT_MIN_RUNS_PER_TASK,
    dry_run: bool = True,
    task_id: Optional[str] = None,
    now: Optional[datetime] = None,
) -> dict:
    """Apply bounded age/count retention to run directories."""
    if retention_days < 1 or max_runs_per_task < 1 or min_runs_per_task < 0:
        raise ValueError("Retention values must be positive (min_runs_per_task may be zero)")
    if min_runs_per_task > max_runs_per_task:
        raise ValueError("min_runs_per_task cannot exceed max_runs_per_task")
    now = now or _utcnow()
    cutoff = now - timedelta(days=retention_days)
    tasks = [read_task_definition(task_id)] if task_id else list_task_definitions()
    tasks = [task for task in tasks if task]
    candidates: list[dict] = []

    for task in tasks:
        runs_dir = _safe_child(RUNS_DIR, task["short_task_id"])
        if not runs_dir.exists():
            continue
        runs = []
        for run_dir in runs_dir.iterdir():
            created_at = _run_created_at(run_dir) if run_dir.is_dir() else None
            if created_at:
                runs.append((created_at, run_dir))
        runs.sort(key=lambda item: item[0], reverse=True)
        for index, (created_at, run_dir) in enumerate(runs):
            over_count = index >= max_runs_per_task
            over_age = created_at < cutoff and index >= min_runs_per_task
            if not (over_count or over_age):
                continue
            candidates.append(
                {
                    "task_id": task["task_id"],
                    "run_id": run_dir.name,
                    "path": str(run_dir),
                    "bytes": _directory_size(run_dir),
                    "reason": "count" if over_count else "age",
                }
            )

    deleted_count = 0
    deleted_bytes = 0
    if not dry_run:
        runs_root = RUNS_DIR.resolve()
        for candidate in candidates:
            path = Path(candidate["path"]).resolve()
            if not path.is_relative_to(runs_root) or path == runs_root:
                raise RuntimeError(f"Refusing to prune unsafe path: {path}")
            shutil.rmtree(path)
            deleted_count += 1
            deleted_bytes += int(candidate["bytes"])

    return {
        "success": True,
        "dry_run": dry_run,
        "retention_days": retention_days,
        "max_runs_per_task": max_runs_per_task,
        "min_runs_per_task": min_runs_per_task,
        "candidate_count": len(candidates),
        "candidate_bytes": sum(int(item["bytes"]) for item in candidates),
        "deleted_count": deleted_count,
        "deleted_bytes": deleted_bytes,
        "candidates": candidates,
    }


def _managed_python_path() -> Path:
    return _HERMES_HOME / "hermes-agent" / "venv" / "bin" / "python"


def _ensure_supported_runtime() -> None:
    """Re-exec the managed Hermes Python when invoked by a legacy cron entry."""
    if sys.version_info >= (3, 11):
        return
    managed_python = _managed_python_path()
    if not managed_python.is_file() or not os.access(managed_python, os.X_OK):
        raise RuntimeError(
            f"agent-task requires Python 3.11+; current={sys.version.split()[0]} managed={managed_python}"
        )
    os.execv(str(managed_python), [str(managed_python), str(Path(__file__).resolve()), *sys.argv[1:]])


def build_health_report() -> dict:
    """Build a side-effect-free production health report."""
    checks: list[dict] = []

    def add(name: str, status: str, detail: str) -> None:
        checks.append({"name": name, "status": status, "detail": detail})

    managed_python = _managed_python_path()
    add(
        "managed_python",
        "ok" if managed_python.is_file() and os.access(managed_python, os.X_OK) else "fail",
        str(managed_python),
    )
    add(
        "current_python",
        "ok" if sys.version_info >= (3, 11) else "fail",
        f"{sys.executable} {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
    )

    tasks = list_task_definitions()
    add("task_definitions", "ok" if tasks else "fail", f"{len(tasks)} task(s)")
    for task in tasks:
        task_id = task.get("task_id", "unknown")
        collector = task.get("collector")
        try:
            collector_path = _safe_child(SCRIPTS_DIR, str(collector)) if collector else None
            valid = bool(collector_path and collector_path.is_file())
        except ValueError:
            collector_path = None
            valid = False
        add(f"collector:{task_id}", "ok" if valid else "fail", str(collector_path or collector or "missing"))

        runs_dir = _safe_child(RUNS_DIR, str(task.get("short_task_id")))
        recent = []
        if runs_dir.exists():
            recent = [path for path in sorted(runs_dir.iterdir(), reverse=True) if _run_created_at(path)]
        if not recent:
            add(f"last_run:{task_id}", "warn", "no runs")
            continue
        run_data = _read_json(recent[0] / "run.json") or {}
        delivery = run_data.get("delivery") or {}
        if delivery.get("status") == "failed":
            add(f"last_delivery:{task_id}", "fail", f"failed run {recent[0].name}")
        elif delivery.get("status"):
            add(f"last_delivery:{task_id}", "ok", f"{delivery.get('status')} run {recent[0].name}")
        else:
            add(f"last_delivery:{task_id}", "warn", f"legacy run {recent[0].name} has no delivery state")

    return {
        "healthy": not any(check["status"] == "fail" for check in checks),
        "generated_at": _utcnow().isoformat(),
        "hermes_home": str(_HERMES_HOME),
        "checks": checks,
    }


# ---------------------------------------------------------------------------
# Collector script runner
# ---------------------------------------------------------------------------


_RUNNER_SCRIPT_TIMEOUT = 120


def run_collector(collector_name: str) -> tuple[bool, str, str]:
    """
    Run a collector script (from ~/.hermes/scripts/) and capture stdout/stderr.

    Returns:
        (success, stdout, stderr)
    """
    try:
        script_path = _safe_child(SCRIPTS_DIR, str(collector_name))
    except ValueError as exc:
        return False, "", f"Invalid collector path: {exc}"
    if not script_path.is_file():
        return False, "", f"Collector script not found: {collector_name}"

    try:
        result = subprocess.run(
            [sys.executable, str(script_path)],
            capture_output=True,
            text=True,
            timeout=_RUNNER_SCRIPT_TIMEOUT,
            cwd=str(script_path.parent),
        )
        stdout = (result.stdout or "").strip()
        stderr = (result.stderr or "").strip()
        success = result.returncode == 0
        if not success:
            stderr = f"Exit code {result.returncode}:\n{stderr}"
        return success, stdout, stderr
    except subprocess.TimeoutExpired:
        return False, "", f"Collector timed out after {_RUNNER_SCRIPT_TIMEOUT}s"
    except Exception as e:
        return False, "", f"Collector error: {e}"


# ---------------------------------------------------------------------------
# Delivery helpers
# ---------------------------------------------------------------------------


def _load_gateway_config_for_delivery():
    """Load Hermes gateway config without requiring this script to be run inside the repo."""
    repo = _HERMES_HOME / "hermes-agent"
    if str(repo) not in sys.path:
        sys.path.insert(0, str(repo))
    try:
        from dotenv import load_dotenv

        load_dotenv(str(_HERMES_HOME / ".env"), override=False)
    except Exception:
        pass
    from gateway.config import load_gateway_config, Platform

    return load_gateway_config(), Platform


def _resolve_telegram_target(
    deliver: str,
    explicit_chat_id: Optional[str] = None,
    explicit_thread_id: Optional[str] = None,
    task: Optional[dict] = None,
) -> tuple[str, Optional[str], str]:
    """Resolve Telegram chat/thread target for delivery."""
    if explicit_chat_id:
        return str(explicit_chat_id), str(explicit_thread_id) if explicit_thread_id else None, "explicit"

    task_delivery = (task or {}).get("delivery") or {}
    if task_delivery.get("platform") == "telegram" and task_delivery.get("chat_id"):
        return (
            str(task_delivery["chat_id"]),
            str(task_delivery.get("thread_id")) if task_delivery.get("thread_id") else None,
            "task_config",
        )

    # origin means current gateway session if env vars are present; otherwise home.
    if deliver == "origin":
        platform = os.getenv("HERMES_SESSION_PLATFORM", "").strip().lower()
        chat_id = os.getenv("HERMES_SESSION_CHAT_ID", "").strip()
        thread_id = os.getenv("HERMES_SESSION_THREAD_ID", "").strip() or None
        if platform == "telegram" and chat_id:
            return chat_id, thread_id, "origin"

    cfg, Platform = _load_gateway_config_for_delivery()
    home = cfg.get_home_channel(Platform.TELEGRAM)
    if not home:
        raise RuntimeError("No Telegram home channel configured; set task delivery or pass --chat-id")
    return str(home.chat_id), None, "telegram_home"


def _format_task_message(task: dict, collector_output: dict) -> str:
    """Create a concise user-facing notification from collector output."""
    run_info = collector_output.get("run_info") or {}
    prompt_context = collector_output.get("prompt_context") or {}
    task_id = run_info.get("task_id") or task.get("task_id", "agent-task")
    run_id = run_info.get("run_id", "?")
    status = prompt_context.get("status", "?")
    summary = str(prompt_context.get("summary") or "No summary.")
    if len(summary) > 3400:
        summary = summary[:3397].rstrip() + "..."
    changes = prompt_context.get("changes_count")
    shipments = prompt_context.get("shipments_count")

    title = f"⏱️ {task_id}: {status}"
    if changes is not None:
        title += f" · changes: {changes}"
    if shipments is not None:
        title += f" · items: {shipments}"
    footer = f"run_id: {run_id}"
    if run_id and run_id != "?":
        footer += "\n\nЧтобы продолжить: ответь reply на это сообщение."
    return f"{title}\n{summary}\n\n{footer}"


def _read_collector_output_from_run(task_id: str, run_id: str) -> dict:
    """Build collector-output-like structure from an existing run directory."""
    run_ctx = read_run(task_id, run_id)
    if not run_ctx:
        raise RuntimeError(f"Run not found: task_id={task_id} run_id={run_id}")
    run_data = run_ctx["run"] or {}
    result = run_ctx["result"] or {}
    task = run_ctx["task"] or {}
    short_task_id = run_data.get("short_task_id") or task.get("short_task_id")
    if not short_task_id:
        raise RuntimeError(f"short_task_id missing for task {task_id}")
    prompt_context = _read_json(Path(run_ctx["run_dir"]) / "prompt_context.json") or {
        "status": result.get("status"),
        "summary": result.get("summary"),
    }
    return {
        "run_info": {
            "task_id": task_id,
            "run_id": run_id,
            "short_task_id": short_task_id,
            "callback_data": make_callback_data(short_task_id, run_id),
            "run_dir": str(run_ctx["run_dir"]),
        },
        "prompt_context": prompt_context,
        "result_schema": run_data.get("schema_id") or result.get("schema_id"),
    }


def _telegram_api_credentials() -> tuple[str, str]:
    """Resolve the bot token and PTB-compatible API base URL."""
    cfg, Platform = _load_gateway_config_for_delivery()
    pconfig = cfg.platforms.get(Platform.TELEGRAM)
    token = getattr(pconfig, "token", None) if pconfig else None
    token = token or os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("Telegram bot token not configured")
    extra = getattr(pconfig, "extra", {}) if pconfig else {}
    base_url = (extra or {}).get("base_url") or "https://api.telegram.org/bot"
    return str(token), str(base_url).rstrip("/")


def _telegram_api_url(method: str) -> str:
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", method):
        raise ValueError(f"Invalid Telegram method: {method!r}")
    token, base_url = _telegram_api_credentials()
    return f"{base_url}{token}/{method}"


def _telegram_api_call(method: str, payload: dict) -> dict:
    """Call Telegram Bot API, honoring the gateway's local Bot API URL."""
    req = urllib.request.Request(
        _telegram_api_url(method),
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    for attempt in range(1, 4):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read().decode("utf-8")
            result = json.loads(data)
            if not isinstance(result, dict):
                raise RuntimeError("Telegram API returned a non-object response")
            return result
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            retryable = exc.code == 429 or 500 <= exc.code < 600
            if retryable and attempt < 3:
                delay = min(2 ** (attempt - 1), 5)
                try:
                    retry_after = (json.loads(body).get("parameters") or {}).get("retry_after")
                    delay = min(max(float(retry_after), 0.1), 30) if retry_after is not None else delay
                except (TypeError, ValueError, json.JSONDecodeError):
                    pass
                time.sleep(delay)
                continue
            raise RuntimeError(f"Telegram API HTTP {exc.code}: {body[:2000]}") from exc
        except urllib.error.URLError as exc:
            if attempt < 3:
                time.sleep(2 ** (attempt - 1))
                continue
            raise RuntimeError(f"Telegram API unavailable: {exc.reason}") from exc
    raise RuntimeError("Telegram API retry loop exhausted")


def _create_telegram_forum_topic(chat_id: str, topic_name: str) -> int:
    """Create a Telegram forum topic and return message_thread_id."""
    result = _telegram_api_call("createForumTopic", {"chat_id": chat_id, "name": topic_name})
    if not result.get("ok"):
        raise RuntimeError(json.dumps(result, ensure_ascii=False))
    thread_id = (result.get("result") or {}).get("message_thread_id")
    if thread_id is None:
        raise RuntimeError(f"Telegram did not return message_thread_id: {result}")
    return int(thread_id)


def _default_task_topic_name(task_id: str) -> str:
    return f"Agent Task: {task_id}"


def _persist_task_delivery_thread_id(
    task_id: str, task: dict, thread_id: str, topic_name: Optional[str] = None
) -> None:
    """Persist a newly-created Telegram forum topic ID into task.json."""
    delivery = task.setdefault("delivery", {})
    delivery["thread_id"] = str(thread_id)
    if topic_name:
        delivery["topic_name"] = str(topic_name)
    delivery["configured_at"] = _utcnow().isoformat()
    task["updated_at"] = _utcnow().isoformat()
    _write_json(TASKS_DIR / task_id / "task.json", task)


def _telegram_chat_can_have_topics(chat_id: str) -> bool:
    """Return True when Bot API createForumTopic may be used for this chat.

    Telegram Bot API 9.4 supports topics in 1:1 chats when the user has enabled
    Topics in the bot DM, in addition to classic supergroup forum topics.
    """
    try:
        int(str(chat_id))
        return True
    except (TypeError, ValueError):
        return False


def _ensure_task_delivery_topic(task_id: str, task: dict, chat_id: str, thread_id: Optional[str]) -> Optional[str]:
    """Create and persist a Telegram forum topic when task delivery has no thread ID."""
    delivery = (task or {}).get("delivery") or {}
    if thread_id or delivery.get("platform") != "telegram" or not _telegram_chat_can_have_topics(chat_id):
        return thread_id
    topic_name = delivery.get("topic_name") or _default_task_topic_name(task_id)
    new_thread_id = str(_create_telegram_forum_topic(chat_id, str(topic_name)))
    _persist_task_delivery_thread_id(task_id, task, new_thread_id, str(topic_name))
    return new_thread_id


def _is_missing_telegram_thread_error(exc: Exception) -> bool:
    """Return True when Telegram rejected a stale/deleted forum topic ID."""
    text = str(exc).lower()
    return any(
        marker in text
        for marker in (
            "message thread not found",
            "message thread_id_invalid",
            "thread not found",
            "topic_deleted",
            "topic deleted",
        )
    )


def _send_telegram_message(
    chat_id: str, text: str, callback_data: Optional[str] = None, thread_id: Optional[str] = None
) -> dict:
    """Send Telegram message directly via Bot API.

    ``callback_data`` is accepted for backwards compatibility but intentionally
    ignored in this profile: agent-task continuation happens by replying to the
    notification message so the task system can remain a plugin without fragile
    Telegram callback plumbing.
    """
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
    }
    if thread_id:
        payload["message_thread_id"] = int(thread_id)

    return _telegram_api_call("sendMessage", payload)


def deliver_task_output(
    task_id: str,
    collector_output: dict,
    deliver: str = "telegram",
    chat_id: Optional[str] = None,
    thread_id: Optional[str] = None,
) -> dict:
    """Deliver a collector output to Telegram. Continuation is via plain reply."""
    task = read_task_definition(task_id) or {"task_id": task_id}
    if deliver in ("local", "none", ""):
        return {"success": True, "delivered": False, "reason": "local"}
    if deliver not in ("origin", "telegram") and not deliver.startswith("telegram"):
        raise RuntimeError(f"Unsupported deliver target for agent_task runner: {deliver}")

    target_chat_id, target_thread_id, target_source = _resolve_telegram_target(deliver, chat_id, thread_id, task=task)
    if target_source == "task_config":
        target_thread_id = _ensure_task_delivery_topic(task_id, task, target_chat_id, target_thread_id)

    text = _format_task_message(task, collector_output)
    callback_data = (collector_output.get("run_info") or {}).get("callback_data")
    try:
        tg_result = _send_telegram_message(
            target_chat_id, text, callback_data=callback_data, thread_id=target_thread_id
        )
    except Exception as exc:
        delivery = (task or {}).get("delivery") or {}
        topic_name = delivery.get("topic_name") or _default_task_topic_name(task_id)
        if target_source != "task_config" or not target_thread_id or not _is_missing_telegram_thread_error(exc):
            raise
        target_thread_id = str(_create_telegram_forum_topic(target_chat_id, str(topic_name)))
        _persist_task_delivery_thread_id(task_id, task, target_thread_id, str(topic_name))
        tg_result = _send_telegram_message(
            target_chat_id, text, callback_data=callback_data, thread_id=target_thread_id
        )

    if not tg_result.get("ok"):
        description = tg_result.get("description") or "Telegram API returned ok=false"
        raise RuntimeError(str(description))

    return {
        "success": True,
        "delivered": True,
        "target": target_source,
        "chat_id": target_chat_id,
        "thread_id": target_thread_id,
        "message_id": ((tg_result.get("result") or {}).get("message_id")),
    }


def retry_failed_deliveries(task_id: str, *, limit: int = 5) -> dict:
    """Retry persisted failed deliveries oldest-first and stop on the first failure."""
    failures = list_failed_deliveries(task_id, limit=limit)
    retried = []
    for item in reversed(failures):
        run_id = item["run_id"]
        collector_output = _read_collector_output_from_run(task_id, run_id)
        try:
            delivery = deliver_task_output(task_id, collector_output, deliver="telegram")
            record_delivery_status(task_id, run_id, status="delivered", details=delivery)
            retried.append({"run_id": run_id, "status": "delivered"})
        except Exception as exc:
            record_delivery_status(task_id, run_id, status="failed", error=str(exc))
            retried.append({"run_id": run_id, "status": "failed", "error": str(exc)[:2000]})
            return {"success": False, "retried": retried, "remaining": len(failures) - len(retried)}
    return {"success": True, "retried": retried, "remaining": 0}


# ---------------------------------------------------------------------------
# Build prompt context for cron agent
# ---------------------------------------------------------------------------


def _skip_requested(collector_output: dict) -> bool:
    """Return True if the collector requested delivery to be skipped.

    The collector script can set ``\"skip_delivery\": true`` at the top level
    of its stdout JSON to indicate there is nothing worth reporting this
    run.  The runner respects this flag and skips delivery entirely.

    This is the generic interface — each collector decides what
    ``skip_delivery`` means for its own task (no shipments, no changes,
    no news, etc.).
    """
    return bool((collector_output or {}).get("skip_delivery"))


def build_agent_prompt_context(task_id: str, run_info: dict) -> str:
    """
    Build the context string injected into the cron agent's prompt.
    Includes run metadata and compact result data.
    """
    task = read_task_definition(task_id)
    if not task:
        return f"Task {task_id!r} not found."

    run_dir = run_info["run_dir"]
    run_data = _read_json(run_dir / "run.json") or {}
    result = _read_json(run_dir / "result.json") or {}
    prompt_context = _read_json(run_dir / "prompt_context.json") or {}

    lines = [
        "# Agent Task Run Context",
        "",
        f"task_id: {task_id}",
        f"run_id: {run_data.get('run_id', '?')}",
        f"schema_id: {run_data.get('schema_id', '?')}",
        f"status: {run_data.get('status', '?')}",
        f"description: {task.get('description', '?')}",
        "",
    ]

    if result:
        lines.append("## Result")
        lines.append(json.dumps(result, indent=2, ensure_ascii=False))
        lines.append("")

    if prompt_context:
        lines.append("## Prompt Context (compact)")
        lines.append(json.dumps(prompt_context, indent=2, ensure_ascii=False))
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def cli_main() -> None:
    """Simple CLI for task management."""
    _ensure_supported_runtime()
    import argparse

    parser = argparse.ArgumentParser(description="Agent Task Runner")
    sub = parser.add_subparsers(dest="command")

    # list
    sub.add_parser("list", help="List all tasks")

    # create
    create_p = sub.add_parser("create", help="Create a task definition")
    create_p.add_argument("task_id", help="Task identifier")
    create_p.add_argument("--short", required=True, help="Short ID (2-8 chars)")
    create_p.add_argument("--desc", required=True, help="Description")
    create_p.add_argument("--schedule", help="Cron schedule")
    create_p.add_argument("--collector", help="Collector script name")

    # run
    run_p = sub.add_parser("run", help="Run a task collector and print collector JSON")
    run_p.add_argument("task_id", help="Task identifier")
    run_p.add_argument(
        "--deliver",
        choices=["local", "none", "origin", "telegram"],
        default="local",
        help="Optional delivery target. Default: local/stdout only",
    )
    run_p.add_argument("--chat-id", help="Explicit Telegram chat ID for delivery")
    run_p.add_argument("--thread-id", help="Explicit Telegram message_thread_id for delivery")
    run_p.add_argument(
        "--skip-if-empty", action="store_true", help="Skip delivery when collector set skip_delivery: true"
    )
    run_p.add_argument(
        "--retry-failed", action="store_true", help="Retry persisted failed deliveries before collecting"
    )
    run_p.add_argument("--prune", action="store_true", help="Apply run retention after a successful collector run")

    retry_p = sub.add_parser("retry-failed", help="Retry persisted failed deliveries for a task")
    retry_p.add_argument("task_id", help="Task identifier")
    retry_p.add_argument("--limit", type=int, default=5, help="Maximum failed runs to retry")

    prune_p = sub.add_parser("prune", help="Prune old run directories")
    prune_p.add_argument("--task-id", help="Limit pruning to one task")
    prune_p.add_argument("--days", type=int, default=DEFAULT_RETENTION_DAYS)
    prune_p.add_argument("--max-runs", type=int, default=DEFAULT_MAX_RUNS_PER_TASK)
    prune_p.add_argument("--min-runs", type=int, default=DEFAULT_MIN_RUNS_PER_TASK)
    prune_p.add_argument("--apply", action="store_true", help="Delete candidates; default is dry-run")

    sub.add_parser("doctor", help="Print a production health report")

    # deliver existing run
    deliver_p = sub.add_parser("deliver", help="Deliver an existing run to the configured Telegram target")
    deliver_p.add_argument("task_id", help="Task identifier")
    deliver_p.add_argument("run_id", help="Run identifier")
    deliver_p.add_argument("--to", choices=["telegram", "origin"], default="telegram", help="Delivery target")
    deliver_p.add_argument("--chat-id", help="Explicit Telegram chat ID")
    deliver_p.add_argument("--thread-id", help="Explicit Telegram message_thread_id")
    deliver_p.add_argument(
        "--skip-if-empty", action="store_true", help="Skip delivery when collector set skip_delivery: true"
    )

    # set-delivery
    set_delivery_p = sub.add_parser("set-delivery", help="Set default delivery target for a task")
    set_delivery_p.add_argument("task_id", help="Task identifier")
    set_delivery_p.add_argument("--platform", choices=["telegram"], default="telegram")
    set_delivery_p.add_argument("--chat-id", required=True, help="Telegram chat ID")
    set_delivery_p.add_argument("--thread-id", help="Telegram message_thread_id / forum topic ID")
    set_delivery_p.add_argument(
        "--topic-name", help="Human-readable topic name (stored only unless --create-topic is used)"
    )
    set_delivery_p.add_argument(
        "--create-topic",
        action="store_true",
        help="Create Telegram forum topic if possible and store returned thread_id",
    )

    # info
    info_p = sub.add_parser("info", help="Show task info")
    info_p.add_argument("task_id", help="Task identifier")

    # list-runs
    list_runs_p = sub.add_parser("list-runs", help="List runs for a task")
    list_runs_p.add_argument("task_id", help="Task identifier")

    args = parser.parse_args()

    if args.command == "list":
        tasks = list_task_definitions()
        if not tasks:
            print("No tasks defined.")
            return
        for t in tasks:
            print(f"  {t['task_id']:30s} short={t['short_task_id']:6s}  {t.get('description', '')}")

    elif args.command == "create":
        schema = {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "type": "object",
            "required": ["job_id", "run_id", "status"],
            "properties": {
                "job_id": {"type": "string"},
                "run_id": {"type": "string"},
                "status": {"type": "string"},
            },
        }
        create_task_definition(
            task_id=args.task_id,
            short_task_id=args.short,
            description=args.desc,
            schema=schema,
            schedule=args.schedule,
            collector=args.collector,
        )
        print(f"Task '{args.task_id}' created.")

    elif args.command == "run":
        try:
            with task_run_lock(args.task_id):
                if args.retry_failed:
                    retry_report = retry_failed_deliveries(args.task_id)
                    print(json.dumps({"retry_failed": retry_report}, ensure_ascii=False), file=sys.stderr)
                    if not retry_report["success"]:
                        sys.exit(1)

                task = read_task_definition(args.task_id)
                if not task:
                    print(f"Task not found: {args.task_id}", file=sys.stderr)
                    sys.exit(1)
                collector = task.get("collector")
                if not collector:
                    print(f"Task has no collector configured: {args.task_id}", file=sys.stderr)
                    sys.exit(1)
                ok, stdout, stderr = run_collector(collector)
                if stderr:
                    print(stderr, file=sys.stderr)
                collector_output = None
                if stdout:
                    print(stdout)
                    try:
                        collector_output = json.loads(stdout)
                    except Exception:
                        collector_output = None
                if ok and args.deliver not in ("local", "none"):
                    if not isinstance(collector_output, dict):
                        print("Cannot deliver: collector stdout is not JSON", file=sys.stderr)
                        sys.exit(1)
                    run_id = _collector_run_id(collector_output)
                    if args.skip_if_empty and _skip_requested(collector_output):
                        if run_id:
                            record_delivery_status(
                                args.task_id,
                                run_id,
                                status="skipped",
                                details={"reason": "collector_requested_skip"},
                            )
                        print("Skipping delivery: collector set skip_delivery", file=sys.stderr)
                    else:
                        try:
                            delivery = deliver_task_output(
                                args.task_id,
                                collector_output,
                                deliver=args.deliver,
                                chat_id=args.chat_id,
                                thread_id=args.thread_id,
                            )
                            if run_id:
                                record_delivery_status(args.task_id, run_id, status="delivered", details=delivery)
                            print(json.dumps({"delivery": delivery}, indent=2, ensure_ascii=False), file=sys.stderr)
                        except Exception as exc:
                            if run_id:
                                record_delivery_status(args.task_id, run_id, status="failed", error=str(exc))
                            print(f"Delivery failed: {exc}", file=sys.stderr)
                            sys.exit(1)
                if not ok:
                    sys.exit(1)
                if args.prune:
                    report = prune_runs(dry_run=False, task_id=args.task_id)
                    print(json.dumps({"retention": report}, ensure_ascii=False), file=sys.stderr)
        except TaskAlreadyRunning as exc:
            print(json.dumps({"success": False, "error": str(exc), "kind": "already_running"}), file=sys.stderr)
            sys.exit(75)

    elif args.command == "deliver":
        try:
            collector_output = _read_collector_output_from_run(args.task_id, args.run_id)
            if args.skip_if_empty and _skip_requested(collector_output):
                record_delivery_status(
                    args.task_id,
                    args.run_id,
                    status="skipped",
                    details={"reason": "collector_requested_skip"},
                )
                print(
                    json.dumps(
                        {"success": True, "skipped": True, "reason": "skip_delivery_flag"}, indent=2, ensure_ascii=False
                    )
                )
            else:
                delivery = deliver_task_output(
                    args.task_id, collector_output, deliver=args.to, chat_id=args.chat_id, thread_id=args.thread_id
                )
                record_delivery_status(args.task_id, args.run_id, status="delivered", details=delivery)
                print(
                    json.dumps(
                        {"success": True, "delivery": delivery, "run_info": collector_output.get("run_info")},
                        indent=2,
                        ensure_ascii=False,
                    )
                )
        except Exception as exc:
            try:
                record_delivery_status(args.task_id, args.run_id, status="failed", error=str(exc))
            except Exception:
                pass
            print(json.dumps({"success": False, "error": str(exc)}, indent=2, ensure_ascii=False), file=sys.stderr)
            sys.exit(1)

    elif args.command == "retry-failed":
        report = retry_failed_deliveries(args.task_id, limit=args.limit)
        print(json.dumps(report, indent=2, ensure_ascii=False))
        if not report["success"]:
            sys.exit(1)

    elif args.command == "prune":
        report = prune_runs(
            retention_days=args.days,
            max_runs_per_task=args.max_runs,
            min_runs_per_task=args.min_runs,
            dry_run=not args.apply,
            task_id=args.task_id,
        )
        print(json.dumps(report, indent=2, ensure_ascii=False))

    elif args.command == "doctor":
        report = build_health_report()
        print(json.dumps(report, indent=2, ensure_ascii=False))
        if not report["healthy"]:
            sys.exit(1)

    elif args.command == "set-delivery":
        task = read_task_definition(args.task_id)
        if not task:
            print(f"Task not found: {args.task_id}", file=sys.stderr)
            sys.exit(1)
        thread_id = args.thread_id
        if args.create_topic:
            if not args.topic_name:
                print("--topic-name is required with --create-topic", file=sys.stderr)
                sys.exit(1)
            try:
                thread_id = str(_create_telegram_forum_topic(args.chat_id, args.topic_name))
            except Exception as exc:
                print(f"Failed to create Telegram topic: {exc}", file=sys.stderr)
                sys.exit(1)
        task["delivery"] = {
            "platform": args.platform,
            "chat_id": str(args.chat_id),
            "thread_id": str(thread_id) if thread_id else None,
            "topic_name": args.topic_name,
            "configured_at": _utcnow().isoformat(),
        }
        task["updated_at"] = _utcnow().isoformat()
        _write_json(_safe_child(TASKS_DIR, args.task_id, "task.json"), task)
        print(
            json.dumps(
                {"success": True, "task_id": args.task_id, "delivery": task["delivery"]}, indent=2, ensure_ascii=False
            )
        )

    elif args.command == "info":
        task = read_task_definition(args.task_id)
        if task:
            print(json.dumps(task, indent=2, ensure_ascii=False))
        else:
            print(f"Task not found: {args.task_id}", file=sys.stderr)
            sys.exit(1)

    elif args.command == "list-runs":
        task = read_task_definition(args.task_id)
        if not task:
            print(f"Task not found: {args.task_id}", file=sys.stderr)
            sys.exit(1)
        runs_dir = RUNS_DIR / task["short_task_id"]
        if not runs_dir.exists():
            print("No runs yet.")
            return
        for d in sorted(runs_dir.iterdir(), reverse=True):
            if d.is_dir():
                run_data = _read_json(d / "run.json") or {}
                status = run_data.get("status", "?")
                created = run_data.get("created_at", "?")
                print(f"  {d.name:35s} status={status:12s}  {created}")

    else:
        parser.print_help()


if __name__ == "__main__":
    cli_main()
