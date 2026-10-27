"""Iterative work agent. Untrusted programs only execute in the sandbox worker."""
import asyncio
import hashlib
import json
import logging

from osa.application.prompt_catalog import CATALOG
from osa.domain.work import clean_text, validate_files

logger = logging.getLogger(__name__)


def parse_work_action(text):
    """Close structural containers only; never guess unfinished keys or strings."""
    raw = text.strip()
    if raw.startswith('```') and raw.endswith('```'):
        raw = raw.split('\n',1)[-1].rsplit('```',1)[0].strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        # A complete value followed by missing closing braces can be recovered
        # unambiguously. Truncated source strings and missing values cannot.
        if exc.pos != len(raw):
            raise
        pending, quoted, escaped = [], False, False
        for char in raw:
            if quoted:
                if escaped:escaped=False
                elif char=='\\':escaped=True
                elif char=='"':quoted=False
            elif char=='"':quoted=True
            elif char in '{[':pending.append('}' if char=='{' else ']')
            elif char in '}]':
                if not pending or pending.pop()!=char:raise exc
        if quoted or not 1 <= len(pending) <= 3:
            raise
        recovered = json.loads(raw+''.join(reversed(pending)))
        logger.info('workspace_action_recovered closing_count=%s',len(pending))
        return recovered


class WorkspaceAgent:
    def __init__(self, model, executor=None, downloader=None, browser=None, consultant=None,
                 before_step=None, save_files=None, artifact_sink=None, max_steps=16):
        self.model, self.executor, self.downloader = model, executor, downloader
        self.browser, self.consultant, self.before_step = browser, consultant, before_step
        self.save_files = save_files
        self.artifact_sink = artifact_sink
        self.max_steps = max_steps
        self.last_execution_result = {}

    async def run(self, prompt, input_reader=None, execution_inputs=None):
        files = dict(prompt.get('existing_files') or {})
        history = [{'role': 'system', 'content': CATALOG['workspace_agent']['default']},
                   {'role': 'user', 'content': json.dumps(prompt, ensure_ascii=False)}]
        last = None
        executions = []
        repairs = 0
        for step in range(self.max_steps):
            if self.before_step:
                await self.before_step(step, '工作模型正在规划下一步')
            last = await self.model.generate(history, 'work')
            try:
                data = parse_work_action(last.text)
                if not isinstance(data, dict) or sum(k in data for k in ('files','tool_call','final_action')) != 1:
                    raise ValueError('Work agent must return one action object')
                if 'final_action' in data and data['final_action'] != 'DONE':
                    raise ValueError('Unknown final action')
                if 'tool_call' in data and (not isinstance(data['tool_call'],dict) or
                    not isinstance(data['tool_call'].get('name'),str) or
                    not isinstance(data['tool_call'].get('arguments',{}),dict)):
                    raise ValueError('Invalid tool call')
            except (ValueError, TypeError):
                if repairs >= 1:
                    raise
                repairs += 1
                history += [{'role':'assistant','content':str(last.text)[:12000]},
                    {'role':'user','content':'上一轮不是有效的工作动作，没有执行工具或交付文件。请只返回一个 JSON：tool_call、files 或 final_action=DONE；不要直接回答用户或输出 Markdown。'}]
                continue
            # Existing work-model configurations remain valid.
            if 'files' in data:
                return validate_files({**files, **data['files']}), last
            if data.get('final_action') == 'DONE':
                return validate_files(files), last
            call = data.get('tool_call')
            if not isinstance(call, dict) or not isinstance(call.get('arguments', {}), dict):
                raise ValueError('Invalid work tool call')
            name, args = str(call.get('name')), call.get('arguments', {})
            if self.before_step:
                await self.before_step(step, {'file.read': '读取文件', 'file.write': '写入文件',
                    'file.list': '查看工作目录', 'python.execute': '隔离运行程序', 'project.test': '隔离测试项目',
                    'web.read': '读取网页', 'browser.browse': '操作独立浏览器', 'deepseek.consult': '咨询 DeepSeek 网页'}.get(name, '检查工具'))
            try:
                result = await self.execute(name, args, files, prompt, input_reader, execution_inputs, step)
            except asyncio.CancelledError:
                raise
            except (ValueError, PermissionError, KeyError) as exc:
                result = {'error': type(exc).__name__, 'detail': str(exc)[:400]}
            except Exception as exc:
                result = {'error': type(exc).__name__, 'detail': '工具调用未成功，可改用其他工具或咨询 DeepSeek；不能把此步骤视为已完成'}
            if name in {'python.execute', 'project.test'}:
                current = {k: result.get(k) for k in ('status', 'exit_code', 'stdout', 'artifacts', 'error', 'detail') if k in result}
                if 'stdout' in current:
                    current['stdout'] = clean_text(current['stdout'], 12000)
                executions.append(current)
                self.last_execution_result = {
                    'last': executions[-1], 'executions': executions[-8:],
                }
            if name == 'file.write' and not result.get('error') and self.save_files:
                await self.save_files(dict(files))
            history += [{'role': 'assistant', 'content': last.text},
                        {'role': 'user', 'content': json.dumps({'tool_result': result}, ensure_ascii=False)[:24000]}]
        raise ValueError('Work agent step limit reached; task requires more input')

    async def execute(self, name, args, files, prompt, input_reader, execution_inputs, step):
        if name == 'file.list':
            return {'files': list(files), 'inputs': prompt.get('input_attachments', [])}
        if name == 'file.read':
            path = str(args['path'])
            if path in files:
                return {'path': path, 'text': files[path][:20000]}
            if input_reader:
                return await input_reader(path)
            raise PermissionError('Only task files and attached inputs are readable')
        if name == 'file.write':
            path, content = str(args['path']), args['content']
            validate_files({**files, path: content})
            files[path] = content
            return {'path': path, 'bytes': len(content.encode()), 'status': 'written'}
        if name in {'python.execute', 'project.test'}:
            if self.executor is None:
                raise ValueError('Isolated execution is not enabled')
            if name == 'python.execute':
                code = str(args['code'])
                if len(code) > 40000:
                    raise ValueError('Program exceeds execution limit')
                arguments = {'code': code, 'timeout': 30}
                if files:
                    arguments['files'] = validate_files(files)
                kind = 'python'
            else:
                arguments = {'files': validate_files(files), 'timeout': 60}
                kind = 'project'
            if execution_inputs:
                arguments['inputs'] = execution_inputs
            key = hashlib.sha256(json.dumps({'task': prompt['task_id'], 'generation': prompt['generation'],
                'step': step, 'kind': kind, 'arguments': arguments}, sort_keys=True).encode()).hexdigest()
            value = await self.executor.run(kind, arguments, key)
            artifacts = await self.artifact_sink(value.get('artifacts', [])) if self.artifact_sink else []
            return {**{key: value.get(key) for key in ('status', 'exit_code', 'stdout')}, 'artifacts': artifacts}
        if name == 'web.read':
            if self.downloader is None:
                raise ValueError('Web access is not enabled')
            raw, mime, url = await self.downloader.fetch(str(args['url']), max_bytes=1_000_000)
            return {'url': url, 'mime_type': mime, 'text': raw.decode('utf-8', errors='replace')[:18000]}
        if name == 'browser.browse':
            if self.browser is None:
                raise ValueError('Isolated browser is not enabled')
            return await self.browser.browse(str(args['url']), args.get('actions', []))
        if name == 'deepseek.consult':
            if self.consultant is None:
                raise ValueError('DeepSeek is not enabled for this conversation')
            return await self.consultant(args, step)
        raise PermissionError('Unknown workspace tool')
