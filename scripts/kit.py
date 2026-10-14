#!/usr/bin/env python3
"""Helpers for install.sh, uninstall.sh and update-check.sh.

Nothing here prints a secret. `env-has` answers with its exit code, `env-get`
refuses any name not on a short non-secret list, and `env-add-api` generates
the API key and writes it to .env without it leaving this process.

Usage: kit.py <command> [args...]   (run with the Hermes venv Python, -I)
"""

import json
import os
import plistlib
import re
import secrets
import sys

NON_SECRET = {"API_SERVER_ENABLED", "API_SERVER_PORT", "API_SERVER_HOST",
              "TELEGRAM_HOME_CHANNEL", "TELEGRAM_ALLOWED_USERS"}


def read_env(path):
    found = {}
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                name, value = line.split("=", 1)
                name = name.strip()
                if name.startswith("export "):
                    name = name[len("export "):].strip()
                found[name] = value.strip().strip('"').strip("'")
    except OSError:
        pass
    return found


def env_has(path, name):
    return 0 if read_env(path).get(name) else 1


def key_usable(value):
    """Hermes starts the API server only with a key of 16+ characters that is
    not a placeholder (gateway/config.py _has_usable_api_server_key)."""
    value = (value or "").strip()
    return len(value) >= 16 and value.lower() not in {"changeme", "your-key-here", "xxx", "placeholder"}


def api_key_ok(path):
    return 0 if key_usable(read_env(path).get("API_SERVER_KEY")) else 1


def env_get(path, name):
    if name not in NON_SECRET:
        print("refusing to print %s" % name, file=sys.stderr)
        return 2
    print(read_env(path).get(name, ""))
    return 0


def env_add_api(path):
    """Turn the API server on and give it a key, keeping every other line."""
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
        mode = os.stat(path).st_mode & 0o777
    except FileNotFoundError:
        lines, mode = [], 0o600
    current = read_env(path)
    wanted = {"API_SERVER_ENABLED": "true"}
    if not key_usable(current.get("API_SERVER_KEY")):
        wanted["API_SERVER_KEY"] = secrets.token_urlsafe(32)
    out, done = [], set()
    for line in lines:
        name = line.split("=", 1)[0].strip()
        if name.startswith("export "):
            name = name[len("export "):].strip()
        if "=" in line and not line.lstrip().startswith("#") and name in wanted:
            out.append("%s=%s" % (name, wanted[name]))
            done.add(name)
        else:
            out.append(line)
    for name, value in wanted.items():
        if name not in done:
            out.append("%s=%s" % (name, value))
    tmp = path + ".tmp-phone-app"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode or 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write("\n".join(out) + "\n")
    os.chmod(tmp, mode or 0o600)
    os.replace(tmp, path)
    print("API server enabled" + (" and key created" if "API_SERVER_KEY" in wanted else ""))
    return 0


def telegram_dm(path):
    """Print the one numeric id in TELEGRAM_ALLOWED_USERS, if there is exactly one.
    In a private chat with the bot, Telegram's chat id is that user's id."""
    ids = [part.strip() for part in (read_env(path).get("TELEGRAM_ALLOWED_USERS") or "").split(",") if part.strip()]
    if len(ids) == 1 and re.match(r"^[0-9]{3,20}$", ids[0]):
        print(ids[0])
        return 0
    return 1


def ts_self(status_path):
    """Two lines from `tailscale status --json`: this Mac's DNS name and its login."""
    with open(status_path, encoding="utf-8") as handle:
        data = json.load(handle)
    me = data.get("Self") or {}
    dns = str(me.get("DNSName") or "").rstrip(".")
    user = (data.get("User") or {}).get(str(me.get("UserID"))) or {}
    print(dns)
    print(str(user.get("LoginName") or "").lower())
    return 0 if dns and user.get("LoginName") else 1


def serve_state(status_path, dns, port, https_port):
    """What `tailscale serve status --json` says about our HTTPS port:
    ours   - already proxies to this proxy, tailnet only
    funnel - proxies to us but is public: refuse
    taken  - something else uses the port
    busy   - port free, but other Serve or Funnel config exists
    empty  - nothing is served"""
    try:
        with open(status_path, encoding="utf-8") as handle:
            data = json.load(handle) or {}
    except (OSError, ValueError):
        data = {}
    tcp = data.get("TCP") or {}
    web = data.get("Web") or {}
    funnel = data.get("AllowFunnel") or {}
    key = "%s:%s" % (dns, https_port)
    if str(https_port) in tcp:
        handlers = (web.get(key) or {}).get("Handlers") or {}
        if handlers == {"/": {"Proxy": "http://127.0.0.1:%s" % port}}:
            print("funnel" if funnel.get(key) else "ours")
        else:
            print("taken")
    elif tcp or web or funnel:
        print("busy")
    else:
        print("empty")
    return 0


def serve_kept(before_path, after_path, https_port):
    """Exit 0 when every Serve/Funnel entry that existed before is unchanged
    after adding our port. `tailscale serve --bg` has been seen to move a
    Funnel port; this catches that so the installer can stop and say so."""
    def read(path):
        try:
            with open(path, encoding="utf-8") as handle:
                return json.load(handle) or {}
        except (OSError, ValueError):
            return {}
    before, after = read(before_path), read(after_path)
    changed = []
    for section in ("TCP", "Web", "AllowFunnel"):
        old, new = before.get(section) or {}, after.get(section) or {}
        for key, value in old.items():
            if new.get(key) != value:
                changed.append("%s %s" % (section, key))
        for key in new:
            if key not in old and not (key == str(https_port) or key.endswith(":%s" % https_port)):
                changed.append("%s %s (new)" % (section, key))
    for item in changed:
        print(item)
    return 1 if changed else 0


def load(path):
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save(path, data):
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    os.replace(tmp, path)


def config_base(path, port, upstream_port, hermes_home):
    data = load(path)
    data.update({"port": int(port), "upstream_port": int(upstream_port), "hermes_home": hermes_home})
    save(path, data)
    return 0


def config_get(path, key):
    value = load(path)
    for part in key.split("."):
        value = value.get(part) if isinstance(value, dict) else None
    print("" if value is None else value)
    return 0


def config_alerts(path, block_json):
    block = json.loads(block_json)
    data = load(path)
    data["alerts"] = block
    save(path, data)
    return 0


def allow_login(path, login):
    login = login.strip().lower()
    if not re.match(r"^[^\s#]+$", login):
        print("bad login", file=sys.stderr)
        return 2
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except FileNotFoundError:
        lines = ["# Tailscale logins allowed to open the phone app, one per line."]
    present = any(line.strip().lower() == login for line in lines)
    if not present:
        lines.append(login)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    os.chmod(path, 0o600)
    print("present" if present else "added")
    return 0


def plist(out, label, root, agent_dir=""):
    """scripts/py finds Hermes's Python at every start, so a Hermes update
    that moves it does not leave the service pointing at a deleted file."""
    log = os.path.join(root, "serve.log")
    data = {
        "Label": label,
        "ProgramArguments": ["/bin/bash", os.path.join(root, "scripts", "py"), "-I",
                             os.path.join(root, "serve.py"), "--foreground"],
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 30,
        "WorkingDirectory": root,
        "StandardOutPath": log,
        "StandardErrorPath": log,
    }
    if agent_dir:
        data["EnvironmentVariables"] = {"HERMES_AGENT_DIR": agent_dir}
    with open(out, "wb") as handle:
        plistlib.dump(data, handle)
    return 0


def faceid_state(path, rp_id):
    """configured-enrolled | configured | other-host | none"""
    data = load(path)
    if not data.get("rp_id"):
        print("none")
    elif data["rp_id"] != rp_id:
        print("other-host")
    else:
        print("configured-enrolled" if data.get("credentials") else "configured")
    return 0


def cron_id(jobs_path, name):
    data = load(jobs_path) if jobs_path.endswith(".json") else {}
    jobs = data.get("jobs", []) if isinstance(data, dict) else []
    for job in jobs:
        if isinstance(job, dict) and job.get("name") == name:
            print(job.get("id", ""))
            return 0
    return 1


COMMANDS = {
    "env-has": env_has, "env-get": env_get, "env-add-api": env_add_api, "api-key-ok": api_key_ok,
    "telegram-dm": telegram_dm,
    "ts-self": ts_self, "serve-state": serve_state, "serve-kept": serve_kept,
    "config-base": config_base, "config-get": config_get, "config-alerts": config_alerts,
    "allow-login": allow_login, "plist": plist, "faceid-state": faceid_state, "cron-id": cron_id,
}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print("usage: kit.py {%s} ..." % ",".join(sorted(COMMANDS)), file=sys.stderr)
        sys.exit(2)
    sys.exit(COMMANDS[sys.argv[1]](*sys.argv[2:]))
