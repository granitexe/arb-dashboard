"""
SQLite data store for market data from multiple exchanges.
"""

import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any


class DataStore:
    """Thread-safe SQLite store for aggregated market data."""

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS market_data (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp REAL NOT NULL,
        exchange TEXT NOT NULL,
        symbol TEXT NOT NULL,
        price REAL NOT NULL,
        volume_24h REAL,
        funding_rate REAL,
        open_interest REAL,
        premium_index REAL,
        created_at TEXT NOT NULL,
        UNIQUE(exchange, symbol, timestamp)
    );

    CREATE TABLE IF NOT EXISTS price_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp REAL NOT NULL,
        exchange TEXT NOT NULL,
        symbol TEXT NOT NULL,
        bid REAL,
        ask REAL,
        mid REAL,
        last REAL,
        volume REAL,
        created_at TEXT NOT NULL,
        UNIQUE(exchange, symbol, timestamp)
    );

    CREATE TABLE IF NOT EXISTS metadata (
        key TEXT PRIMARY KEY,
        value TEXT,
        updated_at TEXT NOT NULL
    );

    CREATE INDEX IF NOT EXISTS idx_market_data_exchange_symbol
        ON market_data(exchange, symbol);
    CREATE INDEX IF NOT EXISTS idx_market_data_timestamp
        ON market_data(timestamp);
    CREATE INDEX IF NOT EXISTS idx_price_snapshots_exchange_symbol
        ON price_snapshots(exchange, symbol);
    CREATE INDEX IF NOT EXISTS idx_price_snapshots_timestamp
        ON price_snapshots(timestamp);
    """

    def __init__(self, db_path: str = "market_data.db"):
        self.db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None
        self._lock = threading.RLock()
        self._init_db()

    def _init_db(self) -> None:
        with self._lock:
            self._conn = sqlite3.connect(
                self.db_path,
                check_same_thread=False,
                isolation_level="IMMEDIATE",
            )
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(self.SCHEMA)
            self._conn.commit()

    def insert_market_data(self, records: List[Dict[str, Any]]) -> int:
        """Insert market data records. Returns number of rows inserted."""
        if not records:
            return 0
        now = datetime.utcnow().isoformat()
        with self._lock:
            cur = self._conn.cursor()
            cur.executemany(
                """
                INSERT OR IGNORE INTO market_data
                (timestamp, exchange, symbol, price, volume_24h, funding_rate,
                 open_interest, premium_index, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        r["timestamp"],
                        r["exchange"],
                        r["symbol"],
                        r["price"],
                        r.get("volume_24h"),
                        r.get("funding_rate"),
                        r.get("open_interest"),
                        r.get("premium_index"),
                        now,
                    )
                    for r in records
                ],
            )
            self._conn.commit()
            return cur.rowcount

    def insert_price_snapshots(self, records: List[Dict[str, Any]]) -> int:
        """Insert price snapshot records. Returns number of rows inserted."""
        if not records:
            return 0
        now = datetime.utcnow().isoformat()
        with self._lock:
            cur = self._conn.cursor()
            cur.executemany(
                """
                INSERT OR IGNORE INTO price_snapshots
                (timestamp, exchange, symbol, bid, ask, mid, last, volume, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        r["timestamp"],
                        r["exchange"],
                        r["symbol"],
                        r.get("bid"),
                        r.get("ask"),
                        r.get("mid"),
                        r.get("last"),
                        r.get("volume"),
                        now,
                    )
                    for r in records
                ],
            )
            self._conn.commit()
            return cur.rowcount

    def set_metadata(self, key: str, value: str) -> None:
        """Set a metadata key-value pair."""
        now = datetime.utcnow().isoformat()
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO metadata (key, value, updated_at) VALUES (?, ?, ?)",
                (key, value, now),
            )
            self._conn.commit()

    def get_latest_price(
        self, exchange: str, symbol: str
    ) -> Optional[Dict[str, Any]]:
        """Get the most recent price for an exchange/symbol pair."""
        with self._lock:
            cur = self._conn.execute(
                """
                SELECT timestamp, price, volume_24h, funding_rate, open_interest
                FROM market_data
                WHERE exchange = ? AND symbol = ?
                ORDER BY timestamp DESC
                LIMIT 1
                """,
                (exchange, symbol),
            )
            row = cur.fetchone()
            if row:
                return {
                    "timestamp": row[0],
                    "price": row[1],
                    "volume_24h": row[2],
                    "funding_rate": row[3],
                    "open_interest": row[4],
                }
            return None

    def get_price_history(
        self,
        exchange: str,
        symbol: str,
        since: float,
        until: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """Get price history for an exchange/symbol pair within a time range."""
        with self._lock:
            query = """
                SELECT timestamp, price, volume_24h, funding_rate, open_interest
                FROM market_data
                WHERE exchange = ? AND symbol = ? AND timestamp >= ?
            """
            params: List[Any] = [exchange, symbol, since]
            if until is not None:
                query += " AND timestamp <= ?"
                params.append(until)
            query += " ORDER BY timestamp ASC"
            cur = self._conn.execute(query, params)
            rows = cur.fetchall()
            return [
                {
                    "timestamp": r[0],
                    "price": r[1],
                    "volume_24h": r[2],
                    "funding_rate": r[3],
                    "open_interest": r[4],
                }
                for r in rows
            ]

    def get_all_latest_prices(self) -> Dict[str, Dict[str, Dict[str, Any]]]:
        """Get latest price per exchange per symbol."""
        with self._lock:
            cur = self._conn.execute(
                """
                SELECT m.exchange, m.symbol, m.timestamp, m.price,
                       m.volume_24h, m.funding_rate, m.open_interest
                FROM market_data m
                INNER JOIN (
                    SELECT exchange, symbol, MAX(timestamp) as max_ts
                    FROM market_data
                    GROUP BY exchange, symbol
                ) latest ON m.exchange = latest.exchange
                     AND m.symbol = latest.symbol
                     AND m.timestamp = latest.max_ts
                """
            )
            rows = cur.fetchall()
            result: Dict[str, Dict[str, Dict[str, Any]]] = {}
            for r in rows:
                ex, sym = r[0], r[1]
                if ex not in result:
                    result[ex] = {}
                result[ex][sym] = {
                    "timestamp": r[2],
                    "price": r[3],
                    "volume_24h": r[4],
                    "funding_rate": r[5],
                    "open_interest": r[6],
                }
            return result

    def vacuum(self) -> None:
        """Run VACUUM to reclaim disk space."""
        with self._lock:
            self._conn.execute("VACUUM")

    def close(self) -> None:
        """Close the database connection."""
        with self._lock:
            if self._conn:
                self._conn.close()
                self._conn = None