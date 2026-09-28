"""An idle lane leaves its last conversation once, and never while it owns a job."""
from pathlib import Path
import tempfile
import unittest

import web_images as w
from image_pool import Pool
from image_worker import Worker
from test_image_pool import FakeRuntime


class LeavingRuntime(FakeRuntime):
    def __init__(self, data):
        super().__init__(data)
        self.left = 0

    def leave_conversation(self):
        self.left += 1
        return {'left': True}


class IdlePageContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.pool = Pool(root / 'pool')
        data, config = root / 'primary' / 'browser', root / 'primary' / 'config'
        w.write_json(config / 'settings.json', {'data_dir': str(data)})
        self.pool.add_account('primary', str(data), str(config), enabled=True)
        self.pool.account_state('primary', 'authenticated', True)
        self.runtime = LeavingRuntime(data)
        self.worker = Worker(self.pool, 'primary', self.runtime)
        self.output = root / 'output'

    def tick(self, n=1):
        for _ in range(n):
            self.worker.control_turn = False
            self.worker.tick()

    def test_idle_lane_leaves_once(self):
        self.tick(5)
        self.assertEqual(self.runtime.left, 1)

    def test_busy_lane_keeps_its_conversation_then_leaves_after_completion(self):
        self.pool.submit(prompt='Draw a simple square.', output_dir=str(self.output), request_id='r1', account_id='auto')
        self.runtime.uncertain = False
        self.tick()          # a queued job does not own the tab yet; the claim starts it
        self.assertEqual(self.runtime.starts, 1)
        before = self.runtime.left
        self.runtime.failed = True
        self.tick(2)         # while the job owns the tab, polls never move it
        self.assertEqual(self.runtime.left, before)
        self.runtime.failed = False
        self.tick(4)         # the poll completes the job; the now idle lane leaves once
        self.assertEqual(self.runtime.left, before + 1)
        self.tick(3)
        self.assertEqual(self.runtime.left, before + 1)

    def test_rate_limited_idle_lane_still_leaves(self):
        # A cooling login must not keep a finished conversation polling in the background.
        self.pool.defer_rate_limit('primary')
        self.tick(2)
        self.assertEqual(self.runtime.left, 1)


if __name__ == '__main__':
    unittest.main()
