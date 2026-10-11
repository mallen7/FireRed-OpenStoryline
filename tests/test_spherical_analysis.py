from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from open_storyline.nodes.spherical_nodes.assisted_reframing import (
    AcceptReframeSuggestionNode,
    AnalyzeSphericalAssetNode,
    InspectSubjectTrackNode,
    ListDetectedSubjectsNode,
    PreviewReframeSuggestionNode,
    RejectReframeSuggestionNode,
    SuggestEstablishingShotNode,
    SuggestGroupReframeNode,
    SuggestSubjectReframeNode,
)
from open_storyline.nodes.spherical_nodes.import_spherical_media import (
    ImportSphericalMediaNode,
)
from open_storyline.spherical.analysis_schemas import (
    DetectionKind,
    NormalizedBoundingBox,
    PerspectiveFrame,
    PerspectiveViewport,
    SphericalAnalysis,
    SphericalDetection,
    SuggestionPolicy,
    SuggestionStatus,
)
from open_storyline.spherical.fixtures import generate_fixture
from open_storyline.spherical.manifests import PUBLIC_SCHEMA_MODELS
from open_storyline.spherical.perspective import (
    build_overlapping_view_grid,
    deduplicate_spherical_detections,
    map_view_detection,
    viewport_normalized_to_angles,
)
from open_storyline.spherical.analysis_schemas import ViewDetection
from open_storyline.spherical.errors import SphericalValidationError
from open_storyline.spherical.session import SphericalSessionRepository
from open_storyline.spherical.suggestions import generate_reframe_suggestion
from open_storyline.spherical.tracking import GreedySphericalSubjectTracker
from tests.test_spherical_nodes import NodeHarness, test_settings


def spherical_detection(
    detection_id: str,
    timestamp: float,
    yaw: float,
    *,
    embedding: list[float] | None = None,
) -> SphericalDetection:
    return SphericalDetection(
        detection_id=detection_id,
        timestamp_seconds=timestamp,
        yaw=yaw,
        pitch=0.0,
        angular_width=8.0,
        angular_height=14.0,
        confidence=0.9,
        kinds=[DetectionKind.PERSON],
        labels=["person-candidate"],
        source_viewports=["test-view"],
        detector_backends=["test-detector"],
        embedding=embedding or [1.0, 0.0, 0.0],
    )


class SphericalPerspectiveAndTrackingTests(unittest.TestCase):
    def test_overlapping_grid_and_view_center_mapping_cover_full_sphere(self) -> None:
        grid = build_overlapping_view_grid(
            yaw_step_degrees=90,
            pitch_centers=(-45.0, 45.0),
            horizontal_fov=120.0,
            width=128,
            height=128,
        )
        self.assertEqual(len(grid), 8)
        for viewport in grid:
            yaw, pitch = viewport_normalized_to_angles(0.5, 0.5, viewport)
            self.assertAlmostEqual(yaw, viewport.center_yaw, places=6)
            self.assertAlmostEqual(pitch, viewport.center_pitch, places=6)
            self.assertGreater(viewport.horizontal_fov, 90.0)
            self.assertGreater(viewport.vertical_fov, 90.0)
        with self.assertRaises(SphericalValidationError):
            build_overlapping_view_grid(
                yaw_step_degrees=120,
                pitch_centers=(0.0,),
                horizontal_fov=90.0,
                width=128,
                height=128,
            )

    def test_overlap_deduplication_reconciles_two_views_across_seam(self) -> None:
        image = Path("fixture.png")
        first_view = PerspectiveViewport(
            viewport_id="seam-left",
            center_yaw=180.0,
            center_pitch=0.0,
            horizontal_fov=100.0,
            vertical_fov=100.0,
            width=128,
            height=128,
        )
        second_view = first_view.model_copy(
            update={"viewport_id": "seam-right", "center_yaw": -180.0}
        )
        first_frame = PerspectiveFrame(
            timestamp_seconds=1.0,
            viewport=first_view,
            image_path=image,
            image_sha256="a" * 64,
        )
        second_frame = PerspectiveFrame(
            timestamp_seconds=1.0,
            viewport=second_view,
            image_path=image,
            image_sha256="b" * 64,
        )
        detections = []
        for index, (frame, x_center) in enumerate(
            ((first_frame, 0.46), (second_frame, 0.54))
        ):
            detections.append(
                map_view_detection(
                    ViewDetection(
                        detection_id=f"view-detection-{index}",
                        timestamp_seconds=1.0,
                        viewport_id=frame.viewport.viewport_id,
                        bounding_box=NormalizedBoundingBox(
                            x_min=x_center - 0.03,
                            y_min=0.4,
                            x_max=x_center + 0.03,
                            y_max=0.7,
                        ),
                        kind=DetectionKind.PERSON,
                        label="person-candidate",
                        confidence=0.9,
                        detector_backend="test",
                        embedding=[1.0, 0.0],
                    ),
                    frame,
                )
            )
        self.assertGreater(detections[0].yaw, 170.0)
        self.assertLess(detections[1].yaw, -170.0)
        merged = deduplicate_spherical_detections(
            detections,
            angular_threshold_degrees=20.0,
        )
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].source_viewports, ["seam-left", "seam-right"])
        self.assertGreater(abs(merged[0].yaw), 175.0)

    def test_tracker_preserves_identity_through_seam_and_occlusion(self) -> None:
        detections = [
            spherical_detection("d0", 0.0, 170.0),
            spherical_detection("d1", 0.5, 179.0),
            spherical_detection("d2", 1.0, -175.0),
            spherical_detection("d4", 2.0, -165.0),
        ]
        tracks = GreedySphericalSubjectTracker(
            max_angular_distance_degrees=45.0,
            max_occlusion_seconds=1.5,
        ).track("asset-test", detections, [0.0, 0.5, 1.0, 1.5, 2.0])
        self.assertEqual(len(tracks), 1)
        track = tracks[0]
        self.assertEqual(len(track.seam_crossing_events), 1)
        self.assertEqual(len(track.occlusion_gaps), 1)
        self.assertEqual(track.occlusion_gaps[0].start_seconds, 1.5)
        self.assertEqual(track.occlusion_gaps[0].end_seconds, 2.0)
        self.assertEqual(track.samples[3].visibility.value, "occluded")
        self.assertEqual(track.samples[-1].visibility.value, "visible")

    def test_phase4_schemas_are_checked_in(self) -> None:
        schema_root = Path("docs/insta360/schemas")
        for name, model in PUBLIC_SCHEMA_MODELS.items():
            path = schema_root / f"{name}.schema.json"
            self.assertTrue(path.is_file(), name)
            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8")),
                model.model_json_schema(),
            )


class SphericalAssistedWorkflowTests(unittest.TestCase):
    def test_analysis_suggestion_preview_accept_and_reject_workflow(self) -> None:
        with tempfile.TemporaryDirectory(prefix="spherical-analysis-") as name:
            root = Path(name)
            cfg = test_settings(root)
            cfg.spherical.analysis_interval_seconds = 0.5
            cfg.spherical.analysis_max_frames = 4
            cfg.spherical.analysis_yaw_step_degrees = 90
            cfg.spherical.analysis_pitch_centers = [-45.0, 45.0]
            cfg.spherical.analysis_horizontal_fov = 120.0
            cfg.spherical.analysis_view_width = 128
            cfg.spherical.analysis_view_height = 128
            fixture = generate_fixture(
                cfg.project.media_dir,
                width=640,
                height=320,
                fps=10.0,
                duration_seconds=2.0,
                output_width=320,
                output_height=180,
            )
            harness = NodeHarness(cfg, "session-assisted")
            imported = harness.call(
                ImportSphericalMediaNode(cfg),
                inputs=[{"path": str(fixture.video)}],
                generate_proxy=True,
                proxy_width=320,
            )
            asset_id = imported["tool_excute_result"]["spherical_asset"]["asset_id"]

            analyzed = harness.call(
                AnalyzeSphericalAssetNode(cfg),
                asset_id=asset_id,
                interval_seconds=0.5,
                max_frames=4,
                yaw_step_degrees=90,
                pitch_centers=[-45.0, 45.0],
                horizontal_fov=120.0,
                view_width=128,
                view_height=128,
                minimum_detection_confidence=0.25,
                dedup_angular_threshold_degrees=12.0,
                max_track_angular_distance_degrees=60.0,
                max_occlusion_seconds=1.0,
            )
            self.assertFalse(analyzed["isError"], analyzed["summary"])
            analysis = analyzed["tool_excute_result"]["spherical_analysis"]
            summary = analyzed["tool_excute_result"]["analysis_summary"]
            self.assertEqual(summary["perspective_frame_count"], 32)
            self.assertEqual(summary["backend"]["name"], "lightweight-cpu")
            self.assertGreater(summary["subject_count"], 0)
            self.assertGreater(
                summary["confidence_summary"]["raw_view_detection_count"],
                summary["confidence_summary"]["deduplicated_detection_count"],
            )
            self.assertTrue(
                any(
                    len(
                        {
                            viewport
                            for sample in track["samples"]
                            if sample["visibility"] == "visible"
                            for viewport in sample["source_viewports"]
                        }
                    )
                    >= 2
                    for track in analysis["subject_tracks"]
                ),
                "expected a tracked subject to move through multiple viewports",
            )
            for path in analysis["review_artifacts"].values():
                if isinstance(path, str):
                    self.assertTrue(Path(path).is_file())

            analyzed_again = harness.call(
                AnalyzeSphericalAssetNode(cfg),
                save=False,
                asset_id=asset_id,
                interval_seconds=0.5,
                max_frames=4,
                yaw_step_degrees=90,
                pitch_centers=[-45.0, 45.0],
                horizontal_fov=120.0,
                view_width=128,
                view_height=128,
                minimum_detection_confidence=0.25,
                dedup_angular_threshold_degrees=12.0,
                max_track_angular_distance_degrees=60.0,
                max_occlusion_seconds=1.0,
            )
            self.assertTrue(
                analyzed_again["tool_excute_result"]["analysis_summary"]["cache_hit"]
            )

            listed = harness.call(ListDetectedSubjectsNode(cfg), save=False)
            self.assertGreater(listed["tool_excute_result"]["count"], 0)
            subject_id = max(
                analysis["subject_tracks"],
                key=lambda track: sum(
                    sample["visibility"] == "visible" for sample in track["samples"]
                ),
            )["subject_id"]
            inspected = harness.call(
                InspectSubjectTrackNode(cfg),
                save=False,
                subject_id=subject_id,
            )
            self.assertEqual(
                inspected["tool_excute_result"]["subject_track"]["subject_id"],
                subject_id,
            )

            suggested = harness.call(
                SuggestSubjectReframeNode(cfg),
                subject_id=subject_id,
                policy="subject_center",
                placement="center",
                output_width=320,
                output_height=180,
                horizontal_fov=None,
                minimum_fov=55.0,
                maximum_fov=110.0,
                avoid_nadir_below_pitch=-60.0,
                stitch_seam_yaws=[180.0],
                stitch_seam_zone_width=12.0,
            )
            self.assertFalse(suggested["isError"], suggested["summary"])
            suggestion = suggested["tool_excute_result"]["reframe_suggestion"]
            suggestion_id = suggestion["suggestion_id"]
            self.assertEqual(suggestion["status"], "proposed")
            self.assertEqual(suggestion["camera_track"]["created_by"], "assisted")
            repository = SphericalSessionRepository(harness.store)
            self.assertEqual(repository.list_track_records(), [])

            analysis_model = SphericalAnalysis.model_validate(analysis)
            asset_model = repository.get_asset(asset_id)
            policy_subject_ids = [
                track.subject_id for track in analysis_model.subject_tracks
            ]
            self.assertGreaterEqual(len(policy_subject_ids), 2)
            policy_cases = (
                (
                    SuggestionPolicy.RULE_OF_THIRDS,
                    [policy_subject_ids[0]],
                    {"placement": "right_third"},
                ),
                (SuggestionPolicy.LARGEST_PERSON, [], {}),
                (
                    SuggestionPolicy.PAN_SUBJECTS,
                    policy_subject_ids[:2],
                    {"start_seconds": 0.0, "end_seconds": 1.5},
                ),
                (
                    SuggestionPolicy.STATIC_DIRECTION,
                    [],
                    {"yaw": 45.0, "pitch": -80.0},
                ),
            )
            for index, (policy, policy_subjects, parameters) in enumerate(policy_cases):
                with self.subTest(policy=policy.value):
                    policy_suggestion = generate_reframe_suggestion(
                        analysis_model,
                        asset_model,
                        policy=policy,
                        destination_dir=root / f"policy-{index}",
                        subject_ids=policy_subjects,
                        output_width=320,
                        output_height=180,
                        parameters=parameters,
                    )
                    self.assertEqual(policy_suggestion.policy, policy)
                    self.assertEqual(
                        policy_suggestion.camera_track.created_by.value,
                        "assisted",
                    )
                    self.assertGreaterEqual(
                        min(
                            frame.pitch
                            for frame in policy_suggestion.camera_track.keyframes
                        ),
                        -60.0,
                    )

            previewed = harness.call(
                PreviewReframeSuggestionNode(cfg),
                suggestion_id=suggestion_id,
                max_dimension=320,
                sample_interval_seconds=0.5,
                use_cache=True,
            )
            self.assertFalse(previewed["isError"], previewed["summary"])
            self.assertTrue(
                Path(
                    previewed["tool_excute_result"]["suggestion_preview"]["output_path"]
                ).is_file()
            )
            self.assertEqual(repository.list_track_records(), [])

            accepted = harness.call(
                AcceptReframeSuggestionNode(cfg), suggestion_id=suggestion_id
            )
            self.assertFalse(accepted["isError"], accepted["summary"])
            active_tracks = repository.list_track_records()
            self.assertEqual(len(active_tracks), 1)
            self.assertEqual(active_tracks[0].track.created_by.value, "assisted")
            self.assertEqual(
                repository.get_suggestion(suggestion_id).suggestion.status,
                SuggestionStatus.ACCEPTED,
            )
            invalid_reject = harness.call(
                RejectReframeSuggestionNode(cfg),
                save=False,
                suggestion_id=suggestion_id,
                reason="too late",
            )
            self.assertTrue(invalid_reject["isError"])

            group = harness.call(
                SuggestGroupReframeNode(cfg),
                subject_ids=[],
                output_width=320,
                output_height=180,
                minimum_fov=70.0,
                maximum_fov=140.0,
                avoid_nadir_below_pitch=-60.0,
                stitch_seam_yaws=[180.0],
                stitch_seam_zone_width=12.0,
            )
            self.assertFalse(group["isError"], group["summary"])
            establishing = harness.call(
                SuggestEstablishingShotNode(cfg),
                output_width=320,
                output_height=180,
                horizontal_fov=120.0,
                avoid_nadir_below_pitch=-60.0,
                stitch_seam_yaws=[180.0],
                stitch_seam_zone_width=12.0,
            )
            establishing_id = establishing["tool_excute_result"]["reframe_suggestion"][
                "suggestion_id"
            ]
            rejected = harness.call(
                RejectReframeSuggestionNode(cfg),
                suggestion_id=establishing_id,
                reason="operator selected another proposal",
            )
            self.assertFalse(rejected["isError"], rejected["summary"])
            self.assertEqual(
                repository.get_suggestion(establishing_id).suggestion.status,
                SuggestionStatus.REJECTED,
            )
            self.assertEqual(len(repository.list_track_records()), 1)


if __name__ == "__main__":
    unittest.main()
