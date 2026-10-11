"""Seam-aware deterministic subject association and temporal smoothing."""

from __future__ import annotations

from dataclasses import dataclass, field
from collections.abc import Sequence

from open_storyline.spherical.analysis_interfaces import cosine_similarity
from open_storyline.spherical.analysis_schemas import (
    OcclusionGap,
    SeamCrossingEvent,
    SphericalDetection,
    SubjectSample,
    SubjectTrack,
    VisibilityState,
)
from open_storyline.spherical.geometry import (
    great_circle_angular_distance,
    normalize_angle,
    shortest_angle_delta,
)
from open_storyline.spherical.manifests import sha256_json


@dataclass
class _TrackState:
    subject_id: str
    samples: list[SubjectSample] = field(default_factory=list)
    embeddings: list[list[float]] = field(default_factory=list)
    labels: set[str] = field(default_factory=set)
    detector_backends: set[str] = field(default_factory=set)
    gaps: list[OcclusionGap] = field(default_factory=list)
    seam_events: list[SeamCrossingEvent] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    last_visible: SphericalDetection | None = None
    missing_since: float | None = None
    closed: bool = False


class GreedySphericalSubjectTracker:
    """Associate detections using spherical distance plus lightweight appearance."""

    name = "greedy-spherical-histogram-v1"

    def __init__(
        self,
        *,
        max_angular_distance_degrees: float = 45.0,
        max_occlusion_seconds: float = 1.5,
        minimum_embedding_similarity: float = 0.35,
        smoothing_alpha: float = 0.65,
        abrupt_motion_degrees: float = 30.0,
    ) -> None:
        self.max_angular_distance_degrees = max_angular_distance_degrees
        self.max_occlusion_seconds = max_occlusion_seconds
        self.minimum_embedding_similarity = minimum_embedding_similarity
        self.smoothing_alpha = smoothing_alpha
        self.abrupt_motion_degrees = abrupt_motion_degrees

    @staticmethod
    def _visible_sample(detection: SphericalDetection) -> SubjectSample:
        return SubjectSample(
            timestamp_seconds=detection.timestamp_seconds,
            yaw=detection.yaw,
            pitch=detection.pitch,
            angular_width=detection.angular_width,
            angular_height=detection.angular_height,
            confidence=detection.confidence,
            visibility=VisibilityState.VISIBLE,
            source_viewports=detection.source_viewports,
            identity_evidence=detection.identity_evidence,
        )

    def _new_state(self, detection: SphericalDetection) -> _TrackState:
        state = _TrackState(
            subject_id=f"subject-{sha256_json(detection.detection_id)[:12]}"
        )
        state.samples.append(self._visible_sample(detection))
        if detection.embedding:
            state.embeddings.append(detection.embedding)
        state.labels.update(detection.labels)
        state.detector_backends.update(detection.detector_backends)
        state.last_visible = detection
        return state

    def _match_cost(
        self,
        state: _TrackState,
        detection: SphericalDetection,
    ) -> float | None:
        if state.last_visible is None:
            return None
        elapsed = detection.timestamp_seconds - state.last_visible.timestamp_seconds
        if elapsed < 0 or elapsed > self.max_occlusion_seconds + 1e-9:
            return None
        distance = great_circle_angular_distance(
            state.last_visible.yaw,
            state.last_visible.pitch,
            detection.yaw,
            detection.pitch,
        )
        allowed_distance = self.max_angular_distance_degrees * max(1.0, elapsed)
        if distance > allowed_distance:
            return None
        similarity = cosine_similarity(
            state.embeddings[-1] if state.embeddings else [],
            detection.embedding,
        )
        if state.embeddings and detection.embedding and similarity < self.minimum_embedding_similarity:
            return None
        return distance / max(1.0, allowed_distance) + (1.0 - max(0.0, similarity)) * 0.25

    def _append_match(
        self,
        state: _TrackState,
        detection: SphericalDetection,
    ) -> None:
        assert state.last_visible is not None
        prior = state.samples[-1]
        raw_distance = great_circle_angular_distance(
            prior.yaw, prior.pitch, detection.yaw, detection.pitch
        )
        evidence = dict(detection.identity_evidence)
        if raw_distance <= self.abrupt_motion_degrees:
            yaw = normalize_angle(
                prior.yaw
                + shortest_angle_delta(prior.yaw, detection.yaw) * self.smoothing_alpha
            )
            pitch = prior.pitch + (detection.pitch - prior.pitch) * self.smoothing_alpha
            angular_width = (
                prior.angular_width
                + (detection.angular_width - prior.angular_width) * self.smoothing_alpha
            )
            angular_height = (
                prior.angular_height
                + (detection.angular_height - prior.angular_height) * self.smoothing_alpha
            )
            evidence["temporal_smoothing"] = self.smoothing_alpha
        else:
            yaw = detection.yaw
            pitch = detection.pitch
            angular_width = detection.angular_width
            angular_height = detection.angular_height
            evidence["temporal_smoothing"] = "bypassed_abrupt_motion"
            state.warnings.append(
                f"Abrupt {raw_distance:.1f}-degree movement at "
                f"{detection.timestamp_seconds:.3f}s was preserved."
            )
        if abs(state.last_visible.yaw - detection.yaw) > 180.0:
            state.seam_events.append(
                SeamCrossingEvent(
                    timestamp_seconds=detection.timestamp_seconds,
                    from_yaw=state.last_visible.yaw,
                    to_yaw=detection.yaw,
                )
            )
        if state.missing_since is not None:
            state.gaps.append(
                OcclusionGap(
                    start_seconds=state.missing_since,
                    end_seconds=detection.timestamp_seconds,
                    duration_seconds=detection.timestamp_seconds - state.missing_since,
                )
            )
            state.warnings.append(
                f"Tracking resumed after an occlusion ending at "
                f"{detection.timestamp_seconds:.3f}s."
            )
            state.missing_since = None
        state.samples.append(
            SubjectSample(
                timestamp_seconds=detection.timestamp_seconds,
                yaw=yaw,
                pitch=pitch,
                angular_width=max(0.001, angular_width),
                angular_height=max(0.001, angular_height),
                confidence=detection.confidence,
                visibility=VisibilityState.VISIBLE,
                source_viewports=detection.source_viewports,
                identity_evidence=evidence,
            )
        )
        if detection.embedding:
            state.embeddings.append(detection.embedding)
        state.labels.update(detection.labels)
        state.detector_backends.update(detection.detector_backends)
        state.last_visible = detection

    def _append_missing(self, state: _TrackState, timestamp: float) -> None:
        if state.last_visible is None or state.closed:
            return
        elapsed = timestamp - state.last_visible.timestamp_seconds
        if state.missing_since is None:
            state.missing_since = timestamp
        prior = state.samples[-1]
        if elapsed <= self.max_occlusion_seconds + 1e-9:
            state.samples.append(
                SubjectSample(
                    timestamp_seconds=timestamp,
                    yaw=prior.yaw,
                    pitch=prior.pitch,
                    angular_width=prior.angular_width,
                    angular_height=prior.angular_height,
                    confidence=max(0.0, prior.confidence * 0.5),
                    visibility=VisibilityState.OCCLUDED,
                    source_viewports=[],
                    identity_evidence={"prediction": "last_visible_position"},
                )
            )
        else:
            state.samples.append(
                SubjectSample(
                    timestamp_seconds=timestamp,
                    yaw=prior.yaw,
                    pitch=prior.pitch,
                    angular_width=prior.angular_width,
                    angular_height=prior.angular_height,
                    confidence=0.0,
                    visibility=VisibilityState.LOST,
                    source_viewports=[],
                    identity_evidence={"reason": "maximum_occlusion_exceeded"},
                )
            )
            state.warnings.append(
                f"Tracking was lost after {self.max_occlusion_seconds:.3f}s of occlusion."
            )
            state.closed = True

    def track(
        self,
        asset_id: str,
        detections: Sequence[SphericalDetection],
        timestamps: Sequence[float],
    ) -> list[SubjectTrack]:
        grouped: dict[float, list[SphericalDetection]] = {}
        for detection in detections:
            grouped.setdefault(detection.timestamp_seconds, []).append(detection)
        states: list[_TrackState] = []
        for timestamp in sorted(set(timestamps)):
            current = grouped.get(timestamp, [])
            candidates: list[tuple[float, int, int]] = []
            for state_index, state in enumerate(states):
                if state.closed:
                    continue
                for detection_index, detection in enumerate(current):
                    cost = self._match_cost(state, detection)
                    if cost is not None:
                        candidates.append((cost, state_index, detection_index))
            matched_states: set[int] = set()
            matched_detections: set[int] = set()
            for _, state_index, detection_index in sorted(candidates):
                if state_index in matched_states or detection_index in matched_detections:
                    continue
                self._append_match(states[state_index], current[detection_index])
                matched_states.add(state_index)
                matched_detections.add(detection_index)
            for state_index, state in enumerate(states):
                if state_index not in matched_states:
                    self._append_missing(state, timestamp)
            for detection_index, detection in enumerate(current):
                if detection_index not in matched_detections:
                    states.append(self._new_state(detection))

        tracks: list[SubjectTrack] = []
        for state in states:
            visible = [
                sample
                for sample in state.samples
                if sample.visibility == VisibilityState.VISIBLE
            ]
            if not visible:
                continue
            mean_confidence = sum(sample.confidence for sample in visible) / len(visible)
            tracks.append(
                SubjectTrack(
                    subject_id=state.subject_id,
                    asset_id=asset_id,
                    start_seconds=state.samples[0].timestamp_seconds,
                    end_seconds=state.samples[-1].timestamp_seconds,
                    samples=state.samples,
                    mean_confidence=mean_confidence,
                    identity_evidence={
                        "labels": sorted(state.labels),
                        "detector_backends": sorted(state.detector_backends),
                        "appearance_samples": len(state.embeddings),
                    },
                    occlusion_gaps=state.gaps,
                    seam_crossing_events=state.seam_events,
                    warnings=list(dict.fromkeys(state.warnings)),
                )
            )
        return sorted(tracks, key=lambda track: track.subject_id)
