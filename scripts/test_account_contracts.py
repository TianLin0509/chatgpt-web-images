import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import web_images as w


CHOOSER = dict(logged_in=False,account_chooser=True,challenge=False,auth_state='account_selection_required')
SIGNED = dict(logged_in=True,account_chooser=False,challenge=False,auth_state='authenticated')


class AccountContracts(unittest.TestCase):
    def test_unauthorized_chooser_never_clicked(self):
        with patch.dict(w.SETTINGS,{},clear=True), patch.object(w,'run_js',return_value=dict(CHOOSER)) as js:
            response=w.safe_call(w.ensure_browser)
        self.assertEqual(response['error']['code'],'account_selection_required')
        self.assertEqual(js.call_count,1)

    def test_configured_account_recovers_once(self):
        with patch.dict(w.SETTINGS,{'account_name':'TIAN LIN'},clear=True),patch.object(w,'run_js',side_effect=[dict(CHOOSER),dict(SIGNED)]) as js:
            result=w.ensure_browser()
        self.assertTrue(result['logged_in'])
        self.assertEqual(js.call_count,2)
        self.assertIn('TIAN LIN',js.call_args.args[0])

    def test_selection_failure_is_not_success(self):
        state=dict(CHOOSER,error_code='account_not_found')
        with patch.object(w,'run_js',side_effect=[dict(CHOOSER),state]):
            result=w.safe_call(w.ensure_browser,account_name='Wrong name')
        self.assertFalse(result['ok'])
        self.assertEqual(result['error']['code'],'account_not_found')

    def test_no_auth_snapshot_saved_or_window_hidden_when_signed_out(self):
        with tempfile.TemporaryDirectory() as temp,patch.object(w,'DATA',Path(temp)),patch.object(w,'ensure_browser',return_value=dict(CHOOSER)),patch.object(w,'cli') as cli,patch.object(w,'set_visible') as hide:
            result=w.status()
        self.assertFalse(result['logged_in'])
        cli.assert_not_called()
        hide.assert_not_called()

    def test_explicit_selection_persists_name_not_credentials(self):
        with tempfile.TemporaryDirectory() as temp,patch.object(w,'DATA',Path(temp)),patch.object(w,'CONFIG_FILE',Path(temp)/'settings.json'),patch.dict(w.SETTINGS,{},clear=True),patch.object(w,'ensure_browser',return_value=dict(SIGNED)),patch.object(w,'cli'):
            result=w.select_account('TIAN LIN')
            self.assertEqual(json.loads(w.CONFIG_FILE.read_text()),{'account_name':'TIAN LIN'})
            self.assertTrue(result['remembered'])

    def test_bridge_auth_failure_does_not_advance_cursor(self):
        # Optional local integration. Point CHATGPT_BRIDGE_PATH at a bridge.py to run it;
        # a hardcoded home directory does not belong in a published repository.
        configured=os.environ.get('CHATGPT_BRIDGE_PATH','')
        path=Path(configured) if configured else None
        if path is None or not path.is_file():
            self.skipTest('Set CHATGPT_BRIDGE_PATH to run the local bridge integration.')
        spec=importlib.util.spec_from_file_location('account_test_bridge',path)
        bridge=importlib.util.module_from_spec(spec);spec.loader.exec_module(bridge)
        cfg={'account_name':'TIAN LIN','storage_state_path':'unused'}
        with patch.object(bridge,'_run_code',side_effect=[dict(CHOOSER),dict(CHOOSER,error_code='account_not_found')]),patch.object(bridge,'_save_seen_message_ids') as cursor:
            with self.assertRaises(bridge.BridgeError) as error:bridge.ensure_session(cfg)
        self.assertEqual(error.exception.code,'account_not_found')
        cursor.assert_not_called()


if __name__=='__main__':unittest.main()
