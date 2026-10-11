"""Security configuration and canonical path policy for the HTTP worker."""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from open_storyline.spherical.distributed_protocol import (
    AnalyzeJobSubmission,
    ReframeJobSubmission,
    StitchJobSubmission,
)
from open_storyline.spherical.schemas import ReframeRenderRequest, SphericalAsset


SECRET_KEYS = {
    "authorization",
    "bearer",
    "bearer_token",
    "token",
    "api_key",
    "apikey",
    "secret",
    "password",
}
BEARER_PATTERN = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+=*")
ASSIGNMENT_PATTERN = re.compile(
    r"(?i)\b(token|api[_-]?key|secret|password)\s*[:=]\s*([^\s,;]+)"
)


class WorkerSecurityError(ValueError):
    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


def sanitize_text(value: str, *, maximum_length: int = 16_000) -> str:
    redacted = BEARER_PATTERN.sub("Bearer [REDACTED]", value)
    redacted = ASSIGNMENT_PATTERN.sub(lambda match: f"{match.group(1)}=[REDACTED]", redacted)
    return redacted[-maximum_length:]


def sanitize_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): (
                "[REDACTED]"
                if str(key).lower() in SECRET_KEYS
                else sanitize_value(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [sanitize_value(item) for item in value]
    if isinstance(value, tuple):
        return [sanitize_value(item) for item in value]
    if isinstance(value, str):
        return sanitize_text(value)
    return value


def sanitize_sdk_log(path: str | Path, *, maximum_bytes: int = 16 * 1024 * 1024) -> Path:
    """Redact common secret forms before an SDK log becomes a downloadable artifact."""

    target = Path(path).expanduser().resolve(strict=True)
    raw = target.read_bytes()
    if len(raw) > maximum_bytes:
        raw = raw[-maximum_bytes:]
    text = raw.decode("utf-8", errors="replace")
    sanitized = sanitize_text(text, maximum_length=maximum_bytes)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".sanitized", dir=target.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(sanitized)
            if sanitized and not sanitized.endswith("\n"):
                handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return target


class DistributedWorkerSettings(BaseModel):
    """Runtime-only service settings; secrets are loaded from the environment."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    state_dir: Path
    media_roots: list[Path] = Field(min_length=1)
    output_roots: list[Path] = Field(min_length=1)
    upload_root: Path | None = None
    bearer_token: SecretStr
    worker_executable: Path
    model_root: Path | None = None
    worker_version: str = "openstoryline-distributed-1.0.0"
    max_request_bytes: int = Field(default=2 * 1024 * 1024, ge=1024, le=64 * 1024 * 1024)
    max_upload_bytes: int = Field(default=64 * 1024 * 1024 * 1024, ge=1024)
    max_concurrent_jobs: int = Field(default=1, ge=1, le=32)
    max_queued_jobs: int = Field(default=100, ge=1, le=100_000)
    default_timeout_seconds: float = Field(default=86_400.0, gt=0.0, le=604_800.0)
    job_retention_days: int = Field(default=30, ge=1, le=3650)
    max_job_history: int = Field(default=10_000, ge=100, le=1_000_000)
    event_poll_seconds: float = Field(default=0.1, ge=0.02, le=5.0)
    shutdown_grace_seconds: float = Field(default=10.0, ge=0.5, le=120.0)
    require_stitch_ready: bool = False

    @model_validator(mode="after")
    def normalize_paths(self) -> "DistributedWorkerSettings":
        object.__setattr__(self, "state_dir", self.state_dir.expanduser().resolve(strict=False))
        object.__setattr__(
            self,
            "media_roots",
            list(dict.fromkeys(path.expanduser().resolve(strict=False) for path in self.media_roots)),
        )
        object.__setattr__(
            self,
            "output_roots",
            list(dict.fromkeys(path.expanduser().resolve(strict=False) for path in self.output_roots)),
        )
        if self.upload_root is not None:
            upload = self.upload_root.expanduser().resolve(strict=False)
            if not any(_is_within(upload, root) for root in self.media_roots):
                raise ValueError("upload_root must be inside an allowed media root")
            object.__setattr__(self, "upload_root", upload)
        object.__setattr__(
            self,
            "worker_executable",
            self.worker_executable.expanduser().resolve(strict=False),
        )
        if self.model_root is not None:
            object.__setattr__(
                self,
                "model_root",
                self.model_root.expanduser().resolve(strict=False),
            )
        if len(self.bearer_token.get_secret_value().strip()) < 16:
            raise ValueError("bearer_token must contain at least 16 characters")
        return self

    @classmethod
    def from_environment(cls) -> "DistributedWorkerSettings":
        def required(name: str) -> str:
            value = os.getenv(name, "").strip()
            if not value:
                raise RuntimeError(f"{name} is required")
            return value

        def paths(name: str) -> list[Path]:
            return [Path(value) for value in required(name).split(os.pathsep) if value]

        upload_value = os.getenv("OPENSTORYLINE_WORKER_UPLOAD_ROOT", "").strip()
        model_value = os.getenv("INSTA360_MODEL_ROOT", "").strip()
        token = os.getenv("OPENSTORYLINE_WORKER_TOKEN", "").strip()
        token_file = os.getenv("OPENSTORYLINE_WORKER_TOKEN_FILE", "").strip()
        if not token and token_file:
            try:
                token = Path(token_file).read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise RuntimeError(
                    "OPENSTORYLINE_WORKER_TOKEN_FILE could not be read"
                ) from exc
        if not token:
            raise RuntimeError(
                "OPENSTORYLINE_WORKER_TOKEN or OPENSTORYLINE_WORKER_TOKEN_FILE is required"
            )
        return cls(
            state_dir=Path(required("OPENSTORYLINE_WORKER_STATE_DIR")),
            media_roots=paths("OPENSTORYLINE_WORKER_MEDIA_ROOTS"),
            output_roots=paths("OPENSTORYLINE_WORKER_OUTPUT_ROOTS"),
            upload_root=Path(upload_value) if upload_value else None,
            bearer_token=token,
            worker_executable=Path(required("OPENSTORYLINE_WORKER_EXECUTABLE")),
            model_root=Path(model_value) if model_value else None,
            max_request_bytes=int(os.getenv("OPENSTORYLINE_WORKER_MAX_REQUEST_BYTES", 2 * 1024 * 1024)),
            max_upload_bytes=int(os.getenv("OPENSTORYLINE_WORKER_MAX_UPLOAD_BYTES", 64 * 1024 * 1024 * 1024)),
            max_concurrent_jobs=int(os.getenv("OPENSTORYLINE_WORKER_CONCURRENCY", "1")),
            max_queued_jobs=int(os.getenv("OPENSTORYLINE_WORKER_MAX_QUEUED", "100")),
            default_timeout_seconds=float(os.getenv("OPENSTORYLINE_WORKER_TIMEOUT_SECONDS", "86400")),
            job_retention_days=int(os.getenv("OPENSTORYLINE_WORKER_RETENTION_DAYS", "30")),
            require_stitch_ready=os.getenv("OPENSTORYLINE_WORKER_REQUIRE_STITCH", "0") == "1",
        )


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


class WorkerPathPolicy:
    def __init__(self, settings: DistributedWorkerSettings) -> None:
        self.settings = settings
        self.read_roots = list(
            dict.fromkeys([*settings.media_roots, *settings.output_roots])
        )

    @staticmethod
    def _canonical(path: str | Path, *, must_exist: bool) -> Path:
        candidate = Path(path).expanduser()
        try:
            return candidate.resolve(strict=must_exist)
        except (OSError, RuntimeError) as exc:
            raise WorkerSecurityError(
                "invalid_path",
                "Path could not be canonicalized.",
                details={"path": str(candidate)},
            ) from exc

    @staticmethod
    def _require_inside(path: Path, roots: list[Path], *, role: str) -> Path:
        if not any(_is_within(path, root) for root in roots):
            raise WorkerSecurityError(
                "path_not_allowed",
                f"{role} is outside configured worker roots.",
                details={"role": role, "path": str(path)},
            )
        return path

    def input_file(self, path: str | Path, *, role: str = "input path") -> Path:
        canonical = self._canonical(path, must_exist=True)
        self._require_inside(canonical, self.read_roots, role=role)
        if not canonical.is_file():
            raise WorkerSecurityError(
                "invalid_input",
                f"{role} must be a regular file.",
                details={"path": str(canonical)},
            )
        return canonical

    def output_path(self, path: str | Path, *, role: str = "output path") -> Path:
        canonical = self._canonical(path, must_exist=False)
        self._require_inside(canonical, self.settings.output_roots, role=role)
        if canonical.exists() and not canonical.is_file():
            raise WorkerSecurityError(
                "invalid_output",
                f"{role} must be a file path.",
                details={"path": str(canonical)},
            )
        return canonical

    def output_directory(self, path: str | Path, *, role: str) -> Path:
        canonical = self._canonical(path, must_exist=False)
        self._require_inside(canonical, self.settings.output_roots, role=role)
        if canonical.exists() and not canonical.is_dir():
            raise WorkerSecurityError(
                "invalid_output",
                f"{role} must be a directory.",
                details={"path": str(canonical)},
            )
        return canonical

    def normalize_stitch(self, submission: StitchJobSubmission) -> StitchJobSubmission:
        request = submission.request
        if request.model_root is not None:
            raise WorkerSecurityError(
                "forbidden_setting",
                "API clients may not select an SDK model directory.",
            )
        normalized = request.model_copy(
            update={
                "input_paths": [self.input_file(path) for path in request.input_paths],
                "output_path": self.output_path(request.output_path),
                "model_root": self.settings.model_root,
            }
        )
        return submission.model_copy(update={"request": normalized})

    def _normalize_asset(self, asset: SphericalAsset) -> SphericalAsset:
        payload = asset.model_dump(mode="python")
        payload["source_paths"] = [self.input_file(path) for path in asset.source_paths]
        payload["master_path"] = self.input_file(asset.master_path, role="master path")
        if asset.proxy_path is not None:
            payload["proxy_path"] = self.input_file(asset.proxy_path, role="proxy path")
        return SphericalAsset.model_validate(payload)

    def normalize_reframe(self, submission: ReframeJobSubmission) -> ReframeJobSubmission:
        request = submission.request
        payload = request.model_dump(mode="python")
        payload["asset"] = self._normalize_asset(request.asset)
        payload["workspace_root"] = self.output_directory(
            request.workspace_root, role="workspace root"
        )
        payload["output_path"] = self.output_path(request.output_path)
        payload["cache_dir"] = self.output_directory(request.cache_dir, role="cache directory")
        normalized = ReframeRenderRequest.model_validate(payload)
        return submission.model_copy(update={"request": normalized})

    def normalize_analyze(self, submission: AnalyzeJobSubmission) -> AnalyzeJobSubmission:
        return submission.model_copy(
            update={
                "asset": self._normalize_asset(submission.asset),
                "cache_dir": self.output_directory(
                    submission.cache_dir, role="analysis cache directory"
                ),
            }
        )

    def upload_destination(self, relative_path: str) -> Path:
        root = self.settings.upload_root
        if root is None:
            raise WorkerSecurityError("uploads_disabled", "HTTP uploads are disabled.")
        relative = Path(relative_path)
        if relative.is_absolute() or not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
            raise WorkerSecurityError("invalid_upload_path", "Upload path must be relative and traversal-free.")
        destination = (root / relative).resolve(strict=False)
        self._require_inside(destination, [root], role="upload path")
        if destination.suffix.lower() not in {".insv", ".mp4"}:
            raise WorkerSecurityError(
                "invalid_upload_type",
                "Uploads must use the .insv or .mp4 extension.",
            )
        return destination
