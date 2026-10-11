# Ubuntu/NVIDIA distributed-worker deployment

This deployment keeps the proprietary MediaSDK, models, and SDK-enabled worker
binary outside Git. The public image contains the tested stub worker and can run
reframe/analysis jobs, health checks, diagnostics, and the complete HTTP job
protocol. Mount a privately built SDK-enabled binary and approved model directory
to enable raw `.insv` stitching.

## Ubuntu 22.04 host

1. Install current security updates, FFmpeg, Python 3, and either Docker Engine
   with the Compose plugin or the systemd prerequisites.
2. Install the NVIDIA driver on the host and confirm `nvidia-smi` works before
   installing NVIDIA Container Toolkit. Configure Docker with the NVIDIA runtime
   and confirm a test container can see the GPU.
3. Create dedicated state, media, upload, and output directories. Media may be
   read-only; state, uploads, and outputs must be writable by UID 10001 for the
   supplied container.
4. Generate at least 32 random bytes for the bearer token. Store it in a root-only
   file, never in Compose YAML, shell history, logs, or Git.
5. Put the licensed SDK, model directory, and private SDK-enabled worker on the
   host. Mount them read-only as shown in `docker-compose.yml`.

Run Compose from the repository root:

```bash
export OPENSTORYLINE_WORKER_TOKEN_FILE=/etc/openstoryline/insta360-worker.token
export WORKER_BIND_ADDRESS=10.20.30.40
docker compose -f services/insta360-worker/docker-compose.yml up -d --build
```

The default public build reports stitch capability as unavailable instead of
pretending it decoded `.insv`.

## Systemd option

Create the unprivileged `openstoryline-worker` account, install the repository
and virtual environment under `/opt/openstoryline`, copy the example environment
file to `/etc/openstoryline/insta360-worker.env`, and install
`insta360-worker.service`. The token file and environment file should be root-owned
and not world-readable. `KillMode=control-group` ensures cancellation or shutdown
reaches isolated task, FFmpeg, and SDK child processes.

## Proxmox GPU passthrough

- Prefer PCIe passthrough of the entire GPU to an Ubuntu 22.04 VM.
- Enable IOMMU in the Proxmox host firmware/kernel and confirm the GPU and its
  companion audio function are isolated in a safe IOMMU group.
- Bind the devices to VFIO on the host, attach every required function to the VM,
  then install the NVIDIA driver inside the guest.
- Do not expose the same consumer GPU concurrently to the host and guest.
- Validate `nvidia-smi`, FFmpeg filters, and the private MediaSDK worker inside the
  VM before enabling `OPENSTORYLINE_WORKER_REQUIRE_STITCH=1`.

Exact passthrough settings are hardware- and Proxmox-version-specific; keep a
console/recovery path before changing the bootloader or VFIO binding.

## Storage modes

Shared storage is preferred. Mount the same media and output paths on the Mac and
worker, or map them consistently before constructing job requests. The server
canonicalizes every path and rejects symlink/traversal escapes.

For non-shared inputs, `POST /v1/uploads/{relative_path}` streams `.insv` or `.mp4`
into the bounded upload root and returns its SHA-256. Completed artifacts can be
downloaded from `/v1/jobs/{job_id}/artifacts/{artifact_name}` with integrity
validation. For multi-gigabyte originals, put TLS and request-timeout-aware proxy
configuration in front of the worker; shared storage remains more efficient.

## Network and firewall

- Bind the service only to a private management/VPN address.
- Permit TCP 8090 only from OpenStoryline hosts or the TLS reverse proxy.
- Bearer tokens do not protect plaintext transport; use TLS or a trusted encrypted
  tunnel across any untrusted network.
- Do not expose SDK/model or media directories through a general file server.
- Health endpoints are unauthenticated and reveal only liveness/readiness booleans;
  job, metrics, diagnostics, uploads, and artifacts require the bearer token.

## Backup and cleanup

Back up `/state/jobs.sqlite3` together with its WAL files using SQLite-aware backup
or stop the service before copying. Back up outputs according to their retention
requirements. Restart recovery requeues interrupted attempts and preserves
completed job results. Attempt work directories are removed after terminal jobs.
Database history defaults to 30 days and 10,000 records; pruning never deletes
media artifacts. Delete media through a separate reviewed retention process.

## Diagnostics

Run locally on the worker:

```bash
python -m open_storyline.spherical.worker_service diagnose
```

Check network reachability and API compatibility from another configured host:

```bash
export OPENSTORYLINE_WORKER_REMOTE_TOKEN='use-a-secret-source-in-production'
python -m open_storyline.spherical.worker_service diagnose \
  --remote-url https://worker.example.internal
```

The report checks OS/architecture, FFmpeg and required filters, NVIDIA visibility,
driver/CUDA signals, optional Vulkan tooling, SDK capability, model presence,
writable directories, network reachability, and API-version compatibility without
listing proprietary SDK contents.
