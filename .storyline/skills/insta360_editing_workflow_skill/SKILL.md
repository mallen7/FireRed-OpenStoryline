---
name: insta360-editing-workflow-skill
description: Use for editing stitched Insta360 or other 360-degree equirectangular MP4 footage through deterministic import, panoramic inspection, assisted subject analysis, storyline-aware directing proposals, explicit approval, virtual-camera planning, preview, revision, and flat-clip rendering. Also use for raw .insv and local or authenticated distributed-worker capability, progress, cancellation, and safe fallback handling.
---

# Insta360 editing workflow

Keep spherical stitching, virtual-camera reframing, and ordinary video editing
as separate stages. Never send raw `.insv` or a 2:1 spherical master to normal
video nodes.

## Classify the input

- Raw `.insv`: never send it to a normal media node. Check the standalone
  worker's capabilities. If both `can_decode_insv` and `can_stitch` are true,
  use the operator-provided worker path with an explicit confirmed input
  ordering, then import only its validated stitched MP4. If it is false or
  unavailable, ask the user to export
  a stabilized, stitched 2:1 equirectangular MP4 through Insta360 Studio.
- Stitched approximately 2:1 MP4: use the spherical workflow below.
- Reframed ordinary MP4: use `load_media` and the normal OpenStoryline workflow.

## Run the spherical workflow

1. Call `import_spherical_media` with exactly one stitched MP4. Keep the returned
   `asset_id`; do not invent an asset or source hash.
2. Call `inspect_spherical_media`. Review the panoramic contact sheet and, when
   present, the front/right/back/left/up/down perspective sheet.
3. For assisted subject framing, call `analyze_spherical_asset`, then
   `list_detected_subjects` and `inspect_subject_track`. Treat lightweight-backend
   labels and identities as candidates, and report confidence, occlusions, seam
   crossings, and warnings.
4. Create a separate proposal with `suggest_subject_reframe`,
   `suggest_group_reframe`, or `suggest_establishing_shot`. Call
   `preview_reframe_suggestion` before deciding. A suggestion is not active.
5. Call `accept_reframe_suggestion` only after explicit user approval; this is
   the only suggestion action that creates a normal camera track. Otherwise call
   `reject_reframe_suggestion`. Never imply previewing accepted the proposal.
6. For manual framing, translate the user's direction into explicit keyframes
   and call `create_virtual_camera_track`.
7. Call `validate_virtual_camera_track` for accepted, stored, or edited plans.
8. Call `preview_virtual_camera_track` before a full render.
9. If framing needs revision, call `update_virtual_camera_track` with the stored
   `track_id`, then validate and preview again.
10. Call `render_reframed_clip` only after the plan is acceptable.
11. Call `load_media`; the session's rendered flat MP4 will enter the ordinary
   split, understand, timeline, and final-render workflow.

## Use the distributed worker safely

When `spherical.worker_transport` is `http`, use the configured authenticated
worker client. Never ask for or echo the bearer token; it comes from the named
environment variable. Check `/health/ready` and capabilities before raw ingest.

- Use one stable idempotency key for retries of the same normalized request.
  Never reuse it for different inputs, output settings, or destinations.
- For shared storage, submit only paths visible on the worker. For non-shared
  inputs, upload `.insv` or `.mp4`, verify the returned SHA-256, and use the
  returned worker path.
- Stream and report persisted job progress. On user cancellation, call the job
  cancellation endpoint and wait for a terminal `cancelled` state.
- Treat `failed`, `timed_out`, and `cancelled` as truthful terminal results. Do
  not silently run a lower-quality local substitute.
- Download only declared artifacts and retain SHA-256 validation. Never bypass
  an invalid-path, size-limit, readiness, or artifact-integrity error.
- A live worker is not necessarily ready. If stitch capability is unavailable,
  preserve the Insta360 Studio export fallback.

## Use storyline-aware direction

Use this path only after spherical analysis and after existing OpenStoryline ASR,
scene, clip-understanding, script, or timeline results are available. Do not run a
second transcription operation.

1. Call `list_director_profiles` and choose the profile matching the material.
2. Call `build_spherical_story_timeline`, passing the existing `local_asr`,
   split-shot, clip-understanding, script, and timeline outputs. Pass mouth or
   audio activity only when actually available.
3. Call `analyze_speaker_candidates`. Treat `probable` as evidence-backed but
   not identity confirmation. Disclose every `ambiguous` or `unresolved` turn
   and retain its alternative candidates.
4. Call `generate_director_proposals` with at least two of speaker-focused,
   cinematic, minimal-motion, high-energy, and vertical-social. Every decision
   must retain its score components, motion constraints, rationale, confidence,
   and warnings.
5. Call `compare_director_proposals`, then `preview_director_proposal` for the
   serious candidates. A ranking or preview is not approval.
6. Call `accept_director_proposal` only after explicit user selection; otherwise
   call `reject_director_proposal`. Only acceptance creates an active camera
   track.
7. Call `render_director_proposal` only for an accepted proposal. It returns an
   ordinary flat MP4 plus normal media/clip objects for OpenStoryline timeline
   planning. Never hand a spherical master or raw `.insv` to `render_video`.

Prefer a cut when a target change would violate pan speed, acceleration, FOV
rate, or cut-spacing constraints. Never conceal a low-confidence speaker guess
behind an apparently certain close-up.

Use `list_virtual_camera_tracks` to recover IDs or compare revisions. Use
`delete_virtual_camera_track` only when the user asks to discard a plan; deletion
creates an auditable tombstone and does not erase history.

## Translate direction into coordinates

Use yaw `0` for front/stage-forward, `90` for right, `180` for back, and `-90`
for left. Use positive pitch for upward/elevated framing and negative pitch for
downward/nadir framing. Horizontal FOV near 100-120 is wide; near 55-75 is a
tighter view.

Examples:

- "Point toward the stage": create a static keyframe at the stage yaw observed
  in the inspection sheet.
- "Start facing forward and pan right over five seconds": keyframe yaw `0` at
  the start and yaw `90` five seconds later.
- "Use a wide view for the first ten seconds": cover that interval with a wide
  horizontal FOV and explicit boundary keyframes.
- "Make a vertical version focused at yaw 40": use a 9:16 output and keyframes
  centered at yaw `40`.
- "Render this camera plan as a preview": call the preview tool, not the final
  renderer.

## Preserve deterministic validation

- Pass camera plans only through the typed camera-track tools. Never bypass or
  weaken schema errors.
- Keep keyframes ordered and inside the source interval.
- Let yaw and roll normalize across `180/-180`; do not expand a seam crossing
  into the long rotation.
- Use only even 16:9, 9:16, or 1:1 output dimensions.
- Keep outputs and artifact IDs in the active session. Never reuse another
  session's asset or track IDs.
- Report renderer warnings, fallback decisions, and cache status rather than
  implying a continuous camera path or quality level that was not delivered.
