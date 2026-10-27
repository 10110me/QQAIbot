"""End-to-end contracts for the unified local flow; no live QQ/model traffic."""
import base64
import json
import tempfile
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx

from osa.application.v3.tools import AgentTools
from osa.application.v3.thinking import ThinkingLoop, ReplyWriter
from osa.application.v3.workspace import WorkspaceAgent
from osa.application.resources import ResourceService
from osa.domain.entities import GroupFile, GroupPolicy, Memory, ModelResult, Scope
from osa.domain.features import Record, ParserRejected
from osa.domain.work import decode_input_files
from osa.main import create_app
from test_core import demo_settings, make_event, FakeResources
from test_v2 import V2AsyncCase


def result(value):
    return ModelResult(json.dumps(value, ensure_ascii=False), 'test', 'work')


class UnifiedFlowTests(V2AsyncCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        config = demo_settings().model_copy(update={'data_dir': self.temp.name,
            'workspace_dir': self.temp.name + '/work', 'v2_database_url': self.temp.name + '/registry.db',
            'v2_enabled': True, 'v3_enabled': True, 'work_enabled': True})
        self.app = create_app(config)
        self.store = self.app.state.store
        await self.store.initialize(['10001'])
        await self.store.update_policy('10001', GroupPolicy(sandbox=True, media_read=True,
            file_content_index=True, file_metadata_read=True, notice_read=True, memory_extract=True))
        self.event = make_event(message_id='unified-source', text='请依据文件编写程序')
        await self.store.publish(self.event, True)
        self.v2 = self.app.state.v2
        self.context = {'actor_id': self.event.actor_id, 'group_id': '10001', 'source_id': self.event.id,
            'source_event': self.event.to_dict(), 'deadline': time.time() + 60,
            'session_id': 'unified', 'tool_call_id': 'one', 'dependency_snapshot': {}}
        self.v2.registry.begin_thinking('unified', self.context)
        self.features = self.app.state.features
        adapter = FakeResources()
        adapter.files = AsyncMock(return_value=[GroupFile('f1', '要求.txt', 12, busid=5)])
        adapter.file_url = AsyncMock(return_value='https://files.example.test/f1')
        self.resources = ResourceService(self.store, adapter)
        self.features.downloader.fetch = AsyncMock(return_value=('计算总和'.encode(), 'text/plain', 'https://files.example.test/f1'))
        self.tools = AgentTools(self.v2, self.store, features=self.features, resources=self.resources)

    async def asyncTearDown(self):
        await self.features.learning.shutdown()
        self.temp.cleanup()

    async def tool(self, name, args, call_id):
        return await self.tools.execute(name, args, {**self.context, 'tool_call_id': call_id})

    async def test_download_then_delegate_binds_real_bytes_and_auto_queues_once(self):
        downloaded = await self.tool('group.file.download', {'file_id': 'f1'}, 'download')
        self.assertNotIn('error', downloaded)
        self.assertIn('要求.txt', downloaded['name'])
        self.assertEqual(Path(downloaded['shared_path']).read_bytes(), '计算总和'.encode())
        self.context['dependency_snapshot']['shared_files'] = downloaded['_shared_files']
        task = await self.tool('coding.submit', {'goal': '读取要求.txt 并编写程序'}, 'submit')
        self.assertEqual(task['status'], 'QUEUED')
        duplicate = await self.tool('coding.submit', {'goal': '读取要求.txt 并编写程序'}, 'submit')
        self.assertEqual(duplicate, task)
        claimed = self.v2.registry.claim('test-worker')
        inputs = await self.features.work_inputs.for_task(claimed, claimed['generation'])
        self.assertEqual(len(inputs), 1)
        self.assertEqual((Path(self.temp.name)/'work'/claimed['id']/'generation-1'/inputs[0]['workspace_input']).read_bytes(), '计算总和'.encode())

    async def test_group_tools_cannot_bypass_policy_and_sources_join_snapshot(self):
        notices = await self.tool('group.notice.read', {}, 'notice')
        self.assertEqual(notices['_resources_used'], ['notices'])
        await self.store.update_policy('10001', GroupPolicy())
        denied = await self.tool('group.file.download', {'file_id': 'f1'}, 'denied')
        self.assertEqual(denied['error'], 'PermissionError')

    async def test_media_input_is_shared_with_work_and_deepseek_and_recall_revokes(self):
        self.features.media_inputs[self.event.id] = [{'url': 'https://example.com/x.png', 'kind': 'image'}]
        self.features.downloader.fetch.return_value = (b'PNG-image', 'image/png', 'https://example.com/x.png')
        media = await self.tool('media.download', {}, 'media')
        self.assertTrue(media['files'][0]['name'].endswith('.png'))
        self.context['dependency_snapshot']['shared_files'] = media['_shared_files']
        files = await self.features.work_inputs.consultation_attachments(self.context, [])
        self.assertEqual(base64.b64decode(files[0]['data_base64']), b'PNG-image')
        duplicates = await self.features.work_inputs.consultation_attachments(self.context, files+files)
        self.assertEqual(len(duplicates), 1)
        await self.store.recall(replace(self.event, event_type='message.recalled'))
        with self.assertRaises(PermissionError):
            await self.features.work_inputs.consultation_attachments(self.context, [])

    async def test_memory_record_is_scoped_and_does_not_modify_persona(self):
        original = self.app.state.runtime_configuration.snapshot().persona
        saved = await self.tool('memory.record', {'content': '正在学习 Python'}, 'remember')
        self.assertEqual(saved['status'], 'recorded')
        memories = await self.store.own_memories(self.event.actor_id)
        self.assertEqual(memories[0].scope, Scope.LONG_GROUP)
        self.assertEqual(self.app.state.runtime_configuration.snapshot().persona, original)
        denied = await self.tool('memory.record', {'content': 'anything', 'owner_id': 'other'}, 'override')
        self.assertEqual(denied['error'], 'IDENTITY_OVERRIDE_DENIED')

    async def test_memory_search_can_browse_one_person_across_groups_and_tracks_sources(self):
        from osa.domain.entities import Destination
        self.store.permissions.global_group_memory = True
        await self.store.ensure_group('10002')
        source = replace(self.event, id='shared-person', dedupe_key='shared-person', message_id='shared-person',
            destination=Destination('group', '10002'))
        await self.store.publish(source, True)
        await self.store.add_memory(Memory('person-history', '自己更换了手机电池', Scope.LONG_GROUP,
            self.event.actor_id, '10002', source.id))
        await self.store.add_memory(Memory('another-person', '爱好篮球', Scope.LONG_GROUP,
            '20002', '10002', source.id))
        outcome = await self.tool('memory.search', {'subject_id': self.event.actor_id}, 'recall-person')
        self.assertNotIn('error', outcome)
        self.assertEqual([memory['id'] for memory in outcome['memories']], ['person-history'])
        self.assertEqual(outcome['memories'][0]['subject_id'], self.event.actor_id)
        self.assertEqual(outcome['memories'][0]['origin_group_id'], '10002')
        self.assertEqual(outcome['visibility'], 'shared_groups')
        self.assertEqual(outcome['_memory_dependencies'][0]['source_id'], source.id)
        for arguments in ({}, {'subject_id': '昵称'}, {'subject_id': 12345}, {'query': None}):
            with self.subTest(arguments=arguments):
                invalid = await self.tool('memory.search', arguments, 'invalid-' + str(arguments))
                self.assertEqual(invalid['error'], 'INVALID_TOOL_ARGUMENTS')

    async def test_group_knowledge_candidates_share_only_when_global_memory_is_enabled(self):
        from osa.domain.entities import Destination
        await self.store.ensure_group('10002')
        await self.features.learning._save_knowledge(self.event.destination,
            {'content': '编译器可以生成机器码', 'source_ids': [self.event.id], 'confidence': .9, 'shareable': False},
            {self.event.id})
        destination = Destination('group', '10002')
        self.assertEqual(await self.features.learning.recall(destination, '编译器'), [])
        self.store.permissions.global_group_memory = True
        memories = await self.features.learning.recall(destination, '编译器')
        self.assertEqual(len(memories), 1)
        self.assertEqual(memories[0]['trust_status'], 'unverified')
        self.assertTrue(await self.features.allowed({'knowledge_ids': [memories[0]['id']]},
            destination, self.event.actor_id))
        self.assertEqual(await self.features.learning.recall(Destination('private', self.event.actor_id), '编译器'), [])
        await self.store.update_policy('10001', GroupPolicy(collect_messages=False))
        self.assertEqual(await self.features.learning.recall(destination, '编译器'), [])
        self.assertFalse(await self.features.allowed({'knowledge_ids': [memories[0]['id']]},
            destination, self.event.actor_id))

    async def test_unaddressed_group_instruction_cannot_start_business_tools(self):
        observer = {**self.context, 'bot_actions_requested': False,
                    'source_event': {**self.event.to_dict(), 'mentioned': False}}
        for name, arguments in (
            ('work.submit', {'goal': '去干活'}),
            ('moderation.mute', {'target_ids': ['12345'], 'duration_seconds': 60}),
            ('qqbot.tools.call', {'tool': 'draw', 'arguments': {}}),
        ):
            with self.subTest(tool=name):
                outcome = await self.tools.execute(name, arguments,
                    {**observer, 'tool_call_id': 'unaddressed-' + name})
                self.assertEqual(outcome['error'], 'DIRECT_REQUEST_REQUIRED')

    async def test_direct_messages_bypass_decision_model_but_keep_policy_gates(self):
        from osa.application.v3.decision import HybridDecision
        from osa.domain.agents import DecisionContext
        model = SimpleNamespace(generate=AsyncMock(return_value=result({'action': 'THINK', 'reason_code': 'direct_question'})))
        decision = await HybridDecision(model, mode='llm').predict(DecisionContext('e', '10001', 1,
            {'direct': True, 'initiative_allowed': False, 'text': '请帮忙'}))
        self.assertEqual(decision.action, 'THINK')
        model.generate.assert_not_awaited()
        model.generate.reset_mock()
        denied = await HybridDecision(model, mode='llm').predict(DecisionContext('e', '10001', 1,
            {'direct': True, 'hard_denied': True}))
        self.assertEqual(denied.action, 'SKIP')
        model.generate.assert_not_awaited()

    async def test_direct_runtime_calls_only_thinking(self):
        provider = SimpleNamespace(generate=AsyncMock(return_value=
            result({'final_action': 'REPLY', 'reply_brief': {'reply_text': '收到你的请求'}})))
        self.app.state.runtime.model.provider = provider
        await self.app.state.runtime.process(self.event)
        self.assertEqual([call.args[1] for call in provider.generate.call_args_list], ['thinking'])
        self.assertEqual(next(iter(self.store.outboxes.values()))['text'], '收到你的请求')

    async def test_batched_work_requests_keep_real_owners_and_reject_forged_scope(self):
        other=make_event(actor='54321',message_id='other-request',text='也帮我计算一下')
        await self.store.publish(other,True)
        self.context['request_events']=[self.event.to_dict(),other.to_dict()]
        result=await self.tool('work.submit',{'goal':'运行计算程序','request_id':other.id},'owner-scoped')
        self.assertNotIn('error',result)
        task=self.v2.registry.get(result['task_id'])
        self.assertEqual(task['owner_id'],other.actor_id)
        self.assertEqual(task['source_event']['id'],other.id)
        self.assertNotEqual(task['parent_task_id'],self.context['session_id'])
        self.assertTrue(self.v2.registry.valid_agent_dependencies(result['_dependencies'],self.event.actor_id,'10001',[other.id]))
        self.assertFalse(self.v2.registry.valid_agent_dependencies(result['_dependencies'],self.event.actor_id,'10001',[]))
        again=await self.tool('work.submit',{'goal':'运行计算程序','request_id':other.id},'owner-scoped')
        self.assertEqual(again['task_id'],task['id'])
        by_qq_message=await self.tool('work.submit',{'goal':'运行计算程序','request_id':other.message_id},'qq-message-scoped')
        self.assertNotIn('error',by_qq_message)
        self.assertEqual(self.v2.registry.get(by_qq_message['task_id'])['owner_id'],other.actor_id)
        denied=await self.tool('work.submit',{'goal':'编写程序','request_id':'forged-id'},'forged')
        self.assertEqual(denied['error'],'REQUEST_SCOPE_DENIED')
        unspecified=await self.tool('work.submit',{'goal':'编写程序'},'no-owner')
        self.assertEqual(unspecified['error'],'REQUEST_SELECTION_REQUIRED')

    async def test_one_planning_round_delegates_two_users_and_sends_one_group_summary(self):
        from osa.application.runtime import OutboxDispatcher
        from test_core import FakePlatform
        other=make_event(actor='54321',message_id='second-batched-request',text='再做一个计算程序')
        await self.store.publish(other,True)
        provider=SimpleNamespace(generate=AsyncMock(side_effect=[
            result({'tool_call':{'tool_call_id':'work-a','name':'work.submit','arguments':{'goal':'编写扫雷','request_id':self.event.id}}}),
            result({'tool_call':{'tool_call_id':'work-b','name':'work.submit','arguments':{'goal':'编写计算程序','request_id':other.id}}}),
            result({'final_action':'REPLY','reply_brief':{'reply_text':'两位的任务都安排好了。'}})]))
        self.app.state.runtime.model.provider=provider
        await self.app.state.runtime.process_batch([self.event,other],{self.event.id,other.id},'direct')
        self.assertEqual([c.args[1] for c in provider.generate.await_args_list],['thinking','thinking','thinking'])
        tasks=self.v2.registry.tasks(admin=True)
        self.assertEqual({t['owner_id'] for t in tasks},{self.event.actor_id,other.actor_id})
        self.assertEqual(len(self.store.outboxes),1)
        platform=FakePlatform()
        dispatcher=OutboxDispatcher(self.store,platform)
        dispatcher.v2=self.v2;dispatcher.features=self.features
        await dispatcher.dispatch_once()
        self.assertEqual(next(iter(self.store.outboxes.values()))['status'],'sent')
        self.assertEqual(len(platform.sent),1)

    async def test_other_members_published_project_can_be_imported_without_changing_owner(self):
        import hashlib,io,zipfile
        other=make_event(actor='54321',message_id='pi-original',text='计算圆周率')
        await self.store.publish(other,True)
        original=self.v2.registry.create(other.actor_id,'10001','计算圆周率','published-pi',other.to_dict())
        stream=io.BytesIO()
        with zipfile.ZipFile(stream,'w') as archive:archive.writestr('pi.py','print(3.14159)')
        raw=stream.getvalue();digest=hashlib.sha256(raw).hexdigest()
        directory=Path(self.temp.name)/'work'/original['id']/'generation-1'
        directory.mkdir(parents=True);(directory/'project.zip').write_bytes(raw)
        with self.v2.registry.tx() as db:
            task=self.v2.registry._task(db,original['id'])
            task.update(status='COMPLETED',artifact_id='1:'+digest,tests_passed=1)
            self.v2.registry._save(db,task)
        await self.store.record_decision(other,'REPLY','published',{'ttl':600,'work_task_id':original['id'],'deliver_work_file':True},ModelResult('项目已交付','test','reply'))
        await self.store.finish_outbox(other.id,'sent','qq-file-message')
        listed=await self.tool('coding.shared',{},'list-shared')
        self.assertEqual(listed['tasks'][0]['task_id'],original['id'])
        imported=await self.tool('coding.import_artifact',{'task_id':original['id']},'import-shared')
        self.assertNotIn('error',imported)
        self.assertIn('pi.py',imported['files'])
        self.context['dependency_snapshot']['shared_files']=imported['_shared_files']
        created=await self.tool('work.submit',{'goal':'运行共享输入里的 pi.py，保留原始源码'},'run-shared')
        claimed=self.v2.registry.claim('test-worker')
        self.assertEqual(claimed['id'],created['task_id'])
        inputs=await self.features.work_inputs.for_task(claimed,claimed['generation'])
        self.assertEqual(inputs[0]['entries'][0]['text'],'print(3.14159)')
        self.assertEqual(self.v2.registry.get(original['id'])['owner_id'],other.actor_id)
        with self.assertRaises(PermissionError):
            from osa.domain.entities import Destination
            await self.v2.artifact_bytes(original['id'],self.event.actor_id,Destination('group','99999'),shared=True)
        await self.store.recall(replace(other,event_type='message.recalled'))
        self.assertEqual(await self.v2.shared_work(self.event.destination),[])

    async def test_idle_learning_survives_service_restart_and_does_not_use_recalled_text(self):
        from osa.application.chat_stream import SilentLearningService
        model = SimpleNamespace(generate=AsyncMock(return_value=result({'knowledge': [], 'facts': []})))
        learner = SilentLearningService(self.features.repo, self.store, model)
        old = replace(self.event, occurred_at=time.time()-180)
        await learner.observe(old, await self.store.policy(old.destination))
        restored = SilentLearningService(self.features.repo, self.store, model)
        await restored.flush_idle()
        model.generate.assert_awaited_once()
        self.assertFalse((await self.features.repo.find('learning_buffer', status='pending')))
        event = make_event(message_id='recalled-learning')
        await self.store.publish(event, True)
        await restored.observe(event, await self.store.policy(event.destination))
        await self.store.recall(replace(event, event_type='message.recalled'))
        model.generate.reset_mock()
        await restored.flush(event.destination)
        model.generate.assert_not_awaited()

    async def test_sticker_brief_carries_attachment_and_rejects_invented_ids(self):
        await self.features.repo.put(Record('sticker', 'sticker', 'maibot-import', None, 'active',
            {'asset_id': 'asset-1', 'tags': ['开心']}))
        model = SimpleNamespace(generate=AsyncMock(side_effect=[
            result({'tool_call': {'tool_call_id': 'emoji', 'name': 'sticker.search', 'arguments': {'tag': '开心'}}}),
            result({'final_action': 'REPLY', 'reply_brief': {'key_points': [], 'attachment_ids': ['asset-1']}})]))
        outcome = await ThinkingLoop(model, self.tools, self.v2.registry).run([], self.context, 'unified')
        self.assertEqual(outcome.final_action, 'REPLY')
        reply = ReplyWriter(SimpleNamespace(generate=AsyncMock(return_value=ModelResult('', 'test', 'reply'))))
        self.assertEqual((await reply.render(outcome.brief)).attachment_ids, ['asset-1'])
        model.generate.side_effect = [result({'final_action': 'REPLY', 'reply_brief': {'key_points': ['hello'], 'attachment_ids': ['invented']}})]
        self.assertEqual((await ThinkingLoop(model, self.tools).run([], self.context)).final_action, 'FAILED')

    async def test_sticker_search_never_substitutes_unrelated_image_and_counts_only_delivery(self):
        await self.features.repo.put(Record('sticker-match','sticker','maibot-import',None,'active',
            {'asset_id':'asset-match','tags':['开心'],'uses':0,'last_used':0}))
        missing=await self.tool('sticker.search',{'tag':'难过'},'sticker-missing')
        self.assertFalse(missing['found'])
        self.assertEqual((await self.features.repo.get('sticker-match')).payload['uses'],0)
        found=await self.tool('sticker.search',{'tag':'开心'},'sticker-found')
        self.assertEqual(found['asset_id'],'asset-match')
        self.assertEqual((await self.features.repo.get('sticker-match')).payload['uses'],0)
        await self.features.media.mark_stickers_sent(['asset-match'],'10001')
        self.assertEqual((await self.features.repo.get('sticker-match')).payload['uses'],1)

    async def test_memory_library_search_and_owner_acl(self):
        await self.store.add_memory(Memory('m1', '喜欢 Python', Scope.LONG_GROUP, self.event.actor_id, '10001', self.event.id))
        later = replace(make_event(message_id='memory-later'), occurred_at=self.event.occurred_at + 60)
        await self.store.publish(later, True)
        await self.store.add_memory(Memory('m2', '正在学习 Rust', Scope.LONG_GROUP, self.event.actor_id, '10001', later.id))
        await self.features.repo.put(Record('k1', 'knowledge_candidate', None, None, 'shared',
            {'content': '共享知识：Python', 'origin_group_id': '10001', 'source_ids': [self.event.id]}))
        await self.features.repo.put(Record('k2', 'knowledge_candidate', None, '10001', 'candidate',
            {'content': '群聊知识：Rust', 'origin_group_id': '10001', 'source_ids': [self.event.id]}))
        await self.features.repo.put(Record('k3', 'knowledge_candidate', None, '10001', 'candidate',
            {'content': '群聊知识：较早', 'origin_group_id': '10001', 'source_ids': [self.event.id]},
            created_at=self.event.occurred_at - 60))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(self.app), base_url='http://test') as client:
            self.assertEqual((await client.get('/api/v1/operations/memories')).status_code, 401)
            headers = {'Authorization': 'Bearer admin-test-token-12345'}
            data = (await client.get('/api/v1/operations/memories?q=Python', headers=headers)).json()['data']
            self.assertEqual(data['total'], 2)
            shared = (await client.get('/api/v1/operations/memories?kind=shared', headers=headers)).json()['data']
            self.assertEqual(shared['items'][0]['status'], 'unverified')
            self.assertEqual(shared['total'], 1)
            knowledge = (await client.get('/api/v1/operations/memories?kind=knowledge', headers=headers)).json()['data']
            self.assertEqual([item['id'] for item in knowledge['items']], ['k2', 'k3'])
            first = (await client.get('/api/v1/operations/memories?kind=personal&page_size=1&page=1', headers=headers)).json()['data']
            second = (await client.get('/api/v1/operations/memories?kind=personal&page_size=1&page=2', headers=headers)).json()['data']
            self.assertEqual([first['items'][0]['id'], second['items'][0]['id']], ['m2', 'm1'])
            self.assertEqual(first['pages'], 2)
            self.assertEqual((await client.delete('/api/v1/operations/memories/k1', headers=headers)).status_code, 200)
            self.assertEqual((await self.features.repo.get('k1')).status, 'deleted')

    async def test_memory_writes_screen_keywords_and_keep_existing_id(self):
        first = await self.store.add_memory(Memory('first', '我喜欢 Python。', Scope.LONG_GROUP,
            self.event.actor_id, '10001', self.event.id))
        duplicate = await self.store.add_memory(Memory('duplicate', '我喜欢Python', Scope.LONG_GROUP,
            self.event.actor_id, '10001', self.event.id))
        different = await self.store.add_memory(Memory('different', '我不喜欢 Python。', Scope.LONG_GROUP,
            self.event.actor_id, '10001', self.event.id))
        self.assertEqual(first.id, duplicate.id)
        self.assertEqual(different.id, 'different')
        self.assertEqual(len(await self.store.own_memories(self.event.actor_id)), 2)
        await self.features.learning._save_knowledge(self.event.destination,
            {'content': 'Python 可用于数据分析。', 'source_ids': [self.event.id], 'confidence': .9}, {self.event.id})
        await self.features.learning._save_knowledge(self.event.destination,
            {'content': 'Python可用于数据分析', 'source_ids': [self.event.id], 'confidence': .9}, {self.event.id})
        self.assertEqual(len(await self.features.repo.find('knowledge_candidate')), 1)

    async def test_new_memory_cleans_older_duplicate_rows(self):
        self.store.memories.update({memory_id:Memory(memory_id, content, Scope.LONG_GROUP,
            self.event.actor_id, '10001', self.event.id)
            for memory_id, content in [('old-a', '喜欢 Python。'), ('old-b', '喜欢Python')]})
        saved = await self.store.add_memory(Memory('new', '喜欢 Python', Scope.LONG_GROUP,
            self.event.actor_id, '10001', self.event.id))
        self.assertEqual(saved.id, 'old-a')
        self.assertEqual((await self.store.memories_by_ids(['old-b']))[0].status, 'deleted')
        self.assertEqual(len(await self.store.own_memories(self.event.actor_id)), 1)

    async def test_shared_knowledge_is_queryable_in_another_group_then_revoked(self):
        await self.features.learning._save_knowledge(self.event.destination,
            {'content': '编译器可以生成机器码', 'source_ids': [self.event.id], 'confidence': .99, 'shareable': True}, {self.event.id})
        from osa.domain.entities import Destination
        evidence = await self.features.learning.recall(Destination('group', '10002'), '编译器如何工作')
        self.assertEqual(len(evidence), 1)
        self.assertNotIn('origin_group_id', evidence[0])
        snapshot = {'knowledge_ids': [evidence[0]['id']]}
        self.assertTrue(await self.features.allowed(snapshot, self.event.destination, self.event.actor_id))
        await self.store.recall(replace(self.event, event_type='message.recalled'))
        self.assertEqual(await self.features.learning.recall(Destination('group', '10002'), '编译器'), [])
        self.assertFalse(await self.features.allowed(snapshot, self.event.destination, self.event.actor_id))

    async def test_work_agent_reads_writes_runs_and_uses_returned_results(self):
        model = SimpleNamespace(generate=AsyncMock(side_effect=[
            result({'tool_call': {'name': 'file.read', 'arguments': {'path': 'inputs/request.txt'}}}),
            result({'tool_call': {'name': 'file.write', 'arguments': {'path': 'main.py', 'content': 'print(1+1)'}}}),
            result({'tool_call': {'name': 'python.execute', 'arguments': {'code': 'print(1+1)'}}}),
            result({'final_action': 'DONE'})]))
        executor = SimpleNamespace(run=AsyncMock(return_value={'status': 'succeeded', 'exit_code': 0, 'stdout': '2'}))
        read = AsyncMock(return_value={'text': '计算 1+1'})
        saved = AsyncMock()
        files, _ = await WorkspaceAgent(model, executor, save_files=saved).run({'task_id': 'task', 'generation': 1, 'requirement': 'write'}, read)
        self.assertEqual(files['main.py'], 'print(1+1)')
        saved.assert_awaited_once_with({'main.py': 'print(1+1)'})
        self.assertEqual(executor.run.call_args.args[0], 'python')
        self.assertEqual(executor.run.call_args.args[1]['files'], {'main.py': 'print(1+1)'})
        self.assertIn('"stdout": "2"', model.generate.call_args.args[0][-1]['content'])

    async def test_workspace_gateway_preserves_program_outputs_for_file_delivery(self):
        from pydantic import SecretStr
        from osa.infrastructure.runtime_config import ProviderConfig, ModelConfig, TaskConfig
        self.app.state.settings.worker_token = SecretStr('test-work-token-1234567890')
        config = self.app.state.runtime_configuration.snapshot()
        config.providers = [ProviderConfig(id='test', name='Test', base_url='https://example.test')]
        config.models = [ModelConfig(model_identifier='work', name='Work', api_provider='test')]
        config.task_configs['work'] = TaskConfig(model_list=['work'])
        self.app.state.runtime_configuration.replace(config)
        await self.tool('coding.submit', {'goal': '生成结果文件'}, 'program-output')
        task = self.v2.registry.claim('worker')
        self.app.state.work_model = SimpleNamespace(generate=AsyncMock(side_effect=[
            result({'tool_call': {'name': 'file.write', 'arguments': {'path': 'main.py', 'content': 'print(2)'}}}),
            result({'tool_call': {'name': 'python.execute', 'arguments': {'code': "from pathlib import Path;Path('result.txt').write_text('2')"}}}),
            result({'final_action': 'DONE'})]))
        self.features.sandbox.run = AsyncMock(return_value={'status': 'succeeded', 'stdout': '', 'exit_code': 0,
            'artifacts': [{'name': 'result.txt', 'content': base64.b64encode(b'2').decode()}]})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(self.app), base_url='http://test') as client:
            response = await client.post('/internal/work-model', json={'task_id': task['id'], 'generation': task['generation']},
                headers={'Authorization': 'Bearer test-work-token-1234567890'})
        self.assertEqual(response.status_code, 200, response.text)
        directory = Path(self.app.state.settings.workspace_dir)/task['id']/'generation-1'
        self.assertEqual((directory/'artifacts/result.txt').read_bytes(), b'2')
        self.assertEqual(self.v2.registry.get(task['id'])['files'], {'main.py': 'print(2)'})

    async def test_workspace_does_not_read_host_paths_or_execute_host_shell(self):
        agent = WorkspaceAgent(None)
        with self.assertRaises(PermissionError):
            await agent.execute('file.read', {'path': 'C:/secret'}, {}, {}, None, None, 0)
        with self.assertRaises(PermissionError):
            await agent.execute('file.write', {'path': '../escape.py', 'content': 'x'}, {}, {}, None, None, 0)
        with self.assertRaises(PermissionError):
            await agent.execute('shell.execute', {'command': 'anything'}, {}, {}, None, None, 0)
        raw = base64.b64encode(b'raw input').decode()
        self.assertEqual(decode_input_files({'data.txt': raw})['data.txt'], b'raw input')
        with self.assertRaises(PermissionError):
            decode_input_files({'../escape.txt': raw})

    async def test_rejected_parser_file_finishes_without_automatic_retry(self):
        self.features.parser.parse = AsyncMock(side_effect=ParserRejected('unsupported'))
        await self.resources.sync('10001', 'files')
        self.features.platform.call = AsyncMock(return_value={'url': 'https://files.example.test/f1'})
        task = await self.features.tasks.submit('index', {'file_id': 'f1'}, actor=None, group='10001')
        await self.features.tasks.execute(task)
        row = await self.features.repo.get(task.id)
        self.assertEqual(row.status, 'succeeded')
        self.assertEqual(row.payload['result']['status'], 'unsupported')

    async def test_completed_delegation_resumes_reply_and_delivers_verified_file(self):
        from osa.application.v2.worker import WorkAgent
        from test_v2 import FILES, report
        from test_core import FakePlatform
        await self.tool('coding.submit', {'goal': '生成加法程序并发送项目文件'}, 'deliver')
        claimed = self.v2.registry.claim('test-worker')
        executor = SimpleNamespace(run=AsyncMock(return_value=report()))
        await WorkAgent(self.v2.registry, AsyncMock(return_value=FILES), executor, self.app.state.settings.workspace_dir).run(claimed)
        self.app.state.runtime.model.provider = SimpleNamespace(generate=AsyncMock(return_value=result({
            'final_action':'REPLY', 'reply_brief':{
                'reply_text':'程序已完成，源码见附件。', 'artifact_names':['project.zip']}})))
        self.assertTrue(await self.app.state.v3.resume_once())
        outbox = next(iter(self.store.outboxes.values()))
        self.assertTrue(self.store.decisions[outbox['decision_id']]['snapshot']['deliver_work_file'])
        platform = FakePlatform()
        platform.send_file = AsyncMock()
        self.app.state.dispatcher.platform = platform
        await self.app.state.dispatcher.dispatch_once()
        platform.send_file.assert_awaited_once()
        self.assertTrue(platform.send_file.call_args.args[2].startswith(b'PK'))
        self.assertEqual(outbox['status'], 'sent')
