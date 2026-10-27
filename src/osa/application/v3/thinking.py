"""Bounded, concurrent short sessions. Tools are injected and policy checked."""
import asyncio
import json
import re
import time
import uuid
import logging
from dataclasses import asdict, replace

from osa.application.prompting import PromptGuard
from osa.application.prompt_catalog import CATALOG
from osa.application.prompt_protocols import THINKING_TOOL_GUIDE
from osa.application.nova_v4 import DIRECT_ADDRESSING, FILE_DOWNLOAD_DISABLED
from osa.application.flow import stage, model_metadata
from osa.domain.context import model_context

from osa.domain.agents import ReplyBrief, ThinkResult
from osa.domain.work import delivery_filename

THINK_PROMPT = CATALOG["agent_thinking"]["default"]
logger = logging.getLogger(__name__)


def parse_action(text):
    value=text.strip()
    value = re.sub(r'^```(?:json)?\s*', '', value, count=1, flags=re.I)
    value = re.sub(r'\s*```$', '', value, count=1).strip()
    try:
        data=json.loads(value)
    except json.JSONDecodeError as exc:
        if exc.msg != 'Extra data':
            raise
        data, end=json.JSONDecoder().raw_decode(value)
        if value[end:].strip() not in {'}', '}}'}:
            raise
    if not isinstance(data,dict):raise ValueError('Expected action object')
    choices = sum(key in data for key in ('tool_call', 'tool_calls', 'final_action'))
    if choices != 1:raise ValueError('Expected one action form')
    if 'tool_calls' in data:
        calls = data['tool_calls']
        if not isinstance(calls, list) or not 1 <= len(calls) <= 4:
            raise ValueError('Expected one to four tool calls')
        ids, signatures = set(), set()
        for tool in calls:
            validated = parse_action(json.dumps({'tool_call': tool}))
            if 'tool_call' not in validated:
                raise ValueError('Reply and finish must follow tool receipts')
            signature = json.dumps([tool['name'], tool['arguments']], sort_keys=True)
            if tool['tool_call_id'] in ids or signature in signatures:
                raise ValueError('Duplicate batch tool call')
            ids.add(tool['tool_call_id'])
            signatures.add(signature)
        return data
    if 'tool_call' in data:
        tool=data['tool_call']
        if not isinstance(tool,dict) or set(tool)!={'tool_call_id','name','arguments'}:
            raise ValueError('Invalid tool action')
        if not isinstance(tool['arguments'],dict) or not isinstance(tool['name'],str):
            raise ValueError('Invalid tool arguments')
        if not isinstance(tool['tool_call_id'],str) or not 1<=len(tool['tool_call_id'])<=100:
            raise ValueError('Invalid tool id')
        if len(json.dumps(tool)) > 12000:
            raise ValueError('Tool arguments too large')
        if tool['name']=='reply':
            raw=tool['arguments']
            if not raw.get('reply_text') and (not isinstance(raw.get('key_points'),list) or not raw['key_points']):
                raise ValueError('reply requires key_points')
            return {'final_action':'REPLY','reply_brief':raw,'_control':True,
                    **({'persona_note': data['persona_note']} if 'persona_note' in data else {})}
        if tool['name']=='finish':
            return {'final_action':'SILENT','_control':True}
        return data
    if data.get('final_action') not in {'SILENT','REPLY','WAIT_FOR_USER','TASK_DELEGATED','FAILED'}:
        raise ValueError('Expected final_action or tool_call')
    if data['final_action'] in {'REPLY','WAIT_FOR_USER'} and not data.get('reply_brief'):
        raise ValueError('Visible action requires reply_brief')
    return data


class ThinkingLoop:
    def __init__(self, model, tools, registry=None, max_steps=12, max_seconds=75, prompt_guard=None):
        self.model, self.tools, self.registry = model, tools, registry
        self.max_steps, self.max_seconds = max_steps, max_seconds
        self.prompt_guard=prompt_guard or PromptGuard()
        self.flow = None

    async def call(self, name, *args, **kwargs):
        return await asyncio.to_thread(getattr(self.registry, name), *args, **kwargs)

    async def run(self, messages, context, session_id=None):
        sid = session_id or uuid.uuid4().hex
        if self.registry:
            session = await self.call("begin_thinking", sid, context)
            if session["status"] == "FINISHED":
                data = session["result"]
                brief = ReplyBrief(**data["brief"]) if data.get("brief") else None
                return ThinkResult(sid, data["final_action"], brief, data.get("delegated_task_ids", []), data.get('persona_note'))
        from osa.application.prompt_catalog import render_prompt
        integrated = context.get('reply_mode') == 'integrated'
        max_chars = context.get('max_reply_chars', 800)
        system=self.prompt_guard.thinking_persona_prompt(context)+'\n【提示词模板：agent_thinking】\n'+render_prompt(self.prompt_guard.source, 'agent_thinking')
        system += self.prompt_guard.turn_identity(context)
        system += await asyncio.to_thread(self.prompt_guard.growth_prompt, context)
        if context.get('group_id'):
            if context.get('bot_actions_requested'):
                system += '\n【接话对象：直接请求分支】\n' + DIRECT_ADDRESSING
            else:
                system += ('\n【接话对象：群旁听分支】这是群旁听，不意味着你有发言义务。不要把其他人的请求变成你的任务。'
                           '跟随正在进行的话题，而不是最近的一个表情。如果没有自然参与理由，就选择 SILENT；'
                           '一句简短的情绪反应也可能是合理参与，但不要为了刷存在感而编造内容。')
        if integrated:
            source = self.prompt_guard.source
            custom_style = source is not None and 'reply_writer' in source.snapshot().prompt_slots
            style = render_prompt(source, 'reply_writer', 最大字数=max_chars) if custom_style else ''
            system += '\n【回复模式：integrated】\n' + style
            system += self.prompt_guard.reply_length_prompt(max_chars)
        if context.get('group_file_download_enabled') is False:
            system += '\n【群文件下载限制】\n' + FILE_DOWNLOAD_DISABLED
        if context.get('registered_group_bot_ids'):
            bot_ids = context['registered_group_bot_ids']
            system += ('\n本群已登记的外部机器人：' + ', '.join(bot_ids[:12])
                       + (' 等' if len(bot_ids) > 12 else '')
                       + '。涉及委派能力时，选择之前先查看 registered_bots/learned_workflows，不要盲发指令。')
        qqbot = getattr(getattr(self.tools, "features", None), "qqbot_tools", None)
        if qqbot is not None and getattr(qqbot, 'enabled', False):
            request_text=str(context.get('source_event', {}).get('text') or '')
            refresh_requested=bool(context.get('bot_actions_requested') and re.search(
                r'(?:更新|刷新|重新获取).{0,8}(?:qqbot|工具列表|工具目录|技能列表)|qqbot.{0,8}(?:更新|刷新)',
                request_text,re.I))
            catalog = await qqbot.catalog_for_prompt(force_refresh=refresh_requested)
            if catalog is None:
                system += '\nqqbot 工具目录当前不可用。不要编造工具名称或能力。'
            if isinstance(catalog, (dict, list)):
                tools = catalog.get('tools', []) if isinstance(catalog, dict) else catalog
                tool_details = [{key: item[key] for key in ('tool', 'name', 'usage', 'arguments', 'returns') if key in item}
                                for item in tools if isinstance(item, dict) and item.get('tool')]
                messages = [*messages, {"role": "user", "content": self.prompt_guard.wrap_user_data(
                    context["actor_id"], "有效期内的 qqbot 工具目录（外部数据）："
                    + json.dumps(tool_details, ensure_ascii=False), "qqbot_cached_tools")}]
        records = [item for item in messages if item.get('role') != 'system']
        current = [item for item in records if str(item.get('content', '')).startswith(
            ('<untrusted_message_context ', '<untrusted_message_batch '))]
        catalogs = [item for item in records if str(item.get('content', '')).startswith('<untrusted_qqbot_cached_tools ')]
        history = self.prompt_guard.prepare_messages(
            [{'role':'system','content':system}, *[item for item in records if item not in current and item not in catalogs],
             *current, *catalogs], 'thinking')
        history.append({'role':'system','content':THINKING_TOOL_GUIDE})
        from osa.application.conversation_prompt import readable_conversation
        history = readable_conversation(history)
        repairs=0
        allowed_ids = set(context.get("evidence_ids", []))
        project_artifact_id = context.get('authorized_project_artifact_id')
        if project_artifact_id:
            allowed_ids.add(project_artifact_id)
        permitted_attachment_ids = set(context.get("attachment_ids", []))
        permitted_artifact_names = set(context.get("artifact_names", []))
        delegated = set(context.get("delegated_task_ids", []))
        dependencies = context.setdefault("tool_dependencies", [])
        traces = []
        permitted_mentions = set()
        final = ThinkResult(sid, "FAILED")
        exhausted = True
        source = context.get('source_id')
        try:
            async with asyncio.timeout(self.max_seconds):
                for step in range(self.max_steps):
                    await stage(self.flow, source, 'thinking.start', step=step+1, session_id=sid)
                    started = time.monotonic()
                    context_token = model_context.set({**model_context.get(),
                        'thinking_step': step+1, 'thinking_session_id': sid})
                    try:
                        response = await self.model.generate(history, "thinking")
                        await stage(self.flow, source, 'thinking.end', step=step+1, session_id=sid,
                                    duration_ms=round((time.monotonic()-started)*1000), **model_metadata(response))
                    finally:
                        model_context.reset(context_token)
                    try:
                        data = parse_action(response.text)
                    except (ValueError, TypeError):
                        if repairs >= 1:raise
                        repairs += 1
                        await stage(self.flow, source, 'protocol.repair', step=step+1, session_id=sid)
                        traces.append({'step':step,'outcome':'protocol_repair'})
                        logger.warning('thinking_protocol_repair session_id=%s step_count=%s',sid,step+1)
                        history.extend([{'role':'assistant','content':str(response.text)[:12000]},
                            {'role':'user','content':'上一轮没有返回有效动作，所以没有发送任何群消息或执行工具。请只返回动作 JSON。要说话请返回 REPLY，并在 reply_brief.reply_text 中写出最终正文；不要直接输出发言文本。'}])
                        continue
                    if data.pop('_control',None):
                        traces.append({'step':step,'tool':'reply' if data['final_action']=='REPLY' else 'finish','outcome':'selected'})
                    calls = data.get('tool_calls', [data['tool_call']] if 'tool_call' in data else [])
                    if calls:
                        history.append({'role': 'assistant', 'content': response.text})
                    for tool in calls:
                        if not isinstance(tool, dict) or set(tool) != {"tool_call_id", "name", "arguments"}:
                            raise ValueError("Invalid tool call")
                        if not isinstance(tool["arguments"], dict) or len(json.dumps(tool)) > 12000:
                            raise ValueError("Tool arguments too large")
                        cid = str(tool["tool_call_id"])
                        if not 1 <= len(cid) <= 100:
                            raise ValueError("Invalid tool id")
                        # Side effects use a stable per-session call key in their own DB transaction.
                        await stage(self.flow, source, 'tool.start', step=step+1, session_id=sid, tool=tool['name'])
                        detail_id = uuid.uuid4().hex
                        keep = context.get('collect_messages', True)
                        logger.info('agent_tool_request tool=%s event_id=%s', tool['name'], source,
                            extra={'model_call': {'id':detail_id, 'purpose':'tool', 'tool_name':tool['name'],
                                'event_id':source, 'status':'requesting', 'started_at':time.time(),
                                'messages':[{'role':'user','content':json.dumps(tool['arguments'],ensure_ascii=False)}] if keep else []}})
                        tool_started = time.monotonic()
                        result = await self.tools.execute(tool["name"], tool["arguments"],
                            {**context, "session_id": sid, "tool_call_id": cid, "deadline": time.time()+self.max_seconds})
                        if tool['name'] == 'group.members' and not result.get('error'):
                            permitted_mentions.update(str(member['id']) for member in result.get('_mention_targets', []))
                            context.setdefault('dependency_snapshot', {})['verified_mention_ids'] = sorted(permitted_mentions)
                        logger.info('agent_tool_returned tool=%s event_id=%s', tool['name'], source,
                            extra={'model_call': {'id':detail_id, 'purpose':'tool', 'status':'failed' if result.get('error') else 'completed',
                                'task_id':result.get('task_id'), 'finished_at':time.time(),
                                'response':json.dumps({k:v for k,v in result.items() if not k.startswith('_')},ensure_ascii=False) if keep else '[群策略未开启消息留存，未记录正文]'}})
                        await stage(self.flow, source, 'tool.end', step=step+1, session_id=sid, tool=tool['name'],
                                    outcome='error' if result.get('error') else 'returned',
                                    duration_ms=round((time.monotonic()-tool_started)*1000),
                                    error_type=str(result.get('error', '')).split(':', 1)[0][:80],
                                    task_id=result.get('task_id', ''),
                                    call_id=detail_id,
                                    count=sum(len(v) for k,v in result.items() if k in {'memories','knowledge','files','notices'} and isinstance(v,list)))
                        if len(json.dumps(result, ensure_ascii=False)) > 24000:
                            result = {"error": "OUTPUT_LIMIT"}
                        if result.get("task_id"):
                            delegated.add(result["task_id"])
                        for dep in result.get("_dependencies", []):
                            if dep not in dependencies:
                                dependencies.append(dep)
                        permitted_attachment_ids.update(str(value) for value in result.get("_attachment_ids", []) if value)
                        if result.get("_shared_files"):
                            context.setdefault("dependency_snapshot", {}).setdefault("shared_files", []).extend(
                                value for value in result["_shared_files"] if value not in context["dependency_snapshot"].get("shared_files", []))
                        snapshot = context.setdefault("dependency_snapshot", {})
                        permitted_artifact_names.update(result.get('_artifact_names', []))
                        if result.get('_authorized_project_artifact_id'):
                            project_artifact_id = result['_authorized_project_artifact_id']
                            allowed_ids.add(project_artifact_id)
                        if result.get('_work_task_id'):
                            snapshot['work_task_id'] = result['_work_task_id']
                        if result.get('_task_capability'):
                            snapshot['task_capability'] = result['_task_capability']
                        for source_id in result.get('_source_ids', []):
                            if source_id not in snapshot.setdefault('source_ids', []):snapshot['source_ids'].append(source_id)
                        for kid in result.get('_knowledge_ids', []):
                            allowed_ids.add(kid)
                            if kid not in snapshot.setdefault('knowledge_ids', []):
                                snapshot['knowledge_ids'].append(kid)
                        snapshot.setdefault('document_versions', {}).update(result.get('_document_versions', {}))
                        for kind in result.get("_resources_used", []):
                            if kind not in snapshot.setdefault("resources_used", []):
                                snapshot["resources_used"].append(kind)
                        for iid in result.get("_input_ids", []):
                            if iid not in snapshot.setdefault("input_ids", []):
                                snapshot["input_ids"].append(iid)
                        for memory in result.get("_memory_dependencies", []):
                            allowed_ids.add(memory["id"])
                            ids = snapshot.setdefault("memory_ids", [])
                            if memory["id"] not in ids:
                                ids.append(memory["id"])
                            snapshot.setdefault("memory_versions", {})[memory["id"]] = memory["version"]
                            sources = snapshot.setdefault("source_ids", [])
                            if memory["source_id"] not in sources:
                                sources.append(memory["source_id"])
                        allowed_ids.add(cid)
                        traces.append({"step": step, "tool": tool["name"], "call_id": cid, "outcome": "error" if result.get("error") else "returned"})
                        history.append({"role": "user", "content": json.dumps(
                            {"tool_result": result, "tool_call_id": cid}, ensure_ascii=False)})
                        if result.get('error'):
                            for skipped in calls[calls.index(tool) + 1:]:
                                history.append({'role': 'user', 'content': json.dumps({
                                    'tool_result': {'error': 'BATCH_SKIPPED',
                                                    'detail': 'Earlier action failed; inspect its receipt before retrying.'},
                                    'tool_call_id': skipped['tool_call_id']}, ensure_ascii=False)})
                            break
                    if calls:
                        continue
                    action = data.get("final_action")
                    if action not in {"SILENT", "REPLY", "WAIT_FOR_USER", "TASK_DELEGATED", "FAILED"}:
                        raise ValueError("Invalid think terminal")
                    ids = data.get("delegated_task_ids", [])
                    if not isinstance(ids, list) or not set(ids) <= delegated:
                        raise ValueError("Unproven task delegation")
                    mention_ids = (data.get('reply_brief') or {}).get('mention_ids', [])
                    if (not isinstance(mention_ids, list) or len(mention_ids) > 5
                            or any(not isinstance(value, str) or not re.fullmatch(r'\d{5,20}', value) for value in mention_ids)
                            or not set(mention_ids) <= permitted_mentions):
                        raise ValueError('Unverified mention target')
                    note = data.get('persona_note')
                    raw_brief = data.get('reply_brief')
                    if isinstance(raw_brief, dict) and isinstance(raw_brief.get('reply_text'), str):
                        marker = re.search(r'\s*(?:〔记〕|【记】|\[记\])([^\r\n]*)\s*$', raw_brief['reply_text'])
                        if marker and self.prompt_guard.source is not None and self.prompt_guard.source.snapshot().persona.self_update_enabled:
                            note = note or {'text': marker[1].strip()}
                            raw_brief['reply_text'] = raw_brief['reply_text'][:marker.start()].rstrip()
                            if not raw_brief['reply_text'] and not raw_brief.get('attachment_ids'):
                                action = 'SILENT'
                    if integrated and action in {"REPLY", "WAIT_FOR_USER", "TASK_DELEGATED"} and data.get("reply_brief") and not (
                        str(data["reply_brief"].get("reply_text") or "").strip() or
                        data["reply_brief"].get("attachment_ids")
                    ):
                        if repairs >= 1:
                            raise ValueError("Final reply text required")
                        repairs += 1
                        await stage(self.flow, source, 'protocol.repair', step=step+1, session_id=sid)
                        history.extend([{'role':'assistant','content':str(response.text)[:12000]},
                            {'role':'user','content':'当前模式没有独立回复模型，也没有向群里发送消息。若确实需要发言，请重新输出动作 JSON，并在 reply_brief.reply_text 写出完整的最终 QQ 回复；否则返回 SILENT。'}])
                        continue
                    brief = None
                    if action != "SILENT" and data.get("reply_brief"):
                        raw = data["reply_brief"]
                        if not isinstance(raw, dict) or set(raw)-{"intent", "key_points", "evidence_ids", "limitations", "attachment_ids", "artifact_names", "artifact_renames", "reply_text", "mention_ids"}:
                            raise ValueError("Invalid brief")
                        reply_text = raw.get('reply_text')
                        if reply_text is not None and (not integrated or not isinstance(reply_text, str)
                                or not reply_text.strip() or len(reply_text) > max_chars):
                            raise ValueError('Invalid final reply text')
                        for key in ("key_points", "evidence_ids", "limitations"):
                            if not isinstance(raw.get(key, []), list) or len(raw.get(key, [])) > 16 or any(not isinstance(x, str) or len(x) > 2000 for x in raw.get(key, [])):
                                raise ValueError("Invalid brief fields")
                        if not set(raw.get("evidence_ids", [])) <= allowed_ids:
                            raise ValueError("Unknown evidence references")
                        attachment_ids = raw.get("attachment_ids", [])
                        if not isinstance(attachment_ids, list) or len(attachment_ids) > 3 or any(not isinstance(x, str) or len(x) > 160 for x in attachment_ids):
                            raise ValueError("Invalid attachment ids")
                        # A validated work ZIP is delivered by the project-file
                        # channel, never looked up as a sticker/media asset.
                        project_selected = bool(project_artifact_id and project_artifact_id in attachment_ids)
                        attachment_ids = [value for value in attachment_ids if value != project_artifact_id]
                        if not set(attachment_ids) <= permitted_attachment_ids:
                            raise ValueError("Unknown attachment references")
                        artifact_names = raw.get("artifact_names", [])
                        if project_selected and 'project.zip' not in artifact_names:
                            artifact_names = [*artifact_names, 'project.zip']
                            permitted_artifact_names.add('project.zip')
                        if not isinstance(artifact_names, list) or len(artifact_names) > 8 or any(not isinstance(x, str) or len(x) > 200 for x in artifact_names):
                            raise ValueError("Invalid artifact names")
                        if not set(artifact_names) <= permitted_artifact_names:
                            raise ValueError("Unknown artifact names")
                        renames = raw.get('artifact_renames', {})
                        if not isinstance(renames, dict) or not set(renames) <= set(artifact_names):
                            raise ValueError('只能给已选择的真实文件改名')
                        for original, renamed in renames.items():
                            delivery_filename(original, renamed)
                        brief = ReplyBrief(str(raw.get("intent", "respond"))[:100], raw.get("key_points", []), raw.get("evidence_ids", []), raw.get("limitations", []), attachment_ids, artifact_names, renames, reply_text, list(dict.fromkeys(mention_ids)))
                        if project_selected and not brief.key_points:
                            brief.key_points.append('项目文件已完成测试并打包。')
                    if action == "REPLY" and (not brief or not (brief.key_points or brief.attachment_ids or brief.reply_text)):
                        raise ValueError("Reply requires a brief")
                    if action == "TASK_DELEGATED" and not ids:
                        raise ValueError("Delegation requires persisted task")
                    final = ThinkResult(sid, action, brief, ids, note if isinstance(note, dict) and action != 'FAILED' else None)
                    exhausted = False
                    await stage(self.flow, source, 'action', step=step+1, session_id=sid, action=action,
                                attachment_count=len(brief.attachment_ids) if brief else 0)
                    break
        except asyncio.CancelledError:
            await stage(self.flow, source, 'interrupted', session_id=sid)
            raise
        except Exception as exc:
            traces.append({"error_type": type(exc).__name__})
            logger.warning("thinking_failed session_id=%s error_type=%s step_count=%s",
                           sid, type(exc).__name__, len(traces))
            await stage(self.flow, source, 'failed', session_id=sid, error_type=type(exc).__name__)
        if exhausted and (not traces or not traces[-1].get('error_type')):
            await stage(self.flow, source, 'failed', session_id=sid, error_type='ThinkingLimitExceeded')
        if self.registry:
            await self.call("finish_thinking", sid, asdict(final), traces, sorted(delegated), dependencies,
                            context.get("dependency_snapshot", {}))
        return final


class ReplyWriter:
    def __init__(self, model, prompt_guard: PromptGuard | None = None):
        self.model = model
        self.prompt_guard = prompt_guard or PromptGuard()

    async def render(self, brief: ReplyBrief, max_chars=800, conversation=None):
        if brief.reply_text is None and brief.attachment_ids:
            from osa.domain.entities import ModelResult
            return ModelResult('', 'builtin', 'integrated-attachment', attachment_ids=list(brief.attachment_ids), mention_ids=list(brief.mention_ids))
        if brief.reply_text is not None:
            from osa.domain.entities import ModelResult
            text = self.prompt_guard.filter_output(brief.reply_text)[:max_chars]
            if self.prompt_guard.claims_game_participation(text):
                text = '' if conversation is not None else '我不能直接进入游戏，但可以在群里帮你们讨论玩法。'
            if text.strip() == '[无需接话]':
                return ModelResult('', 'builtin', 'integrated-reply')
            return ModelResult(text, 'builtin', 'integrated-reply', attachment_ids=list(brief.attachment_ids) if text else [], mention_ids=list(brief.mention_ids) if text else [])
        from osa.domain.context import model_context
        names = model_context.get().get('participants', [])
        messages = [
            {"role": "system", "content": self.prompt_guard.reply_prompt(max_chars)},
            {"role": "user", "content": json.dumps({**asdict(brief),'participants':names}, ensure_ascii=False)},
        ]
        if conversation is not None:
            messages[0]['content'] += '\n本次是自主接话，下面附有生成前最新群聊。结合它自然接当前话题，不逐条追答旧话；新消息不代表必须放弃回复。同一话题仍在聊时可正常回应。如果原问题已经被解决、话题明显结束或转移，且本次回复已经不合时宜，只输出 [无需接话]。这是回复生成阶段，不再增加独立的复核模型调用。'
            messages.append({'role':'user','content':self.prompt_guard.wrap_user_data('conversation',
                json.dumps(conversation,ensure_ascii=False),'latest_conversation')})
        result = await self.model.generate(messages, "reply")
        if not isinstance(getattr(result, "text", None), str):
            return result
        text = self.prompt_guard.filter_output(result.text)[:max_chars]
        if self.prompt_guard.claims_game_participation(text):
            text = '' if conversation is not None else '我不能直接进入游戏，但可以在群里帮你们讨论玩法。'
        if conversation is not None and text.strip() == '[无需接话]':
            return replace(result, text='', attachment_ids=[])
        return replace(result, text=text,
                       attachment_ids=list(brief.attachment_ids) if text else [], mention_ids=list(brief.mention_ids) if text else [])
