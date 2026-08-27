#!/usr/bin/env python3
"""Dry-run/execute IBKR mirror for Mimicry equity coverage."""

from __future__ import annotations

import argparse
import datetime
import json
import math
import os
import time
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

import alerts
import ibkr_contracts
import mirror


MAX_LOT_MULTIPLE = 2.0
IBKR_STATE_FILE = Path(__file__).parent / "last_state_ibkr.json"
IBKR_EXECUTION_LEDGER_FILE = Path(__file__).parent / "ibkr_execution_ledger.json"
KILLSWITCH_FILE = Path(__file__).parent / "KILLSWITCH"
LAST_SUCCESS_FILE = Path(__file__).parent / "last_success_ibkr.json"
RECONCILE_REPORT_FILE = Path(__file__).parent / "reconcile.log"
BUY_LIMIT_BUFFER = 1.03
SELL_LIMIT_BUFFER = 0.97
LIVE_LIMIT_BUFFER_PCT = 0.015
PAPER_LIMIT_BUFFER_PCT = 0.02
LIVE_MAX_ORDERS_PER_RUN = 20
LIVE_MAX_NOTIONAL_PER_ORDER = 5_000.0
LIVE_MAX_DAILY_ORDERS = 60
LIVE_SINGLE_ORDER_NETLIQ_HALT_PCT = 0.05
LIVE_RUN_NOTIONAL_NETLIQ_HALT_PCT = 0.25
LIVE_NETLIQ_HALT_PCT = 0.10
NETLIQ_ALERT_PCT = 0.03
DRIFT_ALERT_PCT = 0.5
FX_USD_FALLBACK = {
    "AUD": 0.65,
    "CAD": 0.73,
    "CHF": 1.10,
    "DKK": 0.15,
    "EUR": 1.08,
    "GBP": 1.25,
    "HKD": 0.13,
    "JPY": 0.0065,
    "NOK": 0.095,
    "SEK": 0.094,
    "TWD": 0.031,
    "USD": 1.0,
}
ORDER_TICK_FALLBACK = {
    "AEB": 0.01,
    "ASX": 0.01,
    "BVME": 0.01,
    "EBS": 0.01,
    "ENEXT.BE": 0.01,
    "HEX": 0.01,
    "IBIS": 0.01,
    "LSE": 0.01,
    "OSE": 0.01,
    "SBF": 0.01,
    "SEHK": 0.01,
    "SFB": 0.01,
    "TSE": 0.01,
    "VENTURE": 0.01,
}
LSE_TICK_RULES = [
    (500.0, 0.1),
    (200.0, 0.05),
    (100.0, 0.02),
    (50.0, 0.01),
    (20.0, 0.005),
    (10.0, 0.002),
    (5.0, 0.001),
    (2.0, 0.0005),
    (1.0, 0.0002),
    (0.5, 0.0001),
    (0.2, 0.00005),
    (0.1, 0.00002),
    (0.05, 0.00001),
    (0.02, 0.000005),
    (0.01, 0.000002),
    (0.0, 0.000001),
]
OSE_TICK_RULES = [
    (200.0, 0.1),
    (100.0, 0.05),
    (50.0, 0.02),
    (20.0, 0.01),
    (10.0, 0.005),
    (5.0, 0.002),
    (2.0, 0.001),
    (1.0, 0.0005),
    (0.5, 0.0002),
    (0.0, 0.0001),
]
SBF_TICK_RULES = [
    (200.0, 0.2),
    (100.0, 0.1),
    (50.0, 0.05),
    (20.0, 0.02),
    (10.0, 0.01),
    (5.0, 0.005),
    (2.0, 0.002),
    (1.0, 0.001),
    (0.5, 0.0005),
    (0.2, 0.0002),
    (0.0, 0.0001),
]
SEHK_TICK_RULES = [
    (5000.0, 5.0),
    (2000.0, 2.0),
    (1000.0, 1.0),
    (500.0, 0.5),
    (200.0, 0.2),
    (100.0, 0.1),
    (50.0, 0.05),
    (20.0, 0.02),
    (0.5, 0.01),
    (0.25, 0.005),
    (0.0, 0.001),
]
TSEJ_TICK_RULES = [
    (1_000_000.0, 100.0),
    (300_000.0, 10.0),
    (100_000.0, 5.0),
    (30_000.0, 1.0),
    (10_000.0, 0.5),
    (0.0, 0.1),
]
EUROPEAN_TICK_RULES = [
    (1000.0, 0.5),
    (500.0, 0.2),
    (200.0, 0.1),
    (100.0, 0.1),
    (50.0, 0.1),
    (20.0, 0.05),
    (10.0, 0.02),
    (5.0, 0.01),
    (2.0, 0.005),
    (1.0, 0.002),
    (0.5, 0.001),
    (0.0, 0.0005),
]
EUROPEAN_TICK_EXCHANGES = {"AEB", "BVME", "CPH", "EBS", "ENEXT.BE", "HEX", "IBIS", "SBF", "SFB"}
EXCHANGE_TICK_RULES = {
    "BM": SBF_TICK_RULES,
    "OSE": OSE_TICK_RULES,
    "SBF": SBF_TICK_RULES,
    "SEHK": SEHK_TICK_RULES,
    "TSEJ": TSEJ_TICK_RULES,
}
DIRECT_ONLY_EXCHANGES = {"SEHK"}
NON_COUNTING_EXECUTION_STATUSES = {"skipped", "Cancelled", "ApiCancelled", "Inactive"}
REJECTED_ORDER_STATUSES = {"Cancelled", "ApiCancelled", "Inactive", "ValidationError"}
DEFAULT_MIN_ORDER_NOTIONAL = 500.0


def net_weights(long_weights: dict[str, float], short_weights: dict[str, float]) -> dict[str, float]:
    targets = dict(long_weights)
    for figi, short_weight in short_weights.items():
        net = targets.get(figi, 0) - short_weight
        if net:
            targets[figi] = net
        elif figi in targets:
            del targets[figi]
    return targets


def target_universe(figi_info: dict, mode: str) -> set[str]:
    universe = set()
    for figi, info in figi_info.items():
        if info is None:
            continue
        if mode == "foreign_only" and ibkr_contracts.is_foreign_equity(info):
            universe.add(figi)
        elif mode == "all_equities" and not ibkr_contracts.is_option_or_derivative(info):
            universe.add(figi)
    return universe


def estimate_price(holdings: dict, figi: str) -> float | None:
    security = holdings.get("securitiesData", {}).get(figi, {})
    close = security.get("close")
    if close is None:
        return None
    try:
        price = float(close)
    except (TypeError, ValueError):
        return None
    return price if price > 0 else None


def lot_sizing(raw_shares: float | None, lot_size: int, target_value: float, min_lot_value: float | None) -> tuple[int | None, int | None]:
    lots = math.floor(raw_shares / lot_size) if raw_shares else None
    if lots == 0 and min_lot_value and min_lot_value <= target_value * MAX_LOT_MULTIPLE:
        lots = 1
    rounded_shares = lots * lot_size if lots is not None else None
    return lots, rounded_shares


def build_rows(holdings: dict, mode: str, portfolio_value: float) -> tuple[list[dict], list[dict]]:
    raw_long_weights, raw_short_weights = mirror.compute_raw_weights(holdings)
    all_figis = sorted(set(raw_long_weights) | set(raw_short_weights))
    figi_info = mirror.resolve_figis(all_figis)
    targets = net_weights(raw_long_weights, raw_short_weights)
    universe = target_universe(figi_info, mode)
    contract_cache = ibkr_contracts.load_contract_cache()

    rows = []
    skipped = []
    for figi, weight in sorted(targets.items(), key=lambda item: -abs(item[1])):
        info = dict(figi_info.get(figi) or {})
        security = holdings.get("securitiesData", {}).get(figi, {})
        if figi not in universe:
            skipped.append({
                "figi": figi,
                "ticker": info.get("ticker") if info else "?",
                "name": info.get("name") if info else "",
                "exch": info.get("exchCode") if info else "?",
                "weight": weight,
                "reason": "options/derivatives not enabled"
                if ibkr_contracts.is_option_or_derivative(info)
                else "outside selected IBKR mode",
            })
            continue
        for key in ("ibkrConId", "isin", "micPrimExch"):
            if security.get(key):
                info[key] = security[key]
        plan = ibkr_contracts.plan_contract(figi, info, contract_cache)
        if plan is None:
            skipped.append({
                "figi": figi,
                "ticker": info.get("ticker") if info else "?",
                "name": info.get("name") if info else "",
                "exch": info.get("exchCode") if info else "?",
                "weight": weight,
                "reason": "no IBKR exchange mapping",
            })
            continue

        target_value = abs(weight) / 100 * portfolio_value
        price = estimate_price(holdings, figi)
        local_target_value = None
        raw_shares = target_value / price if price else None
        min_lot_value = plan.lot_size * price if price else None
        lots, rounded_shares = lot_sizing(raw_shares, plan.lot_size, target_value, min_lot_value)
        signed_target_qty = -rounded_shares if rounded_shares and weight < 0 else rounded_shares
        skip_reason = None

        if price is None:
            skip_reason = "missing price for lot sizing"
        elif min_lot_value and min_lot_value > target_value * MAX_LOT_MULTIPLE:
            skip_reason = "one lot is too large versus target"
        elif lots < 1:
            skip_reason = "target smaller than one lot"
        elif plan.needs_contract_details:
            skip_reason = "needs IBKR contract details for exact lot size"
        elif not plan.con_id:
            skip_reason = "missing IBKR contract id; run --qualify-contracts or fix mapping"

        rows.append({
            "figi": figi,
            "ticker": plan.symbol,
            "name": plan.name,
            "weight": weight,
            "target_value": target_value,
            "local_target_value": local_target_value,
            "price": price,
            "price_multiplier": plan.price_multiplier,
            "price_source": "mimicry_close" if price else None,
            "fx_rate": None,
            "raw_shares": raw_shares,
            "rounded_shares": rounded_shares,
            "target_qty": signed_target_qty,
            "lot_size": plan.lot_size,
            "exchange": plan.exchange,
            "currency": plan.currency,
            "country": plan.country,
            "con_id": plan.con_id,
            "local_symbol": plan.local_symbol,
            "trading_class": plan.trading_class,
            "min_tick": plan.min_tick,
            "skip_reason": skip_reason,
            "needs_contract_details": plan.needs_contract_details,
        })

    ibkr_contracts.save_contract_cache(contract_cache)
    return rows, skipped


def recalc_row_sizing(row: dict, price: float | None, fx_rate: float | None, price_source: str | None) -> None:
    row["price"] = price
    row["fx_rate"] = fx_rate
    row["price_source"] = price_source
    price_is_usd = price_source == "mimicry_close"
    effective_fx = 1.0 if price_is_usd else fx_rate
    row["local_target_value"] = row["target_value"] / effective_fx if price and effective_fx else None
    row["raw_shares"] = row["local_target_value"] / price if row["local_target_value"] and price else None
    min_lot_value = row["lot_size"] * price * effective_fx if price and effective_fx else None
    lots, row["rounded_shares"] = lot_sizing(row["raw_shares"], row["lot_size"], row["target_value"], min_lot_value)
    row["target_qty"] = -row["rounded_shares"] if row["rounded_shares"] and row["weight"] < 0 else row["rounded_shares"]

    if price is None:
        row["skip_reason"] = "missing price for lot sizing"
    elif effective_fx is None:
        row["skip_reason"] = "missing FX rate for USD sizing"
    elif min_lot_value and min_lot_value > row["target_value"] * MAX_LOT_MULTIPLE:
        row["skip_reason"] = "one lot is too large versus target"
    elif lots < 1:
        row["skip_reason"] = "target smaller than one lot"
    elif row.get("needs_contract_details"):
        row["skip_reason"] = "needs IBKR contract details for exact lot size"
    elif not row.get("con_id"):
        row["skip_reason"] = "missing IBKR contract id; run --qualify-contracts or fix mapping"
    else:
        row["skip_reason"] = None


def enrich_market_data(rows: list[dict], use_fallback: bool = True) -> None:
    cache = ibkr_contracts.load_contract_cache()
    plans = []
    by_con_id = {}
    for row in rows:
        cached = cache.get(row["figi"])
        if not cached:
            continue
        plan = ibkr_contracts.ContractPlan(**cached)
        if not plan.con_id:
            continue
        plans.append(plan)
        by_con_id[plan.con_id] = row

    currencies = {row["currency"] for row in rows}
    print(f"\nReading IBKR market data for {len(plans)} contracts and {len(currencies)} currencies...")
    ib = ibkr_contracts.connect_ibkr()
    try:
        prices = ibkr_contracts.snapshot_prices(ib, plans)
        fx_rates = ibkr_contracts.snapshot_fx_rates(ib, currencies)
    finally:
        ib.disconnect()

    for plan in plans:
        row = by_con_id[plan.con_id]
        fallback_price = row["price"]
        fallback_source = row["price_source"]
        fx_rate = fx_rates.get(row["currency"])
        price = prices.get(plan.con_id)
        if price is not None:
            price *= plan.price_multiplier
        price_source = "ibkr_snapshot" if price and fx_rate is not None else None
        if price is None and use_fallback:
            price = fallback_price
            price_source = fallback_source
        elif price is not None and fx_rate is None and use_fallback:
            price = fallback_price
            price_source = fallback_source
        recalc_row_sizing(row, price, fx_rate, price_source)

    missing_prices = sum(1 for row in rows if row["price"] is None)
    missing_fx = sorted({row["currency"] for row in rows if row["fx_rate"] is None})
    print(f"  Prices from IBKR: {len(prices)} | missing prices after fallback: {missing_prices}")
    if missing_fx:
        print(f"  Missing FX rates: {', '.join(missing_fx)}")


def apply_execution_fx_guard(rows: list[dict]) -> None:
    currencies = {row["currency"] for row in rows if row.get("skip_reason") is None}
    non_usd = sorted(currency for currency in currencies if currency != "USD")
    if not non_usd:
        for row in rows:
            if row.get("currency") == "USD":
                row["fx_rate"] = 1.0
        return

    print(f"\nReading required IBKR FX rates for execution: {', '.join(non_usd)}")
    ib = ibkr_contracts.connect_ibkr()
    try:
        fx_rates = ibkr_contracts.snapshot_fx_rates(ib, currencies)
    finally:
        ib.disconnect()

    missing = sorted(currency for currency in currencies if currency not in fx_rates)
    for row in rows:
        currency = row.get("currency")
        if currency in fx_rates:
            row["fx_rate"] = fx_rates[currency]
        elif currency in missing and row.get("skip_reason") is None:
            row["skip_reason"] = "missing IBKR FX rate for execution"

    if missing:
        message = f"Missing IBKR FX rates during execution: {', '.join(missing)}"
        print(f"ERROR: {message}")
        alerts.send_alert("Mimicry IBKR FX missing", message, priority=1)


def print_summary(rows: list[dict], skipped: list[dict], portfolio_value: float, mode: str) -> None:
    executable = [row for row in rows if row["skip_reason"] is None]
    held_back = [row for row in rows if row["skip_reason"] is not None]

    by_country = defaultdict(float)
    for row in rows:
        by_country[row["country"]] += abs(row["weight"])
    target_short_weight = sum(abs(row["weight"]) for row in rows if row["weight"] < 0)
    skipped_by_reason = defaultdict(float)
    for row in skipped:
        skipped_by_reason[row["reason"]] += abs(row.get("weight") or 0)

    mode_label = "foreign sleeve" if mode == "foreign_only" else "all equities/ETPs"
    print(f"\nIBKR {mode_label} mirror dry run - {datetime.datetime.now().isoformat()}")
    print(f"Portfolio value basis: ${portfolio_value:,.2f}")
    print(f"Mapped targets: {len(rows)} | executable now: {len(executable)} | held back: {len(held_back)} | skipped: {len(skipped)}")
    print(f"Mapped exposure: {sum(abs(row['weight']) for row in rows):.2f}%")
    print(f"Mapped short exposure: {target_short_weight:.2f}%")
    if skipped_by_reason:
        skipped_count = len(skipped)
        skipped_weight = sum(skipped_by_reason.values())
        print(f"Skipped {skipped_count} positions totaling {skipped_weight:.2f}%:")
        for reason, weight in sorted(skipped_by_reason.items(), key=lambda item: -item[1]):
            print(f"  {reason}: {weight:.2f}%")
    if by_country:
        print("\nExposure by country:")
        for country, weight in sorted(by_country.items(), key=lambda item: -item[1]):
            print(f"  {country:16s} {weight:6.2f}%")

    print("\nExecutable candidates:")
    for row in executable[:25]:
        print(
            f"  {row['ticker']:12s} {row['exchange']:5s} {row['currency']:3s} "
            f"{row['weight']:6.2f}% ${row['target_value']:9,.0f} "
            f"px={row['price'] or 0:,.2f} qty={row['target_qty']}"
        )

    if held_back:
        print("\nHeld back:")
        for row in held_back[:25]:
            price = f"{row['price']:.2f}" if row["price"] else "?"
            print(
                f"  {row['ticker']:12s} {row['exchange']:5s} {row['currency']:3s} "
                f"{row['weight']:6.2f}% price={price:>8s} lot={row['lot_size']:<4d} "
                f"- {row['skip_reason']}"
            )

    if skipped:
        print("\nSkipped/unmapped targets:")
        for row in skipped[:25]:
            print(f"  {row['ticker']:12s} exch={row['exch'] or '?':4s} {row['weight']:6.2f}% - {row['reason']}")


def qualify_rows(rows: list[dict], limit: int | None = None) -> None:
    cache = ibkr_contracts.load_contract_cache()
    candidates = [row for row in rows if row.get("figi") in cache]
    if limit is not None:
        candidates = candidates[:limit]

    print(f"\nQualifying {len(candidates)} IBKR contracts...")
    ib = ibkr_contracts.connect_ibkr()
    try:
        ok = 0
        failed = 0
        for row in candidates:
            figi = row["figi"]
            plan = ibkr_contracts.ContractPlan(**cache[figi])
            try:
                qualified, error = ibkr_contracts.qualify_contract_plan(ib, plan)
            except Exception as exc:
                qualified = None
                error = str(exc)

            if qualified is None:
                failed += 1
                print(f"  FAIL {plan.symbol:12s} {plan.exchange:8s} {plan.currency:3s} - {error}")
                continue

            cache[figi] = ibkr_contracts.asdict(qualified)
            ok += 1
            print(
                f"  OK   {qualified.symbol:12s} {qualified.exchange:8s} {qualified.currency:3s} "
                f"conId={qualified.con_id} local={qualified.local_symbol or '-'}"
            )
        ibkr_contracts.save_contract_cache(cache)
        print(f"\nQualification complete: {ok} ok, {failed} failed")
    finally:
        ib.disconnect()


def print_ibkr_account_snapshot() -> None:
    print("\nReading IBKR account snapshot...")
    ib = ibkr_contracts.connect_ibkr()
    try:
        summary, positions = ibkr_contracts.account_snapshot(ib)
    finally:
        ib.disconnect()

    for tag in ("NetLiquidation", "TotalCashValue", "BuyingPower", "AvailableFunds", "GrossPositionValue"):
        item = summary.get(tag)
        if item:
            print(f"  {tag:20s} {item['value']} {item['currency']}")

    print(f"\nIBKR positions: {len(positions)}")
    for pos in sorted(positions, key=lambda p: (p["currency"], p["symbol"]))[:50]:
        print(
            f"  {pos['symbol']:12s} {pos['local_symbol'] or '-':12s} "
            f"{pos['exchange']:8s} {pos['currency']:3s} "
            f"qty={pos['position']:,.4f} avg_cost={pos['avg_cost']:,.4f}"
        )


def print_open_orders(cancel: bool = False) -> None:
    print("\nReading IBKR open orders...")
    ib = ibkr_contracts.connect_ibkr()
    try:
        trades = all_open_trades(ib)
        print(f"  Open orders: {len(trades)}")
        if cancel and trades:
            ib.reqGlobalCancel()
            ib.sleep(3)
            trades = all_open_trades(ib)
            print(f"  Global cancel requested; remaining open orders: {len(trades)}")
        for trade in trades:
            contract = trade.contract
            order = trade.order
            status = trade.orderStatus
            print(
                f"  {order.action:4s} {order.totalQuantity:7.0f} {contract.symbol:12s} "
                f"{contract.exchange:8s} {contract.currency:3s} "
                f"limit={order.lmtPrice} status={status.status} order_id={order.orderId}"
            )
    finally:
        ib.disconnect()


def cancel_open_orders_for_paper() -> int:
    print("\nCancelling open IBKR paper orders before execution...")
    ib = ibkr_contracts.connect_ibkr()
    try:
        trades = all_open_trades(ib)
        before = len(trades)
        print(f"  Open orders before cancel: {before}")
        if before:
            ib.reqGlobalCancel()
        remaining = before
        for _attempt in range(10):
            ib.sleep(3)
            remaining = len(all_open_trades(ib))
            if remaining == 0:
                break
        print(f"  Open orders after cancel: {remaining}")
        return remaining
    finally:
        ib.disconnect()


def all_open_trades(ib) -> list:
    trades = ib.reqAllOpenOrders()
    ib.sleep(1)
    return list(trades or ib.openTrades())


def ibkr_positions_by_con_id() -> dict[int, dict]:
    ib = ibkr_contracts.connect_ibkr()
    try:
        _summary, positions = ibkr_contracts.account_snapshot(ib)
    finally:
        ib.disconnect()
    return {int(pos["con_id"]): pos for pos in positions if pos.get("con_id")}


def ibkr_working_orders_by_con_id() -> dict[int, float]:
    ib = ibkr_contracts.connect_ibkr()
    try:
        trades = all_open_trades(ib)
    finally:
        ib.disconnect()

    working = defaultdict(float)
    for trade in trades:
        con_id = getattr(trade.contract, "conId", None)
        if not con_id:
            continue
        remaining = trade.orderStatus.remaining or trade.order.totalQuantity or 0
        signed_qty = remaining if trade.order.action.upper() == "BUY" else -remaining
        working[int(con_id)] += signed_qty
    return dict(working)


def tick_from_rules(rules: list[tuple[float, float]], price: float) -> float:
    tick = rules[-1][1]
    for low_edge, increment in sorted(rules, reverse=True):
        if price >= low_edge:
            tick = increment
            break
    return tick


def order_tick(row: dict, price: float) -> float:
    exchange = row.get("exchange")
    ticks = [
        row.get("min_tick") or 0,
        ORDER_TICK_FALLBACK.get(exchange, 0.01),
    ]
    if exchange == "LSE":
        ticks.append(tick_from_rules(LSE_TICK_RULES, price))
    elif exchange in EXCHANGE_TICK_RULES:
        ticks.append(tick_from_rules(EXCHANGE_TICK_RULES[exchange], price))
    elif exchange in EUROPEAN_TICK_EXCHANGES:
        ticks.append(tick_from_rules(EUROPEAN_TICK_RULES, price))
    return max(ticks)


def ibkr_limit_price(
    row: dict,
    side: str,
    allow_fx_fallback: bool = True,
    limit_buffer_pct: float = PAPER_LIMIT_BUFFER_PCT,
) -> float | None:
    row["fx_fallback_used"] = False
    price = row.get("price")
    if not price:
        return None
    if row.get("price_source") == "mimicry_close":
        # Mimicry close is USD-converted. Convert back to IBKR price units.
        fx_rate = row.get("fx_rate")
        if not fx_rate and allow_fx_fallback:
            fx_rate = FX_USD_FALLBACK.get(row.get("currency"))
            if fx_rate:
                row["fx_fallback_used"] = True
        if fx_rate:
            price = price / fx_rate
        elif row.get("currency") != "USD":
            return None
    price = price / (row.get("price_multiplier") or 1.0)
    limit = price * (1 + limit_buffer_pct if side == "BUY" else 1 - limit_buffer_pct)
    min_tick = order_tick(row, limit)
    if side == "BUY":
        limit = math.ceil(limit / min_tick) * min_tick
    else:
        limit = math.floor(limit / min_tick) * min_tick
    decimals = max(0, min(6, -Decimal(str(min_tick)).as_tuple().exponent))
    return round(limit, decimals)


def build_order_preview(
    rows: list[dict],
    min_order_notional: float = DEFAULT_MIN_ORDER_NOTIONAL,
    allow_fx_fallback: bool = True,
    limit_buffer_pct: float = PAPER_LIMIT_BUFFER_PCT,
) -> list[dict]:
    positions = ibkr_positions_by_con_id()
    working_orders = ibkr_working_orders_by_con_id()
    orders = []

    for row in rows:
        if row.get("skip_reason") is not None:
            continue
        con_id = row.get("con_id")
        target_qty = row.get("target_qty")
        if not con_id or target_qty is None:
            continue
        current_qty = positions.get(int(con_id), {}).get("position", 0.0)
        working_qty = working_orders.get(int(con_id), 0.0)
        delta_qty = target_qty - current_qty - working_qty
        if abs(delta_qty) < 1:
            continue
        estimated_notional_usd = abs(delta_qty) * row["target_value"] / abs(target_qty) if target_qty else None
        if (
            min_order_notional
            and estimated_notional_usd is not None
            and estimated_notional_usd < min_order_notional
        ):
            continue
        side = "BUY" if delta_qty > 0 else "SELL"
        new_qty_after_order = current_qty + delta_qty
        current_short_qty = max(0.0, -current_qty)
        new_short_qty = max(0.0, -new_qty_after_order)
        short_open_qty = max(0.0, new_short_qty - current_short_qty)
        opens_short = side == "SELL" and short_open_qty > 0
        limit_price = ibkr_limit_price(
            row,
            side,
            allow_fx_fallback=allow_fx_fallback,
            limit_buffer_pct=limit_buffer_pct,
        )
        orders.append({
            "figi": row["figi"],
            "con_id": int(con_id),
            "ticker": row["ticker"],
            "local_symbol": row.get("local_symbol"),
            "exchange": row["exchange"],
            "currency": row["currency"],
            "side": side,
            "qty": int(abs(delta_qty)),
            "current_qty": current_qty,
            "working_qty": working_qty,
            "target_qty": int(target_qty),
            "target_weight": row.get("weight"),
            "opens_short": opens_short,
            "short_open_qty": int(math.ceil(short_open_qty)) if short_open_qty else 0,
            "limit_price": limit_price,
            "price_source": row.get("price_source"),
            "fx_rate": row.get("fx_rate"),
            "fx_fallback_used": bool(row.get("fx_fallback_used")),
            "target_value_usd": row["target_value"],
            "estimated_notional_usd": estimated_notional_usd,
        })

    orders.sort(key=lambda item: (0 if item["side"] == "SELL" else 1, item["estimated_notional_usd"] or 0, item["ticker"]))
    return orders


def print_order_preview(orders: list[dict]) -> None:
    print(f"\nIBKR order preview: {len(orders)} orders")
    if not orders:
        return
    for order in orders[:80]:
        limit = f"{order['limit_price']}" if order["limit_price"] is not None else "MISSING_LIMIT"
        short_note = f" short_open={order['short_open_qty']}" if order.get("opens_short") else ""
        print(
            f"  {order['side']:4s} {order['qty']:7d} {order['ticker']:12s} "
            f"{order['exchange']:8s} {order['currency']:3s} "
            f"current={order['current_qty']:,.0f} working={order.get('working_qty', 0):,.0f} "
            f"target={order['target_qty']:,.0f} "
            f"limit={limit} est=${order['estimated_notional_usd'] or 0:,.0f} source={order['price_source']}{short_note}"
        )


def executable_orders(
    orders: list[dict],
    max_orders: int | None,
    max_notional: float | None,
    order_type: str,
) -> tuple[list[dict], list[dict]]:
    selected = []
    rejected = []
    for order in orders:
        reason = None
        if order_type == "limit" and order.get("limit_price") is None:
            reason = "missing limit price"
        elif order.get("fx_fallback_used"):
            reason = "FX fallback used; execution requires IBKR FX"
        elif max_notional is not None and order.get("estimated_notional_usd") and order["estimated_notional_usd"] > max_notional:
            reason = "above max notional"
        elif max_orders is not None and len(selected) >= max_orders:
            reason = "above max order count"

        if reason:
            rejected.append({**order, "skip_reason": reason})
        else:
            selected.append(order)
    return selected, rejected


def load_execution_ledger() -> list[dict]:
    if not IBKR_EXECUTION_LEDGER_FILE.exists():
        return []
    data = json.loads(IBKR_EXECUTION_LEDGER_FILE.read_text())
    if isinstance(data, dict):
        return data.get("orders", [])
    return data


def save_execution_ledger(entries: list[dict]) -> None:
    IBKR_EXECUTION_LEDGER_FILE.write_text(json.dumps({"orders": entries}, indent=2, sort_keys=True))


def daily_execution_count(date: datetime.date | None = None) -> int:
    date = date or datetime.date.today()
    date_key = date.isoformat()
    return sum(
        1
        for entry in load_execution_ledger()
        if entry.get("date") == date_key and entry.get("status") not in NON_COUNTING_EXECUTION_STATUSES
    )


def trade_log_messages(trade) -> list[str]:
    messages = []
    for item in getattr(trade, "log", []) or []:
        message = getattr(item, "message", "") or ""
        error_code = getattr(item, "errorCode", 0) or 0
        if message:
            messages.append(f"{error_code}: {message}" if error_code else message)
    return messages


def update_order_status_from_trade(order: dict, trade, submitted_at: float | None = None) -> dict:
    status = trade.orderStatus
    now_ts = time.time()
    filled_qty = float(status.filled or 0)
    remaining_qty = float(status.remaining or 0)
    avg_fill_price = float(status.avgFillPrice or 0)
    result = {
        **order,
        "status": status.status,
        "final_status": status.status,
        "order_id": trade.order.orderId,
        "perm_id": trade.order.permId,
        "requested_qty": order.get("qty"),
        "filled_qty": filled_qty,
        "remaining_qty": remaining_qty,
        "avg_fill_price": avg_fill_price if avg_fill_price > 0 else None,
        "last_fill_price": float(status.lastFillPrice or 0) or None,
        "status_updated_at": datetime.datetime.now().isoformat(),
        "time_to_fill_seconds": round(now_ts - submitted_at, 3)
        if submitted_at and status.status == "Filled"
        else None,
        "messages": trade_log_messages(trade),
    }
    if status.status in REJECTED_ORDER_STATUSES:
        result["rejection_reason"] = "; ".join(result["messages"]) or status.status
    return result


def record_submitted_orders(submitted_orders: list[dict]) -> None:
    if not submitted_orders:
        return
    now = datetime.datetime.now()
    entries = load_execution_ledger()
    for order in submitted_orders:
        entries.append({
            "timestamp": now.isoformat(),
            "date": now.date().isoformat(),
            "figi": order.get("figi"),
            "con_id": order.get("con_id"),
            "ticker": order.get("ticker"),
            "exchange": order.get("exchange"),
            "currency": order.get("currency"),
            "side": order.get("side"),
            "qty": order.get("qty"),
            "requested_qty": order.get("requested_qty", order.get("qty")),
            "filled_qty": order.get("filled_qty"),
            "remaining_qty": order.get("remaining_qty"),
            "avg_fill_price": order.get("avg_fill_price"),
            "last_fill_price": order.get("last_fill_price"),
            "time_to_fill_seconds": order.get("time_to_fill_seconds"),
            "limit_price": order.get("limit_price"),
            "estimated_notional_usd": order.get("estimated_notional_usd"),
            "status": order.get("status"),
            "final_status": order.get("final_status", order.get("status")),
            "order_id": order.get("order_id"),
            "perm_id": order.get("perm_id"),
            "order_routing": order.get("order_routing"),
            "order_type": order.get("order_type"),
            "opens_short": order.get("opens_short"),
            "short_open_qty": order.get("short_open_qty"),
            "fx_rate": order.get("fx_rate"),
            "target_qty": order.get("target_qty"),
            "target_weight": order.get("target_weight"),
            "rejection_reason": order.get("rejection_reason"),
            "reason": order.get("reason"),
            "messages": order.get("messages", []),
            "run_id": order.get("run_id"),
        })
    save_execution_ledger(entries)


def execute_orders(
    orders: list[dict],
    max_orders: int | None,
    max_notional: float | None,
    outside_rth: bool,
    order_routing: str,
    order_type: str,
    verify_shortability: bool = True,
) -> list[dict]:
    selected, rejected = executable_orders(orders, max_orders, max_notional, order_type)
    print(f"\nIBKR execution: submitting {len(selected)} {order_type} orders; holding back {len(rejected)}")
    for order in rejected[:20]:
        print(f"  SKIP {order['side']:4s} {order['qty']:7d} {order['ticker']:12s} - {order['skip_reason']}")
    if not selected:
        return []

    cache = ibkr_contracts.load_contract_cache()
    submitted = []
    ib = ibkr_contracts.connect_ibkr()
    try:
        shortability = {}
        if verify_shortability:
            short_plans = []
            seen_short_con_ids = set()
            for order in selected:
                if not order.get("opens_short"):
                    continue
                cached = cache.get(order["figi"])
                if not cached:
                    continue
                plan = ibkr_contracts.ContractPlan(**cached)
                if plan.con_id and plan.con_id not in seen_short_con_ids:
                    short_plans.append(plan)
                    seen_short_con_ids.add(plan.con_id)
            if short_plans:
                print(f"\nChecking IBKR shortability for {len(short_plans)} contracts...")
                shortability = ibkr_contracts.snapshot_shortability(ib, short_plans)

        for order in selected:
            cached = cache.get(order["figi"])
            if not cached:
                submitted.append({**order, "status": "skipped", "reason": "missing contract cache"})
                continue
            plan = ibkr_contracts.ContractPlan(**cached)
            if verify_shortability and order.get("opens_short"):
                availability = shortability.get(int(order["con_id"]), {})
                score = availability.get("shortable")
                shares = availability.get("shortable_shares")
                required_shares = order.get("short_open_qty") or order["qty"]
                if score is None and shares is None:
                    reason = "shortability unavailable"
                elif shares is not None and shares < required_shares:
                    reason = f"shortable shares too low ({shares:g} < {required_shares})"
                elif score is not None and score < 3:
                    reason = f"shortability score too low ({score:g})"
                else:
                    reason = None
                if reason:
                    skipped = {
                        **order,
                        "status": "skipped",
                        "final_status": "skipped",
                        "reason": reason,
                        "rejection_reason": reason,
                        "shortability": availability,
                        "order_routing": order_routing,
                        "order_type": order_type,
                    }
                    submitted.append(skipped)
                    print(f"  SKIP SELL {order['qty']:7d} {order['ticker']:12s} - {reason}")
                    continue

            exchange = plan.exchange if plan.exchange in DIRECT_ONLY_EXCHANGES else ("SMART" if order_routing == "smart" else None)
            contract = ibkr_contracts.to_ib_async_contract(plan, exchange=exchange)
            if order_type == "market":
                ib_order = ibkr_contracts.market_order(order["side"], order["qty"], outside_rth=outside_rth)
            else:
                ib_order = ibkr_contracts.limit_order(order["side"], order["qty"], order["limit_price"], outside_rth=outside_rth)
            submitted_at = time.time()
            trade = ib.placeOrder(contract, ib_order)
            ib.sleep(1)
            submitted_order = update_order_status_from_trade(order, trade, submitted_at=submitted_at)
            submitted_order.update({
                "order_routing": order_routing,
                "order_type": order_type,
            })
            submitted.append(submitted_order)
            price_label = "market" if order_type == "market" else f"limit={order['limit_price']}"
            print(
                f"  SENT {order['side']:4s} {order['qty']:7d} {order['ticker']:12s} "
                f"route={contract.exchange} {price_label} "
                f"status={submitted_order['status']} order_id={trade.order.orderId}"
            )
            if submitted_order["status"] in REJECTED_ORDER_STATUSES:
                alerts.send_alert(
                    "Mimicry IBKR order rejection",
                    (
                        f"{order['side']} {order['qty']} {order['ticker']} "
                        f"status={submitted_order['status']} "
                        f"reason={submitted_order.get('rejection_reason') or 'unknown'}"
                    ),
                    priority=1,
                )
    finally:
        ib.disconnect()
    return submitted


def save_ibkr_state(
    rows: list[dict],
    skipped: list[dict],
    orders: list[dict],
    submitted_orders: list[dict],
    portfolio_value: float,
    portfolio_value_source: str,
    mode: str,
) -> None:
    state = {
        "timestamp": datetime.datetime.now().isoformat(),
        "date": datetime.date.today().isoformat(),
        "mode": mode,
        "portfolio_value": portfolio_value,
        "portfolio_value_source": portfolio_value_source,
        "targets": rows,
        "skipped": skipped,
        "orders": orders,
        "submitted_orders": submitted_orders,
    }
    with open(IBKR_STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)
    print(f"\nIBKR state saved to {IBKR_STATE_FILE}")


def load_ibkr_state() -> dict:
    if not IBKR_STATE_FILE.exists():
        return {}
    try:
        return json.loads(IBKR_STATE_FILE.read_text())
    except Exception:
        return {}


def check_killswitch() -> bool:
    if not KILLSWITCH_FILE.exists():
        return False
    message = f"IBKR runner halted because {KILLSWITCH_FILE} exists"
    print(message)
    alerts.send_alert("Mimicry IBKR halted", message, priority=1)
    return True


def ibkr_account_ids() -> list[str]:
    ib = ibkr_contracts.connect_ibkr()
    try:
        return list(ib.managedAccounts() or [])
    finally:
        ib.disconnect()


def connection_looks_paper(account_ids: list[str]) -> bool:
    if any(str(account).upper().startswith("DU") for account in account_ids):
        return True
    try:
        return ibkr_contracts.ibkr_config()["port"] == 4002
    except Exception:
        return False


def validate_execution_mode(confirm_paper: bool, confirm_live: bool) -> str:
    if confirm_paper == confirm_live:
        raise SystemExit("--execute requires exactly one of --confirm-paper or --confirm-live")

    account_ids = ibkr_account_ids()
    is_paper = connection_looks_paper(account_ids)
    account_label = ", ".join(account_ids) if account_ids else "unknown account"
    if confirm_paper and not is_paper:
        raise SystemExit(f"--confirm-paper used but IBKR connection does not look like paper: {account_label}")
    if confirm_live and is_paper:
        raise SystemExit(f"--confirm-live used but IBKR connection looks like paper: {account_label}")
    return "paper" if confirm_paper else "live"


def apply_live_caps(args: argparse.Namespace) -> None:
    args.max_orders = LIVE_MAX_ORDERS_PER_RUN if args.max_orders is None else min(args.max_orders, LIVE_MAX_ORDERS_PER_RUN)
    args.max_notional = (
        LIVE_MAX_NOTIONAL_PER_ORDER
        if args.max_notional is None
        else min(args.max_notional, LIVE_MAX_NOTIONAL_PER_ORDER)
    )
    args.max_daily_orders = (
        LIVE_MAX_DAILY_ORDERS
        if args.max_daily_orders is None
        else min(args.max_daily_orders, LIVE_MAX_DAILY_ORDERS)
    )
    if args.order_type != "limit":
        raise SystemExit("Live execution starts with limit orders only; refusing market orders")


def alert_netliq_change(portfolio_value: float, previous_state: dict) -> None:
    previous_value = previous_state.get("portfolio_value")
    if not previous_value:
        return
    change = (portfolio_value - float(previous_value)) / float(previous_value)
    if abs(change) > NETLIQ_ALERT_PCT:
        alerts.send_alert(
            "Mimicry IBKR NetLiq move",
            f"NetLiq changed {change:+.2%}: ${float(previous_value):,.2f} -> ${portfolio_value:,.2f}",
            priority=1,
        )


def run_live_sanity_checks(orders: list[dict], portfolio_value: float, previous_state: dict) -> None:
    if not orders:
        return
    largest = max((order.get("estimated_notional_usd") or 0 for order in orders), default=0)
    total = sum(order.get("estimated_notional_usd") or 0 for order in orders)
    previous_value = previous_state.get("portfolio_value")
    reasons = []
    if largest > portfolio_value * LIVE_SINGLE_ORDER_NETLIQ_HALT_PCT:
        reasons.append(f"single order ${largest:,.0f} exceeds 5% of NetLiq ${portfolio_value:,.0f}")
    if total > portfolio_value * LIVE_RUN_NOTIONAL_NETLIQ_HALT_PCT:
        reasons.append(f"run notional ${total:,.0f} exceeds 25% of NetLiq ${portfolio_value:,.0f}")
    if previous_value:
        change = (portfolio_value - float(previous_value)) / float(previous_value)
        if abs(change) > LIVE_NETLIQ_HALT_PCT:
            reasons.append(f"NetLiq changed {change:+.2%} since last run")
    if reasons:
        message = "\n".join(reasons)
        alerts.send_alert("Mimicry IBKR live sanity halt", message, priority=1)
        raise SystemExit(f"Live sanity check failed:\n{message}")


def load_ibkr_net_liquidation() -> float:
    ib = ibkr_contracts.connect_ibkr()
    try:
        summary, _positions = ibkr_contracts.account_snapshot(ib)
    finally:
        ib.disconnect()

    item = summary.get("NetLiquidation")
    if not item:
        raise SystemExit("IBKR NetLiquidation was not available")
    if item.get("currency") and item["currency"] != "USD":
        raise SystemExit(f"IBKR NetLiquidation is not USD: {item['value']} {item['currency']}")
    return float(item["value"])


def load_portfolio_value(cli_value: float | None, source: str) -> tuple[float, str]:
    if cli_value is not None:
        return cli_value, "cli"
    if source == "ibkr":
        return load_ibkr_net_liquidation(), "ibkr_net_liquidation"
    account = mirror.get_account()
    return float(account["portfolio_value"]), "alpaca_portfolio_value"


def load_holdings_from_file(path: str) -> dict:
    data = json.loads(Path(path).read_text())
    if "weightsOfPortfolio" in data and "weightsOfBaskets" in data:
        return data

    if isinstance(data, dict):
        for key, value in data.items():
            if "/holdings/" not in key:
                continue
            payload = json.loads(value) if isinstance(value, str) else value
            if "weightsOfPortfolio" in payload and "weightsOfBaskets" in payload:
                return payload

    raise SystemExit(f"Could not find a holdings payload in {path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Plan IBKR Mimicry equity mirror orders")
    parser.add_argument("--mode", choices=["foreign_only", "all_equities"], default="foreign_only")
    parser.add_argument("--date", default=datetime.date.today().isoformat())
    parser.add_argument("--portfolio-value", type=float, help="Use this explicit USD value for target sizing")
    parser.add_argument(
        "--portfolio-value-source",
        choices=["alpaca", "ibkr"],
        default="alpaca",
        help="Account value source for target sizing when --portfolio-value is not supplied",
    )
    parser.add_argument("--holdings-json", help="Read holdings from a saved JSON payload instead of Mimicry")
    parser.add_argument("--qualify-contracts", action="store_true", help="Connect to IBKR and enrich contract cache")
    parser.add_argument("--qualify-limit", type=int, help="Limit contract qualification count for smoke tests")
    parser.add_argument("--ibkr-market-data", action="store_true", help="Use IBKR snapshot prices and FX for sizing")
    parser.add_argument("--no-price-fallback", action="store_true", help="Do not fall back to Mimicry close if IBKR price is unavailable")
    parser.add_argument("--show-ibkr-account", action="store_true", help="Print read-only IBKR account and positions snapshot")
    parser.add_argument("--show-open-orders", action="store_true", help="Print open IBKR orders")
    parser.add_argument("--cancel-open-orders", action="store_true", help="Cancel all open IBKR orders visible to this client")
    parser.add_argument("--preview-orders", action="store_true", help="Read IBKR positions and print proposed orders without executing")
    parser.add_argument("--execute", action="store_true", help="Submit gated paper IBKR orders")
    parser.add_argument("--confirm-paper", action="store_true", help="Required with --execute to confirm this is paper trading")
    parser.add_argument("--confirm-live", action="store_true", help="Required with --execute to confirm live trading")
    parser.add_argument("--max-orders", type=int, help="Optional maximum orders to submit in one execution run")
    parser.add_argument("--max-notional", type=float, help="Optional maximum estimated USD notional per submitted order")
    parser.add_argument("--max-daily-orders", type=int, help="Optional maximum IBKR paper order submissions per local day")
    parser.add_argument(
        "--min-order-notional",
        type=float,
        default=DEFAULT_MIN_ORDER_NOTIONAL,
        help="Skip rebalance orders below this estimated USD notional; pass 0 for exact paper churn",
    )
    parser.add_argument("--allow-outside-rth", action="store_true", help="Allow orders outside regular trading hours")
    parser.add_argument("--order-routing", choices=["smart", "direct"], default="smart", help="Route submitted orders through SMART or direct listing venues")
    parser.add_argument("--order-type", choices=["limit", "market"], default="limit", help="Use limit or market orders for execution")
    parser.add_argument("--skip-shortability-check", action="store_true", help="Skip IBKR shortability check before short-opening orders")
    parser.add_argument(
        "--cancel-before-execute",
        action="store_true",
        help="Cancel open IBKR orders before computing and submitting paper deltas",
    )
    parser.add_argument("--save-state", action="store_true", help="Save dry-run targets and order preview to last_state_ibkr.json")
    args = parser.parse_args()

    if check_killswitch():
        raise SystemExit(75)

    planning_requested = any((
        args.execute,
        args.preview_orders,
        args.qualify_contracts,
        args.ibkr_market_data,
        args.save_state,
    ))
    ibkr_read_requested = args.show_ibkr_account or args.show_open_orders or args.cancel_open_orders
    if args.cancel_open_orders and not (args.confirm_paper or args.confirm_live):
        raise SystemExit("--cancel-open-orders requires --confirm-paper or --confirm-live")
    if args.cancel_before_execute and not (args.execute and (args.confirm_paper or args.confirm_live)):
        raise SystemExit("--cancel-before-execute requires --execute and --confirm-paper or --confirm-live")
    execution_mode = None
    if args.execute:
        execution_mode = validate_execution_mode(args.confirm_paper, args.confirm_live)
        if execution_mode == "live":
            if args.skip_shortability_check:
                raise SystemExit("Live execution requires shortability checks")
            apply_live_caps(args)
    elif args.cancel_open_orders:
        validate_execution_mode(args.confirm_paper, args.confirm_live)
    if ibkr_read_requested and not planning_requested:
        if args.show_ibkr_account:
            print_ibkr_account_snapshot()
        if args.show_open_orders or args.cancel_open_orders:
            print_open_orders(cancel=args.cancel_open_orders)
        return

    previous_state = load_ibkr_state()
    portfolio_value, portfolio_value_source = load_portfolio_value(args.portfolio_value, args.portfolio_value_source)
    if portfolio_value_source == "ibkr_net_liquidation":
        alert_netliq_change(portfolio_value, previous_state)
    if args.holdings_json:
        holdings = load_holdings_from_file(args.holdings_json)
    else:
        mirror.check_session_expiry()
        holdings = mirror.fetch_holdings(args.date)
    rows, skipped = build_rows(holdings, args.mode, portfolio_value)
    if args.ibkr_market_data:
        enrich_market_data(rows, use_fallback=not args.no_price_fallback)
    if args.execute:
        apply_execution_fx_guard(rows)
    print_summary(rows, skipped, portfolio_value, args.mode)
    print(f"Portfolio value source: {portfolio_value_source}")
    orders = []
    submitted_orders = []
    if args.execute and args.cancel_before_execute:
        remaining_open_orders = cancel_open_orders_for_paper()
        if remaining_open_orders:
            raise SystemExit("Open IBKR orders remain after cancel; skipping execution this run")
    if args.preview_orders or args.execute:
        try:
            orders = build_order_preview(
                rows,
                min_order_notional=args.min_order_notional,
                allow_fx_fallback=not args.execute,
                limit_buffer_pct=LIVE_LIMIT_BUFFER_PCT if execution_mode == "live" else PAPER_LIMIT_BUFFER_PCT,
            )
        except Exception as exc:
            if args.execute:
                raise
            print(f"\nIBKR order preview unavailable: {exc}")
            print("  Target dry run still completed; start IB Gateway to include current positions/orders.")
        else:
            print_order_preview(orders)
    if args.execute:
        max_orders = args.max_orders
        if args.max_daily_orders is not None:
            used_today = daily_execution_count()
            remaining_today = max(0, args.max_daily_orders - used_today)
            max_orders = remaining_today if max_orders is None else min(max_orders, remaining_today)
            print(
                f"\nIBKR daily order cap: {used_today}/{args.max_daily_orders} used; "
                f"{remaining_today} remaining today"
            )
        if execution_mode == "live":
            selected_for_sanity, _rejected_for_sanity = executable_orders(
                orders,
                max_orders=max_orders,
                max_notional=args.max_notional,
                order_type=args.order_type,
            )
            run_live_sanity_checks(selected_for_sanity, portfolio_value, previous_state)
        run_id = datetime.datetime.now().strftime("%Y%m%dT%H%M%S")
        submitted_orders = execute_orders(
            orders,
            max_orders=max_orders,
            max_notional=args.max_notional,
            outside_rth=args.allow_outside_rth,
            order_routing=args.order_routing,
            order_type=args.order_type,
            verify_shortability=not args.skip_shortability_check,
        )
        for order in submitted_orders:
            order["run_id"] = run_id
        record_submitted_orders(submitted_orders)
    if args.qualify_contracts:
        qualify_rows(rows, args.qualify_limit)
    if args.show_ibkr_account:
        print_ibkr_account_snapshot()
    if args.show_open_orders or args.cancel_open_orders:
        print_open_orders(cancel=args.cancel_open_orders)
    if args.save_state:
        save_ibkr_state(rows, skipped, orders, submitted_orders, portfolio_value, portfolio_value_source, args.mode)


if __name__ == "__main__":
    main()
