"""Lane throughput contracts: a stalled job must never hold the queue hostage."""
import json
from pathlib import Path
import time
import unittest
from unittest.mock import patch

import web_images as w
import image_worker
from image_pool import ADOPT_BACKOFF, PARK_AFTER_SECONDS, PARKED
import test_image_pool as fixtures
import test_web_images as contracts

# test_web_images loads its OWN copy of the module so that patching DATA cannot reach
# the real browser profile. Runtime-level tests must use that copy: importing
# web_images directly here would run park/adopt against the production data dir.
runtime = contracts.w


class LaneRelease(fixtures.QueueContracts):
    def stalled(self, account='primary'):
        """A job that reached the browser and then needed attention."""
        job = self.submit('stalled', account)
        worker = self.worker(account)
        worker.tick()
        self.pool.save_result(job['job_id'], {'status': 'needs_attention', 'files': [],
                                              'error': {'code': 'conversation_changed', 'message': 'fixture'}})
        return job, worker

    def age(self, job_id):
        with self.pool.connect() as db:
            db.execute('UPDATE jobs SET updated=? WHERE id=?', (time.time() - PARK_AFTER_SECONDS - 1, job_id))

    def due(self, job_id):
        with self.pool.connect() as db:
            db.execute('UPDATE jobs SET retry_after=0 WHERE id=?', (job_id,))

    def test_paused_job_releases_the_lane_so_the_queue_drains(self):
        job, worker = self.stalled()
        waiting = self.submit('waiting', 'primary')
        worker.tick()
        self.assertEqual(self.pool.row(job['job_id'])['status'], 'needs_attention')
        self.assertEqual(self.pool.row(waiting['job_id'])['status'], 'queued')
        self.age(job['job_id'])
        worker.tick()
        self.assertEqual(self.pool.row(job['job_id'])['status'], PARKED)
        worker.tick()
        self.assertEqual(self.pool.row(waiting['job_id'])['status'], 'running')

    def test_parking_keeps_the_account_the_conversation_and_the_files(self):
        job, worker = self.stalled()
        self.age(job['job_id'])
        worker.tick()
        row = self.pool.row(job['job_id'])
        self.assertEqual(row['account_id'], 'primary')
        self.assertEqual(row['runtime_id'], self.runtimes['primary'].active_id)
        self.assertIsNone(self.runtimes['primary'].active)
        result = self.pool.get(job['job_id'])
        self.assertTrue(result['retryable'])
        self.assertEqual(result['error']['code'], 'conversation_changed')

    def test_parked_job_is_re_observed_without_resubmitting(self):
        job, worker = self.stalled()
        lane = self.runtimes['primary']
        self.age(job['job_id'])
        worker.tick()
        starts = lane.starts
        self.due(job['job_id'])
        worker.tick()
        self.assertEqual(lane.adopts, 1)
        self.assertEqual(lane.starts, starts)
        row = self.pool.row(job['job_id'])
        self.assertEqual(row['status'], 'running')
        self.assertEqual(row['account_id'], 'primary')

    def test_auto_adoption_is_bounded_then_waits_for_a_human(self):
        job, worker = self.stalled()
        self.runtimes['primary'].adopt_fails = True
        self.age(job['job_id'])
        worker.tick()
        attempts = 0
        while True:
            self.due(job['job_id'])
            if not self.pool.due_parked('primary'):
                break
            worker.tick()
            attempts += 1
            self.assertLess(attempts, 10, 'unattended adoption never stopped')
        self.assertEqual(attempts, len(ADOPT_BACKOFF))
        self.assertEqual(self.pool.row(job['job_id'])['status'], PARKED)
        self.assertEqual(self.pool.get(job['job_id'])['next_action'], 'inspect_account_then_resume')

    def test_resume_makes_a_parked_job_eligible_again_without_evicting_the_lane(self):
        job, worker = self.stalled()
        self.age(job['job_id'])
        worker.tick()
        other = self.submit('other', 'primary')
        worker.tick()
        self.assertEqual(self.pool.row(other['job_id'])['status'], 'running')
        self.assertIsNone(self.pool.due_parked('primary'))
        self.pool.resume(job['job_id'])
        self.assertEqual(self.pool.row(other['job_id'])['status'], 'running')
        self.assertEqual(self.pool.due_parked('primary')['id'], job['job_id'])

    def test_parked_job_that_never_reached_the_browser_waits_for_a_human(self):
        job = self.submit('never-sent', 'primary')
        with self.pool.connect() as db:
            db.execute("UPDATE jobs SET status='needs_attention',account_id='primary' WHERE id=?", (job['job_id'],))
        worker = self.worker('primary')
        self.age(job['job_id'])
        worker.tick()
        row = self.pool.row(job['job_id'])
        self.assertEqual(row['status'], PARKED)
        self.assertGreater(row['park_count'], len(ADOPT_BACKOFF))
        self.pool.resume(job['job_id'])
        worker.tick()
        self.assertEqual(self.pool.row(job['job_id'])['status'], 'dispatching')
        worker.tick()
        self.assertEqual(self.runtimes['primary'].starts, 1)


class AccountRecovery(fixtures.QueueContracts):
    def test_browser_failure_recovers_without_a_human_but_stays_bounded(self):
        worker = self.worker('primary')
        self.pool.account_state('primary', 'browser_unhealthy', False, {'code': 'browser_operation_failed'})
        account = self.pool.account('primary')
        worker.last_check = time.monotonic()
        self.assertFalse(worker.recheck_due(account))
        worker.last_check = time.monotonic() - image_worker.RECHECK_BACKOFF[0] - 1
        self.assertTrue(worker.recheck_due(account))
        worker.recoveries = len(image_worker.RECHECK_BACKOFF)
        self.assertFalse(worker.recheck_due(account))

    def test_unhealthy_account_queues_its_own_check(self):
        worker = self.worker('primary')
        self.pool.account_state('primary', 'browser_unhealthy', False, {'code': 'browser_operation_failed'})
        worker.last_check = time.monotonic() - image_worker.RECHECK_BACKOFF[0] - 1
        worker.tick()
        self.assertEqual(self.pool.status()['accounts'][0]['last_control']['action'], 'check')
        self.assertEqual(worker.recoveries, 1)

    def test_disabled_account_is_never_touched_on_its_own(self):
        self.pool.enable('primary', False)
        self.pool.account_state('primary', 'browser_unhealthy', False, {'code': 'browser_operation_failed'})
        worker = self.worker('primary')
        worker.last_check = time.monotonic() - 10000
        worker.tick()
        self.assertIsNone(self.pool.status()['accounts'][0].get('last_control'))


class ExtraLanes(fixtures.QueueContracts):
    def login(self, account_id='primary'):
        auth = Path(self.pool.account(account_id)['data_dir']) / 'auth-state.json'
        auth.parent.mkdir(parents=True, exist_ok=True)
        auth.write_text('{"cookies":[]}', encoding='utf-8')
        return auth

    def test_cloned_lane_reuses_the_login_in_its_own_profile(self):
        auth = self.login()
        before = self.pool.status()['configured_capacity']
        result = self.pool.clone_account('primary', 'primary2')
        self.assertEqual(result['cloned_from'], 'primary')
        self.assertEqual(self.pool.status()['configured_capacity'], before + 1)
        clone = self.pool.account('primary2')
        self.assertNotEqual(clone['data_dir'], self.pool.account('primary')['data_dir'])
        self.assertEqual((Path(clone['data_dir']) / 'auth-state.json').read_text(encoding='utf-8'),
                         auth.read_text(encoding='utf-8'))

    def test_cloning_without_a_saved_login_is_refused(self):
        with self.assertRaises(w.ImageError) as caught:
            self.pool.clone_account('primary', 'primary2')
        self.assertEqual(caught.exception.code, 'clone_without_login')

    def test_cloned_lane_runs_a_job_the_busy_original_cannot(self):
        self.login()
        self.pool.clone_account('primary', 'primary2')
        self.pool.account_state('primary2', 'authenticated', True)
        self.runtimes['primary2'] = fixtures.FakeRuntime(self.pool.account('primary2')['data_dir'])
        busy = self.submit('busy', 'primary')
        self.worker('primary').tick()
        self.assertEqual(self.pool.row(busy['job_id'])['status'], 'running')
        overflow = self.submit('overflow', 'auto')
        self.worker('primary').tick()
        self.assertEqual(self.pool.row(overflow['job_id'])['status'], 'queued')
        self.worker('primary2').tick()
        self.assertEqual(self.pool.row(overflow['job_id'])['account_id'], 'primary2')


class PollCadence(fixtures.QueueContracts):
    def test_tick_slows_down_only_while_generation_cannot_change(self):
        self.submit('slow', 'primary')
        worker = self.worker('primary')
        worker.tick()
        lane = self.runtimes['primary']
        with patch.object(lane, 'poll', return_value={'job_id': lane.active_id, 'status': 'generating', 'files': [], 'observed_count': 0}):
            worker.tick()
        self.assertEqual(worker.delay, image_worker.WATCH_TICK)
        with patch.object(lane, 'poll', return_value={'job_id': lane.active_id, 'status': 'generating', 'files': [], 'observed_count': 2}):
            worker.tick()
        self.assertEqual(worker.delay, image_worker.IDLE_TICK)


class ConversationRecovery(contracts.Contracts):
    def stale(self):
        job = self.job()
        job.update(created_at=time.time(), conversation_url='https://chatgpt.com/c/abc-def',
                   identity_hash=runtime.prompt_hash('my prompt'))
        runtime.save_job(job)
        return job

    def snapshot(self, user_text='my prompt'):
        return {'user_count': 1, 'user_text': user_text, 'url': 'https://chatgpt.com/c/abc-def',
                'stop': True, 'pending_images': 0, 'text': '', 'images': []}

    def saved(self, job):
        return json.loads(runtime.job_path(job['job_id']).read_text(encoding='utf-8'))

    def test_stale_conversation_schedules_a_reload_instead_of_pausing(self):
        job = self.stale()
        stale = self.snapshot('Create imagemy prompt')
        with patch.object(runtime, 'ensure_browser', return_value={'url': stale['url']}), \
             patch.object(runtime, 'cli'), patch.object(runtime, 'run_js', return_value=stale):
            result = runtime.safe_call(runtime.poll, job_id=job['job_id'])
        self.assertTrue(result['ok'])
        self.assertTrue(self.saved(job)['refresh_requested'])
        self.assertEqual(self.saved(job)['identity_retry'], 1)

    def test_reload_happens_before_identity_is_judged(self):
        job = self.stale()
        job['refresh_requested'] = True
        runtime.save_job(job)
        polls = iter([self.snapshot('Create imagemy prompt'), self.snapshot()])

        def js(source, **kwargs):
            return next(polls) if source == runtime.POLL_JS else True

        with patch.object(runtime, 'ensure_browser', return_value={'url': 'https://chatgpt.com/c/abc-def'}), \
             patch.object(runtime, 'cli') as browser, patch.object(runtime, 'run_js', side_effect=js):
            result = runtime.poll(job['job_id'])
        self.assertIn(['goto', 'https://chatgpt.com/c/abc-def'], [list(call.args[0]) for call in browser.call_args_list])
        self.assertEqual(result['status'], 'generating')
        self.assertNotIn('identity_retry', self.saved(job))

    def test_a_truly_different_conversation_still_pauses_without_downloading(self):
        job = self.stale()
        foreign = self.snapshot('a completely different task')
        with patch.object(runtime, 'ensure_browser', return_value={'url': foreign['url']}), \
             patch.object(runtime, 'cli'), patch.object(runtime, 'run_js', return_value=foreign):
            for _ in range(runtime.IDENTITY_RETRIES):
                self.assertTrue(runtime.safe_call(runtime.poll, job_id=job['job_id'])['ok'])
            result = runtime.safe_call(runtime.poll, job_id=job['job_id'])
        self.assertFalse(result['ok'])
        self.assertEqual(result['error']['code'], 'conversation_changed')
        self.assertEqual(result['downloaded_count'], 0)
        self.assertIsNotNone(runtime.active_job())

    def test_parking_releases_the_browser_and_adopting_takes_it_back(self):
        job = self.stale()
        runtime.park(job['job_id'])
        self.assertIsNone(runtime.active_job())
        self.assertIn('parked_at', self.saved(job))
        with patch.object(runtime, 'ensure_browser'), patch.object(runtime, 'cli') as browser, patch.object(runtime, 'run_js'):
            runtime.adopt(job['job_id'])
        self.assertIn(['goto', 'https://chatgpt.com/c/abc-def'], [list(call.args[0]) for call in browser.call_args_list])
        self.assertEqual(runtime.active_job()['job_id'], job['job_id'])
        self.assertNotIn('parked_at', self.saved(job))

    def test_adopting_never_steals_a_browser_owned_by_another_job(self):
        job = self.stale()
        runtime.park(job['job_id'])
        runtime.write_json(runtime.DATA / 'active.json', {'job_id': '20260907-ffffffffffff'})
        with patch.object(runtime, 'ensure_browser') as browser:
            result = runtime.safe_call(runtime.adopt, job_id=job['job_id'])
        self.assertEqual(result['error']['code'], 'job_active')
        browser.assert_not_called()

    def test_adopting_a_job_without_a_conversation_is_refused(self):
        job = self.job()
        job.update(created_at=time.time())
        runtime.save_job(job)
        runtime.park(job['job_id'])
        with patch.object(runtime, 'ensure_browser') as browser:
            result = runtime.safe_call(runtime.adopt, job_id=job['job_id'])
        self.assertEqual(result['error']['code'], 'adopt_without_conversation')
        browser.assert_not_called()


class ModuleIsolation(unittest.TestCase):
    """Runtime tests that reach the installed module write into the real browser profile."""

    def test_runtime_tests_use_the_private_module_copy(self):
        self.assertIsNot(runtime, w)

    def test_no_runtime_test_targets_the_installed_module(self):
        source = Path(__file__).read_text(encoding='utf-8')
        body = source.split('class ConversationRecovery', 1)[1].split('class ModuleIsolation', 1)[0]
        for forbidden in ('patch.object(w,', 'w.poll(', 'w.park(', 'w.adopt(', 'w.DATA'):
            self.assertNotIn(forbidden, body)


if __name__ == '__main__':
    unittest.main(verbosity=2)
