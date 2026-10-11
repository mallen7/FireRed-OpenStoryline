# Proprietary Insta360 SDK setup boundary

Phase 3 implements the isolated MediaSDK worker, but the checked-in build uses
the SDK-independent stub. Raw `.insv` ingest is available only when an operator
builds the real provider on an approved host and its capability response says
both `can_decode_insv=true` and `can_stitch=true`. Otherwise export a stabilized
2:1 equirectangular MP4 from Insta360 Studio.

The adapter follows Insta360's public MediaSDK repository and Linux example:

- <https://github.com/Insta360Develop/Desktop-MediaSDK-Cpp>
- <https://github.com/Insta360Develop/Desktop-MediaSDK-Cpp/blob/V3.0.5-build1/Demo/Linux/Media%20SDK/main.cc>
- <https://onlinemanual.insta360.com/developer/en-us/resource/sdk>

Obtain and use the SDK only under the operator's applicable Insta360 license.
Insta360 Studio's private app libraries are not a substitute for the standalone
SDK and must not be copied, inspected, or linked by this project.

## Public/private split

The public repository contains only:

- versioned request, response, event, capability, and discovery JSON Schemas;
- the standalone worker interface and SDK-independent stub;
- the SDK adapter source and a tiny public official-API compile test double;
- build logic that defaults to SDK disabled;
- the local Python subprocess client, CI, Dockerfile, tests, and documentation.

The following remain operator-supplied and untracked:

- SDK archives, headers, shared/static libraries, examples, and license files;
- AI stitching models and model roots;
- vendor runtime packages and GPU-specific binaries;
- raw `.insv`/`.insp` footage and stitched user media.

Ignored local locations include `.storyline/insta360-sdk/`,
`.storyline/insta360-models/`, `services/insta360-worker/sdk/`,
`services/insta360-worker/vendor/insta360/`, and
`services/insta360-worker/local/`. Do not weaken these rules to make a local
build convenient.

## Public build first

On Ubuntu 22.04, install only public build dependencies and prove the stub path:

```bash
sudo apt-get update
sudo apt-get install -y cmake g++ libjson-c-dev python3
cmake -S services/insta360-worker \
  -B services/insta360-worker/build \
  -DINSTA360_ENABLE_SDK=OFF \
  -DBUILD_TESTING=ON
cmake --build services/insta360-worker/build --parallel
ctest --test-dir services/insta360-worker/build --output-on-failure
services/insta360-worker/build/insta360-worker capabilities
```

The last command must report the stub provider, `sdk_enabled=false`, and both
decode/stitch capabilities false. A public success must never be described as a
successful raw decode.

## Private SDK build

The initial private target is Ubuntu 22.04 x86-64 with an SDK-supported GPU,
driver, and runtime. Reference approved dependencies in place rather than
copying them into the source tree or build image:

```bash
export INSTA360_SDK_ROOT=/opt/private/insta360-mediasdk
export INSTA360_MODEL_ROOT=/opt/private/insta360-models
cmake -S services/insta360-worker \
  -B services/insta360-worker/local/build-sdk \
  -DINSTA360_ENABLE_SDK=ON \
  -DINSTA360_SDK_VERSION=3.1.1 \
  -DINSTA360_SDK_LIBRARY=/opt/private/insta360-mediasdk/lib/libMediaSDK.so \
  -DBUILD_TESTING=ON
cmake --build services/insta360-worker/local/build-sdk --parallel
ctest --test-dir services/insta360-worker/local/build-sdk --output-on-failure
```

CMake rejects SDK-on configuration on unsupported hosts and fails clearly for
a missing root, `ins_stitcher.h`, library, or invalid supplied model root. For
an SDK version without `SetModelFileRootDir` or stitch fusion, configure
`INSTA360_SDK_HAS_MODEL_ROOT_API=OFF` or
`INSTA360_SDK_HAS_STITCH_FUSION=OFF`. A request for unavailable functionality
must fail rather than silently downgrade.

## Private acceptance

Before setting `spherical.enable_sdk_worker=true`:

1. Confirm the SDK license permits the intended hosts and deployment.
2. Run `git status --short --ignored` and verify no SDK, model, raw footage, or
   user output is staged.
3. Record the worker, SDK, GPU driver, runtime, model bundle, and camera versions.
4. Verify `capabilities`, then `probe` representative explicitly ordered files.
5. Stitch licensed real-camera fixtures for each approved camera/codec/profile.
6. Exercise progress, SIGINT/SIGTERM cancellation, timeout, provider failure,
   overwrite false/true, and low-disk behavior.
7. Re-hash and ffprobe every result through `LocalStitchWorkerClient`; compare
   seams, stabilization, orientation, audio, duration, and quality against an
   approved Studio reference.
8. Inspect logs and packaged artifacts for path leakage or proprietary files.

No Python module loads SDK libraries directly. Crashes, allocator conflicts,
logs, model state, and license-controlled code remain isolated behind the
worker process. SDK errors are mapped to stable public error codes and raw
messages are path-redacted and bounded before crossing the protocol boundary.
