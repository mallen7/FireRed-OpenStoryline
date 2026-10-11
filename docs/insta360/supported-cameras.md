# Supported-camera statement

## Public release

The checked-in build supports no raw Insta360 camera format. Its worker provider
is `stub`, and capability output says both `can_decode_insv=false` and
`can_stitch=false`. All cameras are supported only through a user-created,
already stitched, approximately 2:1 equirectangular MP4 that passes ffprobe and
import validation.

The Studio-export path has been validated locally with one Insta360 X5
PureVideo recording on 2026-07-25. Insta360 Studio 5.9.10 accepted the lone
marker-bearing `.insv`, identified a 5760x2880 panorama, and exported a valid
2:1 HEVC/AAC master that completed OpenStoryline's proxy, inspection, reframe,
audio, and ordinary-media handoff. This is evidence for that Studio workflow,
not a claim that the public worker can decode X5 `.insv` files.

## Private MediaSDK builds

This project does not maintain a speculative camera-model list. Installed worker
capabilities are the first gate:

- `provider=insta360-mediasdk`
- `sdk_enabled=true`
- `can_decode_insv=true` and `can_stitch=true`
- expected worker/SDK versions and target platform
- requested codec and stitch type present
- required model root configured
- no unresolved capability warning for the requested operation

The current protocol reports operational features, not camera model names. A
camera/firmware/codec/profile is therefore **approved only after** the private
operator records a successful probe and licensed real-footage stitch, validates
the 2:1 result, and compares orientation, seams, FlowState, audio, duration, and
quality with an approved Insta360 Studio reference. Capability output alone is
not a camera certification.

Keep the private approval matrix with worker version, SDK version, camera model,
firmware, capture mode, codec, lens count/order, GPU/driver, model bundle, test
hashes, result, reviewer, and date. Do not commit raw fixtures or licensed SDK
artifacts with that matrix.
