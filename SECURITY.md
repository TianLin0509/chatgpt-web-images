# Local credentials and reporting

This server runs locally over stdio and controls a dedicated browser profile. It opens ChatGPT, submits the user's prompt/reference files, and downloads generated images to the chosen local directory.

- Login happens in the browser. No credentials are included in MCP responses or source releases.
- Browser profile and storage-state files are sensitive local files. Protect them using your operating-system account permissions. The plugin does not encrypt them itself.
- No other application's authentication is imported unless the user explicitly configures a storage-state file. Import does not overwrite that source.
- Raw page HTML, signed image URLs and browser exception output are not returned through MCP. Runtime diagnostic failures retain bounded error codes.
- Generation is a user-authorized write action. This is a trusted local-user tool, not a multi-user hosted service or a filesystem sandbox.
- Output files are verified and never intentionally overwrite existing images. Authentication checks and website limits are not bypassed.

For a bug report, include the version and error code. Do not include cookies, storage state, browser profiles, signed URLs, private prompts, or raw browser logs. For a credential disclosure, remove exposed material and revoke affected sessions before sharing sanitized details.
