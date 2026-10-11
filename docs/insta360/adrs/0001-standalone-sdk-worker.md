# ADR 0001: Isolate the Insta360 SDK in a standalone worker

- Status: Accepted
- Date: 2026-07-24

## Context

The Insta360 MediaSDK is proprietary, platform-specific, model-dependent, and
capable of destabilizing the host process through native ABI/runtime conflicts.
OpenStoryline is a Python application that must remain installable without it.

## Decision

Bind the SDK only inside a standalone C++ worker. OpenStoryline communicates by
versioned JSON, first over a local CLI/subprocess transport and later through an
authenticated HTTP transport. A public stub may test protocol/error paths but
must state that it cannot decode `.insv`.

## Consequences

- Public installs, tests, and ordinary video workflows remain SDK-independent.
- Crashes and native dependencies are isolated, and a remote Ubuntu/GPU host is
  possible.
- Transport, lifecycle, capability, and artifact validation code is required.
- Raw input is unavailable when the worker reports no SDK capability; there is
  no in-process fallback.
