"""Deterministic, explainable virtual-director proposal generation."""

from __future__ import annotations

import math
from pathlib import Path
from statistics import mean
from typing import Any

import numpy as np

from open_storyline.spherical.analysis_schemas import (
    SphericalAnalysis,
    SubjectSample,
    SubjectTrack,
    VisibilityState,
)
from open_storyline.spherical.direction_schemas import (
    DirectorComparison,
    DirectorDecision,
    DirectorIntent,
    DirectorProposal,
    DirectorProposalStatus,
    DirectorReviewArtifacts,
    DirectorScoreComponents,
    DirectorTransition,
    DirectorVariant,
    EditingProfile,
    MotionConstraints,
    MotionValidationReport,
    NormalizedStoryTimeline,
    ProposalComparisonItem,
    ShotSize,
    SpeakerAssessmentStatus,
    SpeakerCandidateAnalysis,
    SpeakerTurnAssessment,
    TimelineSignalKind,
)
from open_storyline.spherical.errors import SphericalValidationError
from open_storyline.spherical.geometry import (
    great_circle_angular_distance,
    normalize_angle,
    shortest_angle_delta,
)
from open_storyline.spherical.manifests import atomic_write_json, sha256_file, sha256_json
from open_storyline.spherical.schemas import (
    CameraKeyframe,
    Easing,
    SphericalAsset,
    TrackCreatedBy,
    VirtualCameraTrack,
)


DIRECTOR_VERSION = "1.0.0"


def _clamp(value: float, minimum: float = 0.0, maximum: float = 1.0) -> float:
    return max(minimum, min(maximum, value))


def default_motion_constraints(
    profile: EditingProfile,
    variant: DirectorVariant,
) -> MotionConstraints:
    minimum = profile.preferred_shot_duration_min_seconds
    maximum = profile.preferred_shot_duration_max_seconds
    max_pan = profile.maximum_pan_speed_degrees_per_second
    cut_spacing = max(0.4, minimum * 0.55)
    if variant == DirectorVariant.CINEMATIC:
        minimum *= 1.15
        maximum *= 1.20
        max_pan *= 0.80
        cut_spacing *= 1.15
    elif variant == DirectorVariant.MINIMAL_MOTION:
        minimum *= 1.45
        maximum *= 1.40
        max_pan *= 0.65
        cut_spacing *= 1.45
    elif variant == DirectorVariant.HIGH_ENERGY:
        minimum *= 0.55
        maximum *= 0.68
        max_pan *= 1.25
        cut_spacing *= 0.60
    elif variant == DirectorVariant.VERTICAL_SOCIAL:
        minimum *= 0.75
        maximum *= 0.85
        max_pan *= 0.90
        cut_spacing *= 0.80
    return MotionConstraints(
        maximum_pan_speed_degrees_per_second=max_pan,
        maximum_acceleration_degrees_per_second_squared=max_pan * 2.5,
        minimum_shot_duration_seconds=minimum,
        maximum_shot_duration_seconds=max(maximum, minimum),
        minimum_time_between_cuts_seconds=cut_spacing,
        maximum_fov_change_degrees_per_second=30.0,
        subject_dead_zone_degrees=6.0,
        maximum_low_confidence_tracking_seconds=1.5,
    )


def _shot_windows(duration: float, constraints: MotionConstraints) -> list[tuple[float, float]]:
    if duration <= 0:
        raise SphericalValidationError("Director source duration must be positive")
    target = (
        constraints.minimum_shot_duration_seconds
        + constraints.maximum_shot_duration_seconds
    ) / 2.0
    target = min(constraints.maximum_shot_duration_seconds, max(0.05, target))
    windows: list[tuple[float, float]] = []
    start = 0.0
    while start < duration - 1e-9:
        end = min(duration, start + target)
        windows.append((round(start, 6), round(end, 6)))
        start = end
    if (
        len(windows) > 1
        and windows[-1][1] - windows[-1][0]
        < constraints.minimum_shot_duration_seconds
    ):
        prior_start, _ = windows[-2]
        if duration - prior_start <= constraints.maximum_shot_duration_seconds + 1e-9:
            windows[-2:] = [(prior_start, round(duration, 6))]
    return windows


def _assessment_at(
    speaker_analysis: SpeakerCandidateAnalysis,
    start: float,
    end: float,
) -> SpeakerTurnAssessment | None:
    candidates = [
        assessment
        for assessment in speaker_analysis.turns
        if assessment.end_seconds > start and assessment.start_seconds < end
    ]
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda assessment: (
            min(end, assessment.end_seconds) - max(start, assessment.start_seconds),
            assessment.confidence,
        ),
    )


def _story_context(
    timeline: NormalizedStoryTimeline,
    start: float,
    end: float,
) -> tuple[float, list[str]]:
    kinds = {
        signal.kind
        for signal in timeline.signals
        if signal.end_seconds > start and signal.start_seconds < end
    }
    weights = {
        TimelineSignalKind.TRANSCRIPT: 0.24,
        TimelineSignalKind.WORD: 0.04,
        TimelineSignalKind.SCENE: 0.08,
        TimelineSignalKind.CLIP_UNDERSTANDING: 0.18,
        TimelineSignalKind.SCRIPT: 0.14,
        TimelineSignalKind.TIMELINE_PLAN: 0.07,
    }
    score = 0.20 + sum(weight for kind, weight in weights.items() if kind in kinds)
    labels = sorted(kind.value for kind in kinds if kind in weights)
    return _clamp(score), labels


def _find_track(analysis: SphericalAnalysis, subject_id: str | None) -> SubjectTrack | None:
    if subject_id is None:
        return None
    return next(
        (track for track in analysis.subject_tracks if track.subject_id == subject_id),
        None,
    )


def _sample_at(track: SubjectTrack | None, timestamp: float) -> SubjectSample | None:
    if track is None:
        return None
    visible = [
        sample for sample in track.samples if sample.visibility == VisibilityState.VISIBLE
    ]
    if not visible:
        return None
    return min(visible, key=lambda sample: abs(sample.timestamp_seconds - timestamp))


def _group_direction(
    analysis: SphericalAnalysis,
    timestamp: float,
) -> tuple[float, float, float]:
    samples = [
        sample
        for track in analysis.subject_tracks
        if (sample := _sample_at(track, timestamp)) is not None
    ]
    if not samples:
        return 0.0, 0.0, 0.2
    vector = np.zeros(3, dtype=np.float64)
    for sample in samples:
        yaw = math.radians(sample.yaw)
        pitch = math.radians(sample.pitch)
        weight = max(0.001, sample.confidence)
        vector += weight * np.asarray(
            [
                math.cos(pitch) * math.cos(yaw),
                math.cos(pitch) * math.sin(yaw),
                math.sin(pitch),
            ]
        )
    if math.isclose(float(np.linalg.norm(vector)), 0.0):
        return 0.0, 0.0, mean(sample.confidence for sample in samples)
    vector /= np.linalg.norm(vector)
    return (
        normalize_angle(math.degrees(math.atan2(vector[1], vector[0]))),
        math.degrees(math.asin(_clamp(float(vector[2]), -1.0, 1.0))),
        mean(sample.confidence for sample in samples),
    )


def _fov(profile: EditingProfile, shot_size: ShotSize) -> float:
    ranges = {
        ShotSize.CLOSE: profile.close_fov_range,
        ShotSize.MEDIUM: profile.medium_fov_range,
        ShotSize.WIDE: profile.wide_fov_range,
    }
    minimum, maximum = ranges[shot_size]
    return (minimum + maximum) / 2.0


def _seam_penalty(yaw: float, profile: EditingProfile) -> float:
    if not profile.stitch_seam_yaws or profile.stitch_seam_zone_width <= 0:
        return 0.0
    closest = min(
        abs(shortest_angle_delta(yaw, seam)) for seam in profile.stitch_seam_yaws
    )
    return _clamp(1.0 - closest / max(0.001, profile.stitch_seam_zone_width))


def _avoid_seam(yaw: float, profile: EditingProfile) -> float:
    adjusted = yaw
    for seam in profile.stitch_seam_yaws:
        delta = shortest_angle_delta(seam, adjusted)
        if abs(delta) < profile.stitch_seam_zone_width:
            direction = 1.0 if delta >= 0 else -1.0
            adjusted = normalize_angle(
                seam + direction * profile.stitch_seam_zone_width
            )
    return adjusted


def _choose_subject(
    assessment: SpeakerTurnAssessment | None,
    variant: DirectorVariant,
    profile: EditingProfile,
    window_index: int,
    previous_subject_id: str | None,
) -> tuple[str | None, DirectorIntent, list[str], float, list[str]]:
    warnings: list[str] = []
    if assessment is None:
        return None, DirectorIntent.NO_RELIABLE_SUBJECT, [], 0.0, warnings
    alternatives = [candidate.subject_id for candidate in assessment.candidates]
    if assessment.status != SpeakerAssessmentStatus.PROBABLE:
        warnings.extend(assessment.warnings)
        warnings.append(
            "Speaker evidence is not probable; director uses a non-identifying wide view."
        )
        return None, DirectorIntent.NO_RELIABLE_SUBJECT, alternatives, assessment.confidence, warnings
    selected = assessment.selected_subject_id
    intent = DirectorIntent.SPEAKER
    reaction_cadence = max(
        1,
        round(1.0 / max(0.01, profile.reaction_shot_frequency)),
    )
    reaction_due = (
        variant == DirectorVariant.HIGH_ENERGY and window_index % 2 == 1
    ) or (
        variant == DirectorVariant.CINEMATIC
        and window_index % reaction_cadence == 0
    )
    if (
        variant in {DirectorVariant.CINEMATIC, DirectorVariant.HIGH_ENERGY}
        and window_index > 0
        and alternatives
        and profile.reaction_shot_frequency > 0
        and reaction_due
    ):
        reaction = next(
            (candidate for candidate in alternatives if candidate != selected),
            None,
        )
        if reaction is not None:
            selected = reaction
            intent = DirectorIntent.REACTION
    if variant == DirectorVariant.MINIMAL_MOTION and previous_subject_id is not None:
        selected = previous_subject_id
        intent = DirectorIntent.HOLD
    return selected, intent, alternatives, assessment.confidence, warnings


def _shot_size(
    variant: DirectorVariant,
    intent: DirectorIntent,
    window_index: int,
) -> ShotSize:
    if intent in {DirectorIntent.ESTABLISHING, DirectorIntent.NO_RELIABLE_SUBJECT}:
        return ShotSize.WIDE
    if intent == DirectorIntent.REACTION:
        return ShotSize.MEDIUM
    if variant in {DirectorVariant.SPEAKER_FOCUSED, DirectorVariant.VERTICAL_SOCIAL}:
        return ShotSize.CLOSE if window_index % 3 else ShotSize.MEDIUM
    if variant == DirectorVariant.HIGH_ENERGY:
        return ShotSize.CLOSE if window_index % 2 else ShotSize.MEDIUM
    if variant == DirectorVariant.MINIMAL_MOTION:
        return ShotSize.WIDE
    return ShotSize.MEDIUM


def _component_scores(
    *,
    intent: DirectorIntent,
    assessment: SpeakerTurnAssessment | None,
    sample: SubjectSample | None,
    subject_id: str | None,
    previous_subject_id: str | None,
    transition: DirectorTransition,
    yaw: float,
    raw_pitch: float,
    profile: EditingProfile,
    motion_cost: float,
    repetition_count: int,
    story_relevance: float,
) -> tuple[DirectorScoreComponents, float]:
    intent_relevance = {
        DirectorIntent.SPEAKER: 0.90,
        DirectorIntent.REACTION: 0.68,
        DirectorIntent.ESTABLISHING: 0.72,
        DirectorIntent.HOLD: 0.62,
        DirectorIntent.NO_RELIABLE_SUBJECT: 0.42,
    }[intent]
    semantic = _clamp(intent_relevance * 0.55 + story_relevance * 0.45)
    speaker = 0.0
    if assessment is not None and subject_id is not None:
        candidate = next(
            (
                candidate
                for candidate in assessment.candidates
                if candidate.subject_id == subject_id
            ),
            None,
        )
        speaker = candidate.total_score if candidate is not None else 0.0
    salience = (
        _clamp(sample.angular_width * sample.angular_height / 900.0)
        if sample is not None
        else 0.45
    )
    seam = _seam_penalty(yaw, profile)
    nadir = (
        _clamp(
            (profile.avoid_nadir_below_pitch - raw_pitch) / 30.0
        )
        if raw_pitch < profile.avoid_nadir_below_pitch
        else 0.0
    )
    composition = _clamp(1.0 - seam * 0.45 - nadir * 0.55)
    continuity = 1.0 if subject_id == previous_subject_id else (0.58 if previous_subject_id else 0.75)
    cut_cost = 1.0 if transition == DirectorTransition.CUT else 0.0
    track_confidence = sample.confidence if sample is not None else 0.35
    repetition = _clamp((repetition_count - 2) / 4.0)
    components = DirectorScoreComponents(
        semantic_relevance=semantic,
        speaker_likelihood=speaker,
        visual_salience=salience,
        composition_quality=composition,
        continuity=continuity,
        motion_cost=_clamp(motion_cost),
        cut_cost=cut_cost,
        stitch_seam_penalty=seam,
        nadir_penalty=nadir,
        track_confidence=track_confidence,
        repetition_penalty=repetition,
    )
    reward = (
        semantic * 0.14
        + speaker * 0.19
        + salience * 0.11
        + composition * 0.11
        + continuity * 0.10
        + track_confidence * 0.15
        + 0.20
    )
    penalty = (
        components.motion_cost * 0.06
        + cut_cost * 0.04
        + seam * 0.04
        + nadir * 0.03
        + repetition * 0.03
    )
    return components, _clamp(reward - penalty)


def _append_keyframe(
    keyframes: list[CameraKeyframe],
    *,
    timestamp: float,
    decision: DirectorDecision,
) -> None:
    frame = CameraKeyframe(
        time_seconds=max(0.0, timestamp),
        yaw=decision.yaw,
        pitch=decision.pitch,
        horizontal_fov=decision.horizontal_fov,
        easing=Easing.SMOOTHSTEP,
        subject_id=decision.subject_id,
        confidence=decision.confidence,
    )
    if keyframes and math.isclose(
        keyframes[-1].time_seconds,
        frame.time_seconds,
        abs_tol=1e-7,
    ):
        keyframes[-1] = frame
    elif not keyframes or frame.time_seconds > keyframes[-1].time_seconds:
        keyframes.append(frame)


def _build_camera_track(
    proposal_id: str,
    asset: SphericalAsset,
    decisions: list[DirectorDecision],
    *,
    output_width: int,
    output_height: int,
    profile: EditingProfile,
    variant: DirectorVariant,
    constraints: MotionConstraints,
    motion_validation: MotionValidationReport,
) -> VirtualCameraTrack:
    keyframes: list[CameraKeyframe] = []
    _append_keyframe(keyframes, timestamp=0.0, decision=decisions[0])
    for index, decision in enumerate(decisions[1:], start=1):
        previous = decisions[index - 1]
        boundary = decision.start_seconds
        if decision.transition == DirectorTransition.CUT:
            epsilon = min(0.001, max(0.000001, (boundary - previous.start_seconds) / 1000.0))
            _append_keyframe(
                keyframes,
                timestamp=max(previous.start_seconds, boundary - epsilon),
                decision=previous,
            )
            _append_keyframe(keyframes, timestamp=boundary, decision=decision)
        elif decision.transition == DirectorTransition.PAN:
            _append_keyframe(keyframes, timestamp=boundary, decision=previous)
            next_is_cut = (
                index + 1 < len(decisions)
                and decisions[index + 1].transition == DirectorTransition.CUT
            )
            target_time = decision.end_seconds - (0.001 if next_is_cut else 0.0)
            _append_keyframe(keyframes, timestamp=target_time, decision=decision)
        else:
            _append_keyframe(keyframes, timestamp=boundary, decision=decision)
    _append_keyframe(
        keyframes,
        timestamp=asset.duration_seconds,
        decision=decisions[-1],
    )
    return VirtualCameraTrack(
        track_id=f"camera-{sha256_json(proposal_id)[:16]}",
        asset_id=asset.asset_id,
        name=f"director-{profile.profile_id}-{variant.value}",
        output_width=output_width,
        output_height=output_height,
        source_start_seconds=0.0,
        source_end_seconds=asset.duration_seconds,
        keyframes=keyframes,
        created_by=TrackCreatedBy.DIRECTOR,
        generation_metadata={
            "director_version": DIRECTOR_VERSION,
            "proposal_id": proposal_id,
            "profile": profile.model_dump(mode="json"),
            "variant": variant.value,
            "motion_constraints": constraints.model_dump(mode="json"),
            "motion_validation": motion_validation.model_dump(mode="json"),
            "decision_scores": [
                {
                    "decision_id": decision.decision_id,
                    "total_score": decision.total_score,
                    "components": decision.scores.model_dump(mode="json"),
                }
                for decision in decisions
            ],
        },
    )


def generate_director_proposal(
    timeline: NormalizedStoryTimeline,
    speaker_analysis: SpeakerCandidateAnalysis,
    analysis: SphericalAnalysis,
    asset: SphericalAsset,
    profile: EditingProfile,
    *,
    variant: DirectorVariant,
    destination_dir: str | Path,
    output_width: int,
    output_height: int,
    motion_constraints: MotionConstraints | None = None,
) -> DirectorProposal:
    """Create one proposal and never activate its camera track."""

    identities = {
        timeline.asset_id,
        speaker_analysis.asset_id,
        analysis.asset_id,
        asset.asset_id,
    }
    if len(identities) != 1:
        raise SphericalValidationError("Director inputs do not reference one asset")
    if timeline.analysis_id != analysis.analysis_id:
        raise SphericalValidationError("Director timeline and analysis do not match")
    if speaker_analysis.timeline_id != timeline.timeline_id:
        raise SphericalValidationError("Speaker analysis and timeline do not match")
    constraints = motion_constraints or default_motion_constraints(profile, variant)
    windows = _shot_windows(asset.duration_seconds, constraints)
    draft: list[dict[str, Any]] = []
    previous_subject: str | None = None
    previous_yaw = 0.0
    previous_pitch = 0.0
    previous_fov = _fov(profile, ShotSize.WIDE)
    previous_pan_speed = 0.0
    last_cut_time = -math.inf
    repetition_count = 0
    low_confidence_duration = 0.0
    forced_cuts = 0
    warnings: list[str] = []
    pan_speeds: list[float] = []
    accelerations: list[float] = []
    fov_rates: list[float] = []

    for index, (start, end) in enumerate(windows):
        midpoint = (start + end) / 2.0
        story_relevance, story_kinds = _story_context(timeline, start, end)
        assessment = _assessment_at(speaker_analysis, start, end)
        subject_id, intent, alternatives, speaker_confidence, decision_warnings = _choose_subject(
            assessment,
            variant,
            profile,
            index,
            previous_subject,
        )
        at_scene_boundary = any(
            math.isclose(start, boundary, abs_tol=1e-6)
            for boundary in timeline.scene_boundaries_seconds[1:-1]
        )
        establishing_cadence = max(
            1,
            round(1.0 / max(0.01, profile.establishing_shot_frequency)),
        )
        if (
            variant == DirectorVariant.CINEMATIC
            and profile.establishing_shot_frequency > 0
            and (
                index == 0
                or (at_scene_boundary and index % establishing_cadence == 0)
            )
        ):
            subject_id = None
            intent = DirectorIntent.ESTABLISHING
        track = _find_track(analysis, subject_id)
        sample = _sample_at(track, midpoint)
        confidence = sample.confidence if sample is not None else speaker_confidence
        shot_duration = end - start
        if subject_id is not None and confidence < profile.minimum_track_confidence:
            low_confidence_duration += shot_duration
        else:
            low_confidence_duration = 0.0
        if (
            subject_id is not None
            and low_confidence_duration
            > constraints.maximum_low_confidence_tracking_seconds
        ):
            decision_warnings.append(
                "Low-confidence tracking exceeded the configured duration; switched to a wide non-identifying view."
            )
            subject_id = None
            track = None
            sample = None
            intent = DirectorIntent.NO_RELIABLE_SUBJECT
            confidence = 0.35
            low_confidence_duration = 0.0

        shot_size = _shot_size(variant, intent, index)
        fov = _fov(profile, shot_size)
        if sample is not None:
            raw_yaw, raw_pitch = sample.yaw, sample.pitch
        else:
            raw_yaw, raw_pitch, group_confidence = _group_direction(analysis, midpoint)
            confidence = max(confidence, group_confidence)
        yaw = raw_yaw if subject_id is not None else _avoid_seam(raw_yaw, profile)
        pitch = max(profile.avoid_nadir_below_pitch, raw_pitch)
        transition = DirectorTransition.HOLD
        motion_cost = 0.0
        if index > 0:
            angular_distance = great_circle_angular_distance(
                previous_yaw,
                previous_pitch,
                yaw,
                pitch,
            )
            pan_speed = angular_distance / max(0.001, shot_duration)
            fov_rate = abs(fov - previous_fov) / max(0.001, shot_duration)
            acceleration = abs(pan_speed - previous_pan_speed) / max(0.001, shot_duration)
            motion_cost = _clamp(
                pan_speed / constraints.maximum_pan_speed_degrees_per_second
            )
            within_dead_zone = (
                angular_distance <= constraints.subject_dead_zone_degrees
                and abs(fov - previous_fov)
                <= constraints.subject_dead_zone_degrees
            )
            pan_valid = (
                pan_speed <= constraints.maximum_pan_speed_degrees_per_second + 1e-9
                and acceleration
                <= constraints.maximum_acceleration_degrees_per_second_squared + 1e-9
                and fov_rate
                <= constraints.maximum_fov_change_degrees_per_second + 1e-9
            )
            if within_dead_zone or variant == DirectorVariant.MINIMAL_MOTION:
                transition = DirectorTransition.HOLD
                yaw, pitch, fov = previous_yaw, previous_pitch, previous_fov
                subject_id = previous_subject
                intent = DirectorIntent.HOLD
                pan_speed = acceleration = fov_rate = 0.0
            elif pan_valid:
                transition = DirectorTransition.PAN
                pan_speeds.append(pan_speed)
                accelerations.append(acceleration)
                fov_rates.append(fov_rate)
                previous_pan_speed = pan_speed
            elif start - last_cut_time >= constraints.minimum_time_between_cuts_seconds:
                transition = DirectorTransition.CUT
                forced_cuts += 1
                last_cut_time = start
                previous_pan_speed = 0.0
                decision_warnings.append(
                    "A cut replaced a pan that would exceed motion or FOV-rate constraints."
                )
            else:
                transition = DirectorTransition.HOLD
                yaw, pitch, fov = previous_yaw, previous_pitch, previous_fov
                subject_id = previous_subject
                intent = DirectorIntent.HOLD
                decision_warnings.append(
                    "Held the current frame because a required cut would violate minimum cut spacing."
                )
                pan_speed = acceleration = fov_rate = 0.0
        if subject_id == previous_subject:
            repetition_count += 1
        else:
            repetition_count = 1
        components, total_score = _component_scores(
            intent=intent,
            assessment=assessment,
            sample=sample,
            subject_id=subject_id,
            previous_subject_id=previous_subject,
            transition=transition,
            yaw=yaw,
            raw_pitch=raw_pitch,
            profile=profile,
            motion_cost=motion_cost,
            repetition_count=repetition_count,
            story_relevance=story_relevance,
        )
        rationale = [
            f"intent={intent.value}",
            f"shot_size={shot_size.value}",
            f"transition={transition.value}",
            f"speaker_likelihood={components.speaker_likelihood:.3f}",
            f"track_confidence={components.track_confidence:.3f}",
            f"story_context={','.join(story_kinds) if story_kinds else 'none'}",
        ]
        draft.append(
            {
                "decision_id": f"decision-{index + 1:04d}",
                "start_seconds": start,
                "end_seconds": end,
                "intent": intent,
                "subject_id": subject_id,
                "alternative_subject_ids": alternatives,
                "shot_size": shot_size,
                "transition": transition,
                "yaw": yaw,
                "pitch": pitch,
                "horizontal_fov": fov,
                "confidence": _clamp(confidence),
                "scores": components,
                "total_score": total_score,
                "rationale": rationale,
                "warnings": list(dict.fromkeys(decision_warnings)),
            }
        )
        previous_subject = subject_id
        previous_yaw, previous_pitch, previous_fov = yaw, pitch, fov

    decisions = [DirectorDecision.model_validate(item) for item in draft]
    durations = [decision.end_seconds - decision.start_seconds for decision in decisions]
    constraints_satisfied = (
        all(
            duration <= constraints.maximum_shot_duration_seconds + 1e-6
            and (
                duration >= constraints.minimum_shot_duration_seconds - 1e-6
                or math.isclose(asset.duration_seconds, duration, abs_tol=1e-6)
            )
            for duration in durations
        )
        and max(pan_speeds, default=0.0)
        <= constraints.maximum_pan_speed_degrees_per_second + 1e-6
        and max(accelerations, default=0.0)
        <= constraints.maximum_acceleration_degrees_per_second_squared + 1e-6
        and max(fov_rates, default=0.0)
        <= constraints.maximum_fov_change_degrees_per_second + 1e-6
    )
    if asset.duration_seconds < constraints.minimum_shot_duration_seconds:
        warnings.append(
            "The source is shorter than the configured minimum shot duration; one source-length shot was retained."
        )
    motion_validation = MotionValidationReport(
        constraints_satisfied=constraints_satisfied,
        maximum_observed_pan_speed=max(pan_speeds, default=0.0),
        maximum_observed_acceleration=max(accelerations, default=0.0),
        maximum_observed_fov_change_rate=max(fov_rates, default=0.0),
        minimum_observed_shot_duration=min(durations),
        maximum_observed_shot_duration=max(durations),
        cut_count=sum(
            decision.transition == DirectorTransition.CUT for decision in decisions
        ),
        pan_count=sum(
            decision.transition == DirectorTransition.PAN for decision in decisions
        ),
        forced_cut_count=forced_cuts,
        warnings=[],
    )
    if not constraints_satisfied:
        raise SphericalValidationError(
            "Director could not satisfy configured motion constraints.",
            details=motion_validation.model_dump(mode="json"),
        )
    identity_payload = {
        "timeline_id": timeline.timeline_id,
        "speaker_analysis_id": speaker_analysis.speaker_analysis_id,
        "analysis_id": analysis.analysis_id,
        "asset_id": asset.asset_id,
        "profile_id": profile.profile_id,
        "variant": variant.value,
        "output": [output_width, output_height],
        "constraints": constraints.model_dump(mode="json"),
        "decisions": [decision.model_dump(mode="json") for decision in decisions],
    }
    proposal_id = f"director-{sha256_json(identity_payload)[:16]}"
    track = _build_camera_track(
        proposal_id,
        asset,
        decisions,
        output_width=output_width,
        output_height=output_height,
        profile=profile,
        variant=variant,
        constraints=constraints,
        motion_validation=motion_validation,
    )
    aggregate = mean(decision.total_score for decision in decisions)
    destination = Path(destination_dir).expanduser().resolve(strict=False) / proposal_id
    destination.mkdir(parents=True, exist_ok=True)
    trajectory_path = destination / "camera-trajectory.json"
    scores_path = destination / "score-breakdown.json"
    atomic_write_json(
        trajectory_path,
        {
            "schema_version": "1.0",
            "proposal_id": proposal_id,
            "profile_id": profile.profile_id,
            "variant": variant.value,
            "keyframes": [frame.model_dump(mode="json") for frame in track.keyframes],
        },
    )
    atomic_write_json(
        scores_path,
        {
            "schema_version": "1.0",
            "proposal_id": proposal_id,
            "aggregate_score": aggregate,
            "decisions": [
                {
                    "decision_id": decision.decision_id,
                    "total_score": decision.total_score,
                    "components": decision.scores.model_dump(mode="json"),
                    "rationale": decision.rationale,
                    "warnings": decision.warnings,
                }
                for decision in decisions
            ],
            "motion_validation": motion_validation.model_dump(mode="json"),
        },
    )
    artifacts = DirectorReviewArtifacts(
        camera_trajectory_json_path=trajectory_path,
        score_breakdown_json_path=scores_path,
        hashes={
            "camera_trajectory": sha256_file(trajectory_path),
            "score_breakdown": sha256_file(scores_path),
        },
    )
    warnings.extend(speaker_analysis.warnings)
    warnings.extend(
        warning for decision in decisions for warning in decision.warnings
    )
    return DirectorProposal(
        proposal_id=proposal_id,
        timeline_id=timeline.timeline_id,
        speaker_analysis_id=speaker_analysis.speaker_analysis_id,
        analysis_id=analysis.analysis_id,
        asset_id=asset.asset_id,
        profile_id=profile.profile_id,
        variant=variant,
        status=DirectorProposalStatus.PROPOSED,
        decisions=decisions,
        camera_track=track,
        motion_constraints=constraints,
        motion_validation=motion_validation,
        aggregate_score=aggregate,
        review_artifacts=artifacts,
        warnings=list(dict.fromkeys(warnings)),
    )


def compare_director_proposals(
    proposals: list[DirectorProposal],
) -> DirectorComparison:
    if len(proposals) < 2:
        raise SphericalValidationError("Compare at least two director proposals")
    timeline_ids = {proposal.timeline_id for proposal in proposals}
    if len(timeline_ids) != 1:
        raise SphericalValidationError("Director proposals must share one timeline")
    proposal_ids = [proposal.proposal_id for proposal in proposals]
    if len(set(proposal_ids)) != len(proposal_ids):
        raise SphericalValidationError("Director proposal comparison contains duplicates")
    ordered = sorted(
        proposals,
        key=lambda proposal: (-proposal.aggregate_score, proposal.proposal_id),
    )
    ranking: list[ProposalComparisonItem] = []
    for rank, proposal in enumerate(ordered, start=1):
        mean_confidence = mean(
            decision.confidence for decision in proposal.decisions
        )
        strengths = [
            f"aggregate explainable score {proposal.aggregate_score:.3f}",
            f"motion constraints satisfied with {proposal.motion_validation.pan_count} pan(s)",
        ]
        tradeoffs: list[str] = []
        if proposal.motion_validation.cut_count:
            tradeoffs.append(
                f"uses {proposal.motion_validation.cut_count} cut(s)"
            )
        if mean_confidence < 0.6:
            tradeoffs.append("contains substantial low-confidence framing")
        if proposal.variant == DirectorVariant.VERTICAL_SOCIAL:
            tradeoffs.append("portrait framing can exclude peripheral subjects")
        ranking.append(
            ProposalComparisonItem(
                proposal_id=proposal.proposal_id,
                variant=proposal.variant,
                rank=rank,
                aggregate_score=proposal.aggregate_score,
                shot_count=len(proposal.decisions),
                cut_count=proposal.motion_validation.cut_count,
                pan_count=proposal.motion_validation.pan_count,
                mean_confidence=mean_confidence,
                strengths=strengths,
                tradeoffs=tradeoffs,
            )
        )
    comparison_key = sha256_json(
        {
            "timeline_id": ordered[0].timeline_id,
            "proposal_ids": proposal_ids,
            "ranking": [item.model_dump(mode="json") for item in ranking],
        }
    )
    recommended = ranking[0].proposal_id
    return DirectorComparison(
        comparison_id=f"comparison-{comparison_key[:16]}",
        timeline_id=ordered[0].timeline_id,
        proposal_ids=proposal_ids,
        ranking=ranking,
        recommended_proposal_id=recommended,
        summary=(
            f"Compared {len(proposals)} inspectable proposals; {recommended} ranks first "
            "by aggregate explainable score. Selection still requires explicit approval."
        ),
    )
