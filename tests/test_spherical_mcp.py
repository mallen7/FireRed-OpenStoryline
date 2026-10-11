from __future__ import annotations

import asyncio
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from open_storyline.config import load_settings
from open_storyline.mcp.register_tools import create_tool_wrapper
from open_storyline.mcp.server import create_server
from open_storyline.nodes.spherical_nodes.import_spherical_media import (
    ImportSphericalMediaNode,
)
from open_storyline.spherical.fixtures import generate_fixture
from open_storyline.spherical.session import SphericalSessionRepository
from open_storyline.storage.session_manager import SessionLifecycleManager


SPHERICAL_TOOLS = {
    "import_spherical_media",
    "inspect_spherical_media",
    "create_virtual_camera_track",
    "update_virtual_camera_track",
    "validate_virtual_camera_track",
    "preview_virtual_camera_track",
    "render_reframed_clip",
    "list_virtual_camera_tracks",
    "delete_virtual_camera_track",
    "analyze_spherical_asset",
    "list_detected_subjects",
    "inspect_subject_track",
    "suggest_subject_reframe",
    "suggest_group_reframe",
    "suggest_establishing_shot",
    "preview_reframe_suggestion",
    "accept_reframe_suggestion",
    "reject_reframe_suggestion",
    "list_director_profiles",
    "build_spherical_story_timeline",
    "analyze_speaker_candidates",
    "generate_director_proposals",
    "compare_director_proposals",
    "preview_director_proposal",
    "accept_director_proposal",
    "reject_director_proposal",
    "render_director_proposal",
}


class FakeRequest:
    def __init__(self, session_id: str, arguments: dict) -> None:
        self.headers = {"X-Storyline-Session-Id": session_id}
        self._arguments = arguments

    async def json(self) -> dict:
        return {"params": {"arguments": self._arguments}}


class SphericalMCPRegistrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        asset_loading_initializers = (
            "open_storyline.nodes.core_nodes.split_shots.SplitShotsNode.__init__",
            "open_storyline.nodes.core_nodes.script_template_rec.ScriptTemplateRecomendation.__init__",
            "open_storyline.nodes.core_nodes.select_bgm.SelectBGMNode.__init__",
            "open_storyline.nodes.core_nodes.recommend_effects.RecommendTextNode.__init__",
            "open_storyline.nodes.core_nodes.render_video.RenderVideoNode.__init__",
        )
        with ExitStack() as stack:
            for initializer in asset_loading_initializers:
                stack.enter_context(patch(initializer, return_value=None))
            disabled_cfg = load_settings("config.toml")
            cls.disabled_server = create_server(disabled_cfg)
            enabled_cfg = disabled_cfg.model_copy(deep=True)
            enabled_cfg.spherical.enabled = True
            cls.enabled_server = create_server(enabled_cfg)

    def test_disabled_default_preserves_existing_tool_surface(self) -> None:
        disabled_names = set(self.disabled_server._tool_manager._tools)
        self.assertTrue(SPHERICAL_TOOLS.isdisjoint(disabled_names))
        self.assertIn("load_media", disabled_names)
        self.assertIn("render_video", disabled_names)
        self.assertIn("read_node_history", disabled_names)

    def test_enabled_config_registers_all_spherical_tools(self) -> None:
        enabled_names = set(self.enabled_server._tool_manager._tools)
        disabled_names = set(self.disabled_server._tool_manager._tools)
        self.assertTrue(SPHERICAL_TOOLS.issubset(enabled_names))
        self.assertEqual(enabled_names - SPHERICAL_TOOLS, disabled_names)

    def test_direct_mcp_import_generates_and_persists_artifact_id(self) -> None:
        with tempfile.TemporaryDirectory(prefix="spherical-direct-mcp-") as name:
            root = Path(name)
            cfg = load_settings("config.toml").model_copy(deep=True)
            cfg.developer.developer_mode = False
            cfg.spherical.enabled = True
            cfg.project.media_dir = root / "media"
            cfg.project.outputs_dir = root / "artifacts"
            cfg.local_mcp_server.server_cache_dir = str(root / "server-cache")
            cfg.spherical.workspace_dir = root / "workspace"
            cfg.spherical.cache_dir = root / "spherical-cache"
            cfg.project.media_dir.mkdir(parents=True)
            fixture = generate_fixture(
                cfg.project.media_dir,
                width=640,
                height=320,
                fps=10.0,
                duration_seconds=1.0,
                output_width=320,
                output_height=180,
            )
            session_id = "direct-mcp-session"
            arguments = {
                "mode": "auto",
                "inputs": [{"path": str(fixture.video)}],
                "generate_proxy": False,
                "proxy_width": None,
            }
            request = FakeRequest(session_id, arguments)
            manager = SessionLifecycleManager(
                artifacts_root=cfg.project.outputs_dir,
                cache_root=cfg.local_mcp_server.server_cache_dir,
            )
            context = SimpleNamespace(
                request_context=SimpleNamespace(
                    request=request,
                    lifespan_context=manager,
                )
            )
            wrapper, _ = create_tool_wrapper(
                ImportSphericalMediaNode(cfg),
                ImportSphericalMediaNode.input_schema,
            )

            result = asyncio.run(wrapper(context, **arguments))

            self.assertFalse(result["isError"])
            self.assertTrue(result["artifact_id"].startswith("import_spherical_media_"))
            repository = SphericalSessionRepository(
                manager.get_artifact_store(session_id)
            )
            self.assertEqual(len(repository.list_assets()), 1)

    def test_mcp_json_schemas_expose_typed_camera_contracts(self) -> None:
        manager = self.enabled_server._tool_manager
        import_schema = manager.get_tool("import_spherical_media").parameters
        create_schema = manager.get_tool("create_virtual_camera_track").parameters
        render_schema = manager.get_tool("render_reframed_clip").parameters
        analyze_schema = manager.get_tool("analyze_spherical_asset").parameters
        suggest_schema = manager.get_tool("suggest_subject_reframe").parameters
        accept_schema = manager.get_tool("accept_reframe_suggestion").parameters
        timeline_schema = manager.get_tool(
            "build_spherical_story_timeline"
        ).parameters
        director_schema = manager.get_tool("generate_director_proposals").parameters
        handoff_schema = manager.get_tool("render_director_proposal").parameters

        self.assertIn("inputs", import_schema["properties"])
        self.assertIn("keyframes", create_schema["properties"])
        self.assertIn("output_width", create_schema["properties"])
        self.assertIn("track_id", render_schema["properties"])
        self.assertIn("yaw_step_degrees", analyze_schema["properties"])
        self.assertIn("subject_id", suggest_schema["properties"])
        self.assertIn("suggestion_id", accept_schema["properties"])
        self.assertIn("asr_infos", timeline_schema["properties"])
        self.assertIn("variants", director_schema["properties"])
        self.assertIn("motion_constraints", director_schema["properties"])
        self.assertIn("proposal_id", handoff_schema["properties"])
        self.assertIn("keyframes", create_schema.get("required", []))

    def test_workflow_skill_contains_required_boundaries_and_tools(self) -> None:
        skill_path = Path(
            ".storyline/skills/insta360_editing_workflow_skill/SKILL.md"
        )
        content = skill_path.read_text(encoding="utf-8")
        self.assertIn("Raw `.insv`", content)
        self.assertIn("Stitched approximately 2:1 MP4", content)
        self.assertIn("Reframed ordinary MP4", content)
        for tool_name in SPHERICAL_TOOLS:
            self.assertIn(tool_name, content)
        self.assertIn("`load_media`", content)


if __name__ == "__main__":
    unittest.main()
