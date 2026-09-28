"""Observed legacy and gallery ChatGPT DOM, scoped to the last user message.

No credentials, page mutation, model selection or prompt submission lives here.
"""

USER_SELECTOR = ':is([data-message-author-role="user"],[data-user-message-bubble])'

DOM_IMAGES_JS = r'''
function messageUsers() {
 const old=Array.from(document.querySelectorAll('[data-message-author-role="user"]'));
 return old.length?old:Array.from(document.querySelectorAll('[data-user-message-bubble]'));
}
function ownedImages() {
 const user=messageUsers().at(-1);
 if(!user)return [];
 const modern=user.hasAttribute('data-user-message-bubble');
 const root=modern?user.closest('[data-turn-key]'):document;
 if(!root)return [];
 const selector=modern?'[data-testid="generated-image-preview"] img,[role="group"][aria-label="Generated images"] button img'
  :'[data-testid^="conversation-turn-"] img[alt^="Generated image"], [data-testid^="conversation-turn-"] img[alt^="生成的图片"]';
 const seen=new Set(),assets=[];
 for(const node of root.querySelectorAll(selector)) {
   if(!(user.compareDocumentPosition(node)&Node.DOCUMENT_POSITION_FOLLOWING))continue;
   if(node.closest('[data-message-author-role="user"],[data-user-message-bubble]'))continue;
   if(!modern&&node.closest('[data-testid^="conversation-turn-"]')===user.closest('[data-testid^="conversation-turn-"]'))continue;
   const source=node.currentSrc||node.src;if(!source&&!modern)continue;
   let key,index=0;
   if(modern){
     const button=node.closest('button');
     index=Number((button?.getAttribute('aria-label')||'').match(/(?:image|图片)\s*(\d+)\s*$/i)?.[1]);
     const group=node.closest('[data-chatgpt-search-message-ids]');
     const ids=group?.getAttribute('data-chatgpt-search-message-ids')?.trim().split(/\s+/);
     if(!index||!ids?.length)continue;
     key='gallery:'+ids.join(',')+':'+index;
   }else{
     const url=new URL(source,location.href),id=url.searchParams.get('id')||url.searchParams.get('file_id');
     key=url.origin+url.pathname+(id?'?id='+id:'');
   }
   if(seen.has(key))continue;seen.add(key);
   assets.push({node,key,alt:node.alt||'Generated image '+index,width:node.naturalWidth,height:node.naturalHeight,
     modern,index,ready:node.complete&&node.naturalWidth>=256&&node.naturalHeight>=256});
 }
 return modern?assets.sort((a,b)=>a.index-b.index):assets;
}
'''

POLL_JS = r'''async page => {
 // Hidden/offscreen Chrome does not load lazy gallery thumbnails after scrolling.
 // Request only this turn's already-present generated assets, without focus changes.
 await page.evaluate(()=>{__DOM__ for(const a of ownedImages())if(!a.ready){a.node.loading='eager';a.node.scrollIntoView({block:'center'});}});
 return await page.evaluate(()=>{
   __DOM__
   const users=messageUsers(),lastUser=users.at(-1);
   const userContent=lastUser?.querySelector('[data-testid="collapsible-user-message-content"],[data-search-result-target]')||lastUser;
   let userText=userContent?.innerText||'';
   if(userContent?.querySelector('[data-inline-selection-pill][data-id="picture_v2"]')){
     const copy=userContent.cloneNode(true);copy.querySelectorAll('[data-inline-selection-pill][data-id="picture_v2"]').forEach(n=>n.remove());userText=copy.textContent||'';
   }
   const modern=!!lastUser?.hasAttribute('data-user-message-bubble');
   const turns=Array.from(document.querySelectorAll('[data-testid^="conversation-turn-"]'));
   const lastTurn=modern?lastUser.closest('[data-turn-key]'):turns.at(-1);
   const response=modern?lastTurn?.querySelector('[data-conversation-role="assistant"]')?.parentElement:lastTurn;
   const assets=ownedImages();
   const turnComplete=modern?!!lastTurn?.querySelector('button[aria-label="Copy image"],button[aria-label="Copy response"]'):
     !!lastTurn&&!!lastTurn.querySelector('[data-testid="copy-turn-action-button"]')&&!!lastUser&&!!(lastUser.compareDocumentPosition(lastTurn)&Node.DOCUMENT_POSITION_FOLLOWING);
   const stopButton=document.querySelector('[data-testid="stop-button"],button[aria-label="Stop generating"],button[aria-label="Stop streaming"],button[aria-label="Stop response"]');
   return {url:location.href,stop:!!stopButton&&!!stopButton.getClientRects().length&&getComputedStyle(stopButton).visibility!=='hidden',
     turn_complete:turnComplete,images:assets.filter(a=>a.ready).map(({node,...a})=>a),pending_images:assets.filter(a=>!a.ready).length,
     text:(response?.innerText||'').slice(-1600),user_count:users.length,user_text:userText};
 });
}'''.replace('__DOM__',DOM_IMAGES_JS)

MODERN_DOWNLOAD_JS = r'''
 const modernAsset=await page.evaluate(key=>{__DOM__ const a=ownedImages().find(a=>a.key===key);return a?.modern?{index:a.index}:null;},__KEY__);
 if(modernAsset){
   if(__TRANSFER__){
     const original=await page.evaluate(async ({key,used})=>{
       __DOM__
       const asset=ownedImages().find(a=>a.key===key);
       if(!asset?.ready)throw Error('original_not_ready');
       const blob=await(await fetch(asset.node.currentSrc||asset.node.src,{signal:AbortSignal.timeout(10000)})).arrayBuffer();
       if(!blob.byteLength||blob.byteLength>50*1024*1024)throw Error('original_size_invalid');
       const hex=b=>Array.from(new Uint8Array(b)).map(n=>n.toString(16).padStart(2,'0')).join('');
       const expected=hex(await crypto.subtle.digest('SHA-256',blob));
       const urls=[...new Set(performance.getEntriesByType('resource').map(e=>e.name))].filter(raw=>{
         try{const u=new URL(raw);return u.origin==='https://chatgpt.com'&&u.pathname==='/backend-api/estuary/content'
           &&['fs','fsns'].includes(u.searchParams.get('p'))&&/^file[_-]/.test(u.searchParams.get('id')||'')
           &&[...u.searchParams.keys()].every(k=>['id','ts','p','cid','sig','v'].includes(k));}catch{return false;}
       });
       // Cache only hash metadata on this page, not image bytes or credentials.
       // A batch scans each candidate at most once, instead of N squared fetches.
       const cache=globalThis.__webImagesOriginalHashes ||= new Map();
       if(cache.size>100)cache.clear();
       const deadline=Date.now()+30000;
       for(const source of urls.slice(-64)){
         const id=new URL(source).searchParams.get('id');if(used.includes(id))continue;
         const known=cache.get(source);if(known&&known!==expected)continue;
         if(Date.now()>=deadline)throw Error('original_lookup_timeout');
       // Some CDP sessions report an empty response.body() for a cached image.
       // Read original bytes in the authenticated page and transfer them privately
       // to Node; only metadata is returned by the tool, never this binary payload.
         const r=await fetch(source,{credentials:'same-origin',cache:'force-cache',signal:AbortSignal.timeout(Math.min(10000,deadline-Date.now()))});
         if(r.status===404||r.status===410)continue;
         if(!r.ok||!/^image\/(png|jpeg|webp)(?:;|$)/i.test(r.headers.get('content-type')||''))throw Error('original_response_invalid');
         const final=new URL(r.url);if(final.origin!=='https://chatgpt.com'||final.pathname!=='/backend-api/estuary/content')throw Error('original_response_invalid');
         const bytes=await r.arrayBuffer();if(!bytes.byteLength||bytes.byteLength>50*1024*1024)throw Error('original_size_invalid');
         const digest=Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',bytes))).map(n=>n.toString(16).padStart(2,'0')).join('');
         cache.set(source,digest);if(digest!==expected)continue;
         const data=await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onerror=()=>reject(Error('original_read_failed'));reader.onload=()=>resolve(String(reader.result).split(',')[1]);reader.readAsDataURL(new Blob([bytes]));});
         return {data,id};
       }
       throw Error('verified_gallery_original_unavailable');
     },{key:__KEY__,used:__USED_ORIGINALS__});
       const bytes=Buffer.from(original.data,'base64');
       if(!bytes.length||bytes.length>50*1024*1024)throw Error('original_response_invalid');
       const saved=await page.request.post(__TRANSFER__,{data:bytes,headers:{'Content-Type':'application/octet-stream'},timeout:5000});
       if(!saved.ok())throw Error('local_original_save_failed');
       return {saved:true,method:'authenticated_gallery_original',original_id:original.id};
   }
   const close=page.getByRole('button',{name:/^Close dialog$|^关闭对话框$/});
   if(await close.isVisible().catch(()=>false))await close.evaluate(n=>n.click());
   const thumb=page.getByRole('button',{name:new RegExp('^Show generated image '+modernAsset.index+'$')});
   if(await thumb.count())await thumb.evaluate(n=>n.click());
   const share=page.getByRole('button',{name:new RegExp('^Share generated image '+modernAsset.index+'$')});
   await share.waitFor({state:'visible',timeout:10000});await share.evaluate(n=>n.click());
   const single=page.getByRole('menuitem',{name:/^This image$|^此图片$/});
   if(await single.isVisible().catch(()=>false))await single.evaluate(n=>n.click());
   const downloadButton=page.getByRole('button',{name:/^Download$|^下载$/});
   await downloadButton.waitFor({state:'visible',timeout:10000});
   const downloading=page.waitForEvent('download',{timeout:30000});
   await downloadButton.evaluate(n=>n.click());
   const download=await downloading;
   if(await download.failure())throw Error('original_download_failed');
   await download.saveAs(__TARGET__);
   if(await close.isVisible().catch(()=>false))await close.evaluate(n=>n.click());
   return {saved:true,method:'native_gallery_download'};
 }
'''.replace('__DOM__',DOM_IMAGES_JS)
