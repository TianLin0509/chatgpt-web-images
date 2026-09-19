# ChatGPT Web Images 0.3.1

Reliability update: pending account actions coalesce and alternate with job work; asynchronous MCP waits do not occupy executor threads. Cross-process launch reservations prevent worker startup storms. Browser preparation/generation failures pause account admission until an explicit successful check. Queue results explain blocking accounts, and worker health distinguishes a live heartbeat from an operation stalled for over 300 seconds. Stalled browser operations are reported, not force-killed or resubmitted. Explicit resume renews observation time and refreshes only the verified original conversation; active jobs prevent account switching. Restart workers between operations and reload existing MCP sessions to activate these changes.

Unofficial Windows stdio MCP for your own ChatGPT web accounts. Original files are saved locally; tool results contain text and paths, never image base64. See [the complete setup guide](README.md).

All local agents share a durable SQLite queue. Each account has its own worker, browser data and login. Each account executes one job; additional callers queue. Workers keep generating, downloading and finalizing after MCP clients disconnect. They restart on a subsequent client call after a crash or OS restart; this release does not install a Windows startup service.

Register the same Python entrypoint (`-m web_images mcp`) and `CHATGPT_WEB_IMAGES_POOL` directory in Claude Code, Codex or other stdio MCP clients. Existing Codex plugin users should update the plugin instead of adding a duplicate server. The MCP descriptions contain the workflow, so loading a vendor-specific skill is optional.

Create an isolated config directory per account with a `settings.json` containing absolute `data_dir`, installed `cli_entry` and `output_dir`. Register with `account-add --account-id primary --data-dir ... --config-dir ...`. Never copy another account's auth state. `open --account-id ...` queues a browser login action; after user login, run `check --account-id ...` and inspect `status`.

Submit once using a stable globally unique `request_id`. `queued`, `dispatching` and `running` are normal. Poll the same job (default 20 seconds, maximum 45); browser operations do not extend this budget. Use `account_id=auto` or a configured alias. Do not change accounts or request IDs to retry an uncertain submission. `needs_attention` pauses that lane; after inspection, `image_resume` continues the same job. Cancel only on user request and verify the final cancelled state.

Tools: `image_status`, `image_generate`, `image_poll`, `image_open`, `image_account_check`, `image_select_account`, `image_resume`, `image_cancel`. The first-party backend image model is unverified; website quotas and actual generation speed are not guaranteed. Count 1–20 is a local input bound, not a concurrency or website quota promise.

`import-legacy --account-id primary` adopts observation of an existing job without recreating or resending its prompt. Original low-level CLI behavior remains available with `--legacy` for historical jobs. Keep private queue databases, profiles and generated output out of source control.

Tests: `test_web_images.py`, `test_mcp_protocol.py`, `test_image_pool.py`, `test_pool_protocol.py`, `test_account_contracts.py`, `test_account_browser.py` in scripts/. Synthetic browser fixtures validate contracts and process lifetime, not real-account image generation.
