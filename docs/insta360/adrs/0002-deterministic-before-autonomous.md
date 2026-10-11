# ADR 0002: Deliver deterministic reframing before autonomous direction

- Status: Accepted
- Date: 2026-07-24

## Context

Subject detection, speaker inference, and automated shot selection add uncertain
behavior. Without a proven camera contract and renderer, failures cannot be
separated into perception, planning, interpolation, or projection problems.

## Decision

Phase 1 accepts explicit human-authored camera keyframes and renders them
deterministically. Assisted analysis and autonomous director proposals build on
the same validated `VirtualCameraTrack` only after static, moving, seam-crossing,
audio, failure-cleanup, and cache behavior pass.

## Consequences

- Rendering can be tested with synthetic fixtures and exact expectations.
- Later AI outputs are inspectable inputs to an already trusted mechanism.
- Initial value is manual rather than automatic, and detection features arrive
  later.
- Automatic plans never bypass validation or directly overwrite accepted edits.
