"""Recovery never sends again, never resets another job, and renews observation time."""
import json
import time
import unittest
from unittest.mock import patch
import test_web_images as fixtures
w = fixtures.w


class Recovery(fixtures.Contracts):
    def test_resume_renews_expired_wait_and_preserves_identity(self):
        job = self.job('needs_attention')
        job['conversation_url'] = 'https://chatgpt.com/c/original'
        job['error'] = {'code': 'generation_wait_exceeded'}
        w.save_job(job)
        with patch.object(w, 'run_js') as run, patch.object(w, 'cli') as cli:
            w.resume_existing(job['job_id'])
        stored = json.loads(w.job_path(job['job_id']).read_text(encoding='utf-8'))
        self.assertGreater(stored['wait_started_at'], time.time()-5)
        self.assertEqual(stored['created_at'], 0)
        self.assertEqual(stored['conversation_url'], job['conversation_url'])
        self.assertEqual(stored['status'], 'generating')
        run.assert_not_called(); cli.assert_not_called()

    def test_resume_cannot_take_another_job_browser(self):
        job = self.job('needs_attention')
        w.write_json(w.DATA/'active.json', {'job_id':'20260918-ffffffffffff'})
        with self.assertRaises(w.ImageError) as caught: w.resume_existing(job['job_id'])
        self.assertEqual(caught.exception.code, 'job_not_active')

    def test_recovery_never_requires_deleted_references_or_submits(self):
        job = self.job('submission_uncertain')
        w.write_json(w.DATA/'requests/pool-key.json', {'job_id':job['job_id']})
        with patch.object(w, 'start') as start, patch.object(w, 'ensure_browser') as browser:
            result = w.recover_existing('pool-key')
        self.assertEqual(result['job_id'],job['job_id'])
        start.assert_not_called(); browser.assert_not_called()


if __name__ == '__main__': unittest.main(verbosity=2)
