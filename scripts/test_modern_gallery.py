"""Real headless Chromium fixtures for Sep 2026 gallery markup."""
import unittest
from playwright.sync_api import sync_playwright
from chatgpt_dom import POLL_JS
from web_images import prompt_hash
from test_image_pool import QueueContracts
import image_worker
import time


class GalleryDOM(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw=sync_playwright().start()
        cls.browser=cls.pw.chromium.launch(channel='chrome',headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close();cls.pw.stop()

    def setUp(self):
        self.page=self.browser.new_page();self.addCleanup(self.page.close)

    def observe(self):
        return self.page.evaluate('async()=>{const page={evaluate:async fn=>fn()};return ('+POLL_JS+')(page)}')

    def fixture(self):
        self.page.set_content('''<style>[data-search-result-target]{white-space:pre-wrap}</style><div data-turn-key="old"><div data-user-message-bubble>old prompt</div><div data-chatgpt-search-message-ids="old-response"><div data-testid="generated-image-gallery"><button data-testid="generated-image-preview" aria-label="Generated image 1"><img alt="Generated image 1"></button></div></div></div>
        <div data-turn-key="current"><div data-user-message-bubble><div data-search-result-target>Keep literal Create image.\nFive options.</div><button>Show more</button><img alt="Uploaded image"></div>
        <div><h4 data-conversation-role="assistant">ChatGPT said:</h4><div data-chatgpt-search-message-ids="current-response"><div data-testid="generated-image-gallery"><button data-testid="generated-image-preview" aria-label="Generated image 3"><img alt="Generated image 3"></button></div><div role="group" aria-label="Generated images">'''+''.join(f'<button aria-label="Show generated image {i}"><img></button>' for i in range(1,6))+'''</div></div><button aria-label="Copy image"></button></div></div>''')
        # Deterministic image metadata; each thumbnail and preview share its URL.
        self.page.evaluate('''()=>{for(const img of document.images){const n=Number(img.closest('button')?.getAttribute('aria-label')?.match(/\d+/)?.[0]||0);Object.defineProperties(img,{naturalWidth:{value:1600,configurable:true},naturalHeight:{value:1000,configurable:true},complete:{value:true,configurable:true},currentSrc:{value:'blob:https://chatgpt.com/'+n,configurable:true}})}}''')

    def test_prompt_scope_multi_image_dedup_and_order(self):
        self.fixture();r=self.observe()
        self.assertEqual(r['user_count'],2)
        self.assertEqual(prompt_hash(r['user_text']),prompt_hash('Keep literal Create image.\nFive options.'))
        self.assertEqual([a['index'] for a in r['images']],[1,2,3,4,5])
        self.assertTrue(all('current-response' in a['key'] for a in r['images']))
        self.assertTrue(r['turn_complete'])

    def test_unloaded_thumbnail_is_pending_not_missing(self):
        self.fixture();self.page.locator('button[aria-label="Show generated image 5"] img').evaluate("i=>Object.defineProperties(i,{naturalWidth:{value:0},currentSrc:{value:''},complete:{value:false}})")
        r=self.observe();self.assertEqual(len(r['images']),4);self.assertEqual(r['pending_images'],1)

    def test_offscreen_lazy_gallery_loads_without_loading_uploads_or_old_turns(self):
        from io import BytesIO
        from PIL import Image
        data=BytesIO();Image.new('RGB',(320,320),'blue').save(data,format='PNG')
        requested=[]
        def route(r):
            if r.request.url.endswith('.png'):
                requested.append(r.request.url.rsplit('/',1)[1]);r.fulfill(body=data.getvalue(),content_type='image/png')
            else:r.fulfill(body='<html><body></body></html>',content_type='text/html')
        self.page.route('**/*',route);self.page.goto('https://chatgpt.com/c/fixture')
        self.page.set_content('''<style>img{position:fixed;top:100000px;width:60px;height:60px}</style>
          <div data-turn-key="old"><div data-user-message-bubble>Old</div><img loading="lazy" src="/old.png"></div>
          <div data-turn-key="current"><div data-user-message-bubble>Current<img loading="lazy" src="/upload.png"></div>
          <div data-chatgpt-search-message-ids="current-response"><div role="group" aria-label="Generated images">
          <button aria-label="Show generated image 1"><img loading="lazy" src="/generated.png"></button></div></div></div>''')
        self.assertEqual(self.page.locator('img[src="/generated.png"]').evaluate('n=>n.naturalWidth'),0)
        self.observe()
        self.page.wait_for_function('document.querySelector(\'img[src="/generated.png"]\').naturalWidth===320',timeout=2000)
        result=self.observe()
        self.assertEqual(len(result['images']),1);self.assertEqual(result['pending_images'],0)
        self.assertEqual(requested,['generated.png'])

    def test_regenerated_blob_urls_keep_checkpoint_identity(self):
        self.fixture();before=[a['key'] for a in self.observe()['images']]
        self.page.evaluate("()=>{for(const i of document.images)Object.defineProperty(i,'currentSrc',{value:'blob:https://chatgpt.com/reloaded-'+Math.random()})}")
        self.assertEqual(before,[a['key'] for a in self.observe()['images']])

    def test_streaming_is_not_complete_even_with_gallery_present(self):
        self.fixture();self.page.evaluate("()=>{const b=document.createElement('button');b.setAttribute('aria-label','Stop generating');b.textContent='stop';document.body.append(b)}")
        self.assertTrue(self.observe()['stop'])


class QuietRecovery(QueueContracts):
    def test_control_failure_keeps_human_challenge_from_automatic_retry(self):
        self.submit('human-control','primary')
        self.pool.account_state('primary','needs_attention',False,{'code':'browser_challenge'})
        worker=self.worker('primary');worker.last_check=time.monotonic()-10000
        worker.tick()
        self.assertEqual(worker.recoveries,0)

    def test_parked_recoverable_job_counts_as_work_but_exhausted_job_does_not(self):
        job=self.submit('parked-recovery','primary');self.worker('primary').tick()
        self.pool.park(job['job_id'])
        with self.pool.connect() as db:db.execute('UPDATE jobs SET retry_after=0 WHERE id=?',(job['job_id'],))
        self.assertTrue(self.worker('primary').has_waiting_work())
        with self.pool.connect() as db:db.execute('UPDATE jobs SET park_count=99 WHERE id=?',(job['job_id'],))
        self.assertFalse(self.worker('primary').has_waiting_work())

    def test_idle_unhealthy_lane_does_not_launch_browser_checks(self):
        self.pool.account_state('primary','worker_error',False)
        worker=self.worker('primary');worker.last_check=time.monotonic()-10000
        for _ in range(4):worker.tick()
        self.assertIsNone(self.pool.status()['accounts'][0].get('last_control'))

    def test_challenge_does_not_poll_login_even_with_queued_work(self):
        self.submit('human-required','primary');self.pool.account_state('primary','browser_challenge',False)
        worker=self.worker('primary');worker.last_check=time.monotonic()-10000
        for _ in range(4):worker.tick()
        self.assertEqual(worker.recoveries,0)
        self.assertIsNone(self.pool.status()['accounts'][0].get('last_control'))

    def test_explicit_check_still_runs_for_challenged_account(self):
        self.pool.account_state('primary','browser_challenge',False)
        self.pool.control('primary','check');self.worker('primary').tick()
        self.assertEqual(self.pool.account('primary')['ready'],1)

if __name__=='__main__':unittest.main()
