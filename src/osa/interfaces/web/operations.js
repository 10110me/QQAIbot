// Live operations share the reference template's controls and authentication.
Object.assign(fullViews, {
  streams: ['聊天流', '观察消息接收、处理决策与回复发送，按日期回看。'],
  logs: ['运行日志', '查看运行状态、错误与管理操作。日志自动轮转保存。']
});
Object.assign(labels, fullViews);
const streamSection = fullView('streams'), logsSection = fullView('logs');
for (const id of ['streams', 'logs']) {
  const nav = document.querySelector('button[data-view="' + id + '"]');
  const anchor = document.querySelector('button[data-view="groups"]');
  if (anchor) anchor.after(nav);
}
const whitelistBox = n('div', undefined, 'stack');
const whitelistPanel = panel('群聊白名单', n('p', '只有白名单内的群会接收消息和发送回复。移除群不会删除历史记录。', 'note'), whitelistBox);
$('sec-groups').prepend(whitelistPanel);
let allowlistState = null, selectedStream = '', streamCursor = null, logCursor = null;
let operationsGeneration = 0, operationsBusy = false;
const whitelistInput = field('添加 QQ 群号');
whitelistInput.control.inputMode = 'numeric';
whitelistInput.control.placeholder = '输入群号';
const openAllOnAdd = check('添加后立即开启全部权限', false);
const whitelistChips = n('div', undefined, 'allowlist-chips');
whitelistBox.append(grid(whitelistInput, openAllOnAdd, btn('+ 添加群聊', async () => {
  const group = whitelistInput.control.value.trim();
  if (!/^[0-9]{1,20}$/.test(group)) throw Error('请输入有效的 QQ 群号');
  if (allowlistState.groups.includes(group)) throw Error('这个群已在白名单中');
  await saveAllowlist([...allowlistState.groups, group]);
  if (openAllOnAdd.control.checked) {
    const current = groups.find(item => String(item.id) === group);
    if (current) {
      const all = ['enabled','collect_messages','notice_read','file_metadata_read','file_content_index','cross_memory','media_read','research','sandbox','initiative','memory_extract','experience_collect','experience_embedding','research_external','echo_silence'];
      const settings = {...current.settings};
      for (const key of all) settings[key] = true;
      await api('/groups/' + encodeURIComponent(group) + '/settings', 'PATCH', settings);
      await loadGroups();
      toast('群聊已加入，并已开启全部权限');
    }
  }
  whitelistInput.control.value = '';
}, 'btn primary')), whitelistChips);
whitelistBox.firstElementChild.classList.add('allowlist-form');
async function saveAllowlist(values) {
  try {
    await api('/operations/allowlist', 'PUT', {groups: values, revision: allowlistState.revision});
  } catch (error) {
    await refreshAllowlist();
    throw error;
  }
  await loadGroups();
  await refreshAllowlist();
  toast('群聊白名单已保存并立即生效');
}
async function refreshAllowlist() {
  whitelistPanel.hidden = principalRole !== 'owner';
  if (principalRole !== 'owner') return;
  const generation = operationsGeneration, data = await api('/operations/allowlist');
  if (!token || generation !== operationsGeneration) return;
  allowlistState = data;
  whitelistChips.replaceChildren();
  for (const group of data.groups) {
    const chip = n('div', undefined, 'allowlist-chip');
    chip.append(n('span', '群 ' + groupLabel(group)), btn('移除', async () => {
      if (confirm('移除群 ' + groupLabel(group) + '？机器人将停止在这个群收消息和发回复。'))
        await saveAllowlist(allowlistState.groups.filter(id => id !== group));
    }, 'btn small danger'));
    whitelistChips.append(chip);
  }
  if (!data.groups.length) whitelistChips.append(n('p', '尚未添加群聊，机器人不会响应任何群聊。', 'empty'));
}
errors.ALLOWLIST_REVISION_CONFLICT = '白名单已在其他页面变更，已刷新，请重试。';

const streamDay = field('日期'), streamSearch = field('查找会话');
streamDay.control.type = 'date';
streamDay.control.value = new Intl.DateTimeFormat('en-CA', {timeZone:'Asia/Shanghai'}).format(new Date());
streamSearch.control.placeholder = '输入群号、群名称或 QQ 号';
const streamLive = check('自动刷新', true), streamList = n('div', undefined, 'stream-list');
const streamTitle = n('strong', '选择一个会话'), streamHint = n('span', '', 'note');
const streamTimeline = n('div', undefined, 'stream-timeline');
streamTimeline.setAttribute('aria-live', 'polite');
const streamMore = btn('加载更早消息', () => refreshStreamEvents(true));
streamMore.hidden = true;
const streamLayout = n('div', undefined, 'stream-layout');
streamLayout.append(panel('会话', streamList), panel('处理时间线', stack(streamTitle, streamHint, streamMore, streamTimeline)));
streamSection.append(panel('聊天流', grid(streamDay, streamSearch, streamLive,
  btn('刷新', () => refreshStreams()))), streamLayout);
let streamSummaries = [];
function renderStreamList() {
  streamList.replaceChildren();
  for (const row of streamSummaries.filter(x => conversationLabel(x.conversation).includes(streamSearch.control.value.trim()))) {
    const button = btn('', async () => {selectedStream = row.conversation; renderStreamList(); await refreshStreamEvents();},
      'stream-choice' + (selectedStream === row.conversation ? ' selected' : ''));
    button.setAttribute('aria-pressed', String(selectedStream === row.conversation));
    button.append(n('strong', row.name ? '群聊 · '+row.name+' · '+row.conversation.slice(6) : conversationLabel(row.conversation)),
      n('span', row.count + ' 条消息 · ' + date(row.last_ts), 'note'));
    streamList.append(button);
  }
  if (!streamList.children.length) streamList.append(n('div', '当天没有匹配的会话。', 'empty'));
}
async function refreshStreams() {
  if (!streamDay.control.value) streamDay.control.value = new Intl.DateTimeFormat('en-CA', {timeZone:'Asia/Shanghai'}).format(new Date());
  const generation = operationsGeneration, day = streamDay.control.value;
  const data = await api('/operations/streams?day=' + encodeURIComponent(day));
  if (!token || generation !== operationsGeneration || day !== streamDay.control.value) return;
  streamSummaries = data;
  if (!data.some(row => row.conversation === selectedStream)) selectedStream = data[0]?.conversation || '';
  renderStreamList();
  await refreshStreamEvents();
}
const actionNames = {IGNORE:'不回复', WAIT:'等待', REPLY:'回复', THINK:'进入思考', SKIP:'本轮跳过', DEFER:'等待后重新判断', SILENT:'结束思考，不回复', WAIT_FOR_USER:'询问用户', TASK_DELEGATED:'等待工作任务', FAILED:'处理失败'};
const flowTools = {'memory.search':'查询记忆','memory.record':'记录记忆','media.download':'下载图片或媒体',
  'group.notice.read':'读取群公告','group.files.list':'查看群文件','group.file.download':'下载群文件',
  'work.submit':'启动 AI 工作空间','coding.submit':'布置 AI 工作任务','coding.recent':'查看已有工作任务','coding.status':'查看工作进度','coding.inspect':'查看工作结果',
  'coding.send_instruction':'追加工作指令','research.submit':'布置研究任务','research.status':'查看研究结果',
  'deepseek.consult':'咨询 DeepSeek 网页','deepseek.status':'查看 DeepSeek 结果','sticker.search':'查找表情包'};
let streamRows = new Map(), streamRenderKey = '', streamRequest = 0;
function flowTime(ts) {return new Date(ts*1000).toLocaleTimeString('zh-CN', {hour12:false});}
function flowDetail(callId) {
  const detail=n('details',undefined,'flow-call-detail'), content=n('div','展开查看模型回复。','model-call-content');
  detail.dataset.detailKey=callId;
  detail.append(n('summary','查看模型回复或工具结果'),content);
  let loaded=false;
  detail.ontoggle=guard(async()=>{
    if(!detail.open||loaded)return;
    streamLive.control.checked=false;
    const generation=operationsGeneration;
    content.textContent='正在读取…';
    try {
      const value=await api('/operations/logs/model-calls/'+encodeURIComponent(callId));
      if(!token||generation!==operationsGeneration)return;
      content.replaceChildren(...modelCallContents(value));loaded=true;
    } catch(error) {if(token&&generation===operationsGeneration)content.textContent=error.message;}
  });
  return detail;
}
function flowNodes(row) {
  const nodes=[{id:'message:'+row.id,ts:row.ts,kind:'message',title:row.type==='message.recalled'?'消息撤回':'接收到消息',
    summary:(row.actor_name?row.actor_name+'（'+row.actor+'）':'QQ '+row.actor)+' · '+(row.text||'[图片 / 语音 / 文件]'),source:row.id}];
  const stages=row.flow||[];
  const pairKey=e=>[e.kind.split('.')[0],e.session_id||'',e.step||''].join(':');
  // Pair each start with its own completion, including repeated DEFER decisions.
  const waiting=new Map(), matched=new Map();
  for(const e of stages){
    if(e.kind.endsWith('.start')){waiting.set(pairKey(e),e);}
    else if(e.kind.endsWith('.end')){const start=waiting.get(pairKey(e));if(start){matched.set(start.id,e);waiting.delete(pairKey(e));}}
  }
  for(const e of stages){
    if(e.kind.endsWith('.end'))continue;
    const end=matched.get(e.id), v=end||e;
    const pending=e.kind.endsWith('.start')&&!end&&row.status==='processing'&&Date.now()/1000-e.ts<180&&!stages.some(x=>x.ts>=e.ts&&['failed','interrupted'].includes(x.kind));
    let title='',summary='',kind=e.kind.split('.')[0];
    if(e.kind==='scheduled'){title='等待消息调度';summary=e.trigger==='direct'?'点名或直接请求，优先处理':'等待群友说完，合并同一轮消息';}
    else if(e.kind==='batch'){title='开始处理这一轮';summary='合并 '+e.message_count+' 条消息 · '+({direct:'直接请求',message_count:'达到消息数量',max_wait:'达到最长等待',idle_compensation:'空窗补偿',wait_timeout:'等待后重新判断',policy:'策略检查'}[e.trigger]||e.trigger||'');}
    else if(e.kind==='decision.start'){title=end?'决策 · '+(actionNames[v.action]||v.action):pending?'正在判断是否思考':'开始判断是否思考';summary=end?(v.reason_label||v.reason||'')+(v.wait_seconds?' · 等待 '+v.wait_seconds+' 秒':''):'';}
    else if(e.kind==='thinking.start'){
      title='第 '+e.step+' 轮思考'+(end?' · 已完成':pending?' · 进行中':' · 已开始');
      const retries=stages.filter(item=>item.kind==='model.retry'&&item.session_id===e.session_id&&item.step===e.step&&item.ts>=e.ts&&(!end||item.ts<=end.ts));
      summary='模型重试 '+retries.length+' 次';
    }
else if(e.kind==='tool.start'){title=(pending?'正在调用 · ':'调用工具 · ')+(flowTools[e.tool]||e.tool);summary=end?(v.outcome==='error'?'工具返回错误'+(v.error_type?' · '+({SCREENSHOT_TIMEOUT:'截图生成超时',SCREENSHOT_NAVIGATION_FAILED:'网页未能加载',SCREENSHOT_UNAVAILABLE:'截图服务无法连接',WRONG_LOOKUP_TOOL:'查询入口选择错误',PermissionError:'当前会话未授权',ValueError:'参数或数据无效',TimeoutError:'执行超时',TOOL_DENIED:'不支持的工具',POLICY_OR_SOURCE_REVOKED:'策略或消息授权已撤销'}[v.error_type]||v.error_type):''):'工具已返回')+(v.count?' · '+v.count+' 条结果':''):'';}
    else if(e.kind==='action'){title='思考决策 · '+(actionNames[e.action]||e.action);summary=e.attachment_count?'包含 '+e.attachment_count+' 个附件':'';}
    else if(e.kind==='reply.start'){title=end?'回复生成完成':pending?'正在生成回复':'开始生成回复';summary=v.attachment_count?'准备发送 '+v.attachment_count+' 个附件':'';}
    else if(e.kind==='send.start'){title=end?({sent:'已发送到 QQ',failed:'发送失败',cancelled:'发送已取消',unknown:'发送结果待确认'}[v.outcome]||'发送流程已结束'):pending?'正在发送到 QQ':'开始发送到 QQ';summary=end&&v.reason?(v.reason_label||v.reason):'';}
    else if(e.kind==='protocol.repair'){title='动作格式修正';summary='上一轮未执行动作，请求模型重新返回有效动作';}
    else if(e.kind==='search.reconsider'){title='补充搜索动作';summary='模型尚未搜索，要求先执行查询，再依据结果回答';}
    else if(e.kind==='mention.reconsider'){title='补充艾特动作';summary='需要先核实目标 QQ，再发送真实的群成员艾特';}
    else if(e.kind==='work.reconsider'){title='补充工作动作';summary='模型尚未创建任务，要求检查并使用工作工具';}
    else if(e.kind==='failed'&&e.error_type==='SearchNotExecuted'){title='检索未执行，回答已拦截';summary='模型未调用要求的查询工具，没有发送未经检索的结论';}
    else if(e.kind==='failed'&&e.error_type==='MentionNotExecuted'){title='艾特未执行，回答已拦截';summary='模型没有选择已核实的艾特对象，未发送口头代替操作的回复';}
    else if(e.kind==='resumed'){title='任务返回，继续思考';summary='AI 工作或研究结果已到达';}
    else if(e.kind==='failed'){title='处理失败';summary=({ThinkingLimitExceeded:'达到思考轮数或时间上限',TimeoutError:'请求超时',ValueError:'模型动作格式或参数无效',UpstreamUnavailable:'模型服务不可用'}[e.error_type]||e.error_type);}
    else if(e.kind==='interrupted'){title='处理已中断';summary='服务停止或任务被取消';}
    else continue;
    nodes.push({id:e.id,seq:e.seq,ts:e.ts,kind,title,summary,pending,source:row.id,model:v.model,task_id:v.task_id,
      duration_ms:v.duration_ms,call_id:v.call_id,error:v.outcome==='error'||kind==='failed'});
  }
  if(!stages.some(e=>e.kind==='action'||e.kind==='decision.end')&&row.decision)nodes.push({id:'decision:'+row.id,ts:row.decision.created_at||row.ts,kind:'decision',title:'处理决策 · '+(actionNames[row.decision.action]||row.decision.action),summary:row.decision.reason_label||row.decision.reason,source:row.id});
  if(row.batch_anchor_id)nodes.push({id:'batch-link:'+row.id,ts:row.ts+.001,kind:'batch',title:'已合并到同一轮处理',summary:'与本群其他消息共用一次决策，过程见同一时间线',source:row.id});
  if(row.error)nodes.push({id:'error:'+row.id,ts:stages.at(-1)?.ts||row.ts,kind:'failed',title:'消息处理失败',summary:row.error,error:true,source:row.id});
  for(const reply of row.replies|| (row.reply?[row.reply]:[]))nodes.push({id:'reply:'+(reply.id||row.id),ts:reply.sent_at||reply.created_at,kind:'message-reply',
    title:({sent:'回复已发送',pending:'回复等待发送',sending:'回复发送中',failed:'回复发送失败',unknown:'发送结果待确认',cancelled:'回复已取消'}[reply.status]||reply.status),
    summary:reply.text||'[附件回复]',model:reply.model,error:['failed','cancelled','unknown'].includes(reply.status),source:row.id,reason:reply.error});
  return nodes;
}
function renderFlowNode(row) {
  const node=n('article',undefined,'flow-node flow-kind-'+row.kind+(row.pending?' is-pending':'')+(row.error?' flow-error':''));
  node.dataset.flowId=row.id;
  const head=n('div',undefined,'flow-node-head');
  head.append(n('strong',row.title),n('time',flowTime(row.ts),'note'));
  node.append(head);
  if(row.summary)node.append(n('p',row.summary,'stream-message'));
  const meta=[row.model,row.duration_ms!==undefined?(row.duration_ms/1000).toFixed(2)+' 秒':null].filter(Boolean);
  if(meta.length)node.append(n('span',meta.join(' · '),'note'));
  if(row.reason)node.append(n('p','原因：'+row.reason,'note'));
  if(row.task_id)node.append(n('p','关联任务：'+row.task_id,'note'));
  if(row.call_id)node.append(flowDetail(row.call_id));
  const identity=n('details',undefined,'flow-identity');identity.append(n('summary','关联消息'),n('code',row.source));node.append(identity);
  return node;
}
function renderStreamTimeline() {
  const values=[...streamRows.values()].flatMap(flowNodes).sort((a,b)=>a.ts-b.ts||(a.seq||0)-(b.seq||0)||a.id.localeCompare(b.id));
  const existing=new Map([...streamTimeline.children].map(node=>[node.dataset.flowId,node]));
  const keep=new Set(values.map(row=>row.id));
  for(const node of [...streamTimeline.children])if(!keep.has(node.dataset.flowId))node.remove();
  for(const row of values){
    const signature=JSON.stringify(row);let node=existing.get(row.id);
    if(!node||node.dataset.signature!==signature){
      const replacement=renderFlowNode(row);replacement.dataset.signature=signature;
      if(node)node.replaceWith(replacement);node=replacement;
    }
    // Moving an unchanged node keeps expanded model details and focus intact.
    streamTimeline.append(node);
  }
  if(!values.length)streamTimeline.append(n('div','暂无消息。','empty'));
}
async function refreshStreamEvents(older = false) {
  if (!selectedStream) {streamTitle.textContent = '选择一个会话'; streamHint.textContent = ''; streamRows.clear();streamTimeline.replaceChildren(n('div','当天暂无聊天记录。','empty')); streamMore.hidden = true; return;}
  const generation = operationsGeneration, conversation = selectedStream, day = streamDay.control.value, request=++streamRequest;
  const key=conversation+'|'+day,params = new URLSearchParams({conversation, day});
  if (older && streamCursor) params.set('before', streamCursor);
  const data = await api('/operations/streams/events?' + params);
  if (!token || generation !== operationsGeneration || request!==streamRequest || conversation !== selectedStream || day !== streamDay.control.value) return;
  const same=streamRenderKey===key,atBottom=streamTimeline.scrollHeight-streamTimeline.scrollTop-streamTimeline.clientHeight<80;
  const oldHeight=streamTimeline.scrollHeight,oldTop=streamTimeline.scrollTop;
  if(!same||!older){streamRows.clear();}
  streamRenderKey=key;
  for(const row of data.items)streamRows.set(row.id,row);
  renderStreamTimeline();
  const groupName=streamSummaries.find(row=>row.conversation===conversation)?.name;
  streamTitle.textContent = groupName?'群聊 · '+groupName+' · '+conversation.slice(6):conversationLabel(conversation);
  streamHint.textContent = older ? '历史回看 · 从上到下按时间排列' : '从上到下按时间排列 · 最近更新 ' + new Date().toLocaleTimeString('zh-CN');
  if(older&&same)streamTimeline.scrollTop=oldTop+streamTimeline.scrollHeight-oldHeight;
  else if(!same||atBottom)streamTimeline.scrollTop=streamTimeline.scrollHeight;
  streamCursor = data.next; streamMore.hidden = !data.next;
}
streamDay.control.onchange = guard(async () => {selectedStream=''; await refreshStreams();});
streamSearch.control.oninput = renderStreamList;
streamLive.control.onchange = () => {streamHint.textContent = streamLive.control.checked ? '自动刷新已开启' : '已暂停自动刷新，可加载历史消息';};
streamMore.onclick = guard(async () => {streamLive.control.checked = false; await refreshStreamEvents(true);});

const logLevel = selectField('级别', [['','全部级别'],['INFO','信息'],['WARNING','警告'],['ERROR','错误'],['CRITICAL','严重']], '');
const logSource = field('来源'), logQuery = field('搜索日志'), logLive = check('自动刷新', true);
logSource.control.placeholder = '例如 模型调用、主服务'; logQuery.control.placeholder = '中文关键词 / 事件编号';
const logList = n('div', undefined, 'log-list'), logHint = n('p', '', 'note');
const logMore = btn('加载更早日志', async () => {logLive.control.checked=false; await refreshLogs(true);});
logMore.hidden = true;
logsSection.append(panel('运行日志', grid(logLevel, logSource, logQuery, logLive,
  btn('查询', () => refreshLogs()))), panel('日志记录', logHint, logList, logMore));
async function refreshLogs(older = false) {
  const generation = operationsGeneration;
  const params = new URLSearchParams({level:logLevel.control.value, source:logSource.control.value, q:logQuery.control.value});
  if (older && logCursor) params.set('before', logCursor);
  const key = [logLevel.control.value,logSource.control.value,logQuery.control.value].join('|');
  const data = await api('/operations/logs?' + params);
  if (!token || generation !== operationsGeneration || key !== [logLevel.control.value,logSource.control.value,logQuery.control.value].join('|')) return;
  if (!older) logList.replaceChildren();
  for (const row of data.items) {
    const item = n('article', undefined, 'log-entry');
    item.append(n('time', date(row.ts), 'note'),
      n('span', ({INFO:'信息',WARNING:'警告',ERROR:'错误',CRITICAL:'严重'})[row.level] || row.level, 'log-level '+row.level),
      n('span', row.source_label || row.source), n('pre', row.display_message || row.message));
    if (row.detail_id) {
      const detail=n('details'), content=n('div','展开后读取模型回复。','model-call-content');
      detail.append(n('summary','模型调用详情'),content);
      let loaded=false;
      detail.ontoggle=guard(async()=>{
        if(!detail.open||loaded)return;
        logLive.control.checked=false;
        content.textContent='正在读取模型调用详情…';
        const generation=operationsGeneration;
        try {
          const value=await api('/operations/logs/model-calls/'+encodeURIComponent(row.detail_id));
          if(!token||generation!==operationsGeneration)return;
          content.replaceChildren(...modelCallContents(value));loaded=true;
        } catch(error) {if(token&&generation===operationsGeneration)content.textContent=error.message;}
      });
      item.append(detail);
    }
    if (!row.detail_id && /decision_recorded.*action=IGNORE/.test(row.message)) item.append(n('p','本条消息最终未回复；是否调用过决策或思考模型，请查看同一事件的模型调用记录。','note'));
    const original = n('details');
    original.append(n('summary', '查看原始日志'), n('code', row.source), n('pre', row.message));
    original.style.gridColumn = '1 / -1';
    item.append(original);
    logList.append(item);
  }
  if (!logList.children.length) logList.append(n('div', '没有匹配的日志。', 'empty'));
  logHint.textContent = data.retention;
  logCursor = data.next; logMore.hidden = !data.next;
}
function modelResponseText(value) {
  const raw=value.response||'';
  let data;
  try {data=JSON.parse(raw);} catch {return raw;}
  if(!data||typeof data!=='object')return raw;
  if(data.tool_call){
    const name=data.tool_call.name||'',args=data.tool_call.arguments||{};
    const title=flowTools[name]||name;
    const task=args.goal||args.requirement||args.question||args.query||args.command||'';
    if(name==='work.submit'||name==='coding.submit')return '调用工作模式，任务：'+(task||'未提供任务说明');
    if(name==='memory.search')return '主动查询记忆，关键词：'+(task||'未提供');
    return '调用'+title+(task?'，内容：'+task:'');
  }
  if(data.final_action){
    const reply=data.reply_brief?.reply_text||data.reply_brief?.key_points?.join('；');
    return reply||({SILENT:'决定保持沉默',TASK_DELEGATED:'已委托任务，等待结果',WAIT_FOR_USER:'等待用户补充信息',FAILED:'本轮处理失败'}[data.final_action]||'本轮思考已完成');
  }
  if(data.action)return '判断结果：'+(actionNames[data.action]||data.action);
  return raw;
}
function modelCallContents(value) {
  const meta=n('div',undefined,'model-call-meta');
  if(value.purpose==='tool'){
    meta.append(n('span','工具：'+(flowTools[value.tool_name]||value.tool_name)),n('span','状态：'+({requesting:'调用中',completed:'已返回',failed:'失败'}[value.status]||value.status)));
    const result=n('section');result.append(n('h4','工具返回结果'),n('pre',value.response||'尚未返回'));
    return [meta,result];
  }
  const purposes={chat:'聊天回复',reply:'回复生成',decision:'回复判断',thinking:'思考',memory:'记忆处理',work:'AI 工作',observer:'代码审查'};
  for(const text of ['服务商：'+(value.provider_name||value.provider||'未记录'), '模型：'+(value.model||'未记录'),
    '用途：'+(purposes[value.purpose]||value.purpose||'未记录'), '响应方式：'+(value.stream?'流式':'非流式'),
    '状态：'+({requesting:'请求中',completed:'已完成',failed:'失败'}[value.status]||value.status)])meta.append(n('span',text));
  meta.append(n('span','首字延迟：'+(value.first_response_ms===undefined?'未记录':value.first_response_ms+' 毫秒')),
    n('span','生成速度：'+(value.output_tokens_per_second===undefined?'未记录':(value.speed_estimated?'约 ':'')+value.output_tokens_per_second+' Token/秒')),
    n('span','重试：'+(value.retry_count||0)+' 次'));
  const result=[meta];
  if(value.event_id)result.push(n('p','关联事件：'+value.event_id,'note'));
  if(value.task_id)result.push(n('p','关联任务：'+value.task_id,'note'));
  const response=n('section');response.append(n('h4','模型回复'));
  if(value.response!==undefined)response.append(n('pre',modelResponseText(value)));
  else response.append(n('p',value.status==='failed'?'请求失败，未收到可用回复。':'请求尚未完成。','note'));
  if(value.error_type)response.append(n('p','错误类型：'+value.error_type+(value.http_status?' · HTTP '+value.http_status:''),'flow-error'));
  const failureLabels={MODEL_EMPTY_RESPONSE:'模型没有返回可用正文',MODEL_REASONING_WITHOUT_ANSWER:'模型只返回推理数据，没有最终回答',MODEL_OUTPUT_TRUNCATED:'模型输出达到上限，被截断',MODEL_STREAM_ERROR:'流式响应中返回了服务商错误'};
  if(value.error_code)response.append(n('p',failureLabels[value.error_code]||value.error_code,'flow-error'));
  const diagnostics=value.response_diagnostics;
  if(diagnostics&&Object.keys(diagnostics).length)response.append(n('p','响应片段：'+(diagnostics.chunks||0)+' · 结束原因：'+(diagnostics.finish_reason||'未提供')+' · '+(diagnostics.done?'收到流结束标记':'未收到流结束标记'),'note'));
  if(value.input_tokens!==undefined)response.append(n('p','输入 Token：'+value.input_tokens+' · 输出 Token：'+value.output_tokens,'note'));
  result.push(response);
  return result;
}
errors.MODEL_CALL_DETAIL_EXPIRED='此调用的详情已轮转清理，或早于详情记录功能上线。';
logLevel.control.onchange = guard(() => refreshLogs());
const operationsOriginalLoad = fullLoad;
fullLoad = async function(id) {
  await operationsOriginalLoad(id);
  if (id === 'streams') await refreshStreams();
  if (id === 'logs') await refreshLogs();
};
const operationsOriginalGroups = loadGroups;
loadGroups = async function() {await operationsOriginalGroups(); await refreshAllowlist();};
const operationsOriginalLogout = logout;
logout = function(silent=false) {
  operationsGeneration++;
  operationsOriginalLogout(silent);
  allowlistState=null; selectedStream=''; streamCursor=logCursor=null; streamSummaries=[];streamRows.clear();streamRenderKey='';streamRequest++;
  for (const node of [whitelistChips,streamList,streamTimeline,logList]) node.replaceChildren();
  streamTitle.textContent='选择一个会话'; streamHint.textContent=logHint.textContent='';
};
setInterval(async () => {
  if (!token || principalRole !== 'owner' || document.hidden || operationsBusy) return;
  if (!(view === 'streams' && streamLive.control.checked || view === 'logs' && logLive.control.checked)) return;
  operationsBusy=true;
  try {if (view==='streams') await refreshStreams(); else await refreshLogs();}
  catch (error) {if(token) toast(error.message);}
  finally {operationsBusy=false;}
}, 4000);
