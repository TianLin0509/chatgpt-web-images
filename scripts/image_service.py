"""Agent-neutral stdio MCP and CLI over the durable account pool."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
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
    from mcp.types import CallToolResult, TextContent
    pool = pool or Pool()
    server = FastMCP('chatgpt-web-images', instructions=(
        'Shared durable local image queue for ALL agents. Run work in parallel on purpose.\n\n'
        'CONCURRENCY: one lane is one browser profile and runs one job at a time. Concurrency '
        'equals the number of lanes, NOT the number of ChatGPT accounts. image_status reports '
        'lanes and load per login. account_id=auto picks a free lane and balances across logins '
        'so no single account absorbs a burst.\n\n'
        'CHOOSE THE REQUEST SHAPE BEFORE CALLING:\n'
        '1. Several alternates of ONE prompt, for example multiple design options for one slide: '
        'use ONE image_generate with count=N. ChatGPT returns all N images from a single request, '
        'costing one lane and one account request instead of N. Do NOT split this into N jobs.\n'
        '2. Several DIFFERENT prompts, for example one image per slide: use ONE '
        'image_generate_batch call listing them all, so every free lane starts immediately.\n\n'
        'NEVER SERIALISE. Submit every job you already know about before polling anything. A '
        'submit-then-wait-then-submit loop leaves lanes idle and is the main cause of slow '
        'batches. Then wait on the whole batch with image_poll(job_ids=[...]). Workers keep '
        'generating and downloading after you disconnect, so polling only reads progress.\n\n'
'REFINING AN EARLIER IMAGE: pass continue_from=<job_id> to ask the follow-up inside that '
        'job\'s own conversation, so ChatGPT still sees those images and "keep the layout, darker '
        'palette" works like it does for a person. Start a fresh request instead when the new image '
        'is unrelated. Follow-ups are serialised per conversation and run on the login that owns it.\n\n'
        'queued, dispatching and running are normal; long queues are expected and are not a '
        'reason to resubmit. parked means the job released its lane so others could run and is '
        'retried automatically. needs_attention requires browser inspection then image_resume. '
        'Never change request_id or account_id to retry an uncertain submission. Only cancel on '
        'user request. Outputs are original local file paths and text metadata, never image base64.'))

    def encoded(result):
        text = json.dumps(result, ensure_ascii=False, separators=(',', ':'))
        if not result.get('ok', True):
            return CallToolResult(isError=True, content=[TextContent(type='text', text=text)])
        return text

    async def call(fn, **kwargs):
        return encoded(await asyncio.to_thread(invoke, fn, **kwargs))

    @server.tool(structured_output=False)
    async def image_status() -> str:
        """Read cached lane health, per-login load, running jobs, queue and account-action results. Does not navigate browsers."""
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
                             request_id: str = '', detail: bool = False, account_id: str = 'auto',
                             continue_from: Annotated[str, Field(description=(
                                 'job_id of a finished job whose conversation this request should continue. '
                                 'ChatGPT then still sees the earlier images, so refinements like "keep the '
                                 'layout, make the palette darker" work. Omit it to start a fresh conversation, '
                                 'which is right whenever the new request is unrelated.'))] = '') -> str:
        """Persist one image request and return job_id immediately. Pass continue_from=<job_id> to refine an earlier result inside its own conversation, which keeps ChatGPT's memory of those images; the follow-up automatically runs on the login that owns that conversation, and a conversation takes one writer at a time. Stable request_id deduplicates retries. Many callers may submit at once: every free lane runs a job in parallel and auto balances across logins. account_id accepts auto, a lane alias or a login name. count=N asks ONE ChatGPT request for N independent images: use it for alternates of the same prompt (for example several options for one slide) instead of submitting N jobs, because it costs one lane and one account request rather than N. Submit separate jobs only when the prompts genuinely differ."""
        return await call(lambda: wake(pool, pool.submit(account_id=account_id, prompt=prompt, prompt_file=prompt_file,
                                                       output_dir=output_dir, name=name, reference_images=reference_images,
                                                       count=count, request_id=request_id, continue_from=continue_from)))

    @server.tool(structured_output=False)
    async def image_generate_batch(
            requests: Annotated[list[dict], Field(description=(
                'One entry per DIFFERENT prompt, each an image_generate argument object with its own '
                'stable request_id. Use this instead of many image_generate calls so every free lane '
                'starts at once. For several alternates of the SAME prompt use image_generate with '
                'count=N instead, which is one ChatGPT request rather than several.'))],
            output_dir: str = '', count: Annotated[int, Field(strict=True, ge=1, le=20)] = 1,
            account_id: str = 'auto', detail: bool = False) -> str:
        """Queue a whole batch of different prompts in one call, filling every free lane at once. output_dir and count apply to entries that do not set their own. Everything is validated before anything is queued, and resending the same batch is idempotent per request_id."""
        shared = {'count': count}
        if output_dir:
            shared['output_dir'] = output_dir
        merged = [{**shared, **entry} if isinstance(entry, dict) else entry for entry in (requests or [])]
        return await call(lambda: wake(pool, pool.submit_many(merged, account_id=account_id)))

    @server.tool(structured_output=False)
    async def image_poll(job_id: str = '', job_ids: list[str] | None = None,
                         wait_seconds: Annotated[int, Field(strict=True, ge=0, le=45)] = 20,
                         detail: bool = False, return_when: str = 'all') -> str:
        """Read or wait for your jobs. Pass job_ids=[...] to wait on a whole batch in ONE call: polling job by job is what makes a parallel batch behave like a serial one. return_when='any' returns as soon as one job settles, 'all' waits for the batch. Workers save originals even if you never poll, and no browser operation extends this waiting budget."""
        try:
            start_errors = await asyncio.to_thread(pool.ensure_workers)
            if job_ids:
                result = await pool.poll_many_async(job_ids, wait_seconds, detail, return_when)
            elif job_id:
                await asyncio.to_thread(pool.row, job_id)
                result = await pool.poll_async(job_id, wait_seconds, detail)
            else:
                raise w.ImageError('invalid_job_id', 'Pass job_id for one job, or job_ids for a batch.')
            if start_errors:
                result['worker_start_errors'] = start_errors
        except w.ImageError as exc:
            result = {'ok': False, 'error': {'code': exc.code, 'message': str(exc)}}
        except Exception as exc:
            result = {'ok': False, 'error': {'code': 'service_error', 'message': type(exc).__name__}}
        return encoded(result)

    @server.tool(structured_output=False)
    async def image_cancel(job_id: str) -> str:
        """Only on user request: cancel a queued job or request cancellation of its own running browser job."""
        return await call(lambda: wake(pool, pool.cancel(job_id)))

    @server.tool(structured_output=False)
    async def image_resume(job_id: str) -> str:
        """After inspecting/fixing a needs_attention or parked account, resume the SAME job on the SAME account; never resubmits an uncertain job."""
        return await call(lambda: wake(pool, pool.resume(job_id)))

    return server


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['mcp','status','open','check','select-account','generate','generate-batch','poll','cancel','resume','account-add','account-clone','account-scale','account-reseed','account-enable','import-legacy'])
    parser.add_argument('--account-id', default='auto')
    parser.add_argument('--clone-from', default='')
    parser.add_argument('--lanes', type=int, default=0)
    parser.add_argument('--requests-file', default='')
    parser.add_argument('--account-name', default='')
    parser.add_argument('--no-remember', action='store_true')
    parser.add_argument('--prompt-file', default='')
    parser.add_argument('--prompt', default='')
    parser.add_argument('--output-dir', default='')
    parser.add_argument('--name', default='image')
    parser.add_argument('--reference', action='append', default=[])
    parser.add_argument('--count', type=int, default=1)
    parser.add_argument('--request-id', default='')
    parser.add_argument('--continue-from', default='')
    parser.add_argument('--job-id', action='append', default=[])
    parser.add_argument('--return-when', default='all')
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
        if args.command == 'account-clone':
            if not args.clone_from:
                raise w.ImageError('invalid_account_id', 'Pass --clone-from with the logged-in account to add a lane for.')
            return pool.clone_account(args.clone_from, account, enabled=not args.disabled)
        if args.command == 'account-scale':
            return pool.scale_account(account, args.lanes, enabled=not args.disabled)
        if args.command == 'account-reseed':
            return pool.reseed_account(account)
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
                               name=args.name, reference_images=args.reference, count=args.count, request_id=args.request_id,
                               continue_from=args.continue_from)
        if args.command == 'generate-batch':
            source = Path(args.requests_file)
            if not source.is_absolute() or not source.is_file():
                raise w.ImageError('invalid_batch', '--requests-file must be an existing absolute JSON file.')
            requests = json.loads(source.read_text(encoding='utf-8-sig'))
            shared = {'count': args.count}
            if args.output_dir:
                shared['output_dir'] = args.output_dir
            merged = [{**shared, **entry} if isinstance(entry, dict) else entry for entry in requests]
            return pool.submit_many(merged, account_id=args.account_id)
        if args.command == 'poll':
            start_errors = pool.ensure_workers()
            if len(args.job_id) > 1:
                result = asyncio.run(pool.poll_many_async(args.job_id, args.wait_seconds, args.detail, args.return_when))
            else:
                pool.row(args.job_id[0])
                result = pool.poll(args.job_id[0], args.wait_seconds, args.detail)
            if start_errors:
                result['worker_start_errors'] = start_errors
            return result
        return (pool.cancel if args.command == 'cancel' else pool.resume)(args.job_id[0])
    result = invoke(execute)
    if result.get('ok'):
        result = wake(pool, result)
    print(json.dumps(result, ensure_ascii=False, separators=(',', ':')))
    return 0 if result.get('ok', True) else 1


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    raise SystemExit(main())
