# Deterministic spherical reframing

The deterministic renderer accepts an already stitched, approximately 2:1
equirectangular MP4 and an explicit `VirtualCameraTrack`. Phase 2 exposes it
through feature-gated OpenStoryline preview and full-render nodes. It still does
not read raw `.insv`, invoke an Insta360 SDK, or infer subjects.

## Renderer lifecycle

1. Strict Pydantic contracts validate schema versions, source ranges,
   keyframes, camera values, output aspect/dimensions, and the workspace path.
2. The source MP4 is hashed and probed; the declared dimensions, duration,
   hash, and selected audio stream must match.
3. FFmpeg is feature-probed for `v360`, `libx264`, concat, and a real runtime
   yaw command.
4. The track is sampled at frame-aligned interval midpoints. Static tracks use
   one segment; moving tracks use the configured interval.
5. Each static-camera segment is rendered through `v360` to identical H.264
   settings, then concatenated without another video encode.
6. The requested source audio interval is encoded to AAC and muxed once. A
   video-only asset stays video-only.
7. Dimensions, duration, requested audio, and the output hash are validated in
   a temporary directory.
8. The validated output and sidecar manifest are atomically moved into place.

If any step fails, temporary media disappears and no new valid-looking output
or manifest is committed. Public renderer entrypoints translate unexpected
exceptions to a typed `SphericalRenderError`.

## Caching and provenance

The render key contains the source content hash, source interval, normalized
full camera track, output dimensions, codec settings, sampling interval,
FFmpeg version, and renderer version. It deliberately excludes the destination
path. Cache hits require a valid manifest, matching render key, existing MP4,
and a matching output SHA-256.

Every render sidecar records:

- source, track, render, and output hashes;
- FFmpeg/capability/application/renderer versions;
- every exact argument vector and `v360` filter graph;
- codec and hardware-acceleration settings;
- selected backend, render time, source interval, output geometry, and audio;
- cache status, warnings, and fallback decisions.

## Approximation and quality controls

The audited FFmpeg 8.1.2 build rejects runtime `v360` camera commands, so moving
cameras are a sequence of static views. A shorter sampling interval more
closely follows the intended camera curve but creates more encoder processes.
The default is 0.25 seconds, the supported range is 0.05-5.0 seconds, and a
request is rejected above 5,000 segments. Cache identity includes the interval,
so quality changes cannot silently reuse an older output.

Initial output is software `libx264`, `yuv420p`, with even 16:9, 9:16, or 1:1
dimensions. Hardware encoding is intentionally not treated as equivalent.

## Phase 2 preview and handoff

Preview keeps the track's aspect ratio but scales it down to the configured
`preview_max_dimension`, then invokes the same renderer and cache validation as
a full render. A preview is for review; it does not replace the approved track.
Full render uses the track's declared dimensions and writes inside the current
session output root.

Node responses intentionally return compact hashes, paths, versions, timings,
warnings, and fallback decisions rather than the renderer's full command list.
The complete exact-command provenance remains in the sidecar manifest. A full
flat MP4 is then eligible for the ordinary `load_media` path; its 2:1 master is
not.
