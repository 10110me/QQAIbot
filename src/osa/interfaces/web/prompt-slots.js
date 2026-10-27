// Fixed, documented destinations: editing a card changes an actual request point.
const importedPromptsRender=renderPrompts;
let promptSlotsRequest=0;
renderPrompts=function(){
  importedPromptsRender();
  const section=$('sec-prompts'),imports=n('details',undefined,'optional-panel');
  imports.append(n('summary','迁入的 MaiBot 模板与自定义片段'));
  imports.append(...section.children);
  section.replaceChildren(panel('机器人提示词',n('p','每张卡片对应实际调用用途。点击编辑、预览变量替换并保存；人设和权限边界仍由管理员配置与程序权限共同保护。','note')),imports);
  const list=n('div',undefined,'prompt-slot-grid');section.firstChild.after(list);
  const ticket=++promptSlotsRequest;
  api('/config/prompt-slots').then(data=>{
    if(!token||ticket!==promptSlotsRequest)return;
    for(const slot of data.slots){
      const card=n('article',undefined,'anime-subcard prompt-slot-card');
      const heading=n('div',undefined,'section-heading');
      heading.append(n('strong',slot.title),badge(slot.customized?'已修改':'默认'));
      card.append(heading,n('p',slot.description,'note'),btn('编辑提示词',()=>editPromptSlot(slot,data),'btn'));
      if(slot.source)card.append(n('p',slot.source,'note'));
      list.append(card);
    }
  }).catch(error=>{if(token&&ticket===promptSlotsRequest)list.textContent=error.message});
};
function editPromptSlot(slot,data){
  const content=field('模板内容',slot.content,'textarea');content.control.rows=15;
  const variables=n('div',undefined,'prompt-variables');
  for(const [key,description]of Object.entries(data.variables)){
    const button=btn('{'+key+'}',()=>{
      const input=content.control,start=input.selectionStart??input.value.length,end=input.selectionEnd??start;
      input.value=input.value.slice(0,start)+'{'+key+'}'+input.value.slice(end);input.focus();
    },'btn small');button.title=description;variables.append(button);
  }
  const guide=n('details');guide.append(n('summary','可用变量（点击插入）'),variables);
  const protocol=n('details');protocol.append(n('summary','系统输出协议（固定）'),n('pre',slot.output_contract||'按此阶段描述输出内容。'));
  const preview=n('pre','点击“预览替换结果”查看渲染后的内容。');
  modal(slot.title,stack(n('p',slot.description,'note'),content,guide,protocol,
    btn('恢复默认内容',()=>{content.control.value=slot.default;}),
    btn('预览替换结果',async()=>{const result=await api('/config/prompt-slots/'+slot.id+'/preview','POST',{content:content.control.value,revision:data.revision});preview.textContent=result.text;}),preview),
    [btn('取消',()=>$('modal').close()),btn('保存并生效',async()=>{
      await api('/config/prompt-slots/'+slot.id,'PUT',{content:content.control.value,revision:data.revision});
      $('modal').close();await loadConfig();toast('提示词已保存，下一次请求生效');
    },'btn primary')]);
}
const slotsLogout=logout;
logout=function(silent=false){promptSlotsRequest++;slotsLogout(silent);};
