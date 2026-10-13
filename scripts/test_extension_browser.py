"""Extension browser contracts; no live account or browser is visited."""
import json
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

import web_images as w
import human_gate
from image_worker import Worker
from image_pool import ACTIONABLE


def binding(tmp_path, **values):
    entry = tmp_path / 'entry.cjs'
    entry.write_text('require("tab_client.cjs").main(' + json.dumps(values) + ');', encoding='utf-8')
    return entry


def test_extension_guard_uses_the_actual_personal_browser_root(tmp_path):
    entry = binding(tmp_path, transport='extension', root=str(tmp_path/'ai'), browserRoot=str(tmp_path/'personal'))
    assert w._hub_root_of(entry) == tmp_path/'personal'


def test_legacy_guard_still_uses_original_browser_root(tmp_path):
    entry = binding(tmp_path, root=str(tmp_path/'ai'))
    assert w._hub_root_of(entry) == tmp_path/'ai'


def test_shared_browser_never_hides_a_persons_window_or_emulates_browser_focus(tmp_path):
    entry = binding(tmp_path, transport='extension', root=str(tmp_path/'ai'), browserRoot=str(tmp_path/'personal'))
    with patch.object(w, 'CLI_ENTRY', entry), patch.object(w, 'run_js', side_effect=AssertionError('Must not touch any window')):
        assert not w.set_visible(False)
        assert not w.set_visible(True)
        source = 'async page => await page.locator("input").fill("hello")'
        assert w.interactive(source) == '/* @web-images-interactive */\n' + source
        assert 'newCDPSession' not in w.interactive(source)


def test_pairing_failure_is_actionable_without_leaking_the_raw_response(tmp_path):
    entry = binding(tmp_path, transport='extension', root=str(tmp_path/'ai'), browserRoot=str(tmp_path/'personal'))
    proc = subprocess.CompletedProcess([], 1, json.dumps({'isError': True, 'error': 'Extension pairing required private-token'}), '')
    with patch.object(w, 'CLI_ENTRY', entry), patch.object(w.shutil, 'which', return_value='node'), patch.object(w.subprocess, 'run', return_value=proc):
        result = w.safe_call(w.cli, args=['open'])
    assert result['error']['code'] == 'extension_pairing_required'
    assert 'private-token' not in json.dumps(result)
    assert human_gate.gate_code('browser_unhealthy', result['error']) == 'extension_pairing_required'
    assert ACTIONABLE['extension_pairing_required'] == 'human'
    worker = object.__new__(Worker)
    assert not worker.recheck_due({'enabled': 1, 'state': 'browser_unhealthy', 'error': result['error']})


def test_extension_pairing_notice_does_not_probe_chatgpt(tmp_path):
    alerts = human_gate.Alerts(tmp_path, background=False, notify=True,
                              fetch=lambda: (_ for _ in ()).throw(AssertionError('No site visit for pairing')),
                              toast=lambda *args: None)
    alerts.observe('primary', 'primary', 'browser_unhealthy', False, {'code': 'extension_pairing_required'})
    gates, _ = alerts.book.summary()
    assert gates[0]['next_action'] == 'pair_official_extension_then_image_account_check'


def test_windows_pairing_token_is_protected_for_current_user():
    if sys.platform != 'win32':
        import pytest
        pytest.skip('Windows DPAPI')
    from extension_token_dpapi import transform
    plain = b'test-pairing-token-with-no-real-credentials'
    protected = transform(plain, protect=True)
    assert plain not in protected
    assert transform(protected, protect=False) == plain
