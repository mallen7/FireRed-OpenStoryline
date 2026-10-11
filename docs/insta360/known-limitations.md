# Known limitations

- Raw `.insv` is unavailable in the public runtime. The Studio-export workflow
  is the only checked-in raw-camera fallback.
- No licensed MediaSDK package, model bundle, Ubuntu/NVIDIA host, or real-camera
  footage was available for this macOS arm64 validation. Stub and compile-double
  tests do not establish raw output quality.
- Browser playback is a review aid. Final framing is created by the deterministic
  FFmpeg renderer and can differ slightly at sampled motion boundaries.
- FFmpeg 8.1.2 advertises `v360` runtime commands but rejects them at execution on
  the audited Mac. Animated cameras therefore use deterministic segmented
  rendering. Long, fast paths cost more and are sampled approximately.
- The tested media baseline is CFR H.264/AAC with one audio stream. Variable frame
  rate, surround/multiple audio tracks, very long footage, HDR/color metadata,
  and every HEVC profile are not release-qualified.
- Software `libx264` is the only deterministic final backend. No hardware encoder
  is selected automatically.
- Local ArtifactStore events are append-only files, not a transactional database.
  The web process serializes spherical mutations per session, but separate MCP
  and web processes can still contend. Cache entries use atomic validation but no
  cross-process per-key lease.
- Cache and uploaded-media retention have no aggregate byte quota. Monitor the
  configured volumes and use a reviewed retention policy.
- Lightweight analysis identifies visual person/face/object candidates, not
  semantic identity. Sampling can miss brief subjects; similar subjects can be
  confused at close crossings. Warnings and confidence require human review.
- Speaker selection has no confirmed diarization, spatial-audio identity, or
  guaranteed mouth evidence. Ambiguous candidates remain unresolved.
- Director semantic scoring is deterministic and coarse. Cuts materialize at the
  renderer sampling boundary rather than a dedicated frame-accurate cut graph.
- The A-Frame viewer and existing page libraries load from public CDNs unless an
  operator vendors them. An offline browser needs locally hosted approved copies.
- LLM/VLM-backed conversation acceptance was not exercised without deliberately
  configured provider credentials. The direct schemas, services, UI, and media
  paths do not require paid model calls.
- The distributed worker is single-host SQLite orchestration. Active-active
  schedulers and shared SQLite on object/eventually consistent storage are not
  supported.
- TLS termination, systemd hardening, reverse-proxy SSE behavior, Proxmox GPU
  passthrough, and licensed private runtime acceptance must be tested on their
  actual target host.
