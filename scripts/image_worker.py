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
from image_pool import Pool, TERMINAL


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

    def controls(self):
        with self.pool.transaction() as db:
            row = db.execute("SELECT * FROM controls WHERE account_id=? AND status='queued' ORDER BY created LIMIT 1", (self.account_id,)).fetchone()
            if not row:
                return False
            db.execute("UPDATE controls SET status='running',updated=? WHERE id=?", (time.time(), row['id']))
        try:
            args = json.loads(row['args'])
            fn = {'open': self.runtime.open_browser, 'check': self.runtime.status, 'select': self.runtime.select_account}[row['action']]
            result = fn(**args)
            # Browser URLs and title are unnecessary in the shared status surface.
            safe = {k: result[k] for k in ('logged_in','auth_state','visible','remembered') if k in result}
            safe['ok'] = True
            self.pool.account_state(self.account_id, result.get('auth_state', 'unknown'), result.get('logged_in', False))
            state = 'complete'
        except Exception as exc:
            safe = {'ok': False, 'error': error_value(exc)}
            self.pool.account_state(self.account_id, 'needs_attention', False, safe['error'])
            state = 'failed'
        with self.pool.connect() as db:
            db.execute('UPDATE controls SET status=?,result=?,updated=? WHERE id=?', (state, json.dumps(safe), time.time(), row['id']))
        self.last_check = time.monotonic()
        return True

    def tick(self):
        if self.controls():
            return
        account = self.pool.account(self.account_id)
        if not account['ready'] and time.monotonic() - self.last_check >= 30:
            # Disabled accounts are touched only when the user asks to open/check them.
            if account['enabled']:
                self.pool.control(self.account_id, 'check')
            self.last_check = time.monotonic()
            return
        with self.pool.connect() as db:
            active = db.execute("SELECT id FROM jobs WHERE account_id=? AND status IN ('dispatching','running','needs_attention')", (self.account_id,)).fetchone()
        if not active and account['ready'] and account['enabled']:
            # Legacy jobs keep their browser ownership. Observe them, never cancel or adopt implicitly.
            legacy = self.runtime.active_job()
            if legacy:
                self.pool.account_state(self.account_id, 'legacy_job_active', False,
                                        {'code': 'legacy_job_active', 'job_id': legacy['job_id'], 'message': 'Finish the existing legacy job before this lane can accept new jobs.'})
                return
        job = self.pool.claim(self.account_id)
        if not job or (job['status'] == 'needs_attention' and not job['cancel_requested']):
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
                self.pool.save_result(job['id'], result, runtime_id)
                return
            if job['status'] == 'dispatching':
                payload = json.loads(job['payload'])
                # Revalidate reference bytes at execution time, unless a durable runtime request already exists.
                _, digest = self.runtime.validate_request(**payload)
                import hashlib
                digest = hashlib.sha256((digest + '\n' + job['preferred']).encode()).hexdigest()
                index = self.runtime.DATA / 'requests' / (payload['request_id'] + '.json')
                if digest != job['request_hash'] and not index.exists():
                    raise w.ImageError('reference_changed', 'Reference content changed while queued; restore it or cancel this job explicitly.')
                result = self.runtime.start(**payload)
                self.pool.save_result(job['id'], result, result['job_id'])
            else:
                result = self.runtime.poll(job['runtime_id'])
                self.pool.save_result(job['id'], result)
            if result.get('status') in TERMINAL:
                self.pool.account_state(self.account_id, 'authenticated', True)
        except Exception as exc:
            error = error_value(exc)
            self.pool.failure(job['id'], error)
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
        try:
            while not (pool.root / ('stop-' + account_id)).exists():
                try:
                    worker.tick()
                except Exception as exc:
                    error = error_value(exc)
                    print(json.dumps({'event': 'tick_error', 'error': error}), flush=True)
                    pool.account_state(account_id, 'worker_error', False, error)
                stop.wait(3)
        finally:
            stop.set()
            thread.join(timeout=5)
            with pool.connect() as db:
                db.execute('UPDATE accounts SET heartbeat=0,pid=NULL WHERE id=?', (account_id,))


if __name__ == '__main__':
    main()
