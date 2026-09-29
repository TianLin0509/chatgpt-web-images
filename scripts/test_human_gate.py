"""Human-gate alerts and worker-error contracts; no browser, network or real toast."""
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest

import web_images as w
import human_gate
from image_pool import Pool
from image_worker import Worker, mark_worker_error
from test_image_pool import FakeRuntime


class ChallengeRuntime(FakeRuntime):
    def __init__(self, data):
        super().__init__(data)
        self.auth_state = 'browser_challenge'

    def status(self):
        return {'logged_in': self.auth_state == 'authenticated', 'auth_state': self.auth_state}

    open_browser = status


class HumanGateContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.toasts, self.egress = [], {'ip': '203.0.113.7', 'colo': 'LAX', 'loc': 'US', 'at': time.time()}
        self.pool = Pool(self.root / 'pool')
        self.pool.alerts = human_gate.Alerts(self.pool.root, notify=True, fetch=lambda: dict(self.egress),
                                             toast=lambda title, body: self.toasts.append((title, body)), background=False)
        self.runtimes = {}
        for account, group in (('primary', ''), ('primary-2', 'primary'), ('secondary', '')):
            data, config = self.root / account / 'browser', self.root / account / 'config'
            w.write_json(config / 'settings.json', {'data_dir': str(data)})
            self.pool.add_account(account, str(data), str(config), enabled=True)
            if group:
                with self.pool.connect() as db:
                    db.execute('UPDATE accounts SET login_group=? WHERE id=?', (group, account))
            self.pool.account_state(account, 'authenticated', True)
            self.runtimes[account] = ChallengeRuntime(data)

    def check(self, account):
        self.pool.control(account, 'check')
        Worker(self.pool, account, self.runtimes[account]).tick()

    def test_gate_codes_from_state_or_error(self):
        self.assertEqual(human_gate.gate_code('browser_challenge'), 'browser_challenge')
        self.assertEqual(human_gate.gate_code('needs_attention', {'code': 'credential_required'}), 'credential_required')
        self.assertEqual(human_gate.gate_code('browser_unhealthy', json.dumps({'code': 'login_required'})), 'login_required')
        self.assertIsNone(human_gate.gate_code('browser_unhealthy', {'code': 'browser_timeout'}))
        self.assertIsNone(human_gate.gate_code('authenticated'))

    def test_challenge_is_announced_once_per_login_and_listed_in_status(self):
        self.check('primary')
        self.check('primary-2')  # same login: recorded, not announced again
        self.assertEqual(len(self.toasts), 1)
        self.assertIn('primary', self.toasts[0][1])
        self.assertIn('Cloudflare', self.toasts[0][1])
        status = self.pool.status()
        self.assertEqual([g['login_group'] for g in status['human_action']], ['primary'])
        gate = status['human_action'][0]
        self.assertEqual((gate['code'], gate['account_id']), ('browser_challenge', 'primary'))
        self.assertEqual(gate['egress']['ip'], '203.0.113.7')
        self.assertIn('image_open(primary)', gate['next_action'])

    def test_authentication_clears_gate_and_remembers_working_exit(self):
        self.check('primary')
        self.runtimes['primary'].auth_state = 'authenticated'
        self.check('primary')
        status = self.pool.status()
        self.assertEqual(status['human_action'], [])
        self.assertEqual(status['egress_last_ok']['ip'], '203.0.113.7')

    def test_changed_exit_is_reported_with_the_gate(self):
        human_gate.GateBook(self.pool.root).record_ok_egress({'ip': '198.51.100.1', 'at': time.time()})
        self.check('secondary')
        gate = self.pool.status()['human_action'][0]
        self.assertTrue(gate['egress_changed'])
        self.assertIn('198.51.100.1', self.toasts[0][1])

    def test_unhandled_gate_is_reannounced_only_after_interval(self):
        book = human_gate.GateBook(self.pool.root)
        start = time.time()
        self.assertTrue(book.enter('g', 'a', 'browser_challenge', now=start))
        self.assertFalse(book.enter('g', 'a', 'browser_challenge', now=start + 60))
        self.assertTrue(book.enter('g', 'a', 'browser_challenge', now=start + human_gate.REALERT_SECONDS + 1))
        self.assertTrue(book.enter('g', 'a', 'login_required', now=start + human_gate.REALERT_SECONDS + 2))

    def test_job_error_gate_is_announced(self):
        runtime = self.runtimes['secondary']
        def challenged(job_id):
            raise w.ImageError('browser_challenge', 'Fixture challenge')
        runtime.poll = challenged
        self.pool.submit(prompt='Draw a square.', output_dir=str(self.root / 'out'), request_id='gate-job', account_id='secondary')
        worker = Worker(self.pool, 'secondary', runtime)
        worker.tick()  # dispatch
        worker.tick()  # control turn yields; poll raises the gate
        worker.tick()
        self.assertEqual(self.pool.account('secondary')['state'], 'browser_challenge')
        self.assertEqual(len(self.toasts), 1)

    def test_busy_database_keeps_lane_ready(self):
        busy = sqlite3.OperationalError('database is locked')
        busy.sqlite_errorname = 'SQLITE_BUSY'
        from image_worker import error_value
        self.assertFalse(mark_worker_error(self.pool, 'primary', error_value(busy)))
        account = self.pool.account('primary')
        self.assertEqual((account['state'], account['ready']), ('authenticated', 1))

    def test_worker_error_never_overwrites_a_human_gate(self):
        self.check('secondary')
        self.assertFalse(mark_worker_error(self.pool, 'secondary', {'code': 'worker_error', 'message': 'KeyError'}))
        self.assertEqual(self.pool.account('secondary')['state'], 'browser_challenge')
        self.assertTrue(mark_worker_error(self.pool, 'primary', {'code': 'worker_error', 'message': 'KeyError'}))
        self.assertEqual(self.pool.account('primary')['state'], 'worker_error')

    def test_alert_failure_never_blocks_state_write(self):
        def broken():
            raise RuntimeError('fixture')
        self.pool.alerts.fetch = broken
        self.check('primary')
        self.assertEqual(self.pool.account('primary')['state'], 'browser_challenge')

    def test_pool_without_alerts_writes_no_gate_file(self):
        pool = Pool(self.root / 'quiet')
        self.assertIsNone(pool.alerts)
        self.assertEqual(pool.status()['human_action'], [])

    def test_toast_script_escapes_markup_and_quotes(self):
        script = human_gate.toast_script("a<b>&'c'", 'x"y')
        self.assertIn('a&lt;b&gt;&amp;', script)
        self.assertIn("''c''", script)
        self.assertNotIn('<b>', script)


if __name__ == '__main__':
    unittest.main(verbosity=2)
