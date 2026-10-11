# Migration notes

## Upstream baseline to spherical release

The extension is additive and disabled by default. Existing installations retain
ordinary behavior until `[spherical].enabled=true` is set. New configuration
fields have defaults, but operators should copy the complete `[spherical]` block
from `config.toml` when they need nondefault roots or a worker.

No upstream application database migration is required. Spherical assets,
camera-track revisions, suggestions, proposals, and render records are stored as
versioned session ArtifactStore events under the configured output root. The web
UI uses those same events; there is no parallel browser database to migrate.

The distributed worker owns SQLite state beneath its configured state directory.
Stop the service or use a SQLite-aware backup before upgrading. Preserve the DB,
WAL files, durable artifacts, token source, and private worker/model mounts. Its
startup migration/recovery path is covered by durability tests; do not point two
active schedulers at one SQLite file.

## Stable contracts

- Spherical asset/camera/render, analysis, director, and web UI schemas: `1.0`.
- Native stitch-worker protocol: `1.0`.
- Distributed HTTP/job schemas: `1.0`.

Reject unknown major/schema versions. After a schema change, regenerate and
review public contracts:

```bash
PYTHONPATH=src .venv/bin/python -m open_storyline.spherical.cli \
  export-schemas docs/insta360/schemas
PYTHONPATH=src .venv/bin/python -m open_storyline.spherical.worker_service \
  export-schemas services/insta360-worker/schemas
```

Cache keys include content and implementation/dependency versions. Old valid
entries can coexist; incompatible entries miss rather than being trusted. A
reviewed cache purge is recoverable but may be expensive. Never delete source or
final media as a cache migration.

Before enabling raw ingest after any SDK/worker update, repeat private capability,
camera/profile, cancellation, hash, ffprobe, seam, stabilization, audio, and
Studio-reference acceptance. An upgrade must not inherit an old camera approval
without evidence.
