"""Concurrent durable-job scheduler using isolated, cancellable child processes."""

from __future__ import annotations

import json
import logging
import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from open_storyline.spherical.distributed_protocol import (
    WorkerArtifact,
    WorkerJobError,
    WorkerJobRecord,
)
from open_storyline.spherical.job_store import DurableJobStore
from open_storyline.spherical.manifests import atomic_write_json, sha256_file
from open_storyline.spherical.worker_logging import log_event
from open_storyline.spherical.worker_security import (
    DistributedWorkerSettings,
    WorkerPathPolicy,
    sanitize_text,
    sanitize_value,
)


logger = logging.getLogger("open_storyline.spherical.worker.runner")


@dataclass(frozen=True)
class ExecutionOutcome:
    result: dict[str, Any]
    artifacts: list[WorkerArtifact]
    metrics: dict[str, Any]


class ExecutionFailure(RuntimeError):
    def __init__(self, error: WorkerJobError):
        super().__init__(error.message)
        self.error = error


class ExecutionCancelled(RuntimeError):
    pass


class ExecutionTimedOut(RuntimeError):
    pass


class ExecutionInterrupted(RuntimeError):
    """Service shutdown interrupted an operation; startup recovery owns the state."""


ProgressEmitter = Callable[[int, str, dict[str, Any]], None]
Flag = Callable[[], bool]


class JobExecutor(Protocol):
    def execute(
        self,
        job: WorkerJobRecord,
        request: dict[str, Any],
        *,
        emit_progress: ProgressEmitter,
        cancel_requested: Flag,
        shutdown_requested: Flag,
    ) -> ExecutionOutcome: ...


class SubprocessJobExecutor:
    def __init__(
        self,
        settings: DistributedWorkerSettings,
        path_policy: WorkerPathPolicy,
    ) -> None:
        self.settings = settings
        self.path_policy = path_policy
        self.jobs_root = settings.state_dir / "job-work"
        self.jobs_root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _read_lines(stream, destination: queue.Queue[str | None]) -> None:
        try:
            for line in iter(stream.readline, ""):
                destination.put(line)
        finally:
            destination.put(None)
            stream.close()

    @staticmethod
    def _read_stderr(stream, chunks: list[str]) -> None:
        try:
            for chunk in iter(lambda: stream.read(4096), ""):
                chunks.append(chunk)
                while sum(len(item) for item in chunks) > 64_000:
                    chunks.pop(0)
        finally:
            stream.close()

    @staticmethod
    def _terminate(process: subprocess.Popen[str], grace_seconds: float) -> None:
        if process.poll() is not None:
            return
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
            process.wait(timeout=grace_seconds)
        except subprocess.TimeoutExpired:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
            process.wait(timeout=5.0)
        except ProcessLookupError:
            return

    def _validate_artifacts(self, values: list[dict[str, Any]]) -> list[WorkerArtifact]:
        artifacts = [WorkerArtifact.model_validate(item) for item in values]
        names = [item.name for item in artifacts]
        if len(names) != len(set(names)):
            raise ExecutionFailure(
                WorkerJobError(
                    code="artifact_validation_failed",
                    message="Task returned duplicate artifact names.",
                    retryable=False,
                )
            )
        for artifact in artifacts:
            path = self.path_policy.output_path(artifact.path, role="job artifact")
            if not path.is_file():
                raise ExecutionFailure(
                    WorkerJobError(
                        code="artifact_validation_failed",
                        message="Task artifact is missing.",
                        retryable=False,
                        details={"artifact": artifact.name},
                    )
                )
            if path.stat().st_size != artifact.size_bytes or sha256_file(path) != artifact.sha256:
                raise ExecutionFailure(
                    WorkerJobError(
                        code="artifact_validation_failed",
                        message="Task artifact failed size or SHA-256 validation.",
                        retryable=False,
                        details={"artifact": artifact.name},
                    )
                )
        return artifacts

    def execute(
        self,
        job: WorkerJobRecord,
        request: dict[str, Any],
        *,
        emit_progress: ProgressEmitter,
        cancel_requested: Flag,
        shutdown_requested: Flag,
    ) -> ExecutionOutcome:
        attempt_dir = self.jobs_root / str(job.job_id) / f"attempt-{job.attempt_count}"
        attempt_dir.mkdir(parents=True, exist_ok=False)
        request_path = attempt_dir / "request.json"
        result_path = attempt_dir / "result.json"
        atomic_write_json(request_path, request)
        command = [
            sys.executable,
            "-m",
            "open_storyline.spherical.worker_task",
            "--operation",
            job.operation.value,
            "--request",
            str(request_path),
            "--result",
            str(result_path),
            "--worker-executable",
            str(self.settings.worker_executable),
            "--worker-timeout",
            str(job.timeout_seconds),
        ]
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                bufsize=1,
                start_new_session=(os.name == "posix"),
            )
        except OSError as exc:
            raise ExecutionFailure(
                WorkerJobError(
                    code="task_start_failed",
                    message="Could not start the isolated worker task.",
                    retryable=True,
                    details={"error": sanitize_text(str(exc))},
                )
            ) from exc

        assert process.stdout is not None
        assert process.stderr is not None
        output: queue.Queue[str | None] = queue.Queue()
        stderr_chunks: list[str] = []
        stdout_thread = threading.Thread(
            target=self._read_lines,
            args=(process.stdout, output),
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=self._read_stderr,
            args=(process.stderr, stderr_chunks),
            daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()
        started = time.monotonic()
        stdout_closed = False
        task_error: WorkerJobError | None = None
        try:
            while not stdout_closed or process.poll() is None:
                if shutdown_requested():
                    self._terminate(process, self.settings.shutdown_grace_seconds)
                    raise ExecutionInterrupted("worker service is shutting down")
                if cancel_requested():
                    self._terminate(process, self.settings.shutdown_grace_seconds)
                    raise ExecutionCancelled("job cancellation terminated the task process group")
                if time.monotonic() - started > job.timeout_seconds:
                    self._terminate(process, self.settings.shutdown_grace_seconds)
                    raise ExecutionTimedOut(
                        f"job exceeded its {job.timeout_seconds:.2f}s timeout"
                    )
                try:
                    line = output.get(timeout=self.settings.event_poll_seconds)
                except queue.Empty:
                    continue
                if line is None:
                    stdout_closed = True
                    continue
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    payload = json.loads(stripped)
                    if payload.get("event") == "progress":
                        emit_progress(
                            int(payload["progress_percent"]),
                            str(payload["message"]),
                            sanitize_value(payload.get("data", {})),
                        )
                    elif payload.get("event") == "error":
                        task_error = WorkerJobError.model_validate(payload["error"])
                    else:
                        raise ValueError("unknown task event")
                except Exception as exc:
                    self._terminate(process, self.settings.shutdown_grace_seconds)
                    raise ExecutionFailure(
                        WorkerJobError(
                            code="task_protocol_failed",
                            message="Isolated task emitted an invalid event.",
                            retryable=False,
                            details={
                                "event": sanitize_text(stripped, maximum_length=2000),
                                "error": sanitize_text(str(exc)),
                            },
                        )
                    ) from exc
            return_code = process.wait(timeout=2.0)
        finally:
            stdout_thread.join(timeout=1.0)
            stderr_thread.join(timeout=1.0)

        if task_error is not None:
            raise ExecutionFailure(task_error)
        if return_code != 0 or not result_path.is_file():
            raise ExecutionFailure(
                WorkerJobError(
                    code="task_process_failed",
                    message="Isolated task exited without a durable result.",
                    retryable=True,
                    details={
                        "exit_code": return_code,
                        "stderr": sanitize_text("".join(stderr_chunks)),
                    },
                )
            )
        try:
            payload = json.loads(result_path.read_text(encoding="utf-8"))
            result = payload["result"]
            metrics = sanitize_value(payload.get("metrics", {}))
            artifacts = self._validate_artifacts(payload["artifacts"])
            if not isinstance(result, dict) or not isinstance(metrics, dict):
                raise TypeError("result and metrics must be objects")
        except ExecutionFailure:
            raise
        except Exception as exc:
            raise ExecutionFailure(
                WorkerJobError(
                    code="task_result_invalid",
                    message="Isolated task result failed validation.",
                    retryable=False,
                    details={"error": sanitize_text(str(exc))},
                )
            ) from exc
        return ExecutionOutcome(result=result, artifacts=artifacts, metrics=metrics)

    def cleanup_workspace(self, job_id: str) -> None:
        shutil.rmtree(self.jobs_root / job_id, ignore_errors=True)


class DurableJobManager:
    def __init__(
        self,
        store: DurableJobStore,
        settings: DistributedWorkerSettings,
        executor: JobExecutor,
    ) -> None:
        self.store = store
        self.settings = settings
        self.executor = executor
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    @property
    def running(self) -> bool:
        return bool(self._threads) and not self._stop.is_set()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        recovered = self.store.recover_interrupted()
        pruned = self.store.prune_history(
            retention_days=self.settings.job_retention_days,
            maximum_jobs=self.settings.max_job_history,
        )
        log_event(
            logger,
            logging.INFO,
            "job manager started",
            recovered_jobs=recovered,
            pruned_history_records=pruned,
        )
        self._threads = [
            threading.Thread(
                target=self._worker_loop,
                name=f"spherical-job-worker-{index}",
                daemon=True,
            )
            for index in range(self.settings.max_concurrent_jobs)
        ]
        for thread in self._threads:
            thread.start()

    def stop(self) -> None:
        self._stop.set()
        deadline = time.monotonic() + self.settings.shutdown_grace_seconds + 2.0
        for thread in self._threads:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        self._threads.clear()
        log_event(logger, logging.INFO, "job manager stopped")

    def _worker_loop(self) -> None:
        while not self._stop.is_set():
            job = self.store.claim_next()
            if job is None:
                self._stop.wait(self.settings.event_poll_seconds)
                continue
            log_event(
                logger,
                logging.INFO,
                "job execution started",
                job_id=str(job.job_id),
                operation=job.operation.value,
                attempt=job.attempt_count,
            )
            try:
                outcome = self.executor.execute(
                    job,
                    self.store.request_payload(job.job_id),
                    emit_progress=lambda progress, message, data: self.store.update_progress(
                        job.job_id, progress, message, data
                    ),
                    cancel_requested=lambda: self.store.cancellation_requested(job.job_id),
                    shutdown_requested=self._stop.is_set,
                )
                self.store.complete(
                    job.job_id,
                    result=outcome.result,
                    artifacts=outcome.artifacts,
                    metrics=outcome.metrics,
                )
                log_event(
                    logger,
                    logging.INFO,
                    "job execution completed",
                    job_id=str(job.job_id),
                    operation=job.operation.value,
                )
            except ExecutionInterrupted:
                log_event(
                    logger,
                    logging.WARNING,
                    "job interrupted by service shutdown",
                    job_id=str(job.job_id),
                )
                return
            except ExecutionCancelled as exc:
                self.store.finish_cancelled(job.job_id, str(exc))
            except ExecutionTimedOut as exc:
                current = self.store.get(job.job_id)
                if current.attempt_count < current.max_attempts:
                    self.store.fail_or_retry(
                        job.job_id,
                        WorkerJobError(
                            code="timed_out",
                            message=str(exc),
                            retryable=True,
                        ),
                    )
                else:
                    self.store.finish_timed_out(job.job_id, str(exc))
            except ExecutionFailure as exc:
                self.store.fail_or_retry(job.job_id, exc.error)
            except Exception as exc:
                self.store.fail_or_retry(
                    job.job_id,
                    WorkerJobError(
                        code="job_runner_failed",
                        message="The durable job runner failed unexpectedly.",
                        retryable=True,
                        details={
                            "error_type": type(exc).__name__,
                            "error": sanitize_text(str(exc)),
                        },
                    ),
                )
            finally:
                current = self.store.get(job.job_id)
                if current.state.terminal and hasattr(self.executor, "cleanup_workspace"):
                    self.executor.cleanup_workspace(str(job.job_id))  # type: ignore[attr-defined]
