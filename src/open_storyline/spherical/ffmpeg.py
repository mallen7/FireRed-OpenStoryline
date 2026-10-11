"""Direct FFmpeg renderer for deterministic spherical reframing."""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from open_storyline import __version__ as application_version
from open_storyline.spherical.cache import (
    CacheLookup,
    RenderCache,
    compute_render_identity,
)
from open_storyline.spherical.errors import (
    SphericalCacheError,
    SphericalCapabilityError,
    SphericalProbeError,
    SphericalRenderError,
    SphericalValidationError,
)
from open_storyline.spherical.geometry import (
    camera_track_is_static,
    sample_camera_track,
)
from open_storyline.spherical.manifests import (
    atomic_write_json,
    load_json_model,
    manifest_path_for,
    sha256_file,
)
from open_storyline.spherical.schemas import (
    CameraKeyframe,
    ReframeRenderManifest,
    ReframeRenderRequest,
)


RENDERER_VERSION = "1.0.0-segmented-v360"
MAX_RENDER_SEGMENTS = 5000


class FFmpegCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ffmpeg_executable: str
    ffprobe_executable: str
    version_line: str
    v360_available: bool
    libx264_available: bool
    concat_demuxer_available: bool
    runtime_commands_supported: bool
    runtime_probe_message: str


class MediaProbe(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: Path
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    fps: float = Field(gt=0)
    duration_seconds: float = Field(gt=0)
    video_codec: str
    audio_stream_count: int = Field(ge=0)
    audio_sample_rates: list[int]


@dataclass(frozen=True)
class RenderSegment:
    index: int
    source_start_seconds: float
    frame_count: int
    camera: CameraKeyframe


def _resolve_executable(name: str) -> str:
    path = Path(name).expanduser()
    if path.is_absolute() and path.is_file():
        return str(path)
    resolved = shutil.which(name)
    if resolved:
        return resolved
    raise SphericalCapabilityError(
        f"Required executable is unavailable: {name}",
        details={"executable": name},
    )


def _run_probe(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )


def probe_ffmpeg_capabilities(
    ffmpeg_executable: str = "ffmpeg",
    ffprobe_executable: str = "ffprobe",
) -> FFmpegCapabilities:
    """Probe required filters/encoders and perform a real runtime-command test."""

    ffmpeg = _resolve_executable(ffmpeg_executable)
    ffprobe = _resolve_executable(ffprobe_executable)

    version = _run_probe([ffmpeg, "-version"])
    filters = _run_probe([ffmpeg, "-hide_banner", "-filters"])
    encoders = _run_probe([ffmpeg, "-hide_banner", "-encoders"])
    formats = _run_probe([ffmpeg, "-hide_banner", "-formats"])
    for name, result in (
        ("version", version),
        ("filters", filters),
        ("encoders", encoders),
        ("formats", formats),
    ):
        if result.returncode != 0:
            raise SphericalCapabilityError(
                f"FFmpeg {name} probe failed.",
                details={"stderr": result.stderr[-4000:]},
            )

    version_line = version.stdout.splitlines()[0].strip()
    filter_text = filters.stdout + filters.stderr
    encoder_text = encoders.stdout + encoders.stderr
    format_text = formats.stdout + formats.stderr
    v360_available = bool(re.search(r"(?m)^\s*[TSC\.]{2,3}\s+v360\s", filter_text))
    libx264_available = bool(re.search(r"(?m)^\s*V\S*\s+libx264\s", encoder_text))
    concat_available = bool(re.search(r"(?m)^\s*D\s+concat\s", format_text))

    runtime_command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "verbose",
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=64x32:rate=4:duration=0.5",
        "-vf",
        (
            "sendcmd=c='0.25 cam yaw 45',"
            "v360@cam=input=e:output=flat:w=32:h=18:yaw=0:h_fov=90"
        ),
        "-an",
        "-f",
        "null",
        "-",
    ]
    runtime = _run_probe(runtime_command)
    runtime_text = runtime.stdout + runtime.stderr
    unsupported_markers = (
        "Function not implemented",
        "ret:Invalid argument",
        "Error applying option",
    )
    runtime_supported = runtime.returncode == 0 and not any(
        marker in runtime_text for marker in unsupported_markers
    )
    if runtime_supported:
        runtime_message = "v360 accepted a runtime yaw command in the feature probe"
    else:
        marker = next(
            (value for value in unsupported_markers if value in runtime_text),
            f"probe exit code {runtime.returncode}",
        )
        runtime_message = f"v360 runtime commands unavailable: {marker}"

    return FFmpegCapabilities(
        ffmpeg_executable=ffmpeg,
        ffprobe_executable=ffprobe,
        version_line=version_line,
        v360_available=v360_available,
        libx264_available=libx264_available,
        concat_demuxer_available=concat_available,
        runtime_commands_supported=runtime_supported,
        runtime_probe_message=runtime_message,
    )


def _parse_frame_rate(value: str | None) -> float:
    if not value or value in {"0/0", "N/A"}:
        return 0.0
    try:
        return float(Fraction(value))
    except (ValueError, ZeroDivisionError):
        return 0.0


def probe_media(path: str | Path, ffprobe_executable: str = "ffprobe") -> MediaProbe:
    """Read the first video stream and all audio streams through ffprobe JSON."""

    media_path = Path(path).expanduser().resolve(strict=False)
    ffprobe = _resolve_executable(ffprobe_executable)
    command = [
        ffprobe,
        "-v",
        "error",
        "-show_streams",
        "-show_format",
        "-of",
        "json",
        str(media_path),
    ]
    completed = _run_probe(command)
    if completed.returncode != 0:
        raise SphericalProbeError(
            "ffprobe could not inspect spherical media.",
            details={
                "path": str(media_path),
                "command": command,
                "stderr": completed.stderr[-8000:],
            },
        )
    try:
        payload = json.loads(completed.stdout)
        streams = payload.get("streams", [])
        video = next(stream for stream in streams if stream.get("codec_type") == "video")
        audio = [stream for stream in streams if stream.get("codec_type") == "audio"]
        duration = float(
            payload.get("format", {}).get("duration")
            or video.get("duration")
            or 0.0
        )
        fps = _parse_frame_rate(video.get("avg_frame_rate")) or _parse_frame_rate(
            video.get("r_frame_rate")
        )
        return MediaProbe(
            path=media_path,
            width=int(video["width"]),
            height=int(video["height"]),
            fps=fps,
            duration_seconds=duration,
            video_codec=str(video.get("codec_name") or "unknown"),
            audio_stream_count=len(audio),
            audio_sample_rates=[
                int(stream["sample_rate"])
                for stream in audio
                if stream.get("sample_rate")
            ],
        )
    except Exception as exc:
        raise SphericalProbeError(
            "ffprobe returned incomplete or invalid media metadata.",
            details={"path": str(media_path), "error": str(exc)},
        ) from exc


def _format_number(value: float) -> str:
    if math.isclose(value, round(value), abs_tol=1e-9):
        return str(int(round(value)))
    return f"{value:.9f}".rstrip("0").rstrip(".")


def build_render_segments(request: ReframeRenderRequest) -> list[RenderSegment]:
    """Create frame-aligned static camera segments for the fallback renderer."""

    track = request.track
    fps = request.asset.fps
    duration = track.source_end_seconds - track.source_start_seconds
    total_frames = max(1, int(round(duration * fps)))
    if camera_track_is_static(track):
        frames_per_segment = total_frames
    else:
        frames_per_segment = max(1, int(round(request.sample_interval_seconds * fps)))

    segments: list[RenderSegment] = []
    for start_frame in range(0, total_frames, frames_per_segment):
        frame_count = min(frames_per_segment, total_frames - start_frame)
        midpoint = track.source_start_seconds + (
            start_frame + frame_count / 2.0
        ) / fps
        midpoint = min(track.source_end_seconds, midpoint)
        segments.append(
            RenderSegment(
                index=len(segments),
                source_start_seconds=track.source_start_seconds + start_frame / fps,
                frame_count=frame_count,
                camera=sample_camera_track(track, midpoint),
            )
        )
    if len(segments) > MAX_RENDER_SEGMENTS:
        raise SphericalValidationError(
            "Camera track produces too many fallback render segments.",
            details={
                "segment_count": len(segments),
                "maximum": MAX_RENDER_SEGMENTS,
                "suggestion": "Increase sample_interval_seconds or shorten the source range.",
            },
        )
    return segments


def _concat_file_line(path: Path) -> str:
    escaped = str(path.resolve()).replace("'", "'\\''")
    return f"file '{escaped}'"


class SphericalRenderer:
    """Render validated virtual-camera tracks with FFmpeg's v360 filter."""

    def __init__(
        self,
        *,
        ffmpeg_executable: str = "ffmpeg",
        ffprobe_executable: str = "ffprobe",
        renderer_version: str = RENDERER_VERSION,
    ) -> None:
        self.capabilities = probe_ffmpeg_capabilities(
            ffmpeg_executable,
            ffprobe_executable,
        )
        self.renderer_version = renderer_version
        missing = []
        if not self.capabilities.v360_available:
            missing.append("v360 filter")
        if not self.capabilities.libx264_available:
            missing.append("libx264 encoder")
        if not self.capabilities.concat_demuxer_available:
            missing.append("concat demuxer")
        if missing:
            raise SphericalCapabilityError(
                "FFmpeg lacks required spherical rendering capabilities.",
                details={"missing": missing},
            )

    @property
    def ffmpeg(self) -> str:
        return self.capabilities.ffmpeg_executable

    @property
    def ffprobe(self) -> str:
        return self.capabilities.ffprobe_executable

    def _run_checked(self, command: list[str], operation: str) -> None:
        completed = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise SphericalRenderError(
                f"FFmpeg {operation} failed.",
                details={
                    "command": command,
                    "exit_code": completed.returncode,
                    "stderr": completed.stderr[-16000:],
                },
            )

    def _validate_source(
        self,
        request: ReframeRenderRequest,
    ) -> tuple[Path, str, MediaProbe, list[str]]:
        source = request.asset.master_path.expanduser().resolve(strict=False)
        if not source.is_file():
            raise SphericalValidationError(
                "Spherical master does not exist.",
                details={"path": str(source)},
            )
        source_hash = sha256_file(source)
        if source_hash not in request.asset.source_hashes:
            raise SphericalValidationError(
                "Spherical master hash does not match the asset contract.",
                details={"actual_sha256": source_hash},
            )

        probe = probe_media(source, self.ffprobe)
        if (probe.width, probe.height) != (request.asset.width, request.asset.height):
            raise SphericalValidationError(
                "Spherical master dimensions do not match the asset contract.",
                details={
                    "actual": [probe.width, probe.height],
                    "declared": [request.asset.width, request.asset.height],
                },
            )
        if request.track.source_end_seconds > probe.duration_seconds + 0.05:
            raise SphericalValidationError(
                "Camera track exceeds the probed source duration.",
                details={"probed_duration_seconds": probe.duration_seconds},
            )

        warnings: list[str] = []
        if not math.isclose(probe.fps, request.asset.fps, rel_tol=0.001, abs_tol=0.01):
            warnings.append(
                f"Declared FPS {request.asset.fps} differs from probed FPS {probe.fps}; "
                "the declared FPS controls fallback segment frame counts."
            )
        audio_index = request.asset.audio_stream_index
        if audio_index is not None and audio_index >= probe.audio_stream_count:
            raise SphericalValidationError(
                "Requested audio stream is unavailable.",
                details={
                    "audio_stream_index": audio_index,
                    "audio_stream_count": probe.audio_stream_count,
                },
            )
        if audio_index is None and probe.audio_stream_count:
            warnings.append(
                "The source contains audio but audio_stream_index is null; output is video-only."
            )
        return source, source_hash, probe, warnings

    def _segment_command(
        self,
        request: ReframeRenderRequest,
        source: Path,
        segment: RenderSegment,
        destination: Path,
    ) -> tuple[list[str], str]:
        camera = segment.camera
        track = request.track
        codec = request.codec_settings
        filter_graph = ":".join(
            [
                "v360=input=equirect",
                "output=flat",
                "interp=linear",
                f"w={track.output_width}",
                f"h={track.output_height}",
                f"yaw={_format_number(camera.yaw)}",
                f"pitch={_format_number(camera.pitch)}",
                f"roll={_format_number(camera.roll)}",
                f"h_fov={_format_number(camera.horizontal_fov)}",
            ]
        )
        filter_graph += (
            f",fps=fps={_format_number(request.asset.fps)},"
            f"setsar=1,format=pix_fmts={codec.pixel_format}"
        )
        command = [
            self.ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
            "-ss",
            _format_number(segment.source_start_seconds),
            "-i",
            str(source),
            "-map",
            "0:v:0",
            "-frames:v",
            str(segment.frame_count),
            "-vf",
            filter_graph,
            "-an",
            "-c:v",
            codec.video_codec,
            "-preset",
            codec.preset,
            "-crf",
            str(codec.crf),
            "-pix_fmt",
            codec.pixel_format,
            "-video_track_timescale",
            "90000",
            "-movflags",
            "+faststart",
            str(destination),
        ]
        return command, filter_graph

    def _commit_pair(
        self,
        staged_output: Path,
        manifest: ReframeRenderManifest,
        *,
        overwrite: bool,
        staging_dir: Path,
    ) -> None:
        output = manifest.output_path
        sidecar = manifest_path_for(output)
        output.parent.mkdir(parents=True, exist_ok=True)
        staged_manifest = staging_dir / "result.manifest.json"
        atomic_write_json(staged_manifest, manifest)

        output_backup = staging_dir / "previous-output.mp4"
        manifest_backup = staging_dir / "previous-manifest.json"
        if output.exists() and not overwrite:
            raise SphericalValidationError(
                "Output already exists and overwrite is disabled.",
                details={"path": str(output)},
            )

        try:
            if output.exists():
                os.replace(output, output_backup)
            if sidecar.exists():
                os.replace(sidecar, manifest_backup)
            os.replace(staged_output, output)
            os.replace(staged_manifest, sidecar)
        except Exception:
            output.unlink(missing_ok=True)
            sidecar.unlink(missing_ok=True)
            if output_backup.exists():
                os.replace(output_backup, output)
            if manifest_backup.exists():
                os.replace(manifest_backup, sidecar)
            raise

    def _materialize_cache_hit(
        self,
        request: ReframeRenderRequest,
        lookup: CacheLookup,
    ) -> ReframeRenderManifest:
        assert lookup.output_path is not None
        assert lookup.manifest is not None
        cached = lookup.manifest
        manifest = cached.model_copy(
            update={
                "output_path": request.output_path,
                "cache_hit": True,
                "creation_timestamp": datetime.now(timezone.utc),
                "warnings": [
                    *cached.warnings,
                    "Validated cache hit; FFmpeg rendering was skipped.",
                ],
            }
        )

        if request.output_path.exists() and not request.overwrite:
            if sha256_file(request.output_path) != manifest.output_hash:
                raise SphericalValidationError(
                    "Output already exists with different content.",
                    details={"path": str(request.output_path)},
                )
            atomic_write_json(manifest_path_for(request.output_path), manifest)
            return manifest

        request.output_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix=".spherical-cache-hit-",
            dir=request.output_path.parent,
        ) as temporary_name:
            temporary = Path(temporary_name)
            staged_output = temporary / "output.mp4"
            shutil.copy2(lookup.output_path, staged_output)
            if sha256_file(staged_output) != manifest.output_hash:
                raise SphericalCacheError("Materialized cache copy failed hash validation.")
            self._commit_pair(
                staged_output,
                manifest,
                overwrite=request.overwrite,
                staging_dir=temporary,
            )
        return manifest

    def render(self, request: ReframeRenderRequest) -> ReframeRenderManifest:
        """Render a validated request and expose only typed spherical failures."""

        try:
            return self._render(request)
        except (
            SphericalCacheError,
            SphericalCapabilityError,
            SphericalProbeError,
            SphericalRenderError,
            SphericalValidationError,
        ):
            raise
        except Exception as exc:
            raise SphericalRenderError(
                "Unexpected spherical rendering failure.",
                details={"error_type": type(exc).__name__, "error": str(exc)},
            ) from exc

    def _render(self, request: ReframeRenderRequest) -> ReframeRenderManifest:
        """Internal renderer implementation after public error normalization."""

        started = time.perf_counter()
        source, source_hash, _, warnings = self._validate_source(request)
        identity = compute_render_identity(
            request,
            source_hash=source_hash,
            ffmpeg_version=self.capabilities.version_line,
            renderer_version=self.renderer_version,
        )
        cache = RenderCache(request.cache_dir)
        cache_lookup = cache.lookup(identity.render_key)
        if request.use_cache and cache_lookup.hit:
            return self._materialize_cache_hit(request, cache_lookup)

        if request.output_path.exists() and not request.overwrite:
            existing_manifest_path = manifest_path_for(request.output_path)
            if existing_manifest_path.is_file():
                try:
                    existing = load_json_model(
                        existing_manifest_path,
                        ReframeRenderManifest,
                    )
                    if (
                        existing.render_key == identity.render_key
                        and sha256_file(request.output_path) == existing.output_hash
                    ):
                        return existing
                except Exception:
                    pass
            raise SphericalValidationError(
                "Output already exists and does not match this render request.",
                details={"path": str(request.output_path)},
            )

        segments = build_render_segments(request)
        request.output_path.parent.mkdir(parents=True, exist_ok=True)
        exact_commands: list[list[str]] = []
        exact_filters: list[str] = []
        if self.capabilities.runtime_commands_supported:
            strategy_decision = (
                "Runtime v360 commands passed the feature probe; selected the "
                "cross-version frame-aligned segment strategy for deterministic output."
            )
        else:
            strategy_decision = (
                "Selected frame-aligned static v360 segments plus concat because "
                "runtime camera commands were not verified."
            )
        fallback_decisions = [
            self.capabilities.runtime_probe_message,
            strategy_decision,
        ]
        if request.use_cache and not cache_lookup.hit:
            fallback_decisions.append(f"Cache miss: {cache_lookup.reason}.")

        with tempfile.TemporaryDirectory(
            prefix=".spherical-render-",
            dir=request.output_path.parent,
        ) as temporary_name:
            temporary = Path(temporary_name)
            segment_paths: list[Path] = []
            for segment in segments:
                segment_path = temporary / f"segment-{segment.index:05d}.mp4"
                command, filter_graph = self._segment_command(
                    request,
                    source,
                    segment,
                    segment_path,
                )
                exact_commands.append(command)
                exact_filters.append(filter_graph)
                self._run_checked(command, f"segment {segment.index}")
                segment_paths.append(segment_path)

            concat_list = temporary / "segments.ffconcat"
            concat_list.write_text(
                "ffconcat version 1.0\n"
                + "\n".join(_concat_file_line(path) for path in segment_paths)
                + "\n",
                encoding="utf-8",
            )
            concatenated = temporary / "concatenated-video.mp4"
            concat_command = [
                self.ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(concat_list),
                "-map",
                "0:v:0",
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                str(concatenated),
            ]
            exact_commands.append(concat_command)
            self._run_checked(concat_command, "segment concatenation")

            staged_output = temporary / "final-output.mp4"
            audio_index = request.asset.audio_stream_index
            if audio_index is not None:
                mux_command = [
                    self.ffmpeg,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-nostdin",
                    "-y",
                    "-i",
                    str(concatenated),
                    "-ss",
                    _format_number(request.track.source_start_seconds),
                    "-t",
                    _format_number(
                        request.track.source_end_seconds
                        - request.track.source_start_seconds
                    ),
                    "-i",
                    str(source),
                    "-map",
                    "0:v:0",
                    "-map",
                    f"1:a:{audio_index}",
                    "-c:v",
                    "copy",
                    "-c:a",
                    request.codec_settings.audio_codec,
                    "-b:a",
                    request.codec_settings.audio_bitrate,
                    "-shortest",
                    "-movflags",
                    "+faststart",
                    str(staged_output),
                ]
            else:
                mux_command = [
                    self.ffmpeg,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-nostdin",
                    "-y",
                    "-i",
                    str(concatenated),
                    "-map",
                    "0:v:0",
                    "-c",
                    "copy",
                    "-movflags",
                    "+faststart",
                    str(staged_output),
                ]
            exact_commands.append(mux_command)
            self._run_checked(mux_command, "final audio/video mux")

            output_probe = probe_media(staged_output, self.ffprobe)
            if (output_probe.width, output_probe.height) != (
                request.track.output_width,
                request.track.output_height,
            ):
                raise SphericalRenderError(
                    "Rendered output dimensions failed validation.",
                    details={
                        "actual": [output_probe.width, output_probe.height],
                        "expected": [
                            request.track.output_width,
                            request.track.output_height,
                        ],
                    },
                )
            expected_frames = sum(segment.frame_count for segment in segments)
            expected_duration = expected_frames / request.asset.fps
            duration_tolerance = max(0.35, 2.0 / request.asset.fps)
            if abs(output_probe.duration_seconds - expected_duration) > duration_tolerance:
                raise SphericalRenderError(
                    "Rendered output duration failed validation.",
                    details={
                        "actual": output_probe.duration_seconds,
                        "expected": expected_duration,
                        "tolerance": duration_tolerance,
                    },
                )
            audio_preserved = audio_index is not None
            if audio_preserved and output_probe.audio_stream_count == 0:
                raise SphericalRenderError("Rendered output lost the requested audio stream.")

            output_hash = sha256_file(staged_output)
            manifest = ReframeRenderManifest(
                render_key=identity.render_key,
                asset_id=request.asset.asset_id,
                track_id=request.track.track_id,
                source_hashes=request.asset.source_hashes,
                camera_track_hash=identity.camera_track_hash,
                ffmpeg_version=self.capabilities.version_line,
                ffmpeg_capabilities=self.capabilities.model_dump(mode="json"),
                exact_filter_graphs=exact_filters,
                exact_commands=exact_commands,
                output_path=request.output_path,
                output_hash=output_hash,
                render_duration_seconds=time.perf_counter() - started,
                source_start_seconds=request.track.source_start_seconds,
                source_end_seconds=request.track.source_end_seconds,
                output_width=request.track.output_width,
                output_height=request.track.output_height,
                output_fps=request.asset.fps,
                codec_settings=request.codec_settings,
                hardware_acceleration={"enabled": False, "backend": "none"},
                selected_backend=(
                    "ffmpeg-v360-static"
                    if len(segments) == 1
                    else "ffmpeg-v360-segmented-concat"
                ),
                renderer_version=self.renderer_version,
                application_version=application_version,
                warnings=warnings,
                fallback_decisions=fallback_decisions,
                audio_preserved=audio_preserved,
                cache_hit=False,
            )

            self._commit_pair(
                staged_output,
                manifest,
                overwrite=request.overwrite,
                staging_dir=temporary,
            )

            # Cache only after the requested artifact and sidecar commit. A
            # destination commit failure must never leave a valid cache entry for
            # an operation that the caller observed as failed.
            if request.use_cache:
                try:
                    cache.store(request.output_path, manifest)
                except SphericalCacheError as exc:
                    manifest = manifest.model_copy(
                        update={
                            "warnings": [
                                *manifest.warnings,
                                f"Render cache write failed: {exc.message}",
                            ],
                            "fallback_decisions": [
                                *manifest.fallback_decisions,
                                "Returned the validated render without a cache entry.",
                            ],
                        }
                    )
                    atomic_write_json(manifest_path_for(request.output_path), manifest)
            return manifest
