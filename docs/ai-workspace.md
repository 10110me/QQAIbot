# 统一 AI 工作空间

工作空间从聊天回复中分离出来，但由同一个 Thinking Agent 决定是否调用。提供三个可配置后端：

- `builtin`：工作模型通过 `file.list/read/write`、`python.execute`、`project.test`、`web.read`、`browser.browse` 和 `deepseek.consult` 逐步执行任务；文件修改保存在任务检查点。代码在隔离 Worker 中运行，平台根据真实测试结果打包交付。
- `opencode`：`src/osa/infrastructure/v3/opencode.py` 的版本锁定适配器。它把每个任务映射到独立 workspace 的 OpenCode 会话，在该隔离容器内允许编程工具、Shell 和网络；平台仍独立运行测试和记录证据。
- `codex`：`src/osa/infrastructure/v3/codex.py` 通过隔离网关使用 Codex CLI，沿用工作模型配置；兼容端点的协议与能力需要按供应商验证，见编程后端说明。

当前交付验证仅支持 Python `unittest` 和少量文本/网页源文件，不支持 Java/Gradle、Forge/Fabric 模组编译。模型即使能在 OpenCode 容器生成这些文件，也不能据此把模组标记为“已编译通过”。

统一约束：

1. 普通聊天模型不能通过回复文字伪造文件或测试结果。
2. `coding.submit` 在群策略允许工作空间时自动创建并排队，输入绑定和父任务绑定在排队前完成。后台旧的手动创建接口仍需要审批；任务状态、检查点、测试证据和最终产物由工作 Worker 写回。
3. QQ 文件在进入工作区前经过大小、路径、压缩包成员和来源校验；撤回原消息会使输入失效。
4. 工作任务可以把附件交给工作 Agent；同一附件也可以由 `deepseek.consult` 作为图片或文件咨询输入。
5. 工作 Agent 可替换，但必须实现任务提交、状态、追加指令、取消、产物和能力声明；不能直接拿到 QQ 登录态、全局记忆库或宿主机密钥。
6. `python.execute` 会恢复工作模型已写入的文件，再在隔离容器执行；程序输出文件保存到代际目录的 `artifacts`，最终随项目压缩包的 `outputs/` 一同交付。

DeepSeek 网页咨询是 Thinking Agent 的另一个工具，不属于工作模型槽位。它使用隔离浏览器的持久登录会话，任务记录保存会话 ID、来源事件、附件状态和外部回答限制。续聊必须显式传 `session_id`；新话题传 `new_conversation=true`。网页回答只作为外部模型资料，不能自动提升为已验证知识。
