"""Authenticated HTTP client for the durable distributed spherical worker."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from open_storyline.spherical.analysis_schemas import SphericalAnalysis
from open_storyline.spherical.distributed_protocol import (
    AnalyzeJobSubmission,
    DistributedWorkerCapabilities,
    JobSubmissionResponse,
    ReframeJobSubmission,
    StitchJobSubmission,
    WorkerJobEvent,
    WorkerJobRecord,
    WorkerJobState,
    WorkerUploadResponse,
)
from open_storyline.spherical.errors import SphericalWorkerError
from open_storyline.spherical.manifests import sha256_file, sha256_json
from open_storyline.spherical.schemas import ReframeRenderManifest
from open_storyline.spherical.worker_client import LocalStitchWorkerClient, ProgressCallback
from open_storyline.spherical.worker_protocol import (
    WorkerCapabilityResponse,
    WorkerDiscoveryResponse,
    WorkerProbeRequest,
    WorkerProbeResponse,
    WorkerStitchProgressEvent,
    WorkerStitchRequest,
    WorkerStitchResult,
)
from open_storyline.spherical.worker_security import sanitize_text


ResponseModel = TypeVar("ResponseModel", bound=BaseModel)
EventCallback = Callable[[WorkerJobEvent], None]


class HttpSphericalWorkerClient:
    """Submit typed jobs and consume progress over server-sent events."""

    def __init__(
        self,
        base_url: str,
        bearer_token: str,
        *,
        timeout_seconds: float = 86_400.0,
        verify_tls: bool = True,
        transport: httpx.BaseTransport | None = None,
        transport_retries: int = 2,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.transport_retries = max(0, min(5, transport_retries))
        self._client = httpx.Client(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {bearer_token}"},
            timeout=httpx.Timeout(30.0, read=None, write=timeout_seconds, pool=30.0),
            verify=verify_tls,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "HttpSphericalWorkerClient":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    @staticmethod
    def _decode(response: httpx.Response, model: type[ResponseModel]) -> ResponseModel:
        try:
            response.raise_for_status()
            return model.model_validate(response.json())
        except (httpx.HTTPError, json.JSONDecodeError, ValidationError) as exc:
            body = sanitize_text(response.text, maximum_length=4000)
            raise SphericalWorkerError(
                "Distributed worker returned an invalid or unsuccessful response.",
                details={
                    "status_code": response.status_code,
                    "response": body,
                    "error": sanitize_text(str(exc)),
                },
            ) from exc

    def service_capabilities(self) -> DistributedWorkerCapabilities:
        return self._decode(
            self._client.get("/v1/capabilities"),
            DistributedWorkerCapabilities,
        )

    def capabilities(self) -> WorkerCapabilityResponse:
        return self.service_capabilities().stitch

    def discover(self, directory: str | Path) -> WorkerDiscoveryResponse:
        raise SphericalWorkerError(
            "Remote discovery is intentionally unavailable; select shared-storage inputs explicitly.",
            details={"directory": str(directory)},
        )

    def probe(self, request: WorkerProbeRequest) -> WorkerProbeResponse:
        raise SphericalWorkerError(
            "Remote probe is intentionally unavailable; job inputs are validated at submission.",
            details={"input_count": len(request.input_paths)},
        )

    @staticmethod
    def _idempotency_key(value: Any, explicit: str | None) -> str:
        return explicit or f"request-{sha256_json(value)}"

    def _submit(
        self,
        endpoint: str,
        submission: BaseModel,
        *,
        idempotency_key: str | None,
    ) -> JobSubmissionResponse:
        key = self._idempotency_key(submission, idempotency_key)
        response: httpx.Response | None = None
        last_error: httpx.TransportError | None = None
        for attempt in range(self.transport_retries + 1):
            try:
                response = self._client.post(
                    endpoint,
                    headers={"Idempotency-Key": key},
                    json=submission.model_dump(mode="json"),
                )
                break
            except httpx.TransportError as exc:
                last_error = exc
                if attempt >= self.transport_retries:
                    raise SphericalWorkerError(
                        "Distributed job submission failed after transport retries.",
                        details={"attempts": attempt + 1, "error": sanitize_text(str(exc))},
                    ) from exc
                time.sleep(0.1 * (2**attempt))
        assert response is not None, last_error
        return self._decode(response, JobSubmissionResponse)

    def submit_stitch(
        self,
        submission: StitchJobSubmission,
        *,
        idempotency_key: str | None = None,
    ) -> JobSubmissionResponse:
        return self._submit(
            "/v1/jobs/stitch", submission, idempotency_key=idempotency_key
        )

    def submit_reframe(
        self,
        submission: ReframeJobSubmission,
        *,
        idempotency_key: str | None = None,
    ) -> JobSubmissionResponse:
        return self._submit(
            "/v1/jobs/reframe", submission, idempotency_key=idempotency_key
        )

    def submit_analyze(
        self,
        submission: AnalyzeJobSubmission,
        *,
        idempotency_key: str | None = None,
    ) -> JobSubmissionResponse:
        return self._submit(
            "/v1/jobs/analyze", submission, idempotency_key=idempotency_key
        )

    def get_job(self, job_id: str) -> WorkerJobRecord:
        return self._decode(self._client.get(f"/v1/jobs/{job_id}"), WorkerJobRecord)

    def cancel(self, job_id: str) -> WorkerJobRecord:
        return self._decode(
            self._client.post(f"/v1/jobs/{job_id}/cancel"), WorkerJobRecord
        )

    def iter_events(
        self,
        job_id: str,
        *,
        after_sequence: int = 0,
        cancel_event: threading.Event | None = None,
    ) -> Iterator[WorkerJobEvent]:
        sequence = after_sequence
        reconnects = 0
        while True:
            try:
                with self._client.stream(
                    "GET",
                    f"/v1/jobs/{job_id}/events",
                    params={"after_sequence": sequence},
                    headers={"Accept": "text/event-stream"},
                ) as response:
                    response.raise_for_status()
                    for line in response.iter_lines():
                        if cancel_event is not None and cancel_event.is_set():
                            self.cancel(job_id)
                        if not line.startswith("data: "):
                            continue
                        event = WorkerJobEvent.model_validate_json(line[6:])
                        sequence = event.sequence
                        yield event
                job = self.get_job(job_id)
                if job.state.terminal:
                    return
            except ValidationError as exc:
                raise SphericalWorkerError(
                    "Distributed worker emitted an invalid progress event.",
                    details={"job_id": job_id, "after_sequence": sequence},
                ) from exc
            except httpx.HTTPStatusError as exc:
                if 400 <= exc.response.status_code < 500:
                    raise SphericalWorkerError(
                        "Could not stream distributed-worker events.",
                        details={"status_code": exc.response.status_code},
                    ) from exc
            except httpx.TransportError:
                pass
            reconnects += 1
            if reconnects > self.transport_retries:
                raise SphericalWorkerError(
                    "Distributed-worker event stream ended before a terminal state.",
                    details={"job_id": job_id, "after_sequence": sequence},
                )
            time.sleep(0.1 * (2 ** (reconnects - 1)))

    def wait_for_job(
        self,
        job_id: str,
        *,
        on_event: EventCallback | None = None,
        cancel_event: threading.Event | None = None,
    ) -> WorkerJobRecord:
        for event in self.iter_events(job_id, cancel_event=cancel_event):
            if on_event is not None:
                on_event(event)
        job = self.get_job(job_id)
        if job.state != WorkerJobState.SUCCEEDED:
            raise SphericalWorkerError(
                f"Distributed {job.operation.value} job ended in state {job.state.value}.",
                details={
                    "job_id": str(job.job_id),
                    "state": job.state.value,
                    "error": job.error.model_dump(mode="json") if job.error else None,
                },
            )
        return job

    def stitch(
        self,
        request: WorkerStitchRequest,
        *,
        on_progress: ProgressCallback | None = None,
        cancel_event: threading.Event | None = None,
    ) -> WorkerStitchResult:
        capabilities = self.capabilities()
        unavailable: list[str] = []
        if not capabilities.can_decode_insv:
            unavailable.append("raw .insv decoding")
        if not capabilities.can_stitch:
            unavailable.append("stitching")
        if request.codec not in capabilities.supported_codecs:
            unavailable.append(f"codec {request.codec}")
        if request.stitch_type not in capabilities.supported_stitch_types:
            unavailable.append(f"stitch type {request.stitch_type}")
        if unavailable:
            raise SphericalWorkerError(
                "Distributed worker does not have the requested stitch capability.",
                details={"unavailable": unavailable, "warnings": capabilities.warnings},
            )
        submission = self.submit_stitch(
            StitchJobSubmission(request=request, timeout_seconds=self.timeout_seconds)
        )

        def forward(event: WorkerJobEvent) -> None:
            if on_progress is not None and event.kind.value == "progress":
                on_progress(
                    WorkerStitchProgressEvent(
                        job_id=request.job_id,
                        progress_percent=event.progress_percent,
                        message=event.message,
                        created_at=event.created_at,
                    )
                )

        job = self.wait_for_job(
            str(submission.job.job_id),
            on_event=forward,
            cancel_event=cancel_event,
        )
        assert job.result is not None
        result = WorkerStitchResult.model_validate(job.result)
        LocalStitchWorkerClient._validate_result(request, result, capabilities)
        return result

    def reframe(
        self,
        submission: ReframeJobSubmission,
        *,
        idempotency_key: str | None = None,
        on_event: EventCallback | None = None,
        cancel_event: threading.Event | None = None,
    ) -> ReframeRenderManifest:
        accepted = self.submit_reframe(
            submission, idempotency_key=idempotency_key
        )
        job = self.wait_for_job(
            str(accepted.job.job_id),
            on_event=on_event,
            cancel_event=cancel_event,
        )
        return ReframeRenderManifest.model_validate(job.result)

    def analyze(
        self,
        submission: AnalyzeJobSubmission,
        *,
        idempotency_key: str | None = None,
        on_event: EventCallback | None = None,
        cancel_event: threading.Event | None = None,
    ) -> SphericalAnalysis:
        accepted = self.submit_analyze(
            submission, idempotency_key=idempotency_key
        )
        job = self.wait_for_job(
            str(accepted.job.job_id),
            on_event=on_event,
            cancel_event=cancel_event,
        )
        return SphericalAnalysis.model_validate(job.result)

    def upload(self, source: str | Path, relative_path: str) -> WorkerUploadResponse:
        path = Path(source).expanduser().resolve(strict=True)
        with path.open("rb") as handle:
            response = self._client.post(
                f"/v1/uploads/{relative_path.lstrip('/')}",
                content=handle,
                headers={"Content-Type": "application/octet-stream"},
            )
        uploaded = self._decode(response, WorkerUploadResponse)
        if uploaded.sha256 != sha256_file(path) or uploaded.size_bytes != path.stat().st_size:
            raise SphericalWorkerError("Uploaded media failed end-to-end integrity validation.")
        return uploaded

    def download_artifact(
        self,
        job_id: str,
        artifact_name: str,
        destination: str | Path,
        *,
        expected_sha256: str | None = None,
    ) -> Path:
        target = Path(destination).expanduser().resolve(strict=False)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.download")
        try:
            with self._client.stream(
                "GET", f"/v1/jobs/{job_id}/artifacts/{artifact_name}"
            ) as response:
                response.raise_for_status()
                with temporary.open("wb") as handle:
                    for chunk in response.iter_bytes():
                        handle.write(chunk)
            actual = sha256_file(temporary)
            if expected_sha256 is not None and actual != expected_sha256:
                raise SphericalWorkerError("Downloaded artifact failed SHA-256 validation.")
            temporary.replace(target)
            return target
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
