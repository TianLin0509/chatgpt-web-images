'use strict';
// Local integration check (real Chrome, isolated profile): the image tab daemon honours the
// Hub's shared web-risk guard. Needs a Hub core that has web-risk-guard.js.
// Run: node scripts/e2e_hub_guard_daemon.cjs <hub core dir> <playwright index.js>
const fs = require('fs'), os = require('os'), path = require('path'), assert = require('assert/strict');
if (process.argv.length < 4) { console.error('usage: node scripts/e2e_hub_guard_daemon.cjs <hub core dir> <playwright index.js>'); process.exit(2); }
const hubCore = path.resolve(process.argv[2]);
const playwright = path.resolve(process.argv[3]);
const { TabDaemon } = require('./tab_daemon.cjs');
const { HubChrome } = require(path.join(hubCore, 'hub-chrome.js'));
const { BrowserTool } = require(path.join(hubCore, 'hub-browser-tool.js'));
const guard = require(path.join(hubCore, 'web-risk-guard.js'));
const sleep = ms => new Promise(r => setTimeout(r, ms));

(async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'images-guard-e2e-'));
  const env = { ...process.env, HUB_CHROME_ROOT: root };
  const hub = new HubChrome({ root, env });
  const lane = 'images-e2e';
  const evidence = {};
  try {
    await hub.ensure({ identityId: 'main' });
    await new BrowserTool({ id: lane, tool: 'images', identity: 'main', root, playwright }, { env, hub }).open('about:blank');
    const daemon = new TabDaemon({ identity: 'main', poolRoot: root, hubCore, root, playwright });
    assert.ok(daemon.guard, 'daemon loaded the Hub guard');
    const step = argv => daemon.handle({ lane, argv, timeoutMs: 40000 });

    evidence.firstGoto = (await step(['goto', 'https://example.com/'])).url;
    const { CDP } = require(path.join(hubCore, 'web-roundtable', 'cdp.js'));
    const ep = await hub.endpoint();
    if (daemon.scoped) {
      // Another tool's tab exists; while the lane holds its connection only the lane's tab is attached.
      await new BrowserTool({ id: 'bridge-e2e', tool: 'bridge', identity: 'main', root, playwright }, { env, hub }).open('about:blank');
      const laneTarget = JSON.parse(fs.readFileSync(path.join(root, 'tool-pages', lane + '.json'), 'utf8')).targetId;
      evidence.secondGoto = (await step(['goto', 'https://example.com/?again'])).url;
      const probeCdp = await CDP.connect(ep.ws, ep.port);
      const all = (await probeCdp.call('Target.getTargets')).targetInfos;
      probeCdp.close();
      evidence.laneAttached = !!all.find(t => t.targetId === laneTarget)?.attached;
      evidence.otherAttached = all.filter(t => t.targetId !== laneTarget && t.attached).map(t => t.type);
      assert.ok(daemon.lanes.has(lane), 'the lane holds its own page connection');
      assert.equal(evidence.laneAttached, true);
      assert.deepEqual(evidence.otherAttached, [], 'no other target is attached by the daemon');
      assert.equal(daemon.browser, null, 'no whole-browser connection');
    } else assert.ok(daemon.browser, 'daemon holds its whole-browser connection');

    evidence.handoff = await step(['human-open', 'https://example.com/']);
    assert.equal(evidence.handoff.handoff, true);
    assert.equal(daemon.browser, null, 'daemon detached for the person');
    assert.equal(daemon.lanes.size, 0, 'lane connections detached for the person');
    let cdp = await CDP.connect(ep.ws, ep.port);
    const pages = (await cdp.call('Target.getTargets')).targetInfos.filter(t => t.type === 'page');
    evidence.attachedPagesDuringHandoff = pages.filter(t => t.attached).length;
    assert.equal(evidence.attachedPagesDuringHandoff, 0, 'nothing attached to any page while the person works');
    await assert.rejects(step(['goto', 'https://example.com/']), /Human handoff/);
    evidence.refused = true;

    const lease = guard.handoff(root);
    await cdp.call('Target.closeTarget', { targetId: lease.targetId });
    cdp.close();
    await sleep(800);
    assert.equal(await daemon.yieldToPerson(), false, 'closing the window ends the handoff');
    evidence.resumedGoto = (await step(['goto', 'https://example.com/'])).url;

    // Tool code that reports a challenge: the daemon records it and leaves the page.
    const probe = path.join(root, 'probe.js');
    fs.writeFileSync(probe, 'async page => ({ challenge: true, auth_state: "browser_challenge" })');
    await step(['run-code', '--filename', probe]);
    cdp = await CDP.connect(ep.ws, ep.port);
    const record = JSON.parse(fs.readFileSync(path.join(root, 'tool-pages', lane + '.json'), 'utf8'));
    evidence.laneUrlAfterChallenge = (await cdp.call('Target.getTargets')).targetInfos.find(t => t.targetId === record.targetId)?.url;
    cdp.close();
    assert.equal(evidence.laneUrlAfterChallenge, 'about:blank');
    evidence.recorded = Object.keys(guard.read(root).sites);
    try { await daemon.browser?.close(); } catch {}
    for (const held of [...daemon.lanes.keys()]) daemon.dropLane(held);
    console.log(JSON.stringify({ ok: true, evidence }, null, 2));
  } catch (e) {
    console.log(JSON.stringify({ ok: false, error: e.message, evidence }, null, 2));
    process.exitCode = 1;
  } finally {
    try { await hub.close(); } catch {}
    try { fs.rmSync(root, { recursive: true, force: true, maxRetries: 10, retryDelay: 500 }); } catch {}
  }
})();
