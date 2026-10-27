"""Social-side V2 facade: short registry reads, ACL and send-time presence checks."""
import asyncio
import hashlib
import json
import re
import shutil
import time
from pathlib import Path

from osa.domain.work import clean_text, external_question, problem_fingerprint
from osa.domain.entities import Event


class V2Service:
    def __init__(self, registry, settings):
        self.registry,self.settings=registry,settings
        self.echo_previous={}
        self.embedder=None

    async def call(self,name,*args,**kwargs):
        return await asyncio.to_thread(getattr(self.registry,name),*args,**kwargs)

    async def allowed(self,destination,initiative=False):
        if destination.kind=='group' and await self.call('echo_active',destination.id):
            policy=await self.call('policy',destination.id)
            if policy.get('echo_silence',True):return False
        # Proactive chat is independent from the coding worker.  The old
        # presence gate silenced every initiative candidate while any project
        # was running, even in unrelated groups; the decision model and group
        # rate limits already provide the appropriate back-pressure.
        return True

    @staticmethod
    def is_work_request(text: str) -> bool:
        value = text.strip().lower()
        if re.search(r"(?:不要|不用|无需|别)(?:帮我)?(?:写|编写|生成|创建)|(?:解释|讲解|分析)(?:一下)?这段",value):
            return False
        artifact = r"(?:python|程序|代码|脚本|项目|文件|文档|(?:py|pip|npm|node|js|java|rust|go)\s*(?:包|库|模块|插件))"
        action = r"(?:写|编写|创建|生成|开发|实现|修改)"
        return bool(re.search(action + r".{0,60}" + artifact + "|" + artifact + r".{0,60}" + action, value, re.I))

    async def create_work_request(self,event,requirement):
        group=event.destination.id if event.destination.kind=='group' else None
        task=await self.call('create',event.actor_id,group,requirement,event.id,{**event.to_dict(),'text':'','segments':[]})
        inputs=getattr(self,'work_inputs',None)
        rows=await inputs.bind(task,event) if inputs else []
        names='；已关联附件：'+ '、'.join(row.payload['name'] for row in rows) if rows else ''
        return f"已创建工作任务 {task['id']}{names}，等待后台审批。可用 /进度 {task['id']} 查询真实进度。"

    async def maybe_work(self,event,policy):
        if not self.settings.work_enabled or (event.destination.kind=='group' and not policy.sandbox):
            return '工作模式尚未启用，或本群未开启「允许代码任务」，不能执行文件编写任务。' if self.is_work_request(event.text) else None
        if event.text.startswith('/') or len(event.text.strip())<2:return None
        model=getattr(self,'model',None)
        if model is None:return None
        inputs=getattr(self,'work_inputs',None)
        rows=await inputs.recent(event) if inputs else []
        from osa.application.prompting import PromptGuard
        guard=getattr(self,'prompt_guard',None) or PromptGuard()
        request={'request':event.text,'attachments':[{'name':row.payload['name'],'status':row.status,
            'entries':row.payload.get('entries',[])[:4]} for row in rows]}
        if getattr(self,'store',None):
            request['recent_conversation']=await self.store.context(event,8)
        try:
            result=await model.generate([{'role':'system','content':guard.slot('work_decision',用户请求=event.text,附件列表=json.dumps(request['attachments'],ensure_ascii=False))},
                {'role':'user','content':json.dumps(request,ensure_ascii=False)[:60000]}],'decision')
            value=json.loads(result.text.removeprefix('```json').removesuffix('```').strip())
            mode=value.get('mode')
            if mode not in {'work','chat','clarify'}:raise ValueError('Invalid work decision')
        except Exception:
            return '暂时无法完成工作模式评估，请检查「回复规划」模型配置后重试；也可以使用 /工作 明确提交任务。'
        if mode=='chat':return None
        if mode=='clarify':return str(value.get('question') or '请补充希望交付的文件类型和具体要求。')[:1000]
        if any(row.status!='ready' for row in rows):return '关联附件尚未读取成功，请重新上传文件或补充文本要求，避免遗漏需求后直接开工。'
        # The original request remains authoritative; the model's summary is data.
        requirement=event.text
        if value.get('requirement'):requirement+='\n需求评估摘要（不替代原始要求）：'+str(value['requirement'])[:3000]
        return await self.create_work_request(event,requirement)

    async def shared_work(self, destination):
        store=getattr(self,'store',None)
        if not store or destination.kind != 'group' or not (await store.policy(destination)).sandbox:return []
        result=[]
        for tid in await store.published_work(destination):
            try:row=await self.call('get',tid)
            except KeyError:continue
            source=(row.get('source_event') or {}).get('id')
            if row['group_id'] == destination.id and row['status']=='COMPLETED' and source and await store.event_active(source):
                result.append({'task_id':tid,'owner_id':row['owner_id'],'status':row['status'],
                    'requirement':row['requirement'][:800],'artifact_id':row.get('artifact_id'),
                    'access':'已交付本群的成果，可复制为输入；修改原工作区仍只限任务发起人'})
        return result[:12]

    async def artifact_bytes(self,task_id,actor,destination,shared=False):
        if shared:
            visible=await self.shared_work(destination)
            if task_id not in {r['task_id'] for r in visible}:raise PermissionError('Artifact not shared in this group')
            row=await self.call('get',task_id)
        else:
            row=await self.call('visible',task_id,actor,destination.id if destination.kind=='group' else None)
        if row['status']!='COMPLETED' or not row.get('artifact_id'):
            raise PermissionError('Work artifact not ready')
        generation,digest=row['artifact_id'].split(':',1)
        inputs=getattr(self,'work_inputs',None)
        if inputs:await inputs.for_task(row,int(generation))
        if not re.fullmatch('[a-f0-9]{32}',task_id) or not generation.isdecimal() or not re.fullmatch('[a-f0-9]{64}',digest):
            raise PermissionError('Invalid artifact reference')
        path=Path(self.settings.workspace_dir)/task_id/f'generation-{int(generation)}'/'project.zip'
        def read():
            if path.stat().st_size>10*1024*1024:raise PermissionError('Artifact too large')
            content=path.read_bytes()
            if hashlib.sha256(content).hexdigest()!=digest:raise PermissionError('Artifact hash mismatch')
            return content
        return await asyncio.to_thread(read)

    async def artifact_output_bytes(self, task_id, actor, destination, name):
        """Read one model-named output file after task and hash authorization."""
        if name == 'project.zip':
            return await self.artifact_bytes(task_id, actor, destination)
        row = await self.call('visible', task_id, actor, destination.id if destination.kind == 'group' else None)
        full = await self.call('get', task_id)
        if row.get('status') != 'COMPLETED' or full.get('status') != 'COMPLETED':
            raise PermissionError('Work artifact not ready')
        name = str(name or '').strip()
        if not name or len(name) > 200 or name.startswith('.') or any(c in name for c in '/\\:'):
            raise PermissionError('Invalid output filename')
        metadata = next((item for item in full.get('output_artifacts', [])
                         if isinstance(item, dict) and item.get('name') == name), None)
        if not metadata:
            raise PermissionError('Output file was not produced by this task')
        path = Path(self.settings.workspace_dir) / task_id / f"generation-{int(full['generation'])}" / 'artifacts' / name
        def read():
            if not path.is_file() or path.stat().st_size > 5_000_000:
                raise PermissionError('Output file unavailable or too large')
            content = path.read_bytes()
            if hashlib.sha256(content).hexdigest() != str(metadata.get('sha256')):
                raise PermissionError('Output file integrity check failed')
            return content
        return await asyncio.to_thread(read)

    async def observe(self,event,policy):
        if event.event_type!='message.created':return
        if event.destination.kind=='group':
            group=event.destination.id
            fingerprint=hashlib.sha256(event.text.strip().encode()).hexdigest() if event.text.strip() else None
            previous=self.echo_previous.get(group)
            if previous and fingerprint and previous[0]==fingerprint and previous[1]!=event.actor_id and event.occurred_at-previous[2]<20:
                await self.call('echo',group,event.id,event.occurred_at,20)
            self.echo_previous[group]=(fingerprint,event.actor_id,event.occurred_at)
            if len(self.echo_previous)>2000:
                self.echo_previous={k:v for k,v in self.echo_previous.items() if time.time()-v[2]<60}
        if policy.collect_messages and policy.experience_collect and re.search(r'ModuleNotFoundError|ImportError|TypeError|ConnectionRefusedError|Traceback',event.text):
            await self.call('record','experience',event.actor_id,event.destination.id if event.destination.kind=='group' else None,
                {'summary':'群聊问题候选','symptom':clean_text(event.text,4000),'source_ids':[event.id],
                 'source_message_id':event.message_id,'environment':{},'fingerprint':problem_fingerprint(event.text),'attempts':[]},'social-issue:'+event.id,'unresolved')
        if event.reply_to_id and policy.collect_messages and policy.experience_collect:
            group=event.destination.id if event.destination.kind=='group' else None
            cases=await self.call('records','experience',event.actor_id,group)
            for case in cases:
                payload=case['payload'];attempts=payload.get('attempts',[])
                parent=payload.get('source_message_id')==event.reply_to_id
                chosen=next((a for a in attempts if a.get('message_id')==event.reply_to_id),None)
                if event.actor_id==case['owner_id'] and chosen and event.text.strip() in {'这个方案解决了','还是没解决','之前说错了，没解决'}:
                    await self.call('update_record',case['id'],case['state'],{'proposed_solution':chosen['solution']})
                    await self.call('feedback',case['id'],event.actor_id,group,event.text.strip()=='这个方案解决了',event.id)
                elif parent and event.text.strip() in {'这个方案解决了','还是没解决','之前说错了，没解决'} and event.actor_id==case['owner_id']:
                    await self.call('feedback',case['id'],event.actor_id,group,event.text.strip()=='这个方案解决了',event.id)
                elif parent and re.search(r'检查|安装|配置|尝试|pip|python|版本|环境',event.text,re.I):
                    attempt={'solution':clean_text(event.text,2000),'message_id':event.message_id,'actor':event.actor_id,'source_event_id':event.id,'result':'proposed'}
                    await self.call('update_record',case['id'],'proposed',{'attempts':(attempts+[attempt])[-20:],
                        'source_ids':list(dict.fromkeys(payload.get('source_ids',[])+[event.id]))})

    async def route(self,event,policy,*,allow_work_creation=True):
        text=event.text.strip();group=event.destination.id if event.destination.kind=='group' else None
        if allow_work_creation and (text.startswith('/工作 ') or (getattr(self,'model',None) is None and self.is_work_request(text))):
            if not self.settings.work_enabled or (group and not policy.sandbox):return '工作运行时未启用，或本群未授权隔离执行。'
            requirement=text[4:].strip() if text.startswith('/工作 ') else text
            return await self.create_work_request(event,requirement)
        if text.startswith('/进度 ') or text in {'进度','写到哪了','做到哪了','还有多久'}:
            tid=text[4:].strip() if text.startswith('/进度 ') else None
            if not tid:
                tasks=await self.call('tasks',event.actor_id,group)
                if not tasks:
                    # Deliberately reveal neither source scope nor private task names.
                    return '当前会话没有可展示的工作任务；跨群项目详情请到原会话或后台查看。'
                tid=tasks[0]['id']
            try:task=await self.call('visible',tid,event.actor_id,group)
            except (KeyError,PermissionError):return '此任务在当前会话不可查看，请到原会话或后台。'
            percent=f"{task['milestones_done']}/{task['milestones_total']} 个里程碑" if task['milestones_total'] else '尚未形成可验证里程碑'
            return f"任务 {tid}：{task['status']}；{percent}。\n当前：{task['current_step']}\n测试通过 {task['tests_passed']}，失败 {task['tests_failed']}。"+(f"\n阻塞：{task['blocker']}" if task['blocker'] else '')+('\n进度未更新，请检查 Worker。' if task['stale'] else '')+'\n完成时间暂无法可靠估算。'
        commands={'/暂停工作 ':'PAUSE','/继续工作 ':'RESUME','/取消工作 ':'CANCEL','/追加需求 ':'ADD_REQUIREMENT'}
        for prefix,kind in commands.items():
            if text.startswith(prefix):
                parts=text[len(prefix):].split(' ',1);tid=parts[0]
                try:
                    task=await self.call('visible',tid,event.actor_id,group)
                    payload={'text':parts[1]} if kind=='ADD_REQUIREMENT' and len(parts)>1 else {}
                    command=await self.call('command',tid,event.actor_id,group,kind,payload,task['version'],event.id)
                    return f"命令 {command['command_id']} 已接收；需求变更在下一个安全步骤应用，尚未应用时不会报完成。"
                except (KeyError,PermissionError,ValueError):return '任务不可控制、版本已变化或参数不完整，请刷新进度后重试。'
        if text.startswith('/经验反馈 '):
            parts=text.split(' ',2)
            if len(parts)!=3 or parts[2] not in {'成功','失败'}:return '格式：/经验反馈 案例ID 成功或失败'
            try:await self.call('feedback',parts[1],event.actor_id,group,parts[2]=='成功',event.id)
            except (KeyError,PermissionError):return '只能反馈本人在原会话中的案例。'
            return '已记录你对该案例的明确反馈；不作为其他环境的成功保证。'
        return None

    async def evidence(self,event):
        group=event.destination.id if event.destination.kind=='group' else None
        cases=await self.call('records','experience',event.actor_id,group)
        public=await self.call('records','public_experience',admin=True)
        public+=await self.call('records','public_claim',admin=True)
        query=event.text.lower();items=[];refs=[]
        ranking={}
        policy=await self.call('policy',group) if group else {}
        if self.embedder and policy.get('experience_embedding'):
            from math import sqrt
            try:
                vector=(await self.embedder.embed([external_question(event.text)]))[0]
                for case in cases+public:
                    embedded=case['payload'].get('embedding',{})
                    other=embedded.get('vector',[])
                    if embedded.get('model')==self.embedder.model_id and len(other)==len(vector):
                        denominator=sqrt(sum(x*x for x in vector)*sum(x*x for x in other))
                        ranking[case['id']]=sum(a*b for a,b in zip(vector,other))/denominator if denominator else 0
            except Exception:pass
        candidates=sorted(cases+public,key=lambda c:ranking.get(c['id'],-1),reverse=True)
        for case in candidates:
            if case['state'] in {'revoked','outdated','disputed'}:continue
            data=case['payload'];summary=data.get('summary','');symptom=data.get('symptom','')
            words=re.findall(r'[A-Za-z_][A-Za-z0-9_.]{3,}',query)
            grams={query[i:i+2] for i in range(len(query)-1) if all('\u4e00'<=c<='\u9fff' for c in query[i:i+2])}
            matches=sum(g in summary+symptom for g in grams)
            exact=problem_fingerprint(event.text).get('error_code_exact')
            recorded=data.get('fingerprint',{}).get('error_code_exact')
            if exact and recorded and exact!=recorded:continue
            version=re.search(r'(?i)python\s*(3\.\d+)',event.text)
            actual=str(data.get('environment',{}).get('python',''))
            if version and actual and not actual.startswith(version.group(1)+'.') and actual!=version.group(1):continue
            if any(word in (summary+' '+symptom).lower() for word in words) or matches>=2 or ranking.get(case['id'],0)>.65:
                # Public retrieval never emits private lineage IDs or source metadata.
                refs.append({'id':case['id'],'state':case['state'],'hash':hashlib.sha256(json.dumps(data,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()})
                items.append({'summary':summary,'symptom':symptom if case['kind']=='experience' else '',
                              'status':case['state'],'environment':data.get('environment',{}),'solution':data.get('proposed_solution',data.get('resolution')),
                              'caveat':'经验是排查线索，需匹配版本和环境；未验证不得当成已解决'})
        return {"items":items[:4],"refs":refs[:4]}

    async def nightly(self,group,date,policy):
        if not policy.research_external:return
        cases=await self.call('records','experience',group=group)
        count=0
        for case in (cases if self.settings.nightly_research_limit > 0 else []):
            question=case['payload'].get('public_question')
            fingerprint=case['payload'].get('fingerprint',{})
            if not question and fingerprint.get('component') in {'PIL','numpy','pandas','requests','fastapi','sqlalchemy','uvicorn'}:
                question='Python 导入公开模块 '+fingerprint['component']+' 出现 ModuleNotFoundError，应如何检查解释器与安装环境？' 
            if not question or case['state'] in {'revoked','outdated'}:continue
            try:question=external_question(question)
            except PermissionError:continue
            fingerprint=hashlib.sha256(question.encode()).hexdigest()
            await self.call('record','night_research',case['owner_id'],group,
                {'question':question,'case_id':case['id'],'date':date,'source_ids':case['payload'].get('source_ids',[]),
                 'external_consent':'group_policy_and_curated_public_question','provider':'deepseek_web'},
                'night:'+group+':'+fingerprint+':'+date[:7]+':v2','QUEUED')
            count+=1
            if count>=self.settings.nightly_research_limit:break
        await self.call('record','consolidation_audit','system',group,
            {'date':date,'research_enqueued':count,'pipeline_version':'v2','input_watermark':self.day_watermark(date,policy.timezone)},
            f'night-audit:{group}:{date}:v2','completed')

    async def erase(self,actor=None,source=None):
        pending=await self.call('records','night_research',admin=True)
        erased=await self.call('erase',actor,source)
        for tid in erased:
            await asyncio.to_thread(shutil.rmtree,Path(self.settings.workspace_dir)/tid,True)
            await self.call('record','erase_work','system',None,{'task_id':tid,'until':time.time()+300},'erase:'+tid,'pending')
        for job in pending:
            if (actor and job['owner_id']==actor) or (source and source in job['payload'].get('source_ids',[])):
                remote=job['payload'].get('remote_id')
                if remote:await self.call('record','erase_browser','system',None,{'remote_id':remote},'erase-browser:'+remote,'pending')

    async def maintenance(self,store):
        while True:
            try:
                if self.embedder:
                    for case in await self.call('records','experience',admin=True):
                        if not case['group_id'] or case['state'] in {'revoked','outdated'}:continue
                        policy=await self.call('policy',case['group_id'])
                        if not policy.get('experience_embedding'):
                            if case['payload'].get('embedding'):await self.call('update_record',case['id'],case['state'],{'embedding':{}})
                            continue
                        fingerprint=case['payload'].get('fingerprint',{})
                        text=' '.join(str(x or '') for x in [fingerprint.get('error_code_exact'),fingerprint.get('component'),case['payload'].get('proposed_solution')])
                        digest=hashlib.sha256(text.encode()).hexdigest()
                        if case['payload'].get('embedding',{}).get('source_hash')==digest:continue
                        try:text=external_question(text)
                        except PermissionError:continue
                        if text.strip():
                            vector=(await self.embedder.embed([text]))[0]
                            await self.call('update_record',case['id'],case['state'],{'embedding':{'model':self.embedder.model_id,'source_hash':digest,'vector':vector}})
                            break
                for deletion in await self.call('records','erase_work',admin=True):
                    if deletion['state']!='pending':continue
                    tid=deletion['payload']['task_id']
                    if re.fullmatch('[a-f0-9]{32}',tid):await asyncio.to_thread(shutil.rmtree,Path(self.settings.workspace_dir)/tid,True)
                    if time.time()>deletion['payload']['until']:await self.call('update_record',deletion['id'],'completed',{})
                for row in await self.call('deliveries'):
                    if row['status']!='COMPLETED':continue
                    if getattr(self.settings,'v3_enabled',False) and row.get('parent_task_id'):
                        # The V3 durable bus resumes Thinking, which chooses REPLY/SILENT.
                        await self.call('delivered',row['id']);continue
                    event=Event.from_dict(row['source_event'])
                    policy=await store.policy(event.destination)
                    if not policy.enabled or (row['group_id'] and not policy.sandbox) or not await store.event_active(event.id):
                        await self.call('delivered',row['id']);continue
                    if not await self.allowed(event.destination) or await store.paused():continue
                    await store.emit_result(event,'work:'+row['id'],f"工作已完成并通过 {row['tests_passed']} 项测试。结果和文件已保存，可在 AI 工作空间查看。",{'task_capability':'sandbox','work_task_id':row['id']})
                    await self.call('delivered',row['id'])
            except asyncio.CancelledError:raise
            except Exception:pass
            await asyncio.sleep(2)

    async def recalled(self,event):
        group=event.destination.id if event.destination.kind=='group' else None
        for case in await self.call('records','experience',event.actor_id,group):
            if case['payload'].get('source_message_id')==event.message_id:
                for source in case['payload'].get('source_ids',[]):await self.erase(source=source)
        for row in await self.call('tasks',admin=True):
            original=row.get('source_event')
            if original and original['message_id']==event.message_id and original['destination']=={'kind':event.destination.kind,'id':event.destination.id}:
                await self.call('erase_task',row['id'])
                await self.call('record','erase_work','system',None,{'task_id':row['id'],'until':time.time()+300},'erase:'+row['id'],'pending')
                await asyncio.to_thread(shutil.rmtree,Path(self.settings.workspace_dir)/row['id'],True)

    @staticmethod
    def day_watermark(date,timezone):
        from datetime import datetime,timedelta
        from zoneinfo import ZoneInfo
        upper=(datetime.fromisoformat(date)+timedelta(days=1)).replace(tzinfo=ZoneInfo(timezone)).timestamp()
        return min(time.time(),upper)
