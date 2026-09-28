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
    if (command === 'goto') {
      const page = await this.page(lane);
      await page.goto(args[0], { waitUntil: 'domcontentloaded' });
      return { url: page.url() };
    }
    if (command === 'run-code') {
      const at = args.indexOf('--filename');
      if (at < 0 || !args[at + 1]) throw Error('run-code requires a local filename');
      const source = this.adaptSource(fs.readFileSync(args[at + 1], 'utf8'));
      // Downloads need the Hub's passive relay connection; they are rare (once per job).
      if (/\bwaitForEvent\s*\(\s*['"]download['"]/.test(source)) return tool.execute(argv);
      const fn = new Function('return (' + source + '\n)')();
      return fn(await this.page(lane));
    }
    if (command === 'close') this.pages.delete(lane);
    // open / close / state-load / state-save keep the Hub's exact semantics.
    return tool.execute(argv);
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

function category(message) {
  return /No browser session/.test(message) ? 'No browser session'
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
}

if (require.main === module) {
  serve(JSON.parse(process.argv[2])).catch(e => { console.error(String(e && e.stack || e)); process.exit(1); });
}
module.exports = { TabDaemon, serve, statePath, category, PROTOCOL };
