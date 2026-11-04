# What the Hermes phone app does

Your own Hermes, on your iPhone. It uses the same chats as the Hermes desktop app, CLI and
Telegram, runs on your Mac, and is reachable only over your Tailscale network. There is no App
Store app and no cloud service. Setup and settings: [README.md](README.md).

## Chat

- **Every chat, one app.** Read and continue chats started on the phone, in the desktop app or in
  a terminal.
- **Watch it work.** A reply's thinking and tool steps show live as they happen; tap one to see
  the result.
- **Guide or stop a running reply.** Add guidance while it works, or stop it.
- **Pictures and files, both ways.** The + button opens Photo Library, Take Photo or Files.
  Photos are shrunk on the phone and lose their location data; files up to 25 MB. Pictures
  Hermes sends back show in the chat with pinch-to-zoom.
- **Task list card.** When Hermes keeps a task list, a card above the message box shows progress
  (for example "3/7") and, on its own line under the bar, the name of the step it's on (the one
  running, or the one it waits on); tap it for the whole list.
  The headline is a weighted % (steps Hermes marks as bigger count more) with the step count
  beside it, over a bar with one segment per step. It turns red with Stopped or Failed when a
  reply started on the phone ends early (every segment red), and green when done. When Hermes
  waits for your answer, or a reply ends with steps still open, it says Waiting on you and only
  the step it waits on turns yellow; finished steps keep the normal colour. A step that hit an
  error while the job went on is red on its own. The running step's stripes move, and a
  finished step fills in over a second. The card stays put: while a reply runs and has no task
  list yet it says "Planning…" with moving grey stripes and the time so far, and a reply that
  writes no list brings the previous one back, folded, as "Previous tasks".
- **"Files changed" card** for each reply that edited files, like the desktop's.
- **One-tap answers.** When Hermes asks a plain yes/no question, **Yes, go ahead** and **Not
  now** buttons appear. Never shown for locks, the alarm, the thermostat, the garage, a siren or
  a camera.
- **Reactions and Copy.** Double-tap a message for the six Tapbacks. Shared with the desktop app,
  and passed to Hermes with your next message. **Copy** in the same bar copies the whole message,
  even a folded one (iOS has no Select All on a page).
- **Long messages fold.** Long messages you sent fold to a few lines with **Show all**, like the
  desktop's.
- **Date dividers.** Long chats show the date above the first message of each day (Today and
  Yesterday by name), like the desktop's.
- **Send into Desktop chats** (Hermes v0.22+). Text and pictures go into any chat; the reply
  appears when Hermes saves it.

## Approvals and questions

- **Approve from the phone with Face ID.** Allow once needs a fresh Face ID check for that one
  prompt; Deny never does.
- **Desktop and Telegram prompts too** (optional plugin). Approvals raised on the desktop or in
  Telegram can be answered on the phone.
- **Question cards.** The desktop's question cards (choices, several choices or free text) show
  on the phone. Answer there and the desktop card closes.

## Alerts

- **Know when Hermes needs you or is done.** Alerts for: reply finished, waiting for your
  approval, a question waiting 45 seconds, and could not finish.
- **Web push straight to the Home Screen app**, with no other account. Encrypted on your Mac so
  Apple's push service can't read it. Tapping an alert opens that chat after Face ID.
- **Or Telegram or Home Assistant**, or no alerts. Those alerts stay generic: no commands, file
  names or chat text.
- **Optional reply preview**: the first sentence of a finished reply in the alert.
- **Unread badge** on the Home Screen icon, shared with the desktop's read state.
- **Use it as a backup alert channel** for other monitors on the Mac (`serve.py --notify`).

## Finding things

- **Sessions, Bots and Cron jobs screens**, in the same order as the desktop sidebar.
- **Search** chat titles and message text across every chat, not just the loaded ones.
- **Pin, Archive, Rename, Fork and Delete** from the chat menu.
- **Sort and group** the list (including by project), and filter by source.
- **Every profile** on the Mac, each with its own Bot Chat.
- **Cron jobs** (read-only): schedule, every run, its chat or saved output, failures in red.
- **Pull to refresh** on every list. New desktop chats appear within about 5 seconds.

## Status screen (optional)

- **Glance cards** from any local script or monitor (OK / warning / bad), plus a card of failed
  cron jobs. Says so if the monitor writing the cards has stopped.

## Security

- **Tailscale only.** No public address; every request must come from your own Tailscale login.
- **Face ID** to open the app; it locks when it leaves the screen and after 10 minutes idle.
- **The phone never holds the Hermes key.** The Mac adds it to each request.
- **Nothing loaded from other sites**, and model output is cleaned before it's shown.
- **"Can't reach the Mac" page** instead of a Safari error when Tailscale is off or the Mac is
  asleep. The phone keeps only that page, never chat text.

## Looking after itself

- **One installer** that asks before every change and checks its own work.
- **Hourly self-test** after a Hermes update; alerts you once if something broke.
- **Your Hermes knows how to maintain it** (skill installed with the app).
- **Your name and colour**: set the app name and accent colour per install.

## Not on the phone (desktop only)

Bot Mode group chats and rooms, bot avatars, creating or editing Bot Chats and cron jobs.
Desktop-started chats on Hermes v0.22+ have no live steps, Stop or guidance yet (planned).
