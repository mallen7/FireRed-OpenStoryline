# Phase 7 regression matrix

All fixtures are generated beneath temporary directories and removed. The suite
contains no checked-in MP4 or `.insv` footage.

A separate ignored local validation on 2026-07-25 used a supplied Insta360 X5
PureVideo `.insv`. Insta360 Studio 5.9.10 exported one 5760x2880 2:1 HEVC/AAC
master, and OpenStoryline passed it through import, proxy, panorama inspection,
virtual-camera preview/final render, audio preservation, and normal-media
loading. This supplements the reproducible matrix; private footage is not a CI
fixture.

| # | Required path | Executable evidence |
| --- | --- | --- |
| 1 | Import stitched panorama | `tests.test_spherical_ui.SphericalUiApiWorkflowTests.test_browser_api_uses_mcp_events_and_hands_final_render_to_normal_media` imports a generated 2:1 MP4 through the MCP node, then reads it through the browser API. |
| 2 | Create manual camera track | The same UI/API workflow posts the strict UI request and verifies the MCP `SphericalSessionRepository` reconstructs revision 1. |
| 3 | Render preview | The UI/API workflow renders and serves a playhead-centered 16:9 segment. |
| 4 | Render final flat clip | The UI/API workflow renders a final H.264/AAC MP4 and verifies the output. |
| 5 | Load flat clip into normal workflow | The UI/API workflow verifies automatic ordinary-media insertion and `LoadMediaNode`; `tests.test_spherical_nodes` also exercises the handoff. |
| 6 | Stub SDK job lifecycle | `tests.test_spherical_worker.WorkerProtocolTests.test_client_streams_progress_and_imports_validated_master` covers capability, progress, result, hashes, ffprobe, and asset import with a synthetic worker. Native stub CLI lifecycle cases run under CTest. |
| 7 | Remote-worker failure | `tests.test_spherical_distributed_worker` covers HTTP auth, invalid requests, retry/failure state, cancellation, SSE terminal events, restart recovery, and sanitized errors. |
| 8 | Cache hit | The UI/API workflow repeats the same segment and asserts `cache_hit=true`; FFmpeg, analysis, proxy, and distributed suites independently validate their caches. |
| 9 | Invalid path rejection | `SphericalUiContractTests.test_preview_path_guard_resolves_symlink_escape` plus the UI/API HTTP 403 assertion cover outside-root and symlink paths; node/worker suites cover traversal and canonical-root rejection. |
| 10 | Seam-crossing camera motion | The UI/API track moves from yaw `170` to `-170`; `tests/spherical_ui_model.test.mjs` proves shortest-path interpolation and geometry/analysis suites exercise seam identity. |
| 11 | Vertical output | The UI/API workflow updates the same shared track to `180x320`, renders it, and verifies the output geometry. |
| 12 | Existing normal MP4 workflow | The UI/API workflow calls the unchanged `LoadMediaNode`; the full Python discovery suite remains the regression gate for ordinary behavior. |

Run the focused release path:

```bash
PYTHONPATH=src:. .venv/bin/python -m unittest tests.test_spherical_ui -v
node --test tests/spherical_ui_model.test.mjs
PYTHONPATH=src:. .venv/bin/python -m unittest \
  tests.test_spherical_worker tests.test_spherical_distributed_worker -v
```

Run the actual release gate, not only the focused matrix:

```bash
PYTHONPATH=src:. .venv/bin/python -m unittest discover -s tests -v
cmake -S services/insta360-worker -B /tmp/openstoryline-worker-release \
  -DINSTA360_ENABLE_SDK=OFF -DBUILD_TESTING=ON
cmake --build /tmp/openstoryline-worker-release --parallel
ctest --test-dir /tmp/openstoryline-worker-release --output-on-failure
```
