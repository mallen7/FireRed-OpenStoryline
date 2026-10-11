"""Geometry and interpolation utilities for virtual spherical cameras."""

from __future__ import annotations

import math
from bisect import bisect_right
from collections.abc import Callable

from open_storyline.spherical.schemas import (
    CameraKeyframe,
    Easing,
    VirtualCameraTrack,
)


def normalize_angle(angle: float) -> float:
    """Normalize an angle to the canonical inclusive -180..180 range."""

    if not math.isfinite(angle):
        raise ValueError("angle must be finite")
    wrapped = (angle + 180.0) % 360.0 - 180.0
    if math.isclose(wrapped, -180.0, abs_tol=1e-12) and angle > 0:
        return 180.0
    if math.isclose(wrapped, 0.0, abs_tol=1e-12):
        return 0.0
    return wrapped


def shortest_angle_delta(start: float, end: float) -> float:
    """Return the signed shortest rotation from start to end in degrees."""

    return normalize_angle(normalize_angle(end) - normalize_angle(start))


def _clamp_unit(value: float) -> float:
    if not math.isfinite(value):
        raise ValueError("interpolation amount must be finite")
    return max(0.0, min(1.0, value))


def linear_interpolate(start: float, end: float, amount: float) -> float:
    amount = _clamp_unit(amount)
    return start + (end - start) * amount


def smooth_step(amount: float) -> float:
    amount = _clamp_unit(amount)
    return amount * amount * (3.0 - 2.0 * amount)


def ease_in(amount: float) -> float:
    amount = _clamp_unit(amount)
    return amount * amount


def ease_out(amount: float) -> float:
    amount = _clamp_unit(amount)
    return 1.0 - (1.0 - amount) * (1.0 - amount)


def ease_in_out(amount: float) -> float:
    amount = _clamp_unit(amount)
    if amount < 0.5:
        return 2.0 * amount * amount
    return 1.0 - ((-2.0 * amount + 2.0) ** 2) / 2.0


EASING_FUNCTIONS: dict[Easing, Callable[[float], float]] = {
    Easing.LINEAR: _clamp_unit,
    Easing.SMOOTHSTEP: smooth_step,
    Easing.EASE_IN: ease_in,
    Easing.EASE_OUT: ease_out,
    Easing.EASE_IN_OUT: ease_in_out,
}


def interpolate_yaw(start: float, end: float, amount: float) -> float:
    """Interpolate yaw across the shortest path, including across the seam."""

    amount = _clamp_unit(amount)
    return normalize_angle(start + shortest_angle_delta(start, end) * amount)


def equirectangular_pixel_to_angles(
    x: float,
    y: float,
    width: int,
    height: int,
) -> tuple[float, float]:
    """Map an equirectangular pixel coordinate to canonical yaw and pitch."""

    if width <= 0 or height <= 0:
        raise ValueError("width and height must be positive")
    if not (0.0 <= x <= width and 0.0 <= y <= height):
        raise ValueError("pixel coordinate is outside the image")
    yaw = normalize_angle((x / width) * 360.0 - 180.0)
    pitch = 90.0 - (y / height) * 180.0
    return yaw, pitch


def angles_to_equirectangular_pixel(
    yaw: float,
    pitch: float,
    width: int,
    height: int,
) -> tuple[float, float]:
    """Map canonical yaw and pitch to an equirectangular pixel coordinate."""

    if width <= 0 or height <= 0:
        raise ValueError("width and height must be positive")
    if not math.isfinite(pitch) or not -90.0 <= pitch <= 90.0:
        raise ValueError("pitch must be between -90 and 90 degrees")
    normalized_yaw = normalize_angle(yaw)
    x = ((normalized_yaw + 180.0) / 360.0) * width
    y = ((90.0 - pitch) / 180.0) * height
    return x, y


def great_circle_angular_distance(
    yaw_a: float,
    pitch_a: float,
    yaw_b: float,
    pitch_b: float,
) -> float:
    """Return great-circle distance between two spherical points in degrees."""

    for pitch in (pitch_a, pitch_b):
        if not math.isfinite(pitch) or not -90.0 <= pitch <= 90.0:
            raise ValueError("pitch must be between -90 and 90 degrees")

    lon_delta = math.radians(shortest_angle_delta(yaw_a, yaw_b))
    lat_a = math.radians(pitch_a)
    lat_b = math.radians(pitch_b)
    cosine = (
        math.sin(lat_a) * math.sin(lat_b)
        + math.cos(lat_a) * math.cos(lat_b) * math.cos(lon_delta)
    )
    return math.degrees(math.acos(max(-1.0, min(1.0, cosine))))


def validate_output_aspect_ratio(width: int, height: int) -> str:
    """Validate an output aspect and return its canonical name."""

    if width <= 0 or height <= 0:
        raise ValueError("width and height must be positive")
    ratio = width / height
    for name, expected in (
        ("16:9", 16.0 / 9.0),
        ("9:16", 9.0 / 16.0),
        ("1:1", 1.0),
    ):
        if math.isclose(ratio, expected, rel_tol=0.01, abs_tol=0.01):
            return name
    raise ValueError("output must use a supported 16:9, 9:16, or 1:1 aspect ratio")


def _interpolate_optional_confidence(
    start: float | None,
    end: float | None,
    amount: float,
) -> float | None:
    if start is None and end is None:
        return None
    if start is None:
        return end
    if end is None:
        return start
    return linear_interpolate(start, end, amount)


def sample_camera_track(
    track: VirtualCameraTrack,
    time_seconds: float,
) -> CameraKeyframe:
    """Sample a validated camera track at an absolute source timestamp."""

    if not track.source_start_seconds <= time_seconds <= track.source_end_seconds:
        raise ValueError("sample time is outside the camera track interval")

    keyframes = track.keyframes
    if time_seconds <= keyframes[0].time_seconds:
        return keyframes[0].model_copy(update={"time_seconds": time_seconds})
    if time_seconds >= keyframes[-1].time_seconds:
        return keyframes[-1].model_copy(update={"time_seconds": time_seconds})

    times = [keyframe.time_seconds for keyframe in keyframes]
    right_index = bisect_right(times, time_seconds)
    left = keyframes[right_index - 1]
    right = keyframes[right_index]
    span = right.time_seconds - left.time_seconds
    raw_amount = (time_seconds - left.time_seconds) / span
    eased_amount = EASING_FUNCTIONS[left.easing](raw_amount)

    return CameraKeyframe(
        time_seconds=time_seconds,
        yaw=interpolate_yaw(left.yaw, right.yaw, eased_amount),
        pitch=linear_interpolate(left.pitch, right.pitch, eased_amount),
        roll=interpolate_yaw(left.roll, right.roll, eased_amount),
        horizontal_fov=linear_interpolate(
            left.horizontal_fov,
            right.horizontal_fov,
            eased_amount,
        ),
        easing=Easing.LINEAR,
        subject_id=left.subject_id if raw_amount < 0.5 else right.subject_id,
        confidence=_interpolate_optional_confidence(
            left.confidence,
            right.confidence,
            eased_amount,
        ),
    )


def resample_camera_track(
    track: VirtualCameraTrack,
    interval_seconds: float,
) -> list[CameraKeyframe]:
    """Resample a camera track at a fixed interval, always including both ends."""

    if not math.isfinite(interval_seconds) or interval_seconds <= 0:
        raise ValueError("interval_seconds must be a finite positive number")

    samples: list[CameraKeyframe] = []
    current = track.source_start_seconds
    while current < track.source_end_seconds - 1e-9:
        samples.append(sample_camera_track(track, current))
        current += interval_seconds
    samples.append(sample_camera_track(track, track.source_end_seconds))
    return samples


def camera_track_is_static(track: VirtualCameraTrack) -> bool:
    """Return whether all camera orientation/FOV values are equivalent."""

    first = track.keyframes[0]
    return all(
        math.isclose(shortest_angle_delta(first.yaw, keyframe.yaw), 0.0, abs_tol=1e-9)
        and math.isclose(first.pitch, keyframe.pitch, abs_tol=1e-9)
        and math.isclose(
            shortest_angle_delta(first.roll, keyframe.roll),
            0.0,
            abs_tol=1e-9,
        )
        and math.isclose(
            first.horizontal_fov,
            keyframe.horizontal_fov,
            abs_tol=1e-9,
        )
        for keyframe in track.keyframes[1:]
    )
