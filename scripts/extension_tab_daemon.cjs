'use strict';
// The official Playwright extension attaches to its own new tab group in the
// person's normal Chrome. Never fall back to starting a debugging Chrome.
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const {spawnSync}=require('child_process');
const { TabDaemon, serve } = require('./tab_daemon.cjs');
const EXTENSION_ID = 'mmlmfjhmonkocbjadbfplnigmagldckm';

function statePath(poolRoot, identity, root) {
  const key = crypto.createHash('sha256').update(path.resolve(root).toLowerCase()).digest('hex').slice(0, 12);
  return path.join(poolRoot, `extension-daemon-${identity}-${key}.json`);
}

function validate(options) {
  for (const key of ['root', 'browserRoot', 'poolRoot', 'hubCore', 'playwright', 'tokenFile']) {
    if (typeof options[key] !== 'string' || !path.isAbsolute(options[key])) throw Error('Invalid extension binding');
  }
  if (!['main', 'alt'].includes(options.identity)) throw Error('Invalid extension identity');
}

function readProtectedToken(options) {
  const record=JSON.parse(fs.readFileSync(options.tokenFile,'utf8'));
  if(record.version!==1 || record.identity!==options.identity ||
     path.resolve(record.root).toLowerCase()!==path.resolve(options.browserRoot).toLowerCase() || typeof record.protectedToken!=='string')
    throw Error('Extension pairing required');
  const processResult=spawnSync('python',[path.join(__dirname,'extension_token_dpapi.py'),'unprotect'],
    {input:record.protectedToken,encoding:'utf8',windowsHide:true,timeout:10000});
  if(processResult.error||processResult.status!==0)throw Error('Extension pairing required');
  return processResult.stdout.trim();
}

class ExtensionTabDaemon extends TabDaemon {
  constructor(options, { factory, launchProfile, readToken, settleMs = 12000, sleep = ms => new Promise(resolve=>setTimeout(resolve,ms)), now = Date.now } = {}) {
    validate(options);
    super({...options,root:options.browserRoot});
    this.options = options;
    this.factory = factory;
    this.launchProfile = launchProfile;
    this.readToken = readToken || (() => readProtectedToken(options));
    this.extensionPages = new Map();
    this.ownedSelectorPage = null;
    this.settleMs=settleMs;
    this.sleep=sleep;
    this.now=now;
    const accountSource=fs.readFileSync(path.join(__dirname,'chatgpt_account.py'),'utf8');
    const match=accountSource.match(/STATE_FUNCTION = r'''([\s\S]*?)'''/);
    if(!match)throw Error('Extension account classifier unavailable');
    this.accountProbe=new Function('return ('+match[1]+')')();
  }

  async extensionBrowser() {
    if (this.browser?.isConnected()) return this.browser;
    if (this.connecting) return this.connecting;
    this.connecting = this.connectExtension().finally(() => { this.connecting = null; });
    return this.connecting;
  }

  async connectExtension() {
    let token;
    try { token = this.readToken(); } catch { throw Error('Extension pairing required'); }
    if (typeof token !== 'string' || token.length < 16) throw Error('Extension pairing required');
    // Pairing is pinned to the account profile: a different profile cannot accept
    // this token. Launch explicitly into this identity before the official relay
    // opens its connect page; older Playwright versions only discover Default/Profile N.
    if (this.launchProfile) await this.launchProfile(this.options);
    else {
      const { PersonalChrome } = require(path.join(this.hubCore, 'personal-chrome.js'));
      await new PersonalChrome({root:this.root}).open(this.identity, [`chrome-extension://${EXTENSION_ID}/status.html`]);
    }
    process.env.PLAYWRIGHT_MCP_EXTENSION_TOKEN = token;
    delete process.env.DEBUG; // Never log the relay URL containing the pairing token.
    let create=this.factory;
    if(!create) {
      const runtimeRoot = path.dirname(require.resolve(this.playwright));
      const core = path.join(path.dirname(runtimeRoot), 'playwright-core', 'lib', 'coreBundle.js');
      create=require(core).tools.createBrowserWithInfo;
    }
    const { chromeExecutable } = require(path.join(this.hubCore, 'hub-chrome.js'));
    let result;
    try {
      result = await create({extension:true,browser:{browserName:'chromium',userDataDir:this.root,launchOptions:{}}},
        {clientName:`chatgpt-web-images-${this.identity}`}, {browser:'chrome',executablePath:chromeExecutable()});
    } finally {delete process.env.PLAYWRIGHT_MCP_EXTENSION_TOKEN;}
    this.browser = result.browser;
    const contexts = this.browser.contexts();
    if (contexts.length !== 1 || contexts[0].pages().some(page =>
        !page.url().startsWith(`chrome-extension://${EXTENSION_ID}/connect.html`))) {
      await this.disconnect();
      throw Error('Extension ownership check failed; browser preserved');
    }
    // With the pinned token the extension connects its own fresh selector page,
    // never an existing human tab. Only pages created by this client are used.
    this.ownedSelectorPage = contexts[0].pages()[0] || null;
    const connected=this.browser;
    connected.once('disconnected', () => {if(this.browser===connected){this.browser=null; this.extensionPages.clear(); this.ownedSelectorPage=null;}});
    return this.browser;
  }

  async disconnect() {
    const browser = this.browser;
    this.browser = null;
    this.extensionPages.clear();
    this.ownedSelectorPage = null;
    if (browser) await browser.close(); // An attached browser close disconnects the relay.
  }

  async yieldToPerson() {
    // The ordinary browser remains open while the person verifies. Reopening or
    // closing Chrome is never necessary. Only explicit human-done ends this lease.
    if (!this.guard?.handoff(this.root)) return false;
    if (this.browser) await this.disconnect();
    return true;
  }

  async page(lane) {
    const held=this.extensionPages.get(lane);
    if (!held || held.isClosed() || !this.browser?.isConnected()) throw Error('No browser session: extension owned page is not open');
    return held;
  }

  async execute(lane, argv) {
    const { argumentsOf } = require(path.join(this.hubCore,'hub-browser-tool.js'));
    const [command,...args]=argumentsOf(argv);
    if(command==='transport-release') {
      if([...this.chains.keys()].some(key=>key!==lane))throw Error('Extension browser busy');
      await this.disconnect();
      return {released:true,browserPreserved:true};
    }
    if (command==='human-open') {
      await this.disconnect();
      const url=args.find(a=>!a.startsWith('--'))||'https://chatgpt.com/';
      const site=this.guard.siteOf(url);
      const lease=this.guard.startHandoff(this.root,{identity:this.identity,site,by:lane,mode:'extension'});
      const {PersonalChrome}=require(path.join(this.hubCore,'personal-chrome.js'));
      await new PersonalChrome({root:this.root}).open(this.identity,[url]);
      return {handoff:true,until:lease.until,sharedBrowser:true};
    }
    if (command==='human-done') {
      const lease=this.guard.handoff(this.root);
      if(lease && lease.identity!==this.identity) throw Error('Human handoff: another account is being used');
      if(lease)this.guard.endHandoff(this.root,lease.id);
      const site=this.guard.siteOf(args.find(a=>!a.startsWith('--'))||'https://chatgpt.com/');
      if(site)this.guard.releaseSite(this.root,this.identity,site);
      return {handoff:false,site};
    }
    if(command==='state-load'||command==='state-save') return {managedBy:'personal-chrome-extension',imported:false,exported:false};
    if(command==='close') {
      const page=this.extensionPages.get(lane);
      this.extensionPages.delete(lane);
      if(page && !page.isClosed())await page.close();
      return {closed:!!page};
    }
    if(await this.yieldToPerson()) throw Error('Human handoff: browser automation is paused');
    this.guard.assertAutomationAllowed(this.root,{identity:this.identity});
    if(command==='open') {
      const page=this.extensionPages.get(lane);
      if(page && !page.isClosed())return {reused:true,sharedBrowser:true};
      const browser=await this.extensionBrowser();
      const owned=await browser.contexts()[0].newPage();
      this.extensionPages.set(lane,owned);
      const url=args.find(a=>!a.startsWith('--'))||'about:blank';
      if(url!=='about:blank')await this.navigate(lane,owned,url);
      return {reused:false,sharedBrowser:true};
    }
    const page=await this.page(lane);
    if(command==='goto')return this.navigate(lane,page,args[0]);
    if(command==='run-code') {
      const at=args.indexOf('--filename');
      if(at<0||!args[at+1])throw Error('run-code requires a local filename');
      const source=this.adaptSource(fs.readFileSync(args[at+1],'utf8'));
      const fn=new Function('return ('+source+'\n)')();
      this.guard.assertAutomationAllowed(this.root,{identity:this.identity,url:page.url()});
      const result=await this.protectedStep(lane,page,()=>fn(page));
      if(result?.challenge===true) {
        // A status script can finish while the managed check is still visible,
        // just before the post-step passive wait observes it clearing. Return
        // the current state, rather than pausing on that stale status snapshot.
        if(page.url().startsWith('https://chatgpt.com/')) {
          const current=await page.evaluate(this.accountProbe);
          if(!current.challenge) {
            const corrected={...result,...current};
            if(corrected.error_code==='browser_challenge')delete corrected.error_code;
            return corrected;
          }
        }
        const site=this.guard.siteOf(page.url())||'unknown';
        try {await page.goto('about:blank');}
        finally {this.guard.recordChallenge(this.root,{identity:this.identity,site,kind:'reported',source:lane});}
        await this.disconnect();
      }
      return result;
    }
    throw Error('Unsupported Hub browser command: '+command);
  }

  async navigate(lane,page,url) {
    this.guard.assertAutomationAllowed(this.root,{identity:this.identity,url,navigate:true});
    await this.protectedStep(lane,page,()=>page.goto(url,{waitUntil:'domcontentloaded'}));
    if(await this.inspectAndLeave(lane,page)) {
      await this.disconnect();
      throw Error('Site challenged: page asked for human verification');
    }
    return {url:page.url()};
  }

  async inspectAndLeave(lane,page) {
    if(!page.url().startsWith('https://chatgpt.com/'))return false;
    let state=await page.evaluate(this.accountProbe);
    if(!state.challenge)return false;
    // A managed check may clear itself, as it does in the normal account tab.
    // Give THIS page one short chance to finish. No reload, no checkbox click,
    // no cookie change, and no new request is submitted while observing.
    const until=this.now()+this.settleMs;
    while(state.challenge && this.now()<until) {
      await this.sleep(Math.min(500,until-this.now()));
      try {state=await page.evaluate(this.accountProbe);}
      catch(error) {
        if(!/Execution context was destroyed|Cannot find context|Inspected target navigated/.test(String(error?.message)))throw error;
      }
    }
    if(!state.challenge){this.log('automatic_check_finished',{lane});return false;}
    this.log('human_check_remaining',{lane,composer:!!state.composer,interstitial:/^(Just a moment|checking your browser|请稍候|請稍候|Attention Required)/i.test(state.title||'')});
    try {await page.goto('about:blank');}
    finally {this.guard.recordChallenge(this.root,{identity:this.identity,site:'chatgpt',kind:'detected',source:lane});}
    return true;
  }

  async protectedStep(lane,page,fn) {
    this.guard.assertAutomationAllowed(this.root,{identity:this.identity,url:page.url()});
    if(await this.inspectAndLeave(lane,page)){await this.disconnect();throw Error('Site challenged: page asked for human verification');}
    let result;
    try {result=await fn();}
    catch(error) {
      if(await this.inspectAndLeave(lane,page)) {await this.disconnect();throw Error('Site challenged: page asked for human verification');}
      throw error;
    }
    if(await this.inspectAndLeave(lane,page)){await this.disconnect();throw Error('Site challenged: page asked for human verification');}
    return result;
  }

  async reap() { return 0; } // The extension controls only its group; never enumerate all Chrome tabs.
  dropLane(lane) { this.extensionPages.delete(lane); }

  async handle(body) {
    try {return await super.handle(body);}
    catch(error) {
      if(/daemon step exceeded/.test(String(error?.message)))await this.disconnect();
      throw error;
    }
  }
}

if(require.main===module) {
  const options=JSON.parse(process.argv[2]);
  validate(options);
  serve(options,{createDaemon:opts=>new ExtensionTabDaemon(opts),stateFile:statePath(options.poolRoot,options.identity,options.browserRoot)})
    .catch(()=>{process.stderr.write('Extension daemon start failed\n');process.exitCode=1;});
}
module.exports={ExtensionTabDaemon,statePath,validate,readProtectedToken,EXTENSION_ID};
