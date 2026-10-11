# Privacy and local processing

## What stays local by default

With `enable_sdk_worker=false`, spherical import, proxy generation, contact
sheets, lightweight analysis, camera-track storage, preview rendering, and final
flat rendering run on the OpenStoryline host. Media is served to the browser from
that same host. The extension does not upload footage to Insta360 or another
third party.

Session artifacts contain media paths, content hashes, camera plans, timestamps,
analysis candidates, confidence, warnings, render metadata, and review outputs.
Treat the configured media, output, cache, and session-state directories as
sensitive. Hashes are identifiers, not anonymization.

## Explicit network paths

- The web page currently downloads A-Frame 1.8.0 from `aframe.io`; existing page
  branding/Markdown sanitization also uses configured public CDNs. These are
  static-library requests, not spherical media uploads. Vendor and self-host
  reviewed copies for a fully offline browser.
- Core OpenStoryline can call configured LLM, VLM, TTS, transition, or media-search
  providers. Their privacy terms remain independent of this extension.
- When `worker_transport=http`, explicitly selected input paths or uploaded bytes,
  job metadata, hashes, and results go to the configured worker. Use a private
  encrypted network/TLS, least-privilege roots, and a secret bearer token.

## Logs and retention

Worker responses bound and sanitize messages; known filesystem paths and bearer
tokens must not enter logs. SDK diagnostic artifacts can still be sensitive and
remain private. Durable job history defaults to retained metadata; cleanup of
history does not delete media. Define separate reviewed retention and backup
policies for state, media, output, cache, and SDK logs.

Before sharing a bug report, remove media, absolute paths, tokens, private SDK
details, subject images, transcript content, and content hashes unless each item
is intentionally approved for disclosure.
