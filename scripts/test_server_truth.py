"""A page stuck on "Thinking" is reloaded only when the server reports a finished image turn,
with a grace period, back-off on HTTP 429 and a bounded number of reloads."""
import shutil
import subprocess
import time
import unittest
from unittest.mock import patch

import web_images as w

URL = 'https://chatgpt.com/c/test-thread-alpha'
POLLED = {'images': [{'key': 'k1', 'width': 1672, 'height': 941}], 'stop': False, 'pending_images': 0}


def job(age=200, **extra):
    return {'created_at': time.time() - age, 'wait_started_at': time.time() - age, 'conversation_url': URL, **extra}


class ServerTruth(unittest.TestCase):
    def run_truth(self, j, truth):
        calls = []
        def fake_js(source, *a, **k):
            if 'backend-api/conversation/' in source:
                calls.append('truth')
                return truth
            calls.append('poll' if source is w.POLL_JS else 'wait')
            return POLLED if source is w.POLL_JS else True
        with patch.object(w, 'run_js', side_effect=fake_js), \
             patch.object(w, 'cli', side_effect=lambda args, *a, **k: calls.append(tuple(args)) or {}):
            return w.server_truth_reload(j), calls

    def test_not_asked_during_grace_period(self):
        result, calls = self.run_truth(job(age=30), {'http': 200, 'finished': True, 'images': 1})
        self.assertIsNone(result)
        self.assertEqual(calls, [])

    def test_finished_image_on_server_reloads_the_recorded_conversation(self):
        j = job()
        result, calls = self.run_truth(j, {'http': 200, 'finished': True, 'images': 1})
        self.assertEqual(result, POLLED)
        self.assertEqual(calls, ['truth', ('goto', URL), 'wait', 'poll'])
        self.assertEqual(j['server_truth_reloads'], 1)

    def test_unfinished_turn_does_not_reload(self):
        j = job()
        result, calls = self.run_truth(j, {'http': 200, 'finished': False, 'images': 0})
        self.assertIsNone(result)
        self.assertEqual(calls, ['truth'])
        self.assertGreater(j['server_truth_next'], time.time() + 50)

    def test_rate_limited_read_backs_off(self):
        j = job()
        self.run_truth(j, {'http': 429})
        first = j['server_truth_next'] - time.time()
        j['server_truth_next'] = 0
        self.run_truth(j, {'http': 429})
        second = j['server_truth_next'] - time.time()
        self.assertGreater(second, first)
        _, calls = self.run_truth(j, {'http': 200, 'finished': True, 'images': 1})
        self.assertEqual(calls, [], 'a backed-off job is not asked again before its next slot')

    def test_reloads_are_bounded(self):
        j = job(server_truth_reloads=w.SERVER_TRUTH_RELOADS)
        result, calls = self.run_truth(j, {'http': 200, 'finished': True, 'images': 1})
        self.assertIsNone(result)
        self.assertEqual(calls, [])

    @unittest.skipUnless(shutil.which('node'), 'node is required to parse the page script')
    def test_page_script_parses(self):
        source = w.SERVER_TRUTH_JS.replace('__ID__', '"test-thread-alpha"')
        proc = subprocess.run([shutil.which('node') or 'node', '-e', 'new Function("return (" + require("fs").readFileSync(0, "utf8") + ")")'],
                              input=source, capture_output=True, text=True, encoding='utf-8',
                              creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.assertEqual(proc.returncode, 0, proc.stderr)


if __name__ == '__main__':
    unittest.main()
