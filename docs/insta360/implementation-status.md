# Insta360 implementation status

Last updated: 2026-07-25 at upstream commit
`04297707e7607dd398e906262235d0797068e7b4`.

| Phase | Status | Delivered |
| --- | --- | --- |
| 0 - Baseline, audit, scaffolding | Complete in working tree | Reproducible dependency baseline, full architecture/audit docs, six ADRs, disabled flags, typed error/version primitives, worker boundary, ignore rules, fixture plan, and focused tests |
| 1 - Deterministic reframing | Complete in working tree | Strict versioned contracts and JSON Schemas, seam-safe geometry, capability-probed FFmpeg renderer, validated content-addressed cache, dependency-free fixture generator, developer CLI, manifests, docs, and integration tests |
| 2 - Nodes and MCP | Complete in working tree | Feature-gated import/inspection, revisioned virtual-camera CRUD and validation, preview/full render, flat-media handoff, strict MCP schemas, workflow skill, and focused tests |
| 3 - MediaSDK worker | Complete in working tree; private runtime unverified | Eight strict protocol schemas, C++20 stub/SDK providers, discovery, JSON/JSONL CLI, signals/cancellation, CMake/CI/Docker controls, subprocess client, automatic asset import, and tests; this Mac has no licensed SDK package |
| 4 - Assisted analysis | Complete in working tree | Cached overlapping perspective analysis, pluggable CPU backends, spherical detection mapping/deduplication, seam-aware subject tracks, nine MCP tools, all requested camera policies, review media, and explicit suggestion decisions |
| 5 - Storyline-aware direction | Complete in working tree | Existing-ASR/story timeline normalization, explainable speaker candidates, constrained virtual director, six profiles, five comparison variants, nine MCP tools, explicit proposal decisions, previews, and ordinary timeline handoff |
| 6 - Distributed worker | Complete in working tree; private GPU runtime unverified | Authenticated FastAPI service, SQLite durable jobs/events, retries/idempotency/recovery, isolated cancellation, secure path/transfer policy, remote client, metrics/diagnostics, 19 worker schemas, and Ubuntu/NVIDIA deployment assets |
| 7 - UX and release | Complete in working tree; private raw-ingest release gate remains external | Session-scoped panoramic review and camera editing, shared UI/MCP contracts, 12-case regression matrix, performance harness, upstream-sync policy, release documentation, and operator checklist |

## Phase 0 acceptance matrix

| Criterion | Evidence/status |
| --- | --- |
| Existing behavior unchanged | Spherical flags default off; no node is registered and ordinary media code is untouched. MCP and web baseline smoke tests succeeded. |
| Baseline commands/results documented | See `development.md`, including the zero-test upstream status and service probes. |
| Extension points identified | See the audit in `architecture.md`. |
| No proprietary files tracked | Ignore rules cover SDK/model/vendor/raw-camera paths; the worker scaffold contains documentation only. |
| New modules import | Covered by `tests/test_spherical_scaffolding.py` and the documented import command. |
| Complete planned architecture | Component/data flow, trust, storage, cache, errors, and local/remote modes are in `architecture.md`. |
| Existing test status known | Upstream had no test directory or lint/type configuration; Phase 0 adds focused unittest coverage. |

## Phase 1 acceptance matrix

| Criterion | Evidence/status |
| --- | --- |
| Synthetic equirectangular input | CLI generator produces a labeled 2:1 H.264 MP4 with two moving references, a tone, asset JSON, and three tracks without committing media. |
| Static yaw/pitch/FOV and 16:9 render | FFmpeg integration test renders 320x180 using a static `v360` graph and validates dimensions, duration, audio, output hash, and sidecar. |
| Two moving-camera keyframes | Integration test renders four frame-aligned camera segments from a two-keyframe track. |
| Seam follows shortest path | Unit tests prove 170 to -170 moves 20 degrees and samples 180 at midpoint, including the reverse -180 midpoint. |
| 16:9, 9:16, and 1:1 | Contracts accept all three; tests render square output and verify portrait command generation. |
| Source ranges and audio | A 0.5-1.5 second square render validates one-second duration and the preserved audio stream; the full static output retains 48 kHz audio. |
| Failure cleanup | A deliberately invalid source hash raises a typed validation error and leaves neither output nor sidecar. |
| Validated caching | A repeated moving-camera request to another destination returns `cache_hit=true`, re-hashes the cache entry, and produces the identical output SHA-256. |
| Test coverage | Standard-library tests cover models, geometry/easing/seam, deterministic JSON/schema export, command generation, fixtures, FFmpeg rendering, audio, caching, and cleanup. |

## Phase 2 acceptance matrix

| Criterion | Evidence/status |
| --- | --- |
| Stitched-master import | `import_spherical_media` accepts one allowed-root MP4, probes it, verifies an approximately 2:1 equirectangular frame, hashes the source, records codec/audio/color metadata, and optionally creates a content-addressed proxy. |
| Inspection artifacts | `inspect_spherical_media` returns compact metadata plus a timestamped panorama contact sheet and optional front/right/back/left/up/down perspective sheets. Cached artifacts are re-hashed and image-validated before reuse. |
| Virtual-camera lifecycle | Create, update, validate, list, and delete tools use strict schemas. Create/update/delete are immutable artifact events; the current session view replays contiguous revisions and preserves provenance. |
| Natural-language camera direction | Tool descriptions and the workflow skill translate requests such as stage view, pan right, wide, vertical, yaw 40, and preview into explicit validated camera-track JSON. LLM output remains advisory until schema validation. |
| Preview and full render | Preview uses the Phase 1 renderer at a configured bounded dimension; full render retains the track's approved geometry. Both return normal MP4 media records with a compact manifest summary. |
| Normal-media handoff | The session interceptor excludes imported spherical masters from ordinary discovery and injects the session's rendered flat MP4s into the existing `load_media` workflow. Ordinary non-spherical loading is unchanged. |
| Raw-file boundary | `.insv` remains absent from normal media suffixes. Spherical import directs raw footage through a capable standalone worker, with Insta360 Studio export as the unavailable-SDK fallback. |
| Feature gate and MCP surface | With `spherical.enabled=false`, existing tools remain and every spherical tool is omitted. Phase 2 contributes nine allowlisted tools and Phase 4 adds nine more through the same registry/MCP wrapper. Direct MCP calls without the internal agent artifact ID receive and persist a server-generated ID. |
| Session isolation | Asset and track reads filter the existing `ArtifactStore` metadata by session. Tests show another session cannot list the first session's tracks. Source and output paths are canonicalized beneath configured roots. |
| Test coverage | Phase 2 tests exercise import, cached inspection, track CRUD/revisions, seam validation, preview, render, normal loader compatibility, session isolation, raw-file handling, path escape rejection, feature gating, MCP schemas, and skill content. |

## Phase 3 acceptance matrix

| Criterion | Evidence/status |
| --- | --- |
| Versioned worker protocol | Strict Pydantic contracts and checked-in JSON Schemas cover probe request/response, stitch request/progress/result, error, capabilities, and discovery. C++ rejects unknown fields, unsupported versions, invalid UUIDs/codecs/paths, and output that is not positive, even, and exactly 2:1. |
| Provider abstraction | `StitchProvider` has explicit `StubStitchProvider` and compile-gated `Insta360SdkStitchProvider` implementations. The stub reports `can_decode_insv=false` and never creates a fake stitched output. |
| SDK adapter | Source follows the official `ins_stitcher.h` Linux example for environment/model/log setup, ordered inputs, output/codec/bitrate, FlowState, direction lock, stitch type/fusion, callbacks, cancellation, and documented error mapping. A public official-API test double proves the adapter source compiles; licensed header/library/runtime validation remains unavailable on this Mac. |
| Source discovery | Directory scanning returns explicit proposed groups with filenames, sizes, timestamps, `_00_`/`_10_` evidence, ordering, confidence, reasons, warnings, and `requires_confirmation=true`. A lone marker-bearing file is now surfaced as an explicitly ambiguous, low-confidence singleton instead of being discarded as an incomplete pair. Filename evidence alone never starts a stitch. |
| CLI transport | `capabilities`, `discover`, `probe`, and `stitch --request` emit UTF-8 JSON on stdout, logs on stderr, JSON Lines progress, and stable exit codes. SIGINT/SIGTERM request SDK cancellation. Errors/cancellation remove the job-specific partial output; success commits only a validated nonempty staged file. |
| Public/private build split | CMake defaults to SDK off; the public Dockerfile and GitHub Actions workflow build/test only the stub. SDK-on configuration requires Linux x86-64, an operator SDK root, `ins_stitcher.h`, a MediaSDK library, and a valid supplied model root. Ignored vendor paths remain unchanged. |
| Python client | The transport-neutral interface and local subprocess client enforce provider capabilities before work, probe/discover, stream progress, terminate/kill on cancellation or timeout, parse structured errors, and validate provider, job identity, paths, dimensions, normalized settings, source hashes, output hash, and output existence. |
| Automatic asset import | A successful test-only transport produces a real generated 2:1 MP4; `stitch_and_import` ffprobes it and returns a renderer-compatible `SphericalAsset` containing both master and ordered raw provenance. |
| Ordinary media isolation | `.insv` remains outside normal video suffixes. The public worker stub and Python client tests never pass raw files to normal OpenStoryline nodes. |
| Validation | Local CMake build and all three CTests pass, including six CLI cases, SHA-256 known vector, and SDK-adapter compilation. The full Python suite passes with worker schema, progress, error, cancellation, and successful-import coverage. |

## Phase 4 acceptance matrix

| Criterion | Evidence/status |
| --- | --- |
| Overlapping analysis views | A configurable full-sphere grid validates horizontal and vertical coverage/overlap. Each cached perspective frame records timestamp, viewport ID, center yaw/pitch, horizontal/vertical FOV, dimensions, path, and SHA-256. |
| Pluggable backends | Protocols define object/person/face detectors, subject tracker, shot-boundary detector, and visual embedder. The default dependency-light CPU bundle uses color saliency, conservative YCbCr face candidates, RGB histograms, histogram shot boundaries, and seam-aware greedy tracking; no model download or GPU is required. |
| Spherical mapping and deduplication | Rectilinear rays map boxes to canonical yaw/pitch and angular size. Weighted unit-vector merging handles the 180/-180 seam, appearance similarity, source viewport evidence, and duplicate detector/view observations. |
| Subject tracking | Versioned subject tracks include visible/occluded/lost samples, confidence, angular size, identity evidence, occlusion gaps, seam-crossing events, and warnings. Tests retain one identity through a seam crossing and temporary occlusion. |
| Suggested camera policies | Normal `VirtualCameraTrack` proposals cover selected-subject center, rule of thirds, largest visible person, wide establishing, subject-to-subject pan, static direction, adaptive group framing, nadir avoidance, and stitch-seam avoidance/warnings. Every track records backend, confidence, parameters, policy, and warnings. |
| Review artifacts | Analysis produces a validated detection-overlay MP4, annotated perspective contact sheet, complete trajectory JSON, confidence summary, warnings, and manifest. Suggestions produce camera-trajectory JSON and use the deterministic renderer for low-resolution previews. |
| Explicit approval | Suggestion events are separate from camera-track events. Suggesting and previewing leave the active-track repository empty; only `accept_reframe_suggestion` appends revision 1 of a normal assisted track. Rejection is auditable and never creates a track. |
| MCP surface | The existing registry exposes exactly the nine requested Phase 4 tools when `spherical.enabled=true`; disabled-mode tests prove the ordinary tool surface remains unchanged. |
| Cache and validation | The analysis key includes source content, timestamps, view grid, backend/settings, FFmpeg, projection generator, and engine versions. Every cache hit re-hashes all perspective frames and review artifacts before reuse. |
| Verification | Schema export and compile checks pass; 10/10 focused Phase 4/MCP tests, 51/51 full Python tests, 3/3 native worker tests, dependency integrity, and workflow-skill validation all pass. |

## Phase 5 acceptance matrix

| Criterion | Evidence/status |
| --- | --- |
| Transcript/timeline alignment | Existing `local_asr` sentence and word timestamps are converted from clip-relative milliseconds to the spherical source clock. Scene boundaries, subject/face/audio/optional-mouth activity, clip captions, scripts, and existing timeline-plan records become ordered versioned signals with source counts and warnings. ASR is not rerun. |
| Speaker candidates | Every transcript turn ranks visible subjects with voice, face, mouth, continuity, scene, previous-speaker, turn-boundary, and track-confidence components. Weak scores or margins produce `ambiguous`/`unresolved`, select nobody, retain alternatives, and disclose missing evidence. |
| Explainable director | Every decision stores the requested eleven score components, total, subject/alternatives, close/medium/wide FOV, cut/pan/hold, intent, confidence, rationale, and warnings. Proposals contain normal director-created camera tracks but remain inactive. |
| Motion constraints | Pan speed, acceleration, minimum/maximum shot duration, cut spacing, FOV-change rate, subject dead-zone, and maximum low-confidence duration are validated. The integration test constrains pan speed to one degree/second and proves an over-speed move is converted to a cut. |
| Profiles | Versioned profiles exist for round-table podcast, family travel vlog, theme-park vlog, walking tour, restaurant review, and vehicle conversation, including music/subtitle handoff policy. |
| Alternatives/comparison | Speaker-focused, cinematic, minimal-motion, high-energy, and vertical-social variants are available. One call generates at least two; structured comparison ranks score, confidence, shots, cuts, pans, strengths, and tradeoffs. |
| Review and explicit approval | Camera/score JSON and low-resolution previews are inspectable. Generation, comparison, and preview append no camera track. Only `accept_director_proposal` creates revision 1; rejection remains auditable. |
| Ordinary timeline handoff | An accepted proposal renders through the existing FFmpeg spherical renderer into a flat H.264/AAC MP4. The returned ordinary media/clip objects are exercised successfully by the existing `PlanTimelineNode`; normal composition never sees raw `.insv` or a 2:1 master. |
| MCP surface | Nine Phase 5 nodes use the existing registry, strict schemas, artifact store, session isolation, and disabled-by-default spherical feature gate. |
| Verification | Public schema export and compilation pass; 12/12 focused Phase 4/5/MCP tests, 53/53 full Python tests, 3/3 native worker tests, dependency integrity, and workflow-skill validation all pass. |

## Phase 6 acceptance matrix

| Criterion | Evidence/status |
| --- | --- |
| HTTP transport | Authenticated FastAPI routes implement capabilities; stitch/reframe/analyze submission; durable job state; SSE event replay; cancellation; live/ready health; metrics; diagnostics; bounded uploads; and validated artifact downloads. `HttpSphericalWorkerClient` submits typed jobs, streams events, cancels, and checks shared/downloaded artifacts. |
| Durable jobs | SQLite transactionally stores UUIDs, normalized request hashes, unique idempotency keys, state, attempts, delayed retries, timeouts, cancellation, results, errors, metrics, validated artifacts, and append-only events. Restart tests requeue an interrupted attempt and preserve a completed result/artifact across another store construction. |
| Cancellation and isolation | Every attempt runs as a fixed argument-vector child process in a new process group. HTTP cancellation terminates a deliberately blocked stitch task and its fake worker without producing output. Shutdown leaves the durable running state for startup recovery. |
| Artifact and cache integrity | Success requires every returned artifact to remain beneath allowed output roots and match persisted size/SHA-256. Exact idempotent replays return the original job; real reframe/analysis retain their existing validated content caches and cache-hit metrics. Attempt work directories are cleaned separately from durable history and media. |
| Security | Constant-time bearer verification protects all `/v1` data routes. JSON/upload sizes, queue/concurrency, timeout and retry counts are bounded. Canonical roots reject traversal and symlink escapes; clients cannot choose model roots or shell/FFmpeg arguments. Upload commit is no-overwrite atomic, downloads re-hash, structured details are redacted, and SDK logs are sanitized before becoming artifacts. |
| Real operation reuse | HTTP integration tests execute the existing C++ stitch boundary through a fake provider, the existing FFmpeg `SphericalRenderer`, and the existing `SphericalAnalyzer`; Phase 6 introduces no second media implementation and Python never loads MediaSDK. |
| Health and observability | Liveness is process-only. Readiness checks SQLite, scheduler, writable outputs, and actual required FFmpeg capabilities, optionally requiring stitch readiness. Structured JSON logs and metrics record state/operation/failure counts, durations, stitch/reframe FPS, cache hits, output bytes, GPU availability/memory, worker version, and sanitized SDK capability. |
| Compatibility diagnostics | The CLI/API checks OS, CPU architecture, FFmpeg/version/filters, NVIDIA visibility, driver/CUDA signals, optional Vulkan tooling, worker/SDK capability, model configuration, writable directories, network reachability, and API-version compatibility without enumerating proprietary contents. |
| Deployment | Public `Dockerfile.http`, Compose, systemd, and environment examples contain no proprietary files. Documentation covers Ubuntu 22.04, NVIDIA runtime, Proxmox passthrough, shared storage, HTTP transfer, TLS/firewall, backups, history/work cleanup, and private SDK binary/model mounts. Compose validates locally; the public Ubuntu 22.04 image builds from the allowlisted context, passes its native tests, starts healthy, reports ready for reframe/analysis, requires bearer authentication, and truthfully reports stub stitch capability. |
| Verification | Distributed schemas and compilation pass; 18/18 focused Phase 6/worker tests, 64/64 full Python tests, 3/3 native worker tests, dependency integrity, Compose/YAML validation, workflow-skill validation, compatibility diagnostics, and a live public-container smoke test all pass. The licensed Ubuntu/NVIDIA MediaSDK runtime remains external and unverified. |

## Phase 7 acceptance matrix

| Criterion | Evidence/status |
| --- | --- |
| Understandable spherical workspace | The existing web app now exposes a session-scoped 360-degree workspace with an A-Frame panoramic preview, spherical badge, source/proxy/stitch/worker metadata, warnings, track list, suggestions, subjects, renders, and explicit loading/empty/disabled/error states. |
| Manual camera editing | The editor exposes timestamp, yaw, pitch, roll, horizontal FOV, easing, subject/confidence, output aspect, add/delete keyframe, scrub/play, undo/redo, save, segment preview, final render, and suggestion/director acceptance without requiring JSON editing. |
| Shared UI/MCP contracts | Strict versioned UI requests call the existing spherical nodes and append the same asset, track, suggestion, director, and render events used by MCP. Schema tests compare every generated UI schema with the checked-in copy. Optimistic revisions reject stale browser writes. |
| Ordinary-media handoff | A completed flat render is copied into the existing session media library and remains loadable by the unchanged `LoadMediaNode`. Imported spherical masters and previews do not leak into ordinary discovery. |
| Required regression matrix | `release-test-matrix.md` maps all 12 required paths to executable Python, JavaScript, native, or distributed-worker evidence. The full Python suite passes 67/67 and the editor model passes 3/3 Node tests. |
| Performance qualification | `scripts/benchmark_spherical.py` generates disposable media and measures proxy/static/animated/analysis/cache timings, Python/parent/child peak memory, throughput/realtime factor, duration/audio/codec/bytes/CRF, and PSNR. The quick profile completed with cache hits, zero duration drift, H.264 plus one audio stream, and 54.525 dB PSNR. |
| Live browser review | A real app session imported a generated stitched panorama, displayed the fail-closed raw-worker warning, created and saved a vertical track, rendered a 360x640 preview and 1080x1920 final MP4, and reported ordinary-media insertion. Desktop and 390-pixel responsive layouts had no console errors or horizontal overflow. |
| Clean environment | A new Python 3.12 virtual environment installed `requirements.txt`, passed `pip check`, compiled the source, imported the disabled-by-default app, and passed the 11 focused FFmpeg plus UI/API workflow tests using generated Studio-style input. |
| Public worker and release tree | The SDK-off C++ worker configured, built, and passed 3/3 CTests. Tracked-file audit found no raw/sample video or proprietary binary extension; ignore/container rules exclude SDK, model, raw-camera, and generated media paths. |
| Release package | Installation, Studio and MediaSDK quick starts, Mac/remote guide, supported-camera policy, limitations, privacy, licensing, troubleshooting, backend contribution, performance, regression, migration, synchronization, and release-checklist documents are present and cross-linked from the Insta360 README. |

## Local real-camera validation (outside Git)

On 2026-07-25, the supplied `VID_20260721_180140_00_036.insv` was validated
without modifying or committing the source. Insta360 Studio 5.9.10 identified
it as an Insta360 X5 PureVideo recording and opened the single file as a
complete stitched panorama; no `_10_` companion was present or required for
this recording. Studio exported a 5760x2880, 30 fps, HEVC/AAC, exactly 2:1
master. OpenStoryline then completed import, a 1024x512 proxy, four-timepoint
equirectangular and six-direction perspective inspection, a two-keyframe
virtual-camera track, a five-second 640x360 H.264/AAC preview/final render with
stereo audio, and the unchanged `LoadMediaNode` handoff. Visual review confirmed
usable perspective geometry. All raw, exported, cached, and rendered media stays
under ignored local paths.

The same sample exposed a discovery ambiguity: current single-container files
can retain an `_00_` marker that was historically used as pair evidence. The
worker now proposes a lone marker-bearing file as a low-confidence singleton
with explicit complete-container-versus-missing-companion warnings and mandatory
confirmation. It still does not claim raw decode or start stitching from
filename evidence.

## Risk register

| Risk | Status and mitigation | Next owner |
| --- | --- | --- |
| FFmpeg reports timeline-capable `v360` options but rejects actual runtime commands | Confirmed on FFmpeg 8.1.2 (`Function not implemented`). Startup performs an actual feature probe; Phase 1 uses frame-aligned static segments and records the fallback. | Re-probe on every environment; consider a continuous graph/backend only after feature evidence. |
| Segment fallback cost and camera stepping grow with duration | Open for long real footage. Phase 7 records repeatable static/animated throughput, cache behavior, memory, duration, audio, and quality for generated inputs; one real X5 Studio export also passed a five-second two-keyframe render with audio. The default 0.25-second sampling remains capped at 5,000 segments and midpoint sampling remains approximate. | Qualify full-length real-camera renders outside Git and compare every release benchmark with the prior tag. |
| Variable-frame-rate, multi-audio, surround, and long-form sync are not covered by the tiny fixture | Open. Phase 1 validates CFR H.264/AAC, explicit source ranges, one selected audio stream, and output duration. | Expand fixture and real-media regression coverage before release. |
| Renderer is software `libx264` only | Accepted for deterministic Phase 1. Hardware acceleration is recorded as disabled in the manifest. | Add only as an explicit, separately keyed backend after parity tests. |
| Cache writes do not coordinate concurrent identical renders | Partially mitigated. Phase 6 serializes job creation/claiming and exact idempotent replays use one job, while existing render/analysis entries remain atomically written and revalidated. Different idempotency keys can still race for one content key across local and remote processes. | Add a per-cache-key filesystem/distributed lease before raising production concurrency. |
| CLI accepts an explicit asset path outside OpenStoryline media roots | Accepted only for the local developer CLI. Phase 2 MCP nodes canonicalize inputs and require them beneath configured media/session/output roots. | Keep the CLI explicitly developer-only; retain node path tests. |
| Artifact event appends are not transactionally locked across processes | Partially mitigated. The Phase 7 web session serializes spherical mutation with a per-session async lock and optimistic revisions reject stale edits. Direct MCP calls in another process still use the existing nontransactional `ArtifactStore`. | Add a cross-process session transaction/lease before multi-process collaborative editing. |
| Proxy and inspection cache writes have no cross-process lease | Partially mitigated. Distributed jobs are quota/concurrency controlled and cache entries are atomically validated, but local MCP work and different job keys can duplicate the same cache operation. | Add a per-content-key lease and cache quota during post-release production hardening. |
| Licensed MediaSDK build/runtime is unverified | Open and explicitly reported. No standalone SDK package, headers, model root, Linux x86-64 GPU host, or license-controlled runtime is available on this Mac. The stub and official-API compile test are not evidence of actual decoding quality. | Validate on an approved Ubuntu 22.04 x86-64 GPU builder with the operator's current SDK/models before enabling raw ingest. |
| MediaSDK API/package layout can drift | Open. The adapter follows the current official docs/example and CMake accepts explicit include/library/version controls; an official-API test double catches local source regressions only. | Compile against every approved SDK update and record capability/runtime versions. |
| Raw `.insv` remains unavailable in this checked-in runtime | Intentional fail-closed state. The built public provider reports `sdk_unavailable`; Insta360 Studio export remains the operational fallback. | Supply licensed dependencies and set both application and build gates only after private acceptance. |
| Discovery is heuristic | Mitigated. Filename markers, timestamp proximity, and size similarity influence confidence, but every proposal requires confirmation. A real X5 single-container `_00_` sample is now proposed with lower confidence and an explicit missing-companion ambiguity instead of being dropped as ungrouped. | Add SDK stream/camera metadata evidence on the private builder and retain real-camera regression fixtures outside Git. |
| Direct worker CLI validates nonempty output but not decoded media internals | Open. The Python client always verifies hashes and ffprobes the 2:1 master before asset import; direct CLI callers must perform equivalent media validation. | Move a licensed-SDK-compatible media probe into the worker after private runtime evaluation. |
| Worker stitching is not globally content-addressed across keys | Partially mitigated. Phase 6 persists normalized request hashes and exact idempotency replays, so transport retries do not restitch. A different idempotency key intentionally creates another auditable job. | Consider a reviewed source/settings content index after licensed output-equivalence testing. |
| Lightweight CPU detections are visual candidates, not semantic identity | Intentional default. The backend records its exact heuristic detector/embedder names and warns against treating person/face labels as identity claims. | Add an approved optional semantic detector/face backend through the Phase 4 protocols; retain the lightweight public/offline path. |
| Sampling can miss brief or very fast subjects | Open. Interval and maximum frame count are explicit, bounded, recorded, and cache-keyed; no quality level is implied. | Tune against real footage and expose backend-specific sampling profiles before release. |
| Greedy association can confuse similar people during close crossings | Open. Spherical distance plus color-histogram evidence, confidence, occlusion gaps, seam events, and ambiguity warnings remain inspectable. | Add stronger optional embeddings/re-identification and human correction without changing core models. |
| Perspective analysis can create many cached PNGs | Open. Sampling/view dimensions, worker queue/concurrency and upload size are bounded; every output is content-addressed and validated. Job-history pruning does not delete media/cache artifacts. | Add explicit cache byte quotas and reviewed media retention before production rollout. |
| Feature is disabled in checked-in configuration | Intentional rollout control. Operators must set `spherical.enabled=true` and restart the MCP server. Disabled-mode tests protect the existing tool surface. | Enable only in a configured environment with approved media/cache paths. |
| Agent interpretation was not exercised against paid LLM/VLM services | Open external validation. Camera-language examples, tool descriptions, workflow routing, and strict schemas are tested locally; model keys remain blank. | Run an operator acceptance conversation when credentials/model endpoints are intentionally configured. |
| Fixture labels cannot use FFmpeg `drawtext` on the audited build | Mitigated with a deterministic built-in bitmap font and temporary PPM; no external font/Pillow dependency. | Keep generator test coverage on supported platforms. |
| ASR payload shape and timestamp units can vary by backend | Mitigated for the current `local_asr` sentence/word fields with explicit millisecond conversion, bounds checks, fallback turn timing, and warnings. | Add captured contract fixtures for every approved ASR backend before enabling it. |
| Speaker scoring has no confirmed diarization or spatial-audio identity | Intentional disclosure. The public implementation combines visible evidence and optional supplied mouth activity, labels only `probable`, and preserves ambiguous alternatives. | Add approved diarization/spatial/mouth backends as evidence providers; never silently upgrade probability to identity. |
| Director semantic relevance is deterministic and coarse | Open quality limitation. It consumes transcript/story context but does not call a paid language model; all component values and rationale remain inspectable. | Evaluate an optional structured semantic scorer against operator-reviewed footage without weakening deterministic fallback. |
| Cut timing is materialized through the sampled FFmpeg fallback | Open. Cut decisions use near-adjacent keyframes; the deterministic renderer's configured sampling interval bounds where the visual change is materialized. | Add explicit cut-boundary segmentation to the renderer and frame-accurate long-form regression coverage before release. |
| Licensed remote GPU deployment is not exercised here | Open external validation. Local tests use a fake SDK provider and real FFmpeg/analysis operations; the public Ubuntu container builds and passes live/ready/authentication smoke checks while remaining fail-closed for stitch. This arm64 Mac still cannot prove x86-64 NVIDIA/MediaSDK behavior or stitch quality. | Run the deployment and private acceptance matrix on the approved Ubuntu 22.04 GPU host before enabling stitch readiness. |
| Bearer authentication depends on transport encryption | Documented operational requirement. Tokens use constant-time comparison and redaction, but the worker does not terminate TLS itself. | Bind to a private VPN address or deploy a TLS reverse proxy with strict firewall rules; never expose plaintext port 8090 to an untrusted network. |
| SQLite is a single-worker-host control plane | Intentional scope. WAL, transactional claims and bounded concurrency support one service host; shared SQLite over object/eventually-consistent storage is unsupported. | Move to a reviewed network database/queue only if release requirements demand active-active schedulers. |
| Path authorization has a local filesystem TOCTOU boundary | Low risk under the dedicated service account. Paths are canonicalized after symlink resolution and artifacts are revalidated, but a privileged local actor could swap mounts/parents after submission. | Keep roots owned/configured by administrators, isolate the worker account, and do not grant untrusted users filesystem mutation rights. |
| HTTP upload disk usage is bounded per request, not by aggregate bytes | Partially mitigated by upload size, job queue, concurrency and explicit roots. Large retained uploads can still fill the volume. | Prefer shared storage and add filesystem quota/retention monitoring before production HTTP-ingest use. |
| Reverse proxy and systemd/NVIDIA examples are not runtime-tested on this Mac | Open platform validation. Compose schema and the public Ubuntu container are validated, including live/ready/authentication behavior, but systemd hardening, GPU passthrough and proxy SSE buffering require the target host. | Execute the documented deployment drill and diagnostics on Ubuntu/Proxmox before release. |

## Baseline caveats

- A clean upstream dependency resolution installed a transitive LangGraph pair
  that failed at import despite `pip check`. Phase 0 directly pins
  `langgraph-prebuilt==1.0.8`; details are in `development.md`.
- Server construction requires the official model/resource download and takes
  roughly a minute on the audited Mac while embedding and shot-split models
  load.
- LLM/VLM keys remain intentionally blank, so no paid or external model call was
  part of Phase 0 validation.
- The clone currently has `origin` and `upstream` set to the public upstream URL.
  Set `origin` to the user's fork before any push. No commit was created.

## Phase boundary

Phase 7 is complete in the working tree, and this plan pack has no later product
phase. Stop here. The public Studio-export workflow is validated with real X5
footage, and the browser editor, regression matrix, benchmark harness, release
documentation, and SDK-off worker gate are validated. Raw `.insv` decode inside
the checked-in worker remains deliberately fail-closed until a licensed SDK
build and the private Ubuntu/GPU/camera matrix pass outside this repository.
