"""Login detection is language independent, and a non-English idle page is reloaded once
so English control names match. A job's own conversation is never reloaded."""
import unittest
from unittest.mock import patch

import chatgpt_account
import web_images as w

LOGGED_IN = {'logged_in': True, 'auth_state': 'authenticated', 'composer': True}


class UiLanguage(unittest.TestCase):
    def run_ensure(self, url, lang, active=None, new_conversation=False):
        states = [dict(LOGGED_IN, url=url, ui_lang=lang), dict(LOGGED_IN, url='https://chatgpt.com/', ui_lang='en-US')]
        calls = []
        with patch.object(w, 'run_js', side_effect=lambda *a, **k: states.pop(0) if states else dict(LOGGED_IN, url=url, ui_lang='en-US')), \
             patch.object(w, 'cli', side_effect=lambda args, *a, **k: calls.append(args) or {}), \
             patch.object(w, 'active_job', return_value=active):
            w.ensure_browser(new_conversation=new_conversation)
        return calls

    def test_chinese_home_page_is_reloaded_once(self):
        self.assertEqual(self.run_ensure('https://chatgpt.com/', 'zh-CN'), [['goto', 'https://chatgpt.com/']])

    def test_english_page_is_left_alone(self):
        self.assertEqual(self.run_ensure('https://chatgpt.com/', 'en-US'), [])

    def test_job_conversation_is_never_reloaded(self):
        self.assertEqual(self.run_ensure('https://chatgpt.com/c/test-thread-alpha', 'zh-CN', active={'job_id': 'x'}), [])

    def test_owned_home_page_reloads_only_for_a_new_task(self):
        self.assertEqual(self.run_ensure('https://chatgpt.com/', 'zh-CN', active={'job_id': 'x'}), [])
        self.assertEqual(self.run_ensure('https://chatgpt.com/', 'zh-CN', active={'job_id': 'x'}, new_conversation=True),
                         [['goto', 'https://chatgpt.com/']])

    def test_profile_detection_covers_chinese_ui(self):
        self.assertIn('打开个人资料菜单', chatgpt_account.STATE_FUNCTION)
        self.assertIn('ui_lang', chatgpt_account.STATE_FUNCTION)


if __name__ == '__main__':
    unittest.main()
