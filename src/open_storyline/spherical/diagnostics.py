"""Compatibility diagnostics for local and remote distributed-worker hosts."""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import httpx

from open_storyline.spherical.distributed_protocol import (
    DISTRIBUTED_API_VERSION,
    WorkerDiagnosticCheck,
    WorkerDiagnosticReport,
)
from open_storyline.spherical.ffmpeg import probe_ffmpeg_capabilities
from open_storyline.spherical.worker_client import LocalStitchWorkerClient
from open_storyline.spherical.worker_security import DistributedWorkerSettings, sanitize_text


def _run(command: list[str], *, timeout: float = 10.0) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def gpu_snapshot() -> dict[str, Any]:
    executable = shutil.which("nvidia-smi")
    if executable is None:
        return {
            "available": False,
            "memory_used_bytes": None,
            "driver_version": None,
            "cuda_version": None,
        }
    completed = _run(
        [
            executable,
            "--query-gpu=memory.used,driver_version",
            "--format=csv,noheader,nounits",
        ]
    )
    if completed is None or completed.returncode != 0:
        return {
            "available": False,
            "memory_used_bytes": None,
            "driver_version": None,
            "cuda_version": None,
        }
    first = completed.stdout.strip().splitlines()[0].split(",")
    memory_mib = int(first[0].strip()) if first and first[0].strip().isdigit() else None
    driver = first[1].strip() if len(first) > 1 else None
    version_probe = _run([executable])
    cuda_version = None
    if version_probe is not None:
        marker = "CUDA Version:"
        if marker in version_probe.stdout:
            cuda_version = version_probe.stdout.split(marker, 1)[1].split()[0]
    return {
        "available": True,
        "memory_used_bytes": memory_mib * 1024 * 1024 if memory_mib is not None else None,
        "driver_version": driver,
        "cuda_version": cuda_version,
    }


def _writable_check(path: Path, name: str) -> WorkerDiagnosticCheck:
    try:
        path.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=".diagnostic-", dir=path)
        os.close(descriptor)
        Path(temporary_name).unlink()
        return WorkerDiagnosticCheck(
            name=name,
            status="pass",
            required=True,
            message="directory is writable",
        )
    except OSError as exc:
        return WorkerDiagnosticCheck(
            name=name,
            status="fail",
            required=True,
            message="directory is not writable",
            details={"error": sanitize_text(str(exc))},
        )


def collect_worker_diagnostics(
    settings: DistributedWorkerSettings,
    *,
    remote_url: str | None = None,
    remote_token: str | None = None,
) -> WorkerDiagnosticReport:
    checks: list[WorkerDiagnosticCheck] = []
    system = platform.system()
    checks.append(
        WorkerDiagnosticCheck(
            name="operating_system",
            status="pass" if system == "Linux" else "warn",
            required=False,
            message=(
                "Linux host detected"
                if system == "Linux"
                else f"{system} supports public diagnostics/reframing; MediaSDK production targets Ubuntu 22.04"
            ),
            details={"system": system, "release": platform.release()},
        )
    )
    architecture = platform.machine().lower()
    checks.append(
        WorkerDiagnosticCheck(
            name="cpu_architecture",
            status="pass" if architecture in {"x86_64", "amd64"} else "warn",
            required=False,
            message=f"CPU architecture is {architecture}",
        )
    )
    try:
        capabilities = probe_ffmpeg_capabilities()
        missing = [
            name
            for name, available in {
                "v360": capabilities.v360_available,
                "libx264": capabilities.libx264_available,
                "concat": capabilities.concat_demuxer_available,
            }.items()
            if not available
        ]
        checks.append(
            WorkerDiagnosticCheck(
                name="ffmpeg",
                status="fail" if missing else "pass",
                required=True,
                message=(
                    f"missing required FFmpeg capabilities: {', '.join(missing)}"
                    if missing
                    else capabilities.version_line
                ),
                details={"missing": missing},
            )
        )
    except Exception as exc:
        checks.append(
            WorkerDiagnosticCheck(
                name="ffmpeg",
                status="fail",
                required=True,
                message="FFmpeg capability probe failed",
                details={"error": sanitize_text(str(exc))},
            )
        )
    gpu = gpu_snapshot()
    checks.append(
        WorkerDiagnosticCheck(
            name="nvidia_gpu",
            status="pass" if gpu["available"] else "warn",
            required=settings.require_stitch_ready,
            message="NVIDIA GPU is visible" if gpu["available"] else "NVIDIA GPU is not visible",
            details=gpu,
        )
    )
    checks.append(
        WorkerDiagnosticCheck(
            name="vulkan",
            status="pass" if shutil.which("vulkaninfo") else "warn",
            required=False,
            message=(
                "vulkaninfo is available"
                if shutil.which("vulkaninfo")
                else "vulkaninfo is not installed; Vulkan validation was skipped"
            ),
        )
    )
    if settings.worker_executable.is_file():
        try:
            stitch = LocalStitchWorkerClient(
                settings.worker_executable, timeout_seconds=30.0
            ).capabilities()
            stitch_ready = stitch.can_decode_insv and stitch.can_stitch
            checks.append(
                WorkerDiagnosticCheck(
                    name="insta360_sdk",
                    status="pass" if stitch_ready else ("fail" if settings.require_stitch_ready else "warn"),
                    required=settings.require_stitch_ready,
                    message=(
                        "MediaSDK stitch capability is available"
                        if stitch_ready
                        else "worker is present but proprietary stitch capability is unavailable"
                    ),
                    details={
                        "provider": stitch.provider,
                        "sdk_enabled": stitch.sdk_enabled,
                        "sdk_version": stitch.sdk_version,
                        "can_stitch": stitch.can_stitch,
                    },
                )
            )
        except Exception as exc:
            checks.append(
                WorkerDiagnosticCheck(
                    name="insta360_sdk",
                    status="fail" if settings.require_stitch_ready else "warn",
                    required=settings.require_stitch_ready,
                    message="worker capability probe failed",
                    details={"error": sanitize_text(str(exc))},
                )
            )
    else:
        checks.append(
            WorkerDiagnosticCheck(
                name="insta360_sdk",
                status="fail" if settings.require_stitch_ready else "warn",
                required=settings.require_stitch_ready,
                message="standalone Insta360 worker executable is absent",
            )
        )
    if settings.model_root is None:
        model_status = "fail" if settings.require_stitch_ready else "warn"
        model_message = "model directory is not configured"
    elif settings.model_root.is_dir():
        model_status = "pass"
        model_message = "model directory is present"
    else:
        model_status = "fail" if settings.require_stitch_ready else "warn"
        model_message = "configured model directory is missing"
    checks.append(
        WorkerDiagnosticCheck(
            name="model_directory",
            status=model_status,
            required=settings.require_stitch_ready,
            message=model_message,
        )
    )
    checks.append(_writable_check(settings.state_dir, "state_directory"))
    checks.extend(
        _writable_check(path, f"output_directory_{index}")
        for index, path in enumerate(settings.output_roots)
    )
    if remote_url is not None:
        headers = {"Authorization": f"Bearer {remote_token}"} if remote_token else {}
        try:
            with httpx.Client(timeout=10.0, headers=headers) as client:
                live = client.get(f"{remote_url.rstrip('/')}/health/live")
                capabilities_response = client.get(
                    f"{remote_url.rstrip('/')}/v1/capabilities"
                )
            remote_payload = capabilities_response.json()
            compatible = remote_payload.get("api_version") == DISTRIBUTED_API_VERSION
            checks.append(
                WorkerDiagnosticCheck(
                    name="network_reachability",
                    status="pass" if live.status_code == 200 else "fail",
                    required=True,
                    message=f"remote liveness returned HTTP {live.status_code}",
                )
            )
            checks.append(
                WorkerDiagnosticCheck(
                    name="api_version_compatibility",
                    status="pass" if compatible else "fail",
                    required=True,
                    message=(
                        f"remote API {remote_payload.get('api_version')} matches client"
                        if compatible
                        else "remote worker API version is incompatible"
                    ),
                )
            )
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            checks.append(
                WorkerDiagnosticCheck(
                    name="network_reachability",
                    status="fail",
                    required=True,
                    message="remote worker could not be reached",
                    details={"error": sanitize_text(str(exc))},
                )
            )
    compatible = not any(check.required and check.status == "fail" for check in checks)
    return WorkerDiagnosticReport(
        worker_version=settings.worker_version,
        compatible=compatible,
        checks=checks,
    )
