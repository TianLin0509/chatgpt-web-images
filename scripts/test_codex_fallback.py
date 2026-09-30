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
        import threading
        self.calls_lock = threading.Lock()

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
            target = Path(kw['output_dir']) / f"{kw['job_id']}-{kw['name']}-{i + kw.get('first_index', 1):02d}.png"
            target.write_bytes(b'png-' + str(i).encode())
            files.append({'path': str(target), 'width': 1536, 'height': 1024, 'bytes': target.stat().st_size,
                          'sha256': hashlib.sha256(target.read_bytes()).hexdigest()})
        with self.calls_lock:
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

    def test_status_answers_whether_images_can_be_made_now(self):
        self.pool.account_state('primary', 'browser_challenge', False)
        status = self.pool.status()
        self.assertEqual((status['can_generate'], status['serving']), (True, 'codex'), 'Codex still takes auto work after the wait')
        (self.pool.root / 'codex-lane-health.json').unlink()
        self.assertEqual(self.pool.status()['serving'], 'codex', 'a lane that is not running yet is started by the call')
        (self.pool.root / 'stop-codex').touch()
        status = self.pool.status()
        self.assertEqual((status['can_generate'], status['serving'], status['next_action']), (False, None, 'resolve_human_action'))
        self.assertTrue(status['human_action_blocks_generation'])
        self.assertEqual({g['login_group']: g for g in status['logins']}['codex']['usable_lanes'], 0)


class CodexFanOutContracts(CodexFallbackContracts):
    """Every image of a job is its own Codex run, in parallel within the lane's slots."""

    def test_a_four_image_job_draws_four_single_images_in_parallel(self):
        import threading
        active, peak, gate = [0], [0], threading.Lock()
        def tracked(**kw):
            with gate:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            time.sleep(0.3)
            try:
                return self.fake_generate(**kw)
            finally:
                with gate:
                    active[0] -= 1
        self.enable(parallel=8)
        job_id = self.pool.submit(prompt='Four mocks.', output_dir=str(self.out), request_id='four', count=4)['job_id']
        self.pool.fallback(job_id)
        self.run_lane(tracked)
        result = self.pool.get(job_id)
        self.assertEqual((result['status'], len(result['files'])), ('complete', 4))
        self.assertEqual(sorted(c['count'] for c in self.calls), [1, 1, 1, 1])
        self.assertEqual(sorted(c['variant'] for c in self.calls), [(1, 4), (2, 4), (3, 4), (4, 4)])
        self.assertEqual(len({f['path'] for f in result['files']}), 4, 'distinct file names')
        self.assertEqual(peak[0], 4, 'all four drew at the same time')

    def test_slots_bound_concurrent_runs_across_jobs(self):
        import threading
        active, peak, gate = [0], [0], threading.Lock()
        def tracked(**kw):
            with gate:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            time.sleep(0.2)
            try:
                return self.fake_generate(**kw)
            finally:
                with gate:
                    active[0] -= 1
        self.enable(parallel=3)
        ids = [self.pool.submit(prompt='x', output_dir=str(self.out), request_id='j%d' % i, count=2)['job_id'] for i in range(3)]
        for job_id in ids:
            self.pool.fallback(job_id)
        lane = CodexLane(self.pool, fallback_settings(self.pool.root), generate=tracked)
        lane.tick()
        self.assertEqual(len(lane.running), 2, 'a third job waits in the queue while slots are reserved')
        for _ in range(40):
            for t in list(lane.running.values()):
                t.join(5)
            lane.tick()
            if all(self.pool.row(j)['status'] == 'complete' for j in ids):
                break
        self.assertTrue(all(self.pool.row(j)['status'] == 'complete' for j in ids))
        self.assertLessEqual(peak[0], 3)

    def test_a_partial_failure_delivers_what_was_drawn(self):
        def flaky(**kw):
            if kw['first_index'] == 2:
                raise cx.CodexImageError('codex_no_image', 'none')
            return self.fake_generate(**kw)
        job_id = self.pool.submit(prompt='x', output_dir=str(self.out), request_id='flaky', count=3)['job_id']
        self.pool.fallback(job_id)
        self.run_lane(flaky)
        result = self.pool.get(job_id)
        self.assertEqual((result['status'], len(result['files'])), ('count_mismatch', 2))

    def test_progress_is_visible_while_other_images_are_still_drawing(self):
        import threading
        release = threading.Event()
        def staged(**kw):
            if kw['first_index'] == 2:
                release.wait(5)
            return self.fake_generate(**kw)
        job_id = self.pool.submit(prompt='x', output_dir=str(self.out), request_id='stage', count=2)['job_id']
        self.pool.fallback(job_id)
        lane = CodexLane(self.pool, fallback_settings(self.pool.root), generate=staged)
        lane.tick()
        for _ in range(50):
            if self.pool.get(job_id)['downloaded_count'] == 1:
                break
            time.sleep(0.1)
        mid = self.pool.get(job_id)
        self.assertEqual((mid['status'], mid['downloaded_count']), ('running', 1))
        release.set()
        for t in list(lane.running.values()):
            t.join(5)
        self.assertEqual(self.pool.get(job_id)['downloaded_count'], 2)

    def test_runs_waiting_for_a_slot_do_not_start_after_a_cancel(self):
        import threading
        self.enable(parallel=1)
        started = []
        job_id = self.pool.submit(prompt='x', output_dir=str(self.out), request_id='cancel-queue', count=3)['job_id']
        self.pool.fallback(job_id)
        def first_cancels(**kw):
            started.append(kw['first_index'])
            self.pool.cancel(job_id)
            return self.fake_generate(**kw)
        self.run_lane(first_cancels)
        self.assertEqual(len(started), 1, 'queued runs saw the cancel and never started')
        self.assertEqual(self.pool.row(job_id)['status'], 'cancelled')

    def test_fanout_can_be_switched_off(self):
        self.enable(fanout=False)
        job_id = self.pool.submit(prompt='x', output_dir=str(self.out), request_id='one-run', count=3)['job_id']
        self.pool.fallback(job_id)
        self.run_lane()
        self.assertEqual([c['count'] for c in self.calls], [3])


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
        for flag in ('--ignore-user-config', '--ignore-rules', '--ephemeral'):
            self.assertIn(flag, seen['argv'], 'a drawing run starts clean: no MCP servers, plugins or history entry')

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



class CodexFirstContracts(CodexFallbackContracts):
    """prefer=codex: Codex is the primary lane, the web the fallback (owner's choice 2026-09-30)."""

    def enable(self, **extra):
        super().enable(prefer='codex', **extra)

    def test_new_work_goes_to_codex_at_once_and_web_lanes_leave_it(self):
        job_id = self.submit()
        self.assertIsNone(self.pool.claim('primary'), 'the web does not take unpinned work')
        self.run_lane()
        result = self.pool.get(job_id)
        self.assertEqual((result['status'], result['provider'], result['fallback_from']), ('complete', 'codex-imagegen', 'codex_first'))

    def test_an_explicit_web_pin_is_respected(self):
        job_id = self.submit(account_id='primary')
        self.run_lane()
        self.assertEqual(self.pool.row(job_id)['status'], 'queued')
        self.assertEqual(self.pool.claim('primary')['id'], job_id)

    def test_a_codex_failure_gets_one_web_attempt_and_never_bounces_back(self):
        job_id = self.submit()
        def broken(**kw):
            raise cx.CodexImageError('codex_no_image', 'none')
        self.run_lane(broken)
        row = self.pool.row(job_id)
        self.assertEqual((row['status'], row['preferred']), ('queued', 'auto'))
        self.assertIsNone(self.pool.claim_codex(90, first=True), 'Codex does not take it back')
        self.assertEqual(self.pool.claim('primary')['id'], job_id, 'the web takes the hand-back')
        Worker(self.pool, 'primary', self.runtime).record_result(job_id, {'status': 'preparation_failed', 'files': [],
                                                                          'error': {'code': 'browser_timeout'}}, 'rt')
        self.assertEqual(self.pool.row(job_id)['status'], 'preparation_failed', 'no ping-pong')

    def test_a_hand_back_no_web_lane_takes_fails_with_the_codex_reason(self):
        job_id = self.submit()
        self.pool.claim_codex(90, first=True)
        self.pool.release_to_web(job_id, {'code': 'codex_timeout'})
        self.pool.expire_handbacks(270)
        self.assertEqual(self.pool.row(job_id)['status'], 'queued', 'a fresh hand-back waits for the web')
        with self.pool.connect() as db:
            note = json.loads(db.execute('SELECT result FROM jobs WHERE id=?', (job_id,)).fetchone()['result'])
            note['to_web_at'] -= 1000
            db.execute('UPDATE jobs SET result=? WHERE id=?', (json.dumps(note), job_id))
        self.pool.expire_handbacks(270)
        result = self.pool.get(job_id)
        self.assertEqual((result['status'], result['error']['code']), ('failed', 'codex_timeout'))

    def test_a_codex_result_can_be_continued_with_its_images(self):
        parent = self.submit('parent')
        self.run_lane()
        self.assertEqual(self.pool.get(parent)['status'], 'complete')
        child = self.pool.submit(prompt='Make it blue.', output_dir=str(self.out), request_id='child', continue_from=parent)['job_id']
        row = self.pool.row(child)
        self.assertEqual((row['preferred'], row['thread']), (CODEX_LANE, 'codex:' + parent))
        self.run_lane()
        self.assertEqual(self.pool.get(child)['status'], 'complete')
        refs = self.calls[-1]['references']
        self.assertEqual([Path(r).name for r in refs], [Path(f['path']).name for f in self.pool.get(parent)['files']])

    def test_results_carry_paths_not_codex_internals(self):
        job_id = self.submit()
        self.calls = []
        def with_internals(**kw):
            result = self.fake_generate(**kw)
            result.update(codex_usage={'input_tokens': 120000}, codex_thread='t-1')
            return result
        self.run_lane(with_internals)
        result = self.pool.get(job_id)
        self.assertNotIn('codex_usage', result)
        self.assertNotIn('codex_thread', result)
        self.assertEqual(set(result['files'][0]), {'path', 'width', 'height', 'bytes', 'sha256'})
        self.assertLess(len(json.dumps(result)), 2000, 'compact text, no image data')

    def test_an_old_hand_back_is_never_pulled_back_by_codex(self):
        job_id = self.submit()
        self.pool.claim_codex(90, first=True)
        self.pool.release_to_web(job_id, {'code': 'codex_timeout'})
        self.age(job_id, 10000)
        self.assertIsNone(self.pool.claim_codex(90, first=True))
        self.assertIsNone(self.pool.claim_codex(90, first=False))

    def test_a_codex_follow_up_handed_to_the_web_starts_there_with_its_images(self):
        parent = self.submit('parent')
        self.run_lane()
        child = self.pool.submit(prompt='Make it blue.', output_dir=str(self.out), request_id='child', continue_from=parent)['job_id']
        def broken(**kw):
            raise cx.CodexImageError('codex_no_image', 'none')
        self.run_lane(broken)
        payload = json.loads(self.pool.row(child)['payload'])
        self.assertEqual(len(payload['reference_images']), 1)
        self.assertEqual(payload['conversation_url'], '')
        worker = Worker(self.pool, 'primary', self.runtime)
        worker.tick(); worker.tick()
        self.assertEqual(self.runtime.starts, 1, 'the pool-rewritten payload is not a changed reference')

    def test_a_parked_job_codex_already_tried_is_not_pulled_back(self):
        job_id = self.submit()
        self.pool.claim_codex(90, first=True)
        self.pool.release_to_web(job_id, {'code': 'codex_timeout'})
        self.pool.account_state('primary', 'browser_challenge', False)
        with self.pool.connect() as db:
            db.execute("UPDATE jobs SET status='parked',account_id='primary',park_count=4 WHERE id=?", (job_id,))
        self.assertIsNone(self.pool.claim_codex(90, first=True))

    def test_repeated_codex_failures_open_the_breaker_and_the_web_takes_over(self):
        def broken(**kw):
            raise cx.CodexImageError('codex_missing', 'gone')
        lane = CodexLane(self.pool, fallback_settings(self.pool.root), generate=broken)
        for key in ('b1', 'b2', 'b3'):
            self.submit(key)
            lane.tick()
            for t in list(lane.running.values()):
                t.join(5)
        self.assertGreater(lane.degraded_until, time.time())
        import codex_lane
        codex_lane.write_health(self.pool, lane)
        from image_pool import codex_first
        self.assertFalse(codex_first(self.pool.root))
        job_id = self.submit('after')
        with self.pool.connect() as db:  # only the new, never-handed-back job is left for the web
            db.execute("UPDATE jobs SET status='cancelled' WHERE id<>?", (job_id,))
        self.assertEqual(self.pool.claim('primary')['id'], job_id, 'breaker open: unpinned work goes to the web again')

    def test_unpinned_work_does_not_wake_web_lanes_and_is_reported_as_codex_served(self):
        job_id = self.submit()
        self.pool.account_state('primary', 'browser_unhealthy', False)
        self.assertFalse(Worker(self.pool, 'primary', self.runtime).has_waiting_work())
        result = self.pool.get(job_id)
        self.assertEqual((result['served_by'], result['next_action']), ('codex', 'poll_same_job'))
        self.assertEqual(result['provider'], 'codex-imagegen', 'a queued Codex job does not claim the web')
        self.assertNotIn('blocking_accounts', result)

    def test_status_says_submit_although_every_web_lane_waits_for_a_person(self):
        # 2026-09-30: an agent read only the web lanes (browser_challenge, 0 usable), asked the
        # user to pass the check and never submitted, while Codex was idle.
        self.pool.account_state('primary', 'browser_challenge', False)
        status = self.pool.status()
        self.assertEqual((status['can_generate'], status['serving'], status['next_action']), (True, 'codex', 'submit'))
        self.assertFalse(status['human_action_blocks_generation'])
        self.assertIn('does not block', status['advice'])
        codex = {g['login_group']: g for g in status['logins']}['codex']
        self.assertEqual((codex['lanes'], codex['usable_lanes'], codex['provider']), (2, 2, 'codex-imagegen'))
        self.assertEqual(status['parallel_capacity'], 2)

    def test_a_lane_stopped_by_hand_leaves_new_work_to_the_web_at_once(self):
        from image_pool import codex_first
        (self.pool.root / 'stop-codex').touch()  # its heartbeat goes on while running jobs finish
        self.assertFalse(codex_first(self.pool.root))
        job_id = self.submit()
        self.assertEqual(self.pool.claim('primary')['id'], job_id)

    def test_web_lanes_of_a_login_waiting_for_a_person_do_not_count_as_serving(self):
        import human_gate
        (self.pool.root / 'stop-codex').touch()
        w.write_json(self.pool.root / 'worker-primary-health.json', {'version': 'x'})
        with self.pool.connect() as db:
            db.execute('UPDATE accounts SET heartbeat=? WHERE id=?', (time.time(), 'primary'))
        self.assertEqual(self.pool.status()['serving'], 'web')
        human_gate.GateBook(self.pool.root).enter('primary', 'primary', 'browser_challenge')
        status = self.pool.status()
        self.assertEqual((status['can_generate'], status['serving']), (False, None), 'claim() gives this login nothing')

    def test_status_does_not_send_the_user_to_the_web_while_the_lane_restarts(self):
        (self.pool.root / 'codex-lane-health.json').unlink()  # between stop and the next heartbeat
        self.pool.account_state('primary', 'browser_challenge', False)
        status = self.pool.status()
        self.assertEqual((status['can_generate'], status['serving']), (True, 'codex'))
        w.write_json(self.pool.root / 'codex-lane-health.json', {'beat': time.time(), 'degraded_until': time.time() + 60})
        self.assertEqual(self.pool.status()['serving'], None, 'breaker open and no web lane: nothing can draw')

    def test_without_a_running_lane_the_web_takes_everything(self):
        (self.pool.root / 'codex-lane-health.json').unlink()
        job_id = self.submit()
        self.assertEqual(self.pool.claim('primary')['id'], job_id)

# CodexFirstContracts reuses the fixture only; the web-first contracts above assert the
# opposite routing on purpose and run in their own class.
for _cls in (CodexFirstContracts, CodexFanOutContracts):
    for _name in [n for n in vars(CodexFallbackContracts) if n.startswith('test_')]:
        if _name not in vars(_cls):
            setattr(_cls, _name, None)


if __name__ == '__main__':
    unittest.main(verbosity=2)
