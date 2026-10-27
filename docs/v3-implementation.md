# 0.4.0 / Design V3 实现对照

原始方案：[design-v3.md](design-v3.md)。本版接入第 46～58 章的核心运行流程，沿用 V2 的 QQ、隐私、文件、工作空间、经验、夜间研究、DeepSeek 可见浏览器和管理后台。仍需真实服务器联调，不能把合同测试视作整份方案的生产验收。

| V3 要求 | 本版行为 |
|---|---|
| Decision 只判断是否思考 | 独立 `THINK / SKIP / DEFER`；群关闭、暂停、复读、忙碌时的主动候选在模型前拦截；合法直接请求走强制路径 |
| 混合决策 | `classifier / llm / hybrid` 可配；低分进入 LLM；内置分类器是版本化规则启发式，**不是训练好的分类器**，所有分数标记未校准，LLM 自评分不当概率 |
| Thinking 多轮工具 | 每个源事件独立短会话，最多 12 步 / 75 秒；低内存 6 步 / 45 秒；工具身份由程序注入，截止时间、大小与幂等键受限 |
| 思考可以沉默 | `SILENT` 不调用 Reply、不写发送队列；另支持 REPLY、WAIT_FOR_USER、TASK_DELEGATED、FAILED |
| Reply 只负责表达 | 仅接收结构化 brief、证据引用和限制；不接收聊天历史、代码工作目录或工具接口；最后仍由原 SendPolicy/Outbox 检查 |
| 异步编程与研究 | 先持久化并返回 task_id；审批后独立 Work 进程执行；完成/失败事件恢复父 Thinking，由它决定回复还是沉默 |
| 编程 Provider | 保留可用的 builtin Python 执行器；可切 OpenCode 1.2.27 HTTP 适配器，单独编程模型配置，版本不匹配拒绝提交 |
| OpenCode 物理隔离 | 可选 Docker 网关为每个 workspace 创建独立容器、项目卷、HOME 卷和派生密码；最多一个活跃编码工作区，180 秒无人访问自动停止 |
| 无脑重复提交防护 | 创建/提示发送前写不确定性标记；重启核对已有 session、稳定 messageID；超时不盲目重发；对外不宣称 exactly-once |
| 暂停与取消能力 | OpenCode 声明无原生 pause/resume；PAUSING 等待安全边界，租约过期不等于已暂停；取消请求 abort 后核对 idle，未知时保留待确认 |
| 真实进度 | 保留 Git、manifest、实际测试、成果、更新时间；OpenCode 观测记录标记 provider 报告，不凭代理文本宣称测试通过 |
| 只读 Observer | 独立模型/密钥/预算；仅拿复制、hash 检查过的注册表源码快照，没有路径、shell、Git 写入或任务修改工具；报告绑定准确 revision |
| 只读审查降级 | 未配 Observer 模型时返回 `snapshot_only`，不假装执行模型审查；STATUS 直接查数据库，不启动或打断编码会话 |
| 学习与科研 | 继承 V2 实际沙箱失败/通过证据、经验状态与共享审核；科研全走 DeepSeek 网页，回答保留待来源核验状态 |
| 视频声音 | 提取最多前 60 秒的真实音轨再 ASR，结合关键帧结果；不可用时明示未知；V3 停用 TTS |
| WebUI/API | 新 Agent 配置/只读审查页；工作台沿用审批、控制、差异、事件与成果；新增 `/coding/tasks`、`/thinking/sessions` 等 API |
| 兼容迁移 | 新增 0005；V2 任务仍是事实源，在同一任务事务中双写 V3 投影和事件；旧任务保持原 owner/group/可见性 |

## 运行与目录

```text
domain/agents.py                  公开 Provider / Brief / Think 契约
application/v3/decision.py        硬规则、分类器和 LLM 回退
application/v3/thinking.py        有界多轮循环、Reply Writer
application/v3/tools.py           封闭工具目录与作用域/副作用检查
application/v3/service.py         社交接入、父任务唤醒和结果路由
infrastructure/v3/registry.py     V2 投影、工具回执、事务事件、唤醒队列
infrastructure/v3/opencode.py     锁版本 HTTP Provider、未知结果恢复
infrastructure/v3/observer.py     固定源码快照审查
workers/opencode/                受信任网关和独立编码镜像
interfaces/v3.py + web/v3.js      owner 控制台与 API
```

领域/应用层不导入 NapCat、OpenCode、数据库或 HTTP 客户端。公开 SDK 的 `AGENT_PROTOCOL_VERSION=1` 与旧插件 `API_VERSION` 独立演进。Mock Coding / Fake Observer 只在示例和测试中，不作真实模式回退。

## 当前能力边界

默认编程交付仍限定 **Python 3.12 项目 + 附属 HTML/JS/CSS**，最多 80 文件 / 500 KB，以平台无网络沙箱的 unittest 报告验收。OpenCode 可以在自己的隔离目录编辑源码，最终导出文件 bundle；它的“测试通过”文字不能替代平台报告。没有自动 npm 安装、通用 Shell、完整桌面 VM、复杂 Node 项目构建或网页交互验收。模型生成的测试仍需要审核其覆盖质量。

内置 OpenCode 配置拒绝 shell、外部目录、MCP 和网页工具，允许项目内文件工具；自身模型 SDK 初始化可能联网下载依赖。编码容器只持有专用编程模型凭据，不持有 QQ/管理员/数据库/浏览器/网关密码或其他项目数据。Docker 网关是受信任控制器，持有 Docker Socket；编码容器不持有该 Socket。模型服务的计费上限应在专用账号侧设置；平台工作请求预算不等于 OpenCode 内部的 token 账单上限。

Observer 审查是模型意见，`reviewed` 不表示经过运行测试或安全认证。Reply 的 brief 引用检查能拦截编造证据 ID，不能从数学上保证模型语义正确。当前没有训练和校准真实本地 Decision 分类模型；可替换实现 `DecisionProvider`。

后台新增管理接口仅向 owner 开放，QQ/工具严格按原发起人与会话检查；没有多租户 RLS 或项目协作者邀请系统。私聊只有用户原文明确 `/研究 ...` 才授权新研究外发；群研究还需 `research=true` 和 `research_external=true`，所有输入均先去身份化检查。原始个人经历不用于公开网页核实。

只持久化公开动作摘要、工具轨迹与证据，不保存模型内部思维链。不采集消息的会话不保留新生成的 brief 正文；明确创建的代码项目仍作为私有工作资产保存。删除/撤回会清理会话、回执和审查，并排队删除对应远端会话；内置网关进一步删该项目容器及两份卷。外部自建 OpenCode 的磁盘和独立备份由部署者管理。

## 未完成的实机验收

本环境没有构建/运行 OpenCode 镜像，没有真实模型、QQ、DeepSeek 当前 DOM、Docker 沙箱、音轨 FFmpeg/ASR、PostgreSQL 或 4C8G 压测。测试中的 OpenCode 是 HTTP MockTransport，网关检查是 ASGI/命令构造检查。完整实测范围见 [verification.md](public-release.md)。

因此本版提供可部署的工程和默认可用执行路径，也提供锁版本 OpenCode 接线与隔离配置；**尚不能声称 V3 最小演示已在两个真实 QQ 群验收通过**。
