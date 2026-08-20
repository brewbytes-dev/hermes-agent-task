from __future__ import annotations

import importlib.util
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_runner(home: Path):
    os.environ["HERMES_HOME"] = str(home)
    spec = importlib.util.spec_from_file_location(
        "agent_task_runner_under_test", ROOT / "scripts" / "agent_task_runner.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def create_task(runner, task_id: str = "sample-task", short_id: str = "sample") -> dict:
    return runner.create_task_definition(
        task_id=task_id,
        short_task_id=short_id,
        description="test task",
        schema={"type": "object"},
        collector="collector.py",
    )


def test_collector_cannot_escape_scripts_directory(tmp_path: Path) -> None:
    runner = load_runner(tmp_path)
    (tmp_path / "scripts").mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("print('{}')\n", encoding="utf-8")

    ok, stdout, stderr = runner.run_collector("../outside.py")

    assert not ok
    assert not stdout
    assert "outside" in stderr.lower() or "invalid" in stderr.lower()


def test_delivery_status_is_persisted_without_overwriting_run_status(tmp_path: Path) -> None:
    runner = load_runner(tmp_path)
    create_task(runner)
    run = runner.create_run("sample-task")
    runner.save_run_result(run["run_dir"], {"status": "ok"})

    runner.record_delivery_status(
        "sample-task",
        run["run_id"],
        status="delivered",
        details={"target": "telegram_home", "message_id": 42},
    )

    data = json.loads(Path(run["run_file"]).read_text(encoding="utf-8"))
    assert data["status"] == "ok"
    assert data["delivery"]["status"] == "delivered"
    assert data["delivery"]["attempts"] == 1
    assert data["delivery"]["message_id"] == 42


def test_task_lock_rejects_overlapping_run(tmp_path: Path) -> None:
    runner = load_runner(tmp_path)

    with runner.task_run_lock("sample-task"):
        with pytest.raises(runner.TaskAlreadyRunning):
            with runner.task_run_lock("sample-task"):
                pass


def test_retry_failed_delivery_uses_persisted_outbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner = load_runner(tmp_path)
    create_task(runner)
    run = runner.create_run("sample-task")
    runner.save_run_result(run["run_dir"], {"status": "ok", "summary": "ready"})
    runner.save_prompt_context(run["run_dir"], {"status": "ok", "summary": "ready"})
    runner.record_delivery_status("sample-task", run["run_id"], status="failed", error="temporary")
    monkeypatch.setattr(
        runner,
        "deliver_task_output",
        lambda *args, **kwargs: {"success": True, "target": "telegram_home", "message_id": 7},
    )

    report = runner.retry_failed_deliveries("sample-task")

    assert report["success"] is True
    data = json.loads(Path(run["run_file"]).read_text(encoding="utf-8"))
    assert data["delivery"]["status"] == "delivered"
    assert data["delivery"]["attempts"] == 2


def test_telegram_api_uses_gateway_local_base_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner = load_runner(tmp_path)

    class Platform:
        TELEGRAM = "telegram"

    class PlatformConfig:
        token = "test-token"
        extra = {"base_url": "http://127.0.0.1:8081/bot", "local_mode": True}

    class Config:
        platforms = {Platform.TELEGRAM: PlatformConfig()}

    monkeypatch.setattr(runner, "_load_gateway_config_for_delivery", lambda: (Config(), Platform))

    assert runner._telegram_api_url("getMe") == "http://127.0.0.1:8081/bottest-token/getMe"


def test_read_run_rejects_path_traversal(tmp_path: Path) -> None:
    runner = load_runner(tmp_path)
    create_task(runner)

    with pytest.raises(ValueError):
        runner.read_run("sample-task", "../../task.json")


def test_prune_enforces_age_and_count_with_dry_run(tmp_path: Path) -> None:
    runner = load_runner(tmp_path)
    create_task(runner)
    now = datetime.now(timezone.utc)
    created = []
    runs_dir = runner.RUNS_DIR / "sample"
    runs_dir.mkdir(parents=True)
    for index, age_days in enumerate((1, 2, 3, 40)):
        stamp = now - timedelta(days=age_days)
        run_dir = runs_dir / f"{stamp.strftime('%Y%m%dT%H%M%S')}-{index:06d}"
        run_dir.mkdir()
        (run_dir / "run.json").write_text("{}\n", encoding="utf-8")
        created.append(run_dir)

    report = runner.prune_runs(
        retention_days=30,
        max_runs_per_task=2,
        min_runs_per_task=1,
        dry_run=True,
        now=now,
    )

    assert report["deleted_count"] == 0
    assert report["candidate_count"] == 2
    assert all(path.exists() for path in created)


def test_doctor_reports_missing_managed_python(tmp_path: Path) -> None:
    runner = load_runner(tmp_path)
    create_task(runner)
    (tmp_path / "scripts").mkdir(exist_ok=True)
    (tmp_path / "scripts" / "collector.py").write_text("print('{}')\n", encoding="utf-8")

    report = runner.build_health_report()

    python_check = next(check for check in report["checks"] if check["name"] == "managed_python")
    assert python_check["status"] == "fail"
    assert report["healthy"] is False
