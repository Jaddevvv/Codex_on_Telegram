#!/usr/bin/env python3
"""Independent cgroup OOM watcher that reports memory kills to Telegram."""

import json
import logging
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path


CGROUP_ROOT = Path("/sys/fs/cgroup")
SERVICE_CGROUP = os.environ.get(
    "CODEX_SERVICE_CGROUP",
    "/user.slice/user-0.slice/user@0.service/app.slice/codex-telegram.service",
)
STATE_FILE = Path(os.environ.get("CODEX_OOM_WATCH_STATE", "/root/codex-telegram/oom_watch.state"))
LOG_FILE = Path(os.environ.get("CODEX_OOM_WATCH_LOG", "/root/codex-telegram/oom_watch.log"))
POLL_SECONDS = 2


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()],
)
LOG = logging.getLogger("codex-telegram-oom-watch")


def read_events():
    path = CGROUP_ROOT / SERVICE_CGROUP.lstrip("/") / "memory.events"
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return None

    events = {}
    for line in lines:
        key, separator, value = line.partition(" ")
        if separator:
            try:
                events[key] = int(value)
            except ValueError:
                continue
    return events


def read_state():
    try:
        state = json.loads(STATE_FILE.read_text())
    except (OSError, ValueError, TypeError):
        return {"oom": 0, "oom_kill": 0, "oom_group_kill": 0}
    return {
        key: int(state.get(key, 0))
        for key in ("oom", "oom_kill", "oom_group_kill")
    }


def write_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = STATE_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, sort_keys=True) + "\n")
    temporary.replace(STATE_FILE)


def load_dotenv(path):
    """Load only simple KEY=VALUE entries when systemd did not provide them."""
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key, value.strip().strip("\"'"))


def send_alert(events):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("ALLOWED_CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError("TELEGRAM_BOT_TOKEN or ALLOWED_CHAT_ID is missing")

    message = (
        "⚠️ Codex memory guard triggered.\n\n"
        "A high-memory worker was stopped before it could exhaust the VPS.\n"
        f"cgroup OOM events: {events.get('oom', 0)}; "
        f"killed workers: {events.get('oom_kill', 0) + events.get('oom_group_kill', 0)}.\n\n"
        "The current Codex task may be incomplete. The Telegram bridge is protected and remains available."
    )
    payload = urllib.parse.urlencode({"chat_id": chat_id, "text": message}).encode()
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload,
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        result = json.loads(response.read().decode())
    if not result.get("ok"):
        raise RuntimeError(f"Telegram error: {result}")


def main():
    load_dotenv(Path(os.environ.get("CODEX_ENV_FILE", "/root/codex-telegram/.env")))
    last_warned = read_state()
    missing_logged_at = 0.0

    while True:
        events = read_events()
        if events is None:
            now = time.monotonic()
            if now - missing_logged_at >= 60:
                LOG.warning("Telegram service cgroup memory.events is unavailable: %s", SERVICE_CGROUP)
                missing_logged_at = now
            time.sleep(POLL_SECONDS)
            continue

        current = {
            key: events.get(key, 0)
            for key in ("oom", "oom_kill", "oom_group_kill")
        }
        kills_increased = any(
            current[key] > last_warned.get(key, 0)
            for key in ("oom_kill", "oom_group_kill")
        )
        oom_increased = current["oom"] > last_warned.get("oom", 0)
        if kills_increased or oom_increased:
            try:
                send_alert(current)
            except Exception:
                LOG.exception("Could not deliver the cgroup OOM alert to Telegram")
                time.sleep(15)
                continue
            LOG.error("Reported cgroup OOM event to Telegram: %s", current)
            last_warned = current
            write_state(last_warned)
        elif any(current[key] < last_warned.get(key, 0) for key in last_warned):
            # systemd may recreate the service cgroup after a restart.
            last_warned = current
            write_state(last_warned)

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
