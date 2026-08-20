from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def load_migration_module():
    spec = importlib.util.spec_from_file_location(
        "agent_task_migrate_crontab_under_test", ROOT / "deploy" / "migrate_crontab.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_loads_plugin_from_explicit_path_outside_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    migration = load_migration_module()
    monkeypatch.delitem(sys.modules, "hermes_constants", raising=False)
    plugin_path = tmp_path / "installed" / "agent_task.py"
    plugin_path.parent.mkdir(parents=True)
    plugin_path.write_text(
        "from hermes_constants import get_hermes_home\n"
        "def home():\n"
        "    return str(get_hermes_home())\n",
        encoding="utf-8",
    )
    hermes_home = tmp_path / "hermes-home"

    plugin = migration.load_plugin(plugin_path, hermes_home)

    assert plugin.home() == str(hermes_home)


def test_task_definitions_only_returns_scheduled_tasks(tmp_path: Path) -> None:
    migration = load_migration_module()
    scheduled = tmp_path / "agent_tasks" / "scheduled"
    unscheduled = tmp_path / "agent_tasks" / "unscheduled"
    scheduled.mkdir(parents=True)
    unscheduled.mkdir(parents=True)
    (scheduled / "task.json").write_text(
        json.dumps({"task_id": "scheduled", "schedule": "0 7 * * *"}), encoding="utf-8"
    )
    (unscheduled / "task.json").write_text(json.dumps({"task_id": "unscheduled"}), encoding="utf-8")

    assert migration.task_definitions(tmp_path) == [{"task_id": "scheduled", "schedule": "0 7 * * *"}]
