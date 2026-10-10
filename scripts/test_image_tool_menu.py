"""Use a real DOM to guard against sidebar chat names shadowing the tool menu."""
import json
import os
import shutil
import subprocess

import pytest
import web_images as w


def test_image_tool_menu_ignores_same_named_sidebar_chats():
    node = shutil.which('node')
    if not node or subprocess.run([node, '-e', "require('playwright')"], capture_output=True).returncode:
        pytest.skip('Optional Playwright Node runtime required for real DOM regression')
    source = r'''
const {chromium}=require('playwright'),assert=require('node:assert/strict');
const input=JSON.parse(require('fs').readFileSync(0,'utf8'));
(async()=>{
 const browser=await chromium.launch({headless:true,...(process.env.CHATGPT_WEB_IMAGES_TEST_CHROME?{executablePath:process.env.CHATGPT_WEB_IMAGES_TEST_CHROME}:{})});
 try{
  for(const [tag,label] of [['button','Create image Visualize anything'],['button','Create an image'],['div role="menuitem"','创建图片']]){
   const page=await browser.newPage();
   await page.setContent(`<nav><div role="group"><a href="#foreign"><span>Create image</span><span>Create image</span></a></div></nav>
    <button data-testid="composer-plus-btn">Add files and more</button><div id="prompt-textarea"></div>
    <section id="tools" hidden><${tag} id="image-tool">${label}</${tag.split(' ')[0]}></section>`);
   await page.evaluate(()=>{
    document.querySelector('[data-testid="composer-plus-btn"]').onkeydown=e=>{if(e.key==='Enter')document.querySelector('#tools').hidden=false;};
    document.querySelector('#image-tool').onclick=()=>{document.querySelector('#prompt-textarea').innerHTML='<span data-inline-selection-pill data-system-hint-type="picture_v2"></span>';};
   });
   assert.equal(await new Function('return ('+input+')')()(page),true);
   assert.equal(await page.locator('#prompt-textarea [data-inline-selection-pill]').count(),1);
   assert.equal(page.url(),'about:blank');
   await page.close();
  }
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1)});
'''
    result = subprocess.run([node, '-e', source], input=json.dumps(w.SELECT_IMAGE_TOOL_JS),
                            encoding='utf-8', capture_output=True, timeout=45, env=os.environ.copy())
    assert result.returncode == 0, result.stderr
