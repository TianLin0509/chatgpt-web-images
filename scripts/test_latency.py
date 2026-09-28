"""Regression contracts for costly retries and premature image finalization."""
import json,time,unittest
import tempfile
from pathlib import Path
from urllib.request import Request,urlopen
from urllib.error import HTTPError
from unittest.mock import patch
import test_web_images as fixtures
w=fixtures.w

class Latency(fixtures.Contracts):
    def test_unreadable_conversation_has_bounded_wait_without_submission(self):
        job=self.job();job['created_at']=time.time();w.save_job(job)
        snapshot={'user_count':0,'user_text':'','url':'https://chatgpt.com/c/abc','stop':False,'images':[]}
        with patch.object(w,'ensure_browser',return_value={'url':snapshot['url']}),patch.object(w,'run_js',return_value=snapshot),patch.object(w,'start') as submit:
            first=w.poll(job['job_id']);self.assertEqual(first['operation_stage'],'read_conversation')
            stored=json.loads(w.job_path(job['job_id']).read_text());stored['unreadable_since']=time.time()-61;w.save_job(stored)
            result=w.poll(job['job_id'])
            self.assertEqual(result['status'],'needs_attention');self.assertEqual(result['error']['code'],'conversation_not_readable');submit.assert_not_called()

    def test_cannot_cancel_a_conversation_without_identity_evidence(self):
        job=self.job()
        with patch.object(w,'ensure_browser'),patch.object(w,'run_js',return_value={'user_count':0,'stop':True}) as run:
            result=w.safe_call(w.cancel,job_id=job['job_id'])
        self.assertEqual(result['error']['code'],'conversation_not_readable');self.assertEqual(run.call_count,1)
        self.assertIsNotNone(w.active_job())

    def test_ui_only_footer_is_not_a_no_image_failure(self):
        job=self.job();job['created_at']=time.time();w.save_job(job)
        snapshot={'user_count':1,'user_text':'my prompt','url':'https://chatgpt.com/c/abc','stop':False,'turn_complete':True,'pending_images':0,'images':[],'text':'Worked for 1m 47s\nEdit'}
        with patch.object(w,'ensure_browser',return_value={'url':snapshot['url']}),patch.object(w,'run_js',return_value=snapshot):
            result=w.poll(job['job_id'])
        self.assertEqual(result['status'],'generating');self.assertNotIn('error',result)

    def test_windows_share_violation_does_not_lose_state(self):
        original=w.os.replace;attempts=[]
        def locked_once(a,b):
            attempts.append(1)
            if len(attempts)==1:raise PermissionError('fixture reader')
            return original(a,b)
        with patch.object(w.os,'replace',side_effect=locked_once),patch.object(w.time,'sleep'):
            w.write_json(self.data/'20260925-latency-state.json',{'state':'submitted'})
        self.assertEqual(json.loads((self.data/'20260925-latency-state.json').read_text()),{'state':'submitted'})
        self.assertEqual(len(attempts),2)

    def test_last_download_completes_without_another_browser_poll(self):
        job,snapshot,js=self.multi_fixture(requested=1,observed=1)
        clock=iter([0,40])
        with patch.object(w,'ensure_browser',return_value={'url':snapshot['url']}),patch.object(w,'run_js',side_effect=js),patch.object(w.time,'monotonic',side_effect=lambda:next(clock)):
            result=w.poll(job['job_id'])
        self.assertEqual(result['status'],'complete');self.assertEqual(len(result['files']),1)

    def test_download_keeps_original_and_has_no_resize_or_base64(self):
        self.assertIn('authenticated_original',w.DOWNLOAD_JS)
        self.assertIn('response.blob()',w.DOWNLOAD_JS)
        self.assertNotIn('toDataURL',w.DOWNLOAD_JS);self.assertNotIn('canvas',w.DOWNLOAD_JS.lower().replace('canvas conversion',''))
        self.assertIn('unverified_asset_variant',w.DOWNLOAD_JS)

    def test_substeps_are_recorded_without_heartbeat_noise(self):
        job=self.job('preparing');job['operation_stage']='fill_prompt';w.save_job(job)
        count=len(job['timeline']);w.save_job(job);self.assertEqual(len(job['timeline']),count)
        job['operation_stage']='submit_prompt';w.save_job(job);self.assertEqual(len(job['timeline']),count+1)

class OriginalTransfer(unittest.TestCase):
    def test_binary_receiver_is_private_bounded_and_one_shot(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'original.download'
            raw=bytes(range(256))*1024
            with w.original_receiver(path) as endpoint:
                for url,headers,code in [(endpoint+'wrong',{},403),(endpoint,{'Origin':'https://example.test'},403),(endpoint,{},413)]:
                    with self.assertRaises(HTTPError) as error:urlopen(Request(url,data=b'',headers=headers),timeout=2)
                    self.assertEqual(error.exception.code,code)
                self.assertFalse(path.exists())
                with urlopen(Request(endpoint,data=raw),timeout=2) as r:self.assertEqual(r.status,200)
                self.assertEqual(path.read_bytes(),raw)
                with self.assertRaises(HTTPError) as error:urlopen(Request(endpoint,data=b'changed'),timeout=2)
                self.assertEqual(error.exception.code,409)
                self.assertEqual(path.read_bytes(),raw)
