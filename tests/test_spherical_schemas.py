from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError

from open_storyline.spherical.schemas import (
    CameraKeyframe,
    ReframeRenderRequest,
    SphericalAsset,
    VirtualCameraTrack,
)


HASH = "a" * 64


def asset(**updates: object) -> SphericalAsset:
    values: dict[str, object] = {
        "asset_id": "asset-1",
        "source_paths": ["master.mp4"],
        "source_hashes": [HASH],
        "master_path": "master.mp4",
        "width": 640,
        "height": 320,
        "fps": 30.0,
        "duration_seconds": 10.0,
    }
    values.update(updates)
    return SphericalAsset(**values)


def track(**updates: object) -> VirtualCameraTrack:
    values: dict[str, object] = {
        "track_id": "track-1",
        "asset_id": "asset-1",
        "name": "test",
        "output_width": 320,
        "output_height": 180,
        "source_start_seconds": 1.0,
        "source_end_seconds": 3.0,
        "keyframes": [
            CameraKeyframe(time_seconds=1.0),
            CameraKeyframe(time_seconds=3.0, yaw=90.0),
        ],
    }
    values.update(updates)
    return VirtualCameraTrack(**values)


class SphericalSchemaTests(unittest.TestCase):
    def test_asset_requires_positive_approximately_two_to_one_dimensions(self) -> None:
        for changes in (
            {"width": 0},
            {"height": -1},
            {"width": 640, "height": 480},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                asset(**changes)

    def test_asset_rejects_unsupported_projection_and_invalid_hash_mapping(self) -> None:
        with self.assertRaises(ValidationError):
            asset(projection="cubemap")
        with self.assertRaises(ValidationError):
            asset(source_hashes=["not-a-hash"])
        with self.assertRaises(ValidationError):
            asset(source_paths=["one.mp4", "two.mp4"])

    def test_track_rejects_invalid_ranges_and_keyframes(self) -> None:
        invalid_values = (
            {"source_start_seconds": 3.0, "source_end_seconds": 3.0},
            {"keyframes": []},
            {"keyframes": [CameraKeyframe(time_seconds=0.5)]},
            {
                "keyframes": [
                    CameraKeyframe(time_seconds=2.0),
                    CameraKeyframe(time_seconds=1.5),
                ]
            },
            {
                "keyframes": [
                    CameraKeyframe(time_seconds=2.0),
                    CameraKeyframe(time_seconds=2.0),
                ]
            },
        )
        for changes in invalid_values:
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                track(**changes)

    def test_camera_normalizes_wrapped_yaw_and_roll(self) -> None:
        camera = CameraKeyframe(time_seconds=0.0, yaw=540.0, roll=-540.0)
        self.assertEqual(camera.yaw, 180.0)
        self.assertEqual(camera.roll, -180.0)

    def test_camera_rejects_invalid_pitch_fov_and_nonfinite_values(self) -> None:
        for changes in (
            {"pitch": 91.0},
            {"horizontal_fov": 180.0},
            {"horizontal_fov": 1.0},
            {"yaw": float("inf")},
            {"time_seconds": float("nan")},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                values: dict[str, object] = {"time_seconds": 0.0}
                values.update(changes)
                CameraKeyframe(**values)

    def test_supported_output_aspects_require_even_dimensions(self) -> None:
        for width, height in ((320, 180), (180, 320), (256, 256)):
            with self.subTest(size=(width, height)):
                result = track(output_width=width, output_height=height)
                self.assertEqual((result.output_width, result.output_height), (width, height))
        for width, height in ((321, 180), (320, 240)):
            with self.subTest(size=(width, height)), self.assertRaises(ValidationError):
                track(output_width=width, output_height=height)

    def test_render_request_normalizes_paths_and_blocks_workspace_escape(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_name:
            workspace = Path(temporary_name) / "workspace"
            request = ReframeRenderRequest(
                asset=asset(),
                track=track(),
                output_path="clip.mp4",
                workspace_root=workspace,
                cache_dir="cache",
            )
            resolved_workspace = workspace.resolve()
            self.assertEqual(request.output_path, resolved_workspace / "clip.mp4")
            self.assertEqual(request.cache_dir, resolved_workspace / "cache")
            with self.assertRaises(ValidationError):
                ReframeRenderRequest(
                    asset=asset(),
                    track=track(),
                    output_path="../escape.mp4",
                    workspace_root=workspace,
                    cache_dir="cache",
                )

    def test_render_request_rejects_asset_mismatch_and_out_of_range_track(self) -> None:
        with self.assertRaises(ValidationError):
            ReframeRenderRequest(
                asset=asset(),
                track=track(asset_id="other"),
                output_path="clip.mp4",
                workspace_root="outputs",
                cache_dir="cache",
            )
        with self.assertRaises(ValidationError):
            ReframeRenderRequest(
                asset=asset(duration_seconds=2.0),
                track=track(),
                output_path="clip.mp4",
                workspace_root="outputs",
                cache_dir="cache",
            )


if __name__ == "__main__":
    unittest.main()
