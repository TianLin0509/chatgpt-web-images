"""ChatGPT web images: isolated Playwright CLI session, text-only MCP results."""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from typing import Annotated
from pydantic import Field

VERSION = '0.2.0'
CONFIG_DIR = Path(os.environ.get('CHATGPT_WEB_IMAGES_CONFIG_DIR', str(Path.home()/'.config'/'chatgpt-web-images')))
CONFIG_FILE = CONFIG_DIR/'settings.json'


def load_settings():
    if not CONFIG_FILE.exists():
        return {}
    value=json.loads(CONFIG_FILE.read_text(encoding='utf-8'))
    if not isinstance(value,dict):
        raise ValueError('settings.json must contain an object')
    return value


SETTINGS = load_settings()
def setting(env, key, default):
    return Path(os.environ.get(env) or SETTINGS.get(key) or default).expanduser().resolve()


DATA = setting('CHATGPT_WEB_IMAGES_DATA','data_dir',Path(os.environ.get('LOCALAPPDATA',str(Path.home()/'.local'/'share')))/'ChatGPTWebImages')
AUTH_VALUE = os.environ.get('CHATGPT_WEB_IMAGES_AUTH') or SETTINGS.get('auth_file')
AUTH = Path(AUTH_VALUE).expanduser().resolve() if AUTH_VALUE else None
SESSION = 'chatgpt-web-images'
CLI_ENTRY = setting('CHATGPT_WEB_IMAGES_CLI','cli_entry',DATA/'runtime'/'node_modules'/'@playwright'/'cli'/'playwright-cli.js')
DEFAULT_OUTPUT = setting('CHATGPT_WEB_IMAGES_OUTPUT','output_dir',DATA/'output')
TERMINAL = {'complete','failed','preparation_failed','cancelled','partial','count_mismatch'}


class ImageError(RuntimeError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp-' + uuid.uuid4().hex)
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temp, path)


def prompt_hash(text):
    return hashlib.sha256(text.replace('\r\n','\n').strip('\ufeff \t\r\n').encode('utf-8')).hexdigest()


@contextlib.contextmanager
def locked():
    DATA.mkdir(parents=True, exist_ok=True)
    # Explicit workspace makes session identity independent of npm cache/install location.
    (DATA/'.playwright').mkdir(exist_ok=True)
    with (DATA / 'operation.lock').open('a+b') as handle:
        handle.seek(0)
        if os.name != 'nt':
            raise ImageError('unsupported_platform', 'This local plugin currently supports Windows.')
        import msvcrt
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise ImageError('busy', 'Another plugin operation is running; retry shortly.') from exc
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def cli(args, timeout=55):
    command = shutil.which('node.exe')
    if not command or not CLI_ENTRY.is_file():
        raise ImageError('runtime_missing', 'Node or the pinned local Playwright CLI runtime is missing.')
    DATA.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.run(
            [command, str(CLI_ENTRY), '--session', SESSION, '--json', *args],
            cwd=DATA, capture_output=True, text=True, encoding='utf-8', errors='replace',
            env={**os.environ, 'NO_COLOR': '1', 'FORCE_COLOR': '0'}, timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except subprocess.TimeoutExpired as exc:
        raise ImageError('browser_timeout', 'Browser operation timed out; check the existing job before resubmitting.') from exc
    if re.search(r"Browser '[^']+' is not open|No browser session", proc.stderr+'\n'+proc.stdout):
        raise ImageError('browser_not_open','The dedicated browser is not open.')
    try:
        envelope = json.loads(proc.stdout)
    except (ValueError, TypeError) as exc:
        raise ImageError('browser_protocol_error', f'Playwright returned invalid JSON (exit {proc.returncode}); raw output withheld.') from exc
    if proc.returncode or envelope.get('isError'):
        # Do not echo traces, page HTML, signed URLs or potentially secret response bodies.
        message = str(envelope.get('error', ''))
        if 'not open' in message or 'No browser' in message:
            code = 'browser_not_open'
        elif 'Timeout' in message:
            code = 'browser_timeout'
        else:
            code = 'browser_operation_failed'
        raise ImageError(code, 'Playwright operation failed; use open to inspect the dedicated browser. Raw output withheld.')
    result = envelope.get('result')
    if isinstance(result, str):
        try:
            return json.loads(result)
        except ValueError:
            return result
    return result


def run_js(source, timeout=55):
    scripts = DATA / 'transient-scripts'
    scripts.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', suffix='.js', dir=scripts,
                                     prefix='web-images-', delete=False) as file:
        file.write(source)
        target = Path(file.name)
    try:
        return cli(['run-code', '--filename', str(target)], timeout)
    finally:
        target.unlink(missing_ok=True)


STATUS_JS = r'''async (page) => {
 const title=await page.title();
 const profile=page.getByTestId('accounts-profile-button');
 return {url:page.url(), logged_in:await profile.count()>0,
   composer:await page.locator('#prompt-textarea').count()>0,
   challenge:/Just a moment|checking your browser/i.test(title),
   browser_session:'chatgpt-web-images', provider:'chatgpt-web', image_model_verified:false};
}'''


def set_visible(visible):
    """Only touch the window with our unique title; never enumerate/kill browser PIDs."""
    if os.name != 'nt':
        return False
    import ctypes
    from ctypes import wintypes
    title = 'ChatGPT Web Images ' + uuid.uuid4().hex
    run_js('async (page) => { await page.evaluate(t => document.title=t, ' + json.dumps(title) + '); return true; }')
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.ShowWindowAsync.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    found = []
    @callback_type
    def visit(hwnd, _):
        buf = ctypes.create_unicode_buffer(1024)
        user32.GetWindowTextW(hwnd, buf, len(buf))
        if buf.value.startswith(title + ' -') or buf.value == title:
            found.append(hwnd)
        return True
    user32.EnumWindows(visit, 0)
    if len(found) != 1:
        return False
    user32.ShowWindowAsync(found[0], 5 if visible else 0)
    time.sleep(0.2)
    return bool(user32.IsWindowVisible(found[0])) == visible


def ensure_browser():
    try:
        state = run_js(STATUS_JS)
    except ImageError as exc:
        if exc.code != 'browser_not_open':
            raise
        cli(['open', 'about:blank', '--headed', '--persistent', '--profile', str(DATA/'profile')])
        auth = DATA/'auth-state.json'
        if not auth.is_file():
            auth = AUTH
        if auth is not None and auth.is_file():
            # Credentials go directly from the existing local file into the browser.
            # Never load, print, copy or overwrite the bridge auth file here.
            cli(['state-load', str(auth)])
        cli(['goto', 'https://chatgpt.com/'])
        run_js('''async page => {
          await page.waitForFunction(() => !!document.querySelector('#prompt-textarea, [data-testid="accounts-profile-button"]') ||
            /Log in|Sign up|登录|注册|Just a moment/i.test((document.body?.innerText||'')+' '+document.title), null, {timeout:20000});
          return true;
        }''')
        state = run_js(STATUS_JS)
    if state.get('challenge'):
        raise ImageError('browser_challenge', 'Complete the verification in the dedicated browser using open.')
    if not state.get('logged_in'):
        raise ImageError('login_required', 'Sign in in the dedicated browser using open.')
    return state


def active_job():
    path = DATA / 'active.json'
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else None


def job_path(job_id):
    if not re.fullmatch(r'\d{8}-[a-f0-9]{12}', job_id):
        raise ImageError('invalid_job_id', 'Invalid job ID.')
    return DATA / 'jobs' / (job_id + '.json')


def public_job(job):
    keys = ['job_id', 'status', 'conversation_url', 'output_dir', 'files', 'error',
            'provider', 'image_model_verified', 'prompt_sha256', 'created_at', 'updated_at',
            'reference_count', 'window_hidden', 'requested_count', 'observed_count', 'count_match', 'submitted_prompt_sha256']
    result={k:job[k] for k in keys if k in job}
    result['requested_count']=job.get('requested_count',1)
    result['downloaded_count']=len(job.get('files',[]))
    result['missing_count']=max(0,result['requested_count']-result['downloaded_count'])
    result['extra_count']=max(0,result['downloaded_count']-result['requested_count'])
    return result


def save_job(job):
    job['updated_at'] = time.time()
    write_json(job_path(job['job_id']), job)


def start(prompt='', prompt_file='', output_dir='', name='image', reference_images=None, count=1, request_id=''):
    if request_id and not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',request_id):
        raise ImageError('invalid_request_id','request_id must contain 1-100 ASCII letters, numbers, underscores or hyphens.')
    if request_id.upper() in {'CON','PRN','AUX','NUL',*(f'COM{i}' for i in range(1,10)),*(f'LPT{i}' for i in range(1,10))}:
        raise ImageError('invalid_request_id','request_id must not be a reserved Windows device name.')
    if type(count) is not int or not 1 <= count <= 20:
        raise ImageError('invalid_count','count must be an integer from 1 to 20 (plugin request limit, not a ChatGPT quota).')
    if bool(prompt) == bool(prompt_file):
        raise ImageError('invalid_prompt', 'Provide exactly one of prompt or prompt_file.')
    if prompt_file:
        source = Path(prompt_file)
        if not source.is_absolute() or not source.is_file():
            raise ImageError('invalid_prompt_file', 'prompt_file must be an existing absolute file path.')
        prompt = source.read_text(encoding='utf-8-sig')
    if not prompt.strip() or len(prompt) > 100000 or re.search(r'data:image/[^;]+;base64,', prompt):
        raise ImageError('invalid_prompt', 'Use nonempty text under 100000 characters; pass images as paths, never base64.')
    if not re.fullmatch(r'[\w\-\u4e00-\u9fff]{1,80}', name) or name.upper() in {'CON','PRN','AUX','NUL'}:
        raise ImageError('invalid_name', 'Use a short name made of letters, numbers, Chinese and hyphens.')
    out = Path(output_dir) if output_dir else DEFAULT_OUTPUT
    if not out.is_absolute():
        raise ImageError('invalid_output_dir', 'output_dir must be absolute.')
    refs = []
    for value in reference_images or []:
        ref = Path(value)
        if not ref.is_absolute() or not ref.is_file():
            raise ImageError('invalid_reference', 'Each reference must be an existing absolute file path.')
        if ref.suffix.lower() not in {'.png','.jpg','.jpeg','.webp'}:
            raise ImageError('invalid_reference', 'References must be PNG, JPEG or WebP files.')
        refs.append(str(ref))
    if len(refs)>5:
        raise ImageError('invalid_reference', 'At most five reference images per job.')
    # Content identity binds a retry key to its exact request, including reference bytes.
    request_hash=hashlib.sha256(json.dumps({'prompt':prompt,'count':count,'output_dir':str(out.resolve()),
        'name':name,'references':[(p,hashlib.sha256(Path(p).read_bytes()).hexdigest()) for p in refs]},
        sort_keys=True,ensure_ascii=False).encode('utf-8')).hexdigest()
    original_hash=hashlib.sha256(prompt.encode('utf-8')).hexdigest()
    if count>1:
        prompt += (f'\n\n[图片交付数量要求]\n本次请求需要 {count} 张独立生成的图片，'
                   f'请在本条请求中完成全部 {count} 张，每张作为独立图片附件交付。'
                   '不要把多张排成一张拼图、联系表或多宫格；不要用裁切一张大图代替独立图片。'
                   '如果上文分项列出了内容，请按顺序逐张生成；如果描述的是同一主题，请生成明显不同的构图方案。'
                   '请直接制作图片，不要只给文字方案。如果无法完成指定数量，请如实说明实际数量。')
    with locked():
        if request_id:
            previous_path=DATA/'requests'/(request_id+'.json')
            if previous_path.exists():
                previous=json.loads(previous_path.read_text(encoding='utf-8'))
                if previous['request_hash']!=request_hash:
                    raise ImageError('request_id_conflict','This request_id was already used with different inputs. Use a new ID for a new request.')
                old=json.loads(job_path(previous['job_id']).read_text(encoding='utf-8'))
                verify_outputs(old)
                return {**public_job(old),'reused_request':True}
        active = active_job()
        if active:
            raise ImageError('job_active', f"Job {active['job_id']} is active; poll it before starting another.")
        ensure_browser()
        out.mkdir(parents=True, exist_ok=True)
        job_id = time.strftime('%Y%m%d') + '-' + uuid.uuid4().hex[:12]
        job = dict(job_id=job_id, status='preparing', provider='chatgpt-web', image_model_verified=False,
                   created_at=time.time(), output_dir=str(out.resolve()), name=name,
                   prompt_sha256=original_hash, requested_count=count, observed_count=0,
                   submitted_prompt_sha256=hashlib.sha256(prompt.encode('utf-8')).hexdigest(),
                   identity_hash=prompt_hash(prompt),
                   reference_count=len(refs), files=[])
        save_job(job)
        write_json(DATA / 'active.json', {'job_id':job_id})
        if request_id:
            write_json(previous_path,{'request_hash':request_hash,'job_id':job_id})
        try:
            cli(['goto','https://chatgpt.com/'])
            run_js(r'''async page => {
              await page.locator('#prompt-textarea').waitFor({timeout:20000});
              await page.getByTestId('composer-plus-btn').click();
              await page.getByText('Create image',{exact:true}).click();
              return true;
            }''')
            if refs:
                run_js('''async page => {
                  const input=page.locator('input[type="file"]').first();
                  await input.setInputFiles(''' + json.dumps(refs) + ''');
                  return {uploaded:''' + str(len(refs)) + '''};
                }''')
            job['status']='submitting'
            save_job(job)
            result=run_js(r'''async page => {
              const prompt=__PROMPT__;
              const composer=page.locator('#prompt-textarea');
              await composer.fill(prompt);
              const button=page.getByTestId('send-button');
              await button.waitFor({state:'visible', timeout:20000});
              await button.click({timeout:20000});
              await page.locator('[data-message-author-role="user"]').last().waitFor({timeout:20000});
              await page.waitForURL(/^https:\/\/chatgpt\.com\/c\/[a-zA-Z0-9-]+$/, {timeout:20000});
              return {url:page.url()};
            }'''.replace('__PROMPT__',json.dumps(prompt)))
            job['status']='generating'
            job['conversation_url']=result['url']
            job['window_hidden']=set_visible(False)
            save_job(job)
        except Exception as exc:
            # Never auto-resend after a timeout: the server may already be generating.
            job['status']='submission_uncertain' if job['status'] in {'submitting','generating'} else 'preparation_failed'
            job['error']={'code':exc.code if isinstance(exc,ImageError) else 'internal_error',
                          'message':str(exc) if isinstance(exc,ImageError) else type(exc).__name__}
            save_job(job)
            if job['status']=='preparation_failed':
                (DATA/'active.json').unlink(missing_ok=True)
        return public_job(job)


DOM_IMAGES_JS = r"""
function ownedImages() {
 const users=Array.from(document.querySelectorAll('[data-message-author-role="user"]'));
 const user=users.at(-1);
 if(!user)return [];
 const seen=new Set(), assets=[];
 const selector='[data-testid^="conversation-turn-"] img[alt^="Generated image"], [data-testid^="conversation-turn-"] img[alt^="生成的图片"]';
 for(const node of document.querySelectorAll(selector)) {
   if(!(user.compareDocumentPosition(node)&Node.DOCUMENT_POSITION_FOLLOWING))continue;
   const source=node.currentSrc||node.src;
   if(!source)continue;
   const url=new URL(source,location.href);
   const id=url.searchParams.get('id')||url.searchParams.get('file_id');
   const key=url.origin+url.pathname+(id?'?id='+id:'');
   if(seen.has(key))continue;
   seen.add(key);
   assets.push({node,key,alt:node.alt,width:node.naturalWidth,height:node.naturalHeight,
     ready:node.complete&&node.naturalWidth>=256&&node.naturalHeight>=256});
 }
 return assets;
}
"""

POLL_JS = r'''async page => {
 // Trigger lazy-loaded generated images without returning image bytes.
 await page.evaluate(()=>{__DOM__ for(const a of ownedImages())if(!a.ready)a.node.scrollIntoView({block:'center'});});
 return await page.evaluate(()=>{
   __DOM__
   const users=Array.from(document.querySelectorAll('[data-message-author-role="user"]'));
   const turns=Array.from(document.querySelectorAll('[data-testid^="conversation-turn-"]'));
   const assets=ownedImages();
   return {url:location.href,stop:!!document.querySelector('[data-testid="stop-button"]'),
     images:assets.filter(a=>a.ready).map(({node,...a})=>a),
     pending_images:assets.filter(a=>!a.ready).length,
     text:(turns.at(-1)?.innerText||'').slice(-1600),user_count:users.length,user_text:users.at(-1)?.innerText||''};
 });
}'''.replace('__DOM__',DOM_IMAGES_JS)

DOWNLOAD_JS = r'''async page => {
 const dialog=page.getByRole('dialog').filter({has:page.getByRole('button',{name:/^Save$|^保存$/})});
 if(await dialog.count()) {
   await dialog.getByRole('button',{name:/^Close$|^关闭$/}).click({timeout:10000});
   await dialog.waitFor({state:'hidden',timeout:10000});
 }
 const key=__KEY__;
 let handle=await page.evaluateHandle(key=>{
   __DOM__
   return ownedImages().find(a=>a.key===key)?.node||null;
 },key);
 let img=handle.asElement();
 if(!img)throw new Error('Requested generated image no longer exists');
 // Multi-image replies use thumbnail buttons. Select the thumbnail first;
 // clicking its nested image can consume the click without opening the editor.
 const thumbnail=await img.evaluateHandle(node=>node.closest('button'));
 if(thumbnail.asElement()) {
   await thumbnail.asElement().press('Enter',{timeout:10000});
   await page.waitForFunction(key=>{
     __DOM__
     const a=ownedImages().find(a=>a.key===key);
     return !!a && !a.node.closest('button') && !!a.node.closest('[role="button"]');
   },key,{timeout:10000});
   handle=await page.evaluateHandle(key=>{__DOM__ return ownedImages().find(a=>a.key===key)?.node||null;},key);
   img=handle.asElement();
   if(!img)throw new Error('Selected generated image no longer exists');
 }
 await img.click({timeout:10000});
 await dialog.waitFor({state:'visible',timeout:15000});
 const [download]=await Promise.all([
   page.waitForEvent('download',{timeout:30000}),
   dialog.getByRole('button',{name:/^Save$|^保存$/}).click({timeout:10000})
 ]);
 if(await download.failure())throw new Error('Image download failed');
 await download.saveAs(__TARGET__);
 return {saved:true};
}'''.replace('__DOM__',DOM_IMAGES_JS)


def image_info(path):
    from PIL import Image
    with Image.open(path) as image:
        image.verify()
    with Image.open(path) as image:
        width,height=image.size
        image_format=image.format
    extension={'PNG':'.png','JPEG':'.jpg','WEBP':'.webp'}.get(image_format)
    if not extension:
        raise ImageError('invalid_image','Downloaded file is not a supported image.')
    return extension,{'width':width,'height':height,'bytes':path.stat().st_size,
                      'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}


def verify_outputs(job):
    for file in job['files']:
        target=Path(file['path'])
        if not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest()!=file['sha256']:
            raise ImageError('output_changed','A downloaded output is missing or changed; it will not be overwritten.')


def download_asset(job, asset):
    source_id=hashlib.sha256(asset['key'].encode('utf-8')).hexdigest()
    if any(f.get('source_id')==source_id for f in job['files']):
        return
    plans=job.setdefault('download_plan',{})
    if source_id not in plans:
        index=len(plans)+1
        stem=Path(job['output_dir'])/f"{job['job_id']}-{job['name']}-{index:02}"
        plans[source_id]={'stem':str(stem),'index':index}
        save_job(job)
    plan=plans[source_id]
    if plan.get('verified') and Path(plan['verified']['path']).exists():
        record=plan['verified']
        if hashlib.sha256(Path(record['path']).read_bytes()).hexdigest()!=record['sha256']:
            raise ImageError('output_changed','An interrupted download target has changed; refusing to overwrite.')
    else:
        stem=Path(plan['stem'])
        # All paths are reserved in this job before any download occurs.
        for extension in ['.png','.jpg','.webp']:
            if stem.with_suffix(extension).exists():
                raise ImageError('output_exists','Refusing to overwrite an existing image.')
        temporary=stem.with_suffix('.download')
        run_js(DOWNLOAD_JS.replace('__KEY__',json.dumps(asset['key'])).replace('__TARGET__',json.dumps(str(temporary))),timeout=55)
        extension,meta=image_info(temporary)
        target=stem.with_suffix(extension)
        record={'path':str(target.resolve()),**meta,'source_id':source_id,'index':plan['index']}
        plan['verified']=record
        save_job(job)  # recovery can recognize the final file after a crash during rename
        if target.exists():
            raise ImageError('output_exists','Refusing to overwrite an existing image.')
        temporary.rename(target)
    job['files'].append(record)
    save_job(job)  # checkpoint every image, not just the final image


def poll(job_id):
    with locked():
        path=job_path(job_id)
        if not path.is_file():
            raise ImageError('job_not_found','Job does not exist.')
        job=json.loads(path.read_text(encoding='utf-8'))
        verify_outputs(job)
        if job['status'] in TERMINAL:
            return public_job(job)
        browser=ensure_browser()
        active=active_job()
        if not active or active['job_id']!=job_id:
            raise ImageError('job_not_active','This unfinished job does not own the browser.')
        url=job.get('conversation_url','')
        if re.fullmatch(r'https://chatgpt.com/c/[a-zA-Z0-9-]+',url) and browser['url']!=url:
            cli(['goto',url])
            run_js('''async page => {
              await page.locator('[data-message-author-role="user"]').last().waitFor({timeout:20000});
              return true;
            }''')
        result=run_js(POLL_JS)
        expected=job.get('identity_hash',job['prompt_sha256'])
        if result['user_count'] and prompt_hash(result['user_text'])!=expected:
            raise ImageError('conversation_changed','The browser contains a different prompt; do not download or resend. Open the recorded conversation and poll again.')
        if re.fullmatch(r'https://chatgpt.com/c/[a-zA-Z0-9-]+',result['url']):
            job['conversation_url']=result['url']
        # Deduplicate repeated DOM representations of the same generated asset.
        assets=list({a['key']:a for a in result['images']}.values())
        job['observed_count']=len(assets)
        pending=result.get('pending_images',0)
        if not result['user_count'] or not assets or result['stop'] or pending:
            if time.time()-job['created_at']>900:
                job['status']='needs_attention'
                job['error']={'code':'generation_wait_exceeded','message':'Generation has not settled after 15 minutes. Inspect the existing conversation; the prompt has not been resent.'}
            if not result['stop'] and result['user_count'] and re.search(
                r"unable to generate|couldn.t generate|can.t generate|image generation limit|too many requests|无法生成|达到.*上限",
                result['text'],re.I) and not assets and not pending:
                job['status']='failed'
                job['error']={'code':'generation_failed','message':result['text'][:800]}
                (DATA/'active.json').unlink(missing_ok=True)
            save_job(job)
            return public_job(job)
        fingerprint=hashlib.sha256(json.dumps(sorted((a['key'],a['width'],a['height']) for a in assets)).encode()).hexdigest()
        if fingerprint!=job.get('image_fingerprint'):
            job['image_fingerprint']=fingerprint
            job['stable_since']=time.time()
            job['status']='image_settling'
            save_job(job)
            return public_job(job)
        requested=job.get('requested_count',1)
        settle_seconds=15 if len(assets)<requested else 4
        if time.time()-job.get('stable_since',time.time())<settle_seconds:
            return public_job(job)
        job['status']='downloading'
        save_job(job)
        # Bound each poll; subsequent polls resume from the per-file checkpoint.
        deadline=time.monotonic()+35
        for asset in assets:
            download_asset(job,asset)
            if time.monotonic()>deadline:
                return public_job(job)
        downloaded=len(job['files'])
        job['count_match']=downloaded==requested
        job['status']='complete' if job['count_match'] else ('partial' if downloaded<requested else 'count_mismatch')
        if job['count_match']:
            job.pop('error',None)
        else:
            job['error']={'code':'image_count_mismatch',
                          'message':f'Requested {requested} independent images; downloaded {downloaded}. All observed images were saved. No automatic resubmission.'}
        save_job(job)
        (DATA/'active.json').unlink(missing_ok=True)
        return public_job(job)


def poll_wait(job_id,wait_seconds=0):
    if type(wait_seconds) is not int or not 0<=wait_seconds<=45:
        raise ImageError('invalid_wait','wait_seconds must be an integer from 0 to 45.')
    deadline=time.monotonic()+wait_seconds
    while True:
        result=poll(job_id)
        if result['status'] in TERMINAL or result['status'] in {'needs_attention','submission_uncertain'}:
            return result
        remaining=deadline-time.monotonic()
        if remaining<=0:
            return result
        # Release the operation lock between checks, allowing status/cancel calls.
        time.sleep(min(5 if result['status']=='image_settling' else 10,remaining))


def compact_result(result,detail=False):
    if detail or 'job_id' not in result:
        return result
    keys=('ok','job_id','status','requested_count','observed_count','downloaded_count','missing_count',
          'extra_count','count_match','error','reused_request')
    slim={key:result[key] for key in keys if key in result}
    if result.get('files'):
        # Full integrity evidence remains on disk and available through detail=true.
        slim['files']=[{k:f[k] for k in ('path','width','height','bytes','sha256') if k in f} for f in result['files']]
    if result.get('status') in TERMINAL:
        for key in ('conversation_url','provider','image_model_verified'):
            if key in result:slim[key]=result[key]
    else:
        slim['next_action']='poll_same_job'
        slim['suggested_wait_seconds']=40
    return slim


def status():
    with locked():
        result=ensure_browser()
        cli(['state-save',str(DATA/'auth-state.json')])
        result['active_job']=active_job()
        result['window_hidden']=set_visible(False)
        return result


def open_browser():
    with locked():
        try:
            result=ensure_browser()
        except ImageError as exc:
            if exc.code not in {'login_required','browser_challenge'}:
                raise
            result={'logged_in':False,'error':exc.code}
        result['visible']=set_visible(True)
        return result


def cancel(job_id):
    with locked():
        job=json.loads(job_path(job_id).read_text(encoding='utf-8'))
        active=active_job()
        if not active or active['job_id']!=job_id:
            raise ImageError('job_not_active','This job does not own the browser.')
        ensure_browser()
        result=run_js(POLL_JS)
        if result['user_count'] and prompt_hash(result['user_text'])!=job.get('identity_hash',job['prompt_sha256']):
            raise ImageError('conversation_changed','A different prompt is open; refusing to stop it.')
        if result['stop']:
            run_js('''async page => {
              const stop=page.getByTestId('stop-button');
              await stop.click({timeout:10000});
              await stop.waitFor({state:'hidden',timeout:15000});
              return {stopped:true};
            }''')
        job['status']='cancelled'
        save_job(job)
        (DATA/'active.json').unlink(missing_ok=True)
        return public_job(job)


def safe_call(fn, **kwargs):
    try:
        value=fn(**kwargs)
        ok=value.get('status') not in {'failed','preparation_failed','submission_uncertain','needs_attention','partial','count_mismatch'}
        return {'ok':ok,**value}
    except ImageError as exc:
        result={'ok':False,'error':{'code':exc.code,'message':str(exc)}}
        # Preserve recovery context after a browser failure without another model call.
        job_id=kwargs.get('job_id')
        if job_id and re.fullmatch(r'\d{8}-[a-f0-9]{12}',job_id):
            path=job_path(job_id)
            if path.is_file():
                try:
                    result={**public_job(json.loads(path.read_text(encoding='utf-8'))),**result}
                except (OSError,ValueError,KeyError):
                    result['recovery_state_unavailable']=True
        return result
    except Exception as exc:
        # Preserve failure, without exposing authentication material through exceptions.
        return {'ok':False,'error':{'code':'internal_error','message':type(exc).__name__}}


def create_mcp():
    from mcp.server.fastmcp import FastMCP
    from mcp.server.fastmcp.exceptions import ToolError
    def call(fn, detail=False, **kwargs):
        result=compact_result(safe_call(fn,**kwargs),detail)
        if not result['ok']:
            raise ToolError(json.dumps(result,ensure_ascii=False,separators=(',',':')))
        # Explicit unstructured tools return one minified JSON text block.
        return json.dumps(result,ensure_ascii=False,separators=(',',':'))
    mcp=FastMCP('chatgpt-web-images',instructions='Own ChatGPT web login; text metadata only. Choose count, submit once with a request_id, then poll with wait_seconds=40. Never resend uncertain jobs. detail=true retrieves full metadata.')
    @mcp.tool(structured_output=False)
    def image_status() -> str:
        """Check the isolated browser login and active image job. No credentials returned."""
        return call(status)
    @mcp.tool(structured_output=False)
    def image_open() -> str:
        """Show only the dedicated image browser for login or inspecting a failed job."""
        return call(open_browser)
    @mcp.tool(structured_output=False)
    def image_generate(prompt: str='', prompt_file: str='', output_dir: str='', name: str='image', reference_images: list[str]|None=None, count: Annotated[int,Field(strict=True,ge=1,le=20)]=1, request_id: str='', detail: bool=False) -> str:
        """One web request for count independent images. Choose count; 1-20 is a local cap. Use paths for references and a unique request_id; identical retries reuse the job. Poll to save originals."""
        return call(start,detail=detail,prompt=prompt,prompt_file=prompt_file,output_dir=output_dir,name=name,reference_images=reference_images,count=count,request_id=request_id)
    @mcp.tool(structured_output=False)
    async def image_poll(job_id: str, wait_seconds: Annotated[int,Field(strict=True,ge=0,le=45)]=40, detail: bool=False) -> str:
        """Wait internally and save all images; resume interrupted downloads. wait_seconds is a polling budget plus any in-flight browser operation. detail=true returns full evidence. Count mismatches are errors."""
        return await asyncio.to_thread(call,poll_wait,detail=detail,job_id=job_id,wait_seconds=wait_seconds)
    @mcp.tool(structured_output=False)
    def image_cancel(job_id: str) -> str:
        """Cancel only this plugin's active job and release its lock. Use only when the user wants to cancel; never cancel automatically to retry."""
        return call(cancel,job_id=job_id)
    return mcp


def serve():
    create_mcp().run(transport='stdio')


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
    parser=argparse.ArgumentParser()
    parser.add_argument('--version',action='version',version=VERSION)
    parser.add_argument('command',choices=['mcp','status','open','generate','poll','cancel','setup','doctor','configure'])
    parser.add_argument('--prompt-file',default='')
    parser.add_argument('--output-dir',default='')
    parser.add_argument('--name',default='image')
    parser.add_argument('--reference',action='append',default=[])
    parser.add_argument('--job-id',default='')
    parser.add_argument('--count',type=int,default=1)
    parser.add_argument('--request-id',default='')
    parser.add_argument('--wait-seconds',type=int,default=0)
    parser.add_argument('--detail',action='store_true')
    parser.add_argument('--data-dir',default='')
    parser.add_argument('--cli-entry',default='')
    parser.add_argument('--auth-file',default='')
    args=parser.parse_args()
    if args.command=='mcp':
        serve()
        return 0
    if args.command=='generate':
        result=safe_call(start,prompt_file=args.prompt_file,output_dir=args.output_dir,name=args.name,reference_images=args.reference,count=args.count,request_id=args.request_id)
    elif args.command in {'poll','cancel'}:
        result=safe_call(poll_wait,job_id=args.job_id,wait_seconds=args.wait_seconds) if args.command=='poll' else safe_call(cancel,job_id=args.job_id)
    elif args.command in {'setup','doctor','configure'}:
        from web_images_setup import operate
        result=safe_call(operate,command=args.command,args=args,runtime=sys.modules[__name__])
    else:
        result=safe_call(status if args.command=='status' else open_browser)
    print(json.dumps(compact_result(result,args.detail),ensure_ascii=False,separators=(',',':')))
    return 0 if result['ok'] else 1


if __name__=='__main__':
    raise SystemExit(main())
