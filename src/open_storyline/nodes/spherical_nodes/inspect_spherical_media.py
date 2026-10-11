"""Generate session-scoped panoramic inspection artifacts."""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel

from open_storyline.nodes.core_nodes.base_node import NodeMeta
from open_storyline.nodes.node_state import NodeState
from open_storyline.nodes.spherical_nodes.common import (
    SphericalNodeBase,
    image_media_item,
)
from open_storyline.nodes.spherical_nodes.schemas import InspectSphericalMediaInput
from open_storyline.spherical.ffmpeg import probe_media
from open_storyline.spherical.media import generate_contact_sheets, generate_proxy
from open_storyline.utils.register import NODE_REGISTRY


@NODE_REGISTRY.register()
class InspectSphericalMediaNode(SphericalNodeBase):
    meta = NodeMeta(
        name="inspect_spherical_media",
        description=(
            "Inspect an imported equirectangular asset. Returns compact metadata, "
            "sample timestamps, a panoramic contact sheet, and optional front/right/"
            "back/left/up/down perspective views for deterministic camera planning."
        ),
        node_id="inspect_spherical_media",
        node_kind="spherical_inspection",
        require_prior_kind=["spherical_asset"],
        default_require_prior_kind=["spherical_asset"],
        next_available_node=["create_virtual_camera_track"],
    )
    input_schema: ClassVar[type[BaseModel]] = InspectSphericalMediaInput

    async def default_process(
        self,
        node_state: NodeState,
        inputs: dict[str, Any],
    ) -> dict[str, Any]:
        return await self.process(node_state, inputs)

    async def process(
        self,
        node_state: NodeState,
        inputs: dict[str, Any],
    ) -> dict[str, Any]:
        asset = self._repository(node_state).get_asset(inputs["asset_id"])
        output_dir = self._prepare_output_directory(node_state)
        proxy_path = asset.proxy_path
        proxy_cache_hit: bool | None = None
        if proxy_path is None or not Path(proxy_path).is_file():
            proxy_result = generate_proxy(
                asset,
                output_dir / "spherical-proxy.mp4",
                cache_dir=self.server_cfg.spherical.cache_dir,
                proxy_width=self.server_cfg.spherical.proxy_width,
            )
            proxy_path = proxy_result.path
            proxy_cache_hit = proxy_result.cache_hit
        contacts = generate_contact_sheets(
            asset,
            proxy_path,
            output_dir,
            cache_dir=self.server_cfg.spherical.cache_dir,
            sample_count=inputs["sample_count"],
            thumbnail_width=(
                inputs["thumbnail_width"]
                or self.server_cfg.spherical.contact_sheet_thumbnail_width
            ),
            include_perspective=inputs["include_perspective"],
            perspective_time_seconds=inputs["perspective_time_seconds"],
        )
        preview_paths = [str(contacts.equirectangular_path)]
        contact_media = [
            image_media_item(
                contacts.equirectangular_path,
                media_id=f"{asset.asset_id}-equirect-contact",
                kind="equirectangular_contact_sheet",
            )
        ]
        if contacts.perspective_path is not None:
            preview_paths.append(str(contacts.perspective_path))
            contact_media.append(
                image_media_item(
                    contacts.perspective_path,
                    media_id=f"{asset.asset_id}-perspective-contact",
                    kind="perspective_contact_sheet",
                )
            )
        node_state.node_summary.info_for_user(
            f"Created spherical inspection sheets for {asset.asset_id}.",
            preview_urls=preview_paths,
        )
        node_state.node_summary.info_for_llm(
            "Use the labeled perspective directions and canonical coordinates when "
            "creating a virtual camera track: front=0, right=90, back=180, left=-90."
        )
        proxy_probe = probe_media(proxy_path)
        return {
            "spherical_inspection": {
                "asset_id": asset.asset_id,
                "projection": asset.projection.value,
                "duration_seconds": asset.duration_seconds,
                "resolution": [asset.width, asset.height],
                "fps": asset.fps,
                "audio_stream_index": asset.audio_stream_index,
                "proxy_path": str(proxy_path),
                "proxy_resolution": [proxy_probe.width, proxy_probe.height],
                "sample_timestamps": contacts.sample_timestamps,
                "equirectangular_contact_sheet_path": str(
                    contacts.equirectangular_path
                ),
                "perspective_contact_sheet_path": (
                    str(contacts.perspective_path)
                    if contacts.perspective_path
                    else None
                ),
                "perspective_views": contacts.perspective_views,
                "inspection_cache_hit": contacts.cache_hit,
                "proxy_cache_hit": proxy_cache_hit,
            },
            "contact_sheets": contact_media,
        }
