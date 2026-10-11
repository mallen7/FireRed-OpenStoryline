"""Cached spherical analysis pipeline using overlapping perspective views."""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import numpy as np
from PIL import Image, ImageDraw
from pydantic import ValidationError

from open_storyline.spherical.analysis_interfaces import (
    ColorHistogramEmbedder,
    DetectorSettings,
    FaceDetector,
    HistogramShotBoundaryDetector,
    ObjectDetector,
    PersonDetector,
    SaliencyObjectDetector,
    ShotBoundaryDetector,
    SkinToneFaceDetector,
    SubjectTracker,
    VisualEmbedder,
)
from open_storyline.spherical.analysis_schemas import (
    AnalysisBackendInfo,
    AnalysisReviewArtifacts,
    DetectionKind,
    PerspectiveFrame,
    SphericalAnalysis,
    SphericalDetection,
)
from open_storyline.spherical.errors import (
    SphericalCacheError,
    SphericalRenderError,
    SphericalValidationError,
)
from open_storyline.spherical.ffmpeg import probe_media
from open_storyline.spherical.geometry import angles_to_equirectangular_pixel
from open_storyline.spherical.manifests import atomic_write_json, sha256_file, sha256_json
from open_storyline.spherical.perspective import (
    PerspectiveMediaGenerator,
    build_overlapping_view_grid,
    deduplicate_spherical_detections,
    map_view_detection,
)
from open_storyline.spherical.schemas import SphericalAsset
from open_storyline.spherical.tracking import GreedySphericalSubjectTracker


ANALYSIS_ENGINE_VERSION = "1.0.0"


@dataclass(frozen=True)
class SphericalAnalysisSettings:
    interval_seconds: float = 0.5
    max_frames: int = 120
    yaw_step_degrees: int = 60
    pitch_centers: tuple[float, ...] = (-45.0, 0.0, 45.0)
    horizontal_fov: float = 100.0
    view_width: int = 256
    view_height: int = 256
    minimum_detection_confidence: float = 0.25
    dedup_angular_threshold_degrees: float = 10.0
    max_track_angular_distance_degrees: float = 45.0
    max_occlusion_seconds: float = 1.5

    def __post_init__(self) -> None:
        if not 0.1 <= self.interval_seconds <= 10.0:
            raise SphericalValidationError("analysis interval must be 0.1 through 10 seconds")
        if not 1 <= self.max_frames <= 600:
            raise SphericalValidationError("analysis max_frames must be 1 through 600")
        if not 0.0 <= self.minimum_detection_confidence <= 1.0:
            raise SphericalValidationError("analysis confidence must be inside 0..1")
        if not 0.0 < self.dedup_angular_threshold_degrees <= 45.0:
            raise SphericalValidationError("analysis deduplication angle is invalid")
        if not 0.0 < self.max_track_angular_distance_degrees <= 180.0:
            raise SphericalValidationError("analysis tracking angle is invalid")
        if not 0.0 <= self.max_occlusion_seconds <= 30.0:
            raise SphericalValidationError("analysis occlusion limit is invalid")

    def as_dict(self) -> dict[str, Any]:
        return {
            "interval_seconds": self.interval_seconds,
            "max_frames": self.max_frames,
            "yaw_step_degrees": self.yaw_step_degrees,
            "pitch_centers": list(self.pitch_centers),
            "horizontal_fov": self.horizontal_fov,
            "view_width": self.view_width,
            "view_height": self.view_height,
            "minimum_detection_confidence": self.minimum_detection_confidence,
            "dedup_angular_threshold_degrees": self.dedup_angular_threshold_degrees,
            "max_track_angular_distance_degrees": self.max_track_angular_distance_degrees,
            "max_occlusion_seconds": self.max_occlusion_seconds,
        }


class DefaultAnalysisComponents:
    """Dependency-light baseline; semantic/GPU implementations can replace protocols."""

    name = "lightweight-cpu"
    version = "1.0.0"

    def __init__(self, settings: SphericalAnalysisSettings) -> None:
        self.visual_embedder = ColorHistogramEmbedder()
        detector_settings = DetectorSettings(
            minimum_confidence=settings.minimum_detection_confidence
        )
        self.object_detector = SaliencyObjectDetector(
            self.visual_embedder,
            settings=detector_settings,
            kind=DetectionKind.OBJECT,
            label="salient-object",
        )
        self.person_detector = SaliencyObjectDetector(
            self.visual_embedder,
            settings=detector_settings,
            kind=DetectionKind.PERSON,
            label="person-candidate",
            require_person_aspect=True,
        )
        self.face_detector = SkinToneFaceDetector(
            self.visual_embedder,
            minimum_confidence=max(0.35, settings.minimum_detection_confidence),
        )
        self.subject_tracker = GreedySphericalSubjectTracker(
            max_angular_distance_degrees=settings.max_track_angular_distance_degrees,
            max_occlusion_seconds=settings.max_occlusion_seconds,
        )
        self.shot_boundary_detector = HistogramShotBoundaryDetector()

    def metadata(self, settings: SphericalAnalysisSettings) -> AnalysisBackendInfo:
        return AnalysisBackendInfo(
            name=self.name,
            version=self.version,
            object_detector=self.object_detector.name,
            person_detector=self.person_detector.name,
            face_detector=self.face_detector.name,
            subject_tracker=self.subject_tracker.name,
            shot_boundary_detector=self.shot_boundary_detector.name,
            visual_embedder=self.visual_embedder.name,
            parameters=settings.as_dict(),
            optional_dependencies=[
                "Replace protocol implementations with approved semantic or GPU backends when configured."
            ],
        )


class AnalysisComponents(Protocol):
    name: str
    version: str
    object_detector: ObjectDetector
    person_detector: PersonDetector
    face_detector: FaceDetector
    subject_tracker: SubjectTracker
    shot_boundary_detector: ShotBoundaryDetector
    visual_embedder: VisualEmbedder

    def metadata(self, settings: SphericalAnalysisSettings) -> AnalysisBackendInfo: ...


def _sample_timestamps(
    duration_seconds: float,
    fps: float,
    settings: SphericalAnalysisSettings,
) -> list[float]:
    if settings.interval_seconds <= 0 or settings.max_frames <= 0:
        raise SphericalValidationError("analysis interval and max_frames must be positive")
    last = max(0.0, duration_seconds - 1.0 / fps)
    timestamps: list[float] = []
    current = 0.0
    while current <= last + 1e-9 and len(timestamps) < settings.max_frames:
        timestamps.append(round(min(current, last), 6))
        current += settings.interval_seconds
    if not timestamps:
        timestamps = [0.0]
    return list(dict.fromkeys(timestamps))


def _load_rgb(path: Path) -> np.ndarray:
    with Image.open(path) as image:
        return np.asarray(image.convert("RGB"), dtype=np.uint8)


def _annotate_perspective_frame(
    source: Path,
    destination: Path,
    detections: list[dict[str, Any]],
    label: str,
) -> None:
    with Image.open(source) as image:
        annotated = image.convert("RGB")
    draw = ImageDraw.Draw(annotated)
    draw.rectangle((3, 3, min(annotated.width - 3, 165), 20), fill=(0, 0, 0))
    draw.text((7, 7), label, fill=(255, 255, 255))
    colors = {"object": (255, 210, 30), "person": (40, 255, 80), "face": (255, 80, 180)}
    for detection in detections:
        box = detection["bounding_box"]
        kind = detection["kind"]
        draw.rectangle(
            (
                box["x_min"] * annotated.width,
                box["y_min"] * annotated.height,
                box["x_max"] * annotated.width,
                box["y_max"] * annotated.height,
            ),
            outline=colors.get(kind, (255, 255, 255)),
            width=2,
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    annotated.save(destination, format="PNG")


def _make_contact_sheet(images: list[Path], destination: Path, limit: int = 16) -> None:
    selected = images[:limit]
    if not selected:
        raise SphericalRenderError("No perspective images were available for review")
    opened = [Image.open(path).convert("RGB") for path in selected]
    try:
        tile_width = max(image.width for image in opened)
        tile_height = max(image.height for image in opened)
        columns = min(4, len(opened))
        rows = math.ceil(len(opened) / columns)
        sheet = Image.new("RGB", (tile_width * columns, tile_height * rows), "black")
        for index, image in enumerate(opened):
            sheet.paste(image, ((index % columns) * tile_width, (index // columns) * tile_height))
        sheet.save(destination, format="PNG")
    finally:
        for image in opened:
            image.close()


def _annotate_equirectangular_frame(
    source: Path,
    destination: Path,
    timestamp: float,
    detections: list[SphericalDetection],
) -> None:
    with Image.open(source) as image:
        annotated = image.convert("RGB")
    draw = ImageDraw.Draw(annotated)
    draw.rectangle((3, 3, 110, 20), fill=(0, 0, 0))
    draw.text((7, 7), f"t={timestamp:.2f}s", fill=(255, 255, 255))
    for detection in detections:
        x, y = angles_to_equirectangular_pixel(
            detection.yaw,
            detection.pitch,
            annotated.width,
            annotated.height,
        )
        radius_x = max(3.0, detection.angular_width / 360.0 * annotated.width / 2.0)
        radius_y = max(3.0, detection.angular_height / 180.0 * annotated.height / 2.0)
        draw.ellipse(
            (x - radius_x, y - radius_y, x + radius_x, y + radius_y),
            outline=(255, 70, 50),
            width=2,
        )
    annotated.save(destination, format="PNG")


def _encode_overlay_clip(
    ffmpeg: str,
    pattern: Path,
    destination: Path,
    *,
    frame_rate: float,
    frame_count: int,
) -> list[str]:
    duration = max(1.0 / frame_rate, frame_count / frame_rate)
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-framerate",
        f"{frame_rate:.6f}",
        "-start_number",
        "0",
        "-i",
        str(pattern),
        "-t",
        f"{duration:.6f}",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "23",
        "-pix_fmt",
        "yuv420p",
        "-an",
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
            "Could not encode the spherical detection overlay clip.",
            details={"exit_code": completed.returncode, "stderr": completed.stderr[-8000:]},
        )
    probe = probe_media(destination)
    if probe.width != probe.height * 2 or probe.duration_seconds <= 0:
        raise SphericalRenderError("Detection overlay clip failed media validation")
    return command


def _paths_for_entry(
    entry: Path,
    hashes: dict[str, str],
) -> AnalysisReviewArtifacts:
    return AnalysisReviewArtifacts(
        detection_overlay_clip_path=entry / "detection-overlay.mp4",
        detection_contact_sheet_path=entry / "detection-contact-sheet.png",
        subject_trajectory_json_path=entry / "subject-trajectories.json",
        analysis_manifest_path=entry / "analysis.json",
        hashes=hashes,
    )


def _validate_cached_analysis(
    entry: Path,
    cache_key: str,
    source_hash: str,
) -> SphericalAnalysis | None:
    manifest_path = entry / "analysis.json"
    try:
        analysis = SphericalAnalysis.model_validate_json(
            manifest_path.read_text(encoding="utf-8")
        )
    except (OSError, ValidationError):
        return None
    if analysis.cache_key != cache_key or analysis.source_sha256 != source_hash:
        return None
    expected_paths = {
        "detection_overlay_clip": entry / "detection-overlay.mp4",
        "detection_contact_sheet": entry / "detection-contact-sheet.png",
        "subject_trajectory_json": entry / "subject-trajectories.json",
    }
    actual_paths = {
        "detection_overlay_clip": analysis.review_artifacts.detection_overlay_clip_path,
        "detection_contact_sheet": analysis.review_artifacts.detection_contact_sheet_path,
        "subject_trajectory_json": analysis.review_artifacts.subject_trajectory_json_path,
    }
    if actual_paths != expected_paths:
        return None
    if analysis.review_artifacts.analysis_manifest_path != manifest_path:
        return None
    for name, expected in analysis.review_artifacts.hashes.items():
        path = actual_paths.get(name)
        if path is None or not path.is_file() or sha256_file(path) != expected:
            return None
    expected_frame_root = entry / "frames"
    if any(
        frame.image_path.parent != expected_frame_root
        or not frame.image_path.is_file()
        or sha256_file(frame.image_path) != frame.image_sha256
        for frame in analysis.perspective_frames
    ):
        return None
    return analysis.model_copy(update={"cache_hit": True})


class SphericalAnalyzer:
    """Generate deterministic review media, detections, and subject tracks."""

    def __init__(
        self,
        *,
        settings: SphericalAnalysisSettings | None = None,
        components: AnalysisComponents | None = None,
    ) -> None:
        self.settings = settings or SphericalAnalysisSettings()
        self.components = components or DefaultAnalysisComponents(self.settings)

    def analyze(
        self,
        asset: SphericalAsset,
        *,
        cache_dir: str | Path,
    ) -> SphericalAnalysis:
        source = (asset.proxy_path or asset.master_path).expanduser().resolve()
        if not source.is_file():
            raise SphericalValidationError("Spherical analysis source does not exist")
        source_hash = sha256_file(source)
        timestamps = _sample_timestamps(
            asset.duration_seconds, asset.fps, self.settings
        )
        viewports = build_overlapping_view_grid(
            yaw_step_degrees=self.settings.yaw_step_degrees,
            pitch_centers=self.settings.pitch_centers,
            horizontal_fov=self.settings.horizontal_fov,
            width=self.settings.view_width,
            height=self.settings.view_height,
        )
        generator = PerspectiveMediaGenerator()
        backend = self.components.metadata(self.settings)
        backend = backend.model_copy(
            update={
                "parameters": {
                    **backend.parameters,
                    "ffmpeg_version": generator.ffmpeg_version,
                    "perspective_generator_version": generator.version,
                    "analysis_engine_version": ANALYSIS_ENGINE_VERSION,
                }
            }
        )
        cache_key = sha256_json(
            {
                "source_sha256": source_hash,
                "asset_id": asset.asset_id,
                "timestamps": timestamps,
                "viewports": [view.model_dump(mode="json") for view in viewports],
                "backend": backend.model_dump(mode="json"),
                "ffmpeg_version": generator.ffmpeg_version,
                "perspective_generator_version": generator.version,
                "analysis_engine_version": ANALYSIS_ENGINE_VERSION,
            }
        )
        root = Path(cache_dir).expanduser().resolve(strict=False) / "analysis"
        entry = root / cache_key
        cached = _validate_cached_analysis(entry, cache_key, source_hash)
        if cached is not None:
            return cached

        root.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=f".{cache_key}.", dir=root))
        try:
            generated = generator.generate(
                source,
                temporary / "frames",
                timestamps=timestamps,
                viewports=viewports,
            )
            frame_detections: dict[str, list[dict[str, Any]]] = {}
            spherical_candidates: list[SphericalDetection] = []
            annotated_paths: list[Path] = []
            final_frames: list[PerspectiveFrame] = []
            for index, frame in enumerate(generated.frames):
                image = _load_rgb(frame.image_path)
                view_detections = [
                    *self.components.object_detector.detect(image, frame),
                    *self.components.person_detector.detect(image, frame),
                    *self.components.face_detector.detect(image, frame),
                ]
                frame_detections[f"{frame.timestamp_seconds}:{frame.viewport.viewport_id}"] = [
                    detection.model_dump(mode="json") for detection in view_detections
                ]
                spherical_candidates.extend(
                    map_view_detection(detection, frame) for detection in view_detections
                )
                annotated_path = temporary / "annotated" / f"annotated-{index:05d}.png"
                _annotate_perspective_frame(
                    frame.image_path,
                    annotated_path,
                    [detection.model_dump(mode="json") for detection in view_detections],
                    f"{frame.viewport.viewport_id} t={frame.timestamp_seconds:.2f}s",
                )
                annotated_paths.append(annotated_path)
                final_frames.append(
                    frame.model_copy(
                        update={
                            "image_path": entry
                            / "frames"
                            / frame.image_path.name
                        }
                    )
                )

            detections = deduplicate_spherical_detections(
                spherical_candidates,
                angular_threshold_degrees=self.settings.dedup_angular_threshold_degrees,
            )
            tracks = self.components.subject_tracker.track(
                asset.asset_id,
                detections,
                timestamps,
            )
            source_frames = [
                (timestamp, _load_rgb(path))
                for timestamp, path in generated.equirectangular_frames
            ]
            shot_boundaries = self.components.shot_boundary_detector.detect(source_frames)

            trajectories_path = temporary / "subject-trajectories.json"
            atomic_write_json(
                trajectories_path,
                {
                    "schema_version": "1.0",
                    "asset_id": asset.asset_id,
                    "subject_tracks": [track.model_dump(mode="json") for track in tracks],
                    "spherical_detections": [
                        detection.model_dump(mode="json") for detection in detections
                    ],
                    "view_detections": frame_detections,
                },
            )
            contact_sheet_path = temporary / "detection-contact-sheet.png"
            _make_contact_sheet(annotated_paths, contact_sheet_path)
            detections_by_time: dict[float, list[SphericalDetection]] = {}
            for detection in detections:
                detections_by_time.setdefault(detection.timestamp_seconds, []).append(detection)
            for index, (timestamp, source_path) in enumerate(generated.equirectangular_frames):
                _annotate_equirectangular_frame(
                    source_path,
                    temporary / f"overlay-{index:04d}.png",
                    timestamp,
                    detections_by_time.get(timestamp, []),
                )
            overlay_path = temporary / "detection-overlay.mp4"
            _encode_overlay_clip(
                generator.ffmpeg,
                temporary / "overlay-%04d.png",
                overlay_path,
                frame_rate=1.0 / self.settings.interval_seconds,
                frame_count=len(timestamps),
            )

            visible_confidences = [
                sample.confidence
                for track in tracks
                for sample in track.samples
                if sample.visibility.value == "visible"
            ]
            confidence_summary = {
                "mean_visible_confidence": (
                    sum(visible_confidences) / len(visible_confidences)
                    if visible_confidences
                    else 0.0
                ),
                "subject_count": float(len(tracks)),
                "deduplicated_detection_count": float(len(detections)),
                "raw_view_detection_count": float(len(spherical_candidates)),
            }
            warnings = [
                "The default lightweight CPU backend finds visual candidates; labels are not semantic identity claims."
            ]
            if not tracks:
                warnings.append("No stable subject track was detected at the configured thresholds.")
            if (
                len(timestamps) >= self.settings.max_frames
                and timestamps[-1] + self.settings.interval_seconds
                < asset.duration_seconds - 1.0 / asset.fps - 1e-9
            ):
                warnings.append("Analysis stopped at the configured max_frames limit.")
            warnings.extend(
                warning for track in tracks for warning in track.warnings
            )
            hashes = {
                "detection_overlay_clip": sha256_file(overlay_path),
                "detection_contact_sheet": sha256_file(contact_sheet_path),
                "subject_trajectory_json": sha256_file(trajectories_path),
            }
            final_artifacts = _paths_for_entry(entry, hashes)
            analysis = SphericalAnalysis(
                analysis_id=f"analysis-{cache_key[:16]}",
                asset_id=asset.asset_id,
                source_sha256=source_hash,
                cache_key=cache_key,
                cache_hit=False,
                sample_timestamps=timestamps,
                perspective_frames=final_frames,
                spherical_detections=detections,
                subject_tracks=tracks,
                shot_boundaries_seconds=shot_boundaries,
                backend=backend,
                confidence_summary=confidence_summary,
                review_artifacts=final_artifacts,
                warnings=list(dict.fromkeys(warnings)),
            )
            atomic_write_json(temporary / "analysis.json", analysis)
            if entry.exists():
                shutil.rmtree(entry)
            os.replace(temporary, entry)
            validated = _validate_cached_analysis(entry, cache_key, source_hash)
            if validated is None:
                raise SphericalCacheError("New spherical analysis cache entry is invalid")
            return validated.model_copy(update={"cache_hit": False})
        except Exception as exc:
            if temporary.exists():
                shutil.rmtree(temporary, ignore_errors=True)
            if isinstance(
                exc,
                (SphericalValidationError, SphericalRenderError, SphericalCacheError),
            ):
                raise
            raise SphericalCacheError(
                "Spherical analysis failed before producing a valid cache entry.",
                details={"cache_key": cache_key, "error": str(exc)},
            ) from exc
