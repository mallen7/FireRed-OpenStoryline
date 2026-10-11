"""Pluggable interfaces and lightweight CPU defaults for spherical analysis."""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

import numpy as np
from numpy.typing import NDArray

from open_storyline.spherical.analysis_schemas import (
    DetectionKind,
    NormalizedBoundingBox,
    PerspectiveFrame,
    SphericalDetection,
    SubjectTrack,
    ViewDetection,
)


RgbImage = NDArray[np.uint8]


class ObjectDetector(Protocol):
    name: str

    def detect(self, image: RgbImage, frame: PerspectiveFrame) -> list[ViewDetection]: ...


class PersonDetector(Protocol):
    name: str

    def detect(self, image: RgbImage, frame: PerspectiveFrame) -> list[ViewDetection]: ...


class FaceDetector(Protocol):
    name: str

    def detect(self, image: RgbImage, frame: PerspectiveFrame) -> list[ViewDetection]: ...


class SubjectTracker(Protocol):
    name: str

    def track(
        self,
        asset_id: str,
        detections: Sequence[SphericalDetection],
        timestamps: Sequence[float],
    ) -> list[SubjectTrack]: ...


class ShotBoundaryDetector(Protocol):
    name: str

    def detect(self, frames: Sequence[tuple[float, RgbImage]]) -> list[float]: ...


class VisualEmbedder(Protocol):
    name: str

    def embed(self, image: RgbImage) -> list[float]: ...


@dataclass(frozen=True)
class DetectorSettings:
    minimum_confidence: float = 0.25
    minimum_area_ratio: float = 0.001
    maximum_area_ratio: float = 0.35
    saturation_threshold: int = 70
    brightness_threshold: int = 90
    max_detections: int = 12


def _connected_components(mask: NDArray[np.bool_]) -> list[tuple[int, int, int, int, int]]:
    """Return x0, y0, x1-exclusive, y1-exclusive, area for a small binary mask."""

    height, width = mask.shape
    visited = np.zeros_like(mask, dtype=np.bool_)
    components: list[tuple[int, int, int, int, int]] = []
    for y, x in np.argwhere(mask):
        if visited[y, x]:
            continue
        queue: deque[tuple[int, int]] = deque([(int(y), int(x))])
        visited[y, x] = True
        x0 = x1 = int(x)
        y0 = y1 = int(y)
        area = 0
        while queue:
            current_y, current_x = queue.popleft()
            area += 1
            x0 = min(x0, current_x)
            x1 = max(x1, current_x)
            y0 = min(y0, current_y)
            y1 = max(y1, current_y)
            for next_y, next_x in (
                (current_y - 1, current_x),
                (current_y + 1, current_x),
                (current_y, current_x - 1),
                (current_y, current_x + 1),
            ):
                if (
                    0 <= next_y < height
                    and 0 <= next_x < width
                    and mask[next_y, next_x]
                    and not visited[next_y, next_x]
                ):
                    visited[next_y, next_x] = True
                    queue.append((next_y, next_x))
        components.append((x0, y0, x1 + 1, y1 + 1, area))
    return components


class ColorHistogramEmbedder:
    """Small deterministic color histogram; optional semantic models can replace it."""

    name = "rgb-histogram-4x4x4"

    def embed(self, image: RgbImage) -> list[float]:
        if image.size == 0:
            return [0.0] * 64
        bins = np.clip(image.astype(np.int16) // 64, 0, 3)
        indices = bins[..., 0] * 16 + bins[..., 1] * 4 + bins[..., 2]
        histogram = np.bincount(indices.reshape(-1), minlength=64).astype(np.float64)
        total = float(histogram.sum())
        if total:
            histogram /= total
        return histogram.tolist()


class SaliencyObjectDetector:
    """Detect compact saturated regions without model downloads or GPU state."""

    name = "lightweight-color-saliency"

    def __init__(
        self,
        embedder: VisualEmbedder,
        *,
        settings: DetectorSettings | None = None,
        kind: DetectionKind = DetectionKind.OBJECT,
        label: str = "salient-object",
        require_person_aspect: bool = False,
    ) -> None:
        self.embedder = embedder
        self.settings = settings or DetectorSettings()
        self.kind = kind
        self.label = label
        self.require_person_aspect = require_person_aspect

    def detect(self, image: RgbImage, frame: PerspectiveFrame) -> list[ViewDetection]:
        height, width, _ = image.shape
        channel_max = image.max(axis=2).astype(np.int16)
        channel_min = image.min(axis=2).astype(np.int16)
        saturation = channel_max - channel_min
        mask = (
            (saturation >= self.settings.saturation_threshold)
            & (channel_max >= self.settings.brightness_threshold)
        )
        # Remove isolated compression noise while preserving small fixture subjects.
        neighbors = np.zeros(mask.shape, dtype=np.uint8)
        for y_shift, x_shift in ((0, 0), (-1, 0), (1, 0), (0, -1), (0, 1)):
            neighbors += np.roll(np.roll(mask, y_shift, axis=0), x_shift, axis=1)
        mask &= neighbors >= 3

        detections: list[ViewDetection] = []
        image_area = width * height
        for index, (x0, y0, x1, y1, area) in enumerate(
            sorted(_connected_components(mask), key=lambda value: value[4], reverse=True)
        ):
            area_ratio = area / image_area
            if not self.settings.minimum_area_ratio <= area_ratio <= self.settings.maximum_area_ratio:
                continue
            box_width = x1 - x0
            box_height = y1 - y0
            if self.require_person_aspect and box_height < box_width * 1.05:
                continue
            crop = image[y0:y1, x0:x1]
            mean_saturation = float(saturation[y0:y1, x0:x1].mean()) / 255.0
            confidence = min(0.99, 0.3 + 0.55 * mean_saturation + min(0.12, area_ratio))
            if confidence < self.settings.minimum_confidence:
                continue
            detections.append(
                ViewDetection(
                    detection_id=(
                        f"{frame.viewport.viewport_id}-{frame.timestamp_seconds:.6f}-"
                        f"{self.kind.value}-{index}"
                    ),
                    timestamp_seconds=frame.timestamp_seconds,
                    viewport_id=frame.viewport.viewport_id,
                    bounding_box=NormalizedBoundingBox(
                        x_min=x0 / width,
                        y_min=y0 / height,
                        x_max=x1 / width,
                        y_max=y1 / height,
                    ),
                    kind=self.kind,
                    label=self.label,
                    confidence=confidence,
                    detector_backend=self.name,
                    embedding=self.embedder.embed(crop),
                    identity_evidence={
                        "mean_saturation": round(mean_saturation, 6),
                        "area_ratio": round(area_ratio, 6),
                    },
                )
            )
            if len(detections) >= self.settings.max_detections:
                break
        return detections


class SkinToneFaceDetector:
    """Conservative CPU face-candidate detector; not an identity recognizer."""

    name = "lightweight-ycbcr-face-candidates"

    def __init__(self, embedder: VisualEmbedder, minimum_confidence: float = 0.35):
        self.embedder = embedder
        self.minimum_confidence = minimum_confidence

    def detect(self, image: RgbImage, frame: PerspectiveFrame) -> list[ViewDetection]:
        rgb = image.astype(np.float32)
        red, green, blue = rgb[..., 0], rgb[..., 1], rgb[..., 2]
        cb = 128.0 - 0.168736 * red - 0.331264 * green + 0.5 * blue
        cr = 128.0 + 0.5 * red - 0.418688 * green - 0.081312 * blue
        mask = (cb >= 77) & (cb <= 127) & (cr >= 133) & (cr <= 173) & (red > 55)
        height, width = mask.shape
        detections: list[ViewDetection] = []
        for index, (x0, y0, x1, y1, area) in enumerate(
            sorted(_connected_components(mask), key=lambda value: value[4], reverse=True)
        ):
            ratio = area / (width * height)
            box_width = x1 - x0
            box_height = y1 - y0
            aspect = box_width / max(1, box_height)
            if ratio < 0.001 or ratio > 0.08 or not 0.45 <= aspect <= 1.8:
                continue
            confidence = min(0.75, 0.35 + ratio * 3)
            if confidence < self.minimum_confidence:
                continue
            crop = image[y0:y1, x0:x1]
            detections.append(
                ViewDetection(
                    detection_id=(
                        f"{frame.viewport.viewport_id}-{frame.timestamp_seconds:.6f}-face-{index}"
                    ),
                    timestamp_seconds=frame.timestamp_seconds,
                    viewport_id=frame.viewport.viewport_id,
                    bounding_box=NormalizedBoundingBox(
                        x_min=x0 / width,
                        y_min=y0 / height,
                        x_max=x1 / width,
                        y_max=y1 / height,
                    ),
                    kind=DetectionKind.FACE,
                    label="face-candidate",
                    confidence=confidence,
                    detector_backend=self.name,
                    embedding=self.embedder.embed(crop),
                    identity_evidence={"skin_tone_candidate": True},
                )
            )
            if len(detections) >= 6:
                break
        return detections


class HistogramShotBoundaryDetector:
    name = "rgb-histogram-distance"

    def __init__(self, threshold: float = 0.65) -> None:
        self.threshold = threshold
        self.embedder = ColorHistogramEmbedder()

    def detect(self, frames: Sequence[tuple[float, RgbImage]]) -> list[float]:
        boundaries: list[float] = []
        previous: np.ndarray | None = None
        for timestamp, image in frames:
            current = np.asarray(self.embedder.embed(image), dtype=np.float64)
            if previous is not None:
                distance = float(np.abs(current - previous).sum()) / 2.0
                if distance >= self.threshold:
                    boundaries.append(timestamp)
            previous = current
        return boundaries


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    left_array = np.asarray(left, dtype=np.float64)
    right_array = np.asarray(right, dtype=np.float64)
    denominator = float(np.linalg.norm(left_array) * np.linalg.norm(right_array))
    if math.isclose(denominator, 0.0):
        return 0.0
    return max(-1.0, min(1.0, float(np.dot(left_array, right_array) / denominator)))
