#!/usr/bin/env python3
"""Reconcile IBKR order state and account positions against Mimicry."""

from __future__ import annotations

import argparse
import datetime
import os
from collections import Counter, defaultdict
from pathlib import Path

import alerts
import ibkr_contracts
import ibkr_execution
import ibkr_state
import mirror
import mirror_ibkr


ROOT = Path(__file__).resolve().parent
RECONCILE_LOG = ROOT / "reconcile.log"
DEFAULT_DRIFT_THRESHOLD_PCT = 0.5
WORKING_STATUSES = {"PendingSubmit", "PreSubmitted", "Submitted"}
REJECTED_STATUSES = {
    "Cancelled",
    "ApiCancelled",
    "Inactive",
    "ValidationError",
    "validation_error",
    "submission_unknown",
}


def classify_order(order: dict) -> str:
    requested = float(order.get("requested_qty") or 0)
    filled = float(order.get("filled_qty") or 0)
    remaining = float(order.get("remaining_qty") or max(0.0, requested - filled))
    status = str(order.get("status") or "unknown")
    if requested and filled >= requested:
        return "filled"
    if filled > 0 and remaining > 0:
        return "partial"
    if status in WORKING_STATUSES:
        return "working"
    if status in REJECTED_STATUSES:
        return "rejected"
    return status.lower()


def signed_working_quantities(ib, account_id: str) -> dict[int, float]:
    working = defaultdict(float)
    for trade in ibkr_execution.owned_open_trades(ib, account_id):
        con_id = int(getattr(trade.contract, "conId", 0) or 0)
        if not con_id:
            continue
        remaining = float(
            getattr(trade.orderStatus, "remaining", 0)
            or getattr(trade.order, "totalQuantity", 0)
            or 0
        )
        working[con_id] += remaining if trade.order.action.upper() == "BUY" else -remaining
    return dict(working)


def position_drifts(
    rows: list[dict],
    positions: list[dict],
    quotes: dict[int, ibkr_contracts.Quote],
    fx_rates: dict[str, float],
    working: dict[int, float],
    netliq: float,
    threshold_pct: float,
) -> tuple[list[dict], list[str]]:
    positions_by_con_id = {
        int(position["con_id"]): position
        for position in positions
        if position.get("con_id") and position.get("sec_type") in {"STK", "OPT"}
    }
    target_con_ids = set()
    drifts = []
    errors = []
    for row in rows:
        con_id = int(row.get("con_id") or 0)
        if not con_id:
            if abs(float(row.get("weight") or 0)) > threshold_pct:
                errors.append(
                    f"{row.get('ticker')}: target has no qualified IBKR contract "
                    f"({row.get('skip_reason') or 'unknown reason'})"
                )
            continue
        target_con_ids.add(con_id)
        if ibkr_execution.market_is_open(row) is False:
            continue
        quote = quotes.get(con_id)
        quote_error = ibkr_execution.quote_validation_error(quote)
        fx_rate = ibkr_execution.finite_positive(fx_rates.get(row.get("currency")))
        if quote_error or fx_rate is None:
            errors.append(
                f"{row.get('ticker')}: {quote_error or 'missing executable FX'}"
            )
            continue
        current_qty = float(positions_by_con_id.get(con_id, {}).get("position", 0))
        force_exit, _reason = ibkr_execution.option_expiry_action(row, current_qty)
        price = (float(quote.bid) + float(quote.ask)) / 2
        target_qty = 0 if force_exit else ibkr_execution.target_quantity(
            row,
            netliq,
            price,
            fx_rate,
        )
        effective_qty = current_qty + float(working.get(con_id, 0))
        drift_value = ibkr_execution.order_notional_usd(
            effective_qty - target_qty,
            price,
            row,
            fx_rate,
        )
        drift_pct = drift_value / netliq * 100 if netliq else 0
        if drift_pct > threshold_pct:
            drifts.append({
                "ticker": row.get("ticker"),
                "con_id": con_id,
                "target_qty": target_qty,
                "actual_qty": current_qty,
                "working_qty": float(working.get(con_id, 0)),
                "effective_qty": effective_qty,
                "drift_value": drift_value,
                "drift_pct": drift_pct,
            })

    for position in positions:
        con_id = int(position.get("con_id") or 0)
        qty = float(position.get("position") or 0)
        if (
            not con_id
            or con_id in target_con_ids
            or qty == 0
            or position.get("sec_type") not in {"STK", "OPT"}
        ):
            continue
        if ibkr_execution.market_is_open(position) is False:
            continue
        quote = quotes.get(con_id)
        quote_error = ibkr_execution.quote_validation_error(quote)
        fx_rate = ibkr_execution.finite_positive(fx_rates.get(position.get("currency")))
        if quote_error or fx_rate is None:
            errors.append(
                f"{position.get('local_symbol') or position.get('symbol')}: "
                f"{quote_error or 'missing executable FX'}"
            )
            continue
        price = (float(quote.bid) + float(quote.ask)) / 2
        effective_qty = qty + float(working.get(con_id, 0))
        drift_value = ibkr_execution.order_notional_usd(
            effective_qty,
            price,
            position,
            fx_rate,
        )
        drift_pct = drift_value / netliq * 100 if netliq else 0
        if drift_pct > threshold_pct:
            drifts.append({
                "ticker": position.get("local_symbol") or position.get("symbol"),
                "con_id": con_id,
                "target_qty": 0,
                "actual_qty": qty,
                "working_qty": float(working.get(con_id, 0)),
                "effective_qty": effective_qty,
                "drift_value": drift_value,
                "drift_pct": drift_pct,
            })
    return sorted(drifts, key=lambda item: -item["drift_pct"]), errors


def append_report(report: str) -> None:
    with RECONCILE_LOG.open("a") as handle:
        handle.write(report.rstrip() + "\n\n")


def new_rejected_orders(connection, orders: list[dict]) -> list[dict]:
    new_rejections = []
    for order in orders:
        if classify_order(order) != "rejected":
            continue
        key = f"reconcile_alerted_order:{order['intent_id']}"
        if ibkr_state.get_meta(connection, key):
            continue
        ibkr_state.set_meta(connection, key, ibkr_state.utc_now())
        new_rejections.append(order)
    connection.commit()
    return new_rejections


def main() -> None:
    parser = argparse.ArgumentParser(description="Reconcile IBKR mirror state")
    parser.add_argument("--date", default=datetime.date.today().isoformat())
    parser.add_argument(
        "--mode",
        choices=["paper", "live"],
        default=os.environ.get("IBKR_EXECUTION_MODE", "paper"),
    )
    parser.add_argument("--holdings-json")
    parser.add_argument("--drift-threshold", type=float, default=DEFAULT_DRIFT_THRESHOLD_PCT)
    parser.add_argument("--no-alert", action="store_true")
    args = parser.parse_args()

    connection = ibkr_state.connect()
    ibkr_state.import_legacy_ledger(connection)
    ib = ibkr_contracts.connect_ibkr()
    try:
        account_id = ibkr_contracts.require_account_id(ib)
        account_is_paper = account_id.upper().startswith("DU")
        if (args.mode == "paper") != account_is_paper:
            raise RuntimeError(
                f"reconcile mode {args.mode!r} does not match configured account {account_id}"
            )
        ibkr_execution.recover_broker_orders(connection, ib, account_id)
        summary, positions = ibkr_contracts.account_snapshot(ib, account_id)
        netliq = mirror_ibkr.net_liquidation_from_summary(summary)

        if args.holdings_json:
            holdings = mirror_ibkr.load_holdings_from_file(args.holdings_json)
        else:
            mirror.check_session_expiry()
            holdings = mirror.fetch_holdings(args.date)
        rows, skipped = mirror_ibkr.build_rows(holdings, "all_equities", netliq)
        plans_by_con_id = mirror_ibkr.qualify_execution_rows(ib, rows)
        positions = mirror_ibkr.qualify_held_positions(ib, positions, plans_by_con_id)
        plans = list(plans_by_con_id.values())
        quotes = ibkr_contracts.snapshot_quotes(ib, plans)
        currencies = {plan.currency for plan in plans}
        fx_quotes = ibkr_contracts.snapshot_fx_quotes(ib, currencies)
        fx_rates, fx_errors = ibkr_execution.executable_fx_rates(currencies, fx_quotes)
        working = signed_working_quantities(ib, account_id)
        drifts, market_errors = position_drifts(
            rows,
            positions,
            quotes,
            fx_rates,
            working,
            netliq,
            args.drift_threshold,
        )

        orders = ibkr_state.latest_run_orders(
            connection,
            mode=args.mode,
            account_id=account_id,
        )
        states = Counter(classify_order(order) for order in orders)
        new_rejections = new_rejected_orders(connection, orders)
        market_errors.extend(
            f"{currency}: {error}" for currency, error in sorted(fx_errors.items())
        )
        market_errors.extend(
            f"{item.get('ticker')}: {item.get('skip_reason')}"
            for item in skipped
            if abs(float(item.get("weight") or 0)) > args.drift_threshold
        )
        run_id = orders[0]["run_id"] if orders else None

        lines = [
            f"IBKR reconciliation - {datetime.datetime.now(datetime.timezone.utc).isoformat()}",
            f"Mode/account: {args.mode} / {account_id}",
            f"Source date: {args.date}",
            f"NetLiq: ${netliq:,.2f}",
            f"Latest run orders: {len(orders)}",
            "Order states: "
            + (", ".join(f"{key}={value}" for key, value in sorted(states.items())) or "none"),
            f"Owned working orders: {len(working)} contracts",
            f"Drift threshold: {args.drift_threshold:.2f}%",
            f"Unexpected drift breaches: {len(drifts)}",
            f"Pricing/contract errors: {len(market_errors)}",
        ]
        if orders:
            lines.append("\nLatest run order detail:")
            for order in orders[:80]:
                lines.append(
                    f"  {classify_order(order):9s} {order.get('action', '?'):4s} "
                    f"{float(order.get('requested_qty') or 0):7.0f} "
                    f"{str(order.get('ticker') or '?'):18s} "
                    f"filled={float(order.get('filled_qty') or 0):.0f} "
                    f"remaining={float(order.get('remaining_qty') or 0):.0f} "
                    f"status={order.get('status')}"
                )
        if drifts:
            lines.append("\nUnexpected position drift:")
            for item in drifts[:80]:
                lines.append(
                    f"  {str(item['ticker']):18s} target={item['target_qty']:,.0f} "
                    f"actual={item['actual_qty']:,.0f} working={item['working_qty']:,.0f} "
                    f"drift={item['drift_pct']:.2f}% (${item['drift_value']:,.0f})"
                )
        if market_errors:
            lines.append("\nPricing/contract errors:")
            lines.extend(f"  {error}" for error in market_errors[:80])

        report = "\n".join(lines)
        print(report)
        append_report(report)
        has_issue = bool(drifts or market_errors or new_rejections)
        technical_failure = bool(market_errors or new_rejections)
        reconciliation_status = (
            "failed"
            if technical_failure
            else "drift"
            if drifts
            else "completed"
        )
        ibkr_state.record_reconciliation(
            connection,
            mode=args.mode,
            account_id=account_id,
            netliq=netliq,
            drift_count=len(drifts),
            max_drift_pct=max((item["drift_pct"] for item in drifts), default=0),
            status=reconciliation_status,
            report=report,
            run_id=run_id,
        )
        ibkr_state.record_netliq(
            connection,
            mode=args.mode,
            account_id=account_id,
            kind="reconciliation",
            netliq=netliq,
            run_id=run_id,
        )
        if has_issue and not args.no_alert:
            if not alerts.send_alert(
                "Mimicry IBKR reconciliation issue",
                report[:1024],
                priority=1,
            ):
                raise RuntimeError("reconciliation alert delivery failed")
        if technical_failure:
            raise SystemExit(1)
    finally:
        ib.disconnect()
        connection.close()


if __name__ == "__main__":
    main()
