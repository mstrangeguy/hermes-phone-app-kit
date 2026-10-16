#!/bin/bash
# Hourly (Hermes cron, no agent): after Hermes or its Python changes, re-run
# the phone app's tests and send one alert through the configured channel if
# any fail. Unchanged and already passed: exits silently in well under a second.
#
# Fingerprint = Hermes commit + venv Python version + cryptography version.
# A failure alerts once per fingerprint; a later pass clears it.
#
# Usage: update-check.sh [--force] [--simulate-fail]
set -uo pipefail
export PATH="/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

ROOT="$(cd "$(dirname "$0")/.." && pwd -P)"
HERMES_HOME="$(/usr/bin/plutil -extract hermes_home raw -o - "$ROOT/config.json" 2>/dev/null || true)"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"; HERMES_HOME="${HERMES_HOME/#\~/$HOME}"
HERMES_AGENT="${HERMES_AGENT_DIR:-$HERMES_HOME/hermes-agent}"
# Looked up every run: a Hermes update can move its Python.
PY="$(HERMES_HOME="$HERMES_HOME" HERMES_AGENT_DIR="$HERMES_AGENT" /bin/bash "$ROOT/scripts/py" --which 2>/dev/null)" || PY=""
STATE="$ROOT/update-check.json"

force=0; simulate=0
for arg in "$@"; do
  case "$arg" in
    --force) force=1 ;;
    --simulate-fail) simulate=1; force=1 ;;
    *) echo "unknown option: $arg"; exit 2 ;;
  esac
done

commit=$(git -C "$HERMES_AGENT" rev-parse --short=12 HEAD 2>/dev/null || echo unknown)
pyver=$("$PY" -I -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])' 2>/dev/null || echo missing)
crypto=$("$PY" -I -c 'import cryptography; print(cryptography.__version__)' 2>/dev/null || echo missing)
fp="hermes=$commit python=$pyver cryptography=$crypto"

SPY="${PY:-/usr/bin/python3}"   # state file only; plain stdlib
read_state() { "$SPY" -I -c 'import json,sys
try: print(json.load(open(sys.argv[1])).get(sys.argv[2], ""))
except Exception: print("")' "$STATE" "$1"; }
last_pass=$(read_state passed); last_fail=$(read_state failed)

[ "$force" = 0 ] && [ "$fp" = "$last_pass" ] && exit 0

write_state() {  # passed failed
  "$SPY" -I -c 'import json,os,sys,time
path, passed, failed = sys.argv[1:4]
fd = os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as f:
    json.dump({"passed": passed, "failed": failed, "checked": time.strftime("%Y-%m-%d %H:%M")}, f)
os.replace(path + ".tmp", path)' "$STATE" "$1" "$2"
}

alert() {  # message: through serve.py, so it uses whatever channel config.json names
  if [ -n "$PY" ] && [ -x "$PY" ]; then
    (cd "$ROOT" && "$PY" -I serve.py --notify "$1") || true
  else
    echo "cannot alert: Hermes Python missing"
  fi
}

work=$(mktemp -d "${TMPDIR:-/tmp}/phone-app-check.XXXX")
trap 'rm -rf "$work"' EXIT
results=""; ok=1
run_test() {  # name
  local dir="$work/$1" out code line
  mkdir -p "$dir"
  out=$(cd "$work" && HERMES_AGENT_DIR="$HERMES_AGENT" "$PY" -I "$ROOT/tests/$1_test.py" "$ROOT" "$dir" 2>&1)
  code=$?
  line=$(printf '%s\n' "$out" | grep -E '[0-9]+(/[0-9]+)? passed' | tail -1)
  if [ $code -ne 0 ] || [ -z "$line" ]; then
    ok=0; results="$results $1: FAILED (${line:-no result}, exit $code);"
    printf '%s\n' "$out" | grep -E '^FAIL|Error|Traceback' | head -5 | sed "s/^/  $1: /"
  else
    results="$results $1: $line;"
  fi
}

if [ -z "$PY" ] || [ ! -x "$PY" ]; then
  ok=0; results=" Hermes Python not found;"
else
  for t in plugin faceid approvals alerts busy profiles cron look; do run_test "$t"; done
fi
[ "$simulate" = 1 ] && { ok=0; results="$results simulated failure;"; }

if [ "$ok" = 1 ]; then
  write_state "$fp" ""
  echo "Phone app tests passed after a Hermes change ($fp):$results"
  exit 0
fi

echo "Phone app tests FAILED ($fp):$results"
if [ "$simulate" = 1 ]; then
  alert "(test) Hermes changed and a phone app test failed. Ask Hermes to check."
  exit 1
fi
if [ "$fp" != "$last_fail" ]; then
  alert "Hermes changed and a phone app test failed. Phone approvals may deny until fixed. Ask Hermes to check."
fi
write_state "$last_pass" "$fp"
exit 1
