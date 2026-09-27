import concurrent.futures
import time
import unittest
from unittest.mock import patch
import test_image_pool as fixtures
import web_images as w


class RateLimits(unittest.TestCase):
    setUp=fixtures.QueueContracts.setUp
    submit=fixtures.QueueContracts.submit
    worker=fixtures.QueueContracts.worker

    def share_login(self):
        with self.pool.connect() as db:db.execute("UPDATE accounts SET login_group='primary' WHERE id='secondary'")

    def test_same_login_cools_together_other_login_keeps_working(self):
        error=self.pool.defer_rate_limit('primary')
        self.assertEqual(error['code'],'rate_limited')
        self.assertTrue(self.pool.account('secondary')['ready'])
        self.share_login();self.pool.defer_rate_limit('primary')
        self.assertFalse(self.pool.account('secondary')['ready'])

    def test_concurrent_429_reports_do_not_multiply_wait_or_strikes(self):
        self.share_login()
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as e:
            values=list(e.map(self.pool.defer_rate_limit,['primary','secondary']*4))
        self.assertEqual(len({v['retry_at'] for v in values}),1)
        self.assertEqual(self.pool.cooldown('primary')['strikes'],1)

    def test_active_job_and_request_identity_survive_cooling_without_browser_calls(self):
        job=self.submit('rate-active','primary');worker=self.worker('primary');worker.tick()
        before=self.pool.row(job['job_id']);runtime=self.runtimes['primary']
        self.pool.defer_rate_limit('primary')
        with patch.object(runtime,'status') as status,patch.object(runtime,'poll') as poll:
            for _ in range(4):worker.tick()
            status.assert_not_called();poll.assert_not_called()
        after=self.pool.row(job['job_id'])
        self.assertEqual(before['request_hash'],after['request_hash']);self.assertEqual(before['runtime_id'],after['runtime_id'])
        self.assertEqual(runtime.starts,1);self.assertEqual(self.pool.get(job['job_id'])['next_action'],'wait_for_account_cooldown')

    def test_explicit_check_during_cooling_reports_wait_without_navigation(self):
        self.pool.defer_rate_limit('primary');self.pool.control('primary','check')
        with patch.object(self.runtimes['primary'],'status') as status:self.worker('primary').tick();status.assert_not_called()
        result=self.pool.status()['accounts'][0]['last_control']['result']
        self.assertEqual(result['auth_state'],'rate_limited');self.assertGreater(result['retry_at'],time.time())

    def test_only_real_job_can_prove_recovery_and_other_login_state_is_not_forged(self):
        self.share_login();self.pool.defer_rate_limit('primary')
        with self.pool.connect() as db:db.execute('UPDATE login_cooldowns SET retry_after=0')
        self.assertTrue(self.pool.acquire_rate_probe('primary'));self.assertFalse(self.pool.acquire_rate_probe('secondary'))
        self.pool.control('primary','check');self.worker('primary').tick()
        self.assertIsNotNone(self.pool.cooldown('primary'))
        self.submit('real-recovery','primary');worker=self.worker('primary');worker.tick()
        self.assertIsNotNone(self.pool.cooldown('primary'))
        worker.tick()
        self.assertIsNone(self.pool.cooldown('primary'));self.assertTrue(self.pool.account('primary')['ready'])
        self.assertFalse(self.pool.account('secondary')['ready'])

    def test_rejected_real_probe_preserves_request_and_increases_backoff(self):
        job=self.submit('real-429','primary');worker=self.worker('primary');worker.tick()
        self.pool.defer_rate_limit('primary')
        with self.pool.connect() as db:db.execute('UPDATE login_cooldowns SET retry_after=0')
        worker.recoveries=99  # provider backoff is independent of browser-repair attempts
        with patch.object(self.runtimes['primary'],'poll',side_effect=w.ImageError('rate_limited','real task fixture')),patch.object(self.runtimes['primary'],'status') as status:
            worker.tick();status.assert_not_called()
        cooldown=self.pool.cooldown('primary')
        self.assertEqual(cooldown['strikes'],2);self.assertGreater(cooldown['retry_after']-time.time(),850)
        self.assertEqual(self.pool.row(job['job_id'])['failures'],0)
        self.assertEqual(self.runtimes['primary'].starts,1)

    def test_queue_claim_cannot_bypass_the_half_open_lease(self):
        self.share_login();self.submit('leased-recovery','primary');self.pool.defer_rate_limit('primary')
        with self.pool.connect() as db:db.execute('UPDATE login_cooldowns SET retry_after=0')
        self.pool.acquire_rate_probe('secondary')
        self.assertIsNone(self.pool.claim('primary',rate_probe=True))

    def test_adopting_a_parked_job_waits_for_actual_poll_before_clearing(self):
        job=self.submit('parked-proof','primary');worker=self.worker('primary');worker.tick();worker.park(self.pool.row(job['job_id']))
        self.pool.defer_rate_limit('primary')
        with self.pool.connect() as db:
            db.execute('UPDATE login_cooldowns SET retry_after=0')
            db.execute('UPDATE jobs SET retry_after=0')
        worker.tick();self.assertIsNotNone(self.pool.cooldown('primary'))
        worker.tick();self.assertIsNone(self.pool.cooldown('primary'))
        self.assertEqual(self.pool.get(job['job_id'])['status'],'complete')

    def test_unreadable_conversation_does_not_prove_recovery(self):
        job=self.submit('unreadable-proof','primary');worker=self.worker('primary');worker.tick();self.pool.defer_rate_limit('primary')
        with self.pool.connect() as db:db.execute('UPDATE login_cooldowns SET retry_after=0')
        with patch.object(self.runtimes['primary'],'poll',return_value={'status':'generating','operation_stage':'read_conversation','files':[]}):worker.tick()
        self.assertIsNotNone(self.pool.cooldown('primary'))

    def test_provider_limit_does_not_consume_parked_job_recovery_budget(self):
        job=self.submit('park-budget','primary');worker=self.worker('primary');worker.tick();worker.park(self.pool.row(job['job_id']))
        count=self.pool.row(job['job_id'])['park_count'];self.pool.defer_rate_limit('primary')
        with self.pool.connect() as db:
            db.execute('UPDATE login_cooldowns SET retry_after=0');db.execute('UPDATE jobs SET retry_after=0')
        with patch.object(self.runtimes['primary'],'adopt',side_effect=w.ImageError('rate_limited','still cooling')):worker.tick()
        self.assertEqual(self.pool.row(job['job_id'])['park_count'],count)
        self.assertEqual(self.pool.cooldown('primary')['strikes'],2)

    def test_live_foreground_job_takes_priority_over_background_recovery(self):
        self.share_login();self.submit('foreground-probe','secondary');self.pool.claim('secondary')
        self.pool.heartbeat('primary');self.pool.heartbeat('secondary');self.pool.defer_rate_limit('primary')
        with self.pool.connect() as db:db.execute('UPDATE login_cooldowns SET retry_after=0')
        self.assertFalse(self.pool.acquire_rate_probe('primary'))
        self.assertTrue(self.pool.acquire_rate_probe('secondary'))

    def test_accepted_prompt_keeps_its_probe_exclusive_until_an_original_is_saved(self):
        self.share_login();self.submit('complete-probe','primary');self.pool.defer_rate_limit('primary')
        with self.pool.connect() as db:db.execute('UPDATE login_cooldowns SET retry_after=0')
        worker=self.worker('primary');worker.tick()
        self.assertIsNotNone(self.pool.cooldown('primary'))
        self.assertFalse(self.pool.acquire_rate_probe('secondary'))
        worker.tick();self.assertIsNone(self.pool.cooldown('primary'))

    def test_second_rate_limit_after_cooldown_uses_longer_wait(self):
        first=self.pool.defer_rate_limit('primary')
        with self.pool.connect() as db:db.execute('UPDATE login_cooldowns SET retry_after=0')
        second=self.pool.defer_rate_limit('primary')
        self.assertGreater(second['retry_at']-time.time(),850)
        self.assertEqual(self.pool.cooldown('primary')['strikes'],2)

    def test_runtime_rate_limit_blocks_all_lanes_without_resubmission(self):
        job=self.submit('runtime-rate','primary');worker=self.worker('primary');worker.tick()
        with patch.object(self.runtimes['primary'],'poll',side_effect=w.ImageError('rate_limited','fixture 429')):worker.tick()
        self.assertEqual(self.pool.account('primary')['state'],'rate_limited')
        self.assertEqual(self.runtimes['primary'].starts,1)
        self.assertEqual(self.pool.get(job['job_id'])['error']['code'],'rate_limited')

    def test_queued_and_parked_batches_report_wait_instead_of_delivered(self):
        queued=self.submit('queued-rate','primary');self.pool.defer_rate_limit('primary')
        self.assertEqual(self.pool.get(queued['job_id'])['next_action'],'wait_for_account_cooldown')
        batch=self.pool.batch_summary([{'job_id':'parked','status':'parked','ok':True,'downloaded_count':0,'next_action':'poll_same_job'}])
        self.assertEqual(batch['next_action'],'poll_same_job_ids');self.assertEqual(batch['files'],[])

    def test_active_lane_cannot_poll_while_another_lane_owns_expired_cooldown_probe(self):
        self.share_login();self.submit('single-probe','secondary');worker=self.worker('secondary');worker.tick()
        self.pool.defer_rate_limit('primary')
        with self.pool.connect() as db:db.execute('UPDATE login_cooldowns SET retry_after=0')
        self.assertTrue(self.pool.acquire_rate_probe('primary'))
        with patch.object(self.runtimes['secondary'],'poll') as poll:
            worker.tick();poll.assert_not_called()


if __name__=='__main__':unittest.main()
