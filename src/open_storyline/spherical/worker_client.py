"""Local subprocess client for the isolated Insta360 MediaSDK worker."""

from __future__ import annotations

import json
import os
import queue
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from open_storyline.spherical.errors import SphericalWorkerError
from open_storyline.spherical.ffmpeg import probe_media
from open_storyline.spherical.manifests import atomic_write_json, sha256_file
from open_storyline.spherical.schemas import IngestMetadata, SphericalAsset
from open_storyline.spherical.worker_protocol import (
    WorkerCapabilityResponse,
    WorkerDiscoveryResponse,
    WorkerErrorResponse,
    WorkerProbeRequest,
    WorkerProbeResponse,
    WorkerStitchProgressEvent,
    WorkerStitchRequest,
    WorkerStitchResult,
)


ProtocolResponse = TypeVar("ProtocolResponse", bound=BaseModel)
ProgressCallback = Callable[[WorkerStitchProgressEvent], None]


class StitchWorkerClient(Protocol):
    """Transport-neutral boundary reserved for a later HTTP implementation."""

    def capabilities(self) -> WorkerCapabilityResponse: ...

    def discover(self, directory: str | Path) -> WorkerDiscoveryResponse: ...

    def probe(self, request: WorkerProbeRequest) -> WorkerProbeResponse: ...

    def stitch(
        self,
        request: WorkerStitchRequest,
        *,
        on_progress: ProgressCallback | None = None,
        cancel_event: threading.Event | None = None,
    ) -> WorkerStitchResult: ...


def create_stitch_worker_client(config) -> StitchWorkerClient:
    """Build the configured local or authenticated HTTP transport."""

    if config.worker_transport == "local":
        return LocalStitchWorkerClient(
            config.worker_executable,
            timeout_seconds=config.worker_timeout_seconds,
        )
    token = os.getenv(config.worker_token_env, "")
    if not token:
        raise SphericalWorkerError(
            "Distributed worker bearer token is not configured.",
            details={"token_environment_variable": config.worker_token_env},
        )
    from open_storyline.spherical.remote_worker_client import HttpSphericalWorkerClient

    return HttpSphericalWorkerClient(
        config.worker_url,
        token,
        timeout_seconds=config.worker_timeout_seconds,
        verify_tls=config.worker_verify_tls,
    )


class LocalStitchWorkerClient:
    """Invoke the worker as a process without loading proprietary code in Python."""

    def __init__(
        self,
        executable: str | Path,
        *,
        timeout_seconds: float = 86_400.0,
        cancellation_grace_seconds: float = 5.0,
    ) -> None:
        self.executable = Path(executable).expanduser().resolve(strict=False)
        self.timeout_seconds = timeout_seconds
        self.cancellation_grace_seconds = cancellation_grace_seconds

    def _command(self, *arguments: str) -> list[str]:
        if not self.executable.is_file():
            raise SphericalWorkerError(
                "Insta360 worker executable was not found.",
                details={"executable": str(self.executable)},
            )
        return [str(self.executable), *arguments]

    @staticmethod
    def _decode_payload(text: str, model: type[ProtocolResponse]) -> ProtocolResponse:
        try:
            payload = json.loads(text)
            if isinstance(payload, dict) and payload.get("event") == "error":
                error = WorkerErrorResponse.model_validate(payload)
                raise SphericalWorkerError(
                    error.message,
                    details={
                        "worker_code": error.code,
                        "retryable": error.retryable,
                        **error.details,
                    },
                )
            return model.model_validate(payload)
        except SphericalWorkerError:
            raise
        except (json.JSONDecodeError, ValidationError) as exc:
            raise SphericalWorkerError(
                "Worker returned an invalid protocol response.",
                details={"response": text[-8000:], "error": str(exc)},
            ) from exc

    def _run_single(
        self,
        arguments: list[str],
        model: type[ProtocolResponse],
    ) -> ProtocolResponse:
        command = self._command(*arguments)
        try:
            completed = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                check=False,
                timeout=min(self.timeout_seconds, 300.0),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SphericalWorkerError(
                "Could not execute the Insta360 worker.",
                details={"command": command, "error": str(exc)},
            ) from exc
        stdout = completed.stdout.strip()
        if not stdout:
            raise SphericalWorkerError(
                "Worker returned no JSON response.",
                details={
                    "exit_code": completed.returncode,
                    "stderr": completed.stderr[-8000:],
                },
            )
        response = self._decode_payload(stdout.splitlines()[-1], model)
        if completed.returncode != 0:
            raise SphericalWorkerError(
                "Worker exited unsuccessfully after returning a response.",
                details={
                    "exit_code": completed.returncode,
                    "stderr": completed.stderr[-8000:],
                },
            )
        return response

    def capabilities(self) -> WorkerCapabilityResponse:
        return self._run_single(
            ["capabilities"],
            WorkerCapabilityResponse,
        )

    def discover(self, directory: str | Path) -> WorkerDiscoveryResponse:
        return self._run_single(
            ["discover", "--directory", str(Path(directory))],
            WorkerDiscoveryResponse,
        )

    def probe(self, request: WorkerProbeRequest) -> WorkerProbeResponse:
        arguments = ["probe"]
        for path in request.input_paths:
            arguments.extend(["--input", str(path)])
        return self._run_single(arguments, WorkerProbeResponse)

    def _require_stitch_capability(
        self,
        request: WorkerStitchRequest,
    ) -> WorkerCapabilityResponse:
        capabilities = self.capabilities()
        unavailable: list[str] = []
        if not capabilities.can_decode_insv:
            unavailable.append("raw .insv decoding")
        if not capabilities.can_stitch:
            unavailable.append("stitching")
        if request.codec not in capabilities.supported_codecs:
            unavailable.append(f"codec {request.codec}")
        if request.stitch_type not in capabilities.supported_stitch_types:
            unavailable.append(f"stitch type {request.stitch_type}")
        if len(request.input_paths) > capabilities.max_input_files:
            unavailable.append(
                f"{len(request.input_paths)} inputs (maximum is "
                f"{capabilities.max_input_files})"
            )
        if unavailable:
            raise SphericalWorkerError(
                "Insta360 worker does not have the requested stitch capability.",
                details={
                    "provider": capabilities.provider,
                    "sdk_enabled": capabilities.sdk_enabled,
                    "unavailable": unavailable,
                    "warnings": capabilities.warnings,
                },
            )
        return capabilities

    @staticmethod
    def _read_lines(stream, output: queue.Queue[str | None]) -> None:
        try:
            for line in iter(stream.readline, ""):
                output.put(line)
        finally:
            output.put(None)
            stream.close()

    @staticmethod
    def _read_stderr(stream, chunks: list[str]) -> None:
        try:
            for chunk in iter(lambda: stream.read(4096), ""):
                chunks.append(chunk)
                if sum(len(item) for item in chunks) > 64_000:
                    chunks.pop(0)
        finally:
            stream.close()

    @staticmethod
    def _terminate_process(
        process: subprocess.Popen[str],
        grace_seconds: float,
    ) -> int:
        if process.poll() is not None:
            return process.returncode
        process.terminate()
        try:
            return process.wait(timeout=grace_seconds)
        except subprocess.TimeoutExpired:
            process.kill()
            return process.wait(timeout=2.0)

    def stitch(
        self,
        request: WorkerStitchRequest,
        *,
        on_progress: ProgressCallback | None = None,
        cancel_event: threading.Event | None = None,
    ) -> WorkerStitchResult:
        capabilities = self._require_stitch_capability(request)
        inputs = [path.expanduser().resolve() for path in request.input_paths]
        for path in inputs:
            if not path.is_file():
                raise SphericalWorkerError(
                    "Worker input does not exist.",
                    details={"path": str(path)},
                )
        output = request.output_path.expanduser().resolve(strict=False)
        normalized = request.model_copy(
            update={"input_paths": inputs, "output_path": output}
        )

        with tempfile.TemporaryDirectory(prefix="openstoryline-worker-") as name:
            request_path = Path(name) / "stitch-request.json"
            atomic_write_json(request_path, normalized.model_dump(mode="json"))
            command = self._command("stitch", "--request", str(request_path))
            try:
                process = subprocess.Popen(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    bufsize=1,
                )
            except OSError as exc:
                raise SphericalWorkerError(
                    "Could not start the Insta360 worker.",
                    details={"command": command, "error": str(exc)},
                ) from exc
            assert process.stdout is not None
            assert process.stderr is not None
            lines: queue.Queue[str | None] = queue.Queue()
            stderr_chunks: list[str] = []
            stdout_thread = threading.Thread(
                target=self._read_lines,
                args=(process.stdout, lines),
                daemon=True,
            )
            stderr_thread = threading.Thread(
                target=self._read_stderr,
                args=(process.stderr, stderr_chunks),
                daemon=True,
            )
            stdout_thread.start()
            stderr_thread.start()

            started = time.monotonic()
            result: WorkerStitchResult | None = None
            worker_error: WorkerErrorResponse | None = None
            stdout_closed = False
            cancellation_sent = False
            cancellation_started: float | None = None
            while not stdout_closed or process.poll() is None:
                if time.monotonic() - started > self.timeout_seconds:
                    self._terminate_process(process, self.cancellation_grace_seconds)
                    raise SphericalWorkerError(
                        "Insta360 worker timed out.",
                        details={"timeout_seconds": self.timeout_seconds},
                    )
                if cancel_event is not None and cancel_event.is_set() and not cancellation_sent:
                    process.terminate()
                    cancellation_sent = True
                    cancellation_started = time.monotonic()
                if (
                    cancellation_started is not None
                    and process.poll() is None
                    and time.monotonic() - cancellation_started
                    > self.cancellation_grace_seconds
                ):
                    process.kill()
                try:
                    line = lines.get(timeout=0.1)
                except queue.Empty:
                    continue
                if line is None:
                    stdout_closed = True
                    continue
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    payload = json.loads(stripped)
                    event = payload.get("event") if isinstance(payload, dict) else None
                    if event == "progress":
                        progress = WorkerStitchProgressEvent.model_validate(payload)
                        if progress.job_id != normalized.job_id:
                            raise ValueError("progress job_id does not match request")
                        if on_progress is not None:
                            on_progress(progress)
                    elif event == "result":
                        result = WorkerStitchResult.model_validate(payload)
                    elif event == "error":
                        worker_error = WorkerErrorResponse.model_validate(payload)
                    else:
                        raise ValueError("unknown worker event")
                except (json.JSONDecodeError, ValidationError, ValueError) as exc:
                    self._terminate_process(process, self.cancellation_grace_seconds)
                    raise SphericalWorkerError(
                        "Worker emitted an invalid JSON Lines event.",
                        details={"line": stripped[-8000:], "error": str(exc)},
                    ) from exc

            exit_code = self._terminate_process(
                process,
                self.cancellation_grace_seconds if cancellation_sent else 1.0,
            )
            stdout_thread.join(timeout=1.0)
            stderr_thread.join(timeout=1.0)
            stderr = "".join(stderr_chunks)[-16000:]

        if cancellation_sent:
            raise SphericalWorkerError(
                "Insta360 stitch job was cancelled.",
                details={"worker_code": "cancelled", "exit_code": exit_code},
            )
        if worker_error is not None:
            raise SphericalWorkerError(
                worker_error.message,
                details={
                    "worker_code": worker_error.code,
                    "retryable": worker_error.retryable,
                    "exit_code": exit_code,
                    **worker_error.details,
                },
            )
        if exit_code != 0 or result is None:
            raise SphericalWorkerError(
                "Worker did not produce a successful stitch result.",
                details={"exit_code": exit_code, "stderr": stderr},
            )
        self._validate_result(normalized, result, capabilities)
        return result

    @staticmethod
    def _validate_result(
        request: WorkerStitchRequest,
        result: WorkerStitchResult,
        capabilities: WorkerCapabilityResponse,
    ) -> None:
        output = request.output_path.resolve(strict=False)
        if result.job_id != request.job_id:
            raise SphericalWorkerError("Worker result job_id does not match request.")
        if result.provider != capabilities.provider:
            raise SphericalWorkerError(
                "Worker result provider does not match its capability response."
            )
        expected_inputs = [path.resolve() for path in request.input_paths]
        actual_inputs = [path.resolve() for path in result.input_paths]
        if actual_inputs != expected_inputs:
            raise SphericalWorkerError("Worker result input paths do not match request.")
        if result.output_path.resolve(strict=False) != output:
            raise SphericalWorkerError("Worker result output path does not match request.")
        if (result.output_width, result.output_height) != (
            request.output_width,
            request.output_height,
        ):
            raise SphericalWorkerError("Worker result dimensions do not match request.")
        expected_settings = (
            request.codec,
            request.bitrate_bps,
            request.enable_flowstate,
            request.enable_direction_lock,
            request.stitch_type,
            request.enable_stitch_fusion,
        )
        actual_settings = (
            result.codec,
            result.bitrate_bps,
            result.enable_flowstate,
            result.enable_direction_lock,
            result.stitch_type,
            result.enable_stitch_fusion,
        )
        if actual_settings != expected_settings:
            raise SphericalWorkerError("Worker result settings do not match request.")
        if not output.is_file():
            raise SphericalWorkerError("Worker reported success without an output file.")
        if sha256_file(output) != result.output_sha256:
            raise SphericalWorkerError("Worker output hash validation failed.")
        source_hashes = [sha256_file(path) for path in request.input_paths]
        if source_hashes != result.source_sha256:
            raise SphericalWorkerError("Worker source hash validation failed.")

    def stitch_and_import(
        self,
        request: WorkerStitchRequest,
        *,
        on_progress: ProgressCallback | None = None,
        cancel_event: threading.Event | None = None,
        camera_model: str | None = None,
    ) -> tuple[WorkerStitchResult, SphericalAsset]:
        """Stitch raw inputs and validate/import the resulting 2:1 master."""

        result = self.stitch(
            request,
            on_progress=on_progress,
            cancel_event=cancel_event,
        )
        probe = probe_media(result.output_path)
        if probe.width != probe.height * 2:
            raise SphericalWorkerError(
                "Worker output is not a 2:1 equirectangular master.",
                details={"width": probe.width, "height": probe.height},
            )
        source_paths = [result.output_path, *result.input_paths]
        source_hashes = [result.output_sha256, *result.source_sha256]
        asset = SphericalAsset(
            asset_id=f"spherical-{result.output_sha256[:16]}",
            source_paths=source_paths,
            source_hashes=source_hashes,
            camera_model=camera_model,
            master_path=result.output_path,
            width=probe.width,
            height=probe.height,
            fps=probe.fps,
            duration_seconds=probe.duration_seconds,
            audio_stream_index=0 if probe.audio_stream_count else None,
            metadata={
                "raw_source_paths": [str(path) for path in result.input_paths],
                "raw_source_sha256": result.source_sha256,
                "worker_result": result.model_dump(mode="json"),
            },
            ingest=IngestMetadata(
                provider="insta360-mediasdk",
                stabilization=("flowstate" if result.enable_flowstate else "disabled"),
                stitch_type=result.stitch_type,
            ),
        )
        return result, asset
