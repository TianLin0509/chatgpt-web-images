"""Tab daemon: lanes of one identity run in parallel, steps of one lane stay ordered, and a
stuck step cannot block its lane. Uses a fake Hub core; no browser or ChatGPT involved."""
import json
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
NODE = shutil.which('node') or shutil.which('node.exe') or ''

FAKE_TOOL = textwrap.dedent('''
  'use strict';
  class BrowserTool {
    constructor(binding) { this.binding = binding; }
    async target() { return { targetId: 't-' + this.binding.id, ep: { port: 1, ws: 'ws://fake' } }; }
    async execute(argv) { return { delegated: argv[0] }; }
  }
  function argumentsOf(argv) { return argv.filter(a => a !== '--json'); }
  module.exports = { BrowserTool, argumentsOf };
''')
FAKE_COMPAT = "module.exports = { adaptSource: s => s };\n"

SCRIPT = textwrap.dedent('''
  const { TabDaemon } = require(process.argv[2]);
  const d = new TabDaemon({ identity: 'main', poolRoot: process.argv[3], hubCore: process.argv[3], root: process.argv[3], playwright: 'unused' });
  const fs = require('fs'), path = require('path');
  // A fake attached page per lane: the daemon only needs isClosed/once and the step function.
  d.page = async lane => ({ lane, isClosed: () => false, once() {} });
  const file = (name, src) => { const f = path.join(process.argv[3], name); fs.writeFileSync(f, src); return f; };
  const sleep = file('sleep.js', 'async page => { await new Promise(r => setTimeout(r, 400)); return Date.now(); }');
  const hang = file('hang.js', 'async page => new Promise(() => {})');
  const quick = file('quick.js', 'async page => page.lane');
  const boom = file('boom.js', 'async page => { throw new Error("boom"); }');
  (async () => {
    const out = {};
    let t = Date.now();
    await Promise.all([d.handle({ lane: 'a', argv: ['run-code', '--filename', sleep], timeoutMs: 5000 }),
                       d.handle({ lane: 'b', argv: ['run-code', '--filename', sleep], timeoutMs: 5000 })]);
    out.parallel_ms = Date.now() - t;
    t = Date.now();
    await Promise.all([d.handle({ lane: 'a', argv: ['run-code', '--filename', sleep], timeoutMs: 5000 }),
                       d.handle({ lane: 'a', argv: ['run-code', '--filename', sleep], timeoutMs: 5000 })]);
    out.same_lane_ms = Date.now() - t;
    try { await d.handle({ lane: 'c', argv: ['run-code', '--filename', hang], timeoutMs: 300 }); out.hang = 'returned'; }
    catch (e) { out.hang = String(e.message).slice(0, 40); }
    out.after_hang = await d.handle({ lane: 'c', argv: ['run-code', '--filename', quick], timeoutMs: 1000 });
    // A failing step must reach its caller without terminating the process (unobserved
    // rejections are fatal in Node); the lane keeps working afterwards.
    try { await d.handle({ lane: 'e', argv: ['run-code', '--filename', boom], timeoutMs: 1000 }); out.boom = 'returned'; }
    catch (e) { out.boom = e.message; }
    await new Promise(r => setTimeout(r, 50));
    out.after_boom = await d.handle({ lane: 'e', argv: ['run-code', '--filename', quick], timeoutMs: 1000 });
    out.delegated = await d.handle({ lane: 'a', argv: ['open', 'about:blank'], timeoutMs: 1000 });
    try { await d.handle({ lane: 'Bad Lane', argv: [], timeoutMs: 1000 }); } catch (e) { out.invalid = e.message; }
    console.log(JSON.stringify(out));
    process.exit(0);
  })();
''')


@unittest.skipUnless(NODE, 'node is required for the tab daemon')
class TabDaemonTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / 'hub-browser-tool.js').write_text(FAKE_TOOL, encoding='utf-8')
        (self.tmp / 'chatgpt-selector-compat.js').write_text(FAKE_COMPAT, encoding='utf-8')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_script(self):
        script = self.tmp / 'drive.cjs'
        script.write_text(SCRIPT, encoding='utf-8')
        proc = subprocess.run([NODE, str(script), str(HERE / 'tab_daemon.cjs'), str(self.tmp)],
                              capture_output=True, text=True, encoding='utf-8', timeout=60,
                              creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.assertEqual(proc.returncode, 0, proc.stderr[-800:])
        return json.loads(proc.stdout.strip().splitlines()[-1])

    def test_lanes_parallel_steps_ordered_and_hang_isolated(self):
        out = self.run_script()
        self.assertLess(out['parallel_ms'], 750, 'two lanes must not wait for each other')
        self.assertGreaterEqual(out['same_lane_ms'], 780, 'steps of one lane must run one at a time')
        self.assertIn('Timeout', out['hang'])
        self.assertEqual(out['after_hang'], 'c', 'a stuck step must not block the next step of its lane')
        self.assertEqual(out['boom'], 'boom')
        self.assertEqual(out['after_boom'], 'e', 'a failed step must not terminate the daemon')
        self.assertEqual(out['delegated'], {'delegated': 'open'})
        self.assertEqual(out['invalid'], 'Invalid request')

    def test_sources_parse(self):
        for name in ('tab_daemon.cjs', 'tab_client.cjs'):
            proc = subprocess.run([NODE, '--check', str(HERE / name)], capture_output=True, text=True,
                                  creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_failures_use_shared_guard_without_replaying_the_step(self):
        script = self.tmp / 'protected.cjs'
        script.write_text('''
const {TabDaemon}=require(process.argv[2]);
const d=new TabDaemon({identity:'main',root:process.argv[3],poolRoot:process.argv[3],hubCore:process.argv[3],playwright:'unused'});
let calls=0,inspected=0;
d.guard={runProtected:async(root,options,fn)=>{try{return await fn();}catch(e){inspected++;if(options.identity!=='main'||options.source!=='lane-a')throw Error('wrong identity');throw Error('Site challenged');}}};
(async()=>{let error='';try{await d.protectedStep('lane-a',{},async()=>{calls++;throw Error('Timeout');});}catch(e){error=e.message;}
console.log(JSON.stringify({calls,inspected,error}));})().catch(()=>process.exit(1));
''', encoding='utf-8')
        proc = subprocess.run([NODE, str(script), str(HERE / 'tab_daemon.cjs'), str(self.tmp)], capture_output=True, text=True, timeout=10, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout), {'calls': 1, 'inspected': 1, 'error': 'Site challenged'})


REAP_SCRIPT = textwrap.dedent("""
  const { TabDaemon } = require(process.argv[2]);
  const root = process.argv[3];
  const fs = require('fs'), path = require('path');
  const d = new TabDaemon({ identity: 'main', poolRoot: root, hubCore: root, root, playwright: 'unused' });
  fs.mkdirSync(path.join(root, 'tool-pages'), { recursive: true });
  const rec = (lane, targetId) => fs.writeFileSync(path.join(root, 'tool-pages', lane + '.json'), JSON.stringify({ targetId, browserWs: 'ws://fake', identity: 'main' }));
  const closed = [];
  const alive = new Set(['T-live', 'T-orphan', 'T-user', 'T-reused']);
  d.endpoint = async () => ({ port: 1, ws: 'ws://fake' });
  d.connection = async () => ({ newBrowserCDPSession: async () => ({
    send: async (m, a) => { if (m === 'Target.getTargets') return { targetInfos: [...alive].map(targetId => ({ targetId })) };
                            if (m === 'Target.closeTarget') { closed.push(a.targetId); alive.delete(a.targetId); return {}; } },
    detach: async () => {} }) });
  d.findPage = async (browser, id) => alive.has(id) ? { isClosed: () => false, once() {} } : null;
  (async () => {
    const out = {};
    rec('images-a', 'T-reused');
    let delegated = 0;
    const tool = { execute: async () => { delegated++; return { targetId: 'T-new', reused: false }; } };
    out.reuse = await d.open('images-a', tool, ['open', 'about:blank']);
    out.delegated_after_reuse = delegated;
    rec('images-b', 'T-gone');
    out.fresh = await d.open('images-b', tool, ['open', 'about:blank']);
    out.delegated_after_fresh = delegated;
    // T-orphan was opened for a lane earlier; its lane record now points elsewhere.
    d.register('images-c', 'T-orphan');
    rec('images-c', 'T-live'); d.register('images-c', 'T-live');
    out.reaped = await d.reap();
    out.closed = closed;
    out.registry = JSON.parse(fs.readFileSync(d.registryPath(), 'utf8')).tabs.map(t => t.targetId).sort();
    console.log(JSON.stringify(out));
    process.exit(0);
  })();
""")


@unittest.skipUnless(NODE, 'node is required for the tab daemon')
class TabRegistryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / 'hub-browser-tool.js').write_text(FAKE_TOOL, encoding='utf-8')
        (self.tmp / 'chatgpt-selector-compat.js').write_text(FAKE_COMPAT, encoding='utf-8')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_reuse_existing_tab_and_reap_only_unowned_registered_tabs(self):
        script = self.tmp / 'reap.cjs'
        script.write_text(REAP_SCRIPT, encoding='utf-8')
        proc = subprocess.run([NODE, str(script), str(HERE / 'tab_daemon.cjs'), str(self.tmp)],
                              capture_output=True, text=True, encoding='utf-8', timeout=60,
                              creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.assertEqual(proc.returncode, 0, proc.stderr[-800:])
        out = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertEqual(out['reuse'], {'targetId': 'T-reused', 'reused': True})
        self.assertEqual(out['delegated_after_reuse'], 0, 'an existing recorded tab must not cause a second tab')
        self.assertEqual(out['fresh'], {'targetId': 'T-new', 'reused': False})
        self.assertEqual(out['delegated_after_fresh'], 1)
        self.assertEqual(out['closed'], ['T-orphan'], 'only a registered tab that no lane owns is closed')
        self.assertNotIn('T-user', out['closed'], 'tabs the daemon never registered are never touched')
        self.assertEqual(out['registry'], ['T-live', 'T-reused'])


FAKE_SCOPED = textwrap.dedent('''
  // Records every connection; a connection sees one page.
  const opened = [];
  async function connectPage(chromium, ep, targetId) {
    const listeners = {}, state = { closed: false, connected: true };
    const connection = { targetId, state,
      page: { url: () => 'https://chatgpt.com/', isClosed: () => state.closed, once: (e, f) => { listeners[e] = f; } },
      browser: { isConnected: () => state.connected, on: (e, f) => { listeners['b' + e] = f; } },
      close: async () => { state.connected = false; } };
    opened.push(connection);
    return connection;
  }
  module.exports = { connectPage, opened };
''')

SCOPED_SCRIPT = textwrap.dedent("""
  const { TabDaemon } = require(process.argv[2]);
  const root = process.argv[3], fs = require('fs'), path = require('path');
  const d = new TabDaemon({ identity: 'main', poolRoot: root, hubCore: root, root, playwright: path.join(root, 'pw.js') });
  const { opened } = require(path.join(root, 'scoped-cdp.js'));
  const quick = path.join(root, 'quick.js'); fs.writeFileSync(quick, 'async page => page.url()');
  (async () => {
    const out = { scoped: !!d.scoped };
    const step = (lane, argv) => d.handle({ lane, argv, timeoutMs: 2000 });
    await step('images-a', ['run-code', '--filename', quick]);
    await step('images-a', ['run-code', '--filename', quick]);
    await step('images-b', ['run-code', '--filename', quick]);
    out.connections = opened.map(c => c.targetId);
    out.whole_browser = d.browser;
    await step('images-a', ['close']);
    out.after_close = [...d.lanes.keys()];
    out.a_closed = !opened[0].state.connected;
    d.guard = { handoff: () => ({ identity: 'main' }), assertAutomationAllowed() {} };
    out.yielded = await d.yieldToPerson();
    out.after_handoff = [...d.lanes.keys()];
    console.log(JSON.stringify(out));
    process.exit(0);
  })();
""")


@unittest.skipUnless(NODE, 'node is required for the tab daemon')
class ScopedLaneTest(unittest.TestCase):
    """With a Hub that offers scoped-cdp.js each lane attaches to its own page only."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / 'hub-browser-tool.js').write_text(FAKE_TOOL, encoding='utf-8')
        (self.tmp / 'chatgpt-selector-compat.js').write_text(FAKE_COMPAT, encoding='utf-8')
        (self.tmp / 'scoped-cdp.js').write_text(FAKE_SCOPED, encoding='utf-8')
        (self.tmp / 'pw.js').write_text('module.exports = { chromium: {} };', encoding='utf-8')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_each_lane_attaches_to_its_own_page_and_lets_go_for_a_person(self):
        script = self.tmp / 'scoped.cjs'
        script.write_text(SCOPED_SCRIPT, encoding='utf-8')
        proc = subprocess.run([NODE, str(script), str(HERE / 'tab_daemon.cjs'), str(self.tmp)],
                              capture_output=True, text=True, encoding='utf-8', timeout=60,
                              creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.assertEqual(proc.returncode, 0, proc.stderr[-800:])
        out = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertTrue(out['scoped'])
        self.assertEqual(out['connections'], ['t-images-a', 't-images-b'], 'one connection per lane, reused across its steps')
        self.assertIsNone(out['whole_browser'], 'no whole-browser connection')
        self.assertEqual(out['after_close'], ['images-b'])
        self.assertTrue(out['a_closed'])
        self.assertTrue(out['yielded'])
        self.assertEqual(out['after_handoff'], [], 'a person verifying gets every lane connection closed')


if __name__ == '__main__':
    unittest.main()
