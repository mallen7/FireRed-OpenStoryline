"""Session-scoped virtual-camera CRUD, preview, validation, and rendering nodes."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel

from open_storyline.nodes.core_nodes.base_node import NodeMeta
from open_storyline.nodes.node_state import NodeState
from open_storyline.nodes.spherical_nodes.common import (
    SphericalNodeBase,
    normal_video_media_item,
)
from open_storyline.nodes.spherical_nodes.schemas import (
    CreateVirtualCameraTrackInput,
    DeleteVirtualCameraTrackInput,
    ListVirtualCameraTracksInput,
    PreviewVirtualCameraTrackInput,
    RenderReframedClipInput,
    UpdateVirtualCameraTrackInput,
    ValidateVirtualCameraTrackInput,
    safe_output_filename,
)
from open_storyline.spherical.artifact_events import (
    compact_manifest as _compact_manifest,
    compact_track as _compact_track,
    preview_dimensions as _preview_dimensions,
    track_event as _track_event,
)
from open_storyline.spherical.errors import SphericalValidationError
from open_storyline.spherical.ffmpeg import SphericalRenderer, probe_media
from open_storyline.spherical.manifests import sha256_json
from open_storyline.spherical.schemas import (
    ReframeRenderManifest,
    ReframeRenderRequest,
    VirtualCameraTrack,
)
from open_storyline.utils.register import NODE_REGISTRY


class _CameraNode(SphericalNodeBase):
    async def default_process(
        self,
        node_state: NodeState,
        inputs: dict[str, Any],
    ) -> dict[str, Any]:
        return await self.process(node_state, inputs)


@NODE_REGISTRY.register()
class CreateVirtualCameraTrackNode(_CameraNode):
    meta = NodeMeta(
        name="create_virtual_camera_track",
        description=(
            "Create a schema-validated virtual camera plan for the latest or selected "
            "spherical asset. Convert natural-language directions into explicit "
            "time/yaw/pitch/roll/horizontal-FOV keyframes: front=0, right=90, "
            "back=180, left=-90. For a five-second right pan, use two timestamps "
            "five seconds apart. The resulting JSON remains editable and overridable."
        ),
        node_id="create_virtual_camera_track",
        node_kind="spherical_camera_track",
        require_prior_kind=["spherical_asset"],
        default_require_prior_kind=["spherical_asset"],
        next_available_node=[
            "validate_virtual_camera_track",
            "preview_virtual_camera_track",
            "render_reframed_clip",
        ],
    )
    input_schema: ClassVar[type[BaseModel]] = CreateVirtualCameraTrackInput

    async def process(self, node_state: NodeState, inputs: dict[str, Any]):
        repository = self._repository(node_state)
        asset = repository.get_asset(inputs["asset_id"])
        track_id = inputs["track_id"] or f"camera-{uuid.uuid4().hex[:16]}"
        existing_ids = {
            record.track.track_id
            for record in repository.list_track_records(include_deleted=True)
        }
        if track_id in existing_ids:
            raise SphericalValidationError(
                "Virtual-camera track ID already exists in this session.",
                details={"track_id": track_id},
            )
        track = VirtualCameraTrack(
            track_id=track_id,
            asset_id=asset.asset_id,
            name=inputs["name"],
            output_width=inputs["output_width"],
            output_height=inputs["output_height"],
            source_start_seconds=inputs["source_start_seconds"],
            source_end_seconds=(
                inputs["source_end_seconds"] or asset.duration_seconds
            ),
            keyframes=inputs["keyframes"],
            created_by=inputs["created_by"],
            generation_metadata=inputs["generation_metadata"],
        )
        if track.source_end_seconds > asset.duration_seconds + 1e-6:
            raise SphericalValidationError(
                "Camera track exceeds the imported asset duration."
            )
        node_state.node_summary.info_for_user(
            f"Created virtual camera track {track.track_id} with "
            f"{len(track.keyframes)} keyframe(s)."
        )
        node_state.node_summary.info_for_llm(
            "Preview this deterministic plan before the final render; revise it with "
            "update_virtual_camera_track if the framing is not correct."
        )
        return {
            "camera_track_event": _track_event("create", track, 1),
            "camera_track": _compact_track(track, 1),
        }


@NODE_REGISTRY.register()
class UpdateVirtualCameraTrackNode(_CameraNode):
    meta = NodeMeta(
        name="update_virtual_camera_track",
        description=(
            "Create a new validated revision of a session camera track. Supply only "
            "the fields to change; keyframes always replace the complete prior set. "
            "Yaw/roll wrap safely and invalid ranges, pitch, FOV, or timestamps fail."
        ),
        node_id="update_virtual_camera_track",
        node_kind="spherical_camera_track",
        require_prior_kind=["spherical_asset", "spherical_camera_track"],
        default_require_prior_kind=["spherical_asset", "spherical_camera_track"],
        next_available_node=[
            "validate_virtual_camera_track",
            "preview_virtual_camera_track",
            "render_reframed_clip",
        ],
    )
    input_schema: ClassVar[type[BaseModel]] = UpdateVirtualCameraTrackInput

    async def process(self, node_state: NodeState, inputs: dict[str, Any]):
        repository = self._repository(node_state)
        record = repository.get_track(inputs["track_id"])
        asset = repository.get_asset(record.track.asset_id)
        payload = record.track.model_dump(mode="python")
        for field_name in (
            "name",
            "output_width",
            "output_height",
            "source_start_seconds",
            "source_end_seconds",
            "keyframes",
            "generation_metadata",
        ):
            if inputs[field_name] is not None:
                payload[field_name] = inputs[field_name]
        track = VirtualCameraTrack.model_validate(payload)
        if track.source_end_seconds > asset.duration_seconds + 1e-6:
            raise SphericalValidationError(
                "Updated camera track exceeds the imported asset duration."
            )
        revision = record.revision + 1
        node_state.node_summary.info_for_user(
            f"Updated virtual camera track {track.track_id} to revision {revision}."
        )
        return {
            "camera_track_event": _track_event("update", track, revision),
            "camera_track": _compact_track(track, revision),
        }


@NODE_REGISTRY.register()
class ValidateVirtualCameraTrackNode(_CameraNode):
    meta = NodeMeta(
        name="validate_virtual_camera_track",
        description=(
            "Validate either an inline VirtualCameraTrack JSON object or a stored "
            "session track. Returns normalized angles and a deterministic track hash; "
            "schema validation cannot be bypassed."
        ),
        node_id="validate_virtual_camera_track",
        node_kind="spherical_camera_validation",
        require_prior_kind=["spherical_asset"],
        default_require_prior_kind=["spherical_asset"],
        next_available_node=["preview_virtual_camera_track"],
    )
    input_schema: ClassVar[type[BaseModel]] = ValidateVirtualCameraTrackInput

    async def process(self, node_state: NodeState, inputs: dict[str, Any]):
        repository = self._repository(node_state)
        if inputs["track"] is not None:
            track = VirtualCameraTrack.model_validate(inputs["track"])
            revision = None
        else:
            record = repository.get_track(inputs["track_id"])
            track = record.track
            revision = record.revision
        asset = repository.get_asset(track.asset_id)
        if track.source_end_seconds > asset.duration_seconds + 1e-6:
            raise SphericalValidationError(
                "Camera track exceeds the imported asset duration."
            )
        node_state.node_summary.info_for_user(
            f"Virtual camera track {track.track_id} is valid."
        )
        return {
            "validation": {
                "valid": True,
                "track_id": track.track_id,
                "revision": revision,
                "camera_track_hash": sha256_json(track.model_dump(mode="json")),
                "normalized_track": track.model_dump(mode="json"),
            }
        }


@NODE_REGISTRY.register()
class ListVirtualCameraTracksNode(_CameraNode):
    meta = NodeMeta(
        name="list_virtual_camera_tracks",
        description=(
            "List the latest camera-track revisions in the active session, optionally "
            "filtered by spherical asset. Tracks from other sessions are never visible."
        ),
        node_id="list_virtual_camera_tracks",
        node_kind="spherical_camera_track_list",
        require_prior_kind=[],
        default_require_prior_kind=[],
    )
    input_schema: ClassVar[type[BaseModel]] = ListVirtualCameraTracksInput

    async def process(self, node_state: NodeState, inputs: dict[str, Any]):
        records = self._repository(node_state).list_track_records(
            include_deleted=inputs["include_deleted"]
        )
        if inputs["asset_id"] is not None:
            records = [
                record
                for record in records
                if record.track.asset_id == inputs["asset_id"]
            ]
        tracks = [
            _compact_track(record.track, record.revision, record.deleted)
            for record in records
        ]
        node_state.node_summary.info_for_user(
            f"Found {len(tracks)} virtual camera track(s) in this session."
        )
        return {"camera_tracks": tracks, "count": len(tracks)}


@NODE_REGISTRY.register()
class DeleteVirtualCameraTrackNode(_CameraNode):
    meta = NodeMeta(
        name="delete_virtual_camera_track",
        description=(
            "Delete a stored virtual-camera track by appending a session-scoped "
            "tombstone revision. Prior artifact history remains auditable."
        ),
        node_id="delete_virtual_camera_track",
        node_kind="spherical_camera_track_deletion",
        require_prior_kind=["spherical_camera_track"],
        default_require_prior_kind=["spherical_camera_track"],
    )
    input_schema: ClassVar[type[BaseModel]] = DeleteVirtualCameraTrackInput

    async def process(self, node_state: NodeState, inputs: dict[str, Any]):
        record = self._repository(node_state).get_track(inputs["track_id"])
        revision = record.revision + 1
        node_state.node_summary.info_for_user(
            f"Deleted virtual camera track {record.track.track_id}."
        )
        return {
            "camera_track_event": _track_event("delete", record.track, revision),
            "deleted_track": {
                "track_id": record.track.track_id,
                "asset_id": record.track.asset_id,
                "revision": revision,
                "deleted": True,
            },
        }


class _RenderCameraNode(_CameraNode):
    def _render(
        self,
        node_state: NodeState,
        *,
        track: VirtualCameraTrack,
        destination: Path,
        sample_interval_seconds: float | None,
        use_cache: bool,
    ) -> ReframeRenderManifest:
        asset = self._repository(node_state).get_asset(track.asset_id)
        request = ReframeRenderRequest(
            asset=asset,
            track=track,
            output_path=destination,
            workspace_root=destination.parent,
            cache_dir=self.server_cfg.spherical.cache_dir,
            sample_interval_seconds=(
                sample_interval_seconds
                or self.server_cfg.spherical.renderer_sample_interval_seconds
            ),
            use_cache=use_cache,
            overwrite=False,
        )
        return SphericalRenderer().render(request)


@NODE_REGISTRY.register()
class PreviewVirtualCameraTrackNode(_RenderCameraNode):
    meta = NodeMeta(
        name="preview_virtual_camera_track",
        description=(
            "Render a lower-resolution preview of a stored camera plan. Use this after "
            "creating or revising a plan and before the full render. The preview uses "
            "the same deterministic schema, interpolation, FFmpeg backend, and cache."
        ),
        node_id="preview_virtual_camera_track",
        node_kind="spherical_camera_preview",
        require_prior_kind=["spherical_asset", "spherical_camera_track"],
        default_require_prior_kind=["spherical_asset", "spherical_camera_track"],
        next_available_node=[
            "update_virtual_camera_track",
            "render_reframed_clip",
        ],
    )
    input_schema: ClassVar[type[BaseModel]] = PreviewVirtualCameraTrackInput

    async def process(self, node_state: NodeState, inputs: dict[str, Any]):
        record = self._repository(node_state).get_track(inputs["track_id"])
        maximum = (
            inputs["max_dimension"]
            or self.server_cfg.spherical.preview_max_dimension
        )
        width, height = _preview_dimensions(record.track, maximum)
        preview_track = record.track.model_copy(
            update={"output_width": width, "output_height": height}
        )
        output_dir = self._prepare_output_directory(node_state)
        destination = output_dir / "camera-preview.mp4"
        manifest = self._render(
            node_state,
            track=preview_track,
            destination=destination,
            sample_interval_seconds=inputs["sample_interval_seconds"],
            use_cache=inputs["use_cache"],
        )
        probe = probe_media(destination)
        node_state.node_summary.info_for_user(
            f"Rendered camera preview for {record.track.track_id}.",
            preview_urls=[str(destination)],
        )
        return {
            "preview_render": {
                "track_id": record.track.track_id,
                "track_revision": record.revision,
                **_compact_manifest(manifest),
            },
            "preview_media": [
                normal_video_media_item(
                    destination,
                    probe,
                    media_id=f"{record.track.track_id}-preview",
                )
            ],
        }


@NODE_REGISTRY.register()
class RenderReframedClipNode(_RenderCameraNode):
    meta = NodeMeta(
        name="render_reframed_clip",
        description=(
            "Render the full-resolution stored virtual-camera plan into an ordinary "
            "flat H.264 MP4. The output is session-scoped and can be passed to the "
            "normal load_media workflow; raw spherical media never enters normal nodes."
        ),
        node_id="render_reframed_clip",
        node_kind="spherical_render",
        require_prior_kind=["spherical_asset", "spherical_camera_track"],
        default_require_prior_kind=["spherical_asset", "spherical_camera_track"],
        next_available_node=["load_media"],
    )
    input_schema: ClassVar[type[BaseModel]] = RenderReframedClipInput

    async def process(self, node_state: NodeState, inputs: dict[str, Any]):
        record = self._repository(node_state).get_track(inputs["track_id"])
        output_dir = self._prepare_output_directory(node_state)
        filename = safe_output_filename(
            inputs["output_filename"],
            record.track.track_id,
        )
        destination = output_dir / filename
        manifest = self._render(
            node_state,
            track=record.track,
            destination=destination,
            sample_interval_seconds=inputs["sample_interval_seconds"],
            use_cache=inputs["use_cache"],
        )
        probe = probe_media(destination)
        node_state.node_summary.info_for_user(
            f"Rendered ordinary clip from camera track {record.track.track_id}.",
            preview_urls=[str(destination)],
        )
        node_state.node_summary.info_for_llm(
            "This output is a flat ordinary MP4. Call load_media next to enter the "
            "existing split/understand/timeline/render workflow."
        )
        return {
            "reframed_clip": {
                "asset_id": record.track.asset_id,
                "track_id": record.track.track_id,
                "track_revision": record.revision,
                **_compact_manifest(manifest),
            },
            "media": [
                normal_video_media_item(
                    destination,
                    probe,
                    media_id=f"{record.track.track_id}-render",
                )
            ],
        }
