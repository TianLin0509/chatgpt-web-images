# Changelog

## 0.2.0

- Compact text-only MCP responses, with full provenance available via `detail=true`.
- Internal polling with a 0–45 second wait budget (default 40 in MCP).
- Persistent `request_id` deduplication, input-conflict detection and retry recovery.
- Browser failures retain job/checkpoint context; a window-hide failure after submission no longer releases the job as a preparation failure.
- Installable Python wheel, explicit runtime setup/doctor, portable local settings and manual first login without Hub dependencies.
- English/Chinese documentation, MIT license, CI, release hygiene checks and protocol tests.

## 0.1.0 (local prototype)

- Isolated ChatGPT web browser, reference images, single-request multi-image generation.
- Requested/observed/downloaded count checks, native original downloads, per-file checkpoints and SHA-256 verification.
- Real local validation of five independent images from one web submission.
