#!/usr/bin/env python3
"""Small alerting helper for Mimicry cron jobs.

Pushover is the primary channel. If credentials are absent, alerts are printed
so local development and dry runs still work.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / "secrets.env")

PUSHOVER_API_URL = "https://api.pushover.net/1/messages.json"
ALERT_HEALTH_FILE = ROOT / "last_success_alert_self_test.json"
DEFAULT_ALERT_HEALTH_MAX_AGE_HOURS = 24.0


def pushover_configured() -> bool:
    return bool(os.environ.get("PUSHOVER_APP_TOKEN") and os.environ.get("PUSHOVER_USER_KEY"))


def send_alert(title: str, message: str, priority: int = 0) -> bool:
    title = title.strip()[:250] or "Mimicry alert"
    message = message.strip() or "(no message)"

    if not pushover_configured():
        print(f"[ALERT noop] {title}\n{message}", file=sys.stderr)
        return False

    payload = {
        "token": os.environ["PUSHOVER_APP_TOKEN"],
        "user": os.environ["PUSHOVER_USER_KEY"],
        "title": title,
        "message": message[:1024],
        "priority": str(priority),
    }
    data = urllib.parse.urlencode(payload).encode()
    req = urllib.request.Request(PUSHOVER_API_URL, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            body = json.loads(response.read().decode())
            return body.get("status") == 1
    except Exception as exc:
        print(f"[ALERT failed] {title}: {exc}", file=sys.stderr)
        return False


def record_self_test() -> None:
    ALERT_HEALTH_FILE.write_text(
        json.dumps(
            {
                "finished_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "channel": "pushover",
            },
            indent=2,
            sort_keys=True,
        )
    )


def alert_health_error(max_age_hours: float = DEFAULT_ALERT_HEALTH_MAX_AGE_HOURS) -> str | None:
    if not pushover_configured():
        return "Pushover credentials are not configured"
    if not ALERT_HEALTH_FILE.exists():
        return "Pushover has not completed a self-test"
    try:
        data = json.loads(ALERT_HEALTH_FILE.read_text())
        finished_at = datetime.datetime.fromisoformat(str(data["finished_at"]).replace("Z", "+00:00"))
        if finished_at.tzinfo is None:
            finished_at = finished_at.replace(tzinfo=datetime.timezone.utc)
    except Exception as exc:
        return f"Pushover self-test marker is invalid: {exc}"
    age_hours = (
        datetime.datetime.now(datetime.timezone.utc) - finished_at.astimezone(datetime.timezone.utc)
    ).total_seconds() / 3600
    if age_hours > max_age_hours:
        return f"Pushover self-test is stale ({age_hours:.1f} hours old)"
    return None


def require_recent_self_test(max_age_hours: float = DEFAULT_ALERT_HEALTH_MAX_AGE_HOURS) -> None:
    error = alert_health_error(max_age_hours=max_age_hours)
    if error:
        raise RuntimeError(error)


def main() -> None:
    parser = argparse.ArgumentParser(description="Send a Mimicry alert")
    parser.add_argument("--title")
    parser.add_argument("--priority", type=int, default=0)
    parser.add_argument("--message", help="Alert body. Defaults to stdin.")
    parser.add_argument("--message-file", help="Read alert body from a file.")
    parser.add_argument("--self-test", action="store_true", help="Send and record a Pushover health test")
    args = parser.parse_args()

    if args.self_test:
        if not pushover_configured():
            raise SystemExit("Pushover credentials are not configured")
        if not send_alert("Mimicry alert self-test", "Pushover delivery path is healthy."):
            raise SystemExit("Pushover self-test failed")
        record_self_test()
        print(f"Alert self-test recorded in {ALERT_HEALTH_FILE}")
        return
    if not args.title:
        parser.error("--title is required unless --self-test is used")

    if args.message_file:
        message = Path(args.message_file).read_text(errors="replace")
    elif args.message is not None:
        message = args.message
    else:
        message = sys.stdin.read()

    if not send_alert(args.title, message, priority=args.priority):
        raise SystemExit("alert delivery failed")


if __name__ == "__main__":
    main()
