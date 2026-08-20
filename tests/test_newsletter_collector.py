from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def load_collector(home: Path):
    os.environ["HERMES_HOME"] = str(home)
    scripts = str(ROOT / "plugin" / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    spec = importlib.util.spec_from_file_location(
        "newsletter_collector_under_test",
        ROOT / "plugin" / "scripts" / "agent_task_newsletter_brief_collector.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_batch_sender_search_uses_one_imap_round_trip(tmp_path: Path) -> None:
    collector = load_collector(tmp_path)

    class Connection:
        calls = []

        def uid(self, *args):
            self.calls.append(args)
            return "OK", [b"11 12"]

    connection = Connection()

    result = collector._search_senders_uids(connection, ["a@example.com", "b@example.com"])

    assert result == ["11", "12"]
    assert len(connection.calls) == 1
    assert connection.calls[0][2] == "X-GM-RAW"
    assert "from:a@example.com" in connection.calls[0][3]
    assert "from:b@example.com" in connection.calls[0][3]


def test_non_briefing_hour_skips_agent_mailbox_login(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    collector = load_collector(tmp_path)
    monkeypatch.setattr(collector, "_is_briefing_time", lambda force=False: False)
    monkeypatch.setattr(
        collector,
        "_connect",
        lambda *args, **kwargs: pytest.fail("agent mailbox must not be opened outside briefing time"),
    )

    messages, errors, initialized = collector._collect_agent_messages({"agent_initialized": True})

    assert messages == []
    assert errors == []
    assert initialized is False
