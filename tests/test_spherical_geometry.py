from __future__ import annotations

import unittest

from open_storyline.spherical.geometry import (
    angles_to_equirectangular_pixel,
    ease_in,
    ease_in_out,
    ease_out,
    equirectangular_pixel_to_angles,
    great_circle_angular_distance,
    interpolate_yaw,
    linear_interpolate,
    normalize_angle,
    resample_camera_track,
    sample_camera_track,
    shortest_angle_delta,
    smooth_step,
    validate_output_aspect_ratio,
)
from open_storyline.spherical.schemas import CameraKeyframe, VirtualCameraTrack


def seam_track() -> VirtualCameraTrack:
    return VirtualCameraTrack(
        track_id="seam",
        asset_id="asset",
        name="seam",
        output_width=320,
        output_height=180,
        source_start_seconds=0.0,
        source_end_seconds=2.0,
        keyframes=[
            CameraKeyframe(time_seconds=0.0, yaw=170.0),
            CameraKeyframe(time_seconds=2.0, yaw=-170.0),
        ],
    )


class SphericalGeometryTests(unittest.TestCase):
    def test_normalization_and_shortest_path_cross_180_seam(self) -> None:
        self.assertEqual(normalize_angle(540.0), 180.0)
        self.assertEqual(normalize_angle(-540.0), -180.0)
        self.assertAlmostEqual(shortest_angle_delta(170.0, -170.0), 20.0)
        self.assertAlmostEqual(shortest_angle_delta(-170.0, 170.0), -20.0)
        self.assertEqual(interpolate_yaw(170.0, -170.0, 0.5), 180.0)
        self.assertEqual(interpolate_yaw(-170.0, 170.0, 0.5), -180.0)

    def test_interpolation_curves_have_expected_midpoints_and_bounds(self) -> None:
        self.assertEqual(linear_interpolate(10.0, 20.0, 0.5), 15.0)
        self.assertEqual(smooth_step(0.5), 0.5)
        self.assertEqual(ease_in(0.5), 0.25)
        self.assertEqual(ease_out(0.5), 0.75)
        self.assertEqual(ease_in_out(0.5), 0.5)
        self.assertEqual(linear_interpolate(0.0, 1.0, -1.0), 0.0)
        self.assertEqual(linear_interpolate(0.0, 1.0, 2.0), 1.0)

    def test_equirectangular_coordinate_round_trip(self) -> None:
        for yaw, pitch in ((-120.0, 45.0), (0.0, 0.0), (120.0, -45.0)):
            x, y = angles_to_equirectangular_pixel(yaw, pitch, 800, 400)
            actual_yaw, actual_pitch = equirectangular_pixel_to_angles(x, y, 800, 400)
            self.assertAlmostEqual(actual_yaw, yaw)
            self.assertAlmostEqual(actual_pitch, pitch)

    def test_great_circle_distance(self) -> None:
        self.assertAlmostEqual(great_circle_angular_distance(0, 0, 90, 0), 90.0)
        self.assertAlmostEqual(great_circle_angular_distance(0, 90, 100, 90), 0.0)

    def test_aspect_validation(self) -> None:
        self.assertEqual(validate_output_aspect_ratio(1920, 1080), "16:9")
        self.assertEqual(validate_output_aspect_ratio(1080, 1920), "9:16")
        self.assertEqual(validate_output_aspect_ratio(512, 512), "1:1")
        with self.assertRaises(ValueError):
            validate_output_aspect_ratio(640, 480)

    def test_track_sampling_and_resampling_preserve_seam_shortest_path(self) -> None:
        track = seam_track()
        midpoint = sample_camera_track(track, 1.0)
        self.assertEqual(midpoint.yaw, 180.0)
        samples = resample_camera_track(track, 0.5)
        self.assertEqual([sample.time_seconds for sample in samples], [0, 0.5, 1, 1.5, 2])
        deltas = [
            abs(shortest_angle_delta(left.yaw, right.yaw))
            for left, right in zip(samples, samples[1:])
        ]
        self.assertTrue(all(delta <= 5.0 for delta in deltas))


if __name__ == "__main__":
    unittest.main()
