# Release procedure

1. Run contract and MCP protocol tests on supported Python versions on Windows. Check installation from the built wheel in a clean virtual environment. Exercise `setup`, `doctor`, and first-run login handling using an isolated data directory.
2. Run a real logged-in image task through the installed release code. Confirm independent originals, actual dimensions, count match, no image/base64 response, and idempotent retry behavior. Keep private conversation IDs, prompts, downloads and auth outside the repository.
3. Run `python scripts/check_release.py` against the exact tracked file set. Review `git diff --cached` and `git ls-files`; exclude profiles, cookies, outputs, task metadata and machine-specific paths.
4. Build a wheel with `python -m pip wheel --no-deps . -w dist`. Build a source archive from the reviewed Git commit. Record SHA-256 hashes for downloadable assets.
5. Push the reviewed commit to a private GitHub repository and check CI. Prepare a **draft** release for that exact commit, attaching the wheel, source archive and checksum file.
6. After the repository owner approves public publication, change visibility to public and publish the draft as a prerelease. Repository visibility and release publication are separate actions. Do not claim PyPI availability or official plugin-store inclusion: neither follows from a GitHub release.

The standard release contains source, metadata, documentation and tests only. Browser runtime and user data remain local. The optional Codex plugin requires an installed command on the client's PATH; an absolute-path stdio configuration is the primary supported installation route.

Suggested initial release name: `v0.2.0 — Windows preview`. State Windows-only support and the unverified internal image model. Do not claim guaranteed website quotas or measured generation-speed multipliers.
