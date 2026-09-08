import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import time
import hashlib
import re
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('web_images',Path(__file__).with_name('web_images.py'))
w=importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)


class Contracts(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data=Path(self.temp.name)
        self.patcher=patch.object(w,'DATA',self.data)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        entry=self.data/'playwright-cli.js'
        entry.touch()
        self.cli_patcher=patch.object(w,'CLI_ENTRY',entry)
        self.cli_patcher.start()
        self.addCleanup(self.cli_patcher.stop)
        self.node_patcher=patch.object(w.shutil,'which',return_value='node.exe')
        self.node_patcher.start()
        self.addCleanup(self.node_patcher.stop)

    def job(self,status='generating'):
        value={'job_id':'20260907-abcdef012345','status':status,'created_at':0,
               'prompt_sha256':w.prompt_hash('my prompt'),'files':[]}
        w.save_job(value)
        w.write_json(self.data/'active.json',{'job_id':value['job_id']})
        return value

    def test_no_duplicate_submission_across_calls(self):
        job=self.job()
        with patch.object(w,'ensure_browser') as browser:
            result=w.safe_call(w.start,prompt='new prompt')
        self.assertFalse(result['ok'])
        self.assertEqual(result['error']['code'],'job_active')
        self.assertIn(job['job_id'],result['error']['message'])
        browser.assert_not_called()

    def test_foreign_prompt_is_never_downloaded(self):
        job=self.job()
        with patch.object(w,'ensure_browser'), patch.object(w,'run_js',return_value={'user_count':1,'user_text':'other task'}):
            result=w.safe_call(w.poll,job_id=job['job_id'])
        self.assertFalse(result['ok'])
        self.assertEqual(result['error']['code'],'conversation_changed')
        self.assertIsNotNone(w.active_job())

    def test_foreign_prompt_is_never_cancelled(self):
        job=self.job()
        with patch.object(w,'ensure_browser'), patch.object(w,'run_js',return_value={'user_count':1,'user_text':'other task'}) as run:
            result=w.safe_call(w.cancel,job_id=job['job_id'])
        self.assertEqual(result['error']['code'],'conversation_changed')
        self.assertEqual(run.call_count,1)

    def test_completed_output_tampering_is_detected(self):
        job=self.job('complete')
        image=self.data/'delivered.png'
        image.write_bytes(b'changed')
        job['files']=[{'path':str(image),'sha256':'0'*64}]
        w.save_job(job)
        with patch.object(w,'ensure_browser') as browser:
            result=w.safe_call(w.poll,job_id=job['job_id'])
        self.assertEqual(result['error']['code'],'output_changed')
        browser.assert_not_called()

    def test_browser_iserror_is_failure_even_with_exit_zero(self):
        proc=subprocess.CompletedProcess([],0,json.dumps({'isError':True,'error':'Timeout secret-cookie-value'}),'')
        with patch.object(w.subprocess,'run',return_value=proc):
            result=w.safe_call(w.cli,args=['test'])
        self.assertFalse(result['ok'])
        self.assertNotIn('secret-cookie-value',json.dumps(result))
        self.assertEqual(result['error']['code'],'browser_timeout')

    def test_invalid_json_does_not_leak_raw_response(self):
        proc=subprocess.CompletedProcess([],1,'secret-cookie-value','more-secrets')
        with patch.object(w.subprocess,'run',return_value=proc):
            result=w.safe_call(w.cli,args=['test'])
        self.assertEqual(result['error']['code'],'browser_protocol_error')
        self.assertNotIn('secret',json.dumps(result))

    def test_timeout_is_reported_without_resubmission(self):
        with patch.object(w.subprocess,'run',side_effect=subprocess.TimeoutExpired(['secret'],1)) as run:
            result=w.safe_call(w.cli,args=['test'])
        self.assertEqual(result['error']['code'],'browser_timeout')
        self.assertEqual(run.call_count,1)

    def test_no_base64_or_relative_reference(self):
        with patch.object(w,'ensure_browser') as browser:
            first=w.safe_call(w.start,prompt='data:image/png;base64,AAAA')
            second=w.safe_call(w.start,prompt='hello',reference_images=['relative.png'])
        self.assertEqual(first['error']['code'],'invalid_prompt')
        self.assertEqual(second['error']['code'],'invalid_reference')
        browser.assert_not_called()

    def test_job_id_cannot_escape_data_directory(self):
        result=w.safe_call(w.poll,job_id='../../auth-state')
        self.assertEqual(result['error']['code'],'invalid_job_id')

    def test_os_lock_excludes_second_handle(self):
        with w.locked():
            with self.assertRaises(w.ImageError) as caught:
                with w.locked():
                    self.fail('lock was acquired twice')
        self.assertEqual(caught.exception.code,'busy')

    def test_count_validation_precedes_browser_work(self):
        with patch.object(w,'ensure_browser') as browser:
            for count in [0,-1,True,1.5,21,'5']:
                response=w.safe_call(w.start,prompt='test',count=count)
                self.assertEqual(response['error']['code'],'invalid_count')
        browser.assert_not_called()

    def test_one_submission_carries_requested_count(self):
        with patch.object(w,'ensure_browser'),patch.object(w,'cli'),patch.object(w,'set_visible',return_value=True),patch.object(w,'run_js',side_effect=[True,{'url':'https://chatgpt.com/c/abc-def'}]) as js:
            result=w.start(prompt='my prompt',count=5,output_dir=str(self.data))
        self.assertEqual(result['requested_count'],5)
        self.assertEqual(js.call_count,2)  # image-mode selection + ONE submitted request
        self.assertIn('5',js.call_args_list[-1].args[0])
        self.assertNotEqual(result['prompt_sha256'],result['submitted_prompt_sha256'])

    def multi_fixture(self,requested=5,observed=5):
        job=self.job('image_settling')
        job.update(requested_count=requested,output_dir=str(self.data),name='multi',created_at=time.time())
        images=[{'key':f'/image/{i}','width':1024,'height':1024} for i in range(observed)]
        fingerprint=hashlib.sha256(json.dumps([(a['key'],a['width'],a['height']) for a in images],sort_keys=True).encode()).hexdigest()
        job.update(image_fingerprint=fingerprint,stable_since=time.time()-60)
        w.save_job(job)
        snapshot={'user_count':1,'user_text':'my prompt','url':'https://chatgpt.com/c/abc-def','stop':False,'pending_images':0,'text':'Done','images':images}
        def fake_js(source,**kwargs):
            if source==w.POLL_JS:return snapshot
            match=re.search(r'await download.saveAs\((.+)\);',source)
            self.assertIsNotNone(match,'Unexpected browser operation')
            target=Path(json.loads(match.group(1)))
            from PIL import Image
            Image.new('RGB',(32,32),(int(target.stem.rsplit('-',1)[-1])*20,0,0)).save(target,format='PNG')
            return {'saved':True}
        return job,snapshot,fake_js

    def test_five_images_saved_and_verified(self):
        job,snapshot,js=self.multi_fixture()
        snapshot['images'].append(dict(snapshot['images'][0]))  # repeated DOM node is not a sixth image
        with patch.object(w,'ensure_browser',return_value={'url':snapshot['url']}),patch.object(w,'run_js',side_effect=js):
            result=w.poll(job['job_id'])
        self.assertEqual(result['status'],'complete')
        self.assertEqual(result['downloaded_count'],5)
        self.assertEqual(result['observed_count'],5)
        self.assertEqual(len({f['path'] for f in result['files']}),5)
        self.assertIsNone(w.active_job())
        for file in result['files']:
            self.assertEqual(hashlib.sha256(Path(file['path']).read_bytes()).hexdigest(),file['sha256'])

    def test_missing_images_never_report_complete(self):
        job,snapshot,js=self.multi_fixture(requested=5,observed=3)
        with patch.object(w,'ensure_browser',return_value={'url':snapshot['url']}),patch.object(w,'run_js',side_effect=js):
            result=w.safe_call(w.poll,job_id=job['job_id'])
        self.assertFalse(result['ok'])
        self.assertEqual(result['status'],'partial')
        self.assertEqual(result['missing_count'],2)
        self.assertEqual(len(result['files']),3)

    def test_extra_images_preserved_and_reported(self):
        job,snapshot,js=self.multi_fixture(requested=5,observed=6)
        with patch.object(w,'ensure_browser',return_value={'url':snapshot['url']}),patch.object(w,'run_js',side_effect=js):
            result=w.safe_call(w.poll,job_id=job['job_id'])
        self.assertFalse(result['ok'])
        self.assertEqual(result['status'],'count_mismatch')
        self.assertEqual(result['extra_count'],1)
        self.assertEqual(len(result['files']),6)

    def test_failed_third_download_resumes_without_redownloading_two(self):
        job,snapshot,js=self.multi_fixture()
        attempts=[]
        def fail_third(source,**kwargs):
            if source!=w.POLL_JS:
                attempts.append(source)
                if len(attempts)==3:raise w.ImageError('browser_timeout','test timeout')
            return js(source,**kwargs)
        with patch.object(w,'ensure_browser',return_value={'url':snapshot['url']}),patch.object(w,'run_js',side_effect=fail_third):
            first=w.safe_call(w.poll,job_id=job['job_id'])
        self.assertFalse(first['ok'])
        checkpoint=json.loads(w.job_path(job['job_id']).read_text(encoding='utf-8'))
        self.assertEqual(len(checkpoint['files']),2)
        with patch.object(w,'ensure_browser',return_value={'url':snapshot['url']}),patch.object(w,'run_js',side_effect=js) as calls:
            second=w.poll(job['job_id'])
        self.assertEqual(second['status'],'complete')
        self.assertEqual(calls.call_count,4)  # one snapshot plus only three remaining downloads

    def test_crash_after_rename_recovers_existing_verified_file(self):
        job,snapshot,js=self.multi_fixture(requested=1,observed=1)
        original_save=w.save_job
        def crash_before_checkpoint(value):
            if value['files']:raise OSError('simulated crash after rename')
            original_save(value)
        with patch.object(w,'run_js',side_effect=js),patch.object(w,'save_job',side_effect=crash_before_checkpoint):
            with self.assertRaises(OSError):w.download_asset(job,snapshot['images'][0])
        recovered=json.loads(w.job_path(job['job_id']).read_text(encoding='utf-8'))
        self.assertEqual(len(recovered['files']),0)
        with patch.object(w,'run_js') as browser:
            w.download_asset(recovered,snapshot['images'][0])
        browser.assert_not_called()
        self.assertEqual(len(recovered['files']),1)

    def test_request_id_reuses_completed_job_without_browser(self):
        with patch.object(w,'ensure_browser'),patch.object(w,'cli'),patch.object(w,'set_visible',return_value=True),patch.object(w,'run_js',side_effect=[True,{'url':'https://chatgpt.com/c/abc-def'}]):
            first=w.start(prompt='hello',request_id='retry-1',output_dir=str(self.data))
        job=json.loads(w.job_path(first['job_id']).read_text(encoding='utf-8'))
        job['status']='complete'
        w.save_job(job)
        (self.data/'active.json').unlink()
        with patch.object(w,'ensure_browser') as browser:
            second=w.start(prompt='hello',request_id='retry-1',output_dir=str(self.data))
        browser.assert_not_called()
        self.assertEqual(second['job_id'],first['job_id'])
        self.assertTrue(second['reused_request'])

    def test_request_id_conflict_never_resubmits(self):
        w.write_json(self.data/'requests'/'retry-1.json',{'request_hash':'different','job_id':'20260907-abcdef012345'})
        with patch.object(w,'ensure_browser') as browser:
            result=w.safe_call(w.start,prompt='changed',request_id='retry-1')
        self.assertEqual(result['error']['code'],'request_id_conflict')
        browser.assert_not_called()

    def test_request_id_path_traversal_rejected(self):
        result=w.safe_call(w.start,prompt='hello',request_id='../secret')
        self.assertEqual(result['error']['code'],'invalid_request_id')

    def test_request_id_windows_device_name_rejected(self):
        for name in ('NUL','con','COM1','lpt9'):
            result=w.safe_call(w.start,prompt='hello',request_id=name)
            self.assertEqual(result['error']['code'],'invalid_request_id')

    def test_uncertain_request_is_reused(self):
        with patch.object(w,'ensure_browser'),patch.object(w,'cli'),patch.object(w,'run_js',side_effect=[True,w.ImageError('browser_timeout','timeout')]):
            first=w.start(prompt='hello',request_id='uncertain-1')
        self.assertEqual(first['status'],'submission_uncertain')
        with patch.object(w,'ensure_browser') as browser:
            second=w.start(prompt='hello',request_id='uncertain-1')
        browser.assert_not_called()
        self.assertEqual(second['job_id'],first['job_id'])

    def test_window_failure_after_submission_keeps_active_job(self):
        with patch.object(w,'ensure_browser'),patch.object(w,'cli'),patch.object(w,'run_js',side_effect=[True,{'url':'https://chatgpt.com/c/abc-def'}]),patch.object(w,'set_visible',side_effect=w.ImageError('browser_timeout','timeout')):
            first=w.start(prompt='hello')
        self.assertEqual(first['status'],'submission_uncertain')
        self.assertIsNotNone(w.active_job())

    def test_wait_stops_at_completion_without_more_polls(self):
        with patch.object(w,'poll',side_effect=[{'status':'generating'},{'status':'complete'}]) as poll,patch.object(w.time,'sleep') as sleep:
            result=w.poll_wait('id',40)
        self.assertEqual(result['status'],'complete')
        self.assertEqual(poll.call_count,2)
        self.assertEqual(sleep.call_count,1)

    def test_wait_releases_lock_and_stops_at_deadline(self):
        def check_sleep(_):
            with w.locked():pass
        with patch.object(w,'poll',return_value={'status':'generating'}) as poll,patch.object(w.time,'monotonic',side_effect=[0,5,45]),patch.object(w.time,'sleep',side_effect=check_sleep):
            result=w.poll_wait('id',40)
        self.assertEqual(poll.call_count,2)
        self.assertEqual(result['status'],'generating')

    def test_wait_validation_and_zero_wait(self):
        for value in (-1,46,True,1.5):
            self.assertEqual(w.safe_call(w.poll_wait,job_id='id',wait_seconds=value)['error']['code'],'invalid_wait')
        with patch.object(w,'poll',return_value={'status':'generating'}) as poll,patch.object(w.time,'sleep') as sleep:
            w.poll_wait('id',0)
        self.assertEqual(poll.call_count,1)
        sleep.assert_not_called()

    def test_compact_hides_repeated_evidence_but_detail_restores_it(self):
        job=self.job()
        job.update(provider='chatgpt-web',prompt_sha256='a'*64,submitted_prompt_sha256='b'*64,output_dir=str(self.data))
        full={'ok':True,**w.public_job(job)}
        slim=w.compact_result(full)
        self.assertNotIn('prompt_sha256',slim)
        self.assertNotIn('output_dir',slim)
        self.assertEqual(slim['requested_count'],1)
        self.assertEqual(w.compact_result(full,True),full)

    def test_compact_keeps_saved_files_and_error(self):
        job,snapshot,js=self.multi_fixture(requested=5,observed=3)
        with patch.object(w,'ensure_browser',return_value={'url':snapshot['url']}),patch.object(w,'run_js',side_effect=js):
            result=w.compact_result(w.safe_call(w.poll,job_id=job['job_id']))
        self.assertFalse(result['ok'])
        self.assertEqual(len(result['files']),3)
        self.assertIn('sha256',result['files'][0])
        self.assertNotIn('source_id',result['files'][0])
        self.assertEqual(result['missing_count'],2)

    def test_poll_failure_returns_checkpoint_context(self):
        job=self.job()
        with patch.object(w,'ensure_browser',side_effect=w.ImageError('browser_timeout','timeout')):
            result=w.safe_call(w.poll,job_id=job['job_id'])
        self.assertFalse(result['ok'])
        self.assertEqual(result['job_id'],job['job_id'])
        self.assertEqual(result['downloaded_count'],0)

    def test_default_auth_is_optional(self):
        with patch.object(w,'AUTH',None),patch.object(w,'cli') as cli,patch.object(w,'run_js',side_effect=[w.ImageError('browser_not_open','closed'),True,{'logged_in':False}]):
            result=w.safe_call(w.ensure_browser)
        self.assertEqual(result['error']['code'],'login_required')
        self.assertFalse(any(c.args[0][0]=='state-load' for c in cli.call_args_list))


if __name__=='__main__':
    unittest.main(verbosity=2)
