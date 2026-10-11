# Spherical fork release checklist

Complete this checklist for every fork tag. Evidence belongs in release notes or
the CI run; do not check an item based on intention.

## Source and licensing

- [ ] `git status --short --ignored` reviewed; no SDK archives, headers,
  libraries, models, private worker binaries, license files, or SDK logs are tracked.
- [ ] No `.insv`, `.insp`, MP4 fixture, user footage, transcript, face crop, or
  generated output is tracked. Tests generate and delete synthetic media.
- [ ] Apache-2.0 and dependency notices reviewed; private SDK use remains under a
  separate operator license. See `licensing.md`.
- [ ] `origin` points to the fork, `upstream` points to FireRedTeam, and the
  upstream base SHA is recorded.

## Contracts, migrations, and security

- [ ] Native worker protocol version `1.0`, distributed API/schema version `1.0`,
  and spherical/UI schema version `1.0` are recorded in release notes.
- [ ] Public JSON Schemas were regenerated and the diff was reviewed.
- [ ] `migration.md` covers settings, ArtifactStore events, worker SQLite, cache,
  rollback/backup, and any release-specific change.
- [ ] Outside-root, traversal, symlink, upload, artifact-download, token/auth,
  redaction, cancellation, and request-limit tests pass.
- [ ] Tokens and private paths are absent from config, logs, docs examples, image
  layers, Git history, and artifacts.

## Regression and performance

- [ ] `.venv/bin/python -m pip check` passes.
- [ ] `PYTHONPATH=src:. .venv/bin/python -m compileall -q src tests agent_fastapi.py` passes.
- [ ] Full Python discovery passes, including existing ordinary media behavior.
- [ ] `node --test tests/spherical_ui_model.test.mjs` passes.
- [ ] Public C++ stub config/build/CTest passes with `INSTA360_ENABLE_SDK=OFF`.
- [ ] The 12 cases in `release-test-matrix.md` pass; no skipped case is reported as passed.
- [ ] Quick benchmark JSON is retained with host/commit; representative benchmark
  is compared with the prior tag for proxy, static/animated reframe, analysis,
  memory, cache, and output quality.

## Clean operation and documentation

- [ ] A clean clone/virtual environment completes `installation.md` with the
  documented Python and FFmpeg versions.
- [ ] Clean Studio-export smoke: import, panorama review, manual edit, preview,
  final flat render, ordinary-media handoff, and normal workflow all succeed.
- [ ] Spherical-disabled MCP/web startup and ordinary upload/edit smoke succeed.
- [ ] UI loading, empty, disabled, warning, worker unavailable, stale revision,
  render failure, cache hit, vertical, and responsive layouts were reviewed.
- [ ] Every command in installation, quick starts, remote guide, performance,
  troubleshooting, sync, and contribution docs matches `--help` or an executed command.
- [ ] Known limitations, privacy/network behavior, supported-camera policy, and
  proprietary boundaries match the shipped capability response.

## Private raw-ingest gate (when claimed)

- [ ] Approved Ubuntu 22.04 x86-64 GPU host records SDK/worker/model/GPU/driver versions.
- [ ] Every claimed camera/firmware/codec/profile has licensed real-footage probe,
  stitch, hash/ffprobe, seam, stabilization, audio, duration, cancellation, and
  Studio-reference evidence outside Git.
- [ ] TLS/firewall/VPN, token rotation, storage roots, backup/restore, disk quota,
  retention, systemd/container shutdown, diagnostics, and GPU passthrough drills pass.

If the private section is incomplete, release notes must say raw `.insv` is
unavailable and direct users to the Studio-export workflow.
