#!/usr/bin/env python3
"""Collector for gmail-morning-digest agent task.

Runs the existing gmail_digest_context.py prefetcher, stores a structured
agent_task run, and prepares a compact Russian email digest for Telegram.
"""

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

HERMES_HOME = Path(os.path.expanduser(os.environ.get("HERMES_HOME", "~/.hermes")))
SCRIPTS_DIR = str(HERMES_HOME / "scripts")
sys.path.insert(0, SCRIPTS_DIR)

from agent_task_runner import (  # noqa: E402
    RUNS_DIR,
    create_run,
    make_callback_data,
    read_task_definition,
    save_prompt_context,
    save_run_result,
    save_run_stdout_stderr,
)

TASK_ID = "gmail-morning-digest"
SCHEMA_ID = f"{TASK_ID}/v1"
TASK_DIR = HERMES_HOME / "agent_tasks" / TASK_ID
STATE_PATH = TASK_DIR / "state.json"
PREFETCH_SCRIPT = str(HERMES_HOME / "scripts" / "gmail_digest_context.py")


def _load_state() -> dict:
    if not STATE_PATH.exists():
        return {"schema_id": f"{TASK_ID}/state/v1", "reviewed_message_ids": [], "reviewed_batches": []}
    try:
        with STATE_PATH.open("r", encoding="utf-8") as f:
            state = json.load(f)
        if not isinstance(state, dict):
            raise ValueError("state is not an object")
        state.setdefault("schema_id", f"{TASK_ID}/state/v1")
        state.setdefault("reviewed_message_ids", [])
        state.setdefault("reviewed_batches", [])
        return state
    except Exception:
        # Corrupt state should not break the morning task; start a fresh cursor.
        return {
            "schema_id": f"{TASK_ID}/state/v1",
            "reviewed_message_ids": [],
            "reviewed_batches": [],
            "state_warning": "state file unreadable; started fresh",
        }


def _save_state(state: dict) -> None:
    TASK_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
        f.write("\n")
    tmp.replace(STATE_PATH)


def _update_state_after_run(state: dict, run_id: str, generated_at: str, prefetch: dict, prompt_context: dict) -> dict:
    meta = prefetch.get("_meta") or {}
    batch_ids = [str(v) for v in (meta.get("batch_message_ids") or []) if v is not None]
    existing = [str(v) for v in (state.get("reviewed_message_ids") or []) if v is not None]
    seen = set(existing)
    merged_ids = existing[:]
    for mid in batch_ids:
        if mid not in seen:
            seen.add(mid)
            merged_ids.append(mid)

    batch_record = {
        "run_id": run_id,
        "generated_at": generated_at,
        "since_at": meta.get("since_at"),
        "threshold_at": meta.get("threshold_at"),
        "batch_min_date": meta.get("batch_min_date"),
        "batch_max_date": meta.get("batch_max_date"),
        "batch_count": int(meta.get("batch_count") or 0),
        "unread_count": int(meta.get("unread_count") or 0),
        "important_count": int(prompt_context.get("important_count") or 0),
        "junk_count": int(prompt_context.get("junk_count") or 0),
        "message_ids": batch_ids,
    }

    batches = list(state.get("reviewed_batches") or [])
    batches.append(batch_record)
    state.update(
        {
            "schema_id": f"{TASK_ID}/state/v1",
            "last_overview_at": generated_at,
            "last_successful_run_id": run_id,
            "last_batch": batch_record,
            # Keep enough overlap history for duplicate suppression without unbounded growth.
            "reviewed_message_ids": merged_ids[-2000:],
            "reviewed_batches": batches[-60:],
            "updated_at": generated_at,
        }
    )
    return state


def _run_prefetch(state: dict | None = None) -> tuple[bool, str, str]:
    if not os.path.exists(PREFETCH_SCRIPT):
        return False, "", f"Prefetch script not found: {PREFETCH_SCRIPT}"
    try:
        env = os.environ.copy()
        state = state or {}
        since_at = state.get("last_overview_at")
        if since_at:
            env["GMAIL_DIGEST_SINCE_AT"] = str(since_at)
        reviewed_ids = state.get("reviewed_message_ids") or []
        if reviewed_ids:
            env["GMAIL_DIGEST_SEEN_IDS_JSON"] = json.dumps(reviewed_ids[-2000:], ensure_ascii=False)
        proc = subprocess.run(
            [sys.executable, PREFETCH_SCRIPT],
            cwd=SCRIPTS_DIR,
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
        )
        return proc.returncode == 0, proc.stdout or "", proc.stderr or ""
    except subprocess.TimeoutExpired:
        return False, "", "gmail_digest_context.py timed out (120s)"
    except Exception as exc:  # defensive: collector must return structured error
        return False, "", f"gmail digest prefetch failed: {exc}"


def _load_previous_run_id(short_task_id: str) -> str | None:
    runs_dir = RUNS_DIR / short_task_id
    if not runs_dir.exists():
        return None
    run_dirs = sorted([d for d in runs_dir.iterdir() if d.is_dir()], reverse=True)
    return run_dirs[0].name if run_dirs else None


def _clean_text(value: str | None, limit: int = 180) -> str:
    text = (value or "").replace("\xa0", " ").replace("\u200c", " ")
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"[-=_]{6,}", " ", text)
    text = " ".join(text.replace("\n", " ").split())
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def _sender(msg: dict) -> str:
    return _clean_text(msg.get("from") or msg.get("from_addr") or "неизвестный отправитель", 80)


def _subject(msg: dict) -> str:
    return _clean_text(msg.get("subject") or "(без темы)", 160)


def _body(msg: dict, limit: int = 180) -> str:
    return _clean_text(msg.get("body") or "", limit)


def _message_kind(msg: dict) -> str:
    text = f"{msg.get('subject', '')} {msg.get('from', '')} {msg.get('from_addr', '')} {msg.get('body', '')}".lower()
    if any(w in text for w in ["parentsquare", "agnew", "right at school", "school", "teacher", "field trip"]):
        return "school"
    if any(
        w in text
        for w in [
            "security",
            "alert",
            "verify",
            "password",
            "suspicious",
            "login",
            "account data",
            "access to your google account",
            "third-party access",
            "permission",
            "privacy",
        ]
    ):
        return "security"
    if any(
        w in text for w in ["delivered", "delivery", "out for delivery", "shipped", "shipment", "package", "order #"]
    ):
        return "delivery"
    if any(w in text for w in ["bill", "payment", "balance", "statement", "invoice"]):
        return "billing"
    if any(w in text for w in ["sale", "deals", "coupon", "newsletter", "webinar", "unsubscribe", "fares as low"]):
        return "marketing"
    return "other"


def _info_line(msg: dict) -> str:
    """Summarize the information, not the email envelope."""
    kind = _message_kind(msg)
    subject = _subject(msg)
    body = _body(msg, 150)
    source = _sender(msg)
    attach = " Есть вложение." if msg.get("has_attachment") else ""
    detail = f" {body}" if body else ""

    if kind == "school":
        return f"- Школа/дети: {subject}.{detail}{attach}"
    if kind == "security":
        return f"- Безопасность: {subject}.{detail}{attach}"
    if kind == "delivery":
        return f"- Доставка: {source} — {subject}.{detail}{attach}"
    if kind == "billing":
        return f"- Платежи/счета: {source} — {subject}.{detail}{attach}"
    return f"- {source}: {subject}.{detail}{attach}"


def _action_for_kind(kind: str) -> str | None:
    actions = {
        "school": "Проверить школьные обновления и понять, нужно ли что-то подтвердить/ответить.",
        "security": "Проверить security alert; если вход/изменение были не твои — срочно менять пароль.",
        "delivery": "Сверить доставки: что уже delivered, что out for delivery, и не требует ли действий.",
        "billing": "Проверить платеж/счет и дедлайн, если письмо про оплату.",
    }
    return actions.get(kind)


def _cleanup_summary(junk: list[dict]) -> str:
    if not junk:
        return "- Явного мусора/промо не заметил."
    senders = []
    for msg in junk[:6]:
        sender = _sender(msg)
        if sender not in senders:
            senders.append(sender)
    return f"- Вижу {len(junk)} промо/рассылок: {', '.join(senders[:4])}. Можно почистить пачкой или отписаться от повторяющихся."


def _delivery_item(msg: dict) -> str:
    source = _sender(msg)
    subject = _subject(msg)
    body = _body(msg, 120)
    text = f"{subject} {body}".lower()
    order_match = re.search(r"order\s*#?\s*([A-Za-z0-9-]+)", f"{subject} {body}", flags=re.I)
    order = f" order #{order_match.group(1)}" if order_match else ""
    arrive_match = re.search(r"arrive by ([A-Za-z]{3,9},?\s+[A-Za-z]{3,9}\s+\d{1,2})", f"{subject} {body}", flags=re.I)

    if "has been delivered" in text or "delivered" in text:
        status = "доставлено"
    elif "out for delivery" in text:
        status = "сегодня в доставке"
    elif arrive_match:
        status = f"ожидается {arrive_match.group(1)}"
    elif "shipped" in text or "shipment" in text:
        status = "отправлено"
    elif "informed delivery" in text or "daily digest" in text:
        status = "USPS digest готов к просмотру"
    else:
        status = subject
    return f"{source}{order}: {status}"


def _summary_lines_for(messages: list[dict]) -> list[str]:
    """Return assistant-style information lines, grouping repetitive categories."""
    if not messages:
        return []
    delivery = [m for m in messages if _message_kind(m) == "delivery"]
    rest = [m for m in messages if _message_kind(m) != "delivery"]
    lines = [_info_line(m) for m in rest]
    if delivery:
        items = [_delivery_item(m) for m in delivery[:5]]
        suffix = "" if len(delivery) <= 5 else f"; еще {len(delivery) - 5} доставок"
        lines.append(f"- Доставки: {'; '.join(items)}{suffix}.")
    return lines


def _classify(messages: list[dict]) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    urgent: list[dict] = []
    today: list[dict] = []
    other: list[dict] = []
    junk: list[dict] = []

    for msg in messages:
        score = int(msg.get("priority") or 0)
        text = f"{msg.get('subject', '')} {msg.get('from', '')} {msg.get('from_addr', '')}".lower()
        is_marketing = score <= 5 or any(
            w in text
            for w in [
                "sale",
                "deals",
                "coupon",
                "memorial day",
                "newsletter",
                "webinar",
                "страхов",
                "insurance",
                "fares as low",
                "surprise",
            ]
        )
        is_urgent = score >= 70 or any(
            w in text
            for w in [
                "security",
                "alert",
                "verify",
                "password",
                "suspicious",
                "login",
                "account data",
                "access to your google account",
                "third-party access",
                "permission",
                "privacy",
                "school",
                "teacher",
                "parent-teacher",
                "parentsquare",
                "field trip",
                "interview",
                "hiring",
            ]
        )
        is_today = (score >= 35 and not is_urgent and not is_marketing) or any(
            w in text
            for w in [
                "bill",
                "payment",
                "balance",
                "statement",
                "delivered",
                "delivery",
                "package",
                "reservation",
                "flight",
                "booking",
            ]
        )

        if is_urgent:
            urgent.append(msg)
        elif is_today:
            today.append(msg)
        elif is_marketing:
            junk.append(msg)
        else:
            other.append(msg)

    return urgent[:3], today[:5], other[:5], junk[:6]


def _build_digest(prefetch: dict) -> tuple[str, dict, bool]:
    meta = prefetch.get("_meta") or {}
    messages = prefetch.get("messages") or []
    unread_count = int(meta.get("unread_count") or 0)
    batch_count = int(meta.get("batch_count") or len(messages) or 0)
    total_inbox = int(meta.get("total_fetched") or meta.get("total_inbox") or 0)
    since_at = meta.get("since_at")

    if batch_count == 0:
        summary = "📬 Утренний обзор почты: после прошлого обзора новых писем в INBOX не найдено."
        return (
            summary,
            {
                "status": "empty",
                "summary": summary,
                "messages_count": 0,
                "batch_count": 0,
                "unread_count": unread_count,
                "total_inbox": total_inbox,
                "since_at": since_at,
                "important_count": 0,
                "junk_count": 0,
            },
            True,
        )

    urgent, today, other, junk = _classify(messages)
    important_count = len(urgent) + len(today) + len(other)

    signal_count = len(urgent) + len(today)
    lines: list[str] = [
        "📬 Утренний обзор почты — 7:00 Bay Area",
        f"Суть: после прошлого обзора пришло {batch_count} писем; непрочитанных среди них {unread_count}. "
        f"Вижу {signal_count} важных/срочных сигналов и {len(junk)} писем, похожих на промо/мусор.",
    ]

    lines.append("\n**Главное и срочное**")
    lines.extend(_summary_lines_for(urgent) or ["- Явных срочных сигналов не вижу."])

    lines.append("\n**Важно сегодня**")
    lines.extend(_summary_lines_for(today) or ["- Ничего явно требующего внимания сегодня не вижу."])

    if other:
        lines.append("\n**Фоном**")
        lines.extend(_summary_lines_for(other[:3]))

    action_items: list[str] = []
    for msg in urgent + today:
        action = _action_for_kind(_message_kind(msg))
        if action and action not in action_items:
            action_items.append(action)
    if junk:
        action_items.append(
            "Почистить промо/рассылки: удалить очевидный мусор или отписаться от повторяющихся отправителей."
        )

    lines.append("\n**Что я бы сделал дальше**")
    lines.extend(
        [f"- {a}" for a in action_items[:5]] or ["- Ничего срочного делать не нужно; можно просто оставить как есть."]
    )

    lines.append("\n**Почистить почту**")
    lines.append(_cleanup_summary(junk))
    lines.append(
        "- Я ничего сам не удаляю и не архивирую. Если хочешь — продолжим, и я помогу разобрать кандидатов на удаление/отписку."
    )

    summary = "\n".join(lines)
    skip_delivery = batch_count == 0
    return (
        summary,
        {
            "status": "ok",
            "summary": summary,
            "messages_count": len(messages),
            "batch_count": batch_count,
            "unread_count": unread_count,
            "total_inbox": total_inbox,
            "since_at": since_at,
            "batch_min_date": meta.get("batch_min_date"),
            "batch_max_date": meta.get("batch_max_date"),
            "important_count": important_count,
            "junk_count": len(junk),
        },
        skip_delivery,
    )


def _error_context(run_id: str, short_task_id: str, run_dir: Path, error: str) -> dict:
    generated_at = datetime.now(timezone.utc).isoformat()
    result_data = {
        "job_id": TASK_ID,
        "run_id": run_id,
        "schema_id": SCHEMA_ID,
        "generated_at": generated_at,
        "status": "error",
        "summary": "Gmail morning digest failed",
        "meta": {},
        "messages": [],
        "errors": [error],
    }
    save_run_result(run_dir, result_data)
    prompt_context = {"status": "error", "summary": f"📬 Gmail digest failed: {error}", "errors": [error]}
    save_prompt_context(run_dir, prompt_context)
    cb = make_callback_data(short_task_id, run_id)
    return {
        "run_info": {
            "task_id": TASK_ID,
            "run_id": run_id,
            "short_task_id": short_task_id,
            "callback_data": cb,
            "run_dir": str(run_dir),
        },
        "prompt_context": prompt_context,
        "result_schema": SCHEMA_ID,
        "skip_delivery": False,
        "agent_instructions": {
            "continue_marker": f"[AR_CONTINUE:{cb}]",
            "note": "End response with continue_marker exactly as provided.",
        },
    }


def main() -> None:
    task = read_task_definition(TASK_ID)
    if not task:
        json.dump({"status": "error", "error": f"Task not found: {TASK_ID}"}, sys.stdout, ensure_ascii=False)
        sys.exit(1)

    short_task_id = task["short_task_id"]
    previous_run_id = _load_previous_run_id(short_task_id)
    state = _load_state()
    run_info = create_run(TASK_ID)
    run_dir = Path(run_info["run_dir"])
    run_id = run_info["run_id"]

    ok, stdout, stderr = _run_prefetch(state)
    save_run_stdout_stderr(run_dir, stdout, stderr)

    if not ok:
        json.dump(
            _error_context(run_id, short_task_id, run_dir, stderr or "unknown error"),
            sys.stdout,
            indent=2,
            ensure_ascii=False,
        )
        return

    try:
        prefetch = json.loads(stdout)
    except Exception as exc:
        json.dump(
            _error_context(run_id, short_task_id, run_dir, f"prefetch returned invalid JSON: {exc}"),
            sys.stdout,
            indent=2,
            ensure_ascii=False,
        )
        return

    summary, prompt_context, skip_delivery = _build_digest(prefetch)
    generated_at = datetime.now(timezone.utc).isoformat()
    result_data = {
        "job_id": TASK_ID,
        "run_id": run_id,
        "schema_id": SCHEMA_ID,
        "generated_at": generated_at,
        "status": prompt_context["status"],
        "summary": summary,
        "meta": prefetch.get("_meta") or {},
        "messages": prefetch.get("messages") or [],
        "errors": [],
        "previous_run_id": previous_run_id,
    }
    save_run_result(run_dir, result_data)
    save_prompt_context(run_dir, prompt_context)
    if os.getenv("GMAIL_DIGEST_DRY_RUN", "").lower() not in {"1", "true", "yes"}:
        _save_state(_update_state_after_run(state, run_id, generated_at, prefetch, prompt_context))

    cb = make_callback_data(short_task_id, run_id)
    context = {
        "run_info": {
            "task_id": TASK_ID,
            "run_id": run_id,
            "short_task_id": short_task_id,
            "callback_data": cb,
            "run_dir": str(run_dir),
        },
        "prompt_context": prompt_context,
        "result_schema": SCHEMA_ID,
        "skip_delivery": skip_delivery,
        "agent_instructions": {
            "continue_marker": f"[AR_CONTINUE:{cb}]",
            "note": "End your response with the continue_marker exactly as provided to enable 'Continue dialog' button.",
        },
    }
    json.dump(context, sys.stdout, indent=2, ensure_ascii=False)


if __name__ == "__main__":
    main()
