// The daily console has one navigation level and a small settings disclosure.
const advancedNavigation = document.querySelector('.extra-navigation');
advancedNavigation.querySelector('summary').textContent = '更多设置';
const advancedGroups = [
  ['设置', ['prompts','filters','system','mediafull','messages','sandbox']]
];
for (const id of ['overview','models','persona','groups','memories','stickersfull','tasksfull','deepseek','streams','logs']) {
  const button=document.querySelector('nav button[data-view="'+id+'"]');
  if(button) advancedNavigation.before(button);
}
for (const id of ['resources','knowledgefull','experiencev2','workv2','researchv2','agentsv3','extensions','pluginsfull','qzonefull']) {
  document.querySelector('nav button[data-view="'+id+'"]')?.remove();
}
for (const [title, ids] of advancedGroups) {
  const group = n('div', undefined, 'nav-settings');
  const items = n('div', undefined, 'nav-group-items');
  group.append(items);
  for (const id of ids) {
    const button = document.querySelector('nav button[data-view="'+id+'"]');
    if (button) items.append(button);
  }
  window.extraNavigation.append(group);
}
advancedNavigation.open = false;
const simpleNavigate = navigate;
navigate = function(id,refreshData=true) {
  if(id==='workv2')id='tasksfull';
  const button = document.querySelector('nav button[data-view="'+id+'"]');
  for (let parent = button?.parentElement; parent && parent !== $('main-nav'); parent = parent.parentElement)
    if (parent.tagName === 'DETAILS') parent.open = true;
  simpleNavigate(id,refreshData);
};

// Memory follows the familiar library pattern: category, search, source cards.
const memorySection=$('sec-memories'),memoryLegacyTable=$('memoriesTable');
const memoryLegacy=n('div');memoryLegacy.hidden=true;
memoryLegacy.append(memoryLegacyTable.closest('.table-wrap'));
const memoryOwner=$('memoryActor');memoryOwner.placeholder='QQ 号（可选）';
const memorySearch=field('关键词','');
memorySearch.control.placeholder='筛选当前类别的记忆';
const memoryKinds={personal:'人物记忆',knowledge:'群聊知识',shared:'共享知识'};
const memoryTabs=n('div',undefined,'memory-tabs');
const memorySummary=n('p','按记入时间从近到远显示。','note');
const memoryCards=n('div',undefined,'memory-library');
const memoryOwnerField=n('label',undefined,'config-field');memoryOwnerField.append(n('span','人物','field-label'),memoryOwner);
const memorySelected=new Set();
const memoryDeleteSelected=btn('删除选中',async()=>{
  if(!memorySelected.size||!confirm('确定删除当前页选中的 '+memorySelected.size+' 条旧记忆？删除后无法恢复。'))return;
  const selected=[...memorySelected];let removed=0;
  try{for(const id of selected){await api('/operations/memories/'+encodeURIComponent(id),'DELETE');removed++;}}
  finally{memorySelected.clear();await loadMemoryLibrary();if(removed)toast('已删除 '+removed+' 条记忆');}
},'btn danger');
memoryDeleteSelected.disabled=true;
const memoryPager=n('div',undefined,'memory-pager');
const memoryPrevious=btn('上一页',()=>{memoryPage--;loadMemoryLibrary();});
const memoryNext=btn('下一页',()=>{memoryPage++;loadMemoryLibrary();});
const memoryPageLabel=n('span','','note');
memoryPager.append(memoryPrevious,memoryPageLabel,memoryNext,memoryDeleteSelected);
memorySection.replaceChildren(panel('记忆库',memoryTabs,grid(memorySearch,memoryOwnerField,
  btn('筛选',()=>{memoryPage=1;loadMemoryLibrary();},'btn primary')),memorySummary),memoryCards,memoryPager,memoryLegacy);
let memoryGeneration=0,memoryPage=1,memoryKind='personal';
const memoryTabButtons={};
for(const [kind,title] of Object.entries(memoryKinds)){
  const button=btn(title,()=>{memoryKind=kind;memoryPage=1;memorySearch.control.value='';memoryOwnerField.hidden=kind!=='personal';
    for(const [key,tab] of Object.entries(memoryTabButtons)){tab.classList.toggle('primary',key===kind);tab.setAttribute('aria-current',key===kind?'page':'false');}
    loadMemoryLibrary();},'btn');
  memoryTabButtons[kind]=button;memoryTabs.append(button);
}
memoryTabButtons.personal.classList.add('primary');memoryTabButtons.personal.setAttribute('aria-current','page');
async function loadMemoryLibrary(){
  const generation=++memoryGeneration;
  const params=new URLSearchParams({q:memorySearch.control.value,kind:memoryKind,
    actor_id:memoryKind==='personal'?memoryOwner.value.trim():'',page:String(memoryPage),page_size:'24'});
  const data=await api('/operations/memories?'+params);
  if(!token||generation!==memoryGeneration)return;
  if(!data.items.length&&data.total&&memoryPage>data.pages){memoryPage=data.pages;return loadMemoryLibrary();}
  memoryCards.replaceChildren();memorySelected.clear();memoryDeleteSelected.disabled=true;
  const scopes={long_group:'来源群',daily_group:'群聊摘要',private_user:'私聊',global_personal_owner:'已授权跨群',global_public_knowledge:'跨群共享'};
  for(const item of data.items){
    const card=n('article',undefined,'memory-card anime-card');
    const head=n('div',undefined,'section-heading');head.append(n('strong',memoryKinds[item.kind]),badge(item.status==='unverified'?'待核实':'active'));
    const selection=n('label',undefined,'memory-select'),checkbox=n('input');checkbox.type='checkbox';
    checkbox.setAttribute('aria-label','选择记忆 '+item.id);selection.append(checkbox,n('span','选中清理'));
    checkbox.onchange=()=>{if(checkbox.checked)memorySelected.add(item.id);else memorySelected.delete(item.id);
      memoryDeleteSelected.disabled=!memorySelected.size;};
    head.append(selection);
    const body=n('p',item.content,'memory-content'),meta=n('p',
      [item.actor_id?(item.actor_name?item.actor_name+'（'+item.actor_id+'）':'QQ '+item.actor_id):'',item.visibility==='shared_groups'?'全局群记忆':scopes[item.scope]||item.scope,item.group_id?'来源群 '+groupLabel(item.group_id):'',item.created_at?date(item.created_at):''].filter(Boolean).join(' · '),'note');
    const actions=n('div',undefined,'toolbar');
    actions.append(btn('原话与上下文',async()=>{const box=n('div',undefined,'stack');box.append(n('h3','完整记忆'),n('p',item.content,'memory-content'),n('p','原始发言','note'));
      for(const source of item.sources||[]){const quote=n('blockquote',undefined,'memory-source');quote.append(n('small',(source.actor_name||'QQ '+source.actor_id)+' · '+date(source.ts),'note'),n('p',source.text,'memory-content'));box.append(quote);}
      if(!item.sources?.length)box.append(n('p','原文已撤回或过期；无法凭空补回。','note'));
      if(item.kind==='personal'){const value=await api('/operations/memories/'+encodeURIComponent(item.id)+'/context');box.append(n('h3','当时的聊天上下文'));for(const row of value.items||[])box.append(n('p',row.content,'memory-content'));}
      modal(memoryKinds[item.kind]+' · 原话与上下文',box);}),
      btn('删除',async()=>{if(!confirm('删除这条记忆？'))return;await api('/operations/memories/'+encodeURIComponent(item.id),'DELETE');await loadMemoryLibrary();},'btn small danger'));
    card.append(head,body,meta);
    if(item.sources?.length){const source=item.sources[0];card.append(n('p','原话：'+source.text,'memory-source-preview'));}
    card.append(actions);memoryCards.append(card);
  }
  if(!data.items.length)memoryCards.append(n('div','暂无匹配的记忆。开启群聊设置中的「自动提取记忆」后，旁听学习会自动积累。','empty'));
  memoryPrevious.disabled=memoryPage<=1;
  memoryNext.disabled=memoryPage>=data.pages;
  memoryPageLabel.textContent='第 '+memoryPage+' / '+Math.max(data.pages,1)+' 页';
  memorySummary.textContent=memoryKinds[memoryKind]+'共 '+data.total+' 条，按记入时间从近到远。'+(data.global_group_memory?'群记忆在已授权群间共享，来源群仅表示出处；私聊记忆不共享。':'');
}
memorySearch.control.onkeydown=event=>{if(event.key==='Enter'){memoryPage=1;guard(loadMemoryLibrary)();}};
const memoryPriorLoad=fullLoad;fullLoad=async function(id){if(id==='memories')return loadMemoryLibrary();return memoryPriorLoad(id);};
const memoryPriorRefresh=refresh;refresh=async function(){await memoryPriorRefresh();if(view==='memories')await loadMemoryLibrary();};
const memoryPriorLogout=logout;logout=function(silent=false){memoryGeneration++;memoryCards.replaceChildren();memoryPriorLogout(silent);};
// Existing optional tool forms remain usable, without confronting users with code/JSON.
function foldPanel(sectionId, title, summary) {
  const section = $(sectionId);
  if (!section) return;
  for (const child of [...section.children]) {
    if (child.querySelector('h2')?.textContent !== title) continue;
    const disclosure = n('details', undefined, 'optional-panel');
    disclosure.append(n('summary', summary));
    child.before(disclosure); disclosure.append(child);
  }
}
foldPanel('tasksfull','隔离代码任务','新建代码任务');
foldPanel('pluginsfull','注册隔离插件','安装自定义插件（高级）');
foldPanel('agentsv3','只读代码审查','代码审查（高级）');
foldPanel('qzonefull','QQ 空间适配器','QQ 空间功能（需先配置适配器）');

const simplePersonaRender = renderPersona;
renderPersona = function() {
  simplePersonaRender();
  const box = $('persona-fields');
  box.classList.add('persona-form');
  personaFields.aliases.control.rows = 2;
  personaFields.personality.control.rows = 5;
  const extra = n('details', undefined, 'anime-subcard rounded-xl p-4');
  const content = n('div', undefined, 'stack');
  extra.append(n('summary', '高级人设与提示词'), content);
  for (const child of [...box.children]) {
    if ([personaFields.name.wrap,personaFields.aliases.wrap,personaFields.personality.wrap].includes(child)) continue;
    content.append(child);
  }
  box.append(n('p','在群里叫名字或任意别名即可唤起，英文名字不区分大小写。','note'),extra);
};
const simpleV3Load = loadV3Config;
loadV3Config = async function() {
  await simpleV3Load();
  const raw = v3Config.querySelector('pre');
  if (!raw) return;
  const disclosure = n('details');
  disclosure.append(n('summary','查看详细运行参数'), raw);
  v3Config.replaceChildren(n('p','日常聊天的模型在「模型配置 → 功能用途」中设置。','note'),
    btn('去设置模型',()=>{navigate('models');switchModelSubTab('routing');},'btn primary'),disclosure);
};
const simpleLogout = logout;
logout = function(silent=false) {
  simpleLogout(silent);
  for (const details of document.querySelectorAll('.extra-navigation,.nav-group,.optional-panel')) details.open=false;
};

const workModelSummary=n('p','加载后显示当前工作模型。','note');
v2Sections.workv2.prepend(panel('AI 工作模型',workModelSummary,
  btn('设置工作模型',async()=>{await loadConfig();taskSettingsDialog('work');},'btn primary'),
  btn('设置代码审查模型',async()=>{await loadConfig();taskSettingsDialog('observer');})));
const workModelPriorLoad=fullLoad;
fullLoad=async function(id){
  await workModelPriorLoad(id);
  if(id==='workv2'){
    const config=await loadConfig(),route=config.task_configs.work;
    workModelSummary.textContent=route?.model_list.length?'当前模型：'+route.model_list.map(id=>config.models.find(m=>m.model_identifier===id)?.name||id).join(' → '):'尚未配置，请先选择 AI 工作模型。';
  }
};
const workModelPriorLogout=logout;
logout=function(silent=false){workModelPriorLogout(silent);workModelSummary.textContent='等待登录';};
