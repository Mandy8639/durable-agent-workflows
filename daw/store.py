"""SQLite checkpoint store: two tables, `runs` and `steps`.

Every state change is one short transaction. A step's output is committed
before the engine starts the next step, so after a crash the database says
exactly which steps finished and what they returned.
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id          TEXT PRIMARY KEY,
    workflow    TEXT NOT NULL,
    status      TEXT NOT NULL,      -- pending | running | completed | failed
    input_json  TEXT NOT NULL,
    spec_json   TEXT NOT NULL,      -- the workflow definition, so resume needs nothing else
    result_json TEXT,
    error       TEXT,
    owner_pid   INTEGER,            -- worker currently executing the run
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL,
    finished_at REAL
);
CREATE TABLE IF NOT EXISTS steps (
    run_id      TEXT NOT NULL REFERENCES runs(id),
    step_index  INTEGER NOT NULL,
    step_name   TEXT NOT NULL,
    status      TEXT NOT NULL,      -- running | retrying | completed | failed
    input_json  TEXT,
    output_json TEXT,
    error       TEXT,               -- last attempt's error
    attempts    INTEGER NOT NULL DEFAULT 0,
    started_at  REAL,
    updated_at  REAL NOT NULL,
    finished_at REAL,
    PRIMARY KEY (run_id, step_index)
);
CREATE INDEX IF NOT EXISTS runs_by_created ON runs(created_at);
"""

RUN_ACTIVE = ("pending", "running")


def dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


class Store:
    def __init__(self, path: str, fullfsync: bool = False):
        """`fullfsync` asks macOS for F_FULLFSYNC (flush the drive cache), which plain fsync
        does not do there. Process crashes are covered either way; power loss is not without it."""
        self.path = path
        self.durable = path != ":memory:"
        self.conn = sqlite3.connect(path, isolation_level=None, timeout=30.0)
        self.conn.row_factory = sqlite3.Row
        if self.durable:
            mode = self.conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            if mode.lower() != "wal":
                raise RuntimeError(f"could not enable WAL mode on {path} (got {mode})")
        self.conn.execute("PRAGMA synchronous=FULL")
        if fullfsync:
            self.conn.execute("PRAGMA fullfsync=ON")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        self.conn.execute("COMMIT")

    # --- runs -----------------------------------------------------------

    def create_run(self, run_id: str, workflow: str, spec: dict, run_input: Any) -> None:
        now = time.time()
        with self.tx() as c:
            c.execute(
                "INSERT INTO runs (id, workflow, status, input_json, spec_json,"
                " created_at, updated_at) VALUES (?, ?, 'pending', ?, ?, ?, ?)",
                (run_id, workflow, dumps(run_input), dumps(spec), now, now),
            )

    def get_run(self, run_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return _run_dict(row) if row else None

    def list_runs(self, status: str | None = None, limit: int = 50) -> list[dict]:
        sql = "SELECT * FROM runs"
        args: tuple = ()
        if status:
            sql += " WHERE status = ?"
            args = (status,)
        sql += " ORDER BY created_at DESC LIMIT ?"
        return [_run_dict(r) for r in self.conn.execute(sql, args + (limit,))]

    def claim_run(self, run_id: str, pid: int, is_alive) -> dict:
        """Mark the run as owned by `pid`. Refuses if another live worker owns it."""
        with self.tx() as c:
            row = c.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(run_id)
            owner = row["owner_pid"]
            if (
                row["status"] == "running"
                and owner is not None
                and owner != pid
                and is_alive(owner)
            ):
                raise RunLocked(run_id, owner)
            if row["status"] in RUN_ACTIVE:
                c.execute(
                    "UPDATE runs SET status = 'running', owner_pid = ?, updated_at = ?"
                    " WHERE id = ?",
                    (pid, time.time(), run_id),
                )
        return self.get_run(run_id)  # type: ignore[return-value]

    def reopen_failed(self, run_id: str) -> bool:
        """Give a failed run's failed step a fresh attempt budget; completed steps are kept."""
        now = time.time()
        with self.tx() as c:
            row = c.execute("SELECT status FROM runs WHERE id = ?", (run_id,)).fetchone()
            if row is None or row["status"] != "failed":
                return False
            c.execute(
                "UPDATE steps SET status = 'retrying', attempts = 0, finished_at = NULL,"
                " updated_at = ? WHERE run_id = ? AND status = 'failed'",
                (now, run_id),
            )
            c.execute(
                "UPDATE runs SET status = 'pending', error = NULL, owner_pid = NULL, updated_at = ?,"
                " finished_at = NULL WHERE id = ?",
                (now, run_id),
            )
        return True

    def complete_run(self, run_id: str, result: Any) -> None:
        now = time.time()
        with self.tx() as c:
            c.execute(
                "UPDATE runs SET status = 'completed', result_json = ?, owner_pid = NULL,"
                " updated_at = ?, finished_at = ? WHERE id = ?",
                (dumps(result), now, now, run_id),
            )

    def fail_run(self, run_id: str, error: str) -> None:
        now = time.time()
        with self.tx() as c:
            c.execute(
                "UPDATE runs SET status = 'failed', error = ?, owner_pid = NULL,"
                " updated_at = ?, finished_at = ? WHERE id = ?",
                (error, now, now, run_id),
            )

    # --- steps ----------------------------------------------------------

    def get_steps(self, run_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM steps WHERE run_id = ? ORDER BY step_index", (run_id,)
        )
        return [_step_dict(r) for r in rows]

    def start_attempt(
        self, run_id: str, index: int, name: str, step_input: Any, attempt: int
    ) -> None:
        """Durably record that attempt `attempt` is starting (before any work)."""
        now = time.time()
        with self.tx() as c:
            c.execute(
                "INSERT INTO steps (run_id, step_index, step_name, status, input_json,"
                " attempts, started_at, updated_at) VALUES (?, ?, ?, 'running', ?, ?, ?, ?)"
                " ON CONFLICT (run_id, step_index) DO UPDATE SET status = 'running',"
                " input_json = excluded.input_json, attempts = excluded.attempts,"
                " updated_at = excluded.updated_at",
                (run_id, index, name, dumps(step_input), attempt, now, now),
            )
            c.execute("UPDATE runs SET updated_at = ? WHERE id = ?", (now, run_id))

    def complete_step(self, run_id: str, index: int, output: Any) -> None:
        """The checkpoint: the step's output and status land in one transaction."""
        now = time.time()
        with self.tx() as c:
            c.execute(
                "UPDATE steps SET status = 'completed', output_json = ?, updated_at = ?,"
                " finished_at = ? WHERE run_id = ? AND step_index = ?",
                (dumps(output), now, now, run_id, index),
            )
            c.execute("UPDATE runs SET updated_at = ? WHERE id = ?", (now, run_id))

    def fail_attempt(self, run_id: str, index: int, error: str, final: bool) -> None:
        """Record a failed attempt. If `final`, the step and the run fail together."""
        now = time.time()
        with self.tx() as c:
            c.execute(
                "UPDATE steps SET status = ?, error = ?, updated_at = ?, finished_at = ?"
                " WHERE run_id = ? AND step_index = ?",
                ("failed" if final else "retrying", error, now, now if final else None,
                 run_id, index),
            )
            if final:
                c.execute(
                    "UPDATE runs SET status = 'failed', error = ?, owner_pid = NULL,"
                    " updated_at = ?, finished_at = ? WHERE id = ?",
                    (error, now, now, run_id),
                )
            else:
                c.execute("UPDATE runs SET updated_at = ? WHERE id = ?", (now, run_id))


class RunLocked(RuntimeError):
    def __init__(self, run_id: str, pid: int):
        super().__init__(f"run {run_id} is owned by live worker pid {pid}")
        self.run_id = run_id
        self.pid = pid


def _loads(text: str | None) -> Any:
    return None if text is None else json.loads(text)


def _run_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["input"] = _loads(d.pop("input_json"))
    d["spec"] = _loads(d.pop("spec_json"))
    d["result"] = _loads(d.pop("result_json"))
    return d


def _step_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["input"] = _loads(d.pop("input_json"))
    d["output"] = _loads(d.pop("output_json"))
    return d
