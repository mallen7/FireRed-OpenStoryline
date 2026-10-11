"""Strict contracts for spherical detections, tracks, and camera suggestions."""

from __future__ import annotations

import math
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from open_storyline.spherical.schemas import (
    SHA256_PATTERN,
    TrackCreatedBy,
    VersionedSphericalModel,
    VirtualCameraTrack,
)


class DetectionKind(str, Enum):
    OBJECT = "object"
    PERSON = "person"
    FACE = "face"


class VisibilityState(str, Enum):
    VISIBLE = "visible"
    OCCLUDED = "occluded"
    LOST = "lost"


class SuggestionPolicy(str, Enum):
    SUBJECT_CENTER = "subject_center"
    RULE_OF_THIRDS = "rule_of_thirds"
    LARGEST_PERSON = "largest_person"
    WIDE_ESTABLISHING = "wide_establishing"
    PAN_SUBJECTS = "pan_subjects"
    STATIC_DIRECTION = "static_direction"
    GROUP = "group"


class SuggestionStatus(str, Enum):
    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class NormalizedBoundingBox(BaseModel):
    model_config = ConfigDict(extra="forbid")

    x_min: float = Field(ge=0.0, le=1.0)
    y_min: float = Field(ge=0.0, le=1.0)
    x_max: float = Field(ge=0.0, le=1.0)
    y_max: float = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def validate_extent(self) -> "NormalizedBoundingBox":
        if self.x_max <= self.x_min or self.y_max <= self.y_min:
            raise ValueError("bounding box must have positive width and height")
        return self

    @property
    def center(self) -> tuple[float, float]:
        return ((self.x_min + self.x_max) / 2, (self.y_min + self.y_max) / 2)


class PerspectiveViewport(VersionedSphericalModel):
    viewport_id: str = Field(min_length=1)
    center_yaw: float
    center_pitch: float = Field(ge=-90.0, le=90.0)
    horizontal_fov: float = Field(gt=1.0, lt=179.0)
    vertical_fov: float = Field(gt=1.0, lt=179.0)
    width: int = Field(gt=0, multiple_of=2)
    height: int = Field(gt=0, multiple_of=2)

    @field_validator("center_yaw", mode="before")
    @classmethod
    def normalize_yaw(cls, value: Any) -> float:
        from open_storyline.spherical.geometry import normalize_angle

        return normalize_angle(float(value))


class PerspectiveFrame(VersionedSphericalModel):
    timestamp_seconds: float = Field(ge=0)
    viewport: PerspectiveViewport
    image_path: Path
    image_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ViewDetection(VersionedSphericalModel):
    detection_id: str = Field(min_length=1)
    timestamp_seconds: float = Field(ge=0)
    viewport_id: str = Field(min_length=1)
    bounding_box: NormalizedBoundingBox
    kind: DetectionKind
    label: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    detector_backend: str = Field(min_length=1)
    embedding: list[float] = Field(default_factory=list, max_length=512)
    identity_evidence: dict[str, Any] = Field(default_factory=dict)

    @field_validator("embedding")
    @classmethod
    def finite_embedding(cls, values: list[float]) -> list[float]:
        if any(not math.isfinite(value) for value in values):
            raise ValueError("embedding values must be finite")
        return values


class SphericalDetection(VersionedSphericalModel):
    detection_id: str = Field(min_length=1)
    timestamp_seconds: float = Field(ge=0)
    yaw: float
    pitch: float = Field(ge=-90.0, le=90.0)
    angular_width: float = Field(gt=0.0, le=180.0)
    angular_height: float = Field(gt=0.0, le=180.0)
    confidence: float = Field(ge=0.0, le=1.0)
    kinds: list[DetectionKind] = Field(min_length=1)
    labels: list[str] = Field(min_length=1)
    source_viewports: list[str] = Field(min_length=1)
    detector_backends: list[str] = Field(min_length=1)
    embedding: list[float] = Field(default_factory=list, max_length=512)
    identity_evidence: dict[str, Any] = Field(default_factory=dict)

    @field_validator("yaw", mode="before")
    @classmethod
    def normalize_yaw(cls, value: Any) -> float:
        from open_storyline.spherical.geometry import normalize_angle

        return normalize_angle(float(value))


class OcclusionGap(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start_seconds: float = Field(ge=0)
    end_seconds: float = Field(ge=0)
    duration_seconds: float = Field(ge=0)

    @model_validator(mode="after")
    def validate_gap(self) -> "OcclusionGap":
        if self.end_seconds < self.start_seconds:
            raise ValueError("occlusion gap end precedes start")
        if not math.isclose(
            self.duration_seconds,
            self.end_seconds - self.start_seconds,
            abs_tol=1e-6,
        ):
            raise ValueError("occlusion duration does not match its interval")
        return self


class SeamCrossingEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timestamp_seconds: float = Field(ge=0)
    from_yaw: float
    to_yaw: float


class SubjectSample(VersionedSphericalModel):
    timestamp_seconds: float = Field(ge=0)
    yaw: float
    pitch: float = Field(ge=-90.0, le=90.0)
    angular_width: float = Field(gt=0.0, le=180.0)
    angular_height: float = Field(gt=0.0, le=180.0)
    confidence: float = Field(ge=0.0, le=1.0)
    visibility: VisibilityState
    source_viewports: list[str] = Field(default_factory=list)
    identity_evidence: dict[str, Any] = Field(default_factory=dict)

    @field_validator("yaw", mode="before")
    @classmethod
    def normalize_yaw(cls, value: Any) -> float:
        from open_storyline.spherical.geometry import normalize_angle

        return normalize_angle(float(value))


class SubjectTrack(VersionedSphericalModel):
    subject_id: str = Field(min_length=1)
    asset_id: str = Field(min_length=1)
    start_seconds: float = Field(ge=0)
    end_seconds: float = Field(ge=0)
    samples: list[SubjectSample] = Field(min_length=1)
    mean_confidence: float = Field(ge=0.0, le=1.0)
    identity_evidence: dict[str, Any] = Field(default_factory=dict)
    occlusion_gaps: list[OcclusionGap] = Field(default_factory=list)
    seam_crossing_events: list[SeamCrossingEvent] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_samples(self) -> "SubjectTrack":
        if self.end_seconds < self.start_seconds:
            raise ValueError("subject track end precedes start")
        times = [sample.timestamp_seconds for sample in self.samples]
        if times != sorted(times) or len(times) != len(set(times)):
            raise ValueError("subject samples must have unique ordered timestamps")
        if times[0] < self.start_seconds or times[-1] > self.end_seconds:
            raise ValueError("subject samples must remain inside the track interval")
        return self


class AnalysisBackendInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    object_detector: str
    person_detector: str
    face_detector: str
    subject_tracker: str
    shot_boundary_detector: str
    visual_embedder: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    optional_dependencies: list[str] = Field(default_factory=list)


class AnalysisReviewArtifacts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    detection_overlay_clip_path: Path
    detection_contact_sheet_path: Path
    subject_trajectory_json_path: Path
    analysis_manifest_path: Path
    hashes: dict[str, str]

    @field_validator("hashes")
    @classmethod
    def validate_hashes(cls, values: dict[str, str]) -> dict[str, str]:
        if any(not SHA256_PATTERN.fullmatch(value) for value in values.values()):
            raise ValueError("review artifact hashes must be SHA-256 values")
        required = {
            "detection_overlay_clip",
            "detection_contact_sheet",
            "subject_trajectory_json",
        }
        if set(values) != required:
            raise ValueError("review artifact hashes must contain every required output")
        return values


class SphericalAnalysis(VersionedSphericalModel):
    analysis_id: str = Field(min_length=1)
    asset_id: str = Field(min_length=1)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    cache_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    cache_hit: bool = False
    sample_timestamps: list[float] = Field(min_length=1)
    perspective_frames: list[PerspectiveFrame] = Field(min_length=1)
    spherical_detections: list[SphericalDetection]
    subject_tracks: list[SubjectTrack]
    shot_boundaries_seconds: list[float] = Field(default_factory=list)
    backend: AnalysisBackendInfo
    confidence_summary: dict[str, float]
    review_artifacts: AnalysisReviewArtifacts
    warnings: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ReframeSuggestion(VersionedSphericalModel):
    suggestion_id: str = Field(min_length=1)
    analysis_id: str = Field(min_length=1)
    asset_id: str = Field(min_length=1)
    policy: SuggestionPolicy
    status: SuggestionStatus = SuggestionStatus.PROPOSED
    subject_ids: list[str] = Field(default_factory=list)
    camera_track: VirtualCameraTrack
    analysis_backend: AnalysisBackendInfo
    confidence: float = Field(ge=0.0, le=1.0)
    parameters: dict[str, Any] = Field(default_factory=dict)
    camera_trajectory_path: Path
    warnings: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="after")
    def validate_identity(self) -> "ReframeSuggestion":
        if self.camera_track.asset_id != self.asset_id:
            raise ValueError("suggestion camera track asset does not match suggestion")
        if self.camera_track.created_by != TrackCreatedBy.ASSISTED:
            raise ValueError("suggestion camera track must be marked assisted")
        return self


ANALYSIS_SCHEMA_MODELS: dict[str, type[VersionedSphericalModel]] = {
    "perspective-viewport": PerspectiveViewport,
    "perspective-frame": PerspectiveFrame,
    "view-detection": ViewDetection,
    "spherical-detection": SphericalDetection,
    "subject-sample": SubjectSample,
    "subject-track": SubjectTrack,
    "spherical-analysis": SphericalAnalysis,
    "reframe-suggestion": ReframeSuggestion,
}
