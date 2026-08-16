"""
Tests for tracker/db.py - schema creation, the fare_type migration for
databases created before that column existed, and basic insert/export
round-trips. No network access.
"""
import sqlite3
import tempfile
from pathlib import Path

import pytest

from tracker import db


@pytest.fixture
def tmp_db_path():
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp) / "test.db"


def test_fresh_database_has_fare_type_column(tmp_db_path):
    with db.connect(tmp_db_path) as conn:
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(seller_offers)").fetchall()
        }
    assert "fare_type" in columns


def test_migration_adds_fare_type_to_pre_existing_table(tmp_db_path):
    # Simulate a database created by an OLDER version of this project,
    # before fare_type existed - exactly what's already sitting in the
    # user's repo after their first few runs.
    old_schema_conn = sqlite3.connect(tmp_db_path)
    old_schema_conn.executescript(
        """
        CREATE TABLE seller_offers (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            observation_id  INTEGER NOT NULL,
            checked_at      TEXT NOT NULL,
            seller          TEXT,
            price           REAL,
            currency        TEXT,
            source          TEXT
        );
        """
    )
    old_schema_conn.commit()
    old_schema_conn.close()

    # Now open it with the current code - the migration should add the
    # missing column without losing anything or raising.
    with db.connect(tmp_db_path) as conn:
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(seller_offers)").fetchall()
        }
        assert "fare_type" in columns

    # And it should be idempotent - connecting again doesn't error.
    with db.connect(tmp_db_path) as conn:
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(seller_offers)").fetchall()
        }
        assert "fare_type" in columns


def test_insert_and_export_seller_offers_round_trip(tmp_db_path):
    with db.connect(tmp_db_path) as conn:
        obs_id = db.insert_observation(
            conn,
            {
                "checked_at": "2026-08-17T00:00:00+03:00",
                "flight_date": "2026-09-17",
                "flight_number": "PC2476",
                "status": "SUCCESS",
                "pc2476_price": 1749,
                "counted_toward_quota": 1,
            },
        )
        db.insert_seller_offers(
            conn,
            obs_id,
            "2026-08-17T00:00:00+03:00",
            "TRY",
            [
                {"seller": "Pegasus Airlines", "price": 1749, "source": "a", "fare_type": "Economy"},
                {"seller": "Pegasus Airlines", "price": 2698, "source": "b", "fare_type": "Economy Flex"},
                {"seller": "Turna.com", "price": 1762, "source": "c", "fare_type": None},
            ],
        )
        offers = db.export_seller_offers(conn)

    assert len(offers) == 3
    pegasus_rows = [o for o in offers if o["seller"] == "Pegasus Airlines"]
    assert {o["fare_type"] for o in pegasus_rows} == {"Economy", "Economy Flex"}
