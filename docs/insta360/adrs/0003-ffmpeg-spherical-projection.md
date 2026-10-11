# ADR 0003: Use FFmpeg for spherical projection

- Status: Accepted
- Date: 2026-07-24

## Context

Panoramic masters can be very high resolution. OpenStoryline's final compositor
uses MoviePy, but routing spherical projection through decoded Python frame
objects would add memory pressure and obscure exact projection/backend behavior.

## Decision

Use FFmpeg directly for spherical projection and H.264 flat-clip output. Probe
the installed version, required filters, and runtime-control support; select the
safest verified filter strategy. Execute argument vectors without a shell and
record the exact command/filter graph in the manifest.

## Consequences

- High-resolution decode/filter/encode stays in a mature media pipeline.
- FFmpeg build variance requires capability probes and integration tests.
- Animated tracks may use runtime commands, generated graphs, or segmented
  renders depending on verified support.
- MoviePy remains the existing flat final compositor, not the spherical engine.
