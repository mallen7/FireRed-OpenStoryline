# Synthetic equirectangular fixture generator

Phase 1 implements the generator as
`python -m open_storyline.spherical.cli generate-fixture`. No generated video
belongs in Git.

## Fixture design

- Default canvas: 1024x512 (exact 2:1), 30 fps, 8-10 seconds.
- Longitude grid every 30 degrees with readable yaw labels from -180 to 180.
- Latitude grid and pitch labels from -90 to 90.
- A deterministic colored background, visible grid/seam markers, and two
  moving reference boxes.
- An 880 Hz audio tone for duration and synchronization checks.
- A built-in 5x7 bitmap font for yaw/pitch labels.

The audited FFmpeg build does not contain the optional `drawtext` filter. The
generator writes a temporary labeled PPM with its built-in bitmap font, then
uses FFmpeg image, frame-evaluated `overlay`, color, and lavfi sine inputs through an argument vector.
It has no Pillow, font-file, or MoviePy dependency.

## Outputs

Given an explicit output directory, generate:

```text
fixture-equirectangular.mp4
asset.json
camera-track-static.json
camera-track-moving.json
camera-track-seam.json
fixture-generation-manifest.json
```

The generation manifest records generator/FFmpeg versions, the exact command,
dimensions, duration, frame rate, audio stream count, and artifact hashes.
Default generated paths may live under ignored `tests/fixtures/generated/`.

## Phase 1 verification uses

1. Validate exact 2:1 asset probing.
2. Render static yaw/pitch/FOV at known labeled regions.
3. Render a two-keyframe moving view.
4. Prove seam interpolation takes the shortest path.
5. Compare probed audio/video duration and stream metadata for synchronization.
6. Run identical requests twice and verify the second result is a validated
   cache hit.
7. Force a render validation failure and verify no valid-looking output or
   manifest remains.

Keep the generated fixture small enough for local integration tests while
retaining labels that survive the low-resolution preview render.
