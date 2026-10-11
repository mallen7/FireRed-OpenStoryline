"""Session-scoped spherical state reconstructed from existing node artifacts."""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import ValidationError

from open_storyline.spherical.errors import SphericalValidationError
from open_storyline.spherical.analysis_schemas import (
    ReframeSuggestion,
    SphericalAnalysis,
    SuggestionStatus,
)
from open_storyline.spherical.direction_schemas import (
    DirectorProposal,
    DirectorProposalStatus,
    NormalizedStoryTimeline,
    SpeakerCandidateAnalysis,
)
from open_storyline.spherical.schemas import SphericalAsset, VirtualCameraTrack
from open_storyline.storage.agent_memory import ArtifactStore


ASSET_NODE_IDS = frozenset({"import_spherical_media"})
ANALYSIS_NODE_IDS = frozenset({"analyze_spherical_asset"})
STORY_TIMELINE_NODE_IDS = frozenset({"build_spherical_story_timeline"})
SPEAKER_ANALYSIS_NODE_IDS = frozenset({"analyze_speaker_candidates"})
DIRECTOR_EVENT_NODE_IDS = frozenset(
    {
        "generate_director_proposals",
        "accept_director_proposal",
        "reject_director_proposal",
    }
)
SUGGESTION_EVENT_NODE_IDS = frozenset(
    {
        "suggest_subject_reframe",
        "suggest_group_reframe",
        "suggest_establishing_shot",
        "accept_reframe_suggestion",
        "reject_reframe_suggestion",
    }
)
TRACK_EVENT_NODE_IDS = frozenset(
    {
        "create_virtual_camera_track",
        "update_virtual_camera_track",
        "delete_virtual_camera_track",
        "accept_reframe_suggestion",
        "accept_director_proposal",
    }
)


@dataclass(frozen=True)
class TrackRecord:
    track: VirtualCameraTrack
    revision: int
    deleted: bool
    artifact_id: str
    updated_at: float


@dataclass(frozen=True)
class AnalysisRecord:
    analysis: SphericalAnalysis
    artifact_id: str
    updated_at: float


@dataclass(frozen=True)
class SuggestionRecord:
    suggestion: ReframeSuggestion
    artifact_id: str
    updated_at: float


@dataclass(frozen=True)
class StoryTimelineRecord:
    timeline: NormalizedStoryTimeline
    artifact_id: str
    updated_at: float


@dataclass(frozen=True)
class SpeakerAnalysisRecord:
    analysis: SpeakerCandidateAnalysis
    artifact_id: str
    updated_at: float


@dataclass(frozen=True)
class DirectorProposalRecord:
    proposal: DirectorProposal
    artifact_id: str
    updated_at: float


class SphericalSessionRepository:
    """Read immutable asset and camera-track events for one ArtifactStore session."""

    def __init__(self, store: ArtifactStore) -> None:
        self.store = store

    def _payloads(self, node_ids: frozenset[str]):
        for meta in self.store.list_meta(node_ids=node_ids):
            loaded_meta, data = self.store.load_result(meta.artifact_id)
            if loaded_meta is None or not isinstance(data, dict):
                raise SphericalValidationError(
                    "Spherical session artifact could not be loaded.",
                    details={"artifact_id": meta.artifact_id},
                )
            if data.get("session_id") != self.store.session_id:
                raise SphericalValidationError(
                    "Spherical artifact session does not match the active session.",
                    details={"artifact_id": meta.artifact_id},
                )
            payload = data.get("payload")
            if not isinstance(payload, dict):
                raise SphericalValidationError(
                    "Spherical artifact payload is invalid.",
                    details={"artifact_id": meta.artifact_id},
                )
            yield meta, payload

    def list_assets(self) -> list[SphericalAsset]:
        assets: dict[str, SphericalAsset] = {}
        for meta, payload in self._payloads(ASSET_NODE_IDS):
            raw = payload.get("spherical_asset")
            if raw is None:
                continue
            try:
                asset = SphericalAsset.model_validate(raw)
            except ValidationError as exc:
                raise SphericalValidationError(
                    "Persisted spherical asset failed schema validation.",
                    details={"artifact_id": meta.artifact_id, "error": str(exc)},
                ) from exc
            # Re-importing the same content-addressed asset makes that event the
            # latest selection while retaining one current record per asset ID.
            assets.pop(asset.asset_id, None)
            assets[asset.asset_id] = asset
        return list(assets.values())

    def get_asset(self, asset_id: str | None = None) -> SphericalAsset:
        assets = self.list_assets()
        if asset_id is None:
            if not assets:
                raise SphericalValidationError(
                    "No spherical asset exists in the active session. Import one first."
                )
            return assets[-1]
        for asset in assets:
            if asset.asset_id == asset_id:
                return asset
        raise SphericalValidationError(
            "Spherical asset was not found in the active session.",
            details={"asset_id": asset_id},
        )

    def list_analysis_records(self) -> list[AnalysisRecord]:
        records: dict[str, AnalysisRecord] = {}
        for meta, payload in self._payloads(ANALYSIS_NODE_IDS):
            raw = payload.get("spherical_analysis")
            if raw is None:
                continue
            try:
                analysis = SphericalAnalysis.model_validate(raw)
            except ValidationError as exc:
                raise SphericalValidationError(
                    "Persisted spherical analysis failed schema validation.",
                    details={"artifact_id": meta.artifact_id, "error": str(exc)},
                ) from exc
            records[analysis.analysis_id] = AnalysisRecord(
                analysis=analysis,
                artifact_id=meta.artifact_id,
                updated_at=meta.created_at,
            )
        return sorted(records.values(), key=lambda record: record.updated_at)

    def get_analysis(self, analysis_id: str | None = None) -> SphericalAnalysis:
        records = self.list_analysis_records()
        if analysis_id is None:
            if not records:
                raise SphericalValidationError(
                    "No spherical analysis exists in the active session."
                )
            return records[-1].analysis
        for record in records:
            if record.analysis.analysis_id == analysis_id:
                return record.analysis
        raise SphericalValidationError(
            "Spherical analysis was not found in the active session.",
            details={"analysis_id": analysis_id},
        )

    def list_story_timeline_records(self) -> list[StoryTimelineRecord]:
        records: dict[str, StoryTimelineRecord] = {}
        for meta, payload in self._payloads(STORY_TIMELINE_NODE_IDS):
            raw = payload.get("normalized_story_timeline")
            if raw is None:
                continue
            try:
                timeline = NormalizedStoryTimeline.model_validate(raw)
            except ValidationError as exc:
                raise SphericalValidationError(
                    "Persisted normalized story timeline failed schema validation.",
                    details={"artifact_id": meta.artifact_id, "error": str(exc)},
                ) from exc
            records[timeline.timeline_id] = StoryTimelineRecord(
                timeline=timeline,
                artifact_id=meta.artifact_id,
                updated_at=meta.created_at,
            )
        return sorted(records.values(), key=lambda record: record.updated_at)

    def get_story_timeline(
        self,
        timeline_id: str | None = None,
    ) -> NormalizedStoryTimeline:
        records = self.list_story_timeline_records()
        if timeline_id is None:
            if not records:
                raise SphericalValidationError(
                    "No normalized spherical story timeline exists in the active session."
                )
            return records[-1].timeline
        for record in records:
            if record.timeline.timeline_id == timeline_id:
                return record.timeline
        raise SphericalValidationError(
            "Normalized spherical story timeline was not found.",
            details={"timeline_id": timeline_id},
        )

    def list_speaker_analysis_records(self) -> list[SpeakerAnalysisRecord]:
        records: dict[str, SpeakerAnalysisRecord] = {}
        for meta, payload in self._payloads(SPEAKER_ANALYSIS_NODE_IDS):
            raw = payload.get("speaker_candidate_analysis")
            if raw is None:
                continue
            try:
                analysis = SpeakerCandidateAnalysis.model_validate(raw)
            except ValidationError as exc:
                raise SphericalValidationError(
                    "Persisted speaker-candidate analysis failed schema validation.",
                    details={"artifact_id": meta.artifact_id, "error": str(exc)},
                ) from exc
            records[analysis.speaker_analysis_id] = SpeakerAnalysisRecord(
                analysis=analysis,
                artifact_id=meta.artifact_id,
                updated_at=meta.created_at,
            )
        return sorted(records.values(), key=lambda record: record.updated_at)

    def get_speaker_analysis(
        self,
        speaker_analysis_id: str | None = None,
    ) -> SpeakerCandidateAnalysis:
        records = self.list_speaker_analysis_records()
        if speaker_analysis_id is None:
            if not records:
                raise SphericalValidationError(
                    "No speaker-candidate analysis exists in the active session."
                )
            return records[-1].analysis
        for record in records:
            if record.analysis.speaker_analysis_id == speaker_analysis_id:
                return record.analysis
        raise SphericalValidationError(
            "Speaker-candidate analysis was not found.",
            details={"speaker_analysis_id": speaker_analysis_id},
        )

    def list_director_proposal_records(
        self,
        *,
        include_decided: bool = True,
    ) -> list[DirectorProposalRecord]:
        records: dict[str, DirectorProposalRecord] = {}
        for meta, payload in self._payloads(DIRECTOR_EVENT_NODE_IDS):
            raw_events = payload.get("director_proposal_events")
            if raw_events is None:
                singular = payload.get("director_proposal_event")
                raw_events = [singular] if singular is not None else []
            if not isinstance(raw_events, list):
                raise SphericalValidationError(
                    "Persisted director proposal events are invalid.",
                    details={"artifact_id": meta.artifact_id},
                )
            for event in raw_events:
                if not isinstance(event, dict):
                    raise SphericalValidationError(
                        "Persisted director proposal event is invalid.",
                        details={"artifact_id": meta.artifact_id},
                    )
                action = event.get("action")
                proposal_id = event.get("proposal_id")
                if action not in {"propose", "accept", "reject"}:
                    raise SphericalValidationError(
                        "Persisted director proposal event has an invalid action.",
                        details={"artifact_id": meta.artifact_id},
                    )
                if not isinstance(proposal_id, str) or not proposal_id:
                    raise SphericalValidationError(
                        "Persisted director proposal event has no proposal ID.",
                        details={"artifact_id": meta.artifact_id},
                    )
                prior = records.get(proposal_id)
                if action == "propose":
                    if prior is not None:
                        raise SphericalValidationError(
                            "A director proposal was proposed more than once.",
                            details={"proposal_id": proposal_id},
                        )
                    try:
                        proposal = DirectorProposal.model_validate(event.get("proposal"))
                    except ValidationError as exc:
                        raise SphericalValidationError(
                            "Persisted director proposal failed schema validation.",
                            details={"artifact_id": meta.artifact_id, "error": str(exc)},
                        ) from exc
                    if (
                        proposal.proposal_id != proposal_id
                        or proposal.status != DirectorProposalStatus.PROPOSED
                    ):
                        raise SphericalValidationError(
                            "Proposed director proposal identity or state is invalid.",
                            details={"proposal_id": proposal_id},
                        )
                    records[proposal_id] = DirectorProposalRecord(
                        proposal=proposal,
                        artifact_id=meta.artifact_id,
                        updated_at=meta.created_at,
                    )
                    continue
                if prior is None:
                    raise SphericalValidationError(
                        "Director decision has no prior proposal.",
                        details={"proposal_id": proposal_id},
                    )
                if prior.proposal.status != DirectorProposalStatus.PROPOSED:
                    raise SphericalValidationError(
                        "A director proposal can be decided only once.",
                        details={"proposal_id": proposal_id},
                    )
                status = (
                    DirectorProposalStatus.ACCEPTED
                    if action == "accept"
                    else DirectorProposalStatus.REJECTED
                )
                records[proposal_id] = DirectorProposalRecord(
                    proposal=prior.proposal.model_copy(update={"status": status}),
                    artifact_id=meta.artifact_id,
                    updated_at=meta.created_at,
                )
        result = sorted(records.values(), key=lambda record: record.updated_at)
        if not include_decided:
            result = [
                record
                for record in result
                if record.proposal.status == DirectorProposalStatus.PROPOSED
            ]
        return result

    def get_director_proposal(self, proposal_id: str) -> DirectorProposalRecord:
        for record in self.list_director_proposal_records(include_decided=True):
            if record.proposal.proposal_id == proposal_id:
                return record
        raise SphericalValidationError(
            "Director proposal was not found in the active session.",
            details={"proposal_id": proposal_id},
        )

    def list_suggestion_records(
        self,
        *,
        include_decided: bool = True,
    ) -> list[SuggestionRecord]:
        records: dict[str, SuggestionRecord] = {}
        for meta, payload in self._payloads(SUGGESTION_EVENT_NODE_IDS):
            event = payload.get("reframe_suggestion_event")
            if not isinstance(event, dict):
                continue
            action = event.get("action")
            suggestion_id = event.get("suggestion_id")
            if action not in {"propose", "accept", "reject"}:
                raise SphericalValidationError(
                    "Persisted reframe suggestion event has an invalid action.",
                    details={"artifact_id": meta.artifact_id},
                )
            if not isinstance(suggestion_id, str) or not suggestion_id:
                raise SphericalValidationError(
                    "Persisted reframe suggestion event has no suggestion ID.",
                    details={"artifact_id": meta.artifact_id},
                )
            prior = records.get(suggestion_id)
            if action == "propose":
                if prior is not None:
                    raise SphericalValidationError(
                        "A reframe suggestion was proposed more than once.",
                        details={"suggestion_id": suggestion_id},
                    )
                try:
                    suggestion = ReframeSuggestion.model_validate(event.get("suggestion"))
                except ValidationError as exc:
                    raise SphericalValidationError(
                        "Persisted reframe suggestion failed schema validation.",
                        details={"artifact_id": meta.artifact_id, "error": str(exc)},
                    ) from exc
                if (
                    suggestion.suggestion_id != suggestion_id
                    or suggestion.status != SuggestionStatus.PROPOSED
                ):
                    raise SphericalValidationError(
                        "Proposed suggestion identity or state is invalid.",
                        details={"suggestion_id": suggestion_id},
                    )
                records[suggestion_id] = SuggestionRecord(
                    suggestion=suggestion,
                    artifact_id=meta.artifact_id,
                    updated_at=meta.created_at,
                )
                continue
            if prior is None:
                raise SphericalValidationError(
                    "Suggestion decision has no prior proposal.",
                    details={"suggestion_id": suggestion_id},
                )
            if prior.suggestion.status != SuggestionStatus.PROPOSED:
                raise SphericalValidationError(
                    "A reframe suggestion can be decided only once.",
                    details={"suggestion_id": suggestion_id},
                )
            status = (
                SuggestionStatus.ACCEPTED
                if action == "accept"
                else SuggestionStatus.REJECTED
            )
            records[suggestion_id] = SuggestionRecord(
                suggestion=prior.suggestion.model_copy(update={"status": status}),
                artifact_id=meta.artifact_id,
                updated_at=meta.created_at,
            )
        result = sorted(records.values(), key=lambda record: record.updated_at)
        if not include_decided:
            result = [
                record
                for record in result
                if record.suggestion.status == SuggestionStatus.PROPOSED
            ]
        return result

    def get_suggestion(self, suggestion_id: str) -> SuggestionRecord:
        for record in self.list_suggestion_records(include_decided=True):
            if record.suggestion.suggestion_id == suggestion_id:
                return record
        raise SphericalValidationError(
            "Reframe suggestion was not found in the active session.",
            details={"suggestion_id": suggestion_id},
        )

    def list_track_records(self, *, include_deleted: bool = False) -> list[TrackRecord]:
        records: dict[str, TrackRecord] = {}
        for meta, payload in self._payloads(TRACK_EVENT_NODE_IDS):
            event = payload.get("camera_track_event")
            if not isinstance(event, dict):
                continue
            action = event.get("action")
            track_id = event.get("track_id")
            revision = event.get("revision")
            if action not in {"create", "update", "delete"}:
                raise SphericalValidationError(
                    "Persisted camera-track event has an invalid action.",
                    details={"artifact_id": meta.artifact_id},
                )
            if not isinstance(track_id, str) or not track_id:
                raise SphericalValidationError(
                    "Persisted camera-track event has no track ID.",
                    details={"artifact_id": meta.artifact_id},
                )
            if not isinstance(revision, int) or revision <= 0:
                raise SphericalValidationError(
                    "Persisted camera-track event has an invalid revision.",
                    details={"artifact_id": meta.artifact_id},
                )

            prior = records.get(track_id)
            if prior is not None and prior.deleted:
                raise SphericalValidationError(
                    "Camera-track event follows a deletion tombstone.",
                    details={"track_id": track_id, "artifact_id": meta.artifact_id},
                )

            if action == "delete":
                if prior is None:
                    raise SphericalValidationError(
                        "Camera-track deletion has no prior track in this session.",
                        details={"track_id": track_id},
                    )
                expected = prior.revision + 1
                if revision != expected:
                    raise SphericalValidationError(
                        "Camera-track revisions are not contiguous.",
                        details={
                            "track_id": track_id,
                            "expected_revision": expected,
                            "actual_revision": revision,
                        },
                    )
                if event.get("asset_id") != prior.track.asset_id:
                    raise SphericalValidationError(
                        "Camera-track deletion asset does not match prior revisions.",
                        details={"track_id": track_id},
                    )
                records[track_id] = TrackRecord(
                    track=prior.track,
                    revision=revision,
                    deleted=True,
                    artifact_id=meta.artifact_id,
                    updated_at=meta.created_at,
                )
                continue

            raw_track = event.get("track")
            try:
                track = VirtualCameraTrack.model_validate(raw_track)
            except ValidationError as exc:
                raise SphericalValidationError(
                    "Persisted virtual-camera track failed schema validation.",
                    details={"artifact_id": meta.artifact_id, "error": str(exc)},
                ) from exc
            if track.track_id != track_id or event.get("asset_id") != track.asset_id:
                raise SphericalValidationError(
                    "Camera-track event identity does not match its track payload.",
                    details={"track_id": track_id, "artifact_id": meta.artifact_id},
                )
            if action == "create" and prior is not None:
                raise SphericalValidationError(
                    "Camera-track create event follows an existing revision.",
                    details={"track_id": track_id},
                )
            if action == "update" and prior is None:
                raise SphericalValidationError(
                    "Camera-track update has no prior track in this session.",
                    details={"track_id": track_id},
                )
            if prior is not None and track.asset_id != prior.track.asset_id:
                raise SphericalValidationError(
                    "Camera-track update cannot change the imported asset.",
                    details={"track_id": track_id},
                )
            expected = 1 if prior is None else prior.revision + 1
            if revision != expected:
                raise SphericalValidationError(
                    "Camera-track revisions are not contiguous.",
                    details={
                        "track_id": track_id,
                        "expected_revision": expected,
                        "actual_revision": revision,
                    },
                )
            records[track_id] = TrackRecord(
                track=track,
                revision=revision,
                deleted=False,
                artifact_id=meta.artifact_id,
                updated_at=meta.created_at,
            )

        result = list(records.values())
        if not include_deleted:
            result = [record for record in result if not record.deleted]
        return sorted(result, key=lambda record: record.updated_at)

    def get_track(self, track_id: str) -> TrackRecord:
        for record in self.list_track_records(include_deleted=True):
            if record.track.track_id == track_id:
                if record.deleted:
                    raise SphericalValidationError(
                        "Virtual-camera track has been deleted.",
                        details={"track_id": track_id},
                    )
                return record
        raise SphericalValidationError(
            "Virtual-camera track was not found in the active session.",
            details={"track_id": track_id},
        )
