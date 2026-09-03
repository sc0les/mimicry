#!/usr/bin/env python3
"""Reconcile IBKR orders and positions against Mimicry targets."""

from __future__ import annotations

import argparse
import datetime
import json
from collections import defaultdict
from pathlib import Path

import alerts
import ibkr_contracts
import mirror
import mirror_ibkr


ROOT = Path(__file__).resolve().parent
RECONCILE_LOG = ROOT / "reconcile.log"
RECONCILE_ALERT_STATE_FILE = ROOT / "reconcile_alert_state.json"
DEFAULT_DRIFT_THRESHOLD_PCT = 0.5

WORKING_STATUSES = {"PendingSubmit", "PreSubmitted", "Submitted"}
REJECTED_STATUSES = {"Cancelled", "ApiCancelled", "Inactive", "ValidationError", "skipped"}


def load_ledger() -> list[dict]:
    return mirror_ibkr.load_execution_ledger()


def latest_run_orders(ledger: list[dict]) -> list[dict]:
    if not ledger:
        return []
    run_ids = [entry.get("run_id") for entry in ledger if entry.get("run_id")]
    if run_ids:
        latest = max(run_ids)
        return [entry for entry in ledger if entry.get("run_id") == latest]
    latest_timestamp = max(entry.get("timestamp", "") for entry in ledger)
    return [entry for entry in ledger if entry.get("timestamp") == latest_timestamp]


def load_alert_state() -> dict:
    if not RECONCILE_ALERT_STATE_FILE.exists():
        return {"alerted_keys": []}
    try:
        data = json.loads(RECONCILE_ALERT_STATE_FILE.read_text())
    except Exception:
        return {"alerted_keys": []}
    if not isinstance(data, dict):
        return {"alerted_keys": []}
    data.setdefault("alerted_keys", [])
    return data


def save_alert_state(state: dict) -> None:
    RECONCILE_ALERT_STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True))


def order_alert_key(order: dict) -> str:
    return ":".join(
        str(part)
        for part in (
            order.get("run_id") or order.get("date"),
            order.get("order_id"),
            order.get("ticker"),
            order.get("classification"),
            order.get("filled_qty"),
            order.get("remaining_qty"),
        )
    )


def new_order_alerts(classified: list[dict]) -> list[dict]:
    state = load_alert_state()
    seen = set(state.get("alerted_keys", []))
    new_orders = []
    for order in classified:
        if order["classification"] not in {"partial", "rejected"}:
            continue
        key = order_alert_key(order)
        if key in seen:
            continue
        new_orders.append(order)
        seen.add(key)
    state["alerted_keys"] = sorted(seen)[-1000:]
    save_alert_state(state)
    return new_orders


def completed_and_open_orders(ib) -> tuple[dict[int, object], dict[int, object], dict[int, list]]:
    open_by_id = {}
    for trade in mirror_ibkr.all_open_trades(ib):
        order_id = getattr(trade.order, "orderId", None)
        if order_id is not None:
            open_by_id[int(order_id)] = trade

    completed_by_id = {}
    try:
        completed = ib.reqCompletedOrders(False)
    except Exception:
        completed = []
    for trade in completed or []:
        order_id = getattr(trade.order, "orderId", None)
        if order_id is not None:
            completed_by_id[int(order_id)] = trade

    executions_by_order_id = defaultdict(list)
    try:
        executions = ib.reqExecutions()
    except Exception:
        executions = []
    for execution_report in executions or []:
        execution = execution_report.execution
        order_id = getattr(execution, "orderId", None)
        if order_id is not None:
            executions_by_order_id[int(order_id)].append(execution)

    return open_by_id, completed_by_id, executions_by_order_id


def classify_order(entry: dict, trade=None, executions: list | None = None) -> dict:
    executions = executions or []
    status = entry.get("final_status") or entry.get("status") or "unknown"
    requested_qty = float(entry.get("requested_qty") or entry.get("qty") or 0)
    filled_qty = float(entry.get("filled_qty") or 0)
    remaining_qty = float(entry.get("remaining_qty") or max(0.0, requested_qty - filled_qty))
    avg_fill_price = entry.get("avg_fill_price")

    if trade is not None:
        status_obj = trade.orderStatus
        status = status_obj.status or status
        filled_qty = float(status_obj.filled or filled_qty)
        remaining_qty = float(status_obj.remaining or remaining_qty)
        avg_fill_price = float(status_obj.avgFillPrice or 0) or avg_fill_price

    if executions:
        filled_qty = sum(float(execution.shares or 0) for execution in executions)
        if filled_qty and requested_qty:
            remaining_qty = max(0.0, requested_qty - filled_qty)
        total_value = sum(float(execution.shares or 0) * float(execution.price or 0) for execution in executions)
        avg_fill_price = total_value / filled_qty if filled_qty else avg_fill_price

    if filled_qty >= requested_qty and requested_qty:
        state = "filled"
    elif filled_qty > 0 and remaining_qty > 0:
        state = "partial"
    elif status in WORKING_STATUSES:
        state = "working"
    elif status in REJECTED_STATUSES:
        state = "rejected"
    else:
        state = status.lower()

    return {
        **entry,
        "classification": state,
        "status": status,
        "final_status": status,
        "requested_qty": requested_qty,
        "filled_qty": filled_qty,
        "remaining_qty": remaining_qty,
        "avg_fill_price": avg_fill_price,
    }


def current_target_rows(date: str, portfolio_value: float) -> list[dict]:
    mirror.check_session_expiry()
    holdings = mirror.fetch_holdings(date)
    rows, _skipped = mirror_ibkr.build_rows(holdings, "all_equities", portfolio_value)
    return [row for row in rows if row.get("con_id") and row.get("skip_reason") is None]


def position_drift_rows(rows: list[dict], positions: list[dict], portfolio_value: float, threshold_pct: float) -> list[dict]:
    positions_by_con_id = {
        int(position["con_id"]): position
        for position in positions
        if position.get("con_id")
    }
    drifts = []
    target_con_ids = set()
    for row in rows:
        con_id = int(row["con_id"])
        target_con_ids.add(con_id)
        target_qty = float(row.get("target_qty") or 0)
        actual_qty = float(positions_by_con_id.get(con_id, {}).get("position", 0.0))
        if not target_qty:
            continue
        per_share_usd = row["target_value"] / abs(target_qty)
        drift_value = abs(actual_qty - target_qty) * per_share_usd
        drift_pct = drift_value / portfolio_value * 100 if portfolio_value else 0
        if drift_pct > threshold_pct:
            drifts.append({
                "ticker": row["ticker"],
                "con_id": con_id,
                "target_qty": target_qty,
                "actual_qty": actual_qty,
                "drift_pct": drift_pct,
                "drift_value": drift_value,
            })

    for position in positions:
        con_id = position.get("con_id")
        if not con_id or int(con_id) in target_con_ids:
            continue
        qty = float(position.get("position") or 0)
        if not qty:
            continue
        avg_cost = abs(float(position.get("avg_cost") or 0))
        drift_value = abs(qty) * avg_cost
        drift_pct = drift_value / portfolio_value * 100 if portfolio_value and drift_value else 0
        if drift_pct > threshold_pct:
            drifts.append({
                "ticker": position.get("symbol"),
                "con_id": int(con_id),
                "target_qty": 0.0,
                "actual_qty": qty,
                "drift_pct": drift_pct,
                "drift_value": drift_value,
            })

    return sorted(drifts, key=lambda item: -item["drift_pct"])


def append_report(text: str) -> None:
    with open(RECONCILE_LOG, "a") as handle:
        handle.write(text.rstrip() + "\n\n")


def update_ledger_with_classifications(ledger: list[dict], classified: list[dict]) -> None:
    by_identity = {}
    for order in classified:
        key = (order.get("run_id"), order.get("order_id"), order.get("ticker"), order.get("side"))
        by_identity[key] = order

    changed = False
    for entry in ledger:
        key = (entry.get("run_id"), entry.get("order_id"), entry.get("ticker"), entry.get("side"))
        update = by_identity.get(key)
        if not update:
            continue
        for field in (
            "classification",
            "status",
            "final_status",
            "requested_qty",
            "filled_qty",
            "remaining_qty",
            "avg_fill_price",
            "rejection_reason",
        ):
            if update.get(field) != entry.get(field):
                entry[field] = update.get(field)
                changed = True
        entry["reconciled_at"] = datetime.datetime.now().isoformat()
        changed = True

    if changed:
        mirror_ibkr.save_execution_ledger(ledger)


def main() -> None:
    parser = argparse.ArgumentParser(description="Reconcile IBKR mirror state")
    parser.add_argument("--date", default=datetime.date.today().isoformat())
    parser.add_argument("--drift-threshold", type=float, default=DEFAULT_DRIFT_THRESHOLD_PCT)
    parser.add_argument("--no-alert", action="store_true")
    args = parser.parse_args()

    portfolio_value = mirror_ibkr.load_ibkr_net_liquidation()
    ib = ibkr_contracts.connect_ibkr()
    try:
        _summary, positions = ibkr_contracts.account_snapshot(ib)
        open_by_id, completed_by_id, executions_by_order_id = completed_and_open_orders(ib)
    finally:
        ib.disconnect()

    ledger = load_ledger()
    run_orders = latest_run_orders(ledger)
    classified = []
    for entry in run_orders:
        order_id = entry.get("order_id")
        trade = None
        executions = []
        if order_id is not None:
            order_id = int(order_id)
            trade = open_by_id.get(order_id) or completed_by_id.get(order_id)
            executions = executions_by_order_id.get(order_id, [])
        classified.append(classify_order(entry, trade=trade, executions=executions))
    update_ledger_with_classifications(ledger, classified)

    target_rows = current_target_rows(args.date, portfolio_value)
    drifts = position_drift_rows(target_rows, positions, portfolio_value, args.drift_threshold)
    status_counts = defaultdict(int)
    for order in classified:
        status_counts[order["classification"]] += 1

    lines = [
        f"IBKR reconciliation - {datetime.datetime.now().isoformat()}",
        f"Date: {args.date}",
        f"NetLiq: ${portfolio_value:,.2f}",
        f"Latest run orders: {len(classified)}",
        "Order states: " + (", ".join(f"{key}={value}" for key, value in sorted(status_counts.items())) or "none"),
        f"Positions checked: {len(target_rows)}",
        f"Drift threshold: {args.drift_threshold:.2f}%",
        f"Drift breaches: {len(drifts)}",
    ]

    if classified:
        lines.append("\nLatest run order detail:")
        for order in classified[:80]:
            lines.append(
                f"  {order.get('classification'):8s} {order.get('side', '?'):4s} "
                f"{float(order.get('requested_qty') or 0):7.0f} {order.get('ticker', '?'):12s} "
                f"filled={float(order.get('filled_qty') or 0):.0f} remaining={float(order.get('remaining_qty') or 0):.0f} "
                f"status={order.get('status')}"
            )

    if drifts:
        lines.append("\nPosition drift:")
        for item in drifts[:50]:
            lines.append(
                f"  {item['ticker']:12s} target={item['target_qty']:,.0f} actual={item['actual_qty']:,.0f} "
                f"drift={item['drift_pct']:.2f}% (${item['drift_value']:,.0f})"
            )

    report = "\n".join(lines)
    print(report)
    append_report(report)

    newly_alertable_orders = new_order_alerts(classified)
    if (drifts or newly_alertable_orders) and not args.no_alert:
        alerts.send_alert("Mimicry IBKR reconciliation issue", report[:1024], priority=1)


if __name__ == "__main__":
    main()
