"""Human gates that end without anyone calling image_account_check.

A gate (human_gate.py) stops every lane of a login until a check sees it authenticated
again, and lanes never re-check a gated login on their own. Observed 2026-09-29..10-07:
both ChatGPT logins sat behind a Cloudflare gate for eight days although the person had
long since used ChatGPT normally in the AI Hub browser; the web route was silently dead.

Two things now queue that check, never more than one per gate and reason:
  1. fresh evidence from the AI Hub, read from files in its browser root: an account check
     that confirmed this login on chatgpt.com after the gate began (last-check.json), or a
     person's handoff for this login that has ended since (web-risk.json);
  2. a conservative timer: a gate older than GATE_RECHECK_SECONDS is checked once every
     GATE_RECHECK_SECONDS.
Neither visits anything while the Hub has paused this login's ChatGPT (a recent challenge)
or a person holds the browser. Logins whose lanes are all disabled are left alone: the
owner switched them off on purpose. Which Hub identity a lane is follows from its own
browser entry (the "identity" and "root" its .cjs entry passes to the Hub), never a guess.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import time

import human_gate

GATE_RECHECK_SECONDS = 12 * 3600
# The pool is asked for status many times a minute; the files change far less often.
SCAN_EVERY_SECONDS = 30
_ENTRY_FIELD = r'"%s"\s*:\s*"((?:[^"\\]|\\.)+)"'


def _json(path):
    """A JSON object from a file, else None (missing, unreadable or not an object)."""
    try:
        value = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def lane_binding(account):
    """{'root': Path, 'identity': str} of a lane that runs in the AI Hub browser, else None."""
    settings = _json(Path(account['config_dir']) / 'settings.json') or {}
    entry = settings.get('cli_entry') or ''
    if not entry.endswith('.cjs'):
        return None
    try:
        text = Path(entry).read_text(encoding='utf-8')
    except OSError:
        return None
    fields = {}
    for name in ('root', 'identity'):
        match = re.search(_ENTRY_FIELD % name, text)
        if not match:
            return None
        fields[name] = json.loads('"' + match.group(1) + '"')
    return {'root': Path(fields['root']), 'identity': fields['identity']}


def hub_blocked(root, identity, now=None):
    """Why the Hub forbids visiting this login's ChatGPT right now, or None."""
    now_ms = (now or time.time()) * 1000
    risk = _json(Path(root) / 'web-risk.json') or {}
    lease = risk.get('handoff')
    if isinstance(lease, dict) and lease.get('until', 0) > now_ms:
        return 'handoff'
    sites = risk.get('sites') if isinstance(risk.get('sites'), dict) else {}
    entry = sites.get(identity + ':chatgpt')
    if isinstance(entry, dict) and entry.get('until', 0) > now_ms:
        return 'paused'
    return None


def hub_login_confirmed_at(root, identity):
    """When the Hub last confirmed this login live on chatgpt.com (seconds), else 0."""
    try:
        site = _json(Path(root) / 'last-check.json')['identities'][identity]['sites']['chatgpt']
    except (TypeError, KeyError):
        return 0
    if isinstance(site, dict) and site.get('state') == 'signed_in' and site.get('live') and site.get('verified'):
        return float(site.get('checkedAt') or 0) / 1000
    return 0


def active_handoff(root, identity, now=None):
    """The Hub's live handoff lease for this login's ChatGPT, else None."""
    lease = (_json(Path(root) / 'web-risk.json') or {}).get('handoff')
    if (isinstance(lease, dict) and lease.get('until', 0) > (now or time.time()) * 1000
            and lease.get('identity') == identity and lease.get('site') in (None, '', 'chatgpt')):
        return lease
    return None


def revisit(pool, now=None):
    """Queue one account check per gated login that has a reason for one. Returns the
    (login_group, reason) pairs queued. Never raises: a gate scan must not cost a caller."""
    now = now or time.time()
    book = human_gate.GateBook(pool.root)
    gates = book.read()['gates']
    if not gates:
        return []
    with pool.connect() as db:
        accounts = [dict(r) for r in db.execute('SELECT * FROM accounts ORDER BY id')]
    from image_pool import login_group
    queued = []
    for group, gate in sorted(gates.items()):
        lanes = [a for a in accounts if login_group(a) == group and a['enabled']]
        if not lanes:
            continue
        binding = next((b for b in map(lane_binding, lanes) if b), None)
        since = float(gate.get('since') or 0)
        reason = None
        if binding:
            root, identity = binding['root'], binding['identity']
            lease = active_handoff(root, identity, now)
            if lease:
                # Seen while the person works; when it is gone they are done.
                if gate.get('handoff_seen') != lease.get('id'):
                    book.update(group, handoff_seen=lease.get('id'), handoff_seen_at=now)
                continue
            if hub_blocked(root, identity, now):
                continue
            confirmed = hub_login_confirmed_at(root, identity)
            if confirmed > max(since, float(gate.get('evidence_at') or 0)):
                reason, mark = 'hub_login_confirmed', {'evidence_at': confirmed}
            elif gate.get('handoff_seen') and gate.get('handoff_done') != gate['handoff_seen']:
                reason, mark = 'hub_handoff_ended', {'handoff_done': gate['handoff_seen']}
        if not reason and now - since >= GATE_RECHECK_SECONDS and now - float(gate.get('auto_check_at') or 0) >= GATE_RECHECK_SECONDS:
            reason, mark = 'periodic', {}
        if not reason:
            continue
        book.update(group, auto_check_at=now, auto_check_reason=reason, **mark)
        # Every enabled lane of the login keeps its own gated state; each needs its own check
        # to take work again (a check never repeats while one is queued for that lane).
        for lane in lanes:
            pool.control(lane['id'], 'check')
        queued.append((group, reason))
    return queued
