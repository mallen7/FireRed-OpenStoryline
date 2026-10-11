"""Inspectable assisted-camera policies built from spherical subject tracks."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import numpy as np

from open_storyline.spherical.analysis_schemas import (
    ReframeSuggestion,
    SphericalAnalysis,
    SubjectSample,
    SubjectTrack,
    SuggestionPolicy,
    VisibilityState,
)
from open_storyline.spherical.errors import SphericalValidationError
from open_storyline.spherical.geometry import (
    great_circle_angular_distance,
    normalize_angle,
    shortest_angle_delta,
)
from open_storyline.spherical.manifests import atomic_write_json, sha256_json
from open_storyline.spherical.schemas import (
    CameraKeyframe,
    Easing,
    SphericalAsset,
    TrackCreatedBy,
    VirtualCameraTrack,
)


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def _visible(track: SubjectTrack) -> list[SubjectSample]:
    return [sample for sample in track.samples if sample.visibility == VisibilityState.VISIBLE]


def _find_subject(analysis: SphericalAnalysis, subject_id: str) -> SubjectTrack:
    for track in analysis.subject_tracks:
        if track.subject_id == subject_id:
            return track
    raise SphericalValidationError(
        "Detected subject was not found in the selected analysis.",
        details={"subject_id": subject_id, "analysis_id": analysis.analysis_id},
    )


def _spherical_mean(samples: list[SubjectSample]) -> tuple[float, float]:
    vector = np.zeros(3, dtype=np.float64)
    for sample in samples:
        yaw = math.radians(sample.yaw)
        pitch = math.radians(sample.pitch)
        weight = max(0.001, sample.confidence)
        vector += weight * np.asarray(
            [math.cos(pitch) * math.cos(yaw), math.cos(pitch) * math.sin(yaw), math.sin(pitch)]
        )
    if math.isclose(float(np.linalg.norm(vector)), 0.0):
        return 0.0, 0.0
    vector /= np.linalg.norm(vector)
    return (
        normalize_angle(math.degrees(math.atan2(vector[1], vector[0]))),
        math.degrees(math.asin(max(-1.0, min(1.0, float(vector[2]))))),
    )


def _near_seam(yaw: float, seam_yaws: list[float], width: float) -> bool:
    return any(abs(shortest_angle_delta(yaw, seam)) < width for seam in seam_yaws)


def _avoid_seam(yaw: float, seam_yaws: list[float], width: float) -> float:
    adjusted = normalize_angle(yaw)
    for seam in seam_yaws:
        delta = shortest_angle_delta(seam, adjusted)
        if abs(delta) < width:
            direction = 1.0 if delta >= 0 else -1.0
            adjusted = normalize_angle(seam + direction * width)
    return adjusted


def _track_interval(
    asset: SphericalAsset,
    samples: list[SubjectSample],
) -> tuple[float, float]:
    sample_times = sorted(sample.timestamp_seconds for sample in samples)
    start = sample_times[0] if sample_times else 0.0
    end = sample_times[-1] if sample_times else asset.duration_seconds
    if end <= start:
        end = min(asset.duration_seconds, start + max(1.0 / asset.fps, 0.05))
    if end <= start:
        start = 0.0
        end = asset.duration_seconds
    return start, end


def _ensure_boundary_keyframes(
    keyframes: list[CameraKeyframe],
    start: float,
    end: float,
) -> list[CameraKeyframe]:
    if not keyframes:
        raise SphericalValidationError("Suggestion policy produced no camera keyframes")
    result = sorted(keyframes, key=lambda frame: frame.time_seconds)
    deduplicated: dict[float, CameraKeyframe] = {
        frame.time_seconds: frame for frame in result
    }
    result = [deduplicated[timestamp] for timestamp in sorted(deduplicated)]
    if result[0].time_seconds > start:
        result.insert(0, result[0].model_copy(update={"time_seconds": start}))
    if result[-1].time_seconds < end:
        result.append(result[-1].model_copy(update={"time_seconds": end}))
    if len(result) == 1:
        result.append(result[0].model_copy(update={"time_seconds": end}))
    return result


def _subject_keyframes(
    subject: SubjectTrack,
    *,
    placement: str,
    horizontal_fov: float | None,
    minimum_fov: float,
    maximum_fov: float,
    avoid_nadir_below_pitch: float,
    seam_yaws: list[float],
    seam_zone_width: float,
) -> tuple[list[CameraKeyframe], list[str]]:
    samples = _visible(subject)
    if not samples:
        raise SphericalValidationError("Selected subject has no visible samples")
    warnings: list[str] = []
    placement_offset = {"center": 0.0, "left_third": 1.0, "right_third": -1.0}.get(
        placement
    )
    if placement_offset is None:
        raise SphericalValidationError("placement must be center, left_third, or right_third")
    keyframes: list[CameraKeyframe] = []
    for sample in samples:
        fov = (
            horizontal_fov
            if horizontal_fov is not None
            else _clamp(sample.angular_width * 3.2, minimum_fov, maximum_fov)
        )
        yaw = normalize_angle(sample.yaw + placement_offset * fov / 6.0)
        pitch = max(avoid_nadir_below_pitch, sample.pitch)
        if _near_seam(yaw, seam_yaws, seam_zone_width):
            warnings.append(
                f"Subject framing approaches a configured stitch-seam zone at "
                f"{sample.timestamp_seconds:.3f}s; subject position was preserved."
            )
        keyframes.append(
            CameraKeyframe(
                time_seconds=sample.timestamp_seconds,
                yaw=yaw,
                pitch=pitch,
                horizontal_fov=fov,
                easing=Easing.SMOOTHSTEP,
                subject_id=subject.subject_id,
                confidence=sample.confidence,
            )
        )
    return keyframes, warnings


def _group_keyframes(
    tracks: list[SubjectTrack],
    *,
    minimum_fov: float,
    maximum_fov: float,
    avoid_nadir_below_pitch: float,
    seam_yaws: list[float],
    seam_zone_width: float,
) -> tuple[list[CameraKeyframe], list[str]]:
    by_time: dict[float, list[SubjectSample]] = {}
    for track in tracks:
        for sample in _visible(track):
            by_time.setdefault(sample.timestamp_seconds, []).append(sample)
    if not by_time:
        raise SphericalValidationError("No visible subjects are available for group framing")
    warnings: list[str] = []
    keyframes: list[CameraKeyframe] = []
    for timestamp in sorted(by_time):
        samples = by_time[timestamp]
        yaw, pitch = _spherical_mean(samples)
        maximum_distance = max(
            great_circle_angular_distance(yaw, pitch, sample.yaw, sample.pitch)
            + sample.angular_width / 2.0
            for sample in samples
        )
        fov = _clamp(maximum_distance * 2.4, minimum_fov, maximum_fov)
        if _near_seam(yaw, seam_yaws, seam_zone_width):
            adjusted = _avoid_seam(yaw, seam_yaws, seam_zone_width)
            if all(
                great_circle_angular_distance(adjusted, pitch, sample.yaw, sample.pitch)
                <= fov / 2.0
                for sample in samples
            ):
                yaw = adjusted
                warnings.append(
                    f"Group camera center was shifted away from a stitch-seam zone at {timestamp:.3f}s."
                )
            else:
                warnings.append(
                    f"Group framing overlaps a stitch-seam zone at {timestamp:.3f}s."
                )
        keyframes.append(
            CameraKeyframe(
                time_seconds=timestamp,
                yaw=yaw,
                pitch=max(avoid_nadir_below_pitch, pitch),
                horizontal_fov=fov,
                easing=Easing.SMOOTHSTEP,
                confidence=sum(sample.confidence for sample in samples) / len(samples),
            )
        )
    return keyframes, warnings


def generate_reframe_suggestion(
    analysis: SphericalAnalysis,
    asset: SphericalAsset,
    *,
    policy: SuggestionPolicy,
    destination_dir: str | Path,
    subject_ids: list[str] | None = None,
    output_width: int = 1920,
    output_height: int = 1080,
    parameters: dict[str, Any] | None = None,
) -> ReframeSuggestion:
    """Generate an inspectable proposal; this function never activates a camera plan."""

    if analysis.asset_id != asset.asset_id:
        raise SphericalValidationError("Analysis and asset identities do not match")
    params = dict(parameters or {})
    selected_ids = list(subject_ids or [])
    minimum_fov = float(params.get("minimum_fov", 55.0))
    maximum_fov = float(params.get("maximum_fov", 120.0))
    avoid_nadir = float(params.get("avoid_nadir_below_pitch", -60.0))
    seam_yaws = [float(value) for value in params.get("stitch_seam_yaws", [180.0])]
    seam_width = float(params.get("stitch_seam_zone_width", 12.0))
    horizontal_fov = params.get("horizontal_fov")
    horizontal_fov = float(horizontal_fov) if horizontal_fov is not None else None
    warnings: list[str] = []

    if policy in {SuggestionPolicy.SUBJECT_CENTER, SuggestionPolicy.RULE_OF_THIRDS}:
        if len(selected_ids) != 1:
            raise SphericalValidationError("Subject-follow suggestions require one subject_id")
        subject = _find_subject(analysis, selected_ids[0])
        placement = str(
            params.get(
                "placement",
                "center" if policy == SuggestionPolicy.SUBJECT_CENTER else "left_third",
            )
        )
        keyframes, policy_warnings = _subject_keyframes(
            subject,
            placement=placement,
            horizontal_fov=horizontal_fov,
            minimum_fov=minimum_fov,
            maximum_fov=maximum_fov,
            avoid_nadir_below_pitch=avoid_nadir,
            seam_yaws=seam_yaws,
            seam_zone_width=seam_width,
        )
        samples = _visible(subject)
        confidence = subject.mean_confidence
        start, end = _track_interval(asset, samples)
        warnings.extend(policy_warnings)
    elif policy == SuggestionPolicy.LARGEST_PERSON:
        candidates = [
            track
            for track in analysis.subject_tracks
            if "person-candidate" in track.identity_evidence.get("labels", [])
        ] or analysis.subject_tracks
        if not candidates:
            raise SphericalValidationError("No detected subject is available")
        subject = max(
            candidates,
            key=lambda track: max(
                sample.angular_width * sample.angular_height
                for sample in _visible(track)
            ),
        )
        selected_ids = [subject.subject_id]
        keyframes, policy_warnings = _subject_keyframes(
            subject,
            placement=str(params.get("placement", "center")),
            horizontal_fov=horizontal_fov,
            minimum_fov=minimum_fov,
            maximum_fov=maximum_fov,
            avoid_nadir_below_pitch=avoid_nadir,
            seam_yaws=seam_yaws,
            seam_zone_width=seam_width,
        )
        samples = _visible(subject)
        confidence = subject.mean_confidence
        start, end = _track_interval(asset, samples)
        warnings.extend(policy_warnings)
    elif policy == SuggestionPolicy.GROUP:
        tracks = (
            [_find_subject(analysis, subject_id) for subject_id in selected_ids]
            if selected_ids
            else analysis.subject_tracks
        )
        selected_ids = [track.subject_id for track in tracks]
        keyframes, policy_warnings = _group_keyframes(
            tracks,
            minimum_fov=minimum_fov,
            maximum_fov=maximum_fov,
            avoid_nadir_below_pitch=avoid_nadir,
            seam_yaws=seam_yaws,
            seam_zone_width=seam_width,
        )
        samples = [sample for track in tracks for sample in _visible(track)]
        confidence = sum(track.mean_confidence for track in tracks) / len(tracks)
        start, end = _track_interval(asset, samples)
        warnings.extend(policy_warnings)
    elif policy == SuggestionPolicy.WIDE_ESTABLISHING:
        samples = [sample for track in analysis.subject_tracks for sample in _visible(track)]
        if samples:
            yaw, pitch = _spherical_mean(samples)
            confidence = sum(sample.confidence for sample in samples) / len(samples)
        else:
            yaw, pitch, confidence = 0.0, 0.0, 0.25
            warnings.append("No detected subjects were available; establishing view uses front.")
        yaw = _avoid_seam(yaw, seam_yaws, seam_width)
        start, end = 0.0, asset.duration_seconds
        keyframes = [
            CameraKeyframe(
                time_seconds=start,
                yaw=yaw,
                pitch=max(avoid_nadir, pitch),
                horizontal_fov=float(params.get("horizontal_fov", 120.0)),
                confidence=confidence,
            ),
            CameraKeyframe(
                time_seconds=end,
                yaw=yaw,
                pitch=max(avoid_nadir, pitch),
                horizontal_fov=float(params.get("horizontal_fov", 120.0)),
                confidence=confidence,
            ),
        ]
    elif policy == SuggestionPolicy.PAN_SUBJECTS:
        if len(selected_ids) != 2:
            raise SphericalValidationError("Pan suggestions require two subject_ids")
        first = _visible(_find_subject(analysis, selected_ids[0]))
        second = _visible(_find_subject(analysis, selected_ids[1]))
        if not first or not second:
            raise SphericalValidationError("Both pan subjects require visible samples")
        start, end = float(params.get("start_seconds", 0.0)), float(
            params.get("end_seconds", asset.duration_seconds)
        )
        keyframes = [
            CameraKeyframe(
                time_seconds=start,
                yaw=first[0].yaw,
                pitch=max(avoid_nadir, first[0].pitch),
                horizontal_fov=float(params.get("horizontal_fov", 80.0)),
                easing=Easing.SMOOTHSTEP,
                subject_id=selected_ids[0],
                confidence=first[0].confidence,
            ),
            CameraKeyframe(
                time_seconds=end,
                yaw=second[-1].yaw,
                pitch=max(avoid_nadir, second[-1].pitch),
                horizontal_fov=float(params.get("horizontal_fov", 80.0)),
                subject_id=selected_ids[1],
                confidence=second[-1].confidence,
            ),
        ]
        confidence = min(first[0].confidence, second[-1].confidence)
    elif policy == SuggestionPolicy.STATIC_DIRECTION:
        start, end = 0.0, asset.duration_seconds
        yaw = _avoid_seam(float(params.get("yaw", 0.0)), seam_yaws, seam_width)
        pitch = max(avoid_nadir, float(params.get("pitch", 0.0)))
        fov = float(params.get("horizontal_fov", 100.0))
        confidence = 1.0
        keyframes = [
            CameraKeyframe(time_seconds=start, yaw=yaw, pitch=pitch, horizontal_fov=fov),
            CameraKeyframe(time_seconds=end, yaw=yaw, pitch=pitch, horizontal_fov=fov),
        ]
    else:
        raise SphericalValidationError("Unsupported suggestion policy")

    keyframes = _ensure_boundary_keyframes(keyframes, start, end)
    suggestion_key = sha256_json(
        {
            "analysis_id": analysis.analysis_id,
            "asset_id": asset.asset_id,
            "policy": policy.value,
            "subject_ids": selected_ids,
            "parameters": params,
            "output_width": output_width,
            "output_height": output_height,
            "keyframes": [frame.model_dump(mode="json") for frame in keyframes],
        }
    )
    suggestion_id = f"suggestion-{suggestion_key[:16]}"
    track = VirtualCameraTrack(
        track_id=f"camera-{suggestion_key[:16]}",
        asset_id=asset.asset_id,
        name=f"assisted-{policy.value}",
        output_width=output_width,
        output_height=output_height,
        source_start_seconds=start,
        source_end_seconds=end,
        keyframes=keyframes,
        created_by=TrackCreatedBy.ASSISTED,
        generation_metadata={
            "analysis_id": analysis.analysis_id,
            "suggestion_id": suggestion_id,
            "analysis_backend": analysis.backend.model_dump(mode="json"),
            "policy": policy.value,
            "confidence": confidence,
            "parameters": params,
            "warnings": list(dict.fromkeys(warnings)),
        },
    )
    destination = Path(destination_dir).expanduser().resolve(strict=False)
    destination.mkdir(parents=True, exist_ok=True)
    trajectory_path = destination / "camera-trajectory.json"
    atomic_write_json(
        trajectory_path,
        {
            "schema_version": "1.0",
            "suggestion_id": suggestion_id,
            "analysis_id": analysis.analysis_id,
            "policy": policy.value,
            "keyframes": [frame.model_dump(mode="json") for frame in keyframes],
        },
    )
    return ReframeSuggestion(
        suggestion_id=suggestion_id,
        analysis_id=analysis.analysis_id,
        asset_id=asset.asset_id,
        policy=policy,
        subject_ids=selected_ids,
        camera_track=track,
        analysis_backend=analysis.backend,
        confidence=confidence,
        parameters=params,
        camera_trajectory_path=trajectory_path,
        warnings=list(dict.fromkeys(warnings)),
    )
