from __future__ import annotations

import json
import tempfile
import textwrap
import threading
import unittest
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError

from open_storyline.spherical.errors import SphericalWorkerError
from open_storyline.spherical.fixtures import generate_fixture
from open_storyline.spherical.manifests import sha256_file
from open_storyline.spherical.worker_client import LocalStitchWorkerClient
from open_storyline.spherical.worker_protocol import (
    WORKER_SCHEMA_MODELS,
    WorkerStitchRequest,
    export_worker_json_schemas,
)


def write_fake_worker(root: Path, master: Path, *, behavior: str = "success") -> Path:
    path = root / f"fake-worker-{behavior}"
    script = textwrap.dedent(
        """\
        #!/usr/bin/env python3
        import hashlib
        import json
        import shutil
        import signal
        import sys
        import time
        from pathlib import Path

        master = Path(__MASTER__)
        behavior = __BEHAVIOR__

        if sys.argv[1] == "capabilities":
            available = behavior != "unavailable"
            print(json.dumps({
                "schema_version": "1.0",
                "worker_version": "test-1.0",
                "provider": "test-fixture",
                "sdk_enabled": available,
                "sdk_version": "test" if available else None,
                "can_decode_insv": available,
                "can_stitch": available,
                "supports_progress": True,
                "supports_cancellation": True,
                "supported_codecs": ["h264", "h265"] if available else [],
                "supported_stitch_types": ["template", "optical_flow", "dynamic", "ai"] if available else [],
                "max_input_files": 2,
                "requires_gpu": False,
                "target_platform": "test",
                "model_root_configured": True,
                "warnings": [] if available else ["test provider unavailable"],
            }))
            raise SystemExit(0)

        request_path = Path(sys.argv[sys.argv.index("--request") + 1])
        request = json.loads(request_path.read_text(encoding="utf-8"))
        job_id = request["job_id"]
        def progress(value, message):
            print(json.dumps({
                "schema_version": "1.0",
                "event": "progress",
                "job_id": job_id,
                "progress_percent": value,
                "message": message,
                "created_at": "2026-07-24T00:00:00Z",
            }), flush=True)

        progress(0, "accepted")
        if behavior == "error":
            print(json.dumps({
                "schema_version": "1.0",
                "event": "error",
                "code": "sdk_open_file",
                "message": "test SDK could not open input",
                "details": {"sdk_error": "1"},
                "retryable": False,
                "job_id": job_id,
            }), flush=True)
            raise SystemExit(5)
        if behavior == "wait":
            time.sleep(30)
            raise SystemExit(6)

        output = Path(request["output_path"])
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(master, output)
        progress(100, "complete")
        def digest(file_path):
            return hashlib.sha256(Path(file_path).read_bytes()).hexdigest()
        print(json.dumps({
            "schema_version": "1.0",
            "event": "result",
            "job_id": job_id,
            "provider": "test-fixture",
            "input_paths": request["input_paths"],
            "source_sha256": [digest(value) for value in request["input_paths"]],
            "output_path": str(output),
            "output_sha256": digest(output),
            "output_width": request["output_width"],
            "output_height": request["output_height"],
            "codec": "h265" if behavior == "mismatch" else request["codec"],
            "bitrate_bps": request["bitrate_bps"],
            "enable_flowstate": request["enable_flowstate"],
            "enable_direction_lock": request["enable_direction_lock"],
            "stitch_type": request["stitch_type"],
            "enable_stitch_fusion": request["enable_stitch_fusion"],
            "elapsed_seconds": 0.01,
            "sdk_log_path": None,
            "warnings": ["test-only fixture worker"],
        }), flush=True)
        """
    )
    script = script.replace("__MASTER__", repr(str(master)))
    script = script.replace("__BEHAVIOR__", repr(behavior))
    path.write_text(script, encoding="utf-8")
    path.chmod(0o755)
    return path


class WorkerProtocolTests(unittest.TestCase):
    def test_stitch_contract_requires_explicit_unique_insv_and_two_to_one(self) -> None:
        valid = {
            "job_id": str(uuid4()),
            "input_paths": ["front.insv", "back.insv"],
            "output_path": "master.mp4",
            "output_width": 640,
            "output_height": 320,
            "stitch_type": "template",
        }
        request = WorkerStitchRequest.model_validate(valid)
        self.assertEqual(request.output_width, request.output_height * 2)
        for change in (
            {"output_height": 318},
            {"input_paths": ["front.insv", "front.insv"]},
            {"input_paths": ["ordinary.mp4"]},
        ):
            with self.subTest(change=change):
                with self.assertRaises(ValidationError):
                    WorkerStitchRequest.model_validate({**valid, **change})

    def test_exported_worker_schemas_match_checked_in_contracts(self) -> None:
        with tempfile.TemporaryDirectory(prefix="worker-schemas-") as name:
            generated = export_worker_json_schemas(name)
            self.assertEqual(len(generated), len(WORKER_SCHEMA_MODELS))
            checked_root = Path("services/insta360-worker/schemas")
            for path in generated:
                self.assertEqual(
                    json.loads(path.read_text(encoding="utf-8")),
                    json.loads((checked_root / path.name).read_text(encoding="utf-8")),
                )

    def test_client_streams_progress_and_imports_validated_master(self) -> None:
        with tempfile.TemporaryDirectory(prefix="worker-client-") as name:
            root = Path(name)
            fixture = generate_fixture(
                root / "fixture",
                width=640,
                height=320,
                fps=10.0,
                duration_seconds=1.0,
                output_width=320,
                output_height=180,
            )
            worker = write_fake_worker(root, fixture.video)
            raw = root / "camera.insv"
            raw.write_bytes(b"ordered raw input")
            output = root / "stitched-master.mp4"
            request = WorkerStitchRequest(
                job_id=uuid4(),
                input_paths=[raw],
                output_path=output,
                output_width=640,
                output_height=320,
                codec="h264",
                bitrate_bps=10_000_000,
                stitch_type="template",
                enable_stitch_fusion=False,
            )
            progress: list[int] = []
            client = LocalStitchWorkerClient(worker, timeout_seconds=10)

            result, asset = client.stitch_and_import(
                request,
                on_progress=lambda event: progress.append(event.progress_percent),
                camera_model="test-camera",
            )

            self.assertEqual(progress, [0, 100])
            self.assertEqual(result.output_sha256, sha256_file(output))
            self.assertEqual(asset.master_path, output.resolve())
            self.assertEqual(asset.source_hashes[0], result.output_sha256)
            self.assertEqual(asset.source_hashes[1], sha256_file(raw))
            self.assertEqual(asset.ingest.provider, "insta360-mediasdk")
            self.assertEqual(asset.ingest.stabilization, "flowstate")

    def test_client_preserves_progress_before_structured_worker_error(self) -> None:
        with tempfile.TemporaryDirectory(prefix="worker-error-") as name:
            root = Path(name)
            fixture = generate_fixture(
                root / "fixture",
                width=640,
                height=320,
                fps=10.0,
                duration_seconds=1.0,
                output_width=320,
                output_height=180,
            )
            worker = write_fake_worker(root, fixture.video, behavior="error")
            raw = root / "camera.insv"
            raw.write_bytes(b"raw")
            request = WorkerStitchRequest(
                job_id=uuid4(),
                input_paths=[raw],
                output_path=root / "master.mp4",
                output_width=640,
                output_height=320,
                stitch_type="template",
            )
            progress: list[int] = []
            with self.assertRaises(SphericalWorkerError) as captured:
                LocalStitchWorkerClient(worker, timeout_seconds=10).stitch(
                    request,
                    on_progress=lambda event: progress.append(event.progress_percent),
                )
            self.assertEqual(progress, [0])
            self.assertEqual(captured.exception.details["worker_code"], "sdk_open_file")
            self.assertFalse(request.output_path.exists())

    def test_client_checks_capability_before_starting_stitch(self) -> None:
        with tempfile.TemporaryDirectory(prefix="worker-unavailable-") as name:
            root = Path(name)
            master = root / "unused.mp4"
            master.write_bytes(b"unused")
            worker = write_fake_worker(root, master, behavior="unavailable")
            raw = root / "camera.insv"
            raw.write_bytes(b"raw")
            output = root / "master.mp4"
            request = WorkerStitchRequest(
                job_id=uuid4(),
                input_paths=[raw],
                output_path=output,
                output_width=640,
                output_height=320,
                stitch_type="template",
            )
            with self.assertRaisesRegex(SphericalWorkerError, "capability") as captured:
                LocalStitchWorkerClient(worker, timeout_seconds=10).stitch(request)
            self.assertFalse(captured.exception.details["sdk_enabled"])
            self.assertIn("raw .insv decoding", captured.exception.details["unavailable"])
            self.assertFalse(output.exists())

    def test_client_rejects_result_settings_that_differ_from_request(self) -> None:
        with tempfile.TemporaryDirectory(prefix="worker-mismatch-") as name:
            root = Path(name)
            fixture = generate_fixture(
                root / "fixture",
                width=640,
                height=320,
                fps=10.0,
                duration_seconds=1.0,
                output_width=320,
                output_height=180,
            )
            worker = write_fake_worker(root, fixture.video, behavior="mismatch")
            raw = root / "camera.insv"
            raw.write_bytes(b"raw")
            request = WorkerStitchRequest(
                job_id=uuid4(),
                input_paths=[raw],
                output_path=root / "master.mp4",
                output_width=640,
                output_height=320,
                codec="h264",
                stitch_type="template",
            )
            with self.assertRaisesRegex(SphericalWorkerError, "settings"):
                LocalStitchWorkerClient(worker, timeout_seconds=10).stitch(request)

    def test_client_cancellation_terminates_worker(self) -> None:
        with tempfile.TemporaryDirectory(prefix="worker-cancel-") as name:
            root = Path(name)
            master = root / "unused.mp4"
            master.write_bytes(b"unused")
            worker = write_fake_worker(root, master, behavior="wait")
            raw = root / "camera.insv"
            raw.write_bytes(b"raw")
            request = WorkerStitchRequest(
                job_id=uuid4(),
                input_paths=[raw],
                output_path=root / "master.mp4",
                output_width=640,
                output_height=320,
                stitch_type="template",
            )
            cancel = threading.Event()
            cancel.set()
            with self.assertRaisesRegex(SphericalWorkerError, "cancelled"):
                LocalStitchWorkerClient(
                    worker,
                    timeout_seconds=10,
                    cancellation_grace_seconds=1,
                ).stitch(request, cancel_event=cancel)


if __name__ == "__main__":
    unittest.main()
