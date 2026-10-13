"""Bounded, page-local preparation and one-shot submission with DOM receipt."""

# Background extension targets cannot use CDP focus emulation. Activate only a
# unique, rendered, enabled control through its normal DOM click handler there;
# pointer actionability depends on animations that hidden Chrome can suspend.
ACTIVATE_CONTROL_JS = r'''
 async function activate(control) {
   await control.waitFor({state:'visible',timeout:4000});
   if(await page.evaluate(()=>document.hidden)) {
     await control.evaluate(el=>{
       if(!el.isConnected||!el.getClientRects().length||getComputedStyle(el).visibility==='hidden'||el.disabled||el.getAttribute('aria-disabled')==='true')throw Error('CONTROL_NOT_READY');
       el.click();
     },null,{timeout:4000});
   }else await control.click({timeout:4000});
 }
'''

SELECT_IMAGE_TOOL_JS = r'''async page => {
 __ACTIVATE__
 const mode=page.locator(':is(#prompt-textarea,[data-composer-body]) [data-inline-selection-pill][data-system-hint-type="picture_v2"], [data-composer-body] button[aria-label="Remove Create image"], [data-composer-body] button[aria-label="移除创建图片"]').first();
 if(await mode.isVisible().catch(()=>false))return true;
 const plus=page.locator('[data-testid="composer-plus-btn"],[data-composer-body] button[aria-label="Add files and more"],[data-composer-body] button[aria-label="添加文件和更多"]');
 const name=/^(Create (?:an? )?images?|创建图片|创建图像|生成图片)(?:\s|$)/;
 const tool=page.getByRole('button',{name}).or(page.getByRole('menuitem',{name})).and(page.locator(':visible'));
 for(let attempt=0;attempt<2;attempt++){
   try{
     // An already-open menu must not be toggled closed.
     if(!await tool.isVisible().catch(()=>false))await activate(plus);
     await activate(tool);
     await mode.waitFor({state:'visible',timeout:4000});
     return true;
   }catch(error){
     if(await mode.isVisible().catch(()=>false))return true;
     if(attempt===1)throw Error('IMAGE_TOOL_UNAVAILABLE');
     await page.keyboard.press('Escape');
   }
 }
}'''.replace('__ACTIVATE__',ACTIVATE_CONTROL_JS)

SUBMIT_PROMPT_JS = r'''async page => {
 __ACTIVATE__
 const expected=__PROMPT__,before=__BEFORE__;
 const mode=page.locator(':is(#prompt-textarea,[data-composer-body]) [data-inline-selection-pill][data-system-hint-type="picture_v2"], [data-composer-body] button[aria-label="Remove Create image"], [data-composer-body] button[aria-label="移除创建图片"]').first();
 const receipt=({expected,before,waitForReceipt=false})=>{
   const normal=text=>(text||'').normalize('NFC').replace(/[\s\u200b\u00ad]+/gu,'');
   const text=node=>{
     if(!node)return '';
     const copy=node.cloneNode(true);
     copy.querySelectorAll('[data-inline-selection-pill][data-system-hint-type="picture_v2"],[data-inline-selection-pill][data-id="picture_v2"],[data-prompt-link-href="chatgpt-system-hint://picture_v2"]').forEach(n=>n.remove());
     return normal(copy.value??copy.textContent);
   };
   const legacy=[...document.querySelectorAll('[data-message-author-role="user"]')];
   const users=legacy.length?legacy:[...document.querySelectorAll('[data-user-message-bubble]')];
   const last=users.at(-1),content=last?.querySelector('[data-testid="collapsible-user-message-content"],[data-search-result-target]')||last;
   const composer=[...document.querySelectorAll('#prompt-textarea,[data-composer-body] [contenteditable="true"][role="textbox"]')].find(n=>n.getClientRects().length&&getComputedStyle(n).visibility!=='hidden');
   const draft=text(composer);
   const state={sameDraft:draft===normal(expected),emptyDraft:!draft,accepted:users.length>before&&text(content)===normal(expected)};
   return waitForReceipt?state.accepted&&state.emptyDraft:state;
 };
 const snapshot=()=>page.evaluate(receipt,{expected,before});
 if(!await mode.isVisible().catch(()=>false))return {submitted:false,reason:'image_tool_unavailable'};
 if(!(await snapshot()).sameDraft)return {submitted:false,reason:'prompt_changed_before_send'};
 const button=page.locator('[data-testid="send-button"],[data-composer-body] button[aria-label="Send"],[data-composer-body] button[aria-label="发送"]').and(page.locator(':visible'));
 try{
   await button.waitFor({state:'visible',timeout:10000});
   await page.waitForFunction(()=>{
     const buttons=[...document.querySelectorAll('[data-testid="send-button"],[data-composer-body] button[aria-label="Send"],[data-composer-body] button[aria-label="发送"]')].filter(n=>n.getClientRects().length);
     return buttons.length===1&&!buttons[0].disabled&&buttons[0].getAttribute('aria-disabled')!=='true';
   },null,{timeout:10000,polling:100});
 }catch{return {submitted:false,reason:'send_button_unavailable'};}
 // Re-read immediately before the only click. A late upload or person edit
 // must never be silently replaced with our older draft.
 if(!(await snapshot()).sameDraft)return {submitted:false,reason:'prompt_changed_before_send'};
 await activate(button);
 let confirmed=false,receiptError=null;
 try{
   // Pass the function directly: reconstructing it with new Function inside
   // the page can be blocked by the live site's Content Security Policy.
   await page.waitForFunction(receipt,{expected,before,waitForReceipt:true},{timeout:15000,polling:100});
   await page.waitForURL(/^https:\/\/chatgpt\.com\/c\/[a-zA-Z0-9-]+$/,{timeout:8000});
   confirmed=true;
 }catch(error){receiptError=/^[A-Za-z]{1,32}$/.test(error.name||'')?error.name:'Error';}
 const url=page.url();
 return {submitted:true,confirmed,url:/^https:\/\/chatgpt\.com\/c\/[a-zA-Z0-9-]+$/.test(url)?url:'',reason:confirmed?null:'submission_acknowledgement_missing',receipt_error:receiptError};
}'''.replace('__ACTIVATE__',ACTIVATE_CONTROL_JS)
