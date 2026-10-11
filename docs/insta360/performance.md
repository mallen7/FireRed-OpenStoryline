# Performance and quality qualification

`scripts/benchmark_spherical.py` creates labeled synthetic media in a temporary
directory, measures the pipeline, prints versioned JSON, and deletes all media at
exit. It never needs a checked-in benchmark video.

Quick CI/developer profile:

```bash
PYTHONPATH=src:. .venv/bin/python scripts/benchmark_spherical.py --quick
```

Representative local profile with JSON retained but media discarded:

```bash
PYTHONPATH=src:. .venv/bin/python scripts/benchmark_spherical.py \
  --output /tmp/openstoryline-spherical-performance.json
```

The harness records:

- first proxy generation and validated proxy-cache replay;
- static reframe at 16:9, 9:16, and 1:1 representative resolutions;
- animated segmented reframe and validated render-cache replay;
- lightweight analysis throughput and analysis-cache replay;
- wall time, realtime factor/processing FPS where meaningful, Python allocation
  peak, parent peak RSS, and child-process peak RSS;
- output resolution, duration drift, audio stream count, codec, bytes, CRF, and
  PSNR of the first CRF-18 output against a CRF-0 reference.

Peak RSS is a process-lifetime high-water mark, not an isolated per-operation
delta. For a production qualification, run the script in a fresh process on an
otherwise idle target, retain the JSON with host/build identifiers, and monitor
GPU memory separately when a private backend uses it.

Do not establish universal pass/fail thresholds from the tiny fixture. A release
owner should compare results with the previous tag on the same host and review
large regressions in wall time, cache replay, memory, duration drift, audio,
resolution, or PSNR. Real-camera quality evaluation remains private and must add
seam, stabilization, orientation, HDR/color, long-duration sync, and approved
Studio-reference review without committing the media.
