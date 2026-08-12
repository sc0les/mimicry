#!/usr/bin/env python3
"""Send a daily Mimicry/IBKR heartbeat summary."""

from __future__ import annotations

import argparse
import datetime
import json
import time
from collections import Counter
from pathlib import Path

import alerts
import ibkr_contracts
import mirror
import mirror_ibkr
import reconcile


ROOT = Path(__file__).resolve().parent


def session_days_left() -> float | None:
    if not mirror.SESSION_FILE.exists():
        return None
    try:
        state = json.loads(mirror.SESSION_FILE.read_text())
    except Exception:
        return None
    for cookie in state.get("cookies", []):
        if cookie.get("name") == "__Secure-better-auth.session_token":
            expires_ts = cookie.get("expires", 0)
            return (expires_ts - time.time()) / 86400 if expires_ts else None
    return None


def todays_orders() -> list[dict]:
    today = datetime.date.today().isoformat()
    return [
        order
        for order in mirror_ibkr.load_execution_ledger()
        if order.get("date") == today
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description="Send daily Mimicry heartbeat")
    parser.add_argument("--date", default=datetime.date.today().isoformat())
    parser.add_argument("--drift-threshold", type=float, default=0.25)
    parser.add_argument("--no-alert", action="store_true")
    args = parser.parse_args()

    netliq = None
    positions = []
    drift_rows = []
    gateway_error = None
    try:
        netliq = mirror_ibkr.load_ibkr_net_liquidation()
        ib = ibkr_contracts.connect_ibkr()
        try:
            _summary, positions = ibkr_contracts.account_snapshot(ib)
        finally:
            ib.disconnect()
        target_rows = reconcile.current_target_rows(args.date, netliq)
        drift_rows = reconcile.position_drift_rows(target_rows, positions, netliq, args.drift_threshold)
    except Exception as exc:
        gateway_error = str(exc)

    orders = todays_orders()
    status_counts = Counter(order.get("final_status") or order.get("status") for order in orders)
    filled = sum(1 for order in orders if (order.get("filled_qty") or 0) and float(order.get("filled_qty") or 0) > 0)
    days_left = session_days_left()

    lines = [
        f"Mimicry daily heartbeat - {datetime.datetime.now().isoformat()}",
        f"Date: {args.date}",
        f"IBKR NetLiq: ${netliq:,.2f}" if netliq is not None else f"IBKR NetLiq: unavailable ({gateway_error})",
        f"Positions held: {len(positions)}",
        f"Orders today: {len(orders)} submitted/logged, {filled} with fills",
        "Order statuses: " + (", ".join(f"{key}={value}" for key, value in sorted(status_counts.items())) or "none"),
        f"Drift > {args.drift_threshold:.2f}%: {len(drift_rows)}",
        f"Tracker session: {days_left:.1f} days left" if days_left is not None else "Tracker session: unavailable",
    ]
    if drift_rows:
        lines.append("\nTop drift:")
        for row in drift_rows[:10]:
            lines.append(
                f"  {row['ticker']:12s} target={row['target_qty']:,.0f} actual={row['actual_qty']:,.0f} "
                f"drift={row['drift_pct']:.2f}%"
            )

    summary = "\n".join(lines)
    print(summary)
    if not args.no_alert:
        alerts.send_alert("Mimicry daily heartbeat", summary, priority=0)


if __name__ == "__main__":
    main()
