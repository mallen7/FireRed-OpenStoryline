# ADR 0004: Use strict versioned JSON contracts

- Status: Accepted
- Date: 2026-07-24

## Context

Assets, camera plans, manifests, worker jobs, analysis results, and remote events
cross process and potentially machine boundaries. Unversioned permissive data
would make cache validity and upgrades ambiguous.

## Decision

Define strict Pydantic models and publish corresponding JSON Schemas. Every
top-level contract includes `schema_version`; unknown fields and unsupported
versions are rejected unless a later compatibility policy explicitly allows
them. Normalize before deterministic hashing.

## Consequences

- Tools, workers, UI, tests, and cache validators share machine-checkable data.
- Schema changes require intentional compatibility/migration work.
- Stored JSON remains inspectable and editable rather than hidden in opaque
  application state.
- The Phase 0 base contract is intentionally minimal; concrete models arrive in
  their owning phases.
