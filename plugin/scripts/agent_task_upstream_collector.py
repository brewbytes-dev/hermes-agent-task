#!/usr/bin/env python3
"""
Collector script for upstream-hermes-check agent task.

Checks for new commits in upstream (NousResearch/hermes-agent) origin/main
that are not yet in our current local branch. Reports only new updates
since the last check.

Usage:
    python3 agent_task_upstream_collector.py
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

HERMES_HOME = Path(os.path.expanduser(os.environ.get("HERMES_HOME", "~/.hermes")))
SCRIPTS_DIR = str(HERMES_HOME / "scripts")
sys.path.insert(0, SCRIPTS_DIR)

from agent_task_runner import (  # noqa: E402
    create_run,
    save_run_result,
    save_prompt_context,
    make_callback_data,
    read_task_definition,
)

TASK_ID = "upstream-hermes-check"
REPO_DIR = str(HERMES_HOME / "hermes-agent")
STATE_FILE = str(HERMES_HOME / "agent_tasks" / TASK_ID / "state.json")
UPSTREAM_BRANCH = "origin/main"
MAX_COMMITS_LIST = 15


def _base_for_range(since_commit: str | None) -> str:
    if not since_commit:
        out, ec = git_cmd(["merge-base", UPSTREAM_BRANCH, "HEAD"])
        return out if ec == 0 and out else "HEAD"

    out, ec = git_cmd(["cat-file", "-t", since_commit])
    if ec == 0 and out:
        return since_commit
    return "HEAD"


def git_cmd(args: list[str]) -> tuple[str, int]:
    """Run a git command in the repo dir. Returns (stdout, exit_code)."""
    try:
        result = subprocess.run(
            ["git"] + args,
            capture_output=True,
            text=True,
            timeout=30,
            cwd=REPO_DIR,
        )
        return (result.stdout or "").strip(), result.returncode
    except subprocess.TimeoutExpired:
        return "(timeout)", 1
    except FileNotFoundError:
        return "git not found", 1
    except Exception as e:
        return str(e), 1


def load_state() -> dict:
    """Load last-check state from state.json."""
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {"last_checked_commit": None, "last_notified_commit": None}


def save_state(state: dict) -> None:
    """Save state to state.json."""
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    os.replace(tmp, STATE_FILE)


def get_current_branch() -> str:
    """Get the current branch name."""
    out, _ = git_cmd(["rev-parse", "--abbrev-ref", "HEAD"])
    return out or "unknown"


def get_current_head() -> str:
    """Get the current HEAD commit hash."""
    out, _ = git_cmd(["rev-parse", "HEAD"])
    return out or ""


def fetch_upstream() -> bool:
    """Fetch origin/main without merging. Returns True on success."""
    git_cmd(["fetch", "origin", "main", "--quiet"])
    out, ec = git_cmd(["rev-parse", "--verify", UPSTREAM_BRANCH])
    return ec == 0 and bool(out)


def get_new_commits(since_commit: str | None) -> list[dict]:
    """Get a small sample of new upstream commits for internal context."""
    base = _base_for_range(since_commit)
    fmt = "--pretty=format:%H||%ai||%an||%s"
    out, ec = git_cmd(["log", f"{base}..{UPSTREAM_BRANCH}", fmt, f"--max-count={MAX_COMMITS_LIST}"])

    if ec != 0 or not out:
        return []

    commits = []
    for line in out.split("\n"):
        parts = line.split("||", 3)
        if len(parts) == 4:
            commits.append(
                {
                    "hash": parts[0][:12],
                    "hash_full": parts[0],
                    "date": parts[1],
                    "author": parts[2],
                    "subject": parts[3],
                }
            )
    return commits


def get_commit_count(since_commit: str | None) -> int:
    """Count total new commits (without limit)."""
    base = _base_for_range(since_commit)
    out, ec = git_cmd(["rev-list", "--count", f"{base}..{UPSTREAM_BRANCH}"])
    if ec == 0 and out:
        try:
            return int(out)
        except ValueError:
            pass
    return 0


def get_commit_stats(since_commit: str | None) -> dict:
    """Return aggregate stats for the new upstream range, without dumping every commit."""
    base = _base_for_range(since_commit)
    total = get_commit_count(since_commit)
    if total <= 0:
        return {"total": 0}

    author_lines, _ = git_cmd(["shortlog", "-sne", f"{base}..{UPSTREAM_BRANCH}"])
    top_authors = []
    for line in author_lines.splitlines()[:5]:
        line = line.strip()
        if not line:
            continue
        count, _, rest = line.partition("\t")
        name = rest.split("<", 1)[0].strip() or rest.strip()
        try:
            n = int(count.strip())
        except ValueError:
            n = 0
        top_authors.append({"name": name, "commits": n})

    first_date, _ = git_cmd(
        ["log", "--reverse", "--date=short", "--pretty=format:%ad", f"{base}..{UPSTREAM_BRANCH}", "--max-count=1"]
    )
    last_date, _ = git_cmd(
        ["log", "--date=short", "--pretty=format:%ad", f"{base}..{UPSTREAM_BRANCH}", "--max-count=1"]
    )
    files_changed, _ = git_cmd(["diff", "--name-only", f"{base}..{UPSTREAM_BRANCH}"])
    diffstat, _ = git_cmd(["diff", "--shortstat", f"{base}..{UPSTREAM_BRANCH}"])
    merge_count, _ = git_cmd(["rev-list", "--count", "--merges", f"{base}..{UPSTREAM_BRANCH}"])
    non_merge_count, _ = git_cmd(["rev-list", "--count", "--no-merges", f"{base}..{UPSTREAM_BRANCH}"])

    extensions = {}
    top_dirs = {}
    changed_files = [line for line in files_changed.splitlines() if line.strip()]
    for file_name in changed_files:
        parts = file_name.split("/")
        top = parts[0] if len(parts) > 1 else "."
        top_dirs[top] = top_dirs.get(top, 0) + 1
        ext = os.path.splitext(file_name)[1].lower() or "(no ext)"
        extensions[ext] = extensions.get(ext, 0) + 1

    def top_items(d: dict, limit: int = 5) -> list[dict]:
        return [{"name": k, "files": v} for k, v in sorted(d.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]]

    return {
        "total": total,
        "date_range": {"from": first_date, "to": last_date},
        "authors_count": len([line for line in author_lines.splitlines() if line.strip()]),
        "top_authors": top_authors,
        "changed_files_count": len(changed_files),
        "diffstat": diffstat,
        "merge_commits": int(merge_count or 0),
        "non_merge_commits": int(non_merge_count or 0),
        "top_dirs": top_items(top_dirs),
        "top_extensions": top_items(extensions),
    }


def main():
    # 1. Get task definition
    task = read_task_definition(TASK_ID)
    if not task:
        result = {
            "task_id": TASK_ID,
            "error": f"Task {TASK_ID!r} not found",
            "status": "error",
        }
        json.dump(result, sys.stdout, indent=2, ensure_ascii=False)
        sys.exit(1)

    short_task_id = task["short_task_id"]

    # 2. Create a new run
    run_info = create_run(TASK_ID)
    run_dir = run_info["run_dir"]
    run_id = run_info["run_id"]

    # 3. Check repo exists
    if not os.path.exists(REPO_DIR):
        result_data = {
            "job_id": TASK_ID,
            "run_id": run_id,
            "schema_id": f"{TASK_ID}/v1",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "status": "error",
            "summary": "Hermes repo not found",
            "new_commits": [],
            "total_new": 0,
            "current_branch": "",
            "errors": [f"Repo not found: {REPO_DIR}"],
        }
        save_run_result(run_dir, result_data)
        save_prompt_context(run_dir, {"status": "error", "error": "Repo not found"})
        context = {
            "run_info": {
                "task_id": TASK_ID,
                "run_id": run_id,
                "short_task_id": short_task_id,
                "callback_data": make_callback_data(short_task_id, run_id),
            },
            "prompt_context": {"status": "error", "error": "Repo not found"},
            "result_schema": f"{TASK_ID}/v1",
            "skip_delivery": False,  # always report errors
            "agent_instructions": {
                "continue_marker": f"[AR_CONTINUE:{make_callback_data(short_task_id, run_id)}]",
                "note": "End response with continue_marker exactly as provided.",
            },
        }
        json.dump(context, sys.stdout, indent=2, ensure_ascii=False)
        sys.exit(0)

    # 4. Load state and fetch upstream
    state = load_state()
    current_branch = get_current_branch()
    current_head = get_current_head()

    fetch_success = fetch_upstream()
    if not fetch_success:
        result_data = {
            "job_id": TASK_ID,
            "run_id": run_id,
            "schema_id": f"{TASK_ID}/v1",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "status": "error",
            "summary": "Failed to fetch upstream",
            "new_commits": [],
            "total_new": 0,
            "current_branch": current_branch,
            "current_head": current_head,
            "errors": ["Could not fetch origin/main"],
        }
        save_run_result(run_dir, result_data)
        save_prompt_context(run_dir, {"status": "error", "error": "Fetch failed"})
        context = {
            "run_info": {
                "task_id": TASK_ID,
                "run_id": run_id,
                "short_task_id": short_task_id,
                "callback_data": make_callback_data(short_task_id, run_id),
            },
            "prompt_context": {"status": "error", "error": "Fetch failed"},
            "result_schema": f"{TASK_ID}/v1",
            "skip_delivery": False,
            "agent_instructions": {
                "continue_marker": f"[AR_CONTINUE:{make_callback_data(short_task_id, run_id)}]",
                "note": "End response with continue_marker exactly as provided.",
            },
        }
        json.dump(context, sys.stdout, indent=2, ensure_ascii=False)
        sys.exit(0)

    # 5. Get new commits since last check
    last_check = state.get("last_checked_commit")
    commits_list = get_new_commits(last_check)
    total_new = get_commit_count(last_check)
    commit_stats = get_commit_stats(last_check)

    has_updates = total_new > 0

    if has_updates:
        latest_commit = commits_list[0]["hash_full"]
    else:
        latest_commit = current_head

    # Check if we already notified about the latest new commit
    already_notified = state.get("last_notified_commit") == latest_commit

    # 6. Save state with current upstream HEAD
    state["last_checked_commit"] = latest_commit
    if has_updates and not already_notified:
        state["last_notified_commit"] = latest_commit
    save_state(state)

    # 7. Build result
    result_data = {
        "job_id": TASK_ID,
        "run_id": run_id,
        "schema_id": f"{TASK_ID}/v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "ok" if not has_updates else "updates_available",
        "summary": f"{total_new} new commit(s) in upstream/main" if has_updates else "Up-to-date with upstream",
        "new_commits": commits_list[:3],  # sample only; notifications use aggregate stats
        "total_new": total_new,
        "stats": commit_stats,
        "current_branch": current_branch,
        "current_head": current_head[:12],
        "last_checked_commit": latest_commit[:12],
        "already_notified": already_notified,
        "errors": [],
    }
    save_run_result(run_dir, result_data)

    # 8. Build prompt context
    if has_updates and not already_notified:
        top_authors = (
            ", ".join(f"{a['name']} ({a['commits']})" for a in commit_stats.get("top_authors", [])[:3]) or "n/a"
        )
        top_dirs = ", ".join(f"{d['name']} ({d['files']} files)" for d in commit_stats.get("top_dirs", [])[:5]) or "n/a"
        date_range = commit_stats.get("date_range", {})
        summary = (
            f"📬 Upstream обновление: {total_new} новых коммит(а/ов)\n"
            f"• Диапазон: {date_range.get('from') or '?'} → {date_range.get('to') or '?'}\n"
            f"• Авторов: {commit_stats.get('authors_count', 0)}; топ: {top_authors}\n"
            f"• Файлов изменено: {commit_stats.get('changed_files_count', 0)}; {commit_stats.get('diffstat') or 'diffstat n/a'}\n"
            f"• Merge/non-merge: {commit_stats.get('merge_commits', 0)}/{commit_stats.get('non_merge_commits', 0)}\n"
            f"• Основные зоны: {top_dirs}\n"
        )
        prompt_context = {
            "status": "updates_available",
            "summary": summary,
            "new_commits_count": total_new,
            "stats": commit_stats,
            "sample_recent_commits": commits_list[:3],
            "branch": current_branch,
        }
    elif has_updates and already_notified:
        prompt_context = {
            "status": "already_notified",
            "summary": f"Всё те же {total_new} новых коммит(а/ов) — уже уведомляли. Новых изменений нет.",
            "new_commits_count": total_new,
            "branch": current_branch,
        }
    else:
        prompt_context = {
            "status": "up_to_date",
            "summary": "Новых обновлений в upstream/main нет. Всё актуально.",
            "new_commits_count": 0,
            "branch": current_branch,
        }
    save_prompt_context(run_dir, prompt_context)

    # 9. Skip delivery if no actual new updates to report
    skip_delivery = not has_updates or already_notified

    # 10. Output JSON for cron agent
    context = {
        "run_info": {
            "task_id": TASK_ID,
            "run_id": run_id,
            "short_task_id": short_task_id,
            "callback_data": make_callback_data(short_task_id, run_id),
            "run_dir": str(run_dir),
        },
        "prompt_context": prompt_context,
        "result_schema": f"{TASK_ID}/v1",
        "skip_delivery": skip_delivery,
        "agent_instructions": {
            "continue_marker": f"[AR_CONTINUE:{make_callback_data(short_task_id, run_id)}]",
            "note": "End your response with the continue_marker exactly as provided to enable 'Continue dialog' button.",
        },
    }
    json.dump(context, sys.stdout, indent=2, ensure_ascii=False)


if __name__ == "__main__":
    main()
