#!/usr/bin/env python3
"""Forward blocked newsletter migrations to the agent mailbox and brief them daily.

Fallback senders that reject automated re-subscription are forwarded from the
personal Gmail inbox to brewbytes.agent@gmail.com and archived after successful
SMTP delivery. All six selected newsletters are monitored in the agent inbox.
"""

from __future__ import annotations

import email
import hashlib
import html
import imaplib
import json
import os
import re
import smtplib
import ssl
import subprocess
import sys
from datetime import datetime, timezone
from email.message import EmailMessage
from email.policy import default
from email.utils import parseaddr
from pathlib import Path
from zoneinfo import ZoneInfo

HERMES_HOME = Path(os.path.expanduser(os.environ.get("HERMES_HOME", "~/.hermes")))
SCRIPTS_DIR = HERMES_HOME / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
from agent_task_runner import (  # noqa: E402
    create_run,
    make_callback_data,
    read_task_definition,
    save_prompt_context,
    save_run_result,
    save_run_stdout_stderr,
)

TASK_ID = "newsletter-brief"
SCHEMA_ID = f"{TASK_ID}/v1"
TASK_DIR = HERMES_HOME / "agent_tasks" / TASK_ID
STATE_PATH = TASK_DIR / "state.json"

PERSONAL_EMAIL = os.environ.get("AGENT_TASK_NEWSLETTER_PERSONAL_EMAIL", "3dotsmrx@gmail.com")
AGENT_EMAIL = os.environ.get("AGENT_TASK_NEWSLETTER_AGENT_EMAIL", "brewbytes.agent@gmail.com")
PERSONAL_PASS_CMD = os.environ.get(
    "AGENT_TASK_NEWSLETTER_PERSONAL_PASS_CMD",
    str(Path.home() / ".config" / "himalaya" / "gmail-app-password.sh"),
)
AGENT_PASS_CMD = os.environ.get(
    "AGENT_TASK_NEWSLETTER_AGENT_PASS_CMD",
    str(Path.home() / ".config" / "himalaya" / "brewbytes-agent-app-password.sh"),
)

# Senders relayed from the personal inbox to the agent inbox. The personal copy
# is archived only after successful SMTP delivery. Drift is filtered separately:
# order/shipping/receipt messages stay in the personal inbox; marketing is relayed.
FORWARD_SENDERS = {
    "thewebscrapingclub@substack.com": "Web Scraping Club",
    "lg@substack.com": "Julie Zhuo / Looking Glass",
    "hello@abarabove.com": "A Bar Above",
    "hello@drift.co": "Drift promotions",
    "topgolf@email.topgolf.com": "Topgolf",
    "newletter.na@edm.anker.com": "Anker",
    "support@drinktrade.com": "Trade Coffee",
    "hello@fellowproducts.com": "Fellow",
    "info@fellowproducts.com": "Fellow",
    "info@onyxcoffeelab.com": "Onyx Coffee Lab",
    "service@digitizefluid.com": "DiFluid",
    "support@thirdwavewater.com": "Third Wave Water",
    "info@nucleuscoffee.com": "Nucleus Coffee",
    "content@mixpanel.com": "Mixpanel",
    "no-reply@mixpanel.com": "Mixpanel",
    "intern@academy.yandex.ru": "Young&&Yandex",
    "hello@mermaid.ai": "Mermaid",
    "marie@tally.so": "Tally",
    "hi@update.betterstack.com": "Better Stack",
    "info@make.com": "Make",
    "mail@ifttt.com": "IFTTT",
    "no_reply@email.heygen.com": "HeyGen",
    "news@info.santaclaraca.gov": "City of Santa Clara",
    "adventureawaits@recreation.gov": "Recreation.gov",
    "insider@marketing.sonomaraceway.com": "Sonoma Raceway",
    "newsletter@email.ticketmaster.com": "Ticketmaster local recommendations",
    "monsterjam@email.feld-inc.com": "Monster Jam",
}

MONITOR_SENDERS = {
    **FORWARD_SENDERS,
    "eddieh@substack.com": "Eddie's List",
    "noreply@mail.selfh.st": "Self-Host Weekly",
    "info@artforintrovert.ru": "Правое полушарие Интроверта",
}

DRIFT_TRANSACTIONAL_RE = re.compile(
    r"order|ordered|shipping|shipped|shipment|delivery|delivered|tracking|receipt|refund|return|subscription|payment|address|account",
    re.I,
)


def _password(cmd: str) -> str:
    return subprocess.check_output([cmd], text=True, timeout=15).strip().replace(" ", "")


def _load_state() -> dict:
    if not STATE_PATH.exists():
        return {"version": 1, "personal_initialized": False, "agent_initialized": False, "forwarded": [], "briefed": []}
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        state.setdefault("forwarded", [])
        state.setdefault("briefed", [])
        return state
    except Exception:
        return {
            "version": 1,
            "personal_initialized": False,
            "agent_initialized": False,
            "forwarded": [],
            "briefed": [],
            "warning": "state reset",
        }


def _save_state(state: dict) -> None:
    TASK_DIR.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    state["forwarded"] = list(dict.fromkeys(state.get("forwarded", [])))[-5000:]
    state["briefed"] = list(dict.fromkeys(state.get("briefed", [])))[-5000:]
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(STATE_PATH)


def _connect(user: str, pass_cmd: str) -> imaplib.IMAP4_SSL:
    conn = imaplib.IMAP4_SSL("imap.gmail.com", 993)
    conn.login(user, _password(pass_cmd))
    return conn


def _search_sender_uids(conn: imaplib.IMAP4_SSL, sender: str) -> list[str]:
    typ, data = conn.uid("search", None, "FROM", f'"{sender}"')
    if typ != "OK" or not data:
        return []
    return data[0].decode().split()


def _message_key(msg: email.message.EmailMessage) -> str:
    message_id = (msg.get("Message-ID") or "").strip()
    if message_id:
        return hashlib.sha256(message_id.encode()).hexdigest()[:24]
    raw = "|".join([msg.get("From", ""), msg.get("Date", ""), msg.get("Subject", "")])
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def _fetch_message(conn: imaplib.IMAP4_SSL, uid: str) -> email.message.EmailMessage:
    typ, data = conn.uid("fetch", uid, "(BODY.PEEK[])")
    if typ != "OK":
        raise RuntimeError(f"IMAP fetch failed for UID {uid}")
    raw = next((part[1] for part in data if isinstance(part, tuple) and len(part) > 1), None)
    if not raw:
        raise RuntimeError(f"Empty IMAP message for UID {uid}")
    return email.message_from_bytes(raw, policy=default)


def _html_to_text(value: str) -> str:
    value = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", value)
    value = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</h[1-6]>", "\n", value)
    value = re.sub(r"(?s)<[^>]+>", " ", value)
    value = html.unescape(value)
    return "\n".join(line.strip() for line in value.splitlines() if line.strip())


def _body_text(msg: email.message.EmailMessage, limit: int = 18000) -> str:
    plain, rich = [], []
    parts = msg.walk() if msg.is_multipart() else [msg]
    for part in parts:
        ctype = part.get_content_type()
        disp = (part.get("Content-Disposition") or "").lower()
        if "attachment" in disp:
            continue
        if ctype not in {"text/plain", "text/html"}:
            continue
        try:
            value = part.get_content()
        except Exception:
            payload = part.get_payload(decode=True) or b""
            value = payload.decode(part.get_content_charset() or "utf-8", "replace")
        (plain if ctype == "text/plain" else rich).append(str(value))
    text = "\n".join(plain).strip() or _html_to_text("\n".join(rich))
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text[:limit]


def _send_forward(msg: email.message.EmailMessage) -> None:
    sender = parseaddr(msg.get("From", ""))[1].lower()
    out = EmailMessage()
    out["From"] = PERSONAL_EMAIL
    out["To"] = AGENT_EMAIL
    out["Subject"] = f"[Newsletter relay] {msg.get('Subject', '(без темы)')}"
    out["X-Hermes-Newsletter-Source"] = sender
    out.set_content(
        f"Forwarded automatically from {PERSONAL_EMAIL}\n"
        f"Original sender: {msg.get('From', '')}\n"
        f"Original date: {msg.get('Date', '')}\n\n"
        f"{_body_text(msg)}"
    )
    with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as smtp:
        smtp.starttls(context=ssl.create_default_context())
        smtp.login(PERSONAL_EMAIL, _password(PERSONAL_PASS_CMD))
        smtp.send_message(out)


def _forward_personal(state: dict) -> tuple[list[dict], list[str]]:
    forwarded, errors = [], []
    conn = _connect(PERSONAL_EMAIL, PERSONAL_PASS_CMD)
    try:
        conn.select("INBOX")
        candidates = []
        for sender, label in FORWARD_SENDERS.items():
            for uid in _search_sender_uids(conn, sender):
                candidates.append((int(uid), uid, sender, label))
        candidates.sort()
        seen = set(state.get("forwarded", []))

        if not state.get("personal_initialized"):
            # Transfer the latest existing issue from each fallback sender as a smoke test;
            # baseline older issues so they are not replayed.
            latest = {}
            for item in candidates:
                latest[item[2]] = item
            latest_uids = (
                {item[1] for item in latest.values()}
                if os.getenv("NEWSLETTER_FORWARD_LATEST", "") in {"1", "true", "yes"}
                else set()
            )
            for _, uid, _, _ in candidates:
                if uid not in latest_uids:
                    try:
                        seen.add(_message_key(_fetch_message(conn, uid)))
                    except Exception as exc:
                        errors.append(str(exc))
            state["personal_initialized"] = True

        for _, uid, sender, label in candidates:
            try:
                msg = _fetch_message(conn, uid)
                key = _message_key(msg)
                if key in seen:
                    continue
                actual = parseaddr(msg.get("From", ""))[1].lower()
                if actual != sender:
                    continue
                # Drift mixes order notifications and marketing on one From address.
                # Keep anything plausibly transactional in the personal inbox.
                if sender == "hello@drift.co" and DRIFT_TRANSACTIONAL_RE.search(msg.get("Subject", "")):
                    seen.add(key)
                    continue
                _send_forward(msg)
                typ, _ = conn.uid("store", uid, "+FLAGS.SILENT", "(\\Deleted)")
                if typ != "OK":
                    raise RuntimeError(f"Forwarded but failed to archive UID {uid}")
                conn.expunge()  # Gmail: removes INBOX label, keeps message in All Mail.
                seen.add(key)
                forwarded.append({"source": label, "subject": msg.get("Subject", ""), "key": key})
            except Exception as exc:
                errors.append(f"{label}: {exc}")
        state["forwarded"] = sorted(seen)
    finally:
        try:
            conn.logout()
        except Exception:
            pass
    return forwarded, errors


def _collect_agent_messages(state: dict, force_brief: bool = False) -> tuple[list[dict], list[str], bool]:
    errors, messages = [], []
    conn = _connect(AGENT_EMAIL, AGENT_PASS_CMD)
    try:
        conn.select("INBOX")
        candidates = []
        search_senders = {**MONITOR_SENDERS, PERSONAL_EMAIL: "Newsletter relay"}
        for sender, label in search_senders.items():
            for uid in _search_sender_uids(conn, sender):
                candidates.append((int(uid), uid, sender, label))
        candidates.sort()
        briefed = set(state.get("briefed", []))
        if not state.get("agent_initialized"):
            # Baseline confirmations and old directly-subscribed messages. Relayed smoke-test
            # messages remain visible but are not replayed to Telegram.
            for _, uid, _, _ in candidates:
                try:
                    briefed.add(_message_key(_fetch_message(conn, uid)))
                except Exception as exc:
                    errors.append(str(exc))
            state["agent_initialized"] = True
            state["briefed"] = sorted(briefed)
            return [], errors, True

        local_hour = datetime.now(ZoneInfo("America/Los_Angeles")).hour
        briefing_time = (
            force_brief or os.getenv("NEWSLETTER_BRIEF_FORCE", "") in {"1", "true", "yes"} or local_hour == 8
        )
        if not briefing_time:
            return [], errors, False

        for _, uid, sender, label in candidates:
            try:
                msg = _fetch_message(conn, uid)
                key = _message_key(msg)
                if key in briefed:
                    continue
                actual = parseaddr(msg.get("From", ""))[1].lower()
                relay_source = (msg.get("X-Hermes-Newsletter-Source") or "").lower()
                if actual not in MONITOR_SENDERS and relay_source not in MONITOR_SENDERS:
                    continue
                messages.append(
                    {
                        "source": MONITOR_SENDERS.get(relay_source) or label,
                        "subject": msg.get("Subject", ""),
                        "date": msg.get("Date", ""),
                        "body": _body_text(msg, 7000),
                        "key": key,
                    }
                )
                briefed.add(key)
            except Exception as exc:
                errors.append(f"{label}: {exc}")
        state["briefed"] = sorted(briefed)
    finally:
        try:
            conn.logout()
        except Exception:
            pass
    return messages[-12:], errors, False


def main() -> None:
    task = read_task_definition(TASK_ID)
    if not task:
        json.dump({"status": "error", "error": f"Task not found: {TASK_ID}"}, sys.stdout)
        return
    run = create_run(TASK_ID)
    run_dir = Path(run["run_dir"])
    state = _load_state()
    forwarded, forward_errors = _forward_personal(state)
    messages, collect_errors, initialized = _collect_agent_messages(state)
    errors = forward_errors + collect_errors
    _save_state(state)

    generated_at = datetime.now(timezone.utc).isoformat()
    if errors:
        status = "error"
        summary = "Newsletter relay/brief encountered errors: " + "; ".join(errors[:5])
        skip = False
    elif messages:
        status = "ok"
        summary = f"Новых выпусков для разбора: {len(messages)}. Переслано из личной почты: {len(forwarded)}."
        skip = False
    else:
        status = "empty"
        summary = f"Новых выпусков для утреннего брифа нет. Переслано из личной почты: {len(forwarded)}."
        skip = True

    result = {
        "job_id": TASK_ID,
        "run_id": run["run_id"],
        "schema_id": SCHEMA_ID,
        "generated_at": generated_at,
        "status": status,
        "forwarded": forwarded,
        "messages": messages,
        "errors": errors,
        "initialized": initialized,
    }
    prompt_context = {
        "status": status,
        "summary": summary,
        "forwarded_count": len(forwarded),
        "messages": messages,
        "errors": errors,
    }
    save_run_result(run_dir, result)
    save_prompt_context(run_dir, prompt_context)
    save_run_stdout_stderr(run_dir, json.dumps({"forwarded": forwarded}, ensure_ascii=False), "\n".join(errors))
    cb = make_callback_data(run["short_task_id"], run["run_id"])
    json.dump(
        {
            "run_info": {
                "task_id": run["task_id"],
                "short_task_id": run["short_task_id"],
                "run_id": run["run_id"],
                "run_dir": str(run["run_dir"]),
                "callback_data": cb,
            },
            "prompt_context": prompt_context,
            "result_schema": SCHEMA_ID,
            "skip_delivery": skip,
            "agent_instructions": {"continuation": "reply_to_task_message"},
        },
        sys.stdout,
        ensure_ascii=False,
        indent=2,
    )


if __name__ == "__main__":
    main()
