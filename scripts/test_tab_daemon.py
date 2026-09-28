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
        self.assertEqual(out['delegated'], {'delegated': 'open'})
        self.assertEqual(out['invalid'], 'Invalid request')

    def test_sources_parse(self):
        for name in ('tab_daemon.cjs', 'tab_client.cjs'):
            proc = subprocess.run([NODE, '--check', str(HERE / name)], capture_output=True, text=True,
                                  creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            self.assertEqual(proc.returncode, 0, proc.stderr)


if __name__ == '__main__':
    unittest.main()
