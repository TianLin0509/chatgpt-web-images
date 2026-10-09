'use strict';
// Read only the official extension's local pairing token through an approved
// setup connection, and store a current-user DPAPI encrypted copy.
const fs=require('fs');
const path=require('path');
const {spawnSync}=require('child_process');
const {validate,EXTENSION_ID}=require('./extension_tab_daemon.cjs');
async function pair(options) {
  validate(options);
  const {HubChrome}=require(path.join(options.hubCore,'hub-chrome.js'));
  const hub=new HubChrome({root:options.browserRoot});
  if(!await hub.endpoint())throw Error('Ordinary browser setup requires one approved extension connection; browser preserved');
  const tab=await hub.openTab(options.identity,`chrome-extension://${EXTENSION_ID}/status.html`);
  const page=await hub.page(tab.targetId);
  try {
    let token;
    for(let i=0;i<20;i++) {
      const result=await page.call('Runtime.evaluate',{
        expression:`location.origin==='chrome-extension://${EXTENSION_ID}' ? localStorage.getItem('auth-token') : null`,
        returnByValue:true
      });
      token=result.result?.value;
      if(typeof token==='string' && token.length>=16)break;
      await new Promise(resolve=>setTimeout(resolve,250));
    }
    if(typeof token!=='string'||token.length<16)throw Error('Official Playwright extension not installed or not initialized');
    const protectedResult=spawnSync('python',[path.join(__dirname,'extension_token_dpapi.py'),'protect'],
      {input:token,encoding:'utf8',windowsHide:true,timeout:10000});
    if(protectedResult.error||protectedResult.status!==0)throw Error('Extension pairing protection failed');
    if(fs.existsSync(options.tokenFile)) {
      const previous=JSON.parse(fs.readFileSync(options.tokenFile,'utf8'));
      if(previous.identity!==options.identity||path.resolve(previous.root).toLowerCase()!==path.resolve(options.browserRoot).toLowerCase())
        throw Error('Existing pairing belongs to another profile; preserved');
    }
    fs.mkdirSync(path.dirname(options.tokenFile),{recursive:true});
    const record={version:1,identity:options.identity,root:options.browserRoot,protectedToken:protectedResult.stdout.trim()};
    const temp=options.tokenFile+'.'+process.pid+'.tmp';
    fs.writeFileSync(temp,JSON.stringify(record),'utf8');
    fs.renameSync(temp,options.tokenFile);
    return {paired:true,identity:options.identity,encrypted:true,setupTargetId:tab.targetId};
  }finally{page.close();await hub.closeTab(tab.targetId);}
}
if(require.main===module) {
  pair(JSON.parse(fs.readFileSync(process.argv[2],'utf8'))).then(result=>console.log(JSON.stringify(result)))
    .catch(()=>{console.error('Official extension pairing failed; token and browser data withheld');process.exitCode=1;});
}
module.exports={pair};
