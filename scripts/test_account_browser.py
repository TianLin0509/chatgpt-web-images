"""Isolated real Chromium fixtures; no live account, cookies or messages used."""
import json
import unittest
import subprocess
import shutil
from pathlib import Path
import playwright
from playwright.sync_api import sync_playwright
from chatgpt_account import STATUS_JS, selection_code


class AccountBrowser(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(channel='chrome', headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.context = self.browser.new_context()
        self.context.route('**/*', lambda route: route.fulfill(body='<html><body></body></html>',content_type='text/html'))
        self.page = self.context.new_page()
        self.page.goto('https://chatgpt.com/')
        self.addCleanup(self.context.close)

    def state(self):
        from chatgpt_account import STATE_FUNCTION
        return self.page.evaluate(STATE_FUNCTION)

    def test_image_pill_survives_fill_before_selection(self):
        self.page.set_content('<div id="prompt-textarea" contenteditable="true"><span contenteditable="false" data-inline-selection-pill data-system-hint-type="picture_v2">Create image</span></div>')
        # Reproduce the old destructive ordering in real Chromium.
        self.page.locator('#prompt-textarea').fill('A boat')
        self.assertEqual(self.page.locator('[data-inline-selection-pill]').count(), 0)
        # New preparation writes the prompt first, then the UI inserts its tool pill.
        self.page.locator('#prompt-textarea').fill('A boat')
        self.page.locator('#prompt-textarea').evaluate("n=>{const pill=document.createElement('span');pill.contentEditable='false';pill.setAttribute('data-inline-selection-pill','');pill.setAttribute('data-system-hint-type','picture_v2');pill.textContent='Create image';n.append(pill)}")
        self.assertEqual(self.page.locator('[data-inline-selection-pill]').count(), 1)
        self.assertIn('A boat', self.page.locator('#prompt-textarea').inner_text())

    def test_image_pill_is_excluded_without_stripping_user_words(self):
        from web_images import POLL_JS, prompt_hash
        prompt = 'Keep the literal words Create image.\nSecond line.'
        self.page.set_content('<div data-message-author-role="user"></div>')
        self.page.locator('[data-message-author-role="user"]').evaluate('''(n,prompt)=>{
          n.append(document.createTextNode(prompt));
          const pill=document.createElement('span');pill.setAttribute('data-inline-selection-pill','');
          pill.setAttribute('data-id','picture_v2');pill.textContent='Create image';n.append(pill);
          n.append(document.createTextNode(' '));
        }''',prompt)
        result=self.page.evaluate('async()=>{const page={evaluate:async fn=>fn()};return ('+POLL_JS+')(page)}')
        self.assertEqual(prompt_hash(result['user_text']),prompt_hash(prompt))
        self.assertIn('literal words Create image',result['user_text'])

    def test_remembered_name_and_profile_are_not_authenticated(self):
        self.page.set_content('<button data-testid="accounts-profile-button">TIAN LIN</button><div id="prompt-textarea">draft</div><div role="dialog">Welcome back. Choose an account to continue.<button>TIAN LIN</button></div>')
        self.assertFalse(self.state()['logged_in'])
        self.assertTrue(self.state()['account_chooser'])

    def test_real_profile_and_composer_are_authenticated(self):
        self.page.set_content('<button data-testid="accounts-profile-button">Profile</button><div id="prompt-textarea">draft</div>')
        self.assertTrue(self.state()['logged_in'])

    def test_hidden_profile_is_not_authenticated(self):
        self.page.set_content('<button style="display:none" data-testid="accounts-profile-button">Profile</button><div id="prompt-textarea">draft</div>')
        self.assertFalse(self.state()['logged_in'])

    def test_sign_in_page_is_not_authenticated(self):
        self.page.set_content('<button data-testid="accounts-profile-button">Profile</button><div id="prompt-textarea">draft</div><button>Log in</button>')
        self.assertFalse(self.state()['logged_in'])

    def test_password_and_challenge_are_reported(self):
        self.page.set_content('<input type="password">')
        self.assertEqual(self.state()['auth_state'],'credential_required')
        self.page.set_content('<title>Just a moment...</title>')
        self.assertEqual(self.state()['auth_state'],'browser_challenge')

    def test_no_auth_from_conversation_text(self):
        self.page.set_content('<p>TIAN LIN 中转站已就绪</p>')
        self.assertFalse(self.state()['logged_in'])

    def test_selection_interactions(self):
        driver = str(Path(playwright.__file__).parent/'driver'/'package')
        source = r'''
const fs=require('fs'),assert=require('assert');
const spec=JSON.parse(fs.readFileSync(0,'utf8'));
const {chromium}=require(spec.driver);
(async()=>{
 const browser=await chromium.launch({channel:'chrome',headless:true});
 try {
  const context=await browser.newContext();
  await context.route('**/*',r=>r.fulfill({body:'<html><body></body></html>',contentType:'text/html'}));
  const page=await context.newPage();await page.goto('https://chatgpt.com/');
  const run = new Function('return ('+spec.selection+')')();
  const signed='<button data-testid="accounts-profile-button">Profile</button><div id="prompt-textarea">draft</div>';
  const chooser=names=>'<div role="dialog"><h1>Welcome back</h1>Choose an account to continue.'+names.map(n=>'<button class="account">'+n+'</button>').join('')+'</div>';
  let checks=0;
  await page.setContent(chooser(['TIAN LIN','Someone else']));
  await page.evaluate(signed=>{window.clicks=0;document.querySelector('.account').onclick=()=>{window.clicks++;document.body.innerHTML=signed};},signed);
  let result=await run(page);assert(result.logged_in);assert.equal(result.account_action,'selected_account');assert.equal(await page.evaluate(()=>window.clicks),1);checks++;
  result=await run(page);assert.equal(result.account_action,'already_logged_in');checks++;
  await page.setContent(chooser(['Someone else']));result=await run(page);assert.equal(result.error_code,'account_not_found');checks++;
  await page.setContent(chooser(['TIAN LIN','TIAN LIN']));result=await run(page);assert.equal(result.error_code,'account_ambiguous');checks++;
  await page.setContent(chooser(['TIAN LIN'])+'<span>Logging in...</span>');
  await page.evaluate(signed=>{window.clicks=0;document.querySelector('.account').onclick=()=>window.clicks++;setTimeout(()=>document.body.innerHTML=signed,100);},signed);
  result=await run(page);assert(result.logged_in);assert.equal(result.account_action,'wait_existing_login');assert.equal(await page.evaluate(()=>window.clicks),0);checks++;
  await page.setContent('<input type="password">');result=await run(page);assert.equal(result.error_code,'credential_required');checks++;
  await page.setContent('<title>Just a moment</title>');result=await run(page);assert.equal(result.error_code,'browser_challenge');checks++;
  await page.setContent(chooser(['TIAN LIN']));
  await page.evaluate(()=>document.querySelector('.account').onclick=()=>document.body.innerHTML='<input autocomplete="one-time-code">');
  result=await run(page);assert.equal(result.error_code,'credential_required');checks++;
  await page.setContent(chooser(['TIAN LIN'])+'<span>Logging in...</span>');
  const shortRun=new Function('return ('+spec.selection.replace('timeout:25000','timeout:30')+')')();
  result=await shortRun(page);assert.equal(result.error_code,'account_login_pending');checks++;
  await page.goto('https://example.com/');await page.setContent(chooser(['TIAN LIN']));result=await run(page);assert.equal(result.error_code,'account_selection_unavailable');checks++;
  console.log(JSON.stringify({interaction_checks:checks,passed:true}));
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1)});
'''
        result = subprocess.run([shutil.which('node'),'-e',source], input=json.dumps({'driver':driver,'selection':selection_code('TIAN LIN')}),text=True,capture_output=True,encoding='utf-8',timeout=60)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout)['interaction_checks'],10)


if __name__ == '__main__':
    unittest.main()
