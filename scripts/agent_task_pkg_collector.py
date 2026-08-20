#!/usr/bin/env python3
"""
Collector script for package-tracker agent task.

Creates a run directory, runs the existing tracker.py, saves structured
result.json, and outputs compact context to stdout for the cron agent.

Usage:
    python3 agent_task_pkg_collector.py
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# Import agent_task_runner from the scripts directory
HERMES_HOME = Path(os.path.expanduser(os.environ.get("HERMES_HOME", "~/.hermes")))
SCRIPTS_DIR = str(HERMES_HOME / "scripts")
sys.path.insert(0, SCRIPTS_DIR)

from agent_task_runner import (  # noqa: E402
    create_run,
    save_run_result,
    save_prompt_context,
    save_run_stdout_stderr,
    make_callback_data,
    read_task_definition,
    RUNS_DIR,
)


TASK_ID = "package-tracker"
TRACKER_SCRIPT = os.path.join(SCRIPTS_DIR, "tracker.py")


def run_tracker() -> tuple[bool, str, str]:
    """Run the existing tracker.py and capture its output."""
    if not os.path.exists(TRACKER_SCRIPT):
        return False, "", f"Tracker script not found: {TRACKER_SCRIPT}"
    try:
        result = subprocess.run(
            [sys.executable, TRACKER_SCRIPT],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=SCRIPTS_DIR,
        )
        stdout = (result.stdout or "").strip()
        stderr = (result.stderr or "").strip()
        success = result.returncode == 0
        return success, stdout, stderr
    except subprocess.TimeoutExpired:
        return False, "", "Tracker script timed out (120s)"
    except Exception as e:
        return False, "", f"Tracker error: {e}"


STATE_FILE = str(HERMES_HOME / "tracker_state.json")


def _load_tracker_state() -> dict | None:
    """Load tracker_state.json directly. Returns None if unavailable."""
    import json

    if not os.path.exists(STATE_FILE):
        return None
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return None


def _state_shipments(state: dict) -> list[dict]:
    """Convert tracker_state.json 'seen' entries to the standard shipments format."""
    shipments = []
    seen = state.get("seen") or {}
    for key, entry in seen.items():
        tracking = entry.get("tracking_number") or entry.get("tracking", "")
        carrier = (entry.get("carrier_code") or entry.get("carrier", "")).lower()
        shipper = entry.get("description") or entry.get("shipper", "")
        raw_status = entry.get("status", "")
        raw_date = entry.get("date_expected") or entry.get("last_checked") or entry.get("date", "")
        subject = entry.get("subject", "")
        latest_event = entry.get("latest_event", "")

        if not tracking or not carrier:
            continue

        s = {
            "carrier": carrier,
            "tracking_number": tracking,
            "shipper": shipper,
            "status": f"{carrier.upper()}: {tracking} — {raw_status} ({shipper})",
            "status_category": _classify_status(raw_status),
            "status_raw": raw_status,
            "last_update": raw_date,
            "subject": subject,
            "latest_event": latest_event,
            "changed_since_last_run": False,
        }
        shipments.append(s)
    return shipments


def parse_tracker_output(stdout: str) -> dict:
    """
    Parse tracker.py stdout into the standard result schema.

    tracker.py outputs a human-readable summary and writes structured data
    to tracker_state.json. We prefer the state file over text heuristics.
    """
    result = {
        "job_id": TASK_ID,
        "schema_id": "package-tracker/v1",
        "status": "ok",
        "summary": "",
        "shipments": [],
        "delivery_changes": [],
        "package_email_mentions": [],
        "errors": [],
    }

    # Prefer structured data from tracker_state.json
    state = _load_tracker_state()
    if state:
        result["shipments"] = _state_shipments(state)
        mentions = state.get("last_package_email_mentions")
        if isinstance(mentions, list):
            result["package_email_mentions"] = mentions[:20]

    # Build summary from tracker stdout (first meaningful lines)
    if stdout:
        lines = stdout.strip().split("\n")
        summary_parts = []
        for line in lines:
            line = line.strip()
            if line and not line.startswith("---") and len(line) > 10:
                summary_parts.append(line)
                if len(summary_parts) >= 3:
                    break
        result["summary"] = " | ".join(summary_parts[:3]) if summary_parts else stdout[:200]
    else:
        result["status"] = "error"
        result["errors"].append("Tracker produced no output")

    # Detect meaningful changes from tracker stdout. If tracker says no changes,
    # keep delivery silent even though known shipments exist in state.
    shipment_changed = any(
        marker in stdout
        for marker in [
            "🔔 Новые/найденные",
            "🔄 Изменения статуса",
            "➕ Добавлено в Parcel",
            "⚠️ Не удалось добавить",
        ]
    )
    package_email_changed = "📬 Письма о посылках/доставке" in stdout
    if shipment_changed:
        result["delivery_changes"] = result["shipments"]
    elif package_email_changed:
        result["delivery_changes"] = [
            {
                "type": "package_email_mentions",
                "count": len(result.get("package_email_mentions") or []),
            }
        ]

    return result


def _classify_status(status_raw: str) -> str:
    """Classify status from a raw status string (tracker state or text line)."""
    lower = status_raw.lower().replace("_", " ")
    if "доставлен" in lower or "delivered" in lower:
        return "delivered"
    if "у курьер" in lower or "out for delivery" in lower:
        return "out_for_delivery"
    if "ждёт" in lower or "pickup" in lower or "ожида" in lower:
        return "pickup"
    if "ошибк" in lower or "exception" in lower or "возвращ" in lower or "возврат" in lower:
        return "exception"
    if "не найден" in lower or "not found" in lower or "notfound" in lower or "no record" in lower:
        return "notfound"
    if "в пут" in lower or "transit" in lower or "прогресс" in lower:
        return "transit"
    return "transit"  # default for anything else that's active


def _extract_eta(line: str) -> str | None:
    """Extract ETA/date from a tracker output line."""
    import re

    # Match dates like "20 мая", "May 20", "2025-05-20"
    date_match = re.search(r"(\d{1,2}\s+(?:янв|фев|мар|апр|мая|июн|июл|авг|сен|окт|ноя|дек|[A-Z][a-z]+))", line)
    if date_match:
        return date_match.group(0)
    iso_match = re.search(r"(\d{4}-\d{2}-\d{2})", line)
    if iso_match:
        return iso_match.group(1)
    return None


def main():
    # 1. Get the task definition
    task = read_task_definition(TASK_ID)
    if not task:
        result = {
            "task_id": TASK_ID,
            "error": f"Task {TASK_ID!r} not found",
            "status": "error",
        }
        json.dump(result, sys.stdout, indent=2, ensure_ascii=False)
        sys.exit(1)

    # 2. Find previous run ID for change detection
    short_task_id = task["short_task_id"]
    runs_dir = RUNS_DIR / short_task_id
    previous_run_id = None
    if runs_dir.exists():
        run_dirs = sorted(
            [d for d in runs_dir.iterdir() if d.is_dir()],
            reverse=True,
        )
        if run_dirs:
            prev_run = run_dirs[0] / "result.json"
            if prev_run.exists():
                previous_run_id = run_dirs[0].name

    # 3. Create a new run
    run_info = create_run(TASK_ID)
    run_dir = run_info["run_dir"]
    run_id = run_info["run_id"]

    # 4. Run the tracker
    success, tracker_stdout, tracker_stderr = run_tracker()

    # Save raw output
    save_run_stdout_stderr(run_dir, tracker_stdout, tracker_stderr)

    if not success:
        # Save error result
        result_data = {
            "job_id": TASK_ID,
            "run_id": run_id,
            "schema_id": "package-tracker/v1",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "status": "error",
            "summary": "Package tracker failed",
            "shipments": [],
            "delivery_changes": [],
            "errors": [tracker_stderr],
            "state_file": STATE_FILE,
            "previous_run_id": previous_run_id,
        }
        save_run_result(run_dir, result_data)
        save_prompt_context(
            run_dir,
            {
                "status": "error",
                "summary": "Package tracker failed",
                "error": tracker_stderr,
            },
        )
        # Output compact context for cron agent
        context = {
            "run_id": run_id,
            "short_task_id": short_task_id,
            "callback_data": make_callback_data(short_task_id, run_id),
            "status": "error",
            "summary": "Package tracker script failed",
            "error": tracker_stderr[:500] if tracker_stderr else "Unknown error",
            "previous_run_id": previous_run_id,
        }
        json.dump(context, sys.stdout, indent=2, ensure_ascii=False)
        sys.exit(0)

    # 5. Parse tracker output into structured result
    result_data = parse_tracker_output(tracker_stdout)
    result_data["job_id"] = TASK_ID
    result_data["run_id"] = run_id
    result_data["schema_id"] = "package-tracker/v1"
    result_data["generated_at"] = datetime.now(timezone.utc).isoformat()
    result_data["state_file"] = STATE_FILE
    result_data["previous_run_id"] = previous_run_id

    # Save full result
    save_run_result(run_dir, result_data)

    # Save compact prompt context
    prompt_context = {
        "status": result_data["status"],
        "summary": result_data["summary"],
        "shipments_count": len(result_data["shipments"]),
        "changes_count": len(result_data["delivery_changes"]),
        "errors": result_data["errors"],
        "carriers": list(set(s["carrier"] for s in result_data["shipments"])),
        "package_email_mentions_count": len(result_data.get("package_email_mentions") or []),
        "recent_package_email_mentions": (result_data.get("package_email_mentions") or [])[:5],
    }
    save_prompt_context(run_dir, prompt_context)

    # 6. Determine whether delivery should be skipped (no news = skip).
    # Known shipments are still stored in result.json for callbacks, but should
    # not trigger Telegram noise unless there are changes/errors.
    no_changes = len(result_data["delivery_changes"]) == 0
    skip_delivery = no_changes and prompt_context.get("status") != "error"

    # 7. Output compact JSON to stdout — this is injected into the cron agent prompt
    context = {
        "run_info": {
            "task_id": TASK_ID,
            "run_id": run_id,
            "short_task_id": short_task_id,
            "callback_data": make_callback_data(short_task_id, run_id),
            "run_dir": str(run_dir),
        },
        "prompt_context": prompt_context,
        "result_schema": "package-tracker/v1",
        "skip_delivery": skip_delivery,
        "agent_instructions": {
            "continue_marker": f"[AR_CONTINUE:{make_callback_data(short_task_id, run_id)}]",
            "note": "End your response with the continue_marker exactly as provided to enable 'Continue dialog' button.",
        },
    }
    json.dump(context, sys.stdout, indent=2, ensure_ascii=False)


if __name__ == "__main__":
    main()
