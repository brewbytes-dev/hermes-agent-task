# Hermes agent-task

Production-hardened scheduled task runner and Hermes tool plugin extracted from
the live Hermes installation. The repository intentionally excludes bot tokens,
Telegram destination IDs, task state, run output, and mailbox credentials.

## Runtime contract

- Python 3.11 or newer. Scheduled runs use the managed Hermes venv explicitly.
- Task definitions remain in `$HERMES_HOME/agent_tasks/<task-id>/`.
- Run output remains in `$HERMES_HOME/agent_runs/<short-id>/<run-id>/`.
- Each task has a non-blocking process lock, so overlapping cron/manual runs fail
  with exit code 75 instead of running concurrently.
- Delivery state is persisted in `run.json`. Failed sends form a small outbox and
  can be retried before the next collector advances state.
- Default retention is 45 days, at most 200 runs per task, while preserving at
  least the newest 10 runs.
- Cron logs are durable under `$HERMES_HOME/logs/agent-task/` and are rotated.

## Local verification

```bash
python3 -m pytest -q
ruff check .
python3 -m compileall -q scripts plugin tests deploy
```

Useful runner commands:

```text
agent_task_runner.py doctor
agent_task_runner.py prune
agent_task_runner.py prune --apply
agent_task_runner.py retry-failed <task-id>
agent_task_runner.py run <task-id> --deliver telegram --skip-if-empty --retry-failed --prune
```

`prune` is a dry-run unless `--apply` is supplied. Timezone-aware schedules may
append an IANA timezone, for example `0 7 * * * America/Los_Angeles`; the cron
renderer uses an hourly local-time guard so DST changes do not require editing
the crontab.

## Deployment

1. Stage this checkout on the host without task definitions or runtime data.
2. Run `deploy/install.sh` as root. It creates a timestamped backup of the live
   runner, plugin, collectors, and Hermes user crontab.
3. Restart only `hermes-gateway.service` so the updated tool plugin is loaded.
4. Run the live gates documented in `PRODUCTION_READINESS.md`.

The installer preserves `agent_tasks`, `agent_runs`, `.env`, auth, sessions,
memory, gateway config, and both gateway services. Use
`deploy/rollback.sh <backup-directory>` for a targeted rollback.
