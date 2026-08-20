#!/usr/bin/env python3
"""Weekly Hermes sessions review collector.

Exports Hermes session history, extracts all not-yet-reviewed session topics
since the previous successful review cursor, writes a Markdown weekly review
into the Obsidian vault, and stores a structured agent_task result for Telegram
delivery + continue-dialog callbacks.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore

HERMES_HOME = Path(os.path.expanduser(os.environ.get("HERMES_HOME", "~/.hermes")))
SCRIPTS_DIR = HERMES_HOME / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from agent_task_runner import (  # noqa: E402
    create_run,
    make_callback_data,
    save_prompt_context,
    save_run_result,
)

TASK_ID = "weekly-session-review"
SCHEMA_ID = "weekly-session-review/v1"
TASK_DIR = HERMES_HOME / "agent_tasks" / TASK_ID
STATE_PATH = TASK_DIR / "state.json"
LOCAL_TZ_NAME = os.environ.get("HERMES_DIARY_TZ", "America/Los_Angeles")
VAULT_DIR = Path(os.path.expanduser(os.environ.get("HERMES_DIARY_VAULT", "/home/hermes/vault")))
WEEKLY_DIR = VAULT_DIR / "Diary" / "Weekly"
KNOWLEDGE_DIR = VAULT_DIR / "Knowledge"
KNOWLEDGE_INBOX_DIR = KNOWLEDGE_DIR / "Inbox"
KNOWLEDGE_INDEX_PATH = KNOWLEDGE_DIR / "README.md"

MAX_TOPIC_CHARS = 220
MAX_TOPICS_PER_SESSION = 8
MAX_REVIEW_ITEMS_PER_SESSION = 12
MAX_REVIEW_ITEMS_FOR_PROMPT = 60
MAX_PROMOTION_CANDIDATES = 30
MAX_PROMOTION_CANDIDATES_FOR_PROMPT = 12
MAX_PROMOTION_EXCERPT_CHARS = 180
MAX_PROMOTION_CONTEXT_CHARS = 160
MAX_PROMPT_ASSISTANT_CONTEXT_CHARS = 180
MAX_ASSISTANT_CONTEXT_CHARS = 360

AGREEMENT_NEEDLES = [
    "да",
    "давай",
    "ок",
    "окей",
    "ага",
    "согласен",
    "go",
    "yes",
    "yep",
    "sounds good",
]

ACTION_NEEDLES = [
    "сделай",
    "давай",
    "создай",
    "настрой",
    "запусти",
    "проверь",
    "почини",
    "добавь",
    "прикрути",
    "поставь",
    "обнови",
    "переведи",
]

ASSISTANT_DONE_NEEDLES = [
    "готов",
    "сделал",
    "добавил",
    "создал",
    "настроил",
    "обновил",
    "изменил",
    "исправил",
    "запустил",
    "проверил",
    "нашёл",
    "нашел",
    "восстановил",
]

ASSISTANT_BLOCKED_NEEDLES = [
    "не смог",
    "не могу",
    "нужно вручную",
    "нужен ручной",
    "нужна авторизация",
    "2fa",
    "captcha",
    "blocked",
    "ошибка",
]

CATEGORY_RULES: list[tuple[str, list[str]]] = [
    ("idea", ["идея", "idea", "придумал", "можно сделать", "could", "maybe", "proposal"]),
    ("plan", ["план", "plan", "roadmap", "implementation", "архитектура", "design"]),
    ("task", ["сделай", "давай", "создай", "настрой", "запусти", "проверь", "почини", "добавь", "task", "todo"]),
    ("question", ["вопрос", "?", "как ", "почему", "что если", "question"]),
    ("bugfix", ["bug", "баг", "ошибка", "traceback", "exception", "сломалось", "fix", "debug"]),
    (
        "hermes",
        ["hermes", "gateway", "agent-task", "agent task", "skill", "honcho", "memory", "сесс", "telegram callback"],
    ),
    ("automation", ["автомат", "automation", "cron", "schedule", "scheduler", "webhook", "monitor", "tracker"]),
    ("email", ["gmail", "email", "почт", "himalaya", "mail"]),
    ("packages", ["package", "parcel", "ups", "fedex", "usps", "посыл", "tracking", "delivery"]),
    ("github", ["github", "pr", "pull request", "commit", "rebase", "branch", "upstream", "diff"]),
    ("research", ["research", "исслед", "найди", "обзор", "compare", "сравни"]),
]

TRANSIENT_HINTS = ["bugfix", "debug"]
PROMOTE_HINTS = ["idea", "plan", "hermes", "automation", "packages", "research"]
PROMOTION_STATUSES = ["needs_review", "idea_backlog", "planning", "blocked_or_needs_user"]
KNOWLEDGE_BUCKET_BY_CATEGORY = {
    "idea": "Ideas",
    "plan": "Projects",
    "task": "Projects",
    "question": "HowTo",
    "bugfix": "Troubleshooting",
    "hermes": "Systems",
    "automation": "Systems",
    "email": "Systems",
    "packages": "Systems",
    "github": "Systems",
    "research": "Research",
    "misc": "Inbox",
}


def local_tz():
    if ZoneInfo:
        try:
            return ZoneInfo(LOCAL_TZ_NAME)
        except Exception:
            pass
    return timezone.utc


def as_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            dt = datetime.fromisoformat(text)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except Exception:
            try:
                return datetime.fromtimestamp(float(text), tz=timezone.utc)
            except Exception:
                return None
    return None


def read_state() -> dict[str, Any]:
    try:
        if STATE_PATH.exists():
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def write_state(state: dict[str, Any]) -> None:
    TASK_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    tmp.replace(STATE_PATH)


def clean_text(text: Any, limit: int = MAX_TOPIC_CHARS) -> str:
    if text is None:
        return ""
    s = str(text)
    # Strip injected memory/system context blocks from user-visible excerpts.
    s = re.sub(r"<memory-context>.*?</memory-context>", " ", s, flags=re.DOTALL | re.IGNORECASE)
    s = re.sub(r"\[CONTEXT COMPACTION.*?--- END OF CONTEXT SUMMARY", " ", s, flags=re.DOTALL | re.IGNORECASE)
    # Redact secrets/credentials before anything is written to Diary/Knowledge artifacts.
    s = re.sub(
        r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\s+[a-z]{4}\s+[a-z]{4}\s+[a-z]{4}\s+[a-z]{4}\b",
        "[REDACTED_CREDENTIAL]",
        s,
    )
    s = re.sub(r"ghp_[A-Za-z0-9_\.]+", "[REDACTED_GITHUB_TOKEN]", s)
    s = re.sub(r"\b_[A-Za-z0-9]{40,}\b", "[REDACTED_API_KEY]", s)
    s = re.sub(r"(?i)(\s-p\s+)(['\"])[^'\"]+\2", r"\1[REDACTED_PASSWORD]", s)
    s = re.sub(r"(?i)\b(password|passwd|api[_-]?key|token|secret|credential)\s*[:=]\s*\S+", r"\1=[REDACTED]", s)
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) > limit:
        s = s[: limit - 1].rstrip() + "…"
    return s


def knowledge_bucket_for(item: dict[str, Any]) -> str:
    categories = item.get("categories") or []
    primary = item.get("primary_category") or (categories[0] if categories else "misc")
    status = item.get("status") or "review"
    if status in {"idea_backlog"}:
        return "Ideas"
    if status in {"planning", "needs_review", "blocked_or_needs_user"}:
        return "Projects"
    if status in {"debugging"}:
        return "Troubleshooting"
    return KNOWLEDGE_BUCKET_BY_CATEGORY.get(primary, "Inbox")


def should_consider_for_promotion(item: dict[str, Any]) -> bool:
    categories = set(item.get("categories") or [])
    status = item.get("status") or "review"
    if status in PROMOTION_STATUSES:
        return True
    if categories.intersection(PROMOTE_HINTS):
        return True
    return False


def build_promotion_candidates(review_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in review_items:
        if not should_consider_for_promotion(item):
            continue
        user_excerpt = clean_text(item.get("user_excerpt"), MAX_PROMOTION_EXCERPT_CHARS)
        if not user_excerpt:
            continue
        key = f"{item.get('session_id')}:{item.get('primary_category')}:{user_excerpt[:120].lower()}"
        if key in seen:
            continue
        seen.add(key)
        candidates.append(
            {
                "session_id": item.get("session_id"),
                "title": clean_text(item.get("session_title"), 120),
                "timestamp": item.get("timestamp"),
                "category": item.get("primary_category", "misc"),
                "categories": item.get("categories", []),
                "status": item.get("status", "review"),
                "timeline_bucket": item.get("timeline_bucket", "unknown_time"),
                "suggested_bucket": knowledge_bucket_for(item),
                "user_excerpt": user_excerpt,
                "assistant_context": clean_text(
                    item.get("assistant_after") or item.get("assistant_before") or "",
                    MAX_PROMOTION_CONTEXT_CHARS,
                ),
            }
        )
        if len(candidates) >= MAX_PROMOTION_CANDIDATES:
            break
    return candidates


def ensure_knowledge_scaffold() -> None:
    for subdir in [
        "Inbox",
        "Ideas",
        "Projects",
        "Decisions",
        "HowTo",
        "Systems",
        "Research",
        "Troubleshooting",
        "Archive",
    ]:
        (KNOWLEDGE_DIR / subdir).mkdir(parents=True, exist_ok=True)
    if not KNOWLEDGE_INDEX_PATH.exists():
        KNOWLEDGE_INDEX_PATH.write_text(
            "# Knowledge\n\n"
            "Budget-aware knowledge base derived from Hermes conversations.\n\n"
            "## Workflow\n\n"
            "1. Raw Hermes sessions remain the source of truth.\n"
            "2. Weekly reviews write compact reports under `Diary/Weekly/`.\n"
            "3. Candidate ideas/tasks/decisions land in `Knowledge/Inbox/` first.\n"
            "4. Only reviewed items should be promoted into `Ideas/`, `Projects/`, `Decisions/`, `HowTo/`, `Systems/`, `Research/`, or `Troubleshooting/`.\n"
            "5. Persistent Hermes memory is reserved for stable preferences/facts, not every idea.\n\n"
            "## Buckets\n\n"
            "- [[Ideas]] — possible future ideas.\n"
            "- [[Projects]] — active/planned work.\n"
            "- [[Decisions]] — durable choices and rationale.\n"
            "- [[HowTo]] — reusable procedures.\n"
            "- [[Systems]] — architecture/infrastructure notes.\n"
            "- [[Research]] — investigated topics and sources.\n"
            "- [[Troubleshooting]] — bugs, fixes, diagnostics.\n"
            "- [[Inbox]] — unreviewed promotion candidates.\n",
            encoding="utf-8",
        )


def message_text(message: dict[str, Any]) -> str:
    content = message.get("content", "")
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or ""))
            else:
                parts.append(str(item))
        return " ".join(parts)
    return str(content)


def is_visible_user_message(message: dict[str, Any]) -> bool:
    if message.get("role") != "user":
        return False
    text = clean_text(message_text(message), 80)
    if not text:
        return False
    # Drop synthetic skill/system injections that sometimes arrive as user-role messages.
    if (
        text.startswith("[SYSTEM:")
        or text.startswith("[System note:")
        or text.startswith("Continue after the Hermes gateway restart.")
    ):
        return False
    # Synthetic callback context is still a topic, but make it readable later.
    return True


def export_sessions() -> list[dict[str, Any]]:
    with tempfile.NamedTemporaryFile(prefix="hermes_sessions_", suffix=".jsonl", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        proc = subprocess.run(
            ["hermes", "sessions", "export", str(tmp_path)],
            cwd=str(HERMES_HOME),
            text=True,
            capture_output=True,
            timeout=100,
        )
        if proc.returncode != 0:
            raise RuntimeError((proc.stderr or proc.stdout or "hermes sessions export failed").strip())
        sessions: list[dict[str, Any]] = []
        for line in tmp_path.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.strip():
                sessions.append(json.loads(line))
        return sessions
    finally:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass


def infer_title(session: dict[str, Any], topics: list[str]) -> str:
    title = clean_text(session.get("title"), 120)
    if title and title != "—":
        return title
    if topics:
        return clean_text(topics[0], 120)
    return session.get("id") or "Untitled session"


def infer_categories(text: str) -> list[str]:
    low = text.lower()
    found: list[str] = []
    for category, needles in CATEGORY_RULES:
        if any(n in low for n in needles):
            found.append(category)
    if not found:
        found.append("misc")
    return found


def infer_primary_category(categories: list[str]) -> str:
    for category in categories:
        if category != "misc":
            return category
    return categories[0] if categories else "misc"


def message_dt(message: dict[str, Any]) -> datetime | None:
    return as_datetime(message.get("timestamp") or message.get("created_at") or message.get("ts"))


def is_assistant_message(message: dict[str, Any]) -> bool:
    return message.get("role") == "assistant" and bool(clean_text(message_text(message), 80))


def assistant_excerpt(message: dict[str, Any], limit: int = MAX_ASSISTANT_CONTEXT_CHARS) -> str:
    text = clean_text(message_text(message), limit)
    # Avoid spending review context on empty tool-call carrier messages.
    if not text or text in {"[]", "{}"}:
        return ""
    return text


def looks_like_agreement(text: str) -> bool:
    low = text.lower().strip(" .!?,—–-\n\t")
    if not low or len(low) > 80:
        return False
    return any(low == needle or low.startswith(needle + " ") for needle in AGREEMENT_NEEDLES)


def looks_like_action(text: str) -> bool:
    low = text.lower()
    return any(needle in low for needle in ACTION_NEEDLES)


def find_prev_assistant(messages: list[dict[str, Any]], index: int) -> str:
    for j in range(index - 1, max(-1, index - 7), -1):
        if isinstance(messages[j], dict) and is_assistant_message(messages[j]):
            return assistant_excerpt(messages[j])
    return ""


def find_next_assistant(messages: list[dict[str, Any]], index: int) -> str:
    for j in range(index + 1, min(len(messages), index + 10)):
        if isinstance(messages[j], dict) and is_assistant_message(messages[j]):
            return assistant_excerpt(messages[j])
    return ""


def timeline_bucket(item_dt: datetime | None, end_local: datetime) -> str:
    if item_dt is None:
        return "unknown_time"
    local = item_dt.astimezone(end_local.tzinfo)
    days = (end_local.date() - local.date()).days
    if days <= 0:
        return "today"
    if days <= 2:
        return "last_48h"
    if days <= 7:
        return "this_week"
    return "older_unreviewed"


def infer_review_status(user_text: str, categories: list[str], assistant_after: str) -> tuple[str, str]:
    low_user = user_text.lower()
    low_after = assistant_after.lower()
    if any(needle in low_after for needle in ASSISTANT_BLOCKED_NEEDLES):
        return "blocked_or_needs_user", "assistant context suggests a blocker or manual step"
    if any(needle in low_after for needle in ASSISTANT_DONE_NEEDLES):
        return "done_or_updated", "assistant context suggests work was completed or updated"
    if looks_like_action(user_text):
        return "needs_review", "user asked for an action; verify whether it should become a task/project"
    if "idea" in categories:
        return "idea_backlog", "user-originated idea candidate"
    if "plan" in categories:
        return "planning", "planning/design discussion"
    if "question" in categories and assistant_after:
        return "answered", "question with nearby assistant answer"
    if "bugfix" in categories:
        return "debugging", "debug/fix discussion"
    if "Продолжение agent-task result:" in user_text:
        return "agent_task_result", "continued from an agent-task notification"
    if "?" in low_user and assistant_after:
        return "answered", "question with nearby assistant answer"
    return "review", "needs lightweight human/LLM review"


def build_review_items(
    session: dict[str, Any], messages: list[dict[str, Any]], end_local: datetime
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for idx, msg in enumerate(messages):
        if not isinstance(msg, dict) or not is_visible_user_message(msg):
            continue
        user_text = clean_text(message_text(msg), MAX_TOPIC_CHARS)
        if not user_text:
            continue
        user_text = user_text.replace(
            "User wants to continue dialog about agent task result.", "Продолжение agent-task result:"
        )
        before = find_prev_assistant(messages, idx) if looks_like_agreement(user_text) else ""
        after = find_next_assistant(messages, idx)
        joined = "\n".join([str(session.get("title") or ""), user_text, before, after])
        categories = infer_categories(joined)
        status, status_reason = infer_review_status(user_text, categories, after)
        dt = message_dt(msg)
        local_dt = dt.astimezone(end_local.tzinfo) if dt else None
        items.append(
            {
                "user_excerpt": user_text,
                "assistant_before": before,
                "assistant_after": after,
                "primary_category": infer_primary_category(categories),
                "categories": categories,
                "status": status,
                "status_reason": status_reason,
                "timeline_bucket": timeline_bucket(dt, end_local),
                "timestamp": local_dt.isoformat(timespec="seconds") if local_dt else "",
                "timestamp_utc": dt.astimezone(timezone.utc).isoformat(timespec="seconds") if dt else "",
            }
        )
        if len(items) >= MAX_REVIEW_ITEMS_PER_SESSION:
            break
    return items


def summarize_session(session: dict[str, Any], start_utc: datetime, end_utc: datetime) -> dict[str, Any] | None:
    last_active = as_datetime(session.get("last_active") or session.get("ended_at") or session.get("started_at"))
    # High-watermark semantics: previous cursor is exclusive, this run's
    # watermark is inclusive. This prevents duplicate weekly items while
    # preserving sessions that update after the prior review.
    if not last_active or not (start_utc < last_active <= end_utc):
        return None

    tz = local_tz()
    end_local = end_utc.astimezone(tz)
    raw_messages = session.get("messages") or []
    messages = [m for m in raw_messages if isinstance(m, dict)]
    user_messages = [m for m in messages if is_visible_user_message(m)]

    topics: list[str] = []
    for msg in user_messages:
        text = clean_text(message_text(msg), MAX_TOPIC_CHARS)
        if text:
            # Make callback-generated prompts readable.
            text = text.replace(
                "User wants to continue dialog about agent task result.", "Продолжение agent-task result:"
            )
            if text not in topics:
                topics.append(text)
        if len(topics) >= MAX_TOPICS_PER_SESSION:
            break

    review_items = build_review_items(session, messages, end_local)
    if not topics and not review_items:
        return None
    item_categories = [item["primary_category"] for item in review_items]
    joined = "\n".join([str(session.get("title") or "")] + topics + item_categories)
    categories = infer_categories(joined)
    title = infer_title(session, topics)

    first_user_dt = next((message_dt(m) for m in user_messages if message_dt(m)), None)
    last_user_dt = next((message_dt(m) for m in reversed(user_messages) if message_dt(m)), None)
    first_user_local = first_user_dt.astimezone(tz) if first_user_dt else None
    last_user_local = last_user_dt.astimezone(tz) if last_user_dt else None

    if first_user_local and last_user_local and first_user_local.date() == last_user_local.date():
        timeline = f"{first_user_local.strftime('%Y-%m-%d %H:%M')}–{last_user_local.strftime('%H:%M %Z')}"
    elif first_user_local and last_user_local:
        timeline = f"{first_user_local.strftime('%Y-%m-%d %H:%M %Z')} → {last_user_local.strftime('%Y-%m-%d %H:%M %Z')}"
    else:
        timeline = last_active.astimezone(tz).strftime("%Y-%m-%d %H:%M %Z")

    return {
        "session_id": session.get("id"),
        "title": title,
        "source": session.get("source") or "unknown",
        "timeline": timeline,
        "timeline_bucket": timeline_bucket(last_user_dt or last_active, end_local),
        "first_user_at": first_user_local.isoformat(timespec="seconds") if first_user_local else "",
        "last_user_at": last_user_local.isoformat(timespec="seconds") if last_user_local else "",
        "last_active": last_active.astimezone(tz).isoformat(timespec="seconds"),
        "last_active_utc": last_active.astimezone(timezone.utc).isoformat(timespec="seconds"),
        "message_count": int(session.get("message_count") or len(session.get("messages") or [])),
        "tool_call_count": int(session.get("tool_call_count") or 0),
        "primary_category": infer_primary_category(categories),
        "categories": categories,
        "topics": topics,
        "review_items": review_items,
    }


def md_escape(text: str) -> str:
    return text.replace("\n", " ").strip()


def render_markdown(
    entries: list[dict[str, Any]],
    start_local: datetime,
    end_local: datetime,
    category_counts: Counter,
    source_counts: Counter,
    markdown_path: Path,
) -> str:
    iso_year, iso_week, _ = end_local.isocalendar()
    total_messages = sum(e.get("message_count", 0) for e in entries)
    review_items = [item for entry in entries for item in entry.get("review_items", [])]
    status_counts = Counter(item.get("status", "review") for item in review_items)
    timeline_counts = Counter(item.get("timeline_bucket", "unknown_time") for item in review_items)
    lines: list[str] = []
    lines.append(f"# Weekly Hermes Sessions Review — {iso_year}-W{iso_week:02d}")
    lines.append("")
    lines.append(f"Generated: {end_local.isoformat(timespec='seconds')}")
    lines.append(
        f"Reviewed window: ({start_local.isoformat(timespec='seconds')}, {end_local.isoformat(timespec='seconds')}]"
    )
    lines.append("Mode: all sessions not reviewed since the previous cursor")
    lines.append(f"Report file: `{markdown_path}`")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append(f"- Sessions: {len(entries)}")
    lines.append(f"- Review items: {len(review_items)}")
    lines.append(f"- Messages: {total_messages}")
    lines.append("- Sources: " + (", ".join(f"{k}: {v}" for k, v in source_counts.most_common()) or "none"))
    lines.append("- Categories: " + (", ".join(f"{k}: {v}" for k, v in category_counts.most_common()) or "none"))
    lines.append("- Statuses: " + (", ".join(f"{k}: {v}" for k, v in status_counts.most_common()) or "none"))
    lines.append("- Timeline: " + (", ".join(f"{k}: {v}" for k, v in timeline_counts.most_common()) or "none"))
    lines.append("")
    lines.append("## Smart review queue")
    lines.append("")
    lines.append(
        "Primary signal is user messages. Assistant snippets are bounded and included only as nearby context for agreement/outcome/status, not as raw transcript dumps."
    )
    lines.append("")
    if review_items:
        for entry in entries:
            for item in entry.get("review_items", [])[:MAX_REVIEW_ITEMS_PER_SESSION]:
                lines.append(
                    f"- {item.get('timestamp') or entry['last_active']} — {item.get('primary_category', 'misc')} — {item.get('status', 'review')} — `{entry['session_id']}`"
                )
                lines.append(f"  - User: {md_escape(item.get('user_excerpt', ''))}")
                if item.get("assistant_before"):
                    lines.append(f"  - Assistant before: {md_escape(item['assistant_before'])}")
                if item.get("assistant_after"):
                    lines.append(f"  - Assistant after: {md_escape(item['assistant_after'])}")
    else:
        lines.append("- None detected.")
    lines.append("")

    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for entry in entries:
        for cat in entry["categories"]:
            buckets[cat].append(entry)

    lines.append("## Category buckets")
    lines.append("")
    for cat, cat_entries in sorted(buckets.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        lines.append(f"### {cat} ({len(cat_entries)})")
        for entry in cat_entries[:12]:
            lines.append(f"- {entry['title']} — `{entry['session_id']}`")
        if len(cat_entries) > 12:
            lines.append(f"- … +{len(cat_entries) - 12} more")
        lines.append("")

    promote = [e for e in entries if any(c in PROMOTE_HINTS for c in e["categories"])]
    transient = [e for e in entries if any(c in TRANSIENT_HINTS for c in e["categories"])]

    lines.append("## Candidates to keep/promote")
    lines.append("")
    if promote:
        for entry in promote[:30]:
            cats = ", ".join(entry["categories"])
            lines.append(f"- {entry['title']} — {cats} — `{entry['session_id']}`")
    else:
        lines.append("- None detected.")
    lines.append("")

    lines.append("## Likely transient / no long-term category needed")
    lines.append("")
    if transient:
        for entry in transient[:30]:
            cats = ", ".join(entry["categories"])
            lines.append(f"- {entry['title']} — {cats} — `{entry['session_id']}`")
    else:
        lines.append("- None detected.")
    lines.append("")

    lines.append("## All sessions and topics")
    lines.append("")
    for entry in entries:
        cats = ", ".join(entry["categories"])
        lines.append(f"### {entry['last_active']} — {entry['title']}")
        lines.append(f"- Session: `{entry['session_id']}`")
        lines.append(f"- Source: {entry['source']}")
        lines.append(
            f"- Timeline: {entry.get('timeline', entry['last_active'])}; bucket: {entry.get('timeline_bucket', 'unknown_time')}"
        )
        lines.append(f"- Messages: {entry['message_count']}; tool calls: {entry['tool_call_count']}")
        lines.append(f"- Primary category: {entry.get('primary_category', 'misc')}")
        lines.append(f"- Categories: {cats}")
        if entry["topics"]:
            lines.append("- Topics:")
            for topic in entry["topics"]:
                lines.append(f"  - {md_escape(topic)}")
        else:
            lines.append("- Topics: no user text extracted")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def render_knowledge_inbox(
    promotion_candidates: list[dict[str, Any]],
    end_local: datetime,
    run_id: str,
    markdown_path: Path,
    inbox_path: Path,
) -> str:
    iso_year, iso_week, _ = end_local.isocalendar()
    lines: list[str] = []
    lines.append(f"# Knowledge Inbox — {iso_year}-W{iso_week:02d} — {run_id}")
    lines.append("")
    lines.append(f"Generated: {end_local.isoformat(timespec='seconds')}")
    lines.append(f"Weekly review: `{markdown_path}`")
    lines.append(f"Inbox file: `{inbox_path}`")
    lines.append("")
    lines.append(
        "This is a budget-aware promotion queue, not durable memory. Promote only reviewed items into stable Knowledge buckets."
    )
    lines.append("")
    lines.append("## Budget policy")
    lines.append("")
    lines.append("- Source of truth: raw Hermes sessions.")
    lines.append("- LLM-facing layer: compact user-first review items, not full transcripts.")
    lines.append("- Promotion rule: keep only ideas/tasks/decisions/how-tos/systems knowledge worth revisiting.")
    lines.append("- Persistent Hermes memory: stable preferences/facts only, not this inbox.")
    lines.append("")
    lines.append("## Promotion candidates")
    lines.append("")
    if not promotion_candidates:
        lines.append("- None detected.")
    for idx, cand in enumerate(promotion_candidates, start=1):
        cats = ", ".join(cand.get("categories") or [cand.get("category", "misc")])
        lines.append(
            f"### {idx}. {cand.get('suggested_bucket', 'Inbox')} — {cand.get('status', 'review')} — {cand.get('category', 'misc')}"
        )
        lines.append(
            f"- Timestamp: {cand.get('timestamp') or 'unknown'}; timeline: {cand.get('timeline_bucket', 'unknown_time')}"
        )
        lines.append(f"- Session: `{cand.get('session_id')}` — {md_escape(cand.get('title', ''))}")
        lines.append(f"- Categories: {cats}")
        lines.append(f"- User signal: {md_escape(cand.get('user_excerpt', ''))}")
        if cand.get("assistant_context"):
            lines.append(f"- Bounded assistant context: {md_escape(cand['assistant_context'])}")
        lines.append("- Promotion decision: [ ] promote / [ ] archive / [ ] merge")
        lines.append("- Target note: ")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def main() -> None:
    tz = local_tz()
    now_utc = datetime.now(timezone.utc)
    now_local = now_utc.astimezone(tz)
    state = read_state()
    previous_cursor_utc = as_datetime(state.get("last_reviewed_until_utc"))
    if previous_cursor_utc is None:
        # True bootstrap behavior: if the cursor is missing, review everything.
        previous_cursor_utc = datetime(1970, 1, 1, tzinfo=timezone.utc)
    start_utc = previous_cursor_utc
    start_local = start_utc.astimezone(tz)

    run_info = create_run(TASK_ID)
    run_dir = Path(run_info["run_dir"])
    run_id = run_info["run_id"]

    errors: list[str] = []
    status = "ok"

    try:
        sessions = export_sessions()
        entries = [summarize_session(s, start_utc, now_utc) for s in sessions]
        entries = [e for e in entries if e is not None]
        entries.sort(key=lambda e: e["last_active_utc" or ""], reverse=True)

        category_counts: Counter = Counter()
        source_counts: Counter = Counter()
        status_counts: Counter = Counter()
        timeline_counts: Counter = Counter()
        review_items: list[dict[str, Any]] = []
        for entry in entries:
            source_counts[entry["source"]] += 1
            for cat in entry["categories"]:
                category_counts[cat] += 1
            for item in entry.get("review_items", []):
                item_with_session = {
                    "session_id": entry["session_id"],
                    "session_title": entry["title"],
                    **item,
                }
                review_items.append(item_with_session)
                status_counts[item.get("status", "review")] += 1
                timeline_counts[item.get("timeline_bucket", "unknown_time")] += 1

        promotion_candidates = build_promotion_candidates(review_items)

        WEEKLY_DIR.mkdir(parents=True, exist_ok=True)
        ensure_knowledge_scaffold()
        iso_year, iso_week, _ = now_local.isocalendar()
        markdown_path = WEEKLY_DIR / f"{iso_year}-W{iso_week:02d}-{run_id}.md"
        knowledge_inbox_path = KNOWLEDGE_INBOX_DIR / f"{iso_year}-W{iso_week:02d}-{run_id}.md"
        markdown = render_markdown(entries, start_local, now_local, category_counts, source_counts, markdown_path)
        markdown_path.write_text(markdown, encoding="utf-8")
        knowledge_inbox = render_knowledge_inbox(
            promotion_candidates, now_local, run_id, markdown_path, knowledge_inbox_path
        )
        knowledge_inbox_path.write_text(knowledge_inbox, encoding="utf-8")

        total_messages = sum(e.get("message_count", 0) for e in entries)
        top_categories = ", ".join(f"{k}: {v}" for k, v in category_counts.most_common(6)) or "none"
        top_statuses = ", ".join(f"{k}: {v}" for k, v in status_counts.most_common(6)) or "none"
        top_timeline = ", ".join(f"{k}: {v}" for k, v in timeline_counts.most_common(6)) or "none"
        summary = (
            f"Weekly review готов: {len(entries)} непросмотренных сессий, {len(review_items)} review items, "
            f"{len(promotion_candidates)} promotion candidates, "
            f"{total_messages} сообщений за окно ({start_local.isoformat(timespec='seconds')}, {now_local.isoformat(timespec='seconds')}]. "
            f"Категории: {top_categories}. Статусы: {top_statuses}. Timeline: {top_timeline}. "
            f"Markdown: {markdown_path}. Knowledge inbox: {knowledge_inbox_path}"
        )
        if not entries:
            status = "empty"
            summary = (
                f"Weekly review: новых непросмотренных Hermes-сессий нет "
                f"за окно ({start_local.isoformat(timespec='seconds')}, {now_local.isoformat(timespec='seconds')}]. "
                f"Markdown: {markdown_path}. Knowledge inbox: {knowledge_inbox_path}"
            )

        promote_candidates = [
            {"session_id": e["session_id"], "title": e["title"], "categories": e["categories"]}
            for e in entries
            if any(c in PROMOTE_HINTS for c in e["categories"])
        ][:50]
        transient_candidates = [
            {"session_id": e["session_id"], "title": e["title"], "categories": e["categories"]}
            for e in entries
            if any(c in TRANSIENT_HINTS for c in e["categories"])
        ][:50]

        result = {
            "job_id": TASK_ID,
            "run_id": run_id,
            "schema_id": SCHEMA_ID,
            "generated_at": now_utc.isoformat(timespec="seconds"),
            "status": status,
            "summary": summary,
            "period": {
                "start": start_local.isoformat(timespec="seconds"),
                "end": now_local.isoformat(timespec="seconds"),
                "timezone": LOCAL_TZ_NAME,
                "mode": "unreviewed_since_cursor",
                "previous_last_reviewed_until_utc": state.get("last_reviewed_until_utc"),
            },
            "markdown_path": str(markdown_path),
            "knowledge_inbox_path": str(knowledge_inbox_path),
            "meta": {
                "total_exported_sessions": len(sessions),
                "sessions_in_period": len(entries),
                "messages_in_period": total_messages,
                "source_counts": dict(source_counts),
                "review_items_count": len(review_items),
                "promotion_candidates_count": len(promotion_candidates),
                "status_counts": dict(status_counts),
                "timeline_counts": dict(timeline_counts),
            },
            "category_counts": dict(category_counts),
            "status_counts": dict(status_counts),
            "timeline_counts": dict(timeline_counts),
            "review_items": review_items,
            "promotion_candidates": promotion_candidates,
            "promote_candidates": promote_candidates,
            "transient_candidates": transient_candidates,
            "sessions": entries,
            "errors": errors,
        }
        write_state(
            {
                "last_reviewed_until_utc": now_utc.isoformat(timespec="seconds"),
                "last_reviewed_until_local": now_local.isoformat(timespec="seconds"),
                "last_run_id": run_id,
                "last_status": status,
                "last_sessions_count": len(entries),
                "last_messages_count": total_messages,
                "last_markdown_path": str(markdown_path),
                "last_knowledge_inbox_path": str(knowledge_inbox_path),
                "last_promotion_candidates_count": len(promotion_candidates),
                "previous_last_reviewed_until_utc": state.get("last_reviewed_until_utc"),
                "updated_at": now_utc.isoformat(timespec="seconds"),
            }
        )
    except Exception as exc:
        status = "error"
        errors.append(str(exc))
        result = {
            "job_id": TASK_ID,
            "run_id": run_id,
            "schema_id": SCHEMA_ID,
            "generated_at": now_utc.isoformat(timespec="seconds"),
            "status": status,
            "summary": f"Weekly session review failed: {exc}",
            "period": {
                "start": start_local.isoformat(timespec="seconds"),
                "end": now_local.isoformat(timespec="seconds"),
                "timezone": LOCAL_TZ_NAME,
                "mode": "unreviewed_since_cursor",
                "previous_last_reviewed_until_utc": state.get("last_reviewed_until_utc"),
            },
            "markdown_path": "",
            "knowledge_inbox_path": "",
            "meta": {},
            "category_counts": {},
            "status_counts": {},
            "timeline_counts": {},
            "review_items": [],
            "promotion_candidates": [],
            "promote_candidates": [],
            "transient_candidates": [],
            "sessions": [],
            "errors": errors,
        }

    save_run_result(run_dir, result)
    callback_data = make_callback_data(run_info["short_task_id"], run_id)
    compact_review_items = [
        {
            "session_id": item.get("session_id"),
            "title": item.get("session_title"),
            "timestamp": item.get("timestamp"),
            "category": item.get("primary_category"),
            "status": item.get("status"),
            "timeline_bucket": item.get("timeline_bucket"),
            "user": item.get("user_excerpt"),
            "assistant_before": clean_text(item.get("assistant_before"), MAX_PROMPT_ASSISTANT_CONTEXT_CHARS),
            "assistant_after": clean_text(item.get("assistant_after"), MAX_PROMPT_ASSISTANT_CONTEXT_CHARS),
        }
        for item in result.get("review_items", [])[:MAX_REVIEW_ITEMS_FOR_PROMPT]
    ]
    compact_promotion_candidates = [
        {
            "session_id": item.get("session_id"),
            "title": item.get("title"),
            "timestamp": item.get("timestamp"),
            "category": item.get("category"),
            "status": item.get("status"),
            "timeline_bucket": item.get("timeline_bucket"),
            "suggested_bucket": item.get("suggested_bucket"),
            "user": item.get("user_excerpt"),
            "assistant_context": item.get("assistant_context"),
        }
        for item in result.get("promotion_candidates", [])[:MAX_PROMOTION_CANDIDATES_FOR_PROMPT]
    ]
    prompt_context = {
        "status": result["status"],
        "summary": result["summary"],
        "sessions_count": len(result.get("sessions", [])),
        "review_items_count": len(result.get("review_items", [])),
        "messages_count": result.get("meta", {}).get("messages_in_period", 0),
        "changes_count": len(result.get("sessions", [])),
        "markdown_path": result.get("markdown_path", ""),
        "knowledge_inbox_path": result.get("knowledge_inbox_path", ""),
        "category_counts": result.get("category_counts", {}),
        "status_counts": result.get("status_counts", {}),
        "timeline_counts": result.get("timeline_counts", {}),
        "review_guidance": "LLM triage should start from user excerpts. Use assistant_before/assistant_after only as bounded context for agreements, outcomes, and current status; do not load full assistant transcripts unless explicitly needed.",
        "knowledge_guidance": "Budget-aware KB policy: raw sessions remain source of truth; weekly markdown is derived review; Knowledge/Inbox contains promotion candidates only; promote manually/explicitly into stable Knowledge buckets; do not store every idea in Hermes persistent memory.",
        "review_items": compact_review_items,
        "promotion_candidates_count": len(result.get("promotion_candidates", [])),
        "promotion_candidates": compact_promotion_candidates,
        "errors": errors,
    }
    save_prompt_context(run_dir, prompt_context)

    output = {
        "run_info": {
            "task_id": TASK_ID,
            "run_id": run_id,
            "short_task_id": run_info["short_task_id"],
            "callback_data": callback_data,
            "run_dir": str(run_dir),
        },
        "prompt_context": prompt_context,
        "result_schema": SCHEMA_ID,
        "skip_delivery": False,
        "agent_instructions": {
            "continue_marker": f"[AR_CONTINUE:{callback_data}]",
            "note": "End your response with continue_marker exactly to enable the Telegram Continue dialog button.",
        },
    }
    json.dump(output, sys.stdout, indent=2, ensure_ascii=False)

    if status == "error":
        sys.exit(1)


if __name__ == "__main__":
    main()
