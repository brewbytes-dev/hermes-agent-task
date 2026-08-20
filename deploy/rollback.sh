#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 || $# -ne 1 ]]; then
  echo "usage: sudo deploy/rollback.sh /home/hermes/.hermes/backups/agent-task-production-<timestamp>" >&2
  exit 2
fi

backup_dir=$1
hermes_home=${HERMES_HOME:-/home/hermes/.hermes}
hermes_user=${HERMES_USER:-hermes}
hermes_group=${HERMES_GROUP:-hermes}

case "$backup_dir" in
  "$hermes_home"/backups/agent-task-production-*) ;;
  *) echo "refusing unexpected backup path: $backup_dir" >&2; exit 2 ;;
esac
test -d "$backup_dir"

for source in "$backup_dir"/agent_task_*.py; do
  [[ -e "$source" ]] || continue
  install -o "$hermes_user" -g "$hermes_group" -m 0755 "$source" "$hermes_home/scripts/$(basename "$source")"
done
if [[ -f "$backup_dir/plugin_agent_task.py" ]]; then
  install -o "$hermes_user" -g "$hermes_group" -m 0644 \
    "$backup_dir/plugin_agent_task.py" "$hermes_home/plugins/agent-task/agent_task.py"
fi
crontab -u "$hermes_user" "$backup_dir/crontab.before"
systemctl restart hermes-gateway.service
systemctl is-active --quiet hermes-gateway.service

echo "rollback restored from $backup_dir"
