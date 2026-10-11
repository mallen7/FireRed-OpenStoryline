#!/usr/bin/env python3
"""Disposable Phase 7 spherical performance and quality benchmark.

The harness generates its own tiny labeled panorama in a temporary directory;
it never needs or retains user footage. Results are JSON on stdout or at the
explicit --output path.
"""

from __future__ import annotations

import argparse
import json
import platform
import re
import resource
import subprocess
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path
from typing import Any, Callable

from open_storyline.spherical.analysis import (
    SphericalAnalysisSettings,
    SphericalAnalyzer,
)
from open_storyline.spherical.ffmpeg import SphericalRenderer, probe_media
from open_storyline.spherical.fixtures import generate_fixture
from open_storyline.spherical.manifests import load_json_model
from open_storyline.spherical.media import generate_proxy
from open_storyline.spherical.schemas import (
    CameraKeyframe,
    CodecSettings,
    ReframeRenderRequest,
    SphericalAsset,
    VirtualCameraTrack,
)


def _rss_megabytes(value: int) -> float:
    # Darwin reports bytes; Linux reports KiB.
    divisor = 1024 * 1024 if sys.platform == "darwin" else 1024
    return round(value / divisor, 3)


def _measure(operation: Callable[[], Any]) -> tuple[Any, dict[str, float]]:
    tracemalloc.start()
    started = time.perf_counter()
    result = operation()
    elapsed = time.perf_counter() - started
    _, python_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    parent = resource.getrusage(resource.RUSAGE_SELF)
    children = resource.getrusage(resource.RUSAGE_CHILDREN)
    return result, {
        "wall_seconds": round(elapsed, 6),
        "python_peak_megabytes": round(python_peak / (1024 * 1024), 3),
        "process_peak_rss_megabytes": _rss_megabytes(parent.ru_maxrss),
        "child_peak_rss_megabytes": _rss_megabytes(children.ru_maxrss),
    }


def _track(
    asset: SphericalAsset,
    name: str,
    width: int,
    height: int,
    *,
    animated: bool,
) -> VirtualCameraTrack:
    frames = [CameraKeyframe(time_seconds=0.0, yaw=0.0, horizontal_fov=90.0)]
    if animated:
        frames.append(
            CameraKeyframe(
                time_seconds=asset.duration_seconds,
                yaw=120.0,
                pitch=18.0,
                horizontal_fov=72.0,
                easing="ease_in_out",
            )
        )
    return VirtualCameraTrack(
        track_id=f"benchmark-{name}-{width}x{height}",
        asset_id=asset.asset_id,
        name=name,
        output_width=width,
        output_height=height,
        source_start_seconds=0.0,
        source_end_seconds=asset.duration_seconds,
        keyframes=frames,
    )


def _request(
    root: Path,
    asset: SphericalAsset,
    track: VirtualCameraTrack,
    filename: str,
    *,
    use_cache: bool,
    crf: int = 18,
    interval: float = 0.5,
) -> ReframeRenderRequest:
    return ReframeRenderRequest(
        asset=asset,
        track=track,
        output_path=root / "outputs" / filename,
        workspace_root=root / "outputs",
        cache_dir=root / "render-cache",
        codec_settings=CodecSettings(preset="veryfast", crf=crf),
        sample_interval_seconds=interval,
        use_cache=use_cache,
    )


def _psnr(reference: Path, candidate: Path) -> float | str | None:
    completed = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-nostdin", "-i", str(reference),
            "-i", str(candidate), "-lavfi", "[0:v][1:v]psnr", "-an",
            "-f", "null", "-",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    match = re.search(r"average:([0-9.]+|inf)", completed.stderr)
    if not match:
        return None
    return "inf" if match.group(1) == "inf" else round(float(match.group(1)), 3)


def run_benchmark(*, quick: bool) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="spherical-performance-") as name:
        root = Path(name)
        duration = 1.0 if quick else 3.0
        fixture = generate_fixture(
            root / "fixture",
            width=640 if quick else 1024,
            height=320 if quick else 512,
            fps=10.0 if quick else 24.0,
            duration_seconds=duration,
            output_width=320,
            output_height=180,
        )
        asset = load_json_model(fixture.asset, SphericalAsset)
        renderer = SphericalRenderer()
        metrics: dict[str, Any] = {}

        proxy, metrics["proxy_generation"] = _measure(
            lambda: generate_proxy(
                asset,
                root / "proxy-first.mp4",
                cache_dir=root / "proxy-cache",
                proxy_width=320 if quick else 640,
            )
        )
        proxy_cached, metrics["proxy_cache_replay"] = _measure(
            lambda: generate_proxy(
                asset,
                root / "proxy-second.mp4",
                cache_dir=root / "proxy-cache",
                proxy_width=320 if quick else 640,
            )
        )
        metrics["proxy_generation"]["cache_hit"] = proxy.cache_hit
        metrics["proxy_cache_replay"]["cache_hit"] = proxy_cached.cache_hit

        profiles = [(320, 180)] if quick else [(640, 360), (360, 640), (512, 512)]
        quality: list[dict[str, Any]] = []
        first_candidate: Path | None = None
        first_reference: Path | None = None
        for width, height in profiles:
            track = _track(asset, "static", width, height, animated=False)
            request = _request(
                root, asset, track, f"static-{width}x{height}.mp4", use_cache=True
            )
            manifest, timing = _measure(lambda request=request: renderer.render(request))
            probe = probe_media(request.output_path)
            timing.update(
                {
                    "processing_fps": round(asset.duration_seconds * asset.fps / timing["wall_seconds"], 3),
                    "realtime_factor": round(asset.duration_seconds / timing["wall_seconds"], 3),
                    "cache_hit": manifest.cache_hit,
                }
            )
            metrics[f"static_reframe_{width}x{height}"] = timing
            quality.append(
                {
                    "resolution": [probe.width, probe.height],
                    "duration_drift_seconds": round(abs(probe.duration_seconds - asset.duration_seconds), 4),
                    "audio_streams": probe.audio_stream_count,
                    "video_codec": probe.video_codec,
                    "output_bytes": request.output_path.stat().st_size,
                    "crf": 18,
                }
            )
            if first_candidate is None:
                first_candidate = request.output_path
                reference_track = _track(asset, "quality-reference", width, height, animated=False)
                reference_request = _request(
                    root,
                    asset,
                    reference_track,
                    f"reference-{width}x{height}.mp4",
                    use_cache=False,
                    crf=0,
                )
                renderer.render(reference_request)
                first_reference = reference_request.output_path

        animated_track = _track(
            asset,
            "animated",
            profiles[0][0],
            profiles[0][1],
            animated=True,
        )
        animated_request = _request(
            root,
            asset,
            animated_track,
            "animated.mp4",
            use_cache=True,
            interval=0.5 if quick else 0.25,
        )
        animated_manifest, metrics["animated_reframe"] = _measure(
            lambda: renderer.render(animated_request)
        )
        metrics["animated_reframe"]["cache_hit"] = animated_manifest.cache_hit
        cached_request = _request(
            root,
            asset,
            animated_track,
            "animated-cache.mp4",
            use_cache=True,
            interval=0.5 if quick else 0.25,
        )
        cached_manifest, metrics["render_cache_replay"] = _measure(
            lambda: renderer.render(cached_request)
        )
        metrics["render_cache_replay"]["cache_hit"] = cached_manifest.cache_hit

        analysis_asset = asset.model_copy(update={"proxy_path": proxy.path})
        analyzer = SphericalAnalyzer(
            settings=SphericalAnalysisSettings(
                interval_seconds=1.0,
                max_frames=1 if quick else 3,
                yaw_step_degrees=90,
                pitch_centers=(-45.0, 0.0, 45.0),
                view_width=96 if quick else 160,
                view_height=96 if quick else 160,
            )
        )
        analysis, metrics["analysis"] = _measure(
            lambda: analyzer.analyze(analysis_asset, cache_dir=root / "analysis-cache")
        )
        perspective_count = len(analysis.perspective_frames)
        metrics["analysis"].update(
            {
                "perspective_frames": perspective_count,
                "perspective_frames_per_second": round(
                    perspective_count / metrics["analysis"]["wall_seconds"], 3
                ),
                "cache_hit": analysis.cache_hit,
            }
        )
        analysis_cached, metrics["analysis_cache_replay"] = _measure(
            lambda: analyzer.analyze(analysis_asset, cache_dir=root / "analysis-cache")
        )
        metrics["analysis_cache_replay"]["cache_hit"] = analysis_cached.cache_hit

        if first_candidate and first_reference:
            quality[0]["psnr_against_crf0_db"] = _psnr(first_reference, first_candidate)

        return {
            "schema_version": "1.0",
            "quick": quick,
            "fixture": {
                "synthetic": True,
                "resolution": [asset.width, asset.height],
                "fps": asset.fps,
                "duration_seconds": asset.duration_seconds,
                "retained": False,
            },
            "environment": {
                "platform": platform.platform(),
                "python": platform.python_version(),
                "ffmpeg": renderer.capabilities.version_line,
                "renderer_backend": renderer.capabilities.model_dump(mode="json"),
            },
            "metrics": metrics,
            "quality": quality,
            "memory_note": (
                "Python allocations and parent/child peak RSS are recorded. Peak RSS is a "
                "process-lifetime high-water mark, not an isolated per-operation delta."
            ),
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quick", action="store_true", help="Use the CI-sized one-second profile.")
    parser.add_argument("--output", type=Path, help="Optional JSON result path; media remains temporary.")
    args = parser.parse_args()
    result = run_benchmark(quick=args.quick)
    serialized = json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False, default=str) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized, encoding="utf-8")
    else:
        print(serialized, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
