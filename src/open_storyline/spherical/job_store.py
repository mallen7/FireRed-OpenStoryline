"""SQLite-backed durable job and append-only event storage."""

from __future__ import annotations

import json
import sqlite3
import threading
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from open_storyline.spherical.distributed_protocol import (
    RetryPolicy,
    WorkerArtifact,
    WorkerJobError,
    WorkerJobEvent,
    WorkerJobEventKind,
    WorkerJobOperation,
    WorkerJobRecord,
    WorkerJobState,
    utc_now,
)
from open_storyline.spherical.manifests import canonical_json_bytes, sha256_json
from open_storyline.spherical.worker_security import sanitize_value


class IdempotencyConflict(ValueError):
    pass


class JobQueueFull(ValueError):
    pass


def _dump(value: Any) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return canonical_json_bytes(sanitize_value(value)).decode("utf-8")


def _load(value: str | None, default: Any) -> Any:
    return default if value is None else json.loads(value)


class DurableJobStore:
    def __init__(self, database_path: str | Path, *, max_queued_jobs: int) -> None:
        self.database_path = Path(database_path).expanduser().resolve(strict=False)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.max_queued_jobs = max_queued_jobs
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path,
            timeout=30.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    operation TEXT NOT NULL,
                    state TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    request_sha256 TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    progress_percent INTEGER NOT NULL,
                    message TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL,
                    max_attempts INTEGER NOT NULL,
                    retry_initial_seconds REAL NOT NULL,
                    retry_maximum_seconds REAL NOT NULL,
                    timeout_seconds REAL NOT NULL,
                    available_at TEXT NOT NULL,
                    cancel_requested INTEGER NOT NULL,
                    result_json TEXT,
                    error_json TEXT,
                    artifacts_json TEXT NOT NULL,
                    metrics_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT
                );
                CREATE INDEX IF NOT EXISTS jobs_queue_index
                    ON jobs(state, created_at);
                CREATE INDEX IF NOT EXISTS jobs_operation_index
                    ON jobs(operation, state);
                CREATE TABLE IF NOT EXISTS job_events (
                    job_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    progress_percent INTEGER NOT NULL,
                    message TEXT NOT NULL,
                    data_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(job_id, sequence),
                    FOREIGN KEY(job_id) REFERENCES jobs(job_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS counters (
                    name TEXT PRIMARY KEY,
                    value INTEGER NOT NULL
                );
                """
            )
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(jobs)").fetchall()
            }
            if "available_at" not in columns:
                connection.execute("ALTER TABLE jobs ADD COLUMN available_at TEXT")
                connection.execute(
                    "UPDATE jobs SET available_at = COALESCE(updated_at, created_at)"
                )

    @staticmethod
    def _iso(value: datetime | None = None) -> str:
        return (value or utc_now()).isoformat()

    @staticmethod
    def _record(row: sqlite3.Row) -> WorkerJobRecord:
        return WorkerJobRecord(
            job_id=UUID(row["job_id"]),
            operation=WorkerJobOperation(row["operation"]),
            state=WorkerJobState(row["state"]),
            idempotency_key=row["idempotency_key"],
            request_sha256=row["request_sha256"],
            progress_percent=row["progress_percent"],
            message=row["message"],
            attempt_count=row["attempt_count"],
            max_attempts=row["max_attempts"],
            timeout_seconds=row["timeout_seconds"],
            cancel_requested=bool(row["cancel_requested"]),
            result=_load(row["result_json"], None),
            error=(
                WorkerJobError.model_validate_json(row["error_json"])
                if row["error_json"]
                else None
            ),
            artifacts=[
                WorkerArtifact.model_validate(item)
                for item in _load(row["artifacts_json"], [])
            ],
            metrics=_load(row["metrics_json"], {}),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            started_at=(
                datetime.fromisoformat(row["started_at"])
                if row["started_at"]
                else None
            ),
            completed_at=(
                datetime.fromisoformat(row["completed_at"])
                if row["completed_at"]
                else None
            ),
        )

    @staticmethod
    def _event(row: sqlite3.Row) -> WorkerJobEvent:
        return WorkerJobEvent(
            job_id=UUID(row["job_id"]),
            sequence=row["sequence"],
            kind=WorkerJobEventKind(row["kind"]),
            progress_percent=row["progress_percent"],
            message=row["message"],
            data=_load(row["data_json"], {}),
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    @staticmethod
    def _append_event(
        connection: sqlite3.Connection,
        job_id: str,
        kind: WorkerJobEventKind,
        progress_percent: int,
        message: str,
        data: dict[str, Any] | None = None,
    ) -> None:
        sequence = connection.execute(
            "SELECT COALESCE(MAX(sequence), 0) + 1 FROM job_events WHERE job_id = ?",
            (job_id,),
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO job_events(
                job_id, sequence, kind, progress_percent, message, data_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                sequence,
                kind.value,
                progress_percent,
                message,
                _dump(data or {}),
                DurableJobStore._iso(),
            ),
        )

    @staticmethod
    def _increment_counter(connection: sqlite3.Connection, name: str) -> None:
        connection.execute(
            """
            INSERT INTO counters(name, value) VALUES (?, 1)
            ON CONFLICT(name) DO UPDATE SET value = value + 1
            """,
            (name,),
        )

    def create_or_get(
        self,
        *,
        operation: WorkerJobOperation,
        idempotency_key: str,
        request: dict[str, Any],
        timeout_seconds: float,
        retry_policy: RetryPolicy,
    ) -> tuple[WorkerJobRecord, bool]:
        request_hash = sha256_json(request)
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM jobs WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                if (
                    existing["request_sha256"] != request_hash
                    or existing["operation"] != operation.value
                ):
                    connection.rollback()
                    raise IdempotencyConflict(
                        "Idempotency-Key was already used for a different request."
                    )
                self._append_event(
                    connection,
                    existing["job_id"],
                    WorkerJobEventKind.IDEMPOTENT_REPLAY,
                    existing["progress_percent"],
                    "duplicate request resolved to the existing job",
                )
                self._increment_counter(connection, "idempotent_replays")
                connection.commit()
                return self.get(existing["job_id"]), False

            queued = connection.execute(
                "SELECT COUNT(*) FROM jobs WHERE state = ?",
                (WorkerJobState.QUEUED.value,),
            ).fetchone()[0]
            if queued >= self.max_queued_jobs:
                connection.rollback()
                raise JobQueueFull("Worker job queue is full.")

            job_id = str(uuid4())
            now = self._iso()
            connection.execute(
                """
                INSERT INTO jobs(
                    job_id, operation, state, idempotency_key, request_sha256,
                    request_json, progress_percent, message, attempt_count,
                    max_attempts, retry_initial_seconds, retry_maximum_seconds,
                    timeout_seconds, available_at, cancel_requested, result_json, error_json,
                    artifacts_json, metrics_json, created_at, updated_at,
                    started_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, 0, ?, 0, ?, ?, ?, ?, ?, 0, NULL,
                          NULL, '[]', '{}', ?, ?, NULL, NULL)
                """,
                (
                    job_id,
                    operation.value,
                    WorkerJobState.QUEUED.value,
                    idempotency_key,
                    request_hash,
                    _dump(request),
                    "queued",
                    retry_policy.max_attempts,
                    retry_policy.initial_delay_seconds,
                    retry_policy.maximum_delay_seconds,
                    timeout_seconds,
                    now,
                    now,
                    now,
                ),
            )
            self._append_event(
                connection,
                job_id,
                WorkerJobEventKind.ACCEPTED,
                0,
                "job accepted and persisted",
                {"operation": operation.value},
            )
            self._increment_counter(connection, "job_submissions")
            connection.commit()
        return self.get(job_id), True

    def get(self, job_id: str | UUID) -> WorkerJobRecord:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE job_id = ?", (str(job_id),)
            ).fetchone()
        if row is None:
            raise KeyError(str(job_id))
        return self._record(row)

    def request_payload(self, job_id: str | UUID) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT request_json FROM jobs WHERE job_id = ?", (str(job_id),)
            ).fetchone()
        if row is None:
            raise KeyError(str(job_id))
        return json.loads(row["request_json"])

    def events(self, job_id: str | UUID, *, after_sequence: int = 0) -> list[WorkerJobEvent]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM job_events
                WHERE job_id = ? AND sequence > ? ORDER BY sequence
                """,
                (str(job_id), after_sequence),
            ).fetchall()
        return [self._event(row) for row in rows]

    def claim_next(self) -> WorkerJobRecord | None:
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM jobs WHERE state = ? AND cancel_requested = 0
                    AND available_at <= ?
                ORDER BY created_at, job_id LIMIT 1
                """,
                (WorkerJobState.QUEUED.value, self._iso()),
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            now = self._iso()
            attempt = row["attempt_count"] + 1
            connection.execute(
                """
                UPDATE jobs SET state = ?, attempt_count = ?, progress_percent = 0,
                    message = ?, started_at = COALESCE(started_at, ?), updated_at = ?,
                    error_json = NULL
                WHERE job_id = ?
                """,
                (
                    WorkerJobState.RUNNING.value,
                    attempt,
                    f"running attempt {attempt}",
                    now,
                    now,
                    row["job_id"],
                ),
            )
            self._append_event(
                connection,
                row["job_id"],
                WorkerJobEventKind.RUNNING,
                0,
                f"running attempt {attempt}",
                {"attempt": attempt},
            )
            connection.commit()
        return self.get(row["job_id"])

    def update_progress(
        self,
        job_id: str | UUID,
        progress_percent: int,
        message: str,
        data: dict[str, Any] | None = None,
    ) -> None:
        progress_percent = max(0, min(100, int(progress_percent)))
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT state, progress_percent FROM jobs WHERE job_id = ?",
                (str(job_id),),
            ).fetchone()
            if row is None or row["state"] != WorkerJobState.RUNNING.value:
                connection.commit()
                return
            monotonic_progress = max(row["progress_percent"], progress_percent)
            connection.execute(
                "UPDATE jobs SET progress_percent = ?, message = ?, updated_at = ? WHERE job_id = ?",
                (monotonic_progress, message, self._iso(), str(job_id)),
            )
            self._append_event(
                connection,
                str(job_id),
                WorkerJobEventKind.PROGRESS,
                monotonic_progress,
                message,
                data,
            )
            connection.commit()

    def request_cancel(self, job_id: str | UUID) -> WorkerJobRecord:
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM jobs WHERE job_id = ?", (str(job_id),)
            ).fetchone()
            if row is None:
                connection.rollback()
                raise KeyError(str(job_id))
            state = WorkerJobState(row["state"])
            if state.terminal:
                connection.commit()
                return self._record(row)
            now = self._iso()
            if state == WorkerJobState.QUEUED:
                connection.execute(
                    """
                    UPDATE jobs SET state = ?, cancel_requested = 1,
                        message = ?, updated_at = ?, completed_at = ? WHERE job_id = ?
                    """,
                    (
                        WorkerJobState.CANCELLED.value,
                        "cancelled before execution",
                        now,
                        now,
                        str(job_id),
                    ),
                )
                kind = WorkerJobEventKind.CANCELLED
                message = "cancelled before execution"
            else:
                connection.execute(
                    "UPDATE jobs SET cancel_requested = 1, message = ?, updated_at = ? WHERE job_id = ?",
                    ("cancellation requested", now, str(job_id)),
                )
                kind = WorkerJobEventKind.CANCEL_REQUESTED
                message = "cancellation requested"
            self._append_event(
                connection,
                str(job_id),
                kind,
                row["progress_percent"],
                message,
            )
            connection.commit()
        return self.get(job_id)

    def cancellation_requested(self, job_id: str | UUID) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT cancel_requested FROM jobs WHERE job_id = ?", (str(job_id),)
            ).fetchone()
        return bool(row and row["cancel_requested"])

    def complete(
        self,
        job_id: str | UUID,
        *,
        result: dict[str, Any],
        artifacts: list[WorkerArtifact],
        metrics: dict[str, Any],
    ) -> WorkerJobRecord:
        now = self._iso()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE jobs SET state = ?, progress_percent = 100, message = ?,
                    result_json = ?, error_json = NULL, artifacts_json = ?,
                    metrics_json = ?, updated_at = ?, completed_at = ?
                WHERE job_id = ?
                """,
                (
                    WorkerJobState.SUCCEEDED.value,
                    "completed",
                    _dump(result),
                    _dump(artifacts),
                    _dump(metrics),
                    now,
                    now,
                    str(job_id),
                ),
            )
            self._append_event(
                connection,
                str(job_id),
                WorkerJobEventKind.SUCCEEDED,
                100,
                "job completed and artifacts validated",
                {"artifact_count": len(artifacts)},
            )
            connection.commit()
        return self.get(job_id)

    def finish_cancelled(self, job_id: str | UUID, message: str) -> WorkerJobRecord:
        return self._finish_terminal(
            job_id,
            state=WorkerJobState.CANCELLED,
            kind=WorkerJobEventKind.CANCELLED,
            error=WorkerJobError(code="cancelled", message=message, retryable=False),
        )

    def finish_timed_out(self, job_id: str | UUID, message: str) -> WorkerJobRecord:
        return self._finish_terminal(
            job_id,
            state=WorkerJobState.TIMED_OUT,
            kind=WorkerJobEventKind.TIMED_OUT,
            error=WorkerJobError(code="timed_out", message=message, retryable=True),
        )

    def fail_or_retry(self, job_id: str | UUID, error: WorkerJobError) -> WorkerJobRecord:
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM jobs WHERE job_id = ?", (str(job_id),)
            ).fetchone()
            if row is None:
                connection.rollback()
                raise KeyError(str(job_id))
            if error.retryable and row["attempt_count"] < row["max_attempts"]:
                delay = min(
                    row["retry_initial_seconds"] * (2 ** max(0, row["attempt_count"] - 1)),
                    row["retry_maximum_seconds"],
                )
                available_at = (utc_now() + timedelta(seconds=delay)).isoformat()
                connection.execute(
                    """
                    UPDATE jobs SET state = ?, progress_percent = 0, message = ?,
                        error_json = ?, available_at = ?, updated_at = ? WHERE job_id = ?
                    """,
                    (
                        WorkerJobState.QUEUED.value,
                        f"retry scheduled after {delay:.2f}s",
                        _dump(error),
                        available_at,
                        self._iso(),
                        str(job_id),
                    ),
                )
                self._append_event(
                    connection,
                    str(job_id),
                    WorkerJobEventKind.RETRYING,
                    0,
                    f"retry scheduled after {delay:.2f}s",
                    {"delay_seconds": delay, "error_code": error.code},
                )
            else:
                now = self._iso()
                connection.execute(
                    """
                    UPDATE jobs SET state = ?, message = ?, error_json = ?,
                        updated_at = ?, completed_at = ? WHERE job_id = ?
                    """,
                    (
                        WorkerJobState.FAILED.value,
                        error.message,
                        _dump(error),
                        now,
                        now,
                        str(job_id),
                    ),
                )
                self._append_event(
                    connection,
                    str(job_id),
                    WorkerJobEventKind.FAILED,
                    row["progress_percent"],
                    error.message,
                    {"error_code": error.code, "retryable": error.retryable},
                )
            connection.commit()
        return self.get(job_id)

    def _finish_terminal(
        self,
        job_id: str | UUID,
        *,
        state: WorkerJobState,
        kind: WorkerJobEventKind,
        error: WorkerJobError,
    ) -> WorkerJobRecord:
        now = self._iso()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT progress_percent FROM jobs WHERE job_id = ?", (str(job_id),)
            ).fetchone()
            if row is None:
                connection.rollback()
                raise KeyError(str(job_id))
            connection.execute(
                """
                UPDATE jobs SET state = ?, message = ?, error_json = ?,
                    updated_at = ?, completed_at = ? WHERE job_id = ?
                """,
                (state.value, error.message, _dump(error), now, now, str(job_id)),
            )
            self._append_event(
                connection,
                str(job_id),
                kind,
                row["progress_percent"],
                error.message,
            )
            connection.commit()
        return self.get(job_id)

    def recover_interrupted(self) -> int:
        recovered = 0
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT * FROM jobs WHERE state = ?",
                (WorkerJobState.RUNNING.value,),
            ).fetchall()
            for row in rows:
                recovered += 1
                if row["cancel_requested"]:
                    state = WorkerJobState.CANCELLED
                    kind = WorkerJobEventKind.CANCELLED
                    message = "cancelled while recovering an interrupted worker"
                    completed = self._iso()
                elif row["attempt_count"] < row["max_attempts"]:
                    state = WorkerJobState.QUEUED
                    kind = WorkerJobEventKind.RECOVERED
                    message = "interrupted job recovered and requeued"
                    completed = None
                else:
                    state = WorkerJobState.FAILED
                    kind = WorkerJobEventKind.FAILED
                    message = "interrupted job exhausted its retry policy"
                    completed = self._iso()
                connection.execute(
                    """
                    UPDATE jobs SET state = ?, message = ?, available_at = ?, updated_at = ?,
                        completed_at = ? WHERE job_id = ?
                    """,
                    (state.value, message, self._iso(), self._iso(), completed, row["job_id"]),
                )
                self._append_event(
                    connection,
                    row["job_id"],
                    kind,
                    row["progress_percent"],
                    message,
                )
            connection.commit()
        return recovered

    def snapshot_metrics(self) -> dict[str, Any]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM jobs").fetchall()
            counters = {
                row["name"]: row["value"]
                for row in connection.execute("SELECT * FROM counters").fetchall()
            }
        state_counts: Counter[str] = Counter()
        operation_counts: Counter[str] = Counter()
        failure_types: Counter[str] = Counter()
        durations: dict[str, list[float]] = defaultdict(list)
        rates: dict[str, list[float]] = defaultdict(list)
        output_bytes = 0
        cache_hits = 0
        completed = 0
        for row in rows:
            state_counts[row["state"]] += 1
            operation_counts[row["operation"]] += 1
            metrics = _load(row["metrics_json"], {})
            if row["state"] == WorkerJobState.SUCCEEDED.value:
                completed += 1
                cache_hits += int(bool(metrics.get("cache_hit")))
            duration = metrics.get("duration_seconds")
            if isinstance(duration, int | float):
                durations[row["operation"]].append(float(duration))
            rate = metrics.get("processing_fps")
            if isinstance(rate, int | float):
                rates[row["operation"]].append(float(rate))
            size = metrics.get("output_size_bytes")
            if isinstance(size, int | float):
                output_bytes += int(size)
            if row["error_json"]:
                error = json.loads(row["error_json"])
                failure_types[error.get("code", "unknown")] += 1
        return {
            "job_counts": dict(state_counts),
            "operation_counts": dict(operation_counts),
            "failure_types": dict(failure_types),
            "average_job_duration_seconds": {
                operation: sum(values) / len(values)
                for operation, values in durations.items()
            },
            "average_processing_fps": {
                operation: sum(values) / len(values)
                for operation, values in rates.items()
            },
            "cache_hit_rate": cache_hits / completed if completed else 0.0,
            "output_bytes": output_bytes,
            "idempotent_replays": counters.get("idempotent_replays", 0),
            "job_submissions": counters.get("job_submissions", 0),
        }

    def terminal_jobs(self) -> list[WorkerJobRecord]:
        terminal_values = tuple(state.value for state in WorkerJobState if state.terminal)
        placeholders = ",".join("?" for _ in terminal_values)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM jobs WHERE state IN ({placeholders}) ORDER BY completed_at",
                terminal_values,
            ).fetchall()
        return [self._record(row) for row in rows]

    def prune_history(self, *, retention_days: int, maximum_jobs: int) -> int:
        """Delete expired database history only; media artifacts are never removed here."""

        cutoff = (utc_now() - timedelta(days=retention_days)).isoformat()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            expired = {
                row["job_id"]
                for row in connection.execute(
                    """
                    SELECT job_id FROM jobs
                    WHERE completed_at IS NOT NULL AND completed_at < ?
                    """,
                    (cutoff,),
                ).fetchall()
            }
            total = connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
            excess = max(0, total - maximum_jobs)
            if excess:
                expired.update(
                    row["job_id"]
                    for row in connection.execute(
                        """
                        SELECT job_id FROM jobs WHERE completed_at IS NOT NULL
                        ORDER BY completed_at, job_id LIMIT ?
                        """,
                        (excess,),
                    ).fetchall()
                )
            if expired:
                connection.executemany(
                    "DELETE FROM jobs WHERE job_id = ?",
                    [(job_id,) for job_id in expired],
                )
            connection.commit()
        return len(expired)
