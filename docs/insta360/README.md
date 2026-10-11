# Spherical and Insta360 extension

This directory documents the optional, disabled-by-default spherical workflow.
The public path starts with a stitched 2:1 equirectangular MP4. Raw `.insv`
requires a separately licensed, capable MediaSDK worker; the checked-in public
worker deliberately cannot decode it.

## Start here

- [Installation](installation.md)
- [Studio-export quick start](quickstart-studio.md)
- [MediaSDK quick start](quickstart-mediasdk.md)
- [Mac plus remote worker](mac-remote-worker.md)
- [Supported-camera policy](supported-cameras.md)
- [Troubleshooting](troubleshooting.md)

## Release and maintenance

- [Known limitations](known-limitations.md)
- [Privacy and local processing](privacy.md)
- [Licensing](licensing.md)
- [Analysis-backend contributions](analysis-backends.md)
- [Performance qualification](performance.md)
- [Regression matrix](release-test-matrix.md)
- [Upstream synchronization](upstream-sync.md)
- [Migration notes](migration.md)
- [Release checklist](release-checklist.md)

Architecture decisions and current evidence remain in [architecture.md](architecture.md),
[development.md](development.md), and [implementation-status.md](implementation-status.md).
