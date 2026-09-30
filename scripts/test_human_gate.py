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

    def test_challenged_lane_leaves_its_page_but_a_just_opened_page_stays(self):
        left = []
        for account in ('primary', 'secondary'):
            self.runtimes[account].leave_conversation = lambda account=account: left.append(account) or {'left': True}
        self.check('secondary')  # human gate pending: a challenge page left open keeps failing
        Worker(self.pool, 'secondary', self.runtimes['secondary']).leave_idle_conversation()
        self.assertEqual(left, ['secondary'])
        self.pool.control('primary', 'open')  # person asked to see this lane's own browser
        with self.pool.connect() as db:
            db.execute("UPDATE controls SET status='complete' WHERE account_id='primary'")
        Worker(self.pool, 'primary', self.runtimes['primary']).leave_idle_conversation()
        self.assertEqual(left, ['secondary'])
        with self.pool.connect() as db:
            db.execute("UPDATE controls SET updated=updated-? WHERE account_id='primary'", (31 * 60,))
        Worker(self.pool, 'primary', self.runtimes['primary']).leave_idle_conversation()
        self.assertEqual(left, ['secondary', 'primary'])

    def test_sibling_lanes_of_a_gated_login_take_no_new_work(self):
        self.check('primary')  # gate recorded for the whole primary login
        self.pool.submit(prompt='Draw a square.', output_dir=str(self.root / 'out'), request_id='sibling', account_id='auto')
        self.assertIsNone(self.pool.claim('primary-2'))
        claimed = self.pool.claim('secondary')
        self.assertEqual(claimed['request_id'] if 'request_id' in claimed else json.loads(claimed['payload'])['request_id'], 'sibling')

    def test_workers_wait_out_a_person_handoff_without_charging_jobs(self):
        runtime = self.runtimes['secondary']
        runtime.human_handoff = lambda: {'identity': 'alt', 'until': 9e15}
        self.pool.submit(prompt='Draw a square.', output_dir=str(self.root / 'out'), request_id='handoff-wait', account_id='secondary')
        worker = Worker(self.pool, 'secondary', runtime)
        worker.tick(); worker.tick()
        self.assertEqual(runtime.starts, 0)
        self.assertEqual(worker.delay, 5)
        runtime.human_handoff = lambda: None
        def handed(job_id):
            raise w.ImageError('human_handoff', 'fixture')
        runtime.poll = handed
        worker.tick(); worker.tick(); worker.tick()
        with self.pool.connect() as db:
            job = db.execute("SELECT status,failures FROM jobs WHERE request_id='handoff-wait'").fetchone()
        self.assertEqual(job['failures'], 0)
        self.assertNotEqual(job['status'], 'needs_attention')

    def test_open_that_hands_over_the_browser_keeps_the_gate(self):
        self.check('secondary')
        self.runtimes['secondary'].open_browser = lambda: {'visible': True, 'handoff': True, 'until': 1}
        self.pool.control('secondary', 'open')
        Worker(self.pool, 'secondary', self.runtimes['secondary']).tick()
        self.assertEqual(self.pool.account('secondary')['state'], 'browser_challenge')
        self.assertEqual([g['login_group'] for g in self.pool.status()['human_action']], ['secondary'])

    def test_person_check_is_passed_through_to_the_runtime(self):
        seen = []
        self.runtimes['primary'].status = lambda **kw: seen.append(kw) or {'logged_in': True, 'auth_state': 'authenticated'}
        self.pool.control('primary', 'check', person=True)
        Worker(self.pool, 'primary', self.runtimes['primary']).tick()
        self.assertEqual(seen, [{'person': True}])

    def test_hub_lane_finds_its_browser_root_and_reads_the_handoff_lease(self):
        from unittest.mock import patch
        hub = self.root / 'HubChrome'
        hub.mkdir()
        entry = self.root / 'images-secondary.cjs'
        entry.write_text('require("x").main(' + json.dumps({'id': 'images-secondary', 'identity': 'alt', 'root': str(hub)}) + ');', encoding='utf-8')
        with patch.object(w, 'CLI_ENTRY', entry):
            self.assertEqual(w.hub_root(), hub)
            self.assertIsNone(w.human_handoff())
            (hub / 'web-risk.json').write_text(json.dumps({'handoff': {'identity': 'alt', 'until': (time.time() + 60) * 1000}}), encoding='utf-8')
            self.assertEqual(w.human_handoff()['identity'], 'alt')
            (hub / 'web-risk.json').write_text(json.dumps({'handoff': {'identity': 'alt', 'until': (time.time() - 1) * 1000}}), encoding='utf-8')
            self.assertIsNone(w.human_handoff(), 'an expired lease never pauses lanes')
        with patch.object(w, 'CLI_ENTRY', self.root / 'playwright-cli.js'):
            self.assertIsNone(w.hub_root(), 'standalone lanes have no Hub browser')

    def test_hub_refusals_map_to_actionable_codes(self):
        from unittest.mock import patch
        import subprocess
        entry = self.root / 'entry.cjs'
        entry.write_text('x', encoding='utf-8')
        for category, code in (('Human handoff', 'human_handoff'), ('Site challenged', 'browser_challenge'), ('Timeout', 'browser_timeout')):
            done = subprocess.CompletedProcess([], 1, stdout=json.dumps({'isError': True, 'error': category}), stderr='')
            with patch.object(w, 'CLI_ENTRY', entry), patch.object(w.shutil, 'which', return_value='node.exe'), patch.object(w.subprocess, 'run', return_value=done):
                with self.assertRaises(w.ImageError) as caught:
                    w.cli(['goto', 'https://chatgpt.com/'])
            self.assertEqual(caught.exception.code, code)

    def test_a_check_during_a_handoff_does_not_take_the_lane_offline(self):
        def handed(**kw):
            raise w.ImageError('human_handoff', 'fixture')
        self.runtimes['primary'].status = handed
        self.pool.control('primary', 'check')
        Worker(self.pool, 'primary', self.runtimes['primary']).tick()
        account = self.pool.account('primary')
        self.assertEqual((account['state'], account['ready']), ('authenticated', 1))

    def test_only_a_hub_with_the_guard_gets_a_handoff_and_its_failures_are_not_hidden(self):
        from unittest.mock import patch
        core = self.root / 'core'; core.mkdir()
        entry = self.root / 'lane.cjs'
        entry.write_text('x(' + json.dumps({'root': str(self.root / 'hub'), 'hubCore': str(core)}) + ')', encoding='utf-8')
        with patch.object(w, 'CLI_ENTRY', entry):
            self.assertFalse(w.hub_has_guard(), 'older Hub: keep the previous open')
        (core / 'web-risk-guard.js').write_text('//', encoding='utf-8')
        w._hub_guard_of.cache_clear()
        with patch.object(w, 'CLI_ENTRY', entry), patch.object(w, 'locked'),              patch.object(w, 'cli', side_effect=w.ImageError('browser_operation_failed', 'real failure')),              patch.object(w, 'ensure_browser') as lane_tab:
            self.assertTrue(w.hub_has_guard())
            with self.assertRaises(w.ImageError):
                w.open_browser()
            lane_tab.assert_not_called()  # never falls back to showing the automated lane tab

    def test_toast_script_escapes_markup_and_quotes(self):
        script = human_gate.toast_script("a<b>&'c'", 'x"y')
        self.assertIn('a&lt;b&gt;&amp;', script)
        self.assertIn("''c''", script)
        self.assertNotIn('<b>', script)


if __name__ == '__main__':
    unittest.main(verbosity=2)
