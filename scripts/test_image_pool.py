"""Queue and worker safety contracts, all isolated from real accounts."""
import concurrent.futures
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import web_images as w
from image_pool import Pool, account_env
from image_worker import Worker


class FakeRuntime:
    validate_request = staticmethod(w.validate_request)

    def __init__(self, data):
        self.DATA = Path(data)
        self.DATA.mkdir(parents=True, exist_ok=True)
        self.starts = 0
        self.polls = 0
        self.adopts = 0
        self.released = 0
        self.failed = False
        self.uncertain = False
        self.adopt_fails = False
        self.active = None
        self.active_id = '20260916-123456abcdef'

    def active_job(self):
        return self.active

    def status(self):
        return {'logged_in': True, 'auth_state': 'authenticated'}

    open_browser = status

    def select_account(self, **kwargs):
        return self.status()

    def start(self, **kwargs):
        index = self.DATA / 'requests' / (kwargs['request_id'] + '.json')
        if index.exists():
            return json.loads(index.read_text())
        self.starts += 1
        result = {'job_id': self.active_id, 'status': 'submission_uncertain' if self.uncertain else 'generating', 'files': []}
        w.write_json(index, result)
        self.active = {'job_id': result['job_id']}
        return result

    def poll(self, job_id):
        self.polls += 1
        if self.failed:
            raise w.ImageError('browser_timeout', 'Fixture browser timeout')
        self.active = None
        import hashlib
        output = self.DATA / 'fixture.png'
        output.write_bytes(b'fixture-only')
        return {'job_id': job_id, 'status': 'complete', 'files': [{'path': str(output), 'sha256': hashlib.sha256(output.read_bytes()).hexdigest()}], 'count_match': True}

    def cancel(self, job_id):
        self.active = None
        return {'job_id': job_id, 'status': 'cancelled', 'files': []}

    def release_browser(self):
        self.released += 1
        return {'closed': True}

    def park(self, job_id):
        self.active = None
        return {'job_id': job_id, 'status': 'generating', 'files': []}

    def adopt(self, job_id):
        if self.adopt_fails:
            raise w.ImageError('browser_timeout', 'Fixture adopt failure')
        self.adopts += 1
        self.active = {'job_id': job_id}
        return {'job_id': job_id, 'status': 'generating', 'files': []}


class QueueContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.pool = Pool(self.root / 'pool')
        self.runtimes = {}
        for account in ('primary', 'secondary'):
            data, config = self.root / account / 'browser', self.root / account / 'config'
            w.write_json(config / 'settings.json', {'data_dir': str(data)})
            self.pool.add_account(account, str(data), str(config), enabled=True)
            self.pool.account_state(account, 'authenticated', True)
            self.runtimes[account] = FakeRuntime(data)

    def submit(self, key='request', account='auto', **kwargs):
        return self.pool.submit(prompt='Draw a simple square.', output_dir=str(self.root / 'output'),
                                request_id=key, account_id=account, **kwargs)

    def worker(self, account):
        return Worker(self.pool, account, self.runtimes[account])

    def test_concurrent_submit_same_request_is_one_job(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as threads:
            results = list(threads.map(lambda _: self.submit(), range(20)))
        self.assertEqual(len({r['job_id'] for r in results}), 1)
        self.assertEqual(sum(not r['reused_request'] for r in results), 1)

    def test_two_accounts_claim_different_jobs_and_third_waits(self):
        jobs = [self.submit(str(i)) for i in range(3)]
        with concurrent.futures.ThreadPoolExecutor() as threads:
            claimed = list(threads.map(self.pool.claim, ['primary', 'secondary']))
        self.assertEqual(len({j['id'] for j in claimed}), 2)
        self.assertEqual(self.pool.get(jobs[2]['job_id'])['status'], 'queued')
        self.assertEqual(self.pool.claim('primary')['id'], claimed[0]['id'])

    def test_two_clients_same_account_cannot_double_claim(self):
        self.submit('one')
        self.submit('two')
        with concurrent.futures.ThreadPoolExecutor() as threads:
            claimed = list(threads.map(self.pool.claim, ['primary'] * 8))
        self.assertEqual(len({j['id'] for j in claimed}), 1)

    def test_conflicting_request_never_overwrites(self):
        self.submit()
        with self.assertRaises(w.ImageError) as caught:
            self.submit(count=2)
        self.assertEqual(caught.exception.code, 'request_id_conflict')

    def test_completion_without_client_poll(self):
        job = self.submit()
        worker = self.worker('primary')
        worker.tick()
        worker.tick()
        self.assertEqual(self.pool.get(job['job_id'])['status'], 'complete')
        self.assertEqual(self.runtimes['primary'].starts, 1)

    def test_missing_output_is_reported_instead_of_success(self):
        job = self.submit()
        worker = self.worker('primary')
        worker.tick()
        worker.tick()
        result = self.pool.get(job['job_id'])
        Path(result['files'][0]['path']).unlink()
        with self.assertRaises(w.ImageError) as caught:
            self.pool.get(job['job_id'])
        self.assertEqual(caught.exception.code, 'output_changed')

    def test_uncertain_submission_is_polled_not_resent(self):
        self.runtimes['primary'].uncertain = True
        job = self.submit()
        worker = self.worker('primary')
        worker.tick()
        self.assertEqual(self.pool.get(job['job_id'])['phase'], 'submission_uncertain')
        worker.tick()
        self.assertEqual(self.runtimes['primary'].starts, 1)
        self.assertEqual(self.pool.get(job['job_id'])['status'], 'complete')

    def test_crash_after_submit_recovers_durable_runtime_request(self):
        job = self.submit()
        claimed = self.pool.claim('primary')
        self.runtimes['primary'].start(**json.loads(claimed['payload']))
        worker = self.worker('primary')
        worker.tick()
        worker.tick()
        self.assertEqual(self.runtimes['primary'].starts, 1)
        self.assertEqual(self.pool.get(job['job_id'])['status'], 'complete')

    def test_failed_account_does_not_block_other_account(self):
        a, b = self.submit('a', 'primary'), self.submit('b', 'secondary')
        wa, wb = self.worker('primary'), self.worker('secondary')
        wa.tick()
        self.runtimes['primary'].failed = True
        for _ in range(3):
            wa.tick()
        wb.tick()
        wb.tick()
        self.assertEqual(self.pool.get(a['job_id'])['status'], 'needs_attention')
        self.assertEqual(self.pool.get(b['job_id'])['status'], 'complete')
        self.assertEqual(self.runtimes['secondary'].starts, 1)

    def test_resume_retains_account_and_submission_identity(self):
        job = self.submit('a', 'primary')
        worker = self.worker('primary')
        worker.tick()
        self.runtimes['primary'].failed = True
        for _ in range(3):
            worker.tick()
        before = self.pool.row(job['job_id'])
        self.pool.resume(job['job_id'])
        after = self.pool.row(job['job_id'])
        self.assertEqual((before['account_id'], before['runtime_id']), (after['account_id'], after['runtime_id']))
        self.runtimes['primary'].failed = False
        worker.tick()
        self.assertEqual(self.runtimes['primary'].starts, 1)

    def test_queued_cancellation_never_submits(self):
        job = self.submit()
        self.pool.cancel(job['job_id'])
        self.worker('primary').tick()
        self.assertEqual(self.pool.get(job['job_id'])['status'], 'cancelled')
        self.assertEqual(self.runtimes['primary'].starts, 0)

    def test_active_cancellation_applies_only_to_its_account(self):
        job = self.submit('a', 'primary')
        worker = self.worker('primary')
        worker.tick()
        self.pool.cancel(job['job_id'])
        worker.tick()
        self.assertEqual(self.pool.get(job['job_id'])['status'], 'cancelled')
        self.assertEqual(self.runtimes['secondary'].polls, 0)

    def test_disabled_or_unhealthy_accounts_do_not_claim(self):
        self.submit()
        self.pool.enable('primary', False)
        self.pool.account_state('secondary', 'login_required', False)
        self.assertIsNone(self.pool.claim('primary'))
        self.assertIsNone(self.pool.claim('secondary'))

    def test_legacy_owner_blocks_new_work_without_cancellation(self):
        job = self.submit('a', 'primary')
        self.runtimes['primary'].active = {'job_id': 'legacy'}
        self.worker('primary').tick()
        self.assertEqual(self.pool.get(job['job_id'])['status'], 'queued')
        self.assertEqual(self.pool.account('primary')['state'], 'legacy_job_active')

    def test_reference_changed_while_queued_pauses_before_submission(self):
        ref = self.root / 'ref.png'
        ref.write_bytes(b'original')
        job = self.submit(reference_images=[str(ref)])
        ref.write_bytes(b'changed')
        self.worker('primary').tick()
        self.assertEqual(self.pool.get(job['job_id'])['status'], 'needs_attention')
        self.assertEqual(self.runtimes['primary'].starts, 0)

    def test_duplicate_profile_rejected(self):
        account = self.pool.account('primary')
        with self.assertRaises(w.ImageError) as caught:
            self.pool.add_account('third', account['data_dir'], account['config_dir'])
        self.assertEqual(caught.exception.code, 'duplicate_profile')

    def test_account_env_removes_inherited_auth(self):
        with patch.dict(os.environ, {'CHATGPT_WEB_IMAGES_AUTH': 'wrong-account', 'CHATGPT_WEB_IMAGES_CLI': 'wrong-cli'}):
            env = account_env(self.pool.account('secondary'), self.pool.root)
        self.assertNotIn('CHATGPT_WEB_IMAGES_AUTH', env)
        self.assertNotIn('CHATGPT_WEB_IMAGES_CLI', env)
        self.assertEqual(env['CHATGPT_WEB_IMAGES_CONFIG_DIR'], self.pool.account('secondary')['config_dir'])

    def test_account_action_is_asynchronous_and_no_browser_leaks(self):
        result = self.pool.control('secondary', 'check')
        self.assertEqual(result['status'], 'queued')
        self.worker('secondary').tick()
        status = self.pool.status()['accounts'][1]
        self.assertEqual(status['last_control']['status'], 'complete')
        self.assertNotIn('url', status['last_control']['result'])

    def test_import_legacy_never_submits_again(self):
        runtime = self.runtimes['primary']
        jid = '20260916-000000abcdef'
        w.write_json(runtime.DATA / 'active.json', {'job_id': jid})
        w.write_json(runtime.DATA / 'jobs' / (jid + '.json'), {'job_id': jid, 'status': 'submission_uncertain', 'requested_count': 1, 'files': []})
        result = self.pool.import_legacy('primary')
        self.assertEqual(result['job_id'], jid)
        self.worker('primary').tick()
        self.assertEqual(runtime.starts, 0)
        self.assertEqual(runtime.polls, 1)
        self.assertEqual(self.pool.get(jid)['status'], 'complete')


if __name__ == '__main__':
    unittest.main(verbosity=2)
