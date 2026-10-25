# Hermes phone app

A phone chat app for your own Hermes agent. It uses the same sessions as the Hermes desktop app, CLI and Telegram. You can read and continue any session, watch a reply's tool steps live, guide a running reply, stop it, answer approval prompts with Face ID, and get an alert when Hermes needs you or finishes.

It runs on the Mac that runs Hermes and is reachable only over your Tailscale tailnet. It is a web page added to the iPhone Home Screen. There is no App Store app and no cloud service.

## What you need

- macOS, with Hermes installed in `~/.hermes` and its gateway running. Tested on Hermes `v2026.9.24` and `v0.22.0`.
- Tailscale on the Mac and the iPhone, signed in to the same tailnet, with HTTPS certificates enabled for the tailnet (admin console → DNS → HTTPS Certificates).
- Alerts: web push to the Home Screen app needs nothing else (iOS 16.4+, best on 18.4+). Telegram (a bot already set up in Hermes) and Home Assistant also work, or no alerts.

## Install

```bash
git clone https://github.com/mstrangeguy/hermes-phone-app-kit.git ~/.hermes/phone-chat
cd ~/.hermes/phone-chat
./install.sh
```

Run `./install.sh` yourself, in a Terminal window on the Mac. It asks questions only you should answer, and it prints a Face ID setup code for your phone. Your Hermes can clone the repo, explain a question, or fix a failure, but if Hermes runs the installer itself it cannot answer the questions and takes every default.

Do not clone into iCloud Drive, Dropbox or OneDrive. Sync damages git folders, and the installer refuses those paths.

The installer asks before each change. In order, it:

1. Finds Hermes and its Python, and checks that the Python has `cryptography` (needed for Face ID). Older Hermes keeps it in `hermes-agent/venv`; current Hermes manages it itself. `scripts/py` finds either.
2. Checks the Hermes API server, which runs when `API_SERVER_KEY` in `~/.hermes/.env` is set (16+ characters). If it is not, it offers to add a random key and `API_SERVER_ENABLED=true`. The key is never shown. The gateway must restart to pick that up; the installer asks, because a restart ends any reply running at that moment, including on Telegram.
3. Reads your Tailscale login and writes it to `allowed-logins.txt`. Every request to the app must come from that login.
4. Writes `config.json` and asks where alerts go: web push (the default), Telegram (through the bot Hermes already uses), Home Assistant, or none. For Telegram it uses your Hermes home channel (`/sethome`), or else your private chat with the bot. It can send one test alert.
5. Installs a LaunchAgent, `com.hermes.phone-chat`, that keeps the proxy running on `127.0.0.1:8643`. It checks that the proxy answers and that it refuses a request without your login.
6. Publishes the proxy on your tailnet at `https://<this-mac>.<tailnet>.ts.net:8443/` with Tailscale Serve, tailnet only. It never uses Funnel. It stops if port 8443 is already used, and stops if adding it changed any other Serve or Funnel entry.
7. Sets up Face ID and prints a setup code, valid for 10 minutes.
8. Tests the optional phone-approvals plugin against your Hermes version and offers to install it only if the test passes.
9. Links `skill/` into `~/.hermes/skills`, so your Hermes knows how to look after the app.
10. Offers an hourly check that re-runs the tests after a Hermes update and alerts you once if something broke.

## On the iPhone

1. With Tailscale connected, open the URL the installer printed in Safari.
2. Share → Add to Home Screen. Open the app from the new icon.
3. Enter the setup code, then tap Set up Face ID. From then on the app unlocks with Face ID, and allowing a command needs Face ID again.
4. The menu button (or the word Hermes) opens a side panel with three screens, in the Hermes desktop sidebar's order, plus that screen's view options:

| Screen | What it shows |
|---|---|
| Sessions | Your own chats: ones started on the phone, in the desktop app or in a terminal. Pinned chats stay at the top whatever their source. Cron runs, Bot Chats and messaging-platform chats are left out, as in the desktop sidebar. |
| Bots | One row per profile the phone serves, opening that profile's Bot Chat (a dot shows when it is replying). A profile without one says "No Bot Chat yet"; Bot Chats are made in the desktop app. Below them, chats from messaging platforms (Telegram, Slack, ...) under one heading per platform, if there are any. |
| Cron jobs | One row per job with a state dot (accent scheduled, amber paused, grey finished, red failed). A job opens its schedule and its runs, newest first, labeled by time. A run opens its chat, or its saved output when there is no chat or the run failed. Read-only. |

View options → Show → **Cron runs and bot chats** (off by default, kept on the phone) puts cron runs and messaging chats back in the Sessions list, as before. The Source chips still filter on top of it.

Not on the phone (desktop only): Bot Mode group chats and rooms, bot avatars, creating or editing Bot Chats and cron jobs.

In a chat:

- **Search.** The box above the Sessions list searches chat titles and message text across every chat in the profiles in view, not just the loaded ones. Results replace the list until you clear the box.
- **Reactions.** Double-tap a message for the six Tapbacks (❤️ 👍 👎 😂 ‼️ ❓). Tap your own reaction to take it back. They are the desktop app's reactions, kept in Hermes, so both apps show them, and Hermes's own reactions appear too. Your reactions are passed to the agent with your next message, as the desktop does. They follow the desktop setting Settings → Appearance → message reactions (`display.message_reactions` in that profile's `config.yaml`); with it off, the phone hides them. The agent cannot react to a message sent from the phone yet: on the API server, Hermes looks the message up under the run, not the chat.
- **Task list.** When the agent keeps a task list, a card above the message box shows progress ("3/7") and the step in hand; tap it for the whole list. It shows while a reply runs, here or on the desktop, and stays after for that turn until you close it. Task-list steps in the chat read as progress and open as a checklist.
- **Pin and Archive** are in the session menu, next to Rename, Fork and Delete. Open it with ••• or by tapping the chat's title (handy when a phone's camera sits over the top-right corner). Archived chats are under View → Show → Archived.

If the code expired: `cd ~/.hermes/phone-chat && scripts/py -I serve.py --setup-code`

If the page does not load with iCloud Private Relay on, add this to the tailnet policy file (admin console → Access controls → JSON editor), as its own section beside `acls`. Then turn Tailscale off and on again on the phone.

```json
"nodeAttrs": [
  { "target": ["autogroup:member"], "attr": ["disable-split-dns-when-no-custom-resolvers"] }
]
```

## Phone approvals (optional)

Without the plugin, the phone answers approval prompts for replies it started. With it, the phone also answers prompts raised in the desktop app or on Telegram. A prompt in a terminal `hermes` chat is answered only in that terminal. Only Allow once and Deny are offered.

After the installer links the plugin:

1. Let any running reply finish, then quit the desktop app (Cmd-Q).
2. `hermes plugins enable phone-approvals`
3. `hermes gateway restart`, so Telegram prompts are covered too.
4. Reopen the desktop app.

The plugin uses Hermes internals (`tools.approval.list_gateway_approvals` and `resolve_gateway_approval`). A Hermes update can change them. If it does, a prompt answered from the phone is not applied and the prompt times out to Deny, as it would if nobody answered. The hourly check catches this.

## Settings: `config.json`

Written by the installer. Edit it by hand to change alerts; the alerts block is re-read every few seconds, so no restart is needed.

| Key | Meaning |
|---|---|
| `app_name` | Name shown in the app, on the Home Screen icon and in alerts, e.g. `Hermes Work`. Default `Hermes`. Up to 30 characters. Needs a proxy restart, then delete and re-add the Home Screen icon. |
| `accent_color` | Accent color for buttons, highlights and the app name in the list header, as `#rrggbb`, e.g. `#e5a50a`. Default blue, with a white name. Needs a proxy restart. |
| `port` | Local proxy port. Default 8643. Needs a proxy restart. |
| `upstream_port` | Hermes API port. Default 8642. |
| `hermes_home` | Hermes home. Default `~/.hermes`. |
| `alerts.channel` | `webpush`, `telegram`, `homeassistant` or `none`. |
| `alerts.chat_id` | Telegram chat id. Empty means your Hermes home channel (`TELEGRAM_HOME_CHANNEL`, set with `/sethome`). |
| `alerts.notify` | Home Assistant notify service, e.g. `mobile_app_my_iphone`. Uses `HASS_URL` and `HASS_TOKEN` from `.env`. |
| `alerts.url` | Link opened from the alert. Web push needs it: the Mac's `https://` address for this app. |
| `alerts.subject` | Web push only, optional. The contact Apple sees for this sender, `mailto:` or `https:`. Default: the origin of `alerts.url`. Apple refuses made-up contacts (`localhost`, `.invalid`). |

Telegram and Home Assistant alerts are generic, for example "Hermes is waiting for your approval." No commands, file names or chat text are sent through them.

### Web push

`webpush` sends alerts straight to the Home Screen app, with no other account. Turn it on from the phone: open the app from the Home Screen icon (iOS offers notifications only there, not in a Safari tab), tap the menu, **Notifications: on**, allow, then **Send test**. Each phone that turns it on gets every alert; **Notifications: off** stops it for that phone.

- Each alert shows the session's title and a short line (Reply finished, Waiting for your approval, Could not finish the reply). Tapping it opens that session, after Face ID. No commands or reply text are sent.
- The alert is encrypted on the Mac for the phone's own key (RFC 8291) and signed with a key made on this Mac (VAPID, RFC 8292). Apple's push service carries it but cannot read it. The Mac only connects out to the push service; nothing reaches the Mac from the internet.
- The Home Screen icon shows a badge with the number of unread sessions.
- iOS 18.4 or later shows the alert with no script running (Declarative Web Push). Older browsers use `page/sw.js`.
- iOS gives web push no Time Sensitive level, so these alerts do not break through a Focus. Approval alerts are sent with high urgency.
- If Tailscale is off on the phone, the alert still arrives, but the app opens only once Tailscale is back on.
- Deleting the Home Screen icon ends that phone's subscription. Turn notifications on again after re-adding it.
- The keys and the list of phones are in `webpush.json` (mode 600, never committed). Deleting it makes a new key; every phone must turn notifications on again.

## Security

This app is a remote control for an agent that can run commands on your Mac. Read this before installing.

- The proxy listens on `127.0.0.1` only. The only way in from outside is Tailscale Serve on your tailnet. There is no public URL.
- Every request must carry an allowed Tailscale login, which Tailscale Serve sets and clients cannot forge. Devices shared into your tailnet, or tagged devices, are refused.
- With Face ID enrolled, the phone never holds the Hermes API key. The Mac adds it to each request. Opening the app runs Face ID with no tap (iOS allows one such prompt per page load; if it refuses, tap Unlock). The app locks as soon as it leaves the screen, and after 10 minutes without a touch; replies keep running on the Mac. The Mac also ends an unlock after 30 minutes idle or 12 hours. Allow once needs a fresh Face ID check tied to that one prompt. Deny never needs it.
- The page loads nothing from other sites. Its content policy blocks inline scripts, and model output is cleaned before display.
- Attachments (the + in the message box opens the iOS picker: Photo Library, Take Photo, Choose File) are saved where the Hermes desktop app saves its own: pictures in `<Hermes home>/images/`, other files in `<Hermes home>/attachments/` under their own name (never overwriting; files 600). A photo is shrunk on the phone first, which also drops its location data. Files are sent unchanged, up to 25 MB. The message lists them as the desktop's `@image:` / `@file:` lines, so both apps show them from history; Hermes opens a file with its own tools when it reads the message. Delete old ones there whenever you like. Pictures Hermes sends back (`MEDIA:/path` in a reply) are fetched through the proxy: image files only, judged by their contents, from your home or temp folders only, behind the same login and Face ID checks. Paths are never logged.
- The proxy reads Hermes's session database directly for the list extras, cron runs and search (read-only), and writes to it in one place only: the reactions field of a message, when you react. Hermes's own API has no reaction or full-text search routes. Search queries are not logged.
- If this Mac is managed by an employer, check with them before opening a remote path into it.

## Everyday commands

```bash
cd ~/.hermes/phone-chat
launchctl kickstart -k gui/$(id -u)/com.hermes.phone-chat     # restart the proxy
tail -f serve.log                                              # proxy log (no keys or logins are logged)
scripts/py -I serve.py --notify "test"                        # send a test alert
scripts/update-check.sh --force                               # run every test now
git pull && launchctl kickstart -k gui/$(id -u)/com.hermes.phone-chat   # update
```

Run a single test suite (each one needs an empty scratch folder):

```bash
scripts/py -I tests/plugin_test.py "$PWD" "$(mktemp -d)"
```

## Uninstall

`./uninstall.sh` removes the LaunchAgent, the Tailscale port (only if it points at this proxy), the plugin and skill links, and the cron job. It leaves Hermes, `.env` and this folder alone. On the iPhone, delete the Home Screen icon and, in Settings → Passwords, the passkey for this Mac.

## Files

| Path | What |
|---|---|
| `serve.py` | The proxy: serves the page, checks the login and Face ID, forwards to Hermes, sends alerts. |
| `faceid.py` | Passkey checks. |
| `webpush.py` | Web push: encryption, signing, the list of subscribed phones. |
| `page/` | The app: HTML, JS, CSS, icon, plus local copies of marked and DOMPurify. |
| `plugin/phone-approvals/` | Optional Hermes plugin. |
| `scripts/` | `py` (finds Hermes's Python), installer helpers, the update check. |
| `tests/` | Offline tests. None of them contacts a real phone, bot, push service or Home Assistant. |
| `skill/SKILL.md` | Instructions for your Hermes agent to maintain this app. |

This app is MIT licensed (see `LICENSE`). Third-party code: marked (MIT) and DOMPurify (Apache-2.0 / MPL-2.0), licences in `page/vendor/`. The icon is Hermes's own (MIT), licence in `page/icon-LICENSE`.
