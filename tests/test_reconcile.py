import datetime

import ibkr_contracts
import reconcile


def quote(con_id, bid=99.0, ask=101.0):
    return ibkr_contracts.Quote(
        con_id=con_id,
        bid=bid,
        ask=ask,
        last=100.0,
        close=100.0,
        timestamp=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        age_seconds=1.0,
        market_data_type=1,
        frozen=False,
    )


def test_reconciliation_accounts_for_working_orders():
    rows = [{
        "figi": "A",
        "con_id": 1,
        "ticker": "A",
        "weight": 10.0,
        "sec_type": "STK",
        "currency": "USD",
        "country": "United States",
        "lot_size": 1,
        "multiplier": 1.0,
        "price_multiplier": 1.0,
        "market_rules": [[0.0, 0.01]],
    }]
    positions = [{
        "con_id": 1,
        "position": 25,
        "sec_type": "STK",
        "currency": "USD",
    }]
    drifts, errors = reconcile.position_drifts(
        rows,
        positions,
        {1: quote(1)},
        {"USD": 1.0},
        {1: 25},
        netliq=50_000,
        threshold_pct=0.5,
    )
    assert not drifts
    assert not errors


def test_reconciliation_values_foreign_orphan_with_fx():
    positions = [{
        "con_id": 2,
        "symbol": "FOREIGN",
        "local_symbol": "FOREIGN",
        "position": 100,
        "sec_type": "STK",
        "currency": "EUR",
        "multiplier": 1.0,
        "price_multiplier": 1.0,
    }]
    drifts, errors = reconcile.position_drifts(
        [],
        positions,
        {2: quote(2, bid=49, ask=51)},
        {"EUR": 1.2},
        {},
        netliq=50_000,
        threshold_pct=0.5,
    )
    assert not errors
    assert drifts[0]["drift_value"] == 6_000
    assert drifts[0]["drift_pct"] == 12.0
