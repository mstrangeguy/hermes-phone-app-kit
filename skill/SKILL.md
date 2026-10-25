---
name: hermes-phone-app
description: "Use when installing, fixing, or updating the Hermes phone app."
version: 1.0.0
license: MIT
platforms: [macos]
metadata:
  hermes:
    tags: [hermes, phone, tailscale, faceid, approvals]
---

# Hermes phone app

A phone chat page for this Hermes, served by a small proxy (`serve.py`) on `127.0.0.1:8643` and published tailnet-only by Tailscale Serve on `:8443`. The checkout is normally `~/.hermes/phone-chat` (this skill is linked from its `skill/` folder). `README.md` there is the user guide; this file is the maintenance rules.

## Installing

The user runs `./install.sh` himself in a Terminal window on the Mac. You may clone the repo (`git clone https://github.com/mstrangeguy/hermes-phone-app-kit.git ~/.hermes/phone-chat`), then tell him the command. Do not run `install.sh` from your own terminal tool: it has no keyboard, so every question silently takes its default and the user agrees to nothing. Do not answer its questions for him. Afterwards you can explain a question, read `serve.log`, or fix a failed step.

Run Python for this app with `scripts/py`, never a hardcoded interpreter path. It finds Hermes's own Python on older installs (`hermes-agent/venv`) and on current ones (managed by Hermes's package manager), which a Hermes update can move.

## Rules

- Never print, log, or commit `API_SERVER_KEY`, `TELEGRAM_BOT_TOKEN`, `HASS_TOKEN`, or the contents of `allowed-logins.txt`, `faceid.json`, `config.json`. Check a secret is set with `scripts/kit.py env-has`, never by reading it.
- Never run `tailscale funnel` for this port, and never bind the proxy to anything but `127.0.0.1`. The login check trusts `Tailscale-User-Login` only because Tailscale Serve is the sole way in.
- Do not run `tailscale serve reset`. Before changing Serve, save `tailscale serve status --json`. Afterwards, compare it with `scripts/kit.py serve-kept`.
- Do not add an origin to `API_SERVER_CORS_ORIGINS`. The page works because the proxy strips `Origin`.
- Do not add an inline `<script>`, `<style>`, `on*=` handler or `style=""` to `page/index.html`. The content policy blocks them silently. A new page file must be added to `STATIC` in `serve.py`, or it returns 404.
- Restart the proxy with `launchctl kickstart -k gui/$(id -u)/com.hermes.phone-chat`, never by running `serve.py` by hand: that would fight launchd for the port. If the session you are in came through the phone page, a restart cuts it. Do proxy work from the CLI, the desktop app or Telegram.
- Editing a `page/` file takes effect on the next page load. Editing `serve.py` or `faceid.py` needs a restart. The `alerts` block of `config.json` is re-read every 3 seconds.
- `hermes plugins enable/disable phone-approvals` only with the desktop app quit: enabling while it runs makes the backend rediscover every plugin mid-turn.

## Tests

Run every suite with Hermes's venv Python, each with its own empty scratch folder:

```bash
for t in plugin faceid approvals alerts busy profiles cron look pictures bots chat; do
  scripts/py -I tests/${t}_test.py "$PWD" "$(mktemp -d)" | tail -1
done
```

- `bots_test.py` runs `page/lists.js` (main-list, Bots and Cron rules) in node: from `PATH`, `$NODE`, or the copy Hermes keeps beside its checkout (`<Hermes home>/node/bin/node`). Keep list rules in `lists.js`, not `app.js`, so they stay testable.
- Re-run `plugin_test.py` after every `hermes update`. It drives Hermes's real approval code; set `HERMES_AGENT_DIR` to test another checkout.
- Any test importing `serve` or `faceid` must point `serve.CONFIG_PATH`, `faceid.CONFIG_PATH` and `faceid.SETUP_PATH` into its scratch folder. Otherwise a live enrolled passkey locks the API-key path and the test fails for the wrong reason. Keep the `FAIL ISOLATION` guard in new tests.
- Never test alerts by starting a real run. `alerts_test.py` uses fake Home Assistant and Telegram servers. To check the real channel once: `serve.py --notify "test"`.

## Troubleshooting

- **403 "limited to its owner's Tailscale login":** the phone's Tailscale login is not in `allowed-logins.txt`. Lines starting with `#` are comments.
- **"Locked" loop or Face ID fails:** the passkey is bound to the host name in `faceid.json`. If the Mac's tailnet name changed, re-run `install.sh` and choose to reset Face ID, then enrol again with a new setup code.
- **Page does not load on the phone with iCloud Private Relay on:** the tailnet policy is missing the `disable-split-dns-when-no-custom-resolvers` node attribute (see README). This Mac cannot open its own tailnet name, so a local test proves nothing; the phone is the test.
- **A phone message waits forever:** another reply is running in that session (`session_turn_leases` in `state.db`), or it is paused on an approval. It continues when that turn ends.
- **"alert failed: HTTP 400/401/403"** in `serve.log`: wrong chat id, the bot is blocked by the user, or the token is wrong. Telegram bots can only message chats that messaged them first.
- **The proxy will not start after a Hermes update** (`serve.log` says it cannot find Hermes's Python): run `scripts/py --which`. If it fails, set `HERMES_AGENT_DIR` or `PHONE_APP_PYTHON` in the LaunchAgent, or re-run `./install.sh`.
- **Approval prompt in a terminal `hermes` chat does not show on the phone:** expected. Hermes answers terminal prompts in that terminal; the plugin only sees desktop and messaging prompts.
- **Desktop approval answered on the phone but nothing happened:** check `agent.log` for `phone-approvals:` lines and run `plugin_test.py`. If it fails after a Hermes update, disable the plugin until fixed; prompts then work only on the desktop.
