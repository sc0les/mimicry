import datetime
from pathlib import Path

import pytest

import ibkr_contracts
import ibkr_execution
import ibkr_state
import mirror_ibkr


def quote(con_id, bid=99.0, ask=101.0, age=1.0, frozen=False):
    return ibkr_contracts.Quote(
        con_id=con_id,
        bid=bid,
        ask=ask,
        last=(bid + ask) / 2,
        close=(bid + ask) / 2,
        timestamp=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        age_seconds=age,
        market_data_type=2 if frozen else 1,
        frozen=frozen,
    )


def row(con_id=1, weight=10.0, sec_type="STK", multiplier=1.0, currency="USD"):
    return {
        "figi": f"FIGI-{con_id}",
        "con_id": con_id,
        "ticker": f"T{con_id}",
        "local_symbol": f"T{con_id}",
        "weight": weight,
        "sec_type": sec_type,
        "currency": currency,
        "country": "United States" if currency == "USD" else "Foreign",
        "exchange": "SMART",
        "lot_size": 1,
        "multiplier": multiplier,
        "price_multiplier": 1.0,
        "market_rules": [[0.0, 0.01]],
        "skip_reason": None,
    }


def position(con_id, qty, sec_type="STK", currency="USD", multiplier=1.0):
    return {
        "con_id": con_id,
        "symbol": f"T{con_id}",
        "local_symbol": f"T{con_id}",
        "position": qty,
        "sec_type": sec_type,
        "currency": currency,
        "multiplier": multiplier,
        "price_multiplier": 1.0,
        "market_rules": [[0.0, 0.01]],
        "lot_size": 1,
    }


def plan(con_id=1, sec_type="STK"):
    return ibkr_contracts.ContractPlan(
        figi=f"FIGI-{con_id}",
        symbol=f"T{con_id}",
        exchange="SMART",
        primary_exchange="",
        currency="USD",
        sec_type=sec_type,
        country="United States",
        lot_size=1,
        name=f"T{con_id}",
        source_exch_code="US",
        needs_contract_details=False,
        con_id=con_id,
        market_rules=[[0.0, 0.01]],
    )


def test_target_netting():
    assert mirror_ibkr.net_weights({"A": 4.0, "B": 2.0}, {"A": 1.5, "C": 3.0}) == {
        "A": 2.5,
        "B": 2.0,
        "C": -3.0,
    }


def test_orphan_exit_bypasses_minimum_delta():
    orders, held = ibkr_execution.plan_order_deltas(
        [row(1, weight=10)],
        [position(1, 50), position(2, 0.1)],
        {1: quote(1), 2: quote(2, bid=9.9, ask=10.1)},
        {"USD": 1.0},
        50_000,
    )
    orphan = next(order for order in orders if order["con_id"] == 2)
    assert orphan["complete_exit"] is True
    assert orphan["orphan_exit"] is True
    assert orphan["side"] == "SELL"
    assert orphan["qty"] == 0.1
    assert not held

    selected, deferred = ibkr_execution.slice_orders([orphan], netliq=50_000)
    assert selected[0]["qty"] == 0.1
    assert not deferred


def test_stale_and_frozen_quotes_are_not_executable():
    stale = quote(1, age=1_201)
    frozen = quote(2, frozen=True)
    assert "stale" in ibkr_execution.quote_validation_error(stale)
    assert "frozen" in ibkr_execution.quote_validation_error(frozen)


def test_market_rule_limit_is_marketable_and_bounded():
    value, error = ibkr_execution.marketable_limit_price(
        quote(1, bid=99.9, ask=100.0),
        "BUY",
        [[0.0, 0.05]],
    )
    assert error is None
    assert value == 100.05


def test_ibkr_liquid_hours_distinguish_closed_from_missing_quote():
    instrument = {
        "liquid_hours": (
            "20260717:20260717:0930-20260717:1600;"
            "20260718:CLOSED"
        ),
        "time_zone_id": "America/New_York",
    }
    open_time = datetime.datetime(
        2026,
        7,
        17,
        14,
        0,
        tzinfo=datetime.timezone.utc,
    )
    closed_time = datetime.datetime(
        2026,
        7,
        18,
        14,
        0,
        tzinfo=datetime.timezone.utc,
    )
    assert ibkr_execution.market_is_open(instrument, open_time) is True
    assert ibkr_execution.market_is_open(instrument, closed_time) is False


def test_real_notional_includes_option_multiplier_and_fx():
    option = row(1, sec_type="OPT", multiplier=100, currency="CAD")
    assert ibkr_execution.order_notional_usd(2, 3.0, option, 0.75) == 450.0


def test_large_order_is_sliced_not_rejected():
    parent = {
        **row(1),
        "side": "BUY",
        "qty": 100,
        "limit_price": 100.0,
        "actual_notional_usd": 10_000.0,
        "estimated_notional_usd": 10_000.0,
    }
    selected, held = ibkr_execution.slice_orders([parent], netliq=50_000)
    assert [child["qty"] for child in selected] == [25, 25, 25, 25]
    assert not held
    assert sum(child["actual_notional_usd"] for child in selected) == 10_000


def test_unbuyable_board_lot_is_reported_not_silently_dropped():
    foreign = {
        **row(1, weight=0.5, currency="JPY"),
        "country": "Japan",
        "lot_size": 100,
    }
    orders, held = ibkr_execution.plan_order_deltas(
        [foreign],
        [],
        {1: quote(1, bid=999, ask=1_001)},
        {"JPY": 0.0065},
        50_000,
    )
    assert not orders
    assert held[0]["skip_reason"] == "target is smaller than one tradable lot"


def test_equity_short_is_marked_only_when_new_short_exposure_opens():
    orders, _held = ibkr_execution.plan_order_deltas(
        [row(1, weight=-10)],
        [position(1, -10)],
        {1: quote(1)},
        {"USD": 1.0},
        50_000,
    )
    assert orders[0]["opens_short"] is True
    assert orders[0]["short_open_qty"] == 40


def test_negative_option_target_blocks_execution():
    with pytest.raises(RuntimeError, match="negative option"):
        ibkr_execution.plan_order_deltas(
            [row(1, weight=-1, sec_type="OPT", multiplier=100)],
            [],
            {1: quote(1, bid=4.9, ask=5.1)},
            {"USD": 1.0},
            50_000,
        )


def test_option_target_uses_premium_times_multiplier_and_one_contract_minimum():
    option = row(1, weight=0.2, sec_type="OPT", multiplier=100)
    assert ibkr_execution.target_quantity(option, 50_000, 12.0, 1.0) == 1
    option["weight"] = 5.0
    assert ibkr_execution.target_quantity(option, 50_000, 5.0, 1.0) == 5


def test_execution_fx_rejects_frozen_rate():
    fx_quote = ibkr_contracts.FxQuote(
        currency="EUR",
        usd_rate=1.2,
        quote=quote(77, bid=1.19, ask=1.21, frozen=True),
        inverted=False,
    )
    rates, errors = ibkr_execution.executable_fx_rates({"EUR"}, {"EUR": fx_quote})
    assert rates == {}
    assert "frozen" in errors["EUR"]


def test_expiration_cutoff_forces_option_exit():
    option = {
        **row(1, weight=2, sec_type="OPT", multiplier=100),
        "expiry": "20260717",
    }
    now = datetime.datetime(2026, 7, 17, 15, 31, tzinfo=datetime.timezone(
        datetime.timedelta(hours=-4)
    ))
    force, reason = ibkr_execution.option_expiry_action(option, current_qty=1, now=now)
    assert force is True
    assert "3:30" in reason

    close_cycle = datetime.datetime(2026, 7, 17, 15, 15, tzinfo=datetime.timezone(
        datetime.timedelta(hours=-4)
    ))
    force, _reason = ibkr_execution.option_expiry_action(
        option,
        current_qty=1,
        now=close_cycle,
    )
    assert force is True


class AccountIB:
    def managedAccounts(self):
        return ["DU111", "U222"]


def test_exact_account_selection(monkeypatch):
    monkeypatch.setenv("IBKR_ACCOUNT_ID", "U222")
    assert ibkr_contracts.require_account_id(AccountIB()) == "U222"
    monkeypatch.setenv("IBKR_ACCOUNT_ID", "U22")
    with pytest.raises(RuntimeError, match="exactly match"):
        ibkr_contracts.require_account_id(AccountIB())


class FakeStatus:
    status = "Submitted"
    filled = 0
    remaining = 1


class FakeOrder:
    orderId = 0
    permId = 0


class FakeTrade:
    orderStatus = FakeStatus()
    order = FakeOrder()
    fills = []


class CrashAfterOneIB:
    def __init__(self):
        self.calls = 0
        self.seen_orders = []

    def placeOrder(self, _contract, order):
        self.calls += 1
        self.seen_orders.append(order)
        if self.calls == 2:
            raise RuntimeError("simulated disconnect")
        trade = FakeTrade()
        trade.order.orderId = self.calls
        trade.order.permId = 100 + self.calls
        trade.order.orderRef = order.orderRef
        return trade

    def sleep(self, _seconds):
        return None


def test_mid_batch_failure_is_durable_and_retry_does_not_duplicate(tmp_path: Path):
    connection = ibkr_state.connect(tmp_path / "state.sqlite3")
    ibkr_state.start_run(connection, "RUN1", "paper", "DU111", "2026-07-17", 50_000)
    orders = [
        {
            **row(1),
            "side": "BUY",
            "qty": 1,
            "limit_price": 100.0,
            "actual_notional_usd": 100.0,
            "estimated_notional_usd": 100.0,
            "fx_rate": 1.0,
        },
        {
            **row(2),
            "side": "BUY",
            "qty": 1,
            "limit_price": 100.0,
            "actual_notional_usd": 100.0,
            "estimated_notional_usd": 100.0,
            "fx_rate": 1.0,
        },
    ]
    fake = CrashAfterOneIB()
    with pytest.raises(RuntimeError, match="simulated disconnect"):
        ibkr_execution.submit_orders(
            fake,
            connection,
            "DU111",
            "RUN1",
            orders,
            {1: plan(1), 2: plan(2)},
        )
    persisted = connection.execute("SELECT * FROM orders ORDER BY order_ref").fetchall()
    assert len(persisted) == 2
    assert persisted[0]["status"] == "Submitted"
    assert persisted[1]["status"] == "submission_unknown"
    assert fake.seen_orders[0].account == "DU111"
    assert fake.seen_orders[0].orderRef == "MIMICRY:RUN1:001"

    retry = ibkr_execution.submit_orders(
        fake,
        connection,
        "DU111",
        "RUN1",
        orders,
        {1: plan(1), 2: plan(2)},
    )
    assert len(retry) == 2
    assert fake.calls == 2
    connection.close()


class OpenOrder:
    def __init__(self, order_ref, account="DU111"):
        self.orderRef = order_ref
        self.account = account
        self.action = "BUY"
        self.totalQuantity = 1


class OpenTrade:
    def __init__(self, order_ref, account="DU111"):
        self.order = OpenOrder(order_ref, account)
        self.contract = type("Contract", (), {"conId": 1})()
        self.orderStatus = type("Status", (), {"remaining": 1})()


class CancelIB:
    def __init__(self):
        self.trades = [
            OpenTrade("MIMICRY:OLD:001"),
            OpenTrade("MANUAL:001"),
            OpenTrade("MIMICRY:OTHER:001", account="DU999"),
        ]
        self.cancelled = []

    def reqAllOpenOrders(self):
        return list(self.trades)

    def openTrades(self):
        return list(self.trades)

    def cancelOrder(self, order):
        self.cancelled.append(order.orderRef)
        self.trades = [trade for trade in self.trades if trade.order is not order]

    def sleep(self, _seconds):
        return None


def test_stale_order_replacement_cancels_only_owned_account_orders():
    fake = CancelIB()
    remaining = ibkr_execution.cancel_owned_orders(fake, "DU111")
    assert fake.cancelled == ["MIMICRY:OLD:001"]
    assert remaining == []
    assert {trade.order.orderRef for trade in fake.trades} == {
        "MANUAL:001",
        "MIMICRY:OTHER:001",
    }
