"""Fallback lane: pool jobs the web lanes cannot serve in time are generated through Codex.

Web lanes stay the first choice. A job moves here when it has waited in the queue longer
than `queue_wait_seconds` with no web lane taking it (three times longer for jobs pinned to
an account or continuing a conversation), or when a web lane failed it before any image was
saved. Several jobs run in parallel; each result is labelled provider=codex-imagegen.
"""
from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import sqlite3
import threading
import time

import codex_imagegen as cx
from image_pool import Pool, fallback_settings, VERSION

TICK_SECONDS = 2
HEALTH_FILE = 'codex-lane-health.json'
CANCEL_CHECK_SECONDS = 5


@contextlib.contextmanager
def single_instance(path):
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


def references_for(pool, job, payload):
    """Explicit references (all of them must still exist), or for a follow-up the images of its
    conversation's last result, so a fallback of "change the colours" sees the picture it means."""
    declared = list(payload.get('reference_images') or [])
    if declared:
        missing = [r for r in declared if not Path(r).is_file()]
        if missing:
            raise cx.CodexImageError('reference_changed', 'A reference image of this job no longer exists; restore it or resubmit.')
        return declared[:5]
    if not job.get('thread'):
        return []
    with pool.connect() as db:
        rows = db.execute("SELECT result FROM jobs WHERE status IN ('complete','partial','count_mismatch') AND "
                          "(thread=? OR result LIKE ?) ORDER BY updated DESC LIMIT 5",
                          (job['thread'], '%' + job['thread'] + '%')).fetchall()
    for row in rows:
        files = [f['path'] for f in json.loads(row['result'] or '{}').get('files', []) if Path(f.get('path', '')).is_file()]
        if files:
            return files[:5]
    return []


def durable(fn, attempts=12):
    """Retry a queue write through a briefly busy database (observed SQLITE_BUSY 2026-09-29)."""
    for attempt in range(attempts):
        try:
            return fn()
        except sqlite3.OperationalError:
            if attempt == attempts - 1:
                raise
            time.sleep(min(5, 0.5 * (attempt + 1)))


class CodexLane:
    def __init__(self, pool, settings, generate=cx.generate):
        self.pool, self.settings, self.generate = pool, settings, generate
        self.running = {}
        self.lock = threading.Lock()

    def log(self, event, **fields):
        print(json.dumps({'event': event, 'at': time.time(), **fields}, ensure_ascii=False), flush=True)

    def cancel_watch(self, job_id):
        last = {'at': 0.0, 'value': False}
        def cancelled():
            if time.monotonic() - last['at'] >= CANCEL_CHECK_SECONDS:
                last['at'] = time.monotonic()
                with contextlib.suppress(Exception):
                    last['value'] = bool(self.pool.row(job_id)['cancel_requested'])
            return last['value']
        return cancelled

    def run_job(self, job):
        try:
            self._run(job)
        except Exception as exc:  # the lane survives; the job is re-queued at the next start
            self.log('run_error', job_id=job['id'], error=type(exc).__name__)
        finally:
            with self.lock:
                self.running.pop(job['id'], None)

    def _run(self, job):
        job_id = job['id']
        payload = json.loads(job['payload'])
        origin = json.loads(job['result'] or '{}').get('fallback_from')
        base = {'provider': cx.PROVIDER, 'fallback_from': origin}
        try:
            references = references_for(self.pool, job, payload)
        except cx.CodexImageError as exc:
            durable(lambda: self.pool.save_result(job_id, {**base, 'status': 'failed', 'files': [],
                                                           'error': {'code': exc.code, 'message': str(exc)}}, 'codex-' + job_id))
            return
        durable(lambda: self.pool.save_result(job_id, {**base, 'status': 'generating', 'files': []}, 'codex-' + job_id))
        self.log('codex_started', job_id=job_id, count=payload['count'], fallback_from=origin)
        try:
            result = self.generate(prompt=payload['prompt'], count=payload['count'], output_dir=payload['output_dir'],
                                   name=payload.get('name') or 'image', job_id=job_id,
                                   codex_home=self.settings['codex_home'], work_dir=self.pool.root / 'codex-work' / job_id,
                                   references=references, codex_command=self.settings.get('codex_command'),
                                   cancelled=self.cancel_watch(job_id))
        except cx.CodexImageError as exc:
            status = 'cancelled' if exc.code == 'codex_cancelled' else 'failed'
            outcome = {**base, 'status': status, 'files': []}
            if status == 'failed':
                outcome['error'] = {'code': exc.code, 'message': str(exc)[:400]}
            durable(lambda: self.pool.save_result(job_id, outcome, 'codex-' + job_id))
            self.log('codex_' + status, job_id=job_id, error_code=exc.code)
            return
        except Exception as exc:
            durable(lambda: self.pool.save_result(job_id, {**base, 'status': 'failed', 'files': [],
                                                           'error': {'code': 'codex_error', 'message': type(exc).__name__}}, 'codex-' + job_id))
            self.log('codex_failed', job_id=job_id, error_code='codex_error')
            return
        result.update(base)
        if self.pool.row(job_id)['cancel_requested']:
            result['status'] = 'cancelled'  # finished anyway; the saved files stay listed
        # Images exist now: never downgrade this to a failure because a write was busy.
        durable(lambda: self.pool.save_result(job_id, result, 'codex-' + job_id), attempts=40)
        self.log('codex_finished', job_id=job_id, status=result['status'], files=len(result['files']), seconds=result.get('seconds'))

    def tick(self):
        with self.lock:
            free = max(0, int(self.settings.get('parallel', 3)) - len(self.running))
        for _ in range(free):
            job = self.pool.claim_codex(float(self.settings.get('queue_wait_seconds', 90)), exclude=set(self.running))
            if not job:
                break
            thread = threading.Thread(target=self.run_job, args=(job,), daemon=True)
            with self.lock:
                self.running[job['id']] = thread
            thread.start()


def write_health(pool, lane):
    health = pool.root / HEALTH_FILE
    with contextlib.suppress(OSError):
        tmp = health.with_suffix('.tmp')
        tmp.write_text(json.dumps({'version': VERSION, 'pid': os.getpid(), 'beat': time.time(),
                                   'running': sorted(lane.running) if lane else []}), encoding='utf-8')
        os.replace(tmp, health)


def main():
    pool = Pool()
    with single_instance(pool.root / 'codex-lane.lock') as acquired:
        if not acquired:
            return
        lane = None
        while not (pool.root / 'stop-codex').exists():
            settings = fallback_settings(pool.root)
            if not settings:
                break
            if lane is None:
                lane = CodexLane(pool, settings)
                # A job this lane owned when it last stopped is generated again (Codex runs
                # are not resumable; nothing is ever sent to the web from here).
                durable(pool.requeue_codex_orphans)
            lane.settings = settings
            try:
                lane.tick()
            except Exception as exc:
                lane.log('tick_error', error=type(exc).__name__)
            write_health(pool, lane)
            time.sleep(TICK_SECONDS)
        while lane and lane.running:
            write_health(pool, lane)
            time.sleep(1)
        # Switched off or stopped: hand back what was waiting for this lane.
        with contextlib.suppress(Exception):
            durable(pool.release_codex_handovers)
        with contextlib.suppress(OSError):
            (pool.root / HEALTH_FILE).unlink()


if __name__ == '__main__':
    main()
