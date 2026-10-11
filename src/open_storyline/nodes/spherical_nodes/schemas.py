"""MCP-facing input contracts for deterministic spherical nodes."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from open_storyline.spherical.schemas import (
    CameraKeyframe,
    TrackCreatedBy,
    VirtualCameraTrack,
)
from open_storyline.spherical.direction_schemas import MotionConstraints


class SphericalNodeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["auto", "skip", "default"] = Field(
        default="auto",
        description="Spherical nodes execute deterministically; all modes use validated inputs.",
    )


class SphericalMediaInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(
        min_length=1,
        description=(
            "Stitched 2:1 equirectangular MP4 path. Raw .insv must first pass through "
            "a configured MediaSDK worker or Insta360 Studio."
        ),
    )
    base64: str | None = Field(default=None, exclude=True)
    md5: str | None = None
    orig_path: str | None = None
    orig_md5: str | None = None


class ImportSphericalMediaInput(SphericalNodeInput):
    inputs: list[SphericalMediaInput] = Field(
        min_length=1,
        max_length=1,
        description="Exactly one stitched equirectangular MP4 to import.",
    )
    generate_proxy: bool = Field(
        default=True,
        description="Generate a cached lower-resolution 2:1 inspection proxy.",
    )
    proxy_width: int | None = Field(
        default=None,
        ge=320,
        le=4096,
        description="Optional even proxy width; defaults to spherical.proxy_width.",
    )

    @model_validator(mode="after")
    def validate_proxy_width(self) -> "ImportSphericalMediaInput":
        if self.proxy_width is not None and self.proxy_width % 2:
            raise ValueError("proxy_width must be even")
        return self


class InspectSphericalMediaInput(SphericalNodeInput):
    asset_id: str | None = Field(
        default=None,
        description="Asset ID; defaults to the latest spherical asset in this session.",
    )
    sample_count: int = Field(default=4, ge=1, le=12)
    thumbnail_width: int | None = Field(default=None, ge=160, le=1024)
    include_perspective: bool = True
    perspective_time_seconds: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_thumbnail_width(self) -> "InspectSphericalMediaInput":
        if self.thumbnail_width is not None and self.thumbnail_width % 2:
            raise ValueError("thumbnail_width must be even")
        return self


class CreateVirtualCameraTrackInput(SphericalNodeInput):
    asset_id: str | None = Field(
        default=None,
        description="Asset ID; defaults to the latest imported spherical asset.",
    )
    track_id: str | None = Field(
        default=None,
        description="Optional stable ID; one is generated when omitted.",
    )
    name: str = Field(default="manual-camera", min_length=1)
    output_width: int = Field(default=1920, gt=0)
    output_height: int = Field(default=1080, gt=0)
    source_start_seconds: float = Field(default=0.0, ge=0)
    source_end_seconds: float | None = Field(
        default=None,
        gt=0,
        description="Defaults to the spherical asset duration.",
    )
    keyframes: list[CameraKeyframe] = Field(
        min_length=1,
        description=(
            "Explicit camera states. Yaw: 0 front, 90 right, 180 back, -90 left. "
            "Pitch is -90..90 and horizontal_fov must be 1..179."
        ),
    )
    created_by: TrackCreatedBy = TrackCreatedBy.MANUAL
    generation_metadata: dict[str, Any] = Field(default_factory=dict)


class UpdateVirtualCameraTrackInput(SphericalNodeInput):
    track_id: str = Field(min_length=1)
    name: str | None = Field(default=None, min_length=1)
    output_width: int | None = Field(default=None, gt=0)
    output_height: int | None = Field(default=None, gt=0)
    source_start_seconds: float | None = Field(default=None, ge=0)
    source_end_seconds: float | None = Field(default=None, gt=0)
    keyframes: list[CameraKeyframe] | None = Field(default=None, min_length=1)
    generation_metadata: dict[str, Any] | None = None


class ValidateVirtualCameraTrackInput(SphericalNodeInput):
    track_id: str | None = Field(default=None, min_length=1)
    track: VirtualCameraTrack | None = None

    @model_validator(mode="after")
    def require_one_track_source(self) -> "ValidateVirtualCameraTrackInput":
        if (self.track_id is None) == (self.track is None):
            raise ValueError("provide exactly one of track_id or track")
        return self


class PreviewVirtualCameraTrackInput(SphericalNodeInput):
    track_id: str = Field(min_length=1)
    max_dimension: int | None = Field(default=None, ge=160, le=1920)
    sample_interval_seconds: float | None = Field(default=None, ge=0.05, le=5.0)
    use_cache: bool = True


class RenderReframedClipInput(SphericalNodeInput):
    track_id: str = Field(min_length=1)
    output_filename: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*\.mp4$",
        description="Safe MP4 basename; defaults to <track_id>.mp4.",
    )
    sample_interval_seconds: float | None = Field(default=None, ge=0.05, le=5.0)
    use_cache: bool = True


class ListVirtualCameraTracksInput(SphericalNodeInput):
    asset_id: str | None = None
    include_deleted: bool = False


class DeleteVirtualCameraTrackInput(SphericalNodeInput):
    track_id: str = Field(min_length=1)


class AnalyzeSphericalAssetInput(SphericalNodeInput):
    asset_id: str | None = Field(
        default=None,
        description="Spherical asset ID; defaults to the latest session asset.",
    )
    interval_seconds: float | None = Field(default=None, ge=0.1, le=10.0)
    max_frames: int | None = Field(default=None, ge=1, le=600)
    yaw_step_degrees: Literal[30, 45, 60, 90, 120] | None = None
    pitch_centers: list[float] | None = Field(default=None, min_length=1, max_length=7)
    horizontal_fov: float | None = Field(default=None, gt=1.0, lt=179.0)
    view_width: int | None = Field(default=None, ge=96, le=1024)
    view_height: int | None = Field(default=None, ge=96, le=1024)
    minimum_detection_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    dedup_angular_threshold_degrees: float | None = Field(default=None, gt=0.0, le=45.0)
    max_track_angular_distance_degrees: float | None = Field(default=None, gt=0.0, le=180.0)
    max_occlusion_seconds: float | None = Field(default=None, ge=0.0, le=30.0)

    @model_validator(mode="after")
    def validate_analysis_grid(self) -> "AnalyzeSphericalAssetInput":
        for field_name in ("view_width", "view_height"):
            value = getattr(self, field_name)
            if value is not None and value % 2:
                raise ValueError(f"{field_name} must be even")
        if self.pitch_centers is not None:
            if any(not -90.0 <= value <= 90.0 for value in self.pitch_centers):
                raise ValueError("pitch_centers must be inside -90..90")
            if len(set(self.pitch_centers)) != len(self.pitch_centers):
                raise ValueError("pitch_centers must be unique")
        return self


class ListDetectedSubjectsInput(SphericalNodeInput):
    analysis_id: str | None = None
    minimum_confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class InspectSubjectTrackInput(SphericalNodeInput):
    subject_id: str = Field(min_length=1)
    analysis_id: str | None = None


class SuggestSubjectReframeInput(SphericalNodeInput):
    analysis_id: str | None = None
    subject_id: str | None = None
    policy: Literal["subject_center", "rule_of_thirds", "largest_person"] = (
        "subject_center"
    )
    placement: Literal["center", "left_third", "right_third"] = "center"
    output_width: int = Field(default=1920, gt=0)
    output_height: int = Field(default=1080, gt=0)
    horizontal_fov: float | None = Field(default=None, gt=1.0, lt=179.0)
    minimum_fov: float = Field(default=55.0, gt=1.0, lt=179.0)
    maximum_fov: float = Field(default=120.0, gt=1.0, lt=179.0)
    avoid_nadir_below_pitch: float = Field(default=-60.0, ge=-90.0, le=90.0)
    stitch_seam_yaws: list[float] = Field(default_factory=lambda: [180.0], max_length=8)
    stitch_seam_zone_width: float = Field(default=12.0, ge=0.0, le=90.0)

    @model_validator(mode="after")
    def validate_subject_suggestion(self) -> "SuggestSubjectReframeInput":
        if self.policy != "largest_person" and self.subject_id is None:
            raise ValueError("subject_id is required for selected-subject policies")
        if self.minimum_fov > self.maximum_fov:
            raise ValueError("minimum_fov cannot exceed maximum_fov")
        return self


class SuggestGroupReframeInput(SphericalNodeInput):
    analysis_id: str | None = None
    subject_ids: list[str] = Field(default_factory=list, max_length=32)
    output_width: int = Field(default=1920, gt=0)
    output_height: int = Field(default=1080, gt=0)
    minimum_fov: float = Field(default=70.0, gt=1.0, lt=179.0)
    maximum_fov: float = Field(default=140.0, gt=1.0, lt=179.0)
    avoid_nadir_below_pitch: float = Field(default=-60.0, ge=-90.0, le=90.0)
    stitch_seam_yaws: list[float] = Field(default_factory=lambda: [180.0], max_length=8)
    stitch_seam_zone_width: float = Field(default=12.0, ge=0.0, le=90.0)

    @model_validator(mode="after")
    def validate_group_suggestion(self) -> "SuggestGroupReframeInput":
        if len(set(self.subject_ids)) != len(self.subject_ids):
            raise ValueError("subject_ids must be unique")
        if self.minimum_fov > self.maximum_fov:
            raise ValueError("minimum_fov cannot exceed maximum_fov")
        return self


class SuggestEstablishingShotInput(SphericalNodeInput):
    analysis_id: str | None = None
    output_width: int = Field(default=1920, gt=0)
    output_height: int = Field(default=1080, gt=0)
    horizontal_fov: float = Field(default=120.0, gt=1.0, lt=179.0)
    avoid_nadir_below_pitch: float = Field(default=-60.0, ge=-90.0, le=90.0)
    stitch_seam_yaws: list[float] = Field(default_factory=lambda: [180.0], max_length=8)
    stitch_seam_zone_width: float = Field(default=12.0, ge=0.0, le=90.0)


class PreviewReframeSuggestionInput(SphericalNodeInput):
    suggestion_id: str = Field(min_length=1)
    max_dimension: int | None = Field(default=None, ge=160, le=1920)
    sample_interval_seconds: float | None = Field(default=None, ge=0.05, le=5.0)
    use_cache: bool = True


class AcceptReframeSuggestionInput(SphericalNodeInput):
    suggestion_id: str = Field(min_length=1)


class RejectReframeSuggestionInput(SphericalNodeInput):
    suggestion_id: str = Field(min_length=1)
    reason: str | None = Field(default=None, max_length=1000)


class ListDirectorProfilesInput(SphericalNodeInput):
    profile_id: str | None = Field(
        default=None,
        description="Optional profile ID; omit to list all built-in profiles.",
    )


class BuildSphericalStoryTimelineInput(SphericalNodeInput):
    analysis_id: str | None = None
    asr_infos: list[dict[str, Any]] = Field(
        default_factory=list,
        max_length=1000,
        description=(
            "Existing local_asr output items. Timestamps remain source-relative "
            "milliseconds and are normalized here; ASR is not rerun."
        ),
    )
    split_shots: list[dict[str, Any]] = Field(default_factory=list, max_length=1000)
    clip_captions: list[dict[str, Any]] = Field(default_factory=list, max_length=1000)
    group_scripts: list[dict[str, Any]] = Field(default_factory=list, max_length=500)
    timeline_tracks: dict[str, Any] = Field(default_factory=dict)
    mouth_activity: list[dict[str, Any]] = Field(default_factory=list, max_length=5000)
    audio_activity: list[dict[str, Any]] = Field(default_factory=list, max_length=5000)


class AnalyzeSpeakerCandidatesInput(SphericalNodeInput):
    timeline_id: str | None = None
    probable_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    ambiguity_margin: float = Field(default=0.12, ge=0.0, le=1.0)
    profile_id: str = Field(default="round-table-podcast", min_length=1)


class GenerateDirectorProposalsInput(SphericalNodeInput):
    timeline_id: str | None = None
    speaker_analysis_id: str | None = None
    profile_id: str = Field(default="round-table-podcast", min_length=1)
    variants: list[
        Literal[
            "speaker-focused",
            "cinematic",
            "minimal-motion",
            "high-energy",
            "vertical-social",
        ]
    ] = Field(
        default_factory=lambda: ["speaker-focused", "minimal-motion"],
        min_length=2,
        max_length=5,
    )
    output_width: int = Field(default=1920, gt=0)
    output_height: int = Field(default=1080, gt=0)
    vertical_output_width: int = Field(default=1080, gt=0)
    vertical_output_height: int = Field(default=1920, gt=0)
    motion_constraints: MotionConstraints | None = None

    @model_validator(mode="after")
    def validate_director_outputs(self) -> "GenerateDirectorProposalsInput":
        if len(set(self.variants)) != len(self.variants):
            raise ValueError("director variants must be unique")
        for width, height in (
            (self.output_width, self.output_height),
            (self.vertical_output_width, self.vertical_output_height),
        ):
            if width % 2 or height % 2:
                raise ValueError("director output dimensions must be even")
        return self


class CompareDirectorProposalsInput(SphericalNodeInput):
    proposal_ids: list[str] = Field(min_length=2, max_length=10)

    @model_validator(mode="after")
    def validate_unique_proposals(self) -> "CompareDirectorProposalsInput":
        if len(set(self.proposal_ids)) != len(self.proposal_ids):
            raise ValueError("proposal_ids must be unique")
        return self


class PreviewDirectorProposalInput(SphericalNodeInput):
    proposal_id: str = Field(min_length=1)
    max_dimension: int | None = Field(default=None, ge=160, le=1920)
    sample_interval_seconds: float | None = Field(default=None, ge=0.05, le=5.0)
    use_cache: bool = True


class AcceptDirectorProposalInput(SphericalNodeInput):
    proposal_id: str = Field(min_length=1)


class RejectDirectorProposalInput(SphericalNodeInput):
    proposal_id: str = Field(min_length=1)
    reason: str | None = Field(default=None, max_length=1000)


class RenderDirectorProposalInput(SphericalNodeInput):
    proposal_id: str = Field(min_length=1)
    output_filename: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*\.mp4$",
        description="Safe flat MP4 basename; defaults to a proposal-derived name.",
    )
    sample_interval_seconds: float | None = Field(default=None, ge=0.05, le=5.0)
    use_cache: bool = True


def safe_output_filename(value: str | None, track_id: str) -> str:
    from open_storyline.spherical.manifests import sha256_json

    candidate = value or f"reframed-{sha256_json(track_id)[:12]}.mp4"
    if Path(candidate).name != candidate or not candidate.lower().endswith(".mp4"):
        raise ValueError("output_filename must be a safe MP4 basename")
    return candidate
