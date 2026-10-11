# Insta360 development and validation

Baseline captured on 2026-07-24 before spherical product behavior was added.

## Upstream snapshot

- Repository: `https://github.com/FireRedTeam/FireRed-OpenStoryline.git`
- Commit: `04297707e7607dd398e906262235d0797068e7b4`
- Commit date: `2026-04-16T11:39:13+08:00`
- Commit subject: `fix: include torchaudio for local ASR and document requirement (#82)`
- Branch: `main`
- `origin` and the newly added local `upstream` remote both point at the URL
  above. Replace `origin` with the actual fork URL before publishing changes.

## Verified host and tools

- macOS 26.3 (25D2125), arm64
- Python 3.12.13 in repo-local `.venv` (upstream recommends Python >=3.11)
- FFmpeg/ffprobe 8.1.2 with Homebrew codecs
- Node 26.5.0 and npm 11.17.0 (not required by the current static web UI)

The supported source installation is:

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
bash download.sh
```

`download.sh` installed the official model bundle and resource bundle. At this
baseline they occupy approximately 116 MB under `.storyline` and 527 MB under
`resource`; both locations are ignored.

## Baseline commands and results

| Command/check | Result |
| --- | --- |
| `.venv/bin/python -m pip install -r requirements.txt` | Completed. The initial upstream dependency set selected incompatible `langgraph-prebuilt==1.0.10`; see below. |
| `.venv/bin/python -m pip check` | `No broken requirements found.` after the direct compatibility pin. |
| `PYTHONPATH=src .venv/bin/python -m compileall -q src cli.py agent_fastapi.py` | Passed. |
| `PYTHONPATH=src .venv/bin/python -m unittest discover -v` before Phase 0 tests | Exit 5: `Ran 0 tests`, `NO TESTS RAN`. The upstream tree had no `tests/` directory. |
| Existing lint command | None found: no Ruff/Flake8 configuration or dependency. |
| Existing type-check command | None found: no mypy/pyright configuration or dependency. |
| Existing packaging/test config | None found: no `pyproject.toml`, `setup.cfg`, `tox.ini`, or pre-commit config. |
| `load_settings('config.toml')` | Passed and resolved paths relative to the config file. |
| MCP startup | Passed after resources and compatibility pin; Uvicorn listened on `127.0.0.1:8001`. A plain GET reached the server and correctly returned HTTP 406 because an MCP client must accept SSE. |
| Web startup | Passed; Uvicorn listened on `127.0.0.1:8005`, and `GET /` returned HTTP 200 with the OpenStoryline page. |

No LLM/VLM-backed editing job was executed because the checked-in configuration
contains no model names, endpoints, or API keys. That is an external-service
configuration limitation, not a server startup failure.

## Pre-existing failures and Phase 0 resolution

The clean upstream requirements allowed `langgraph-prebuilt==1.0.10` alongside
`langgraph==1.0.10`. Importing the MCP server then failed because prebuilt tried
to import `ExecutionInfo` and `ServerInfo` from `langgraph.runtime`, where those
symbols were absent. `pip check` did not detect the problem because package
metadata declared the combination valid.

Phase 0 pins `langgraph-prebuilt==1.0.8`, the last release before that breaking
runtime import. This is a dependency-reproducibility fix, not spherical runtime
behavior. With the pin, imports progressed to the expected missing-model error;
running `download.sh` then allowed both services to start.

The upstream baseline also contained no executable test/lint/type-check suite.
Phase 0 adds focused standard-library `unittest` coverage without pretending an
upstream regression suite exists.

## Development flags

Spherical support is inert by default:

```toml
[spherical]
enabled = false
enable_sdk_worker = false
```

`enable_sdk_worker` never implies that an SDK is present. Orchestration must
also require a successful worker capability response with `can_decode_insv`
and `can_stitch` true. The Phase 1 developer CLI and Phase 3 worker client are
directly invokable for development while the product flag is off. Phase 2 MCP
tools are registered only when `spherical.enabled=true`; raw input remains
outside those ordinary import tools.

## Phase 0 validation

Run from the repository root:

```bash
.venv/bin/python -m pip check
PYTHONPATH=src .venv/bin/python -m compileall -q src tests
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
PYTHONPATH=src .venv/bin/python -c "from open_storyline.spherical import VersionedSphericalModel; print(VersionedSphericalModel().model_dump_json())"
PYTHONPATH=src .venv/bin/python -c "from open_storyline.config import load_settings; print(load_settings('config.toml').spherical)"
```

For a service smoke test, start the MCP server and web app in separate shells,
wait for their Uvicorn startup messages, probe the endpoints, and stop them with
Ctrl-C:

```bash
PYTHONPATH=src .venv/bin/python -m open_storyline.mcp.server
PYTHONPATH=src .venv/bin/python -m uvicorn agent_fastapi:app --host 127.0.0.1 --port 8005
```

## Phase 1 developer workflow

Generate disposable labeled media and sample camera tracks:

```bash
PYTHONPATH=src .venv/bin/python -m open_storyline.spherical.cli generate-fixture \
  tests/fixtures/generated --overwrite
```

Validate or inspect the generated contracts and media:

```bash
PYTHONPATH=src .venv/bin/python -m open_storyline.spherical.cli validate-track \
  tests/fixtures/generated/camera-track-seam.json
PYTHONPATH=src .venv/bin/python -m open_storyline.spherical.cli inspect \
  tests/fixtures/generated/fixture-equirectangular.mp4
```

Render a camera track. Relative outputs are resolved inside `--workspace`; an
output that resolves outside that root is rejected.

```bash
PYTHONPATH=src .venv/bin/python -m open_storyline.spherical.cli reframe \
  --workspace outputs \
  --asset tests/fixtures/generated/asset.json \
  --track tests/fixtures/generated/camera-track-moving.json \
  --output moving-reframe.mp4
```

Export the versioned contracts after a schema change:

```bash
PYTHONPATH=src .venv/bin/python -m open_storyline.spherical.cli export-schemas \
  docs/insta360/schemas
```

Phase 1 verification:

```bash
.venv/bin/python -m pip check
PYTHONPATH=src .venv/bin/python -m compileall -q src tests
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
```

The integration tests generate a temporary 640x320, 10 fps, two-second source
with one 48 kHz audio stream. They exercise static 16:9 output, moving camera
segments, a square nonzero source subrange, portrait command generation, cache
rehydration, output/manifest hashes, and failure cleanup. Generated media is
deleted after the run.

## Phase 2 MCP workflow

Configure the roots and opt in before starting the MCP server:

```toml
[spherical]
enabled = true
workspace_root = "outputs"
cache_root = ".storyline/spherical-cache"
sample_media_root = "tests/fixtures/generated"
proxy_width = 1024
contact_sheet_thumbnail_width = 320
preview_max_dimension = 640
```

Restart the MCP server after changing the flag. The available tool sequence is:

1. `import_spherical_media` for an already stitched 2:1 MP4.
2. `inspect_spherical_media` for metadata, a panoramic contact sheet, and
   optional front/right/back/left/up/down views.
3. `create_virtual_camera_track` with explicit keyframes and output geometry.
4. `validate_virtual_camera_track` before committing an expensive render.
5. `preview_virtual_camera_track`, then
   `update_virtual_camera_track` as needed.
6. `render_reframed_clip` for the approved flat MP4.
7. Continue with ordinary `load_media` and existing storyline tools.

Use `list_virtual_camera_tracks` to inspect revisions and
`delete_virtual_camera_track` for a session-local tombstone. Raw `.insv` is not
a normal video input: run it through an explicitly enabled and capable Phase 3
worker, or export a stitched equirectangular MP4 from Insta360 Studio when the
licensed SDK is unavailable.

Phase 2 verification:

```bash
.venv/bin/python -m pip check
PYTHONPATH=src .venv/bin/python -m compileall -q src tests
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
.venv/bin/python /Users/scott/.codex/skills/.system/skill-creator/scripts/quick_validate.py \
  .storyline/skills/insta360_editing_workflow_skill
```

The node integration test generates its stitched master under a temporary
configured media root, exercises import through flat render, verifies contact
sheet cache reuse and normal-media compatibility, then removes the media.

## Phase 3 worker workflow

The standalone worker is a C++20/json-c process. Its public build defaults to
the truthful stub provider and does not download, link, or package proprietary
SDK files:

```bash
cmake -S services/insta360-worker \
  -B services/insta360-worker/build \
  -DINSTA360_ENABLE_SDK=OFF \
  -DBUILD_TESTING=ON
cmake --build services/insta360-worker/build --parallel
ctest --test-dir services/insta360-worker/build --output-on-failure
```

Inspect the provider before submitting raw work:

```bash
services/insta360-worker/build/insta360-worker capabilities
services/insta360-worker/build/insta360-worker discover --directory /media/card
services/insta360-worker/build/insta360-worker \
  probe --input /media/card/front.insv --input /media/card/back.insv
services/insta360-worker/build/insta360-worker \
  stitch --request /jobs/stitch-request.json
```

Stdout is versioned JSON (or JSON Lines for stitch progress/result/error) and
stderr is diagnostic logging. Discovery only proposes ordered groups and every
group requires confirmation. A direct stitch request must contain explicit
ordered `.insv` paths and exact 2:1 even output dimensions.

The Python boundary is transport-neutral. Local operation constructs
`LocalStitchWorkerClient`, checks `capabilities()`, builds a validated
`WorkerStitchRequest`, and calls `stitch_and_import(...)`. That call streams
typed progress through a callback, honors a `threading.Event` cancellation
signal, validates provider/request identity, normalized settings, and
source/output hashes, ffprobes the final 2:1 MP4, and returns both the worker result and a renderer-compatible
`SphericalAsset`. Phase 6 adds the authenticated HTTP implementation described
below without changing this local boundary.

Regenerate the checked-in protocol schemas after contract changes:

```bash
PYTHONPATH=src .venv/bin/python -c \
  "from open_storyline.spherical.worker_protocol import export_worker_json_schemas; export_worker_json_schemas('services/insta360-worker/schemas')"
```

Phase 3 verification:

```bash
.venv/bin/python -m pip check
PYTHONPATH=src .venv/bin/python -m compileall -q src tests
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
cmake -S services/insta360-worker \
  -B services/insta360-worker/build \
  -DINSTA360_ENABLE_SDK=OFF \
  -DBUILD_TESTING=ON
cmake --build services/insta360-worker/build --parallel
ctest --test-dir services/insta360-worker/build --output-on-failure
```

Local public validation passes the CLI protocol cases, SHA-256 known vector,
and an SDK-adapter source compile against a small public test double. A real
SDK runtime was not present on this macOS arm64 host; private acceptance must
run on the documented Ubuntu 22.04 x86-64 GPU target before enabling raw ingest.

## Phase 4 assisted-analysis workflow

The default analysis backend is local, offline, CPU-only, and dependency-light.
It detects visual candidates; it does not claim semantic identity. Default
settings are explicit and can be overridden per analysis call:

```toml
[spherical]
analysis_interval_seconds = 0.5
analysis_max_frames = 120
analysis_yaw_step_degrees = 60
analysis_pitch_centers = [-45.0, 0.0, 45.0]
analysis_horizontal_fov = 100.0
analysis_view_width = 256
analysis_view_height = 256
analysis_minimum_detection_confidence = 0.25
analysis_dedup_angular_threshold_degrees = 10.0
analysis_max_track_angular_distance_degrees = 45.0
analysis_max_occlusion_seconds = 1.5
```

The default grid has horizontal and vertical overlap. Invalid grids that leave
the zenith/nadir uncovered or have touching/gapped neighbors fail validation.
Long footage is bounded by both sampling interval and maximum frame count; both
values are recorded in the analysis backend metadata and cache key.

Operator review sequence:

1. Import and inspect the stitched master.
2. Call `analyze_spherical_asset`.
3. Review its overlay clip/contact sheet and call `list_detected_subjects`.
4. Call `inspect_subject_track` for confidence, visibility, identity evidence,
   occlusion gaps, seam crossings, and warnings.
5. Create a proposal with `suggest_subject_reframe`,
   `suggest_group_reframe`, or `suggest_establishing_shot`.
6. Call `preview_reframe_suggestion`. This does not activate the proposal.
7. Explicitly call `accept_reframe_suggestion` or
   `reject_reframe_suggestion`. Only acceptance creates revision 1 of a normal
   assisted `VirtualCameraTrack`.
8. Validate/revise/render an accepted track through the Phase 2 tools.

Library policy coverage also includes largest visible person, rule-of-thirds,
subject-to-subject pan, and static direction. Nadir avoidance and configured
stitch-seam zones either adjust the camera safely or produce explicit warnings
when subject/group coverage prevents adjustment.

Regenerate every public spherical JSON Schema after contract changes:

```bash
PYTHONPATH=src .venv/bin/python -m open_storyline.spherical.cli \
  export-schemas docs/insta360/schemas
```

Phase 4 verification:

```bash
.venv/bin/python -m pip check
PYTHONPATH=src .venv/bin/python -m compileall -q src tests
PYTHONPATH=src .venv/bin/python -m unittest tests.test_spherical_analysis -v
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
.venv/bin/python /Users/scott/.codex/skills/.system/skill-creator/scripts/quick_validate.py \
  .storyline/skills/insta360_editing_workflow_skill
```

The Phase 4 integration test creates disposable real 2:1 H.264/AAC media,
generates 32 overlapping perspective frames, reconciles duplicate detections,
tracks candidates across viewports, validates cache reuse, renders a suggestion
preview, proves no active track exists before acceptance, then exercises both
acceptance and rejection. Separate geometry tests cover seam deduplication and
identity continuity through an occlusion.

## Phase 5 storyline-aware direction

Phase 5 reuses prior-node results. Run `local_asr`, scene splitting, clip
understanding, script generation, and ordinary timeline planning when those
signals are wanted, then pass their structured outputs into
`build_spherical_story_timeline`. That node performs alignment only; it never
loads or invokes an ASR model.

Operator sequence:

1. Complete spherical analysis and any existing OpenStoryline ASR/understanding
   operations.
2. Call `list_director_profiles`; choose one of the six named profiles.
3. Call `build_spherical_story_timeline` with existing ASR, split-shot,
   caption, script, and timeline results plus actual mouth/audio activity when
   available.
4. Call `analyze_speaker_candidates`. Review probable, ambiguous, and unresolved
   turns and their alternative candidates.
5. Call `generate_director_proposals` with at least two variants. Optional
   `motion_constraints` overrides all pan speed, acceleration, shot length, cut
   spacing, FOV-rate, dead-zone, and low-confidence limits as one validated
   object.
6. Call `compare_director_proposals` and preview likely choices with
   `preview_director_proposal`.
7. Explicitly call `accept_director_proposal` or
   `reject_director_proposal`. Generation, ranking, and preview do not activate
   a track.
8. Call `render_director_proposal` only for the accepted choice. Feed the
   returned `ordinary_media` and `ordinary_clip` into the normal media,
   grouping/timeline, subtitle/music, and final-render operations.

The comparison variants are speaker-focused, cinematic, minimal-motion,
high-energy, and vertical-social. Every proposal stores its exact profile,
motion constraints/measurements, camera trajectory, per-decision score JSON,
confidence, alternatives, rationale, and warnings. Portrait output uses the
separate validated vertical dimensions.

Phase 5 verification:

```bash
PYTHONPATH=src .venv/bin/python -m open_storyline.spherical.cli \
  export-schemas docs/insta360/schemas
PYTHONPATH=src .venv/bin/python -m compileall -q src tests
PYTHONPATH=src .venv/bin/python -m unittest \
  tests.test_spherical_direction tests.test_spherical_analysis \
  tests.test_spherical_mcp -v
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m pip check
.venv/bin/python /Users/scott/.codex/skills/.system/skill-creator/scripts/quick_validate.py \
  .storyline/skills/insta360_editing_workflow_skill
ctest --test-dir services/insta360-worker/build --output-on-failure
```

The Phase 5 integration test uses disposable real H.264/AAC spherical media,
aligns two ASR turns and word timestamps to two tracked candidates, proves weak
speaker evidence remains unselected, generates three distinct proposals,
forces an over-speed move to a cut, compares and previews proposals, proves no
track exists before acceptance, accepts one/rejects another, and renders the
accepted plan to ordinary media/clip objects.

## Phase 6 distributed worker

The service requires explicit roots and bearer authentication. Secrets are read
from `OPENSTORYLINE_WORKER_TOKEN` or a root-readable
`OPENSTORYLINE_WORKER_TOKEN_FILE`; they are never read from `config.toml`.

Minimal local development configuration:

```bash
export OPENSTORYLINE_WORKER_STATE_DIR=/tmp/openstoryline-worker-state
export OPENSTORYLINE_WORKER_MEDIA_ROOTS=/absolute/media/root
export OPENSTORYLINE_WORKER_OUTPUT_ROOTS=/absolute/output/root
export OPENSTORYLINE_WORKER_UPLOAD_ROOT=/absolute/media/root/uploads
export OPENSTORYLINE_WORKER_EXECUTABLE=/absolute/path/to/insta360-worker
export OPENSTORYLINE_WORKER_TOKEN='replace-with-a-random-development-token'
PYTHONPATH=src .venv/bin/python -m open_storyline.spherical.worker_service serve \
  --host 127.0.0.1 --port 8090
```

Health endpoints distinguish process liveness from scheduler/storage readiness:

```bash
curl http://127.0.0.1:8090/health/live
curl http://127.0.0.1:8090/health/ready
curl -H "Authorization: Bearer $OPENSTORYLINE_WORKER_TOKEN" \
  http://127.0.0.1:8090/v1/capabilities
```

Job endpoints are `POST /v1/jobs/stitch`, `/reframe`, and `/analyze`, plus job,
SSE event, cancellation, metrics, diagnostics, upload, and artifact-download
routes. Every job submission requires a stable `Idempotency-Key`. Reusing it
with identical normalized JSON returns the original job; reusing it for different
content returns HTTP 409.

Configure a Mac client without putting its token in TOML:

```toml
[spherical]
enable_sdk_worker = true
worker_transport = "http"
worker_url = "https://worker.example.internal"
worker_token_env = "OPENSTORYLINE_WORKER_TOKEN"
worker_verify_tls = true
worker_timeout_seconds = 86400
```

`create_stitch_worker_client(settings.spherical)` selects the existing local
subprocess client or `HttpSphericalWorkerClient`. The HTTP client submits typed
requests, consumes persisted SSE progress, sends cancellation, validates shared
artifacts, and can upload inputs or download artifacts with SHA-256 checks.

Run diagnostics and distributed-schema export:

```bash
PYTHONPATH=src .venv/bin/python -m open_storyline.spherical.worker_service diagnose
PYTHONPATH=src .venv/bin/python -m open_storyline.spherical.worker_service \
  export-schemas services/insta360-worker/schemas
```

Phase 6 verification:

```bash
PYTHONPATH=src .venv/bin/python -m compileall -q src tests
PYTHONPATH=src .venv/bin/python -m unittest \
  tests.test_spherical_distributed_worker tests.test_spherical_worker -v
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m pip check
cmake -S services/insta360-worker -B /tmp/openstoryline-worker-build \
  -DINSTA360_ENABLE_SDK=OFF -DBUILD_TESTING=ON
cmake --build /tmp/openstoryline-worker-build --parallel
ctest --test-dir /tmp/openstoryline-worker-build --output-on-failure
```

Deployment examples and Ubuntu 22.04, NVIDIA runtime, Proxmox GPU passthrough,
shared-storage/HTTP-transfer, firewall, backup, and cleanup guidance are in
`services/insta360-worker/deploy/README.md`.

## Phase 7 browser, regression, and release workflow

The web app keeps spherical review inside the existing FastAPI session rather
than introducing another service. Enable the feature for an operator instance,
restart the web app, import a stitched 2:1 MP4 through the spherical workflow,
then choose **360°** beside the normal upload control:

```toml
[spherical]
enabled = true
```

The workspace reads session assets, tracks, suggestions, director proposals,
subjects, renders, worker capability, and warnings from
`GET /api/sessions/{session_id}/spherical`. Manual edits use strict versioned
create/update requests and optimistic revisions; suggestion/director acceptance,
segment preview, and final render invoke the same node/event contracts as MCP.
A final flat render is copied into the ordinary session media library.

The panoramic viewer uses the official A-Frame 1.8.0 browser build. If WebGL or
the viewer script is unavailable, the workspace retains a conventional video
fallback and all track/render controls. The checked-in feature flag remains off.

Focused UI and release regression:

```bash
PYTHONPATH=src:. .venv/bin/python -m unittest tests.test_spherical_ui -v
node --test tests/spherical_ui_model.test.mjs
PYTHONPATH=src:. .venv/bin/python scripts/benchmark_spherical.py --quick
```

Full Phase 7 release gate:

```bash
.venv/bin/python -m pip check
PYTHONPATH=src:. .venv/bin/python -m compileall -q src tests agent_fastapi.py cli.py
PYTHONPATH=src:. .venv/bin/python -m unittest discover -s tests -v
node --test tests/spherical_ui_model.test.mjs
.venv/bin/python /Users/scott/.codex/skills/.system/skill-creator/scripts/quick_validate.py \
  .storyline/skills/insta360_editing_workflow_skill
cmake -S services/insta360-worker -B /tmp/openstoryline-worker-release \
  -DINSTA360_ENABLE_SDK=OFF -DBUILD_TESTING=ON
cmake --build /tmp/openstoryline-worker-release --parallel
ctest --test-dir /tmp/openstoryline-worker-release --output-on-failure
git diff --check
```

On the 2026-07-25 audited Mac, the gate passed 67/67 Python tests, 3/3 Node
tests, and 3/3 native CTests. A fresh Python 3.12 virtual environment installed
the requirements, passed dependency/compile/import checks, and passed all 11
focused FFmpeg plus UI/API workflow tests. Live in-app-browser review imported
a generated panorama, saved a vertical track, rendered a 360x640 preview and a
1080x1920 final MP4, verified ordinary-media insertion, checked the 390-pixel
responsive layout, and reported no browser-console errors. Disposable media,
session artifacts, the clean virtual environment, and native build were removed
after validation.

The executable 12-case mapping is in `release-test-matrix.md`; performance and
quality methodology is in `performance.md`; the operator sign-off template is
`release-checklist.md`. The private raw-ingest section must remain unchecked
unless the licensed SDK, Ubuntu/NVIDIA host, and real-camera matrix are actually
validated outside Git.

## Ongoing development rules

- Complete and validate one phase at a time.
- Keep ordinary media behavior and public CI independent of the SDK.
- Add tests for each contract and operation; never report skipped tests as a
  pass.
- Use direct FFmpeg argument arrays for spherical media.
- Generate fixtures rather than committing large binary media.
- Keep agent camera decisions as inspectable, overridable JSON.
- Do not commit generated output, user footage, SDK files, or models.
