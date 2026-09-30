#!/usr/bin/env python3
"""Send the daily account, order, drift, and authentication heartbeat."""

from __future__ import annotations

import argparse
import datetime
import json
import os
import time
from collections import Counter

import alerts
import ibkr_contracts
import ibkr_state
import mirror
import mirror_ibkr


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


def todays_orders(connection, account_id: str, mode: str) -> list[dict]:
    local_tz = datetime.datetime.now().astimezone().tzinfo
    start = datetime.datetime.combine(
        datetime.date.today(),
        datetime.time.min,
        tzinfo=local_tz,
    ).astimezone(datetime.timezone.utc)
    end = start + datetime.timedelta(days=1)
    return [
        dict(row)
        for row in connection.execute(
            """
            SELECT orders.*
            FROM orders
            JOIN runs ON runs.run_id = orders.run_id
            WHERE orders.account_id = ?
              AND runs.mode = ?
              AND orders.created_at >= ?
              AND orders.created_at < ?
            ORDER BY orders.created_at
            """,
            (account_id, mode, start.isoformat(), end.isoformat()),
        ).fetchall()
    ]


def latest_reconciliation(connection, account_id: str, mode: str) -> dict | None:
    row = connection.execute(
        """
        SELECT *
        FROM reconciliations
        WHERE account_id = ? AND mode = ?
        ORDER BY completed_at DESC
        LIMIT 1
        """,
        (account_id, mode),
    ).fetchone()
    return dict(row) if row else None


def main() -> None:
    parser = argparse.ArgumentParser(description="Send daily Mimicry heartbeat")
    parser.add_argument(
        "--mode",
        choices=["paper", "live"],
        default=os.environ.get("IBKR_EXECUTION_MODE", "paper"),
    )
    parser.add_argument("--no-alert", action="store_true")
    args = parser.parse_args()

    connection = ibkr_state.connect()
    ib = ibkr_contracts.connect_ibkr()
    try:
        account_id = ibkr_contracts.require_account_id(ib)
        summary, positions = ibkr_contracts.account_snapshot(ib, account_id)
        netliq = mirror_ibkr.net_liquidation_from_summary(summary)
    finally:
        ib.disconnect()

    orders = todays_orders(connection, account_id, args.mode)
    statuses = Counter(str(order.get("status") or "unknown") for order in orders)
    submitted = sum(1 for order in orders if order.get("submitted_at"))
    completed = sum(
        1
        for order in orders
        if float(order.get("requested_qty") or 0)
        and float(order.get("filled_qty") or 0) >= float(order.get("requested_qty") or 0)
    )
    cancelled = sum(
        1
        for order in orders
        if str(order.get("status")) in {"Cancelled", "ApiCancelled", "Inactive"}
    )
    reconciliation = latest_reconciliation(connection, account_id, args.mode)
    days_left = session_days_left()
    lines = [
        f"Mimicry daily heartbeat - {datetime.datetime.now(datetime.timezone.utc).isoformat()}",
        f"Mode/account: {args.mode} / {account_id}",
        f"NetLiq: ${netliq:,.2f}",
        f"Positions held: {sum(1 for position in positions if position.get('position'))}",
        f"Orders today: {len(orders)} intents, {submitted} submitted, "
        f"{completed} filled, {cancelled} cancelled",
        "Order statuses: "
        + (", ".join(f"{key}={value}" for key, value in sorted(statuses.items())) or "none"),
        (
            f"Latest reconciliation: {reconciliation['status']}, "
            f"drift breaches={reconciliation['drift_count']}, "
            f"max drift={reconciliation['max_drift_pct']:.2f}%"
            if reconciliation
            else "Latest reconciliation: unavailable"
        ),
        (
            f"Tracker session: {days_left:.1f} days left"
            if days_left is not None
            else "Tracker session: unavailable"
        ),
    ]
    message = "\n".join(lines)
    print(message)
    ibkr_state.record_netliq(
        connection,
        mode=args.mode,
        account_id=account_id,
        kind="heartbeat",
        netliq=netliq,
    )
    connection.close()
    if not args.no_alert and not alerts.send_alert(
        "Mimicry daily heartbeat",
        message,
        priority=0,
    ):
        raise SystemExit("daily heartbeat alert delivery failed")


if __name__ == "__main__":
    main()
