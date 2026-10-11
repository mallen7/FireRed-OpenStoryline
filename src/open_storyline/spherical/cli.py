"""Developer CLI for Phase 1 spherical validation, inspection, and rendering."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from open_storyline.config import default_config_path, load_settings
from open_storyline.spherical.errors import SphericalError, SphericalValidationError
from open_storyline.spherical.ffmpeg import SphericalRenderer, probe_media
from open_storyline.spherical.fixtures import generate_fixture
from open_storyline.spherical.manifests import (
    export_json_schemas,
    load_json_model,
    sha256_file,
)
from open_storyline.spherical.schemas import (
    ReframeRenderManifest,
    ReframeRenderRequest,
    SphericalAsset,
    VirtualCameraTrack,
)
from open_storyline.spherical.ui_schemas import export_spherical_ui_schemas


def _json_dump(value: Any, *, stream: Any = sys.stdout) -> None:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    print(json.dumps(value, indent=2, ensure_ascii=False, default=str), file=stream)


def _load_asset(path: Path) -> SphericalAsset:
    source_file = path.expanduser().resolve()
    asset = load_json_model(source_file, SphericalAsset)
    payload = asset.model_dump()
    base = source_file.parent

    def resolve(value: Path | None) -> Path | None:
        if value is None:
            return None
        return value if value.is_absolute() else (base / value).resolve(strict=False)

    payload["source_paths"] = [resolve(value) for value in asset.source_paths]
    payload["master_path"] = resolve(asset.master_path)
    payload["proxy_path"] = resolve(asset.proxy_path)
    return SphericalAsset.model_validate(payload)


def _runtime_paths(args: argparse.Namespace) -> tuple[Path, Path, float]:
    config_path = Path(args.config).expanduser()
    if config_path.is_file():
        spherical = load_settings(config_path).spherical
        configured_workspace = spherical.workspace_dir
        configured_cache = spherical.cache_dir
        configured_interval = spherical.renderer_sample_interval_seconds
    else:
        configured_workspace = Path.cwd() / "outputs"
        configured_cache = Path.cwd() / ".storyline" / "spherical-cache"
        configured_interval = 0.25
    workspace = Path(args.workspace) if args.workspace else configured_workspace
    cache = Path(args.cache_dir) if args.cache_dir else configured_cache
    interval = (
        args.sample_interval
        if args.sample_interval is not None
        else configured_interval
    )
    return (
        workspace.expanduser().resolve(strict=False),
        cache.expanduser().resolve(strict=False),
        interval,
    )


def _cmd_reframe(args: argparse.Namespace) -> int:
    workspace, cache_dir, interval = _runtime_paths(args)
    request = ReframeRenderRequest(
        asset=_load_asset(Path(args.asset)),
        track=load_json_model(args.track, VirtualCameraTrack),
        output_path=Path(args.output),
        workspace_root=workspace,
        cache_dir=cache_dir,
        sample_interval_seconds=interval,
        use_cache=not args.no_cache,
        overwrite=args.overwrite,
    )
    renderer = SphericalRenderer(
        ffmpeg_executable=args.ffmpeg,
        ffprobe_executable=args.ffprobe,
    )
    _json_dump(renderer.render(request))
    return 0


def _cmd_validate_track(args: argparse.Namespace) -> int:
    track = load_json_model(args.track, VirtualCameraTrack)
    _json_dump(
        {
            "valid": True,
            "track": track.model_dump(mode="json"),
        }
    )
    return 0


def _inspect_json(path: Path) -> BaseModel:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise SphericalValidationError("Inspected JSON must contain an object.")
    if "render_key" in payload:
        return ReframeRenderManifest.model_validate(payload)
    if "asset" in payload and "track" in payload:
        return ReframeRenderRequest.model_validate(payload)
    if "master_path" in payload and "source_hashes" in payload:
        return _load_asset(path)
    if "keyframes" in payload and "track_id" in payload:
        return VirtualCameraTrack.model_validate(payload)
    raise SphericalValidationError(
        "JSON does not match a supported spherical contract.",
        details={"path": str(path)},
    )


def _cmd_inspect(args: argparse.Namespace) -> int:
    path = Path(args.path).expanduser().resolve()
    if not path.is_file():
        raise SphericalValidationError(
            "Inspection target does not exist.",
            details={"path": str(path)},
        )
    if path.suffix.lower() == ".json":
        model = _inspect_json(path)
        _json_dump(
            {
                "path": str(path),
                "contract": model.__class__.__name__,
                "normalized": model.model_dump(mode="json"),
            }
        )
    else:
        probe = probe_media(path, args.ffprobe)
        _json_dump(
            {
                "media": probe.model_dump(mode="json"),
                "sha256": sha256_file(path),
                "approximately_equirectangular": abs(probe.width / probe.height - 2.0)
                <= 0.04,
            }
        )
    return 0


def _cmd_generate_fixture(args: argparse.Namespace) -> int:
    artifacts = generate_fixture(
        args.output_dir,
        width=args.width,
        height=args.height,
        fps=args.fps,
        duration_seconds=args.duration,
        output_width=args.output_width,
        output_height=args.output_height,
        ffmpeg_executable=args.ffmpeg,
        ffprobe_executable=args.ffprobe,
        overwrite=args.overwrite,
    )
    _json_dump(
        {
            "generated": True,
            "artifacts": {
                key: str(value)
                for key, value in artifacts.__dict__.items()
            },
        }
    )
    return 0


def _cmd_export_schemas(args: argparse.Namespace) -> int:
    paths = [
        *export_json_schemas(args.output_dir),
        *export_spherical_ui_schemas(args.output_dir),
    ]
    _json_dump({"schemas": [str(path.resolve()) for path in paths]})
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m open_storyline.spherical.cli",
        description="Deterministic FFmpeg-based spherical reframing tools.",
    )
    parser.add_argument(
        "--config",
        default=default_config_path(),
        help="OpenStoryline config used for default workspace/cache paths.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    reframe = subparsers.add_parser("reframe", help="Render a virtual-camera track.")
    reframe.add_argument("--asset", required=True)
    reframe.add_argument("--track", required=True)
    reframe.add_argument("--output", required=True)
    reframe.add_argument("--workspace")
    reframe.add_argument("--cache-dir")
    reframe.add_argument("--sample-interval", type=float)
    reframe.add_argument("--no-cache", action="store_true")
    reframe.add_argument("--overwrite", action="store_true")
    reframe.add_argument("--ffmpeg", default="ffmpeg")
    reframe.add_argument("--ffprobe", default="ffprobe")
    reframe.set_defaults(handler=_cmd_reframe)

    validate_track = subparsers.add_parser(
        "validate-track",
        help="Validate and print a normalized virtual-camera track.",
    )
    validate_track.add_argument("track")
    validate_track.set_defaults(handler=_cmd_validate_track)

    inspect = subparsers.add_parser("inspect", help="Inspect media or spherical JSON.")
    inspect.add_argument("path")
    inspect.add_argument("--ffprobe", default="ffprobe")
    inspect.set_defaults(handler=_cmd_inspect)

    fixture = subparsers.add_parser(
        "generate-fixture",
        help="Generate a labeled 2:1 fixture, audio, and sample tracks.",
    )
    fixture.add_argument("output_dir")
    fixture.add_argument("--width", type=int, default=1024)
    fixture.add_argument("--height", type=int, default=512)
    fixture.add_argument("--fps", type=float, default=30.0)
    fixture.add_argument("--duration", type=float, default=8.0)
    fixture.add_argument("--output-width", type=int, default=640)
    fixture.add_argument("--output-height", type=int, default=360)
    fixture.add_argument("--ffmpeg", default="ffmpeg")
    fixture.add_argument("--ffprobe", default="ffprobe")
    fixture.add_argument("--overwrite", action="store_true")
    fixture.set_defaults(handler=_cmd_generate_fixture)

    schemas = subparsers.add_parser(
        "export-schemas",
        help="Write all public spherical and web-UI JSON Schemas.",
    )
    schemas.add_argument("output_dir")
    schemas.set_defaults(handler=_cmd_export_schemas)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except SphericalError as exc:
        _json_dump({"error": exc.as_dict()}, stream=sys.stderr)
        return 2
    except (ValidationError, ValueError, json.JSONDecodeError, OSError) as exc:
        error = SphericalValidationError(
            "Spherical input validation failed.",
            details={"error": str(exc)},
        )
        _json_dump({"error": error.as_dict()}, stream=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
