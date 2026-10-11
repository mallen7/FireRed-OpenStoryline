"""Dependency-free synthetic equirectangular fixture generation."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from open_storyline.spherical.errors import (
    SphericalCapabilityError,
    SphericalRenderError,
    SphericalValidationError,
)
from open_storyline.spherical.ffmpeg import probe_media
from open_storyline.spherical.manifests import atomic_write_json, sha256_file
from open_storyline.spherical.schemas import (
    CameraKeyframe,
    SphericalAsset,
    VirtualCameraTrack,
)


FIXTURE_GENERATOR_VERSION: Final = "1.0.0"

# A deliberately small 5x7 font keeps fixture generation independent of FFmpeg's
# optional drawtext/freetype support and of imaging libraries such as Pillow.
_GLYPHS: dict[str, tuple[str, ...]] = {
    " ": ("00000",) * 7,
    "-": ("00000", "00000", "00000", "11111", "00000", "00000", "00000"),
    ":": ("00000", "00100", "00100", "00000", "00100", "00100", "00000"),
    "0": ("01110", "10001", "10011", "10101", "11001", "10001", "01110"),
    "1": ("00100", "01100", "00100", "00100", "00100", "00100", "01110"),
    "2": ("01110", "10001", "00001", "00010", "00100", "01000", "11111"),
    "3": ("11110", "00001", "00001", "01110", "00001", "00001", "11110"),
    "4": ("00010", "00110", "01010", "10010", "11111", "00010", "00010"),
    "5": ("11111", "10000", "10000", "11110", "00001", "00001", "11110"),
    "6": ("01110", "10000", "10000", "11110", "10001", "10001", "01110"),
    "7": ("11111", "00001", "00010", "00100", "01000", "01000", "01000"),
    "8": ("01110", "10001", "10001", "01110", "10001", "10001", "01110"),
    "9": ("01110", "10001", "10001", "01111", "00001", "00001", "01110"),
    "A": ("01110", "10001", "10001", "11111", "10001", "10001", "10001"),
    "C": ("01111", "10000", "10000", "10000", "10000", "10000", "01111"),
    "H": ("10001", "10001", "10001", "11111", "10001", "10001", "10001"),
    "I": ("11111", "00100", "00100", "00100", "00100", "00100", "11111"),
    "P": ("11110", "10001", "10001", "11110", "10000", "10000", "10000"),
    "T": ("11111", "00100", "00100", "00100", "00100", "00100", "00100"),
    "W": ("10001", "10001", "10001", "10101", "10101", "10101", "01010"),
    "Y": ("10001", "10001", "01010", "00100", "00100", "00100", "00100"),
}


@dataclass(frozen=True)
class FixtureArtifacts:
    video: Path
    asset: Path
    static_track: Path
    moving_track: Path
    seam_track: Path
    generation_manifest: Path

    def model_paths(self) -> list[Path]:
        return [self.asset, self.static_track, self.moving_track, self.seam_track]


class _Canvas:
    def __init__(self, width: int, height: int) -> None:
        self.width = width
        self.height = height
        self.pixels = bytearray(width * height * 3)
        for y in range(height):
            latitude_mix = abs((y / max(1, height - 1)) * 2.0 - 1.0)
            for x in range(width):
                longitude_mix = x / max(1, width - 1)
                self.set_pixel(
                    x,
                    y,
                    (
                        int(18 + 34 * longitude_mix),
                        int(30 + 25 * (1.0 - latitude_mix)),
                        int(54 + 45 * latitude_mix),
                    ),
                )

    def set_pixel(self, x: int, y: int, color: tuple[int, int, int]) -> None:
        if not (0 <= x < self.width and 0 <= y < self.height):
            return
        offset = (y * self.width + x) * 3
        self.pixels[offset : offset + 3] = bytes(color)

    def line_vertical(self, x: int, color: tuple[int, int, int], width: int = 1) -> None:
        for line_x in range(x - width // 2, x + (width + 1) // 2):
            for y in range(self.height):
                self.set_pixel(line_x, y, color)

    def line_horizontal(self, y: int, color: tuple[int, int, int], width: int = 1) -> None:
        for line_y in range(y - width // 2, y + (width + 1) // 2):
            for x in range(self.width):
                self.set_pixel(x, line_y, color)

    def rectangle(
        self,
        x: int,
        y: int,
        width: int,
        height: int,
        color: tuple[int, int, int],
    ) -> None:
        for row in range(y, y + height):
            for column in range(x, x + width):
                self.set_pixel(column, row, color)

    def text(
        self,
        x: int,
        y: int,
        value: str,
        *,
        scale: int,
        color: tuple[int, int, int],
        background: tuple[int, int, int] = (8, 12, 20),
    ) -> None:
        value = value.upper()
        glyph_width = 6 * scale
        self.rectangle(
            x - scale,
            y - scale,
            max(scale, len(value) * glyph_width + scale),
            9 * scale,
            background,
        )
        cursor = x
        for character in value:
            glyph = _GLYPHS.get(character, _GLYPHS[" "])
            for row, bits in enumerate(glyph):
                for column, bit in enumerate(bits):
                    if bit == "1":
                        self.rectangle(
                            cursor + column * scale,
                            y + row * scale,
                            scale,
                            scale,
                            color,
                        )
            cursor += glyph_width

    def write_ppm(self, path: Path) -> None:
        with path.open("wb") as file_handle:
            file_handle.write(f"P6\n{self.width} {self.height}\n255\n".encode("ascii"))
            file_handle.write(self.pixels)


def _marker_x(yaw: float, width: int) -> int:
    return int(round(((yaw + 180.0) / 360.0) * (width - 1)))


def _marker_y(pitch: float, height: int) -> int:
    return int(round(((90.0 - pitch) / 180.0) * (height - 1)))


def _make_labeled_background(path: Path, width: int, height: int) -> None:
    canvas = _Canvas(width, height)
    for yaw in range(-180, 181, 30):
        canvas.line_vertical(
            _marker_x(float(yaw), width),
            (250, 210, 70) if yaw == 0 else (100, 120, 145),
            width=3 if yaw == 0 else 1,
        )
    for pitch in range(-90, 91, 30):
        canvas.line_horizontal(
            _marker_y(float(pitch), height),
            (80, 230, 180) if pitch == 0 else (100, 120, 145),
            width=3 if pitch == 0 else 1,
        )

    scale = 2 if width >= 900 else 1
    for yaw in range(-180, 181, 60):
        label = f"YAW {yaw}"
        label_width = len(label) * 6 * scale
        x = max(2, min(width - label_width - 2, _marker_x(float(yaw), width) - label_width // 2))
        canvas.text(x, 4, label, scale=scale, color=(255, 235, 100))
    for pitch in range(60, -61, -30):
        label = f"PITCH {pitch}"
        y = max(2, min(height - 9 * scale - 2, _marker_y(float(pitch), height) - 4 * scale))
        canvas.text(4, y, label, scale=scale, color=(115, 255, 210))
    canvas.write_ppm(path)


def _ffmpeg_fixture_command(
    ffmpeg: str,
    background: Path,
    destination: Path,
    *,
    width: int,
    height: int,
    fps: float,
    duration_seconds: float,
) -> list[str]:
    box_size = max(18, min(width, height) // 10)
    duration = f"{duration_seconds:.9f}".rstrip("0").rstrip(".")
    frame_rate = f"{fps:.9f}".rstrip("0").rstrip(".")
    moving_boxes = (
        f"[0:v][2:v]overlay=x=(W-w)*t/{duration}:"
        f"y=(H-h)/2+sin(t*PI)*(H-h)/4:eval=frame:shortest=1[first];"
        f"[first][3:v]overlay=x=(W-w)*(1-t/{duration}):"
        f"y=(H-h)*t/{duration}:eval=frame:shortest=1,"
        "format=pix_fmts=yuv420p[fixture]"
    )
    return [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-loop",
        "1",
        "-framerate",
        frame_rate,
        "-i",
        str(background),
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency=880:sample_rate=48000:duration={duration}",
        "-f",
        "lavfi",
        "-i",
        f"color=color=red:size={box_size}x{box_size}:rate={frame_rate}:duration={duration}",
        "-f",
        "lavfi",
        "-i",
        f"color=color=cyan:size={box_size}x{box_size}:rate={frame_rate}:duration={duration}",
        "-filter_complex",
        moving_boxes,
        "-t",
        duration,
        "-map",
        "[fixture]",
        "-map",
        "1:a:0",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "18",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-shortest",
        "-movflags",
        "+faststart",
        str(destination),
    ]


def generate_fixture(
    output_dir: str | Path,
    *,
    width: int = 1024,
    height: int = 512,
    fps: float = 30.0,
    duration_seconds: float = 8.0,
    output_width: int = 640,
    output_height: int = 360,
    ffmpeg_executable: str = "ffmpeg",
    ffprobe_executable: str = "ffprobe",
    overwrite: bool = False,
) -> FixtureArtifacts:
    """Generate a labeled 2:1 MP4, its asset contract, and three camera tracks."""

    if width <= 0 or height <= 0 or width % 2 or height % 2:
        raise SphericalValidationError("Fixture dimensions must be positive and even.")
    if abs(width / height - 2.0) > 0.001:
        raise SphericalValidationError("Fixture dimensions must use an exact 2:1 ratio.")
    if fps <= 0 or duration_seconds <= 0:
        raise SphericalValidationError("Fixture FPS and duration must be positive.")

    ffmpeg = shutil.which(ffmpeg_executable)
    if ffmpeg is None:
        raise SphericalCapabilityError(
            f"Required executable is unavailable: {ffmpeg_executable}"
        )
    root = Path(output_dir).expanduser().resolve(strict=False)
    root.mkdir(parents=True, exist_ok=True)
    artifacts = FixtureArtifacts(
        video=root / "fixture-equirectangular.mp4",
        asset=root / "asset.json",
        static_track=root / "camera-track-static.json",
        moving_track=root / "camera-track-moving.json",
        seam_track=root / "camera-track-seam.json",
        generation_manifest=root / "fixture-generation-manifest.json",
    )
    targets = [artifacts.video, *artifacts.model_paths(), artifacts.generation_manifest]
    existing = [path for path in targets if path.exists()]
    if existing and not overwrite:
        raise SphericalValidationError(
            "Fixture output already exists and overwrite is disabled.",
            details={"paths": [str(path) for path in existing]},
        )

    with tempfile.TemporaryDirectory(prefix=".fixture-", dir=root) as temporary_name:
        temporary = Path(temporary_name)
        background = temporary / "labeled-background.ppm"
        staged_video = temporary / artifacts.video.name
        _make_labeled_background(background, width, height)
        command = _ffmpeg_fixture_command(
            ffmpeg,
            background,
            staged_video,
            width=width,
            height=height,
            fps=fps,
            duration_seconds=duration_seconds,
        )
        completed = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise SphericalRenderError(
                "FFmpeg synthetic fixture generation failed.",
                details={
                    "command": command,
                    "exit_code": completed.returncode,
                    "stderr": completed.stderr[-16000:],
                },
            )

        version_result = subprocess.run(
            [ffmpeg, "-version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        ffmpeg_version = (
            version_result.stdout.splitlines()[0]
            if version_result.returncode == 0 and version_result.stdout
            else "unavailable"
        )

        probe = probe_media(staged_video, ffprobe_executable)
        if (probe.width, probe.height) != (width, height):
            raise SphericalRenderError("Generated fixture dimensions failed validation.")
        if probe.audio_stream_count != 1:
            raise SphericalRenderError("Generated fixture audio failed validation.")

        source_hash = sha256_file(staged_video)
        asset = SphericalAsset(
            asset_id="synthetic-equirectangular-fixture-v1",
            source_paths=[artifacts.video],
            source_hashes=[source_hash],
            camera_model="synthetic-grid",
            master_path=artifacts.video,
            width=probe.width,
            height=probe.height,
            fps=probe.fps,
            duration_seconds=probe.duration_seconds,
            audio_stream_index=0,
            metadata={
                "fixture_generator_version": FIXTURE_GENERATOR_VERSION,
                "features": [
                    "longitude markers",
                    "latitude markers",
                    "yaw labels",
                    "pitch labels",
                    "moving reference objects",
                    "880 Hz audio tone",
                ],
            },
        )
        end = min(duration_seconds, probe.duration_seconds)
        common = {
            "asset_id": asset.asset_id,
            "output_width": output_width,
            "output_height": output_height,
            "source_start_seconds": 0.0,
            "source_end_seconds": end,
        }
        static_track = VirtualCameraTrack(
            **common,
            track_id="synthetic-static-v1",
            name="static-front",
            keyframes=[CameraKeyframe(time_seconds=0.0, yaw=0.0, pitch=0.0)],
        )
        moving_track = VirtualCameraTrack(
            **common,
            track_id="synthetic-moving-v1",
            name="moving-camera",
            keyframes=[
                CameraKeyframe(
                    time_seconds=0.0,
                    yaw=-120.0,
                    pitch=-15.0,
                    horizontal_fov=105.0,
                    easing="smoothstep",
                ),
                CameraKeyframe(
                    time_seconds=end,
                    yaw=120.0,
                    pitch=20.0,
                    horizontal_fov=70.0,
                ),
            ],
        )
        seam_track = VirtualCameraTrack(
            **common,
            track_id="synthetic-seam-v1",
            name="shortest-path-seam",
            keyframes=[
                CameraKeyframe(time_seconds=0.0, yaw=170.0),
                CameraKeyframe(time_seconds=end, yaw=-170.0),
            ],
        )

        staged_models = {
            artifacts.asset: asset,
            artifacts.static_track: static_track,
            artifacts.moving_track: moving_track,
            artifacts.seam_track: seam_track,
        }
        staged_paths: dict[Path, Path] = {}
        for target, model in staged_models.items():
            staged_path = temporary / target.name
            atomic_write_json(staged_path, model)
            staged_paths[target] = staged_path

        generation_manifest = {
            "schema_version": "1.0",
            "generator_version": FIXTURE_GENERATOR_VERSION,
            "ffmpeg_version": ffmpeg_version,
            "ffmpeg_command": command,
            "video_sha256": source_hash,
            "width": probe.width,
            "height": probe.height,
            "fps": probe.fps,
            "duration_seconds": probe.duration_seconds,
            "audio_stream_count": probe.audio_stream_count,
            "artifacts": [path.name for path in targets[:-1]],
            "artifact_hashes": {
                artifacts.video.name: source_hash,
                **{
                    target.name: sha256_file(staged_path)
                    for target, staged_path in staged_paths.items()
                },
            },
        }
        staged_generation_manifest = temporary / artifacts.generation_manifest.name
        atomic_write_json(staged_generation_manifest, generation_manifest)

        # The MP4 is committed last among the generated media inputs only after it
        # and every JSON contract have validated successfully.
        for target, staged_path in staged_paths.items():
            os.replace(staged_path, target)
        os.replace(staged_generation_manifest, artifacts.generation_manifest)
        os.replace(staged_video, artifacts.video)

    return artifacts
