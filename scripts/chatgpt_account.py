"""Remembered-account selection; never reads or returns authentication secrets.

Vendored by chatgpt_bridge so each tool keeps its own browser/profile and runtime.
"""
import json


STATE_FUNCTION = r'''() => {
 const visible = n => !!n && !!n.getClientRects().length &&
   getComputedStyle(n).visibility !== 'hidden' && getComputedStyle(n).display !== 'none';
 const nodes = selector => Array.from(document.querySelectorAll(selector)).filter(visible);
 const text = n => (n.innerText || n.textContent || '').trim();
 const buttons = nodes('button,[role="button"],a');
 const login = buttons.some(n => /^(Log in|Sign in|登录|登入)$/i.test(text(n)));
 const chooser = nodes('[role="dialog"],dialog').some(n =>
   /Choose an account|选择.*[账帐]号|选择.*[账帐]户/i.test(text(n))) ||
   (buttons.some(n => /Log in to another account|登录其他[账帐]号|使用其他[账帐]户/i.test(text(n))) &&
    /Welcome back|欢迎回来|Choose an account/i.test(document.body.innerText));
 const challenge = /Just a moment|checking your browser/i.test(document.title) ||
   nodes('iframe[src*="challenges.cloudflare.com"]').length > 0;
 const credential = nodes('input[type="password"],input[autocomplete="one-time-code"],input[name="code"]').length > 0;
 // Tabs of one account can render in different UI languages (a zh-CN tab loaded earlier
 // next to a fresh en-US one, observed 2026-09-27); the login check must not depend on it.
 const profile = nodes('[data-testid="accounts-profile-button"],button[aria-label="Open profile menu"],button[aria-label="打开个人资料菜单"]').length > 0;
 const composer = nodes('#prompt-textarea,[contenteditable="true"][data-composer-markdown]').length > 0;
 const trusted = location.origin === 'https://chatgpt.com';
 const logged_in = trusted && profile && composer && !login && !chooser && !challenge && !credential;
 const current=location.pathname.match(/^\/c\/([^/]+)\/?$/)?.[1];
 const rendered=nodes('[data-user-message-bubble],[data-message-author-role="user"]').length>0;
 let rate_limited=false,background_rate_limited=false;
 for(const e of performance.getEntriesByType('resource')) {
   try {
     const u=new URL(e.name);
     if(e.responseStatus!==429||u.origin!=='https://chatgpt.com'||!u.pathname.startsWith('/backend-api/')||performance.now()-e.responseEnd>=120000)continue;
     // The sidebar can fail while the requested conversation and its generated
     // images are already readable. Poll still verifies the exact prompt before
     // accepting any image. Generation and unreadable-page failures stay blocking.
     const readCurrent=current&&['/backend-api/conversations/','/backend-api/conversation/'].some(p=>u.pathname===p+current);
     // Reading some other conversation (a previous page in this tab) says nothing about
     // whether this login can submit; counting it renewed the cooldown indefinitely.
     const readOther=/^\/backend-api\/conversations?\/[^/]+$/.test(u.pathname)&&!readCurrent;
     if(logged_in&&(u.pathname==='/backend-api/conversations'||readOther||(readCurrent&&rendered)))background_rate_limited=true;
     else rate_limited=true;
   } catch { /* Non-URL performance entries do not describe provider responses. */ }
 }
 return {url:location.href,title:document.title,ui_lang:document.documentElement.lang||'',logged_in,composer,account_chooser:chooser,
   login_in_progress:chooser && /Logging in|Signing in|正在登录|登录中/i.test(document.body.innerText),
   challenge,cloudflare:challenge,credential_required:credential,rate_limited,background_rate_limited,
   auth_state:challenge?'browser_challenge':credential?'credential_required':chooser?'account_selection_required':
     rate_limited?'rate_limited':logged_in?'authenticated':login?'login_required':'page_not_ready'};
}'''

STATUS_JS = 'async page => await page.evaluate(' + STATE_FUNCTION + ')'

SETTLE_JS = r'''async page => {
 try {
   await page.waitForFunction(() => {
     const state = (__STATE__)();
     return state.logged_in || state.account_chooser || state.challenge || state.credential_required || state.rate_limited || state.auth_state==='login_required';
   }, null, {timeout:10000});
 } catch(error) { if(error.name !== 'TimeoutError') throw error; }
 return await page.evaluate(__STATE__);
}'''.replace('__STATE__', STATE_FUNCTION)

# A stalled authenticated shell is different from an explicit sign-in page.
# Refresh only the same owned ChatGPT URL, once during an actual operation.
RECOVER_LOADING_JS = r'''async page => {
 if(new URL(page.url()).origin!=='https://chatgpt.com')return await page.evaluate(__STATE__);
 await page.reload({waitUntil:'domcontentloaded',timeout:20000});
 try {
   await page.waitForFunction(()=>{const s=(__STATE__)();return s.logged_in||s.account_chooser||s.challenge||s.credential_required||s.rate_limited||s.auth_state==='login_required'},null,{timeout:15000});
 } catch(e) {if(e.name!=='TimeoutError')throw e;}
 return await page.evaluate(__STATE__);
}'''.replace('__STATE__',STATE_FUNCTION)


def selection_code(account_name: str) -> str:
    if not isinstance(account_name, str) or not account_name.strip() or len(account_name) > 120:
        raise ValueError('Provide an explicit remembered account name (1-120 characters).')
    return r'''async page => {
 const inspect = __STATE__;
 const account = __ACCOUNT__;
 let state = await page.evaluate(inspect);
 if (state.logged_in) return {...state,account_action:'already_logged_in'};
 if (state.challenge || state.credential_required) return {...state,error_code:state.auth_state};
 if (!state.account_chooser || new URL(page.url()).origin !== 'https://chatgpt.com')
   return {...state,error_code:'account_selection_unavailable'};
 let action = 'wait_existing_login';
 if (!state.login_in_progress) {
   const escaped = account.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
   const matches = page.getByText(new RegExp('^'+escaped+'$','i'));
   const candidates = [];
   for (const candidate of await matches.all()) if (await candidate.isVisible()) candidates.push(candidate);
   if (candidates.length !== 1)
     return {...state,error_code:candidates.length?'account_ambiguous':'account_not_found'};
   await candidates[0].click({timeout:10000});
   action = 'selected_account';
 }
 try {
   await page.waitForFunction(() => {
     const s = (__STATE__)();
     return s.logged_in || s.challenge || s.credential_required;
   }, null, {timeout:25000});
 } catch (error) {
   if (error.name !== 'TimeoutError') throw error;
 }
 state = await page.evaluate(inspect);
 return {...state,account_action:action,...(state.logged_in?{}:
   {error_code:state.challenge||state.credential_required?state.auth_state:'account_login_pending'})};
}'''.replace('__STATE__', STATE_FUNCTION).replace('__ACCOUNT__', json.dumps(account_name.strip()))


ERROR_MESSAGES = {
    'account_selection_required': 'Select the explicitly authorized remembered account, or open the dedicated browser.',
    'account_selection_unavailable': 'No remembered-account chooser is open; inspect the dedicated browser.',
    'account_not_found': 'The authorized account is not visible; no other account was selected.',
    'account_ambiguous': 'More than one visible account matches; no account was selected.',
    'account_login_pending': 'Account sign-in is still pending. Check status; do not repeatedly click.',
    'credential_required': 'Complete the password or verification step in the dedicated browser.',
    'browser_challenge': 'Complete the browser verification in the dedicated browser.',
}
