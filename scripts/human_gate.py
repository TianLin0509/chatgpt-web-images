"""Human gates: tell the person at the machine when a login needs their hands.

A Cloudflare check, an expired login or a password prompt cannot be solved by retrying.
Before this module the job parked quietly and the lane waited until someone happened to
read image_status (observed 2026-09-27: the secondary login sat on a Cloudflare check for
two days). A gate is now recorded once per login, announced once with a Windows toast
that does not take focus, and cleared as soon as that login is authenticated again.

The egress address Cloudflare sees is recorded with each gate, beside the last address
that worked, so a check caused by a changed proxy exit is visible as such.
"""
from __future__ import annotations

import base64
import contextlib
import json
import os
from pathlib import Path
import subprocess
import threading
import time
import urllib.request
from xml.sax.saxutils import escape

HUMAN_GATES = {'login_required', 'browser_challenge', 'account_selection_required', 'credential_required'}
LABELS = {'browser_challenge': '卡在 Cloudflare 真人验证', 'login_required': '登录已失效',
          'credential_required': '需要输入密码或验证码', 'account_selection_required': '需要选择登录账号'}
# A gate the person has not handled yet is announced again at most this often.
REALERT_SECONDS = 2 * 3600
# The address that last worked is refreshed at most this often; one tiny request per hour.
OK_EGRESS_SECONDS = 3600
# Cloudflare's own trace endpoint on the ChatGPT host: the address and edge ChatGPT sees.
TRACE_URL = 'https://chatgpt.com/cdn-cgi/trace'
# Windows PowerShell's registered AppUserModelID; toasts need a registered sender.
TOAST_APP = r'{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'


def gate_code(state, error=None):
    """The human gate an account state stands for, or None."""
    if isinstance(error, str):
        with contextlib.suppress(ValueError):
            error = json.loads(error)
    code = (error or {}).get('code') if isinstance(error, dict) else None
    if state in HUMAN_GATES:
        return state
    return code if code in HUMAN_GATES else None


def system_proxies():
    """Chrome follows the Windows proxy setting and ignores HTTP_PROXY; follow it the same way.
    A worker started without HTTP_PROXY otherwise went direct and measured nothing."""
    proxies = {}
    with contextlib.suppress(OSError):
        proxies = getattr(urllib.request, 'getproxies_registry', dict)()  # Windows only
    return proxies if proxies.get('https') else urllib.request.getproxies()


def fetch_egress(timeout=6):
    """Address, edge and country ChatGPT's Cloudflare sees, over the proxy Chrome uses."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler(system_proxies()))
    try:
        with opener.open(TRACE_URL, timeout=timeout) as response:
            text = response.read(4096).decode('ascii', 'replace')
    except Exception:
        return None
    fields = dict(line.split('=', 1) for line in text.splitlines() if '=' in line)
    if not fields.get('ip'):
        return None
    return {'ip': fields['ip'], 'colo': fields.get('colo'), 'loc': fields.get('loc'), 'at': time.time()}


def toast_script(title, body):
    xml = ('<toast><visual><binding template="ToastGeneric"><text>%s</text><text>%s</text>'
           '</binding></visual></toast>') % (escape(title), escape(body))
    return ('[Windows.UI.Notifications.ToastNotificationManager,Windows.UI.Notifications,ContentType=WindowsRuntime]|Out-Null;'
            '[Windows.Data.Xml.Dom.XmlDocument,Windows.Data.Xml.Dom.XmlDocument,ContentType=WindowsRuntime]|Out-Null;'
            '$x=New-Object Windows.Data.Xml.Dom.XmlDocument;$x.LoadXml(\'%s\');'
            '[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier(\'%s\').Show('
            '[Windows.UI.Notifications.ToastNotification]::new($x))') % (xml.replace("'", "''"), TOAST_APP)


def show_toast(title, body):
    """Fire-and-forget toast from a hidden PowerShell; never steals focus or blocks the worker."""
    if os.name != 'nt':
        return False
    encoded = base64.b64encode(toast_script(title, body).encode('utf-16-le')).decode('ascii')
    exe = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32' / 'WindowsPowerShell' / 'v1.0' / 'powershell.exe'
    try:
        subprocess.Popen([str(exe), '-NoProfile', '-NonInteractive', '-WindowStyle', 'Hidden', '-EncodedCommand', encoded],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=subprocess.CREATE_NO_WINDOW, close_fds=True)
    except OSError:
        return False
    return True


def alert_text(gate):
    label = LABELS.get(gate['code'], gate['code'])
    lines = ['账号 %s %s。在 agent 里调用 image_open("%s")，在弹出的浏览器里处理完后 image_account_check。'
             % (gate['account_id'], label, gate['account_id'])]
    egress, ok = gate.get('egress'), gate.get('egress_last_ok')
    if egress:
        where = '%s（%s）' % (egress['ip'], egress.get('colo') or egress.get('loc') or '?')
        if ok and ok.get('ip') and ok['ip'] != egress['ip']:
            lines.append('出口 IP 已从 %s 变为 %s，这常是触发验证的原因。' % (ok['ip'], where))
        else:
            lines.append('出口 IP %s。' % where)
    return 'ChatGPT 生图需要你处理', '\n'.join(lines)


class GateBook:
    """human-gates.json in the pool root, shared by every worker process of the pool."""

    def __init__(self, root):
        self.root = Path(root)
        self.path = self.root / 'human-gates.json'

    @contextlib.contextmanager
    def _locked(self):
        import msvcrt
        with (self.root / 'human-gates.lock').open('a+b') as handle:
            handle.seek(0)
            for _ in range(50):
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(0.1)
            else:
                raise TimeoutError('human-gates.lock')
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

    def read(self):
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            data = {}
        data.setdefault('gates', {})
        return data

    def _write(self, data):
        temp = self.path.with_suffix('.tmp')
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(temp, self.path)

    def enter(self, group, account_id, code, now=None):
        """Record a gate; True when it should be announced (new, or unannounced for REALERT_SECONDS)."""
        now = now or time.time()
        with self._locked():
            data = self.read()
            gate = data['gates'].get(group)
            if gate and gate['code'] == code:
                if now - gate.get('notified_at', 0) < REALERT_SECONDS:
                    return False
                gate['notified_at'] = now
            else:
                data['gates'][group] = {'code': code, 'account_id': account_id, 'since': now, 'notified_at': now}
            self._write(data)
            return True

    def clear(self, group):
        with self._locked():
            data = self.read()
            if data['gates'].pop(group, None) is None:
                return False
            self._write(data)
            return True

    def update(self, group, **fields):
        with self._locked():
            data = self.read()
            if group in data['gates']:
                data['gates'][group].update(fields)
                self._write(data)
                return dict(data['gates'][group])
            return None

    def ok_egress_due(self, now=None):
        ok = self.read().get('egress_last_ok') or {}
        return (now or time.time()) - ok.get('at', 0) >= OK_EGRESS_SECONDS

    def record_ok_egress(self, egress):
        with self._locked():
            data = self.read()
            data['egress_last_ok'] = egress
            self._write(data)

    def summary(self):
        """Pending gates for image_status: who must act, how, and whether the exit changed."""
        data = self.read()
        ok = data.get('egress_last_ok')
        items = []
        for group, gate in sorted(data['gates'].items()):
            item = {'login_group': group, **{k: gate[k] for k in ('code', 'account_id', 'since') if k in gate},
                    'next_action': 'image_open(%s) then image_account_check' % gate.get('account_id')}
            if gate.get('egress'):
                item['egress'] = gate['egress']
                item['egress_changed'] = bool(ok and ok.get('ip') and ok['ip'] != gate['egress']['ip'])
            items.append(item)
        return items, ok


class Alerts:
    """Observes account state writes. Network and toast work runs on a daemon thread."""

    def __init__(self, root, *, notify=None, fetch=fetch_egress, toast=show_toast, background=True):
        self.book = GateBook(root)
        self.background = background
        self.notify = (os.environ.get('CHATGPT_WEB_IMAGES_NOTIFY', '1') != '0') if notify is None else notify
        self.fetch, self.toast = fetch, toast

    def observe(self, account_id, group, state, ready, error=None):
        code = gate_code(state, error)
        if code:
            if self.book.enter(group, account_id, code):
                self._run(self._announce, group)
        elif ready and state == 'authenticated':
            if self.book.clear(group) or self.book.ok_egress_due():
                self._run(self._record_ok, None)

    def _run(self, fn, arg):
        if self.background:
            threading.Thread(target=fn, args=(arg,), daemon=True).start()
        else:
            fn(arg)

    def _announce(self, group):
        egress = self.fetch()
        ok = self.book.read().get('egress_last_ok')
        gate = self.book.update(group, egress=egress, egress_last_ok=ok)
        if gate and self.notify:
            self.toast(*alert_text(gate))

    def _record_ok(self, _):
        egress = self.fetch()
        if egress:
            self.book.record_ok_egress(egress)
