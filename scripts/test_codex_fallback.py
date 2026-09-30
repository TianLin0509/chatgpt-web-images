"""Codex fallback lane contracts: routing, subscription-only execution, results. No real Codex."""
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

import web_images as w
import codex_imagegen as cx
from codex_lane import CodexLane, references_for
from image_pool import CODEX_LANE, Pool, fallback_settings
from image_worker import Worker
from test_image_pool import FakeRuntime


class CodexFallbackContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.pool = Pool(self.root / 'pool')
        self.home = self.root / 'codex-home'
        self.home.mkdir()
        (self.home / 'auth.json').write_text(json.dumps({'auth_mode': 'chatgpt', 'tokens': {'x': 'SECRET'}}), encoding='utf-8')
        self.enable()
        self.alive()
        data, config = self.root / 'primary' / 'browser', self.root / 'primary' / 'config'
        w.write_json(config / 'settings.json', {'data_dir': str(data)})
        self.pool.add_account('primary', str(data), str(config), enabled=True)
        self.pool.account_state('primary', 'authenticated', True)
        self.runtime = FakeRuntime(data)
        self.out = self.root / 'out'
        self.calls = []

    def alive(self):
        w.write_json(self.pool.root / 'codex-lane-health.json', {'beat': time.time(), 'pid': 1})

    def enable(self, **extra):
        w.write_json(self.pool.root / 'codex-fallback.json', {'enabled': True, 'codex_home': str(self.home), 'parallel': 2,
                                                             'queue_wait_seconds': 90, **extra})

    def submit(self, key='r1', **kw):
        return self.pool.submit(prompt='Draw a kite.', output_dir=str(self.out), request_id=key, **kw)['job_id']

    def fake_generate(self, **kw):
        Path(kw['output_dir']).mkdir(parents=True, exist_ok=True)
        files = []
        for i in range(kw['count']):
            target = Path(kw['output_dir']) / f"{kw['job_id']}-{kw['name']}-{i + 1:02d}.png"
            target.write_bytes(b'png-' + str(i).encode())
            files.append({'path': str(target), 'width': 1536, 'height': 1024, 'bytes': target.stat().st_size,
                          'sha256': hashlib.sha256(target.read_bytes()).hexdigest()})
        self.calls.append(kw)
        return {'job_id': kw['job_id'], 'status': 'complete', 'files': files, 'provider': cx.PROVIDER, 'count_match': True}

    def run_lane(self, generate=None):
        lane = CodexLane(self.pool, fallback_settings(self.pool.root), generate=generate or self.fake_generate)
        lane.tick()
        for t in list(lane.running.values()):
            t.join(5)
        return lane

    def age(self, job_id, seconds):
        with self.pool.connect() as db:
            db.execute('UPDATE jobs SET created=created-? WHERE id=?', (seconds, job_id))

    def test_settings_are_off_unless_configured(self):
        self.assertTrue(fallback_settings(self.pool.root))
        self.enable(enabled=False)
        self.assertIsNone(fallback_settings(self.pool.root))
        (self.pool.root / 'codex-fallback.json').unlink()
        self.assertIsNone(fallback_settings(self.pool.root))

    def test_a_fresh_job_waits_for_web_lanes_and_an_old_one_goes_to_codex(self):
        job_id = self.submit()
        self.run_lane()
        self.assertEqual(self.pool.row(job_id)['status'], 'queued', 'web lanes get the first chance')
        self.age(job_id, 120)
        self.run_lane()
        result = self.pool.get(job_id)
        self.assertEqual((result['status'], result['provider'], result['account_id']), ('complete', 'codex-imagegen', CODEX_LANE))
        self.assertEqual(result['fallback_from'], 'queue_wait')
        self.assertEqual(len(result['files']), 1)

    def test_preparation_failure_is_handed_to_codex_instead_of_failing(self):
        job_id = self.submit()
        self.pool.claim('primary')
        Worker(self.pool, 'primary', self.runtime).record_result(job_id, {'status': 'preparation_failed', 'files': [],
                                                                          'error': {'code': 'browser_timeout'}}, 'rt')
        row = self.pool.row(job_id)
        self.assertEqual((row['status'], row['preferred'], row['account_id']), ('queued', CODEX_LANE, None))
        self.assertEqual(self.pool.get(job_id)['fallback_from'], 'browser_timeout')
        self.run_lane()
        self.assertEqual(self.pool.get(job_id)['status'], 'complete')

    def test_without_fallback_a_preparation_failure_stays_a_failure(self):
        (self.pool.root / 'codex-fallback.json').unlink()
        job_id = self.submit()
        self.pool.claim('primary')
        Worker(self.pool, 'primary', self.runtime).record_result(job_id, {'status': 'preparation_failed', 'files': [],
                                                                          'error': {'code': 'browser_timeout'}}, 'rt')
        self.assertEqual(self.pool.row(job_id)['status'], 'preparation_failed')

    def test_no_hand_over_to_a_lane_that_is_not_running(self):
        (self.pool.root / 'codex-lane-health.json').unlink()
        job_id = self.submit()
        self.pool.claim('primary')
        Worker(self.pool, 'primary', self.runtime).record_result(job_id, {'status': 'preparation_failed', 'files': [],
                                                                          'error': {'code': 'browser_timeout'}}, 'rt')
        self.assertEqual(self.pool.row(job_id)['status'], 'preparation_failed')

    def test_no_hand_over_when_the_codex_home_is_not_a_subscription(self):
        (self.home / 'auth.json').write_text(json.dumps({'auth_mode': 'apikey'}), encoding='utf-8')
        self.assertFalse(self.pool.codex_ready())

    def test_a_stuck_web_job_without_files_moves_when_it_would_be_parked(self):
        job_id = self.submit()
        job = self.pool.claim('primary')
        self.assertTrue(Worker(self.pool, 'primary', self.runtime).park(dict(job, runtime_id=None)))
        self.assertEqual(self.pool.row(job_id)['preferred'], CODEX_LANE)

    def test_a_parked_job_the_web_cannot_reopen_goes_to_codex(self):
        job_id = self.submit()
        self.pool.claim('primary')
        with self.pool.connect() as db:
            db.execute("UPDATE jobs SET status='parked',runtime_id='rt',park_count=0,retry_after=0 WHERE id=?", (job_id,))
        def refused(runtime_id):
            raise w.ImageError('browser_challenge', 'Site challenged')
        self.runtime.adopt = refused
        self.assertTrue(Worker(self.pool, 'primary', self.runtime).adopt_parked())
        row = self.pool.row(job_id)
        self.assertEqual((row['status'], row['preferred']), ('queued', CODEX_LANE))
        self.assertEqual(self.pool.get(job_id)['fallback_from'], 'browser_challenge')

    def test_parked_jobs_of_a_lane_waiting_for_a_person_go_to_codex(self):
        job_id = self.submit()
        self.pool.claim('primary')
        with self.pool.connect() as db:
            db.execute("UPDATE jobs SET status='parked',runtime_id='rt',park_count=0,retry_after=0 WHERE id=?", (job_id,))
        self.assertIsNone(self.pool.claim_codex(90), 'a healthy lane re-opens its own parked job')
        self.pool.account_state('primary', 'browser_challenge', False)
        claimed = self.pool.claim_codex(90)
        self.assertEqual(claimed['id'], job_id)
        self.assertEqual((claimed['runtime_id'], json.loads(claimed['result'])['fallback_from']), (None, 'web_unavailable'))

    def test_old_or_imaged_parked_jobs_are_left_alone(self):
        old, imaged = self.submit('old'), self.submit('imaged')
        self.pool.account_state('primary', 'browser_challenge', False)
        with self.pool.connect() as db:
            db.execute("UPDATE jobs SET status='parked',account_id='primary',created=created-90000 WHERE id=?", (old,))
            db.execute("UPDATE jobs SET status='parked',account_id='primary',result=? WHERE id=?", (json.dumps({'files': [{'path': 'x'}]}), imaged))
        self.assertIsNone(self.pool.claim_codex(90))

    def test_a_job_with_saved_files_is_never_moved(self):
        job_id = self.submit()
        self.pool.claim('primary')
        self.pool.save_result(job_id, {'status': 'downloading', 'files': [{'path': 'x'}]})
        self.assertFalse(self.pool.fallback(job_id, {'code': 'web_stuck'}))

    def test_cancelled_jobs_are_not_generated(self):
        job_id = self.submit()
        self.pool.cancel(job_id)
        self.age(job_id, 120)
        self.run_lane()
        self.assertEqual(self.calls, [])

    def test_cancel_stops_a_running_codex_job(self):
        job_id = self.submit()
        self.pool.fallback(job_id)
        def slow(**kw):
            self.pool.cancel(job_id)
            with patch('codex_lane.CANCEL_CHECK_SECONDS', 0):
                if kw['cancelled']():
                    raise cx.CodexImageError('codex_cancelled', 'stopped')
            raise AssertionError('cancel not seen')
        self.run_lane(slow)
        self.assertEqual(self.pool.row(job_id)['status'], 'cancelled')

    def test_codex_failure_is_reported_with_actionable_code(self):
        job_id = self.submit()
        self.pool.fallback(job_id, {'code': 'browser_challenge'})
        def broken(**kw):
            raise cx.CodexImageError('codex_timeout', 'slow')
        self.run_lane(broken)
        result = self.pool.get(job_id)
        self.assertEqual((result['status'], result['error']['code'], result['error']['actionable_by']), ('failed', 'codex_timeout', 'retry'))

    def test_orphans_of_a_stopped_lane_are_generated_again(self):
        job_id = self.submit()
        self.pool.fallback(job_id)
        self.pool.claim_codex(90)
        self.pool.requeue_codex_orphans()
        self.assertEqual(self.pool.row(job_id)['status'], 'queued')

    def test_several_codex_jobs_run_at_once_while_a_browser_lane_keeps_one(self):
        first, second = self.submit('a'), self.submit('b')
        for job_id in (first, second):
            self.pool.fallback(job_id, {'code': 'browser_timeout'})
        a, b = self.pool.claim_codex(90), self.pool.claim_codex(90)
        self.assertEqual({a['id'], b['id']}, {first, second})
        for job_id in (first, second):
            self.pool.save_result(job_id, {'status': 'generating', 'files': []}, 'codex-' + job_id)
        self.assertEqual({self.pool.row(first)['status'], self.pool.row(second)['status']}, {'running'})
        web_a, web_b = self.submit('wa'), self.submit('wb')
        self.pool.claim('primary')
        with self.assertRaises(sqlite3.IntegrityError):
            with self.pool.connect() as db:
                db.execute("UPDATE jobs SET account_id='primary',status='running' WHERE id=?", (web_b,))

    def test_an_old_queue_index_is_migrated(self):
        with self.pool.connect() as db:
            db.execute('DROP INDEX account_active')
            db.execute("CREATE UNIQUE INDEX account_active ON jobs(account_id) WHERE status IN ('dispatching','running','needs_attention')")
        pool = Pool(self.pool.root)
        with pool.connect() as db:
            self.assertIn('codex', db.execute("SELECT sql FROM sqlite_master WHERE name='account_active'").fetchone()['sql'])

    def test_switching_off_returns_waiting_hand_overs_to_their_web_queue(self):
        job_id = self.submit(account_id='primary')
        self.pool.fallback(job_id, {'code': 'browser_timeout'})
        self.enable(enabled=False)
        with patch('image_pool.subprocess.Popen', return_value=type('Proc', (), {'pid': 1})()):
            self.pool.ensure_workers()
        self.assertEqual(self.pool.row(job_id)['preferred'], 'primary')

    def test_pinned_jobs_wait_longer_before_codex_takes_them(self):
        job_id = self.submit(account_id='primary')
        self.age(job_id, 120)
        self.assertIsNone(self.pool.claim_codex(90))
        self.age(job_id, 200)
        self.assertEqual(self.pool.claim_codex(90)['id'], job_id)

    def test_a_missing_reference_fails_instead_of_generating_without_it(self):
        ref = self.root / 'ref.png'
        ref.write_bytes(b'x')
        job_id = self.pool.submit(prompt='Recolour it.', output_dir=str(self.out), request_id='ref', reference_images=[str(ref)])['job_id']
        ref.unlink()
        self.pool.fallback(job_id)
        self.run_lane()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.pool.get(job_id)['error']['code'], 'reference_changed')

    def test_a_follow_up_uses_the_last_images_of_its_conversation(self):
        parent = self.submit('parent')
        self.out.mkdir(exist_ok=True)
        img = self.out / 'parent.png'
        img.write_bytes(b'p')
        with self.pool.connect() as db:
            db.execute("UPDATE jobs SET status='complete',thread='https://chatgpt.com/c/abc',result=? WHERE id=?",
                       (json.dumps({'files': [{'path': str(img)}]}), parent))
        self.assertEqual(references_for(self.pool, {'thread': 'https://chatgpt.com/c/abc'}, {'reference_images': []}), [str(img)])

    def test_status_reports_the_lane(self):
        status = self.pool.status()['codex_fallback']
        self.assertEqual((status['enabled'], status['alive'], status['parallel']), (True, True, 2))


class CodexRunnerContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / 'home'
        self.home.mkdir()

    def login(self, mode):
        (self.home / 'auth.json').write_text(json.dumps({'auth_mode': mode}), encoding='utf-8')

    def call(self, run, count=1):
        return cx.generate(prompt='p', count=count, output_dir=self.root / 'o', name='n', job_id='j', codex_home=self.home,
                           work_dir=self.root / 'w', codex_command=['codex'], run=run)

    def test_api_key_logins_are_refused_before_anything_runs(self):
        self.login('apikey')
        ran = []
        with self.assertRaises(cx.CodexImageError) as caught:
            self.call(lambda *a, **k: ran.append(a))
        self.assertEqual(caught.exception.code, 'codex_not_subscription')
        self.assertEqual(ran, [])

    def test_a_config_naming_another_provider_is_refused(self):
        self.login('chatgpt')
        (self.home / 'config.toml').write_text('model_provider = "azure"\n', encoding='utf-8')
        with self.assertRaises(cx.CodexImageError) as caught:
            cx.check_home(self.home)
        self.assertEqual(caught.exception.code, 'codex_config_unsafe')
        (self.home / 'config.toml').write_text('sandbox_mode = "danger-full-access"\n', encoding='utf-8')
        cx.check_home(self.home)  # the run itself is forced read-only

    def test_environment_never_carries_api_keys_and_pins_the_subscription_home(self):
        env = cx.environment(self.home, {'OPENAI_API_KEY': 'sk-x', 'OPENAI_BASE_URL': 'u', 'PATH': 'p', 'CHATGPT_WEB_IMAGES_POOL': 'x',
                                         'DEEPSEEK_API_KEY': 'd', 'ANTHROPIC_AUTH_TOKEN': 'a'})
        self.assertEqual(env, {'PATH': 'p', 'CODEX_HOME': str(self.home)})

    def test_images_of_the_thread_are_copied_with_hashes_and_sizes(self):
        self.login('chatgpt')
        folder = self.home / 'generated_images' / 'thread-1'
        folder.mkdir(parents=True)
        from PIL import Image
        Image.new('RGB', (64, 32), 'orange').save(folder / 'a.png')
        seen = {}
        def run(argv, **kw):
            seen.update(argv=argv, input=kw['input'], env=kw['env'])
            return 0, '\n'.join(json.dumps(e) for e in ({'type': 'thread.started', 'thread_id': 'thread-1'},
                                                        {'type': 'turn.completed', 'usage': {'input_tokens': 5}})), 'exited'
        result = cx.generate(prompt='橙色风筝', count=1, output_dir=self.root / 'o', name='kite', job_id='20260930-abcdef012345',
                             codex_home=self.home, work_dir=self.root / 'w', codex_command=['codex'], run=run)
        self.assertEqual(result['status'], 'complete')
        self.assertEqual((result['files'][0]['width'], result['files'][0]['height']), (64, 32))
        self.assertTrue(Path(result['files'][0]['path']).name.startswith('20260930-abcdef012345-kite-01'))
        self.assertEqual(seen['argv'][-1], '-', 'the prompt travels on stdin, never through a shell')
        self.assertIn('橙色风筝', seen['input'])
        self.assertNotIn('OPENAI_API_KEY', seen['env'])
        self.assertIn('read-only', seen['argv'])
        self.assertIn('model_provider="openai"', seen['argv'])

    def test_no_image_is_an_error_not_an_empty_success(self):
        self.login('chatgpt')
        with self.assertRaises(cx.CodexImageError) as caught:
            self.call(lambda argv, **kw: (1, json.dumps({'type': 'thread.started', 'thread_id': 't'}), 'exited'))
        self.assertEqual(caught.exception.code, 'codex_no_image')

    def test_a_timeout_keeps_images_already_generated(self):
        self.login('chatgpt')
        folder = self.home / 'generated_images' / 'tt'
        folder.mkdir(parents=True)
        (folder / 'a.png').write_bytes(b'x')
        result = self.call(lambda argv, **kw: (None, json.dumps({'type': 'thread.started', 'thread_id': 'tt'}), 'timeout'), count=2)
        self.assertEqual((result['status'], len(result['files']), result['stopped']), ('count_mismatch', 1, 'timeout'))

    def test_a_cancel_is_reported_as_such(self):
        self.login('chatgpt')
        with self.assertRaises(cx.CodexImageError) as caught:
            self.call(lambda argv, **kw: (None, '', 'cancelled'))
        self.assertEqual(caught.exception.code, 'codex_cancelled')

    def test_the_real_runner_stops_the_whole_tree_on_timeout(self):
        import sys
        started = time.monotonic()
        code, out, outcome = cx.default_runner([sys.executable, '-c', 'import time; time.sleep(60)'], input='x', env=None,
                                               cwd=str(self.root), timeout=2, cancelled=lambda: False, log_dir=self.root)
        self.assertEqual(outcome, 'timeout')
        self.assertLess(time.monotonic() - started, 30)


if __name__ == '__main__':
    unittest.main(verbosity=2)
