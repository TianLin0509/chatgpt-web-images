"""One detached process per account; owns browser operations, not MCP lifetimes."""
from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import sys
import threading
import time

import web_images as w
from image_pool import ADOPT_BACKOFF, PARK_AFTER_SECONDS, PARKED, Pool, TERMINAL, VERSION

# Waiting for generation is minutes long, so tick fast only when something can change.
IDLE_TICK, WATCH_TICK = 3, 10
# A browser failure used to take its account offline until a human checked it.
RECHECK_BACKOFF = (60, 300, 900)
# Each open lane holds a full browser in memory, so an idle one gives it back.
# The profile keeps the login; the next job reopens it without a sign-in.
IDLE_BROWSER_SECONDS = 300
UNHEALTHY = {'browser_unhealthy', 'worker_error'}


@contextlib.contextmanager
def worker_lock(path):
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


def error_value(exc):
    return {'code': exc.code if isinstance(exc, w.ImageError) else 'worker_error',
            'message': str(exc) if isinstance(exc, w.ImageError) else type(exc).__name__}


class Worker:
    def __init__(self, pool, account_id, runtime=w):
        self.pool, self.account_id, self.runtime = pool, account_id, runtime
        self.last_check = 0
        self.control_turn = True
        self.recoveries = 0
        self.delay = IDLE_TICK
        self.idle_since = time.monotonic()
        self.browser_released = False

    def log(self, event, **fields):
        print(json.dumps({'event': event, 'account_id': self.account_id, **fields}), flush=True)

    def recheck_due(self, account):
        """Decide whether to re-check a not-ready account, with backoff when unhealthy."""
        if not account['enabled']:
            return False
        waited = time.monotonic() - self.last_check
        if account['state'] not in UNHEALTHY:
            return waited >= 30
        # A browser hiccup should not take the lane offline until a human notices it,
        # but a genuinely broken profile must not be retried forever either.
        if self.recoveries >= len(RECHECK_BACKOFF):
            return False
        return waited >= RECHECK_BACKOFF[self.recoveries]

    def record_result(self, job_id, result, runtime_id=None):
        previous = self.pool.row(job_id)
        self.pool.save_result(job_id, result, runtime_id)
        phase = result.get('status')
        if phase in {'preparation_failed', 'failed'}:
            self.pool.account_state(self.account_id, 'browser_unhealthy', False, result.get('error'))
        elif phase in {'complete', 'partial', 'count_mismatch', 'cancelled'}:
            self.pool.account_state(self.account_id, 'authenticated', True)
        if json.loads(previous['result'] or '{}').get('status') != phase:
            print(json.dumps({'event': 'job_phase', 'job_id': job_id, 'account_id': self.account_id,
                              'phase': phase, 'operation_stage': result.get('operation_stage'),
                              'error_code': (result.get('error') or {}).get('code')}), flush=True)

    def controls(self):
        with self.pool.transaction() as db:
            row = db.execute("SELECT * FROM controls WHERE account_id=? AND status='queued' ORDER BY created LIMIT 1", (self.account_id,)).fetchone()
            if not row:
                return False
            db.execute("UPDATE controls SET status='running',updated=? WHERE id=?", (time.time(), row['id']))
        try:
            args = json.loads(row['args'])
            if row['action'] == 'select':
                with self.pool.connect() as db:
                    if db.execute("SELECT 1 FROM jobs WHERE account_id=? AND status IN ('dispatching','running','needs_attention')", (self.account_id,)).fetchone():
                        raise w.ImageError('account_busy', 'An existing job owns this account; login identity cannot be switched.')
            fn = {'open': self.runtime.open_browser, 'check': self.runtime.status, 'select': self.runtime.select_account}[row['action']]
            result = fn(**args)
            # Browser URLs and title are unnecessary in the shared status surface.
            safe = {k: result[k] for k in ('logged_in','auth_state','visible','remembered') if k in result}
            safe['ok'] = True
            self.pool.account_state(self.account_id, result.get('auth_state', 'unknown'), result.get('logged_in', False))
            state = 'complete'
        except Exception as exc:
            safe = {'ok': False, 'error': error_value(exc)}
            if safe['error']['code'] != 'account_busy':
                self.pool.account_state(self.account_id, 'needs_attention', False, safe['error'])
            state = 'failed'
        with self.pool.connect() as db:
            db.execute('UPDATE controls SET status=?,result=?,updated=? WHERE id=?', (state, json.dumps(safe), time.time(), row['id']))
        self.last_check = time.monotonic()
        return True

    def release_idle_browser(self):
        """Hand an idle lane's memory back; work reopens the browser from the profile."""
        if self.browser_released or time.monotonic() - self.idle_since < IDLE_BROWSER_SECONDS:
            return
        try:
            self.runtime.release_browser()
            self.log('browser_released')
        except Exception as exc:
            self.log('browser_release_failed', error_code=error_value(exc)['code'])
        # Either way, stop retrying until this lane has work again.
        self.browser_released = True

    def park(self, job):
        """Hand the lane back to the queue while keeping this job's conversation."""
        if job['runtime_id']:
            try:
                self.runtime.park(job['runtime_id'])
            except Exception as exc:
                self.log('park_failed', job_id=job['id'], error_code=error_value(exc)['code'])
                return False
        parked = self.pool.park(job['id'])
        self.log('job_parked', job_id=job['id'], auto_retry_at=parked.get('auto_retry_at'),
                 attempts_left=parked.get('auto_retry_attempts_left'))
        return True

    def adopt_parked(self):
        """Re-observe one parked job that is due; never resubmits its prompt."""
        job = self.pool.due_parked(self.account_id)
        if not job:
            return False
        if not job['runtime_id']:
            # It never reached the browser, so the normal dispatch path applies. start()
            # stays idempotent through the durable request index.
            self.pool.redispatch(job['id'])
            self.log('job_redispatched', job_id=job['id'])
            return True
        try:
            result = self.runtime.adopt(job['runtime_id'])
        except Exception as exc:
            error = error_value(exc)
            self.pool.park(job['id'], error)
            self.log('adopt_failed', job_id=job['id'], error_code=error['code'])
            return True
        self.record_result(job['id'], result)
        self.log('job_adopted', job_id=job['id'])
        return True

    def tick(self):
        self.delay = IDLE_TICK
        if self.control_turn and self.controls():
            self.control_turn = False
            return
        self.control_turn = True
        account = self.pool.account(self.account_id)
        # Disabled accounts are touched only when the user asks to open/check them.
        if not account['ready'] and self.recheck_due(account):
            if account['state'] in UNHEALTHY:
                self.recoveries += 1
                self.log('account_recovery', state=account['state'], attempt=self.recoveries)
            self.pool.control(self.account_id, 'check')
            self.last_check = time.monotonic()
            return
        if account['ready']:
            self.recoveries = 0
        with self.pool.connect() as db:
            active = db.execute("SELECT id FROM jobs WHERE account_id=? AND status IN ('dispatching','running','needs_attention')", (self.account_id,)).fetchone()
        if not active and account['ready'] and account['enabled']:
            # Legacy jobs keep their browser ownership. Observe them, never cancel or adopt implicitly.
            legacy = self.runtime.active_job()
            if legacy:
                self.pool.account_state(self.account_id, 'legacy_job_active', False,
                                        {'code': 'legacy_job_active', 'job_id': legacy['job_id'], 'message': 'Finish the existing legacy job before this lane can accept new jobs.'})
                return
            if self.adopt_parked():
                return
        job = self.pool.claim(self.account_id)
        if not job:
            self.release_idle_browser()
            return
        self.idle_since = time.monotonic()
        self.browser_released = False
        if job['status'] == 'needs_attention' and not job['cancel_requested']:
            # Waiting for a human while holding the lane stalls every queued job behind
            # it. Park instead: same account, same conversation, no prompt resent.
            if time.time() - job['updated'] >= PARK_AFTER_SECONDS:
                self.park(job)
            return
        try:
            if job['cancel_requested']:
                runtime_id = job['runtime_id']
                if not runtime_id:
                    index = self.runtime.DATA / 'requests' / ('pool-' + job['id'] + '.json')
                    runtime_id = json.loads(index.read_text(encoding='utf-8'))['job_id'] if index.exists() else None
                if runtime_id:
                    result = self.runtime.cancel(runtime_id)
                else:
                    result = {'status': 'cancelled', 'files': []}
                self.record_result(job['id'], result, runtime_id)
                return
            if job['status'] == 'dispatching':
                payload = json.loads(job['payload'])
                index = self.runtime.DATA / 'requests' / (payload['request_id'] + '.json')
                if index.exists() and hasattr(self.runtime, 'recover_existing'):
                    # The browser may already have received this request. Do not require
                    # the original reference files just to recover its existing identity.
                    result = self.runtime.recover_existing(payload['request_id'])
                    self.record_result(job['id'], result, result['job_id'])
                    return
                # Revalidate reference bytes at execution time, unless a durable runtime request already exists.
                _, digest = self.runtime.validate_request(**payload)
                import hashlib
                digest = hashlib.sha256((digest + '\n' + job['preferred']).encode()).hexdigest()
                index = self.runtime.DATA / 'requests' / (payload['request_id'] + '.json')
                if digest != job['request_hash'] and not index.exists():
                    raise w.ImageError('reference_changed', 'Reference content changed while queued; restore it or cancel this job explicitly.')
                result = self.runtime.start(**payload)
                self.record_result(job['id'], result, result['job_id'])
            else:
                prior = json.loads(job['result'] or '{}')
                if prior.get('_resume_requested') and hasattr(self.runtime, 'resume_existing'):
                    result = self.runtime.resume_existing(job['runtime_id'])
                    self.pool.save_result(job['id'], result)
                result = self.runtime.poll(job['runtime_id'])
                self.record_result(job['id'], result)
                # Generation runs for minutes; only slow down while nothing can change.
                if result.get('status') == 'generating' and not result.get('observed_count'):
                    self.delay = WATCH_TICK
        except Exception as exc:
            error = error_value(exc)
            self.pool.failure(job['id'], error)
            print(json.dumps({'event': 'job_error', 'job_id': job['id'], 'account_id': self.account_id,
                              'error_code': error['code']}), flush=True)
            if error['code'] in {'login_required','browser_challenge','account_selection_required','credential_required'}:
                self.pool.account_state(self.account_id, error['code'], False, error)


def main():
    account_id = sys.argv[1]
    pool = Pool()
    account = pool.account(account_id)
    if Path(account['data_dir']).resolve() != w.DATA.resolve():
        raise RuntimeError('Worker account environment mismatch')
    with worker_lock(pool.root / ('worker-' + account_id + '.lock')) as acquired:
        if not acquired:
            return
        # A crashed control is not replayed blindly (it may have clicked a remembered account).
        with pool.connect() as db:
            db.execute("UPDATE controls SET status='failed',result=?,updated=? WHERE account_id=? AND status='running'",
                       (json.dumps({'ok': False, 'error': {'code': 'control_interrupted', 'message': 'Inspect the browser and explicitly retry the account action.'}}), time.time(), account_id))
        stop = threading.Event()
        def pulse():
            while not stop.is_set():
                try:
                    pool.heartbeat(account_id)
                except Exception as exc:
                    print(json.dumps({'event': 'heartbeat_error', 'type': type(exc).__name__}), flush=True)
                stop.wait(3)
        thread = threading.Thread(target=pulse, daemon=True)
        thread.start()
        worker = Worker(pool, account_id)
        health = {'version': VERSION, 'pid': os.getpid(), 'tick_started': 0, 'tick_finished': 0}
        health_path = pool.root / ('worker-' + account_id + '-health.json')
        try:
            while not (pool.root / ('stop-' + account_id)).exists():
                try:
                    health['tick_started'] = time.time()
                    w.write_json(health_path, health)
                    worker.tick()
                except Exception as exc:
                    error = error_value(exc)
                    print(json.dumps({'event': 'tick_error', 'error': error}), flush=True)
                    pool.account_state(account_id, 'worker_error', False, error)
                finally:
                    health['tick_finished'] = time.time()
                    w.write_json(health_path, health)
                stop.wait(worker.delay)
        finally:
            stop.set()
            thread.join(timeout=5)
            with pool.connect() as db:
                db.execute('UPDATE accounts SET heartbeat=0,pid=NULL WHERE id=?', (account_id,))


if __name__ == '__main__':
    main()
