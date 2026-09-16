#!/usr/bin/env python3
"""Deterministic planning and crash-safe submission for the IBKR mirror."""

from __future__ import annotations

import datetime
import math
import time
from collections import defaultdict
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from zoneinfo import ZoneInfo

import ibkr_contracts
import ibkr_state


ORDER_REF_PREFIX = "MIMICRY:"
MAX_QUOTE_AGE_SECONDS = 20 * 60
MIN_US_EQUITY_DELTA = 25.0
MIN_FOREIGN_EQUITY_DELTA = 100.0
MAX_ORDERS_PER_RUN = 20
MAX_DAILY_ORDERS = 60
MAX_CHILD_NOTIONAL_USD = 5_000.0
MAX_CHILD_NETLIQ_PCT = 0.05
MAX_RUN_NETLIQ_PCT = 0.25
LIMIT_BOUND_PCT = 0.015
WORKING_STATUSES = {"PendingSubmit", "PreSubmitted", "Submitted"}
TERMINAL_STATUSES = {"Filled", "Cancelled", "ApiCancelled", "Inactive"}


def finite_positive(value) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) and result > 0 else None


def normalized_quantity(value: float) -> int | float:
    value = float(value)
    rounded = round(value)
    return int(rounded) if math.isclose(value, rounded, abs_tol=1e-9) else value


def quote_validation_error(
    quote: ibkr_contracts.Quote | None,
    max_age_seconds: float = MAX_QUOTE_AGE_SECONDS,
) -> str | None:
    if quote is None:
        return "missing IBKR quote"
    if quote.frozen:
        return "frozen IBKR quote"
    if quote.age_seconds is None:
        return "IBKR quote has no timestamp"
    if quote.age_seconds > max_age_seconds:
        return f"stale IBKR quote ({quote.age_seconds / 60:.1f} minutes old)"
    bid = finite_positive(quote.bid)
    ask = finite_positive(quote.ask)
    if bid is None or ask is None:
        return "IBKR bid/ask unavailable"
    if ask < bid:
        return f"invalid IBKR market ({bid:g} bid > {ask:g} ask)"
    return None


def executable_fx_rates(
    currencies: set[str],
    quotes: dict[str, ibkr_contracts.FxQuote],
) -> tuple[dict[str, float], dict[str, str]]:
    rates = {}
    errors = {}
    for currency in sorted(currencies):
        fx_quote = quotes.get(currency)
        if fx_quote is None or finite_positive(fx_quote.usd_rate) is None:
            errors[currency] = "missing IBKR FX quote"
            continue
        error = quote_validation_error(fx_quote.quote)
        if error:
            errors[currency] = error
            continue
        rates[currency] = float(fx_quote.usd_rate)
    return rates, errors


def market_is_open(
    row: dict,
    now: datetime.datetime | None = None,
) -> bool | None:
    hours = str(row.get("liquid_hours") or "").strip()
    time_zone_id = str(row.get("time_zone_id") or "").strip()
    if not hours or not time_zone_id:
        return None
    try:
        timezone = ZoneInfo(time_zone_id)
    except Exception:
        return None
    local_now = (now or datetime.datetime.now(datetime.timezone.utc)).astimezone(timezone)
    date_key = local_now.strftime("%Y%m%d")
    matching = [
        item
        for item in hours.split(";")
        if item.startswith(f"{date_key}:")
    ]
    if not matching:
        return None
    for item in matching:
        value = item.split(":", 1)[1]
        if value == "CLOSED":
            continue
        for session in value.split(","):
            try:
                start_text, end_text = session.split("-", 1)
                if ":" not in start_text:
                    start_text = f"{date_key}:{start_text}"
                if ":" not in end_text:
                    end_text = f"{date_key}:{end_text}"
                start = datetime.datetime.strptime(
                    start_text,
                    "%Y%m%d:%H%M",
                ).replace(tzinfo=timezone)
                end = datetime.datetime.strptime(
                    end_text,
                    "%Y%m%d:%H%M",
                ).replace(tzinfo=timezone)
            except ValueError:
                return None
            if start <= local_now <= end:
                return True
    return False


def tick_for_price(market_rules: list[list[float]] | None, price: float) -> float | None:
    if not market_rules:
        return None
    tick = None
    for low_edge, increment in sorted(market_rules, key=lambda item: float(item[0])):
        if price >= float(low_edge):
            tick = finite_positive(increment)
        else:
            break
    return tick


def _round_to_tick(value: float, tick: float, rounding: str) -> float:
    decimal_value = Decimal(str(value))
    decimal_tick = Decimal(str(tick))
    mode = ROUND_CEILING if rounding == "up" else ROUND_FLOOR
    units = (decimal_value / decimal_tick).to_integral_value(rounding=mode)
    result = units * decimal_tick
    decimals = max(0, min(10, -decimal_tick.as_tuple().exponent))
    return round(float(result), decimals)


def marketable_limit_price(
    quote: ibkr_contracts.Quote,
    side: str,
    market_rules: list[list[float]] | None,
    bound_pct: float = LIMIT_BOUND_PCT,
) -> tuple[float | None, str | None]:
    error = quote_validation_error(quote)
    if error:
        return None, error
    bid = float(quote.bid)
    ask = float(quote.ask)
    midpoint = (bid + ask) / 2
    last = finite_positive(quote.last)
    reference = last if last is not None and bid <= last <= ask else midpoint
    touch = ask if side == "BUY" else bid
    tick = tick_for_price(market_rules, touch)
    if tick is None:
        return None, "missing IBKR market-rule increment"

    if side == "BUY":
        upper_bound = reference * (1 + bound_pct)
        if touch > upper_bound:
            return None, "ask is beyond the 1.5% execution bound"
        candidate = min(touch + tick, upper_bound)
        limit = _round_to_tick(candidate, tick, "up")
        if limit > upper_bound:
            limit = _round_to_tick(upper_bound, tick, "down")
        if limit < touch:
            return None, "valid buy tick cannot remain marketable inside the 1.5% bound"
    else:
        lower_bound = reference * (1 - bound_pct)
        if touch < lower_bound:
            return None, "bid is beyond the 1.5% execution bound"
        candidate = max(touch - tick, lower_bound)
        limit = _round_to_tick(candidate, tick, "down")
        if limit < lower_bound:
            limit = _round_to_tick(lower_bound, tick, "up")
        if limit > touch:
            return None, "valid sell tick cannot remain marketable inside the 1.5% bound"
    return limit, None


def contract_multiplier(row: dict) -> float:
    return finite_positive(row.get("multiplier")) or 1.0


def price_multiplier(row: dict) -> float:
    return finite_positive(row.get("price_multiplier")) or 1.0


def order_notional_usd(qty: float, limit_price: float, row: dict, fx_rate: float) -> float:
    return (
        abs(float(qty))
        * float(limit_price)
        * contract_multiplier(row)
        * price_multiplier(row)
        * float(fx_rate)
    )


def target_quantity(row: dict, netliq: float, quote_price: float, fx_rate: float) -> int:
    weight = float(row.get("weight") or 0)
    target_value = abs(weight) / 100 * float(netliq)
    per_unit_usd = (
        float(quote_price)
        * contract_multiplier(row)
        * price_multiplier(row)
        * float(fx_rate)
    )
    if target_value <= 0 or per_unit_usd <= 0:
        return 0
    lot_size = max(1, int(row.get("lot_size") or 1))
    if row.get("sec_type") == "OPT":
        units = max(1, math.floor(target_value / per_unit_usd))
    else:
        units = math.floor(target_value / per_unit_usd / lot_size) * lot_size
    return -units if weight < 0 else units


def option_expiry_action(
    row: dict,
    current_qty: float,
    now: datetime.datetime | None = None,
) -> tuple[bool, str | None]:
    if row.get("sec_type") != "OPT" or not row.get("expiry"):
        return False, None
    now_et = (now or datetime.datetime.now(datetime.timezone.utc)).astimezone(
        ZoneInfo("America/New_York")
    )
    expiry = datetime.datetime.strptime(str(row["expiry"])[:8], "%Y%m%d").date()
    if now_et.date() != expiry:
        return False, None
    close_window = datetime.time(15, 15)
    if now_et.time() >= close_window:
        if current_qty > 0:
            return True, "expiration-day close window before 3:30 p.m. ET"
        return True, "same-day option opens stop before the 3:30 p.m. ET cutoff"
    return False, None


def _minimum_delta(row: dict) -> float:
    if row.get("sec_type") == "OPT":
        return 0.0
    if row.get("country") == "United States" or row.get("currency") == "USD":
        return MIN_US_EQUITY_DELTA
    return MIN_FOREIGN_EQUITY_DELTA


def _position_map(positions: list[dict]) -> dict[int, dict]:
    return {
        int(position["con_id"]): position
        for position in positions
        if position.get("con_id") and float(position.get("position") or 0) != 0
    }


def plan_order_deltas(
    rows: list[dict],
    positions: list[dict],
    quotes: dict[int, ibkr_contracts.Quote],
    fx_rates: dict[str, float],
    netliq: float,
    now: datetime.datetime | None = None,
) -> tuple[list[dict], list[dict]]:
    positions_by_con_id = _position_map(positions)
    target_con_ids = {int(row["con_id"]) for row in rows if row.get("con_id")}
    orders = []
    held_back = []

    negative_options = [
        row for row in rows
        if row.get("sec_type") == "OPT" and float(row.get("weight") or 0) < 0
    ]
    if negative_options:
        labels = ", ".join(str(row.get("ticker") or row.get("local_symbol")) for row in negative_options)
        raise RuntimeError(f"negative option targets are unsupported and block execution: {labels}")

    for row in rows:
        con_id = row.get("con_id")
        if not con_id:
            held_back.append({**row, "skip_reason": row.get("skip_reason") or "missing qualified contract"})
            continue
        con_id = int(con_id)
        if row.get("skip_reason"):
            held_back.append(dict(row))
            continue
        session_open = market_is_open(row, now=now)
        if session_open is False:
            held_back.append({**row, "skip_reason": "market is closed"})
            continue
        quote = quotes.get(con_id)
        quote_error = quote_validation_error(quote)
        if quote_error:
            held_back.append({**row, "skip_reason": quote_error})
            continue
        fx_rate = finite_positive(fx_rates.get(row.get("currency")))
        if fx_rate is None:
            held_back.append({**row, "skip_reason": f"missing IBKR FX for {row.get('currency')}"})
            continue
        current_qty = float(positions_by_con_id.get(con_id, {}).get("position", 0))
        force_expiry_exit, expiry_reason = option_expiry_action(row, current_qty, now=now)
        side_reference = quote.ask if float(row.get("weight") or 0) >= 0 else quote.bid
        desired_qty = 0 if force_expiry_exit else target_quantity(
            row,
            netliq,
            float(side_reference),
            fx_rate,
        )
        if (
            not force_expiry_exit
            and desired_qty == 0
            and current_qty == 0
            and float(row.get("weight") or 0) != 0
        ):
            held_back.append({
                **row,
                "skip_reason": "target is smaller than one tradable lot",
            })
            continue
        if row.get("sec_type") == "OPT" and current_qty < 0:
            raise RuntimeError(
                f"dedicated account contains unsupported short option {row.get('ticker')} qty={current_qty:g}"
            )
        delta_qty = desired_qty - current_qty
        if abs(delta_qty) < 1e-9:
            continue
        side = "BUY" if delta_qty > 0 else "SELL"
        if row.get("sec_type") == "OPT" and side == "SELL" and abs(delta_qty) > max(current_qty, 0):
            raise RuntimeError(f"option sell would open a short position: {row.get('ticker')}")
        limit_price, limit_error = marketable_limit_price(
            quote,
            side,
            row.get("market_rules"),
        )
        if limit_error:
            held_back.append({**row, "skip_reason": limit_error})
            continue
        notional = order_notional_usd(delta_qty, limit_price, row, fx_rate)
        complete_exit = desired_qty == 0 and current_qty != 0
        if not complete_exit and notional < _minimum_delta(row):
            held_back.append({
                **row,
                "skip_reason": f"delta ${notional:,.2f} below minimum",
            })
            continue
        new_qty = current_qty + delta_qty
        risk_reducing = (
            (current_qty > 0 and delta_qty < 0 and abs(new_qty) < abs(current_qty))
            or (current_qty < 0 and delta_qty > 0 and abs(new_qty) < abs(current_qty))
        )
        current_value = current_qty * float(limit_price) * contract_multiplier(row) * price_multiplier(row) * fx_rate
        target_value = desired_qty * float(limit_price) * contract_multiplier(row) * price_multiplier(row) * fx_rate
        orders.append({
            **row,
            "side": side,
            "qty": normalized_quantity(abs(delta_qty)),
            "current_qty": current_qty,
            "target_qty": int(round(desired_qty)),
            "limit_price": limit_price,
            "fx_rate": fx_rate,
            "quote_timestamp": quote.timestamp,
            "market_data_type": quote.market_data_type,
            "estimated_notional_usd": notional,
            "actual_notional_usd": notional,
            "complete_exit": complete_exit,
            "risk_reducing": risk_reducing,
            "opens_short": (
                row.get("sec_type") == "STK"
                and max(0.0, -desired_qty) > max(0.0, -current_qty)
            ),
            "short_open_qty": max(
                0,
                int(math.ceil(max(0.0, -desired_qty) - max(0.0, -current_qty))),
            ),
            "absolute_drift_usd": abs(target_value - current_value),
            "reason": expiry_reason if force_expiry_exit else row.get("reason"),
        })

    for position in positions:
        con_id = int(position.get("con_id") or 0)
        current_qty = float(position.get("position") or 0)
        if (
            not con_id
            or con_id in target_con_ids
            or current_qty == 0
            or position.get("sec_type") not in {"STK", "OPT"}
        ):
            continue
        row = dict(position)
        row.setdefault("ticker", position.get("local_symbol") or position.get("symbol"))
        row.setdefault("lot_size", 1)
        row.setdefault("price_multiplier", 1.0)
        row.setdefault("country", "United States" if position.get("currency") == "USD" else "Foreign")
        row.setdefault("market_rules", position.get("market_rules"))
        session_open = market_is_open(row, now=now)
        if session_open is False:
            held_back.append({
                **row,
                "skip_reason": "market is closed",
                "orphan_exit": True,
            })
            continue
        quote = quotes.get(con_id)
        quote_error = quote_validation_error(quote)
        fx_rate = finite_positive(fx_rates.get(position.get("currency")))
        if quote_error or fx_rate is None:
            held_back.append({
                **row,
                "skip_reason": quote_error or f"missing IBKR FX for {position.get('currency')}",
                "orphan_exit": True,
            })
            continue
        if position.get("sec_type") == "OPT" and current_qty < 0:
            raise RuntimeError(
                f"dedicated account contains unsupported short orphan option "
                f"{row.get('ticker')} qty={current_qty:g}"
            )
        side = "SELL" if current_qty > 0 else "BUY"
        limit_price, limit_error = marketable_limit_price(
            quote,
            side,
            row.get("market_rules"),
        )
        if limit_error:
            held_back.append({**row, "skip_reason": limit_error, "orphan_exit": True})
            continue
        notional = order_notional_usd(current_qty, limit_price, row, fx_rate)
        orders.append({
            **row,
            "figi": None,
            "side": side,
            "qty": normalized_quantity(abs(current_qty)),
            "current_qty": current_qty,
            "target_qty": 0,
            "limit_price": limit_price,
            "fx_rate": fx_rate,
            "quote_timestamp": quote.timestamp,
            "market_data_type": quote.market_data_type,
            "estimated_notional_usd": notional,
            "actual_notional_usd": notional,
            "complete_exit": True,
            "orphan_exit": True,
            "risk_reducing": True,
            "absolute_drift_usd": notional,
            "reason": "position absent from Mimicry target",
        })

    orders.sort(
        key=lambda order: (
            0 if order.get("complete_exit") else 1 if order.get("risk_reducing") else 2,
            -float(order.get("absolute_drift_usd") or 0),
            str(order.get("ticker") or ""),
        )
    )
    return orders, held_back


def slice_orders(
    orders: list[dict],
    netliq: float,
    used_today: int = 0,
    max_orders: int = MAX_ORDERS_PER_RUN,
    max_daily_orders: int = MAX_DAILY_ORDERS,
    max_child_notional: float = MAX_CHILD_NOTIONAL_USD,
    max_run_notional: float | None = None,
) -> tuple[list[dict], list[dict]]:
    child_cap = min(float(max_child_notional), float(netliq) * MAX_CHILD_NETLIQ_PCT)
    run_cap = min(
        float(max_run_notional) if max_run_notional is not None else math.inf,
        float(netliq) * MAX_RUN_NETLIQ_PCT,
    )
    order_slots = max(0, min(int(max_orders), int(max_daily_orders) - int(used_today)))
    selected = []
    held_back = []
    run_notional = 0.0

    for parent in orders:
        remaining_qty = float(parent["qty"])
        per_unit = float(parent["actual_notional_usd"]) / remaining_qty if remaining_qty else 0
        lot_size = 1 if parent.get("sec_type") == "OPT" else max(1, int(parent.get("lot_size") or 1))
        max_qty_by_child = math.floor(child_cap / per_unit / lot_size) * lot_size if per_unit else 0
        if max_qty_by_child < lot_size:
            held_back.append({
                **parent,
                "skip_reason": (
                    f"one tradable unit ${per_unit * lot_size:,.2f} exceeds "
                    f"child cap ${child_cap:,.2f}"
                ),
            })
            continue
        child_index = 0
        while remaining_qty >= lot_size - 1e-9:
            if len(selected) >= order_slots:
                break
            remaining_run_notional = run_cap - run_notional
            max_qty_by_run = math.floor(remaining_run_notional / per_unit / lot_size) * lot_size
            child_qty = min(math.floor(remaining_qty / lot_size) * lot_size, max_qty_by_child, max_qty_by_run)
            child_qty = math.floor(child_qty / lot_size) * lot_size
            if child_qty < lot_size:
                break
            child_index += 1
            child_notional = child_qty * per_unit
            selected.append({
                **parent,
                "qty": normalized_quantity(child_qty),
                "requested_qty": normalized_quantity(child_qty),
                "estimated_notional_usd": child_notional,
                "actual_notional_usd": child_notional,
                "slice_index": child_index,
                "parent_qty": normalized_quantity(parent["qty"]),
            })
            remaining_qty -= child_qty
            run_notional += child_notional
        fractional_allowed = (
            parent.get("sec_type") == "STK"
            and (
                parent.get("country") == "United States"
                or parent.get("currency") == "USD"
            )
        )
        fractional_notional = remaining_qty * per_unit
        if (
            remaining_qty > 1e-9
            and fractional_allowed
            and len(selected) < order_slots
            and fractional_notional <= child_cap + 0.01
            and run_notional + fractional_notional <= run_cap + 0.01
        ):
            child_index += 1
            selected.append({
                **parent,
                "qty": normalized_quantity(remaining_qty),
                "requested_qty": normalized_quantity(remaining_qty),
                "estimated_notional_usd": fractional_notional,
                "actual_notional_usd": fractional_notional,
                "slice_index": child_index,
                "parent_qty": normalized_quantity(parent["qty"]),
            })
            run_notional += fractional_notional
            remaining_qty = 0.0
        if remaining_qty > 1e-9:
            held_back.append({
                **parent,
                "qty": normalized_quantity(remaining_qty),
                "estimated_notional_usd": remaining_qty * per_unit,
                "actual_notional_usd": remaining_qty * per_unit,
                "skip_reason": "deferred by per-run or daily execution caps",
            })
    return selected, held_back


def trade_order_ref(trade) -> str:
    return str(getattr(getattr(trade, "order", None), "orderRef", "") or "")


def owned_open_trades(ib, account_id: str) -> list:
    try:
        trades = ib.reqAllOpenOrders()
        ib.sleep(0.5)
    except Exception:
        trades = []
    if not trades:
        trades = ib.openTrades()
    return [
        trade
        for trade in trades or []
        if trade_order_ref(trade).startswith(ORDER_REF_PREFIX)
        and str(getattr(trade.order, "account", "") or "") == account_id
    ]


def cancel_owned_orders(ib, account_id: str, timeout_seconds: float = 30.0) -> list:
    trades = owned_open_trades(ib, account_id)
    for trade in trades:
        ib.cancelOrder(trade.order)
    deadline = time.monotonic() + timeout_seconds
    while trades and time.monotonic() < deadline:
        ib.sleep(0.5)
        trades = owned_open_trades(ib, account_id)
    return trades


def _trade_status(trade) -> dict:
    status = trade.orderStatus
    return {
        "order_id": int(getattr(trade.order, "orderId", 0) or 0) or None,
        "perm_id": int(getattr(trade.order, "permId", 0) or 0) or None,
        "status": str(getattr(status, "status", "") or "unknown"),
        "filled_qty": float(getattr(status, "filled", 0) or 0),
        "remaining_qty": float(getattr(status, "remaining", 0) or 0),
        "avg_fill_price": finite_positive(getattr(status, "avgFillPrice", None)),
        "limit_price": finite_positive(getattr(trade.order, "lmtPrice", None)),
    }


def recover_broker_orders(connection, ib, account_id: str) -> int:
    trades = []
    for getter in (
        lambda: ib.trades(),
        lambda: ib.reqCompletedOrders(False),
        lambda: ib.reqAllOpenOrders(),
    ):
        try:
            trades.extend(getter() or [])
        except Exception:
            continue
    recovered = 0
    seen_refs = set()
    for trade in trades:
        order_ref = trade_order_ref(trade)
        if (
            not order_ref.startswith(ORDER_REF_PREFIX)
            or order_ref in seen_refs
            or str(getattr(trade.order, "account", "") or "") != account_id
        ):
            continue
        seen_refs.add(order_ref)
        entry = ibkr_state.order_by_ref(connection, order_ref)
        if not entry:
            continue
        status = _trade_status(trade)
        fills = _fill_records(trade, entry["intent_id"])
        filled_at = max(
            (fill.get("filled_at") for fill in fills if fill.get("filled_at")),
            default=None,
        )
        time_to_fill = None
        if status["status"] == "Filled" and entry.get("submitted_at"):
            try:
                submitted_at = datetime.datetime.fromisoformat(
                    str(entry["submitted_at"]).replace("Z", "+00:00")
                )
                fill_time = (
                    datetime.datetime.fromisoformat(str(filled_at).replace("Z", "+00:00"))
                    if filled_at
                    else datetime.datetime.now(datetime.timezone.utc)
                )
                time_to_fill = max(0.0, (fill_time - submitted_at).total_seconds())
            except (TypeError, ValueError):
                time_to_fill = None
        actual_notional = entry.get("actual_notional_usd")
        if (
            status.get("avg_fill_price")
            and status.get("filled_qty")
            and entry.get("fx_rate")
        ):
            actual_notional = (
                float(status["filled_qty"])
                * float(status["avg_fill_price"])
                * float(entry.get("multiplier") or 1)
                * float(entry.get("price_multiplier") or 1)
                * float(entry["fx_rate"])
            )
        ibkr_state.update_order(
            connection,
            entry["intent_id"],
            **status,
            filled_at=filled_at,
            time_to_fill_seconds=time_to_fill,
            actual_notional_usd=actual_notional,
        )
        for fill in fills:
            ibkr_state.upsert_fill(connection, fill)
        recovered += 1
    return recovered


def _fill_records(trade, intent_id: str) -> list[dict]:
    records = []
    for fill in getattr(trade, "fills", []) or []:
        execution = getattr(fill, "execution", None)
        if execution is None:
            continue
        records.append({
            "exec_id": getattr(execution, "execId", None),
            "intent_id": intent_id,
            "order_id": getattr(execution, "orderId", None),
            "perm_id": getattr(execution, "permId", None),
            "qty": getattr(execution, "shares", None),
            "price": getattr(execution, "price", None),
            "currency": getattr(getattr(fill, "contract", None), "currency", None),
            "filled_at": str(getattr(execution, "time", "") or ""),
        })
    return records


def submit_orders(
    ib,
    connection,
    account_id: str,
    run_id: str,
    orders: list[dict],
    plans_by_con_id: dict[int, ibkr_contracts.ContractPlan],
    outside_rth: bool = False,
) -> list[dict]:
    submitted = []
    for sequence, order in enumerate(orders, start=1):
        order_ref = f"{ORDER_REF_PREFIX}{run_id}:{sequence:03d}"
        existing = ibkr_state.order_by_ref(connection, order_ref)
        if existing:
            submitted.append(existing)
            continue
        intent = ibkr_state.create_order_intent(
            connection,
            run_id,
            account_id,
            {**order, "order_ref": order_ref},
        )
        plan = plans_by_con_id.get(int(order["con_id"]))
        if plan is None:
            ibkr_state.update_order(
                connection,
                intent["intent_id"],
                status="validation_error",
                rejection_reason="missing exact contract plan",
            )
            raise RuntimeError(f"missing exact contract plan for conId {order['con_id']}")
        contract = ibkr_contracts.to_ib_async_contract(plan, exchange="SMART")
        ib_order = ibkr_contracts.limit_order(
            order["side"],
            order["qty"],
            float(order["limit_price"]),
            outside_rth=outside_rth,
            account_id=account_id,
            order_ref=order_ref,
        )
        submitted_at = ibkr_state.utc_now()
        try:
            trade = ib.placeOrder(contract, ib_order)
            ib.sleep(0.5)
        except Exception as exc:
            ibkr_state.update_order(
                connection,
                intent["intent_id"],
                status="submission_unknown",
                rejection_reason=str(exc),
                submitted_at=submitted_at,
            )
            raise
        status = _trade_status(trade)
        ibkr_state.update_order(
            connection,
            intent["intent_id"],
            submitted_qty=float(order["qty"]),
            submitted_at=submitted_at,
            actual_notional_usd=float(order["actual_notional_usd"]),
            **status,
        )
        for fill in _fill_records(trade, intent["intent_id"]):
            ibkr_state.upsert_fill(connection, fill)
        submitted.append({
            **order,
            **status,
            "intent_id": intent["intent_id"],
            "order_ref": order_ref,
        })
    return submitted


def positions_by_con_id(positions: list[dict]) -> dict[int, float]:
    result = defaultdict(float)
    for position in positions:
        if position.get("con_id"):
            result[int(position["con_id"])] += float(position.get("position") or 0)
    return dict(result)
