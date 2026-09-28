'use strict';
// Drop-in replacement for the Hub per-step browser entry (same argv, same JSON output).
// Sends the step to the identity's long-lived tab daemon, starting it when needed. If the
// daemon cannot be reached the step falls back to the Hub's own per-step transport, so a
// daemon problem degrades speed, never correctness.
const fs = require('fs');
const path = require('path');
const { spawn } = require('child_process');
const { statePath, PROTOCOL } = require('./tab_daemon.cjs');

function read(file) { try { return JSON.parse(fs.readFileSync(file, 'utf8')); } catch { return null; } }
const sleep = ms => new Promise(r => setTimeout(r, ms));

async function call(state, body, timeoutMs) {
  const r = await fetch(`http://127.0.0.1:${state.port}/call`, {
    method: 'POST', headers: { 'content-type': 'application/json', 'x-token': state.token },
    body: JSON.stringify(body), signal: AbortSignal.timeout(timeoutMs + 5000),
  });
  if (!r.ok) throw Error('daemon_http_' + r.status);
  return r.json();
}

async function alive(state) {
  if (!state || state.protocol !== PROTOCOL) return false;
  try { process.kill(state.pid, 0); } catch { return false; }
  try {
    const r = await fetch(`http://127.0.0.1:${state.port}/call`, { method: 'POST', headers: { 'x-token': 'probe' }, signal: AbortSignal.timeout(1500) });
    return r.status === 403;
  } catch { return false; }
}

async function ensureDaemon(opts) {
  const file = statePath(opts.poolRoot, opts.identity);
  let state = read(file);
  if (await alive(state)) return state;
  const lock = file + '.lock';
  let fd = null;
  try { fd = fs.openSync(lock, 'wx'); } catch {
    // Another lane is starting it; stale locks older than 30 s are ignored.
    try { if (Date.now() - fs.statSync(lock).mtimeMs > 30000) { fs.rmSync(lock, { force: true }); fd = fs.openSync(lock, 'wx'); } } catch {}
  }
  try {
    if (fd !== null) {
      state = read(file);
      if (!(await alive(state))) {
        const child = spawn(process.execPath, [path.join(__dirname, 'tab_daemon.cjs'), JSON.stringify(opts)],
          { detached: true, stdio: 'ignore', windowsHide: true });
        child.unref();
      }
    }
    for (let i = 0; i < 60; i++) {
      state = read(file);
      if (await alive(state)) return state;
      await sleep(250);
    }
    return null;
  } finally { if (fd !== null) { fs.closeSync(fd); fs.rmSync(lock, { force: true }); } }
}

async function main(binding, argv = process.argv.slice(2)) {
  const opts = { identity: binding.identity, poolRoot: binding.poolRoot, hubCore: binding.hubCore, root: binding.root, playwright: binding.playwright };
  let out;
  try {
    const state = await ensureDaemon(opts);
    if (!state) throw Error('daemon_unavailable');
    const timeoutMs = Number(process.env.CHATGPT_WEB_IMAGES_STEP_TIMEOUT_MS) || 50000;
    out = await call(state, { lane: binding.id, argv, timeoutMs }, timeoutMs);
  } catch (e) {
    if (e && /Timeout|timeout|aborted/i.test(String(e.message)) && !/daemon_unavailable/.test(String(e.message))) {
      out = { isError: true, error: 'Timeout' };
    } else {
      // Daemon missing or broken: keep working through the Hub's per-step transport.
      return require(path.join(binding.hubCore, 'hub-browser-tool.js')).main(binding, argv);
    }
  }
  process.stdout.write(JSON.stringify(out) + '\n');
  if (out.isError) process.exitCode = 1;
}

module.exports = { main, ensureDaemon, alive };
