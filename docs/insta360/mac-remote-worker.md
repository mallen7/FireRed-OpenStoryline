# macOS client with a remote Ubuntu/GPU worker

Use the Mac for OpenStoryline and UI review, and an approved Ubuntu 22.04 x86-64
host for licensed MediaSDK operations. The service reuses the existing worker,
FFmpeg renderer, analyzer, and strict contracts; it is not a second media stack.

## Worker host

Follow `services/insta360-worker/deploy/README.md`. Bind only to a private VPN or
management address, put TLS in front of untrusted links, restrict TCP 8090 to
OpenStoryline clients, and keep the bearer token in a root-readable file or
secret store. Check:

```bash
curl https://worker.example.internal/health/live
curl https://worker.example.internal/health/ready
curl -H "Authorization: Bearer $OPENSTORYLINE_WORKER_TOKEN" \
  https://worker.example.internal/v1/capabilities
```

Readiness can be true for reframe/analysis while raw stitching remains false.
Require stitch readiness only after private SDK acceptance.

## Mac configuration

Keep the token out of TOML:

```bash
export OPENSTORYLINE_WORKER_TOKEN='read-from-an-approved-secret-source'
```

```toml
[spherical]
enabled = true
enable_sdk_worker = true
worker_transport = "http"
worker_url = "https://worker.example.internal"
worker_token_env = "OPENSTORYLINE_WORKER_TOKEN"
worker_verify_tls = true
worker_timeout_seconds = 86400
```

Restart MCP and the web server after a config change. The 360 workspace worker
card must say `ready` and show decode/stitch available before raw work is offered.

## Storage choice

- Shared storage is preferred for multi-gigabyte recordings. Configure the same
  canonical media/output roots on both hosts.
- HTTP upload/download is bounded and hash-verified, but less efficient. Use it
  only through TLS with suitable proxy timeouts.
- The bearer token authenticates; it does not encrypt. Never expose plaintext
  port 8090 to an untrusted network.

Run compatibility diagnostics from the Mac before a production job:

```bash
export OPENSTORYLINE_WORKER_REMOTE_TOKEN="$OPENSTORYLINE_WORKER_TOKEN"
PYTHONPATH=src .venv/bin/python -m open_storyline.spherical.worker_service diagnose \
  --remote-url https://worker.example.internal
```
