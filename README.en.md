# ChatGPT Web Images

[中文](README.md)

A local, unofficial **Windows MCP server** that generates images through your own ChatGPT web account and saves the original downloads. Tool responses contain text metadata, never image base64.

Requirements: Python 3.11+, Node.js 18+ with npm, installed Google Chrome, and your own ChatGPT account with image generation available.

```powershell
python -m venv .venv
& .\.venv\Scripts\python.exe -m pip install .
& .\.venv\Scripts\chatgpt-web-images.exe setup
& .\.venv\Scripts\chatgpt-web-images.exe open
# Sign in yourself in the dedicated browser, then:
& .\.venv\Scripts\chatgpt-web-images.exe status
$imagePython = (Resolve-Path .\.venv\Scripts\python.exe).Path
codex mcp add chatgpt-web-images -- $imagePython -m web_images mcp
```

Set `tool_timeout_sec = 180` in Codex's `[mcp_servers.chatgpt-web-images]` configuration table and start a new thread. For another stdio client, use the venv Python's absolute path with args `["-m", "web_images", "mcp"]` and a tool timeout of at least 180 seconds. [Official Codex MCP configuration](https://learn.chatgpt.com/zh-Hans/docs/extend/mcp)

Tools: `image_generate`, `image_poll`, `image_status`, `image_open`, `image_cancel`.

- `count=1..20` requests separate images in **one** web submission. Default 1; the LLM may choose 3–5 for related variants. The local cap is not a website quota or guarantee. Number each requested image in the prompt; do not accept a collage as multiple files.
- Use exactly one of `prompt` / UTF-8 `prompt_file`. Reference images (up to 5 PNG/JPEG/WebP files) and output directories must use absolute paths.
- A stable `request_id` reuses the original job for identical retries, even after completion. Changed input with the same ID is an error. Use a new ID for a new task.
- `image_poll(job_id, wait_seconds=40)` waits internally. The 0–45 second polling budget excludes a browser operation already in flight. Keep polling the same job if unfinished; do not resubmit uncertain requests.
- Compact responses preserve output paths, actual dimensions, bytes, hashes and counts. Use `detail=true` for complete provenance. Download checkpoints preserve progress after interruptions.
- Missing images return `partial`; extra images return `count_mismatch`. Both are explicit MCP errors with saved files. Exact counts return `complete`.

Default data: `%LOCALAPPDATA%\ChatGPTWebImages`; outputs: its `output` subdirectory. Configuration: `~/.config/chatgpt-web-images/settings.json`. Use `configure --data-dir`, `--output-dir`, `--cli-entry`, or `--auth-file` for explicit local overrides. `doctor` checks runtime availability. Environment overrides are documented in the Chinese README.

`setup` installs pinned `@playwright/cli@0.1.19` in the dedicated data directory. No global npm installation and no npx lookup during generation. Sign in manually with `open`; no other application's cookies are imported by default. Optional storage-state import only reads the user-selected source file.

Only one active generation job per data directory. No model switching, authentication/challenge bypass, automatic resubmission, upscaling, or screenshots passed off as originals. The internal image model is unverified; website limits and DOM changes still apply. Windows only in v0.2. Batch submission reduces orchestration overhead; generation speedup has not been benchmarked. Text prompts and metadata still consume context.

See [validation](docs/VALIDATION.md), [release procedure](docs/RELEASING.md), [privacy](SECURITY.md), and [changelog](CHANGELOG.md). MIT licensed. Microsoft Playwright CLI and the official MCP Python SDK are installed separately under their own licenses.
