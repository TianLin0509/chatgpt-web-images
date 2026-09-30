"""An interrupted cold open must recover its owned blank page exactly once."""
import unittest
from unittest.mock import patch
import web_images as w

BLANK={'url':'about:blank','logged_in':False,'auth_state':'page_not_ready'}
SIGNED={'url':'https://chatgpt.com/','logged_in':True,'auth_state':'authenticated'}

class BlankOwnedPage(unittest.TestCase):
    def test_existing_blank_page_finishes_navigation_without_opening_another_tab(self):
        with patch.object(w,'run_js',side_effect=[dict(BLANK),dict(SIGNED)]),patch.object(w,'cli') as cli:
            result=w.ensure_browser()
        self.assertTrue(result['logged_in'])
        cli.assert_called_once_with(['goto','https://chatgpt.com/'])

    def test_blank_page_recovery_preserves_human_challenge(self):
        challenge={'url':'https://chatgpt.com/','logged_in':False,'challenge':True,'auth_state':'browser_challenge'}
        with patch.object(w,'run_js',side_effect=[dict(BLANK),challenge]),patch.object(w,'cli') as cli:
            with self.assertRaises(w.ImageError) as error:w.ensure_browser()
        self.assertEqual(error.exception.code,'browser_challenge')
        # 0.7.23: the only navigation after a challenge is leaving it; a check page left open
        # retries by itself and every failure counts against the login.
        self.assertEqual([c.args[0] for c in cli.call_args_list],[['goto','https://chatgpt.com/'],['goto','about:blank']])

    def test_loaded_conversation_is_not_navigated_for_a_health_check(self):
        signed={**SIGNED,'url':'https://chatgpt.com/c/fixture'}
        with patch.object(w,'run_js',return_value=signed),patch.object(w,'cli') as cli:
            self.assertTrue(w.ensure_browser()['logged_in'])
        cli.assert_not_called()

    def test_new_task_checks_its_fresh_composer_instead_of_a_failed_old_thread(self):
        old={'url':'https://chatgpt.com/c/fixture-old','logged_in':False,'auth_state':'rate_limited','rate_limited':True}
        with patch.object(w,'run_js',side_effect=[old,dict(SIGNED)]),patch.object(w,'cli') as cli:
            result=w.ensure_browser(new_conversation=True)
        self.assertTrue(result['logged_in']);cli.assert_called_once_with(['goto','https://chatgpt.com/'])

    def test_new_task_does_not_navigate_away_from_a_human_authentication_gate(self):
        old={'url':'https://chatgpt.com/c/fixture-old','logged_in':False,'auth_state':'browser_challenge','challenge':True}
        with patch.object(w,'run_js',return_value=old),patch.object(w,'cli') as cli:
            with self.assertRaises(w.ImageError) as error:w.ensure_browser(new_conversation=True)
        self.assertEqual(error.exception.code,'browser_challenge')
        # Never back into ChatGPT for a new task; only away from the challenge.
        cli.assert_called_once_with(['goto','about:blank'])

    def test_page_shown_to_a_person_keeps_its_challenge(self):
        gate={'url':'https://chatgpt.com/','logged_in':False,'auth_state':'browser_challenge','challenge':True}
        with patch.object(w,'run_js',return_value=gate),patch.object(w,'cli') as cli:
            state=w.ensure_browser(require_login=False,for_person=True)
        self.assertTrue(state['challenge']);cli.assert_not_called()

if __name__=='__main__':unittest.main()
