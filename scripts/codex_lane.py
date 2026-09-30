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
from image_pool import Pool, codex_first, fallback_settings, VERSION

TICK_SECONDS = 2
# Consecutive Codex failures that open the breaker, and how long it stays open.
BREAKER_FAILURES, BREAKER_SECONDS = 3, 600
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
    root = job['thread'][len('codex:'):] if job['thread'].startswith('codex:') else ''
    with pool.connect() as db:
        rows = db.execute("SELECT result FROM jobs WHERE status IN ('complete','partial','count_mismatch','cancelled') AND id<>? AND "
                          "(thread=? OR id=? OR result LIKE ?) ORDER BY updated DESC LIMIT 5",
                          (job.get('id', ''), job['thread'], root, '%' + job['thread'] + '%')).fetchall()
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
        self.failures, self.degraded_until = 0, 0.0
        # Image runs reserved per job and the slots they draw in (one Codex process each).
        self.parallel = max(1, int(settings.get('parallel', 3)))
        self.slots = threading.BoundedSemaphore(self.parallel)
        self.reservations = {}

    def outcome(self, ok):
        with self.lock:
            self.failures = 0 if ok else self.failures + 1
            if self.failures >= BREAKER_FAILURES:
                self.degraded_until, self.failures = time.time() + BREAKER_SECONDS, 0
                self.log('breaker_open', until=self.degraded_until)

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

    def reserve(self, job_id, runs):
        with self.lock:
            self.reservations[job_id] = self.reservations.get(job_id, 0) + runs

    def release(self, job_id, runs=1):
        with self.lock:
            left = self.reservations.get(job_id, 0) - runs
            if left > 0:
                self.reservations[job_id] = left
            else:
                self.reservations.pop(job_id, None)

    def reserved(self):
        with self.lock:
            return sum(self.reservations.values())

    def run_job(self, job):
        try:
            self._run(job)
        except Exception as exc:  # the lane survives; the job is re-queued at the next start
            self.log('run_error', job_id=job['id'], error=type(exc).__name__)
        finally:
            with self.lock:
                self.running.pop(job['id'], None)
                self.reservations.pop(job['id'], None)

    def produce(self, job_id, payload, references, base):
        """All images of one job. By default every image is its own Codex run, in parallel
        within the lane's slots (one run draws its images one after another: 2 images took
        261 s, while two single-image runs side by side took 112 s and 122 s). Each finished
        image is saved into the job at once, so a poll shows the count rising."""
        count, name = payload['count'], payload.get('name') or 'image'
        common = dict(prompt=payload['prompt'], output_dir=payload['output_dir'], name=name, job_id=job_id,
                      codex_home=self.settings['codex_home'], references=references,
                      codex_command=self.settings.get('codex_command'), cancelled=self.cancel_watch(job_id))
        work = self.pool.root / 'codex-work' / job_id
        if count == 1 or not self.settings.get('fanout', True):
            try:
                with self.slots:
                    return self.generate(**common, count=count, work_dir=work)
            finally:
                self.release(job_id, 1)
        files, errors, guard, save_lock = [], [], threading.Lock(), threading.Lock()
        started = time.time()
        def one(index):
            try:
                with self.slots:
                    # A run that waited for a slot rechecks the cancel flag itself (the shared
                    # watcher caches it for a few seconds) before starting a Codex process.
                    if self.pool.row(job_id)['cancel_requested']:
                        raise cx.CodexImageError('codex_cancelled', 'The job was cancelled before this image started.')
                    result = self.generate(**common, count=1, work_dir=work / str(index), first_index=index,
                                           variant=(index, count))
                with guard:
                    files.extend(result['files'])
                # Snapshot and write under one lock so the stored count only ever grows.
                with save_lock:
                    with guard:
                        progress = sorted(files, key=lambda f: f['path'])
                    with contextlib.suppress(Exception):  # progress is cosmetic; the image is kept
                        durable(lambda: self.pool.save_result(job_id, {**base, 'status': 'generating', 'files': progress}, 'codex-' + job_id))
            except cx.CodexImageError as exc:
                with guard:
                    errors.append(exc)
            except Exception as exc:
                with guard:
                    errors.append(cx.CodexImageError('codex_error', type(exc).__name__))
            finally:
                self.release(job_id, 1)
        threads = [threading.Thread(target=one, args=(i,), daemon=True) for i in range(1, count + 1)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        if not files:
            cancelled = [e for e in errors if e.code == 'codex_cancelled']
            raise (cancelled or errors or [cx.CodexImageError('codex_no_image', 'Codex returned no image.')])[0]
        files.sort(key=lambda f: f['path'])
        return {'job_id': job_id, 'status': 'complete' if len(files) == count else 'count_mismatch', 'files': files,
                'provider': cx.PROVIDER, 'image_model_verified': False, 'requested_count': count,
                'observed_count': len(files), 'count_match': len(files) == count, 'parallel_runs': count,
                'seconds': round(time.time() - started, 1),
                **({'run_errors': sorted({e.code for e in errors})} if errors else {})}

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
            result = self.produce(job_id, payload, references, base)
        except cx.CodexImageError as exc:
            if exc.code not in ('codex_cancelled', 'reference_changed'):
                self.outcome(False)
            # Codex preferred: a Codex failure gets one web attempt instead of failing the job.
            if (exc.code not in ('codex_cancelled', 'reference_changed') and codex_first(self.pool.root)
                    and durable(lambda: self.pool.release_to_web(job_id, {'code': exc.code}, references))):
                self.log('codex_to_web', job_id=job_id, error_code=exc.code)
                return
            status = 'cancelled' if exc.code == 'codex_cancelled' else 'failed'
            outcome = {**base, 'status': status, 'files': []}
            if status == 'failed':
                outcome['error'] = {'code': exc.code, 'message': str(exc)[:400]}
            durable(lambda: self.pool.save_result(job_id, outcome, 'codex-' + job_id))
            self.log('codex_' + status, job_id=job_id, error_code=exc.code)
            return
        except Exception as exc:
            self.outcome(False)
            if codex_first(self.pool.root) and durable(lambda: self.pool.release_to_web(job_id, {'code': 'codex_error'}, references)):
                self.log('codex_to_web', job_id=job_id, error_code='codex_error')
                return
            durable(lambda: self.pool.save_result(job_id, {**base, 'status': 'failed', 'files': [],
                                                           'error': {'code': 'codex_error', 'message': type(exc).__name__}}, 'codex-' + job_id))
            self.log('codex_failed', job_id=job_id, error_code='codex_error')
            return
        result.update(base)
        self.outcome(True)
        if self.pool.row(job_id)['cancel_requested']:
            result['status'] = 'cancelled'  # finished anyway; the saved files stay listed
        # Images exist now: never downgrade this to a failure because a write was busy.
        durable(lambda: self.pool.save_result(job_id, result, 'codex-' + job_id), attempts=40)
        self.log('codex_finished', job_id=job_id, status=result['status'], files=len(result['files']), seconds=result.get('seconds'))

    def tick(self):
        first = self.settings.get('prefer') == 'codex'
        if first:
            self.pool.expire_handbacks(3 * float(self.settings.get('queue_wait_seconds', 90)))
        if time.time() < self.degraded_until:
            return  # breaker open: the web serves everything until it closes
        # `parallel` counts Codex processes (images being drawn), shared by all jobs. A job is
        # taken only while slots are free, so waiting work stays visible in the queue.
        fanout = self.settings.get('fanout', True)
        while self.reserved() < self.parallel:
            job = self.pool.claim_codex(float(self.settings.get('queue_wait_seconds', 90)), exclude=set(self.running), first=first)
            if not job:
                break
            runs = json.loads(job['payload'])['count'] if fanout else 1
            self.reserve(job['id'], runs)
            thread = threading.Thread(target=self.run_job, args=(job,), daemon=True)
            with self.lock:
                self.running[job['id']] = thread
            thread.start()


def write_health(pool, lane):
    health = pool.root / HEALTH_FILE
    with contextlib.suppress(OSError):
        tmp = health.with_suffix('.tmp')
        tmp.write_text(json.dumps({'version': VERSION, 'pid': os.getpid(), 'beat': time.time(),
                                   'running': sorted(lane.running) if lane else [],
                                   'degraded_until': lane.degraded_until if lane else 0}), encoding='utf-8')
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
