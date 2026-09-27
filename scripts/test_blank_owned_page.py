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
        cli.assert_called_once_with(['goto','https://chatgpt.com/'])

    def test_loaded_conversation_is_not_navigated_for_a_health_check(self):
        signed={**SIGNED,'url':'https://chatgpt.com/c/fixture'}
        with patch.object(w,'run_js',return_value=signed),patch.object(w,'cli') as cli:
            self.assertTrue(w.ensure_browser()['logged_in'])
        cli.assert_not_called()

if __name__=='__main__':unittest.main()
