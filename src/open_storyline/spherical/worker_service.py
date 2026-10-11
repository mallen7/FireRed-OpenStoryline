"""CLI for serving and diagnosing the distributed spherical worker."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import uvicorn

from open_storyline.spherical.diagnostics import collect_worker_diagnostics
from open_storyline.spherical.distributed_protocol import export_distributed_json_schemas
from open_storyline.spherical.worker_http import create_worker_app
from open_storyline.spherical.worker_logging import configure_structured_logging
from open_storyline.spherical.worker_security import DistributedWorkerSettings


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m open_storyline.spherical.worker_service"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve")
    serve.add_argument("--host", default=os.getenv("OPENSTORYLINE_WORKER_HOST", "0.0.0.0"))
    serve.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("OPENSTORYLINE_WORKER_PORT", "8090")),
    )
    diagnose = commands.add_parser("diagnose")
    diagnose.add_argument("--remote-url")
    diagnose.add_argument(
        "--remote-token-env", default="OPENSTORYLINE_WORKER_REMOTE_TOKEN"
    )
    schemas = commands.add_parser("export-schemas")
    schemas.add_argument("destination", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "export-schemas":
        for path in export_distributed_json_schemas(args.destination):
            print(path)
        return 0
    settings = DistributedWorkerSettings.from_environment()
    if args.command == "diagnose":
        token = os.getenv(args.remote_token_env) if args.remote_url else None
        report = collect_worker_diagnostics(
            settings,
            remote_url=args.remote_url,
            remote_token=token,
        )
        print(json.dumps(report.model_dump(mode="json"), indent=2))
        return 0 if report.compatible else 1
    configure_structured_logging()
    uvicorn.run(
        create_worker_app(settings),
        host=args.host,
        port=args.port,
        access_log=True,
        proxy_headers=False,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
