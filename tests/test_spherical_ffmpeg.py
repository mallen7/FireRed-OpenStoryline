from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from open_storyline.spherical.cache import RenderCache, compute_render_identity
from open_storyline.spherical.errors import SphericalValidationError
from open_storyline.spherical.ffmpeg import (
    RENDERER_VERSION,
    SphericalRenderer,
    build_render_segments,
    probe_media,
)
from open_storyline.spherical.fixtures import generate_fixture
from open_storyline.spherical.manifests import (
    load_json_model,
    manifest_path_for,
    sha256_file,
)
from open_storyline.spherical.schemas import (
    CameraKeyframe,
    ReframeRenderRequest,
    SphericalAsset,
    VirtualCameraTrack,
)


class SphericalFFmpegIntegrationTests(unittest.TestCase):
    temporary: tempfile.TemporaryDirectory[str]
    root: Path
    renderer: SphericalRenderer
    asset: SphericalAsset
    static_track: VirtualCameraTrack
    moving_track: VirtualCameraTrack

    @classmethod
    def setUpClass(cls) -> None:
        if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
            raise unittest.SkipTest("FFmpeg integration tools are unavailable")
        cls.temporary = tempfile.TemporaryDirectory(prefix="spherical-integration-")
        cls.root = Path(cls.temporary.name)
        artifacts = generate_fixture(
            cls.root / "fixture",
            width=640,
            height=320,
            fps=10.0,
            duration_seconds=2.0,
            output_width=320,
            output_height=180,
        )
        cls.artifacts = artifacts
        cls.asset = load_json_model(artifacts.asset, SphericalAsset)
        cls.static_track = load_json_model(artifacts.static_track, VirtualCameraTrack)
        cls.moving_track = load_json_model(artifacts.moving_track, VirtualCameraTrack)
        try:
            cls.renderer = SphericalRenderer()
        except Exception as exc:
            cls.temporary.cleanup()
            raise unittest.SkipTest(f"FFmpeg lacks spherical rendering support: {exc}") from exc

    @classmethod
    def tearDownClass(cls) -> None:
        if hasattr(cls, "temporary"):
            cls.temporary.cleanup()

    def request(
        self,
        track: VirtualCameraTrack,
        output_name: str,
        *,
        asset: SphericalAsset | None = None,
        use_cache: bool = True,
    ) -> ReframeRenderRequest:
        return ReframeRenderRequest(
            asset=asset or self.asset,
            track=track,
            output_path=output_name,
            workspace_root=self.root,
            cache_dir=self.root / "cache",
            sample_interval_seconds=0.5,
            use_cache=use_cache,
        )

    def test_capability_probe_and_static_16_by_9_render_with_audio(self) -> None:
        self.assertTrue(self.renderer.capabilities.v360_available)
        self.assertTrue(self.renderer.capabilities.libx264_available)
        request = self.request(self.static_track, "static.mp4")
        manifest = self.renderer.render(request)
        probe = probe_media(request.output_path)

        self.assertEqual((probe.width, probe.height), (320, 180))
        self.assertAlmostEqual(probe.duration_seconds, 2.0, delta=0.2)
        self.assertEqual(probe.audio_stream_count, 1)
        self.assertEqual(probe.audio_sample_rates, [48000])
        self.assertEqual(manifest.selected_backend, "ffmpeg-v360-static")
        self.assertTrue(manifest.audio_preserved)
        self.assertEqual(manifest.output_hash, sha256_file(request.output_path))
        self.assertTrue(manifest_path_for(request.output_path).is_file())

    def test_fixture_manifest_records_versions_features_and_hashes(self) -> None:
        payload = json.loads(self.artifacts.generation_manifest.read_text())
        self.assertEqual(payload["schema_version"], "1.0")
        self.assertTrue(payload["ffmpeg_version"].startswith("ffmpeg version"))
        self.assertEqual(
            payload["artifact_hashes"][self.artifacts.video.name],
            sha256_file(self.artifacts.video),
        )
        self.assertIn("yaw labels", self.asset.metadata["features"])
        self.assertIn("pitch labels", self.asset.metadata["features"])

    def test_fixture_reference_objects_are_visible_and_move(self) -> None:
        def centroids(time_seconds: float) -> tuple[float, float]:
            completed = subprocess.run(
                [
                    self.renderer.ffmpeg,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-i",
                    str(self.artifacts.video),
                    "-ss",
                    str(time_seconds),
                    "-frames:v",
                    "1",
                    "-f",
                    "rawvideo",
                    "-pix_fmt",
                    "rgb24",
                    "pipe:1",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )
            width = self.asset.width
            red_x: list[int] = []
            cyan_x: list[int] = []
            for pixel_index in range(0, len(completed.stdout), 3):
                red, green, blue = completed.stdout[pixel_index : pixel_index + 3]
                x = (pixel_index // 3) % width
                if red > 180 and green < 100 and blue < 100:
                    red_x.append(x)
                if red < 50 and green > 180 and blue > 200:
                    cyan_x.append(x)
            self.assertGreater(len(red_x), 100)
            self.assertGreater(len(cyan_x), 100)
            return sum(red_x) / len(red_x), sum(cyan_x) / len(cyan_x)

        early_red, early_cyan = centroids(0.2)
        late_red, late_cyan = centroids(1.6)
        self.assertGreater(late_red, early_red + 200)
        self.assertLess(late_cyan, early_cyan - 200)

    def test_cache_lookup_rejects_corrupted_output(self) -> None:
        corruption_track = VirtualCameraTrack(
            track_id="cache-corruption",
            asset_id=self.asset.asset_id,
            name="cache-corruption",
            output_width=320,
            output_height=180,
            source_start_seconds=0.0,
            source_end_seconds=2.0,
            keyframes=[CameraKeyframe(time_seconds=0.0, yaw=5.0)],
        )
        request = self.request(corruption_track, "cache-corruption-source.mp4")
        manifest = self.renderer.render(request)
        cache = RenderCache(request.cache_dir)
        cached_output = cache.entry_dir(manifest.render_key) / cache.OUTPUT_FILENAME
        cached_output.write_bytes(b"corrupt")
        lookup = cache.lookup(manifest.render_key)
        self.assertFalse(lookup.hit)
        self.assertIn("hash does not match", lookup.reason)

    def test_moving_keyframes_render_in_segments_and_repeat_uses_cache(self) -> None:
        first = self.request(self.moving_track, "moving-first.mp4")
        second = self.request(self.moving_track, "moving-cache-hit.mp4")
        first_manifest = self.renderer.render(first)
        second_manifest = self.renderer.render(second)

        self.assertFalse(first_manifest.cache_hit)
        self.assertTrue(second_manifest.cache_hit)
        self.assertEqual(first_manifest.output_hash, second_manifest.output_hash)
        self.assertEqual(first_manifest.render_key, second_manifest.render_key)
        self.assertEqual(first_manifest.selected_backend, "ffmpeg-v360-segmented-concat")
        self.assertEqual(len(first_manifest.exact_filter_graphs), 4)
        self.assertIn("yaw=", first_manifest.exact_filter_graphs[0])
        self.assertEqual(probe_media(second.output_path).audio_stream_count, 1)

    def test_source_subrange_preserves_expected_audio_duration(self) -> None:
        track = VirtualCameraTrack(
            track_id="subrange",
            asset_id=self.asset.asset_id,
            name="subrange",
            output_width=256,
            output_height=256,
            source_start_seconds=0.5,
            source_end_seconds=1.5,
            keyframes=[CameraKeyframe(time_seconds=0.5, yaw=45.0, pitch=10.0)],
        )
        request = self.request(track, "subrange-square.mp4", use_cache=False)
        manifest = self.renderer.render(request)
        probe = probe_media(request.output_path)
        self.assertEqual((probe.width, probe.height), (256, 256))
        self.assertAlmostEqual(probe.duration_seconds, 1.0, delta=0.2)
        self.assertTrue(manifest.audio_preserved)

    def test_command_generation_supports_portrait_and_cache_key_ignores_destination(self) -> None:
        portrait = VirtualCameraTrack(
            track_id="portrait",
            asset_id=self.asset.asset_id,
            name="portrait",
            output_width=180,
            output_height=320,
            source_start_seconds=0.0,
            source_end_seconds=2.0,
            keyframes=[CameraKeyframe(time_seconds=0.0, yaw=30.0, pitch=-5.0)],
        )
        first = self.request(portrait, "portrait-a.mp4")
        second = self.request(portrait, "portrait-b.mp4")
        segment = build_render_segments(first)[0]
        command, filter_graph = self.renderer._segment_command(
            first,
            self.asset.master_path,
            segment,
            self.root / "portrait-segment.mp4",
        )
        self.assertIn("w=180:h=320", filter_graph)
        self.assertIn("h_fov=90", filter_graph)
        self.assertEqual(command[command.index("-frames:v") + 1], "20")

        source_hash = sha256_file(self.asset.master_path)
        first_identity = compute_render_identity(
            first,
            source_hash=source_hash,
            ffmpeg_version=self.renderer.capabilities.version_line,
            renderer_version=RENDERER_VERSION,
        )
        second_identity = compute_render_identity(
            second,
            source_hash=source_hash,
            ffmpeg_version=self.renderer.capabilities.version_line,
            renderer_version=RENDERER_VERSION,
        )
        self.assertEqual(first_identity, second_identity)

    def test_failed_render_does_not_leave_output_or_manifest(self) -> None:
        invalid_asset = SphericalAsset.model_validate(
            {
                **self.asset.model_dump(),
                "source_hashes": ["b" * 64],
            }
        )
        request = self.request(
            self.static_track,
            "must-not-exist.mp4",
            asset=invalid_asset,
            use_cache=False,
        )
        with self.assertRaises(SphericalValidationError):
            self.renderer.render(request)
        self.assertFalse(request.output_path.exists())
        self.assertFalse(manifest_path_for(request.output_path).exists())


if __name__ == "__main__":
    unittest.main()
