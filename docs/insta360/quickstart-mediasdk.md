# MediaSDK quick start

The public worker is a protocol-test stub. It truthfully reports
`sdk_enabled=false`, `can_decode_insv=false`, and `can_stitch=false`. It is not a
raw ingest implementation.

## 1. Prove the public boundary

```bash
cmake -S services/insta360-worker \
  -B /tmp/openstoryline-worker-public \
  -DINSTA360_ENABLE_SDK=OFF -DBUILD_TESTING=ON
cmake --build /tmp/openstoryline-worker-public --parallel
ctest --test-dir /tmp/openstoryline-worker-public --output-on-failure
/tmp/openstoryline-worker-public/insta360-worker capabilities
```

## 2. Build privately on an approved host

Use Ubuntu 22.04 x86-64, a supported GPU/runtime, and operator-supplied licensed
SDK and model paths outside the repository:

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
services/insta360-worker/local/build-sdk/insta360-worker capabilities
```

Do not use Insta360 Studio application libraries as SDK substitutes.

## 3. Private acceptance before enabling

The capability response must declare SDK enabled, decode and stitch true, the
expected SDK/worker versions, configured models, supported codec/stitch modes,
and the correct target platform. Then probe and stitch licensed representative
files for every camera/profile the operator intends to approve. Compare seams,
orientation, stabilization, duration, audio, and hashes against a Studio export.

Only after that evidence, configure either the local executable or the
authenticated HTTP worker:

```toml
[spherical]
enabled = true
enable_sdk_worker = true
worker_transport = "local"
worker_executable = "./services/insta360-worker/local/build-sdk/insta360-worker"
```

Raw discovery is advisory and every proposed input group/order requires human
confirmation. Never describe the public stub, compile test double, or a
capability response alone as proof of real-camera output quality.
