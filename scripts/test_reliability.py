"""Regressions for observed failures and multi-client scheduling, no real accounts."""
import asyncio
import concurrent.futures
import json
import time
import unittest
from unittest.mock import patch

import web_images as w
from image_pool import Pool
import test_image_pool as fixtures


class Reliability(fixtures.QueueContracts):
    def test_preparation_failure_does_not_feed_broken_account_more_jobs(self):
        first = self.submit('first', 'primary')
        second = self.submit('second', 'primary')
        runtime = self.runtimes['primary']
        with patch.object(runtime, 'start', return_value={
            'job_id': '20260918-000000abcdef', 'status': 'preparation_failed',
            'error': {'code': 'browser_operation_failed', 'message': 'fixture'}, 'files': []}):
            worker = self.worker('primary')
            worker.tick()
            self.assertFalse(self.pool.account('primary')['ready'])
            worker.tick()
            self.assertEqual(self.pool.row(second['job_id'])['status'], 'queued')

    def test_concurrent_identical_controls_coalesce(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as executor:
            controls = list(executor.map(lambda _: self.pool.control('primary', 'check'), range(30)))
        self.assertEqual(len({c['control_id'] for c in controls}), 1)

    def test_control_flood_cannot_starve_active_job(self):
        job = self.submit('fairness', 'primary')
        worker = self.worker('primary')
        worker.tick()
        for _ in range(6):
            self.pool.control('primary', 'check')
            worker.tick()
        self.assertEqual(self.pool.row(job['job_id'])['status'], 'complete')

    def test_account_switch_rejected_while_job_owns_browser(self):
        self.submit('owned', 'primary')
        self.pool.claim('primary')
        with self.assertRaises(w.ImageError) as caught:
            self.pool.control('primary', 'select', account_name='other')
        self.assertEqual(caught.exception.code, 'account_busy')

    def test_stop_marker_prevents_worker_spawn(self):
        for account in ('primary', 'secondary'):
            (self.pool.root / ('stop-' + account)).touch()
        with patch('image_pool.subprocess.Popen') as spawn:
            self.pool.ensure_workers()
            self.assertEqual(spawn.call_count, 0)

    def test_many_clients_launch_at_most_one_worker_per_account(self):
        with patch('image_pool.subprocess.Popen') as spawn:
            spawn.return_value.pid = 12345
            with concurrent.futures.ThreadPoolExecutor(max_workers=12) as executor:
                list(executor.map(lambda _: self.pool.ensure_workers(), range(24)))
            self.assertEqual(spawn.call_count, 2)

    def test_poll_default_omits_internal_conversation_and_hashes(self):
        job = self.submit('slim')
        self.pool.save_result(job['job_id'], {'status': 'generating',
            'conversation_url': 'https://chatgpt.com/c/private', 'prompt_sha256': 'private'})
        result = self.pool.get(job['job_id'])
        self.assertNotIn('conversation_url', result)
        self.assertNotIn('prompt_sha256', result)

    def test_queue_explains_blocking_account(self):
        active = self.submit('blocked', 'primary')
        waiting = self.submit('waiting', 'primary')
        self.pool.claim('primary')
        self.pool.save_result(active['job_id'], {'status': 'needs_attention', 'error': {'code':'fixture'}})
        result = self.pool.get(waiting['job_id'])
        self.assertEqual(result['blocking_accounts'][0]['active_job_id'], active['job_id'])

    def test_async_waiters_do_not_starve_status_executor(self):
        job = self.submit('waiters')
        async def check():
            loop = asyncio.get_running_loop()
            executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)
            loop.set_default_executor(executor)
            tasks = [asyncio.create_task(self.pool.poll_async(job['job_id'], 2)) for _ in range(40)]
            try:
                await asyncio.sleep(.2)
                started = time.monotonic()
                result = await asyncio.wait_for(asyncio.to_thread(self.pool.status), timeout=1)
                self.assertTrue(result['ok'])
                self.assertLess(time.monotonic()-started, 1)
            finally:
                for task in tasks: task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        asyncio.run(check())

    def test_live_heartbeat_does_not_hide_stalled_operation(self):
        self.pool.heartbeat('primary')
        import os
        w.write_json(self.pool.root/'worker-primary-health.json',
                     {'pid':os.getpid(),'version':'0.3.1','tick_started':time.time()-400,'tick_finished':0})
        account = self.pool.status()['accounts'][0]
        self.assertTrue(account['worker_alive'])
        self.assertTrue(account['worker_stalled'])

    def test_uncertain_cancel_stops_after_bounded_failures(self):
        job = self.submit('cancel-error', 'primary')
        worker = self.worker('primary')
        worker.tick()
        self.pool.cancel(job['job_id'])
        with patch.object(self.runtimes['primary'], 'cancel', side_effect=w.ImageError('browser_timeout','fixture')) as cancel:
            for _ in range(8): worker.tick()
        self.assertEqual(cancel.call_count, 3)
        self.assertEqual(self.pool.row(job['job_id'])['status'], 'needs_attention')


if __name__ == '__main__':
    unittest.main(verbosity=2)
