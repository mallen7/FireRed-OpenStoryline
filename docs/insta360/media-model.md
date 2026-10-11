# Spherical media model

## Media classes

The extension uses three deliberately distinct media classes:

1. Raw Insta360 source (`.insv`): proprietary camera input. Only the optional
   standalone worker may inspect or stitch it. Normal OpenStoryline nodes must
   reject it with a capability message.
2. Spherical master: stabilized, stitched, approximately 2:1 equirectangular
   video. It is the interchange format for import, analysis, planning, and
   deterministic reframing.
3. Flat clip: an ordinary 16:9, 9:16, or 1:1 render from an approved virtual
   camera track. Existing OpenStoryline nodes handle it normally.

Changing a file extension does not change its class; the probe and versioned
metadata establish the contract.

## Contract versioning

`VersionedSphericalModel` is a strict Pydantic base with
`schema_version: Literal["1.0"]` and extra fields forbidden. Phase 1 implements
`SphericalAsset`, `CameraKeyframe`, `VirtualCameraTrack`,
`ReframeRenderRequest`, `ReframeRenderManifest`, supporting enums/settings, and
their emitted JSON Schemas under `docs/insta360/schemas/`.

Readers must reject unsupported major versions. Additive compatible fields
require a documented minor-version policy; never reinterpret an existing field
in place. Manifests retain the exact input schema versions they consumed.

## Canonical coordinates and time

- Yaw is degrees in `[-180, 180]` after normalization.
- Pitch is degrees in `[-90, 90]` and is validated, not wrapped.
- Roll is normalized to `[-180, 180]`.
- Horizontal field of view is degrees and must be strictly within the renderer's
  supported interval.
- Spherical contract time uses floating-point seconds.
- Existing OpenStoryline timeline boundaries use milliseconds; conversion is
  explicit at the integration boundary.
- Equirectangular longitude maps to yaw and latitude maps to pitch.
- Interpolation from yaw 179 to -179 follows the two-degree seam crossing, not
  the 358-degree long path.

## Core records

### SphericalAsset

Identifies source paths/hashes, projection, master/proxy locations, dimensions,
frame rate, duration, audio stream, camera hints, ingest provider,
stabilization, and stitch type. Phase 1 accepts only an approximately 2:1
equirectangular master and validates its content hash before rendering.

Phase 3 worker import makes the stitched MP4 the first source path/hash because
it is the renderer's immutable master, followed by the ordered raw `.insv`
paths and their hashes as provenance. The embedded worker result records the
provider and normalized stitch settings. Before creating the asset, the local
client independently re-hashes every source and output, verifies request/result
identity, and ffprobes an exact 2:1 final master. Raw paths never become normal
OpenStoryline media items.

### CameraKeyframe and VirtualCameraTrack

A keyframe records time, yaw, pitch, roll, horizontal FOV, easing, optional
subject ID, and optional confidence. A track binds ordered keyframes to one
asset and source interval plus output geometry, creator (`manual`, `assisted`,
or `director`), and generation metadata.

Automatic and assisted tracks are proposals until explicitly accepted. Editing
creates a new revision rather than mutating the provenance record invisibly.
Phase 2 implements create/update/delete as session artifact events, requires
contiguous revisions, and reconstructs only the latest non-deleted track for
current-use tools. Tombstones remain in history.

### PerspectiveFrame, SphericalDetection, and SubjectTrack

A Phase 4 perspective frame binds one cached image to its exact timestamp,
viewport ID, center yaw/pitch, horizontal/vertical FOV, dimensions, and hash.
View-local normalized boxes are projected to canonical spherical yaw/pitch and
angular extents. A `SphericalDetection` retains confidence, candidate kinds and
labels, detector backends, source viewports, appearance evidence, and the count
of observations reconciled across overlapping views.

A `SubjectTrack` contains ordered visible, occluded, and lost samples plus
angular size, confidence, identity evidence, explicit occlusion intervals,
seam-crossing events, and warnings. Subject IDs are stable within one analysis
when spherical distance and lightweight appearance evidence support the match;
they are not claims about a real person's identity across assets or sessions.

### SphericalAnalysis and ReframeSuggestion

`SphericalAnalysis` records source/cache hashes, timestamps, every perspective
mapping, deduplicated detections, subject tracks, shot boundaries, backend and
normalized parameters, confidence summary, review-artifact paths/hashes,
warnings, and cache state.

`ReframeSuggestion` contains a normal schema-valid `VirtualCameraTrack`, but it
is a separate proposed artifact with policy, selected subjects, analysis
backend, confidence, parameters, trajectory path, and warnings. Suggestion and
preview artifacts are never interpreted as active camera-track events. Only an
explicit acceptance event creates revision 1 of the embedded assisted track;
rejection changes suggestion state without creating a track.

### NormalizedStoryTimeline and SpeakerCandidateAnalysis

`NormalizedStoryTimeline` connects existing OpenStoryline ASR sentences and
word timestamps, split-shot/analysis scene boundaries, spherical subjects,
face/audio/optional-mouth activity, clip understanding, scripts, and timeline
plans on one absolute source clock. It records source counts and alignment
warnings. Existing millisecond records are converted exactly once at this
boundary; spherical records remain seconds.

`SpeakerCandidateAnalysis` stores one ordered assessment per transcript turn.
Each candidate retains component scores for voice, visible face, mouth movement,
continuity, scene, previous speaker, turn boundary, and track confidence.
`probable` is an evidence state, not confirmed identity. Ambiguous or unresolved
turns select no speaker and preserve ranked alternatives and warnings.

### EditingProfile, DirectorProposal, and DirectorComparison

An `EditingProfile` is a reusable policy for shot lengths, close/medium/wide FOV,
pan limits, speaker/reaction/establishing behavior, vertical output, confidence,
nadir/seam avoidance, and music/subtitle handoff. The six checked-in profiles
are versioned public contracts.

A `DirectorProposal` contains ordered decisions, all eleven required score
components, transition intent, confidence, alternatives, rationale, motion
constraints/validation, review JSON, and a normal `VirtualCameraTrack` marked
`created_by=director`. It is not active until a separate acceptance event
creates camera-track revision 1. A `DirectorComparison` ranks at least two
same-timeline proposals with structured strengths and tradeoffs but cannot
activate one.

### TimelineSourceReference and DirectorTimelineHandoff

`TimelineSourceReference` distinguishes an ordinary flat source, a spherical
asset plus approved camera track, and a generated reframe artifact. The final
handoff always resolves the latter two to a validated flat MP4 before ordinary
composition. `DirectorTimelineHandoff` records the proposal, generated path and
hash, render manifest, ordinary media/clip IDs, and music/subtitle policy. Raw
`.insv` is never a valid timeline reference.

### ReframeRenderManifest

The manifest records source hashes, normalized track hash, FFmpeg version,
exact argument vector and filter graph, output hash, render duration, codec and
hardware settings, application/renderer versions, creation time, warnings, and
fallback decisions. A successful output without its matching manifest is not a
valid cache entry.

### Distributed job contracts

`StitchJobSubmission`, `ReframeJobSubmission`, and `AnalyzeJobSubmission` wrap
the existing operation contracts with timeout and bounded retry policy. They do
not add alternate stitch, render, or analysis parameters.

`WorkerJobRecord` is the durable state machine: UUID, operation, queued/running/
terminal state, idempotency key, normalized request hash, progress, attempts,
timeout/cancel state, structured result or error, validated artifacts, metrics,
and UTC timestamps. `WorkerJobEvent` is an ordered append-only transition or
progress record. Terminal success requires all declared `WorkerArtifact` paths,
sizes, and SHA-256 values to validate beneath configured output roots.

`DistributedWorkerCapabilities` describes API version, supported operations,
auth/event transport, transfer modes, request/queue/concurrency limits, and the
nested standalone stitch capability. `WorkerHealthResponse` separates live,
ready, and not-ready state. `WorkerMetricsResponse` contains aggregate values;
`WorkerDiagnosticReport` contains pass/warn/fail checks without SDK file listings
or secrets.

The checked-in distributed schemas live with the C++ protocol schemas under
`services/insta360-worker/schemas`. SQLite is the storage representation, not a
public contract; callers use these JSON models and API version `1.0`.

## Enforced Phase 1 invariants

- Dimensions and durations are positive.
- The source interval is ordered and within the asset duration.
- Keyframes are nonempty, ordered, and inside the source interval.
- Pitch/FOV are valid; yaw and roll are normalized.
- Projection values are allowlisted; Phase 1 accepts equirectangular input.
- Output dimensions match an approved aspect or an explicitly supported custom
  policy.
- Resolved output paths remain inside the configured workspace.
- JSON serialization is deterministic before hashing.
- H.264 output dimensions are even and use 16:9, 9:16, or 1:1.
- Render manifests contain the selected backend, exact commands/filter graphs,
  hashes, versions, audio decision, warnings, and fallback decisions.

## Phase 2 node ownership and review media

The standalone Phase 1 CLI has no OpenStoryline session. Phase 2 nodes bind
every imported asset, track event, preview, and render to the active existing
OpenStoryline session and artifact ID. Asset and track lookup never scans a
different session.

An imported spherical master remains a spherical reference and is excluded
from generic media discovery. A proxy is also review-only. Panoramic and
perspective contact sheets are inspection artifacts, not normal source clips.
Only a validated flat preview or full reframe becomes an ordinary media item;
completed full renders are injected into the current session's `load_media`
handoff.
