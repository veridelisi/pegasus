"""
SQLite persistence for observations.

One row = one scheduled execution's outcome, whatever that outcome was
(a price, or a documented reason there wasn't one). Nothing is silently
dropped - see tracker/extract.py and tracker/main.py for the possible
`status` values.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

from tracker.config import DB_PATH, ISTANBUL_TZ

SCHEMA = """
CREATE TABLE IF NOT EXISTS price_history (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    checked_at            TEXT NOT NULL,   -- ISO 8601, Europe/Istanbul aware
    flight_date           TEXT,
    flight_number         TEXT,
    departure_airport     TEXT,
    arrival_airport       TEXT,
    departure_time        TEXT,
    arrival_time          TEXT,

    pc2476_price          REAL,
    currency              TEXT,

    google_lowest_price       REAL,
    google_price_level        TEXT,
    google_typical_price_low  REAL,
    google_typical_price_high REAL,

    status                TEXT NOT NULL,
    error_message         TEXT,

    price_source          TEXT,   -- JSON path the price came from, e.g. booking_options[0].together.price
    seller                TEXT,
    serpapi_search_id     TEXT,
    monthly_call_number   INTEGER,
    counted_toward_quota  INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_price_history_checked_at
    ON price_history (checked_at);

-- One row per (observation, seller) offer found in booking_options for
-- that observation - NOT just the cheapest one that ends up in
-- price_history.pc2476_price. Lets you track how each individual
-- seller's price for PC2476 behaves over time.
CREATE TABLE IF NOT EXISTS seller_offers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    observation_id  INTEGER NOT NULL REFERENCES price_history(id),
    checked_at      TEXT NOT NULL,   -- duplicated from price_history for easy querying
    seller          TEXT,
    fare_type       TEXT,            -- e.g. "Basic Economy" - same seller can have several
    price           REAL,
    currency        TEXT,
    source          TEXT             -- e.g. booking_options[3].together.price
);

CREATE INDEX IF NOT EXISTS idx_seller_offers_observation
    ON seller_offers (observation_id);
CREATE INDEX IF NOT EXISTS idx_seller_offers_seller
    ON seller_offers (seller);
CREATE INDEX IF NOT EXISTS idx_seller_offers_checked_at
    ON seller_offers (checked_at);
"""

# Statuses that represent an actual SerpApi request having been made
# (and therefore should count toward the monthly quota / rate reporting).
STATUSES_THAT_CONSUME_QUOTA = {
    "SUCCESS",
    "PRICE_NOT_FOUND",
    "FLIGHT_NOT_FOUND",
    "FLIGHT_NOT_MATCHED",
    "INVALID_RESPONSE",
    "API_ERROR",
}
# Explicitly NOT counted: NETWORK_ERROR (request never confirmed reaching
# SerpApi), SKIPPED_DUPLICATE, MONTHLY_LIMIT_GUARD, TRACKING_COMPLETE.


@contextmanager
def connect(db_path: Path = DB_PATH):
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.executescript(SCHEMA)
        _migrate(conn)
        yield conn
        conn.commit()
    finally:
        conn.close()


def _migrate(conn: sqlite3.Connection) -> None:
    """Lightweight, additive-only migrations for columns introduced
    after a database file may already have been created. Safe to run
    on every connect() - each ALTER only fires if the column is
    genuinely missing.
    """
    existing_columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(seller_offers)").fetchall()
    }
    if "fare_type" not in existing_columns:
        conn.execute("ALTER TABLE seller_offers ADD COLUMN fare_type TEXT")


def now_istanbul_iso() -> str:
    return datetime.now(ISTANBUL_TZ).isoformat(timespec="seconds")


def insert_observation(conn: sqlite3.Connection, row: dict) -> int:
    """`row` keys must be a subset of the price_history columns above.
    Missing keys are stored as NULL.
    """
    columns = [
        "checked_at", "flight_date", "flight_number", "departure_airport",
        "arrival_airport", "departure_time", "arrival_time",
        "pc2476_price", "currency",
        "google_lowest_price", "google_price_level",
        "google_typical_price_low", "google_typical_price_high",
        "status", "error_message", "price_source", "seller",
        "serpapi_search_id", "monthly_call_number", "counted_toward_quota",
    ]
    values = [row.get(c) for c in columns]
    placeholders = ", ".join(["?"] * len(columns))
    sql = f"INSERT INTO price_history ({', '.join(columns)}) VALUES ({placeholders})"
    cur = conn.execute(sql, values)
    return cur.lastrowid


def insert_seller_offers(
    conn: sqlite3.Connection,
    observation_id: int,
    checked_at: str,
    currency: Optional[str],
    offers: list[dict],
) -> None:
    """`offers` is the ExtractionResult.all_offers list: each item is
    {"seller": str|None, "price": float, "source": str,
    "fare_type": str|None}. Safe to call with an empty list (does
    nothing). The SAME seller can legitimately appear multiple times
    per observation with different fare_type values (e.g. "Basic
    Economy" vs "Economy Plus") - that's not a bug.
    """
    for offer in offers or []:
        conn.execute(
            """
            INSERT INTO seller_offers
                (observation_id, checked_at, seller, fare_type, price, currency, source)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                observation_id,
                checked_at,
                offer.get("seller"),
                offer.get("fare_type"),
                offer.get("price"),
                currency,
                offer.get("source"),
            ),
        )


def export_seller_offers(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    cur = conn.execute(
        "SELECT * FROM seller_offers ORDER BY checked_at ASC, id ASC"
    )
    return cur.fetchall()


def get_latest_observation(conn: sqlite3.Connection) -> Optional[sqlite3.Row]:
    cur = conn.execute(
        "SELECT * FROM price_history ORDER BY checked_at DESC, id DESC LIMIT 1"
    )
    return cur.fetchone()


def get_latest_api_call_observation(conn: sqlite3.Connection) -> Optional[sqlite3.Row]:
    """Latest row that actually represents a SerpApi request attempt
    (used for the duplicate-protection window - we don't want a prior
    SKIPPED_DUPLICATE/MONTHLY_LIMIT_GUARD row to reset the clock).
    """
    cur = conn.execute(
        """
        SELECT * FROM price_history
        WHERE counted_toward_quota = 1
        ORDER BY checked_at DESC, id DESC LIMIT 1
        """
    )
    return cur.fetchone()


def get_latest_success(conn: sqlite3.Connection) -> Optional[sqlite3.Row]:
    cur = conn.execute(
        """
        SELECT * FROM price_history
        WHERE status = 'SUCCESS'
        ORDER BY checked_at DESC, id DESC LIMIT 1
        """
    )
    return cur.fetchone()


def count_calls_this_month(conn: sqlite3.Connection, year: int, month: int) -> int:
    prefix = f"{year:04d}-{month:02d}"
    cur = conn.execute(
        """
        SELECT COUNT(*) AS n FROM price_history
        WHERE counted_toward_quota = 1
          AND substr(checked_at, 1, 7) = ?
        """,
        (prefix,),
    )
    return cur.fetchone()["n"]


def row_count(conn: sqlite3.Connection) -> int:
    cur = conn.execute("SELECT COUNT(*) AS n FROM price_history")
    return cur.fetchone()["n"]


def export_all(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    cur = conn.execute("SELECT * FROM price_history ORDER BY checked_at ASC, id ASC")
    return cur.fetchall()
