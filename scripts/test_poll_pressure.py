"""Real stdio pressure: four independent MCP clients, each with 24 outstanding waits."""
import asyncio
import concurrent.futures
from contextlib import AsyncExitStack
from datetime import timedelta
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import patch


async def server():
    from image_service import create_mcp
    from image_pool import Pool
    asyncio.get_running_loop().set_default_executor(concurrent.futures.ThreadPoolExecutor(max_workers=4))
    with patch.object(Pool, 'ensure_workers', return_value=[]):
        await create_mcp().run_stdio_async()


async def check():
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from image_pool import Pool
    import web_images as w
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp); pool = Pool(root/'pool')
        config = root/'config'; w.write_json(config/'settings.json', {'data_dir':str(root/'data')})
        pool.add_account('primary', str(root/'data'), str(config))
        job = pool.submit(prompt='Synthetic fixture, never sent.', request_id='pressure', output_dir=str(root/'output'))
        env = {**os.environ, 'CHATGPT_WEB_IMAGES_POOL':str(pool.root), 'PYTHONIOENCODING':'utf-8'}
        async with AsyncExitStack() as stack:
            clients=[]
            for _ in range(4):
                streams=await stack.enter_async_context(stdio_client(StdioServerParameters(command=sys.executable,args=[__file__,'--server'],env=env)))
                client=await stack.enter_async_context(ClientSession(*streams,read_timeout_seconds=timedelta(seconds=20)))
                await client.initialize();clients.append(client)
            pending=[asyncio.create_task(c.call_tool('image_poll',{'job_id':job['job_id'],'wait_seconds':2})) for c in clients for _ in range(24)]
            await asyncio.sleep(.35)
            async def status(c):
                started=time.monotonic(); result=await c.call_tool('image_status',{})
                assert not result.isError
                return time.monotonic()-started
            latencies=await asyncio.gather(*(status(c) for c in clients))
            results=await asyncio.gather(*pending)
            assert max(latencies)<1.5, latencies
            assert all(not r.isError and all(b.type=='text' for b in r.content) for r in results)
            print(json.dumps({'clients':4,'simultaneous_polls':96,'executor_threads_per_server':4,
                              'status_latency_seconds':latencies,'all_text_responses':True}))


if __name__=='__main__': asyncio.run(server() if '--server' in sys.argv else check())
