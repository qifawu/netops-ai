"""Incident case storage for correlated network faults.

CASE data lives in the runtime records directory because it can contain real
device details such as IP addresses, interfaces, and topology. That is separate
from the SOP library under ``playbooks/``: SOPs are neutral, reusable runbooks
that are safe to version in the repository, while incident CASE records are
environment-specific history and must not be committed.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


REPO_ROOT = Path(__file__).resolve().parents[2]
RECORDS_DIR = REPO_ROOT / "records"
DEFAULT_DB_PATH = RECORDS_DIR / "incidents.db"


@dataclass
class Incident:
    incident_id: str
    fingerprint: str
    hostid: str
    first_clock: int
    last_clock: int
    event_ids: list[str]
    root_event_id: str | None
    analysis: dict | None
    created_at: str


def compute_fingerprint(
    hostid: str,
    triggerids: Iterable[str],
    interfaces: Iterable[str],
) -> str:
    raw = f"{hostid}|{sorted(triggerids)}|{sorted(interfaces)}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def make_incident_id(fingerprint: str, first_clock: int) -> str:
    return f"{fingerprint}-{int(first_clock)}"


def init_db(db_path: str | Path | None = None) -> None:
    path = _db_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS incidents (
                incident_id TEXT PRIMARY KEY,
                fingerprint TEXT NOT NULL,
                hostid TEXT NOT NULL,
                first_clock INTEGER NOT NULL,
                last_clock INTEGER NOT NULL,
                event_ids_json TEXT NOT NULL,
                root_event_id TEXT,
                analysis_json TEXT,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_incidents_fingerprint_last_clock
            ON incidents(fingerprint, last_clock);

            CREATE INDEX IF NOT EXISTS idx_incidents_host_last_clock
            ON incidents(hostid, last_clock);

            CREATE TABLE IF NOT EXISTS incident_interfaces (
                incident_id TEXT NOT NULL,
                hostid TEXT NOT NULL,
                interface TEXT NOT NULL,
                first_clock INTEGER NOT NULL,
                last_clock INTEGER NOT NULL,
                PRIMARY KEY (incident_id, interface),
                FOREIGN KEY (incident_id) REFERENCES incidents(incident_id)
                    ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_incident_interfaces_lookup
            ON incident_interfaces(hostid, interface, last_clock);
            """
        )
        conn.commit()


def save_incident(
    incident: Incident,
    interfaces: Iterable[str] | None = None,
    db_path: str | Path | None = None,
) -> Incident:
    """Insert or replace an incident and its optional interface index rows."""
    path = _db_path(db_path)
    init_db(path)
    event_ids = json.dumps(incident.event_ids, ensure_ascii=False)
    analysis = None if incident.analysis is None else json.dumps(incident.analysis, ensure_ascii=False)
    interface_values = sorted({str(interface) for interface in (interfaces or [])})

    with closing(sqlite3.connect(path)) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        with conn:
            conn.execute(
                """
                INSERT INTO incidents (
                    incident_id, fingerprint, hostid, first_clock, last_clock,
                    event_ids_json, root_event_id, analysis_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(incident_id) DO UPDATE SET
                    fingerprint = excluded.fingerprint,
                    hostid = excluded.hostid,
                    first_clock = excluded.first_clock,
                    last_clock = excluded.last_clock,
                    event_ids_json = excluded.event_ids_json,
                    root_event_id = excluded.root_event_id,
                    analysis_json = excluded.analysis_json,
                    created_at = excluded.created_at
                """,
                (
                    incident.incident_id,
                    incident.fingerprint,
                    incident.hostid,
                    int(incident.first_clock),
                    int(incident.last_clock),
                    event_ids,
                    incident.root_event_id,
                    analysis,
                    incident.created_at,
                ),
            )
            conn.execute("DELETE FROM incident_interfaces WHERE incident_id = ?", (incident.incident_id,))
            conn.executemany(
                """
                INSERT INTO incident_interfaces (
                    incident_id, hostid, interface, first_clock, last_clock
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (
                        incident.incident_id,
                        incident.hostid,
                        interface,
                        int(incident.first_clock),
                        int(incident.last_clock),
                    )
                    for interface in interface_values
                ],
            )
    return incident


upsert_incident = save_incident


def get_incident(incident_id: str, db_path: str | Path | None = None) -> Incident | None:
    path = _db_path(db_path)
    init_db(path)
    with closing(sqlite3.connect(path)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM incidents WHERE incident_id = ?",
            (incident_id,),
        ).fetchone()
    return None if row is None else _row_to_incident(row)


def recent_incidents(
    hostid: str,
    interface: str,
    since: int,
    db_path: str | Path | None = None,
) -> list[Incident]:
    path = _db_path(db_path)
    init_db(path)
    with closing(sqlite3.connect(path)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT i.*
            FROM incidents i
            JOIN incident_interfaces ii ON ii.incident_id = i.incident_id
            WHERE ii.hostid = ?
              AND ii.interface = ?
              AND ii.last_clock >= ?
            ORDER BY ii.last_clock DESC, i.incident_id DESC
            """,
            (hostid, interface, int(since)),
        ).fetchall()
    return [_row_to_incident(row) for row in rows]


def same_fingerprint_count(
    fingerprint: str,
    since: int,
    db_path: str | Path | None = None,
) -> int:
    path = _db_path(db_path)
    init_db(path)
    with closing(sqlite3.connect(path)) as conn:
        count = conn.execute(
            """
            SELECT COUNT(*)
            FROM incidents
            WHERE fingerprint = ?
              AND last_clock >= ?
            """,
            (fingerprint, int(since)),
        ).fetchone()[0]
    return int(count)


def _db_path(db_path: str | Path | None) -> Path:
    return Path(db_path) if db_path is not None else DEFAULT_DB_PATH


def _row_to_incident(row: sqlite3.Row) -> Incident:
    analysis_json = row["analysis_json"]
    return Incident(
        incident_id=row["incident_id"],
        fingerprint=row["fingerprint"],
        hostid=row["hostid"],
        first_clock=int(row["first_clock"]),
        last_clock=int(row["last_clock"]),
        event_ids=list(json.loads(row["event_ids_json"])),
        root_event_id=row["root_event_id"],
        analysis=None if analysis_json is None else json.loads(analysis_json),
        created_at=row["created_at"],
    )


__all__ = [
    "DEFAULT_DB_PATH",
    "Incident",
    "compute_fingerprint",
    "get_incident",
    "init_db",
    "make_incident_id",
    "recent_incidents",
    "same_fingerprint_count",
    "save_incident",
    "upsert_incident",
]
