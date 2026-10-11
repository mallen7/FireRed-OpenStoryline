"""Proxy and inspection media generation for spherical MCP nodes."""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

from open_storyline.spherical.errors import (
    SphericalCacheError,
    SphericalCapabilityError,
    SphericalRenderError,
    SphericalValidationError,
)
from open_storyline.spherical.ffmpeg import probe_media
from open_storyline.spherical.manifests import (
    atomic_write_json,
    sha256_file,
    sha256_json,
)
from open_storyline.spherical.schemas import SphericalAsset


PROXY_GENERATOR_VERSION = "1.0.0"
INSPECTION_GENERATOR_VERSION = "1.0.0"


@dataclass(frozen=True)
class ProxyArtifact:
    path: Path
    output_hash: str
    width: int
    height: int
    cache_hit: bool
    command: list[str]


@dataclass(frozen=True)
class ContactSheetArtifacts:
    equirectangular_path: Path
    perspective_path: Path | None
    sample_timestamps: list[float]
    perspective_views: list[dict[str, float | str]]
    cache_hit: bool
    commands: list[list[str]]


def _resolve_executable(name: str) -> str:
    resolved = shutil.which(name)
    if not resolved:
        raise SphericalCapabilityError(
            f"Required executable is unavailable: {name}",
            details={"executable": name},
        )
    return resolved


def _run(command: list[str], operation: str) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise SphericalRenderError(
            f"Could not start FFmpeg for {operation}.",
            details={"command": command, "error": str(exc)},
        ) from exc
    if completed.returncode != 0:
        raise SphericalRenderError(
            f"FFmpeg {operation} failed.",
            details={
                "command": command,
                "exit_code": completed.returncode,
                "stderr": completed.stderr[-16000:],
            },
        )
    return completed


def _ffmpeg_version(ffmpeg: str) -> str:
    completed = _run([ffmpeg, "-version"], "version probe")
    return completed.stdout.splitlines()[0].strip()


def _atomic_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _load_cache_manifest(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _valid_cached_file(
    path: Path,
    manifest: dict[str, Any] | None,
    *,
    key: str,
    hash_field: str,
) -> bool:
    if manifest is None or manifest.get("cache_key") != key or not path.is_file():
        return False
    expected = manifest.get(hash_field)
    return isinstance(expected, str) and sha256_file(path) == expected


def generate_proxy(
    asset: SphericalAsset,
    destination: str | Path,
    *,
    cache_dir: str | Path,
    proxy_width: int,
    ffmpeg_executable: str = "ffmpeg",
    ffprobe_executable: str = "ffprobe",
) -> ProxyArtifact:
    """Generate or materialize a validated, content-addressed 2:1 proxy."""

    if proxy_width <= 0 or proxy_width % 2:
        raise SphericalValidationError("Proxy width must be a positive even number.")
    ffmpeg = _resolve_executable(ffmpeg_executable)
    ffprobe = _resolve_executable(ffprobe_executable)
    version = _ffmpeg_version(ffmpeg)
    source = asset.master_path.expanduser().resolve()
    source_hash = sha256_file(source)
    width = min(proxy_width, asset.width)
    width -= width % 2
    height = width // 2
    key = sha256_json(
        {
            "source_hash": source_hash,
            "width": width,
            "height": height,
            "ffmpeg_version": version,
            "generator_version": PROXY_GENERATOR_VERSION,
        }
    )
    root = Path(cache_dir).expanduser().resolve(strict=False) / "proxies"
    entry = root / key
    cached_proxy = entry / "proxy.mp4"
    cached_manifest_path = entry / "manifest.json"
    manifest = _load_cache_manifest(cached_manifest_path)
    cache_hit = _valid_cached_file(
        cached_proxy,
        manifest,
        key=key,
        hash_field="output_hash",
    )

    if not cache_hit:
        root.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=f".{key}.", dir=root))
        try:
            staged_proxy = temporary / "proxy.mp4"
            filter_graph = f"scale=w={width}:h={height}:flags=lanczos,format=yuv420p"
            command = [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-y",
                "-i",
                str(source),
                "-map",
                "0:v:0",
                "-vf",
                filter_graph,
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "23",
                "-pix_fmt",
                "yuv420p",
            ]
            if asset.audio_stream_index is None:
                command.extend(["-an"])
            else:
                command.extend(
                    [
                        "-map",
                        f"0:a:{asset.audio_stream_index}",
                        "-c:a",
                        "aac",
                        "-b:a",
                        "128k",
                    ]
                )
            command.extend(["-movflags", "+faststart", str(staged_proxy)])
            _run(command, "spherical proxy generation")
            probe = probe_media(staged_proxy, ffprobe)
            if (probe.width, probe.height) != (width, height):
                raise SphericalRenderError(
                    "Generated spherical proxy dimensions failed validation."
                )
            output_hash = sha256_file(staged_proxy)
            atomic_write_json(
                temporary / "manifest.json",
                {
                    "schema_version": "1.0",
                    "cache_key": key,
                    "source_hash": source_hash,
                    "output_hash": output_hash,
                    "width": width,
                    "height": height,
                    "ffmpeg_version": version,
                    "generator_version": PROXY_GENERATOR_VERSION,
                    "command": command,
                },
            )
            if entry.exists():
                shutil.rmtree(entry)
            os.replace(temporary, entry)
        except Exception as exc:
            if temporary.exists():
                shutil.rmtree(temporary, ignore_errors=True)
            if isinstance(exc, (SphericalRenderError, SphericalValidationError)):
                raise
            raise SphericalCacheError(
                "Spherical proxy cache write failed.",
                details={"cache_key": key, "error": str(exc)},
            ) from exc

        cached_proxy = entry / "proxy.mp4"
        cached_manifest_path = entry / "manifest.json"
        manifest = _load_cache_manifest(cached_manifest_path)
        if not _valid_cached_file(
            cached_proxy,
            manifest,
            key=key,
            hash_field="output_hash",
        ):
            raise SphericalCacheError("New spherical proxy cache entry is invalid.")

    assert manifest is not None
    target = Path(destination).expanduser().resolve(strict=False)
    _atomic_copy(cached_proxy, target)
    if sha256_file(target) != manifest["output_hash"]:
        target.unlink(missing_ok=True)
        raise SphericalCacheError("Materialized spherical proxy failed hash validation.")
    return ProxyArtifact(
        path=target,
        output_hash=manifest["output_hash"],
        width=width,
        height=height,
        cache_hit=cache_hit,
        command=manifest["command"],
    )


def _sample_timestamps(duration_seconds: float, count: int) -> list[float]:
    if count < 1:
        raise SphericalValidationError("Contact-sheet sample count must be positive.")
    return [round(duration_seconds * index / count, 6) for index in range(count)]


def _extract_frame_command(
    ffmpeg: str,
    source: Path,
    timestamp: float,
    destination: Path,
    filter_graph: str,
) -> list[str]:
    return [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-i",
        str(source),
        "-ss",
        str(timestamp),
        "-frames:v",
        "1",
        "-vf",
        filter_graph,
        str(destination),
    ]


def _tile_command(
    ffmpeg: str,
    pattern: Path,
    destination: Path,
    *,
    columns: int,
    rows: int,
    frame_count: int,
) -> list[str]:
    return [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-framerate",
        "1",
        "-start_number",
        "0",
        "-i",
        str(pattern),
        "-vf",
        (
            f"tile=layout={columns}x{rows}:nb_frames={frame_count}:"
            "padding=4:margin=4:color=black"
        ),
        "-frames:v",
        "1",
        str(destination),
    ]


def _validate_image(path: Path) -> None:
    try:
        with Image.open(path) as image:
            image.verify()
    except Exception as exc:
        raise SphericalRenderError(
            "Generated contact sheet failed image validation.",
            details={"path": str(path), "error": str(exc)},
        ) from exc


def _label_image(path: Path, label: str) -> None:
    """Add a portable high-contrast tile label without FFmpeg drawtext."""

    try:
        with Image.open(path) as image:
            labeled = image.convert("RGB")
        draw = ImageDraw.Draw(labeled)
        left, top, right, bottom = draw.textbbox((0, 0), label)
        width = right - left
        height = bottom - top
        draw.rectangle((4, 4, 12 + width, 12 + height), fill=(0, 0, 0))
        draw.text((8, 8), label, fill=(255, 255, 255))
        labeled.save(path, format="PNG")
    except Exception as exc:
        raise SphericalRenderError(
            "Could not label spherical inspection frame.",
            details={"path": str(path), "label": label, "error": str(exc)},
        ) from exc


def generate_contact_sheets(
    asset: SphericalAsset,
    proxy_path: str | Path,
    output_dir: str | Path,
    *,
    cache_dir: str | Path,
    sample_count: int = 4,
    thumbnail_width: int = 320,
    include_perspective: bool = True,
    perspective_time_seconds: float | None = None,
    ffmpeg_executable: str = "ffmpeg",
) -> ContactSheetArtifacts:
    """Generate cached equirectangular and six-view perspective contact sheets."""

    if thumbnail_width <= 0 or thumbnail_width % 2:
        raise SphericalValidationError(
            "Contact-sheet thumbnail width must be a positive even number."
        )
    if not 1 <= sample_count <= 12:
        raise SphericalValidationError("Contact-sheet sample count must be 1 through 12.")
    ffmpeg = _resolve_executable(ffmpeg_executable)
    version = _ffmpeg_version(ffmpeg)
    source = Path(proxy_path).expanduser().resolve()
    proxy_hash = sha256_file(source)
    timestamps = _sample_timestamps(asset.duration_seconds, sample_count)
    frame_duration = 1.0 / asset.fps
    perspective_time = (
        asset.duration_seconds / 2.0
        if perspective_time_seconds is None
        else perspective_time_seconds
    )
    perspective_time = max(
        0.0,
        min(perspective_time, max(0.0, asset.duration_seconds - frame_duration)),
    )
    perspective_views: list[dict[str, float | str]] = [
        {"name": "front", "yaw": 0.0, "pitch": 0.0},
        {"name": "right", "yaw": 90.0, "pitch": 0.0},
        {"name": "back", "yaw": 180.0, "pitch": 0.0},
        {"name": "left", "yaw": -90.0, "pitch": 0.0},
        {"name": "up", "yaw": 0.0, "pitch": 60.0},
        {"name": "down", "yaw": 0.0, "pitch": -60.0},
    ]
    key = sha256_json(
        {
            "proxy_hash": proxy_hash,
            "timestamps": timestamps,
            "thumbnail_width": thumbnail_width,
            "include_perspective": include_perspective,
            "perspective_time_seconds": perspective_time,
            "perspective_views": perspective_views,
            "ffmpeg_version": version,
            "generator_version": INSPECTION_GENERATOR_VERSION,
        }
    )
    root = Path(cache_dir).expanduser().resolve(strict=False) / "inspections"
    entry = root / key
    cached_eq = entry / "equirectangular-contact-sheet.png"
    cached_perspective = entry / "perspective-contact-sheet.png"
    cached_manifest_path = entry / "manifest.json"
    manifest = _load_cache_manifest(cached_manifest_path)
    cache_hit = _valid_cached_file(
        cached_eq,
        manifest,
        key=key,
        hash_field="equirectangular_hash",
    ) and (
        not include_perspective
        or _valid_cached_file(
            cached_perspective,
            manifest,
            key=key,
            hash_field="perspective_hash",
        )
    )

    if not cache_hit:
        root.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=f".{key}.", dir=root))
        commands: list[list[str]] = []
        try:
            for index, timestamp in enumerate(timestamps):
                frame_path = temporary / f"eq-{index:03d}.png"
                command = _extract_frame_command(
                    ffmpeg,
                    source,
                    timestamp,
                    frame_path,
                    f"scale=w={thumbnail_width}:h={thumbnail_width // 2}:flags=lanczos",
                )
                commands.append(command)
                _run(command, f"equirectangular contact frame {index}")
                _label_image(frame_path, f"t={timestamp:.2f}s")
            columns = 2 if sample_count > 1 else 1
            rows = math.ceil(sample_count / columns)
            eq_sheet = temporary / cached_eq.name
            command = _tile_command(
                ffmpeg,
                temporary / "eq-%03d.png",
                eq_sheet,
                columns=columns,
                rows=rows,
                frame_count=sample_count,
            )
            commands.append(command)
            _run(command, "equirectangular contact-sheet tiling")
            _validate_image(eq_sheet)

            perspective_sheet: Path | None = None
            if include_perspective:
                view_width = thumbnail_width
                view_height = max(2, int(round(view_width * 9 / 16)))
                view_height += view_height % 2
                for index, view in enumerate(perspective_views):
                    frame_path = temporary / f"view-{index:03d}.png"
                    filter_graph = (
                        "v360=input=equirect:output=flat:interp=linear:"
                        f"w={view_width}:h={view_height}:"
                        f"yaw={view['yaw']}:pitch={view['pitch']}:h_fov=90"
                    )
                    command = _extract_frame_command(
                        ffmpeg,
                        source,
                        perspective_time,
                        frame_path,
                        filter_graph,
                    )
                    commands.append(command)
                    _run(command, f"perspective contact frame {view['name']}")
                    _label_image(frame_path, str(view["name"]).upper())
                perspective_sheet = temporary / cached_perspective.name
                command = _tile_command(
                    ffmpeg,
                    temporary / "view-%03d.png",
                    perspective_sheet,
                    columns=3,
                    rows=2,
                    frame_count=6,
                )
                commands.append(command)
                _run(command, "perspective contact-sheet tiling")
                _validate_image(perspective_sheet)

            payload = {
                "schema_version": "1.0",
                "cache_key": key,
                "proxy_hash": proxy_hash,
                "equirectangular_hash": sha256_file(eq_sheet),
                "perspective_hash": (
                    sha256_file(perspective_sheet) if perspective_sheet else None
                ),
                "sample_timestamps": timestamps,
                "perspective_time_seconds": perspective_time,
                "perspective_views": perspective_views,
                "ffmpeg_version": version,
                "generator_version": INSPECTION_GENERATOR_VERSION,
                "commands": commands,
            }
            atomic_write_json(temporary / "manifest.json", payload)
            for frame in temporary.glob("eq-*.png"):
                frame.unlink()
            for frame in temporary.glob("view-*.png"):
                frame.unlink()
            if entry.exists():
                shutil.rmtree(entry)
            os.replace(temporary, entry)
        except Exception as exc:
            if temporary.exists():
                shutil.rmtree(temporary, ignore_errors=True)
            if isinstance(exc, (SphericalRenderError, SphericalValidationError)):
                raise
            raise SphericalCacheError(
                "Spherical inspection cache write failed.",
                details={"cache_key": key, "error": str(exc)},
            ) from exc

        cached_eq = entry / "equirectangular-contact-sheet.png"
        cached_perspective = entry / "perspective-contact-sheet.png"
        cached_manifest_path = entry / "manifest.json"
        manifest = _load_cache_manifest(cached_manifest_path)

    if not _valid_cached_file(
        cached_eq,
        manifest,
        key=key,
        hash_field="equirectangular_hash",
    ):
        raise SphericalCacheError("Equirectangular contact-sheet cache is invalid.")
    if include_perspective and not _valid_cached_file(
        cached_perspective,
        manifest,
        key=key,
        hash_field="perspective_hash",
    ):
        raise SphericalCacheError("Perspective contact-sheet cache is invalid.")
    assert manifest is not None

    output_root = Path(output_dir).expanduser().resolve(strict=False)
    eq_destination = output_root / cached_eq.name
    perspective_destination = (
        output_root / cached_perspective.name if include_perspective else None
    )
    _atomic_copy(cached_eq, eq_destination)
    if perspective_destination is not None:
        _atomic_copy(cached_perspective, perspective_destination)
    return ContactSheetArtifacts(
        equirectangular_path=eq_destination,
        perspective_path=perspective_destination,
        sample_timestamps=manifest["sample_timestamps"],
        perspective_views=manifest["perspective_views"],
        cache_hit=cache_hit,
        commands=manifest["commands"],
    )
