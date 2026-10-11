"""Versioned contracts for the authenticated distributed spherical worker."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from open_storyline.spherical.manifests import atomic_write_json
from open_storyline.spherical.schemas import ReframeRenderRequest, SphericalAsset
from open_storyline.spherical.worker_protocol import WorkerCapabilityResponse, WorkerStitchRequest


DISTRIBUTED_API_VERSION = "1.0"
DISTRIBUTED_SCHEMA_VERSION = Literal["1.0"]
IDEMPOTENCY_KEY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class DistributedProtocolModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    schema_version: DISTRIBUTED_SCHEMA_VERSION = "1.0"


class WorkerJobOperation(str, Enum):
    STITCH = "stitch"
    REFRAME = "reframe"
    ANALYZE = "analyze"


class WorkerJobState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"

    @property
    def terminal(self) -> bool:
        return self in {
            WorkerJobState.SUCCEEDED,
            WorkerJobState.FAILED,
            WorkerJobState.CANCELLED,
            WorkerJobState.TIMED_OUT,
        }


class WorkerJobEventKind(str, Enum):
    ACCEPTED = "accepted"
    IDEMPOTENT_REPLAY = "idempotent_replay"
    RUNNING = "running"
    PROGRESS = "progress"
    RETRYING = "retrying"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    RECOVERED = "recovered"


class RetryPolicy(DistributedProtocolModel):
    max_attempts: int = Field(default=2, ge=1, le=5)
    initial_delay_seconds: float = Field(default=1.0, ge=0.0, le=300.0)
    maximum_delay_seconds: float = Field(default=30.0, ge=0.0, le=900.0)


class AnalysisSettings(DistributedProtocolModel):
    interval_seconds: float = Field(default=0.5, ge=0.1, le=10.0)
    max_frames: int = Field(default=120, ge=1, le=600)
    yaw_step_degrees: Literal[30, 45, 60, 90, 120] = 60
    pitch_centers: list[float] = Field(default_factory=lambda: [-45.0, 0.0, 45.0])
    horizontal_fov: float = Field(default=100.0, gt=1.0, lt=179.0)
    view_width: int = Field(default=256, ge=96, le=1024, multiple_of=2)
    view_height: int = Field(default=256, ge=96, le=1024, multiple_of=2)
    minimum_detection_confidence: float = Field(default=0.25, ge=0.0, le=1.0)
    dedup_angular_threshold_degrees: float = Field(default=10.0, gt=0.0, le=45.0)
    max_track_angular_distance_degrees: float = Field(default=45.0, gt=0.0, le=180.0)
    max_occlusion_seconds: float = Field(default=1.5, ge=0.0, le=30.0)

    @field_validator("pitch_centers")
    @classmethod
    def validate_pitch_centers(cls, values: list[float]) -> list[float]:
        if (
            not values
            or len(values) > 9
            or any(value < -90.0 or value > 90.0 for value in values)
        ):
            raise ValueError(
                "pitch_centers must contain one through nine values inside -90..90"
            )
        return values


class StitchJobSubmission(DistributedProtocolModel):
    request: WorkerStitchRequest
    timeout_seconds: float | None = Field(default=None, gt=0.0, le=604_800.0)
    retry_policy: RetryPolicy = Field(default_factory=RetryPolicy)


class ReframeJobSubmission(DistributedProtocolModel):
    request: ReframeRenderRequest
    timeout_seconds: float | None = Field(default=None, gt=0.0, le=604_800.0)
    retry_policy: RetryPolicy = Field(default_factory=RetryPolicy)


class AnalyzeJobSubmission(DistributedProtocolModel):
    asset: SphericalAsset
    cache_dir: Path
    settings: AnalysisSettings = Field(default_factory=AnalysisSettings)
    timeout_seconds: float | None = Field(default=None, gt=0.0, le=604_800.0)
    retry_policy: RetryPolicy = Field(default_factory=RetryPolicy)


class WorkerJobError(DistributedProtocolModel):
    code: str = Field(min_length=1, pattern=r"^[a-z0-9_]+$")
    message: str = Field(min_length=1)
    retryable: bool = False
    details: dict[str, Any] = Field(default_factory=dict)


class WorkerArtifact(DistributedProtocolModel):
    name: str = Field(min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    path: Path
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)
    media_type: str = Field(default="application/octet-stream", min_length=1)


class WorkerJobRecord(DistributedProtocolModel):
    job_id: UUID
    operation: WorkerJobOperation
    state: WorkerJobState
    idempotency_key: str = Field(pattern=IDEMPOTENCY_KEY_PATTERN)
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    progress_percent: int = Field(default=0, ge=0, le=100)
    message: str = Field(default="queued", min_length=1)
    attempt_count: int = Field(default=0, ge=0)
    max_attempts: int = Field(default=1, ge=1, le=5)
    timeout_seconds: float = Field(gt=0.0)
    cancel_requested: bool = False
    result: dict[str, Any] | None = None
    error: WorkerJobError | None = None
    artifacts: list[WorkerArtifact] = Field(default_factory=list)
    metrics: dict[str, float | int | bool | str | None] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    started_at: datetime | None = None
    completed_at: datetime | None = None


class WorkerJobEvent(DistributedProtocolModel):
    job_id: UUID
    sequence: int = Field(ge=1)
    kind: WorkerJobEventKind
    progress_percent: int = Field(ge=0, le=100)
    message: str = Field(min_length=1)
    data: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)


class JobSubmissionResponse(DistributedProtocolModel):
    job: WorkerJobRecord
    created: bool
    idempotent_replay: bool


class DistributedWorkerCapabilities(DistributedProtocolModel):
    api_version: str = DISTRIBUTED_API_VERSION
    worker_version: str = Field(min_length=1)
    operations: list[WorkerJobOperation]
    authentication: Literal["bearer"] = "bearer"
    event_transport: Literal["server-sent-events"] = "server-sent-events"
    shared_storage_supported: bool = True
    http_upload_supported: bool
    max_request_bytes: int = Field(gt=0)
    max_upload_bytes: int = Field(gt=0)
    max_concurrent_jobs: int = Field(gt=0)
    max_queued_jobs: int = Field(gt=0)
    stitch: WorkerCapabilityResponse
    diagnostics_summary: dict[str, str | bool | int | float | None] = Field(
        default_factory=dict
    )


class WorkerHealthResponse(DistributedProtocolModel):
    status: Literal["live", "ready", "not_ready"]
    worker_version: str = Field(min_length=1)
    checks: dict[str, bool | str | int | float | None] = Field(default_factory=dict)


class WorkerMetricsResponse(DistributedProtocolModel):
    worker_version: str = Field(min_length=1)
    generated_at: datetime = Field(default_factory=utc_now)
    job_counts: dict[str, int]
    operation_counts: dict[str, int]
    failure_types: dict[str, int]
    average_job_duration_seconds: dict[str, float]
    stitch_fps: float | None = None
    reframe_fps: float | None = None
    cache_hit_rate: float = Field(ge=0.0, le=1.0)
    output_bytes: int = Field(ge=0)
    gpu_available: bool
    gpu_memory_used_bytes: int | None = Field(default=None, ge=0)
    sdk_capability: dict[str, Any]


class WorkerDiagnosticCheck(DistributedProtocolModel):
    name: str = Field(min_length=1)
    status: Literal["pass", "warn", "fail"]
    required: bool
    message: str = Field(min_length=1)
    details: dict[str, Any] = Field(default_factory=dict)


class WorkerDiagnosticReport(DistributedProtocolModel):
    worker_version: str = Field(min_length=1)
    compatible: bool
    checks: list[WorkerDiagnosticCheck]
    generated_at: datetime = Field(default_factory=utc_now)


class WorkerUploadResponse(DistributedProtocolModel):
    path: Path
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)


DISTRIBUTED_SCHEMA_MODELS: dict[str, type[BaseModel]] = {
    "distributed-capabilities.schema.json": DistributedWorkerCapabilities,
    "job-submission-response.schema.json": JobSubmissionResponse,
    "job-record.schema.json": WorkerJobRecord,
    "job-event.schema.json": WorkerJobEvent,
    "stitch-job-submission.schema.json": StitchJobSubmission,
    "reframe-job-submission.schema.json": ReframeJobSubmission,
    "analyze-job-submission.schema.json": AnalyzeJobSubmission,
    "health-response.schema.json": WorkerHealthResponse,
    "metrics-response.schema.json": WorkerMetricsResponse,
    "diagnostic-report.schema.json": WorkerDiagnosticReport,
    "upload-response.schema.json": WorkerUploadResponse,
}


def export_distributed_json_schemas(destination: str | Path) -> list[Path]:
    root = Path(destination).expanduser().resolve(strict=False)
    root.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for filename, model in sorted(DISTRIBUTED_SCHEMA_MODELS.items()):
        path = root / filename
        atomic_write_json(path, model.model_json_schema())
        written.append(path)
    return written
