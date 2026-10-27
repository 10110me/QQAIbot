import unittest
from pathlib import Path


class WorkspaceUITests(unittest.TestCase):
    def test_cards_filters_remove_and_detail_timer(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            candidates=[Path(p.chromium.executable_path),Path('C:/Program Files/Google/Chrome/Application/chrome.exe')]
            executable=next((path for path in candidates if path.is_file()),None)
            if executable is None:
                self.skipTest('Chromium is not installed')
            browser=p.chromium.launch(headless=True,executable_path=str(executable))
            try:
                page=browser.new_page()
                page.on('dialog',lambda dialog:dialog.accept())
                page.set_content('<main><div class="footer"></div></main><nav></nav><button id="logout"></button>'
                                 '<dialog id="modal"><h2 id="modalTitle"></h2><div id="modalBody"></div><div id="modalFooter"></div></dialog>')
                page.add_script_tag(content="""
                    var labels={}, groups=[], token='test', view='tasksfull';
                    window.extraNavigation=document.createElement('div');
                    function $(id){return document.getElementById(id)}
                    function n(tag,text,cls){const el=document.createElement(tag);if(text!==undefined)el.textContent=text;if(cls)el.className=cls;return el}
                    function btn(text,fn,cls){const el=n('button',text,cls);el.onclick=fn;return el}
                    function badge(s){return n('span',s,'badge')}
                    function date(s){return String(s)}
                    function groupLabel(s){return s}
                    function uuid(){return 'synthetic-ui-command'}
                    function navigate(){} function toast(){} function json(){}
                    function v2Download(){}
                    function modal(title,body,buttons){$('modalTitle').textContent=title;$('modalBody').replaceChildren(body);$('modalFooter').replaceChildren(...buttons);$('modal').showModal()}
                    $('logout').onclick=()=>{};
                    var calls=[],rows=[
                      {id:'failed',status:'FAILED',failure_kind:'infrastructure',current_step:'服务失败任务',requirement:'失败任务要求',created_at:1,version:2},
                      {id:'complete',status:'COMPLETED',current_step:'已完成任务',requirement:'完成任务要求',created_at:2,version:3,artifact_id:'verified'},
                      {id:'running',status:'RUNNING',current_step:'运行任务',requirement:'正在运行',created_at:3,version:4,percent:null}];
                    async function api(path,method='GET'){
                      calls.push([path,method]);
                      if(path==='/coding/tasks')return rows.map(r=>({...r}));
                      if(path.endsWith('/timeline'))return [];
                      const id=path.split('/')[3].split('?')[0];
                      if(method==='DELETE'){rows=rows.filter(r=>r.id!==id);return {archived:true}}
                      return {...rows.find(r=>r.id===id)};
                    }
                """)
                page.add_script_tag(path=str(Path(__file__).parents[1]/'src/osa/interfaces/web/full.js'))
                page.evaluate('refreshTasks()')
                self.assertEqual(page.locator('#tasksfull .workspace-task').count(),3)
                self.assertEqual(page.evaluate("calls.filter(c=>c[0]!=='/coding/tasks').length"),0)
                page.get_by_label('筛选任务状态').select_option('COMPLETED')
                page.wait_for_function("document.querySelectorAll('#tasksfull .workspace-task').length===1")
                self.assertIn('已完成任务',page.locator('#tasksfull').inner_text())
                page.get_by_label('筛选任务状态').select_option('all')
                page.wait_for_function("document.querySelectorAll('#tasksfull .workspace-task').length===3")
                page.locator('#tasksfull .workspace-task').filter(has_text='服务失败任务').get_by_role('button',name='删除记录').click()
                page.wait_for_function("document.querySelectorAll('#tasksfull .workspace-task').length===2")
                page.locator('#tasksfull .workspace-task').filter(has_text='运行任务').click()
                page.wait_for_function('taskDetailTimer!==null')
                page.locator('#modalFooter').get_by_role('button',name='关闭').click()
                page.wait_for_function('taskDetailTimer===null')
                page.locator('#tasksfull .workspace-task').filter(has_text='已完成任务').click()
                page.wait_for_function("document.getElementById('modal').open")
                self.assertTrue(page.evaluate('taskDetailTimer===null'))
            finally:
                browser.close()
