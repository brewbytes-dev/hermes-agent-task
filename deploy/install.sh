#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "install.sh must run as root" >&2
  exit 2
fi

source_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
hermes_home=${HERMES_HOME:-/home/hermes/.hermes}
hermes_user=${HERMES_USER:-hermes}
hermes_group=${HERMES_GROUP:-hermes}
managed_python="$hermes_home/hermes-agent/venv/bin/python"
timestamp=$(date -u +%Y%m%dT%H%M%SZ)
backup_dir="$hermes_home/backups/agent-task-production-$timestamp"

test -x "$managed_python"
install -d -o "$hermes_user" -g "$hermes_group" -m 0700 "$backup_dir"
install -d -o "$hermes_user" -g "$hermes_group" -m 0700 "$hermes_home/logs/agent-task"
install -o "$hermes_user" -g "$hermes_group" -m 0700 \
  "$source_root/deploy/migrate_crontab.py" "$backup_dir/migrate_crontab.py"

for source in "$source_root"/scripts/agent_task_*.py; do
  target="$hermes_home/scripts/$(basename "$source")"
  if [[ -f "$target" ]]; then
    cp -a "$target" "$backup_dir/"
  fi
  install -o "$hermes_user" -g "$hermes_group" -m 0755 "$source" "$target"
done

plugin_dir="$hermes_home/plugins/agent-task"
plugin_target="$plugin_dir/agent_task.py"
install -d -o "$hermes_user" -g "$hermes_group" -m 0755 "$plugin_dir"
if [[ -d "$plugin_dir" ]]; then
  install -d -o "$hermes_user" -g "$hermes_group" -m 0700 "$backup_dir/plugin"
  cp -a "$plugin_dir"/. "$backup_dir/plugin/"
fi
for plugin_file in __init__.py agent_task.py plugin.yaml; do
  install -o "$hermes_user" -g "$hermes_group" -m 0644 \
    "$source_root/plugin/$plugin_file" "$plugin_dir/$plugin_file"
done

if ! crontab -u "$hermes_user" -l >"$backup_dir/crontab.before" 2>/dev/null; then
  : >"$backup_dir/crontab.before"
fi
chown "$hermes_user:$hermes_group" "$backup_dir/crontab.before"
chmod 0600 "$backup_dir/crontab.before"

sudo -u "$hermes_user" -H env HERMES_HOME="$hermes_home" \
  "$managed_python" -m py_compile "$hermes_home"/scripts/agent_task_*.py "$plugin_dir/__init__.py" "$plugin_target"
sudo -u "$hermes_user" -H env HERMES_HOME="$hermes_home" PYTHONPATH="$hermes_home/hermes-agent" \
  "$managed_python" "$backup_dir/migrate_crontab.py" --plugin-path "$plugin_target" --apply
# The installer runs as root, so the parent-shell redirects intentionally own these files first.
# shellcheck disable=SC2024
sudo -u "$hermes_user" -H env HERMES_HOME="$hermes_home" \
  "$managed_python" "$hermes_home/scripts/agent_task_runner.py" rebuild-reply-index >"$backup_dir/reply-index-rebuild.json"
# shellcheck disable=SC2024
sudo -u "$hermes_user" -H env HERMES_HOME="$hermes_home" \
  "$managed_python" "$hermes_home/scripts/agent_task_runner.py" prune >"$backup_dir/retention-dry-run.json"
chown "$hermes_user:$hermes_group" "$backup_dir/reply-index-rebuild.json" "$backup_dir/retention-dry-run.json"
chmod 0600 "$backup_dir/reply-index-rebuild.json" "$backup_dir/retention-dry-run.json"

install -o root -g root -m 0644 "$source_root/deploy/agent-task-health.service" /etc/systemd/system/agent-task-health.service
install -o root -g root -m 0644 "$source_root/deploy/agent-task-health.timer" /etc/systemd/system/agent-task-health.timer
install -o root -g root -m 0644 "$source_root/deploy/agent-task.logrotate" /etc/logrotate.d/agent-task
systemctl daemon-reload
systemctl enable --now agent-task-health.timer

echo "backup_dir=$backup_dir"
