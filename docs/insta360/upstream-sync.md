# Upstream synchronization policy

## Remotes and history policy

The canonical upstream is:

```text
https://github.com/FireRedTeam/FireRed-OpenStoryline.git
```

Configure `upstream` to that URL and `origin` to the actual fork. This working
tree was based on upstream commit `04297707e7607dd398e906262235d0797068e7b4`.
Do not push while both remotes still target upstream.

For an unpublished integration branch, prefer rebase onto `upstream/main` so the
fork delta remains reviewable. For a shared/released branch, do not rewrite
published history; merge `upstream/main` with a dedicated synchronization PR.
Never mix a large upstream sync with feature work.

## Procedure

```bash
git status --short
git remote -v
git fetch upstream --prune
git log --oneline --decorate --left-right HEAD...upstream/main
git switch -c sync/upstream-YYYYMMDD
git rebase upstream/main
```

Use `git merge --no-ff upstream/main` instead of the last command for a published
branch. Resolve only after comparing both sides; never take an entire conflicted
file automatically when it crosses extension boundaries.

Most likely conflicts:

- `agent_fastapi.py`, `config.toml`, `requirements.txt`, and `.gitignore`;
- `src/open_storyline/config.py`, MCP registration/interceptors, and ArtifactStore;
- `web/index.html`, `web/static/app.js`, and `web/static/style.css`;
- workflow files and upstream documentation.

Low-conflict extension-owned areas are `src/open_storyline/spherical/`,
`src/open_storyline/nodes/spherical_nodes/`, `services/insta360-worker/`,
`docs/insta360/`, spherical tests, and the spherical workflow skill. Preserve the
feature gate and keep core normal-media behavior upstream-compatible.

## Required post-sync validation

```bash
.venv/bin/python -m pip check
PYTHONPATH=src:. .venv/bin/python -m compileall -q src tests agent_fastapi.py
PYTHONPATH=src:. .venv/bin/python -m unittest discover -s tests -v
node --test tests/spherical_ui_model.test.mjs
PYTHONPATH=src:. .venv/bin/python scripts/benchmark_spherical.py --quick
cmake -S services/insta360-worker -B /tmp/openstoryline-worker-sync \
  -DINSTA360_ENABLE_SDK=OFF -DBUILD_TESTING=ON
cmake --build /tmp/openstoryline-worker-sync --parallel
ctest --test-dir /tmp/openstoryline-worker-sync --output-on-failure
```

Also start MCP/web with spherical disabled and enabled, open the ordinary editor,
open the 360 workspace, and complete the Studio-export smoke test. Compare schema
exports without blindly accepting changes.

Fork releases use annotated tags such as `insta360-v0.1.0-fork.1`. Record the
upstream base SHA, worker protocol `1.0`, UI schema `1.0`, migration notes, public
versus private validation, and exact commands in the release notes.
