"""Explainable speaker-candidate scoring over normalized spherical timelines."""

from __future__ import annotations

from statistics import mean

from open_storyline.spherical.analysis_schemas import (
    SphericalAnalysis,
    SubjectSample,
    SubjectTrack,
    VisibilityState,
)
from open_storyline.spherical.direction_schemas import (
    NormalizedStoryTimeline,
    SpeakerAssessmentStatus,
    SpeakerCandidateAnalysis,
    SpeakerCandidateScore,
    SpeakerScoreComponents,
    SpeakerTurnAssessment,
    TimelineSignal,
    TimelineSignalKind,
    TranscriptTurn,
)
from open_storyline.spherical.manifests import sha256_json


SPEAKER_COMPONENT_WEIGHTS = {
    "voice_activity": 0.05,
    "visible_face_activity": 0.17,
    "mouth_movement": 0.26,
    "subject_continuity": 0.14,
    "scene_context": 0.08,
    "previous_speaker": 0.07,
    "transcript_turn_boundary": 0.08,
    "track_confidence": 0.15,
}


def _overlaps(start: float, end: float, other_start: float, other_end: float) -> bool:
    return end > other_start and start < other_end


def _overlap_amount(start: float, end: float, signal: TimelineSignal) -> float:
    return max(0.0, min(end, signal.end_seconds) - max(start, signal.start_seconds))


def _matching_signals(
    timeline: NormalizedStoryTimeline,
    kind: TimelineSignalKind,
    turn: TranscriptTurn,
    subject_id: str | None = None,
) -> list[TimelineSignal]:
    return [
        signal
        for signal in timeline.signals
        if signal.kind == kind
        and (subject_id is None or signal.subject_id == subject_id)
        and _overlaps(
            turn.start_seconds,
            turn.end_seconds,
            signal.start_seconds,
            signal.end_seconds,
        )
    ]


def _coverage(signals: list[TimelineSignal], turn: TranscriptTurn) -> float:
    duration = max(0.001, turn.end_seconds - turn.start_seconds)
    covered = sum(_overlap_amount(turn.start_seconds, turn.end_seconds, signal) for signal in signals)
    return max(0.0, min(1.0, covered / duration))


def _visible_samples(track: SubjectTrack, turn: TranscriptTurn) -> list[SubjectSample]:
    return [
        sample
        for sample in track.samples
        if sample.visibility == VisibilityState.VISIBLE
        and turn.start_seconds - 0.25 <= sample.timestamp_seconds <= turn.end_seconds + 0.25
    ]


def _mean_confidence(signals: list[TimelineSignal], default: float = 0.0) -> float:
    values = [signal.confidence for signal in signals if signal.confidence is not None]
    return mean(values) if values else default


def _score_candidate(
    track: SubjectTrack,
    turn: TranscriptTurn,
    timeline: NormalizedStoryTimeline,
    previous_speaker: str | None,
) -> SpeakerCandidateScore | None:
    samples = _visible_samples(track, turn)
    subject_signals = _matching_signals(
        timeline,
        TimelineSignalKind.SUBJECT,
        turn,
        track.subject_id,
    )
    if not samples and not subject_signals:
        return None
    audio_signals = _matching_signals(
        timeline,
        TimelineSignalKind.AUDIO_ACTIVITY,
        turn,
    )
    face_signals = _matching_signals(
        timeline,
        TimelineSignalKind.FACE_ACTIVITY,
        turn,
        track.subject_id,
    )
    mouth_signals = _matching_signals(
        timeline,
        TimelineSignalKind.MOUTH_ACTIVITY,
        turn,
        track.subject_id,
    )
    scene_signals = _matching_signals(
        timeline,
        TimelineSignalKind.SCENE,
        turn,
    )
    voice_activity = min(1.0, _coverage(audio_signals, turn) * 0.7 + _mean_confidence(audio_signals) * 0.3)
    visible_face = min(1.0, _coverage(face_signals, turn) * 0.65 + _mean_confidence(face_signals) * 0.35)
    mouth = min(1.0, _coverage(mouth_signals, turn) * 0.5 + _mean_confidence(mouth_signals) * 0.5)
    continuity = min(1.0, _coverage(subject_signals, turn))
    scene_context = min(
        1.0,
        (0.45 if samples else 0.2) + _coverage(scene_signals, turn) * 0.55,
    )
    previous = 1.0 if previous_speaker == track.subject_id else (0.35 if previous_speaker is None else 0.0)
    nearest_start = min(
        (abs(sample.timestamp_seconds - turn.start_seconds) for sample in samples),
        default=10.0,
    )
    boundary = max(0.0, min(1.0, 1.0 - nearest_start / 0.75))
    sample_confidence = mean(sample.confidence for sample in samples) if samples else 0.0
    track_confidence = min(1.0, track.mean_confidence * 0.6 + sample_confidence * 0.4)
    components = SpeakerScoreComponents(
        voice_activity=voice_activity,
        visible_face_activity=visible_face,
        mouth_movement=mouth,
        subject_continuity=continuity,
        scene_context=scene_context,
        previous_speaker=previous,
        transcript_turn_boundary=boundary,
        track_confidence=track_confidence,
    )
    total = sum(
        getattr(components, name) * weight
        for name, weight in SPEAKER_COMPONENT_WEIGHTS.items()
    )
    evidence = [
        f"voice_activity={voice_activity:.3f}",
        f"visible_face_activity={visible_face:.3f}",
        f"mouth_movement={mouth:.3f}",
        f"subject_continuity={continuity:.3f}",
        f"track_confidence={track_confidence:.3f}",
    ]
    warnings: list[str] = []
    if not mouth_signals:
        warnings.append("No mouth-movement evidence was available for this candidate.")
    if not face_signals:
        warnings.append("No face-activity evidence was available for this candidate.")
    return SpeakerCandidateScore(
        subject_id=track.subject_id,
        total_score=max(0.0, min(1.0, total)),
        components=components,
        evidence=evidence,
        warnings=warnings,
    )


def analyze_speaker_candidates(
    timeline: NormalizedStoryTimeline,
    analysis: SphericalAnalysis,
    *,
    profile_id: str = "custom",
    probable_threshold: float = 0.68,
    ambiguity_margin: float = 0.12,
) -> SpeakerCandidateAnalysis:
    """Rank candidates without asserting identity when evidence is weak."""

    if timeline.analysis_id != analysis.analysis_id or timeline.asset_id != analysis.asset_id:
        raise ValueError("normalized timeline and spherical analysis do not match")
    assessments: list[SpeakerTurnAssessment] = []
    warnings: list[str] = []
    previous_speaker: str | None = None
    mouth_available = any(
        signal.kind == TimelineSignalKind.MOUTH_ACTIVITY for signal in timeline.signals
    )
    if not mouth_available:
        warnings.append(
            "Mouth movement was unavailable; no speaker is confirmed from face visibility alone."
        )
    for turn in timeline.transcript_turns:
        candidates = [
            candidate
            for track in analysis.subject_tracks
            if (
                candidate := _score_candidate(
                    track,
                    turn,
                    timeline,
                    previous_speaker,
                )
            )
            is not None
        ]
        candidates.sort(key=lambda candidate: (-candidate.total_score, candidate.subject_id))
        top_score = candidates[0].total_score if candidates else 0.0
        margin = (
            top_score - candidates[1].total_score
            if len(candidates) > 1
            else top_score
        )
        turn_warnings: list[str] = []
        selected: str | None = None
        if not candidates:
            status = SpeakerAssessmentStatus.UNRESOLVED
            turn_warnings.append("No visible subject candidate overlaps this transcript turn.")
        elif top_score < probable_threshold:
            status = SpeakerAssessmentStatus.UNRESOLVED
            turn_warnings.append(
                f"Top candidate score {top_score:.3f} is below the probable threshold {probable_threshold:.3f}."
            )
        elif len(candidates) > 1 and margin < ambiguity_margin:
            status = SpeakerAssessmentStatus.AMBIGUOUS
            turn_warnings.append(
                f"Top-candidate margin {margin:.3f} is below ambiguity margin {ambiguity_margin:.3f}."
            )
        else:
            status = SpeakerAssessmentStatus.PROBABLE
            selected = candidates[0].subject_id
            previous_speaker = selected
        assessments.append(
            SpeakerTurnAssessment(
                turn_id=turn.turn_id,
                start_seconds=turn.start_seconds,
                end_seconds=turn.end_seconds,
                text=turn.text,
                status=status,
                selected_subject_id=selected,
                confidence=top_score,
                candidates=candidates,
                warnings=turn_warnings,
            )
        )
    if timeline.transcript_turns and not assessments:
        warnings.append("No speaker assessments could be constructed.")
    unresolved = sum(
        assessment.status != SpeakerAssessmentStatus.PROBABLE
        for assessment in assessments
    )
    if unresolved:
        warnings.append(
            f"{unresolved} transcript turn(s) remain ambiguous or unresolved; alternatives are preserved."
        )
    identity_payload = {
        "timeline_id": timeline.timeline_id,
        "analysis_id": analysis.analysis_id,
        "profile_id": profile_id,
        "probable_threshold": probable_threshold,
        "ambiguity_margin": ambiguity_margin,
        "turns": [assessment.model_dump(mode="json") for assessment in assessments],
    }
    return SpeakerCandidateAnalysis(
        speaker_analysis_id=f"speaker-analysis-{sha256_json(identity_payload)[:16]}",
        timeline_id=timeline.timeline_id,
        analysis_id=analysis.analysis_id,
        asset_id=analysis.asset_id,
        profile_id=profile_id,
        turns=assessments,
        probable_threshold=probable_threshold,
        ambiguity_margin=ambiguity_margin,
        warnings=list(dict.fromkeys(warnings)),
    )
