# Install the spherical extension

## Requirements

- Python 3.11 or newer; Python 3.12 is the release-test version.
- FFmpeg and ffprobe with `v360`, `libx264`, and concat-demuxer support.
- Git and enough temporary disk for proxies, cache entries, and final renders.
- Network access on first page load for the existing browser CDN assets and
  A-Frame 1.8.0. Spherical media itself remains on the configured OpenStoryline
  host unless a remote worker is explicitly enabled.

On macOS, install FFmpeg with Homebrew. On Ubuntu, use the distribution package
or a reviewed build that provides the required filters and codecs.

## Source install

Run from a fresh clone:

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
bash download.sh
.venv/bin/python -m pip check
```

Enable only the public Studio-export path first:

```toml
[spherical]
enabled = true
enable_sdk_worker = false
worker_transport = "local"
workspace_dir = "./outputs"
cache_dir = "./.storyline/spherical-cache"
```

Start the services in separate terminals:

```bash
PYTHONPATH=src .venv/bin/python -m open_storyline.mcp.server
PYTHONPATH=src .venv/bin/python -m uvicorn agent_fastapi:app --host 127.0.0.1 --port 8005
```

Open `http://127.0.0.1:8005`, create or restore a conversation, and use the
`360°` button beside upload. The workspace is session-scoped. It displays only
spherical assets imported into that conversation.

## Verify the installation

```bash
PYTHONPATH=src:. .venv/bin/python -m compileall -q src tests agent_fastapi.py
PYTHONPATH=src:. .venv/bin/python -m unittest discover -s tests -v
node --test tests/spherical_ui_model.test.mjs
PYTHONPATH=src:. .venv/bin/python scripts/benchmark_spherical.py --quick
```

Generate disposable local fixtures only when diagnosing a media path:

```bash
PYTHONPATH=src .venv/bin/python -m open_storyline.spherical.cli \
  generate-fixture /tmp/openstoryline-spherical-fixture --overwrite
```

Do not add generated media, user footage, SDK packages, or model bundles to Git.
See [quickstart-studio.md](quickstart-studio.md) for the first edit.
