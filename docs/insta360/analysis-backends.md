# Contributing an analysis backend

The public default is deterministic, offline, CPU-only, and dependency-light.
New semantic/GPU behavior must remain optional and must not change the meaning of
existing version `1.0` records silently.

## Extension boundary

Implement the protocols in `src/open_storyline/spherical/analysis_interfaces.py`:

- `ObjectDetector`, `PersonDetector`, and `FaceDetector`
- `SubjectTracker` and `ShotBoundaryDetector`
- `VisualEmbedder`

Bundle them behind an `AnalysisComponents` implementation from
`src/open_storyline/spherical/analysis.py`. It must expose stable `name` and
`version` values and return complete `AnalysisBackendInfo` metadata containing
component names, versions/parameters, and optional dependencies. Inject the
bundle through `SphericalAnalyzer(components=...)`; do not fork perspective
generation, spherical mapping, schema validation, caching, or review-artifact
generation.

## Required behavior

- Accept only bounded RGB perspective frames and return strict `ViewDetection`
  records with normalized boxes, confidence, backend name, and inspectable
  identity evidence.
- Never turn a face/person candidate into a confirmed identity without an
  explicit, approved identity contract and disclosure.
- Make sampling and resource limits configurable and cache-key every parameter,
  model/backend version, and relevant dependency version.
- Keep CPU fallback available. Missing weights/GPU/provider access must produce a
  clear capability error or an explicit fallback recorded in metadata; it must
  not silently claim the optional backend ran.
- Store weights, credentials, and private model licenses outside Git. Redact
  sensitive provider errors.
- Preserve seam-aware mapping, overlap/deduplication, confidence, warnings,
  review artifacts, and explicit suggestion approval.

## Tests and contribution evidence

Add synthetic tests for projection boundaries, seam crossings, vertical coverage,
deduplication, occlusion, similar-subject ambiguity, deterministic cache hits,
missing capability, bounded long input, and review artifact hashes. Add an
operator-owned real-footage evaluation outside Git when quality claims depend on
real people/cameras.

Run:

```bash
PYTHONPATH=src:. .venv/bin/python -m unittest \
  tests.test_spherical_analysis tests.test_spherical_direction -v
PYTHONPATH=src:. .venv/bin/python -m unittest discover -s tests -v
PYTHONPATH=src .venv/bin/python -m open_storyline.spherical.cli \
  export-schemas docs/insta360/schemas
```

Document new dependencies, license/privacy implications, resource envelopes,
quality limits, cache migration, and configuration. A new backend is not enabled
by default until the public fallback and existing normal-video suite remain green.
