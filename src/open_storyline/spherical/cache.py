"""Validated content-addressed cache for spherical renders."""

from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from open_storyline.spherical.errors import SphericalCacheError
from open_storyline.spherical.manifests import (
    atomic_write_json,
    load_json_model,
    sha256_file,
    sha256_json,
)
from open_storyline.spherical.schemas import (
    ReframeRenderManifest,
    ReframeRenderRequest,
)


@dataclass(frozen=True)
class RenderIdentity:
    render_key: str
    camera_track_hash: str


@dataclass(frozen=True)
class CacheLookup:
    hit: bool
    reason: str
    output_path: Path | None = None
    manifest: ReframeRenderManifest | None = None


def compute_render_identity(
    request: ReframeRenderRequest,
    *,
    source_hash: str,
    ffmpeg_version: str,
    renderer_version: str,
) -> RenderIdentity:
    """Build the deterministic track hash and full render key."""

    track_payload = request.track.model_dump(mode="json")
    camera_track_hash = sha256_json(track_payload)
    render_key = sha256_json(
        {
            "source_hash": source_hash,
            "source_interval": [
                request.track.source_start_seconds,
                request.track.source_end_seconds,
            ],
            "camera_track": track_payload,
            "output_dimensions": [
                request.track.output_width,
                request.track.output_height,
            ],
            "codec_settings": request.codec_settings.model_dump(mode="json"),
            "sample_interval_seconds": request.sample_interval_seconds,
            "ffmpeg_version": ffmpeg_version,
            "renderer_version": renderer_version,
        }
    )
    return RenderIdentity(
        render_key=render_key,
        camera_track_hash=camera_track_hash,
    )


class RenderCache:
    """Store an MP4 and its manifest as one validated cache entry."""

    OUTPUT_FILENAME = "output.mp4"
    MANIFEST_FILENAME = "manifest.json"

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve(strict=False)

    def entry_dir(self, render_key: str) -> Path:
        return self.root / render_key

    def lookup(self, render_key: str) -> CacheLookup:
        entry = self.entry_dir(render_key)
        output = entry / self.OUTPUT_FILENAME
        manifest_path = entry / self.MANIFEST_FILENAME
        if not entry.exists():
            return CacheLookup(False, "entry does not exist")
        if not output.is_file() or not manifest_path.is_file():
            return CacheLookup(False, "entry is missing output or manifest")

        try:
            manifest = load_json_model(manifest_path, ReframeRenderManifest)
        except Exception as exc:
            return CacheLookup(False, f"manifest validation failed: {exc}")
        if manifest.render_key != render_key:
            return CacheLookup(False, "manifest render key does not match entry")

        try:
            actual_hash = sha256_file(output)
        except OSError as exc:
            return CacheLookup(False, f"cached output could not be hashed: {exc}")
        if actual_hash != manifest.output_hash:
            return CacheLookup(False, "cached output hash does not match manifest")

        return CacheLookup(
            True,
            "validated cache hit",
            output_path=output,
            manifest=manifest,
        )

    def store(
        self,
        output_path: str | Path,
        manifest: ReframeRenderManifest,
    ) -> CacheLookup:
        """Atomically create or repair a cache entry."""

        source = Path(output_path)
        if not source.is_file():
            raise SphericalCacheError(
                "Cannot cache a missing render output.",
                details={"path": str(source)},
            )

        self.root.mkdir(parents=True, exist_ok=True)
        target = self.entry_dir(manifest.render_key)
        existing = self.lookup(manifest.render_key)
        if existing.hit:
            return existing

        temporary = Path(
            tempfile.mkdtemp(prefix=f".{manifest.render_key}.", dir=self.root)
        )
        try:
            cached_output = temporary / self.OUTPUT_FILENAME
            cached_manifest_path = temporary / self.MANIFEST_FILENAME
            shutil.copy2(source, cached_output)
            if sha256_file(cached_output) != manifest.output_hash:
                raise SphericalCacheError("Cached copy failed output hash validation.")

            cached_manifest = manifest.model_copy(
                update={
                    "output_path": target / self.OUTPUT_FILENAME,
                    "cache_hit": False,
                }
            )
            atomic_write_json(cached_manifest_path, cached_manifest)

            if target.exists():
                shutil.rmtree(target)
            os.replace(temporary, target)
        except Exception as exc:
            if isinstance(exc, SphericalCacheError):
                raise
            raise SphericalCacheError(
                "Failed to store spherical render cache entry.",
                details={"render_key": manifest.render_key, "error": str(exc)},
            ) from exc
        finally:
            if temporary.exists():
                shutil.rmtree(temporary, ignore_errors=True)

        result = self.lookup(manifest.render_key)
        if not result.hit:
            raise SphericalCacheError(
                "New cache entry failed validation.",
                details={"reason": result.reason},
            )
        return result
