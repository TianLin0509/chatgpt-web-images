# Changelog

## 0.3.1

- Fix account admission after preparation failure, control starvation, repeated account controls, and concurrent worker launch storms.
- Replace executor-blocking MCP polling with asynchronous waits; expose queue blockers and worker operation health.
- Preserve durable runtime identity during crash recovery even if original references are no longer available.
- Renew observation deadlines on explicit resume, without resending; prevent switching an account owned by an active job.
- Bound cancellation failure retries, respect maintenance stop markers, and restore compact default output.
- Add isolated regression, runtime recovery and four-client / 96-wait stdio pressure checks.
- Fill the prompt before selecting the inline image tool and verify its picture_v2 marker before dispatch; return parseable JSON for MCP error results.

## 0.3.0

- Agent-neutral stdio tools backed by a shared SQLite queue and isolated per-account worker processes.
- Immediate submissions, two-account concurrency, persistent FIFO waiting and downloads independent of client lifetime.
- Cross-client idempotency, same-account crash recovery, bounded failures and explicit resume/cancel controls.
- Asynchronous account login controls, cached health, environment isolation and observe-only legacy migration.
- Claude Code registration instructions and universal MCP configuration; new process/concurrency regression coverage.

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
