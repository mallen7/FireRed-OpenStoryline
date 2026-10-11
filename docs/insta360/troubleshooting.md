# Insta360 worker troubleshooting

## The 360° workspace says spherical editing is disabled

Set `[spherical].enabled=true` in the config loaded by both MCP and the web
server, then restart both processes. The UI intentionally does not enable a
server-side feature flag. Confirm the active config path before editing it.

## The workspace is empty after uploading a panorama

An ordinary upload is not automatically classified as spherical. Ask the agent
to run `import_spherical_media` on the stitched 2:1 MP4 in the same conversation,
then refresh the workspace. A valid asset is session-scoped and must have an
approximately 2:1 frame. Raw `.insv` is not accepted by the import node.

## Panorama viewer is blank but metadata loads

Check the browser console/network panel for the A-Frame 1.8.0 request and the
session preview URL. The viewer library is loaded from `aframe.io`; an offline or
filtered browser needs an operator-vendored approved copy. AFrame failure falls
back to a normal video element, which cannot provide drag-to-look 360 navigation.
HTTP 403 on preview means the source resolved outside configured media, output,
BGM, or server-cache roots (including through a symlink); fix the configured root
instead of weakening the guard.

## Saving a camera track reports a stale revision

Another UI or MCP mutation changed that track after the page loaded. Refresh,
review the newest revision, and reapply the intended edits. The optimistic
revision check prevents one editor from silently overwriting another.

## Preview works but a final render does not appear in ordinary media

The final endpoint first completes and validates the flat MP4, then copies it into
the session media directory. Check session media limits, free disk space, write
permission, FFmpeg output, and the error shown in the workspace. A failed copy
does not turn the render into a pending chat attachment. The validated render
remains in its session artifact directory for recovery.

## Viewer framing and final output differ at a motion boundary

The viewer interpolates the same camera schema for interactive review, while the
audited FFmpeg build renders animated paths as deterministic sampled segments
because runtime `v360` commands fail. Preview at a finer sample interval or add
explicit keyframes/cuts. Treat the final low-resolution preview as the approval
reference, not the WebGL frame alone.

## Capability output says the SDK is unavailable

This is the expected public state. Confirm `provider` is `stub`,
`sdk_enabled=false`, `can_decode_insv=false`, and `can_stitch=false`. Use a
Studio-exported 2:1 MP4, or move the raw job to an approved private SDK host.
Do not pass `.insv` to `import_spherical_media` or ordinary video nodes.

## SDK-enabled CMake configuration fails

- `host is unsupported`: build the real provider on Ubuntu 22.04 x86-64, not
  macOS/arm64 or a public container.
- `INSTA360_SDK_ROOT`: point it at the extracted operator-supplied SDK without
  copying that SDK into a tracked path.
- `ins_stitcher.h`: confirm the approved package layout and SDK version.
- `MediaSDK library`: pass the full local file with
  `-DINSTA360_SDK_LIBRARY=...` when discovery cannot find it.
- `INSTA360_MODEL_ROOT`: remove the setting when the request does not need it,
  or use a real approved model directory.

Never work around these checks with Insta360 Studio's private app libraries.

## Discovery proposes an uncertain or wrong pair

Discovery is advisory and always requires confirmation. Review filenames,
sizes, timestamps, marker evidence, proposed ordering, reasons, and warnings.
Submit only explicit ordered inputs known to belong to the same recording. An
ungrouped file is not automatically an error; some supported sources may be a
single file. A lone marker-bearing filename such as `*_00_*.insv` is proposed
as a low-confidence, confirmation-gated singleton because current cameras may
store both lens streams in one container while older cameras may require a
matching `*_10_*` file. Verify the camera model and recording completeness;
filename evidence alone cannot distinguish those cases.

## Stitch produces progress and then fails

Read the final structured `error` event and stable process exit code. Logs are
on stderr; SDK logs may be emitted beside the job output on a private build.
The requested final output is not reported as success, and the job-specific
partial file should be removed. Retry only when the error is marked retryable
and the underlying resource problem has been corrected.

## Cancellation does not complete promptly

The local client sends SIGTERM, waits its configured grace interval, then kills
the process. The worker maps SIGINT/SIGTERM to `CancelStitch` when supported.
Confirm the process exited and no job-specific `.partial.mp4` remains before
resubmitting to the same output path.

## The CLI reports success but Python import rejects the result

The Python client applies stricter postconditions: matching job/path/dimensions,
ordered source hashes, output existence/hash, and a successful ffprobe of an
exact 2:1 master. Treat rejection as a failed ingest. Preserve sanitized logs
and the structured result for diagnosis; do not manually insert the MP4 into a
`SphericalAsset`.

## Raw files appear in ordinary media discovery

That is a boundary regression. `.insv` must remain absent from normal video
suffixes, and only validated worker output may re-enter spherical import. Stop
the workflow and run the spherical node/worker tests before proceeding.

## Analysis reports no stable subjects

Review the detection overlay and annotated contact sheet before changing any
threshold. The default backend finds saturated visual candidates and is not a
semantic person recognizer. For dim or low-saturation footage, lower
`minimum_detection_confidence` cautiously; for fast subjects, shorten
`interval_seconds`. Every change creates a distinct cache key. Do not invent a
subject ID or silently describe a manual camera direction as detected.

## Analysis grid validation fails

Horizontal FOV must exceed the yaw step, pitch rows must overlap, and their
vertical fields must reach both zenith and nadir. Use the default 60-degree yaw
step, `[-45, 0, 45]` pitch rows, square views, and 100-degree horizontal FOV as
the safe baseline. View dimensions must be positive and even.

## One person becomes multiple subject tracks

Inspect seam events, occlusion gaps, source viewports, appearance evidence, and
warnings. Increase `max_track_angular_distance_degrees` only for legitimately
fast motion, or `max_occlusion_seconds` for a known short obstruction. Similar
people crossing closely can remain ambiguous with the lightweight histogram
embedder; keep both proposals separate or use an approved stronger backend.

## Different people were merged into one track

Reduce the deduplication angle or tracking distance and re-run analysis. The
cache key will change. Do not accept a subject-follow proposal until its
trajectory overlay and sample evidence are coherent; manual correction remains
safer than asserting identity.

## Suggestion preview does not appear in active camera tracks

That is the required behavior. Suggestion, inspection, and preview operations
cannot activate a camera plan. Call `accept_reframe_suggestion` explicitly only
after approval. A rejected or already-decided suggestion cannot be decided a
second time.

## Cached analysis is unexpectedly recomputed

The source hash, timestamps, entire view grid, detector/tracker parameters,
FFmpeg version, perspective generator, and analysis engine all participate in
the key. A cache entry is also rejected if any perspective frame or review
artifact is missing or has the wrong SHA-256. Preserve the cache for reuse, but
never bypass its validation after manual file edits.

## Transcript turns are missing or shifted

Pass the existing `local_asr` result without changing its millisecond fields.
`source_ref.start` supplies each clip's offset; sentence and word timestamps are
relative to that clip. Confirm split-shot `start`, `end`, and `duration` values
are also milliseconds and within the spherical asset duration. Out-of-range
items are dropped with alignment warnings. Do not rerun ASR just to repair a
unit mismatch.

## Speaker analysis is ambiguous or unresolved

This is a valid result, not a failed analysis. Inspect the ranked component
scores and warnings. Face visibility alone is not speech proof; when mouth
movement is unavailable its component remains zero and the analysis discloses
that fact. Improve real evidence or use a wide/non-identifying view. Do not
lower thresholds merely to force a named speaker.

## Director proposal uses a cut instead of a pan

Inspect the decision's motion report. A cut is required when pan speed,
acceleration, or FOV-change rate would exceed the selected constraints. If a
cut would also violate minimum cut spacing, the director holds the current
frame. Change constraints only when the footage and desired style justify the
new values; every override is persisted in the proposal.

## Director generation reports unsatisfied shot duration

Minimum and maximum shot durations must be ordered and compatible with source
length. The final short remainder is merged when that remains below the maximum.
A source shorter than the configured minimum is retained as one source-length
shot with a warning. For other failures, choose a profile appropriate to the
clip length or provide a complete validated motion-constraint object.

## Preview or comparison changed the active edit

That is a state-boundary regression. `generate_director_proposals`, comparison,
and preview may persist review artifacts but cannot append camera-track events.
Only `accept_director_proposal` creates revision 1. Stop and run the Phase 5
session tests if a track appears earlier.

## Final composition receives a spherical master

Stop the workflow. `render_director_proposal` must first produce a validated
flat MP4 and return `ordinary_media` plus `ordinary_clip`. Pass those objects to
normal timeline/final-render nodes. Neither a 2:1 master nor raw `.insv` is an
ordinary timeline source.

## Distributed worker returns 401

Every `/v1` route requires `Authorization: Bearer ...`; health routes do not.
Confirm the server reads either `OPENSTORYLINE_WORKER_TOKEN` or its root-readable
token file and that the Mac uses the environment variable named by
`spherical.worker_token_env`. Never print the value while debugging. Restart the
client/server after rotating it.

## Liveness passes but readiness returns 503

This distinction is intentional. Inspect the readiness `checks` object and run
`worker_service diagnose`. The durable database, scheduler, and output roots are
always required. Stitch capability is required only when
`OPENSTORYLINE_WORKER_REQUIRE_STITCH=1`; the public stub can remain ready for
reframe/analysis when that setting is false.

## Job submission says path is not allowed

Use paths visible on the worker and beneath the configured media/output roots.
Canonicalization follows symlinks, so a link that escapes a root is rejected.
Client-selected SDK model roots are always rejected. In a non-shared deployment,
upload `.insv`/`.mp4` first and use the returned worker path.

## Reusing Idempotency-Key returns HTTP 409

That key already identifies different normalized request JSON. Generate one
stable key per logical operation and reuse it only for exact retries. An exact
replay returns the original job, including a completed result after restart.

## SSE progress arrives only after completion

Disable reverse-proxy buffering for `text/event-stream`, extend proxy/read
timeouts beyond the longest job, and preserve the `Authorization` header. The
service sends heartbeat comments and `X-Accel-Buffering: no`. Persisted events
can be replayed with `after_sequence` after reconnecting.

## Cancellation stays in requested state

The task is being terminated as a process group and may use the configured
shutdown grace period before `cancelled`. With systemd retain
`KillMode=control-group`; with Docker retain `init: true`. If it remains running
after a service crash, restart recovery requeues or closes it according to retry
and cancellation state.

## Upload returns 413 or output download returns 409

413 means the configured upload/request bound was exceeded; prefer shared
storage for multi-gigabyte camera files or raise the explicit deployment limit.
409 on download means the persisted artifact size/hash no longer matches disk.
Restore the artifact from backup or rerun with a new idempotency key; do not
override validation.

## NVIDIA or SDK diagnostic is warning/failure

Verify `nvidia-smi` on the Ubuntu host and inside the container/VM, then check the
driver, NVIDIA container runtime, GPU passthrough, private SDK-enabled binary,
and model mount. The public binary truthfully reports stitch unavailable. Do not
set `REQUIRE_STITCH=1` until the licensed runtime passes private validation.

## SQLite state grows or reports locking problems

Keep the state directory on a local filesystem that supports SQLite locking,
not an eventually consistent object-store mount. Default history retention is
30 days/10,000 records; pruning never deletes media. Back up SQLite with its WAL
files or stop the service before copying. Attempt work directories are removed
after terminal jobs.
