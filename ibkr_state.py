#!/usr/bin/env python3
"""Transactional state and audit storage for the IBKR mirror."""

from __future__ import annotations

import datetime
import json
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


ROOT = Path(__file__).resolve().parent
STATE_DB_FILE = ROOT / "mimicry_state.sqlite3"
LEGACY_LEDGER_FILE = ROOT / "ibkr_execution_ledger.json"
SCHEMA_VERSION = 4


def utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    db_path = Path(path) if path is not None else STATE_DB_FILE
    connection = sqlite3.connect(db_path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = FULL")
    connection.execute("PRAGMA busy_timeout = 30000")
    initialize_schema(connection)
    return connection


def initialize_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS runs (
            run_id TEXT PRIMARY KEY,
            mode TEXT NOT NULL,
            account_id TEXT,
            source_date TEXT,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            status TEXT NOT NULL,
            netliq REAL,
            error TEXT
        );

        CREATE TABLE IF NOT EXISTS orders (
            intent_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES runs(run_id),
            order_ref TEXT NOT NULL UNIQUE,
            account_id TEXT NOT NULL,
            figi TEXT,
            con_id INTEGER,
            ticker TEXT,
            sec_type TEXT NOT NULL,
            action TEXT NOT NULL,
            requested_qty REAL NOT NULL,
            submitted_qty REAL,
            filled_qty REAL NOT NULL DEFAULT 0,
            remaining_qty REAL,
            avg_fill_price REAL,
            time_to_fill_seconds REAL,
            limit_price REAL,
            currency TEXT,
            fx_rate REAL,
            multiplier REAL NOT NULL DEFAULT 1,
            price_multiplier REAL NOT NULL DEFAULT 1,
            estimated_notional_usd REAL,
            actual_notional_usd REAL,
            order_id INTEGER,
            perm_id INTEGER,
            status TEXT NOT NULL,
            rejection_reason TEXT,
            created_at TEXT NOT NULL,
            submitted_at TEXT,
            updated_at TEXT NOT NULL,
            filled_at TEXT,
            raw_json TEXT
        );

        CREATE INDEX IF NOT EXISTS orders_run_idx ON orders(run_id);
        CREATE INDEX IF NOT EXISTS orders_order_id_idx ON orders(order_id);
        CREATE INDEX IF NOT EXISTS orders_perm_id_idx ON orders(perm_id);
        CREATE INDEX IF NOT EXISTS orders_created_idx ON orders(created_at);

        CREATE TABLE IF NOT EXISTS fills (
            exec_id TEXT PRIMARY KEY,
            intent_id TEXT REFERENCES orders(intent_id),
            order_id INTEGER,
            perm_id INTEGER,
            qty REAL NOT NULL,
            price REAL NOT NULL,
            commission REAL,
            currency TEXT,
            filled_at TEXT,
            raw_json TEXT
        );

        CREATE TABLE IF NOT EXISTS reconciliations (
            reconciliation_id TEXT PRIMARY KEY,
            run_id TEXT,
            mode TEXT NOT NULL,
            account_id TEXT NOT NULL,
            started_at TEXT NOT NULL,
            completed_at TEXT NOT NULL,
            status TEXT NOT NULL,
            netliq REAL NOT NULL,
            drift_count INTEGER NOT NULL,
            max_drift_pct REAL NOT NULL,
            report TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS reconciliations_run_idx
            ON reconciliations(run_id);

        CREATE TABLE IF NOT EXISTS netliq_state (
            snapshot_id TEXT PRIMARY KEY,
            run_id TEXT,
            mode TEXT NOT NULL,
            account_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            netliq REAL NOT NULL,
            captured_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS netliq_account_idx
            ON netliq_state(account_id, mode, captured_at);
        """
    )
    ensure_column(connection, "orders", "avg_fill_price", "REAL")
    ensure_column(connection, "orders", "time_to_fill_seconds", "REAL")
    ensure_column(
        connection,
        "orders",
        "price_multiplier",
        "REAL NOT NULL DEFAULT 1",
    )
    set_meta(connection, "schema_version", str(SCHEMA_VERSION))
    connection.commit()


def ensure_column(
    connection: sqlite3.Connection,
    table: str,
    column: str,
    declaration: str,
) -> None:
    columns = {
        str(row["name"])
        for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
    }
    if column not in columns:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")


@contextmanager
def transaction(connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    try:
        connection.execute("BEGIN IMMEDIATE")
        yield connection
    except Exception:
        connection.rollback()
        raise
    else:
        connection.commit()


def set_meta(connection: sqlite3.Connection, key: str, value: str) -> None:
    connection.execute(
        """
        INSERT INTO meta(key, value, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET
            value = excluded.value,
            updated_at = excluded.updated_at
        """,
        (key, value, utc_now()),
    )


def get_meta(connection: sqlite3.Connection, key: str) -> str | None:
    row = connection.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return str(row["value"]) if row else None


def start_run(
    connection: sqlite3.Connection,
    run_id: str,
    mode: str,
    account_id: str | None,
    source_date: str,
    netliq: float | None,
) -> None:
    connection.execute(
        """
        INSERT INTO runs(run_id, mode, account_id, source_date, started_at, status, netliq)
        VALUES (?, ?, ?, ?, ?, 'running', ?)
        """,
        (run_id, mode, account_id, source_date, utc_now(), netliq),
    )
    record_netliq(
        connection,
        mode=mode,
        account_id=account_id or "",
        kind="execution",
        netliq=float(netliq),
        run_id=run_id,
        commit=False,
    ) if netliq is not None else None
    connection.commit()


def finish_run(connection: sqlite3.Connection, run_id: str, status: str, error: str | None = None) -> None:
    connection.execute(
        """
        UPDATE runs
        SET completed_at = ?, status = ?, error = ?
        WHERE run_id = ?
        """,
        (utc_now(), status, error, run_id),
    )
    connection.commit()


def make_intent_id() -> str:
    return uuid.uuid4().hex


def create_order_intent(
    connection: sqlite3.Connection,
    run_id: str,
    account_id: str,
    order: dict,
    intent_id: str | None = None,
) -> dict:
    intent_id = intent_id or make_intent_id()
    order_ref = order.get("order_ref") or f"MIMICRY:{run_id}:{intent_id[:12]}"
    now = utc_now()
    payload = {
        **order,
        "intent_id": intent_id,
        "run_id": run_id,
        "order_ref": order_ref,
        "account_id": account_id,
        "status": "intent",
        "created_at": now,
        "updated_at": now,
    }
    connection.execute(
        """
        INSERT INTO orders(
            intent_id, run_id, order_ref, account_id, figi, con_id, ticker,
            sec_type, action, requested_qty, remaining_qty, limit_price,
            currency, fx_rate, multiplier, estimated_notional_usd,
            price_multiplier, actual_notional_usd, status, created_at,
            updated_at, raw_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            intent_id,
            run_id,
            order_ref,
            account_id,
            order.get("figi"),
            order.get("con_id"),
            order.get("ticker"),
            order.get("sec_type", "STK"),
            order.get("side") or order.get("action"),
            float(order.get("qty") or order.get("requested_qty") or 0),
            float(order.get("qty") or order.get("requested_qty") or 0),
            order.get("limit_price"),
            order.get("currency"),
            order.get("fx_rate"),
            float(order.get("multiplier") or 1),
            order.get("estimated_notional_usd"),
            float(order.get("price_multiplier") or 1),
            order.get("actual_notional_usd"),
            "intent",
            now,
            now,
            json.dumps(payload, sort_keys=True, default=str),
        ),
    )
    connection.commit()
    return payload


def update_order(connection: sqlite3.Connection, intent_id: str, **fields) -> None:
    allowed = {
        "submitted_qty",
        "filled_qty",
        "remaining_qty",
        "avg_fill_price",
        "time_to_fill_seconds",
        "limit_price",
        "actual_notional_usd",
        "order_id",
        "perm_id",
        "status",
        "rejection_reason",
        "submitted_at",
        "filled_at",
        "raw_json",
    }
    updates = {key: value for key, value in fields.items() if key in allowed}
    updates["updated_at"] = utc_now()
    columns = ", ".join(f"{key} = ?" for key in updates)
    connection.execute(
        f"UPDATE orders SET {columns} WHERE intent_id = ?",
        (*updates.values(), intent_id),
    )
    connection.commit()


def upsert_fill(connection: sqlite3.Connection, fill: dict) -> None:
    exec_id = str(fill.get("exec_id") or fill.get("execId") or "").strip()
    if not exec_id:
        return
    connection.execute(
        """
        INSERT INTO fills(
            exec_id, intent_id, order_id, perm_id, qty, price, commission,
            currency, filled_at, raw_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(exec_id) DO UPDATE SET
            intent_id = excluded.intent_id,
            order_id = excluded.order_id,
            perm_id = excluded.perm_id,
            qty = excluded.qty,
            price = excluded.price,
            commission = excluded.commission,
            currency = excluded.currency,
            filled_at = excluded.filled_at,
            raw_json = excluded.raw_json
        """,
        (
            exec_id,
            fill.get("intent_id"),
            fill.get("order_id"),
            fill.get("perm_id"),
            float(fill.get("qty") or 0),
            float(fill.get("price") or 0),
            fill.get("commission"),
            fill.get("currency"),
            fill.get("filled_at"),
            json.dumps(fill, sort_keys=True, default=str),
        ),
    )
    connection.commit()


def order_by_ref(connection: sqlite3.Connection, order_ref: str) -> dict | None:
    row = connection.execute("SELECT * FROM orders WHERE order_ref = ?", (order_ref,)).fetchone()
    return dict(row) if row else None


def latest_run_orders(
    connection: sqlite3.Connection,
    mode: str | None = None,
    account_id: str | None = None,
) -> list[dict]:
    filters = ["mode IN ('paper', 'live')"]
    params: list[str] = []
    if mode:
        filters.append("mode = ?")
        params.append(mode)
    if account_id:
        filters.append("account_id = ?")
        params.append(account_id)
    row = connection.execute(
        f"""
        SELECT run_id
        FROM runs
        WHERE {' AND '.join(filters)}
        ORDER BY started_at DESC
        LIMIT 1
        """,
        params,
    ).fetchone()
    if not row:
        return []
    return [
        dict(item)
        for item in connection.execute(
            "SELECT * FROM orders WHERE run_id = ? ORDER BY created_at",
            (row["run_id"],),
        ).fetchall()
    ]


def todays_order_count(connection: sqlite3.Connection, day: datetime.date | None = None) -> int:
    day = day or datetime.date.today()
    local_tz = datetime.datetime.now().astimezone().tzinfo
    start = datetime.datetime.combine(day, datetime.time.min, tzinfo=local_tz).astimezone(
        datetime.timezone.utc
    )
    end = start + datetime.timedelta(days=1)
    row = connection.execute(
        """
        SELECT COUNT(*) AS count
        FROM orders
        WHERE created_at >= ? AND created_at < ?
          AND status NOT IN ('intent', 'skipped', 'cancelled', 'ApiCancelled', 'Inactive')
        """,
        (start.isoformat(), end.isoformat()),
    ).fetchone()
    return int(row["count"])


def set_live_baseline(connection: sqlite3.Connection, account_id: str, netliq: float) -> None:
    set_meta(
        connection,
        f"live_baseline:{account_id}",
        json.dumps({"netliq": float(netliq), "recorded_at": utc_now()}),
    )
    record_netliq(
        connection,
        mode="live",
        account_id=account_id,
        kind="baseline",
        netliq=netliq,
        commit=False,
    )
    connection.commit()


def live_baseline(connection: sqlite3.Connection, account_id: str) -> dict | None:
    value = get_meta(connection, f"live_baseline:{account_id}")
    return json.loads(value) if value else None


def set_last_completed_netliq(
    connection: sqlite3.Connection,
    mode: str,
    account_id: str,
    netliq: float,
) -> None:
    set_meta(
        connection,
        f"last_completed_netliq:{mode}:{account_id}",
        json.dumps({"netliq": float(netliq), "recorded_at": utc_now()}),
    )
    connection.commit()


def last_completed_netliq(connection: sqlite3.Connection, mode: str, account_id: str) -> dict | None:
    value = get_meta(connection, f"last_completed_netliq:{mode}:{account_id}")
    return json.loads(value) if value else None


def record_netliq(
    connection: sqlite3.Connection,
    mode: str,
    account_id: str,
    kind: str,
    netliq: float,
    run_id: str | None = None,
    commit: bool = True,
) -> str:
    snapshot_id = uuid.uuid4().hex
    connection.execute(
        """
        INSERT INTO netliq_state(
            snapshot_id, run_id, mode, account_id, kind, netliq, captured_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            snapshot_id,
            run_id,
            mode,
            account_id,
            kind,
            float(netliq),
            utc_now(),
        ),
    )
    if commit:
        connection.commit()
    return snapshot_id


def record_reconciliation(
    connection: sqlite3.Connection,
    mode: str,
    account_id: str,
    netliq: float,
    drift_count: int,
    max_drift_pct: float,
    status: str,
    report: str,
    run_id: str | None = None,
) -> str:
    reconciliation_id = uuid.uuid4().hex
    now = utc_now()
    connection.execute(
        """
        INSERT INTO reconciliations(
            reconciliation_id, run_id, mode, account_id, started_at,
            completed_at, status, netliq, drift_count, max_drift_pct, report
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            reconciliation_id,
            run_id,
            mode,
            account_id,
            now,
            now,
            status,
            float(netliq),
            int(drift_count),
            float(max_drift_pct),
            report,
        ),
    )
    connection.commit()
    return reconciliation_id


def import_legacy_ledger(
    connection: sqlite3.Connection,
    legacy_path: Path | str | None = None,
) -> int:
    path = Path(legacy_path) if legacy_path is not None else LEGACY_LEDGER_FILE
    migration_key = f"legacy_ledger_imported:{path.resolve()}"
    if get_meta(connection, migration_key) is not None or not path.exists():
        return 0

    data = json.loads(path.read_text())
    entries = data.get("orders", []) if isinstance(data, dict) else data
    imported = 0
    for index, entry in enumerate(entries):
        run_id = entry.get("run_id") or f"legacy-{entry.get('date', 'unknown')}-{index:06d}"
        connection.execute(
            """
            INSERT OR IGNORE INTO runs(
                run_id, mode, account_id, source_date, started_at,
                completed_at, status, netliq
            ) VALUES (?, 'legacy', ?, ?, ?, ?, 'imported', NULL)
            """,
            (
                run_id,
                entry.get("account_id"),
                entry.get("date"),
                entry.get("timestamp") or utc_now(),
                entry.get("timestamp") or utc_now(),
            ),
        )
        intent_id = f"legacy-{index:08d}"
        order_ref = entry.get("order_ref") or f"MIMICRY:LEGACY:{index:08d}"
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO orders(
                intent_id, run_id, order_ref, account_id, figi, con_id, ticker,
                sec_type, action, requested_qty, submitted_qty, filled_qty,
                remaining_qty, limit_price, currency, fx_rate, multiplier,
                estimated_notional_usd, actual_notional_usd, order_id, perm_id,
                status, rejection_reason, created_at, submitted_at, updated_at,
                filled_at, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                      ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                intent_id,
                run_id,
                order_ref,
                entry.get("account_id") or "legacy",
                entry.get("figi"),
                entry.get("con_id"),
                entry.get("ticker"),
                entry.get("sec_type") or "STK",
                entry.get("side") or "?",
                float(entry.get("requested_qty") or entry.get("qty") or 0),
                float(entry.get("requested_qty") or entry.get("qty") or 0),
                float(entry.get("filled_qty") or 0),
                float(entry.get("remaining_qty") or 0),
                entry.get("limit_price"),
                entry.get("currency"),
                entry.get("fx_rate"),
                float(entry.get("multiplier") or 1),
                entry.get("estimated_notional_usd"),
                entry.get("actual_notional_usd"),
                entry.get("order_id"),
                entry.get("perm_id"),
                entry.get("final_status") or entry.get("status") or "imported",
                entry.get("rejection_reason"),
                entry.get("timestamp") or utc_now(),
                entry.get("timestamp"),
                entry.get("reconciled_at") or entry.get("timestamp") or utc_now(),
                entry.get("filled_at"),
                json.dumps(entry, sort_keys=True, default=str),
            ),
        )
        imported += int(cursor.rowcount > 0)

    set_meta(connection, migration_key, json.dumps({"count": imported, "at": utc_now()}))
    connection.commit()
    return imported


def backup_database(source: Path | str, destination: Path | str) -> None:
    source_connection = connect(source)
    destination_path = Path(destination)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    destination_connection = sqlite3.connect(destination_path)
    try:
        source_connection.backup(destination_connection)
    finally:
        destination_connection.close()
        source_connection.close()
