"""Application service shared by the spherical web UI and its HTTP routes."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from open_storyline.config import Settings
from open_storyline.nodes.spherical_nodes.schemas import safe_output_filename
from open_storyline.spherical.artifact_events import (
    camera_track_create_event,
    compact_manifest,
    compact_proposal,
    compact_suggestion,
    compact_track,
    preview_dimensions,
    proposal_event,
    suggestion_event,
    track_event,
)
from open_storyline.spherical.analysis_schemas import SuggestionStatus
from open_storyline.spherical.direction_schemas import DirectorProposalStatus
from open_storyline.spherical.errors import (
    SphericalDisabledError,
    SphericalValidationError,
)
from open_storyline.spherical.ffmpeg import SphericalRenderer, probe_media
from open_storyline.spherical.geometry import sample_camera_track
from open_storyline.spherical.manifests import sha256_json
from open_storyline.spherical.schemas import ReframeRenderRequest, VirtualCameraTrack
from open_storyline.spherical.session import SphericalSessionRepository
from open_storyline.spherical.ui_schemas import (
    SphericalUiMutationResponse,
    SphericalUiPreviewRequest,
    SphericalUiRenderRequest,
    SphericalUiRenderResponse,
    SphericalUiState,
    SphericalUiTrackCreateRequest,
    SphericalUiTrackUpdateRequest,
)
from open_storyline.spherical.worker_client import create_stitch_worker_client
from open_storyline.storage.agent_memory import ArtifactStore


PreviewUrlBuilder = Callable[[Path], str]
_RENDER_NODE_IDS = frozenset(
    {
        "preview_virtual_camera_track",
        "render_reframed_clip",
        "preview_reframe_suggestion",
        "preview_director_proposal",
        "render_director_proposal",
    }
)


class SphericalUiService:
    """Persist UI actions as the same validated events consumed by MCP nodes."""

    def __init__(self, cfg: Settings, session_id: str) -> None:
        self.cfg = cfg
        self.session_id = session_id
        self.store = ArtifactStore(cfg.project.outputs_dir, session_id=session_id)
        self.repository = SphericalSessionRepository(self.store)

    def _require_enabled(self) -> None:
        if not self.cfg.spherical.enabled:
            raise SphericalDisabledError(
                "Spherical editing is disabled. Set spherical.enabled=true and restart."
            )

    def _artifact_id(self, node_id: str) -> str:
        return self.store.generate_artifact_id(node_id)

    def _save(self, node_id: str, artifact_id: str, payload: dict[str, Any], summary: str) -> None:
        self.store.save_result(
            self.session_id,
            node_id,
            {
                "artifact_id": artifact_id,
                "summary": summary,
                "tool_excute_result": payload,
                "isError": False,
            },
        )

    def _output_directory(self, artifact_id: str) -> Path:
        root = Path(self.cfg.local_mcp_server.server_cache_dir).expanduser()
        if not root.is_absolute():
            root = Path.cwd() / root
        destination = (root / self.session_id / artifact_id).resolve(strict=False)
        destination.mkdir(parents=True, exist_ok=True)
        return destination

    @staticmethod
    def _asset_summary(asset, preview_url: PreviewUrlBuilder) -> dict[str, Any]:
        proxy = asset.metadata.get("proxy", {}) if isinstance(asset.metadata, dict) else {}
        source_names = [Path(path).name for path in asset.source_paths]
        media_path = asset.proxy_path or asset.master_path
        return {
            "schema_version": asset.schema_version,
            "asset_id": asset.asset_id,
            "badge": "360° spherical",
            "projection": asset.projection.value,
            "source_type": asset.source_type.value,
            "source_names": source_names,
            "source_hashes": asset.source_hashes,
            "master_name": Path(asset.master_path).name,
            "proxy_name": Path(asset.proxy_path).name if asset.proxy_path else None,
            "preview_url": preview_url(Path(media_path)),
            "width": asset.width,
            "height": asset.height,
            "fps": asset.fps,
            "duration_seconds": asset.duration_seconds,
            "audio_stream_index": asset.audio_stream_index,
            "camera_model": asset.camera_model,
            "source_video_codec": asset.metadata.get("source_video_codec"),
            "proxy": proxy,
            "stitch_status": {
                "provider": asset.ingest.provider,
                "stabilization": asset.ingest.stabilization,
                "stitch_type": asset.ingest.stitch_type,
                "status": (
                    "studio_export"
                    if asset.ingest.provider == "studio-export"
                    else "worker_output"
                ),
            },
        }

    def worker_status(self) -> dict[str, Any]:
        configured = {
            "transport": self.cfg.spherical.worker_transport,
            "configured": bool(self.cfg.spherical.enable_sdk_worker),
            "status": "disabled" if not self.cfg.spherical.enable_sdk_worker else "unavailable",
            "can_stitch": False,
            "can_decode_insv": False,
        }
        if not self.cfg.spherical.enable_sdk_worker:
            return configured
        client = None
        try:
            client = create_stitch_worker_client(self.cfg.spherical)
            capability = client.capabilities()
            configured.update(
                {
                    "status": "ready" if capability.can_stitch else "limited",
                    "provider": capability.provider,
                    "worker_version": capability.worker_version,
                    "sdk_enabled": capability.sdk_enabled,
                    "sdk_version": capability.sdk_version,
                    "can_stitch": capability.can_stitch,
                    "can_decode_insv": capability.can_decode_insv,
                    "requires_gpu": capability.requires_gpu,
                    "target_platform": capability.target_platform,
                    "warnings": capability.warnings,
                }
            )
        except Exception as exc:
            configured["message"] = str(exc)
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                close()
        return configured

    def _render_history(self, preview_url: PreviewUrlBuilder) -> list[dict[str, Any]]:
        renders: list[dict[str, Any]] = []
        for meta in self.store.list_meta(node_ids=_RENDER_NODE_IDS):
            _, data = self.store.load_result(meta.artifact_id)
            payload = data.get("payload", {}) if isinstance(data, dict) else {}
            if not isinstance(payload, dict):
                continue
            compact = None
            kind = meta.node_id
            for key in (
                "preview_render",
                "reframed_clip",
                "suggestion_preview",
                "director_preview",
                "director_render",
            ):
                value = payload.get(key)
                if isinstance(value, dict) and value.get("output_path"):
                    compact = value
                    break
            if compact is None:
                continue
            record = {
                "artifact_id": meta.artifact_id,
                "kind": kind,
                "created_at": meta.created_at,
                **compact,
            }
            record["preview_url"] = preview_url(Path(compact["output_path"]))
            renders.append(record)
        return renders[-20:]

    def snapshot(self, preview_url: PreviewUrlBuilder) -> SphericalUiState:
        if not self.cfg.spherical.enabled:
            return SphericalUiState(
                enabled=False,
                worker=self.worker_status(),
                warnings=[
                    "Spherical editing is disabled in config.toml. Enable it and restart to use this workspace."
                ],
            )

        assets = [self._asset_summary(asset, preview_url) for asset in self.repository.list_assets()]
        tracks = [
            {
                **compact_track(record.track, record.revision, record.deleted),
                "updated_at": record.updated_at,
                "origin": record.track.created_by.value,
            }
            for record in self.repository.list_track_records(include_deleted=True)
        ]
        suggestions = [
            {
                **compact_suggestion(record.suggestion),
                "camera_track": record.suggestion.camera_track.model_dump(mode="json"),
                "warnings": record.suggestion.warnings,
                "updated_at": record.updated_at,
            }
            for record in self.repository.list_suggestion_records(include_decided=True)
        ]
        director_proposals = [
            {**compact_proposal(record.proposal), "updated_at": record.updated_at}
            for record in self.repository.list_director_proposal_records(include_decided=True)
        ]
        subjects: list[dict[str, Any]] = []
        for analysis_record in self.repository.list_analysis_records():
            for subject in analysis_record.analysis.subject_tracks:
                subjects.append(
                    {
                        "analysis_id": analysis_record.analysis.analysis_id,
                        "asset_id": subject.asset_id,
                        "subject_id": subject.subject_id,
                        "start_seconds": subject.start_seconds,
                        "end_seconds": subject.end_seconds,
                        "mean_confidence": subject.mean_confidence,
                        "labels": subject.identity_evidence.get("labels", []),
                        "warnings": subject.warnings,
                    }
                )
        warnings: list[str] = []
        for asset in assets:
            if asset["stitch_status"]["status"] == "studio_export":
                warnings.append(
                    f"{asset['asset_id']} was imported from a Studio export; raw .insv was not decoded here."
                )
        for suggestion in suggestions:
            warnings.extend(suggestion.get("warnings", []))
        for proposal in director_proposals:
            warnings.extend(proposal.get("warnings", []))
        return SphericalUiState(
            enabled=True,
            assets=assets,
            tracks=tracks,
            suggestions=suggestions,
            director_proposals=director_proposals,
            subjects=subjects,
            renders=self._render_history(preview_url),
            worker=self.worker_status(),
            warnings=list(dict.fromkeys(str(item) for item in warnings if item)),
        )

    def create_track(self, request: SphericalUiTrackCreateRequest) -> SphericalUiMutationResponse:
        self._require_enabled()
        track = request.track
        asset = self.repository.get_asset(track.asset_id)
        if track.source_end_seconds > asset.duration_seconds + 1e-6:
            raise SphericalValidationError("Camera track exceeds the imported asset duration.")
        if track.track_id in {
            record.track.track_id
            for record in self.repository.list_track_records(include_deleted=True)
        }:
            raise SphericalValidationError(
                "Virtual-camera track ID already exists in this session.",
                details={"track_id": track.track_id},
            )
        artifact_id = self._artifact_id("create_virtual_camera_track")
        self._save(
            "create_virtual_camera_track",
            artifact_id,
            {
                "camera_track_event": track_event("create", track, 1),
                "camera_track": compact_track(track, 1),
                "ui_origin": True,
            },
            f"Created virtual camera track {track.track_id} from the spherical UI.",
        )
        return SphericalUiMutationResponse(
            artifact_id=artifact_id,
            track=track,
            revision=1,
            camera_track_hash=sha256_json(track.model_dump(mode="json")),
        )

    def update_track(self, track_id: str, request: SphericalUiTrackUpdateRequest) -> SphericalUiMutationResponse:
        self._require_enabled()
        current = self.repository.get_track(track_id)
        track = request.track
        if current.revision != request.expected_revision:
            raise SphericalValidationError(
                "Camera track changed after the UI loaded it. Refresh before saving.",
                details={
                    "track_id": track_id,
                    "expected_revision": request.expected_revision,
                    "actual_revision": current.revision,
                },
            )
        if track.track_id != track_id or track.asset_id != current.track.asset_id:
            raise SphericalValidationError("Camera-track identity cannot change during update.")
        asset = self.repository.get_asset(track.asset_id)
        if track.source_end_seconds > asset.duration_seconds + 1e-6:
            raise SphericalValidationError("Updated camera track exceeds the imported asset duration.")
        revision = current.revision + 1
        artifact_id = self._artifact_id("update_virtual_camera_track")
        self._save(
            "update_virtual_camera_track",
            artifact_id,
            {
                "camera_track_event": track_event("update", track, revision),
                "camera_track": compact_track(track, revision),
                "ui_origin": True,
            },
            f"Updated virtual camera track {track.track_id} to revision {revision} from the spherical UI.",
        )
        return SphericalUiMutationResponse(
            artifact_id=artifact_id,
            track=track,
            revision=revision,
            camera_track_hash=sha256_json(track.model_dump(mode="json")),
        )

    def accept_suggestion(self, suggestion_id: str) -> SphericalUiMutationResponse:
        self._require_enabled()
        record = self.repository.get_suggestion(suggestion_id)
        if record.suggestion.status != SuggestionStatus.PROPOSED:
            raise SphericalValidationError("Only a proposed suggestion can be accepted.")
        track = record.suggestion.camera_track
        if track.track_id in {
            item.track.track_id
            for item in self.repository.list_track_records(include_deleted=True)
        }:
            raise SphericalValidationError("Suggestion camera track already exists.")
        accepted = record.suggestion.model_copy(update={"status": SuggestionStatus.ACCEPTED})
        artifact_id = self._artifact_id("accept_reframe_suggestion")
        self._save(
            "accept_reframe_suggestion",
            artifact_id,
            {
                "reframe_suggestion_event": suggestion_event("accept", accepted),
                "camera_track_event": camera_track_create_event(track),
                "accepted_suggestion": compact_suggestion(accepted),
                "camera_track": compact_track(track, 1),
                "ui_origin": True,
            },
            f"Accepted suggestion {suggestion_id} from the spherical UI.",
        )
        return SphericalUiMutationResponse(
            artifact_id=artifact_id,
            track=track,
            revision=1,
            camera_track_hash=sha256_json(track.model_dump(mode="json")),
        )

    def accept_director_proposal(self, proposal_id: str) -> SphericalUiMutationResponse:
        self._require_enabled()
        record = self.repository.get_director_proposal(proposal_id)
        if record.proposal.status != DirectorProposalStatus.PROPOSED:
            raise SphericalValidationError("Only a proposed director plan can be accepted.")
        track = record.proposal.camera_track
        if track.track_id in {
            item.track.track_id
            for item in self.repository.list_track_records(include_deleted=True)
        }:
            raise SphericalValidationError("Director proposal camera track already exists.")
        accepted = record.proposal.model_copy(update={"status": DirectorProposalStatus.ACCEPTED})
        artifact_id = self._artifact_id("accept_director_proposal")
        self._save(
            "accept_director_proposal",
            artifact_id,
            {
                "director_proposal_event": proposal_event("accept", accepted),
                "camera_track_event": camera_track_create_event(track),
                "accepted_director_proposal": compact_proposal(accepted),
                "ui_origin": True,
            },
            f"Accepted director proposal {proposal_id} from the spherical UI.",
        )
        return SphericalUiMutationResponse(
            artifact_id=artifact_id,
            track=track,
            revision=1,
            camera_track_hash=sha256_json(track.model_dump(mode="json")),
        )

    @staticmethod
    def _segment_track(track: VirtualCameraTrack, center: float, duration: float) -> VirtualCameraTrack:
        half = duration / 2.0
        start = max(track.source_start_seconds, center - half)
        end = min(track.source_end_seconds, center + half)
        if end - start < 0.25:
            if start <= track.source_start_seconds + 1e-9:
                end = min(track.source_end_seconds, start + 0.25)
            else:
                start = max(track.source_start_seconds, end - 0.25)
        if end <= start:
            raise SphericalValidationError("Preview segment has no duration.")
        keyframes = [sample_camera_track(track, start)]
        keyframes.extend(
            keyframe
            for keyframe in track.keyframes
            if start < keyframe.time_seconds < end
        )
        keyframes.append(sample_camera_track(track, end))
        return track.model_copy(
            update={
                "source_start_seconds": start,
                "source_end_seconds": end,
                "keyframes": keyframes,
            }
        )

    def render_preview(self, track_id: str, request: SphericalUiPreviewRequest) -> SphericalUiRenderResponse:
        self._require_enabled()
        record = self.repository.get_track(track_id)
        track = record.track
        if request.center_seconds is not None:
            if not (
                track.source_start_seconds
                <= request.center_seconds
                <= track.source_end_seconds
            ):
                raise SphericalValidationError(
                    "Preview center must remain inside the camera-track source interval."
                )
            track = self._segment_track(track, request.center_seconds, request.duration_seconds)
        width, height = preview_dimensions(track, request.max_dimension)
        track = track.model_copy(update={"output_width": width, "output_height": height})
        artifact_id = self._artifact_id("preview_virtual_camera_track")
        destination = self._output_directory(artifact_id) / "camera-preview.mp4"
        manifest = SphericalRenderer().render(
            ReframeRenderRequest(
                asset=self.repository.get_asset(track.asset_id),
                track=track,
                output_path=destination,
                workspace_root=destination.parent,
                cache_dir=self.cfg.spherical.cache_dir,
                sample_interval_seconds=(
                    request.sample_interval_seconds
                    or self.cfg.spherical.renderer_sample_interval_seconds
                ),
                use_cache=request.use_cache,
                overwrite=False,
            )
        )
        probe_media(destination)
        compact = {
            "track_id": record.track.track_id,
            "track_revision": record.revision,
            "segment_start_seconds": track.source_start_seconds,
            "segment_end_seconds": track.source_end_seconds,
            **compact_manifest(manifest),
        }
        self._save(
            "preview_virtual_camera_track",
            artifact_id,
            {"preview_render": compact, "ui_origin": True},
            f"Rendered a UI preview for camera track {track_id}.",
        )
        return SphericalUiRenderResponse(
            artifact_id=artifact_id,
            track_id=record.track.track_id,
            track_revision=record.revision,
            **compact_manifest(manifest),
        )

    def render_final(self, track_id: str, request: SphericalUiRenderRequest) -> SphericalUiRenderResponse:
        self._require_enabled()
        record = self.repository.get_track(track_id)
        artifact_id = self._artifact_id("render_reframed_clip")
        filename = safe_output_filename(request.output_filename, track_id)
        destination = self._output_directory(artifact_id) / filename
        manifest = SphericalRenderer().render(
            ReframeRenderRequest(
                asset=self.repository.get_asset(record.track.asset_id),
                track=record.track,
                output_path=destination,
                workspace_root=destination.parent,
                cache_dir=self.cfg.spherical.cache_dir,
                sample_interval_seconds=(
                    request.sample_interval_seconds
                    or self.cfg.spherical.renderer_sample_interval_seconds
                ),
                use_cache=request.use_cache,
                overwrite=False,
            )
        )
        probe_media(destination)
        compact = {
            "asset_id": record.track.asset_id,
            "track_id": record.track.track_id,
            "track_revision": record.revision,
            **compact_manifest(manifest),
        }
        self._save(
            "render_reframed_clip",
            artifact_id,
            {"reframed_clip": compact, "ui_origin": True},
            f"Rendered an ordinary flat clip from UI camera track {track_id}.",
        )
        return SphericalUiRenderResponse(
            artifact_id=artifact_id,
            track_id=record.track.track_id,
            track_revision=record.revision,
            **compact_manifest(manifest),
        )
