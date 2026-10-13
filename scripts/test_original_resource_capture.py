"""Keep originals after the browser drops its old resource timing entries."""
import json
import shutil
import subprocess
from chatgpt_dom import ORIGINAL_CAPTURE_JS


def test_original_capture_survives_timing_buffer_overflow_and_is_bounded():
    script=r'''
const vm=require('vm'),assert=require('node:assert/strict');
const source=JSON.parse(require('fs').readFileSync(0,'utf8'));
let callback,observers=0;
const context={URL,performance:{getEntriesByType:()=>[{name:'https://chatgpt.com/static.js'}]},PerformanceObserver:class{constructor(fn){callback=fn;observers++;}observe(options){assert.equal(options.buffered,true);}}};
vm.runInNewContext(source,context);
vm.runInNewContext(source,context);
assert.equal(observers,1);
for(let n=0;n<150;n++)callback({getEntries:()=>[{name:'https://chatgpt.com/backend-api/estuary/content?id=file_'+n+'&p=fs'},{name:'https://foreign.example/image.png'}]});
assert.equal(context.__webImagesOriginalCapture.urls.length,128);
assert(context.__webImagesOriginalCapture.urls.at(-1).includes('file_149'));
assert(!JSON.stringify(context.__webImagesOriginalCapture.urls).includes('foreign.example'));
'''
    result=subprocess.run([shutil.which('node'),'-e',script],input=json.dumps(ORIGINAL_CAPTURE_JS),encoding='utf-8',capture_output=True,timeout=15)
    assert result.returncode==0,result.stderr
