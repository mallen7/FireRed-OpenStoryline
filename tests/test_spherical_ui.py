from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient
from pydantic import ValidationError

import agent_fastapi
from agent_fastapi import SessionStore, _is_under_dir, app
from open_storyline.nodes.core_nodes.load_media import LoadMediaNode
from open_storyline.nodes.spherical_nodes.import_spherical_media import (
    ImportSphericalMediaNode,
)
from open_storyline.spherical.fixtures import generate_fixture
from open_storyline.spherical.session import SphericalSessionRepository
from open_storyline.spherical.ui_schemas import (
    SPHERICAL_UI_SCHEMA_MODELS,
    SphericalUiPreviewRequest,
    SphericalUiTrackCreateRequest,
    export_spherical_ui_schemas,
)
from open_storyline.storage.agent_memory import ArtifactStore
from tests.test_spherical_nodes import NodeHarness, test_settings


def manual_track(asset_id: str, *, track_id: str = "ui-manual") -> dict:
    return {
        "schema_version": "1.0",
        "track_id": track_id,
        "asset_id": asset_id,
        "name": "UI manual camera",
        "output_width": 320,
        "output_height": 180,
        "source_start_seconds": 0.0,
        "source_end_seconds": 2.0,
        "keyframes": [
            {
                "schema_version": "1.0",
                "time_seconds": 0.0,
                "yaw": 170.0,
                "pitch": 0.0,
                "roll": 0.0,
                "horizontal_fov": 95.0,
                "easing": "smoothstep",
                "subject_id": None,
                "confidence": None,
            },
            {
                "schema_version": "1.0",
                "time_seconds": 2.0,
                "yaw": -170.0,
                "pitch": 5.0,
                "roll": 0.0,
                "horizontal_fov": 80.0,
                "easing": "ease_in_out",
                "subject_id": None,
                "confidence": 0.8,
            },
        ],
        "created_by": "manual",
        "generation_metadata": {"origin": "test-ui"},
    }


class SphericalUiContractTests(unittest.TestCase):
    def test_ui_contracts_are_strict_versioned_and_exportable(self) -> None:
        valid = manual_track("asset-contract")
        parsed = SphericalUiTrackCreateRequest.model_validate(
            {"schema_version": "1.0", "track": valid}
        )
        self.assertEqual(parsed.track.created_by.value, "manual")
        with self.assertRaises(ValidationError):
            SphericalUiTrackCreateRequest.model_validate(
                {
                    "schema_version": "1.0",
                    "track": {**valid, "created_by": "assisted"},
                }
            )
        with self.assertRaises(ValidationError):
            SphericalUiPreviewRequest.model_validate(
                {"schema_version": "1.0", "max_dimension": 641}
            )
        with tempfile.TemporaryDirectory(prefix="spherical-ui-schemas-") as name:
            written = export_spherical_ui_schemas(name)
            self.assertEqual(len(written), len(SPHERICAL_UI_SCHEMA_MODELS))
            for path in written:
                payload = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(payload["additionalProperties"], False)
                checked_in = Path("docs/insta360/schemas") / path.name
                self.assertEqual(
                    payload,
                    json.loads(checked_in.read_text(encoding="utf-8")),
                    f"checked-in UI schema is stale: {checked_in}",
                )

    def test_preview_path_guard_resolves_symlink_escape(self) -> None:
        with tempfile.TemporaryDirectory(prefix="spherical-ui-path-") as name:
            root = Path(name)
            allowed = root / "allowed"
            allowed.mkdir()
            outside = root / "outside.mp4"
            outside.write_bytes(b"private")
            link = allowed / "looks-safe.mp4"
            try:
                os.symlink(outside, link)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"symlinks unavailable: {exc}")
            self.assertFalse(_is_under_dir(str(link), str(allowed)))
            self.assertTrue(_is_under_dir(str(allowed / "normal.mp4"), str(allowed)))


class SphericalUiApiWorkflowTests(unittest.TestCase):
    def test_browser_api_uses_mcp_events_and_hands_final_render_to_normal_media(self) -> None:
        with tempfile.TemporaryDirectory(prefix="spherical-ui-api-") as name:
            root = Path(name)
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
            with TestClient(app) as client:
                app.state.cfg = cfg
                app.state.sessions = SessionStore(cfg)
                created_session = client.post("/api/sessions")
                self.assertEqual(created_session.status_code, 200)
                session_id = created_session.json()["session_id"]

                imported = NodeHarness(cfg, session_id).call(
                    ImportSphericalMediaNode(cfg),
                    inputs=[{"path": str(fixture.video)}],
                    generate_proxy=True,
                    proxy_width=320,
                )
                self.assertFalse(imported["isError"])
                asset_id = imported["tool_excute_result"]["spherical_asset"]["asset_id"]

                state_response = client.get(f"/api/sessions/{session_id}/spherical")
                self.assertEqual(state_response.status_code, 200, state_response.text)
                state = state_response.json()
                self.assertEqual(state["assets"][0]["badge"], "360° spherical")
                self.assertEqual(state["assets"][0]["stitch_status"]["status"], "studio_export")
                self.assertIn("preview_url", state["assets"][0])
                outside = root / "not-an-approved-preview.mp4"
                outside.write_bytes(b"must not be served")
                rejected = client.get(
                    f"/api/sessions/{session_id}/preview",
                    params={"path": str(outside)},
                )
                self.assertEqual(rejected.status_code, 403)

                track = manual_track(asset_id)
                create_response = client.post(
                    f"/api/sessions/{session_id}/spherical/tracks",
                    json={"schema_version": "1.0", "track": track},
                )
                self.assertEqual(create_response.status_code, 200, create_response.text)
                self.assertEqual(create_response.json()["revision"], 1)

                repository = SphericalSessionRepository(
                    ArtifactStore(cfg.project.outputs_dir, session_id)
                )
                stored = repository.get_track(track["track_id"])
                self.assertEqual(stored.track.keyframes[0].yaw, 170.0)
                self.assertEqual(stored.revision, 1)

                updated_track = {**track, "name": "UI and MCP shared track"}
                update_response = client.put(
                    f"/api/sessions/{session_id}/spherical/tracks/{track['track_id']}",
                    json={
                        "schema_version": "1.0",
                        "expected_revision": 1,
                        "track": updated_track,
                    },
                )
                self.assertEqual(update_response.status_code, 200, update_response.text)
                self.assertEqual(update_response.json()["revision"], 2)
                stale = client.put(
                    f"/api/sessions/{session_id}/spherical/tracks/{track['track_id']}",
                    json={
                        "schema_version": "1.0",
                        "expected_revision": 1,
                        "track": updated_track,
                    },
                )
                self.assertEqual(stale.status_code, 422)

                preview = client.post(
                    f"/api/sessions/{session_id}/spherical/tracks/{track['track_id']}/preview",
                    json={
                        "schema_version": "1.0",
                        "center_seconds": 1.0,
                        "duration_seconds": 1.0,
                        "max_dimension": 320,
                        "sample_interval_seconds": 0.5,
                        "use_cache": True,
                    },
                )
                self.assertEqual(preview.status_code, 200, preview.text)
                self.assertEqual((preview.json()["output_width"], preview.json()["output_height"]), (320, 180))
                self.assertEqual(client.get(preview.json()["preview_url"]).status_code, 200)
                cached_preview = client.post(
                    f"/api/sessions/{session_id}/spherical/tracks/{track['track_id']}/preview",
                    json={
                        "schema_version": "1.0",
                        "center_seconds": 1.0,
                        "duration_seconds": 1.0,
                        "max_dimension": 320,
                        "sample_interval_seconds": 0.5,
                        "use_cache": True,
                    },
                )
                self.assertEqual(cached_preview.status_code, 200, cached_preview.text)
                self.assertTrue(cached_preview.json()["cache_hit"])

                final = client.post(
                    f"/api/sessions/{session_id}/spherical/tracks/{track['track_id']}/render",
                    json={
                        "schema_version": "1.0",
                        "output_filename": "ui-final.mp4",
                        "sample_interval_seconds": 0.5,
                        "use_cache": True,
                    },
                )
                self.assertEqual(final.status_code, 200, final.text)
                payload = final.json()
                self.assertTrue(Path(payload["output_path"]).is_file())
                self.assertEqual(payload["ordinary_media"]["kind"], "video")
                media_response = client.get(payload["ordinary_media"]["file_url"])
                self.assertEqual(media_response.status_code, 200)
                self.assertGreater(len(media_response.content), 1000)

                normal = NodeHarness(cfg, session_id).call(
                    LoadMediaNode(cfg),
                    save=False,
                    inputs=[
                        {
                            "path": payload["output_path"],
                            "orig_path": payload["output_path"],
                            "orig_md5": None,
                        }
                    ],
                )
                self.assertFalse(normal["isError"])
                self.assertEqual(normal["tool_excute_result"]["media"][0]["media_type"], "video")

                vertical_track = {
                    **updated_track,
                    "output_width": 180,
                    "output_height": 320,
                }
                vertical_update = client.put(
                    f"/api/sessions/{session_id}/spherical/tracks/{track['track_id']}",
                    json={
                        "schema_version": "1.0",
                        "expected_revision": 2,
                        "track": vertical_track,
                    },
                )
                self.assertEqual(vertical_update.status_code, 200, vertical_update.text)
                vertical = client.post(
                    f"/api/sessions/{session_id}/spherical/tracks/{track['track_id']}/render",
                    json={
                        "schema_version": "1.0",
                        "output_filename": "ui-vertical.mp4",
                        "sample_interval_seconds": 0.5,
                        "use_cache": True,
                    },
                )
                self.assertEqual(vertical.status_code, 200, vertical.text)
                self.assertEqual(
                    (vertical.json()["output_width"], vertical.json()["output_height"]),
                    (180, 320),
                )

                schema_response = client.get("/api/meta/spherical-ui-schemas")
                self.assertEqual(schema_response.status_code, 200)
                self.assertEqual(set(schema_response.json()), set(SPHERICAL_UI_SCHEMA_MODELS))


if __name__ == "__main__":
    unittest.main()
