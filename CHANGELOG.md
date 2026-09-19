# Changelog

## 0.7.1

- Share one preparation path between `image_generate` and `image_generate_batch`, so a batch entry may use `continue_from` instead of raising an opaque error.
- Keep a parked follow-up holding its conversation. Parking frees the lane, but the job still intends to write to that thread, and releasing it let a second follow-up into the same conversation.
- Queue a whole batch in a single transaction, so a conflict on the last entry cannot leave earlier entries running behind a caller who was told the batch failed.
- Report an unreadable job row as `job_state_unreadable` inside the batch result instead of letting it end the whole poll.

## 0.7.0

- Add conversation continuity: `continue_from=<job_id>` asks the follow-up inside that job's own conversation, so ChatGPT still sees the earlier images and refinements such as "keep the layout, darker palette" behave the way they do for a person.
- Route a follow-up to the ChatGPT login that owns the conversation rather than to one browser profile. Conversations are account-scoped, so any free lane of that login can serve it; verified against the live site.
- Serialise writers per conversation so two follow-ups cannot interleave in one thread, while unrelated jobs keep using every other lane.
- Report a finished reply that produced no image immediately as `no_image_in_reply` (or `generation_failed` for an explicit refusal), carrying ChatGPT's own words, instead of waiting out the fifteen minute observation budget.
- Classify every error by who can act on it: `actionable_by` is `agent`, `retry` or `human`, and an unrecognised code never claims an agent can fix it.
- Guard source encoding: no byte order mark, no double-encoded text and readable Chinese selectors, because a PowerShell round trip silently corrupted them once.

## 0.6.1

- Compare conversation identity independently of browser layout. innerText reports what a reader sees, so a soft wrap inside a hyphenated word returned `On- Device` for a prompt that said `On-Device`, and that one invisible space made a finished job look like a foreign conversation for hours. Identity now also matches with whitespace removed, which still compares the whole prompt.
- Record both the exact and the layout-independent identity when a job starts, and use the same comparison for polling and for cancelling.

## 0.6.0

- Rewrite the MCP instructions so the surface itself teaches parallelism: concurrency is lanes rather than accounts, alternates of one prompt belong in one `count=N` request, different prompts belong in one `image_generate_batch` call, and callers must submit everything before polling anything.
- Add `image_generate_batch`: one call validates and queues a whole batch, filling every free lane at once, and stays idempotent per `request_id`.
- Let `image_poll` take `job_ids` and wait on a whole batch in one call, with `return_when='any'` for early delivery. Polling job by job was what turned a parallel batch back into a serial one.
- Report an unknown job id inside the batch result instead of failing or stalling the whole poll.
- Accept repeated `--job-id` on the CLI `poll` command for the same batch waiting.

## 0.5.1

- Point callers at `count=N` when several single-image jobs for one output directory are in flight at once: alternates of one prompt belong in one ChatGPT request, which costs one lane and one account request instead of N.
- Count only in-flight jobs for that hint, so retrying a finished job is never mistaken for a batch.
- State the request-shape rule in the tool description and skill: one prompt with several options is one request; genuinely different prompts stay separate jobs.

## 0.5.0

- Decouple concurrency from account count: one ChatGPT login can own many browser lanes, and `account-scale --lanes N` provisions or retires them in one call.
- Balance `auto` work across logins instead of letting whichever lane polls first take everything, so a burst is shared between the two accounts.
- Accept a login name wherever a lane alias is accepted, so pinning reaches any lane of that login.
- Release an idle lane's browser after five minutes and reopen it from the persistent profile on the next job, so a large lane pool does not hold memory while idle.
- Report per-login lane counts, usable lanes and load in `status`; cap total lanes at 16 because each lane is a full browser profile.
- Add `account-reseed` to refresh an idle lane's saved login from its group leader after a re-login.

## 0.4.0

- Park a paused job instead of letting it hold its account: the browser is released while the same account, conversation and downloaded files are kept, so the queue keeps draining.
- Re-observe parked jobs automatically on a 60/300/900 second backoff, at most three times, then wait for a human; never resend a prompt to recover one.
- Reload the recorded conversation before judging identity, and spend two forced reloads on a mismatch, so a stale DOM no longer pauses a job that is still generating.
- Re-check an unhealthy account on the same backoff instead of leaving the lane offline until a human runs an account check.
- Add `account-clone` so one signed-in account can serve several isolated browser lanes; concurrency is bounded by lanes, not by accounts.
- Slow background polling to 10 seconds while generation has produced nothing yet, and expose parked job counts per account.

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
