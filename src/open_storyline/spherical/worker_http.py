"""Authenticated FastAPI service for durable distributed spherical jobs."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import platform
import re
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Callable

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, status
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from open_storyline.spherical.diagnostics import collect_worker_diagnostics, gpu_snapshot
from open_storyline.spherical.distributed_protocol import (
    IDEMPOTENCY_KEY_PATTERN,
    AnalyzeJobSubmission,
    DistributedWorkerCapabilities,
    JobSubmissionResponse,
    ReframeJobSubmission,
    StitchJobSubmission,
    WorkerHealthResponse,
    WorkerJobOperation,
    WorkerJobRecord,
    WorkerJobState,
    WorkerMetricsResponse,
    WorkerUploadResponse,
)
from open_storyline.spherical.job_runner import (
    DurableJobManager,
    JobExecutor,
    SubprocessJobExecutor,
)
from open_storyline.spherical.job_store import (
    DurableJobStore,
    IdempotencyConflict,
    JobQueueFull,
)
from open_storyline.spherical.ffmpeg import probe_ffmpeg_capabilities
from open_storyline.spherical.manifests import sha256_file
from open_storyline.spherical.worker_client import LocalStitchWorkerClient
from open_storyline.spherical.worker_logging import configure_structured_logging
from open_storyline.spherical.worker_protocol import WorkerCapabilityResponse
from open_storyline.spherical.worker_security import (
    DistributedWorkerSettings,
    WorkerPathPolicy,
    WorkerSecurityError,
    sanitize_text,
)


class RequestSizeLimitMiddleware:
    """Bound JSON and upload bodies even when Content-Length is absent."""

    def __init__(self, app, *, request_limit: int, upload_limit: int) -> None:
        self.app = app
        self.request_limit = request_limit
        self.upload_limit = upload_limit

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        limit = self.upload_limit if path.startswith("/v1/uploads/") else self.request_limit
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        content_length = headers.get(b"content-length")
        if content_length is not None:
            try:
                if int(content_length) > limit:
                    await self._reject(send, limit)
                    return
            except ValueError:
                await self._reject(send, limit)
                return
        received = 0

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise _RequestTooLarge(limit)
            return message

        try:
            await self.app(scope, limited_receive, send)
        except _RequestTooLarge:
            await self._reject(send, limit)

    @staticmethod
    async def _reject(send, limit: int) -> None:
        body = json.dumps(
            {"detail": "request body exceeds configured limit", "limit_bytes": limit}
        ).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


class _RequestTooLarge(RuntimeError):
    pass


def _unavailable_stitch_capability(settings: DistributedWorkerSettings) -> WorkerCapabilityResponse:
    return WorkerCapabilityResponse(
        worker_version="unavailable",
        provider="unavailable",
        sdk_enabled=False,
        sdk_version=None,
        can_decode_insv=False,
        can_stitch=False,
        supports_progress=True,
        supports_cancellation=True,
        supported_codecs=[],
        supported_stitch_types=[],
        max_input_files=2,
        requires_gpu=True,
        target_platform=platform.platform(),
        model_root_configured=settings.model_root is not None,
        warnings=["standalone MediaSDK worker capability is unavailable"],
    )


def _stitch_capability(settings: DistributedWorkerSettings) -> WorkerCapabilityResponse:
    try:
        return LocalStitchWorkerClient(
            settings.worker_executable, timeout_seconds=30.0
        ).capabilities()
    except Exception:
        return _unavailable_stitch_capability(settings)


def _validate_idempotency_key(value: str) -> str:
    if not re.fullmatch(IDEMPOTENCY_KEY_PATTERN, value):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Idempotency-Key must be 8-128 safe ASCII characters.",
        )
    return value


def create_worker_app(
    settings: DistributedWorkerSettings,
    *,
    executor: JobExecutor | None = None,
    start_manager: bool = True,
) -> FastAPI:
    configure_structured_logging()
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    for root in [*settings.media_roots, *settings.output_roots]:
        root.mkdir(parents=True, exist_ok=True)
    if settings.upload_root is not None:
        settings.upload_root.mkdir(parents=True, exist_ok=True)
    path_policy = WorkerPathPolicy(settings)
    store = DurableJobStore(
        settings.state_dir / "jobs.sqlite3",
        max_queued_jobs=settings.max_queued_jobs,
    )
    selected_executor = executor or SubprocessJobExecutor(settings, path_policy)
    manager = DurableJobManager(store, settings, selected_executor)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if start_manager:
            manager.start()
        try:
            yield
        finally:
            if start_manager:
                manager.stop()

    app = FastAPI(
        title="OpenStoryline Distributed Spherical Worker",
        version=settings.worker_version,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.add_middleware(
        RequestSizeLimitMiddleware,
        request_limit=settings.max_request_bytes,
        upload_limit=settings.max_upload_bytes,
    )
    app.state.worker_settings = settings
    app.state.path_policy = path_policy
    app.state.job_store = store
    app.state.job_manager = manager

    bearer = HTTPBearer(auto_error=False)

    def require_auth(
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    ) -> None:
        expected = settings.bearer_token.get_secret_value().encode("utf-8")
        supplied = (
            credentials.credentials.encode("utf-8")
            if credentials is not None and credentials.scheme.lower() == "bearer"
            else b""
        )
        if not supplied or not hmac.compare_digest(supplied, expected):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="valid bearer authentication is required",
                headers={"WWW-Authenticate": "Bearer"},
            )

    @app.exception_handler(WorkerSecurityError)
    async def security_error_handler(_: Request, exc: WorkerSecurityError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={
                "error": {
                    "code": exc.code,
                    "message": str(exc),
                    "details": exc.details,
                }
            },
        )

    @app.get("/health/live", response_model=WorkerHealthResponse)
    def health_live() -> WorkerHealthResponse:
        return WorkerHealthResponse(
            status="live",
            worker_version=settings.worker_version,
            checks={"process": True},
        )

    def readiness() -> WorkerHealthResponse:
        stitch = _stitch_capability(settings)
        try:
            ffmpeg = probe_ffmpeg_capabilities()
            media_ready = bool(
                ffmpeg.v360_available
                and ffmpeg.libx264_available
                and ffmpeg.concat_demuxer_available
            )
            ffmpeg_status = ffmpeg.version_line if media_ready else "required capability missing"
        except Exception as exc:
            media_ready = False
            ffmpeg_status = sanitize_text(str(exc), maximum_length=500)
        checks: dict[str, bool | str | int | float | None] = {
            "database": store.database_path.is_file(),
            "job_manager": manager.running if start_manager else True,
            "output_roots": all(path.is_dir() and os.access(path, os.W_OK) for path in settings.output_roots),
            "stitch": stitch.can_stitch,
            "reframe": media_ready,
            "analyze": media_ready,
            "ffmpeg": ffmpeg_status,
        }
        required_ready = bool(
            checks["database"]
            and checks["job_manager"]
            and checks["output_roots"]
            and media_ready
        )
        if settings.require_stitch_ready:
            required_ready = required_ready and stitch.can_stitch
        return WorkerHealthResponse(
            status="ready" if required_ready else "not_ready",
            worker_version=settings.worker_version,
            checks=checks,
        )

    @app.get("/health/ready", response_model=WorkerHealthResponse)
    def health_ready(response: Response) -> WorkerHealthResponse:
        report = readiness()
        if report.status != "ready":
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return report

    @app.get(
        "/v1/capabilities",
        response_model=DistributedWorkerCapabilities,
        dependencies=[Depends(require_auth)],
    )
    def capabilities() -> DistributedWorkerCapabilities:
        stitch = _stitch_capability(settings)
        gpu = gpu_snapshot()
        return DistributedWorkerCapabilities(
            worker_version=settings.worker_version,
            operations=list(WorkerJobOperation),
            http_upload_supported=settings.upload_root is not None,
            max_request_bytes=settings.max_request_bytes,
            max_upload_bytes=settings.max_upload_bytes,
            max_concurrent_jobs=settings.max_concurrent_jobs,
            max_queued_jobs=settings.max_queued_jobs,
            stitch=stitch,
            diagnostics_summary={
                "gpu_available": gpu["available"],
                "gpu_memory_used_bytes": gpu["memory_used_bytes"],
                "sdk_enabled": stitch.sdk_enabled,
                "stitch_ready": stitch.can_stitch,
            },
        )

    def submit(
        operation: WorkerJobOperation,
        request_payload: dict[str, Any],
        idempotency_key: str,
        timeout_seconds: float | None,
        retry_policy,
        response: Response,
    ) -> JobSubmissionResponse:
        key = _validate_idempotency_key(idempotency_key)
        try:
            job, created = store.create_or_get(
                operation=operation,
                idempotency_key=key,
                request=request_payload,
                timeout_seconds=timeout_seconds or settings.default_timeout_seconds,
                retry_policy=retry_policy,
            )
        except IdempotencyConflict as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        except JobQueueFull as exc:
            raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=str(exc)) from exc
        response.status_code = status.HTTP_202_ACCEPTED if created else status.HTTP_200_OK
        return JobSubmissionResponse(
            job=job,
            created=created,
            idempotent_replay=not created,
        )

    @app.post(
        "/v1/jobs/stitch",
        response_model=JobSubmissionResponse,
        dependencies=[Depends(require_auth)],
    )
    def submit_stitch(
        submission: StitchJobSubmission,
        response: Response,
        idempotency_key: str = Header(alias="Idempotency-Key"),
    ) -> JobSubmissionResponse:
        normalized = path_policy.normalize_stitch(submission)
        return submit(
            WorkerJobOperation.STITCH,
            normalized.model_dump(mode="json"),
            idempotency_key,
            normalized.timeout_seconds,
            normalized.retry_policy,
            response,
        )

    @app.post(
        "/v1/jobs/reframe",
        response_model=JobSubmissionResponse,
        dependencies=[Depends(require_auth)],
    )
    def submit_reframe(
        submission: ReframeJobSubmission,
        response: Response,
        idempotency_key: str = Header(alias="Idempotency-Key"),
    ) -> JobSubmissionResponse:
        normalized = path_policy.normalize_reframe(submission)
        return submit(
            WorkerJobOperation.REFRAME,
            normalized.model_dump(mode="json"),
            idempotency_key,
            normalized.timeout_seconds,
            normalized.retry_policy,
            response,
        )

    @app.post(
        "/v1/jobs/analyze",
        response_model=JobSubmissionResponse,
        dependencies=[Depends(require_auth)],
    )
    def submit_analyze(
        submission: AnalyzeJobSubmission,
        response: Response,
        idempotency_key: str = Header(alias="Idempotency-Key"),
    ) -> JobSubmissionResponse:
        normalized = path_policy.normalize_analyze(submission)
        return submit(
            WorkerJobOperation.ANALYZE,
            normalized.model_dump(mode="json"),
            idempotency_key,
            normalized.timeout_seconds,
            normalized.retry_policy,
            response,
        )

    def get_job_or_404(job_id: str) -> WorkerJobRecord:
        try:
            return store.get(job_id)
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="job not found") from exc

    @app.get(
        "/v1/jobs/{job_id}",
        response_model=WorkerJobRecord,
        dependencies=[Depends(require_auth)],
    )
    def get_job(job_id: str) -> WorkerJobRecord:
        return get_job_or_404(job_id)

    @app.post(
        "/v1/jobs/{job_id}/cancel",
        response_model=WorkerJobRecord,
        dependencies=[Depends(require_auth)],
    )
    def cancel_job(job_id: str) -> WorkerJobRecord:
        get_job_or_404(job_id)
        return store.request_cancel(job_id)

    @app.get(
        "/v1/jobs/{job_id}/events",
        dependencies=[Depends(require_auth)],
    )
    async def stream_events(
        job_id: str,
        request: Request,
        after_sequence: int = 0,
    ) -> StreamingResponse:
        get_job_or_404(job_id)

        async def generate() -> AsyncIterator[str]:
            sequence = max(0, after_sequence)
            idle_loops = 0
            while True:
                if await request.is_disconnected():
                    return
                events = store.events(job_id, after_sequence=sequence)
                for event in events:
                    sequence = event.sequence
                    yield (
                        f"id: {event.sequence}\n"
                        f"event: {event.kind.value}\n"
                        f"data: {event.model_dump_json()}\n\n"
                    )
                job = store.get(job_id)
                if job.state.terminal and not store.events(job_id, after_sequence=sequence):
                    return
                idle_loops += 1
                if not events and idle_loops % 50 == 0:
                    yield ": heartbeat\n\n"
                await asyncio.sleep(settings.event_poll_seconds)

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get(
        "/v1/jobs/{job_id}/artifacts/{artifact_name}",
        dependencies=[Depends(require_auth)],
    )
    def download_artifact(job_id: str, artifact_name: str) -> FileResponse:
        job = get_job_or_404(job_id)
        artifact = next((item for item in job.artifacts if item.name == artifact_name), None)
        if artifact is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="artifact not found")
        path = path_policy.output_path(artifact.path, role="download artifact")
        if not path.is_file() or sha256_file(path) != artifact.sha256:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="artifact is missing or failed integrity validation",
            )
        return FileResponse(path, media_type=artifact.media_type, filename=path.name)

    @app.post(
        "/v1/uploads/{relative_path:path}",
        response_model=WorkerUploadResponse,
        status_code=status.HTTP_201_CREATED,
        dependencies=[Depends(require_auth)],
    )
    async def upload_media(relative_path: str, request: Request) -> WorkerUploadResponse:
        destination = path_policy.upload_destination(relative_path)
        if destination.exists():
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="upload already exists")
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".upload", dir=destination.parent
        )
        temporary = Path(temporary_name)
        digest = hashlib.sha256()
        size = 0
        try:
            with os.fdopen(descriptor, "wb") as handle:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > settings.max_upload_bytes:
                        raise HTTPException(
                            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                            detail="upload exceeds configured limit",
                        )
                    handle.write(chunk)
                    digest.update(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary, destination)
            except FileExistsError as exc:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="upload already exists",
                ) from exc
            temporary.unlink()
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        return WorkerUploadResponse(
            path=destination,
            sha256=digest.hexdigest(),
            size_bytes=size,
        )

    @app.get(
        "/v1/metrics",
        response_model=WorkerMetricsResponse,
        dependencies=[Depends(require_auth)],
    )
    def metrics() -> WorkerMetricsResponse:
        snapshot = store.snapshot_metrics()
        gpu = gpu_snapshot()
        stitch = _stitch_capability(settings)
        rates = snapshot["average_processing_fps"]
        return WorkerMetricsResponse(
            worker_version=settings.worker_version,
            job_counts=snapshot["job_counts"],
            operation_counts=snapshot["operation_counts"],
            failure_types=snapshot["failure_types"],
            average_job_duration_seconds=snapshot["average_job_duration_seconds"],
            stitch_fps=rates.get("stitch"),
            reframe_fps=rates.get("reframe"),
            cache_hit_rate=snapshot["cache_hit_rate"],
            output_bytes=snapshot["output_bytes"],
            gpu_available=bool(gpu["available"]),
            gpu_memory_used_bytes=gpu["memory_used_bytes"],
            sdk_capability={
                "provider": stitch.provider,
                "sdk_enabled": stitch.sdk_enabled,
                "sdk_version": stitch.sdk_version,
                "can_stitch": stitch.can_stitch,
            },
        )

    @app.get(
        "/v1/diagnostics",
        dependencies=[Depends(require_auth)],
    )
    def diagnostics():
        return collect_worker_diagnostics(settings)

    return app
