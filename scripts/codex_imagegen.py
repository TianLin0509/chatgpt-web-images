"""Codex subscription image generation, used as the fallback lane of the pool.

Codex CLI ships a built-in `image_gen` tool (feature `image_generation`, stable since at
least 0.157.1). It runs on the ChatGPT subscription of the Codex login and needs no
browser, so Cloudflare checks, stuck pages and website incidents do not reach it
(measured 2026-09-30: images in 132-261 s while ChatGPT web reported elevated errors).
It must never reach the paid API: the Codex home has to be a ChatGPT subscription login,
its config may not name another model provider, every API key variable is removed from
the environment, and the run is read-only (the tool itself saves the image).
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time

PROVIDER = 'codex-imagegen'
# One image took 132-196 s, two 261 s; allow generously, then stop the whole process tree.
BASE_SECONDS, PER_IMAGE_SECONDS, MAX_SECONDS = 300, 180, 3600
IMAGE_SUFFIXES = {'.png', '.jpg', '.jpeg', '.webp'}
# A drawing run needs the image tool only. The owner's config.toml brings MCP servers, plugins
# and skills: measured 2026-09-30, one image read 117k input tokens with it and 42k without.
# The login still comes from CODEX_HOME. --ephemeral keeps drawing runs out of the owner's
# Codex session history (the image files are saved all the same).
CLEAN_START = ['--ignore-user-config', '--ignore-rules', '--ephemeral']


class CodexImageError(RuntimeError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def auth_mode(codex_home):
    """'chatgpt' for a subscription login; reads the mode only, never a token."""
    try:
        return json.loads((Path(codex_home) / 'auth.json').read_text(encoding='utf-8')).get('auth_mode')
    except (OSError, ValueError):
        return None


def check_home(codex_home):
    """Refuse anything that could bill the API: a non-subscription login or another provider."""
    mode = auth_mode(codex_home)
    if mode != 'chatgpt':
        raise CodexImageError('codex_not_subscription',
                              f'The Codex home is signed in with {mode or "nothing"}; only a ChatGPT subscription login is allowed.')
    try:
        config = (Path(codex_home) / 'config.toml').read_text(encoding='utf-8')
    except OSError:
        config = ''
    if re.search(r'^\s*\[model_providers', config, re.M) or re.search(r'^\s*model_provider\s*=\s*"(?!openai")', config, re.M):
        raise CodexImageError('codex_config_unsafe', 'The Codex home configures another model provider; the fallback only runs on the subscription.')


def command(configured=None):
    """Codex entry as an argv prefix. The npm .cmd shim is unwrapped to `node <entry.js>` so
    no argument goes through cmd.exe quoting and the process tree can be stopped as a whole."""
    if configured:
        return list(configured) if isinstance(configured, (list, tuple)) else [str(configured)]
    shim = shutil.which('codex.cmd') or shutil.which('codex')
    if not shim:
        raise CodexImageError('codex_missing', 'Codex CLI is not installed or not on PATH.')
    if shim.lower().endswith('.cmd'):
        text = Path(shim).read_text(encoding='utf-8', errors='replace')
        node = shutil.which('node')
        for match in reversed(re.findall(r'"([^"]+\.js)"', text)):
            entry = Path(match.replace('%dp0%', str(Path(shim).parent) + os.sep).replace('%~dp0', str(Path(shim).parent) + os.sep))
            if node and entry.is_file():
                return [node, str(entry)]
    return [shim]


def prompt_for(prompt, count, has_references, variant=None):
    lines = [
        f'Use your built-in image_gen tool to create exactly {count} separate image{"s" if count > 1 else ""}.',
        'Use only the built-in image_gen tool. Do not run scripts or commands, do not use any API or CLI image path, '
        'do not write files yourself, and do not ask questions.',
    ]
    if count > 1:
        lines.append('Make each image a distinct composition that follows the same request.')
    if variant:
        # Parallel runs cannot see each other; name the variant so they do not converge.
        lines.append(f'This is option {variant[0]} of {variant[1]} made in parallel for the same request; '
                     'give it its own distinct composition, layout or viewpoint.')
    if has_references:
        lines.append('The attached images are references for this request.')
    lines += ['', 'Request:', prompt.strip(), '', 'When done, reply with one line per generated image file path.']
    return '\n'.join(lines)


def environment(codex_home, base=None):
    """No API credentials of any vendor, no pool overrides; the subscription home only."""
    def dropped(name):
        upper = name.upper()
        return ('API_KEY' in upper or upper.startswith(('OPENAI_', 'ANTHROPIC_', 'AZURE_OPENAI')) or
                upper.startswith('CHATGPT_WEB_IMAGES_') or upper == 'CODEX_API_KEY')
    env = {k: v for k, v in (base or os.environ).items() if not dropped(k)}
    env['CODEX_HOME'] = str(codex_home)
    return env


def kill_tree(pid):
    """Stop Codex and everything it started (node -> codex.exe -> tools)."""
    if os.name == 'nt':
        subprocess.run(['taskkill', '/F', '/T', '/PID', str(pid)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    else:
        try:
            os.killpg(pid, 9)
        except OSError:
            pass


def default_runner(argv, *, input, env, cwd, timeout, cancelled, log_dir):
    """Run Codex with its output in files (no pipe can hold the caller), watching the clock
    and the job's cancel flag. Returns (returncode, stdout, outcome)."""
    log_dir = Path(log_dir)
    out_path, err_path = log_dir / 'events.jsonl', log_dir / 'stderr.txt'
    with out_path.open('wb') as out, err_path.open('wb') as err:
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=out, stderr=err, env=env, cwd=cwd,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0) | getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0),
                                start_new_session=os.name != 'nt')
        try:
            proc.stdin.write(input.encode('utf-8'))
            proc.stdin.close()
        except OSError:
            pass
        deadline, outcome = time.monotonic() + timeout, 'exited'
        while proc.poll() is None:
            if time.monotonic() > deadline:
                outcome = 'timeout'
            elif cancelled():
                outcome = 'cancelled'
            if outcome != 'exited':
                kill_tree(proc.pid)
                try:
                    proc.wait(15)
                except subprocess.TimeoutExpired:
                    pass
                break
            time.sleep(1)
    return proc.returncode, out_path.read_text(encoding='utf-8', errors='replace'), outcome


def parse_events(text):
    thread, usage, failure = None, None, None
    for line in (text or '').splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get('type') == 'thread.started':
            thread = event.get('thread_id')
        elif event.get('type') == 'turn.completed':
            usage = event.get('usage')
        elif event.get('type') in ('turn.failed', 'error'):
            failure = event.get('error') or event.get('message') or event.get('type')
    return thread, usage, failure


def image_size(path):
    try:
        from PIL import Image
        with Image.open(path) as image:
            return image.size
    except Exception:
        return (None, None)


def generate(*, prompt, count, output_dir, name, job_id, codex_home, work_dir, references=(),
             codex_command=None, timeout=None, cancelled=lambda: False, run=default_runner, first_index=1, variant=None):
    """Generate `count` images and copy them into output_dir. Returns a job-shaped result."""
    codex_home = Path(codex_home)
    check_home(codex_home)
    work_dir = Path(work_dir); work_dir.mkdir(parents=True, exist_ok=True)
    output_dir = Path(output_dir); output_dir.mkdir(parents=True, exist_ok=True)
    argv = command(codex_command) + ['exec', '--json', '--skip-git-repo-check', '-C', str(work_dir),
                                     '--sandbox', 'read-only'] + CLEAN_START + [
                                     '-c', 'approval_policy="never"', '-c', 'model_provider="openai"',
                                     '-c', 'model_reasoning_effort="low"']
    for ref in references:
        argv += ['-i', str(ref)]
    argv.append('-')
    started = time.time()
    limit = timeout or min(MAX_SECONDS, BASE_SECONDS + PER_IMAGE_SECONDS * count)
    returncode, stdout, outcome = run(argv, input=prompt_for(prompt, count, bool(references), variant), env=environment(codex_home),
                                      cwd=str(work_dir), timeout=limit, cancelled=cancelled, log_dir=work_dir)
    thread, usage, failure = parse_events(stdout)
    generated = []
    if thread:
        folder = codex_home / 'generated_images' / thread
        if folder.is_dir():
            generated = sorted((p for p in folder.iterdir() if p.suffix.lower() in IMAGE_SUFFIXES), key=lambda p: p.stat().st_mtime)
    if outcome == 'cancelled':
        raise CodexImageError('codex_cancelled', 'The job was cancelled; Codex was stopped.')
    if not generated:
        if outcome == 'timeout':
            raise CodexImageError('codex_timeout', f'Codex image generation did not finish within {limit} s; it was stopped.')
        detail = failure if failure else f'exit {returncode}'
        raise CodexImageError('codex_no_image', f'Codex returned no generated image ({str(detail)[:300]}).')
    files = []
    for index, source in enumerate(generated[:count], first_index):
        target = output_dir / f'{job_id}-{name}-{index:02d}{source.suffix.lower()}'
        shutil.copy2(source, target)
        data = target.read_bytes()
        width, height = image_size(target)
        files.append({'path': str(target), 'width': width, 'height': height, 'bytes': len(data),
                      'sha256': hashlib.sha256(data).hexdigest(), 'source': 'codex'})
    status = 'complete' if len(files) == count else 'count_mismatch'
    return {'job_id': job_id, 'status': status, 'files': files, 'provider': PROVIDER, 'image_model_verified': False,
            'requested_count': count, 'observed_count': len(generated), 'count_match': len(files) == count,
            'codex_thread': thread, 'codex_usage': usage, 'seconds': round(time.time() - started, 1),
            **({'stopped': outcome} if outcome != 'exited' else {})}
