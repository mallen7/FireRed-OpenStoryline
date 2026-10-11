"""Shared node lifecycle helpers for feature-gated spherical tools."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from open_storyline.nodes.core_nodes.base_node import BaseNode
from open_storyline.nodes.node_state import NodeState
from open_storyline.spherical.errors import (
    SphericalDisabledError,
    SphericalValidationError,
)
from open_storyline.spherical.ffmpeg import MediaProbe
from open_storyline.spherical.session import SphericalSessionRepository
from open_storyline.storage.agent_memory import ArtifactStore


class SphericalNodeBase(BaseNode):
    """Validate public MCP inputs and bind nodes to the active artifact session."""

    def _parse_input(self, node_state: NodeState, inputs: dict[str, Any]):
        if not self.server_cfg.spherical.enabled:
            raise SphericalDisabledError(
                "Spherical MCP tools are disabled. Set spherical.enabled=true to use them."
            )
        if self.input_schema is None:
            return inputs
        public_payload = {
            field_name: inputs[field_name]
            for field_name in self.input_schema.model_fields
            if field_name in inputs
        }
        validated = self.input_schema.model_validate(public_payload)
        parsed = dict(inputs)
        parsed.update(validated.model_dump(mode="python"))
        return parsed

    def _artifact_store(self, node_state: NodeState) -> ArtifactStore:
        try:
            lifecycle = node_state.mcp_ctx.request_context.lifespan_context
            if hasattr(lifecycle, "get_artifact_store"):
                return lifecycle.get_artifact_store(node_state.session_id)
        except (AttributeError, TypeError):
            pass
        return ArtifactStore(
            self.server_cfg.project.outputs_dir,
            session_id=node_state.session_id,
        )

    def _repository(self, node_state: NodeState) -> SphericalSessionRepository:
        return SphericalSessionRepository(self._artifact_store(node_state))

    def _allowed_source_roots(self, node_state: NodeState) -> list[Path]:
        media_root = Path(self.server_cfg.project.media_dir).expanduser().resolve()
        outputs_root = Path(self.server_cfg.project.outputs_dir).expanduser().resolve()
        cache_root = Path(self.server_cache_dir).expanduser().resolve()
        return [
            media_root,
            media_root.parent / node_state.session_id / media_root.name,
            outputs_root / node_state.session_id,
            cache_root / node_state.session_id,
        ]

    def _validate_source_path(self, node_state: NodeState, value: str | Path) -> Path:
        path = Path(value).expanduser().resolve()
        if not path.is_file():
            raise SphericalValidationError(
                "Spherical source file does not exist.",
                details={"path": str(path)},
            )
        for root in self._allowed_source_roots(node_state):
            try:
                path.relative_to(root.resolve(strict=False))
                return path
            except ValueError:
                continue
        raise SphericalValidationError(
            "Spherical source path is outside the allowed session/media roots.",
            details={
                "path": str(path),
                "allowed_roots": [
                    str(root.resolve(strict=False))
                    for root in self._allowed_source_roots(node_state)
                ],
            },
        )


def normal_video_media_item(
    path: Path,
    probe: MediaProbe,
    *,
    media_id: str,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "duration": int(round(probe.duration_seconds * 1000)),
        "width": probe.width,
        "height": probe.height,
        "fps": probe.fps,
        "has_audio": probe.audio_stream_count > 0,
    }
    if probe.audio_sample_rates:
        metadata["audio_sample_rate_hz"] = probe.audio_sample_rates[0]
    return {
        "media_id": media_id,
        "path": str(path),
        "media_type": "video",
        "metadata": metadata,
        "orig_path": str(path),
        "orig_md5": None,
    }


def image_media_item(path: Path, *, media_id: str, kind: str) -> dict[str, Any]:
    return {
        "media_id": media_id,
        "path": str(path),
        "media_type": "image",
        "kind": kind,
        "orig_path": str(path),
        "orig_md5": None,
    }
