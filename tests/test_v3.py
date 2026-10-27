"""V3 behavioral contracts: no live QQ, model, browser or OpenCode claims."""
import asyncio
import hashlib
import json
import tempfile
import time
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import AsyncMock
from unittest.mock import patch

import httpx

from osa.application.resources import ResourceService
from osa.application.runtime import AgentRuntime, inbox_loop
from osa.application.v2.service import V2Service
from osa.application.v3.decision import HybridDecision
from osa.application.v3.service import V3Service
from osa.application.v3.thinking import ThinkingLoop
from osa.application.v3.tools import AgentTools
from osa.domain.agents import CodingTaskRequest, DecisionContext, DecisionResult, ProviderStateUnknown, UnsupportedCapability
from osa.domain.work import WorkModelUnavailable
from osa.domain.entities import GroupPolicy, ModelResult, Memory, Scope
from osa.infrastructure.memory_store import InMemoryStore
from osa.infrastructure.v3.observer import SnapshotObserver
from osa.infrastructure.v3.opencode import OpenCodeAdapter
from osa.infrastructure.v3.registry import Registry
from osa.main import create_app
from test_core import demo_settings, make_event, FakeResources, FakePlatform
from test_v2 import V2AsyncCase, FILES, report


def model_result(value):
    return ModelResult(json.dumps(value, ensure_ascii=False), "test", "test-model")


class V3Contracts(V2AsyncCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.registry = Registry(str(Path(self.directory.name)/"registry.db"))
        self.settings = demo_settings().model_copy(update={"v2_enabled": True, "v3_enabled": True, "work_enabled": True,
            "v2_database_url": self.registry.location, "workspace_dir": self.directory.name})
        self.store = InMemoryStore()
        await self.store.initialize(["10001", "10002"])
        self.event = make_event()
        await self.store.publish(self.event, True)
        self.context = {"actor_id": self.event.actor_id, "group_id": "10001", "source_id": self.event.id,
            "source_event": {**self.event.to_dict(), "text": "", "segments": []}, "evidence_ids": []}
        self.v2 = V2Service(self.registry, self.settings)

    async def asyncTearDown(self):
        self.directory.cleanup()

    async def test_think_then_silent_does_not_call_reply_or_queue_send(self):
        model = AsyncMock()
        model.generate.return_value = model_result({"final_action": "SILENT"})
        runtime = AgentRuntime(self.store, model, ResourceService(self.store, FakeResources()), v2=self.v2)
        runtime.v3 = V3Service(self.settings, model, self.v2, self.store)
        await runtime.process(self.event)
        self.assertEqual([c.args[1] for c in model.generate.await_args_list], ["thinking"])
        self.assertEqual(self.store.outboxes, {})
        self.assertEqual(self.store.decisions[self.event.id]["reason"], "thinking_silent")

    async def test_low_confidence_fallback_ignores_llm_probability_and_direct_is_forced(self):
        model, classifier = AsyncMock(), AsyncMock()
        classifier.predict.return_value = DecisionResult("DEFER", "classifier", "rules-test", "unsure", .3)
        model.generate.return_value = model_result({"action": "THINK", "confidence": 1})
        engine = HybridDecision(model, classifier)
        result = await engine.predict(DecisionContext("e", "g", 1, {"initiative_allowed": True}))
        self.assertEqual(result.action, "THINK")
        self.assertFalse(result.calibrated)
        self.assertIsNone(result.confidence)
        model.generate.reset_mock()
        forced = await engine.predict(DecisionContext("e", "g", 1, {"direct": True}))
        self.assertEqual(forced.action, "THINK")
        model.generate.assert_not_awaited()
        hard = await engine.predict(DecisionContext("e", "g", 1, {"direct": True, "hard_denied": True}))
        self.assertEqual(hard.action, "SKIP")

    async def test_work_request_is_decided_in_main_tool_loop_without_legacy_classifier(self):
        event = make_event(message_id="work-main", text="帮我编写一个 Python 程序并发给我文件")
        await self.store.publish(event, True)
        await self.store.update_policy("10001", GroupPolicy(sandbox=True))
        self.v2.maybe_work = AsyncMock(side_effect=AssertionError("legacy work routing must not run"))
        model = AsyncMock()
        model.generate.side_effect = [
            model_result({"tool_call": {"tool_call_id": "create", "name": "coding.submit",
                                       "arguments": {"goal": event.text}}}),
            model_result({"final_action": "REPLY", "reply_brief": {"reply_text": "任务已记录，等待审批"}}),
        ]
        runtime = AgentRuntime(self.store, model, ResourceService(self.store, FakeResources()), v2=self.v2)
        runtime.v3 = V3Service(self.settings, model, self.v2, self.store)
        await runtime.process(event)
        self.v2.maybe_work.assert_not_awaited()
        self.assertEqual([call.args[1] for call in model.generate.await_args_list], ["thinking", "thinking"])
        task = self.registry.tasks(admin=True)[0]
        self.assertEqual(task["requirement"], event.text)
        self.assertTrue(task["parent_task_id"])
        self.assertEqual(len(self.store.outboxes), 1)

    async def test_python_package_request_uses_model_selected_task_without_forced_reconsideration(self):
        event = make_event(message_id="python-package", text="帮我写个包装dtpweb的py包，让我可以像调用mcu的绘图api一样调用这些api")
        await self.store.publish(event, True)
        await self.store.update_policy("10001", GroupPolicy(sandbox=True))
        model = AsyncMock()
        model.generate.side_effect = [
            model_result({"tool_call": {"tool_call_id": "package-create", "name": "work.submit",
                                        "arguments": {"goal": event.text}}}),
            model_result({"final_action": "REPLY", "reply_brief": {"reply_text": "任务已进入工作队列。"}}),
        ]
        runtime = AgentRuntime(self.store, model, ResourceService(self.store, FakeResources()), v2=self.v2)
        runtime.v3 = V3Service(self.settings, model, self.v2, self.store)
        await runtime.process(event)
        self.assertEqual([call.args[1] for call in model.generate.await_args_list], ["thinking"] * 2)
        self.assertEqual(self.registry.tasks(admin=True)[0]["requirement"], event.text)
        self.assertEqual(next(iter(self.store.outboxes.values()))["text"], "任务已进入工作队列。")

    async def test_unavailable_work_does_not_force_task_creation(self):
        event = make_event(message_id="work-unavailable", text="帮我写个 Python 程序")
        await self.store.publish(event, True)
        model = AsyncMock()
        model.generate.return_value = model_result({"final_action": "REPLY", "reply_brief": {
            "reply_text": "本群尚未授权代码任务。"}})
        runtime = AgentRuntime(self.store, model, ResourceService(self.store, FakeResources()), v2=self.v2)
        runtime.v3 = V3Service(self.settings, model, self.v2, self.store)
        await runtime.process(event)
        self.assertEqual(model.generate.await_count, 1)
        self.assertEqual(self.registry.tasks(admin=True), [])

    async def test_explicit_work_command_also_uses_main_thinking(self):
        event = make_event(text="/工作 写一个程序", mention=False)
        self.assertIsNone(await self.v2.route(event, GroupPolicy(sandbox=True), allow_work_creation=False))
        self.assertEqual(self.registry.tasks(admin=True), [])
        runtime = AgentRuntime(self.store, AsyncMock(), ResourceService(self.store, FakeResources()), v2=self.v2)
        self.assertEqual(runtime.social.decide(event, GroupPolicy(), False).reason, "explicit_task_request")
        self.assertEqual(runtime.social.decide(event, GroupPolicy(enabled=False), False).action, "IGNORE")

    async def test_coding_submit_accepts_paragraph_criteria_and_replays_without_duplicate_task(self):
        await self.store.update_policy('10001', GroupPolicy(sandbox=True))
        self.registry.begin_thinking('criteria-paragraph', self.context)
        tools = AgentTools(self.v2, self.store)
        context = {**self.context, 'session_id': 'criteria-paragraph', 'tool_call_id': 'coding-1', 'deadline': time.time()+60}
        args = {'goal': '编写贪吃蛇', 'acceptance_criteria': '单文件；方向键控制；撞墙后可重新开始'}
        first = await tools.execute('coding.submit', args, context)
        self.assertNotIn('error', first)
        self.assertEqual((await tools.execute('coding.submit', args, context))['task_id'], first['task_id'])
        self.assertEqual(len(self.registry.tasks(admin=True)), 1)
        task = self.registry.get(first['task_id'])
        self.assertEqual(task['status'], 'QUEUED')
        self.assertIn(args['acceptance_criteria'], task['requirement'])
        self.assertEqual(task['parent_task_id'], 'criteria-paragraph')

    async def test_invalid_coding_arguments_are_repairable_without_false_permission_error(self):
        await self.store.update_policy('10001', GroupPolicy(sandbox=True))
        self.registry.begin_thinking('criteria-invalid', self.context)
        tools = AgentTools(self.v2, self.store)
        context = {**self.context, 'session_id': 'criteria-invalid', 'tool_call_id': 'coding-1', 'deadline': time.time()+60}
        result = await tools.execute('coding.submit', {'goal': '贪吃蛇', 'acceptance_criteria': {'wrong': 'shape'}}, context)
        self.assertEqual(result['error'], 'INVALID_TOOL_ARGUMENTS')
        self.assertTrue(result['retryable'])
        self.assertIn('acceptance_criteria', result['detail'])
        self.assertEqual(self.registry.tasks(admin=True), [])
        context['tool_call_id'] = 'coding-2'
        fixed = await tools.execute('coding.submit', {'goal': '贪吃蛇', 'acceptance_criteria': ['单文件']}, context)
        self.assertIn('task_id', fixed)

    async def test_group_thinking_receives_recent_conversation(self):
        before = make_event(message_id="before", text="这道题还有另一个条件", mention=False)
        await self.store.publish(before, True)
        current = make_event(message_id="after", text="等等", mention=True)
        await self.store.publish(current, True)
        model = AsyncMock()
        model.generate.return_value = model_result({"final_action": "SILENT"})
        runtime = AgentRuntime(self.store, model, ResourceService(self.store, FakeResources()), v2=self.v2)
        runtime.v3 = V3Service(self.settings, model, self.v2, self.store)
        await runtime.process(current)
        self.assertIn("这道题还有另一个条件", json.dumps(model.generate.await_args.args[0], ensure_ascii=False))
        self.assertEqual([call.args[1] for call in model.generate.await_args_list], ["thinking"])

    async def test_dynamic_memory_search_tracks_new_evidence_and_send_cancellation(self):
        from osa.application.runtime import OutboxDispatcher
        source = make_event(message_id="memory-source", text="我在维护一个小型 Python 项目")
        await self.store.publish(source, True)
        memory = Memory("later-memory", source.text, Scope.LONG_GROUP, source.actor_id, "10001", source.id)
        await self.store.add_memory(memory)
        other = make_event(message_id="private-source", group="10002", text="另一群私密记录")
        await self.store.publish(other, True)
        await self.store.add_memory(Memory("other-memory", other.text, Scope.LONG_GROUP, other.actor_id, "10002", other.id))
        original_search = self.store.visible_memories

        async def search(actor, destination, query, limit):
            return await original_search(actor, destination, query, limit) if query == "Python项目" else []

        self.store.visible_memories = search
        model = AsyncMock()
        model.generate.side_effect = [
            model_result({"tool_call": {"tool_call_id": "lookup", "name": "memory.search", "arguments": {"query": "Python项目"}}}),
            model_result({"final_action": "REPLY", "reply_brief": {"reply_text": "你提过一个小型 Python 项目", "evidence_ids": [memory.id]}}),
        ]
        runtime = AgentRuntime(self.store, model, ResourceService(self.store, FakeResources()), v2=self.v2)
        service = runtime.v3 = V3Service(self.settings, model, self.v2, self.store)
        await runtime.process(self.event)
        tool_result = json.loads(model.generate.await_args_list[1].args[0][-1]["content"])["tool_result"]
        self.assertEqual([row["id"] for row in tool_result["memories"]], [memory.id])
        snapshot = self.store.decisions[self.event.id]["snapshot"]
        self.assertEqual(snapshot["memory_versions"], {memory.id: 1})
        self.assertIn(source.id, snapshot["source_ids"])
        parent = self.registry.thinking(snapshot["thinking"]["session_id"])["payload"]["context"]
        self.assertTrue(await service.dependencies_valid(self.event, parent))
        await self.store.delete_memory(memory.id)
        self.assertFalse(await service.dependencies_valid(self.event, parent))
        platform = FakePlatform()
        dispatcher = OutboxDispatcher(self.store, platform)
        dispatcher.v2 = self.v2
        await dispatcher.dispatch_once()
        self.assertEqual(platform.sent, [])

    async def test_dynamic_memory_source_and_version_rechecked_before_resume(self):
        source = make_event(message_id="source-for-version")
        await self.store.publish(source, True)
        memory = Memory("versioned", "原话", Scope.LONG_GROUP, source.actor_id, "10001", source.id)
        await self.store.add_memory(memory)
        context = {**self.context, "dependency_snapshot": {
            "memory_ids": [memory.id], "memory_versions": {memory.id: 1}, "source_ids": [source.id]}}
        service = V3Service(self.settings, AsyncMock(), self.v2, self.store)
        self.assertTrue(await service.dependencies_valid(self.event, context))
        await self.store.correct_memory(memory.id, source.actor_id, "更正", source.id)
        self.assertFalse(await service.dependencies_valid(self.event, context))
        context["dependency_snapshot"]["memory_versions"][memory.id] = 2
        self.assertTrue(await service.dependencies_valid(self.event, context))
        await self.store.recall(replace(source, event_type="message.recalled"))
        self.assertFalse(await service.dependencies_valid(self.event, context))

    async def test_three_tool_rounds_feed_results_then_thinking_writes_reply(self):
        model, tools = AsyncMock(), AsyncMock()
        tools.execute.side_effect = [{"n": 1}, {"n": 2}, {"n": 3}]
        model.generate.side_effect = [*[model_result({"tool_call": {"tool_call_id": str(n), "name": "memory.search", "arguments": {"query": "x"}}}) for n in range(3)],
            model_result({"final_action": "REPLY", "reply_brief": {"intent": "answer", "reply_text": "结论", "evidence_ids": ["2"]}})]
        outcome = await ThinkingLoop(model, tools, self.registry).run([{"role": "user", "content": "hello"}],
            {**self.context, 'reply_mode': 'integrated'}, "sid-three")
        self.assertEqual(outcome.final_action, "REPLY")
        self.assertEqual(tools.execute.await_count, 3)
        self.assertIn('"n": 3', model.generate.await_args_list[-1].args[0][-1]["content"])
        traces = self.registry.thinking("sid-three")["payload"]["traces"]
        self.assertEqual([t["call_id"] for t in traces], ["0", "1", "2"])
        self.assertEqual(outcome.brief.reply_text, "结论")
        self.assertEqual([call.args[1] for call in model.generate.await_args_list], ["thinking"] * 4)

    async def test_unbounded_loop_and_fake_evidence_fail_closed(self):
        model, tools = AsyncMock(), AsyncMock()
        model.generate.return_value = model_result({"final_action": "REPLY", "reply_brief": {"key_points": ["claim"], "evidence_ids": ["invented"]}})
        result = await ThinkingLoop(model, tools, self.registry, max_steps=1).run([], self.context, "bad-evidence")
        self.assertEqual(result.final_action, "FAILED")
        tools.execute.assert_not_awaited()
        model.generate.return_value = model_result({"tool_call": {"tool_call_id": "one", "name": "x", "arguments": {}}})
        tools.execute.return_value = {"value": "ok"}
        result = await ThinkingLoop(model, tools, max_steps=2).run([], self.context)
        self.assertEqual(result.final_action, "FAILED")

    async def test_tools_enforce_scope_and_record_idempotent_receipts(self):
        await self.store.update_policy("10001", GroupPolicy(sandbox=True))
        self.registry.begin_thinking("sid", self.context)
        tools = AgentTools(self.v2, self.store)
        ctx = {**self.context, "session_id": "sid", "tool_call_id": "create", "deadline": time.time()+30}
        first = await tools.execute("coding.submit", {"goal": "addition", "acceptance_criteria": ["test 1+1"]}, ctx)
        again = await tools.execute("coding.submit", {"goal": "addition", "acceptance_criteria": ["test 1+1"]}, ctx)
        self.assertEqual(first, again)
        self.assertEqual(len(self.registry.tasks(admin=True)), 1)
        self.assertEqual(self.registry.get(first["task_id"])["parent_task_id"], "sid")
        denied = await tools.execute("coding.status", {"task_id": first["task_id"]}, {**ctx, "group_id": "10002", "tool_call_id": "read-other-group"})
        self.assertIn("error", denied)
        denied = await tools.execute("shell.execute", {"command": "x"}, ctx)
        self.assertEqual(denied["error"], "TOOL_DENIED")
        denied = await tools.execute("coding.submit", {"goal": "x", "owner_id": "admin"}, ctx)
        self.assertEqual(denied["error"], "IDENTITY_OVERRIDE_DENIED")

    async def test_observer_is_hash_checked_readonly_and_revision_bound(self):
        task = self.registry.create("u", None, "addition", "one")
        self.registry.command(task["id"], "admin", None, "APPROVE", {}, task["version"], "approve", admin=True)
        task = self.registry.claim("w")
        manifest = hashlib.sha256(json.dumps(FILES, sort_keys=True).encode()).hexdigest()
        self.registry.checkpoint(task["id"], task["generation"], "source", {"files": FILES, "manifest_hash": manifest, "git_commit": "a"*40}, {})
        model = AsyncMock()
        model.generate.return_value = model_result({"findings": [{"severity": "low", "file": "main.py", "line": 1, "issue": "补充边界测试"}]})
        observer = SnapshotObserver(self.registry, model)
        before = self.registry.get(task["id"])
        report = await observer.inspect(task["id"], "REVIEW", "a"*40)
        self.assertEqual(report["observed_revision"], "a"*40)
        self.assertTrue(report["readonly"])
        self.assertEqual(self.registry.get(task["id"]), before)
        self.assertEqual(len(self.registry.reviews(task["id"])), 1)
        with self.assertRaises(ValueError):
            await observer.inspect(task["id"], "REVIEW", "b"*40)
        self.registry.checkpoint(task["id"], task["generation"], "bad", {"files": {"main.py": "changed"}}, {})
        with self.assertRaises(ValueError):
            await observer.inspect(task["id"], "REVIEW")

    async def test_completion_event_is_atomic_and_parent_wakes_once_after_restart(self):
        self.registry.begin_thinking("sid-parent", self.context)
        task = self.registry.create(self.event.actor_id, "10001", "x", "one")
        self.registry.attach_parent(task["id"], "sid-parent")
        self.registry.command(task["id"], "admin", None, "APPROVE", {}, task["version"], "approve", admin=True)
        task = self.registry.claim("worker")
        self.registry.checkpoint(task["id"], task["generation"], "tests", {"tests_passed": 1, "artifact_id": "1:"+"a"*64}, {})
        self.registry.finish(task["id"], task["generation"], "COMPLETED")
        reloaded = Registry(self.registry.location)
        events = [e for e in reloaded.domain_events(task["id"]) if e["event_type"] == "coding.completed"]
        self.assertEqual(len(events), 1)
        e = events[0]
        reloaded.publish(task["id"], "sid-parent", e["event_type"], e["idempotency_key"], e["payload"])
        job = reloaded.claim_wakeup()
        self.assertEqual(job["parent_task_id"], "sid-parent")
        reloaded.finish_wakeup(job["id"], job["generation"])
        self.assertIsNone(reloaded.claim_wakeup())

    async def test_research_finishes_as_unverified_and_wakes_scoped_parent(self):
        self.registry.begin_thinking("parent", self.context)
        rid = self.registry.record("night_research", self.event.actor_id, "10001", {"parent_task_id": "parent", "source_ids": [self.event.id]}, "r", "QUEUED")
        self.registry.update_record(rid, "NEEDS_REVIEW", {"verified_claims": 0})
        self.registry.update_record(rid, "NEEDS_REVIEW", {"verified_claims": 0})
        self.assertEqual(len(self.registry.domain_events(rid)), 1)
        self.assertIsNotNone(self.registry.claim_wakeup())
        self.registry.erase(source=self.event.id)
        with self.assertRaises(KeyError):
            self.registry.thinking("parent")

    async def test_api_v3_owner_controls_and_readonly_status(self):
        app = create_app(self.settings.model_copy(update={"features_enabled": False}))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            self.assertEqual((await client.get("/api/v1/agents/config")).status_code, 401)
            client.headers["Authorization"] = "Bearer admin-test-token-12345"
            config = (await client.get("/api/v1/agents/config")).json()["data"]
            self.assertFalse(config["tts"])
            self.assertEqual(config["research"]["provider"], "deepseek_web")
            created = await client.post("/api/v1/coding/tasks", json={"requirement": "x", "idempotency_key": "create-code-1"})
            self.assertEqual(created.status_code, 200)
            row = created.json()["data"]
            self.assertEqual((await client.get('/api/v1/coding/tasks/'+row['id']+'/progress')).status_code, 200)
            review = await client.post('/api/v1/coding/tasks/'+row['id']+'/inspect', json={"mode": "STATUS"})
            self.assertTrue(review.json()["data"]["readonly"])
            self.assertEqual((await client.get('/api/v1/coding/tasks/'+row['id'])).status_code, 200)
            self.assertEqual((await client.get('/console-v3.js')).status_code, 200)

    async def test_opencode_model_config_requires_worker_auth_and_uses_web_work_route(self):
        from osa.infrastructure.runtime_config import ModelConfig, ProviderConfig, TaskConfig
        from pydantic import SecretStr
        app = create_app(self.settings.model_copy(update={'features_enabled': False, 'worker_token': SecretStr('worker-token-test-1234567890')}))
        config = app.state.runtime_configuration.snapshot()
        config.providers = [ProviderConfig(id='coding', name='Coding provider', base_url='https://example.test/v1', api_key='dedicated-code-key')]
        config.models = [ModelConfig(model_identifier='actual-coding-model', name='My work model', api_provider='coding')]
        config.task_configs = {'work': TaskConfig(model_list=['actual-coding-model'])}
        app.state.runtime_configuration.replace(config)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            self.assertEqual((await client.get('/internal/opencode/model')).status_code, 401)
            client.headers['Authorization'] = 'Bearer ' + app.state.settings.worker_token.get_secret_value()
            result = await client.get('/internal/opencode/model')
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json()['data']['model'], 'actual-coding-model')
            self.assertEqual(result.json()['data']['api_key'], 'dedicated-code-key')

    async def test_finished_thinking_is_idempotent_and_no_hidden_reasoning_is_stored(self):
        model, tools = AsyncMock(), AsyncMock()
        model.generate.return_value = model_result({"final_action": "SILENT"})
        loop = ThinkingLoop(model, tools, self.registry)
        await loop.run([], self.context, "one")
        await loop.run([], self.context, "one")
        self.assertEqual(model.generate.await_count, 1)
        data = self.registry.thinking("one")["payload"]
        self.assertNotIn("messages", data)
        self.assertNotIn("reasoning", data)

    async def test_no_collection_redacts_persisted_brief_and_recall_erases_session(self):
        model = AsyncMock()
        model.generate.return_value = model_result({'final_action': 'REPLY', 'reply_brief': {'key_points': ['private phrase'], 'intent': 'answer'}})
        await ThinkingLoop(model, AsyncMock(), self.registry).run([], {**self.context, 'collect_messages': False}, 'no-collect')
        stored = self.registry.thinking('no-collect')['payload']
        self.assertIsNone(stored['result']['brief'])
        service = V3Service(self.settings, model, self.v2, self.store)
        await service.recalled(replace(self.event, event_type='message.recalled'))
        with self.assertRaises(KeyError):
            self.registry.thinking('no-collect')

    async def test_video_transcribes_actual_track_and_does_not_infer_missing_sound(self):
        from osa.application.features.media import MediaService
        from osa.infrastructure.features.repository import MemoryFeatureRepository
        import io, wave
        output = io.BytesIO()
        with wave.open(output, 'wb') as f:
            f.setnchannels(1);f.setsampwidth(2);f.setframerate(16000);f.writeframes(b'\x00\x00'*800)
        audio = output.getvalue()
        objects, provider, video = AsyncMock(), AsyncMock(), AsyncMock()
        objects.get.return_value = b'video fixture'
        provider.analyze_image.return_value = '一张画面'
        provider.transcribe.return_value = '真实音轨的转写'
        video.frames.return_value = [b'frame']
        video.audio.return_value = audio
        media = MediaService(MemoryFeatureRepository(), objects, AsyncMock(), provider, video)
        asset = await media.save(b'video fixture', 'video/mp4', 'video', '10001', 'u')
        result = await media.analyze(asset.id, '内容是什么')
        self.assertIn('真实音轨的转写', result['text'])
        self.assertEqual(provider.transcribe.await_args.args[0], audio)
        video.audio.return_value = None
        provider.transcribe.reset_mock()
        result = await media.analyze(asset.id, '内容是什么')
        self.assertIn('音轨不可用', result['text'])
        provider.transcribe.assert_not_awaited()
        video.audio.return_value = audio
        provider.transcribe.side_effect = RuntimeError('provider unavailable')
        result = await media.analyze(asset.id, '内容是什么')
        self.assertIn('音轨分析失败', result['text'])
        self.assertIn('一张画面', result['text'])

    async def test_child_execution_completion_resumes_parent_and_delivers_only_once(self):
        from osa.application.v2.worker import WorkAgent
        await self.store.update_policy('10001', GroupPolicy(sandbox=True))
        self.registry.begin_thinking('parent', self.context)
        task = self.registry.create(self.event.actor_id, '10001', 'addition', 'one', self.context['source_event'])
        self.registry.attach_parent(task['id'], 'parent')
        self.registry.command(task['id'], 'admin', None, 'APPROVE', {}, task['version'], 'approval', admin=True)
        task = self.registry.claim('worker')
        executor = AsyncMock()
        executor.run.return_value = report()
        await WorkAgent(self.registry, AsyncMock(return_value=FILES), executor, self.directory.name).run(task)
        artifact_id = self.registry.get(task['id'])['artifact_id']
        model = AsyncMock()
        model.generate.side_effect = [model_result({'final_action': 'REPLY', 'reply_brief': {'intent': 'deliver',
            'reply_text': '已通过隔离单元测试，成果在后台；尚未人工验收。', 'attachment_ids':[artifact_id]}})]
        service = V3Service(self.settings, model, self.v2, self.store)
        self.assertTrue(await service.resume_once())
        self.assertFalse(await service.resume_once())
        self.assertEqual(len(self.store.outboxes), 1)
        self.assertEqual([c.args[1] for c in model.generate.await_args_list], ['thinking'])
        item = next(iter(self.store.outboxes.values()))
        self.assertIn('尚未人工验收', item['text'])
        self.assertEqual(item['attachment_ids'],[])
        self.assertTrue(self.store.decisions[item['decision_id']]['snapshot']['deliver_work_file'])

    async def test_remote_lease_expiry_does_not_claim_pause_without_idle_confirmation(self):
        task = self.registry.create('u', None, 'x', 'one')
        self.registry.command(task['id'], 'admin', None, 'APPROVE', {}, 1, 'a', admin=True)
        task = self.registry.claim('w')
        self.registry.bind(task['id'], task['generation'], 'opencode', 'ses_x', 'RUNNING', {})
        self.registry.command(task['id'], 'u', None, 'PAUSE', {}, task['version'], 'pause')
        with self.registry.tx() as db:
            db.execute('UPDATE v2_tasks SET lease_until=0 WHERE id=?', (task['id'],))
        self.registry.reap()
        self.assertEqual(self.registry.get(task['id'])['status'], 'PAUSING')
        self.assertIsNone(self.registry.claim('new-worker'))
        self.assertTrue(self.registry.confirm_remote_stop(task['id'], 'PAUSING'))
        self.assertEqual(self.registry.get(task['id'])['status'], 'PAUSED')


class OpenCodeContracts(V2AsyncCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.registry = Registry(str(Path(self.directory.name)/'registry.db'))
        task = self.registry.create('u', None, 'addition', 'x')
        self.registry.command(task['id'], 'admin', None, 'APPROVE', {}, 1, 'approve', admin=True)
        self.task = self.registry.claim('worker')
        self.req = CodingTaskRequest('p', 'u', None, self.task['workspace_id'], 'addition', [], self.task['id'])

    async def asyncTearDown(self):
        self.directory.cleanup()

    async def test_pinned_version_and_ambiguous_submit_never_create_twice(self):
        calls = []
        def handle(request):
            calls.append((request.method, request.url.path))
            if request.url.path == '/global/health':
                return httpx.Response(200, json={'healthy': True, 'version': '1.2.27'})
            if request.method == 'POST' and request.url.path == '/session':
                raise httpx.ReadTimeout('ambiguous', request=request)
            return httpx.Response(200, json=[])
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            adapter = OpenCodeAdapter(self.registry, 'http://opencode', 'secret', client=client)
            with self.assertRaises(ProviderStateUnknown):
                await adapter.submit(self.req)
            with self.assertRaises(ProviderStateUnknown):
                await adapter.submit(self.req)
            self.assertEqual(calls.count(('POST', '/session')), 1)
            with self.assertRaises(UnsupportedCapability):
                await adapter.pause(self.task['id'])

    async def test_restart_reconciles_session_and_prompt_id_without_repeat(self):
        tid = self.task['id']
        self.registry.bind(tid, self.task['generation'], 'opencode', 'ses_old', 'RUNNING', {'command_id': 'c', 'message_id': 'msg_saved'})
        posted = []
        mid = 'msg_saved'
        def handle(request):
            if request.method == 'POST':
                posted.append(request.url.path)
            if request.url.path == '/global/health':
                return httpx.Response(200, json={'healthy': True, 'version': '1.2.27'})
            if request.url.path.endswith('/message'):
                return httpx.Response(200, json=[{'info': {'id': mid}}])
            return httpx.Response(200, json={'id': 'ses_old'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            adapter = OpenCodeAdapter(self.registry, 'http://opencode', 'secret', client=client)
            self.assertEqual(await adapter.submit(self.req), tid)
            self.assertEqual(await adapter.send_instruction(tid, {'command_id': 'c', 'text': 'goal'}), mid)
            self.assertEqual(posted, [])

    async def test_wrong_server_version_fails_before_mutation(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, json={'healthy': True, 'version': 'unlocked'}))) as client:
            adapter = OpenCodeAdapter(self.registry, 'http://opencode', 'secret', client=client)
            with self.assertRaises(ValueError):
                await adapter.submit(self.req)
            self.assertIsNone(self.registry.binding(self.task['id']))

    async def test_collects_workspace_files_without_final_json_and_ignores_internal_files(self):
        def handle(request):
            if request.url.path == '/file':
                if request.url.params['path'] == '.':
                    return httpx.Response(200, json=[{'path': 'snake.py', 'type': 'file'},
                        {'path': 'tests', 'type': 'directory'}, {'path': '.env', 'type': 'file'},
                        {'path': '/etc/passwd', 'type': 'file'}, {'path': '../secret.py', 'type': 'file'}])
                return httpx.Response(200, json=[{'path': 'tests/test_snake.py', 'type': 'file'}])
            self.assertEqual(request.url.path, '/file/content')
            self.assertIn(request.url.params['path'], ['snake.py', 'tests/test_snake.py'])
            return httpx.Response(200, json={'type': 'text', 'content': '# real workspace file'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            adapter = OpenCodeAdapter(self.registry, 'http://opencode', 'secret', client=client)
            files = await adapter.project_files(self.task['id'])
            self.assertEqual(set(files), {'snake.py', 'tests/test_snake.py'})

    async def test_reasoning_only_output_limit_reports_model_failure(self):
        tid = self.task['id']
        self.registry.bind(tid, self.task['generation'], 'opencode', 'ses_test', 'RUNNING', {'message_id': 'msg_user'})
        adapter = OpenCodeAdapter(self.registry, 'http://opencode', 'secret')
        messages = [{'info': {'role': 'assistant', 'parentID': 'msg_user', 'finish': 'length',
            'time': {'completed': 1}}, 'parts': [{'type': 'reasoning', 'text': 'private'}]}]
        with patch.object(adapter, 'submit', new_callable=AsyncMock), \
                patch.object(adapter, 'send_instruction', new_callable=AsyncMock), \
                patch.object(adapter, 'get_status', new_callable=AsyncMock, return_value={'status': 'IDLE'}), \
                patch.object(adapter, 'request', new_callable=AsyncMock, return_value=messages):
            with self.assertRaisesRegex(WorkModelUnavailable, '只进行了推理'):
                await adapter._plan(self.task)


class ConcurrentInbox(V2AsyncCase):
    async def test_a_slow_group_does_not_hold_free_worker_slots_in_other_group(self):
        store = InMemoryStore()
        await store.initialize(['10001', '10002'])
        first, second = make_event(message_id='1'), make_event(group='10002', message_id='2')
        third = make_event(group='10002', message_id='3')
        gate, started, third_done = asyncio.Event(), asyncio.Event(), asyncio.Event()
        class Runtime:
            async def process(self, event):
                if event.id == first.id:
                    started.set()
                    await gate.wait()
                if event.id == third.id:
                    third_done.set()
        await store.publish(first, True)
        await store.publish(second, True)
        loop = asyncio.create_task(inbox_loop(store, Runtime(), 2))
        try:
            await asyncio.wait_for(started.wait(), 1)
            await store.publish(make_event(message_id='4'), True)
            await store.publish(third, True)
            await asyncio.wait_for(third_done.wait(), 1)
            self.assertFalse(gate.is_set())
        finally:
            loop.cancel()
            await asyncio.gather(loop, return_exceptions=True)


class GatewayContract(V2AsyncCase):
    @staticmethod
    def module():
        import importlib.util
        path = Path(__file__).resolve().parents[1]/'workers/opencode/service.py'
        spec = importlib.util.spec_from_file_location('osa_gateway_test', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    async def test_gateway_auth_path_and_directory_controls_run_before_docker(self):
        module = self.module()
        password = 'test-gateway-password-123456789'
        with patch.dict('os.environ', {'OSA_OPENCODE_PASSWORD': password, 'OSA_OPENCODE_NETWORK': 'test_net'}):
            with patch.object(module.Gateway, 'ensure', new_callable=AsyncMock) as ensure:
                app = module.create_app()
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                    path = '/projects/'+'a'*32
                    self.assertEqual((await client.get(path+'/global/health')).status_code, 401)
                    client.auth = httpx.BasicAuth('opencode', password)
                    self.assertEqual((await client.post(path+'/session/ses_x/shell', json={'command': 'anything'})).status_code, 403)
                    self.assertEqual((await client.get(path+'/global/health?directory=/other')).status_code, 403)
                    self.assertEqual((await client.get(path+'/file/content?path=../secret')).status_code, 403)
                    self.assertEqual((await client.get(path+'/file/content?path=/etc/passwd')).status_code, 403)
                    self.assertEqual((await client.get('/projects/bad/global/health')).status_code, 400)
                    ensure.assert_not_awaited()

    async def test_generated_container_has_no_socket_host_path_or_qq_secret(self):
        module = self.module()
        with patch.dict('os.environ', {'OSA_OPENCODE_PASSWORD': 'test-gateway-password-123456789', 'OSA_OPENCODE_NETWORK': 'test_net'}):
            gateway = module.Gateway()
            self.assertNotEqual(gateway.child_auth('project-a')[1], gateway.password)
            self.assertNotEqual(gateway.child_auth('project-a')[1], gateway.child_auth('project-b')[1])
            fake = AsyncMock()
            fake.wait.return_value = 0
            fake.returncode = 0
            with patch.object(module.asyncio, 'create_subprocess_exec', new=AsyncMock(return_value=fake)) as run:
                await gateway.create(module.workspace_name('scope', 'a'*32), {'OPENCODE_SERVER_PASSWORD': 'p', 'OPENCODE_CONFIG_CONTENT': '{}'})
                args = run.await_args.args
                self.assertIn('--read-only', args)
                self.assertIn('--cap-drop', args)
                self.assertNotIn('/var/run/docker.sock', ' '.join(args))
                self.assertNotIn('OSA_NAPCAT_TOKEN', args)
                self.assertTrue(all('type=volume' in arg for index, arg in enumerate(args) if index and args[index-1] == '--mount'))
                self.assertIn('osa-opencode-runner:1.2.27', args)


if __name__ == '__main__':
    unittest.main()
