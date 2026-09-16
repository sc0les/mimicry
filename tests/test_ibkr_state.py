import json

import ibkr_state


def test_legacy_json_import_is_one_time_and_non_destructive(tmp_path):
    legacy = tmp_path / "ledger.json"
    legacy.write_text(json.dumps({
        "orders": [{
            "run_id": "OLD1",
            "account_id": "DU111",
            "date": "2026-06-05",
            "timestamp": "2026-06-05T14:00:00+00:00",
            "ticker": "A",
            "con_id": 1,
            "side": "BUY",
            "qty": 2,
            "filled_qty": 2,
            "remaining_qty": 0,
            "status": "Filled",
        }]
    }))
    connection = ibkr_state.connect(tmp_path / "state.sqlite3")
    assert ibkr_state.import_legacy_ledger(connection, legacy) == 1
    assert ibkr_state.import_legacy_ledger(connection, legacy) == 0
    assert legacy.exists()
    assert connection.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 1
    connection.close()


def test_live_baseline_and_paper_netliq_are_separate(tmp_path):
    connection = ibkr_state.connect(tmp_path / "state.sqlite3")
    ibkr_state.set_live_baseline(connection, "U111", 50_000)
    ibkr_state.start_run(
        connection,
        "PAPER1",
        "paper",
        "DU111",
        "2026-07-17",
        1_000_000,
    )
    assert ibkr_state.live_baseline(connection, "U111")["netliq"] == 50_000
    rows = connection.execute(
        "SELECT mode, account_id, kind, netliq FROM netliq_state ORDER BY captured_at"
    ).fetchall()
    assert [tuple(row) for row in rows] == [
        ("live", "U111", "baseline", 50_000),
        ("paper", "DU111", "execution", 1_000_000),
    ]
    connection.close()
