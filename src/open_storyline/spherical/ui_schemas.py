"""Versioned request and response contracts for the spherical web workspace."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from open_storyline.spherical.manifests import atomic_write_json
from open_storyline.spherical.schemas import TrackCreatedBy, VirtualCameraTrack


class SphericalUiModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    schema_version: str = Field(default="1.0", pattern=r"^1\.0$")


class SphericalUiTrackCreateRequest(SphericalUiModel):
    track: VirtualCameraTrack

    @model_validator(mode="after")
    def validate_manual_track(self) -> "SphericalUiTrackCreateRequest":
        if self.track.created_by != TrackCreatedBy.MANUAL:
            raise ValueError("the UI can create only manual camera tracks")
        if len(self.track.keyframes) > 1000:
            raise ValueError("camera tracks are limited to 1000 keyframes")
        return self


class SphericalUiTrackUpdateRequest(SphericalUiModel):
    expected_revision: int = Field(gt=0)
    track: VirtualCameraTrack

    @model_validator(mode="after")
    def bound_keyframes(self) -> "SphericalUiTrackUpdateRequest":
        if len(self.track.keyframes) > 1000:
            raise ValueError("camera tracks are limited to 1000 keyframes")
        return self


class SphericalUiPreviewRequest(SphericalUiModel):
    center_seconds: float | None = Field(default=None, ge=0)
    duration_seconds: float = Field(default=4.0, ge=0.25, le=60.0)
    max_dimension: int = Field(default=640, ge=160, le=1920)
    sample_interval_seconds: float | None = Field(default=None, ge=0.05, le=5.0)
    use_cache: bool = True

    @model_validator(mode="after")
    def even_maximum(self) -> "SphericalUiPreviewRequest":
        if self.max_dimension % 2:
            raise ValueError("max_dimension must be even")
        return self


class SphericalUiRenderRequest(SphericalUiModel):
    output_filename: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*\.mp4$",
    )
    sample_interval_seconds: float | None = Field(default=None, ge=0.05, le=5.0)
    use_cache: bool = True


class SphericalUiState(SphericalUiModel):
    enabled: bool
    assets: list[dict[str, Any]] = Field(default_factory=list)
    tracks: list[dict[str, Any]] = Field(default_factory=list)
    suggestions: list[dict[str, Any]] = Field(default_factory=list)
    director_proposals: list[dict[str, Any]] = Field(default_factory=list)
    subjects: list[dict[str, Any]] = Field(default_factory=list)
    renders: list[dict[str, Any]] = Field(default_factory=list)
    worker: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


class SphericalUiMutationResponse(SphericalUiModel):
    artifact_id: str = Field(min_length=1)
    track: VirtualCameraTrack
    revision: int = Field(gt=0)
    camera_track_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class SphericalUiRenderResponse(SphericalUiModel):
    artifact_id: str = Field(min_length=1)
    track_id: str = Field(min_length=1)
    track_revision: int = Field(gt=0)
    render_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    output_path: Path
    output_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    output_width: int = Field(gt=0)
    output_height: int = Field(gt=0)
    output_fps: float = Field(gt=0)
    render_duration_seconds: float = Field(ge=0)
    selected_backend: str = Field(min_length=1)
    audio_preserved: bool
    cache_hit: bool
    warnings: list[str] = Field(default_factory=list)
    fallback_decisions: list[str] = Field(default_factory=list)


SPHERICAL_UI_SCHEMA_MODELS: dict[str, type[BaseModel]] = {
    "spherical-ui-state.schema.json": SphericalUiState,
    "spherical-ui-track-create.schema.json": SphericalUiTrackCreateRequest,
    "spherical-ui-track-update.schema.json": SphericalUiTrackUpdateRequest,
    "spherical-ui-preview-request.schema.json": SphericalUiPreviewRequest,
    "spherical-ui-render-request.schema.json": SphericalUiRenderRequest,
    "spherical-ui-mutation-response.schema.json": SphericalUiMutationResponse,
    "spherical-ui-render-response.schema.json": SphericalUiRenderResponse,
}


def export_spherical_ui_schemas(destination: str | Path) -> list[Path]:
    root = Path(destination).expanduser().resolve(strict=False)
    root.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for filename, model in SPHERICAL_UI_SCHEMA_MODELS.items():
        path = root / filename
        atomic_write_json(path, model.model_json_schema())
        written.append(path)
    return written
