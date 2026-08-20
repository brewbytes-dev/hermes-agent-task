# Production readiness gate

## Automated gates

- Unit/regression tests pass.
- Ruff and Python compilation pass.
- `doctor` passes as the `hermes` Unix user using the managed venv.
- System Python 3.10 invocation re-execs into the managed Python 3.11+ runtime.
- The configured Telegram Bot API `getMe` request succeeds without printing the
  token; the live installation uses its configured local Bot API base URL.
- Cron entries use the managed Python, durable logs, failed-delivery retry,
  retention, and one managed marker per task.
- `agent-task-health.timer` is active and its service has a successful result.
- The plugin package registers both the `agent_task` tool and the
  `pre_gateway_dispatch` continuation hook.
- `hermes plugins doctor plugin --ci` passes with no manifest drift, and a clean
  `hermes plugins install brewbytes-dev/hermes-agent-task/plugin` includes the
  bundled runner without packaging host-administration files.
- The published manifest targets schema v1 so it remains installable by the
  stable Hermes installer while declaring the current tool and hook metadata.
- Default tool responses omit runner stdout/stderr, exit codes, and paths;
  `debug: true` is the explicit diagnostic escape hatch.

## Live delivery gate

Use an existing run and its configured task topic. Confirm all of the following:

1. `deliver` exits zero and returns a Telegram message ID.
2. `run.json.delivery.status` is `delivered` and `attempts` increased.
3. The notification appears in the intended Telegram topic.
4. `run.json.delivery.reply_context` is `ready`, and the reply index contains
   the exact chat/message pair.
5. A synthetic dispatch through Hermes rewrites the user reply with the matching
   prompt context while a different chat/message pair receives no context.
6. A real Telegram reply answers from the originating result without asking the
   user for a task ID, run ID, path, or repeated context.

Do not use collector/build success alone as delivery evidence.

## Retention gate

Run `prune` without `--apply`, inspect candidate counts and bytes, then apply the
45-day/200-run/10-minimum policy. The command refuses to delete paths outside
`$HERMES_HOME/agent_runs`.

## Rollback gate

The deployment backup must contain every replaced file plus the previous Hermes
user crontab. After rollback, compile the restored runner, reload the crontab,
restart only `hermes-gateway.service`, and rerun `doctor`.
