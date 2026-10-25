#!/bin/bash
# Install the Hermes phone app on this Mac. Safe to re-run: each step checks
# what is already there and leaves it alone.
#
#   ./install.sh
#
# Asks before anything that changes Hermes (.env, plugins, cron) or Tailscale.
# Never prints the API key, a bot token, or a Home Assistant token.
#
# Overrides (normally unset):
#   HERMES_HOME             Hermes home (default ~/.hermes)
#   HERMES_AGENT_DIR        Hermes checkout (default $HERMES_HOME/hermes-agent)
#   PHONE_APP_PORT          local proxy port (default 8643)
#   PHONE_APP_HTTPS_PORT    tailnet HTTPS port (default 8443)
#   PHONE_APP_LABEL         LaunchAgent label (default com.hermes.phone-chat)
#   PHONE_APP_SKIP_SERVE=1  do not touch Tailscale Serve (testing)
set -uo pipefail
export PATH="/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin:/opt/homebrew/bin:$PATH"

ROOT="$(cd "$(dirname "$0")" && pwd -P)"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
export HERMES_HOME   # every `hermes` command below acts on this home
HERMES_AGENT="${HERMES_AGENT_DIR:-$HERMES_HOME/hermes-agent}"
PORT="${PHONE_APP_PORT:-8643}"
HTTPS_PORT="${PHONE_APP_HTTPS_PORT:-8443}"
LABEL="${PHONE_APP_LABEL:-com.hermes.phone-chat}"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
CONFIG="$ROOT/config.json"
ENV_FILE="$HERMES_HOME/.env"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/phone-app-install.XXXX")"
trap 'rm -rf "$WORK"' EXIT

say()  { printf '\n== %s\n' "$*"; }
ok()   { printf '   ok: %s\n' "$*"; }
warn() { printf '   !! %s\n' "$*"; }
die()  { printf '\nStopped: %s\n' "$*"; exit 1; }
ask()  {  # question default(y|n) -> 0 for yes
  local reply hint="[y/N]"; [ "$2" = y ] && hint="[Y/n]"
  read -r -p "   $1 $hint " reply || reply=""
  reply="${reply:-$2}"
  case "$reply" in y|Y|yes|YES) return 0 ;; *) return 1 ;; esac
}
kit() { "$PY" -I "$ROOT/scripts/kit.py" "$@"; }

# --- 1. Where this copy lives ----------------------------------------------------
say "Checking this folder"
case "$ROOT" in
  *"Mobile Documents"*|*CloudDocs*|*OneDrive*|*Dropbox*)
    die "this folder is in a cloud-synced location ($ROOT). Sync damages git folders and the
proxy's private files. Clone it to ~/.hermes/phone-chat and run install.sh there." ;;
esac
[ "$(uname -s)" = Darwin ] || die "this installer is for macOS."
ok "$ROOT"

# --- 2. Hermes and its Python ------------------------------------------------------
say "Finding Hermes"
[ -d "$HERMES_HOME" ] || die "no Hermes home at $HERMES_HOME. Set HERMES_HOME if it lives elsewhere."
[ -d "$HERMES_AGENT" ] || die "no Hermes checkout at $HERMES_AGENT. Set HERMES_AGENT_DIR to it."
# scripts/py knows both layouts: <checkout>/venv (older Hermes) and the
# dependency environment Hermes's package manager selects (current Hermes).
PY="$(HERMES_AGENT_DIR="$HERMES_AGENT" /bin/bash "$ROOT/scripts/py" --which 2>/dev/null)" || PY=""
[ -n "$PY" ] && [ -x "$PY" ] || die "could not find Hermes's Python. Set HERMES_AGENT_DIR to the hermes-agent
checkout, or PHONE_APP_PYTHON to its interpreter, and re-run."
"$PY" -I -c 'import cryptography' 2>/dev/null \
  || die "Hermes's Python ($PY) has no 'cryptography' package, which Face ID needs. Run 'hermes update' and try again."
ok "Hermes home $HERMES_HOME"
ok "Python $("$PY" -I -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])') with cryptography"
HVER="$(git -C "$HERMES_AGENT" describe --tags --always 2>/dev/null || echo unknown)"
ok "Hermes version $HVER"

# --- 3. Hermes API server ---------------------------------------------------------
say "Checking the Hermes API server"
# Hermes starts its API server whenever API_SERVER_KEY is usable (16+ characters).
api_on=1
kit api-key-ok "$ENV_FILE" || api_on=0
API_PORT="$(kit env-get "$ENV_FILE" API_SERVER_PORT 2>/dev/null)"; API_PORT="${API_PORT:-8642}"
API_HOST="$(kit env-get "$ENV_FILE" API_SERVER_HOST 2>/dev/null)"
if [ -n "$API_HOST" ] && [ "$API_HOST" != 127.0.0.1 ] && [ "$API_HOST" != localhost ]; then
  warn "API_SERVER_HOST is $API_HOST. The phone app only needs 127.0.0.1; anything wider exposes the API."
fi
restart_needed=0
if [ "$api_on" = 0 ]; then
  echo "   The phone app talks to Hermes through its API server, which needs a key of 16+ characters."
  echo "   This adds API_SERVER_ENABLED=true and a new random API_SERVER_KEY to $ENV_FILE."
  echo "   The key is never shown. Other lines in .env are kept."
  ask "Turn on the API server?" y || die "the API server is required."
  kit env-add-api "$ENV_FILE" || die "could not update $ENV_FILE"
  restart_needed=1
else
  ok "API server enabled with a key"
fi
if [ "$restart_needed" = 1 ] || ! curl -fsS -m 3 "http://127.0.0.1:$API_PORT/health" >/dev/null 2>&1; then
  echo "   The Hermes gateway must be (re)started to serve the API on 127.0.0.1:$API_PORT."
  echo "   A restart ends any reply the gateway is running right now (Telegram included)."
  if ask "Run 'hermes gateway restart' now?" n; then
    hermes gateway restart || warn "restart failed; run it yourself"
    for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
      curl -fsS -m 2 "http://127.0.0.1:$API_PORT/health" >/dev/null 2>&1 && break; sleep 2
    done
  fi
  if curl -fsS -m 3 "http://127.0.0.1:$API_PORT/health" >/dev/null 2>&1; then
    ok "API answers on 127.0.0.1:$API_PORT"
  else
    warn "API is not answering yet. Run 'hermes gateway restart' later; the rest still installs."
  fi
else
  ok "API answers on 127.0.0.1:$API_PORT"
fi

# --- 4. Tailscale identity ------------------------------------------------------------
say "Checking Tailscale"
TS="$(command -v tailscale || true)"
[ -n "$TS" ] || TS=/Applications/Tailscale.app/Contents/MacOS/Tailscale
[ -x "$TS" ] || die "Tailscale CLI not found. Install Tailscale and sign in first."
"$TS" status --json > "$WORK/ts.json" 2>/dev/null || die "Tailscale is not running or not signed in."
kit ts-self "$WORK/ts.json" > "$WORK/self.txt" || die "could not read this Mac's tailnet name and login."
DNS="$(sed -n 1p "$WORK/self.txt")"
LOGIN="$(sed -n 2p "$WORK/self.txt")"
URL="https://$DNS:$HTTPS_PORT/"
ok "this Mac is $DNS"
printf '   Only your Tailscale login may open the app. '
kit allow-login "$ROOT/allowed-logins.txt" "$LOGIN"
echo "   (Add other logins to allowed-logins.txt, one per line. Devices tagged in the"
echo "    Tailscale admin console carry no login and are always refused.)"

# --- 5. Settings file -------------------------------------------------------------------
say "Writing config.json"
kit config-base "$CONFIG" "$PORT" "$API_PORT" "$HERMES_HOME"
chmod 600 "$CONFIG"
ok "proxy 127.0.0.1:$PORT -> Hermes 127.0.0.1:$API_PORT"

# --- 6. Alerts -------------------------------------------------------------------------------
say "Alerts (approval needed, reply finished)"
current="$(kit config-get "$CONFIG" alerts.channel)"
if [ -n "$current" ] && ! ask "Alerts already go to '$current'. Change that?" n; then
  ok "kept $current"
else
  has_tg=0; kit env-has "$ENV_FILE" TELEGRAM_BOT_TOKEN && has_tg=1
  has_ha=0; kit env-has "$ENV_FILE" HASS_TOKEN && has_ha=1
  echo "   Tapping an alert opens the app. Telegram and Home Assistant alerts are generic (no"
  echo "   chat text). Web push is encrypted end to end, so it shows the session title."
  echo "     w = web push to the Home Screen app (no other account needed)"
  [ "$has_tg" = 1 ] && echo "     t = Telegram, through the bot Hermes already uses"
  [ "$has_ha" = 1 ] && echo "     h = Home Assistant mobile app notification"
  echo "     n = no alerts"
  default=w
  read -r -p "   Choice [$default] " choice || choice=""
  choice="${choice:-$default}"
  case "$choice" in
    w|W)
      kit config-alerts "$CONFIG" "{\"channel\": \"webpush\", \"url\": \"$URL\"}" ;;
    t|T)
      [ "$has_tg" = 1 ] || die "no TELEGRAM_BOT_TOKEN in $ENV_FILE."
      home="$(kit env-get "$ENV_FILE" TELEGRAM_HOME_CHANNEL)"
      if [ -n "$home" ]; then
        echo "   Telegram alerts go to your Hermes home channel (/sethome) unless you give a chat id."
        read -r -p "   Chat id [home channel] " chat || chat=""
      else
        # In a private chat with the bot, the chat id is the user's own Telegram id.
        dm="$(kit telegram-dm "$ENV_FILE" 2>/dev/null)" || dm=""
        if [ -n "$dm" ]; then
          echo "   No home channel is set. Your private chat with the bot is the one Telegram id"
          echo "   in TELEGRAM_ALLOWED_USERS ($dm)."
          read -r -p "   Chat id [$dm] " chat || chat=""
          chat="${chat:-$dm}"
        else
          echo "   No home channel is set. Send /sethome to the bot in your private chat and re-run,"
          echo "   or enter your numeric Telegram user id (from @userinfobot)."
          read -r -p "   Chat id " chat || chat=""
        fi
        [ -n "$chat" ] || die "Telegram alerts need a chat id."
      fi
      if [ -n "$chat" ]; then
        kit config-alerts "$CONFIG" "{\"channel\": \"telegram\", \"chat_id\": \"$chat\", \"url\": \"$URL\"}"
      else
        kit config-alerts "$CONFIG" "{\"channel\": \"telegram\", \"url\": \"$URL\"}"
      fi ;;
    h|H)
      [ "$has_ha" = 1 ] || die "no HASS_URL/HASS_TOKEN in $ENV_FILE."
      read -r -p "   Notify service, e.g. mobile_app_my_iphone: " notify || notify=""
      kit config-alerts "$CONFIG" "{\"channel\": \"homeassistant\", \"notify\": \"$notify\", \"url\": \"$URL\"}" ;;
    *)
      kit config-alerts "$CONFIG" '{"channel": "none"}' ;;
  esac
  chan="$(kit config-get "$CONFIG" alerts.channel)"
  ok "alerts: $chan"
  if [ "$chan" = webpush ]; then
    echo "   On the iPhone: open the app from the Home Screen, tap the menu, Notifications: on,"
    echo "   allow, then Send test. (iOS offers notifications only to the Home Screen app.)"
  elif [ "$chan" != none ] && ask "Send one test alert now?" y; then
    (cd "$ROOT" && "$PY" -I serve.py --notify "Hermes phone app: test alert.") \
      && echo "   Sent. Check that it arrived on the phone." || warn "test alert failed; see $ROOT/serve.log"
  fi
fi

# --- 7. Background service -----------------------------------------------------------------
say "Starting the proxy (LaunchAgent $LABEL)"
listener="$(lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null | head -1)"
if [ -n "$listener" ] && ! launchctl print "gui/$(id -u)/$LABEL" 2>/dev/null | grep -q "pid = $listener"; then
  die "port $PORT is already used by another program (pid $listener). Set PHONE_APP_PORT to a free port."
fi
mkdir -p "$HOME/Library/LaunchAgents"
kit plist "$WORK/agent.plist" "$LABEL" "$ROOT" "$HERMES_AGENT"
if launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  sleep 1
fi
cp "$WORK/agent.plist" "$PLIST"
launchctl bootstrap "gui/$(id -u)" "$PLIST" || die "launchctl could not load $PLIST"
code=""
for _ in 1 2 3 4 5 6 7 8 9 10; do
  code="$(curl -s -o /dev/null -w '%{http_code}' -m 2 -H "Tailscale-User-Login: $LOGIN" "http://127.0.0.1:$PORT/")"
  [ "$code" = 200 ] && break; sleep 1
done
[ "$code" = 200 ] || die "the proxy did not answer on 127.0.0.1:$PORT. See $ROOT/serve.log"
nologin="$(curl -s -o /dev/null -w '%{http_code}' -m 2 "http://127.0.0.1:$PORT/")"
[ "$nologin" = 403 ] || die "the login lock did not refuse a request without a login (got $nologin)."
ok "proxy answers, and refuses requests without your login"

# --- 8. Tailscale Serve (tailnet only, never Funnel) -----------------------------------------
say "Publishing on your tailnet at $URL"
if [ "${PHONE_APP_SKIP_SERVE:-0}" = 1 ]; then
  warn "skipped (PHONE_APP_SKIP_SERVE=1)"
else
  "$TS" serve status --json > "$WORK/serve.json" 2>/dev/null || echo '{}' > "$WORK/serve.json"
  state="$(kit serve-state "$WORK/serve.json" "$DNS" "$PORT" "$HTTPS_PORT")"
  case "$state" in
    ours)   ok "already published, tailnet only" ;;
    funnel) die "port $HTTPS_PORT is published to the PUBLIC internet with Funnel. Turn that off
first: $TS funnel --https=$HTTPS_PORT off" ;;
    taken)  die "port $HTTPS_PORT is already used by another Serve handler. Set PHONE_APP_HTTPS_PORT
to a free port (8443, 10000 and 443 are the ports Tailscale allows) and re-run." ;;
    busy|empty)
      [ "$state" = busy ] && echo "   You already publish other things with Tailscale Serve. This adds port $HTTPS_PORT only."
      echo "   Command: $TS serve --bg --https=$HTTPS_PORT http://127.0.0.1:$PORT"
      if ask "Publish it?" y; then
        "$TS" serve --bg --yes --https="$HTTPS_PORT" "http://127.0.0.1:$PORT" \
          || die "Tailscale Serve failed. If it printed a link to enable Serve or HTTPS, open it and re-run."
        "$TS" serve status --json > "$WORK/serve-after.json" 2>/dev/null
        [ "$(kit serve-state "$WORK/serve-after.json" "$DNS" "$PORT" "$HTTPS_PORT")" = ours ] \
          || die "Serve did not end up as expected; check 'tailscale serve status'."
        if ! kit serve-kept "$WORK/serve.json" "$WORK/serve-after.json" "$HTTPS_PORT" > "$WORK/changed.txt"; then
          warn "Tailscale changed other entries while adding port $HTTPS_PORT:"
          sed 's/^/      /' "$WORK/changed.txt"
          echo "      Before (restore with 'tailscale serve' / 'tailscale funnel' if needed):"
          sed 's/^/      /' "$WORK/serve.json"
          die "check 'tailscale serve status' before going further."
        fi
        ok "published, tailnet only; nothing else in Serve or Funnel changed"
      else
        warn "not published; the phone cannot reach the app until you run the command above"
      fi ;;
  esac
fi

# --- 9. Face ID -----------------------------------------------------------------------------
say "Face ID unlock"
fstate="$(kit faceid-state "$ROOT/faceid.json" "$DNS")"
case "$fstate" in
  configured-enrolled) ok "a passkey is already enrolled for $DNS" ;;
  other-host)
    warn "faceid.json is set up for a different host name; existing passkeys will not work here."
    if ask "Reset Face ID for $DNS?" n; then
      rm -f "$ROOT/faceid.json" "$ROOT/faceid-setup.json"; fstate=none
    fi ;;
esac
if [ "$fstate" = none ] || [ "$fstate" = configured ]; then
  (cd "$ROOT" && "$PY" -I serve.py --faceid-config "$DNS" "https://$DNS:$HTTPS_PORT") >/dev/null \
    || die "could not write faceid.json"
  CODE_LINE="$(cd "$ROOT" && "$PY" -I serve.py --setup-code)"
  ok "ready for enrolment"
fi

# --- 10. Phone approvals plugin (optional) -------------------------------------------------
say "Phone approvals (optional)"
echo "   Lets the phone answer approval prompts raised in the desktop app or Telegram."
echo "   It uses Hermes internals, so it is tested against your Hermes version first."
mkdir -p "$WORK/plugin-test"
if (cd "$WORK" && HERMES_AGENT_DIR="$HERMES_AGENT" "$PY" -I "$ROOT/tests/plugin_test.py" "$ROOT" "$WORK/plugin-test" > "$WORK/plugin.out" 2>&1); then
  ok "plugin test passed on $HVER ($(tail -1 "$WORK/plugin.out"))"
  link="$HERMES_HOME/plugins/phone-approvals"
  if [ -L "$link" ] && [ "$(readlink "$link")" = "$ROOT/plugin/phone-approvals" ]; then
    ok "plugin already linked"
  elif [ -e "$link" ]; then
    warn "$link exists and is not this copy's plugin; left alone"
  elif ask "Install the phone approvals plugin?" y; then
    mkdir -p "$HERMES_HOME/plugins"
    ln -s "$ROOT/plugin/phone-approvals" "$link"
    ok "linked $link"
    PLUGIN_STEP=1
  fi
else
  warn "plugin test FAILED on $HVER; phone approvals were not installed. Everything else works."
  grep -E '^FAIL|Error' "$WORK/plugin.out" | head -5 | sed 's/^/      /'
fi

# --- 11. Skill for this Hermes ----------------------------------------------------------
say "Hermes skill (so your Hermes knows how to look after the app)"
skill_link="$HERMES_HOME/skills/hermes-phone-app"
if [ -L "$skill_link" ] && [ "$(readlink "$skill_link")" = "$ROOT/skill" ]; then
  ok "already linked"
elif [ -e "$skill_link" ]; then
  warn "$skill_link exists and is not this copy's skill; left alone"
elif ask "Link skill/ into $HERMES_HOME/skills?" y; then
  mkdir -p "$HERMES_HOME/skills"
  ln -s "$ROOT/skill" "$skill_link"
  ok "linked; new Hermes sessions will see it"
fi

# --- 12. Update check (optional) ------------------------------------------------------------
say "Hourly update check (optional)"
echo "   After 'hermes update', re-runs the tests and sends one alert if something broke."
wrapper="$HERMES_HOME/scripts/phone_app_update_check.sh"
if kit cron-id "$HERMES_HOME/cron/jobs.json" "Phone app update check" >/dev/null 2>&1; then
  ok "already scheduled"
elif ask "Schedule it with Hermes cron?" y; then
  mkdir -p "$HERMES_HOME/scripts"
  # Cron scripts must live in $HERMES_HOME/scripts; this one only hands over to the repo.
  printf '#!/bin/bash\nexec /bin/bash %q "$@"\n' "$ROOT/scripts/update-check.sh" > "$wrapper"
  chmod 700 "$wrapper"
  HERMES_HOME="$HERMES_HOME" hermes cron create "20 * * * *" --name "Phone app update check" \
    --script phone_app_update_check.sh --no-agent --deliver local >/dev/null \
    && ok "scheduled at 20 past each hour" || warn "could not create the cron job"
fi

# --- Done ---------------------------------------------------------------------------------------
say "Installed"
cat <<EOF

   Open on the iPhone (Tailscale connected):  $URL
   Then: Share > Add to Home Screen, and open the Home Screen icon.
EOF
if [ -n "${CODE_LINE:-}" ]; then
  cat <<EOF

   Face ID: the app asks for a setup code. $CODE_LINE
   It is valid for 10 minutes. New code later: cd "$ROOT" && scripts/py -I serve.py --setup-code
EOF
fi
if [ "${PLUGIN_STEP:-0}" = 1 ]; then
  cat <<EOF

   To turn on phone approvals:
     1. Quit the Hermes desktop app (Cmd-Q) if it is open, after any reply finishes.
     2. hermes plugins enable phone-approvals
     3. hermes gateway restart     (for Telegram approvals; ends a running Telegram reply)
     4. Reopen the desktop app.
EOF
fi
cat <<EOF

   Logs: $ROOT/serve.log     Remove: $ROOT/uninstall.sh
EOF
