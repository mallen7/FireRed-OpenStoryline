"""Versioned protocol contracts for the standalone Insta360 worker."""

from __future__ import annotations

import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from open_storyline.spherical.manifests import atomic_write_json


WORKER_PROTOCOL_VERSION = "1.0"
WORKER_SCHEMA_VERSION = Literal["1.0"]
WorkerCodec = Literal["h264", "h265"]
WorkerStitchType = Literal["template", "optical_flow", "dynamic", "ai"]


class WorkerProtocolModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    schema_version: WORKER_SCHEMA_VERSION = "1.0"


class WorkerProbeRequest(WorkerProtocolModel):
    input_paths: list[Path] = Field(min_length=1, max_length=2)


class WorkerInputMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: Path
    size_bytes: int = Field(ge=0)
    modified_unix_seconds: float = Field(ge=0)
    readable: bool
    extension: str
    stream_metadata: dict[str, Any] = Field(default_factory=dict)


class WorkerProbeResponse(WorkerProtocolModel):
    provider: str
    decodable: bool
    inputs: list[WorkerInputMetadata] = Field(min_length=1)
    camera_model: str | None = None
    warnings: list[str] = Field(default_factory=list)


class WorkerStitchRequest(WorkerProtocolModel):
    job_id: UUID
    input_paths: list[Path] = Field(min_length=1, max_length=2)
    output_path: Path
    output_width: int = Field(default=7680, gt=0, multiple_of=2)
    output_height: int = Field(default=3840, gt=0, multiple_of=2)
    codec: WorkerCodec = "h265"
    bitrate_bps: int = Field(default=120_000_000, gt=0, le=1_000_000_000)
    enable_flowstate: bool = True
    enable_direction_lock: bool = False
    stitch_type: WorkerStitchType = "ai"
    enable_stitch_fusion: bool = True
    model_root: Path | None = None
    overwrite: bool = False

    @model_validator(mode="after")
    def validate_stitch_request(self) -> "WorkerStitchRequest":
        if not math.isclose(
            self.output_width / self.output_height,
            2.0,
            rel_tol=0.0,
            abs_tol=1e-12,
        ):
            raise ValueError("worker output dimensions must be exactly 2:1")
        if self.output_path.suffix.lower() != ".mp4":
            raise ValueError("worker output_path must use the .mp4 extension")
        if len({str(path) for path in self.input_paths}) != len(self.input_paths):
            raise ValueError("worker input_paths must be unique and explicitly ordered")
        for path in self.input_paths:
            if path.suffix.lower() != ".insv":
                raise ValueError("worker video inputs must use the .insv extension")
        return self


class WorkerStitchProgressEvent(WorkerProtocolModel):
    event: Literal["progress"] = "progress"
    job_id: UUID
    progress_percent: int = Field(ge=0, le=100)
    message: str = Field(min_length=1)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


class WorkerStitchResult(WorkerProtocolModel):
    event: Literal["result"] = "result"
    job_id: UUID
    provider: str
    input_paths: list[Path] = Field(min_length=1, max_length=2)
    source_sha256: list[str] = Field(min_length=1, max_length=2)
    output_path: Path
    output_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    output_width: int = Field(gt=0)
    output_height: int = Field(gt=0)
    codec: WorkerCodec
    bitrate_bps: int = Field(gt=0)
    enable_flowstate: bool
    enable_direction_lock: bool
    stitch_type: WorkerStitchType
    enable_stitch_fusion: bool
    elapsed_seconds: float = Field(ge=0)
    sdk_log_path: Path | None = None
    warnings: list[str] = Field(default_factory=list)

    @field_validator("source_sha256")
    @classmethod
    def validate_source_hashes(cls, values: list[str]) -> list[str]:
        if any(
            len(value) != 64
            or value.lower() != value
            or any(character not in "0123456789abcdef" for character in value)
            for value in values
        ):
            raise ValueError("source_sha256 must contain lowercase SHA-256 values")
        return values

    @model_validator(mode="after")
    def validate_result(self) -> "WorkerStitchResult":
        if len(self.input_paths) != len(self.source_sha256):
            raise ValueError("result input_paths/source_sha256 lengths must match")
        if self.output_width != self.output_height * 2:
            raise ValueError("worker result dimensions must be exactly 2:1")
        return self


class WorkerErrorResponse(WorkerProtocolModel):
    event: Literal["error"] = "error"
    code: str = Field(min_length=1, pattern=r"^[a-z0-9_]+$")
    message: str = Field(min_length=1)
    details: dict[str, Any] = Field(default_factory=dict)
    retryable: bool = False
    job_id: UUID | None = None


class WorkerCapabilityResponse(WorkerProtocolModel):
    worker_version: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    sdk_enabled: bool
    sdk_version: str | None = None
    can_decode_insv: bool
    can_stitch: bool
    supports_progress: bool
    supports_cancellation: bool
    supported_codecs: list[WorkerCodec]
    supported_stitch_types: list[WorkerStitchType]
    max_input_files: int = Field(ge=1)
    requires_gpu: bool
    target_platform: str
    model_root_configured: bool
    warnings: list[str] = Field(default_factory=list)


class WorkerDiscoveryFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: Path
    filename: str
    size_bytes: int = Field(ge=0)
    modified_unix_seconds: float = Field(ge=0)
    lens_marker: Literal["00", "10"] | None = None
    stream_metadata: dict[str, Any] = Field(default_factory=dict)


class WorkerDiscoveryGroup(BaseModel):
    model_config = ConfigDict(extra="forbid")

    group_id: str = Field(min_length=1)
    files: list[WorkerDiscoveryFile] = Field(min_length=1, max_length=2)
    proposed_order: list[Path] = Field(min_length=1, max_length=2)
    confidence: float = Field(ge=0.0, le=1.0)
    reasons: list[str] = Field(min_length=1)
    warnings: list[str] = Field(default_factory=list)
    requires_confirmation: bool = True


class WorkerDiscoveryResponse(WorkerProtocolModel):
    directory: Path
    groups: list[WorkerDiscoveryGroup]
    ungrouped: list[WorkerDiscoveryFile] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


WORKER_SCHEMA_MODELS: dict[str, type[BaseModel]] = {
    "probe-request.schema.json": WorkerProbeRequest,
    "probe-response.schema.json": WorkerProbeResponse,
    "stitch-request.schema.json": WorkerStitchRequest,
    "stitch-progress-event.schema.json": WorkerStitchProgressEvent,
    "stitch-result.schema.json": WorkerStitchResult,
    "error-response.schema.json": WorkerErrorResponse,
    "capability-response.schema.json": WorkerCapabilityResponse,
    "discovery-response.schema.json": WorkerDiscoveryResponse,
}


def export_worker_json_schemas(destination: str | Path) -> list[Path]:
    """Write deterministic JSON Schemas shared by the worker and Python client."""

    root = Path(destination).expanduser().resolve(strict=False)
    root.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for filename, model in WORKER_SCHEMA_MODELS.items():
        path = root / filename
        atomic_write_json(path, model.model_json_schema())
        written.append(path)
    return written
