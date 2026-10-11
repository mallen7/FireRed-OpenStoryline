from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError

from open_storyline.nodes.core_nodes.plan_timeline import PlanTimelineNode
from open_storyline.nodes.spherical_nodes.assisted_reframing import (
    AnalyzeSphericalAssetNode,
)
from open_storyline.nodes.spherical_nodes.import_spherical_media import (
    ImportSphericalMediaNode,
)
from open_storyline.nodes.spherical_nodes.storyline_direction import (
    AcceptDirectorProposalNode,
    AnalyzeSpeakerCandidatesNode,
    BuildSphericalStoryTimelineNode,
    CompareDirectorProposalsNode,
    GenerateDirectorProposalsNode,
    ListDirectorProfilesNode,
    PreviewDirectorProposalNode,
    RejectDirectorProposalNode,
    RenderDirectorProposalNode,
)
from open_storyline.spherical.direction_schemas import (
    DirectorProposalStatus,
    DirectorScoreComponents,
    MotionConstraints,
    SpeakerAssessmentStatus,
    TimelineReferenceKind,
    TimelineSourceReference,
)
from open_storyline.spherical.fixtures import generate_fixture
from open_storyline.spherical.session import SphericalSessionRepository
from tests.test_spherical_nodes import NodeHarness, test_settings


class SphericalStorylineDirectionTests(unittest.TestCase):
    def test_timeline_source_reference_modes_are_explicit_and_strict(self) -> None:
        flat = TimelineSourceReference(
            kind=TimelineReferenceKind.FLAT_SOURCE,
            flat_path=Path("flat.mp4"),
        )
        spherical = TimelineSourceReference(
            kind=TimelineReferenceKind.SPHERICAL_APPROVED_TRACK,
            spherical_asset_id="asset-1",
            approved_camera_track_id="camera-1",
        )
        self.assertEqual(flat.kind, TimelineReferenceKind.FLAT_SOURCE)
        self.assertEqual(
            spherical.kind,
            TimelineReferenceKind.SPHERICAL_APPROVED_TRACK,
        )
        with self.assertRaises(ValidationError):
            TimelineSourceReference(
                kind=TimelineReferenceKind.FLAT_SOURCE,
                flat_path=Path("flat.mp4"),
                spherical_asset_id="asset-1",
            )

    def test_profiles_timeline_speakers_proposals_comparison_and_handoff(self) -> None:
        with tempfile.TemporaryDirectory(prefix="spherical-direction-") as name:
            root = Path(name)
            cfg = test_settings(root)
            cfg.spherical.analysis_interval_seconds = 0.25
            cfg.spherical.analysis_max_frames = 8
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
            harness = NodeHarness(cfg, "session-direction")
            profile_result = harness.call(
                ListDirectorProfilesNode(cfg),
                save=False,
                profile_id=None,
            )
            self.assertFalse(profile_result["isError"])
            self.assertEqual(profile_result["tool_excute_result"]["count"], 6)
            self.assertEqual(
                {
                    profile["profile_id"]
                    for profile in profile_result["tool_excute_result"]["editing_profiles"]
                },
                {
                    "round-table-podcast",
                    "family-travel-vlog",
                    "theme-park-vlog",
                    "walking-tour",
                    "restaurant-review",
                    "vehicle-conversation",
                },
            )

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
                interval_seconds=0.25,
                max_frames=8,
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
            tracks = analyzed["tool_excute_result"]["spherical_analysis"][
                "subject_tracks"
            ]
            self.assertGreaterEqual(len(tracks), 2)
            first_subject, second_subject = tracks[0]["subject_id"], tracks[1]["subject_id"]

            timeline_result = harness.call(
                BuildSphericalStoryTimelineNode(cfg),
                analysis_id=None,
                asr_infos=[
                    {
                        "clip_id": "clip_0001",
                        "asr_text": "hello world",
                        "asr_timestamps": [[0, 350], [400, 800]],
                        "asr_sentence_info": [
                            {"start": 0, "end": 900, "text": "hello world"}
                        ],
                        "source_ref": {
                            "start": 0,
                            "end": 1000,
                            "duration": 1000,
                        },
                    },
                    {
                        "clip_id": "clip_0002",
                        "asr_text": "your turn",
                        "asr_timestamps": [[0, 300], [350, 750]],
                        "asr_sentence_info": [
                            {"start": 0, "end": 900, "text": "your turn"}
                        ],
                        "source_ref": {
                            "start": 1000,
                            "end": 2000,
                            "duration": 1000,
                        },
                    },
                ],
                split_shots=[
                    {
                        "clip_id": "clip_0001",
                        "kind": "video",
                        "path": str(fixture.video),
                        "source_ref": {
                            "start": 0,
                            "end": 1000,
                            "duration": 1000,
                        },
                    },
                    {
                        "clip_id": "clip_0002",
                        "kind": "video",
                        "path": str(fixture.video),
                        "source_ref": {
                            "start": 1000,
                            "end": 2000,
                            "duration": 1000,
                        },
                    },
                ],
                clip_captions=[
                    {"clip_id": "clip_0001", "caption": "first person talks"},
                    {"clip_id": "clip_0002", "caption": "second person responds"},
                ],
                group_scripts=[
                    {
                        "group_id": "group_0001",
                        "raw_text": "A short conversation",
                        "subtitle_units": [],
                    }
                ],
                timeline_tracks={
                    "subtitles": [
                        {"text": "hello world", "timeline_window": {"start": 0, "end": 900}}
                    ],
                    "bgm": [],
                },
                mouth_activity=[
                    {
                        "subject_id": first_subject,
                        "start_seconds": 0.0,
                        "end_seconds": 0.9,
                        "confidence": 0.99,
                    },
                    {
                        "subject_id": second_subject,
                        "start_seconds": 1.0,
                        "end_seconds": 1.9,
                        "confidence": 0.99,
                    },
                ],
                audio_activity=[],
            )
            self.assertFalse(timeline_result["isError"], timeline_result["summary"])
            timeline = timeline_result["tool_excute_result"]["normalized_story_timeline"]
            self.assertEqual(len(timeline["transcript_turns"]), 2)
            self.assertEqual(len(timeline["words"]), 4)
            self.assertEqual(timeline["transcript_turns"][1]["start_seconds"], 1.0)
            self.assertEqual(timeline["source_summary"]["clip_understanding_items"], 2)
            self.assertEqual(timeline["source_summary"]["script_items"], 1)
            self.assertEqual(timeline["source_summary"]["timeline_plan_items"], 1)

            speaker_result = harness.call(
                AnalyzeSpeakerCandidatesNode(cfg),
                timeline_id=None,
                probable_threshold=0.5,
                ambiguity_margin=0.05,
                profile_id="round-table-podcast",
            )
            self.assertFalse(speaker_result["isError"], speaker_result["summary"])
            speaker_analysis = speaker_result["tool_excute_result"][
                "speaker_candidate_analysis"
            ]
            self.assertEqual(len(speaker_analysis["turns"]), 2)
            self.assertTrue(
                all(turn["candidates"] for turn in speaker_analysis["turns"])
            )
            self.assertTrue(
                any(
                    turn["status"] == SpeakerAssessmentStatus.PROBABLE.value
                    for turn in speaker_analysis["turns"]
                )
            )
            for turn in speaker_analysis["turns"]:
                if turn["status"] != SpeakerAssessmentStatus.PROBABLE.value:
                    self.assertIsNone(turn["selected_subject_id"])
                    self.assertTrue(turn["warnings"])

            conservative_speakers = harness.call(
                AnalyzeSpeakerCandidatesNode(cfg),
                save=False,
                timeline_id=None,
                probable_threshold=0.99,
                ambiguity_margin=0.2,
                profile_id="round-table-podcast",
            )["tool_excute_result"]["speaker_candidate_analysis"]
            self.assertTrue(
                all(
                    turn["status"] != SpeakerAssessmentStatus.PROBABLE.value
                    and turn["selected_subject_id"] is None
                    and turn["warnings"]
                    for turn in conservative_speakers["turns"]
                )
            )

            constraints = MotionConstraints(
                maximum_pan_speed_degrees_per_second=1.0,
                maximum_acceleration_degrees_per_second_squared=4.0,
                minimum_shot_duration_seconds=0.35,
                maximum_shot_duration_seconds=0.65,
                minimum_time_between_cuts_seconds=0.2,
                maximum_fov_change_degrees_per_second=5.0,
                subject_dead_zone_degrees=2.0,
                maximum_low_confidence_tracking_seconds=0.75,
            )
            proposals_result = harness.call(
                GenerateDirectorProposalsNode(cfg),
                timeline_id=None,
                speaker_analysis_id=None,
                profile_id="round-table-podcast",
                variants=[
                    "speaker-focused",
                    "cinematic",
                    "minimal-motion",
                    "high-energy",
                    "vertical-social",
                ],
                output_width=320,
                output_height=180,
                vertical_output_width=180,
                vertical_output_height=320,
                motion_constraints=constraints.model_dump(mode="json"),
            )
            self.assertFalse(proposals_result["isError"], proposals_result["summary"])
            proposals = proposals_result["tool_excute_result"]["director_proposals"]
            self.assertEqual(len(proposals), 5)
            self.assertEqual(len({proposal["proposal_id"] for proposal in proposals}), 5)
            score_names = set(DirectorScoreComponents.model_fields)
            for proposal in proposals:
                self.assertEqual(proposal["status"], DirectorProposalStatus.PROPOSED.value)
                self.assertTrue(proposal["motion_validation"]["constraints_satisfied"])
                self.assertLessEqual(
                    proposal["motion_validation"]["maximum_observed_pan_speed"],
                    constraints.maximum_pan_speed_degrees_per_second,
                )
                self.assertLessEqual(
                    proposal["motion_validation"]["maximum_observed_acceleration"],
                    constraints.maximum_acceleration_degrees_per_second_squared,
                )
                self.assertLessEqual(
                    proposal["motion_validation"]["maximum_observed_fov_change_rate"],
                    constraints.maximum_fov_change_degrees_per_second,
                )
                self.assertGreater(len(proposal["decisions"]), 1)
                self.assertTrue(
                    all(
                        set(decision["scores"]) == score_names
                        for decision in proposal["decisions"]
                    )
                )
                self.assertEqual(
                    proposal["camera_track"]["created_by"],
                    "director",
                )
                for artifact_path in proposal["review_artifacts"].values():
                    if isinstance(artifact_path, str):
                        self.assertTrue(Path(artifact_path).is_file())
            self.assertTrue(
                any(
                    proposal["motion_validation"]["forced_cut_count"] > 0
                    for proposal in proposals
                ),
                "an over-speed move should be converted to a cut",
            )
            self.assertEqual(
                proposals[1]["decisions"][0]["intent"],
                "establishing",
            )
            self.assertTrue(
                any(
                    decision["intent"] == "reaction"
                    for decision in proposals[3]["decisions"]
                )
            )
            self.assertEqual(
                (proposals[4]["camera_track"]["output_width"], proposals[4]["camera_track"]["output_height"]),
                (180, 320),
            )
            repository = SphericalSessionRepository(harness.store)
            self.assertEqual(repository.list_track_records(), [])

            proposal_ids = [proposal["proposal_id"] for proposal in proposals]
            compared = harness.call(
                CompareDirectorProposalsNode(cfg),
                save=False,
                proposal_ids=proposal_ids,
            )
            self.assertFalse(compared["isError"], compared["summary"])
            comparison = compared["tool_excute_result"]["director_comparison"]
            self.assertEqual(len(comparison["ranking"]), 5)
            self.assertIn(comparison["recommended_proposal_id"], proposal_ids)

            previewed = harness.call(
                PreviewDirectorProposalNode(cfg),
                proposal_id=proposal_ids[0],
                max_dimension=320,
                sample_interval_seconds=0.5,
                use_cache=True,
            )
            self.assertFalse(previewed["isError"], previewed["summary"])
            self.assertTrue(
                Path(
                    previewed["tool_excute_result"]["director_preview"]["output_path"]
                ).is_file()
            )
            self.assertEqual(repository.list_track_records(), [])

            accepted = harness.call(
                AcceptDirectorProposalNode(cfg),
                proposal_id=proposal_ids[0],
            )
            self.assertFalse(accepted["isError"], accepted["summary"])
            self.assertEqual(len(repository.list_track_records()), 1)
            self.assertEqual(
                repository.get_director_proposal(proposal_ids[0]).proposal.status,
                DirectorProposalStatus.ACCEPTED,
            )

            rejected = harness.call(
                RejectDirectorProposalNode(cfg),
                proposal_id=proposal_ids[1],
                reason="operator selected the speaker-focused proposal",
            )
            self.assertFalse(rejected["isError"], rejected["summary"])
            self.assertEqual(len(repository.list_track_records()), 1)
            self.assertEqual(
                repository.get_director_proposal(proposal_ids[1]).proposal.status,
                DirectorProposalStatus.REJECTED,
            )

            rendered = harness.call(
                RenderDirectorProposalNode(cfg),
                proposal_id=proposal_ids[0],
                output_filename="directed-handoff.mp4",
                sample_interval_seconds=0.5,
                use_cache=True,
            )
            self.assertFalse(rendered["isError"], rendered["summary"])
            handoff = rendered["tool_excute_result"]["director_timeline_handoff"]
            clip = rendered["tool_excute_result"]["ordinary_clip"]
            media = rendered["tool_excute_result"]["ordinary_media"]
            self.assertEqual(
                handoff["source_reference"]["kind"],
                "generated_reframe_artifact",
            )
            self.assertTrue(Path(clip["path"]).is_file())
            self.assertEqual(clip["kind"], "video")
            self.assertEqual(clip["source_ref"]["media_id"], media["media_id"])
            self.assertEqual(media["media_type"], "video")

            planned = harness.call(
                PlanTimelineNode(cfg),
                save=False,
                load_media={"media": [media]},
                split_shots={"clips": [clip]},
                group_clips={
                    "groups": [
                        {
                            "group_id": "group_directed",
                            "summary": "accepted spherical reframe",
                            "clip_ids": [clip["clip_id"]],
                        }
                    ]
                },
                generate_script={"group_scripts": []},
                tts={"voiceover": []},
                music_rec={"bgm": []},
                use_beats=False,
                is_speech_rough_cut=False,
                is_ai_transition=False,
                image_duration_ms=3000,
            )
            self.assertFalse(planned["isError"], planned["summary"])
            self.assertEqual(
                planned["tool_excute_result"]["tracks"]["video"][0]["clip_id"],
                clip["clip_id"],
            )

            invalid_render = harness.call(
                RenderDirectorProposalNode(cfg),
                save=False,
                proposal_id=proposal_ids[2],
                output_filename=None,
                sample_interval_seconds=0.5,
                use_cache=True,
            )
            self.assertTrue(invalid_render["isError"])


if __name__ == "__main__":
    unittest.main()
