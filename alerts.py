#!/usr/bin/env python3
"""Small alerting helper for Mimicry cron jobs.

Pushover is the primary channel. If credentials are absent, alerts are printed
so local development and dry runs still work.
"""

from __future__ import annotations

import argparse
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


def main() -> None:
    parser = argparse.ArgumentParser(description="Send a Mimicry alert")
    parser.add_argument("--title", required=True)
    parser.add_argument("--priority", type=int, default=0)
    parser.add_argument("--message", help="Alert body. Defaults to stdin.")
    parser.add_argument("--message-file", help="Read alert body from a file.")
    args = parser.parse_args()

    if args.message_file:
        message = Path(args.message_file).read_text(errors="replace")
    elif args.message is not None:
        message = args.message
    else:
        message = sys.stdin.read()

    send_alert(args.title, message, priority=args.priority)


if __name__ == "__main__":
    main()
