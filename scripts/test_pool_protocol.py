"""Two real stdio clients and two independent workers; synthetic browser only."""
import asyncio
from datetime import timedelta
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

import web_images as w
from image_pool import Pool


def fixture_server():
    from image_service import create_mcp
    with patch.object(Pool, 'ensure_workers', return_value=[]):
        create_mcp().run(transport='stdio')


def fixture_worker(account):
    from image_worker import Worker, worker_lock
    from test_image_pool import FakeRuntime
    pool = Pool()
    class DelayedRuntime(FakeRuntime):
        def poll(self, job_id):
            if not hasattr(self, 'ready_at'):
                self.ready_at = time.monotonic() + 2
            if time.monotonic() < self.ready_at:
                return {'job_id': job_id, 'status': 'generating', 'files': []}
            return super().poll(job_id)
    with worker_lock(pool.root / ('worker-' + account + '.lock')) as acquired:
        if not acquired:
            return
        worker = Worker(pool, account, DelayedRuntime(pool.account(account)['data_dir']))
        while not (pool.root / 'fixture-stop').exists():
            pool.heartbeat(account)
            worker.tick()
            time.sleep(0.1)


async def check():
    from contextlib import AsyncExitStack
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    checks = []
    with tempfile.TemporaryDirectory() as folder:
        pool = Pool(Path(folder) / 'pool')
        env = {**os.environ, 'CHATGPT_WEB_IMAGES_POOL': str(pool.root), 'PYTHONIOENCODING': 'utf-8'}
        for account in ('primary', 'secondary'):
            data, config = Path(folder) / account / 'data', Path(folder) / account / 'config'
            w.write_json(config / 'settings.json', {'data_dir': str(data)})
            pool.add_account(account, str(data), str(config), enabled=True)
            pool.account_state(account, 'authenticated', True)
        workers = [subprocess.Popen([sys.executable, __file__, '--worker', account], env=env,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
                   for account in ('primary', 'secondary')]
        async def client(stack):
            params = StdioServerParameters(command=sys.executable, args=[__file__, '--server'], env=env)
            read, write = await stack.enter_async_context(stdio_client(params))
            session = await stack.enter_async_context(ClientSession(read, write, read_timeout_seconds=timedelta(seconds=15)))
            await session.initialize()
            return session
        try:
            async with AsyncExitStack() as stack:
                a, b = await client(stack), await client(stack)
                catalog = await a.list_tools()
                assert len(catalog.tools) == 9
                gen = next(t for t in catalog.tools if t.name == 'image_generate')
                assert gen.inputSchema['properties']['count']['maximum'] == 20
                checks.append('nine universal tools and bounded count schema')
                for bad in (0, 21, True, 1.5):
                    response = await a.call_tool('image_generate', {'prompt': 'fixture', 'count': bad})
                    assert response.isError
                checks.append('strict invalid counts rejected')
                async def submit(session, key):
                    response = await session.call_tool('image_generate', {'prompt': 'fixture', 'request_id': key, 'output_dir': str(Path(folder) / 'output')})
                    assert not response.isError and all(c.type == 'text' for c in response.content)
                    return json.loads(response.content[0].text)
                one, two = await asyncio.gather(submit(a, 'one'), submit(b, 'two'))
                repeat = await submit(b, 'one')
                assert repeat['job_id'] == one['job_id'] and repeat['reused_request']
                checks.append('cross-client persistent idempotency')
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    rows = [pool.row(j['job_id']) for j in (one, two)]
                    if all(r['status'] == 'running' for r in rows):
                        break
                    await asyncio.sleep(0.05)
                assert len({r['account_id'] for r in rows}) == 2
                assert all(r['status'] == 'running' for r in rows)
                checks.append('two workers executing overlapping jobs on separate accounts')
            # Both real MCP processes are now gone. Workers must finish without any poll.
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if all(pool.get(j['job_id'])['status'] == 'complete' for j in (one, two)):
                    break
                await asyncio.sleep(0.1)
            assert all(pool.get(j['job_id'])['status'] == 'complete' for j in (one, two))
            checks.append('both jobs complete after all clients disconnect')
            async with AsyncExitStack() as stack:
                resumed = await client(stack)
                response = await resumed.call_tool('image_poll', {'job_id': one['job_id'], 'wait_seconds': 0})
                assert not response.isError
                assert json.loads(response.content[0].text)['status'] == 'complete'
                assert response.structuredContent is None and 'base64' not in response.content[0].text
                checks.append('new client retrieves durable text-only result')
                batch = await resumed.call_tool('image_generate_batch', {
                    'requests': [{'prompt': 'deck %d' % i, 'request_id': 'deck%d' % i} for i in range(3)],
                    'output_dir': str(Path(folder) / 'output')})
                assert not batch.isError
                queued = json.loads(batch.content[0].text)
                assert queued['submitted'] == 3 and len(set(queued['job_ids'])) == 3
                checks.append('one call queues a whole batch')
                many = await resumed.call_tool('image_poll', {'job_ids': queued['job_ids'], 'wait_seconds': 0})
                assert not many.isError
                states = json.loads(many.content[0].text)
                assert states['total'] == 3 and len(states['jobs']) == 3
                checks.append('one call polls a whole batch')
                bad = await resumed.call_tool('image_poll', {'job_id': 'unknown'})
                assert bad.isError and 'job_not_found' in bad.content[0].text
                assert json.loads(bad.content[0].text)['error']['code'] == 'job_not_found'
                checks.append('unknown job errors preserved through stdio')
        finally:
            (pool.root / 'fixture-stop').touch()
            for process in workers:
                _, stderr = process.communicate(timeout=15)
                assert process.returncode == 0, stderr.decode('utf-8', errors='replace')
        print(json.dumps({'pool_protocol_checks': len(checks), 'checks': checks, 'passed': True, 'real_account_used': False}))


if __name__ == '__main__':
    if '--server' in sys.argv:
        fixture_server()
    elif '--worker' in sys.argv:
        fixture_worker(sys.argv[-1])
    else:
        asyncio.run(check())
