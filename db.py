import sqlite3
import json
from datetime import datetime, timedelta
from dataclasses import dataclass, asdict
from typing import List, Optional


DB_PATH = "flight_data.db"


@dataclass
class FlightRecord:
    flight_number: str
    destination: str
    time: str
    date: str  # DD.MM format
    seats: Optional[int]  # None = no flight/no data
    book_url: Optional[str] = None


@dataclass
class Change:
    timestamp: str
    flight_number: str
    destination: str
    time: str
    date: str
    old_seats: Optional[int]
    new_seats: Optional[int]
    change_type: str  # new_flight, seats_changed, flight_removed


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS flights (
            flight_number TEXT NOT NULL,
            destination TEXT NOT NULL,
            time TEXT NOT NULL,
            date TEXT NOT NULL,
            seats INTEGER,
            book_url TEXT,
            last_seen_at TEXT NOT NULL,
            PRIMARY KEY (flight_number, date, time)
        )
    """)
    # Migrate: add book_url column if missing (existing DBs)
    try:
        conn.execute("ALTER TABLE flights ADD COLUMN book_url TEXT")
    except sqlite3.OperationalError:
        pass
    conn.execute("""
        CREATE TABLE IF NOT EXISTS changes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            flight_number TEXT NOT NULL,
            destination TEXT NOT NULL DEFAULT '',
            time TEXT NOT NULL,
            date TEXT NOT NULL,
            old_seats INTEGER,
            new_seats INTEGER,
            change_type TEXT NOT NULL
        )
    """)
    try:
        conn.execute("ALTER TABLE changes ADD COLUMN destination TEXT NOT NULL DEFAULT ''")
    except sqlite3.OperationalError:
        pass
    conn.commit()
    conn.close()


def get_current_flights() -> dict:
    """Return current flights as {(flight_number, date, time): FlightRecord}."""
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT flight_number, destination, time, date, seats, book_url FROM flights").fetchall()
    conn.close()
    result = {}
    for row in rows:
        rec = FlightRecord(*row)
        result[(rec.flight_number, rec.date, rec.time)] = rec
    return result


def process_scrape(new_records: List[FlightRecord]) -> List[Change]:
    """Compare new scrape data with DB, detect changes, update DB, return changes."""
    now = datetime.now().isoformat(timespec="seconds")
    current = get_current_flights()
    new_map = {}
    for rec in new_records:
        new_map[(rec.flight_number, rec.date, rec.time)] = rec

    changes: List[Change] = []

    # Detect new flights and seat changes
    for key, rec in new_map.items():
        if key not in current:
            if rec.seats is not None:
                changes.append(Change(
                    timestamp=now,
                    flight_number=rec.flight_number,
                    destination=rec.destination,
                    time=rec.time,
                    date=rec.date,
                    old_seats=None,
                    new_seats=rec.seats,
                    change_type="new_flight",
                ))
        else:
            old = current[key]
            if old.seats != rec.seats:
                changes.append(Change(
                    timestamp=now,
                    flight_number=rec.flight_number,
                    destination=rec.destination,
                    time=rec.time,
                    date=rec.date,
                    old_seats=old.seats,
                    new_seats=rec.seats,
                    change_type="seats_changed",
                ))

    # Write changes to DB (skip removal events -- stale data cleaned separately)
    conn = sqlite3.connect(DB_PATH)
    for ch in changes:
        conn.execute(
            "INSERT INTO changes (timestamp, flight_number, destination, time, date, old_seats, new_seats, change_type) VALUES (?,?,?,?,?,?,?,?)",
            (ch.timestamp, ch.flight_number, ch.destination, ch.time, ch.date, ch.old_seats, ch.new_seats, ch.change_type),
        )

    # Upsert flights
    for rec in new_records:
        conn.execute(
            "INSERT INTO flights (flight_number, destination, time, date, seats, book_url, last_seen_at) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(flight_number, date, time) DO UPDATE SET seats=excluded.seats, book_url=excluded.book_url, last_seen_at=excluded.last_seen_at",
            (rec.flight_number, rec.destination, rec.time, rec.date, rec.seats, rec.book_url, now),
        )

    # Only remove flights not seen for 5+ consecutive minutes (avoids
    # deleting data that a single incomplete scroll missed)
    stale_cutoff = (datetime.now() - timedelta(minutes=5)).isoformat(timespec="seconds")
    stale = conn.execute(
        "SELECT flight_number, date, time, destination, seats FROM flights WHERE last_seen_at < ?",
        (stale_cutoff,),
    ).fetchall()
    for row in stale:
        fn, date, time, dest, seats = row
        changes.append(Change(
            timestamp=now, flight_number=fn, destination=dest,
            time=time, date=date, old_seats=seats, new_seats=None,
            change_type="flight_removed",
        ))
        conn.execute("DELETE FROM flights WHERE flight_number=? AND date=? AND time=?", (fn, date, time))

    conn.commit()
    conn.close()
    return changes


def get_flights_json() -> list:
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT flight_number, destination, time, date, seats, book_url, last_seen_at FROM flights ORDER BY date, time"
    ).fetchall()
    conn.close()
    return [
        {"flight_number": r[0], "destination": r[1], "time": r[2], "date": r[3], "seats": r[4], "book_url": r[5], "last_seen_at": r[6]}
        for r in rows
    ]


def get_changes_json(limit: int = 100) -> list:
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT timestamp, flight_number, destination, time, date, old_seats, new_seats, change_type FROM changes ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return [
        {"timestamp": r[0], "flight_number": r[1], "destination": r[2], "time": r[3], "date": r[4], "old_seats": r[5], "new_seats": r[6], "change_type": r[7]}
        for r in rows
    ]


# ── ntfy configurations (multiple rules) ──


ALL_TRIGGER_TYPES = ["new_flight", "seats_available", "seats_changed", "seats_decreased", "flight_removed"]
DEFAULT_TRIGGERS = ["new_flight", "seats_available"]


def _ensure_ntfy_table():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ntfy_configs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL DEFAULT 'Notification',
            enabled INTEGER NOT NULL DEFAULT 0,
            server_url TEXT NOT NULL DEFAULT 'https://ntfy.sh',
            topic TEXT NOT NULL DEFAULT '',
            mode TEXT NOT NULL DEFAULT 'all',
            min_seats INTEGER NOT NULL DEFAULT 1,
            destinations TEXT NOT NULL DEFAULT '[]',
            triggers TEXT NOT NULL DEFAULT '["new_flight","seats_available"]'
        )
    """)
    try:
        conn.execute("ALTER TABLE ntfy_configs ADD COLUMN triggers TEXT NOT NULL DEFAULT '[\"new_flight\",\"seats_available\"]'")
    except sqlite3.OperationalError:
        pass
    # Migrate from old single-row table if it exists
    try:
        row = conn.execute("SELECT enabled, server_url, topic, mode, min_seats, destinations FROM ntfy_config WHERE id=1").fetchone()
        if row:
            conn.execute(
                "INSERT INTO ntfy_configs (name, enabled, server_url, topic, mode, min_seats, destinations) VALUES (?,?,?,?,?,?,?)",
                ("Migrated", row[0], row[1], row[2], row[3], row[4], row[5]),
            )
            conn.execute("DROP TABLE ntfy_config")
    except sqlite3.OperationalError:
        pass
    conn.commit()
    conn.close()


def _row_to_dict(row) -> dict:
    return {
        "id": row[0],
        "name": row[1],
        "enabled": bool(row[2]),
        "server_url": row[3],
        "topic": row[4],
        "mode": row[5],
        "min_seats": row[6],
        "destinations": json.loads(row[7]),
        "triggers": json.loads(row[8]) if row[8] else list(DEFAULT_TRIGGERS),
    }


_NTFY_COLS = "id, name, enabled, server_url, topic, mode, min_seats, destinations, triggers"


def get_all_ntfy_configs() -> list:
    _ensure_ntfy_table()
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(f"SELECT {_NTFY_COLS} FROM ntfy_configs ORDER BY id").fetchall()
    conn.close()
    return [_row_to_dict(r) for r in rows]


def get_ntfy_config(config_id: int) -> Optional[dict]:
    _ensure_ntfy_table()
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(f"SELECT {_NTFY_COLS} FROM ntfy_configs WHERE id=?", (config_id,)).fetchone()
    conn.close()
    return _row_to_dict(row) if row else None


def save_ntfy_config(cfg: dict) -> dict:
    """Insert or update a notification config. Returns the saved config with id."""
    _ensure_ntfy_table()
    conn = sqlite3.connect(DB_PATH)
    params = (
        cfg.get("name", "Notification"),
        int(cfg.get("enabled", False)),
        cfg.get("server_url", "https://ntfy.sh").rstrip("/"),
        cfg.get("topic", ""),
        cfg.get("mode", "all"),
        int(cfg.get("min_seats", 1)),
        json.dumps(cfg.get("destinations", [])),
        json.dumps(cfg.get("triggers", list(DEFAULT_TRIGGERS))),
    )
    config_id = cfg.get("id")
    if config_id:
        conn.execute(
            "UPDATE ntfy_configs SET name=?, enabled=?, server_url=?, topic=?, mode=?, min_seats=?, destinations=?, triggers=? WHERE id=?",
            params + (config_id,),
        )
    else:
        cur = conn.execute(
            "INSERT INTO ntfy_configs (name, enabled, server_url, topic, mode, min_seats, destinations, triggers) VALUES (?,?,?,?,?,?,?,?)",
            params,
        )
        config_id = cur.lastrowid
    conn.commit()
    conn.close()
    return get_ntfy_config(config_id)


def delete_ntfy_config(config_id: int) -> bool:
    _ensure_ntfy_table()
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM ntfy_configs WHERE id=?", (config_id,))
    conn.commit()
    affected = conn.total_changes
    conn.close()
    return affected > 0


def get_all_destinations() -> list:
    """Return sorted list of unique destination strings currently in the flights table."""
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT DISTINCT destination FROM flights ORDER BY destination").fetchall()
    conn.close()
    return [r[0] for r in rows]
