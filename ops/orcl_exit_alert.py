#!/usr/bin/env python3
"""Send a one-time ten-message ORCL exit alert through the Telegram bot.

The process is intentionally independent of the Codex app-server.  Cron runs
it every minute; the local-time and exchange-session guards make the useful
work happen only in the 15:50-15:54 America/New_York window on a regular 2026
session.  The 40-session time exit is exact.  The MA condition is an
early-warning estimate based on the live regular-market price shortly before
the close; the backtest's exact rule confirms a breach only at the completed
close and executes on the following open.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import statistics
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


NY = ZoneInfo("America/New_York")
ROOT = Path(__file__).resolve().parents[2]
DEPLOYED_ROOT = Path("/root/codex-telegram")
ENV_FILE = Path(os.environ.get("ORCL_ALERT_ENV_FILE", DEPLOYED_ROOT / ".env"))
STATE_FILE = Path(os.environ.get("ORCL_ALERT_STATE_FILE", DEPLOYED_ROOT / "orcl_exit_alert_state.json"))
LOCK_FILE = Path(os.environ.get("ORCL_ALERT_LOCK_FILE", DEPLOYED_ROOT / "orcl_exit_alert.lock"))
LOG_FILE = Path(os.environ.get("ORCL_ALERT_LOG_FILE", DEPLOYED_ROOT / "orcl_exit_alert.log"))

TICKER = "ORCL"
ENTRY_DATE = date(2026, 9, 14)
HARD_EXIT_DATE = date(2026, 11, 6)
MA_LENGTH = 40
MA_BUFFER = 0.03
MA_ACTIVATION_SESSIONS = 3
ALERT_HOUR = 15
ALERT_MINUTE_START = 50
ALERT_MINUTE_END = 54
ALERT_COUNT = 10
MESSAGE_DELAY_SECONDS = 0.15

# NYSE full-day closures relevant to this alert's 2026 lifetime.  The
# schedule exits before the November 27 and December 24 early closes.
NYSE_HOLIDAYS_2026 = {
    date(2026, 1, 1),
    date(2026, 1, 19),
    date(2026, 2, 16),
    date(2026, 4, 3),
    date(2026, 5, 25),
    date(2026, 6, 19),
    date(2026, 7, 3),
    date(2026, 9, 7),
    date(2026, 11, 26),
    date(2026, 12, 25),
}


def _load_env_file(path: Path) -> dict[str, str]:
    """Read simple KEY=value dotenv entries without printing their values."""

    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def _config() -> tuple[str, int]:
    file_values = _load_env_file(ENV_FILE)
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or file_values.get("TELEGRAM_BOT_TOKEN")
    chat_value = os.environ.get("ALLOWED_CHAT_ID") or file_values.get("ALLOWED_CHAT_ID")
    if not token or not chat_value:
        raise RuntimeError("Telegram credentials are unavailable")
    try:
        chat_id = int(chat_value)
    except ValueError as error:
        raise RuntimeError("Telegram chat configuration is invalid") from error
    return token, chat_id


def _logger() -> logging.Logger:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("orcl-exit-alert")
    if not logger.handlers:
        handler = logging.FileHandler(LOG_FILE)
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    try:
        os.chmod(LOG_FILE, 0o600)
    except FileNotFoundError:
        pass
    return logger


LOG = _logger()


def is_exchange_session(day: date) -> bool:
    return day.weekday() < 5 and day not in NYSE_HOLIDAYS_2026


def holding_session_count(day: date) -> int:
    if day < ENTRY_DATE:
        return 0
    current = ENTRY_DATE
    count = 0
    while current <= day:
        if is_exchange_session(current):
            count += 1
        current += timedelta(days=1)
    return count


def in_alert_window(local_now: datetime) -> bool:
    return (
        local_now.hour == ALERT_HOUR
        and ALERT_MINUTE_START <= local_now.minute <= ALERT_MINUTE_END
    )


def _yahoo_payload() -> dict:
    # Four months provides comfortably more than the 40 completed sessions
    # needed by the SMA while keeping the cron request small.
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/ORCL"
        "?range=4mo&interval=1d&events=div%2Csplits"
    )
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "KMGCapStocks/0.2 ORCL exit alert"},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        payload = json.loads(response.read().decode("utf-8"))
    error = (payload.get("chart") or {}).get("error")
    if error:
        raise RuntimeError("Yahoo chart request failed")
    result = ((payload.get("chart") or {}).get("result") or [None])[0]
    if not result:
        raise RuntimeError("Yahoo returned no ORCL chart")
    return result


def current_ma_snapshot(today: date) -> dict[str, float | int | str] | None:
    """Estimate today's close and calculate the 40-session MA threshold."""

    result = _yahoo_payload()
    meta = result.get("meta") or {}
    current_price = meta.get("regularMarketPrice")
    current_timestamp = meta.get("regularMarketTime")
    if current_price is None or current_timestamp is None:
        return None
    market_day = datetime.fromtimestamp(
        float(current_timestamp), timezone.utc
    ).astimezone(NY).date()
    if market_day != today:
        # Fail closed if the endpoint is serving a stale session or the market
        # is not publishing a current regular-session quote yet.
        return None

    timestamps = result.get("timestamp") or []
    quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    adjusted = ((result.get("indicators") or {}).get("adjclose") or [{}])[0].get("adjclose", [])
    rows: list[dict[str, float | date]] = []
    for index, stamp in enumerate(timestamps):
        raw_closes = quote.get("close") or []
        raw_close = raw_closes[index] if index < len(raw_closes) else None
        if raw_close is None:
            continue
        raw_close = float(raw_close)
        adjusted_close = adjusted[index] if index < len(adjusted) else None
        adjusted_close = float(adjusted_close) if adjusted_close is not None else raw_close
        session = datetime.fromtimestamp(float(stamp), timezone.utc).astimezone(NY).date()
        rows.append({"session": session, "raw_close": raw_close, "adjusted_close": adjusted_close})

    prior = [row for row in rows if row["session"] < today]
    if len(prior) < MA_LENGTH - 1:
        return None

    today_rows = [row for row in rows if row["session"] == today]
    if today_rows:
        factor = float(today_rows[-1]["adjusted_close"]) / float(today_rows[-1]["raw_close"])
    else:
        factor = float(prior[-1]["adjusted_close"]) / float(prior[-1]["raw_close"])
    estimated_adjusted_close = float(current_price) * factor
    closes = [float(row["adjusted_close"]) for row in prior[-(MA_LENGTH - 1):]]
    closes.append(estimated_adjusted_close)
    sma = statistics.fmean(closes)
    threshold = sma * (1.0 - MA_BUFFER)
    return {
        "current_price": float(current_price),
        "estimated_adjusted_close": estimated_adjusted_close,
        "sma": sma,
        "threshold": threshold,
        "holding_sessions": holding_session_count(today),
    }


def _state_load() -> dict:
    if not STATE_FILE.is_file():
        return {}
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return state if isinstance(state, dict) else {}


def _state_save(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = STATE_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, STATE_FILE)
    os.chmod(STATE_FILE, 0o600)


def _telegram_request(token: str, method: str, values: dict | None = None) -> dict:
    encoded = urllib.parse.urlencode(values or {}).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=encoded,
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not payload.get("ok"):
        raise RuntimeError("Telegram API rejected the request")
    return payload["result"]


def _reason_for(local_now: datetime) -> tuple[str, str, dict] | None:
    today = local_now.date()
    if not (ENTRY_DATE <= today <= HARD_EXIT_DATE):
        return None
    if not is_exchange_session(today) or not in_alert_window(local_now):
        return None

    if today == HARD_EXIT_DATE:
        return (
            "TIME_EXIT",
            "The fixed 40-session exit is due today. Close the full ORCL position at the regular NYSE close.",
            {"exit_date": today.isoformat(), "holding_sessions": holding_session_count(today)},
        )

    if holding_session_count(today) < MA_ACTIVATION_SESSIONS:
        return None
    snapshot = current_ma_snapshot(today)
    if not snapshot or snapshot["holding_sessions"] < MA_ACTIVATION_SESSIONS:
        return None
    if float(snapshot["estimated_adjusted_close"]) >= float(snapshot["threshold"]):
        return None
    return (
        "MA_EARLY_WARNING",
        "ORCL is currently below the provisional SMA(40) minus 3% threshold. The exact research rule confirms this only at today's completed close and exits at the next session open; closing now is an earlier manual exit.",
        snapshot,
    )


def _message(reason: str, explanation: str, details: dict, number: int) -> str:
    if reason == "TIME_EXIT":
        detail = (
            f"Scheduled date: {details['exit_date']}\n"
            f"Holding session: {details['holding_sessions']} of 40\n"
            "Timing: 10 minutes before the regular NYSE close (15:50 ET)."
        )
    else:
        detail = (
            f"Estimated ORCL price: {float(details['current_price']):.2f}\n"
            f"SMA(40): {float(details['sma']):.2f}\n"
            f"SMA threshold (97%): {float(details['threshold']):.2f}\n"
            f"Holding session: {int(details['holding_sessions'])}"
        )
    return (
        f"ORCL EXIT ALERT {number}/{ALERT_COUNT}\n\n"
        f"{explanation}\n\n"
        f"{detail}\n\n"
        "This alert is a research-rule reminder, not an automatic order. Verify your position and broker fill before acting."
    )


def send_burst(token: str, chat_id: int, reason: str, explanation: str, details: dict, state: dict) -> None:
    pending = state.get("pending")
    if not isinstance(pending, dict) or pending.get("reason") != reason:
        pending = {"reason": reason, "date": datetime.now(NY).date().isoformat(), "sent_count": 0}
        state["pending"] = pending
        _state_save(state)

    sent_count = int(pending.get("sent_count", 0))
    for number in range(sent_count + 1, ALERT_COUNT + 1):
        _telegram_request(token, "sendMessage", {"chat_id": chat_id, "text": _message(reason, explanation, details, number)})
        pending["sent_count"] = number
        _state_save(state)
        if number < ALERT_COUNT:
            time.sleep(MESSAGE_DELAY_SECONDS)

    state.pop("pending", None)
    state["completed"] = {
        "reason": reason,
        "date": datetime.now(NY).date().isoformat(),
        "sent_count": ALERT_COUNT,
    }
    _state_save(state)
    LOG.info("Sent %d-message %s burst", ALERT_COUNT, reason)


def run(local_now: datetime, *, dry_run: bool = False) -> int:
    reason = _reason_for(local_now)
    if reason is None:
        return 0
    state = _state_load()
    if state.get("completed"):
        return 0
    if dry_run:
        print(json.dumps({"reason": reason[0], "explanation": reason[1], "details": reason[2]}, sort_keys=True))
        return 0
    token, chat_id = _config()
    send_burst(token, chat_id, reason[0], reason[1], reason[2], state)
    return 0


def check_api() -> int:
    token, chat_id = _config()
    _telegram_request(token, "getMe")
    _telegram_request(token, "getChat", {"chat_id": chat_id})
    print("Telegram bot credentials and the configured allowed chat were accepted.")
    return 0


def parse_now(value: str | None) -> datetime:
    if value is None:
        return datetime.now(NY)
    parsed = datetime.fromisoformat(value)
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=NY)).astimezone(NY)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="evaluate without sending or changing state")
    parser.add_argument("--at", help="evaluate at an ISO timestamp, intended for tests")
    parser.add_argument("--check-api", action="store_true", help="validate bot token and target chat without sending a message")
    args = parser.parse_args()
    if args.check_api:
        return check_api()

    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_FILE.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        try:
            return run(parse_now(args.at), dry_run=args.dry_run)
        except Exception as error:  # cron must never emit a token or traceback
            LOG.error("ORCL alert run failed: %s", str(error).replace("TELEGRAM_BOT_TOKEN", "token"))
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
