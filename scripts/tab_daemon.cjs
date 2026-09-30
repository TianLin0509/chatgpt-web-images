'use strict';
// One long-lived browser connection per Hub identity, shared by every image lane of that
// identity. Each lane still owns its own tab (parallel sessions, like a person with several
// ChatGPT tabs open); only the connection is shared.
//
// Why: the per-step transport started a Node process and attached Playwright to the whole
// Hub Chrome for every queue poll. Each attach touched all tabs, ChatGPT refetched on the
// resulting focus/visibility events, and the request bursts ended in account-wide HTTP 429.
// Here the attach happens once; a step is a local request to an already attached page.
const fs = require('fs');
const http = require('http');
const path = require('path');
const crypto = require('crypto');

const PROTOCOL = 1;
const IDLE_EXIT_MS = 6 * 3600 * 1000;
const REAP_EVERY_MS = 10 * 60 * 1000;

function statePath(poolRoot, identity) { return path.join(poolRoot, `tab-daemon-${identity}.json`); }
function logPath(poolRoot, identity) { return path.join(poolRoot, `tab-daemon-${identity}.log`); }

class TabDaemon {
  constructor({ identity, poolRoot, hubCore, root, playwright }) {
    this.identity = identity;
    this.poolRoot = poolRoot;
    this.hubCore = hubCore;
    this.root = root;
    this.playwright = playwright;
    this.browser = null;
    this.browserWs = null;
    this.pages = new Map();       // lane id -> { targetId, page }
    this.chains = new Map();      // lane id -> promise tail; one step at a time per lane
    this.lastCall = Date.now();
    const { BrowserTool } = require(path.join(hubCore, 'hub-browser-tool.js'));
    this.BrowserTool = BrowserTool;
    this.adaptSource = require(path.join(hubCore, 'chatgpt-selector-compat.js')).adaptSource;
    // Shared challenge/handoff rules of the Hub (absent in older Hubs: then nothing changes).
    try { this.guard = require(path.join(hubCore, 'web-risk-guard.js')); } catch { this.guard = null; }
  }

  // While a person verifies or signs in, this connection (attached to every page, including
  // the challenge frame) must not exist: observed 2026-09-29, the person could not pass the
  // check until it was gone. Checked before every step and on a timer.
  async yieldToPerson() {
    let lease = this.guard && this.guard.handoff(this.root);
    // The person closed the window they were given: that ends the handoff (browser-level check only).
    if (lease && this.guard.settleHandoff) lease = await this.guard.settleHandoff(this.tool('images-reaper').hub).catch(() => lease);
    if (!lease) return false;
    if (this.browser) {
      const browser = this.browser;
      this.browser = null; this.pages.clear();
      try { await browser.close(); } catch {}
      this.log('handoff_detached', { identity: lease.identity });
    }
    return true;
  }

  log(event, extra = {}) {
    // Never log page contents, evaluated code or URLs with private conversation ids.
    try { fs.appendFileSync(logPath(this.poolRoot, this.identity), JSON.stringify({ at: Date.now() / 1000, event, ...extra }) + '\n'); } catch {}
  }

  tool(lane) {
    return new this.BrowserTool({ id: lane, tool: 'images', identity: this.identity, root: this.root, playwright: this.playwright });
  }

  connection(ep) {
    // Attaching to the Hub Chrome takes seconds; concurrent lanes share one attach.
    if (this.connecting && this.connectingWs === ep.ws) return this.connecting;
    this.connectingWs = ep.ws;
    this.connecting = this._connection(ep).finally(() => { this.connecting = null; });
    return this.connecting;
  }

  async _connection(ep) {
    if (this.browser && this.browser.isConnected() && this.browserWs === ep.ws) return this.browser;
    if (this.browser) { try { await this.browser.close(); } catch {} }
    this.pages.clear();
    const { chromium } = require(this.playwright);
    // noDefaults: attach without emulating focus or viewport in tabs we do not own.
    this.browser = await chromium.connectOverCDP(`http://127.0.0.1:${ep.port}`, { noDefaults: true });
    this.browserWs = ep.ws;
    this.browser.on('disconnected', () => { this.log('disconnected'); this.browser = null; this.pages.clear(); });
    this.log('connected');
    return this.browser;
  }

  async findPage(browser, targetId) {
    for (const context of browser.contexts()) for (const page of context.pages()) {
      const cdp = await context.newCDPSession(page);
      let info;
      try { info = await cdp.send('Target.getTargetInfo'); } finally { await cdp.detach().catch(() => {}); }
      if (info.targetInfo.targetId === targetId) return page;
    }
    return null;
  }

  async page(lane) {
    const cached = this.pages.get(lane);
    if (cached && !cached.page.isClosed() && this.browser && this.browser.isConnected()) return cached.page;
    const target = await this.tool(lane).target();
    if (!target) throw Error('No browser session: owned Hub page is not open');
    let browser = await this.connection(target.ep);
    let page = await this.findPage(browser, target.targetId);
    for (let i = 0; !page && i < 10; i++) {
      // A tab opened moments ago is adopted by the live connection asynchronously.
      await new Promise(r => setTimeout(r, 300));
      page = await this.findPage(browser, target.targetId);
    }
    if (!page) {
      // Still missing: its context was never adopted. Reattach once (drops other lanes' cache).
      this.log('reattach_for_missing_page');
      try { await browser.close(); } catch {}
      this.browser = null;
      browser = await this.connection(target.ep);
      page = await this.findPage(browser, target.targetId);
    }
    if (!page) throw Error('No browser session: owned Hub page is not visible to the runtime');
    this.pages.set(lane, { targetId: target.targetId, page });
    page.once('close', () => { if (this.pages.get(lane)?.page === page) this.pages.delete(lane); });
    return page;
  }

  async execute(lane, argv) {
    const tool = this.tool(lane);
    const { argumentsOf } = require(path.join(this.hubCore, 'hub-browser-tool.js'));
    const [command, ...args] = argumentsOf(argv);
    if (command === 'human-open') { await this.yieldToPerson(); const r = await tool.execute(argv); await this.yieldToPerson(); return r; }
    if (command === 'human-done') return tool.execute(argv);
    if (this.guard && !['close', 'state-load', 'state-save'].includes(command)) {
      if (await this.yieldToPerson()) this.guard.assertAutomationAllowed(this.root, { identity: this.identity });
    }
    if (command === 'goto') {
      if (this.guard) this.guard.assertAutomationAllowed(this.root, { identity: this.identity, url: args[0] });
      const page = await this.page(lane);
      await page.goto(args[0], { waitUntil: 'domcontentloaded' });
      if (this.guard && await this.guard.inspectAndLeave(this.root, { identity: this.identity, page, url: args[0], source: lane }))
        throw Error('Site challenged: the page asked for human verification; left it and paused this site');
      return { url: page.url() };
    }
    if (command === 'run-code') {
      const at = args.indexOf('--filename');
      if (at < 0 || !args[at + 1]) throw Error('run-code requires a local filename');
      const source = this.adaptSource(fs.readFileSync(args[at + 1], 'utf8'));
      // Downloads need the Hub's passive relay connection; they are rare (once per job).
      if (/\bwaitForEvent\s*\(\s*['"]download['"]/.test(source)) return tool.execute(argv);
      const fn = new Function('return (' + source + '\n)')();
      const page = await this.page(lane);
      if (this.guard) this.guard.assertAutomationAllowed(this.root, { identity: this.identity, url: page.url() });
      const result = await fn(page);
      if (this.guard && result && typeof result === 'object' && result.challenge === true) {
        // The tool saw a human check: stop this tab retrying first, then record it for every Hub tool.
        const site = this.guard.siteOf(page.url()) || 'unknown';
        await page.goto('about:blank').catch(() => {});
        try { this.guard.recordChallenge(this.root, { identity: this.identity, site, kind: 'reported', source: lane }); } catch {}
      }
      return result;
    }
    if (command === 'open') return this.open(lane, tool, argv);
    if (command === 'close') this.pages.delete(lane);
    // close / state-load / state-save keep the Hub's exact semantics.
    return tool.execute(argv);
  }

  endpoint() { return this.tool('images-reaper').hub.endpoint(); }
  registryPath() { return path.join(this.poolRoot, `tab-registry-${this.identity}.json`); }
  liveTargets() {
    const dir = path.join(this.root, 'tool-pages');
    const live = new Set();
    let names = [];
    try { names = fs.readdirSync(dir); } catch {}
    for (const name of names) { const rec = readJson(path.join(dir, name)); if (rec?.targetId) live.add(rec.targetId); }
    return live;
  }
  register(lane, targetId) {
    if (!targetId) return;
    const reg = readJson(this.registryPath()) || { tabs: [] };
    if (!reg.tabs.some(t => t.targetId === targetId)) reg.tabs.push({ targetId, lane, at: Date.now() / 1000 });
    writeJson(this.registryPath(), reg);
  }

  async open(lane, tool, argv) {
    // Reuse the recorded tab whenever it still exists. The Hub's own check can miss it under
    // load (a short CDP health deadline) and then opens another tab, leaving the first one
    // polling ChatGPT with no owner; 23 such orphans pushed the login into HTTP 429.
    const record = readJson(path.join(this.root, 'tool-pages', lane + '.json'));
    if (record?.targetId && record.identity === this.identity) {
      const ep = await this.endpoint();
      if (ep && record.browserWs === ep.ws) {
        const page = await this.findPage(await this.connection(ep), record.targetId);
        if (page) {
          this.pages.set(lane, { targetId: record.targetId, page });
          this.register(lane, record.targetId);
          return { targetId: record.targetId, reused: true };
        }
      }
    }
    const result = await tool.execute(argv);
    this.register(lane, result?.targetId);
    return result;
  }

  async reap() {
    // Close only tabs this daemon opened or reused for a lane and that no lane owns now.
    if (await this.yieldToPerson()) return 0;  // never attach while a person has the browser
    const reg = readJson(this.registryPath());
    if (!reg?.tabs?.length) return 0;
    const ep = await this.endpoint();
    if (!ep) return 0;
    const browser = await this.connection(ep);
    const session = await browser.newBrowserCDPSession();
    let closed = 0;
    try {
      const { targetInfos } = await session.send('Target.getTargets');
      const exists = new Set(targetInfos.map(t => t.targetId));
      const live = this.liveTargets();
      const keep = [];
      for (const tab of reg.tabs) {
        if (live.has(tab.targetId)) { keep.push(tab); continue; }
        if (exists.has(tab.targetId)) {
          try { await session.send('Target.closeTarget', { targetId: tab.targetId }); closed++; this.log('reaped', { lane: tab.lane }); } catch {}
        }
      }
      writeJson(this.registryPath(), { tabs: keep });
    } finally { await session.detach().catch(() => {}); }
    return closed;
  }

  enqueue(lane, work) {
    const prev = this.chains.get(lane) || Promise.resolve();
    const next = prev.catch(() => {}).then(work);
    // The stored tail must never reject: an unobserved rejection terminates Node, and a
    // failed step (for example a closed tab) used to take the whole daemon down with it.
    const tail = next.catch(() => {}).finally(() => { if (this.chains.get(lane) === tail) this.chains.delete(lane); });
    this.chains.set(lane, tail);
    return next;
  }

  async handle(body) {
    this.lastCall = Date.now();
    const { lane, argv, timeoutMs } = body;
    if (!/^[a-z0-9_-]{1,64}$/.test(lane || '') || !Array.isArray(argv)) throw Error('Invalid request');
    let timer;
    const deadline = new Promise((_, reject) => { timer = setTimeout(() => reject(Error('Timeout: daemon step exceeded ' + timeoutMs + 'ms')), timeoutMs || 55000); });
    try {
      return await Promise.race([this.enqueue(lane, () => this.execute(lane, argv)), deadline]);
    } catch (e) {
      // A step that never settles must not block every later step of its lane.
      if (/daemon step exceeded/.test(String(e && e.message))) { this.chains.delete(lane); this.pages.delete(lane); }
      throw e;
    } finally { clearTimeout(timer); }
  }
}

function readJson(file) { try { return JSON.parse(fs.readFileSync(file, 'utf8')); } catch { return null; } }
function writeJson(file, value) {
  const tmp = file + '.' + process.pid + '.' + crypto.randomBytes(4).toString('hex') + '.tmp';
  fs.writeFileSync(tmp, JSON.stringify(value), 'utf8');
  fs.renameSync(tmp, file);
}

function category(message) {
  return /^Unsupported Hub browser command/.test(message) ? 'Unsupported command'
    : /^Human handoff/.test(message) ? 'Human handoff'
    : /^Site challenged/.test(message) ? 'Site challenged'
    : /No browser session/.test(message) ? 'No browser session'
    : /IMAGE_TOOL_UNAVAILABLE/.test(message) ? 'IMAGE_TOOL_UNAVAILABLE'
    : /strict mode violation/.test(message) ? 'strict mode violation'
    : /Target.*closed/.test(message) ? 'Target closed'
    : /Timeout|timeout/.test(message) ? 'Timeout' : 'Hub browser operation failed';
}

async function serve(options) {
  const daemon = new TabDaemon(options);
  // One lane's failure must never stop the connection every other lane depends on.
  process.on('unhandledRejection', e => daemon.log('unhandled_rejection', { error: category(String(e && e.message || e)) }));
  process.on('uncaughtException', e => daemon.log('uncaught_exception', { error: category(String(e && e.message || e)) }));
  const token = crypto.randomBytes(24).toString('hex');
  const server = http.createServer((req, res) => {
    if (req.method !== 'POST' || req.url !== '/call' || req.headers['x-token'] !== token) { res.writeHead(403); res.end(); return; }
    let raw = '';
    req.setEncoding('utf8');
    req.on('data', chunk => { raw += chunk; if (raw.length > 1e6) req.destroy(); });
    req.on('end', async () => {
      let out;
      try { out = { result: (await daemon.handle(JSON.parse(raw))) ?? null }; }
      catch (e) { out = { isError: true, error: category(String(e && e.message || e)) }; daemon.log('step_error', { error: out.error }); }
      res.writeHead(200, { 'content-type': 'application/json' });
      res.end(JSON.stringify(out));
    });
  });
  server.listen(0, '127.0.0.1', () => {
    const state = { protocol: PROTOCOL, pid: process.pid, port: server.address().port, token, identity: options.identity, started: Date.now() / 1000 };
    const file = statePath(options.poolRoot, options.identity);
    const tmp = file + '.' + process.pid + '.tmp';
    fs.writeFileSync(tmp, JSON.stringify(state), 'utf8');
    fs.renameSync(tmp, file);
    daemon.log('listening', { port: state.port, protocol: PROTOCOL });
  });
  setInterval(() => {
    if (Date.now() - daemon.lastCall > IDLE_EXIT_MS) { daemon.log('idle_exit'); process.exit(0); }
  }, 60000).unref();
  setInterval(() => { daemon.yieldToPerson().catch(() => {}); }, 2000).unref();
  const reap = () => daemon.reap().catch(e => daemon.log('reap_failed', { error: category(String(e && e.message || e)) }));
  setTimeout(reap, 30000).unref();
  setInterval(reap, REAP_EVERY_MS).unref();
}

if (require.main === module) {
  serve(JSON.parse(process.argv[2])).catch(e => { console.error(String(e && e.stack || e)); process.exit(1); });
}
module.exports = { TabDaemon, serve, statePath, category, PROTOCOL };
