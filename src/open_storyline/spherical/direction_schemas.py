"""Versioned contracts for storyline-aware spherical direction."""

from __future__ import annotations

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


class TimelineSignalKind(str, Enum):
    TRANSCRIPT = "transcript"
    WORD = "word"
    SCENE = "scene"
    SUBJECT = "subject"
    FACE_ACTIVITY = "face_activity"
    MOUTH_ACTIVITY = "mouth_activity"
    AUDIO_ACTIVITY = "audio_activity"
    CLIP_UNDERSTANDING = "clip_understanding"
    SCRIPT = "script"
    TIMELINE_PLAN = "timeline_plan"


class SpeakerAssessmentStatus(str, Enum):
    PROBABLE = "probable"
    AMBIGUOUS = "ambiguous"
    UNRESOLVED = "unresolved"


class DirectorVariant(str, Enum):
    SPEAKER_FOCUSED = "speaker-focused"
    CINEMATIC = "cinematic"
    MINIMAL_MOTION = "minimal-motion"
    HIGH_ENERGY = "high-energy"
    VERTICAL_SOCIAL = "vertical-social"


class DirectorProposalStatus(str, Enum):
    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class ShotSize(str, Enum):
    CLOSE = "close"
    MEDIUM = "medium"
    WIDE = "wide"


class DirectorTransition(str, Enum):
    CUT = "cut"
    PAN = "pan"
    HOLD = "hold"


class DirectorIntent(str, Enum):
    SPEAKER = "speaker"
    REACTION = "reaction"
    ESTABLISHING = "establishing"
    HOLD = "hold"
    NO_RELIABLE_SUBJECT = "no_reliable_subject"


class VerticalOutputBehavior(str, Enum):
    CENTER_CROP = "center_crop"
    ACTIVE_SUBJECT = "active_subject"
    GROUP_WHEN_UNCERTAIN = "group_when_uncertain"


class TimelineReferenceKind(str, Enum):
    FLAT_SOURCE = "flat_source"
    SPHERICAL_APPROVED_TRACK = "spherical_approved_track"
    GENERATED_REFRAME_ARTIFACT = "generated_reframe_artifact"


class TranscriptWord(VersionedSphericalModel):
    word_id: str = Field(min_length=1)
    start_seconds: float = Field(ge=0)
    end_seconds: float = Field(gt=0)
    text: str = Field(min_length=1)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    clip_id: str | None = None

    @model_validator(mode="after")
    def validate_interval(self) -> "TranscriptWord":
        if self.end_seconds <= self.start_seconds:
            raise ValueError("word end must follow word start")
        return self


class TranscriptTurn(VersionedSphericalModel):
    turn_id: str = Field(min_length=1)
    start_seconds: float = Field(ge=0)
    end_seconds: float = Field(gt=0)
    text: str = Field(min_length=1)
    word_ids: list[str] = Field(default_factory=list)
    clip_id: str | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def validate_interval(self) -> "TranscriptTurn":
        if self.end_seconds <= self.start_seconds:
            raise ValueError("transcript turn end must follow its start")
        return self


class TimelineSignal(VersionedSphericalModel):
    signal_id: str = Field(min_length=1)
    kind: TimelineSignalKind
    start_seconds: float = Field(ge=0)
    end_seconds: float = Field(ge=0)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    subject_id: str | None = None
    clip_id: str | None = None
    text: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_interval(self) -> "TimelineSignal":
        if self.end_seconds < self.start_seconds:
            raise ValueError("timeline signal end precedes its start")
        return self


class NormalizedStoryTimeline(VersionedSphericalModel):
    timeline_id: str = Field(min_length=1)
    analysis_id: str = Field(min_length=1)
    asset_id: str = Field(min_length=1)
    duration_seconds: float = Field(gt=0)
    transcript_turns: list[TranscriptTurn] = Field(default_factory=list)
    words: list[TranscriptWord] = Field(default_factory=list)
    scene_boundaries_seconds: list[float] = Field(default_factory=list)
    signals: list[TimelineSignal] = Field(default_factory=list)
    source_summary: dict[str, int] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="after")
    def validate_timeline(self) -> "NormalizedStoryTimeline":
        for items, label in (
            (self.transcript_turns, "transcript turns"),
            (self.words, "words"),
            (self.signals, "signals"),
        ):
            starts = [item.start_seconds for item in items]
            if starts != sorted(starts):
                raise ValueError(f"{label} must be ordered by start time")
            if any(item.end_seconds > self.duration_seconds + 1e-6 for item in items):
                raise ValueError(f"{label} exceed the asset duration")
        if self.scene_boundaries_seconds != sorted(set(self.scene_boundaries_seconds)):
            raise ValueError("scene boundaries must be sorted and unique")
        if any(
            boundary < 0 or boundary > self.duration_seconds
            for boundary in self.scene_boundaries_seconds
        ):
            raise ValueError("scene boundary is outside the asset duration")
        for identifiers, label in (
            ([turn.turn_id for turn in self.transcript_turns], "turn IDs"),
            ([word.word_id for word in self.words], "word IDs"),
            ([signal.signal_id for signal in self.signals], "signal IDs"),
        ):
            if len(identifiers) != len(set(identifiers)):
                raise ValueError(f"{label} must be unique")
        known_words = {word.word_id for word in self.words}
        if any(
            word_id not in known_words
            for turn in self.transcript_turns
            for word_id in turn.word_ids
        ):
            raise ValueError("transcript turn references an unknown word ID")
        return self


class SpeakerScoreComponents(BaseModel):
    model_config = ConfigDict(extra="forbid")

    voice_activity: float = Field(ge=0.0, le=1.0)
    visible_face_activity: float = Field(ge=0.0, le=1.0)
    mouth_movement: float = Field(ge=0.0, le=1.0)
    subject_continuity: float = Field(ge=0.0, le=1.0)
    scene_context: float = Field(ge=0.0, le=1.0)
    previous_speaker: float = Field(ge=0.0, le=1.0)
    transcript_turn_boundary: float = Field(ge=0.0, le=1.0)
    track_confidence: float = Field(ge=0.0, le=1.0)


class SpeakerCandidateScore(VersionedSphericalModel):
    subject_id: str = Field(min_length=1)
    total_score: float = Field(ge=0.0, le=1.0)
    components: SpeakerScoreComponents
    evidence: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class SpeakerTurnAssessment(VersionedSphericalModel):
    turn_id: str = Field(min_length=1)
    start_seconds: float = Field(ge=0)
    end_seconds: float = Field(gt=0)
    text: str = Field(min_length=1)
    status: SpeakerAssessmentStatus
    selected_subject_id: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    candidates: list[SpeakerCandidateScore] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_assessment(self) -> "SpeakerTurnAssessment":
        if self.end_seconds <= self.start_seconds:
            raise ValueError("speaker assessment end must follow start")
        scores = [candidate.total_score for candidate in self.candidates]
        if scores != sorted(scores, reverse=True):
            raise ValueError("speaker candidates must be ordered by descending score")
        if self.status == SpeakerAssessmentStatus.PROBABLE:
            if not self.selected_subject_id:
                raise ValueError("probable speaker assessment requires a subject")
            if self.selected_subject_id not in {
                candidate.subject_id for candidate in self.candidates
            }:
                raise ValueError("selected speaker must be one of the candidates")
        elif self.selected_subject_id is not None:
            raise ValueError("ambiguous and unresolved turns cannot select a speaker")
        return self


class SpeakerCandidateAnalysis(VersionedSphericalModel):
    speaker_analysis_id: str = Field(min_length=1)
    timeline_id: str = Field(min_length=1)
    analysis_id: str = Field(min_length=1)
    asset_id: str = Field(min_length=1)
    profile_id: str = Field(min_length=1)
    turns: list[SpeakerTurnAssessment] = Field(default_factory=list)
    probable_threshold: float = Field(ge=0.0, le=1.0)
    ambiguity_margin: float = Field(ge=0.0, le=1.0)
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_turns(self) -> "SpeakerCandidateAnalysis":
        starts = [turn.start_seconds for turn in self.turns]
        if starts != sorted(starts):
            raise ValueError("speaker assessments must be ordered")
        identifiers = [turn.turn_id for turn in self.turns]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("speaker assessment turn IDs must be unique")
        return self


class MotionConstraints(VersionedSphericalModel):
    maximum_pan_speed_degrees_per_second: float = Field(gt=0)
    maximum_acceleration_degrees_per_second_squared: float = Field(gt=0)
    minimum_shot_duration_seconds: float = Field(gt=0)
    maximum_shot_duration_seconds: float = Field(gt=0)
    minimum_time_between_cuts_seconds: float = Field(ge=0)
    maximum_fov_change_degrees_per_second: float = Field(gt=0)
    subject_dead_zone_degrees: float = Field(ge=0)
    maximum_low_confidence_tracking_seconds: float = Field(ge=0)

    @model_validator(mode="after")
    def validate_shot_lengths(self) -> "MotionConstraints":
        if self.maximum_shot_duration_seconds < self.minimum_shot_duration_seconds:
            raise ValueError("maximum shot duration cannot be shorter than minimum")
        return self


class DirectorScoreComponents(BaseModel):
    model_config = ConfigDict(extra="forbid")

    semantic_relevance: float = Field(ge=0.0, le=1.0)
    speaker_likelihood: float = Field(ge=0.0, le=1.0)
    visual_salience: float = Field(ge=0.0, le=1.0)
    composition_quality: float = Field(ge=0.0, le=1.0)
    continuity: float = Field(ge=0.0, le=1.0)
    motion_cost: float = Field(ge=0.0, le=1.0)
    cut_cost: float = Field(ge=0.0, le=1.0)
    stitch_seam_penalty: float = Field(ge=0.0, le=1.0)
    nadir_penalty: float = Field(ge=0.0, le=1.0)
    track_confidence: float = Field(ge=0.0, le=1.0)
    repetition_penalty: float = Field(ge=0.0, le=1.0)


class DirectorDecision(VersionedSphericalModel):
    decision_id: str = Field(min_length=1)
    start_seconds: float = Field(ge=0)
    end_seconds: float = Field(gt=0)
    intent: DirectorIntent
    subject_id: str | None = None
    alternative_subject_ids: list[str] = Field(default_factory=list)
    shot_size: ShotSize
    transition: DirectorTransition
    yaw: float
    pitch: float = Field(ge=-90.0, le=90.0)
    horizontal_fov: float = Field(gt=1.0, lt=179.0)
    confidence: float = Field(ge=0.0, le=1.0)
    scores: DirectorScoreComponents
    total_score: float = Field(ge=0.0, le=1.0)
    rationale: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @field_validator("yaw", mode="before")
    @classmethod
    def normalize_yaw(cls, value: Any) -> float:
        from open_storyline.spherical.geometry import normalize_angle

        return normalize_angle(float(value))

    @model_validator(mode="after")
    def validate_interval(self) -> "DirectorDecision":
        if self.end_seconds <= self.start_seconds:
            raise ValueError("director decision end must follow start")
        return self


class MotionValidationReport(VersionedSphericalModel):
    constraints_satisfied: bool
    maximum_observed_pan_speed: float = Field(ge=0)
    maximum_observed_acceleration: float = Field(ge=0)
    maximum_observed_fov_change_rate: float = Field(ge=0)
    minimum_observed_shot_duration: float = Field(ge=0)
    maximum_observed_shot_duration: float = Field(ge=0)
    cut_count: int = Field(ge=0)
    pan_count: int = Field(ge=0)
    forced_cut_count: int = Field(ge=0)
    warnings: list[str] = Field(default_factory=list)


class EditingProfile(VersionedSphericalModel):
    profile_id: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    preferred_shot_duration_min_seconds: float = Field(gt=0)
    preferred_shot_duration_max_seconds: float = Field(gt=0)
    close_fov_range: tuple[float, float]
    medium_fov_range: tuple[float, float]
    wide_fov_range: tuple[float, float]
    maximum_pan_speed_degrees_per_second: float = Field(gt=0)
    speaker_following_strength: float = Field(ge=0.0, le=1.0)
    reaction_shot_frequency: float = Field(ge=0.0, le=1.0)
    establishing_shot_frequency: float = Field(ge=0.0, le=1.0)
    vertical_output_behavior: VerticalOutputBehavior
    probable_speaker_threshold: float = Field(ge=0.0, le=1.0)
    minimum_track_confidence: float = Field(ge=0.0, le=1.0)
    avoid_nadir_below_pitch: float = Field(ge=-90.0, le=90.0)
    stitch_seam_yaws: list[float] = Field(default_factory=lambda: [180.0])
    stitch_seam_zone_width: float = Field(ge=0.0, le=90.0)
    music_handoff_behavior: str = Field(min_length=1)
    subtitle_handoff_behavior: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_ranges(self) -> "EditingProfile":
        if self.preferred_shot_duration_max_seconds < self.preferred_shot_duration_min_seconds:
            raise ValueError("profile shot duration range is reversed")
        for field_name in ("close_fov_range", "medium_fov_range", "wide_fov_range"):
            minimum, maximum = getattr(self, field_name)
            if not 1.0 < minimum <= maximum < 179.0:
                raise ValueError(f"{field_name} must be an ordered valid FOV range")
        return self


class DirectorReviewArtifacts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    camera_trajectory_json_path: Path
    score_breakdown_json_path: Path
    hashes: dict[str, str]

    @field_validator("hashes")
    @classmethod
    def validate_hashes(cls, values: dict[str, str]) -> dict[str, str]:
        required = {"camera_trajectory", "score_breakdown"}
        if set(values) != required:
            raise ValueError("director artifact hashes must contain every required output")
        if any(not SHA256_PATTERN.fullmatch(value) for value in values.values()):
            raise ValueError("director artifact hashes must be SHA-256 values")
        return values


class DirectorProposal(VersionedSphericalModel):
    proposal_id: str = Field(min_length=1)
    timeline_id: str = Field(min_length=1)
    speaker_analysis_id: str = Field(min_length=1)
    analysis_id: str = Field(min_length=1)
    asset_id: str = Field(min_length=1)
    profile_id: str = Field(min_length=1)
    variant: DirectorVariant
    status: DirectorProposalStatus = DirectorProposalStatus.PROPOSED
    decisions: list[DirectorDecision] = Field(min_length=1)
    camera_track: VirtualCameraTrack
    motion_constraints: MotionConstraints
    motion_validation: MotionValidationReport
    aggregate_score: float = Field(ge=0.0, le=1.0)
    review_artifacts: DirectorReviewArtifacts
    warnings: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="after")
    def validate_proposal(self) -> "DirectorProposal":
        if self.camera_track.asset_id != self.asset_id:
            raise ValueError("director proposal track does not match its asset")
        if self.camera_track.created_by != TrackCreatedBy.DIRECTOR:
            raise ValueError("director proposal track must be marked director")
        starts = [decision.start_seconds for decision in self.decisions]
        if starts != sorted(starts):
            raise ValueError("director decisions must be ordered")
        identifiers = [decision.decision_id for decision in self.decisions]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("director decision IDs must be unique")
        if not self.motion_validation.constraints_satisfied:
            raise ValueError("director proposal motion constraints are not satisfied")
        if not abs(self.decisions[0].start_seconds - self.camera_track.source_start_seconds) <= 1e-6:
            raise ValueError("director decisions do not start with the camera track")
        if not abs(self.decisions[-1].end_seconds - self.camera_track.source_end_seconds) <= 1e-6:
            raise ValueError("director decisions do not end with the camera track")
        if any(
            abs(left.end_seconds - right.start_seconds) > 1e-6
            for left, right in zip(self.decisions, self.decisions[1:])
        ):
            raise ValueError("director decisions must cover a contiguous timeline")
        return self


class ProposalComparisonItem(VersionedSphericalModel):
    proposal_id: str = Field(min_length=1)
    variant: DirectorVariant
    rank: int = Field(gt=0)
    aggregate_score: float = Field(ge=0.0, le=1.0)
    shot_count: int = Field(gt=0)
    cut_count: int = Field(ge=0)
    pan_count: int = Field(ge=0)
    mean_confidence: float = Field(ge=0.0, le=1.0)
    strengths: list[str] = Field(default_factory=list)
    tradeoffs: list[str] = Field(default_factory=list)


class DirectorComparison(VersionedSphericalModel):
    comparison_id: str = Field(min_length=1)
    timeline_id: str = Field(min_length=1)
    proposal_ids: list[str] = Field(min_length=2)
    ranking: list[ProposalComparisonItem] = Field(min_length=2)
    recommended_proposal_id: str = Field(min_length=1)
    summary: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_comparison(self) -> "DirectorComparison":
        if len(set(self.proposal_ids)) != len(self.proposal_ids):
            raise ValueError("comparison proposal IDs must be unique")
        if self.recommended_proposal_id not in self.proposal_ids:
            raise ValueError("recommended proposal is not in this comparison")
        if {item.proposal_id for item in self.ranking} != set(self.proposal_ids):
            raise ValueError("comparison ranking must cover every proposal")
        if sorted(item.rank for item in self.ranking) != list(
            range(1, len(self.ranking) + 1)
        ):
            raise ValueError("comparison ranks must be contiguous from one")
        return self


class TimelineSourceReference(VersionedSphericalModel):
    kind: TimelineReferenceKind
    flat_path: Path | None = None
    spherical_asset_id: str | None = None
    approved_camera_track_id: str | None = None
    generated_reframe_path: Path | None = None
    render_manifest_path: Path | None = None
    render_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_reference(self) -> "TimelineSourceReference":
        if self.kind == TimelineReferenceKind.FLAT_SOURCE:
            if self.flat_path is None:
                raise ValueError("flat source reference requires flat_path")
            if any(
                value is not None
                for value in (
                    self.spherical_asset_id,
                    self.approved_camera_track_id,
                    self.generated_reframe_path,
                    self.render_manifest_path,
                    self.render_hash,
                )
            ):
                raise ValueError("flat source reference contains incompatible fields")
        elif self.kind == TimelineReferenceKind.SPHERICAL_APPROVED_TRACK:
            if not self.spherical_asset_id or not self.approved_camera_track_id:
                raise ValueError("spherical source reference requires asset and approved track")
            if any(
                value is not None
                for value in (
                    self.flat_path,
                    self.generated_reframe_path,
                    self.render_manifest_path,
                    self.render_hash,
                )
            ):
                raise ValueError("spherical source reference contains incompatible fields")
        elif self.kind == TimelineReferenceKind.GENERATED_REFRAME_ARTIFACT:
            if (
                self.generated_reframe_path is None
                or self.render_manifest_path is None
                or self.render_hash is None
                or not self.spherical_asset_id
                or not self.approved_camera_track_id
            ):
                raise ValueError("generated reframe reference is incomplete")
            if self.flat_path is not None:
                raise ValueError("generated reframe reference cannot also be flat source")
        return self


class DirectorTimelineHandoff(VersionedSphericalModel):
    handoff_id: str = Field(min_length=1)
    proposal_id: str = Field(min_length=1)
    timeline_id: str = Field(min_length=1)
    source_reference: TimelineSourceReference
    ordinary_media_id: str = Field(min_length=1)
    ordinary_clip_id: str = Field(min_length=1)
    music_handoff_behavior: str = Field(min_length=1)
    subtitle_handoff_behavior: str = Field(min_length=1)
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_handoff(self) -> "DirectorTimelineHandoff":
        if self.source_reference.kind != TimelineReferenceKind.GENERATED_REFRAME_ARTIFACT:
            raise ValueError("director timeline handoff requires a rendered flat artifact")
        return self


DIRECTION_SCHEMA_MODELS: dict[str, type[VersionedSphericalModel]] = {
    "transcript-word": TranscriptWord,
    "transcript-turn": TranscriptTurn,
    "timeline-signal": TimelineSignal,
    "normalized-story-timeline": NormalizedStoryTimeline,
    "speaker-candidate-score": SpeakerCandidateScore,
    "speaker-turn-assessment": SpeakerTurnAssessment,
    "speaker-candidate-analysis": SpeakerCandidateAnalysis,
    "motion-constraints": MotionConstraints,
    "director-decision": DirectorDecision,
    "editing-profile": EditingProfile,
    "director-proposal": DirectorProposal,
    "director-comparison": DirectorComparison,
    "timeline-source-reference": TimelineSourceReference,
    "director-timeline-handoff": DirectorTimelineHandoff,
}
