from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def load_plugin(home: Path):
    constants = types.ModuleType("hermes_constants")
    constants.get_hermes_home = lambda: home
    sys.modules["hermes_constants"] = constants
    spec = importlib.util.spec_from_file_location("agent_task_plugin_under_test", ROOT / "agent_task.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_plugin_package(home: Path):
    constants = types.ModuleType("hermes_constants")
    constants.get_hermes_home = lambda: home
    sys.modules["hermes_constants"] = constants
    package_name = "agent_task_package_under_test"
    spec = importlib.util.spec_from_file_location(
        package_name,
        ROOT / "__init__.py",
        submodule_search_locations=[str(ROOT)],
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[package_name] = module
    spec.loader.exec_module(module)
    return module


def test_cron_line_uses_managed_python_and_durable_log(tmp_path: Path) -> None:
    plugin = load_plugin(tmp_path)

    line = plugin._task_cron_line("newsletter-brief", "0 * * * *", deliver="telegram")

    assert str(tmp_path / "hermes-agent" / "venv" / "bin" / "python") in line
    assert str(ROOT / "scripts" / "agent_task_runner.py") in line
    assert f"AGENT_TASK_SCRIPTS_DIR={ROOT / 'scripts'}" in line
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


def test_reply_hook_restores_full_run_context_without_showing_it_in_notification(tmp_path: Path) -> None:
    plugin = load_plugin(tmp_path)
    run_id = "20260820T030000-abc123"
    task_dir = tmp_path / "agent_tasks" / "package-tracker"
    run_dir = tmp_path / "agent_runs" / "pkg" / run_id
    state_dir = tmp_path / "state"
    task_dir.mkdir(parents=True)
    run_dir.mkdir(parents=True)
    state_dir.mkdir(parents=True)
    (task_dir / "task.json").write_text(
        json.dumps(
            {
                "task_id": "package-tracker",
                "short_task_id": "pkg",
                "description": "Посылки и доставки",
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "run.json").write_text(
        json.dumps({"task_id": "package-tracker", "run_id": run_id, "status": "ok"}),
        encoding="utf-8",
    )
    (run_dir / "prompt_context.json").write_text(
        json.dumps(
            {
                "status": "ok",
                "summary": "Нашлась одна новая доставка.",
                "shipments": [{"carrier": "UPS", "state": "out for delivery"}],
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "result.json").write_text(
        json.dumps({"status": "ok", "tracking": [{"carrier": "UPS", "eta": "today"}]}),
        encoding="utf-8",
    )
    (state_dir / "agent_task_reply_index.json").write_text(
        json.dumps(
            {
                "version": 1,
                "entries": {
                    '["12345","42"]': {
                        "task_id": "package-tracker",
                        "short_task_id": "pkg",
                        "run_id": run_id,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    event = types.SimpleNamespace(
        text="А когда привезут?",
        reply_to_message_id="42",
        source=types.SimpleNamespace(platform="telegram", chat_id="12345"),
    )

    result = plugin.agent_task_reply_hook(event=event)

    assert result["action"] == "rewrite"
    assert "А когда привезут?" in result["text"]
    assert "Нашлась одна новая доставка." in result["text"]
    assert '"eta": "today"' in result["text"]
    assert "trusted context supplied by the agent-task plugin" in result["text"]


def test_reply_hook_never_crosses_chat_boundary(tmp_path: Path) -> None:
    plugin = load_plugin(tmp_path)
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "agent_task_reply_index.json").write_text(
        json.dumps(
            {
                "version": 1,
                "entries": {
                    '["12345","42"]': {
                        "task_id": "sample-task",
                        "short_task_id": "sample",
                        "run_id": "20260820T030000-abc123",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    event = types.SimpleNamespace(
        text="Продолжим",
        reply_to_message_id="42",
        source=types.SimpleNamespace(platform="telegram", chat_id="99999"),
    )

    assert plugin.agent_task_reply_hook(event=event) is None


def test_reply_hook_does_not_attach_context_before_gateway_authorization(tmp_path: Path) -> None:
    plugin = load_plugin(tmp_path)
    event = types.SimpleNamespace(
        text="Продолжим",
        reply_to_message_id="42",
        source=types.SimpleNamespace(platform="telegram", chat_id="12345"),
    )
    gateway = types.SimpleNamespace(_is_user_authorized=lambda source: False)

    assert plugin.agent_task_reply_hook(event=event, gateway=gateway) is None


def test_default_tool_response_hides_runner_internals(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plugin = load_plugin(tmp_path)
    monkeypatch.setattr(
        plugin,
        "_run_runner",
        lambda *args, **kwargs: {
            "success": True,
            "exit_code": 0,
            "stdout": json.dumps(
                {
                    "healthy": True,
                    "checks": [{"name": "managed_python", "status": "pass", "detail": "/secret/path"}],
                }
            ),
            "stderr": "technical warning",
        },
    )

    response = json.loads(plugin.agent_task_tool({"action": "health"}))

    assert response == {"success": True, "message": "Все фоновые задачи работают нормально."}


def test_debug_tool_response_keeps_runner_diagnostics(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plugin = load_plugin(tmp_path)
    monkeypatch.setattr(
        plugin,
        "_run_runner",
        lambda *args, **kwargs: {
            "success": True,
            "exit_code": 0,
            "stdout": '{"healthy": true}',
            "stderr": "technical warning",
        },
    )

    response = json.loads(plugin.agent_task_tool({"action": "health", "debug": True}))

    assert response["success"] is True
    assert response["debug"]["exit_code"] == 0
    assert response["debug"]["stderr"] == "technical warning"


def test_plugin_registration_installs_reply_hook(tmp_path: Path) -> None:
    package = load_plugin_package(tmp_path)
    registered = {"tools": [], "hooks": []}

    class Context:
        def register_tool(self, **kwargs):
            registered["tools"].append(kwargs)

        def register_hook(self, name, callback):
            registered["hooks"].append((name, callback))

    package.register(Context())

    assert registered["tools"][0]["name"] == "agent_task"
    assert registered["hooks"][0][0] == "pre_gateway_dispatch"
    assert registered["hooks"][0][1].__name__ == "agent_task_reply_hook"


def test_manifest_targets_stable_installer_and_declares_reply_hook() -> None:
    manifest_lines = (ROOT / "plugin.yaml").read_text(encoding="utf-8").splitlines()

    assert "manifest_version: 1" in manifest_lines
    assert "provides_hooks:" in manifest_lines
    assert "  - pre_gateway_dispatch" in manifest_lines


def test_read_defaults_to_latest_run_and_hides_storage_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plugin = load_plugin(tmp_path)
    run_id = "20260820T030000-abc123"
    run_dir = tmp_path / "agent_runs" / "sample" / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(json.dumps({"status": "ok"}), encoding="utf-8")
    (run_dir / "prompt_context.json").write_text(
        json.dumps({"status": "ok", "summary": "Контекст сохранён."}), encoding="utf-8"
    )
    (run_dir / "result.json").write_text(json.dumps({"answer": 42}), encoding="utf-8")
    monkeypatch.setattr(
        plugin,
        "_run_runner",
        lambda *args, **kwargs: {
            "success": True,
            "exit_code": 0,
            "stdout": json.dumps(
                {"task_id": "sample-task", "short_task_id": "sample", "description": "Полезная задача"}
            ),
            "stderr": "",
        },
    )

    response = json.loads(plugin.agent_task_tool({"action": "read", "task_id": "sample-task"}))

    assert response["success"] is True
    assert response["summary"] == "Контекст сохранён."
    assert response["result"] == {"answer": 42}
    assert str(tmp_path) not in json.dumps(response)


def test_read_rejects_run_path_traversal_without_leaking_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plugin = load_plugin(tmp_path)
    monkeypatch.setattr(
        plugin,
        "_run_runner",
        lambda *args, **kwargs: {
            "success": True,
            "stdout": json.dumps({"task_id": "sample-task", "short_task_id": "sample"}),
            "stderr": "",
        },
    )

    response = json.loads(
        plugin.agent_task_tool({"action": "read", "task_id": "sample-task", "run_id": "../../secret"})
    )

    assert response == {"success": False, "message": "Не удалось определить нужный запуск."}
