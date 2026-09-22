"""Durable local queue shared by all stdio clients; no listening network port."""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
import asyncio

import web_images as w

TERMINAL = w.TERMINAL | {'cancelled'}
LIVE = ('dispatching', 'running', 'needs_attention')
# A parked job keeps its account, conversation and files but no longer occupies the lane.
PARKED = 'parked'
# How long a paused job may hold its lane before it is parked and the queue moves on.
PARK_AFTER_SECONDS = 120
# Unattended re-observation delays for a parked job; afterwards it waits for a human.
ADOPT_BACKOFF = (60, 300, 900)
# A caller splitting one picture request into many single-image jobs pays the whole
# submit-and-navigate cost per image and spends one account request per image.
# One call may queue a whole deck; a job still settles independently of its batch.
MAX_BATCH = 20
REQUEST_KEYS = ('prompt', 'prompt_file', 'output_dir', 'name', 'reference_images', 'count',
                'request_id', 'continue_from')
BATCH_HINT_WINDOW = 900
BATCH_HINT_SIBLINGS = 3
# Every lane is a full browser profile (~0.6 GB resident), so the cap is about the
# machine, not about ChatGPT. Raising it without free memory only causes swapping.
MAX_LANES = 16
# Who can actually act on a failure. An agent that cannot tell 'rewrite your prompt'
# from 'a human must log in' either gives up too early or retries something hopeless.
ACTIONABLE = {
    'no_image_in_reply': 'agent', 'generation_failed': 'agent', 'image_count_mismatch': 'agent',
    'reference_changed': 'agent', 'request_id_conflict': 'agent', 'invalid_prompt': 'agent',
    'browser_timeout': 'retry', 'browser_operation_failed': 'retry', 'browser_not_open': 'retry',
    'browser_closed': 'retry', 'busy': 'retry', 'browser_protocol_error': 'retry',
    'job_state_unreadable': 'human',
    'generation_wait_exceeded': 'retry', 'browser_locator_ambiguous': 'retry',
    'login_required': 'human', 'browser_challenge': 'human', 'account_selection_required': 'human',
    'credential_required': 'human', 'image_tool_unavailable': 'human', 'output_exists': 'human',
    'output_changed': 'human', 'conversation_changed': 'human', 'legacy_job_active': 'human',
}
# A job in one of these states will not change on its own while a caller waits.
SETTLED = TERMINAL | {'needs_attention', PARKED, 'unknown'}
VERSION = '0.7.2'


@contextlib.contextmanager
def launch_lock(path):
    """Cross-process gate for launch reservations; never waits on another caller."""
    import msvcrt
    with Path(path).open('a+b') as handle:
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def pool_root():
    return Path(os.environ.get('CHATGPT_WEB_IMAGES_POOL') or
                w.SETTINGS.get('pool_dir') or w.DATA.parent / 'ChatGPTWebImagesPool').resolve()


def login_group(account):
    """The ChatGPT login a lane belongs to; several lanes can share one login."""
    return account['login_group'] or account['id']


def identifier(value):
    if not re.fullmatch(r'[a-z][a-z0-9_-]{0,39}', value):
        raise w.ImageError('invalid_account_id', 'Use a short lowercase account alias, such as primary or secondary.')
    return value


class Pool:
    def __init__(self, root=None):
        self.root = Path(root or pool_root()).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = self.root / 'queue.sqlite3'
        with self.connect() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS accounts (
                    id TEXT PRIMARY KEY, data_dir TEXT UNIQUE NOT NULL, config_dir TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 0, ready INTEGER NOT NULL DEFAULT 0,
                    state TEXT NOT NULL DEFAULT 'not_checked', error TEXT,
                    heartbeat REAL NOT NULL DEFAULT 0, pid INTEGER, updated REAL NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, request_id TEXT UNIQUE NOT NULL, request_hash TEXT NOT NULL,
                    payload TEXT NOT NULL, preferred TEXT NOT NULL DEFAULT 'auto', account_id TEXT,
                    status TEXT NOT NULL, runtime_id TEXT, result TEXT, error TEXT,
                    failures INTEGER NOT NULL DEFAULT 0, cancel_requested INTEGER NOT NULL DEFAULT 0,
                    created REAL NOT NULL, updated REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS jobs_schedule ON jobs(status, created);
                CREATE UNIQUE INDEX IF NOT EXISTS account_active ON jobs(account_id)
                    WHERE status IN ('dispatching','running','needs_attention');
                CREATE TABLE IF NOT EXISTS controls (
                    id TEXT PRIMARY KEY, account_id TEXT NOT NULL, action TEXT NOT NULL,
                    args TEXT NOT NULL, status TEXT NOT NULL, result TEXT, created REAL NOT NULL,
                    updated REAL NOT NULL);
            ''')
            # Existing queues are migrated in place; a rebuild would lose live ownership.
            known = {row['name'] for row in db.execute('PRAGMA table_info(jobs)')}
            for column, ddl in (('park_count', 'INTEGER NOT NULL DEFAULT 0'),
                                ('retry_after', 'REAL NOT NULL DEFAULT 0'),
                                ('thread', "TEXT NOT NULL DEFAULT ''")):
                if column not in known:
                    db.execute('ALTER TABLE jobs ADD COLUMN ' + column + ' ' + ddl)
            # A lane is one browser profile. Several lanes can share one ChatGPT login,
            # and login_group is what the website rate-limits, so scheduling balances on it.
            if 'login_group' not in {row['name'] for row in db.execute('PRAGMA table_info(accounts)')}:
                db.execute("ALTER TABLE accounts ADD COLUMN login_group TEXT NOT NULL DEFAULT ''")
            db.execute('CREATE INDEX IF NOT EXISTS jobs_parked ON jobs(account_id, status, retry_after)')

    def connect(self):
        db = sqlite3.connect(self.db, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA busy_timeout=10000')
        return contextlib.closing(db)

    @contextlib.contextmanager
    def transaction(self):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def account(self, account_id):
        with self.connect() as db:
            row = db.execute('SELECT * FROM accounts WHERE id=?', (identifier(account_id),)).fetchone()
        if row is None:
            raise w.ImageError('account_not_found', 'Register the account with account-add before using it.')
        return dict(row)

    def add_account(self, account_id, data_dir, config_dir, enabled=False):
        identifier(account_id)
        data, config = Path(data_dir), Path(config_dir)
        if not data.is_absolute() or not config.is_absolute():
            raise w.ImageError('invalid_directory', 'Account directories must be absolute.')
        settings = json.loads((config / 'settings.json').read_text(encoding='utf-8-sig'))
        if Path(settings.get('data_dir', '')).resolve() != data.resolve():
            raise w.ImageError('account_config_mismatch', 'Account config must point at its own data directory.')
        with self.transaction() as db:
            old = db.execute('SELECT * FROM accounts WHERE id=?', (account_id,)).fetchone()
            if old:
                if old['data_dir'] != str(data.resolve()) or old['config_dir'] != str(config.resolve()):
                    raise w.ImageError('account_exists', 'Do not change directories of an existing account.')
            else:
                for peer in db.execute('SELECT data_dir FROM accounts'):
                    if os.path.normcase(str(data.resolve())) == os.path.normcase(peer['data_dir']):
                        raise w.ImageError('duplicate_profile', 'Two accounts must not share a browser data directory.')
                try:
                    db.execute('INSERT INTO accounts(id,data_dir,config_dir,enabled) VALUES(?,?,?,?)',
                               (account_id, str(data.resolve()), str(config.resolve()), int(enabled)))
                except sqlite3.IntegrityError as exc:
                    raise w.ImageError('duplicate_profile', 'Two accounts must not share a browser data directory.') from exc
        return self.account(account_id)

    def clone_account(self, source_id, account_id, enabled=True):
        """Add another browser lane reusing an account that is already logged in.

        ChatGPT has no one-image-at-a-time rule per account; the single-page browser
        automation does. A clone is an isolated profile seeded with the same saved
        login, which is what opening a second browser window already does by hand.
        """
        source = self.account(source_id)
        identifier(account_id)
        auth = Path(source['data_dir']) / 'auth-state.json'
        if not auth.is_file():
            raise w.ImageError('clone_without_login', 'Check the source account first so its login state is saved.')
        base = self.root / 'accounts' / account_id
        data, config = base / 'browser', base / 'config'
        data.mkdir(parents=True, exist_ok=True)
        settings = json.loads((Path(source['config_dir']) / 'settings.json').read_text(encoding='utf-8-sig'))
        settings['data_dir'] = str(data)
        w.write_json(config / 'settings.json', settings)
        shutil.copy2(auth, data / 'auth-state.json')
        row = self.add_account(account_id, str(data), str(config), enabled=enabled)
        group = login_group(source)
        with self.connect() as db:
            db.execute('UPDATE accounts SET login_group=?,updated=? WHERE id=?', (group, time.time(), account_id))
        return {'ok': True, 'account_id': row['id'], 'login_group': group, 'cloned_from': source_id,
                'data_dir': row['data_dir'], 'enabled': bool(row['enabled']), 'next_action': 'image_account_check'}

    def scale_account(self, account_id, lanes, enabled=True):
        """Give one ChatGPT login the requested number of parallel browser lanes.

        Surplus lanes are disabled, never deleted: their profiles hold a real login and
        possibly an unfinished conversation, so removing them is the user's decision.
        """
        if type(lanes) is not int or not 1 <= lanes <= MAX_LANES:
            raise w.ImageError('invalid_lane_count', 'Choose 1 to %d lanes; each lane is a full browser profile.' % MAX_LANES)
        group = login_group(self.account(account_id))
        with self.connect() as db:
            rows = [dict(r) for r in db.execute('SELECT * FROM accounts ORDER BY id')]
        mine = {r['id'] for r in rows if login_group(r) == group}
        if len(rows) - len(mine) + lanes > MAX_LANES:
            raise w.ImageError('lane_budget_exceeded', 'Total lanes would exceed %d; scale another login down first.' % MAX_LANES)
        wanted = [group] + [group + '-' + str(index) for index in range(2, lanes + 1)]
        added = [self.clone_account(group, lane, enabled=enabled)['account_id'] for lane in wanted if lane not in mine]
        disabled = [lane for lane in sorted(mine) if lane not in wanted]
        for lane in disabled:
            self.enable(lane, False)
        for lane in wanted:
            if lane in mine:
                self.enable(lane, enabled)
        return {'ok': True, 'login_group': group, 'lanes': wanted, 'added': added, 'disabled': disabled,
                'next_action': 'image_account_check_each_new_lane' if added else 'image_status'}

    def reseed_account(self, account_id):
        """Refresh an idle lane's saved login from its group leader after a re-login."""
        lane = self.account(account_id)
        group = login_group(lane)
        if lane['id'] == group:
            raise w.ImageError('reseed_leader', 'The group leader holds the original login; check it instead.')
        with self.connect() as db:
            if db.execute("SELECT 1 FROM jobs WHERE account_id=? AND status IN ('dispatching','running','needs_attention')", (account_id,)).fetchone():
                raise w.ImageError('account_busy', 'A job owns this lane; do not replace its login identity now.')
        auth = Path(self.account(group)['data_dir']) / 'auth-state.json'
        if not auth.is_file():
            raise w.ImageError('clone_without_login', 'Check the group leader first so its login state is saved.')
        shutil.copy2(auth, Path(lane['data_dir']) / 'auth-state.json')
        self.account_state(account_id, 'not_checked', False)
        return {'ok': True, 'account_id': account_id, 'login_group': group, 'next_action': 'image_account_check'}

    def enable(self, account_id, enabled):
        self.account(account_id)
        with self.connect() as db:
            db.execute('UPDATE accounts SET enabled=?,updated=? WHERE id=?', (int(enabled), time.time(), account_id))
        return {'ok': True, 'account_id': account_id, 'enabled': bool(enabled)}

    def continuation(self, job_id):
        """Resolve a follow-up target into its conversation and the login that owns it.

        A conversation belongs to the ChatGPT account, not to one browser profile, so any
        lane of that login may continue it. Verified against the live site on 2026-09-19.
        """
        row = self.row(job_id)
        conversation = json.loads(row['result'] or '{}').get('conversation_url', '')
        if not re.fullmatch(w.CONVERSATION_URL, conversation or ''):
            raise w.ImageError('continue_without_conversation',
                               'That job never recorded a conversation, so there is nothing to continue.')
        if row['status'] not in TERMINAL:
            raise w.ImageError('continue_while_active',
                               'Finish or cancel that job before asking a follow-up in its conversation.')
        group = login_group(self.account(row['account_id'])) if row['account_id'] else 'auto'
        return conversation, group

    def prepare(self, account_id='auto', **kwargs):
        """Resolve one request into everything the queue needs, touching nothing yet."""
        with self.connect() as db:
            if not db.execute('SELECT 1 FROM accounts LIMIT 1').fetchone():
                raise w.ImageError('no_accounts', 'Register and log in to at least one isolated account before submitting.')
        parent = kwargs.pop('continue_from', '')
        thread = ''
        if parent:
            thread, group = self.continuation(parent)
            kwargs['conversation_url'] = thread
            # The follow-up must reach the login that owns the conversation; any of its
            # lanes will do, so this pins the login rather than one browser profile.
            if account_id == 'auto':
                account_id = group
            elif login_group(self.account(account_id)) != group:
                raise w.ImageError('continue_wrong_account',
                                   'That conversation belongs to a different ChatGPT login.')
        if account_id != 'auto':
            self.account(account_id)
        payload, digest = w.validate_request(**kwargs)
        digest = hashlib.sha256((digest + '\n' + account_id).encode()).hexdigest()
        return {'request_id': payload['request_id'] or 'auto-' + uuid.uuid4().hex,
                'payload': payload, 'digest': digest, 'preferred': account_id, 'thread': thread}

    def insert(self, db, plan):
        """Queue one prepared request inside a caller-owned transaction."""
        old = db.execute('SELECT * FROM jobs WHERE request_id=?', (plan['request_id'],)).fetchone()
        if old:
            if old['request_hash'] != plan['digest']:
                raise w.ImageError('request_id_conflict', 'This request_id belongs to different input; do not resend an uncertain job.')
            return old['id'], True, None
        job_id = time.strftime('%Y%m%d') + '-' + uuid.uuid4().hex[:12]
        payload = dict(plan['payload'], request_id='pool-' + job_id)
        now = time.time()
        db.execute('INSERT INTO jobs(id,request_id,request_hash,payload,preferred,status,thread,created,updated) VALUES(?,?,?,?,?,?,?,?,?)',
                   (job_id, plan['request_id'], plan['digest'], json.dumps(payload, ensure_ascii=False),
                    plan['preferred'], 'queued', plan['thread'], now, now))
        return job_id, False, self.batch_hint(db, payload)

    def submit(self, account_id='auto', **kwargs):
        plan = self.prepare(account_id, **kwargs)
        with self.transaction() as db:
            job_id, reused, hint = self.insert(db, plan)
        result = self.get(job_id)
        result['reused_request'] = reused
        if not reused and hint:
            result['hint'] = hint
        return result

    def batch_hint(self, db, payload):
        """Flag single-image jobs a caller could have asked for as one count=N request.

        Only jobs still in flight are counted. A retry of a finished job looks exactly
        like a sibling, and telling an agent to merge a retry would be wrong advice.
        """
        if payload['count'] != 1:
            return None
        rows = db.execute(
            "SELECT id FROM jobs WHERE status IN ('queued','dispatching','running') AND created>?"
            " AND json_extract(payload,'$.count')=1 AND json_extract(payload,'$.output_dir')=?",
            (time.time() - BATCH_HINT_WINDOW, payload['output_dir'])).fetchall()
        if len(rows) < BATCH_HINT_SIBLINGS:
            return None
        return {'code': 'consider_one_multi_image_request', 'in_flight_single_image_jobs': len(rows),
                'window_seconds': BATCH_HINT_WINDOW,
                'message': 'Several single-image jobs are in flight for this output_dir. If they are '
                           'alternates of one prompt, one image_generate with count=N delivers them from a '
                           'single ChatGPT request: one lane instead of N, and one request against the '
                           'account limit instead of N. Keep separate jobs when the prompts really differ.'}

    def submit_many(self, requests, account_id='auto'):
        """Queue a whole batch in one call so every free lane can start at once.

        Everything is validated before anything is inserted: a caller should not have to
        reason about half a batch. Re-sending the same batch is idempotent per request_id.
        """
        if not isinstance(requests, list) or not 1 <= len(requests) <= MAX_BATCH:
            raise w.ImageError('invalid_batch', 'Provide 1 to %d image requests in a list.' % MAX_BATCH)
        seen, plans = set(), []
        for item in requests:
            if not isinstance(item, dict):
                raise w.ImageError('invalid_batch', 'Each batch entry must be an object of image_generate arguments.')
            unknown = set(item) - set(REQUEST_KEYS)
            if unknown:
                raise w.ImageError('invalid_batch', 'Unsupported batch entry keys: ' + ', '.join(sorted(unknown)))
            # prepare() also resolves continue_from, which validate_request cannot accept.
            plan = self.prepare(account_id, **item)
            if not plan['payload']['request_id']:
                raise w.ImageError('invalid_request_id', 'Every batch entry needs its own stable request_id.')
            if plan['request_id'] in seen:
                raise w.ImageError('request_id_conflict', 'Batch entries must use distinct request_ids: ' + plan['request_id'])
            seen.add(plan['request_id'])
            plans.append(plan)
        # One transaction for the whole batch: a conflict on the last entry must not
        # leave the earlier ones queued behind a caller who was told the batch failed.
        results = []
        with self.transaction() as db:
            for plan in plans:
                results.append(self.insert(db, plan))
        jobs = []
        for (job_id, reused, hint) in results:
            job = self.get(job_id)
            job['reused_request'] = reused
            if not reused and hint:
                job['hint'] = hint
            jobs.append(job)
        return {'ok': True, 'submitted': len(jobs), 'reused': sum(bool(j['reused_request']) for j in jobs),
                'job_ids': [j['job_id'] for j in jobs], 'jobs': jobs,
                'next_action': 'image_poll_with_job_ids'}

    def poll_states(self, job_ids, detail=False):
        """Read every job in one pass; an unknown id is reported, it does not fail the batch."""
        states = []
        for job_id in job_ids:
            try:
                states.append(self.get(job_id, detail))
            except w.ImageError as exc:
                states.append({'job_id': job_id, 'ok': False, 'status': 'unknown',
                               'error': {'code': exc.code, 'message': str(exc)}})
            except Exception as exc:
                # A damaged row is this job's problem, not the whole batch's.
                states.append({'job_id': job_id, 'ok': False, 'status': 'unknown',
                               'error': {'code': 'job_state_unreadable', 'message': type(exc).__name__}})
        return states

    def batch_summary(self, states):
        waiting = [s for s in states if s['status'] not in SETTLED]
        attention = [s['job_id'] for s in states if s.get('next_action') == 'inspect_account_then_resume']
        return {'ok': all(s.get('ok', False) for s in states), 'total': len(states),
                'settled': len(states) - len(waiting), 'pending': len(waiting),
                'complete': sum(s['status'] == 'complete' for s in states),
                'downloaded_count': sum(s.get('downloaded_count', 0) for s in states),
                'files': [f['path'] for s in states for f in s.get('files', [])],
                'pending_job_ids': [s['job_id'] for s in waiting], 'needs_attention': attention,
                'jobs': states,
                'next_action': 'inspect_blocking_accounts' if attention else
                               'poll_same_job_ids' if waiting else 'deliver_saved_files'}

    async def poll_many_async(self, job_ids, wait_seconds=0, detail=False, return_when='all'):
        """Wait on a whole batch in one call; polling one job at a time is what serialises callers."""
        if type(wait_seconds) is not int or not 0 <= wait_seconds <= 45:
            raise w.ImageError('invalid_wait', 'wait_seconds must be an integer from 0 to 45.')
        if return_when not in {'all', 'any'}:
            raise w.ImageError('invalid_return_when', "return_when must be 'all' or 'any'.")
        unique = list(dict.fromkeys(job_ids))
        if not 1 <= len(unique) <= MAX_BATCH:
            raise w.ImageError('invalid_batch', 'Poll 1 to %d job ids at a time.' % MAX_BATCH)
        deadline = time.monotonic() + wait_seconds
        while True:
            states = await asyncio.to_thread(self.poll_states, unique, detail)
            settled = sum(s['status'] in SETTLED for s in states)
            enough = settled == len(states) if return_when == 'all' else settled > 0
            remaining = deadline - time.monotonic()
            if enough or remaining <= 0:
                return self.batch_summary(states)
            await asyncio.sleep(min(0.5, remaining))

    def import_legacy(self, account_id):
        account = self.account(account_id)
        data = Path(account['data_dir'])
        pointer = data / 'active.json'
        if not pointer.exists():
            return {'ok': True, 'imported': False}
        legacy_id = json.loads(pointer.read_text(encoding='utf-8'))['job_id']
        if not re.fullmatch(r'\d{8}-[a-f0-9]{12}', legacy_id):
            raise w.ImageError('invalid_job_id', 'Invalid legacy active job identifier.')
        legacy = json.loads((data / 'jobs' / (legacy_id + '.json')).read_text(encoding='utf-8'))
        if legacy['status'] in TERMINAL:
            raise w.ImageError('stale_legacy_pointer', 'The legacy pointer references a finished job; inspect it before migration.')
        # Import only observation ownership. The original prompt is never reconstructed/resubmitted.
        payload = {'count': legacy.get('requested_count', 1)}
        now = time.time()
        with self.transaction() as db:
            old = db.execute('SELECT * FROM jobs WHERE id=?', (legacy_id,)).fetchone()
            if old and (old['account_id'] != account_id or old['runtime_id'] != legacy_id):
                raise w.ImageError('legacy_conflict', 'This job ID already belongs to another queued request.')
            if not old:
                db.execute('INSERT INTO jobs(id,request_id,request_hash,payload,preferred,account_id,status,runtime_id,result,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                           (legacy_id, 'legacy-' + account_id + '-' + legacy_id, 'legacy-observe-only', json.dumps(payload),
                            account_id, account_id, 'running', legacy_id, json.dumps(legacy), legacy.get('created_at', now), now))
        return {**self.get(legacy_id), 'imported': True}

    def row(self, job_id):
        with self.connect() as db:
            row = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        if row is None:
            raise w.ImageError('job_not_found', 'Unknown pool job_id. Legacy jobs use the original account CLI.')
        return dict(row)

    def get(self, job_id, detail=False):
        row = self.row(job_id)
        payload = json.loads(row['payload'])
        result = json.loads(row['result'] or '{}')
        if row['status'] in TERMINAL and result.get('files'):
            w.verify_outputs(result)
        result.update(job_id=row['id'], request_id=row['request_id'], account_id=row['account_id'],
                      status=row['status'], created_at=row['created'], updated_at=row['updated'],
                      requested_count=payload['count'], provider='chatgpt-web', image_model_verified=False)
        result['phase'] = json.loads(row['result'] or '{}').get('status', row['status'])
        result['downloaded_count'] = len(result.get('files', []))
        result['missing_count'] = max(0, payload['count'] - result['downloaded_count'])
        result['cancel_requested'] = bool(row['cancel_requested'])
        if row['thread']:
            result['continues_conversation'] = True
        if row['error']:
            result['error'] = json.loads(row['error'])
            result['error']['actionable_by'] = ACTIONABLE.get(result['error'].get('code'), 'human')
        result['ok'] = row['status'] not in {'failed', 'preparation_failed', 'partial', 'count_mismatch'}
        result['retryable'] = row['status'] in {'queued', 'dispatching', 'running', PARKED}
        attended = row['status'] == 'needs_attention' or (row['status'] == PARKED and row['park_count'] > len(ADOPT_BACKOFF))
        result['next_action'] = ('inspect_account_then_resume' if attended else
                                 'poll_same_job' if row['status'] not in TERMINAL else 'deliver_saved_files')
        if row['status'] == PARKED:
            # Parking is not a failure: the conversation, the account and every saved
            # file are kept, and the lane is released so other jobs are not blocked.
            result['parked_reason'] = 'lane_released_for_queue'
            result['auto_retry_attempts_left'] = max(0, len(ADOPT_BACKOFF) - row['park_count'] + 1)
            result['auto_retry_at'] = row['retry_after'] if not attended else None
        if row['status'] == 'queued':
            with self.connect() as db:
                result['queue_position'] = db.execute("SELECT COUNT(*) FROM jobs WHERE status='queued' AND created<=?", (row['created'],)).fetchone()[0]
            lanes = self.status()['accounts']
            lanes = [a for a in lanes if row['preferred'] in ('auto', a['id'])]
            blocking = [{k: a[k] for k in ('id', 'login_group', 'state', 'worker_alive', 'worker_stalled', 'active_job_id', 'active_job_status')}
                        for a in lanes if not a['enabled'] or not a['ready'] or not a['worker_alive'] or a['worker_stalled'] or a['active_job_id']]
            result['blocking_lane_count'] = len(blocking)
            result['blocking_accounts'] = blocking[:5]
            if lanes and all(not a['enabled'] or not a['ready'] or a['worker_stalled'] or
                             a['active_job_status'] == 'needs_attention' for a in lanes):
                result['next_action'] = 'inspect_blocking_accounts'
        if detail:
            result['runtime_job_id'] = row['runtime_id']
        else:
            result = {k: v for k, v in result.items() if k not in {
                'conversation_url', 'output_dir', 'prompt_sha256', 'submitted_prompt_sha256',
                'reference_count', 'window_hidden', '_resume_requested'}}
            if 'files' in result:
                result['files'] = [{k: v for k, v in f.items() if k in
                    {'path', 'width', 'height', 'bytes', 'sha256'}} for f in result['files']]
        return result

    async def poll_async(self, job_id, wait_seconds=0, detail=False):
        """Sleep in the event loop, not in the shared thread executor."""
        if type(wait_seconds) is not int or not 0 <= wait_seconds <= 45:
            raise w.ImageError('invalid_wait', 'wait_seconds must be an integer from 0 to 45.')
        deadline = time.monotonic() + wait_seconds
        while True:
            result = await asyncio.to_thread(self.get, job_id, detail)
            remaining = deadline - time.monotonic()
            if result['status'] in TERMINAL | {'needs_attention', PARKED} or remaining <= 0:
                return result
            await asyncio.sleep(min(0.5, remaining))

    def poll(self, job_id, wait_seconds=0, detail=False):
        if type(wait_seconds) is not int or not 0 <= wait_seconds <= 45:
            raise w.ImageError('invalid_wait', 'wait_seconds must be an integer from 0 to 45.')
        deadline = time.monotonic() + wait_seconds
        while True:
            result = self.get(job_id, detail)
            if result['status'] in TERMINAL | {'needs_attention', PARKED} or time.monotonic() >= deadline:
                return result
            time.sleep(min(0.5, max(0, deadline-time.monotonic())))

    def cancel(self, job_id):
        with self.transaction() as db:
            row = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
            if row is None:
                raise w.ImageError('job_not_found', 'Unknown job_id.')
            if row['status'] == 'queued':
                db.execute("UPDATE jobs SET status='cancelled',updated=? WHERE id=?", (time.time(), job_id))
            elif row['status'] not in TERMINAL:
                db.execute('UPDATE jobs SET cancel_requested=1,updated=? WHERE id=?', (time.time(), job_id))
        return self.get(job_id)

    def resume(self, job_id):
        with self.transaction() as db:
            row = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
            if row is None or row['status'] not in {'needs_attention', PARKED}:
                raise w.ImageError('not_paused', 'Resume only a needs_attention or parked job after inspecting its account.')
            # Never move an uncertain submission to another account or allocate a new request ID.
            if row['status'] == PARKED:
                # The lane is free; hand the job back to its own worker instead of
                # seizing the lane here, which would evict whatever is running now.
                db.execute('UPDATE jobs SET park_count=0,retry_after=0,failures=0,error=NULL,updated=? WHERE id=?',
                           (time.time(), job_id))
                return self.get(job_id)
            state = 'running' if row['runtime_id'] else 'dispatching'
            result = json.loads(row['result'] or '{}')
            result['_resume_requested'] = True
            db.execute('UPDATE jobs SET status=?,failures=0,error=NULL,result=?,updated=? WHERE id=?',
                       (state, json.dumps(result), time.time(), job_id))
        return self.get(job_id)

    def park(self, job_id, error=None):
        """Give the lane back without abandoning the job's account or conversation."""
        with self.transaction() as db:
            row = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
            if row is None or row['status'] in TERMINAL:
                raise w.ImageError('not_parkable', 'Only an unfinished job can be parked.')
            # Without a runtime job there is no conversation to re-observe, so parking
            # only frees the lane; such a job waits for an explicit human resume.
            count = len(ADOPT_BACKOFF) + 1 if not row['runtime_id'] else row['park_count'] + 1
            delay = ADOPT_BACKOFF[min(row['park_count'], len(ADOPT_BACKOFF) - 1)]
            db.execute('UPDATE jobs SET status=?,park_count=?,retry_after=?,error=COALESCE(?,error),updated=? WHERE id=?',
                       (PARKED, count, time.time() + delay, json.dumps(error) if error else None, time.time(), job_id))
        return self.get(job_id)

    def redispatch(self, job_id):
        """Return a parked job that never reached the browser to its own lane."""
        with self.transaction() as db:
            row = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
            if row is None or row['status'] != PARKED or row['runtime_id']:
                raise w.ImageError('not_redispatchable', 'Only a parked job without a runtime job can be re-dispatched.')
            db.execute("UPDATE jobs SET status='dispatching',failures=0,error=NULL,updated=? WHERE id=?",
                       (time.time(), job_id))
        return self.get(job_id)

    def due_parked(self, account_id):
        """The parked job this account should re-observe now, if any."""
        with self.connect() as db:
            row = db.execute('SELECT * FROM jobs WHERE account_id=? AND status=? AND park_count<=? AND retry_after<=? ORDER BY created,id LIMIT 1',
                             (account_id, PARKED, len(ADOPT_BACKOFF), time.time())).fetchone()
        return dict(row) if row else None

    def claim(self, account_id):
        with self.transaction() as db:
            active = db.execute("SELECT * FROM jobs WHERE account_id=? AND status IN ('dispatching','running','needs_attention')", (account_id,)).fetchone()
            if active:
                return dict(active)
            account = db.execute('SELECT * FROM accounts WHERE id=?', (account_id,)).fetchone()
            if not account or not account['enabled'] or not account['ready']:
                return None
            group = login_group(account)
            # Two follow-ups typing into one conversation would interleave, so a thread
            # admits a single writer at a time regardless of how many lanes are free.
            # A parked follow-up still intends to write there once it is re-observed, so
            # it keeps holding its thread; other threads and lanes are unaffected.
            busy = ("SELECT thread FROM jobs WHERE status IN "
                    "('dispatching','running','needs_attention','" + PARKED + "') AND thread<>''")
            free = "(thread='' OR thread NOT IN (%s))" % busy
            # An explicit pin names either this lane or its login, and always wins.
            queued = db.execute("SELECT * FROM jobs WHERE status='queued' AND preferred IN (?,?) AND " + free +
                                " ORDER BY created,id LIMIT 1", (account_id, group)).fetchone()
            if not queued:
                # Spreading auto work evenly over logins is what keeps one ChatGPT account
                # from absorbing a whole burst, so a busier login lets the other go first.
                if not self.fair_turn(db, group):
                    return None
                queued = db.execute("SELECT * FROM jobs WHERE status='queued' AND preferred='auto' AND " + free +
                                    " ORDER BY created,id LIMIT 1").fetchone()
            if not queued:
                return None
            db.execute("UPDATE jobs SET status='dispatching',account_id=?,updated=? WHERE id=?", (account_id, time.time(), queued['id']))
            return dict(db.execute('SELECT * FROM jobs WHERE id=?', (queued['id'],)).fetchone())

    def fair_turn(self, db, group):
        """True unless another login with a free lane is carrying strictly less work.

        Fails open: a login whose worker is not heartbeating is not counted as free, so a
        dead lane can never stall the queue waiting for its turn.
        """
        load, free = {}, {group}
        alive = time.time() - 20
        for row in db.execute('SELECT * FROM accounts'):
            name = login_group(row)
            busy = db.execute("SELECT 1 FROM jobs WHERE account_id=? AND status IN ('dispatching','running','needs_attention') LIMIT 1",
                              (row['id'],)).fetchone()
            load[name] = load.get(name, 0) + (1 if busy else 0)
            if not busy and row['enabled'] and row['ready'] and row['heartbeat'] > alive:
                free.add(name)
        return load.get(group, 0) <= min(load.get(name, 0) for name in free)

    def account_state(self, account_id, state, ready=False, error=None):
        with self.connect() as db:
            db.execute('UPDATE accounts SET state=?,ready=?,error=?,updated=? WHERE id=?',
                       (state, int(ready), json.dumps(error) if error else None, time.time(), account_id))

    def heartbeat(self, account_id):
        with self.connect() as db:
            db.execute('UPDATE accounts SET heartbeat=?,pid=? WHERE id=?', (time.time(), os.getpid(), account_id))

    def save_result(self, job_id, result, runtime_id=None):
        phase = result.get('status', 'running')
        state = phase if phase in TERMINAL | {'needs_attention'} else 'running'
        with self.connect() as db:
            db.execute('UPDATE jobs SET status=?,runtime_id=COALESCE(?,runtime_id),result=?,error=?,failures=0,updated=? WHERE id=?',
                       (state, runtime_id, json.dumps(result, ensure_ascii=False),
                        json.dumps(result['error']) if result.get('error') else None, time.time(), job_id))

    def failure(self, job_id, error):
        with self.transaction() as db:
            row = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
            failures = row['failures'] + 1
            pause = failures >= 3 or error['code'] in {'conversation_changed', 'job_not_active', 'output_changed', 'reference_changed'}
            db.execute('UPDATE jobs SET failures=?,status=?,error=?,cancel_requested=?,updated=? WHERE id=?',
                       (failures, 'needs_attention' if pause else row['status'], json.dumps(error),
                        0 if pause else row['cancel_requested'], time.time(), job_id))

    def control(self, account_id, action, **args):
        self.account(account_id)
        if action not in {'open', 'check', 'select'}:
            raise w.ImageError('invalid_action', 'Unsupported account action.')
        control_id = uuid.uuid4().hex
        now = time.time()
        encoded = json.dumps(args, sort_keys=True)
        with self.transaction() as db:
            if action == 'select' and db.execute(
                "SELECT 1 FROM jobs WHERE account_id=? AND status IN ('dispatching','running','needs_attention')", (account_id,)).fetchone():
                raise w.ImageError('account_busy', 'An existing job owns this account. Do not switch its login identity.')
            old = db.execute("SELECT id,status FROM controls WHERE account_id=? AND action=? AND args=? AND status IN ('queued','running') ORDER BY created LIMIT 1",
                             (account_id, action, encoded)).fetchone()
            if old:
                return {'ok': True, 'account_id': account_id, 'control_id': old['id'],
                        'status': old['status'], 'reused_control': True,
                        'next_action': 'image_status_for_control_result'}
            db.execute('INSERT INTO controls(id,account_id,action,args,status,created,updated) VALUES(?,?,?,?,?,?,?)',
                       (control_id, account_id, action, encoded, 'queued', now, now))
        return {'ok': True, 'account_id': account_id, 'control_id': control_id, 'status': 'queued',
                'next_action': 'image_status_for_control_result'}

    def status(self):
        with self.connect() as db:
            accounts = []
            for row in db.execute('SELECT * FROM accounts ORDER BY id'):
                account = {k: row[k] for k in ('id','enabled','ready','state','heartbeat','updated')}
                account['login_group'] = login_group(row)
                account['worker_alive'] = time.time() - row['heartbeat'] < 20
                health = self.worker_health(row['id'])
                account['worker_version'] = health.get('version', 'unknown')
                account['worker_stalled'] = bool(account['worker_alive'] and health.get('pid') == row['pid'] and
                    health.get('tick_started', 0) > health.get('tick_finished', 0) and
                    time.time() - health['tick_started'] > 300)
                if row['error']:
                    account['error'] = json.loads(row['error'])
                active = db.execute("SELECT id,status FROM jobs WHERE account_id=? AND status IN ('dispatching','running','needs_attention')", (row['id'],)).fetchone()
                account['active_job_id'] = active['id'] if active else None
                account['active_job_status'] = active['status'] if active else None
                account['parked_jobs'] = db.execute('SELECT COUNT(*) FROM jobs WHERE account_id=? AND status=?',
                                                    (row['id'], PARKED)).fetchone()[0]
                control = db.execute('SELECT id,action,status,result,updated FROM controls WHERE account_id=? ORDER BY created DESC LIMIT 1', (row['id'],)).fetchone()
                if control:
                    account['last_control'] = dict(control)
                    account['last_control']['result'] = json.loads(control['result'] or '{}')
                accounts.append(account)
            counts = {r['status']: r['n'] for r in db.execute('SELECT status,COUNT(*) n FROM jobs GROUP BY status')}
        groups = {}
        for account in accounts:
            slot = groups.setdefault(account['login_group'], {'login_group': account['login_group'],
                                     'lanes': 0, 'enabled_lanes': 0, 'usable_lanes': 0, 'busy_lanes': 0, 'parked_jobs': 0})
            slot['lanes'] += 1
            slot['enabled_lanes'] += bool(account['enabled'])
            slot['usable_lanes'] += bool(account['enabled'] and account['ready'] and account['worker_alive'] and not account['worker_stalled'])
            slot['busy_lanes'] += bool(account['active_job_id'])
            slot['parked_jobs'] += account['parked_jobs']
        return {'ok': True, 'service': 'local-durable-pool', 'version': VERSION, 'accounts': accounts, 'jobs': counts,
                'logins': sorted(groups.values(), key=lambda g: g['login_group']),
                'configured_capacity': sum(bool(a['enabled']) for a in accounts),
                'parallel_capacity': sum(bool(a['enabled'] and a['ready'] and a['worker_alive'] and not a['worker_stalled'] and a['active_job_status'] != 'needs_attention') for a in accounts)}

    def worker_health(self, account_id):
        try:
            return json.loads((self.root / ('worker-' + account_id + '-health.json')).read_text(encoding='utf-8'))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            return {'error': 'worker_health_unreadable'}

    def ensure_workers(self):
        failures = []
        with self.connect() as db:
            accounts = [dict(r) for r in db.execute('SELECT * FROM accounts')]
        for account in accounts:
            if (self.root / ('stop-' + account['id'])).exists() or time.time() - account['heartbeat'] < 20:
                continue
            env = account_env(account, self.root)
            try:
                with launch_lock(self.root / ('launch-' + account['id'] + '.lock')) as acquired:
                    if not acquired:
                        continue
                    reservation = self.root / ('launch-' + account['id'] + '.json')
                    if reservation.exists() and time.time() - reservation.stat().st_mtime < 20:
                        continue
                    # Re-read after acquiring the gate; another client may have just started it.
                    if time.time() - self.account(account['id'])['heartbeat'] < 20:
                        continue
                    with (self.root / ('worker-' + account['id'] + '.log')).open('ab') as log:
                        child = subprocess.Popen([sys.executable, str(Path(__file__).with_name('image_worker.py')), account['id']],
                                     env=env, cwd=self.root, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                     creationflags=(subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS) if os.name == 'nt' else 0,
                                     close_fds=True)
                    w.write_json(reservation, {'pid': child.pid, 'started': time.time(), 'version': VERSION})
            except OSError as exc:
                failures.append({'account_id': account['id'], 'code': 'worker_start_failed', 'type': type(exc).__name__})
        return failures


def account_env(account, root):
    # Never inherit another lane's auth file, remembered account, data or output overrides.
    env = {k: v for k, v in os.environ.items() if not k.startswith('CHATGPT_WEB_IMAGES_')}
    env.update(CHATGPT_WEB_IMAGES_CONFIG_DIR=account['config_dir'], CHATGPT_WEB_IMAGES_DATA=account['data_dir'],
               CHATGPT_WEB_IMAGES_POOL=str(root), CHATGPT_WEB_IMAGES_WORKER='1', PYTHONIOENCODING='utf-8')
    return env
