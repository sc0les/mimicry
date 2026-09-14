#!/usr/bin/env python3
"""Deadman's switch for Mimicry cron jobs."""

from __future__ import annotations

import argparse
import datetime
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import alerts


ROOT = Path(__file__).resolve().parent
DEFAULT_MARKERS = [
    ROOT / "last_success_ibkr_execute_paper.json",
    ROOT / "last_success_alpaca_mirror.json",
]


def parse_hhmm(value: str) -> datetime.time:
    hour, minute = value.split(":", 1)
    return datetime.time(int(hour), int(minute))


def in_market_window(now: datetime.datetime, start: str, end: str) -> bool:
    if now.weekday() >= 5:
        return False
    start_time = parse_hhmm(start)
    end_time = parse_hhmm(end)
    return start_time <= now.time() <= end_time


def marker_finished_at(path: Path) -> datetime.datetime | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        finished = data.get("finished_at")
        if not finished:
            return None
        if finished.endswith("Z"):
            finished = finished[:-1] + "+00:00"
        return datetime.datetime.fromisoformat(finished)
    except Exception:
        return None


def main() -> None:
    parser = argparse.ArgumentParser(description="Alert if Mimicry cron jobs stop succeeding")
    parser.add_argument("--max-age-minutes", type=float, default=60.0)
    parser.add_argument("--timezone", default="America/New_York")
    parser.add_argument("--market-start", default="09:30")
    parser.add_argument("--market-end", default="16:00")
    parser.add_argument("--marker", action="append", help="Success marker path. Can be repeated.")
    parser.add_argument("--force", action="store_true", help="Check even outside market hours")
    parser.add_argument("--no-alert", action="store_true")
    args = parser.parse_args()

    tz = ZoneInfo(args.timezone)
    now = datetime.datetime.now(tz)
    if not args.force and not in_market_window(now, args.market_start, args.market_end):
        print(f"Outside market window ({now.isoformat()}); deadman check skipped")
        return

    marker_paths = [Path(path) for path in args.marker] if args.marker else DEFAULT_MARKERS
    stale = []
    for marker in marker_paths:
        finished = marker_finished_at(marker)
        if finished is None:
            stale.append(f"{marker.name}: no success marker")
            continue
        if finished.tzinfo is None:
            finished = finished.replace(tzinfo=tz)
        age_minutes = (now - finished.astimezone(tz)).total_seconds() / 60
        if age_minutes > args.max_age_minutes:
            stale.append(f"{marker.name}: last success {age_minutes:.1f} minutes ago")

    if stale:
        message = "\n".join(stale)
        print(message)
        if not args.no_alert:
            alerts.send_alert("Mimicry deadman alert", message, priority=1)
        raise SystemExit(1)

    print("Deadman check OK")


if __name__ == "__main__":
    main()
