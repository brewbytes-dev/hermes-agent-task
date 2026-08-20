from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def load_plugin(home: Path):
    constants = types.ModuleType("hermes_constants")
    constants.get_hermes_home = lambda: home
    sys.modules["hermes_constants"] = constants
    spec = importlib.util.spec_from_file_location("agent_task_plugin_under_test", ROOT / "plugin" / "agent_task.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cron_line_uses_managed_python_and_durable_log(tmp_path: Path) -> None:
    plugin = load_plugin(tmp_path)

    line = plugin._task_cron_line("newsletter-brief", "0 * * * *", deliver="telegram")

    assert str(tmp_path / "hermes-agent" / "venv" / "bin" / "python") in line
    assert str(tmp_path / "logs" / "agent-task" / "newsletter-brief.log") in line
    assert "python3 scripts/agent_task_runner.py" not in line
    assert "umask 077" in line
    assert ">>" in line


def test_timezone_schedule_uses_hourly_dst_safe_guard(tmp_path: Path) -> None:
    plugin = load_plugin(tmp_path)

    line = plugin._task_cron_line(
        "gmail-morning-digest",
        "0 7 * * * America/Los_Angeles",
        deliver="telegram",
    )

    assert line.startswith("0 * * * * ")
    assert "TZ=America/Los_Angeles date +\\%H" in line
    assert '= "07" ] &&' in line


def test_schedule_replaces_only_managed_entry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plugin = load_plugin(tmp_path)
    existing = (
        'MAILTO=""\n'
        "5 4 * * * /usr/local/bin/unrelated\n"
        "# agent-task:package-tracker\n"
        "0 9,18 * * * cd /old && python3 scripts/agent_task_runner.py run package-tracker\n"
    )
    written = []
    monkeypatch.setattr(plugin, "_crontab_read", lambda: existing)
    monkeypatch.setattr(plugin, "_crontab_write", written.append)

    result = plugin._schedule_task_in_crontab("package-tracker", "0 9,18 * * *", deliver="telegram")

    assert result["updated_existing"] is True
    assert len(written) == 1
    assert 'MAILTO=""' in written[0]
    assert "/usr/local/bin/unrelated" in written[0]
    assert written[0].count("# agent-task:package-tracker") == 1
    assert "python3 scripts/agent_task_runner.py" not in written[0]


@pytest.mark.parametrize(
    ("task_id", "schedule", "deliver"),
    [
        ("valid; touch /tmp/pwned", "0 * * * *", "telegram"),
        ("valid", "0 * * * *; touch /tmp/pwned", "telegram"),
        ("valid", "0 * * * *", "telegram; touch /tmp/pwned"),
    ],
)
def test_cron_line_rejects_shell_injection(task_id: str, schedule: str, deliver: str, tmp_path: Path) -> None:
    plugin = load_plugin(tmp_path)

    with pytest.raises(ValueError):
        plugin._task_cron_line(task_id, schedule, deliver=deliver)
