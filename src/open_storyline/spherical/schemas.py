"""Strict, versioned contracts for deterministic spherical reframing."""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


CURRENT_SCHEMA_VERSION = "1.0"
SUPPORTED_SCHEMA_VERSIONS = frozenset({CURRENT_SCHEMA_VERSION})
SphericalSchemaVersion = Literal["1.0"]
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def _normalize_sha256_values(values: list[str]) -> list[str]:
    normalized = [value.lower() for value in values]
    invalid = [value for value in normalized if not SHA256_PATTERN.fullmatch(value)]
    if invalid:
        raise ValueError("values must contain lowercase SHA-256 values")
    return normalized


def _normalize_wrapped_angle(value: float) -> float:
    if not math.isfinite(value):
        raise ValueError("angle must be finite")
    wrapped = (value + 180.0) % 360.0 - 180.0
    if math.isclose(wrapped, -180.0, abs_tol=1e-12) and value > 0:
        return 180.0
    if math.isclose(wrapped, 0.0, abs_tol=1e-12):
        return 0.0
    return wrapped


def _supported_aspect_ratio(width: int, height: int) -> str | None:
    ratio = width / height
    for name, expected in (
        ("16:9", 16.0 / 9.0),
        ("9:16", 9.0 / 16.0),
        ("1:1", 1.0),
    ):
        if math.isclose(ratio, expected, rel_tol=0.01, abs_tol=0.01):
            return name
    return None


class VersionedSphericalModel(BaseModel):
    """Strict base model shared by all versioned spherical JSON contracts."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    schema_version: SphericalSchemaVersion = Field(
        default=CURRENT_SCHEMA_VERSION,
        description="Version of this JSON contract.",
    )


class SourceType(str, Enum):
    EQUIRECTANGULAR = "equirectangular"


class Projection(str, Enum):
    EQUIRECTANGULAR = "equirectangular"


class Easing(str, Enum):
    LINEAR = "linear"
    SMOOTHSTEP = "smoothstep"
    EASE_IN = "ease_in"
    EASE_OUT = "ease_out"
    EASE_IN_OUT = "ease_in_out"


class TrackCreatedBy(str, Enum):
    MANUAL = "manual"
    ASSISTED = "assisted"
    DIRECTOR = "director"


class IngestMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str = "studio-export"
    stabilization: str = "unknown"
    stitch_type: str = "unknown"


class SphericalAsset(VersionedSphericalModel):
    asset_id: str = Field(min_length=1)
    source_type: SourceType = SourceType.EQUIRECTANGULAR
    source_paths: list[Path] = Field(min_length=1)
    source_hashes: list[str] = Field(min_length=1)
    camera_model: str | None = None
    projection: Projection = Projection.EQUIRECTANGULAR
    master_path: Path
    proxy_path: Path | None = None
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    fps: float = Field(gt=0)
    duration_seconds: float = Field(gt=0)
    audio_stream_index: int | None = Field(default=None, ge=0)
    metadata: dict[str, Any] = Field(default_factory=dict)
    ingest: IngestMetadata = Field(default_factory=IngestMetadata)

    @field_validator("fps", "duration_seconds")
    @classmethod
    def finite_positive_numbers(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("value must be finite")
        return value

    @field_validator("source_hashes")
    @classmethod
    def normalize_source_hashes(cls, values: list[str]) -> list[str]:
        return _normalize_sha256_values(values)

    @model_validator(mode="after")
    def validate_asset(self) -> "SphericalAsset":
        if len(self.source_paths) != len(self.source_hashes):
            raise ValueError("source_paths and source_hashes must have equal length")
        if not math.isclose(self.width / self.height, 2.0, rel_tol=0.02):
            raise ValueError("equirectangular assets must be approximately 2:1")
        return self


class CameraKeyframe(VersionedSphericalModel):
    time_seconds: float = Field(ge=0)
    yaw: float = 0.0
    pitch: float = Field(default=0.0, ge=-90.0, le=90.0)
    roll: float = 0.0
    horizontal_fov: float = Field(default=90.0, gt=1.0, lt=179.0)
    easing: Easing = Easing.LINEAR
    subject_id: str | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)

    @field_validator("time_seconds", "pitch", "horizontal_fov")
    @classmethod
    def finite_numbers(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("value must be finite")
        return value

    @field_validator("yaw", "roll", mode="before")
    @classmethod
    def normalize_angles(cls, value: Any) -> float:
        return _normalize_wrapped_angle(float(value))


class VirtualCameraTrack(VersionedSphericalModel):
    track_id: str = Field(min_length=1)
    asset_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    output_width: int = Field(gt=0)
    output_height: int = Field(gt=0)
    source_start_seconds: float = Field(ge=0)
    source_end_seconds: float = Field(gt=0)
    keyframes: list[CameraKeyframe] = Field(min_length=1)
    created_by: TrackCreatedBy = TrackCreatedBy.MANUAL
    generation_metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("source_start_seconds", "source_end_seconds")
    @classmethod
    def finite_times(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("time must be finite")
        return value

    @model_validator(mode="after")
    def validate_track(self) -> "VirtualCameraTrack":
        if self.source_end_seconds <= self.source_start_seconds:
            raise ValueError("source_end_seconds must be greater than source_start_seconds")
        if self.output_width % 2 or self.output_height % 2:
            raise ValueError("H.264 output dimensions must be even")
        if _supported_aspect_ratio(self.output_width, self.output_height) is None:
            raise ValueError("output must use a supported 16:9, 9:16, or 1:1 aspect ratio")

        previous_time: float | None = None
        for keyframe in self.keyframes:
            if not (
                self.source_start_seconds
                <= keyframe.time_seconds
                <= self.source_end_seconds
            ):
                raise ValueError("keyframes must be inside the source interval")
            if previous_time is not None and keyframe.time_seconds <= previous_time:
                raise ValueError("keyframe timestamps must be strictly increasing")
            previous_time = keyframe.time_seconds
        return self


class CodecSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    video_codec: Literal["libx264"] = "libx264"
    preset: Literal[
        "ultrafast",
        "superfast",
        "veryfast",
        "faster",
        "fast",
        "medium",
        "slow",
        "slower",
        "veryslow",
    ] = "medium"
    crf: int = Field(default=18, ge=0, le=51)
    pixel_format: Literal["yuv420p"] = "yuv420p"
    audio_codec: Literal["aac"] = "aac"
    audio_bitrate: str = Field(default="192k", pattern=r"^[1-9][0-9]*k$")


class ReframeRenderRequest(VersionedSphericalModel):
    asset: SphericalAsset
    track: VirtualCameraTrack
    output_path: Path
    workspace_root: Path
    cache_dir: Path
    codec_settings: CodecSettings = Field(default_factory=CodecSettings)
    sample_interval_seconds: float = Field(default=0.25, ge=0.05, le=5.0)
    use_cache: bool = True
    overwrite: bool = False

    @model_validator(mode="after")
    def validate_request(self) -> "ReframeRenderRequest":
        if self.asset.asset_id != self.track.asset_id:
            raise ValueError("track asset_id does not match the supplied asset")
        if self.track.source_end_seconds > self.asset.duration_seconds + 1e-6:
            raise ValueError("track source interval exceeds asset duration")

        workspace = self.workspace_root.expanduser().resolve(strict=False)
        output = self.output_path.expanduser()
        if not output.is_absolute():
            output = workspace / output
        output = output.resolve(strict=False)
        try:
            output.relative_to(workspace)
        except ValueError as exc:
            raise ValueError("output_path must remain inside workspace_root") from exc
        if output.suffix.lower() != ".mp4":
            raise ValueError("output_path must use the .mp4 extension")

        cache = self.cache_dir.expanduser()
        if not cache.is_absolute():
            cache = workspace / cache

        # Bypass assignment validation while finalizing canonical paths; assigning
        # normally from an `after` validator would recursively re-enter it.
        object.__setattr__(self, "workspace_root", workspace)
        object.__setattr__(self, "output_path", output)
        object.__setattr__(self, "cache_dir", cache.resolve(strict=False))
        return self


class ReframeRenderManifest(VersionedSphericalModel):
    render_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    asset_id: str
    track_id: str
    source_hashes: list[str] = Field(min_length=1)
    camera_track_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    ffmpeg_version: str
    ffmpeg_capabilities: dict[str, Any]
    exact_filter_graphs: list[str]
    exact_commands: list[list[str]]
    output_path: Path
    output_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    render_duration_seconds: float = Field(ge=0)
    source_start_seconds: float = Field(ge=0)
    source_end_seconds: float = Field(gt=0)
    output_width: int = Field(gt=0)
    output_height: int = Field(gt=0)
    output_fps: float = Field(gt=0)
    codec_settings: CodecSettings
    hardware_acceleration: dict[str, Any]
    selected_backend: str
    renderer_version: str
    application_version: str
    warnings: list[str] = Field(default_factory=list)
    fallback_decisions: list[str] = Field(default_factory=list)
    audio_preserved: bool
    cache_hit: bool = False
    creation_timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    @field_validator("source_hashes")
    @classmethod
    def normalize_source_hashes(cls, values: list[str]) -> list[str]:
        return _normalize_sha256_values(values)


SCHEMA_MODELS: dict[str, type[VersionedSphericalModel]] = {
    "spherical-asset": SphericalAsset,
    "camera-keyframe": CameraKeyframe,
    "virtual-camera-track": VirtualCameraTrack,
    "reframe-render-request": ReframeRenderRequest,
    "reframe-render-manifest": ReframeRenderManifest,
}
