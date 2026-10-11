"""Typed errors for the optional spherical-media extension."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar


class SphericalError(RuntimeError):
    """Base class for stable, serializable spherical-media failures."""

    code: ClassVar[str] = "spherical_error"

    def __init__(
        self,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.details = dict(details or {})

    def as_dict(self) -> dict[str, Any]:
        """Return the transport-neutral form used by future nodes and workers."""

        return {
            "code": self.code,
            "message": self.message,
            "details": self.details,
        }


class SphericalDisabledError(SphericalError):
    code = "spherical_disabled"


class SphericalValidationError(SphericalError):
    code = "spherical_validation_failed"


class SphericalCapabilityError(SphericalError):
    code = "spherical_capability_unavailable"


class SphericalProbeError(SphericalError):
    code = "spherical_probe_failed"


class SphericalRenderError(SphericalError):
    code = "spherical_render_failed"


class SphericalCacheError(SphericalError):
    code = "spherical_cache_failed"


class SphericalWorkerError(SphericalError):
    code = "spherical_worker_failed"
