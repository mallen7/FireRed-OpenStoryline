from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from typing import Any

from open_storyline.config import Settings, load_settings
from open_storyline.nodes.core_nodes.load_media import LoadMediaNode
from open_storyline.nodes.node_state import NodeState
from open_storyline.nodes.node_summary import NodeSummary
from open_storyline.nodes.spherical_nodes.camera_tracks import (
    CreateVirtualCameraTrackNode,
    DeleteVirtualCameraTrackNode,
    ListVirtualCameraTracksNode,
    PreviewVirtualCameraTrackNode,
    RenderReframedClipNode,
    UpdateVirtualCameraTrackNode,
    ValidateVirtualCameraTrackNode,
)
from open_storyline.nodes.spherical_nodes.import_spherical_media import (
    ImportSphericalMediaNode,
)
from open_storyline.nodes.spherical_nodes.inspect_spherical_media import (
    InspectSphericalMediaNode,
)
from open_storyline.spherical.fixtures import generate_fixture
from open_storyline.spherical.errors import SphericalValidationError
from open_storyline.spherical.session import SphericalSessionRepository
from open_storyline.storage.agent_memory import ArtifactStore


def test_settings(root: Path, *, enabled: bool = True) -> Settings:
    cfg = load_settings("config.toml").model_copy(deep=True)
    cfg.developer.developer_mode = False
    cfg.project.media_dir = root / "media"
    cfg.project.outputs_dir = root / "artifacts"
    cfg.local_mcp_server.server_cache_dir = str(root / "server-cache")
    cfg.spherical.enabled = enabled
    cfg.spherical.workspace_dir = root / "workspace"
    cfg.spherical.cache_dir = root / "spherical-cache"
    cfg.spherical.proxy_width = 320
    cfg.spherical.contact_sheet_thumbnail_width = 160
    cfg.spherical.preview_max_dimension = 320
    cfg.spherical.renderer_sample_interval_seconds = 0.5
    cfg.project.media_dir.mkdir(parents=True, exist_ok=True)
    cfg.project.outputs_dir.mkdir(parents=True, exist_ok=True)
    return cfg


class NodeHarness:
    def __init__(self, cfg: Settings, session_id: str) -> None:
        self.cfg = cfg
        self.session_id = session_id
        self.store = ArtifactStore(cfg.project.outputs_dir, session_id)
        self.counter = 0

    def call(self, node: Any, *, save: bool = True, **params: Any) -> dict[str, Any]:
        self.counter += 1
        state = NodeState(
            session_id=self.session_id,
            artifact_id=f"{node.meta.node_id}-{self.counter:03d}",
            lang="en",
            node_summary=NodeSummary(auto_console=False),
            llm=None,  # type: ignore[arg-type]
            mcp_ctx=None,  # type: ignore[arg-type]
        )
        result = asyncio.run(node(state, **params))
        if save and not result["isError"]:
            self.store.save_result(
                self.session_id,
                node.meta.node_id,
                result,
            )
        return result


class SphericalNodeWorkflowTests(unittest.TestCase):
    def test_repository_rejects_noncontiguous_delete_event(self) -> None:
        with tempfile.TemporaryDirectory(prefix="spherical-events-") as temporary_name:
            store = ArtifactStore(Path(temporary_name), "session-events")
            track = {
                "schema_version": "1.0",
                "track_id": "camera-integrity",
                "asset_id": "asset-integrity",
                "name": "integrity",
                "output_width": 320,
                "output_height": 180,
                "source_start_seconds": 0.0,
                "source_end_seconds": 1.0,
                "keyframes": [{"time_seconds": 0.0, "yaw": 0.0}],
                "created_by": "manual",
                "generation_metadata": {},
            }
            for artifact_id, node_id, event in (
                (
                    "create-001",
                    "create_virtual_camera_track",
                    {
                        "schema_version": "1.0",
                        "action": "create",
                        "track_id": "camera-integrity",
                        "asset_id": "asset-integrity",
                        "revision": 1,
                        "track": track,
                    },
                ),
                (
                    "delete-099",
                    "delete_virtual_camera_track",
                    {
                        "schema_version": "1.0",
                        "action": "delete",
                        "track_id": "camera-integrity",
                        "asset_id": "asset-integrity",
                        "revision": 99,
                    },
                ),
            ):
                store.save_result(
                    "session-events",
                    node_id,
                    {
                        "artifact_id": artifact_id,
                        "summary": "test event",
                        "tool_excute_result": {"camera_track_event": event},
                    },
                )

            with self.assertRaisesRegex(
                SphericalValidationError,
                "revisions are not contiguous",
            ):
                SphericalSessionRepository(store).list_track_records(
                    include_deleted=True
                )

    def test_complete_session_workflow_and_normal_media_handoff(self) -> None:
        with tempfile.TemporaryDirectory(prefix="spherical-nodes-") as temporary_name:
            root = Path(temporary_name)
            cfg = test_settings(root)
            fixture = generate_fixture(
                cfg.project.media_dir,
                width=640,
                height=320,
                fps=10.0,
                duration_seconds=2.0,
                output_width=320,
                output_height=180,
            )
            harness = NodeHarness(cfg, "session-a")

            imported = harness.call(
                ImportSphericalMediaNode(cfg),
                inputs=[{"path": str(fixture.video)}],
                generate_proxy=True,
                proxy_width=320,
            )
            self.assertFalse(imported["isError"])
            asset = imported["tool_excute_result"]["spherical_asset"]
            asset_id = asset["asset_id"]
            self.assertEqual(asset["width"], 640)
            self.assertEqual(asset["height"], 320)
            self.assertEqual(asset["metadata"]["streams"]["video"]["codec"], "h264")
            self.assertEqual(len(asset["metadata"]["streams"]["audio"]), 1)
            self.assertTrue(Path(asset["proxy_path"]).is_file())

            inspected = harness.call(
                InspectSphericalMediaNode(cfg),
                asset_id=asset_id,
                sample_count=4,
                thumbnail_width=160,
                include_perspective=True,
            )
            self.assertFalse(inspected["isError"])
            inspection = inspected["tool_excute_result"]["spherical_inspection"]
            self.assertEqual(inspection["sample_timestamps"], [0.0, 0.5, 1.0, 1.5])
            self.assertEqual(
                [view["name"] for view in inspection["perspective_views"]],
                ["front", "right", "back", "left", "up", "down"],
            )
            self.assertTrue(Path(inspection["equirectangular_contact_sheet_path"]).is_file())
            self.assertTrue(Path(inspection["perspective_contact_sheet_path"]).is_file())

            inspected_again = harness.call(
                InspectSphericalMediaNode(cfg),
                save=False,
                asset_id=asset_id,
                sample_count=4,
                thumbnail_width=160,
                include_perspective=True,
            )
            self.assertTrue(
                inspected_again["tool_excute_result"]["spherical_inspection"][
                    "inspection_cache_hit"
                ]
            )

            created = harness.call(
                CreateVirtualCameraTrackNode(cfg),
                asset_id=asset_id,
                name="front-to-right",
                output_width=320,
                output_height=180,
                source_start_seconds=0.0,
                source_end_seconds=2.0,
                keyframes=[
                    {"time_seconds": 0.0, "yaw": 0.0, "horizontal_fov": 100.0},
                    {"time_seconds": 2.0, "yaw": 90.0, "horizontal_fov": 80.0},
                ],
            )
            self.assertFalse(created["isError"])
            track_id = created["tool_excute_result"]["camera_track"]["track"][
                "track_id"
            ]

            validated = harness.call(
                ValidateVirtualCameraTrackNode(cfg),
                save=False,
                track_id=track_id,
            )
            self.assertTrue(validated["tool_excute_result"]["validation"]["valid"])

            previewed = harness.call(
                PreviewVirtualCameraTrackNode(cfg),
                track_id=track_id,
                max_dimension=320,
                sample_interval_seconds=0.5,
                use_cache=True,
            )
            preview = previewed["tool_excute_result"]["preview_render"]
            self.assertEqual((preview["output_width"], preview["output_height"]), (320, 180))
            self.assertTrue(preview["audio_preserved"])
            self.assertNotIn("exact_commands", preview)

            updated = harness.call(
                UpdateVirtualCameraTrackNode(cfg),
                track_id=track_id,
                name="shortest-seam-pan",
                keyframes=[
                    {"time_seconds": 0.0, "yaw": 170.0},
                    {"time_seconds": 2.0, "yaw": -170.0},
                ],
            )
            self.assertEqual(
                updated["tool_excute_result"]["camera_track"]["revision"],
                2,
            )

            listed = harness.call(ListVirtualCameraTracksNode(cfg), save=False)
            self.assertEqual(listed["tool_excute_result"]["count"], 1)
            self.assertEqual(
                listed["tool_excute_result"]["camera_tracks"][0]["revision"],
                2,
            )

            rendered = harness.call(
                RenderReframedClipNode(cfg),
                track_id=track_id,
                output_filename="vertical-ready-flat.mp4",
                sample_interval_seconds=0.5,
                use_cache=True,
            )
            render = rendered["tool_excute_result"]["reframed_clip"]
            render_path = Path(render["output_path"])
            self.assertTrue(render_path.is_file())
            self.assertEqual(render["track_revision"], 2)
            self.assertTrue(render["audio_preserved"])
            self.assertNotIn("exact_commands", render)

            loaded = harness.call(
                LoadMediaNode(cfg),
                save=False,
                inputs=[
                    {
                        "path": str(render_path),
                        "orig_path": str(render_path),
                        "orig_md5": None,
                    }
                ],
            )
            normal_media = loaded["tool_excute_result"]["media"]
            self.assertEqual(len(normal_media), 1)
            self.assertEqual(normal_media[0]["media_type"], "video")
            self.assertEqual(Path(normal_media[0]["path"]), render_path)

            deleted = harness.call(
                DeleteVirtualCameraTrackNode(cfg),
                track_id=track_id,
            )
            self.assertTrue(
                deleted["tool_excute_result"]["deleted_track"]["deleted"]
            )
            listed_after_delete = harness.call(
                ListVirtualCameraTracksNode(cfg),
                save=False,
            )
            self.assertEqual(listed_after_delete["tool_excute_result"]["count"], 0)

            other_store = ArtifactStore(cfg.project.outputs_dir, "session-b")
            other_repository = SphericalSessionRepository(other_store)
            self.assertEqual(other_repository.list_assets(), [])
            self.assertEqual(other_repository.list_track_records(), [])

    def test_raw_insv_is_clear_capability_error_and_normal_loader_skips_it(self) -> None:
        with tempfile.TemporaryDirectory(prefix="spherical-raw-") as temporary_name:
            root = Path(temporary_name)
            cfg = test_settings(root)
            raw_path = cfg.project.media_dir / "camera.insv"
            raw_path.write_bytes(b"synthetic raw placeholder")
            harness = NodeHarness(cfg, "session-raw")

            imported = harness.call(
                ImportSphericalMediaNode(cfg),
                save=False,
                inputs=[{"path": str(raw_path)}],
            )
            self.assertTrue(imported["isError"])
            self.assertIn("Insta360 Studio", str(imported["summary"]))

            loaded = harness.call(
                LoadMediaNode(cfg),
                save=False,
                inputs=[
                    {
                        "path": str(raw_path),
                        "orig_path": str(raw_path),
                        "orig_md5": None,
                    }
                ],
            )
            self.assertFalse(loaded["isError"])
            self.assertEqual(loaded["tool_excute_result"]["media"], [])

    def test_disabled_feature_and_source_root_escape_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="spherical-disabled-") as temporary_name:
            root = Path(temporary_name)
            enabled_cfg = test_settings(root, enabled=True)
            disabled_cfg = test_settings(root, enabled=False)
            outside = root / "outside.mp4"
            outside.write_bytes(b"not media")

            disabled = NodeHarness(disabled_cfg, "session-disabled").call(
                ImportSphericalMediaNode(disabled_cfg),
                save=False,
                inputs=[{"path": str(outside)}],
            )
            self.assertTrue(disabled["isError"])
            self.assertIn("spherical.enabled=true", str(disabled["summary"]))

            escaped = NodeHarness(enabled_cfg, "session-escape").call(
                ImportSphericalMediaNode(enabled_cfg),
                save=False,
                inputs=[{"path": str(outside)}],
            )
            self.assertTrue(escaped["isError"])
            self.assertIn("outside the allowed", str(escaped["summary"]))


if __name__ == "__main__":
    unittest.main()
