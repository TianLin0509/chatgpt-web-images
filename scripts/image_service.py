"""Agent-neutral stdio MCP and CLI over the durable account pool."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Annotated

from pydantic import Field
import web_images as w
from image_pool import Pool


def invoke(fn, **kwargs):
    try:
        return fn(**kwargs)
    except w.ImageError as exc:
        return {'ok': False, 'error': {'code': exc.code, 'message': str(exc)}}
    except Exception as exc:
        return {'ok': False, 'error': {'code': 'service_error', 'message': type(exc).__name__}}


def wake(pool, result):
    failures = pool.ensure_workers()
    if failures:
        result['worker_start_errors'] = failures
        result['next_action'] = 'fix_worker_start_then_poll_same_job'
    return result


def create_mcp(pool=None):
    from mcp.server.fastmcp import FastMCP
    from mcp.server.fastmcp.exceptions import ToolError
    pool = pool or Pool()
    server = FastMCP('chatgpt-web-images', instructions=(
        'Shared persistent local image queue for ALL agents. Submit once with a stable request_id. '
        'queued/running are normal: workers continue after you disconnect. Poll the SAME job_id '
        'with wait_seconds=20; never resend or switch accounts for uncertain submissions. '
        'account_id=auto selects an available independent account. needs_attention requires '
        'browser inspection then image_resume. Only cancel on user request. '
        'Outputs are original local file paths and text metadata, never image base64.'))

    def encoded(result):
        text = json.dumps(result, ensure_ascii=False, separators=(',', ':'))
        if not result.get('ok', True):
            raise ToolError(text)
        return text

    async def call(fn, **kwargs):
        return encoded(await asyncio.to_thread(invoke, fn, **kwargs))

    @server.tool(structured_output=False)
    async def image_status() -> str:
        """Read cached account health, running jobs, queue and account-action results. Does not navigate browsers."""
        return await call(lambda: wake(pool, pool.status()))

    @server.tool(structured_output=False)
    async def image_open(account_id: str = 'primary') -> str:
        """Queue opening this account's isolated login browser; inspect image_status for completion."""
        return await call(lambda: wake(pool, pool.control(account_id, 'open')))

    @server.tool(structured_output=False)
    async def image_account_check(account_id: str = 'primary') -> str:
        """After the user logs in, queue a login check for this account; image_status reports the result."""
        return await call(lambda: wake(pool, pool.control(account_id, 'check')))

    @server.tool(structured_output=False)
    async def image_select_account(account_name: str, remember: bool = True, account_id: str = 'primary') -> str:
        """Select only a user-authorized remembered account in the specified isolated profile; never handles credentials."""
        return await call(lambda: wake(pool, pool.control(account_id, 'select', account_name=account_name, remember=remember)))

    @server.tool(structured_output=False)
    async def image_generate(prompt: str = '', prompt_file: str = '', output_dir: str = '', name: str = 'image',
                             reference_images: list[str] | None = None,
                             count: Annotated[int, Field(strict=True, ge=1, le=20)] = 1,
                             request_id: str = '', detail: bool = False, account_id: str = 'auto') -> str:
        """Persist one image request and return job_id immediately. Stable request_id deduplicates retries. Multiple callers queue safely; workers generate/download independently. count is images in one request."""
        return await call(lambda: wake(pool, pool.submit(account_id=account_id, prompt=prompt, prompt_file=prompt_file,
                                                       output_dir=output_dir, name=name, reference_images=reference_images,
                                                       count=count, request_id=request_id)))

    @server.tool(structured_output=False)
    async def image_poll(job_id: str, wait_seconds: Annotated[int, Field(strict=True, ge=0, le=45)] = 20,
                         detail: bool = False) -> str:
        """Read or wait for this job only. The background worker saves originals even without polling. No browser operation extends this waiting budget."""
        def poll_awake():
            start_errors = pool.ensure_workers()
            result = pool.poll(job_id, wait_seconds, detail)
            if start_errors:
                result['worker_start_errors'] = start_errors
            return result
        return await call(poll_awake)

    @server.tool(structured_output=False)
    async def image_cancel(job_id: str) -> str:
        """Only on user request: cancel a queued job or request cancellation of its own running browser job."""
        return await call(lambda: wake(pool, pool.cancel(job_id)))

    @server.tool(structured_output=False)
    async def image_resume(job_id: str) -> str:
        """After inspecting/fixing a needs_attention account, resume the SAME job on the SAME account; never resubmits an uncertain job."""
        return await call(lambda: wake(pool, pool.resume(job_id)))

    return server


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['mcp','status','open','check','select-account','generate','poll','cancel','resume','account-add','account-enable','import-legacy'])
    parser.add_argument('--account-id', default='auto')
    parser.add_argument('--account-name', default='')
    parser.add_argument('--no-remember', action='store_true')
    parser.add_argument('--prompt-file', default='')
    parser.add_argument('--prompt', default='')
    parser.add_argument('--output-dir', default='')
    parser.add_argument('--name', default='image')
    parser.add_argument('--reference', action='append', default=[])
    parser.add_argument('--count', type=int, default=1)
    parser.add_argument('--request-id', default='')
    parser.add_argument('--job-id', default='')
    parser.add_argument('--wait-seconds', type=int, default=0)
    parser.add_argument('--detail', action='store_true')
    parser.add_argument('--data-dir', default='')
    parser.add_argument('--config-dir', default='')
    parser.add_argument('--disabled', action='store_true')
    args = parser.parse_args(argv)
    pool = Pool()
    if args.command == 'mcp':
        create_mcp(pool).run(transport='stdio')
        return 0
    account = 'primary' if args.account_id == 'auto' else args.account_id
    def execute():
        if args.command == 'account-add':
            row = pool.add_account(account, args.data_dir, args.config_dir, enabled=not args.disabled)
            return {'ok': True, 'account_id': row['id'], 'enabled': bool(row['enabled'])}
        if args.command == 'account-enable':
            return pool.enable(account, not args.disabled)
        if args.command == 'import-legacy':
            return pool.import_legacy(account)
        if args.command == 'status':
            return pool.status()
        if args.command in {'open','check','select-account'}:
            kwargs = {'account_name': args.account_name, 'remember': not args.no_remember} if args.command == 'select-account' else {}
            return pool.control(account, 'select' if args.command == 'select-account' else args.command, **kwargs)
        if args.command == 'generate':
            return pool.submit(account_id=args.account_id, prompt=args.prompt, prompt_file=args.prompt_file, output_dir=args.output_dir,
                               name=args.name, reference_images=args.reference, count=args.count, request_id=args.request_id)
        if args.command == 'poll':
            return pool.poll(args.job_id, args.wait_seconds, args.detail)
        return (pool.cancel if args.command == 'cancel' else pool.resume)(args.job_id)
    result = invoke(execute)
    if result.get('ok'):
        result = wake(pool, result)
    print(json.dumps(result, ensure_ascii=False, separators=(',', ':')))
    return 0 if result.get('ok', True) else 1


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    raise SystemExit(main())
