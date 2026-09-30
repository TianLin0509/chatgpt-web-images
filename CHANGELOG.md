# Changelog

## 0.7.23

- A lane that meets a human check leaves the page at once. The check page reloads itself about every 85 s and each failure raises Cloudflare's partitioned retry counter; observed 2026-09-29, enough of them made even the person loop on "Verifying". 0.7.22's rule that kept a gated lane's page open is reversed; only a page just shown to a person by `open` stays.
- Sibling lanes of a login with a pending human gate take no new work.
- Lanes in the AI Hub shared browser: `open` becomes a person handoff through the Hub's shared guard (`core/web-risk-guard.js`): every Hub web tool pauses and whole-browser connections detach (the tab daemon closes its Playwright connection), challenge-state cookies of that site are reset (login cookies untouched), and an on-screen window with no debugger attached opens. Closing that window or `image_account_check` ends the handoff; it expires after 15 minutes. Older Hubs keep the previous behaviour.
- Workers neither dispatch nor poll during a handoff and never charge a job for it (`human_handoff`, actionable by `retry`). A Hub refusal for a paused site maps to `browser_challenge`.
- The tab daemon records a challenge reported by tool code and leaves that page, so every Hub tool sees the pause.
- Cloudflare's localised title (请稍候) counts as a challenge.

## 0.7.22

- An idle lane no longer moves its tab to about:blank while a person is using it: while its login has a pending human gate, or for 30 minutes after an explicit `open`. Observed 2026-09-29: a restarted worker navigated the secondary lane away from the page where the user was completing the Cloudflare check and login.

## 0.7.21

- The wheel ships `chatgpt_interaction`. It was missing from `py-modules` since 0.7.10, so every pip-installed command failed at start with `ModuleNotFoundError`; running from the source tree was unaffected, which is why CI (run from `scripts/`) never saw it. CI now imports every runtime module from the installed wheel outside the source tree.

## 0.7.20

- A login that needs a person (Cloudflare check, expired login, password prompt, account chooser) is announced once per login with a Windows toast that does not take focus, again after 2 hours if still unhandled, and cleared when the login authenticates. Observed 2026-09-27: the secondary login sat on a Cloudflare check for two days unnoticed.
- `image_status` adds `human_action` (login, gate, account, next action, egress at the gate, whether it differs from the last working egress) and `egress_last_ok`. Egress comes from Cloudflare's `/cdn-cgi/trace` on chatgpt.com over the Windows system proxy Chrome uses, at most once an hour while healthy; lane pages are never probed. `CHATGPT_WEB_IMAGES_NOTIFY=0` disables toasts.
- A transient `SQLITE_BUSY`/`SQLITE_LOCKED` tick error no longer marks the lane `worker_error`, and no worker error overwrites a pending human gate. Observed 2026-09-29: one busy tick took all eight lanes offline at the same second (parallel capacity 0) and erased the secondary login's Cloudflare state.

## 0.7.19

- Cancelling a parked job finishes immediately. A parked job owns no browser page, so no worker ever claimed it to honour a cancel request; cancelled jobs on disabled lanes stayed parked forever and callers such as the weekly-report connectors kept polling them.

## 0.7.18

- The server, not only the page, decides whether an image turn has finished. After 90 s without rendered images a job asks the recorded conversation once (then every 60 s, backing off to 120/300 s on HTTP 429) and reloads the conversation, at most three times, only when the server reports a finished turn with image assets. Observed 2026-09-27: the page stayed on "Thinking" after its own reads were rate-limited while the server already held the finished 1672x941 image.

## 0.7.17

- A lane reuses its recorded tab whenever that tab still exists, instead of opening another one when the Hub's short health check misses it. Twenty-three ownerless ChatGPT tabs had accumulated in the Hub browser (12 stuck on a Cloudflare check), all polling the same login into HTTP 429.
- The tab daemon records every tab it opens or reuses for a lane and every 10 minutes closes only those that no lane owns any more. Tabs it never registered (the user's, the Hub's, other tools') are never touched.

## 0.7.16

- Login detection accepts the Chinese profile-menu label. One account's tabs can render in different UI languages (a zh-CN tab loaded earlier beside a fresh en-US tab, observed 2026-09-27); the zh-CN tab was reported page_not_ready indefinitely.
- An idle or new-task ChatGPT page that renders a non-English UI is reloaded once, so English control names used by the composer, tool and download steps match. A job's own conversation is never reloaded and nothing is submitted.

## 0.7.15

- Tab daemon survives failed steps. A failed step left an unobserved rejected promise on the lane chain, which terminates Node, so the first closed tab or page error took the shared connection down and every lane fell back to per-step attaches.

## 0.7.14

- An idle lane moves its own tab to about:blank once per idle period. A finished conversation left open kept polling ChatGPT; when those reads returned HTTP 429, every status check renewed the whole login's cooldown, so one idle tab stalled all lanes for hours (observed 2026-09-27: 429s only in the idle tab, none in the busy one).
- HTTP 429 on reading a conversation other than the current one counts as background, not as a blocking rate limit. Submission-path and current-conversation 429s still block.

## 0.7.13

- Add a long-lived tab daemon per Hub identity (`tab_daemon.cjs`) and a drop-in lane entry (`tab_client.cjs`). Every lane still owns its own tab, so an identity keeps generating in parallel like a person with several ChatGPT tabs; only the browser connection is shared. The per-step transport attached Playwright to the whole Hub Chrome for every queue poll (measured 9-19 s per attach), which caused step timeouts and browser-wide refetch bursts that ended in HTTP 429. Steps now take about 0.2 s after one attach.
- `use_tab_daemon.py --pool <pool>` switches Hub-bound lanes to the daemon and records the previous entry; `--revert` restores it. If the daemon is unreachable a step falls back to the Hub's own transport.

## 0.7.12

- Launch lane browsers with Chrome's background-safe switches (no timer throttling, no occluded-window backgrounding, no renderer backgrounding, Windows occlusion tracking off), so an off-screen lane runs at foreground speed instead of relying on per-step focus emulation.
- Keep an idle lane's browser warm for 6 hours by default (`CHATGPT_WEB_IMAGES_IDLE_RELEASE_SECONDS`). Releasing after 5 minutes forced a cold Chrome/Cloudflare/ChatGPT start on most jobs, which is where most browser_timeout and browser_not_open failures occurred.

## 0.7.11

- Handle the observed hidden-but-focused Chrome state: temporarily emulate visibility for owned-page input even when document.hasFocus() remains true.


## 0.7.10

- Temporarily emulate focus only in the owned page during prompt input, image-tool selection and submission. Background polls stay passive; hidden composer menus work without waking other tabs.
- Restore focus emulation and detach the scoped CDP session on success and error.


## 0.7.9

- After provider cooling, allow one lane to continue a real task under a durable exclusive lease, prioritizing live foreground jobs over parked retries. Keep the probe exclusive until a verified original is saved. A healthy homepage or accepted prompt no longer clears a blocked conversation. Rejected task probes retain 5/15/30-minute escalation instead of repeatedly resetting to five minutes.
- Keep request identity, job failure count and parked recovery budget unchanged for HTTP 429. Explicit account checks report the shared wait without generating another provider request, including after its nominal deadline.
- A new task verifies its own fresh composer, not a failed conversation left in the released lane. Authentication challenges retain priority and no prompt is resent by this preparation step.

## 0.7.8

- Load pending generated gallery assets eagerly in hidden/offscreen Chrome. This avoids waiting forever for lazy thumbnails while retaining hidden windows and checking exact prompt identity before downloading. User uploads and earlier turns are excluded; no window focus changes or prompt resubmission.
- Finish an interrupted cold navigation when the already-owned tab is still `about:blank`, instead of waiting forever or creating another tab. Normal conversations and human authentication gates keep their existing behavior.

## 0.7.7

- Distinguish a throttled sidebar or background conversation refetch from a blocked generation. A rendered authenticated conversation remains observable and downloadable; prompt ownership is still checked before accepting images. Unreadable conversations and generation HTTP 429 retain shared cooldown.
- Pair with the Hub browser transport fix using Playwright `noDefaults: true`: attaching to one owned tab must not simulate focus across every ChatGPT tab and trigger a refetch storm. No additional dependencies, account changes or prompt resubmission.

## 0.7.6

- Recognize observed ChatGPT HTTP 429 responses without sending an extra probe. Keep rate limits separate from sign-out and page loading.
- Persist cooldown per login across workers and clients, with 5/15/30-minute backoff and one recovery probe. Concurrent reports coalesce and do not multiply the cooling period.
- Retain queued, active and parked request identity during cooling; expose retry times and wait actions. Keep explicit browser opening available and stop batch summaries from calling parked work deliverable.

## 0.7.5

- Distinguish an authenticated page shell that has not loaded its composer from an explicit sign-in page. Report `page_not_ready` instead of falsely declaring the account logged out.
- During a real task or explicit account check, refresh the same owned ChatGPT page once after the loading wait expires. Never resend its prompt; persistent loading remains bounded and retryable.
- Recognize both current and legacy profile/composer elements directly in the shared account inspector.

## 0.7.4

- Support the current user-turn markup and generated-image galleries alongside legacy message nodes. Scope originals to the verified latest prompt, deduplicate preview/thumbnail copies and keep stable keys across page reloads.
- Download verified gallery originals from the authenticated page, including fsns assets and cached responses with empty CDP bodies. Compare SHA-256 with the displayed asset, cache candidate hashes and return metadata only over MCP.
- Bound unreadable-conversation waiting separately from generation; expose observation and download stages and refuse cancellation without prompt identity evidence.
- Stop automatic idle health navigation and repeated login/challenge checks. Keep explicit account actions and bounded recovery for queued work. Poll idle local queues every three seconds.
- Preserve generic stdio MCP, account separation, request deduplication, batch generation, references, continuations and checkpoints. Add real headless Chromium fixtures for gallery, download and recovery contracts.

## 0.7.3

- Save verified full-size generated assets from authenticated browser responses; preserve original bytes and retain UI fallback for unrecognized sources.
- Exclude uploaded references and transient action-footer text from completion checks. Skip redundant cold navigation, observe generation every three seconds and complete immediately after the last saved image.
- Retry transient Windows state-file sharing violations and keep health telemetry from terminating workers. Record preparation substeps and timing metadata.

## 0.7.2

- Launch every lane window off screen. Hiding can only happen once a window exists, and reaching that point costs two child processes, so a cold lane used to flash on screen for seconds; with four lanes starting at once that was four windows. Measured with a 0.5s sampler over 150s of cold opens: 0 windows on screen, against 4 before.
- Stop Chrome opening its own windows: after an unclean shutdown it showed a crash-restore bubble titled "Restore pages?", which the window lookup cannot match and therefore never hid.
- Hide a lane's window the moment the browser opens rather than after a prompt has been submitted, and hide it again after adopting a parked conversation. Hiding still runs so a lane stays out of the taskbar as well.
- Bring a window back on screen when `image_open` asks for it, since it is now created off screen.
- Retry the window lookup: the title is published to Windows asynchronously, and a single miss left that lane visible permanently.
- Treat hiding as cosmetic — a lane that cannot hide is still a working lane, and the failure no longer propagates.
- Keep `--headed` deliberately. Measured on a scratch profile seeded with a real login: a headless browser is answered by Cloudflare's "Just a moment..." interstitial and never reaches the app, so invisibility comes from window placement, not from running headless.

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
