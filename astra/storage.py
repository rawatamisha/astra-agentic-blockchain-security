"""SQLite admission lifecycle and append-only event history."""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from astra.policy import apply_admission_policy


class Store:
    def __init__(self, path):
        self.path = path
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS candidates (
                    id TEXT PRIMARY KEY, status TEXT NOT NULL, payload TEXT NOT NULL,
                    report TEXT
                );
                CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY, candidate TEXT NOT NULL,
                    time TEXT NOT NULL, status TEXT NOT NULL, detail TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS investigations (
                    id TEXT PRIMARY KEY, candidate TEXT NOT NULL, report TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS monitoring (
                    sequence INTEGER PRIMARY KEY,
                    candidate TEXT NOT NULL,
                    observation_id TEXT NOT NULL,
                    time TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    report TEXT NOT NULL,
                    UNIQUE(candidate, observation_id)
                );
                CREATE INDEX IF NOT EXISTS monitoring_candidate_sequence
                    ON monitoring(candidate, sequence);
            """)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def event(self, db, node_id, status, detail):
        db.execute("INSERT INTO events(candidate,time,status,detail) VALUES(?,?,?,?)",
                   (node_id, datetime.now(timezone.utc).isoformat(), status, json.dumps(detail)))

    def register(self, candidate):
        with self.connection() as db:
            db.execute("INSERT INTO candidates VALUES(?,?,?,NULL)",
                       (candidate["node_id"], "PENDING_INVESTIGATION", json.dumps(candidate)))
            self.event(db, candidate["node_id"], "PENDING_INVESTIGATION", {})

    def transition(self, node_id, previous, status, detail, report=None):
        placeholders = ",".join("?" for _ in previous)
        with self.connection() as db:
            result = db.execute(
                f"""UPDATE candidates SET status=?, report=COALESCE(?,report)
                    WHERE id=? AND status IN ({placeholders})""",
                (status, json.dumps(report) if report is not None else None, node_id, *previous),
            )
            if result.rowcount != 1:
                raise ValueError("Admission state changed or action is unavailable")
            if report is not None:
                db.execute("INSERT INTO investigations VALUES(?,?,?)",
                           (report["investigation_id"], node_id, json.dumps(report)))
            self.event(db, node_id, status, detail)

    def list(self):
        with self.connection() as db:
            rows = db.execute("SELECT * FROM candidates ORDER BY rowid DESC").fetchall()
        candidates = []
        for row in rows:
            report = json.loads(row["report"]) if row["report"] else None
            if report is not None and "trust" in report:
                conflict = report.get("evidence", {}).get("finney", {}).get("conflict", False)
                report["trust"] = apply_admission_policy(report["trust"], spend_conflict=conflict)
            candidates.append({"id": row["id"], "status": row["status"],
                               **json.loads(row["payload"]), "report": report})
        return candidates

    def get(self, node_id):
        return next((candidate for candidate in self.list() if candidate["id"] == node_id), None)

    def events(self, node_id):
        with self.connection() as db:
            rows = db.execute("SELECT * FROM events WHERE candidate=? ORDER BY sequence",
                              (node_id,)).fetchall()
        return [{**dict(row), "detail": json.loads(row["detail"])} for row in rows]

    def monitoring_records(self, db, node_id, limit):
        rows = db.execute("""SELECT sequence, observation_id, time, payload, report FROM monitoring
                             WHERE candidate=? ORDER BY sequence DESC LIMIT ?""",
                          (node_id, limit)).fetchall()
        return [{**dict(row), "payload": json.loads(row["payload"]),
                 "report": json.loads(row["report"])} for row in rows]
