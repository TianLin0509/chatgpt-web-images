"""Lane throughput contracts: a stalled job must never hold the queue hostage."""
import asyncio
import json
from pathlib import Path
import time
import unittest
from unittest.mock import patch

import web_images as w
import image_worker
from image_pool import (ADOPT_BACKOFF, BATCH_HINT_SIBLINGS, MAX_BATCH, MAX_LANES,
                        PARK_AFTER_SECONDS, PARKED, login_group)
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


class LoginBalance(fixtures.QueueContracts):
    """Concurrency comes from lanes; fairness comes from spreading lanes over logins."""

    def lanes(self, group, count):
        auth = Path(self.pool.account(group)['data_dir']) / 'auth-state.json'
        auth.parent.mkdir(parents=True, exist_ok=True)
        if not auth.exists():
            auth.write_text('{"cookies":[]}', encoding='utf-8')
        result = self.pool.scale_account(group, count)
        for lane in result['lanes']:
            self.pool.account_state(lane, 'authenticated', True)
            self.pool.heartbeat(lane)
            self.runtimes.setdefault(lane, fixtures.FakeRuntime(self.pool.account(lane)['data_dir']))
        return result['lanes']

    def group_of(self, job):
        owner = self.pool.row(job['job_id'])['account_id']
        return owner and login_group(self.pool.account(owner))

    def drive(self, lanes, rounds=8):
        for _ in range(rounds):
            for lane in lanes:
                self.worker(lane).tick()

    def test_eight_lanes_run_eight_jobs_at_the_same_time(self):
        lanes = self.lanes('primary', 4) + self.lanes('secondary', 4)
        for lane in lanes:
            self.runtimes[lane].poll = lambda job_id: {'job_id': job_id, 'status': 'generating', 'files': []}
        jobs = [self.submit('bulk%d' % index) for index in range(8)]
        self.drive(lanes)
        owners = [self.pool.row(job['job_id'])['account_id'] for job in jobs]
        self.assertNotIn(None, owners)
        self.assertEqual(len(set(owners)), 8)
        self.assertEqual([self.group_of(job) for job in jobs].count('primary'), 4)

    def test_auto_work_alternates_between_logins(self):
        lanes = self.lanes('primary', 2) + self.lanes('secondary', 2)
        jobs = [self.submit('job%d' % index) for index in range(4)]
        self.drive(lanes)
        groups = [self.group_of(job) for job in jobs]
        self.assertEqual(sorted(groups), ['primary', 'primary', 'secondary', 'secondary'])
        self.assertNotEqual(groups[0], groups[1])

    def test_a_login_already_carrying_more_waits_for_the_other(self):
        primary = self.lanes('primary', 2)
        secondary = self.lanes('secondary', 2)
        self.submit('first')
        self.worker(primary[0]).tick()
        second = self.submit('second')
        self.worker(primary[1]).tick()
        self.assertEqual(self.pool.row(second['job_id'])['status'], 'queued')
        self.worker(secondary[0]).tick()
        self.assertEqual(self.group_of(second), 'secondary')

    def test_fairness_never_stalls_on_a_login_without_a_live_worker(self):
        primary = self.lanes('primary', 2)
        secondary = self.lanes('secondary', 1)
        with self.pool.connect() as db:
            db.execute('UPDATE accounts SET heartbeat=0 WHERE id=?', (secondary[0],))
        jobs = [self.submit('dead%d' % index) for index in range(2)]
        self.drive(primary, rounds=2)
        self.assertEqual([self.group_of(job) for job in jobs], ['primary', 'primary'])

    def test_pinning_a_login_reaches_any_of_its_lanes(self):
        lanes = self.lanes('primary', 3)
        self.lanes('secondary', 1)
        job = self.submit('pinned', 'primary')
        self.worker(lanes[2]).tick()
        self.assertEqual(self.pool.row(job['job_id'])['account_id'], lanes[2])

    def test_status_reports_load_per_login(self):
        self.lanes('primary', 3)
        self.lanes('secondary', 2)
        status = self.pool.status()
        logins = {group['login_group']: group for group in status['logins']}
        self.assertEqual(logins['primary']['lanes'], 3)
        self.assertEqual(logins['secondary']['lanes'], 2)
        self.assertEqual(status['configured_capacity'], 5)


class LaneScaling(fixtures.QueueContracts):
    def login(self, account_id='primary'):
        auth = Path(self.pool.account(account_id)['data_dir']) / 'auth-state.json'
        auth.parent.mkdir(parents=True, exist_ok=True)
        auth.write_text('{"cookies":[]}', encoding='utf-8')
        return auth

    def test_scale_adds_lanes_and_disables_surplus_without_deleting(self):
        self.login()
        grown = self.pool.scale_account('primary', 4)
        self.assertEqual(grown['lanes'], ['primary', 'primary-2', 'primary-3', 'primary-4'])
        self.assertEqual(grown['added'], ['primary-2', 'primary-3', 'primary-4'])
        shrunk = self.pool.scale_account('primary', 2)
        self.assertEqual(shrunk['disabled'], ['primary-3', 'primary-4'])
        self.assertFalse(self.pool.account('primary-4')['enabled'])
        self.assertTrue(Path(self.pool.account('primary-4')['data_dir']).exists())

    def test_scaling_again_reuses_existing_lanes(self):
        self.login()
        self.pool.scale_account('primary', 3)
        again = self.pool.scale_account('primary', 3)
        self.assertEqual(again['added'], [])
        self.assertEqual(again['disabled'], [])

    def test_lane_count_is_capped_by_the_machine_not_the_website(self):
        self.login()
        with self.assertRaises(w.ImageError) as caught:
            self.pool.scale_account('primary', MAX_LANES + 1)
        self.assertEqual(caught.exception.code, 'invalid_lane_count')

    def test_total_lane_budget_is_enforced_across_logins(self):
        self.login()
        with self.assertRaises(w.ImageError) as caught:
            self.pool.scale_account('primary', MAX_LANES)
        self.assertEqual(caught.exception.code, 'lane_budget_exceeded')

    def test_reseed_refreshes_a_lane_login_but_never_while_it_is_busy(self):
        leader = self.login()
        lanes = self.pool.scale_account('primary', 2)['lanes']
        leader.write_text('{"cookies":[],"session":"renewed"}', encoding='utf-8')
        self.pool.reseed_account(lanes[1])
        copied = Path(self.pool.account(lanes[1])['data_dir']) / 'auth-state.json'
        self.assertEqual(copied.read_text(encoding='utf-8'), leader.read_text(encoding='utf-8'))
        self.assertFalse(self.pool.account(lanes[1])['ready'])
        self.pool.account_state(lanes[1], 'authenticated', True)
        self.submit('busy', lanes[1])
        self.pool.claim(lanes[1])
        with self.assertRaises(w.ImageError) as caught:
            self.pool.reseed_account(lanes[1])
        self.assertEqual(caught.exception.code, 'account_busy')

    def test_reseeding_the_group_leader_is_refused(self):
        self.login()
        self.pool.scale_account('primary', 2)
        with self.assertRaises(w.ImageError) as caught:
            self.pool.reseed_account('primary')
        self.assertEqual(caught.exception.code, 'reseed_leader')


class IdleMemory(fixtures.QueueContracts):
    """Many lanes are only affordable when the idle ones are not holding a browser."""

    def test_idle_lane_gives_its_browser_back_after_the_grace_period(self):
        worker = self.worker('primary')
        worker.tick()
        self.assertEqual(self.runtimes['primary'].released, 0)
        worker.idle_since = time.monotonic() - image_worker.IDLE_BROWSER_SECONDS - 1
        worker.tick()
        self.assertEqual(self.runtimes['primary'].released, 1)

    def test_a_released_lane_does_not_keep_retrying(self):
        worker = self.worker('primary')
        worker.idle_since = time.monotonic() - image_worker.IDLE_BROWSER_SECONDS - 1
        for _ in range(5):
            worker.tick()
        self.assertEqual(self.runtimes['primary'].released, 1)

    def test_a_lane_with_work_never_closes_its_browser(self):
        self.submit('busy', 'primary')
        worker = self.worker('primary')
        worker.idle_since = time.monotonic() - image_worker.IDLE_BROWSER_SECONDS - 1
        worker.tick()
        self.assertEqual(self.runtimes['primary'].released, 0)
        self.assertFalse(worker.browser_released)

    def test_new_work_re_arms_the_grace_period(self):
        worker = self.worker('primary')
        worker.idle_since = time.monotonic() - image_worker.IDLE_BROWSER_SECONDS - 1
        worker.tick()
        self.assertTrue(worker.browser_released)
        self.submit('later', 'primary')
        worker.tick()
        self.assertFalse(worker.browser_released)
        self.assertEqual(self.runtimes['primary'].released, 1)


class BatchShape(fixtures.QueueContracts):
    """One prompt wanting N alternates is one request, not N jobs."""

    def singles(self, how_many, folder='shared'):
        return [self.pool.submit(prompt='Draw option %d.' % index, request_id='opt%d' % index,
                                 output_dir=str(self.root / folder), account_id='auto')
                for index in range(how_many)]

    def test_several_in_flight_single_image_jobs_suggest_one_request(self):
        results = self.singles(BATCH_HINT_SIBLINGS)
        self.assertNotIn('hint', results[0])
        self.assertEqual(results[-1]['hint']['code'], 'consider_one_multi_image_request')
        self.assertEqual(results[-1]['hint']['in_flight_single_image_jobs'], BATCH_HINT_SIBLINGS)

    def test_a_multi_image_request_is_never_hinted(self):
        self.singles(BATCH_HINT_SIBLINGS)
        batched = self.pool.submit(prompt='Draw four options.', count=4, request_id='batched',
                                   output_dir=str(self.root / 'shared'))
        self.assertNotIn('hint', batched)

    def test_retrying_finished_jobs_is_never_called_a_batch(self):
        done = self.singles(BATCH_HINT_SIBLINGS)
        for job in done:
            self.pool.save_result(job['job_id'], {'status': 'failed', 'files': [],
                                                  'error': {'code': 'browser_timeout', 'message': 'fixture'}})
        retry = self.pool.submit(prompt='Draw option 0.', request_id='opt0-retry',
                                 output_dir=str(self.root / 'shared'))
        self.assertNotIn('hint', retry)

    def test_jobs_for_different_folders_are_not_one_batch(self):
        for index in range(BATCH_HINT_SIBLINGS):
            result = self.pool.submit(prompt='Draw %d.' % index, request_id='sep%d' % index,
                                      output_dir=str(self.root / ('folder%d' % index)))
        self.assertNotIn('hint', result)

    def test_resubmitting_the_same_request_never_repeats_the_hint(self):
        self.singles(BATCH_HINT_SIBLINGS)
        again = self.pool.submit(prompt='Draw option 0.', request_id='opt0',
                                 output_dir=str(self.root / 'shared'))
        self.assertTrue(again['reused_request'])
        self.assertNotIn('hint', again)


class BatchApi(fixtures.QueueContracts):
    """One call queues the batch, one call waits on it; that is what keeps lanes full."""

    def batch(self, how_many=3, **shared):
        return [{'prompt': 'Draw page %d.' % index, 'request_id': 'page%d' % index,
                 'output_dir': str(self.root / 'deck'), **shared} for index in range(how_many)]

    def test_one_call_queues_the_whole_batch(self):
        result = self.pool.submit_many(self.batch(4))
        self.assertEqual(result['submitted'], 4)
        self.assertEqual(len(set(result['job_ids'])), 4)
        self.assertEqual(result['reused'], 0)
        for job_id in result['job_ids']:
            self.assertEqual(self.pool.row(job_id)['status'], 'queued')

    def test_resending_a_batch_is_idempotent(self):
        first = self.pool.submit_many(self.batch(3))
        again = self.pool.submit_many(self.batch(3))
        self.assertEqual(first['job_ids'], again['job_ids'])
        self.assertEqual(again['reused'], 3)

    def test_nothing_is_queued_when_any_entry_is_invalid(self):
        requests = self.batch(2) + [{'prompt': '', 'request_id': 'broken'}]
        with self.assertRaises(w.ImageError):
            self.pool.submit_many(requests)
        self.assertEqual(self.pool.status()['jobs'].get('queued', 0), 0)

    def test_duplicate_request_ids_in_one_batch_are_refused(self):
        requests = self.batch(2)
        requests[1]['request_id'] = requests[0]['request_id']
        with self.assertRaises(w.ImageError) as caught:
            self.pool.submit_many(requests)
        self.assertEqual(caught.exception.code, 'request_id_conflict')

    def test_every_batch_entry_needs_its_own_request_id(self):
        with self.assertRaises(w.ImageError) as caught:
            self.pool.submit_many([{'prompt': 'no id', 'output_dir': str(self.root / 'deck')}])
        self.assertEqual(caught.exception.code, 'invalid_request_id')

    def test_unsupported_batch_keys_are_refused(self):
        with self.assertRaises(w.ImageError) as caught:
            self.pool.submit_many([{'prompt': 'x', 'request_id': 'k', 'account_id': 'primary'}])
        self.assertEqual(caught.exception.code, 'invalid_batch')

    def test_batch_size_is_bounded(self):
        with self.assertRaises(w.ImageError):
            self.pool.submit_many(self.batch(MAX_BATCH + 1))

    def test_polling_a_batch_reports_every_job_in_one_result(self):
        jobs = self.pool.submit_many(self.batch(3))['job_ids']
        summary = asyncio.run(self.pool.poll_many_async(jobs, 0))
        self.assertEqual(summary['total'], 3)
        self.assertEqual(summary['pending'], 3)
        self.assertEqual(summary['next_action'], 'poll_same_job_ids')
        self.assertEqual([job['job_id'] for job in summary['jobs']], jobs)

    def test_a_finished_batch_reports_every_file_once(self):
        jobs = self.pool.submit_many(self.batch(2))['job_ids']
        for lane in ('primary', 'secondary'):
            self.worker(lane).tick()
            self.worker(lane).tick()
        summary = asyncio.run(self.pool.poll_many_async(jobs, 0))
        self.assertEqual(summary['settled'], 2)
        self.assertEqual(summary['complete'], 2)
        self.assertEqual(summary['next_action'], 'deliver_saved_files')
        self.assertEqual(len(summary['files']), 2)

    def test_return_when_any_stops_at_the_first_settled_job(self):
        jobs = self.pool.submit_many(self.batch(2))['job_ids']
        self.worker('primary').tick()
        self.worker('primary').tick()
        summary = asyncio.run(self.pool.poll_many_async(jobs, 45, return_when='any'))
        self.assertEqual(summary['settled'], 1)
        self.assertEqual(summary['pending'], 1)

    def test_an_unknown_job_id_never_stalls_or_fails_the_batch(self):
        jobs = self.pool.submit_many(self.batch(1))['job_ids'] + ['20260101-deadbeefdead']
        summary = asyncio.run(self.pool.poll_many_async(jobs, 45, return_when='all'))
        self.assertFalse(summary['ok'])
        unknown = [job for job in summary['jobs'] if job['status'] == 'unknown'][0]
        self.assertEqual(unknown['error']['code'], 'job_not_found')

    def test_duplicate_job_ids_are_polled_once(self):
        jobs = self.pool.submit_many(self.batch(1))['job_ids']
        summary = asyncio.run(self.pool.poll_many_async(jobs * 3, 0))
        self.assertEqual(summary['total'], 1)

    def test_batch_poll_rejects_an_unsupported_return_mode(self):
        jobs = self.pool.submit_many(self.batch(1))['job_ids']
        with self.assertRaises(w.ImageError) as caught:
            asyncio.run(self.pool.poll_many_async(jobs, 0, return_when='first'))
        self.assertEqual(caught.exception.code, 'invalid_return_when')


class McpTeachesParallelism(unittest.TestCase):
    """The MCP surface must state the parallel rules, not merely permit them."""

    def surface(self):
        import tempfile
        from image_service import create_mcp
        from image_pool import Pool
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        server = create_mcp(Pool(Path(folder.name) / 'pool'))
        return server, asyncio.run(server.list_tools())

    def test_instructions_state_the_two_request_shapes(self):
        text = self.surface()[0].instructions
        for phrase in ('count=N', 'image_generate_batch', 'alternates of ONE prompt',
                       'DIFFERENT prompts', 'NEVER SERIALISE', 'job_ids'):
            self.assertIn(phrase, text)

    def test_instructions_say_concurrency_is_lanes_not_accounts(self):
        text = self.surface()[0].instructions
        self.assertIn('NOT the number of ChatGPT accounts', text)
        self.assertIn('balances across logins', text)

    def test_batch_and_multi_poll_are_exposed_as_tools(self):
        names = {tool.name for tool in self.surface()[1]}
        self.assertIn('image_generate_batch', names)
        poll = [tool for tool in self.surface()[1] if tool.name == 'image_poll'][0]
        self.assertIn('job_ids', poll.inputSchema['properties'])
        self.assertIn('return_when', poll.inputSchema['properties'])

    def test_generate_tool_points_at_count_for_alternates(self):
        generate = [tool for tool in self.surface()[1] if tool.name == 'image_generate'][0]
        self.assertIn('count=N', generate.description)
        self.assertIn('one slide', generate.description)


class IdentityUnderReflow(contracts.Contracts):
    """A finished job must not look foreign because the browser wrapped a line."""

    # Real capture from the primary lane, 2026-09-19: the prompt ended with
    # 'On-Device', the word landed at the end of a rendered line, and innerText
    # returned 'On- Device'. One invisible space stalled a completed job for hours.
    SENT = 'Gemini Robotics On-Device\n页脚：来源：产品官方资料。'
    REFLOWED = 'Gemini Robotics On- Device\n页脚：来源：产品官方资料。'

    def wrapped(self):
        job = self.job()
        job.update(created_at=time.time(), conversation_url='https://chatgpt.com/c/abc-def',
                   identity_hash=runtime.prompt_hash(self.SENT),
                   identity_loose=runtime.loose_hash(self.SENT))
        runtime.save_job(job)
        return job

    def snapshot(self, user_text):
        return {'user_count': 1, 'user_text': user_text, 'url': 'https://chatgpt.com/c/abc-def',
                'stop': False, 'pending_images': 0, 'text': 'Worked for 1m 34s', 'images': []}

    def test_a_soft_wrapped_hyphen_is_not_a_different_conversation(self):
        job = self.wrapped()
        with patch.object(runtime, 'ensure_browser', return_value={'url': 'https://chatgpt.com/c/abc-def'}), \
             patch.object(runtime, 'cli'), patch.object(runtime, 'run_js', return_value=self.snapshot(self.REFLOWED)):
            result = runtime.safe_call(runtime.poll, job_id=job['job_id'])
        self.assertTrue(result['ok'])
        self.assertNotIn('identity_retry', json.loads(runtime.job_path(job['job_id']).read_text(encoding='utf-8')))

    def test_reflow_tolerance_still_rejects_a_different_prompt(self):
        job = self.wrapped()
        foreign = self.snapshot('An entirely unrelated request about cats.')
        with patch.object(runtime, 'ensure_browser', return_value={'url': 'https://chatgpt.com/c/abc-def'}), \
             patch.object(runtime, 'cli'), patch.object(runtime, 'run_js', return_value=foreign):
            for _ in range(runtime.IDENTITY_RETRIES):
                runtime.safe_call(runtime.poll, job_id=job['job_id'])
            result = runtime.safe_call(runtime.poll, job_id=job['job_id'])
        self.assertEqual(result['error']['code'], 'conversation_changed')

    def test_cancel_also_tolerates_reflow(self):
        job = self.wrapped()
        with patch.object(runtime, 'ensure_browser'), \
             patch.object(runtime, 'run_js', return_value=self.snapshot(self.REFLOWED)):
            result = runtime.safe_call(runtime.cancel, job_id=job['job_id'])
        self.assertEqual(result['status'], 'cancelled')

    def test_line_break_differences_do_not_break_identity(self):
        job = self.wrapped()
        squashed = self.snapshot(self.SENT.replace('\n', '  '))
        with patch.object(runtime, 'ensure_browser', return_value={'url': 'https://chatgpt.com/c/abc-def'}), \
             patch.object(runtime, 'cli'), patch.object(runtime, 'run_js', return_value=squashed):
            result = runtime.safe_call(runtime.poll, job_id=job['job_id'])
        self.assertTrue(result['ok'])

    def test_a_started_job_records_both_identities(self):
        with patch.object(runtime, 'ensure_browser'), patch.object(runtime, 'cli'), \
             patch.object(runtime, 'set_visible', return_value=True), \
             patch.object(runtime, 'run_js', side_effect=[True, True, True, {'url': 'https://chatgpt.com/c/abc-def'}]):
            started = runtime.start(prompt=self.SENT, output_dir=str(self.data))
        stored = json.loads(runtime.job_path(started['job_id']).read_text(encoding='utf-8'))
        self.assertEqual(stored['identity_loose'], runtime.loose_hash(self.SENT))
        self.assertNotEqual(stored['identity_loose'], stored['identity_hash'])


class AnswerWithoutImage(contracts.Contracts):
    """A finished reply with no image is an answer the caller can act on, not a wait."""

    def pending_job(self):
        job = self.job()
        job.update(created_at=time.time(), requested_count=1, output_dir=str(self.data), name='x',
                   conversation_url='https://chatgpt.com/c/abc-def',
                   identity_hash=runtime.prompt_hash('my prompt'),
                   identity_loose=runtime.loose_hash('my prompt'))
        runtime.save_job(job)
        return job

    def reply(self, text, turn_complete=True, images=None, stop=False, pending=0):
        return {'user_count': 1, 'user_text': 'my prompt', 'url': 'https://chatgpt.com/c/abc-def',
                'stop': stop, 'pending_images': pending, 'text': text,
                'turn_complete': turn_complete, 'images': images or []}

    def poll_with(self, job, snapshot):
        with patch.object(runtime, 'ensure_browser', return_value={'url': 'https://chatgpt.com/c/abc-def'}), \
             patch.object(runtime, 'cli'), patch.object(runtime, 'run_js', return_value=snapshot):
            return runtime.safe_call(runtime.poll, job_id=job['job_id'])

    def test_a_finished_reply_without_an_image_fails_at_once(self):
        job = self.pending_job()
        result = self.poll_with(job, self.reply('I can help with that, but here is a plan instead.'))
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['error']['code'], 'no_image_in_reply')
        self.assertIn('plan instead', result['error']['message'])
        self.assertIsNone(runtime.active_job())

    def test_a_refusal_is_reported_as_a_generation_failure(self):
        job = self.pending_job()
        result = self.poll_with(job, self.reply("I'm unable to generate that image."))
        self.assertEqual(result['error']['code'], 'generation_failed')

    def test_an_unfinished_turn_is_still_waited_for(self):
        job = self.pending_job()
        result = self.poll_with(job, self.reply('Thinking', turn_complete=False))
        self.assertTrue(result['ok'])
        self.assertEqual(result['status'], 'generating')
        self.assertIsNotNone(runtime.active_job())

    def test_a_streaming_reply_is_never_called_imageless(self):
        job = self.pending_job()
        result = self.poll_with(job, self.reply('Working', turn_complete=True, stop=True))
        self.assertEqual(result['status'], 'generating')

    def test_images_still_loading_are_not_called_imageless(self):
        job = self.pending_job()
        result = self.poll_with(job, self.reply('Done', turn_complete=True, pending=1))
        self.assertEqual(result['status'], 'generating')

    def test_poll_js_derives_turn_completion_from_the_action_footer(self):
        # Captured live 2026-09-19: both an image turn and a text turn expose this button
        # only once the turn has finished, while a streaming turn does not.
        self.assertIn('copy-turn-action-button', runtime.POLL_JS)
        self.assertIn('turn_complete', runtime.POLL_JS)
        self.assertIn('DOCUMENT_POSITION_FOLLOWING', runtime.POLL_JS)


class ActionableErrors(fixtures.QueueContracts):
    """An agent must be able to tell 'rewrite your prompt' from 'a human must log in'."""

    def failed_with(self, code):
        job = self.submit('err-' + code, 'primary')
        self.pool.save_result(job['job_id'], {'status': 'failed', 'files': [],
                                              'error': {'code': code, 'message': 'fixture'}})
        return self.pool.get(job['job_id'])

    def test_a_refusal_is_marked_for_the_agent(self):
        for code in ('no_image_in_reply', 'generation_failed', 'image_count_mismatch'):
            self.assertEqual(self.failed_with(code)['error']['actionable_by'], 'agent', code)

    def test_a_transient_browser_fault_is_marked_retry(self):
        for code in ('browser_timeout', 'browser_operation_failed', 'busy'):
            self.assertEqual(self.failed_with(code)['error']['actionable_by'], 'retry', code)

    def test_an_account_problem_is_marked_for_a_human(self):
        for code in ('login_required', 'browser_challenge', 'conversation_changed'):
            self.assertEqual(self.failed_with(code)['error']['actionable_by'], 'human', code)

    def test_an_unknown_code_never_claims_the_agent_can_fix_it(self):
        self.assertEqual(self.failed_with('something_new')['error']['actionable_by'], 'human')


class SourceEncoding(unittest.TestCase):
    """Non-ASCII selectors and prompts must survive every edit of these files.

    PowerShell 5.1 reads with the ANSI code page unless told otherwise, so round-tripping
    a UTF-8 source file through `Get-Content -Raw` and writing it back as UTF-8 double
    encodes every Chinese character. The damage is invisible in a diff viewer that renders
    the mojibake, and it silently breaks the Chinese ChatGPT selectors and the multi-image
    instruction block. Byte-level assertions are the only reliable detection.
    """

    SOURCES = ('web_images.py', 'image_pool.py', 'image_worker.py', 'image_service.py', 'chatgpt_account.py')
    # Sequences that only appear when UTF-8 has been decoded as ANSI and re-encoded.
    MOJIBAKE = ('Ã§', 'Ã¦', 'Ã¥', 'â', 'Â ')

    def source(self, name):
        return (Path(__file__).with_name(name)).read_bytes()

    def test_no_source_file_carries_a_byte_order_mark(self):
        for name in self.SOURCES:
            self.assertFalse(self.source(name).startswith(b'\xef\xbb\xbf'), name)

    def test_no_source_file_contains_double_encoded_text(self):
        for name in self.SOURCES:
            text = self.source(name).decode('utf-8')
            for marker in self.MOJIBAKE:
                self.assertNotIn(marker, text, '%s looks double encoded' % name)

    def test_the_chinese_browser_selectors_are_still_readable(self):
        text = self.source('web_images.py').decode('utf-8')
        for phrase in ('图片交付数量要求', '生成的图片',
                       '创建图片', '保存', '下载图片',
                       '登录', '无法生成'):
            self.assertIn(phrase, text)


class Continuity(fixtures.QueueContracts):
    """A follow-up belongs in the original conversation, on the login that owns it."""

    CONVERSATION = 'https://chatgpt.com/c/test-thread-alpha'

    def finished(self, lane='primary', conversation=None, status='complete'):
        job = self.submit('parent-' + lane, lane)
        self.pool.claim(lane)
        self.pool.save_result(job['job_id'], {'status': status, 'files': [],
                                              'conversation_url': self.CONVERSATION if conversation is None else conversation})
        return job

    def follow_up(self, parent, key='follow', account='auto'):
        return self.pool.submit(prompt='Keep the layout, use a darker palette.', request_id=key,
                                output_dir=str(self.root / 'out'), account_id=account,
                                continue_from=parent['job_id'])

    def payload(self, job):
        import json as _json
        return _json.loads(self.pool.row(job['job_id'])['payload'])

    def test_a_follow_up_carries_the_parent_conversation(self):
        child = self.follow_up(self.finished())
        self.assertEqual(self.payload(child)['conversation_url'], self.CONVERSATION)
        self.assertTrue(child['continues_conversation'])

    def test_a_follow_up_is_pinned_to_the_login_that_owns_the_conversation(self):
        child = self.follow_up(self.finished('secondary'))
        self.assertEqual(self.pool.row(child['job_id'])['preferred'], 'secondary')

    def test_any_lane_of_that_login_may_run_the_follow_up(self):
        # Conversations are account-scoped, not profile-scoped: proven on the live site.
        auth = Path(self.pool.account('primary')['data_dir']) / 'auth-state.json'
        auth.parent.mkdir(parents=True, exist_ok=True)
        auth.write_text('{"cookies":[]}', encoding='utf-8')
        lanes = self.pool.scale_account('primary', 2)['lanes']
        for lane in lanes:
            self.pool.account_state(lane, 'authenticated', True)
            self.runtimes.setdefault(lane, fixtures.FakeRuntime(self.pool.account(lane)['data_dir']))
        parent = self.finished('primary')
        self.pool.save_result(parent['job_id'], {'status': 'complete', 'files': [],
                                                 'conversation_url': self.CONVERSATION})
        child = self.follow_up(parent)
        claimed = self.pool.claim(lanes[1])
        self.assertEqual(claimed['id'], child['job_id'])

    def test_a_conversation_takes_one_writer_at_a_time(self):
        parent = self.finished()
        first = self.follow_up(parent, 'follow-a')
        second = self.follow_up(parent, 'follow-b')
        self.pool.claim('primary')
        self.assertEqual(self.pool.row(first['job_id'])['status'], 'dispatching')
        self.assertIsNone(self.pool.claim('secondary'))
        self.assertEqual(self.pool.row(second['job_id'])['status'], 'queued')

    def test_an_unrelated_job_is_not_blocked_by_a_busy_thread(self):
        parent = self.finished()
        self.follow_up(parent, 'follow-a')
        other = self.submit('unrelated', 'auto')
        self.pool.claim('primary')
        claimed = self.pool.claim('secondary')
        self.assertEqual(claimed['id'], other['job_id'])

    def test_continuing_a_job_without_a_conversation_is_refused(self):
        parent = self.finished(conversation='')
        with self.assertRaises(w.ImageError) as caught:
            self.follow_up(parent)
        self.assertEqual(caught.exception.code, 'continue_without_conversation')

    def test_continuing_a_job_that_is_still_running_is_refused(self):
        parent = self.finished(status='generating')
        with self.assertRaises(w.ImageError) as caught:
            self.follow_up(parent)
        self.assertEqual(caught.exception.code, 'continue_while_active')

    def test_pinning_the_wrong_login_for_a_conversation_is_refused(self):
        parent = self.finished('primary')
        with self.assertRaises(w.ImageError) as caught:
            self.follow_up(parent, account='secondary')
        self.assertEqual(caught.exception.code, 'continue_wrong_account')

    def test_an_unknown_parent_is_refused(self):
        with self.assertRaises(w.ImageError) as caught:
            self.pool.submit(prompt='x', request_id='orphan', output_dir=str(self.root / 'out'),
                             continue_from='20260101-deadbeefdead')
        self.assertEqual(caught.exception.code, 'job_not_found')

    def test_the_same_prompt_in_two_conversations_is_two_requests(self):
        one = self.finished('primary')
        two = self.finished('secondary', conversation='https://chatgpt.com/c/aaaaaaaa-bbbb-cccc-dddd')
        a = self.follow_up(one, 'shared-key')
        with self.assertRaises(w.ImageError) as caught:
            self.follow_up(two, 'shared-key')
        self.assertEqual(caught.exception.code, 'request_id_conflict')
        self.assertTrue(a['continues_conversation'])


class ContinuityRuntime(contracts.Contracts):
    """start() must join the recorded conversation rather than open a new chat."""

    CONVERSATION = 'https://chatgpt.com/c/test-thread-alpha'

    def run_start(self, conversation_url):
        # The composer wait now also returns how many user messages are already there.
        script = []

        def js(source, **kwargs):
            script.append(source)
            if 'composer-plus-btn' in source and 'waitFor' in source:
                return 2 if conversation_url else 0
            if 'send-button' in source:
                return {'url': conversation_url or 'https://chatgpt.com/c/new-one'}
            return True

        with patch.object(runtime, 'ensure_browser'), patch.object(runtime, 'cli') as browser, \
             patch.object(runtime, 'set_visible', return_value=True), \
             patch.object(runtime, 'run_js', side_effect=js):
            result = runtime.start(prompt='darker palette', output_dir=str(self.data),
                                   conversation_url=conversation_url)
        return result, script, [list(call.args[0]) for call in browser.call_args_list]

    def test_a_follow_up_navigates_to_the_recorded_conversation(self):
        result, _, calls = self.run_start(self.CONVERSATION)
        self.assertIn(['goto', self.CONVERSATION], calls)
        self.assertEqual(result['conversation_url'], self.CONVERSATION)
        self.assertEqual(result['status'], 'generating')

    def test_a_fresh_request_still_opens_a_new_chat(self):
        _, _, calls = self.run_start('')
        self.assertIn(['goto', 'https://chatgpt.com/'], calls)

    def test_the_follow_up_waits_for_a_new_user_message_not_any(self):
        _, script, _ = self.run_start(self.CONVERSATION)
        submit = [s for s in script if 'send-button' in s][0]
        self.assertIn('waitForFunction', submit)
        self.assertIn('> n', submit)
        self.assertIn('2, {timeout', submit)

    def test_a_follow_up_into_a_bad_link_is_refused(self):
        with self.assertRaises(runtime.ImageError) as caught:
            runtime.validate_request(prompt='x', conversation_url='https://example.com/c/abc')
        self.assertEqual(caught.exception.code, 'invalid_conversation')

    def test_the_conversation_is_part_of_request_identity(self):
        _, one = runtime.validate_request(prompt='x', conversation_url=self.CONVERSATION)
        _, two = runtime.validate_request(prompt='x', conversation_url='')
        self.assertNotEqual(one, two)


class ReviewFindings(fixtures.QueueContracts):
    """Defects found reviewing this session's own changes; each one shipped once."""

    CONVERSATION = 'https://chatgpt.com/c/aaaa-bbbb-cccc-dddd'

    def parent(self, lane='primary'):
        job = self.submit('parent', lane)
        self.pool.claim(lane)
        self.pool.save_result(job['job_id'], {'status': 'complete', 'files': [],
                                              'conversation_url': self.CONVERSATION})
        return job

    def entry(self, key, **extra):
        return dict({'prompt': 'draw ' + key, 'request_id': key,
                     'output_dir': str(self.root / 'out')}, **extra)

    def test_a_batch_entry_may_continue_a_conversation(self):
        # validate_request cannot accept continue_from, so the batch used to raise a
        # bare TypeError and surface as an opaque service_error.
        parent = self.parent()
        result = self.pool.submit_many([self.entry('follow', continue_from=parent['job_id'])])
        self.assertEqual(result['submitted'], 1)
        self.assertTrue(self.pool.get(result['job_ids'][0])['continues_conversation'])

    def test_a_parked_follow_up_keeps_holding_its_conversation(self):
        # Parking frees the lane, but the job still intends to write to that thread.
        parent = self.parent()
        first = self.pool.submit(**self.entry('f1', continue_from=parent['job_id']))
        second = self.pool.submit(**self.entry('f2', continue_from=parent['job_id']))
        self.pool.claim('primary')
        self.pool.save_result(first['job_id'], {'status': 'needs_attention', 'files': [],
                                                'error': {'code': 'browser_timeout', 'message': 'fixture'}})
        self.pool.park(first['job_id'])
        self.assertEqual(self.pool.row(first['job_id'])['status'], PARKED)
        self.assertIsNone(self.pool.claim('primary'))
        self.assertEqual(self.pool.row(second['job_id'])['status'], 'queued')

    def test_a_parked_follow_up_blocks_only_its_own_thread(self):
        parent = self.parent()
        first = self.pool.submit(**self.entry('f1', continue_from=parent['job_id']))
        unrelated = self.pool.submit(**self.entry('fresh'))
        self.pool.claim('primary')
        self.pool.save_result(first['job_id'], {'status': 'needs_attention', 'files': [],
                                                'error': {'code': 'browser_timeout', 'message': 'fixture'}})
        self.pool.park(first['job_id'])
        self.assertEqual(self.pool.claim('primary')['id'], unrelated['job_id'])

    def test_a_batch_that_conflicts_on_its_last_entry_queues_nothing(self):
        # The caller is told the batch failed, so none of it may be running.
        self.pool.submit(**self.entry('dup'))
        batch = [self.entry('n1'), self.entry('n2'),
                 dict(self.entry('dup'), prompt='completely different text')]
        with self.assertRaises(w.ImageError) as caught:
            self.pool.submit_many(batch)
        self.assertEqual(caught.exception.code, 'request_id_conflict')
        self.assertEqual(self.pool.status()['jobs'].get('queued', 0), 1)

    def test_resending_an_identical_batch_still_reuses_every_job(self):
        first = self.pool.submit_many([self.entry('a'), self.entry('b')])
        again = self.pool.submit_many([self.entry('a'), self.entry('b')])
        self.assertEqual(first['job_ids'], again['job_ids'])
        self.assertEqual(again['reused'], 2)

    def test_one_damaged_row_never_fails_the_whole_batch_poll(self):
        good = self.pool.submit(**self.entry('good'))
        broken = self.pool.submit(**self.entry('broken'))
        with self.pool.connect() as db:
            db.execute("UPDATE jobs SET result='{not json' WHERE id=?", (broken['job_id'],))
        summary = asyncio.run(self.pool.poll_many_async([good['job_id'], broken['job_id']], 0))
        self.assertEqual(summary['total'], 2)
        damaged = [s for s in summary['jobs'] if s['job_id'] == broken['job_id']][0]
        self.assertEqual(damaged['error']['code'], 'job_state_unreadable')
        self.assertEqual([s for s in summary['jobs'] if s['job_id'] == good['job_id']][0]['status'], 'queued')


class PublicationHygiene(unittest.TestCase):
    """This repository is public; tracked files must not carry anyone's machine or chats.

    Learned the hard way preparing a push: a real conversation id had been pasted into
    a fixture, and .mcp.json had been overwritten with one developer's absolute paths.
    check_release.py catches both, but only if someone remembers to run it.
    """

    def tracked(self):
        import subprocess
        root = Path(__file__).resolve().parents[1]
        names = subprocess.run(['git', 'ls-files'], cwd=root, capture_output=True,
                               check=True).stdout.decode('utf-8').split()
        for name in names:
            data = (root / name).read_bytes()
            try:
                yield name, data.decode('utf-8')
            except UnicodeDecodeError:
                continue

    def test_no_tracked_file_carries_a_home_directory(self):
        import re
        for name, text in self.tracked():
            self.assertIsNone(re.search(r'[A-Za-z]:[\\/]Users[\\/][A-Za-z0-9_]', text.replace('\\\\', '\\')),
                              '%s contains a machine-specific home path' % name)

    def test_no_tracked_file_carries_a_real_conversation_id(self):
        import re
        # check_release.py rejects 30+ run of [a-f0-9-] after /c/; fixtures must stay short
        # and obviously fake so a real link can never hide among them.
        for name, text in self.tracked():
            self.assertIsNone(re.search(r'https://chatgpt\.com/c/[a-f0-9-]{30,}', text),
                              '%s looks like it contains a real conversation link' % name)


class QuietWindows(contracts.Contracts):
    """A lane must be invisible from the moment it opens, not from the moment it submits."""

    def open_path(self, **kwargs):
        """Drive ensure_browser through its cold-open branch, recording call order."""
        events = []

        def js(source, **_):
            if not events:
                events.append('probe')
                raise runtime.ImageError('browser_not_open', 'closed')
            if source in (runtime.STATUS_JS, runtime.SETTLE_JS):
                return {'logged_in': True, 'auth_state': 'authenticated'}
            return True

        def browser_cli(args, **_):
            events.append(args[0])
            return True

        def hide(value):
            events.append('hide' if value is False else 'show')
            return True

        with patch.object(runtime, 'cli', side_effect=browser_cli) as browser, \
             patch.object(runtime, 'run_js', side_effect=js), \
             patch.object(runtime, 'set_visible', side_effect=hide) as visible:
            state = runtime.ensure_browser(**kwargs)
        return state, events, visible

    def test_a_cold_open_hides_the_window_before_it_navigates(self):
        # Ordering is the property that matters: hiding after the navigation leaves the
        # window on screen for the whole page load, which is most of a cold open.
        state, events, visible = self.open_path()
        self.assertIn('open', events)
        self.assertIn('hide', events)
        self.assertLess(events.index('hide'), events.index('goto'))
        self.assertTrue(state['window_hidden'])

    def test_the_login_entry_point_still_shows_its_window(self):
        _, events, visible = self.open_path(require_login=False, hide_on_open=False)
        self.assertNotIn('hide', events)
        visible.assert_not_called()

    def test_failing_to_hide_never_fails_the_lane(self):
        script = []

        def js(source, **_):
            script.append(source)
            if len(script) == 1:
                raise runtime.ImageError('browser_not_open', 'closed')
            if 'waitForFunction' in source:
                return True
            return {'logged_in': True, 'auth_state': 'authenticated'}

        with patch.object(runtime, 'cli'), patch.object(runtime, 'run_js', side_effect=js), \
             patch.object(runtime, 'set_visible', side_effect=RuntimeError('no window')):
            state = runtime.ensure_browser()
        self.assertTrue(state['logged_in'])
        self.assertFalse(state['window_hidden'])

    def test_adopting_a_parked_conversation_leaves_nothing_on_screen(self):
        job = self.job()
        job.update(created_at=time.time(), conversation_url='https://chatgpt.com/c/abc-def')
        runtime.save_job(job)
        runtime.park(job['job_id'])
        with patch.object(runtime, 'ensure_browser'), patch.object(runtime, 'cli'), \
             patch.object(runtime, 'run_js'), patch.object(runtime, 'set_visible', return_value=True) as visible:
            runtime.adopt(job['job_id'])
        visible.assert_called_once_with(False)

    def test_the_window_lookup_retries_before_giving_up(self):
        # The title is published asynchronously; one miss used to leave a lane on screen.
        self.assertGreater(runtime.WINDOW_LOOKUP_TRIES, 1)
        import inspect
        source = inspect.getsource(runtime.set_visible)
        self.assertIn('WINDOW_LOOKUP_TRIES', source)
        self.assertIn('found.clear()', source)

    def test_the_browser_must_stay_headed(self):
        # Measured 2026-09-21 on a scratch profile seeded with a real login: opening
        # without --headed lands on Cloudflare's "Just a moment..." interstitial with
        # auth_state=browser_challenge, so the app is never reached. Invisibility comes
        # from hiding the window, not from running headless.
        import inspect
        source = inspect.getsource(runtime.ensure_browser)
        self.assertIn("'--headed'", source)
        self.assertIn('Cloudflare', source)


class OffscreenBirth(contracts.Contracts):
    """A lane window must never be on screen, not even for the first second."""

    def test_the_launch_position_is_off_screen(self):
        args = ' '.join(runtime.BROWSER_ARGS)
        self.assertIn('--window-position=%d,%d' % runtime.OFFSCREEN, args)
        self.assertLess(runtime.OFFSCREEN[0], -10000)
        self.assertLess(runtime.OFFSCREEN[1], -10000)

    def test_chrome_is_told_not_to_open_its_own_windows(self):
        args = ' '.join(runtime.BROWSER_ARGS)
        for switch in ('--hide-crash-restore-bubble', '--disable-session-crashed-bubble',
                       '--no-first-run', '--no-default-browser-check'):
            self.assertIn(switch, args)

    def test_every_lane_gets_the_config_before_its_browser_opens(self):
        import inspect
        source = inspect.getsource(runtime.ensure_browser)
        self.assertLess(source.index('ensure_cli_config()'), source.index("'open', 'about:blank'"))

    def test_the_config_is_written_next_to_the_profile(self):
        path = runtime.ensure_cli_config()
        self.assertEqual(path, self.data / '.playwright' / 'cli.config.json')
        written = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(written['browser']['launchOptions']['args'], runtime.BROWSER_ARGS)

    def test_showing_a_window_also_brings_it_back_on_screen(self):
        # Without this, image_open would "show" a window the user still cannot see.
        import inspect
        source = inspect.getsource(runtime.set_visible)
        self.assertIn('SetWindowPos', source)
        self.assertIn('ONSCREEN', source)
        self.assertGreater(runtime.ONSCREEN[0], 0)


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
