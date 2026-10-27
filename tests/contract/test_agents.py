import importlib.util
import unittest
from pathlib import Path
from osa.domain.agents import CodingTaskRequest


class ExampleContract(unittest.IsolatedAsyncioTestCase):
    async def test_mock_lifecycle_events_and_readonly_observer(self):
        path = Path(__file__).resolve().parents[2]/'examples/coding-agent-provider/provider.py'
        spec = importlib.util.spec_from_file_location('example_provider', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        provider = module.MockCodingAgent()
        request = CodingTaskRequest('parent', 'owner', 'group', 'workspace', 'goal', [], 'unique-key')
        tid = await provider.submit(request)
        self.assertEqual(await provider.submit(request), tid)
        await provider.pause(tid)
        self.assertEqual((await provider.get_status(tid))['status'], 'PAUSED')
        await provider.resume(tid)
        self.assertEqual(await provider.send_instruction(tid, {'command_id': 'c', 'text': 'add timer'}), 'c')
        events = [e async for e in provider.stream_events(tid)]
        self.assertEqual(events[0]['schema_version'], 1)
        self.assertTrue(events[0]['simulated'])
        before = await provider.get_status(tid)
        result = await module.FakeObserver().inspect(tid, 'REVIEW', 'a'*40)
        self.assertTrue(result['readonly'])
        self.assertEqual(await provider.get_status(tid), before)
        await provider.cancel(tid)
        self.assertEqual((await provider.get_status(tid))['status'], 'CANCELLED')
