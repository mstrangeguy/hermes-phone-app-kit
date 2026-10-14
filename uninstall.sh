#!/bin/bash
# Remove the phone app's background pieces. Leaves Hermes, its .env (the API
# server stays on) and this folder alone; delete the folder yourself after.
#
# Removes: the LaunchAgent, the Tailscale Serve port (only if it points at this
# proxy), the phone-approvals plugin link (after disabling it), the update-check
# cron job and its wrapper script.
set -uo pipefail
export PATH="/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin:/opt/homebrew/bin:$PATH"

ROOT="$(cd "$(dirname "$0")" && pwd -P)"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
export HERMES_HOME   # every `hermes` command below acts on this home
LABEL="${PHONE_APP_LABEL:-com.hermes.phone-chat}"
HTTPS_PORT="${PHONE_APP_HTTPS_PORT:-8443}"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PY="${HERMES_AGENT_DIR:-$HERMES_HOME/hermes-agent}/venv/bin/python"
[ -x "$PY" ] || PY=/usr/bin/python3
PORT="$("$PY" -I "$ROOT/scripts/kit.py" config-get "$ROOT/config.json" port 2>/dev/null)"; PORT="${PORT:-8643}"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/phone-app-uninstall.XXXX")"
trap 'rm -rf "$WORK"' EXIT

read -r -p "Remove the Hermes phone app's service, Tailscale port, plugin link and cron job? [y/N] " r || r=""
case "$r" in y|Y|yes) ;; *) echo "Nothing changed."; exit 0 ;; esac

# Service
if launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
  launchctl bootout "gui/$(id -u)/$LABEL" && echo "stopped $LABEL"
fi
[ -f "$PLIST" ] && rm -f "$PLIST" && echo "removed $PLIST"

# Tailscale Serve, only our own handler
TS="$(command -v tailscale || echo /Applications/Tailscale.app/Contents/MacOS/Tailscale)"
if [ -x "$TS" ] && "$TS" status --json > "$WORK/ts.json" 2>/dev/null; then
  DNS="$("$PY" -I "$ROOT/scripts/kit.py" ts-self "$WORK/ts.json" 2>/dev/null | sed -n 1p)"
  "$TS" serve status --json > "$WORK/serve.json" 2>/dev/null || echo '{}' > "$WORK/serve.json"
  state="$("$PY" -I "$ROOT/scripts/kit.py" serve-state "$WORK/serve.json" "$DNS" "$PORT" "$HTTPS_PORT")"
  if [ "$state" = ours ] || [ "$state" = funnel ]; then
    "$TS" serve --https="$HTTPS_PORT" off && echo "unpublished https://$DNS:$HTTPS_PORT"
  else
    echo "Tailscale port $HTTPS_PORT does not point at this proxy; left alone"
  fi
fi

# Plugin
link="$HERMES_HOME/plugins/phone-approvals"
if [ -L "$link" ] && [ "$(readlink "$link")" = "$ROOT/plugin/phone-approvals" ]; then
  hermes plugins disable phone-approvals >/dev/null 2>&1 || true
  rm -f "$link" && echo "removed plugin link (quit and reopen the desktop app to unload it)"
fi

# Cron
id="$("$PY" -I "$ROOT/scripts/kit.py" cron-id "$HERMES_HOME/cron/jobs.json" "Phone app update check" 2>/dev/null)"
[ -n "$id" ] && hermes cron remove "$id" >/dev/null && echo "removed cron job $id"
rm -f "$HERMES_HOME/scripts/phone_app_update_check.sh"

echo
echo "Done. Hermes, its .env and $ROOT were not changed."
echo "On the iPhone, delete the Home Screen icon and, under Settings > Passwords, the passkey for this Mac."
