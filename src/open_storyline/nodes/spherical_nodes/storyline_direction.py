"""Phase 5 storyline-aware spherical direction and flat-media handoff nodes."""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel

from open_storyline.nodes.core_nodes.base_node import NodeMeta
from open_storyline.nodes.node_state import NodeState
from open_storyline.nodes.spherical_nodes.common import (
    SphericalNodeBase,
    normal_video_media_item,
)
from open_storyline.nodes.spherical_nodes.schemas import (
    AcceptDirectorProposalInput,
    AnalyzeSpeakerCandidatesInput,
    BuildSphericalStoryTimelineInput,
    CompareDirectorProposalsInput,
    GenerateDirectorProposalsInput,
    ListDirectorProfilesInput,
    PreviewDirectorProposalInput,
    RejectDirectorProposalInput,
    RenderDirectorProposalInput,
)
from open_storyline.spherical.artifact_events import (
    camera_track_create_event as _camera_track_create_event,
    compact_manifest as _compact_manifest,
    compact_proposal as _compact_proposal,
    preview_dimensions as _preview_dimensions,
    proposal_event as _proposal_event,
)
from open_storyline.spherical.direction_schemas import (
    DirectorProposal,
    DirectorProposalStatus,
    DirectorTimelineHandoff,
    DirectorVariant,
    MotionConstraints,
    TimelineReferenceKind,
    TimelineSourceReference,
)
from open_storyline.spherical.director import (
    compare_director_proposals,
    generate_director_proposal,
)
from open_storyline.spherical.errors import SphericalValidationError
from open_storyline.spherical.ffmpeg import SphericalRenderer, probe_media
from open_storyline.spherical.manifests import manifest_path_for, sha256_json
from open_storyline.spherical.profiles import (
    get_editing_profile,
    list_editing_profiles,
)
from open_storyline.spherical.schemas import ReframeRenderRequest, VirtualCameraTrack
from open_storyline.spherical.speaker import analyze_speaker_candidates
from open_storyline.spherical.story_timeline import normalize_story_timeline
from open_storyline.utils.register import NODE_REGISTRY


class _DirectionNode(SphericalNodeBase):
    async def default_process(
        self,
        node_state: NodeState,
        inputs: dict[str, Any],
    ) -> dict[str, Any]:
        return await self.process(node_state, inputs)


@NODE_REGISTRY.register()
class ListDirectorProfilesNode(_DirectionNode):
    meta = NodeMeta(
        name="list_director_profiles",
        description=(
            "List the six built-in spherical editing profiles and their shot length, "
            "FOV, motion, speaker, reaction, establishing, vertical, confidence, "
            "seam/nadir, music, and subtitle policies."
        ),
        node_id="list_director_profiles",
        node_kind="spherical_director_profiles",
        require_prior_kind=[],
        default_require_prior_kind=[],
    )
    input_schema: ClassVar[type[BaseModel]] = ListDirectorProfilesInput

    async def process(self, node_state: NodeState, inputs: dict[str, Any]):
        profiles = list_editing_profiles()
        if inputs["profile_id"] is not None:
            profiles = [get_editing_profile(inputs["profile_id"])]
        node_state.node_summary.info_for_user(
            f"Found {len(profiles)} spherical editing profile(s)."
        )
        return {
            "editing_profiles": [profile.model_dump(mode="json") for profile in profiles],
            "count": len(profiles),
        }


@NODE_REGISTRY.register()
class BuildSphericalStoryTimelineNode(_DirectionNode):
    meta = NodeMeta(
        name="build_spherical_story_timeline",
        description=(
            "Normalize existing OpenStoryline ASR segments/words, split scenes, clip "
            "understanding, script/timeline plans, audio activity, optional mouth "
            "activity, and spherical subject/face tracks onto one source timeline. "
            "This reuses existing ASR output and never reruns transcription."
        ),
        node_id="build_spherical_story_timeline",
        node_kind="spherical_story_timeline",
        require_prior_kind=["spherical_analysis"],
        default_require_prior_kind=["spherical_analysis"],
        next_available_node=["analyze_speaker_candidates"],
    )
    input_schema: ClassVar[type[BaseModel]] = BuildSphericalStoryTimelineInput

    async def process(self, node_state: NodeState, inputs: dict[str, Any]):
        repository = self._repository(node_state)
        analysis = repository.get_analysis(inputs["analysis_id"])
        asset = repository.get_asset(analysis.asset_id)
        timeline = normalize_story_timeline(
            analysis,
            duration_seconds=asset.duration_seconds,
            asr_infos=inputs["asr_infos"],
            split_shots=inputs["split_shots"],
            clip_captions=inputs["clip_captions"],
            group_scripts=inputs["group_scripts"],
            timeline_tracks=inputs["timeline_tracks"],
            mouth_activity=inputs["mouth_activity"],
            audio_activity=inputs["audio_activity"],
        )
        node_state.node_summary.info_for_user(
            f"Built normalized spherical story timeline {timeline.timeline_id} with "
            f"{len(timeline.transcript_turns)} transcript turn(s)."
        )
        node_state.node_summary.info_for_llm(
            "Speaker identity is not inferred by this step. Run candidate analysis "
            "and inspect ambiguity before generating director proposals."
        )
        return {
            "normalized_story_timeline": timeline.model_dump(mode="json"),
            "timeline_summary": {
                "timeline_id": timeline.timeline_id,
                "analysis_id": timeline.analysis_id,
                "asset_id": timeline.asset_id,
                "duration_seconds": timeline.duration_seconds,
                "source_summary": timeline.source_summary,
                "warnings": timeline.warnings,
            },
        }


@NODE_REGISTRY.register()
class AnalyzeSpeakerCandidatesNode(_DirectionNode):
    meta = NodeMeta(
        name="analyze_speaker_candidates",
        description=(
            "Score probable speaker candidates for each transcript turn using voice, "
            "face, optional mouth, subject continuity, scene, previous-turn, boundary, "
            "and track-confidence evidence. Weak evidence remains ambiguous or unresolved "
            "with ranked alternatives; no identity is confirmed."
        ),
        node_id="analyze_speaker_candidates",
        node_kind="spherical_speaker_analysis",
        require_prior_kind=["spherical_story_timeline"],
        default_require_prior_kind=["spherical_story_timeline"],
        next_available_node=["generate_director_proposals"],
    )
    input_schema: ClassVar[type[BaseModel]] = AnalyzeSpeakerCandidatesInput

    async def process(self, node_state: NodeState, inputs: dict[str, Any]):
        repository = self._repository(node_state)
        timeline = repository.get_story_timeline(inputs["timeline_id"])
        analysis = repository.get_analysis(timeline.analysis_id)
        profile = get_editing_profile(inputs["profile_id"])
        result = analyze_speaker_candidates(
            timeline,
            analysis,
            profile_id=profile.profile_id,
            probable_threshold=(
                inputs["probable_threshold"]
                if inputs["probable_threshold"] is not None
                else profile.probable_speaker_threshold
            ),
            ambiguity_margin=inputs["ambiguity_margin"],
        )
        probable = sum(
            turn.status.value == "probable" for turn in result.turns
        )
        node_state.node_summary.info_for_user(
            f"Scored {len(result.turns)} transcript turn(s): {probable} probable, "
            f"{len(result.turns) - probable} ambiguous or unresolved."
        )
        return {
            "speaker_candidate_analysis": result.model_dump(mode="json"),
            "speaker_summary": {
                "speaker_analysis_id": result.speaker_analysis_id,
                "timeline_id": result.timeline_id,
                "turn_count": len(result.turns),
                "probable_count": probable,
                "ambiguous_or_unresolved_count": len(result.turns) - probable,
                "warnings": result.warnings,
            },
        }


@NODE_REGISTRY.register()
class GenerateDirectorProposalsNode(_DirectionNode):
    meta = NodeMeta(
        name="generate_director_proposals",
        description=(
            "Generate at least two inactive, inspectable director proposals from a "
            "normalized timeline and speaker candidates. Every shot records all eleven "
            "required score components, rationale, confidence, alternatives, cut/pan/hold "
            "choice, motion validation, profile, and warnings."
        ),
        node_id="generate_director_proposals",
        node_kind="spherical_director_proposal",
        require_prior_kind=["spherical_speaker_analysis"],
        default_require_prior_kind=["spherical_speaker_analysis"],
        next_available_node=[
            "compare_director_proposals",
            "preview_director_proposal",
            "accept_director_proposal",
            "reject_director_proposal",
        ],
    )
    input_schema: ClassVar[type[BaseModel]] = GenerateDirectorProposalsInput

    async def process(self, node_state: NodeState, inputs: dict[str, Any]):
        repository = self._repository(node_state)
        timeline = repository.get_story_timeline(inputs["timeline_id"])
        speaker_analysis = repository.get_speaker_analysis(
            inputs["speaker_analysis_id"]
        )
        if speaker_analysis.timeline_id != timeline.timeline_id:
            raise SphericalValidationError(
                "Selected speaker analysis does not belong to the selected timeline."
            )
        analysis = repository.get_analysis(timeline.analysis_id)
        asset = repository.get_asset(timeline.asset_id)
        profile = get_editing_profile(inputs["profile_id"])
        if speaker_analysis.profile_id != profile.profile_id:
            raise SphericalValidationError(
                "Speaker analysis profile does not match the requested director profile."
            )
        motion_constraints = (
            MotionConstraints.model_validate(inputs["motion_constraints"])
            if inputs["motion_constraints"] is not None
            else None
        )
        existing = {
            record.proposal.proposal_id: record.proposal
            for record in repository.list_director_proposal_records(
                include_decided=True
            )
        }
        proposal_events: list[dict[str, Any]] = []
        proposals: list[DirectorProposal] = []
        for raw_variant in inputs["variants"]:
            variant = DirectorVariant(raw_variant)
            width, height = (
                (
                    inputs["vertical_output_width"],
                    inputs["vertical_output_height"],
                )
                if variant == DirectorVariant.VERTICAL_SOCIAL
                else (inputs["output_width"], inputs["output_height"])
            )
            proposal = generate_director_proposal(
                timeline,
                speaker_analysis,
                analysis,
                asset,
                profile,
                variant=variant,
                destination_dir=self._prepare_output_directory(node_state),
                output_width=width,
                output_height=height,
                motion_constraints=motion_constraints,
            )
            if proposal.proposal_id in existing:
                proposal = existing[proposal.proposal_id]
            else:
                proposal_events.append(_proposal_event("propose", proposal))
            proposals.append(proposal)
        node_state.node_summary.info_for_user(
            f"Generated {len(proposals)} inactive director proposal(s)."
        )
        node_state.node_summary.info_for_llm(
            "Compare and preview proposals. Only accept_director_proposal activates "
            "one normal camera track; generation never changes the active edit."
        )
        result: dict[str, Any] = {
            "director_proposals": [
                _compact_proposal(proposal) for proposal in proposals
            ],
            "count": len(proposals),
        }
        if proposal_events:
            result["director_proposal_events"] = proposal_events
        return result


@NODE_REGISTRY.register()
class CompareDirectorProposalsNode(_DirectionNode):
    meta = NodeMeta(
        name="compare_director_proposals",
        description=(
            "Compare two or more same-timeline director proposals by explainable score, "
            "shot/cut/pan counts, confidence, strengths, and tradeoffs. A recommendation "
            "is informational and does not activate a proposal."
        ),
        node_id="compare_director_proposals",
        node_kind="spherical_director_comparison",
        require_prior_kind=["spherical_director_proposal"],
        default_require_prior_kind=["spherical_director_proposal"],
        next_available_node=["preview_director_proposal"],
    )
    input_schema: ClassVar[type[BaseModel]] = CompareDirectorProposalsInput

    async def process(self, node_state: NodeState, inputs: dict[str, Any]):
        repository = self._repository(node_state)
        proposals = [
            repository.get_director_proposal(proposal_id).proposal
            for proposal_id in inputs["proposal_ids"]
        ]
        comparison = compare_director_proposals(proposals)
        node_state.node_summary.info_for_user(comparison.summary)
        return {"director_comparison": comparison.model_dump(mode="json")}


@NODE_REGISTRY.register()
class PreviewDirectorProposalNode(_DirectionNode):
    meta = NodeMeta(
        name="preview_director_proposal",
        description=(
            "Render a cached low-resolution flat preview of an inactive or accepted "
            "director proposal without changing proposal or timeline state."
        ),
        node_id="preview_director_proposal",
        node_kind="spherical_director_preview",
        require_prior_kind=["spherical_director_proposal"],
        default_require_prior_kind=["spherical_director_proposal"],
        next_available_node=["accept_director_proposal", "reject_director_proposal"],
    )
    input_schema: ClassVar[type[BaseModel]] = PreviewDirectorProposalInput

    async def process(self, node_state: NodeState, inputs: dict[str, Any]):
        repository = self._repository(node_state)
        proposal = repository.get_director_proposal(inputs["proposal_id"]).proposal
        if proposal.status == DirectorProposalStatus.REJECTED:
            raise SphericalValidationError("Rejected director proposals cannot be previewed")
        asset = repository.get_asset(proposal.asset_id)
        maximum = inputs["max_dimension"] or self.server_cfg.spherical.preview_max_dimension
        width, height = _preview_dimensions(proposal.camera_track, maximum)
        preview_track = proposal.camera_track.model_copy(
            update={"output_width": width, "output_height": height}
        )
        output_dir = self._prepare_output_directory(node_state)
        destination = output_dir / "director-preview.mp4"
        manifest = SphericalRenderer().render(
            ReframeRenderRequest(
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
        )
        probe = probe_media(destination)
        node_state.node_summary.info_for_user(
            f"Rendered preview for director proposal {proposal.proposal_id}.",
            preview_urls=[str(destination)],
        )
        return {
            "director_preview": {
                "proposal_id": proposal.proposal_id,
                "proposal_status": proposal.status.value,
                **_compact_manifest(manifest),
            },
            "preview_media": [
                normal_video_media_item(
                    destination,
                    probe,
                    media_id=f"{proposal.proposal_id}-preview",
                )
            ],
        }


@NODE_REGISTRY.register()
class AcceptDirectorProposalNode(_DirectionNode):
    meta = NodeMeta(
        name="accept_director_proposal",
        description=(
            "Explicitly accept one still-proposed director plan. This is the only "
            "Phase 5 operation that creates revision 1 of its normal director camera track."
        ),
        node_id="accept_director_proposal",
        node_kind="spherical_camera_track",
        require_prior_kind=["spherical_director_proposal"],
        default_require_prior_kind=["spherical_director_proposal"],
        next_available_node=["render_director_proposal"],
    )
    input_schema: ClassVar[type[BaseModel]] = AcceptDirectorProposalInput

    async def process(self, node_state: NodeState, inputs: dict[str, Any]):
        repository = self._repository(node_state)
        record = repository.get_director_proposal(inputs["proposal_id"])
        if record.proposal.status != DirectorProposalStatus.PROPOSED:
            raise SphericalValidationError("Only a proposed director plan can be accepted")
        track = record.proposal.camera_track
        if track.track_id in {
            item.track.track_id
            for item in repository.list_track_records(include_deleted=True)
        }:
            raise SphericalValidationError("Director proposal camera track already exists")
        accepted = record.proposal.model_copy(
            update={"status": DirectorProposalStatus.ACCEPTED}
        )
        node_state.node_summary.info_for_user(
            f"Accepted director proposal {accepted.proposal_id} as camera track {track.track_id}."
        )
        return {
            "director_proposal_event": _proposal_event("accept", accepted),
            "camera_track_event": _camera_track_create_event(track),
            "accepted_director_proposal": _compact_proposal(accepted),
        }


@NODE_REGISTRY.register()
class RejectDirectorProposalNode(_DirectionNode):
    meta = NodeMeta(
        name="reject_director_proposal",
        description=(
            "Explicitly reject one still-proposed director plan. The decision is "
            "auditable and never creates or deletes a camera track."
        ),
        node_id="reject_director_proposal",
        node_kind="spherical_director_decision",
        require_prior_kind=["spherical_director_proposal"],
        default_require_prior_kind=["spherical_director_proposal"],
    )
    input_schema: ClassVar[type[BaseModel]] = RejectDirectorProposalInput

    async def process(self, node_state: NodeState, inputs: dict[str, Any]):
        repository = self._repository(node_state)
        record = repository.get_director_proposal(inputs["proposal_id"])
        if record.proposal.status != DirectorProposalStatus.PROPOSED:
            raise SphericalValidationError("Only a proposed director plan can be rejected")
        rejected = record.proposal.model_copy(
            update={"status": DirectorProposalStatus.REJECTED}
        )
        node_state.node_summary.info_for_user(
            f"Rejected director proposal {rejected.proposal_id}."
        )
        return {
            "director_proposal_event": _proposal_event(
                "reject",
                rejected,
                reason=inputs["reason"],
            ),
            "rejected_director_proposal": _compact_proposal(rejected),
        }


@NODE_REGISTRY.register()
class RenderDirectorProposalNode(_DirectionNode):
    meta = NodeMeta(
        name="render_director_proposal",
        description=(
            "Render an explicitly accepted director proposal into an ordinary flat "
            "H.264/AAC MP4 and return normal load-media and split-shot compatible items. "
            "The existing final renderer never receives raw .insv or spherical media."
        ),
        node_id="render_director_proposal",
        node_kind="spherical_director_handoff",
        require_prior_kind=["spherical_camera_track"],
        default_require_prior_kind=["spherical_camera_track"],
        next_available_node=["load_media", "split_shots", "plan_timeline", "render_video"],
    )
    input_schema: ClassVar[type[BaseModel]] = RenderDirectorProposalInput

    async def process(self, node_state: NodeState, inputs: dict[str, Any]):
        repository = self._repository(node_state)
        proposal = repository.get_director_proposal(inputs["proposal_id"]).proposal
        if proposal.status != DirectorProposalStatus.ACCEPTED:
            raise SphericalValidationError(
                "Director proposal must be explicitly accepted before final rendering"
            )
        active_track = repository.get_track(proposal.camera_track.track_id).track
        asset = repository.get_asset(proposal.asset_id)
        output_dir = self._prepare_output_directory(node_state)
        filename = inputs["output_filename"] or (
            f"directed-{sha256_json(proposal.proposal_id)[:12]}.mp4"
        )
        if Path(filename).name != filename or not filename.lower().endswith(".mp4"):
            raise SphericalValidationError("output_filename must be a safe MP4 basename")
        destination = output_dir / filename
        manifest = SphericalRenderer().render(
            ReframeRenderRequest(
                asset=asset,
                track=active_track,
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
        )
        probe = probe_media(destination)
        profile = get_editing_profile(proposal.profile_id)
        media_id = f"media-{sha256_json([proposal.proposal_id, manifest.output_hash])[:16]}"
        clip_id = f"clip-{sha256_json(media_id)[:16]}"
        handoff = DirectorTimelineHandoff(
            handoff_id=f"handoff-{sha256_json([proposal.proposal_id, manifest.render_key])[:16]}",
            proposal_id=proposal.proposal_id,
            timeline_id=proposal.timeline_id,
            source_reference=TimelineSourceReference(
                kind=TimelineReferenceKind.GENERATED_REFRAME_ARTIFACT,
                spherical_asset_id=asset.asset_id,
                approved_camera_track_id=active_track.track_id,
                generated_reframe_path=destination,
                render_manifest_path=manifest_path_for(destination),
                render_hash=manifest.output_hash,
            ),
            ordinary_media_id=media_id,
            ordinary_clip_id=clip_id,
            music_handoff_behavior=profile.music_handoff_behavior,
            subtitle_handoff_behavior=profile.subtitle_handoff_behavior,
            warnings=manifest.warnings,
        )
        media = normal_video_media_item(destination, probe, media_id=media_id)
        duration_ms = int(round(probe.duration_seconds * 1000))
        clip = {
            "clip_id": clip_id,
            "kind": "video",
            "media_type": "video",
            "path": str(destination),
            "fps": probe.fps,
            "source_ref": {
                "media_id": media_id,
                "start": 0,
                "end": duration_ms,
                "duration": duration_ms,
                "width": probe.width,
                "height": probe.height,
            },
            "extra_info": {
                "director_proposal_id": proposal.proposal_id,
                "approved_camera_track_id": active_track.track_id,
                "render_manifest_path": str(manifest_path_for(destination)),
            },
        }
        node_state.node_summary.info_for_user(
            f"Rendered accepted director proposal {proposal.proposal_id} as ordinary flat media.",
            preview_urls=[str(destination)],
        )
        return {
            "director_timeline_handoff": handoff.model_dump(mode="json"),
            "render_manifest": _compact_manifest(manifest),
            "ordinary_media": media,
            "ordinary_clip": clip,
        }
