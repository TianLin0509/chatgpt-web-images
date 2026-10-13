"""Real browser DOM regressions for stuck drafts and uncertain sends."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import pytest
import web_images as w
from unittest.mock import patch


def test_one_click_waits_for_exact_message_and_preserves_uncertain_sends():
    node=shutil.which('node')
    if not node or subprocess.run([node,'-e',"require('playwright')"],capture_output=True).returncode:
        pytest.skip('Optional Playwright runtime required')
    source=r'''
const {chromium}=require('playwright'),assert=require('node:assert/strict');
const spec=JSON.parse(require('fs').readFileSync(0,'utf8'));
(async()=>{
 const browser=await chromium.launch({headless:true,...(process.env.CHATGPT_WEB_IMAGES_TEST_CHROME?{executablePath:process.env.CHATGPT_WEB_IMAGES_TEST_CHROME}:{})});
 try{
  for(const scenario of ['ok','background-modern','draft-changed','delayed-button','lost-ack','old-message','handoff-during-click']){
   const page=await browser.newPage();
   await page.route('https://chatgpt.com/**',route=>route.fulfill({contentType:'text/html',body:`<div data-message-author-role="user">draw a red square</div><div data-composer-body><div id="prompt-textarea" contenteditable="true">draw a red square</div><button aria-label="Remove Create image">Create image</button><button data-testid="send-button">Send</button></div>`}));
   await page.goto('https://chatgpt.com/c/receipt-test');
   await page.evaluate(scenario=>{
    window.clicks=0;const composer=document.querySelector('#prompt-textarea'),button=document.querySelector('[data-testid="send-button"]');
    if(scenario==='background-modern'){
     Object.defineProperty(document,'hidden',{get:()=>true});
     composer.removeAttribute('id');composer.setAttribute('role','textbox');
     button.removeAttribute('data-testid');button.setAttribute('aria-label','Send');
    }
    if(scenario==='draft-changed')composer.textContent='a different task';
    if(scenario==='delayed-button'){button.disabled=true;setTimeout(()=>{button.disabled=false;},100);}
    button.onclick=()=>{
     window.clicks++;
     if(['lost-ack','old-message','handoff-during-click'].includes(scenario))return;
     const user=document.createElement('div');user.setAttribute('data-message-author-role','user');user.textContent=composer.textContent;document.body.prepend(user);composer.textContent='';
    };
   },scenario);
   // Shorten only the no-receipt watchdog in this fixture; the same script and
   // browser events still decide whether the server acknowledged the prompt.
   const wait=page.waitForFunction.bind(page);page.waitForFunction=(fn,arg,opts)=>wait(fn,arg,{...opts,timeout:Math.min(opts?.timeout||30000,400)});
   const fn=new Function('return ('+spec.submit+')')();
   const result=await fn(page);
   const clicks=await page.evaluate(()=>window.clicks);
   if(scenario==='draft-changed'){assert.equal(result.submitted,false);assert.equal(clicks,0);}
   else if(['lost-ack','old-message','handoff-during-click'].includes(scenario)){assert.equal(result.submitted,true);assert.equal(result.confirmed,false);assert.equal(clicks,1);}
   else{assert.equal(result.confirmed,true);assert.equal(clicks,1);}
   await page.close();
  }
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1)});
'''
    result=subprocess.run([node,'-e',source],input=json.dumps({'submit':w.SUBMIT_PROMPT_JS.replace('__PROMPT__',json.dumps('draw a red square')).replace('__BEFORE__','1')}),encoding='utf-8',capture_output=True,timeout=35)
    assert result.returncode==0,result.stderr


class SubmissionContracts(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data=Path(self.temp.name)
        patcher=patch.object(w,'DATA',self.data)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_handoff_after_send_keeps_request_identity(self):
        error=w.ImageError('human_handoff','A person has the browser')
        with patch.object(w,'ensure_browser'),patch.object(w,'cli'),patch.object(w,'run_js',side_effect=[1,True,True,error]):
            first=w.start(prompt='hello',request_id='after-send',output_dir=str(self.data))
        assert first['status']=='submission_uncertain'
        with patch.object(w,'ensure_browser') as browser:
            again=w.start(prompt='hello',request_id='after-send',output_dir=str(self.data))
        browser.assert_not_called()
        assert first['job_id']==again['job_id']

    def test_explicit_unsent_result_releases_lane_and_reports_reason(self):
        with patch.object(w,'ensure_browser'),patch.object(w,'cli'),patch.object(w,'run_js',side_effect=[1,True,True,{'submitted':False,'reason':'send_button_unavailable'}]):
            result=w.start(prompt='hello',output_dir=str(self.data))
        assert result['status']=='preparation_failed'
        assert result['error']['code']=='send_button_unavailable'
        assert w.active_job() is None

    def test_missing_receipt_retains_conversation_and_does_not_resend(self):
        response={'submitted':True,'confirmed':False,'url':'https://chatgpt.com/c/receipt-test'}
        with patch.object(w,'ensure_browser'),patch.object(w,'cli'),patch.object(w,'run_js',side_effect=[1,True,True,response]):
            first=w.start(prompt='hello',request_id='receipt-1',output_dir=str(self.data))
        assert first['status']=='submission_uncertain'
        assert first['conversation_url']==response['url']
        assert set(['fill_prompt','select_image_tool','submit_prompt']).issubset(first['timings'])

    def test_changed_draft_keeps_page_ownership_for_inspection(self):
        with patch.object(w,'ensure_browser'),patch.object(w,'cli'),patch.object(w,'run_js',side_effect=[1,True,True,{'submitted':False,'reason':'prompt_changed_before_send'}]):
            result=w.start(prompt='hello',output_dir=str(self.data))
        assert result['status']=='needs_attention'
        assert w.active_job()['job_id']==result['job_id']
