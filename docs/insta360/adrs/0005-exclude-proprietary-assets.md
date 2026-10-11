# ADR 0005: Exclude proprietary SDK assets from Git

- Status: Accepted
- Date: 2026-07-24

## Context

SDK headers, libraries, packages, models, examples, and user footage may be
license-restricted, large, sensitive, or unsuitable for public distribution.

## Decision

Keep all proprietary SDK/model material and raw camera/user media in ignored,
operator-managed locations. Public build logic references explicit local paths
and defaults to SDK disabled. Public images and CI never copy or package those
assets.

## Consequences

- The public repository remains redistributable and testable without vendor
  files.
- Operators must separately acquire, license, and configure the SDK.
- Private build/release procedures must inspect contents and capabilities.
- Missing assets produce an explicit capability/build error, never a fake or
  silent decoder fallback.
