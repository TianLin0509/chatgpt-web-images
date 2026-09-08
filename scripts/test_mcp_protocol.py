"""Real stdio transport with deterministic browser fixtures; no account required."""
import asyncio
from datetime import timedelta
import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

import web_images as w


def fixture_server():
    with tempfile.TemporaryDirectory() as directory:
        w.DATA=Path(directory)
        w.DEFAULT_OUTPUT=w.DATA/'output'
        def poll(job_id):
            if job_id=='error':
                raise w.ImageError('browser_timeout','Inspect the current job; do not resubmit.')
            return {'job_id':job_id,'status':'complete','requested_count':5,'observed_count':5,
                'downloaded_count':5,'missing_count':0,'extra_count':0,'count_match':True,'files':[],
                'prompt_sha256':'a'*64,'provider':'chatgpt-web','image_model_verified':False}
        with patch.object(w,'poll',side_effect=poll),patch.object(w,'ensure_browser',return_value={'logged_in':True}),patch.object(w,'cli'),patch.object(w,'set_visible',return_value=True),patch.object(w,'run_js',side_effect=lambda source,**kw: {'url':'https://chatgpt.com/c/fixture'} if 'button.click' in source else True):
            w.serve()


async def check():
    from mcp import ClientSession,StdioServerParameters
    from mcp.client.stdio import stdio_client
    params=StdioServerParameters(command=sys.executable,args=[str(Path(__file__).resolve()),'--server'])
    checks=0
    async with stdio_client(params) as (read,write):
        async with ClientSession(read,write,read_timeout_seconds=timedelta(seconds=30)) as session:
            await session.initialize()
            catalog=await session.list_tools()
            assert len(catalog.tools)==5
            gen=next(t for t in catalog.tools if t.name=='image_generate')
            poll=next(t for t in catalog.tools if t.name=='image_poll')
            assert gen.inputSchema['properties']['count']['maximum']==20
            assert poll.inputSchema['properties']['wait_seconds']['maximum']==45
            assert 'request_id' in gen.inputSchema['properties']
            checks+=1
            for count in (True,0,21,1.5):
                response=await session.call_tool('image_generate',{'prompt':'fixture','count':count})
                assert response.isError
                checks+=1
            first=await session.call_tool('image_generate',{'prompt':'fixture','count':5,'request_id':'protocol-1'})
            assert not first.isError
            assert all(block.type=='text' for block in first.content)
            first_data=json.loads(first.content[0].text)
            assert first_data['requested_count']==5
            repeat=await session.call_tool('image_generate',{'prompt':'fixture','count':5,'request_id':'protocol-1'})
            repeated=json.loads(repeat.content[0].text)
            assert repeated['reused_request'] and repeated['job_id']==first_data['job_id']
            checks+=1
            compact=await session.call_tool('image_poll',{'job_id':'fixture','wait_seconds':0})
            full=await session.call_tool('image_poll',{'job_id':'fixture','wait_seconds':0,'detail':True})
            assert not compact.isError and not full.isError
            assert all(b.type=='text' for b in compact.content)
            assert compact.structuredContent is None
            assert 'prompt_sha256' not in json.loads(compact.content[0].text)
            assert 'prompt_sha256' in json.loads(full.content[0].text)
            assert 'data:image/' not in compact.content[0].text
            checks+=1
            bad=await session.call_tool('image_poll',{'job_id':'error','wait_seconds':0})
            assert bad.isError and 'browser_timeout' in bad.content[0].text
            checks+=1
    print(json.dumps({'protocol_checks':checks,'passed':True,'account_used':False}))


if __name__=='__main__':
    if '--server' in sys.argv:fixture_server()
    else:asyncio.run(check())
