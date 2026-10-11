"""Overlapping perspective-view generation and spherical detection mapping."""

from __future__ import annotations

import math
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray
from PIL import Image

from open_storyline.spherical.analysis_interfaces import cosine_similarity
from open_storyline.spherical.analysis_schemas import (
    PerspectiveFrame,
    PerspectiveViewport,
    SphericalDetection,
    ViewDetection,
)
from open_storyline.spherical.errors import (
    SphericalCapabilityError,
    SphericalRenderError,
    SphericalValidationError,
)
from open_storyline.spherical.geometry import (
    great_circle_angular_distance,
    normalize_angle,
)
from open_storyline.spherical.manifests import sha256_file, sha256_json


RgbImage = NDArray[np.uint8]


@dataclass(frozen=True)
class GeneratedPerspectiveMedia:
    frames: list[PerspectiveFrame]
    equirectangular_frames: list[tuple[float, Path]]
    commands: list[list[str]]


def vertical_fov(horizontal_fov: float, width: int, height: int) -> float:
    return math.degrees(
        2.0
        * math.atan(math.tan(math.radians(horizontal_fov) / 2.0) * height / width)
    )


def build_overlapping_view_grid(
    *,
    yaw_step_degrees: int = 60,
    pitch_centers: tuple[float, ...] = (-45.0, 0.0, 45.0),
    horizontal_fov: float = 100.0,
    width: int = 256,
    height: int = 256,
) -> list[PerspectiveViewport]:
    """Build a full-sphere grid whose adjacent view fields overlap."""

    if yaw_step_degrees <= 0 or 360 % yaw_step_degrees:
        raise SphericalValidationError("analysis yaw step must divide 360 degrees")
    if width <= 0 or height <= 0 or width % 2 or height % 2:
        raise SphericalValidationError("analysis view dimensions must be positive and even")
    if horizontal_fov <= yaw_step_degrees:
        raise SphericalValidationError(
            "analysis horizontal FOV must exceed yaw step to preserve overlap"
        )
    view_vertical_fov = vertical_fov(horizontal_fov, width, height)
    ordered_pitches = tuple(sorted(set(float(value) for value in pitch_centers)))
    if not ordered_pitches or any(not -90.0 <= value <= 90.0 for value in ordered_pitches):
        raise SphericalValidationError("analysis pitch centers must be inside -90..90")
    if ordered_pitches[0] - view_vertical_fov / 2 > -90.0 + 1e-6:
        raise SphericalValidationError("analysis grid leaves an uncovered nadir region")
    if ordered_pitches[-1] + view_vertical_fov / 2 < 90.0 - 1e-6:
        raise SphericalValidationError("analysis grid leaves an uncovered zenith region")
    for left, right in zip(ordered_pitches, ordered_pitches[1:]):
        if right - left >= view_vertical_fov:
            raise SphericalValidationError("analysis pitch rows must overlap")

    views: list[PerspectiveViewport] = []
    for pitch in ordered_pitches:
        for yaw in range(-180, 180, yaw_step_degrees):
            yaw_token = f"{yaw:+04d}".replace("+", "p").replace("-", "m")
            pitch_token = (
                f"{int(round(pitch)):+04d}".replace("+", "p").replace("-", "m")
            )
            views.append(
                PerspectiveViewport(
                    viewport_id=f"yaw-{yaw_token}-pitch-{pitch_token}",
                    center_yaw=float(yaw),
                    center_pitch=pitch,
                    horizontal_fov=horizontal_fov,
                    vertical_fov=view_vertical_fov,
                    width=width,
                    height=height,
                )
            )
    return views


def _camera_basis(viewport: PerspectiveViewport) -> tuple[np.ndarray, ...]:
    yaw = math.radians(viewport.center_yaw)
    pitch = math.radians(viewport.center_pitch)
    forward = np.asarray(
        [math.cos(pitch) * math.cos(yaw), math.cos(pitch) * math.sin(yaw), math.sin(pitch)]
    )
    right = np.asarray([-math.sin(yaw), math.cos(yaw), 0.0])
    up = np.asarray(
        [-math.sin(pitch) * math.cos(yaw), -math.sin(pitch) * math.sin(yaw), math.cos(pitch)]
    )
    return forward, right, up


def viewport_normalized_to_angles(
    x: float,
    y: float,
    viewport: PerspectiveViewport,
) -> tuple[float, float]:
    """Map normalized flat-view coordinates to canonical spherical angles."""

    if not 0.0 <= x <= 1.0 or not 0.0 <= y <= 1.0:
        raise ValueError("normalized view coordinates must be inside 0..1")
    forward, right, up = _camera_basis(viewport)
    horizontal = (x * 2.0 - 1.0) * math.tan(
        math.radians(viewport.horizontal_fov) / 2.0
    )
    vertical = (1.0 - y * 2.0) * math.tan(
        math.radians(viewport.vertical_fov) / 2.0
    )
    ray = forward + horizontal * right + vertical * up
    ray /= np.linalg.norm(ray)
    yaw = normalize_angle(math.degrees(math.atan2(ray[1], ray[0])))
    pitch = math.degrees(math.asin(max(-1.0, min(1.0, float(ray[2])))))
    return yaw, pitch


def project_equirectangular(image: RgbImage, viewport: PerspectiveViewport) -> RgbImage:
    """Project one equirectangular RGB frame into a rectilinear viewport."""

    source_height, source_width, channels = image.shape
    if channels != 3 or source_width != source_height * 2:
        raise SphericalValidationError("analysis source frame must be RGB and exactly 2:1")
    x = (np.arange(viewport.width, dtype=np.float64) + 0.5) / viewport.width
    y = (np.arange(viewport.height, dtype=np.float64) + 0.5) / viewport.height
    normalized_x, normalized_y = np.meshgrid(x * 2.0 - 1.0, 1.0 - y * 2.0)
    tan_horizontal = math.tan(math.radians(viewport.horizontal_fov) / 2.0)
    tan_vertical = math.tan(math.radians(viewport.vertical_fov) / 2.0)
    forward, right, up = _camera_basis(viewport)
    rays = (
        forward[None, None, :]
        + normalized_x[..., None] * tan_horizontal * right[None, None, :]
        + normalized_y[..., None] * tan_vertical * up[None, None, :]
    )
    rays /= np.linalg.norm(rays, axis=2, keepdims=True)
    yaw = np.arctan2(rays[..., 1], rays[..., 0])
    pitch = np.arcsin(np.clip(rays[..., 2], -1.0, 1.0))
    source_x = ((yaw + math.pi) / (2.0 * math.pi) * source_width - 0.5) % source_width
    source_y = np.clip((math.pi / 2.0 - pitch) / math.pi * source_height - 0.5, 0, source_height - 1)

    x0 = np.floor(source_x).astype(np.int64)
    y0 = np.floor(source_y).astype(np.int64)
    x1 = (x0 + 1) % source_width
    y1 = np.minimum(y0 + 1, source_height - 1)
    x_weight = (source_x - x0)[..., None]
    y_weight = (source_y - y0)[..., None]
    top = image[y0, x0] * (1.0 - x_weight) + image[y0, x1] * x_weight
    bottom = image[y1, x0] * (1.0 - x_weight) + image[y1, x1] * x_weight
    projected = top * (1.0 - y_weight) + bottom * y_weight
    return np.clip(np.rint(projected), 0, 255).astype(np.uint8)


class PerspectiveMediaGenerator:
    version = "1.0.0"

    def __init__(self, ffmpeg_executable: str = "ffmpeg") -> None:
        resolved = shutil.which(ffmpeg_executable)
        if not resolved:
            raise SphericalCapabilityError("ffmpeg is required for spherical analysis")
        self.ffmpeg = resolved
        completed = subprocess.run(
            [self.ffmpeg, "-version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if completed.returncode != 0 or not completed.stdout.splitlines():
            raise SphericalCapabilityError("ffmpeg version could not be determined")
        self.ffmpeg_version = completed.stdout.splitlines()[0].strip()

    def _extract_frame(
        self,
        source: Path,
        timestamp: float,
        destination: Path,
    ) -> list[str]:
        command = [
            self.ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
            "-ss",
            f"{timestamp:.6f}",
            "-i",
            str(source),
            "-frames:v",
            "1",
            str(destination),
        ]
        completed = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise SphericalRenderError(
                "FFmpeg could not extract an equirectangular analysis frame.",
                details={
                    "timestamp_seconds": timestamp,
                    "exit_code": completed.returncode,
                    "stderr": completed.stderr[-8000:],
                },
            )
        return command

    def generate(
        self,
        source: str | Path,
        destination: str | Path,
        *,
        timestamps: list[float],
        viewports: list[PerspectiveViewport],
    ) -> GeneratedPerspectiveMedia:
        source_path = Path(source).expanduser().resolve()
        root = Path(destination).expanduser().resolve(strict=False)
        root.mkdir(parents=True, exist_ok=True)
        frames: list[PerspectiveFrame] = []
        equirectangular_frames: list[tuple[float, Path]] = []
        commands: list[list[str]] = []
        for time_index, timestamp in enumerate(timestamps):
            source_frame_path = root / f"source-{time_index:04d}.png"
            commands.append(self._extract_frame(source_path, timestamp, source_frame_path))
            with Image.open(source_frame_path) as source_image:
                source_array = np.asarray(source_image.convert("RGB"), dtype=np.uint8)
            if source_array.shape[1] != source_array.shape[0] * 2:
                raise SphericalValidationError(
                    "analysis proxy frames must be exactly 2:1 equirectangular images"
                )
            equirectangular_frames.append((timestamp, source_frame_path))
            for view_index, viewport in enumerate(viewports):
                projected = project_equirectangular(source_array, viewport)
                image_path = root / f"view-{time_index:04d}-{view_index:03d}.png"
                Image.fromarray(projected).save(image_path, format="PNG")
                frames.append(
                    PerspectiveFrame(
                        timestamp_seconds=timestamp,
                        viewport=viewport,
                        image_path=image_path,
                        image_sha256=sha256_file(image_path),
                    )
                )
        return GeneratedPerspectiveMedia(
            frames=frames,
            equirectangular_frames=equirectangular_frames,
            commands=commands,
        )


def map_view_detection(
    detection: ViewDetection,
    frame: PerspectiveFrame,
) -> SphericalDetection:
    if detection.viewport_id != frame.viewport.viewport_id:
        raise SphericalValidationError("detection viewport does not match its frame")
    center_x, center_y = detection.bounding_box.center
    yaw, pitch = viewport_normalized_to_angles(center_x, center_y, frame.viewport)
    left_yaw, left_pitch = viewport_normalized_to_angles(
        detection.bounding_box.x_min, center_y, frame.viewport
    )
    right_yaw, right_pitch = viewport_normalized_to_angles(
        detection.bounding_box.x_max, center_y, frame.viewport
    )
    top_yaw, top_pitch = viewport_normalized_to_angles(
        center_x, detection.bounding_box.y_min, frame.viewport
    )
    bottom_yaw, bottom_pitch = viewport_normalized_to_angles(
        center_x, detection.bounding_box.y_max, frame.viewport
    )
    return SphericalDetection(
        detection_id=detection.detection_id,
        timestamp_seconds=detection.timestamp_seconds,
        yaw=yaw,
        pitch=pitch,
        angular_width=max(
            0.001,
            great_circle_angular_distance(left_yaw, left_pitch, right_yaw, right_pitch),
        ),
        angular_height=max(
            0.001,
            great_circle_angular_distance(top_yaw, top_pitch, bottom_yaw, bottom_pitch),
        ),
        confidence=detection.confidence,
        kinds=[detection.kind],
        labels=[detection.label],
        source_viewports=[detection.viewport_id],
        detector_backends=[detection.detector_backend],
        embedding=detection.embedding,
        identity_evidence=detection.identity_evidence,
    )


def _mean_spherical_position(
    detections: list[SphericalDetection],
) -> tuple[float, float]:
    vector = np.zeros(3, dtype=np.float64)
    for detection in detections:
        yaw = math.radians(detection.yaw)
        pitch = math.radians(detection.pitch)
        weight = max(0.001, detection.confidence)
        vector += weight * np.asarray(
            [math.cos(pitch) * math.cos(yaw), math.cos(pitch) * math.sin(yaw), math.sin(pitch)]
        )
    vector /= np.linalg.norm(vector)
    return (
        normalize_angle(math.degrees(math.atan2(vector[1], vector[0]))),
        math.degrees(math.asin(max(-1.0, min(1.0, float(vector[2]))))),
    )


def deduplicate_spherical_detections(
    detections: list[SphericalDetection],
    *,
    angular_threshold_degrees: float = 10.0,
    embedding_similarity_threshold: float = 0.65,
) -> list[SphericalDetection]:
    """Merge the same subject observed in overlapping perspective viewports."""

    grouped: dict[float, list[SphericalDetection]] = {}
    for detection in detections:
        grouped.setdefault(detection.timestamp_seconds, []).append(detection)
    merged: list[SphericalDetection] = []
    for timestamp in sorted(grouped):
        clusters: list[list[SphericalDetection]] = []
        for detection in sorted(
            grouped[timestamp], key=lambda value: value.confidence, reverse=True
        ):
            target: list[SphericalDetection] | None = None
            for cluster in clusters:
                center_yaw, center_pitch = _mean_spherical_position(cluster)
                distance = great_circle_angular_distance(
                    center_yaw, center_pitch, detection.yaw, detection.pitch
                )
                similarities = [
                    cosine_similarity(detection.embedding, member.embedding)
                    for member in cluster
                    if detection.embedding and member.embedding
                ]
                identity_matches = not similarities or max(similarities) >= embedding_similarity_threshold
                if distance <= angular_threshold_degrees and identity_matches:
                    target = cluster
                    break
            if target is None:
                clusters.append([detection])
            else:
                target.append(detection)

        for cluster in clusters:
            yaw, pitch = _mean_spherical_position(cluster)
            embedding_members = [item.embedding for item in cluster if item.embedding]
            embedding = (
                np.asarray(embedding_members, dtype=np.float64).mean(axis=0).tolist()
                if embedding_members
                else []
            )
            member_ids = sorted(item.detection_id for item in cluster)
            merged.append(
                SphericalDetection(
                    detection_id=f"spherical-{sha256_json(member_ids)[:16]}",
                    timestamp_seconds=timestamp,
                    yaw=yaw,
                    pitch=pitch,
                    angular_width=max(item.angular_width for item in cluster),
                    angular_height=max(item.angular_height for item in cluster),
                    confidence=max(item.confidence for item in cluster),
                    kinds=sorted(
                        {kind for item in cluster for kind in item.kinds},
                        key=lambda value: value.value,
                    ),
                    labels=sorted({label for item in cluster for label in item.labels}),
                    source_viewports=sorted(
                        {viewport for item in cluster for viewport in item.source_viewports}
                    ),
                    detector_backends=sorted(
                        {backend for item in cluster for backend in item.detector_backends}
                    ),
                    embedding=embedding,
                    identity_evidence={
                        "deduplicated_view_detections": len(cluster),
                        "member_detection_ids": member_ids,
                    },
                )
            )
    return merged
