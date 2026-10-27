/* Reference NekoBot UI connected to QQAIBOT's real APIs. */
'use strict';
const $=id=>document.getElementById(id);
let principalRole='owner';
let loginPending=false;
let token='',groups=[],cursor=null,view='overview',timer,runtimeConfig=null,activeVendorId='';
const taskLabels={work:['💻 AI 工作','在独立工作空间读取、写入、运行程序与浏览网页'],observer:['🔎 代码审查','独立选择只读审查模型；未配置时保留原审查方式'],planner:['🧠 思考与回复','判断是否行动、调用工具并直接生成最终回复'],memory:['📚 记忆处理','提取与检索'],emoji:['🎨 表情理解','理解与选择表情'],vlm:['🖼️ 图片理解','识别图片内容'],video:['🎬 直接视频识别','完整视频通过 OpenAI video_url 输入模型，不在本地抽关键帧；需 API 和模型支持视频输入']};
const visibleTaskEntries=config=>Object.entries(config.task_configs).filter(([name])=>Object.hasOwn(taskLabels,name));
const strategies={sequential:'🛡️ 主备容灾切换',balance:'⚖️ 按负载均衡',random:'🔀 随机分配'};
const labels={overview:['📊 总览看板','查看机器人实际运行状态、消息与模型用途。'],models:['🧠 模型配置中心','API 厂商 → 模型池 → 功能用途，逐步完成机器人的模型配置。'],persona:['🎭 灵魂人格设定','配置稳定身份、性格、回复风格和受保护称呼。'],groups:['💬 QQ 群管与风控','配置实际授权群的响应与数据权限。'],sandbox:['🧪 对话回放','用已有消息检查回复与决策，回放结果不会发送到 QQ。'],messages:['💬 消息与评价','查看回复、评价与真实交付状态。'],memories:['📚 记忆管理','查询和删除指定用户的记忆。'],resources:['📁 群资料','读取授权群的公告和文件元数据。'],extensions:['🧩 扩展能力','查看当前能力的实际状态。'],system:['⚙️ 系统设置','查看运行配置，暂停或恢复机器人。'],prompts:['📝 提示词系统','编辑已迁入的模板和使用阶段。'],filters:['🛡️ 输出过滤','保护人设与称呼，过滤推理和禁用内容。']};
function n(tag,text,cls){const el=document.createElement(tag);if(text!==undefined)el.textContent=String(text);if(cls)el.className=cls;return el}
function toast(message){$('toast').textContent=message;$('toast').classList.remove('hidden');$('toast').classList.add('show');clearTimeout(timer);timer=setTimeout(()=>{$('toast').classList.remove('show');$('toast').classList.add('hidden')},3600)}
const showToast=toast;
function guard(action){return async event=>{const b=event?.currentTarget;try{if(b?.tagName==='BUTTON')b.disabled=true;await action(event)}catch(e){toast(e.message)}finally{if(b?.isConnected)b.disabled=false}}}
const errors={CONFIG_REVISION_CONFLICT:'配置已在其他页面更新，请刷新后重试。',PROVIDER_IN_USE:'厂商仍有关联模型，请先移除模型。',MODEL_IN_USE:'模型已分配给功能，请先在模型用途中移除。',MODEL_IDENTIFIER_EXISTS:'这个实际请求模型 ID 已存在，请编辑已有模型，不能覆盖其他模型。',MODEL_NOT_FOUND:'原模型已不存在，请刷新配置后重试。',PROMPT_IN_USE:'模板正在人设中使用，请先取消绑定。',INVALID_INPUT:'请检查填写内容和数值范围。',INVALID_CONFIGURATION:'配置无效，请检查输入。',FORBIDDEN:'当前账号没有管理权限。'};
async function api(path,method='GET',body){if(!token)throw Error('请先登录控制台');const r=await fetch('/api/v1'+path,{method,headers:{Authorization:'Bearer '+token,'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body)});const v=await r.json();if(!r.ok){if(r.status===401){if(!loginPending){logout(true);showLoginError('登录已失效，请重新登录。')}throw Object.assign(Error('管理密钥无效或已失效'),{status:401})}throw Error(errors[v.error?.code]||v.error?.code||'请求失败')}return v.data}
function btn(text,action,cls='btn small'){const el=n('button',text,cls);el.type='button';el.onclick=guard(action);return el}
function badge(v){return n('span',({done:'已处理',processing:'处理中',active:'启用',disabled:'停用',sent:'已发送',pending:'待处理',sending:'发送中',failed:'失败',succeeded:'已完成',unknown:'结果未知',cancelled:'已取消'})[v]||v,'badge '+v)}
function date(v){return v?new Date(v*1000).toLocaleString('zh-CN',{hour12:false}):'未提供'}
function empty(id,text){const tr=n('tr'),td=n('td');td.colSpan=8;td.append(n('div',text,'empty'));tr.append(td);$(id).replaceChildren(tr)}
function modal(title,body,buttons=[]){$('modal').classList.toggle('route-settings-modal',body.classList.contains('route-settings'));$('modalTitle').textContent=title;$('modalBody').replaceChildren(body);$('modalFooter').replaceChildren(...buttons);if(!$('modal').open)$('modal').showModal()}
function json(title,v){modal(title,n('pre',JSON.stringify(v,null,2)))}
function field(label,value='',kind='input'){const wrap=n('label',undefined,'config-field'),control=n(kind);control.value=value??'';wrap.append(n('span',label,'field-label'),control);return{wrap,control}}
function check(label,value=false){const wrap=n('label',undefined,'check-field'),control=n('input');control.type='checkbox';control.checked=value;wrap.append(control,n('span',label));return{wrap,control}}
function selectField(label,items,value){const f=field(label,'','select');for(const[id,text]of items){const o=n('option',text);o.value=id;f.control.append(o)}f.control.value=value;return f}
function grid(...items){const el=n('div',undefined,'config-grid');el.append(...items.map(x=>x.wrap||x));return el}
function stack(...items){const el=n('div',undefined,'stack');el.append(...items.map(x=>x.wrap||x));return el}
function details(title,...items){const el=n('details',undefined,'anime-subcard rounded-xl p-4');el.append(n('summary',title),grid(...items));return el}
function numeric(label,value,min,max){const f=field(label,value);Object.assign(f.control,{type:'number',min,max});return f}
function required(v,message){if(!v.trim())throw Error(message);return v.trim()}
function uuid(){if(crypto.randomUUID)return crypto.randomUUID();return Array.from(crypto.getRandomValues(new Uint8Array(16)),v=>v.toString(16).padStart(2,'0')).join('')}
function number(f){if(f.control.value===''||!f.control.checkValidity())throw Error('请检查「'+f.wrap.textContent+'」的数值范围');return Number(f.control.value)}
function ensureConfig(){if(!runtimeConfig)throw Error('请先连接管理员控制台');return runtimeConfig}
async function loadConfig(){runtimeConfig=await api('/config');if(!runtimeConfig.providers.some(p=>p.id===activeVendorId))activeVendorId=runtimeConfig.providers[0]?.id||'';renderConfig();return runtimeConfig}
async function mutate(path,method,body,revision=ensureConfig().revision){await api(path+(method==='DELETE'?'?revision='+revision:''),method,method==='DELETE'?undefined:{...body,revision});await loadConfig();toast('已保存并立即生效')}
function switchTab(id){navigate(id)}
const viewScrollPositions=new Map();
function navigate(id,refreshData=true){if(!labels[id])return;const main=document.querySelector('body > main'),changed=id!==view;if(changed)viewScrollPositions.set(view,{main:main.scrollTop,window:window.scrollY});view=id;document.querySelectorAll('main > section').forEach(s=>{const active=s.id===id||s.id==='sec-'+id;s.classList.toggle('hidden',!active);s.classList.toggle('visible',active)});document.querySelectorAll('[data-view]').forEach(b=>{const active=b.dataset.view===id;b.classList.toggle('active',active);b.setAttribute('aria-current',active?'page':'false')});$('page-title').textContent=labels[id][0];$('page-desc').textContent=labels[id][1];const restore=()=>{if(changed&&view===id){const pos=viewScrollPositions.get(id)||{main:0,window:0};main.scrollTo({top:pos.main,behavior:'instant'});window.scrollTo({top:pos.window,behavior:'instant'});}};restore();if(token&&refreshData)refreshConsole().then(restore).catch(e=>toast(e.message))}
function switchModelSubTab(id){for(const key of ['vendors','pool','routing']){$('model-view-'+key).classList.toggle('hidden',id!==key);$('subtab-'+key).classList.toggle('active',id===key);$('subtab-'+key).setAttribute('aria-selected',String(id===key))}}
function selectVendor(id){activeVendorId=id;renderProviders();renderModels()}
function renderConfig(){renderProviders();renderModels();renderRoutes();$('active-persona').textContent=runtimeConfig.persona.name;$('stat-vendor-count').textContent=runtimeConfig.providers.length+' 家';$('stat-model-count').textContent=runtimeConfig.models.length;renderPersona();renderPrompts();renderFilters()}
function renderProviders(){
  const c=ensureConfig();$('tab-badge-vendors').textContent=c.providers.length+' 厂商';
  const box=$('vendor-list');box.replaceChildren();box.setAttribute('aria-label','API 厂商列表');
  for(const p of c.providers){
    const selected=p.id===activeVendorId;
    const card=n('article',undefined,'vendor-item anime-subcard p-4 rounded-2xl stack'+(selected?' active':''));
    card.dataset.providerId=p.id;card.tabIndex=0;
    card.setAttribute('role','group');card.setAttribute('aria-label',p.name+(selected?'，已选择':'，点击选择'));
    card.setAttribute('aria-current',String(selected));
    card.onclick=event=>{if(!event.target.closest('button'))selectVendor(p.id);};
    card.onkeydown=event=>{
      if(event.target===card&&(event.key==='Enter'||event.key===' ')){
        event.preventDefault();selectVendor(p.id);
        [...box.children].find(x=>x.dataset.providerId===p.id)?.focus();
      }
    };
    const head=n('div',undefined,'section-heading'),actions=n('div',undefined,'toolbar');
    head.append(n('strong',p.name),badge(p.enabled?'active':'disabled'));
    actions.append(btn('管理模型 →',()=>{selectVendor(p.id);switchModelSubTab('pool')}),btn('编辑',()=>providerDialog(p)),
      btn('连接测试',async()=>json('厂商连接测试',await api('/config/providers/'+encodeURIComponent(p.id)+'/test','POST'))),
      btn('删除',async()=>{if(confirm('删除「'+p.name+'」？关联模型需先移除。'))await mutate('/config/providers/'+encodeURIComponent(p.id),'DELETE')},'btn small danger'));
    card.append(head,n('div',p.base_url,'mono'),n('div',p.has_api_key?'密钥已保存':'未设置密钥','note'),actions);box.append(card);
  }
  if(!c.providers.length)box.append(n('div','先在上方添加第一个 API 厂商。','empty'));
}
async function addVendor(){ensureConfig();const name=required($('new-vendor-name').value,'请输入厂商名称'),base_url=required($('new-vendor-url').value,'请输入 API Base URL');if(!/^https?:\/\//.test(base_url))throw Error('API 地址需要以 http:// 或 https:// 开头');const id='provider_'+uuid().slice(0,8);await mutate('/config/providers/'+id,'PUT',{name,base_url,api_key:$('new-vendor-key').value});activeVendorId=id;for(const id of ['new-vendor-name','new-vendor-url','new-vendor-key'])$(id).value='';renderProviders();renderModels()}
function providerDialog(p){const c=ensureConfig(),name=field('厂商名称',p.name),url=field('API Base URL',p.base_url),key=field('新密钥（留空保留原密钥）'),enabled=check('启用厂商',p.enabled),auth=selectField('认证方式',[['bearer','Bearer'],['api_key','API Key Header'],['custom','自定义 Header'],['none','无 Header 认证']],p.auth_type),header=field('认证 Header 名',p.auth_header_name),prefix=field('认证值前缀',p.auth_header_prefix),query=field('密钥查询参数名',p.auth_query_name),retry=numeric('失败重试次数',p.max_retry,0,8),timeout=numeric('接口超时（秒）',p.timeout,1,120),endpoint=field('模型列表路径',p.model_list_endpoint);key.control.type='password';modal('编辑 API 厂商',stack(grid(name,url,key,enabled),details('高级设置',auth,header,prefix,query,retry,timeout,endpoint)),[btn('保存',async()=>{const{id,has_api_key,...data}=p;await mutate('/config/providers/'+encodeURIComponent(id),'PUT',{...data,name:required(name.control.value,'请输入厂商名称'),base_url:url.control.value.trim(),api_key:key.control.value||null,enabled:enabled.control.checked,auth_type:auth.control.value,auth_header_name:header.control.value,auth_header_prefix:prefix.control.value,auth_query_name:query.control.value,max_retry:number(retry),timeout:number(timeout),model_list_endpoint:endpoint.control.value},c.revision);$('modal').close()},'btn primary')])}
function renderModels(){const c=ensureConfig(),p=c.providers.find(x=>x.id===activeVendorId);$('tab-badge-models').textContent=c.models.length+' 模型';$('current-vendor-label').textContent=p?.name||'请选择厂商';const pills=$('pool-vendor-pills');pills.replaceChildren();for(const item of c.providers){const count=c.models.filter(m=>m.api_provider===item.id).length;pills.append(btn('🏢 '+item.name+' ('+count+')',()=>selectVendor(item.id),'btn'+(item.id===activeVendorId?' selected':'')))}const box=$('model-list');box.replaceChildren();const models=c.models.filter(m=>m.api_provider===activeVendorId);$('pool-count-hint').textContent=(p?.name||'未选择厂商')+' · '+models.length+' 个模型';for(const m of models){const card=n('article',undefined,'anime-subcard p-4 rounded-2xl stack'),top=n('div',undefined,'section-heading'),actions=n('div',undefined,'toolbar');top.append(n('strong',m.name),badge(m.enabled?'active':'disabled'));const usages=visibleTaskEntries(c).filter(([,t])=>t.model_list.includes(m.model_identifier)).map(([id])=>taskLabels[id]?.[0]||id);actions.append(btn('模型设置',()=>modelDialog(m)),btn('移除',async()=>{if(confirm('从模型池中移除「'+m.name+'」？'))await mutate('/config/models/'+encodeURIComponent(m.model_identifier),'DELETE')},'btn small danger'));card.append(top,n('div',m.model_identifier,'mono'),n('div',m.force_stream_mode?'流式响应':'非流式响应','note'),n('div','失败重试：'+(m.max_retry??p?.max_retry??2)+' 次','note'),n('div',(m.capabilities|| (m.visual?['image']:['chat'])).map(x=>({chat:'💬 对话',video:'🎬 视频',image:'🖼️ 图片',embedding:'🔗 向量'}[x]||x)).join(' · '),'note'),n('div',usages.length?'服务于：'+usages.join(' / '):'暂未分配给任何功能','note'),actions);box.append(card)}if(!models.length)box.append(n('div',p?'这个厂商还没有模型，可手动添加或从接口拉取。':'请先添加 API 厂商。','empty'))}
async function addModelToVendor(){const c=ensureConfig();if(!activeVendorId)throw Error('请先选择 API 厂商');const id=required($('new-model-id').value,'请输入模型 ID');if(c.models.some(m=>m.model_identifier===id))throw Error('这个模型 ID 已存在，请编辑已有模型。');await mutate('/config/models/'+encodeURIComponent(id),'PUT',{model_identifier:id,name:$('new-model-alias').value.trim()||id,api_provider:activeVendorId,capabilities:[...document.querySelectorAll('#new-model-capabilities input:checked')].map(x=>x.value),visual:[...document.querySelectorAll('#new-model-capabilities input:checked')].some(x=>x.value==='image'),force_stream_mode:$('new-model-mode').value==='stream',max_retry:2});$('new-model-id').value='';$('new-model-alias').value=''}
function capabilityField(label, selected=[]) {
  const wrap=n('div',undefined,'config-field capability-field'), options=n('div',undefined,'capability-options');
  const labels={chat:'💬 对话',video:'🎬 视频',image:'🖼️ 图片',embedding:'🔗 向量'};
  const controls=[];
  wrap.append(n('span',label,'field-label'));
  for(const [id,text] of Object.entries(labels)){const item=check(text,selected.includes(id));item.control.value=id;controls.push(item.control);options.append(item.wrap)}
  wrap.append(options);return {wrap,controls};
}
function modelDialog(m, returnToTask=null) {
  const c=ensureConfig(), name=field('显示名称',m.name),identifier=field('实际请求模型 ID',m.model_identifier);
  identifier.control.dataset.modelIdentifier='true';
  const mode=selectField('响应方式', [['normal','非流式（等待完整回答）'],['stream','流式（逐步接收回答）']], m.force_stream_mode?'stream':'normal');
  mode.control.dataset.modelStream='true';
  const caps=capabilityField('模型能力（可多选）',m.capabilities|| (m.visual?['image']:['chat'])),enabled=check('启用模型',m.enabled);
  const retry=numeric('失败重试次数（不含首次请求）',m.max_retry??c.providers.find(p=>p.id===m.api_provider)?.max_retry??2,0,20);
  const temp=field('温度（留空使用默认值）',m.temperature),tokens=field('最大输出 Token（留空使用默认值）',m.max_tokens),contextWindow=field('上下文窗口 Token',m.context_window_tokens??32768);
  const extra=field('接口额外参数 JSON',JSON.stringify(m.extra_params||{},null,2),'textarea');
  temp.control.type=tokens.control.type=contextWindow.control.type='number';
  const cancel=btn('取消',()=>{if(returnToTask)returnToTask();else $('modal').close();});
  modal('模型设置 · '+m.model_identifier, stack(grid(name,identifier,mode,retry),
    n('p','显示名称仅用于界面；实际请求模型 ID 会发送给 API。修改后，已绑定该模型的所有功能同步更新，无需重新添加。流式设置同样统一生效。','note'),
    n('p','失败后按 1、2、4、8、16 秒等待；之后每次最多等待 16 秒。不可重试的错误会立即停止，仍受本轮总时限约束。','note'),
    caps,details('高级设置（通常无需修改）',enabled,temp,tokens,contextWindow,n('p','填写模型实际支持的上下文上限；请求会预留输出空间并按 Token 估算裁剪旧消息。','note'),extra)),[cancel,btn('保存',async()=>{
      const nextIdentifier=required(identifier.control.value,'请输入实际请求模型 ID');
      await mutate('/config/models/'+encodeURIComponent(m.model_identifier),'PUT',{...m,model_identifier:nextIdentifier,name:required(name.control.value,'请输入显示名称'),
        capabilities:caps.controls.filter(x=>x.checked).map(x=>x.value),visual:caps.controls.some(x=>x.checked&&x.value==='image'),enabled:enabled.control.checked,force_stream_mode:mode.control.value==='stream',
        temperature:temp.control.value===''?null:Number(temp.control.value),max_tokens:tokens.control.value===''?null:Number(tokens.control.value),context_window_tokens:Number(contextWindow.control.value),max_retry:number(retry),extra_params:JSON.parse(extra.control.value)},c.revision);
      if(returnToTask)returnToTask(m.model_identifier,nextIdentifier);else $('modal').close();
    },'btn primary')]);
}
async function QuickFetchModels(){const c=ensureConfig(),providerId=activeVendorId;if(!providerId)throw Error('请先选择 API 厂商');const list=await api('/config/providers/'+encodeURIComponent(providerId)+'/models'),box=n('div',undefined,'stack'),choices=[];box.append(n('p','选择要加入模型池的模型。'));for(const id of list){const item=check(id,c.models.some(m=>m.model_identifier===id));item.control.disabled=c.models.some(m=>m.model_identifier===id);choices.push([id,item]);box.append(item.wrap)}if(!list.length)box.append(n('p','接口没有返回模型。'));modal('从厂商接口获取模型',box,[btn('加入选中模型',async()=>{const next=structuredClone(runtimeConfig);for(const[id,item]of choices)if(item.control.checked&&!item.control.disabled&&!next.models.some(m=>m.model_identifier===id))next.models.push({model_identifier:id,name:id,api_provider:providerId,force_stream_mode:$('new-model-mode').value==='stream',capabilities:[...document.querySelectorAll('#new-model-capabilities input:checked')].map(x=>x.value),max_retry:2});await api('/config','PUT',{configuration:next,revision:next.revision});await loadConfig();$('modal').close();toast('选中模型已加入模型池')},'btn primary')])}
function renderRoutes() {
  const config = ensureConfig();
  const cards = $('function-routing-list');
  const overview = $('overview-routing-grid');
  cards.replaceChildren();
  overview.replaceChildren();
  $('tab-badge-functions').textContent = visibleTaskEntries(config).length + ' 功能';

  for (const [name, task] of visibleTaskEntries(config)) {
    const label = taskLabels[name] || ['⚙️ ' + name, '模型用途'];
    const title = label[0].replace(/^\S+\s+/, '');
    const icon = label[0].split(' ')[0];
    const modelNames = task.model_list.map(id => config.models.find(m => m.model_identifier === id)?.name || id);
    const activeModels = task.model_list.filter(id => {
      const model = config.models.find(m => m.model_identifier === id);
      return model?.enabled && config.providers.some(p => p.id === model.api_provider && p.enabled);
    });
    const card = btn('', () => taskSettingsDialog(name), 'task-preview anime-subcard');
    card.id = 'task-preview-' + name;
    card.dataset.task = name;
    card.setAttribute('aria-haspopup', 'dialog');
    card.setAttribute('aria-label', '配置' + title + '的模型');

    const head = n('div', undefined, 'task-preview-head');
    const identity = n('div', undefined, 'task-preview-identity');
    const text = n('div');
    text.append(n('strong', title), n('span', label[1], 'note'));
    identity.append(n('span', icon, 'task-preview-icon'), text);
    const status = badge(!modelNames.length ? '未配置' : activeModels.length ? '已配置' : '模型已停用');
    status.classList.add(activeModels.length ? 'task-configured' : 'task-unconfigured');
    head.append(identity, status);

    const models = n('div', undefined, 'task-preview-models');
    modelNames.slice(0, 2).forEach((modelName, index) => {
      const chip = n('span', modelName, 'task-model-chip');
      chip.title = (task.selection_strategy === 'sequential' ? (index === 0 ? '主模型：' : '备用模型：') : '') + modelName;
      models.append(chip);
    });
    if (modelNames.length > 2) {
      const more = n('span', '+' + (modelNames.length - 2), 'task-model-more');
      more.title = modelNames.slice(2).join('、');
      models.append(more);
    }
    if (!modelNames.length) models.append(n('span', '点击选择这个功能使用的模型', 'note'));

    const footer = n('div', undefined, 'task-preview-footer');
    footer.append(n('span', strategies[task.selection_strategy], 'task-strategy'), n('span', '设置 ↗', 'task-preview-action'));
    card.append(head, models, n('div', modelNames.length + ' 个模型 · 点击配置', 'task-preview-meta'), footer);
    cards.append(card);

    const summary = n('div', undefined, 'anime-subcard p-4 rounded-xl stack');
    summary.append(n('strong', label[0], 'text-xs'), n('div', modelNames.join(' → ') || '尚未配置', 'text-xs text-pink-500'), n('span', strategies[task.selection_strategy] + ' · ' + task.hard_timeout + 's', 'note'));
    overview.append(summary);
  }
}

function taskSettingsDialog(name, draft=null) {
  const config = ensureConfig();
  const task = draft || config.task_configs[name];
  if (!task) throw Error('这个模型用途不存在，请刷新后重试。');
  const label = taskLabels[name] || [name, '模型用途'];
  const selected = [...task.model_list];
  const strategy = selectField('调度策略', Object.entries(strategies), task.selection_strategy);
  const tokens = numeric('最大 Token', task.max_tokens, 1, 128000);
  const temperature = numeric('温度', task.temperature, 0, 2);
  const timeout = numeric('硬超时（秒）', task.hard_timeout, 1, 180);
  const slow = numeric('慢速阈值（秒）', task.slow_threshold, 0, 120);
  temperature.control.step = timeout.control.step = slow.control.step = '0.1';

  const available = config.models.filter(m => m.enabled && config.providers.some(p => p.id === m.api_provider && p.enabled));
  const picker = selectField('从模型池添加', [['', '选择模型'], ...available.map(m => [m.model_identifier, m.name])], '');
  const list = n('div', undefined, 'task-model-list');
  const hint = n('p', undefined, 'task-order-hint');
  const add = btn('+ 添加模型', () => {
    const id = picker.control.value;
    if (id && !selected.includes(id)) selected.push(id);
    draw();
  });
  const labels = {
    sequential: '按顺序调用模型，失败后尝试下一项。可用箭头调整主备优先级。',
    balance: '根据调用量、延迟和失败情况选择模型，失败后尝试其他候选。',
    random: '随机选择候选模型，失败后继续尝试其他模型。'
  };

  function draw() {
    hint.textContent = labels[strategy.control.value];
    list.replaceChildren();
    selected.forEach((id, index) => {
      const model = config.models.find(m => m.model_identifier === id);
      const provider = config.providers.find(p => p.id === model?.api_provider);
      const row = n('div', undefined, 'route-node');
      const identity = n('div', undefined, 'route-model-identity');
      identity.append(n('strong', model?.name || id), n('span', (provider?.name || '未知厂商') + ' · ' + (model?.model_identifier || id), 'mono'));
      if (model) identity.append(n('span', model.force_stream_mode ? '流式响应' : '非流式响应', 'note'));
      if (!model?.enabled || !provider?.enabled) identity.append(n('span', '已停用', 'note'));
      const position = strategy.control.value === 'sequential' ? (index === 0 ? '主模型' : '备用 ' + index) : String(index + 1).padStart(2, '0');
      const actions = n('div', undefined, 'toolbar');
      const up = btn('↑', () => { [selected[index - 1], selected[index]] = [selected[index], selected[index - 1]]; draw(); });
      up.disabled = index === 0;
      up.setAttribute('aria-label', '上移 ' + (model?.name || id));
      const down = btn('↓', () => { [selected[index + 1], selected[index]] = [selected[index], selected[index + 1]]; draw(); });
      down.disabled = index === selected.length - 1;
      down.setAttribute('aria-label', '下移 ' + (model?.name || id));
      const remove = btn('移除', () => { selected.splice(index, 1); draw(); }, 'btn small danger');
      remove.setAttribute('aria-label', '移除 ' + (model?.name || id));
      actions.append(up, down);
      if (model) actions.append(btn('模型设置', () => modelDialog(model, (oldId,newId) => {
        const draft=draftTask();
        if(newId)draft.model_list=draft.model_list.map(value=>value===oldId?newId:value);
        taskSettingsDialog(name,draft);
      })));
      actions.append(remove);
      row.append(n('span', position, 'route-number'), identity, actions);
      list.append(row);
    });
    if (!selected.length) list.append(n('div', '尚未绑定模型，从下方的模型池中添加。', 'task-model-empty'));
    for (const option of picker.control.querySelectorAll('option')) option.disabled = !!option.value && selected.includes(option.value);
    const next = available.find(m => !selected.includes(m.model_identifier));
    picker.control.value = next?.model_identifier || '';
    picker.control.disabled = !next;
    add.disabled = !next;
  }

  strategy.control.onchange = draw;
  picker.control.onchange = () => { add.disabled = !picker.control.value; };
  draw();
  const addRow = n('div', undefined, 'task-model-picker');
  addRow.append(picker.wrap, add);
  function draftTask() {return {...task,model_list:[...selected],selection_strategy:strategy.control.value,max_tokens:number(tokens),temperature:number(temperature),hard_timeout:number(timeout),slow_threshold:number(slow)};}
  const body = stack(n('p', '选择一个主模型即可使用；需要时再添加备用模型。', 'note'), list, addRow,
    details('高级设置（调度、生成参数与超时）', strategy, hint, tokens, temperature, timeout, slow));
  body.classList.add('route-settings');
  const cancel = btn('取消', () => $('modal').close());
  const save = btn('保存配置', async () => {
    await mutate('/config/tasks/' + name, 'PUT', {
      ...task, model_list: selected, selection_strategy: strategy.control.value,
      max_tokens: number(tokens), temperature: number(temperature),
      hard_timeout: number(timeout), slow_threshold: number(slow)
    }, config.revision);
    $('modal').close();
    $('task-preview-' + name)?.focus();
  }, 'btn primary');
  modal(label[0] + ' · 模型配置', body, [cancel, save]);
}

function replaceTaskModelsDialog() {
  const config = ensureConfig();
  const used = config.models.filter(model => Object.values(config.task_configs).some(task => task.model_list.includes(model.model_identifier)));
  if (!used.length) throw Error('还没有功能绑定模型。');
  if (config.models.length < 2) throw Error('请先在模型池中添加替换后的目标模型。');
  const label = model => (model.name || model.model_identifier) + ' · ' + model.model_identifier + ' · ' + (config.providers.find(provider => provider.id === model.api_provider)?.name || model.api_provider);
  const source = selectField('要替换的模型', used.map(model => [model.model_identifier, label(model)]), used[0].model_identifier);
  const target = selectField('替换为', [], '');
  source.control.setAttribute('aria-label', '要替换的模型');
  target.control.setAttribute('aria-label', '替换为');
  const preview = n('div', undefined, 'stack');
  const submit = btn('一键替换', async () => {
    const result = await api('/config/tasks/replace-model', 'POST', {
      source_model: source.control.value, target_model: target.control.value, revision: config.revision
    });
    await loadConfig();
    $('modal').close();
    toast('已替换 ' + result.updated_count + ' 个功能的模型，立即生效');
  }, 'btn primary');
  function drawPreview() {
    const affected = Object.entries(config.task_configs).filter(([, task]) => task.model_list.includes(source.control.value));
    preview.replaceChildren(n('strong', '将更新 ' + affected.length + ' 个功能'));
    for (const [name, task] of affected) {
      const next = [...new Set(task.model_list.map(identifier => identifier === source.control.value ? target.control.value : identifier))];
      const names = identifiers => identifiers.map(identifier => config.models.find(model => model.model_identifier === identifier)?.name || identifier).join(' → ');
      preview.append(n('div', (taskLabels[name]?.[0] || name) + '：' + names(task.model_list) + ' ⇒ ' + names(next), 'note'));
    }
    submit.disabled = !affected.length || !target.control.value || target.control.value === source.control.value;
  }
  source.control.onchange = () => {
    const previous = target.control.value;
    const options = config.models.filter(model => model.model_identifier !== source.control.value);
    target.control.replaceChildren(...options.map(model => {
      const option = n('option', label(model));
      option.value = model.model_identifier;
      return option;
    }));
    target.control.value = options.some(model => model.model_identifier === previous) ? previous : options[0].model_identifier;
    drawPreview();
  };
  target.control.onchange = drawPreview;
  source.control.onchange();
  modal('批量替换功能模型', stack(grid(source, target),
    n('p', '仅替换所有功能中的模型绑定，不修改或删除模型池。保留其他模型顺序及功能参数，重复绑定自动合并；请确认目标模型支持相关功能。', 'note'),
    preview), [btn('取消', () => $('modal').close()), submit]);
}
$('replace-task-models').onclick = guard(replaceTaskModelsDialog);

let personaFields={};
function renderPersona(){const p=ensureConfig().persona,box=$('persona-fields');box.replaceChildren();personaFields={};for(const[key,label,kind]of [['name','名字'],['aliases','别名（每行一个）','textarea'],['owner_address','专属称呼'],['owner_qq_ids','专属称呼对应的 QQ（每行一个，留空不使用）','textarea'],['identity','核心身份','textarea'],['personality','性格','textarea'],['reply_style','回复风格','textarea'],['relationship_rules','关系与称呼规则','textarea']]){const f=field(label,['aliases','owner_qq_ids'].includes(key)?(p[key]||[]).join('\n'):p[key],kind);personaFields[key]=f;box.append(f.wrap)}box.append(n('p','专属称呼只按 QQ 号匹配；群主、群管理员和相同昵称不会获得此称呼。','note'));const growthEnabled=check('允许 Nova 自主更新性格',p.self_update_enabled);personaFields.self_update_enabled=growthEnabled;box.append(growthEnabled.wrap);const growthBox=n('div',undefined,'stack');box.append(growthBox);renderPersonaGrowth(growthBox).catch(error=>toast(error.message));const promptOptions=n('div',undefined,'stack');promptOptions.append(n('span','附加人设提示词','field-label'));personaFields.active_prompt_names=[];for(const prompt of runtimeConfig.prompts.filter(x=>x.compatible_stage==='persona')){const f=check(prompt.name,p.active_prompt_names.includes(prompt.name));personaFields.active_prompt_names.push([prompt.name,f]);promptOptions.append(f.wrap)}box.append(promptOptions)}
async function renderPersonaGrowth(box,conversation=''){
  box.replaceChildren(n('p','正在读取成长记录…','note'));
  const data=await api('/config/persona/growth'+(conversation?'?conversation='+encodeURIComponent(conversation):''));
  const picker=n('select'),all=n('option','全部会话');all.value='';picker.append(all);
  for(const value of data.conversations||[]){const option=n('option',value.replace('group:','群聊 · ').replace('private:','私聊 · '));option.value=value;picker.append(option)}
  picker.value=conversation;picker.onchange=guard(()=>renderPersonaGrowth(box,picker.value));
  const active=n('div',undefined,'stack'),history=n('details'),summary=n('summary','已撤销及历史记录');history.append(summary);
  for(const note of data.items||[]){
    const row=n('article',undefined,'anime-subcard rounded-xl p-4 stack');
    row.append(n('p',note.text),n('span',note.conversation.replace('group:','群聊 · ').replace('private:','私聊 · ')+' · '+new Date(note.created_at*1000).toLocaleString(),'note'));
    row.append(btn(note.active?'撤销':'恢复',async()=>{await api('/config/persona/growth/'+encodeURIComponent(note.id),'PUT',{active:!note.active});await renderPersonaGrowth(box,conversation)}));
    (note.active?active:history).append(row);
  }
  if(!active.childElementCount)active.append(n('p','还没有生效的性格笔记。','empty compact'));
  box.replaceChildren(n('h3','自己记下的'),n('p','Nova 可以积累或修订自己的性格、偏好和表达习惯。每条最多 30 字，各群与私聊分别保存；身份和主人绑定保持固定。','note'),picker,active,history);
}
async function savePersona(){const c=ensureConfig(),p={...c.persona};for(const[key,f]of Object.entries(personaFields)){if(key==='active_prompt_names')p[key]=f.filter(([,x])=>x.control.checked).map(([id])=>id);else if(key==='self_update_enabled')p[key]=f.control.checked;else p[key]=['aliases','owner_qq_ids'].includes(key)?f.control.value.split(/\r?\n/).map(x=>x.trim()).filter(Boolean):f.control.value}await mutate('/config/persona','PUT',p)}
function renderPrompts(){const section=$('sec-prompts');section.replaceChildren();const content=n('div',undefined,'prompt-list'),c=ensureConfig();for(const p of c.prompts){const card=n('article',undefined,'anime-subcard rounded-xl p-4 stack'),head=n('div',undefined,'section-heading');head.append(n('strong',p.name),badge(p.enabled?'active':'disabled'));const actions=n('div',undefined,'toolbar');actions.append(btn('编辑',()=>promptDialog(p)),btn('删除',async()=>{if(confirm('删除提示词「'+p.name+'」？'))await mutate('/config/prompts/'+encodeURIComponent(p.name),'DELETE')},'btn small danger'));card.append(head,n('p',p.source+' · '+({persona:'人设附加',reply:'回复',thinking:'思考',archived:'归档'})[p.compatible_stage],'note'),n('p',p.content.slice(0,140)+(p.content.length>140?'…':''),'prompt-excerpt'),actions);content.append(card)}section.append(panel('📝 提示词管理',btn('+ 新建提示词',()=>promptDialog(),'btn primary'),content));if(!c.prompts.length)content.append(n('div','尚未添加提示词模板。','empty'))}
function promptDialog(p={name:'',content:'',enabled:true,source:'admin',compatible_stage:'archived'}){const c=ensureConfig(),name=field('名称（字母、数字、下划线、短横线）',p.name),stage=selectField('使用阶段',[['persona','人设附加'],['thinking','思考'],['reply','回复'],['archived','仅归档']],p.compatible_stage),enabled=check('启用模板',p.enabled),content=field('提示词内容',p.content,'textarea');name.control.disabled=!!p.name;content.control.rows=14;modal(p.name?'编辑提示词':'新建提示词',stack(grid(name,stage,enabled),content),[btn('保存提示词',async()=>{const id=required(name.control.value,'请输入名称');await mutate('/config/prompts/'+encodeURIComponent(id),'PUT',{...p,name:id,content:content.control.value,enabled:enabled.control.checked,compatible_stage:stage.control.value},c.revision);$('modal').close()},'btn primary')])}
function renderFilters(){const section=$('sec-filters'),c=ensureConfig(),f=c.output_filter;section.replaceChildren();const words=field('禁用词（每行一个）',f.blocked_words.join('\n'),'textarea'),patterns=field('正则表达式（每行一个）',f.blocked_regex.join('\n'),'textarea'),replacement=field('替换文本',f.replacement),reasoning=check('移除 think / analysis 推理片段',f.strip_reasoning),identity=check('保护人设与固定称呼',f.protect_identity),sample=field('待测试文本','<think>内部推理</think>你好','textarea'),result=n('pre','等待测试');section.append(panel('🛡️ 发送前过滤',n('p','过滤规则会在生成后和发送前检查。'),grid(words,patterns,replacement,reasoning,identity),btn('保存过滤规则',async()=>{await mutate('/config/output-filter','PUT',{...f,blocked_words:words.control.value.split(/\r?\n/).filter(Boolean),blocked_regex:patterns.control.value.split(/\r?\n/).filter(Boolean),replacement:replacement.control.value,strip_reasoning:reasoning.control.checked,protect_identity:identity.control.checked},c.revision)},'btn primary')),panel('🧪 测试已保存的规则',sample.wrap,btn('测试过滤',async()=>{const v=await api('/config/output-filter/preview','POST',{text:sample.control.value});result.textContent=v.accepted?v.text:'已拦截此输出'}),result))}
function groupLabel(id){const group=groups.find(g=>String(g.id)===String(id));return String(id)+(group?.name?' · '+group.name:'');}
function conversationLabel(value){if(value.startsWith('group:'))return '群聊 · '+groupLabel(value.slice(6));return value.replace('private:','私聊 · ');}
async function loadGroups(){groups=await api('/groups');const box=$('group-cards');box.replaceChildren();$('resourceGroup').replaceChildren();for(const group of groups){const card=n('article',undefined,'group-policy-card anime-subcard p-4 rounded-xl section-heading'),text=n('div');text.append(n('strong','💬 群 '+groupLabel(group.id)),n('p',group.settings.enabled?'已开启机器人响应':'当前停止响应','note'));card.append(text,badge(group.settings.enabled?'active':'disabled'),btn('编辑策略',()=>editGroup(group)));box.append(card);const o=n('option',groupLabel(group.id));o.value=group.id;$('resourceGroup').append(o)}if(!groups.length)box.append(n('div','当前没有授权群，请在部署配置中设置群白名单。','empty'));for(const select of document.querySelectorAll('select[data-group-select]')){const chosen=select.value;select.replaceChildren(...groups.map(g=>{const o=n('option',groupLabel(g.id));o.value=g.id;return o}));if(groups.some(g=>g.id===chosen))select.value=chosen}return groups}
function editGroup(group) {
  const fields=[], basic=[], advanced=[];
  const names={enabled:'开启群聊回复',collect_messages:'保留聊天记录',notice_read:'读取群公告',file_metadata_read:'读取群文件列表',file_content_index:'读取群文件正文',cross_memory:'允许授权后的跨群记忆',media_read:'理解图片、语音和视频',research:'允许网页研究',sandbox:'允许代码任务',initiative:'允许主动参与聊天',memory_extract:'自动提取记忆',experience_collect:'积累问题解决经验',experience_embedding:'为经验建立搜索索引',research_external:'允许向外部服务提交研究问题',echo_silence:'群友复读时保持安静'};
  for(const[key,value]of Object.entries(group.settings))if(typeof value==='boolean'){
    const f=check(names[key]||key,value);fields.push([key,f]);
    (['enabled','collect_messages','media_read','initiative'].includes(key)?basic:advanced).push(f);
  }
  const timing=[];
  for(const [key,label,value,min,max] of [
    ['talk_frequency','主动聊天频率（0.5 约合两条消息触发）',.5,.01,1],
    ['initiative_min_interval','主动接话最小间隔（秒）',45,10,86400],
    ['initiative_daily_limit','每天主动接话上限（0 为不限）',0,0,10000],
    ['batch_quiet_seconds','等对方说完（秒）',1,0,5],
    ['batch_max_wait_seconds','消息合并最长等待（秒）',8,1,60],
    ['decision_cooldown_seconds','普通决策最小间隔（秒）',8,0,120],
    ['decision_wait_seconds','暂缓判断默认等待（秒）',5,1,30]]){
    const f=numeric(label,group.settings[key]??value,min,max);f.control.step=key==='talk_frequency'?'.01':key.startsWith('initiative_')?'1':'.1';timing.push([key,f]);
  }
  const allPermissions = ['enabled','collect_messages','notice_read','file_metadata_read','file_content_index','cross_memory','media_read','research','sandbox','initiative','memory_extract','experience_collect','experience_embedding','research_external','echo_silence'];
  modal('群聊设置 · '+groupLabel(group.id),stack(grid(...basic),details('聊天节奏',...timing.map(([,f])=>f)),details('更多功能与权限',...advanced),n('p','一键开启会打开本群的消息留存、公告/群文件读取、媒体、网页研究、AI 工作、主动参与和记忆能力。', 'note')), [btn('一键开启全部权限',async()=>{
    const data={...group.settings};for(const key of allPermissions)data[key]=true;
    await api('/groups/'+encodeURIComponent(group.id)+'/settings','PATCH',data);await loadGroups();$('modal').close();toast('已开启本群全部权限');
  },'btn primary'),btn('取消',()=>$('modal').close()),btn('保存',async()=>{
    const data={...group.settings};for(const[key,f]of fields)data[key]=f.control.checked;for(const[key,f]of timing)data[key]=number(f);
    await api('/groups/'+encodeURIComponent(group.id)+'/settings','PATCH',data);await loadGroups();$('modal').close();toast('群聊设置已保存');
  },'btn primary')]);
}
async function health(){try{const r=await fetch('/health/ready'),data=(await r.json()).data,online=!!data?.onebot_connected;$('connectionText').textContent=online?'OneBot v11 · 已连接':'OneBot v11 · 未连接';$('connectionDot').classList.toggle('online',online)}catch{$('connectionText').textContent='服务连接异常'}}
async function overview(){const[stats,info,list]=await Promise.all([api('/stats'),api('/system/info'),api('/messages?limit=5')]);$('eventCount').textContent=stats.events??'—';$('sentCount').textContent=stats.outbox.sent||0;$('groupCount').textContent=groups.length;$('unknownCount').textContent=stats.outbox.unknown||0;$('modeBadge').textContent=info.demo_mode?'演示模式':'正式模式';$('runBadge').textContent=info.paused?'已暂停':'运行中';$('pausedLabel').textContent=info.paused?'暂停响应':'响应开启';$('modelLabel').textContent='聊天模型 · '+info.chat_model;$('storageLabel').textContent=info.storage;$('runtime-status').textContent=info.paused?'已暂停响应':'运行中';const box=$('recentMessages');box.replaceChildren();for(const item of list.items){const row=n('div',undefined,'status-line');row.append(n('span',item.text.slice(0,100)),badge(item.status));box.append(row)}if(!list.items.length)box.append(n('div','尚无机器人回复。','empty'))}
async function runReplay(){const id=required($('replay-id').value,'请先填写消息 ID，或到消息列表选择回放。');$('replay-result').textContent=JSON.stringify(await api('/replays','POST',{message_id:id}),null,2)}
async function refresh(){await health();if(!token)return;if(view==='overview')await overview();if(view==='groups')await loadGroups();if(view==='messages')await messages();if(view==='bot-skills')await botSkills();if(['system','extensions'].includes(view))await system()}
async function botSkills(){const section=$('sec-bot-skills');if(!section)return;section.replaceChildren();const groupId=groups[0]?.id||'';const data=await api('/bot-skills/profiles'+(groupId?'?group_id='+encodeURIComponent(groupId):''));const profiles=data.profiles||[],workflows=data.workflows||[],group=selectField('群',groups.map(x=>[x.id,groupLabel(x.id)]),groupId),bot=field('机器人 QQ 号',''),name=field('备注名称',''),list=n('div',undefined,'stack');if(!profiles.length)list.append(n('div','还没有登记外部机器人。群主/管理员可在群里 @ 机器人并说“这是个机器人”。','empty'));for(const row of profiles){const p=row.payload||{},card=n('article',undefined,'anime-subcard rounded-xl p-4 stack');card.append(n('strong',(p.bot_name||'未命名机器人')+' · '+p.bot_id),n('p',p.summary||'尚未观察到足够的使用记录。','note'),n('p','已学习指令：'+(p.capabilities||[]).join('、')+'；流程 '+(p.workflow_count||0)+' 条','note'));list.append(card)}const workflowList=n('div',undefined,'stack');for(const row of workflows){const p=row.payload||{},card=n('article',undefined,'anime-subcard rounded-xl p-4 stack');card.append(n('strong','机器人 '+p.bot_id),n('p',p.summary||'多步交互流程','note'),n('pre',JSON.stringify(p.steps||[],null,2)));workflowList.append(card)}section.append(panel('🤖 外部机器人学习',n('p','登记后，Nova 会把机器人资料带入回复判断，并按需试探 help、/help、帮助或菜单。'),grid(group,bot,name),btn('登记机器人',async()=>{await api('/bot-skills/profiles','POST',{group_id:group.control.value,bot_id:bot.control.value.trim(),name:name.control.value.trim()});await botSkills();toast('外部机器人已登记')},'btn primary'),list),panel('🧭 已学习的多步流程',workflowList))}
async function refreshConsole(){await refresh();if(principalRole==='owner'&&['models','persona','prompts','filters'].includes(view))await loadConfig();if(typeof fullLoad==='function'&&fullViews[view])await fullLoad(view)}
function toggleTheme(){const dark=document.body.classList.toggle('dark-theme');$('theme-icon').textContent=dark?'☀️':'🌙';$('theme-text').textContent=dark?'浅樱模式':'暗夜模式';try{localStorage.setItem('neko-theme',dark?'dark':'light')}catch{}}
function addPrimaryNav(id,label){const b=btn(label,()=>navigate(id),'nav-btn');b.dataset.view=id;b.id='tab-'+id;$('main-nav').append(b);return b}
function buildSections(){for(const[id,label]of [['prompts','📝 提示词系统'],['filters','🛡️ 输出过滤'],['bot-skills','🤖 外部机器人']]){const s=n('section',undefined,'hidden space-y-6');s.id='sec-'+id;document.querySelector('main').insertBefore(s,document.querySelector('.footer'));addPrimaryNav(id,label)}const extra=n('details',undefined,'extra-navigation');extra.append(n('summary','更多管理功能'));const nav=n('div',undefined,'extra-navigation-items');extra.append(nav);$('main-nav').append(extra);for(const[id,label]of [['messages','💬 消息与评价'],['memories','📚 记忆管理'],['resources','📁 群资料'],['extensions','🧩 扩展能力'],['system','⚙️ 系统设置']])nav.append(addPrimaryNav(id,label));window.extraNavigation=nav}
function setAuthenticated(value){document.body.classList.toggle('auth-locked',!value);$('login-screen').hidden=value;$('account-menu').open=false;if(!value)$('token').focus()}
function showLoginError(message){$('login-error').textContent=message;$('login-error').hidden=!message;$('token').setAttribute('aria-invalid',message?'true':'false')}
function logout(silent=false){forgetLogin();token='';runtimeConfig=null;groups=[];cursor=null;activeVendorId='';$('token').value='';$('sessionStatus').textContent='NekoBot Console · QQAIBOT';$('active-persona').textContent='等待登录';$('runtime-status').textContent='等待管理员登录';for(const id of ['vendor-list','model-list','pool-vendor-pills','function-routing-list','overview-routing-grid','persona-fields','sec-prompts','sec-filters','group-cards','recentMessages'])$(id).replaceChildren();for(const id of ['messagesTable','memoriesTable','resourcesTable'])empty(id,'请登录控制台');for(const id of ['groupCount','eventCount','sentCount','stat-vendor-count','stat-model-count'])$(id).textContent='—';for(const id of ['systemInfo','replay-result'])$(id).textContent='等待登录';$('resourceGroup').replaceChildren();$('modal').close();$('modalBody').replaceChildren();$('modalFooter').replaceChildren();document.querySelectorAll('main input:not([type=checkbox]),main textarea').forEach(x=>x.value='');document.querySelectorAll('main .table-wrap tbody,main .answer').forEach(x=>x.replaceChildren());document.querySelectorAll('main pre').forEach(x=>x.textContent='');document.querySelectorAll('main iframe').forEach(x=>{x.src='about:blank';x.hidden=true});showLoginError('');setAuthenticated(false);if(!silent)toast('已退出登录')}
buildSections();
document.querySelector('#tab-bot-skills')?.remove();
document.querySelector('#sec-bot-skills')?.remove();
// Same-tab storage survives reloads and is cleared by explicit logout or a rejected key.
const loginStorageKey='qqaibot-console-login';
function rememberLogin(value){try{sessionStorage.setItem(loginStorageKey,value)}catch{}}
function forgetLogin(){try{sessionStorage.removeItem(loginStorageKey)}catch{}}
async function restoreSession(){
  let saved='';try{saved=sessionStorage.getItem(loginStorageKey)||''}catch{}
  if(saved)await login(undefined,saved);
}
async function login(event,restoredToken=''){
  event?.preventDefault();if(loginPending)return;
  showLoginError('');loginPending=true;$('connect').disabled=true;
  $('login-form').setAttribute('aria-busy','true');
  $('login-button-label').textContent=restoredToken?'正在恢复登录…':'正在登录…';
  try{
    token=restoredToken||required($('token').value,'请输入管理密钥');
    const info=await api('/system/info');principalRole=info.role;
    for(const id of ['models','persona','prompts','filters'])document.querySelector('[data-view='+id+']').hidden=info.role!=='owner';
    if(info.role==='owner')await loadConfig();
    await loadGroups();navigate('overview',false);await refreshConsole();
    rememberLogin(token);$('token').value='';setAuthenticated(true);
  }catch(error){
    logout(true);
    // A temporary NAS/network outage must not erase a valid saved login.
    if(restoredToken&&error.status!==401)rememberLogin(restoredToken);
    showLoginError(restoredToken&&error.status!==401?'暂时无法恢复登录，请刷新重试。'+error.message:error.message);
    $('token').focus();
  }finally{
    loginPending=false;$('connect').disabled=false;
    $('login-form').setAttribute('aria-busy','false');$('login-button-label').textContent='登录控制台';
  }
}
document.addEventListener('DOMContentLoaded',()=>{restoreSession()},{once:true});
$('login-form').onsubmit=login;
$('logout').onclick=()=>logout();$('closeModal').onclick=()=>$('modal').close();
document.addEventListener('click',event=>{if(!$('account-menu').contains(event.target))$('account-menu').open=false});document.addEventListener('keydown',event=>{if(event.key==='Escape')$('account-menu').open=false});$('token').addEventListener('input',()=>showLoginError(''));
for(const b of document.querySelectorAll('[onclick]')){const action=b.getAttribute('onclick');b.removeAttribute('onclick');const direct={'toggleTheme()':toggleTheme,'refreshConsole()':refreshConsole,'addVendor()':addVendor,'addModelToVendor()':addModelToVendor,'QuickFetchModels()':QuickFetchModels,'savePersona()':savePersona,'runReplay()':runReplay};if(direct[action])b.onclick=guard(direct[action]);else{const tab=action.match(/switchTab\('([^']+)'\)/),sub=action.match(/switchModelSubTab\('([^']+)'\)/);b.onclick=()=>{if(tab)navigate(tab[1]);if(sub)switchModelSubTab(sub[1])}}}
for(const[id,action]of [['searchMessages',()=>messages()],['moreMessages',()=>messages(true)],['searchMemories',()=>memories()],['loadResources',()=>resources()],['syncResources',()=>resources(true)],['pauseBot',async()=>{await api('/controls/emergency-stop','POST',{paused:true});toast('已暂停响应');await refreshConsole()}],['resumeBot',async()=>{await api('/controls/emergency-stop','POST',{paused:false});toast('已恢复响应');await refreshConsole()}]])$(id).onclick=guard(action);
try{if(localStorage.getItem('neko-theme')==='dark')toggleTheme()}catch{}
document.addEventListener('keydown',()=>{document.body.dataset.keyboard='true'});document.addEventListener('pointerdown',()=>{delete document.body.dataset.keyboard});
document.addEventListener('click',e=>{const b=e.target.closest('button');if(!b||document.body.dataset.keyboard||matchMedia('(prefers-reduced-motion: reduce)').matches)return;b.getAnimations().filter(a=>a.id==='press-feedback').forEach(a=>a.cancel());const a=b.animate([{transform:'scale(.97)'},{transform:'scale(1.015)',offset:.65},{transform:'scale(1)'}],{duration:220,easing:'cubic-bezier(.23,1,.32,1)'});a.id='press-feedback'});
switchModelSubTab('vendors');health();setInterval(health,15000);
