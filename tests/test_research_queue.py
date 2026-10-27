"""Durable DeepSeek queue fairness and truthful state propagation."""
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

from osa.infrastructure.v3.registry import Registry
from osa.infrastructure.v2.research_processor import research_cycle
from test_v2 import V2AsyncCase


class ResearchQueueTests(V2AsyncCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.registry=Registry(str(Path(self.temp.name)/'registry.db'))
        self.browser=AsyncMock()

    async def asyncTearDown(self):
        self.temp.cleanup()

    def job(self, key, **payload):
        return self.registry.record('night_research','u',None,
            {'question':'测试问题','interactive':True,**payload},key,'QUEUED')

    async def test_waiting_task_does_not_starve_later_job(self):
        first=self.job('first',remote_id='remote-first')
        second=self.job('second')
        self.browser.ask.return_value={'id':second,'status':'queued'}
        async def status(remote):
            if remote=='remote-first':return {'status':'waiting_human','error':'需要登录'}
            return {'status':'queued','submitted':False}
        self.browser.status.side_effect=status
        with patch.dict(os.environ,{'OSA_WORK_MODEL_GATEWAY_URL':''}):
            await research_cycle(self.registry,self.browser)
        self.browser.ask.assert_awaited_once()
        self.assertEqual(self.browser.ask.await_args.args[0],'测试问题')
        rows={row['id']:row for row in self.registry.records('night_research',admin=True)}
        self.assertEqual(rows[first]['state'],'WAITING_HUMAN')
        self.assertEqual(rows[second]['state'],'QUEUED')
        self.assertIsNone(rows[second]['payload']['submitted_at'])

    async def test_one_broken_remote_does_not_block_another_result(self):
        first=self.job('first',remote_id='broken')
        second=self.job('second',remote_id='healthy')
        async def status(remote):
            if remote=='broken':raise TimeoutError('not reachable')
            return {'status':'succeeded','submitted':True,'answer':'有依据的测试回答，但网页回答本身仍然不自动当作已核实事实。'}
        self.browser.status.side_effect=status
        await research_cycle(self.registry,self.browser)
        rows={row['id']:row for row in self.registry.records('night_research',admin=True)}
        self.assertEqual(rows[second]['state'],'NEEDS_REVIEW')
        self.assertEqual(rows[first]['payload']['poll_error_type'],'TimeoutError')

    async def test_failure_reason_reaches_parent_and_is_not_misreported_as_login(self):
        jid=self.job('failure',remote_id='remote')
        self.browser.status.return_value={'status':'failed','submitted':False,
            'error_code':'SEND_NOT_CONFIRMED','error':'网页未确认收到问题'}
        await research_cycle(self.registry,self.browser)
        row=self.registry.records('night_research',admin=True)[0]
        self.assertEqual(row['id'],jid)
        self.assertEqual(row['state'],'DEFERRED')
        self.assertEqual(row['payload']['reason'],'网页未确认收到问题')
        self.assertEqual(row['payload']['error_code'],'SEND_NOT_CONFIRMED')
