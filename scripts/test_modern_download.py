"""Exercise the real downloader in isolated Chromium with byte-level fixtures."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest
import playwright
from web_images import DOWNLOAD_JS


class GalleryOriginalTransfer(unittest.TestCase):
    def test_original_identity_cached_lookup_and_metadata_only_result(self):
        source = r'''
const fs=require('fs'),assert=require('assert'),crypto=require('crypto');
const spec=JSON.parse(fs.readFileSync(0,'utf8'));
const {chromium}=require(spec.driver);
(async()=>{
 const browser=await chromium.launch({channel:'chrome',headless:true});
 try{
  const context=await browser.newContext();await context.route('**/*',r=>r.fulfill({body:'<html></html>',contentType:'text/html'}));
  const page=await context.newPage();await page.goto('https://chatgpt.com/');
  await page.setContent('<div data-turn-key="current"><div data-user-message-bubble>own prompt</div><div data-chatgpt-search-message-ids="response"><div data-testid="generated-image-gallery"><button data-testid="generated-image-preview" aria-label="Generated image 1"><img></button></div><div role="group" aria-label="Generated images"><button aria-label="Show generated image 2"><img></button></div></div></div>');
  await page.evaluate(()=>{
   const originalURL=i=>'https://chatgpt.com/backend-api/estuary/content?id=file_'+i+'&p=fsns&sig=private';
   window.originalURL=originalURL;window.fetches=[];
   for(const img of document.images){const n=img.closest('button').getAttribute('aria-label').match(/\d+/)[0];Object.defineProperties(img,{currentSrc:{value:'blob:https://chatgpt.com/'+n},naturalWidth:{value:1600},naturalHeight:{value:1000},complete:{value:true}});}
   window.entries=[{name:originalURL('stale')},{name:originalURL('wrong')},{name:originalURL(2)},{name:originalURL(1)},{name:originalURL(1)+'&width=500'},{name:'https://untrusted.invalid/content?id=file_1&p=fsns'}];
   performance.getEntriesByType=()=>window.entries;
   window.fetch=async url=>{
    window.fetches.push(url);const isBlob=url.startsWith('blob:'),u=isBlob?null:new URL(url),id=isBlob?url.split('/').at(-1):u.searchParams.get('id').replace('file_','');
    const r=new Response(new TextEncoder().encode('exact-original-'+id),{status:id==='stale'?404:200,headers:{'Content-Type':'image/png'}});
    Object.defineProperty(r,'url',{value:url});return r;
   };
  });
  const saved=[];
  const runtime={evaluate:page.evaluate.bind(page),request:{post:async(url,opt)=>{saved.push(opt.data);return {ok:()=>true}}}};
  const run=(index,used=[])=>new Function('return ('+spec.download.replaceAll('__KEY__',JSON.stringify('gallery:response:'+index)).replaceAll('__TARGET__','"unused"').replaceAll('__TRANSFER__','"http://127.0.0.1/fixture"').replaceAll('__USED_ORIGINALS__',JSON.stringify(used))+')')()(runtime);
  const first=await run(1);assert.equal(saved[0].toString(),'exact-original-1');assert.equal(first.original_id,'file_1');
  const second=await run(2,['file_1']);assert.equal(saved[1].toString(),'exact-original-2');assert.equal(second.original_id,'file_2');
  const fetches=await page.evaluate(()=>window.fetches);
  assert.equal(fetches.filter(u=>u.includes('file_wrong')).length,1,'Mismatched originals must not be fetched repeatedly');
  assert(!fetches.some(u=>u.includes('width=')||u.includes('untrusted')));
  assert(!JSON.stringify([first,second]).includes('private'));assert(!JSON.stringify(first).includes('data'));
  await page.evaluate(()=>{window.entries=[{name:originalURL('wrong')},{name:originalURL(1)+'&width=500'}]});
  await assert.rejects(run(1),/verified_gallery_original_unavailable/);assert.equal(saved.length,2,'Wrong bytes must never be saved');
  console.log(JSON.stringify({passed:true,originals:2,cachedMismatchFetches:1,untrustedRequests:0}));
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1});
'''
        result=subprocess.run([shutil.which('node'),'-e',source],input=json.dumps({
            'driver':str(Path(playwright.__file__).parent/'driver'/'package'),
            'download':DOWNLOAD_JS}),text=True,capture_output=True,encoding='utf-8',timeout=60,
            creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertTrue(json.loads(result.stdout)['passed'])


if __name__=='__main__':unittest.main()
