"""Fair durable research polling. Website answers remain unverified evidence."""
import asyncio
import hashlib
import logging
import os
import re
import time


from osa.domain.work import clean_text, external_question
from osa.infrastructure.features.clients import BrowserWorkerClient
from osa.infrastructure.features.downloader import PublicDownloader

logger = logging.getLogger(__name__)


async def process_research_job(registry, browser, job):
    payload = job['payload']
    if payload.get('next_poll_at', 0) > time.time():
        return
    if job['group_id']:
        policy = await asyncio.to_thread(registry.policy, job['group_id'])
        if not policy.get('enabled') or not policy.get('research_external'):
            if payload.get('remote_id'):
                await browser.cancel(payload['remote_id'])
            await asyncio.to_thread(registry.update_record, job['id'], 'DEFERRED', {'reason': '原群外发研究授权已撤销'})
            return
    remote = payload.get('remote_id')
    if not remote:
        # Explicit interactive consultations are not nightly batch jobs.
        if not payload.get('interactive'):
            day = time.strftime('%Y-%m-%d', time.gmtime())
            if not await asyncio.to_thread(registry.budget, 'night:'+day, int(os.environ.get('OSA_NIGHTLY_RESEARCH_LIMIT', '20'))):
                await asyncio.to_thread(registry.update_record, job['id'], 'DEFERRED', {'reason': '夜间预算已用完'})
                return
        question = clean_text(payload['question'], 12000) if payload.get('interactive') else external_question(payload['question'])
        result = await browser.ask(question, job['id'], session_id=payload.get('session_id') or None,
            new_conversation=bool(payload.get('new_conversation', False)), attachments=payload.get('attachments') or [])
        remote = result['id']
        # The stable request ID makes recovery idempotent after a crash.
        await asyncio.to_thread(registry.update_record, job['id'], 'QUEUED', {
            'remote_id': remote, 'session_id': result.get('session_id') or payload.get('session_id') or job['id'],
            'queued_at': result.get('created_at') or time.time(), 'submitted_at': None, 'browser_status': result.get('status', 'queued')})
    result = await browser.status(remote)
    state = result['status']
    diagnostics = {'browser_status': state, 'submitted': bool(result.get('submitted')),
        'submitted_at': result.get('submitted_at') if result.get('submitted') else None,
        'error_code': result.get('error_code'), 'next_poll_at': 0}
    if state == 'succeeded':
        answer = clean_text(result.get('answer', ''), 40000)
        sources = []
        for url in list(dict.fromkeys(re.findall(r'https?://[^\s<>\]\)"，。]+', answer)))[:3]:
            try:
                raw, mime, final = await PublicDownloader(1_000_000).fetch(url)
                from bs4 import BeautifulSoup
                excerpt = BeautifulSoup(raw.decode(errors='replace'), 'html.parser').get_text(' ', strip=True)[:20000] if mime.startswith('text/') else ''
                sources.append({'url': final, 'fetched_at': time.time(), 'sha256': hashlib.sha256(raw).hexdigest(),
                    'mime': mime, 'bytes': len(raw), 'excerpt': clean_text(excerpt, 20000)})
            except Exception:
                sources.append({'url': url, 'status': 'unreachable'})
        claims = [{'text': line[:1000], 'status': 'INSUFFICIENT', 'supporting_source_ids': []}
            for line in answer.splitlines() if len(line.strip()) > 20][:12]
        await asyncio.to_thread(registry.update_record, job['id'], 'NEEDS_REVIEW', {**diagnostics,
            'answer': answer, 'sources': sources, 'claims': claims, 'verified_claims': 0, 'finished_at': time.time(),
            'reason': '模型回答和可访问链接不等于主张核实；需原文对应证据或本地复现'})
    elif state == 'waiting_human':
        await asyncio.to_thread(registry.update_record, job['id'], 'WAITING_HUMAN', {
            **diagnostics, 'reason': result.get('error') or '请在实时浏览器完成登录/验证'})
    elif state in {'failed', 'cancelled', 'deleted'}:
        await asyncio.to_thread(registry.update_record, job['id'], 'DEFERRED', {**diagnostics,
            'reason': result.get('error') or '网页研究未完成', 'finished_at': time.time()})
    else:
        confirmed = result.get('submitted') and state == 'running'
        await asyncio.to_thread(registry.update_record, job['id'], 'CONSULTING_MODEL' if confirmed else 'QUEUED', {
            **diagnostics, 'reason': '问题已发送，等待网页回复' if confirmed else '等待网页处理或确认发送'})


async def research_cycle(registry, browser):
    for deletion in await asyncio.to_thread(registry.records, 'erase_browser', admin=True):
        if deletion['state'] == 'pending':
            try:
                await browser.request('DELETE', '/tasks/'+deletion['payload']['remote_id'])
                await asyncio.to_thread(registry.update_record, deletion['id'], 'completed', {})
            except Exception as exc:
                logger.warning('deepseek_delete_failed request_id=%s error_type=%s', deletion['id'], type(exc).__name__)
    jobs = await asyncio.to_thread(registry.records, 'night_research', admin=True)
    for job in jobs:
        if job['state'] not in {'QUEUED', 'CONSULTING_MODEL', 'WAITING_HUMAN'}:
            continue
        try:
            await process_research_job(registry, browser, job)
        except Exception as exc:
            logger.warning('deepseek_poll_failed request_id=%s error_type=%s', job['id'], type(exc).__name__)
            await asyncio.to_thread(registry.update_record, job['id'], job['state'], {
                'next_poll_at': time.time()+10, 'poll_error_type': type(exc).__name__})
        # Browser execution is serial; durable polling must visit every job.


async def research_loop(registry):
    browser = BrowserWorkerClient(os.environ.get('OSA_BROWSER_WORKER_URL', 'http://browser-worker:8090'), os.environ.get('OSA_WORKER_TOKEN', ''))
    while True:
        try:
            await research_cycle(registry, browser)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning('deepseek_queue_failed error_type=%s', type(exc).__name__)
        await asyncio.sleep(5)
