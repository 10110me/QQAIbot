# DeepSeek 可见浏览器工作区

## 登录和查看

使用完整版 Compose 启动 browser-worker。进入管理页“DeepSeek 工作区”，输入 owner Token 后点击“连接实时浏览器”。应用先用 owner 认证签发只作用于 /browser 的 HttpOnly 会话，再代理 noVNC HTTP 静态资源和 WebSocket。

浏览器运行在独立容器，专用 profile 在 browserdata 数据卷，包含登录 Cookie。首次登录、扫码、人机验证、账号安全确认均由你在真实桌面操作。程序不读取或输出密码、Cookie，也不实现验证码绕过。

普通群域后台账号无权进入这个桌面。不要直接公开 browser-worker 的 6080 或 8090 端口。公网映射通过 HTTPS，设置 OSA_BROWSER_COOKIE_SECURE=true。

## 问题、答案和任务

所有研究任务调用 browser-worker /ask，请求 ID 来自持久化主任务。浏览器队列串行提交，避免多个群同时在一个输入框打字。网站回答稳定且未观察到生成按钮时标记完成；如果网页结构不符合预期，标记待人工处理。

提交前记录 submitted 状态。发生进程重启、断线或按钮点击异常时，不会自动把同一个问题再提交一次。已保存任务通过相同请求 ID 查询；不能确定是否提交成功时需要在桌面确认。

UI 中的“问题与回答历史”来自浏览器任务记录，可查看机器人实际提交的提示、得到的网页正文、来源、时间和错误状态。实时桌面显示网页本身；两者都能用于调试页面兼容性。研究不提供网站账号的完整历史抓取。

研究提示只带本次问题及搜索引擎证据，不带所有群的聊天或全局记忆。搜索引擎故障时来源列表为空，提示要求 DeepSeek 明确表达无法验证；不会伪造已找到来源。最终答案是否可靠仍需结合原始公开来源核查。

## 页面选择器

这些变量传给 Worker，不是发给模型：

| 变量 | 作用 |
|---|---|
| DEEPSEEK_INPUT_SELECTOR | 可见且可编辑的输入框 |
| DEEPSEEK_ANSWER_SELECTOR | 答案正文列表，默认 .ds-markdown |
| DEEPSEEK_SEND_SELECTOR | 发送按钮；空值用 Enter |
| DEEPSEEK_NEW_CHAT_SELECTOR | 可选新对话按钮；空值沿用当前对话 |
| DEEPSEEK_SEARCH_TOGGLE_SELECTOR | 可选联网搜索开关 |
| DEEPSEEK_SEARCH_ENABLED_SELECTOR | 已开启搜索时可见的状态元素；与上一项一起配置 |
| DEEPSEEK_STOP_SELECTOR | 生成中的停止按钮 |
| DEEPSEEK_TIMEOUT_SECONDS | 未确认完成时进入待人工状态的时间 |

两个搜索选择器均配置时，程序会先观察是否已开启，再点击并复核；否则由你在桌面中手动设置，避免盲目把开关反复切换。页面改版后优先在实时桌面检查，再修改变量并重建 Worker。默认值只是兼容实现，需要实机确认。

## 数据与隔离

主进程持有 QQ、模型与数据库凭据；浏览器容器只有自身 Worker Token 和登录卷。它不接收这些其他凭据。截图另用 screenshot-worker，既不挂载 DeepSeek 登录卷，也不共享已认证浏览器上下文。

删除主系统研究数据会尝试清理 Worker 缓存。Worker 离线时保留无明文的删除待办，恢复后重试。DeepSeek 网站服务器保存的账号历史应在其网页中另外管理；删除本地记录不能保证第三方已经删除。
