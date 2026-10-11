"""Phase 4 spherical analysis, suggestion review, and explicit decisions."""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel

from open_storyline.nodes.core_nodes.base_node import NodeMeta
from open_storyline.nodes.node_state import NodeState
from open_storyline.nodes.spherical_nodes.common import (
    SphericalNodeBase,
    image_media_item,
    normal_video_media_item,
)
from open_storyline.nodes.spherical_nodes.schemas import (
    AcceptReframeSuggestionInput,
    AnalyzeSphericalAssetInput,
    InspectSubjectTrackInput,
    ListDetectedSubjectsInput,
    PreviewReframeSuggestionInput,
    RejectReframeSuggestionInput,
    SuggestEstablishingShotInput,
    SuggestGroupReframeInput,
    SuggestSubjectReframeInput,
)
from open_storyline.spherical.artifact_events import (
    camera_track_create_event as _camera_track_create_event,
    compact_manifest as _compact_manifest,
    compact_suggestion as _compact_suggestion,
    preview_dimensions as _preview_dimensions,
    suggestion_event as _suggestion_event,
)
from open_storyline.spherical.analysis import (
    SphericalAnalysisSettings,
    SphericalAnalyzer,
)
from open_storyline.spherical.analysis_schemas import (
    ReframeSuggestion,
    SphericalAnalysis,
    SubjectTrack,
    SuggestionPolicy,
    SuggestionStatus,
)
from open_storyline.spherical.errors import SphericalValidationError
from open_storyline.spherical.ffmpeg import SphericalRenderer, probe_media
from open_storyline.spherical.manifests import sha256_json
from open_storyline.spherical.media import generate_proxy
from open_storyline.spherical.schemas import ReframeRenderRequest, VirtualCameraTrack
from open_storyline.spherical.suggestions import generate_reframe_suggestion
from open_storyline.utils.register import NODE_REGISTRY


def _compact_subject(track: SubjectTrack) -> dict[str, Any]:
    visible = [sample for sample in track.samples if sample.visibility.value == "visible"]
    return {
        "subject_id": track.subject_id,
        "start_seconds": track.start_seconds,
        "end_seconds": track.end_seconds,
        "visible_samples": len(visible),
        "total_samples": len(track.samples),
        "mean_confidence": track.mean_confidence,
        "labels": track.identity_evidence.get("labels", []),
        "occlusion_gap_count": len(track.occlusion_gaps),
        "seam_crossing_count": len(track.seam_crossing_events),
        "warnings": track.warnings,
    }


def _compact_analysis(analysis: SphericalAnalysis) -> dict[str, Any]:
    return {
        "analysis_id": analysis.analysis_id,
        "asset_id": analysis.asset_id,
        "cache_key": analysis.cache_key,
        "cache_hit": analysis.cache_hit,
        "backend": analysis.backend.model_dump(mode="json"),
        "sample_timestamps": analysis.sample_timestamps,
        "perspective_frame_count": len(analysis.perspective_frames),
        "detection_count": len(analysis.spherical_detections),
        "subject_count": len(analysis.subject_tracks),
        "shot_boundaries_seconds": analysis.shot_boundaries_seconds,
        "confidence_summary": analysis.confidence_summary,
        "review_artifacts": analysis.review_artifacts.model_dump(mode="json"),
        "warnings": analysis.warnings,
    }


class _AssistedNode(SphericalNodeBase):
    async def default_process(
        self,
        node_state: NodeState,
        inputs: dict[str, Any],
    ) -> dict[str, Any]:
        return await self.process(node_state, inputs)


@NODE_REGISTRY.register()
class AnalyzeSphericalAssetNode(_AssistedNode):
    meta = NodeMeta(
        name="analyze_spherical_asset",
        description=(
            "Analyze a stitched spherical asset through overlapping perspective views. "
            "Returns inspectable detections, seam-aware subject tracks, confidence, "
            "review media, backend identity, parameters, and warnings. The default "
            "CPU backend proposes visual candidates and does not claim identity."
        ),
        node_id="analyze_spherical_asset",
        node_kind="spherical_analysis",
        require_prior_kind=["spherical_asset"],
        default_require_prior_kind=["spherical_asset"],
        next_available_node=[
            "list_detected_subjects",
            "inspect_subject_track",
            "suggest_subject_reframe",
            "suggest_group_reframe",
            "suggest_establishing_shot",
        ],
    )
    input_schema: ClassVar[type[BaseModel]] = AnalyzeSphericalAssetInput

    async def process(self, node_state: NodeState, inputs: dict[str, Any]):
        repository = self._repository(node_state)
        asset = repository.get_asset(inputs["asset_id"])
        if asset.proxy_path is None:
            output_dir = self._prepare_output_directory(node_state)
            proxy = generate_proxy(
                asset,
                output_dir / "analysis-proxy.mp4",
                cache_dir=self.server_cfg.spherical.cache_dir,
                proxy_width=self.server_cfg.spherical.proxy_width,
            )
            asset = asset.model_copy(update={"proxy_path": proxy.path})
        cfg = self.server_cfg.spherical
        settings = SphericalAnalysisSettings(
            interval_seconds=inputs["interval_seconds"] or cfg.analysis_interval_seconds,
            max_frames=inputs["max_frames"] or cfg.analysis_max_frames,
            yaw_step_degrees=inputs["yaw_step_degrees"] or cfg.analysis_yaw_step_degrees,
            pitch_centers=tuple(inputs["pitch_centers"] or cfg.analysis_pitch_centers),
            horizontal_fov=inputs["horizontal_fov"] or cfg.analysis_horizontal_fov,
            view_width=inputs["view_width"] or cfg.analysis_view_width,
            view_height=inputs["view_height"] or cfg.analysis_view_height,
            minimum_detection_confidence=(
                inputs["minimum_detection_confidence"]
                if inputs["minimum_detection_confidence"] is not None
                else cfg.analysis_minimum_detection_confidence
            ),
            dedup_angular_threshold_degrees=(
                inputs["dedup_angular_threshold_degrees"]
                or cfg.analysis_dedup_angular_threshold_degrees
            ),
            max_track_angular_distance_degrees=(
                inputs["max_track_angular_distance_degrees"]
                or cfg.analysis_max_track_angular_distance_degrees
            ),
            max_occlusion_seconds=(
                inputs["max_occlusion_seconds"]
                if inputs["max_occlusion_seconds"] is not None
                else cfg.analysis_max_occlusion_seconds
            ),
        )
        analysis = SphericalAnalyzer(settings=settings).analyze(
            asset,
            cache_dir=cfg.cache_dir,
        )
        overlay_probe = probe_media(
            analysis.review_artifacts.detection_overlay_clip_path
        )
        node_state.node_summary.info_for_user(
            f"Analyzed spherical asset {asset.asset_id}: "
            f"{len(analysis.subject_tracks)} subject track(s).",
            preview_urls=[
                str(analysis.review_artifacts.detection_contact_sheet_path),
                str(analysis.review_artifacts.detection_overlay_clip_path),
            ],
        )
        node_state.node_summary.info_for_llm(
            "Detected subjects are candidates. Inspect a subject and preview a "
            "suggestion before explicitly accepting any camera plan."
        )
        return {
            "spherical_analysis": analysis.model_dump(mode="json"),
            "analysis_summary": _compact_analysis(analysis),
            "analysis_review_media": [
                image_media_item(
                    analysis.review_artifacts.detection_contact_sheet_path,
                    media_id=f"{analysis.analysis_id}-contact-sheet",
                    kind="spherical_detection_contact_sheet",
                ),
                normal_video_media_item(
                    analysis.review_artifacts.detection_overlay_clip_path,
                    overlay_probe,
                    media_id=f"{analysis.analysis_id}-detection-overlay",
                ),
            ],
        }


@NODE_REGISTRY.register()
class ListDetectedSubjectsNode(_AssistedNode):
    meta = NodeMeta(
        name="list_detected_subjects",
        description=(
            "List compact detected-subject records from the latest or selected "
            "spherical analysis, including confidence, occlusions, seam crossings, "
            "candidate labels, and warnings."
        ),
        node_id="list_detected_subjects",
        node_kind="spherical_subject_list",
        require_prior_kind=["spherical_analysis"],
        default_require_prior_kind=["spherical_analysis"],
    )
    input_schema: ClassVar[type[BaseModel]] = ListDetectedSubjectsInput

    async def process(self, node_state: NodeState, inputs: dict[str, Any]):
        analysis = self._repository(node_state).get_analysis(inputs["analysis_id"])
        subjects = [
            _compact_subject(track)
            for track in analysis.subject_tracks
            if track.mean_confidence >= inputs["minimum_confidence"]
        ]
        node_state.node_summary.info_for_user(
            f"Found {len(subjects)} detected subject track(s)."
        )
        return {
            "analysis_id": analysis.analysis_id,
            "subjects": subjects,
            "count": len(subjects),
        }


@NODE_REGISTRY.register()
class InspectSubjectTrackNode(_AssistedNode):
    meta = NodeMeta(
        name="inspect_subject_track",
        description=(
            "Inspect one detected subject's spherical trajectory, visibility, "
            "confidence, identity evidence, occlusion gaps, and seam-crossing events."
        ),
        node_id="inspect_subject_track",
        node_kind="spherical_subject_inspection",
        require_prior_kind=["spherical_analysis"],
        default_require_prior_kind=["spherical_analysis"],
    )
    input_schema: ClassVar[type[BaseModel]] = InspectSubjectTrackInput

    async def process(self, node_state: NodeState, inputs: dict[str, Any]):
        analysis = self._repository(node_state).get_analysis(inputs["analysis_id"])
        subject = next(
            (
                track
                for track in analysis.subject_tracks
                if track.subject_id == inputs["subject_id"]
            ),
            None,
        )
        if subject is None:
            raise SphericalValidationError(
                "Detected subject was not found in the selected analysis."
            )
        node_state.node_summary.info_for_user(
            f"Inspected subject track {subject.subject_id}."
        )
        return {
            "analysis_id": analysis.analysis_id,
            "subject_track": subject.model_dump(mode="json"),
            "summary": _compact_subject(subject),
            "trajectory_json_path": str(
                analysis.review_artifacts.subject_trajectory_json_path
            ),
        }


class _SuggestNode(_AssistedNode):
    def _create_suggestion(
        self,
        node_state: NodeState,
        *,
        analysis_id: str | None,
        policy: SuggestionPolicy,
        subject_ids: list[str],
        output_width: int,
        output_height: int,
        parameters: dict[str, Any],
    ) -> dict[str, Any]:
        repository = self._repository(node_state)
        analysis = repository.get_analysis(analysis_id)
        asset = repository.get_asset(analysis.asset_id)
        suggestion = generate_reframe_suggestion(
            analysis,
            asset,
            policy=policy,
            destination_dir=self._prepare_output_directory(node_state),
            subject_ids=subject_ids,
            output_width=output_width,
            output_height=output_height,
            parameters=parameters,
        )
        existing = {
            record.suggestion.suggestion_id: record
            for record in repository.list_suggestion_records(include_decided=True)
        }.get(suggestion.suggestion_id)
        if existing is not None:
            node_state.node_summary.info_for_user(
                f"Reused existing suggestion {suggestion.suggestion_id}."
            )
            return {
                "reframe_suggestion": _compact_suggestion(existing.suggestion),
                "already_exists": True,
            }
        node_state.node_summary.info_for_user(
            f"Created proposed camera track {suggestion.suggestion_id}."
        )
        node_state.node_summary.info_for_llm(
            "This is not an active camera plan. Preview it, then call "
            "accept_reframe_suggestion or reject_reframe_suggestion explicitly."
        )
        return {
            "reframe_suggestion_event": _suggestion_event("propose", suggestion),
            "reframe_suggestion": _compact_suggestion(suggestion),
            "already_exists": False,
        }


@NODE_REGISTRY.register()
class SuggestSubjectReframeNode(_SuggestNode):
    meta = NodeMeta(
        name="suggest_subject_reframe",
        description=(
            "Propose—but do not activate—a normal VirtualCameraTrack that centers a "
            "selected subject, places it on a rule-of-thirds position, or follows the "
            "largest visible person candidate. Records backend, confidence, parameters, "
            "nadir/seam constraints, and warnings."
        ),
        node_id="suggest_subject_reframe",
        node_kind="spherical_reframe_suggestion",
        require_prior_kind=["spherical_analysis"],
        default_require_prior_kind=["spherical_analysis"],
        next_available_node=[
            "preview_reframe_suggestion",
            "accept_reframe_suggestion",
            "reject_reframe_suggestion",
        ],
    )
    input_schema: ClassVar[type[BaseModel]] = SuggestSubjectReframeInput

    async def process(self, node_state: NodeState, inputs: dict[str, Any]):
        parameters = {
            key: inputs[key]
            for key in (
                "placement",
                "horizontal_fov",
                "minimum_fov",
                "maximum_fov",
                "avoid_nadir_below_pitch",
                "stitch_seam_yaws",
                "stitch_seam_zone_width",
            )
            if inputs[key] is not None
        }
        return self._create_suggestion(
            node_state,
            analysis_id=inputs["analysis_id"],
            policy=SuggestionPolicy(inputs["policy"]),
            subject_ids=[inputs["subject_id"]] if inputs["subject_id"] else [],
            output_width=inputs["output_width"],
            output_height=inputs["output_height"],
            parameters=parameters,
        )


@NODE_REGISTRY.register()
class SuggestGroupReframeNode(_SuggestNode):
    meta = NodeMeta(
        name="suggest_group_reframe",
        description=(
            "Propose—but do not activate—a group-framing VirtualCameraTrack for "
            "selected or all detected subjects, with adaptive FOV and seam/nadir warnings."
        ),
        node_id="suggest_group_reframe",
        node_kind="spherical_reframe_suggestion",
        require_prior_kind=["spherical_analysis"],
        default_require_prior_kind=["spherical_analysis"],
        next_available_node=["preview_reframe_suggestion"],
    )
    input_schema: ClassVar[type[BaseModel]] = SuggestGroupReframeInput

    async def process(self, node_state: NodeState, inputs: dict[str, Any]):
        parameters = {
            key: inputs[key]
            for key in (
                "minimum_fov",
                "maximum_fov",
                "avoid_nadir_below_pitch",
                "stitch_seam_yaws",
                "stitch_seam_zone_width",
            )
        }
        return self._create_suggestion(
            node_state,
            analysis_id=inputs["analysis_id"],
            policy=SuggestionPolicy.GROUP,
            subject_ids=inputs["subject_ids"],
            output_width=inputs["output_width"],
            output_height=inputs["output_height"],
            parameters=parameters,
        )


@NODE_REGISTRY.register()
class SuggestEstablishingShotNode(_SuggestNode):
    meta = NodeMeta(
        name="suggest_establishing_shot",
        description=(
            "Propose—but do not activate—a wide static establishing shot using the "
            "detected group center when available and explicit seam/nadir constraints."
        ),
        node_id="suggest_establishing_shot",
        node_kind="spherical_reframe_suggestion",
        require_prior_kind=["spherical_analysis"],
        default_require_prior_kind=["spherical_analysis"],
        next_available_node=["preview_reframe_suggestion"],
    )
    input_schema: ClassVar[type[BaseModel]] = SuggestEstablishingShotInput

    async def process(self, node_state: NodeState, inputs: dict[str, Any]):
        parameters = {
            key: inputs[key]
            for key in (
                "horizontal_fov",
                "avoid_nadir_below_pitch",
                "stitch_seam_yaws",
                "stitch_seam_zone_width",
            )
        }
        return self._create_suggestion(
            node_state,
            analysis_id=inputs["analysis_id"],
            policy=SuggestionPolicy.WIDE_ESTABLISHING,
            subject_ids=[],
            output_width=inputs["output_width"],
            output_height=inputs["output_height"],
            parameters=parameters,
        )


@NODE_REGISTRY.register()
class PreviewReframeSuggestionNode(_AssistedNode):
    meta = NodeMeta(
        name="preview_reframe_suggestion",
        description=(
            "Render a low-resolution preview of a proposed assisted camera track "
            "without activating it. Rejected suggestions cannot be previewed."
        ),
        node_id="preview_reframe_suggestion",
        node_kind="spherical_suggestion_preview",
        require_prior_kind=["spherical_reframe_suggestion"],
        default_require_prior_kind=["spherical_reframe_suggestion"],
        next_available_node=[
            "accept_reframe_suggestion",
            "reject_reframe_suggestion",
        ],
    )
    input_schema: ClassVar[type[BaseModel]] = PreviewReframeSuggestionInput

    async def process(self, node_state: NodeState, inputs: dict[str, Any]):
        repository = self._repository(node_state)
        record = repository.get_suggestion(inputs["suggestion_id"])
        if record.suggestion.status == SuggestionStatus.REJECTED:
            raise SphericalValidationError("Rejected suggestions cannot be previewed")
        track = record.suggestion.camera_track
        asset = repository.get_asset(track.asset_id)
        maximum = inputs["max_dimension"] or self.server_cfg.spherical.preview_max_dimension
        width, height = _preview_dimensions(track, maximum)
        preview_track = track.model_copy(
            update={"output_width": width, "output_height": height}
        )
        output_dir = self._prepare_output_directory(node_state)
        destination = output_dir / "suggestion-preview.mp4"
        request = ReframeRenderRequest(
            asset=asset,
            track=preview_track,
            output_path=destination,
            workspace_root=output_dir,
            cache_dir=self.server_cfg.spherical.cache_dir,
            sample_interval_seconds=(
                inputs["sample_interval_seconds"]
                or self.server_cfg.spherical.renderer_sample_interval_seconds
            ),
            use_cache=inputs["use_cache"],
            overwrite=False,
        )
        manifest = SphericalRenderer().render(request)
        probe = probe_media(destination)
        node_state.node_summary.info_for_user(
            f"Rendered preview for proposed suggestion {record.suggestion.suggestion_id}.",
            preview_urls=[str(destination)],
        )
        return {
            "suggestion_preview": {
                "suggestion_id": record.suggestion.suggestion_id,
                "suggestion_status": record.suggestion.status.value,
                **_compact_manifest(manifest),
            },
            "preview_media": [
                normal_video_media_item(
                    destination,
                    probe,
                    media_id=f"{record.suggestion.suggestion_id}-preview",
                )
            ],
        }


@NODE_REGISTRY.register()
class AcceptReframeSuggestionNode(_AssistedNode):
    meta = NodeMeta(
        name="accept_reframe_suggestion",
        description=(
            "Explicitly accept one still-proposed assisted camera suggestion. Only "
            "this decision creates revision 1 of a normal active VirtualCameraTrack."
        ),
        node_id="accept_reframe_suggestion",
        node_kind="spherical_camera_track",
        require_prior_kind=["spherical_reframe_suggestion"],
        default_require_prior_kind=["spherical_reframe_suggestion"],
        next_available_node=[
            "validate_virtual_camera_track",
            "preview_virtual_camera_track",
            "render_reframed_clip",
        ],
    )
    input_schema: ClassVar[type[BaseModel]] = AcceptReframeSuggestionInput

    async def process(self, node_state: NodeState, inputs: dict[str, Any]):
        repository = self._repository(node_state)
        record = repository.get_suggestion(inputs["suggestion_id"])
        if record.suggestion.status != SuggestionStatus.PROPOSED:
            raise SphericalValidationError("Only a proposed suggestion can be accepted")
        existing_track_ids = {
            existing.track.track_id
            for existing in repository.list_track_records(include_deleted=True)
        }
        track = record.suggestion.camera_track
        if track.track_id in existing_track_ids:
            raise SphericalValidationError("Suggestion camera track already exists")
        accepted = record.suggestion.model_copy(
            update={"status": SuggestionStatus.ACCEPTED}
        )
        node_state.node_summary.info_for_user(
            f"Accepted suggestion {accepted.suggestion_id} as camera track {track.track_id}."
        )
        return {
            "reframe_suggestion_event": _suggestion_event("accept", accepted),
            "camera_track_event": _camera_track_create_event(track),
            "accepted_suggestion": _compact_suggestion(accepted),
            "camera_track": {
                "track": track.model_dump(mode="json"),
                "revision": 1,
                "deleted": False,
                "camera_track_hash": sha256_json(track.model_dump(mode="json")),
            },
        }


@NODE_REGISTRY.register()
class RejectReframeSuggestionNode(_AssistedNode):
    meta = NodeMeta(
        name="reject_reframe_suggestion",
        description=(
            "Explicitly reject one still-proposed assisted camera suggestion. The "
            "decision is auditable and does not create or delete a camera track."
        ),
        node_id="reject_reframe_suggestion",
        node_kind="spherical_suggestion_decision",
        require_prior_kind=["spherical_reframe_suggestion"],
        default_require_prior_kind=["spherical_reframe_suggestion"],
    )
    input_schema: ClassVar[type[BaseModel]] = RejectReframeSuggestionInput

    async def process(self, node_state: NodeState, inputs: dict[str, Any]):
        repository = self._repository(node_state)
        record = repository.get_suggestion(inputs["suggestion_id"])
        if record.suggestion.status != SuggestionStatus.PROPOSED:
            raise SphericalValidationError("Only a proposed suggestion can be rejected")
        rejected = record.suggestion.model_copy(
            update={"status": SuggestionStatus.REJECTED}
        )
        node_state.node_summary.info_for_user(
            f"Rejected suggestion {rejected.suggestion_id}."
        )
        return {
            "reframe_suggestion_event": _suggestion_event(
                "reject",
                rejected,
                reason=inputs["reason"],
            ),
            "rejected_suggestion": _compact_suggestion(rejected),
        }
