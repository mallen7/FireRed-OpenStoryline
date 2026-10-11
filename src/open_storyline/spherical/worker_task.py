"""Isolated child-process entry point for one durable distributed-worker job."""

from __future__ import annotations

import argparse
import json
import mimetypes
import sys
import time
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from open_storyline.spherical.analysis import SphericalAnalysisSettings, SphericalAnalyzer
from open_storyline.spherical.distributed_protocol import (
    AnalyzeJobSubmission,
    ReframeJobSubmission,
    StitchJobSubmission,
    WorkerArtifact,
    WorkerJobError,
    WorkerJobOperation,
)
from open_storyline.spherical.errors import SphericalCacheError, SphericalError, SphericalWorkerError
from open_storyline.spherical.ffmpeg import SphericalRenderer, probe_media
from open_storyline.spherical.manifests import atomic_write_json, manifest_path_for, sha256_file
from open_storyline.spherical.worker_client import LocalStitchWorkerClient
from open_storyline.spherical.worker_security import sanitize_sdk_log, sanitize_value


def _emit(progress: int, message: str, **data: Any) -> None:
    print(
        json.dumps(
            {
                "event": "progress",
                "progress_percent": max(0, min(100, progress)),
                "message": message,
                "data": sanitize_value(data),
            },
            separators=(",", ":"),
        ),
        flush=True,
    )


def _artifact(name: str, path: Path) -> WorkerArtifact:
    resolved = path.expanduser().resolve(strict=True)
    return WorkerArtifact(
        name=name,
        path=resolved,
        sha256=sha256_file(resolved),
        size_bytes=resolved.stat().st_size,
        media_type=mimetypes.guess_type(resolved.name)[0] or "application/octet-stream",
    )


def _rate_metrics(path: Path, elapsed: float, *, cache_hit: bool = False) -> dict[str, Any]:
    probe = probe_media(path)
    frames = probe.duration_seconds * probe.fps
    return {
        "duration_seconds": elapsed,
        "media_duration_seconds": probe.duration_seconds,
        "output_fps": probe.fps,
        "processing_fps": frames / elapsed if elapsed > 0 else 0.0,
        "output_size_bytes": path.stat().st_size,
        "cache_hit": cache_hit,
    }


def _run_stitch(
    payload: dict[str, Any], worker_executable: Path, worker_timeout: float
) -> dict[str, Any]:
    submission = StitchJobSubmission.model_validate(payload)
    started = time.perf_counter()
    client = LocalStitchWorkerClient(
        worker_executable,
        timeout_seconds=min(worker_timeout, submission.timeout_seconds or worker_timeout),
    )
    result = client.stitch(
        submission.request,
        on_progress=lambda event: _emit(
            event.progress_percent,
            event.message,
            source="insta360-mediasdk",
        ),
    )
    artifacts = [_artifact("stitched-master", result.output_path)]
    if result.sdk_log_path is not None and result.sdk_log_path.is_file():
        sanitize_sdk_log(result.sdk_log_path)
        artifacts.append(_artifact("sanitized-sdk-log", result.sdk_log_path))
    elapsed = time.perf_counter() - started
    return {
        "result": result.model_dump(mode="json"),
        "artifacts": [item.model_dump(mode="json") for item in artifacts],
        "metrics": _rate_metrics(result.output_path, elapsed),
    }


def _run_reframe(payload: dict[str, Any]) -> dict[str, Any]:
    submission = ReframeJobSubmission.model_validate(payload)
    _emit(5, "validated reframe request")
    started = time.perf_counter()
    manifest = SphericalRenderer().render(submission.request)
    elapsed = time.perf_counter() - started
    _emit(95, "validating rendered output")
    artifacts = [
        _artifact("reframed-video", manifest.output_path),
        _artifact("reframe-manifest", manifest_path_for(manifest.output_path)),
    ]
    return {
        "result": manifest.model_dump(mode="json"),
        "artifacts": [item.model_dump(mode="json") for item in artifacts],
        "metrics": _rate_metrics(
            manifest.output_path,
            elapsed,
            cache_hit=manifest.cache_hit,
        ),
    }


def _run_analyze(payload: dict[str, Any]) -> dict[str, Any]:
    submission = AnalyzeJobSubmission.model_validate(payload)
    settings = submission.settings
    _emit(5, "validated analysis request")
    started = time.perf_counter()
    analysis = SphericalAnalyzer(
        settings=SphericalAnalysisSettings(
            interval_seconds=settings.interval_seconds,
            max_frames=settings.max_frames,
            yaw_step_degrees=settings.yaw_step_degrees,
            pitch_centers=tuple(settings.pitch_centers),
            horizontal_fov=settings.horizontal_fov,
            view_width=settings.view_width,
            view_height=settings.view_height,
            minimum_detection_confidence=settings.minimum_detection_confidence,
            dedup_angular_threshold_degrees=settings.dedup_angular_threshold_degrees,
            max_track_angular_distance_degrees=settings.max_track_angular_distance_degrees,
            max_occlusion_seconds=settings.max_occlusion_seconds,
        )
    ).analyze(submission.asset, cache_dir=submission.cache_dir)
    elapsed = time.perf_counter() - started
    _emit(95, "validating analysis artifacts")
    review = analysis.review_artifacts
    artifacts = [
        _artifact("detection-overlay", review.detection_overlay_clip_path),
        _artifact("detection-contact-sheet", review.detection_contact_sheet_path),
        _artifact("subject-trajectories", review.subject_trajectory_json_path),
        _artifact("analysis-manifest", review.analysis_manifest_path),
    ]
    return {
        "result": analysis.model_dump(mode="json"),
        "artifacts": [item.model_dump(mode="json") for item in artifacts],
        "metrics": {
            "duration_seconds": elapsed,
            "sampled_frames": len(analysis.sample_timestamps),
            "processing_fps": len(analysis.sample_timestamps) / elapsed if elapsed > 0 else 0.0,
            "output_size_bytes": sum(item.size_bytes for item in artifacts),
            "cache_hit": analysis.cache_hit,
        },
    }


def _error_from_exception(exc: Exception) -> WorkerJobError:
    if isinstance(exc, SphericalError):
        retryable = isinstance(exc, SphericalCacheError)
        if isinstance(exc, SphericalWorkerError):
            retryable = bool(exc.details.get("retryable", False))
        return WorkerJobError(
            code=exc.code,
            message=exc.message,
            retryable=retryable,
            details=sanitize_value(exc.details),
        )
    if isinstance(exc, ValidationError):
        return WorkerJobError(
            code="invalid_request",
            message="The isolated task rejected its persisted request.",
            retryable=False,
            details={"validation": sanitize_value(exc.errors(include_url=False))},
        )
    return WorkerJobError(
        code="task_failed",
        message="The isolated worker task failed.",
        retryable=False,
        details={"error_type": type(exc).__name__, "error": sanitize_value(str(exc))},
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--operation", choices=[item.value for item in WorkerJobOperation], required=True)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--worker-executable", type=Path, required=True)
    parser.add_argument("--worker-timeout", type=float, required=True)
    arguments = parser.parse_args(argv)
    try:
        payload = json.loads(arguments.request.read_text(encoding="utf-8"))
        operation = WorkerJobOperation(arguments.operation)
        if operation == WorkerJobOperation.STITCH:
            output = _run_stitch(payload, arguments.worker_executable, arguments.worker_timeout)
        elif operation == WorkerJobOperation.REFRAME:
            output = _run_reframe(payload)
        else:
            output = _run_analyze(payload)
        atomic_write_json(arguments.result, output)
        _emit(100, "task result persisted")
        return 0
    except Exception as exc:
        error = _error_from_exception(exc)
        print(
            json.dumps(
                {"event": "error", "error": error.model_dump(mode="json")},
                separators=(",", ":"),
            ),
            flush=True,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
