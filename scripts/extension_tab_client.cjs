'use strict';
const fs=require('fs');
const path=require('path');
const {spawn}=require('child_process');
const {statePath,validate}=require('./extension_tab_daemon.cjs');
const {alive}=require('./tab_client.cjs');
const sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));
function read(file){try{return JSON.parse(fs.readFileSync(file,'utf8'));}catch{return null;}}

async function ensureDaemon(options) {
  validate(options);
  const file=statePath(options.poolRoot,options.identity,options.browserRoot);
  fs.mkdirSync(options.poolRoot,{recursive:true});
  let state=read(file);
  if(await alive(state))return state;
  const lock=file+'.lock';
  let fd;
  try {fd=fs.openSync(lock,'wx');} catch(e) {
    if(e.code!=='EEXIST')throw e;
    if(Date.now()-fs.statSync(lock).mtimeMs>30000)throw Error('Extension daemon stale start lock');
  }
  try {
    if(fd!==undefined) {
      state=read(file);
      if(!await alive(state)) {
        const child=spawn(process.execPath,[path.join(__dirname,'extension_tab_daemon.cjs'),JSON.stringify(options)],
          {detached:true,stdio:'ignore',windowsHide:true});
        child.unref();
      }
    }
    for(let i=0;i<60;i++) {
      state=read(file);
      if(await alive(state))return state;
      await sleep(250);
    }
    throw Error('Extension daemon unavailable');
  }finally{if(fd!==undefined){fs.closeSync(fd);fs.unlinkSync(lock);}}
}

function category(error) {
  const message=String(error?.message||error);
  if(/Extension pairing/.test(message))return 'Extension pairing required';
  if(/Timeout|timeout|aborted/.test(message))return 'Timeout';
  return 'Extension browser unavailable';
}

async function main(binding,argv=process.argv.slice(2),{ensure=ensureDaemon,fetchImpl=fetch}={}) {
  // New Hubs provide one ordinary-browser broker shared with the web roundtable
  // and bridge. Older Hubs retain the already verified standalone extension path.
  const shared=path.join(binding.hubCore,'ordinary-browser-client.js');
  if(ensure===ensureDaemon&&fs.existsSync(shared))return require(shared).main(binding,argv);
  let out;
  try {
    if(!/^[a-z0-9_-]{1,64}$/.test(binding.id||''))throw Error('Invalid extension lane');
    validate(binding);
    const state=await ensure(binding);
    const timeoutMs=Number(process.env.CHATGPT_WEB_IMAGES_STEP_TIMEOUT_MS)||50000;
    const response=await fetchImpl(`http://127.0.0.1:${state.port}/call`,{
      method:'POST',headers:{'content-type':'application/json','x-token':state.token},
      body:JSON.stringify({lane:binding.id,argv,timeoutMs}),signal:AbortSignal.timeout(timeoutMs+5000)
    });
    if(!response.ok)throw Error('Extension daemon HTTP failure');
    out=await response.json();
    if(!out||typeof out!=='object'||(!Object.hasOwn(out,'result')&&!Object.hasOwn(out,'isError')))throw Error('Extension daemon protocol failure');
  }catch(error){out={isError:true,error:category(error)};}
  // No CDP fallback: a failure must not switch to another Chrome or profile.
  process.stdout.write(JSON.stringify(out)+'\n');
  if(out.isError)process.exitCode=1;
  return out;
}
module.exports={main,ensureDaemon,category};
