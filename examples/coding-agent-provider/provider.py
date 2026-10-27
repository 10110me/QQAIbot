"""Deterministic community-provider double. Never used as a live fallback."""
from dataclasses import asdict
from osa.domain.agents import AgentCapabilities


class MockCodingAgent:
    capabilities = AgentCapabilities(can_pause=True, can_resume=True, can_cancel=True, supports_stream=True)

    def __init__(self):
        self.tasks = {}

    async def submit(self, request):
        tid = request.idempotency_key
        self.tasks.setdefault(tid, {"task_id": tid, "status": "QUEUED", "request": asdict(request), "simulated": True})
        return tid

    async def get_status(self, task_id):
        return dict(self.tasks[task_id])

    async def send_instruction(self, task_id, command):
        self.tasks[task_id]["last_instruction"] = command
        return command["command_id"]

    async def pause(self, task_id):
        self.tasks[task_id]["status"] = "PAUSED"

    async def resume(self, task_id):
        self.tasks[task_id]["status"] = "QUEUED"

    async def cancel(self, task_id):
        self.tasks[task_id]["status"] = "CANCELLED"

    async def stream_events(self, task_id):
        yield {"schema_version": 1, "event_type": "coding.progress.updated", "aggregate_id": task_id,
               "payload": await self.get_status(task_id), "simulated": True}

    async def artifacts(self, task_id):
        return []


class FakeObserver:
    async def inspect(self, task_id, mode, revision=None):
        return {"task_id": task_id, "mode": mode, "observed_revision": revision, "readonly": True,
                "findings": [], "status": "simulated", "simulated": True,
                "limitations": ["测试替身；没有查看或验证真实代码"]}
