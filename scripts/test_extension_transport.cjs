'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const path=require('path');
const fs=require('fs');
const os=require('os');
const {EventEmitter}=require('events');
const {ExtensionTabDaemon,statePath,EXTENSION_ID}=require('./extension_tab_daemon.cjs');
const {main}=require('./extension_tab_client.cjs');

test('new Hub broker is delegated once without starting an independent image connection',async t=>{
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'images-shared-broker-test-'));t.after(()=>fs.rmSync(root,{recursive:true,force:true}));
  fs.writeFileSync(path.join(root,'ordinary-browser-client.js'),'module.exports.main=async(binding,argv)=>({shared:true,lane:binding.id,argv});');
  fs.writeFileSync(path.join(root,'hub-browser-tool.js'),'module.exports.argumentsOf=args=>args;');
  const result=await main({hubCore:root,id:'images-primary'},['open']);
  assert.deepEqual(result,{shared:true,lane:'images-primary',argv:['open']});
});

test('view opens a personal tab without touching the shared connection or handoff state',async t=>{
  const f=fixture();t.after(()=>fs.rmSync(f.root,{recursive:true,force:true}));
  await f.daemon.execute('images-primary',['open']);
  const owned=await f.daemon.page('images-primary');
  const riskFile=path.join(f.root,'web-risk.json');
  fs.writeFileSync(riskFile,JSON.stringify({handoff:{id:'other-person',until:Date.now()+60000}}));
  const riskBefore=fs.readFileSync(riskFile,'utf8');
  fs.writeFileSync(path.join(f.options.hubCore,'personal-chrome.js'), 'module.exports.PersonalChrome=class{constructor(o){this.root=o.root;}async open(identity,urls){require("fs").writeFileSync(require("path").join(this.root,"view-proof.json"),JSON.stringify({identity,urls}));}};');
  let daemonCalls=0;const oldWrite=process.stdout.write;process.stdout.write=()=>true;
  let result;
  try{result=await main({...f.options,id:'images-primary'},['browser-show','https://chatgpt.com/'],{ensure:async()=>{daemonCalls++;throw Error('Must not touch automation');}});}
  finally{process.stdout.write=oldWrite;}
  assert.equal(result.result.automation_paused,false);
  assert.equal(daemonCalls,0);assert.equal(f.browser.connected,true);assert.equal(owned.closed,false);
  assert.equal(fs.readFileSync(riskFile,'utf8'),riskBefore);
  assert.deepEqual(JSON.parse(fs.readFileSync(path.join(f.root,'view-proof.json'),'utf8')),{identity:'main',urls:['https://chatgpt.com/']});
});

function fixture({initialHumanPage=false}={}) {
  const calls=[];
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'images-extension-test-'));
  const hubCore=path.join(root,'core');
  fs.mkdirSync(hubCore);
  fs.writeFileSync(path.join(hubCore,'hub-browser-tool.js'),'module.exports={BrowserTool:class{},argumentsOf:args=>args};');
  fs.writeFileSync(path.join(hubCore,'chatgpt-selector-compat.js'),'module.exports={adaptSource:source=>source};');
  fs.writeFileSync(path.join(hubCore,'hub-chrome.js'),'module.exports={chromeExecutable:()=>"chrome"};');
  let handoff=null;
  class Page {
    constructor(url='about:blank'){this.location=url;this.closed=false;}
    url(){return this.location;}
    isClosed(){return this.closed;}
    async goto(url){calls.push(['goto',url]);this.location=url;}
    async close(){calls.push(['page-close',this.location]);this.closed=true;}
  }
  const selector=new Page(initialHumanPage?'https://chatgpt.com/c/persons-draft':`chrome-extension://${EXTENSION_ID}/connect.html?token=test-pairing`);
  const pages=[selector];
  const context={pages:()=>pages,newPage:async()=>{const page=new Page();pages.push(page);return page;}};
  class Browser extends EventEmitter {
    constructor(){super();this.connected=true;}
    isConnected(){return this.connected;}
    contexts(){return [context];}
    async close(){calls.push(['disconnect']);this.connected=false;this.emit('disconnected');}
  }
  const browser=new Browser();
  const options={identity:'main',root:root,browserRoot:root,poolRoot:root,tokenFile:path.join(root,'pairing-token'),playwright:path.join(root,'playwright.js'),hubCore};
  let clock=0;
  const daemon=new ExtensionTabDaemon(options,{settleMs:1000,now:()=>clock,sleep:async ms=>{clock+=ms;},readToken:()=> 'unit-test-pairing-token',launchProfile:async()=>calls.push(['ordinary-profile']),factory:async(config,client,cli)=>{calls.push(['factory',config,client,cli]);return {browser};}});
  daemon.guard={assertAutomationAllowed:(root,options)=>calls.push(['guard',options]),handoff:()=>handoff,
    startHandoff:(root,lease)=>(handoff={...lease,id:'test',until:Date.now()+60000}),endHandoff:()=>{handoff=null;},
    releaseSite:()=>calls.push(['release']),siteOf:url=>url.startsWith('https://chatgpt.com')?'chatgpt':null,
    runProtected:async(root,opts,fn)=>fn(),inspectAndLeave:async()=>false,
    recordChallenge:()=>calls.push(['challenge'])};
  return {daemon,calls,browser,pages,root,options,setHandoff:lease=>{handoff=lease;}};
}

test('extension session shares login but starts its own new page; no debugging launch',async()=>{
  const f=fixture();
  await f.daemon.execute('images-primary',['open','about:blank']);
  const page=await f.daemon.page('images-primary');
  assert.notEqual(page,f.pages[0]);
  assert.equal(f.pages[0].url().includes('/connect.html'),true);
  const factory=f.calls.find(c=>c[0]==='factory');
  assert.equal(factory[1].extension,true);
  assert.equal(factory[1].browser.userDataDir,f.root);
  assert.equal(JSON.stringify(factory).includes('remote-debugging-port'),false);
  assert.equal(f.calls.some(c=>c[0]==='goto'),false);
});

test('two concurrent opens share a single extension connection',async()=>{
  const f=fixture();
  await Promise.all([f.daemon.execute('images-one',['open']),f.daemon.execute('images-two',['open'])]);
  assert.equal(f.calls.filter(c=>c[0]==='factory').length,1);
  assert.notEqual(await f.daemon.page('images-one'),await f.daemon.page('images-two'));
});

test('an existing human tab is refused rather than adopted',async()=>{
  const f=fixture({initialHumanPage:true});
  await assert.rejects(f.daemon.execute('images-primary',['open']),/ownership check failed/);
  assert.equal(f.pages[0].closed,false);
  assert.equal(f.calls.some(c=>c[0]==='goto'),false);
});

test('missing pairing stops before launching or visiting any site',async()=>{
  const f=fixture();
  f.daemon.readToken=()=>{throw Error('secret-in-error');};
  await assert.rejects(f.daemon.execute('images-primary',['open']),/^Error: Extension pairing required$/);
  assert.equal(f.calls.some(c=>c[0]==='factory'||c[0]==='ordinary-profile'||c[0]==='goto'),false);
});

test('handoff detaches automation, preserves pages, and blocks further navigation',async()=>{
  const f=fixture();
  await f.daemon.execute('images-primary',['open']);
  const owned=await f.daemon.page('images-primary');
  f.setHandoff({identity:'main',site:'chatgpt'});
  await assert.rejects(f.daemon.execute('images-primary',['goto','https://chatgpt.com/']),/Human handoff/);
  assert.equal(f.browser.isConnected(),false);
  assert.equal(owned.closed,false);
  assert.equal(f.pages[0].closed,false);
  assert.equal(f.calls.some(c=>c[0]==='goto'),false);
});

test('closing a lane closes only its own page',async()=>{
  const f=fixture();
  await f.daemon.execute('images-primary',['open']);
  await f.daemon.execute('images-other',['open']);
  const one=await f.daemon.page('images-primary');
  const two=await f.daemon.page('images-other');
  await f.daemon.execute('images-primary',['close']);
  assert.equal(one.closed,true);
  assert.equal(two.closed,false);
  assert.equal(f.pages[0].closed,false);
  assert.equal(f.browser.isConnected(),true);
});

test('real reported human check is left once and disconnected',async()=>{
  const f=fixture();
  await f.daemon.execute('images-primary',['open']);
  const file=path.join(f.root,'probe.js');
  fs.writeFileSync(file,'async page => ({challenge:true})');
  const result=await f.daemon.execute('images-primary',['run-code','--filename',file]);
  assert.equal(result.challenge,true);
  assert.equal(f.calls.filter(c=>c[0]==='challenge').length,1);
  assert.equal(f.browser.isConnected(),false);
});

test('daemon states cannot reuse a CDP daemon or another profile root',()=>{
  const root=path.resolve('profile-a'),pool=path.resolve('pool');
  const one=statePath(pool,'main',root);
  assert.notEqual(one,statePath(pool,'main',path.resolve('profile-b')));
  assert.notEqual(one,statePath(pool,'alt',root));
  assert.equal(one.includes('tab-daemon-'),false);
});

test('broken extension reports a safe error and never invokes CDP fallback',async()=>{
  const f=fixture();
  let output='';
  const original=process.stdout.write;
  const code=process.exitCode;
  process.stdout.write=chunk=>{output+=chunk;return true;};
  try {
    const result=await main({...f.options,id:'images-primary'},['open'],{ensure:async()=>{throw Error('secret signed URL https://private/credentials');}});
    assert.equal(result.error,'Extension browser unavailable');
    assert.equal(output.includes('credentials'),false);
    assert.equal(f.calls.length,0);
  }finally{process.stdout.write=original;process.exitCode=code;}
});

test('automatic check clears on the same page without reload, click or cookie edits',async()=>{
  const f=fixture();
  let reads=0,navigations=0;
  const page={url:()=> 'https://chatgpt.com/',evaluate:async()=>({challenge:++reads<3,title:'Just a moment...'}),goto:async()=>{navigations++;}};
  assert.equal(await f.daemon.inspectAndLeave('images-primary',page),false);
  assert.equal(reads,3);
  assert.equal(navigations,0);
  assert.equal(f.calls.some(c=>c[0]==='challenge'),false);
});

test('persistent human check remains blocking after a bounded passive wait',async()=>{
  const f=fixture();
  let reads=0,navigations=0;
  const page={url:()=> 'https://chatgpt.com/',evaluate:async()=>{reads++;return {challenge:true,title:'Just a moment...'};},goto:async()=>{navigations++;}};
  assert.equal(await f.daemon.inspectAndLeave('images-primary',page),true);
  assert.equal(reads,3);
  assert.equal(navigations,1);
  assert.equal(f.calls.filter(c=>c[0]==='challenge').length,1);
});

test('a detected challenge disconnects the relay before any prompt action',async()=>{
  const f=fixture();
  await f.daemon.execute('images-primary',['open']);
  const page=await f.daemon.page('images-primary');
  page.location='https://chatgpt.com/';
  page.evaluate=async()=>({challenge:true,title:'Just a moment...'});
  let submitted=false;
  await assert.rejects(f.daemon.protectedStep('images-primary',page,async()=>{submitted=true;}),/Site challenged/);
  assert.equal(submitted,false);
  assert.equal(f.browser.isConnected(),false);
  assert.equal(page.url(),'about:blank');
});

test('releasing an idle transport detaches while preserving the ordinary browser',async()=>{
  const f=fixture();
  await f.daemon.execute('images-primary',['open']);
  const page=await f.daemon.page('images-primary');
  const result=await f.daemon.execute('images-primary',['transport-release']);
  assert.equal(result.released,true);
  assert.equal(f.browser.isConnected(),false);
  assert.equal(page.closed,false);
  assert.equal(f.pages[0].closed,false);
  assert.equal(process.env.PLAYWRIGHT_MCP_EXTENSION_TOKEN,undefined);
});

test('a timed-out step disconnects instead of letting late code submit',async()=>{
  const f=fixture();
  await f.daemon.execute('images-primary',['open']);
  f.daemon.execute=async()=>new Promise(()=>{});
  await assert.rejects(f.daemon.handle({lane:'images-primary',argv:['run-code'],timeoutMs:20}),/daemon step exceeded/);
  assert.equal(f.browser.isConnected(),false);
});

test('a stale status does not re-block a managed check which already cleared',async()=>{
  const f=fixture();
  await f.daemon.execute('images-primary',['open']);
  const page=await f.daemon.page('images-primary');
  page.location='https://chatgpt.com/';
  page.evaluate=async()=>({challenge:false,cloudflare:false,logged_in:true,auth_state:'authenticated'});
  const file=path.join(f.root,'stale-status.js');
  fs.writeFileSync(file,'async page => ({challenge:true,error_code:"browser_challenge",auth_state:"browser_challenge"})');
  const result=await f.daemon.execute('images-primary',['run-code','--filename',file]);
  assert.equal(result.logged_in,true);
  assert.equal(result.challenge,false);
  assert.equal(result.error_code,undefined);
  assert.equal(f.browser.isConnected(),true);
  assert.equal(f.calls.some(c=>c[0]==='challenge'),false);
});
