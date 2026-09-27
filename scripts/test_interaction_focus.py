import json
import shutil
import subprocess
from chatgpt_interaction import interactive


def test_focus_targets_owned_page_and_restores_on_success_and_failure():
    script = r'''
const assert=require('assert');const spec=JSON.parse(require('fs').readFileSync(0,'utf8'));
(async()=>{
 for(const focused of [false,true])for(const hidden of [false,true])for(const fail of [false,true]){
  const events=[];global.document={hasFocus:()=>focused,hidden};const page={evaluate:async fn=>fn(),context:()=>({newCDPSession:async target=>{
   assert.strictEqual(target,page);events.push('own');return{send:async(method,p)=>events.push([method,p.enabled]),detach:async()=>events.push('detach')};}})};
  const fn=new Function('return ('+spec[fail?'failure':'success']+')')();
  if(fail)await assert.rejects(()=>fn(page),/original failure/);else assert.equal(await fn(page),42);
  assert.deepEqual(events,focused&&!hidden?['own','detach']:['own',['Emulation.setFocusEmulationEnabled',true],['Emulation.setFocusEmulationEnabled',false],'detach']);
 }
 console.log('8 scoped focus cases passed');
})().catch(e=>{console.error(e);process.exit(1)});
'''
    result = subprocess.run([shutil.which('node'), '-e', script], input=json.dumps({
        'success': interactive('async page => 42'),
        'failure': interactive("async page => {throw Error('original failure')}"),
    }), encoding='utf-8', capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr


def test_focus_detaches_when_restore_fails():
    script = r'''
const assert=require('assert');const source=JSON.parse(require('fs').readFileSync(0,'utf8'));let detached=false;
const page={evaluate:async()=>false,context:()=>({newCDPSession:async()=>({send:async(m,p)=>{if(!p.enabled)throw Error('restore failed')},detach:async()=>{detached=true}})})};
(async()=>{await assert.rejects(()=>new Function('return ('+source+')')()(page),/restore failed/);assert(detached)})().catch(e=>{console.error(e);process.exit(1)});
'''
    result = subprocess.run([shutil.which('node'), '-e', script], input=json.dumps(interactive('async page => true')),
                            encoding='utf-8', capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr
