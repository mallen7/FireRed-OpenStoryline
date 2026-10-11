"""Import a stitched equirectangular MP4 into the active OpenStoryline session."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel

from open_storyline.nodes.core_nodes.base_node import NodeMeta
from open_storyline.nodes.node_state import NodeState
from open_storyline.nodes.spherical_nodes.common import (
    SphericalNodeBase,
    normal_video_media_item,
)
from open_storyline.nodes.spherical_nodes.schemas import ImportSphericalMediaInput
from open_storyline.spherical.errors import (
    SphericalCapabilityError,
    SphericalProbeError,
    SphericalValidationError,
)
from open_storyline.spherical.ffmpeg import probe_media
from open_storyline.spherical.manifests import sha256_file
from open_storyline.spherical.media import generate_proxy
from open_storyline.spherical.schemas import SphericalAsset
from open_storyline.utils.register import NODE_REGISTRY


def _stream_metadata(path: Path, ffprobe_executable: str = "ffprobe") -> dict[str, Any]:
    ffprobe = shutil.which(ffprobe_executable)
    if not ffprobe:
        raise SphericalCapabilityError("ffprobe is required for spherical import.")
    command = [
        ffprobe,
        "-v",
        "error",
        "-show_streams",
        "-of",
        "json",
        str(path),
    ]
    completed = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise SphericalProbeError(
            "ffprobe stream inspection failed.",
            details={"command": command, "stderr": completed.stderr[-8000:]},
        )
    try:
        streams = json.loads(completed.stdout).get("streams", [])
    except (json.JSONDecodeError, AttributeError) as exc:
        raise SphericalProbeError("ffprobe returned invalid stream JSON.") from exc
    video = next(
        (stream for stream in streams if stream.get("codec_type") == "video"),
        {},
    )
    audio = [stream for stream in streams if stream.get("codec_type") == "audio"]
    return {
        "video": {
            "stream_index": video.get("index"),
            "codec": video.get("codec_name"),
            "pixel_format": video.get("pix_fmt"),
            "color_space": video.get("color_space"),
            "color_transfer": video.get("color_transfer"),
        },
        "audio": [
            {
                "stream_index": stream.get("index"),
                "relative_audio_index": index,
                "codec": stream.get("codec_name"),
                "sample_rate": int(stream["sample_rate"])
                if stream.get("sample_rate")
                else None,
                "channels": stream.get("channels"),
                "channel_layout": stream.get("channel_layout"),
            }
            for index, stream in enumerate(audio)
        ],
    }


@NODE_REGISTRY.register()
class ImportSphericalMediaNode(SphericalNodeBase):
    meta = NodeMeta(
        name="import_spherical_media",
        description=(
            "Import one already-stitched approximately 2:1 equirectangular MP4, "
            "hash and ffprobe it, and create a session-scoped SphericalAsset plus "
            "inspection proxy. This node does not decode raw .insv; stitch it with "
            "an enabled MediaSDK worker or export it through Insta360 Studio first."
        ),
        node_id="import_spherical_media",
        node_kind="spherical_asset",
        require_prior_kind=[],
        default_require_prior_kind=[],
        next_available_node=[
            "inspect_spherical_media",
            "create_virtual_camera_track",
        ],
    )
    input_schema: ClassVar[type[BaseModel]] = ImportSphericalMediaInput

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
        source_input = inputs["inputs"][0]
        source = self._validate_source_path(node_state, source_input["path"])
        suffix = source.suffix.lower()
        if suffix == ".insv":
            raise SphericalCapabilityError(
                "Raw .insv is not decoded by import_spherical_media. Run it through "
                "an enabled worker with SDK stitching capability, or export a "
                "stabilized 2:1 MP4 through Insta360 Studio, then import that MP4.",
                details={"worker_required": True, "path": str(source)},
            )
        if suffix != ".mp4":
            raise SphericalValidationError(
                "Spherical import currently accepts stitched MP4 files only.",
                details={"suffix": suffix},
            )

        probe = probe_media(source)
        if abs(probe.width / probe.height - 2.0) > 0.04:
            raise SphericalValidationError(
                "Spherical source must be approximately 2:1 equirectangular media.",
                details={"width": probe.width, "height": probe.height},
            )
        source_hash = sha256_file(source)
        stream_metadata = _stream_metadata(source)
        output_dir = self._prepare_output_directory(node_state)
        proxy_path: Path | None = None
        proxy_result = None
        if inputs["generate_proxy"]:
            proxy_path = output_dir / "spherical-proxy.mp4"
            proxy_result = generate_proxy(
                SphericalAsset(
                    asset_id=f"spherical-{source_hash[:16]}",
                    source_paths=[source],
                    source_hashes=[source_hash],
                    master_path=source,
                    width=probe.width,
                    height=probe.height,
                    fps=probe.fps,
                    duration_seconds=probe.duration_seconds,
                    audio_stream_index=0 if probe.audio_stream_count else None,
                    metadata={"streams": stream_metadata},
                ),
                proxy_path,
                cache_dir=self.server_cfg.spherical.cache_dir,
                proxy_width=(
                    inputs["proxy_width"] or self.server_cfg.spherical.proxy_width
                ),
            )

        asset = SphericalAsset(
            asset_id=f"spherical-{source_hash[:16]}",
            source_paths=[source],
            source_hashes=[source_hash],
            camera_model=None,
            master_path=source,
            proxy_path=proxy_path,
            width=probe.width,
            height=probe.height,
            fps=probe.fps,
            duration_seconds=probe.duration_seconds,
            audio_stream_index=0 if probe.audio_stream_count else None,
            metadata={
                "streams": stream_metadata,
                "source_filename": source.name,
                "source_video_codec": probe.video_codec,
                "proxy": {
                    "generated": proxy_result is not None,
                    "sha256": proxy_result.output_hash if proxy_result else None,
                    "width": proxy_result.width if proxy_result else None,
                    "height": proxy_result.height if proxy_result else None,
                    "cache_hit": proxy_result.cache_hit if proxy_result else None,
                },
            },
        )
        node_state.node_summary.info_for_user(
            f"Imported spherical asset {asset.asset_id}: "
            f"{asset.width}x{asset.height}, {asset.duration_seconds:.2f}s."
        )
        node_state.node_summary.info_for_llm(
            "The source is a stitched equirectangular master. Inspect it before "
            "creating explicit yaw/pitch/FOV camera keyframes."
        )
        proxy_media = (
            [
                normal_video_media_item(
                    proxy_path,
                    probe_media(proxy_path),
                    media_id=f"{asset.asset_id}-proxy",
                )
            ]
            if proxy_path is not None
            else []
        )
        return {
            "spherical_asset": asset.model_dump(mode="json"),
            "asset_summary": {
                "asset_id": asset.asset_id,
                "projection": asset.projection.value,
                "resolution": [asset.width, asset.height],
                "fps": asset.fps,
                "duration_seconds": asset.duration_seconds,
                "video_codec": probe.video_codec,
                "audio_stream_count": probe.audio_stream_count,
                "proxy_path": str(proxy_path) if proxy_path else None,
            },
            "proxy_media": proxy_media,
        }
