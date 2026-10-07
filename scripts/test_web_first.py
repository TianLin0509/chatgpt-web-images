"""Web-first routing and gate revisits (0.7.33). No browser, Codex, network or real Hub."""
import json
from pathlib import Path
import tempfile
import time
import unittest

import web_images as w
import human_gate
import gate_revisit
from codex_lane import CodexLane
from image_pool import CODEX_LANE, Pool, fallback_settings
from image_worker import Worker
from test_image_pool import FakeRuntime


class SwitchRuntime(FakeRuntime):
    """A login that answers checks with whatever auth_state the test sets."""

    def __init__(self, data):
        super().__init__(data)
        self.auth_state = 'browser_challenge'
        self.checks = 0

    def status(self):
        self.checks += 1
        return {'logged_in': self.auth_state == 'authenticated', 'auth_state': self.auth_state}

    open_browser = status


class WebFirstContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.pool = Pool(self.root / 'pool')
        self.pool.alerts = human_gate.Alerts(self.pool.root, notify=False, fetch=lambda: None, background=False)
        self.home = self.root / 'codex-home'
        self.home.mkdir()
        (self.home / 'auth.json').write_text(json.dumps({'auth_mode': 'chatgpt'}), encoding='utf-8')
        w.write_json(self.pool.root / 'codex-fallback.json', {'enabled': True, 'codex_home': str(self.home),
                                                             'parallel': 2, 'queue_wait_seconds': 90})
        w.write_json(self.pool.root / 'codex-lane-health.json', {'beat': time.time(), 'pid': 1})
        self.hub = self.root / 'HubChrome'
        self.hub.mkdir()
        self.runtimes = {}
        for account, group, identity in (('primary', '', 'main'), ('primary-2', 'primary', 'main'), ('secondary', '', 'alt')):
            data, config = self.root / account / 'browser', self.root / account / 'config'
            entry = self.root / 'entrypoints' / ('images-' + account + '.cjs')
            entry.parent.mkdir(exist_ok=True)
            entry.write_text("'use strict';\nrequire(\"x\").main(%s);\n" % json.dumps(
                {'id': 'images-' + account, 'tool': 'images', 'identity': identity, 'root': str(self.hub)}), encoding='utf-8')
            w.write_json(config / 'settings.json', {'data_dir': str(data), 'cli_entry': str(entry)})
            self.pool.add_account(account, str(data), str(config), enabled=True)
            if group:
                with self.pool.connect() as db:
                    db.execute('UPDATE accounts SET login_group=? WHERE id=?', (group, account))
            self.pool.account_state(account, 'authenticated', True)
            self.runtimes[account] = SwitchRuntime(data)
        self.out = self.root / 'out'

    # ---- helpers
    def gate(self, account):
        self.pool.account_state(account, 'browser_challenge', False)

    def gate_all(self):
        for account in self.runtimes:
            self.gate(account)

    def submit(self, key, **kw):
        return self.pool.submit(prompt='Draw a kite.', output_dir=str(self.out), request_id=key, **kw)['job_id']

    def fake_generate(self, **kw):
        Path(kw['output_dir']).mkdir(parents=True, exist_ok=True)
        target = Path(kw['output_dir']) / (kw['job_id'] + '.png')
        target.write_bytes(b'png')
        import hashlib
        return {'job_id': kw['job_id'], 'status': 'complete', 'provider': 'codex-imagegen', 'count_match': True,
                'files': [{'path': str(target), 'width': 1, 'height': 1, 'bytes': 3, 'sha256': hashlib.sha256(b'png').hexdigest()}]}

    def run_lane(self):
        lane = CodexLane(self.pool, fallback_settings(self.pool.root), generate=self.fake_generate)
        lane.tick()
        for t in list(lane.running.values()):
            t.join(5)

    def checks_queued(self):
        with self.pool.connect() as db:
            return sorted(r['account_id'] for r in db.execute("SELECT account_id FROM controls WHERE action='check' AND status='queued'"))

    def write_last_check(self, identity, checked_at_s, state='signed_in'):
        w.write_json(self.hub / 'last-check.json', {'identities': {identity: {'sites': {'chatgpt': {
            'state': state, 'live': True, 'verified': True, 'checkedAt': int(checked_at_s * 1000)}}}}})

    def age_gate(self, group, seconds):
        book = human_gate.GateBook(self.pool.root)
        gate = book.read()['gates'][group]
        book.update(group, since=gate['since'] - seconds)

    # ---- routing
    def test_a_ready_web_lane_gets_the_first_chance(self):
        job = self.submit('web-first')
        status = self.pool.status()
        self.assertEqual((status['serving'], status['web_lanes_able']), ('web', 3))
        self.run_lane()
        self.assertEqual(self.pool.row(job)['status'], 'queued', 'Codex waits while a web lane could take it')

    def test_no_web_lane_able_sends_new_work_to_codex_at_once(self):
        self.gate_all()
        status = self.pool.status()
        self.assertEqual((status['serving'], status['can_generate'], status['web_lanes_able']), ('codex', True, 0))
        job = self.submit('no-web')
        self.assertEqual(self.pool.get(job).get('served_by'), 'codex')
        self.run_lane()
        result = self.pool.get(job)
        self.assertEqual((result['status'], result['account_id'], result['fallback_from']), ('complete', CODEX_LANE, 'web_unavailable'))

    def test_disabled_or_cooling_logins_do_not_count_as_web(self):
        self.pool.enable('primary', False)
        self.pool.enable('primary-2', False)
        self.pool.defer_rate_limit('secondary')
        self.assertEqual(self.pool.web_takers(), [])
        job = self.submit('cooling')
        self.run_lane()
        self.assertEqual(self.pool.get(job)['status'], 'complete')

    def test_pinned_work_keeps_its_long_wait_even_without_web(self):
        self.gate_all()
        job = self.submit('pinned', account_id='secondary')
        self.run_lane()
        self.assertEqual(self.pool.row(job)['status'], 'queued', 'only the web keeps pinned work on its login')

    def test_codex_first_still_wins_when_chosen(self):
        settings = json.loads((self.pool.root / 'codex-fallback.json').read_text(encoding='utf-8'))
        w.write_json(self.pool.root / 'codex-fallback.json', {**settings, 'prefer': 'codex'})
        self.assertEqual(self.pool.status()['serving'], 'codex')
        job = self.submit('codex-first')
        self.run_lane()
        self.assertEqual(self.pool.get(job)['fallback_from'], 'codex_first')

    # ---- gate revisits
    def test_lane_binding_comes_from_the_lane_entry(self):
        account = self.pool.account('secondary')
        self.assertEqual(gate_revisit.lane_binding(account), {'root': self.hub, 'identity': 'alt'})
        w.write_json(Path(account['config_dir']) / 'settings.json', {'data_dir': 'x', 'cli_entry': 'C:/x/playwright-cli.js'})
        self.assertIsNone(gate_revisit.lane_binding(account))

    def test_hub_confirmation_after_the_gate_queues_one_check_per_enabled_lane(self):
        self.gate('primary')
        self.gate('primary-2')
        self.write_last_check('main', time.time() - 3600)  # older than the gate: no evidence
        self.assertEqual(gate_revisit.revisit(self.pool), [])
        self.write_last_check('main', time.time() + 1)
        self.assertEqual(gate_revisit.revisit(self.pool), [('primary', 'hub_login_confirmed')])
        self.assertEqual(self.checks_queued(), ['primary', 'primary-2'])
        self.assertEqual(gate_revisit.revisit(self.pool), [], 'the same evidence queues nothing twice')

    def test_a_check_that_passes_clears_the_gate_and_web_serves_again(self):
        self.gate_all()
        self.write_last_check('main', time.time() + 1)
        gate_revisit.revisit(self.pool)
        for account in ('primary', 'primary-2'):
            self.runtimes[account].auth_state = 'authenticated'
            Worker(self.pool, account, self.runtimes[account]).tick()
        self.assertNotIn('primary', human_gate.GateBook(self.pool.root).read()['gates'])
        self.assertEqual(self.pool.status()['serving'], 'web')

    def test_a_check_that_still_fails_keeps_the_gate_without_looping(self):
        self.gate('secondary')
        self.write_last_check('alt', time.time() + 1)
        gate_revisit.revisit(self.pool)
        Worker(self.pool, 'secondary', self.runtimes['secondary']).tick()
        self.assertEqual(self.runtimes['secondary'].checks, 1)
        self.assertIn('secondary', human_gate.GateBook(self.pool.root).read()['gates'])
        self.assertEqual(gate_revisit.revisit(self.pool), [])
        self.assertEqual(self.checks_queued(), [])

    def test_nothing_is_visited_while_the_hub_pauses_the_site_or_a_person_holds_it(self):
        self.gate('secondary')
        self.write_last_check('alt', time.time() + 1)
        now_ms = time.time() * 1000
        w.write_json(self.hub / 'web-risk.json', {'handoff': None, 'sites': {'alt:chatgpt': {'until': now_ms + 600000}}})
        self.assertEqual(gate_revisit.revisit(self.pool), [])
        w.write_json(self.hub / 'web-risk.json', {'handoff': {'id': 'h1', 'identity': 'alt', 'site': 'chatgpt', 'until': now_ms + 600000}, 'sites': {}})
        self.assertEqual(gate_revisit.revisit(self.pool), [])
        self.assertEqual(self.checks_queued(), [])

    def test_an_ended_handoff_queues_a_check(self):
        self.gate('secondary')
        now_ms = time.time() * 1000
        w.write_json(self.hub / 'web-risk.json', {'handoff': {'id': 'h1', 'identity': 'alt', 'site': 'chatgpt', 'until': now_ms + 600000}, 'sites': {}})
        self.assertEqual(gate_revisit.revisit(self.pool), [])
        w.write_json(self.hub / 'web-risk.json', {'handoff': None, 'sites': {}})
        self.assertEqual(gate_revisit.revisit(self.pool), [('secondary', 'hub_handoff_ended')])
        self.assertEqual(self.checks_queued(), ['secondary'])
        self.assertEqual(gate_revisit.revisit(self.pool), [])

    def test_a_handoff_for_another_login_is_not_evidence(self):
        self.gate('secondary')
        now_ms = time.time() * 1000
        w.write_json(self.hub / 'web-risk.json', {'handoff': {'id': 'h1', 'identity': 'main', 'site': 'chatgpt', 'until': now_ms + 600000}, 'sites': {}})
        gate_revisit.revisit(self.pool)
        w.write_json(self.hub / 'web-risk.json', {'handoff': None, 'sites': {}})
        self.assertEqual(gate_revisit.revisit(self.pool), [])

    def test_periodic_recheck_every_twelve_hours_at_most(self):
        self.gate('secondary')
        self.assertEqual(gate_revisit.revisit(self.pool), [], 'a fresh gate waits for a person or evidence')
        self.age_gate('secondary', gate_revisit.GATE_RECHECK_SECONDS + 1)
        self.assertEqual(gate_revisit.revisit(self.pool), [('secondary', 'periodic')])
        with self.pool.connect() as db:
            db.execute("UPDATE controls SET status='complete'")
        self.assertEqual(gate_revisit.revisit(self.pool), [], 'not again within 12 hours')
        later = time.time() + gate_revisit.GATE_RECHECK_SECONDS + 1
        self.assertEqual(gate_revisit.revisit(self.pool, now=later), [('secondary', 'periodic')])

    def test_logins_switched_off_by_the_owner_are_left_alone(self):
        self.gate('secondary')
        self.pool.enable('secondary', False)
        self.write_last_check('alt', time.time() + 1)
        self.age_gate('secondary', gate_revisit.GATE_RECHECK_SECONDS + 1)
        self.assertEqual(gate_revisit.revisit(self.pool), [])
        self.assertEqual(self.checks_queued(), [])

    def test_ensure_workers_runs_the_scan_and_survives_a_broken_hub_file(self):
        self.gate('secondary')
        (self.hub / 'last-check.json').write_text('{not json', encoding='utf-8')
        (self.hub / 'web-risk.json').write_text('[]', encoding='utf-8')
        self.age_gate('secondary', gate_revisit.GATE_RECHECK_SECONDS + 1)
        self.pool.ensure_workers = Pool.ensure_workers.__get__(self.pool)
        import image_pool
        original = image_pool.subprocess.Popen
        image_pool.subprocess.Popen = lambda *a, **k: type('P', (), {'pid': 1})()
        try:
            self.pool.ensure_workers(include_codex=False)
        finally:
            image_pool.subprocess.Popen = original
        self.assertEqual(self.checks_queued(), ['secondary'])


if __name__ == '__main__':
    unittest.main()
