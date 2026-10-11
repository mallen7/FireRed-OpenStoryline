# Insta360 MediaSDK worker

This standalone C++20 process is the only boundary that may load the
proprietary Insta360 MediaSDK. The default public build uses
`StubStitchProvider`: it validates the protocol, discovery, progress, error,
and subprocess paths while truthfully reporting that it cannot decode `.insv`.

The implementation follows Insta360's current public `ins_stitcher.h` API and
Linux example:

- https://github.com/Insta360Develop/Desktop-MediaSDK-Cpp
- https://github.com/Insta360Develop/Desktop-MediaSDK-Cpp/blob/V3.0.5-build1/Demo/Linux/Media%20SDK/main.cc

Insta360 currently documents the production target as Ubuntu 22.04 x86-64,
requires a GPU for 3.x, and distributes the SDK through an application process.
Insta360 Studio's private app libraries are not SDK inputs and must not be used.

## Public build

Prerequisites on Ubuntu:

```bash
sudo apt-get install cmake g++ libjson-c-dev python3
cmake -S services/insta360-worker \
  -B services/insta360-worker/build \
  -DINSTA360_ENABLE_SDK=OFF \
  -DBUILD_TESTING=ON
cmake --build services/insta360-worker/build --parallel
ctest --test-dir services/insta360-worker/build --output-on-failure
```

The public Dockerfile also builds and tests only the stub provider. It never
copies or downloads proprietary packages or models.

## CLI protocol

Stdout is JSON or JSON Lines only; logs go to stderr. Exit codes are stable:

| Code | Meaning |
| --- | --- |
| 0 | Success |
| 2 | CLI usage error |
| 3 | Request validation error |
| 4 | Capability unavailable |
| 5 | Provider/SDK failure |
| 6 | Cancelled |
| 7 | Output validation failure |
| 8 | Protocol/internal failure |

```bash
insta360-worker capabilities
insta360-worker discover --directory /media/card
insta360-worker probe --input /media/card/front.insv --input /media/card/back.insv
insta360-worker stitch --request /jobs/stitch-request.json
```

`discover` returns proposals with evidence, confidence, warnings, and
`requires_confirmation=true`. It never launches a stitch or treats filename
suffixes as sufficient proof. `stitch` accepts explicit ordered paths only.

## Private SDK build

Keep the approved SDK and model roots outside Git or in an ignored `local/`,
`sdk/`, or `vendor/insta360/` directory. Reference them in place:

```bash
export INSTA360_SDK_ROOT=/opt/private/insta360-mediasdk
export INSTA360_MODEL_ROOT=/opt/private/insta360-models
cmake -S services/insta360-worker \
  -B services/insta360-worker/local/build-sdk \
  -DINSTA360_ENABLE_SDK=ON \
  -DINSTA360_SDK_VERSION=3.1.1 \
  -DINSTA360_SDK_LIBRARY=/opt/private/insta360-mediasdk/lib/libMediaSDK.so
cmake --build services/insta360-worker/local/build-sdk --parallel
```

SDK configuration fails if the host is not Linux x86-64, the root or
`ins_stitcher.h` is missing, the library cannot be found, or a supplied model
root is invalid. For older SDKs without `SetModelFileRootDir` or stitch fusion,
set `INSTA360_SDK_HAS_MODEL_ROOT_API=OFF` or
`INSTA360_SDK_HAS_STITCH_FUSION=OFF`; requests for those unavailable operations
then fail rather than silently falling back.

The adapter initializes the environment, configures ordered inputs/output,
2:1 dimensions, H.264/H.265, bitrate, FlowState, direction lock, stitch type,
models and fusion, streams progress, maps documented SDK error codes, writes
SDK logs beside the output, and invokes `CancelStitch` on SIGINT/SIGTERM.
Partial outputs are removed on provider error or cancellation.

## Distributed HTTP service

Phase 6 adds a Python FastAPI service around the same standalone process and the
existing FFmpeg renderer/analyzer. It provides authenticated durable stitch,
reframe, and analysis jobs, SSE progress, cancellation, SQLite restart recovery,
idempotency, validated artifacts, bounded concurrency/uploads, metrics, health,
and compatibility diagnostics. It never loads the proprietary SDK in Python.

```bash
PYTHONPATH=src python -m open_storyline.spherical.worker_service serve \
  --host 127.0.0.1 --port 8090
```

All `/v1` routes require a bearer token. `/health/live` and `/health/ready` are
deliberately unauthenticated for container/systemd health checks and expose no
job or proprietary data. See `deploy/README.md`, `docker-compose.yml`,
`Dockerfile.http`, and `deploy/insta360-worker.service` for production setup.
