# Validation boundaries

The local v0.1 prototype was exercised against a real signed-in ChatGPT web account: one request produced five independent PNG downloads, each 1672 × 941, with five distinct SHA-256 hashes. This establishes a tested case, not a fixed resolution, website quota or timing guarantee.

v0.2 automated verification covers:

- strict count/input validation, one active job, foreign-conversation rejection;
- persistent retry-key reuse/conflicts, including uncertain submissions;
- all-image discovery/download accounting, duplicates, missing/extra images;
- per-file recovery, crash-after-rename recovery and changed-file detection;
- compact/full metadata, retained error context and bounded internal polling;
- real stdio MCP initialization, schema validation, text-only response and error contracts;
- isolated installation, runtime setup and first-login behavior.

The CI suite uses synthetic image fixtures and mocked browser responses. It does not log into ChatGPT or spend account generation allowance. Account/browser smoke tests are performed locally; private evidence is excluded from public releases. Byte measurements describe serialized tool responses, not exact model-token accounting.

Local v0.2 release preparation on 2026-09-07:

- 30 contract tests and 8 stdio protocol checks passed on Windows/Python 3.12.
- Fresh virtual-environment installation and isolated runtime download succeeded. A brand-new browser with no imported auth reached the manual-login flow successfully; the test browser was closed afterward.
- The installed v0.2 package generated and downloaded two independent 1672 × 941 originals. Repeating the same request ID returned the completed job without another submission.
- Replaying the same previous five-image result reduced serialized MCP response size from 3,178 to 1,821 UTF-8 bytes (42.7%), preserving all five output paths, dimensions, bytes and SHA-256 hashes. Full provenance remains available with `detail=true`.
- Fresh installation exposed an SDK/settings warning with a newer dependency combination; the release pins the tested Pydantic 2.12.5 and pydantic-settings 2.13.1 combination explicitly.

Not certified: macOS/Linux, every ChatGPT locale or DOM variant, 20-image delivery, concurrent generation, exact internal image model, or any generation-speed multiplier. Optional image viewing by the assistant can still consume image context.
