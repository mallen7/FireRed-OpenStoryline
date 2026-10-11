# ADR 0006: Use a 2:1 equirectangular master as interchange

- Status: Accepted
- Date: 2026-07-24

## Context

Raw camera formats tie all downstream work to proprietary decoding and camera
pairing. Analysis, preview, projection, and ordinary editing need a stable,
widely inspectable boundary.

## Decision

The worker, when present, outputs a stabilized stitched 2:1 equirectangular
master. Studio exports use the same boundary. All spherical analysis and virtual
camera rendering consume that master; ordinary OpenStoryline nodes consume only
the resulting flat clips.

## Consequences

- SDK and Studio ingest converge on one deterministic downstream pipeline.
- Master generation may be storage- and compute-intensive but is cacheable.
- Projection, stabilization, stitch type, provider, and hashes must be recorded
  rather than inferred.
- Unsupported or ambiguous raw pairs cannot be passed directly to normal video
  nodes.
