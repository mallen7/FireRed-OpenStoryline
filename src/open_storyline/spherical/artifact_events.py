"""Shared spherical artifact-event and compact-view contracts.

This module has no node-registry side effects, so web/API code can reuse the
exact MCP persistence shape without importing and registering node packages.
"""

from __future__ import annotations

from typing import Any

from open_storyline.spherical.analysis_schemas import ReframeSuggestion
from open_storyline.spherical.direction_schemas import DirectorProposal
from open_storyline.spherical.geometry import validate_output_aspect_ratio
from open_storyline.spherical.manifests import sha256_json
from open_storyline.spherical.schemas import (
    ReframeRenderManifest,
    VirtualCameraTrack,
)


def track_event(action: str, track: VirtualCameraTrack, revision: int) -> dict[str, Any]:
    event: dict[str, Any] = {
        "schema_version": "1.0",
        "action": action,
        "track_id": track.track_id,
        "asset_id": track.asset_id,
        "revision": revision,
    }
    if action != "delete":
        event["track"] = track.model_dump(mode="json")
    return event


def camera_track_create_event(track: VirtualCameraTrack) -> dict[str, Any]:
    return track_event("create", track, 1)


def compact_track(
    track: VirtualCameraTrack,
    revision: int,
    deleted: bool = False,
) -> dict[str, Any]:
    return {
        "track": track.model_dump(mode="json"),
        "revision": revision,
        "deleted": deleted,
        "camera_track_hash": sha256_json(track.model_dump(mode="json")),
    }


def preview_dimensions(track: VirtualCameraTrack, max_dimension: int) -> tuple[int, int]:
    maximum = max_dimension - max_dimension % 2
    aspect = validate_output_aspect_ratio(track.output_width, track.output_height)
    if aspect == "16:9":
        width = maximum
        height = int(round(width * 9 / 16))
        height += height % 2
    elif aspect == "9:16":
        height = maximum
        width = int(round(height * 9 / 16))
        width += width % 2
    else:
        width = height = maximum
    return width, height


def compact_manifest(manifest: ReframeRenderManifest) -> dict[str, Any]:
    return {
        "render_key": manifest.render_key,
        "output_path": str(manifest.output_path),
        "output_hash": manifest.output_hash,
        "output_width": manifest.output_width,
        "output_height": manifest.output_height,
        "output_fps": manifest.output_fps,
        "render_duration_seconds": manifest.render_duration_seconds,
        "selected_backend": manifest.selected_backend,
        "audio_preserved": manifest.audio_preserved,
        "cache_hit": manifest.cache_hit,
        "warnings": manifest.warnings,
        "fallback_decisions": manifest.fallback_decisions,
    }


def suggestion_event(
    action: str,
    suggestion: ReframeSuggestion,
    **extra: Any,
) -> dict[str, Any]:
    event: dict[str, Any] = {
        "schema_version": "1.0",
        "action": action,
        "suggestion_id": suggestion.suggestion_id,
        "analysis_id": suggestion.analysis_id,
        "asset_id": suggestion.asset_id,
    }
    if action == "propose":
        event["suggestion"] = suggestion.model_dump(mode="json")
    event.update(extra)
    return event


def compact_suggestion(suggestion: ReframeSuggestion) -> dict[str, Any]:
    return {
        "suggestion_id": suggestion.suggestion_id,
        "analysis_id": suggestion.analysis_id,
        "asset_id": suggestion.asset_id,
        "policy": suggestion.policy.value,
        "status": suggestion.status.value,
        "subject_ids": suggestion.subject_ids,
        "confidence": suggestion.confidence,
        "parameters": suggestion.parameters,
        "camera_track_hash": sha256_json(
            suggestion.camera_track.model_dump(mode="json")
        ),
        "camera_track": suggestion.camera_track.model_dump(mode="json"),
        "analysis_backend": suggestion.analysis_backend.model_dump(mode="json"),
        "camera_trajectory_path": str(suggestion.camera_trajectory_path),
        "warnings": suggestion.warnings,
    }


def proposal_event(
    action: str,
    proposal: DirectorProposal,
    **extra: Any,
) -> dict[str, Any]:
    event: dict[str, Any] = {
        "schema_version": "1.0",
        "action": action,
        "proposal_id": proposal.proposal_id,
        "timeline_id": proposal.timeline_id,
        "analysis_id": proposal.analysis_id,
        "asset_id": proposal.asset_id,
    }
    if action == "propose":
        event["proposal"] = proposal.model_dump(mode="json")
    event.update(extra)
    return event


def compact_proposal(proposal: DirectorProposal) -> dict[str, Any]:
    return {
        "proposal_id": proposal.proposal_id,
        "timeline_id": proposal.timeline_id,
        "speaker_analysis_id": proposal.speaker_analysis_id,
        "analysis_id": proposal.analysis_id,
        "asset_id": proposal.asset_id,
        "profile_id": proposal.profile_id,
        "variant": proposal.variant.value,
        "status": proposal.status.value,
        "aggregate_score": proposal.aggregate_score,
        "motion_constraints": proposal.motion_constraints.model_dump(mode="json"),
        "motion_validation": proposal.motion_validation.model_dump(mode="json"),
        "decisions": [
            decision.model_dump(mode="json") for decision in proposal.decisions
        ],
        "camera_track": proposal.camera_track.model_dump(mode="json"),
        "camera_track_hash": sha256_json(
            proposal.camera_track.model_dump(mode="json")
        ),
        "review_artifacts": proposal.review_artifacts.model_dump(mode="json"),
        "warnings": proposal.warnings,
    }
