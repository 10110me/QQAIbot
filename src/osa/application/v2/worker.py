"""Independent Work Runtime. Never imports a QQ adapter or Social model session."""
import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import sqlite3
import subprocess
import time
import uuid
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED
import httpx


from osa.domain.work import StaleLease, WorkModelUnavailable, clean_text, validate_files, problem_fingerprint

logger = logging.getLogger(__name__)


def failure_details(exc):
    """Classify execution failures without asking the user for missing input."""
    if isinstance(exc, (sqlite3.OperationalError, httpx.HTTPError, OSError)):
        return 'infrastructure', '工作服务连接或数据库暂时异常，请重试；已保留项目和检查点。'
    if isinstance(exc, PermissionError):
        return 'policy', '工作输出触发了文件或内容校验，请查看任务要求和执行记录。'
    text = str(exc)
    if 'without readable project files' in text:
        return 'provider', f'OpenCode 未生成可读取的项目文件：{clean_text(text, 800)}'
    if text.startswith('OpenCode reported an error'):
        return 'provider', clean_text(text, 800)
    if 'Repair cannot weaken' in text:
        return 'validation', '修复过程改动了已有测试，平台未接受结果；重试时会继续使用原测试。'
    if 'unittest evidence' in text:
        return 'validation', '项目缺少可执行的验证测试，尚未完成交付。'
    return 'provider', '工作执行失败，已保留原项目；请查看执行记录后重试。'


class WorkAgent:
    def __init__(self, registry, planner, executor, root):
        self.registry,self.planner,self.executor=registry,planner,executor
        self.root=Path(root)

    async def run(self, task):
        tid,generation=task['id'],task['generation']
        directory=self.root/tid/f'generation-{generation}'
        repo=directory/'repo';repo.mkdir(parents=True,exist_ok=True)
        pending=set()
        activity=None
        async def heartbeat():
            while True:
                await asyncio.sleep(20)
                state=await asyncio.to_thread(self.registry.heartbeat,tid,generation)
                if state=='CANCEL_REQUESTED':
                    for request_id in list(pending):
                        await self.executor.cancel(request_id)
                    raise asyncio.CancelledError()
        beat=asyncio.create_task(heartbeat())
        try:
            for repair in range(3):
                task=await asyncio.to_thread(self.registry.boundary,tid,generation)
                if task['status'] in {'PAUSED','CANCELLED'}:return
                if task.get('artifact_id') and task.get('tests_passed',0)>0 and not task.get('tests_failed'):
                    if await asyncio.to_thread(self.registry.finish,tid,generation,'COMPLETED'):return
                    continue
                if task.get('files') and task.get('cursor',0)>=1:
                    files=validate_files(task['files'])
                else:
                    await asyncio.to_thread(self.registry.checkpoint,tid,generation,"model_request",{'current_step':'生成或修复项目源码'},{'type':'TOOL_STARTED','tool':'work_model'})
                    if not await asyncio.to_thread(self.registry.budget,'work:'+time.strftime('%Y-%m-%d'),int(os.environ.get('OSA_WORK_DAILY_MODEL_CALLS','0'))):
                        await asyncio.to_thread(self.registry.finish,tid,generation,'FAILED','工作模型日预算已用完',failure_kind='infrastructure');return
                    activity=asyncio.create_task(self.planner(task))
                    done,_=await asyncio.wait({activity,beat},return_when=asyncio.FIRST_COMPLETED)
                    if beat in done:
                        activity.cancel();await asyncio.gather(activity,return_exceptions=True)
                        await beat
                    files=validate_files(await activity)
                if task.get('test_plan_version')==task['plan_version']:
                    original={k:v for k,v in task.get('files',{}).items() if k.startswith('tests/')}
                    if any(files.get(k)!=v for k,v in original.items()):raise ValueError('Repair cannot weaken or replace the existing tests; request a new plan explicitly')
                if not any(name.startswith('tests/test') and name.endswith('.py') for name in files):
                    raise ValueError('A project needs executable Python unittest evidence')
                if any(re.search(r'-----BEGIN .*PRIVATE KEY|\bsk-[A-Za-z0-9_-]{12,}',content) for content in files.values()):
                    raise PermissionError('Secret-like content in generated project')
                # A generation writes only its own private directory; no tools run here.
                for path in sorted(repo.rglob('*'),reverse=True):
                    if path.is_file() and '.git' not in path.parts:path.unlink()
                for name,content in files.items():
                    target=repo/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_text(content)
                manifest=hashlib.sha256(json.dumps(files,sort_keys=True).encode()).hexdigest()
                git_sha=await asyncio.to_thread(git_checkpoint,repo)
                milestones=[{'key':'source','state':'done','proof':git_sha or manifest},
                            {'key':'tests','state':'running','proof':None},{'key':'artifact','state':'queued','proof':None}]
                task=await asyncio.to_thread(self.registry.checkpoint,tid,generation,'source',
                    {'files':files,'milestones':milestones,'cursor':1,'current_step':'隔离执行单元测试',
                     'manifest_hash':manifest,'git_commit':git_sha,'test_plan_version':task['plan_version'],'tests_passed':0,'tests_failed':0,'artifact_id':None},
                    {'type':'CODE_DIFF','manifest_hash':manifest,'git_commit':git_sha})
                task=await asyncio.to_thread(self.registry.boundary,tid,generation)
                if task['status'] in {'PAUSED','CANCELLED'}:return
                if task['cursor']==0:continue
                await asyncio.to_thread(self.registry.start_validation,tid,generation)
                request_id=hashlib.sha256(f'{tid}:{task["plan_version"]}:{manifest}'.encode()).hexdigest()
                pending.add(request_id)
                input_files = {}
                for path in sorted((directory/'inputs').glob('*')):
                    if path.is_symlink() or not path.is_file():
                        raise PermissionError('Invalid bound input')
                    if path.stat().st_size > 10 * 1024 * 1024:
                        raise ValueError('Bound input exceeds limit')
                    input_files[path.name] = base64.b64encode(await asyncio.to_thread(path.read_bytes)).decode('ascii')
                execution_args = {'files': files, 'timeout': 60}
                if input_files:
                    execution_args['inputs'] = input_files
                execution=asyncio.create_task(self.executor.run('project',execution_args,request_id))
                activity=execution
                done,_=await asyncio.wait({execution,beat},return_when=asyncio.FIRST_COMPLETED)
                if beat in done:
                    execution.cancel();await asyncio.gather(execution,return_exceptions=True)
                    await beat
                result=await execution;pending.discard(request_id)
                report=extract_report(result)
                passed=int(report.get('passed',0));failed=int(report.get('failures',0))+int(report.get('errors',0))
                good=result.get('status')=='succeeded' and passed>0 and failed==0 and report.get('exit_code')==0
                milestones[1].update(state='done' if good else 'failed',proof=request_id)
                log=clean_text(result.get('stdout',''),8000)
                previous_result = (await asyncio.to_thread(self.registry.get,tid)).get('execution_result') or {}
                validation = {'status':result.get('status'),'exit_code':result.get('exit_code'),
                              'stdout':log,'test_report':report}
                execution_result = {**previous_result, 'validation':validation,
                                    'last':previous_result.get('last') or validation}
                task=await asyncio.to_thread(self.registry.checkpoint,tid,generation,'tests',
                    {'milestones':milestones,'cursor':2 if good else 0,'tests_passed':passed,'tests_failed':failed or (0 if good else 1),
                     'execution_result':execution_result,
                     'blocker':None if good else log[-2000:],'current_step':'归档交付' if good else '测试失败，准备修复'},
                    {'type':'TEST_RESULT','report':report,'execution_id':request_id,'stdout':log,'exit_code':result.get('exit_code')})
                if not good:
                    await asyncio.to_thread(self.registry.record,'experience',task['owner_id'],task['group_id'],
                        {'summary':'工作测试失败','symptom':log[-4000:],'task_id':tid,'source_ids':[tid],
                         'environment':{'python':report.get('runtime')},'fingerprint':problem_fingerprint(log,{'python':report.get('runtime')}),'attempts':[{'manifest':manifest,'result':'failed','evidence':request_id}]},
                        'work-error:'+request_id,'unresolved')
                    continue
                # A source bundle is published only after another current-lease check.
                task=await asyncio.to_thread(self.registry.boundary,tid,generation)
                if task['status'] in {'PAUSED','CANCELLED'}:return
                if task['cursor']==0:continue
                outputs = directory/'artifacts'
                outputs.mkdir(parents=True,exist_ok=True)
                # All selectable files are ready before notifying Thinking.
                # Keep simple source files available individually as well as ZIP.
                for name, content in files.items():
                    if '/' not in name and name != 'project.zip':
                        (outputs/name).write_bytes(content.encode('utf-8'))
                for output in result.get('artifacts', []):
                    name = str(output.get('name') or '')
                    if not name or name.startswith('.') or any(c in name for c in '/\\:') or name in {'test-report.json','project.zip'}:
                        continue
                    raw = base64.b64decode(output['content'],validate=True)
                    if len(raw)>5_000_000:raise ValueError('Program artifact exceeds delivery limit')
                    if output.get('sha256') and hashlib.sha256(raw).hexdigest()!=output['sha256']:
                        raise PermissionError('Program artifact integrity mismatch')
                    (outputs/name).write_bytes(raw)
                output_metadata=[]
                for output in sorted(outputs.glob('*')):
                    if output.is_symlink() or not output.is_file():raise PermissionError('Invalid program artifact')
                    raw=output.read_bytes()
                    output_metadata.append({'name':output.name,'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest(),'status':'stored_for_delivery'})
                artifact=directory/'project.zip'
                with ZipFile(artifact,'w',ZIP_DEFLATED) as archive:
                    for name,content in files.items():archive.writestr(name,content)
                    archive.writestr('test-report.json',json.dumps(report,ensure_ascii=False))
                    output_bytes = 0
                    for output in sorted((directory/'artifacts').glob('*')):
                        if output.is_symlink() or not output.is_file():
                            raise PermissionError('Invalid program artifact')
                        output_bytes += output.stat().st_size
                        if output_bytes > 10_000_000:
                            raise ValueError('Program artifacts exceed delivery limit')
                        archive.write(output, 'outputs/' + output.name)
                artifact_id=f'{generation}:{hashlib.sha256(artifact.read_bytes()).hexdigest()}'
                milestones[2].update(state='done',proof=artifact_id)
                await asyncio.to_thread(self.registry.checkpoint,tid,generation,'artifact',
                    {'artifact_id':artifact_id,'milestones':milestones,'cursor':3,'current_step':'测试通过，成果已准备',
                     'output_artifacts':output_metadata},
                    {'type':'ARTIFACT','sha256':artifact_id.split(':')[1],'size':artifact.stat().st_size})
                for case in await asyncio.to_thread(self.registry.records,'experience',admin=True):
                    if case['payload'].get('task_id')==tid and case['state']=='unresolved':
                        await asyncio.to_thread(self.registry.update_record,case['id'],'verified_local',
                            {'resolution':'修复后在隔离 Python 环境中通过当前项目测试','verification':report,'verified_execution':request_id,'attempts':case['payload'].get('attempts',[])+[{'manifest':manifest,'git_commit':git_sha,'result':'tests_passed','evidence':request_id}]})
                if await asyncio.to_thread(self.registry.finish,tid,generation,'COMPLETED'):return
            task=await asyncio.to_thread(self.registry.boundary,tid,generation)
            if task['status'] in {'PAUSED','CANCELLED'}:return
            await asyncio.to_thread(self.registry.finish,tid,generation,'FAILED','三次生成或修复后仍未通过测试，可查看测试记录后重试',failure_kind='validation')
        except asyncio.CancelledError:
            if activity and not activity.done():
                activity.cancel();await asyncio.gather(activity,return_exceptions=True)
            for request_id in pending:
                try:await self.executor.cancel(request_id)
                except Exception:pass
            try:
                task=await asyncio.to_thread(self.registry.boundary,tid,generation)
            except StaleLease:pass
            else:
                if task['status']=='CANCELLED':return
            raise
        except StaleLease:
            return
        except (WorkModelUnavailable, TimeoutError) as exc:
            try:
                current = await asyncio.to_thread(self.registry.boundary,tid,generation)
                if current['status'] in {'PAUSED','CANCELLED'}:return
                await asyncio.to_thread(self.registry.checkpoint, tid, generation, 'model-failed',
                    {'current_step':'模型服务失败，任务未完成','failure_kind':'infrastructure'}, {'error_type':'WorkModelUnavailable'})
                message = str(exc) if isinstance(exc, WorkModelUnavailable) else '工作空间服务连接失败或请求超时，任务未完成；请检查服务后重试'
                await asyncio.to_thread(self.registry.finish,tid,generation,'FAILED',clean_text(message,1000))
            except StaleLease:pass
        except Exception as exc:
            kind, message = failure_details(exc)
            logger.warning('work_task_failed task_id=%s error_type=%s failure_kind=%s detail=%s',
                tid, type(exc).__name__, kind, clean_text(str(exc), 300))
            for attempt in range(3):
                try:
                    await asyncio.to_thread(self.registry.finish,tid,generation,'FAILED',message,failure_kind=kind)
                    break
                except StaleLease:
                    break
                except sqlite3.OperationalError:
                    if attempt == 2:
                        logger.warning('work_failure_save_deferred task_id=%s',tid)
                        break
                    await asyncio.sleep(1)
        finally:
            beat.cancel();await asyncio.gather(beat,return_exceptions=True)


def extract_report(result):
    import base64
    for item in result.get('artifacts',[]):
        if item.get('name')=='test-report.json':
            raw=base64.b64decode(item['content'],validate=True)
            if len(raw)>20000:raise ValueError('Test report too large')
            report=json.loads(raw)
            for key in ['passed','failures','errors','tests_run']:
                if not isinstance(report.get(key),int) or not 0<=report[key]<=10000:raise ValueError('Invalid test counts')
            if report['passed']>report['tests_run']:raise ValueError('Invalid test report')
            return report
    return {'passed':0,'failures':0,'errors':1,'tests_run':0,'error':'No test report'}


def git_checkpoint(repo):
    try:
        for command in [['git','init','--quiet'],['git','add','--all'],
                        ['git','-c','user.name=OSA Work','-c','user.email=work@localhost','-c','core.hooksPath=/dev/null','commit','--quiet','--allow-empty','-m','Verified source checkpoint']]:
            subprocess.run(command,cwd=repo,check=True,capture_output=True,timeout=20)
        return subprocess.run(['git','rev-parse','HEAD'],cwd=repo,check=True,capture_output=True,text=True,timeout=5).stdout.strip()
    except (OSError,subprocess.SubprocessError):return None

