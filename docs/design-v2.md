# OpenSocialAgent：可扩展多群 QQ AI 社交 Agent 升级版完整技术方案

> **文档类型**：架构设计 / 需求规格 / 技术实施 / 开源工程规划  
> **版本**：Design V2.0（完整升级版；面向逐步实现，不表示功能已完成）  
> **编制日期**：2026-10-15  
> **项目代号**：OpenSocialAgent（暂定，可更改）  
> **运行目标**：基于 NapCat 接入多个 QQ 群、私聊及可选 QQ 空间；以统一人格、自然聊天、跨群授权记忆、多模态理解、知识学习、隔离式虚拟电脑为基础，并纳入**社交/工作双运行时、持久任务与进度询问、经验踩坑记忆、夜间 DeepSeek 研究验证**。  
> **总体技术选择**：Python 3.12+ / asyncio / FastAPI / PostgreSQL / pgvector / Redis（可选，生产任务队列建议）/ S3 兼容对象存储 / Vue 3 + TypeScript / Docker Compose。  
> **文档适用对象**：项目发起人、核心维护者、后端/前端/AI/安全开发者、开源贡献者。  
> **文档状态标记**：`[确定]` 表示架构决定；`[拟定]` 表示默认建议；`[待验证]` 表示受运行版本、第三方接口、账号权限、法律或服务条款影响。

**升级阅读说明**：本文件完整保留第 0～29 章的基础架构，并在第 30～45 章补入 V2 专项设计。**发生冲突时，第 30～45 章优先**。所有代码、接口和数据库例子均是待实现的规格或示例，不代表项目已上线。

### V2.0 升级速览

| 系统 | V2.0 最终决定 | 对应章节 |
|---|---|---|
| 社交与工作 | 双运行时隔离；工作时不主动水群，但仍回应 @/任务进度 | 31～33 |
| 长任务 | Task Registry + 状态机 + 持久命令 + Git/证据检查点 + 租约防双写 | 32～33 |
| 经验记忆 | 自己执行程序和群友讨论都能形成“问题—尝试—结果”案例 | 34～35 |
| 夜间研究 | 天记忆巩固联动 Research Engine、DeepSeek、联网资料及主张验证 | 36～37 |
| 跨群权限 | 可分享通用经验，但私聊/群文件/任务进度不自动跨群披露 | 38 |
| QQ +1 | 现有独立模块实现；Bot **只识别队形并静默，绝不参与 +1** | 39 |
| 管理后台 | 实时任务进度、经验搜索/审核、研究证据审计、聊天评价回放 | 40 |
| 工程可扩展 | V2 SQL 增量、插件接口、配置、部署、测试与开源里程碑 | 41～45 |

**推荐阅读路径**：产品/开源贡献者先看第 0、2、30、44 章；后端开发看 3、12、13、31～33、41～42 章；记忆/AI 开发看 5～9、34～37 章；安全开发看 6、10、16、38、39、43 章；前端开发看 14、40 章。  

---

## 0. 一页摘要

**产品定位**：不是收到一条消息就回复的命令型机器人，而是跨多个 QQ 群存在的**同一个 AI 社交身份**。它具备自然的发言节奏、个性化但可审计的行为、对本人授权信息的跨群记忆、群内知识检索、看图/看视频/发适合的表情、外部联网研究，以及能安全运行代码和截图的独立虚拟电脑。

**第一优先级**：自然、不打断别人、不泄露记忆、不自作主张执行高风险动作。

**产品主原则**：

1. **一个身份、多场景**：全局人格和可共享的知识统一；各群即时对话上下文独立；个人跨群回忆以用户身份和授权规则为边界。
2. **允许沉默**：机器人没有义务回复每条消息；不夸张、不过度赞美、不强行接梗、不刻意炫耀记忆。
3. **记忆不是聊天记录**：原始消息、当天情景记忆、长期记忆、知识库和实践技能是不同资产；向量是检索索引，不是事实真相。
4. **思考与行动分离**：模型给出决策/计划，程序执行权限校验；后台展示可审计的决策摘要和工具轨迹，不依赖暴露模型隐含推理。
5. **权限先于检索**：跨群披露、知识查阅、文件下载和工具执行，均通过程序实现权限检查；绝不只靠提示词。
6. **扩展接口优先**：平台、模型、检索、记忆、媒体、工具、技能、社交行为、管理后台均可替换；社区插件遵守稳定 API 与权限声明。
7. **风险分级**：读取群公告、读取授权群文件与发群消息属于不同权限；删文件、发公告、空间发帖、上传/转发和运行不可信代码需要更严格审核。
8. **开源可持续**：不捆绑某一模型、不把非开源依赖复制到自己的仓库；测试、文档、贡献规范、迁移和升级机制从第一版建立。

### 0.1 范围与不做什么

**计划支持**：群聊、私聊、多群调度、全天消息留痕与检索、消息评价、主题跟踪、主动接话、天记忆、夜间巩固、长期/人物/群记忆、授权跨群回忆、知识库、群文件/群公告读取、多模态和表情包、网页研究、受限虚拟电脑、空间适配器、后台运维。

**不做**：通过未授权渠道绕过验证码/风控；默认自动抓取所有好友/群文件；把群公告或文档当作系统指令；未经同意跨群发布他人信息；普通群成员直接取得宿主机 Shell 或数据库权限；自动把聊天中出现的代码升级成永久技能；为了互动率故意引战。

### 0.2 初步能力优先级与成熟度

| 能力 | 目标阶段 | 接口来源 / 说明 | 状态 |
|---|---|---|---|
| 群聊消息与私聊 | P0 | OneBot 11 / NapCat | 官方文档列明，需集成测试 |
| 群文件列表、文件链接 | P1 | `get_group_root_files` / `get_group_files_by_folder` / `get_group_file_url` | 官方文档列明，需权限与下载测试 |
| 群文件上传事件 | P1 | `notice_type=group_upload` | 官方事件文档列明 |
| 群公告查询 | P1 | `_get_group_notice` | 官方文档列明 |
| 群公告发布与删除 | P3 | `_send_group_notice` / `_del_group_notice` | 官方文档列明；强审批 |
| 向量检索 | P1 | PostgreSQL + pgvector | 方案确定 |
| DeepSeek 官方 API | P2 | 官方开发者平台 | 方案确定，供应商模型名动态配置 |
| DeepSeek 网页版自动化 | 实验 | 使用条款与实现方式受限制 | 默认关闭；未获授权不得自动化抓取 |
| 视觉模型 | P2 | 可配置的图片模型服务 | 接口待配置 |
| 视频理解 | P3 | 视频模型/关键帧/ASR | 供应商与费用待定 |
| QQ 空间 | P4 | 第三方适配器，兼容性不保证 | 高风险实验模块 |
| 虚拟电脑 | P3 | 独立 Sandbox Worker | 需资源隔离与审批 |

---

## 1. 用户故事与验收标准

### 1.1 普通群友

- **US-001**：在群 A 提过个人项目，过几天在群 B 对机器人说“上次那个还记得吗”，若本人授权跨群记忆，机器人能检索到相应项目并以自然方式确认，而非复述一长段背景。
- **US-002**：多人在群里交叉聊天时，机器人能识别引用和话题，必要时保持沉默。
- **US-003**：发图片并 @ 机器人请求解释，机器人能识别图片；看不清就明确说看不清，不编造。
- **US-004**：询问当前群公告、群文件在哪里、某份 PDF 讲了什么，机器人能基于**当前群授权可访问**的内容回答并给出文件名、来源、时间。
- **US-005**：提出最新版本的软件/游戏问题，机器人发现现有知识可能过期时触发独立联网研究，并注明信息更新时点与来源。
- **US-006**：要求生成截图/运行受限 Python，机器人经过工具权限检查后在沙箱完成并回传结果。
- **US-007**：告诉机器人“你记错了”“这件事别记”，可以触发明确的更正或删除流程。

### 1.2 管理员/项目维护者

- **US-101**：后台按照群、发送时间、模型、内容关键词、动作类型搜索机器人发过的每条消息。
- **US-102**：点开消息可看到前后文、触发理由、命中记忆、生成模型、工具轨迹、发送结果；对该消息标记“太人机”“强行接梗”“太啰嗦”“不该回复”“答非所问”“事实错误”“自然”等。
- **US-103**：可写更好的回复，形成偏好样本；能在冻结上下文中回放新策略，且绝不真实发往 QQ。
- **US-104**：按群配置是否收集消息、是否索引文件、是否可跨群回忆、表情包频率、主动发言上限。
- **US-105**：一键关闭主动发言、停用某插件或强制停止虚拟电脑任务；查看全局任务、错误、API 消耗。
- **US-106**：可查看/纠正/删除记忆、知识和文件索引；可导出或清理数据。

### 1.3 开源贡献者

- **US-201**：无需改动核心源代码，即可实现新的聊天平台适配器、模型 Provider、工具或文件解析插件。
- **US-202**：在没有 QQ 账号和真实大模型费用的情况下，可以用模拟 OneBot 事件、FakeModel、测试媒体完成大多数自动化测试。
- **US-203**：插件带版本化 Manifest、权限声明与契约测试；核心升级对插件采用语义化版本规则。

### 1.4 可量化验收（初期目标，非现有实测）

| 项目 | 建议验收门槛 |
|---|---|
| @ 与引用回复 | 在测试集上的正确识别率 ≥ 98% |
| 错误串话 | 标注的多话题测试集上逐步达成 ≥ 90% 正确指向 |
| 主动发言自然度 | 通过人工盲评；重要的是负面干扰率而非发言数量 |
| 权限隔离 | 跨群未授权信息泄露测试 0 通过泄露案例 |
| 消息重复 | 重连/重复事件测试下同一意图最多一次实际发送（可恢复时核实） |
| 记忆来源 | 每条长期事实都能追溯源记录或外部文档 |
| 夜间任务 | 任意节点重启后幂等继续；不重复创建事实 |
| 文件操作 | 遇到不可下载、已删除、超大、恶意格式时安全失败 |
| 沙箱 | 超时/资源配额强制中断；无宿主密钥可见 |
| 开源质量 | 格式检查、类型检查、关键单测、集成测试在 CI 中通过 |

---

## 2. 架构原则和系统边界

### 2.1 优先采用模块化单体 + 高风险 Worker 隔离

早期不使用大量微服务。`app` 核心是单个 Python 进程或少量相同实例，通过清晰模块边界组织；隔离要求高的代码执行器和浏览器执行器运行在独立容器/虚拟机。随着规模增大，再拆成独立服务而不更改域接口。

```mermaid
flowchart TB
  QQ[QQ 群/私聊] --> NC[NapCat / OneBot 11]
  QZ[QQ 空间可选] --> QZA[QZone Adapter]
  NC --> GW[Platform Gateway]
  QZA --> GW
  GW --> EB[Event Normalizer / Inbox / Event Bus]
  EB --> CT[Conversation & Topic Tracker]
  CT --> DE[Social Decision Engine]
  DE --> AR[Agent Runtime & Model Router]
  AR <--> MEM[Memory + Knowledge Retrieval]
  AR <--> RES[Research Providers]
  AR <--> MM[Multimodal Providers]
  AR --> TA[Tool Authorization]
  TA --> SB[Sandbox Workers / Browser Workers]
  AR --> OUT[Outbox & Send Scheduler]
  OUT --> NC
  OUT --> QZA
  EB --> DB[(PostgreSQL + pgvector)]
  MEM <--> DB
  MM <--> OBJ[(S3 / MinIO)]
  SB <--> OBJ
  ADMIN[Admin Web / API] <--> DB
  ADMIN --> CTRL[Policy / Review / Replay]
  CTRL --> DE
  SCHED[Nightly Jobs / Sync Jobs] --> MEM
  SCHED --> EB
```

### 2.2 核心分层

1. **Adapter**：接入外部事件，转换成统一事件，转换发送动作；不持有聊天业务逻辑。
2. **Application**：处理命令、触发工作流、调度任务、保证事务和幂等。
3. **Domain**：社交决策、记忆、知识、权限、工具策略、评价规则；不依赖第三方 SDK。
4. **Infrastructure**：PostgreSQL、pgvector、对象存储、OneBot WebSocket、模型 SDK、队列、缓存。
5. **Plugin SDK**：稳定的协议和能力权限，以扩展点取代核心硬编码。
6. **Admin UI**：配置、审计、人工评价、回放、监控和数据治理。

**依赖方向**：外层可以依赖内层接口，内层绝不直接 import NapCat/DeepSeek/某数据库的 SDK。域服务通过 Port / Protocol 调用基础设施适配器。

### 2.3 需要长期稳定的边界接口

- `PlatformAdapter`：监听事件、发送消息、读取群信息、枚举可用能力。
- `MediaProvider`：图像/视频/音频理解与标准结果。
- `ModelProvider`：聊天/决策/推理/Embedding/Rerank。
- `MemoryRepository` / `KnowledgeRepository`：读写与可见域过滤。
- `RetrievalProvider`：向量、关键词、融合与排序。
- `ToolExecutor`：执行经授权的工具任务，返回结构化结果。
- `PermissionPolicy`：资源访问与发言可披露性判定。
- `SocialActionProvider`：点赞、评论、空间等非消息行为。
- `PluginManager`：发现、版本校验、激活、依赖和事件订阅。

---

## 3. NapCat / OneBot 接入详细设计

### 3.1 连接与通信

- **优先模式**：OneBot 11 反向 WebSocket（NapCat 主动连接应用服务）。
- **隔离**：WebSocket 仅绑定可信内网；启用 Access Token，反向代理必要时加 TLS 和 IP 限制。
- **状态机**：`DISCONNECTED → CONNECTING → AUTHENTICATING → READY → DEGRADED → RETRYING`。
- **重连**：指数退避 + 抖动；重连后查询可用的必要状态，但不盲目重放已发送动作。
- **运行时能力检测**：通过版本、支持 API 集合、可用角色和实际调用结果建立 `CapabilityRegistry`；不因接口存在就认为当前机器人有群管理权限。
- **消息 ID**：QQ ID 和 OneBot ID 以字符串处理，避免跨语言整数溢出及实现差异。
- **原始事件保留**：保存脱敏后的 Raw Payload 与 Schema 版本，用于兼容性排错和事件回放。

### 3.2 统一事件结构（平台无关）

```json
{
  "event_id": "evt_01J...",
  "schema_version": "1.0",
  "platform": "qq_onebot11",
  "adapter_instance": "napcat-primary",
  "event_type": "message.created",
  "platform_event_id": "group:123456:msg:78900",
  "conversation": {
    "kind": "group",
    "id": "123456"
  },
  "sender": {
    "platform_user_id": "10001",
    "global_identity_id": "usr_..."
  },
  "message": {
    "platform_message_id": "78900",
    "reply_to_id": null,
    "segments": [
      {"kind": "text", "text": "今天群文件有更新吗"}
    ]
  },
  "occurred_at": "2026-10-15T13:00:00+08:00",
  "received_at": "2026-10-15T13:00:01+08:00",
  "trace_id": "trace_..."
}
```

**事件类型建议**：`message.created`、`message.recalled`、`member.joined`、`member.left`、`group.role_changed`、`group.file_uploaded`、`group.notice_snapshot_changed`（轮询衍生）、`platform.connection_changed`、`media.analysis_finished`、`tool.job_finished`、`knowledge.index_updated`、`user.memory_consent_changed`。

**事件交付语义**：按“至少一次”设计；平台可能重复上报。因此事件处理器须依据 `(adapter_instance, platform, platform_event_id, event_type)` 计算可重现 dedupe key，入口实现唯一约束；对缺失稳定 ID 的事件用内容哈希 + 时间窗口去重。

### 3.3 OneBot 操作统一封装

```python
from typing import Protocol, Any

class QQActionClient(Protocol):
    async def call(self, action: str, params: dict[str, Any]) -> dict: ...

# 所有 action 走一个可替换 client；不要在业务模块里散写 WebSocket send。
```

动作执行必须记录 `action_request_id`、`echo`、请求参数的脱敏副本、超时、返回值、错误码、重试次数和最终 QQ 消息 ID。无法确认某次发消息是否成功时，避免无条件重发；先尝试关联 echo 和平台历史或按“结果不确定”记录供人工判断。

### 3.4 群文件与群公告可用接口（2026-10-15 查阅）

| 需求 | NapCat OneBot Action | 关键参数（文档所列） | 默认策略 |
|---|---|---|---|
| 读取群根目录文件 | `get_group_root_files` | `group_id` | 授权群只读 |
| 读取文件夹 | `get_group_files_by_folder` | `group_id`, `folder_id` | 授权群只读 |
| 查询群文件系统 | `get_group_file_system_info` | `group_id` | 授权群只读 |
| 获取文件资源 URL | `get_group_file_url` | `group_id`, `file_id`, `busid` | 需校验来源和 URL |
| 群文件上传通知 | `notice_type=group_upload` | event 内含 `file.id`, `file.name`, `file.size`, `file.busid` | 触发增量索引任务 |
| 上传文件 | `upload_group_file` | `group_id`, `file`, `name`, 可选 `folder` | 写操作，审批 |
| 删除文件 | `delete_group_file` | `group_id`, `file_id`, `busid` | 默认关闭 |
| 创建文件夹 | `create_group_file_folder` | `group_id`, `name` | 管理角色/审批 |
| 读取群公告 | `_get_group_notice` | `group_id` | 授权群只读 |
| 发布群公告 | `_send_group_notice` | `group_id`, `content` 等 | 强审批/权限要求 |
| 删除群公告 | `_del_group_notice` | `group_id`, `notice_id` | 默认关闭 |
| 获取精华消息 | `get_essence_msg_list` | `group_id` | 群知识候选源 |

**注意**：以上来自官方接口清单；字段可选项、返回结构、权限和运行差异应以实际 NapCat 版本及在线测试为准。部分 OneBot 实现的扩展 Action 命名不通用，所以严禁把 `_get_group_notice` 当成 OneBot 11 全平台标准接口。必须做能力探测、失败降级与契约测试。

官方参考：

- NapCat API 清单：<https://napneko.github.io/onebot/api>
- NapCat 事件文档：<https://napneko.github.io/onebot/event>
- NapCat 兼容接口：<https://napneko.github.io/develop/api>
- 文件处理文档：<https://doc.napneko.icu/develop/file>


## 4. 群文件与群公告子系统（新增重点）

### 4.1 产品目标

一个群里的“文件”和“公告”应当像可检索的群内资料库：用户可以问“入群须知是什么”“上周上传的数学 PDF 在哪”“群文件中的安装步骤怎么写”。机器人通过获得许可的文件索引和公告快照回答，而不是每次都下载全部文件。

**必须清晰区分**：

- **元数据索引**：文件名、所在文件夹、大小、上传者、上传时间、file_id、busid、状态、最后同步时间。
- **内容索引**：提取的文档正文、切片、向量及引用位置信息；需要单独的“允许索引内容”授权。
- **公告索引**：公告 ID、文字、作者（若可取得）、发布时间、最后观察时间、状态和版本。
- **管理操作**：上传、删除、重命名、发布公告，均属于写操作，不能跟只读问答共用权限。

### 4.2 群文件扫描工作流

```mermaid
sequenceDiagram
  participant Scheduler as Sync Scheduler
  participant Adapter as NapCat File Adapter
  participant DB as PostgreSQL
  participant Policy as Group Resource Policy
  participant Worker as Document Worker
  participant Object as Object Storage
  participant KB as Knowledge Index
  Scheduler->>Policy: 检查群授权/同步策略
  Policy-->>Scheduler: 允许读取元数据
  Scheduler->>Adapter: get_group_root_files(group_id)
  Adapter-->>Scheduler: 文件/文件夹列表
  loop 有权限且未超限的目录
    Scheduler->>Adapter: get_group_files_by_folder(...)
    Adapter-->>Scheduler: 子目录列表
  end
  Scheduler->>DB: Upsert 文件元数据 / 标记缺失候选
  alt 已许可内容解析且文件发生变化
    Scheduler->>Adapter: get_group_file_url(...)
    Adapter-->>Scheduler: 临时资源 URL
    Scheduler->>Worker: 安全下载任务(限制大小/域名/次数)
    Worker->>Object: 保存检验后的原始资源
    Worker->>KB: 解析文本、分片、索引
    Worker->>DB: 更新状态和溯源
  end
```

推荐同步策略：

1. 初次启用只扫描文件元数据，不自动下载全部文件。
2. 通过 `group_upload` 通知实时入队**单文件更新任务**。
3. 每 6～24 小时运行一次增量/全量元数据核对（按群规模可配置）。
4. 记录扫描游标、父目录路径、文件 ID、大小、上传时间；对于没有稳定修改时间的文件，增加 `sha256` 作为已下载内容版本标识。
5. 本次扫描中未出现的文件先标记 `missing_candidate`；经过重复确认，再变更为 `removed`。避免接口短暂异常导致大规模误删。
6. 单次处理最多 N 个文件，按群限流；超大文件、压缩包、执行文件不自动解析。
7. `get_group_file_url` 产生的资源链接可能过期或有限次下载，不应该永久保存为访问凭据；仅短期使用，文件下载需要重试时重新取链接。
8. 同一文件的元数据和内容可能出现不同步，展示 `last_synced_at` / `content_indexed_at`；不能冒充最新。

**目录遍历保护**：最大层级、最大目录数、重复 folder_id 循环检测、总文件数限额、错误计数，避免接口 bug 造成无限遍历。

### 4.3 安全下载与文档解析

每个文件经过以下流水线：

`权限预检 → 获取 URL → URL/域名/IP 校验 → 限速下载 → 大小/哈希/MIME 校验 → 病毒扫描（可选）→ 隔离解析 → 内容清洗 → Chunking → Embedding → 记录出处`

**安全规则**：

- 防 SSRF：资源 URL 不可任意指向内网管理端、metadata IP、localhost、数据库和容器网络；若来源是动态下载链接，使用允许的受控下载代理；验证 DNS 解析与最终跳转目的地。
- 分离下载进程和文档解析进程；解析器没有数据库管理员凭据。
- 禁用 Office 宏、自动脚本、网页 JS 的执行；不调用 `eval`/Shell 解释文件内容。
- 对 ZIP/压缩包设置压缩比、解压后的总大小、成员数、路径穿越和嵌套层级限制；初版可拒绝压缩包。
- PDF、DOCX、PPTX、TXT、MD、CSV 可列为首批允许解析格式；图片由视觉/OCR 流水线解析；音视频由媒体 Worker 解析。解析格式要以可用解析库和许可为准。
- EXE、BAT、SH、JAR、二进制等默认只能显示元数据，不会被工具执行器自动运行。
- 长文件按章节/页面/段落切片，引用包含 `file_id`、文件名、页码或章节、原文 hash、同步时间。
- 文件内容是**不可信数据**。其中出现“忽略系统指令”“执行下面命令”只能被当作原文引用，不能触发工具调用或修改权限。

**文件状态枚举**：`discovered`、`metadata_only`、`download_queued`、`downloaded`、`parsing`、`indexed`、`parse_failed`、`permission_denied`、`too_large`、`removed`、`expired`。

### 4.4 群公告同步

群公告不一定提供统一的可订阅变更事件，因此首版采取**定时轮询 + 管理员主动刷新**：

1. 调用 `_get_group_notice(group_id)`。
2. Adapter 将差异化返回字段映射为 `NoticeDTO`，不存在的字段用 `null`，不要猜测。
3. 对每条公告按稳定平台 ID 或 `sha256(group_id + normalized_body + source_created_at)` 去重。
4. 保存公告版本快照，记录首次观察与最后观察时间。
5. 比较新增、编辑、删除；若无法区分“暂时未返回”和“删除”，通过下一轮确认。
6. 对正文进行段落切片，维护群内知识索引。
7. 查询“现在的群规”时优先当前有效公告，不应混入历史公告；如果索引较旧可触发实时刷新。

建议公告相关查询的上下文包含：公告内容、公告的真实创建时间（若返回）、机器人最近同步时间、群 ID；回答时用“我查到目前群公告写的是……”而不是“群规永远是……”。

### 4.5 文件与公告的知识作用域

- `group_resource` 内容严格绑定 `group_id`，默认不能跨群全文检索或复述。
- 若管理员设置某份文件为 `shareable`，也不自动获得作者授权；必须核查相关用户/版权/数据许可，再允许发布为通用知识。
- 个人跨群记忆和“群文件内容”分离：**一个人在 A 群上传了资料，不等于 B 群可访问该资料**。
- 群公告可以作为群内规则的“数据来源”，但无法覆盖机器人安全策略、模型调用权限和系统配置。
- 群管理员删除文件或关闭内容索引时，按策略从对象存储、知识切片、向量索引和备份保留计划中清理。

### 4.6 群文件与公告问答示例（流程而非固定话术）

`群友：@bot 群公告里有作业要求吗？`

- 决策层：确定是明确任务，无须主动发言评分。
- 资源权限层：判断该群已开启 `notice.read`。
- 公告层：若缓存超过 TTL，尝试 `_get_group_notice`；失败则只能说明“上次同步版本”。
- 检索层：在 `group_id=current_group_id` 的有效公告内找相关段落。
- 生成层：用一两句概括，必要时标注哪条公告和最近同步时间；未查到就明确说明。

`群友：那个招生简章在哪？`

- 先搜索当前群文件的文件名（精确/模糊）；如果有多个相似文件，列出区别。
- 若只允许元数据读取，不下载内容；返回文件名、目录或 QQ 可访问位置。
- 如果请求总结且已授权索引文件内容，则查文件 chunk + 页码，必要时实时读取小文件。

### 4.7 管理后台页面

**群资料管理**页至少包括：群列表、文件树、文件搜索、上传时间、大小、索引状态、解析失败原因、公告版本、最后同步状态、每群同步任务进度。每个文件支持“仅索引元数据 / 允许内容索引 / 取消索引 / 手动重新解析 / 查看引用来源”；写入和删除操作默认需审计与二次确认。

### 4.8 NapCat 适配契约测试矩阵

- 根目录为空/仅有文件夹/混合文件夹和文件/有隐藏或特殊字符名称。
- 多层文件夹、重复文件 ID、文件被移动/重命名、临时链接失效。
- 无权限读取某群、机器人被移出群、群角色变化。
- 群文件上传事件丢失但定时同步补回。
- 频繁更新群公告、空公告、多个公告、删除后短暂返回旧缓存。
- 文件 URL 重定向到禁止地址、超大文件、嵌套压缩包、损坏 PDF。
- 同一群中普通成员可查询但不能修改群公告/删除文件。

---

## 5. 自然社交决策系统

> **V2 补充**：群内 +1/复读仅观察并静默，不实现自动复读；主要工作任务正在执行时全局抑制主动聊天。参见第 31、39 章。


### 5.1 关键改进目标

用户对“不人机”的定义必须落实到**可测试的行为**，而不只是 Prompt 中写“自然一点”。禁止通过故意堆网络流行语、强行幽默、莫名赞叹、过度拉长聊天、每句话都提问来增加存在感。

**社交行为排序**：不打断 > 不泄漏 > 不胡说 > 能接话 > 说得自然 > 语言有趣。

### 5.2 消息处理管线

```mermaid
flowchart LR
  A[新消息] --> B[去重/类型/权限过滤]
  B --> C[当前群短期上下文更新]
  C --> D{是否明确 @ / 引用 / 提问}
  D -->|是| E[高优先级候选]
  D -->|否| F[观察窗口/话题聚合]
  F --> G[低成本决策]
  E --> G
  G --> H{IGNORE / WAIT / REACT / REPLY}
  H -->|REPLY| I[授权记忆检索]
  I --> J[意图与长度规划]
  J --> K[主模型生成]
  K --> L[自然度/事实/过期/权限校验]
  L --> M[Outbox 发送]
```

### 5.3 决策动作

- `IGNORE`：无参与价值，保持沉默。不是系统错误。
- `WAIT`：故事未讲完、需要更多上下文或话题还在变化，延迟复核。
- `REACT`：轻量表情/反应，若 QQ 版本不支持则允许退化为 IGNORE，而不是强制发文字。
- `REPLY`：回应明确请求或参与开放话题。
- `INITIATE`：仅由独立主动社交调度器提交候选，不随单个消息自动直接触发。
- `TASK`：显式请求使用模型、检索、文件、虚拟电脑等工具的任务流。

**程序硬限制**：黑白名单、管理员停用、禁止发送时段、每群/全局冷却、每话题连续发言上限、低置信度披露拦截。**模型软评分**：是否自然、是否相关、打断风险、重复程度、对方是否期待回复。

### 5.4 并行话题追踪（Thread Tracker）

关联优先级：

1. `reply_to_id` 的精确引用。
2. 显式 @ 与命名对象。
3. 同一用户连续消息和时间窗口。
4. 话题语义聚类；只有不确定时才调用小模型。

`ThreadState`：`thread_id`、`group_id`、`participants`、`summary`、`last_message_id`、`version`、`last_activity_at`、`topic_embedding_ref`、`bot_turn_count`、`closed_at`。

保存**快照版本**，避免机器人生成回复期间群聊已经进入其他话题。回复发出前做 `stale check`：检查消息仍存在、当前群可发送、目标话题未终结、机器人回复尚有关联；否则取消或在明确请求场景下转成引用回复。

### 5.5 群语言风格档案

`GroupStyleProfile` 数据可由授权样本统计和管理员修正：

- 日常消息长短分布、问号/标点/表情频率。
- 常用语的*出现频率*，而不是强制用某个网络词。
- 群聊互动的平均接话延迟、常见话题和严肃程度。
- 被明确反感的表达：强行类比、谄媚、模板赞美、夸张感叹、硬凑梗等。
- 个体说话方式只用于识别语境；不复制他人独特口癖、冒充他人身份。

群内风格分析需要聚合和脱敏；不能把单人的私密特点直接用于训练或展示。

### 5.6 生成流程：先意图，后措辞

统一结构：

```json
{
  "action": "REPLY",
  "target_thread_id": "thr_012",
  "target_message_id": "78900",
  "intent": "confirm_context",
  "semantic_goal": "确认对方说的是前几天修的乘法器",
  "style_profile_id": "group_123456_v4",
  "length_preference": "brief",
  "allowed_memory_ids": ["mem_100"],
  "requires_tools": false
}
```

主模型只负责把 `semantic_goal` 变成语境合适的一句话/几句话，**不要**被提示“必须幽默”“必须像年轻人”“必须提及所有检索记忆”。引用历史应当克制，比如“乘法那部分弄好了？”就足够。

### 5.7 自然度检验器 Naturalness Evaluator

输出 `accept/rewrite/drop` + 分类原因：过度热情、强行接梗、复述背景、机械提问、刻意长篇、事实错误、不恰当主动发言、不合群风格。`drop` 是合法结果；一条普通主动插话失败时宁可不发，也不反复生成五六轮。

**使用策略**：初期仅对主动插话或风险较高的回复进行检测，避免把每条消息都变成双倍模型费用。负面检测器不能简单把“短句”视为自然，更不能为追求符合口癖而用攻击性语言。

### 5.8 主动聊天调度器

- L1：正在进行的开放话题里**适度加入**（优先）。
- L2：上下文相关时自然延续过去的群体讨论。
- L3：冷场时提议新话题（默认关闭或低频开启）。
- 定时检查只生成候选，**不强制发言**。
- 观察互动反馈；连续无人回应可以降主动性，明确表示不想被打扰时立即停止。
- 群级和全局双限流，主动发送预算与被 @ 的任务预算分开。
- QQ 空间点赞不是“主动聊天”的默认替代动作，不可为了维持活跃而批量点赞。

**建议初始配置（待群内实际验证）**：观察窗口 2～5 秒、每群自然接话最短间隔 3～8 分钟、每 15 分钟普通主动发言最多 3 次、机器人连续普通发言最多 2 条。被直接 @ 不走普通主动冷却，但仍受安全和整体速率限制。

---

## 6. 统一身份与跨群记忆权限模型

### 6.1 核心身份

- `AgentIdentity`：机器人唯一全球身份、人格基本原则、稳定偏好与已完成项目。
- `PlatformIdentity`：平台 + 用户 ID 对应的身份；在 QQ 中以稳定 QQ 用户 ID 为主键，昵称只作显示数据。
- `GroupMembership`：用户在群中的本地角色、昵称、加入时间、互动上下文。
- `RelationshipState`：机器人和用户在某群或全局的互动阶段（非真实情感或诊断）。

**同一个 QQ 号**在 A、B 群可以链接到同一个用户对象。但仍不能推断这个人授权了跨群传播。

### 6.2 记忆类型与共享规则

| Scope | 示例 | 默认可检索范围 | 允许披露 |
|---|---|---|---|
| `working_context` | 当前群最近 50 条消息 | 当前群、当前话题 | 当前话题 |
| `daily_group` | A 群今天发生的事 | A 群 | 仅 A 群 |
| `long_group` | A 群共同梗或项目 | A 群 | 仅 A 群 |
| `global_public_knowledge` | 已验证的公开技术知识 | 所有授权群 | 可按知识许可使用 |
| `global_personal_owner` | 用户自行授权跨群回忆的个人兴趣/项目 | 同一用户自我查询，前置身份核验 | 看当前场景是否适合公开 |
| `private_user` | 私聊中的事情 | 该用户与机器人的私聊 | 默认不进群 |
| `group_resource` | A 群文件、群公告 | A 群授权上下文 | 默认不跨群 |
| `agent_experience` | 机器人自己运行过的任务 | 机器人内部 | 仅安全、真实、非敏感成果 |

### 6.3 必须拆开的两种授权

1. **记忆访问授权** `can_retrieve`：引擎内部能否检索某条记忆。
2. **对外披露授权** `can_disclose`：某条记忆能否发送到当前群/目标用户。

第一种许可**不能推出**第二种许可。例如用户问“你记得我私聊里说的是什么吗”，在大群里也不应直接复述；可以提示对方私聊询问或给出不涉及内容的确认。

另需 `can_store`、`can_vectorize`、`can_share`、`can_export`、`can_delete`，分别限制保存、生成向量、跨群传播、导出及删除。

### 6.4 权限判定算法（强制后台校验）

```python
async def authorize_memory(candidate, *, actor_id, destination, purpose, policy):
    if candidate.status != "active":
        return False
    if candidate.expires_at and candidate.expires_at <= now():
        return False
    if candidate.scope == "global_public_knowledge":
        return policy.public_knowledge_enabled(destination)
    if candidate.scope == "group_resource":
        return candidate.origin_group_id == destination.group_id and \
               policy.group_resource_read_allowed(actor_id, destination.group_id)
    if candidate.scope in {"daily_group", "long_group"}:
        return candidate.origin_group_id == destination.group_id
    if candidate.scope == "private_user":
        return destination.kind == "private" and \
               candidate.subject_user_id == actor_id
    if candidate.scope == "global_personal_owner":
        return candidate.subject_user_id == actor_id and \
               policy.has_cross_group_consent(actor_id) and \
               policy.disclosure_allowed(candidate, destination)
    return False
```

代码是**原则示例**，实际权限判断必须由数据层 query predicate + domain policy 双重实现，防止先检索再过滤时将敏感内容塞入模型上下文。模型最终生成消息后还需要输出披露检查，防止总结或改写泄漏。

### 6.5 跨群记忆的用户控制

- 按用户开启/关闭跨群自我记忆（默认关闭，用户明确同意后开启）。
- 允许选择“仅兴趣偏好”“个人项目”“群公共事实”等细粒度类别。
- 查看“我被记住了什么”、更正某条事实、撤销共享、申请删除。
- 用户撤销跨群权限后，新的检索必须立即被拒绝；物理删除或备份过期按公开保留策略执行。
- 群管理员可设置“群资料不得被用作跨群知识来源”。

### 6.6 QQ 聊天撤回与删除

- 收到撤回事件：更新原始消息 `recalled_at`，对仍在运行的引用任务执行取消检查。
- 已据此生成的记忆与知识需能通过 `source_links` 反向查找并重新审查，不得继续以撤回内容作为唯一证据。
- 群或用户删除请求需传播至：原始消息 → 天记忆 → 长期记忆 → embedding → 媒体对象 → 搜索索引 → 缓存 → 可清理备份。
- 历史审计日志保留必要的操作事实和脱敏 ID，但不无限保存已要求删除的明文。


## 7. 天记忆、夜间巩固、长期记忆与知识库

> **V2 补充**：本章基本巩固流程仍适用；夜间整理须接入“经验案例 + 不确定性检测 + 有来源的 DeepSeek 研究”增量管线，参见第 34～37 章。


### 7.1 四类信息资产

1. **原始事件与消息** `RawEvent / Message`：真实来源、时间、引用关系、消息段，按群设置保存期限。
2. **工作记忆** `WorkingMemory`：当前线程最近 30～80 条相关内容和压缩摘要，短时缓存，按群/线程隔离。
3. **天记忆** `DailyEpisode`：按日期、群、话题聚合的事件卡片，包含起止时间、参与者、可信度、开放问题、源消息 ID。
4. **长期记忆** `LongTermMemory`：稳定的人物偏好、项目进展、重要的群体历史、可复用的自身经验。
5. **知识库** `KnowledgeDocument / KnowledgeChunk`：来自公开资料、群文件（群作用域）和已核实的群聊知识。知识与人物经历在 schema 和授权上独立。

> 长期记忆可以按置信度与时效分级；“夜间整理”不是仅有的持久化入口。当用户直接确认了长期偏好，可以实时生成候选记录，晚间再去重巩固。

### 7.2 天记忆结构

```json
{
  "episode_id": "ep_20261015_g123_t42",
  "group_id": "123456",
  "local_date": "2026-10-15",
  "timezone": "Asia/Shanghai",
  "thread_id": "thr_42",
  "title": "某用户在修复自己的编程项目",
  "summary": "用户解释了卡住的位置，其他群友提出了修复建议。",
  "participant_ids": ["10001", "10002"],
  "source_message_ids": ["msg_1", "msg_8", "msg_9"],
  "open_loops": ["尚未确认问题是否解决"],
  "started_at": "2026-10-15T10:00:00+08:00",
  "ended_at": "2026-10-15T10:25:00+08:00",
  "importance": 0.63,
  "confidence": 0.90,
  "status": "active"
}
```

`open_loops` 用于“之前说要做、还没确认完成”的事件，主动聊天只能在真正相关时调用，不能每天追问。不要保存“群友明天考试”这样的相对时间而不保存绝对时间，应解析为具体日期并保留原语句供复核。

### 7.3 夜间巩固（Nightly Consolidation）完整流水线

时间建议按每群配置，例如默认 `03:30 Asia/Shanghai`，以“群当地自然日”为单位运行。跨过午夜但仍在继续的线程按事件时间保留链接。即使机器人关机，重启也要补跑未完成的日期。

```mermaid
flowchart TD
 A[选定 group + local_date] --> B[读取当天 Episode / 源记录]
 B --> C[提取候选事实/事件/知识/经验]
 C --> D[隐私与存储范围审查]
 D --> E[查重与检索旧记忆]
 E --> F{判定操作}
 F -->|INSERT| G[新建 candidate]
 F -->|MERGE| H[合并证据与摘要]
 F -->|REVISE| I[新版本替代旧版本]
 F -->|EXPIRE| J[标记失效]
 F -->|SKIP| K[不写入长期]
 G --> L[事务提交+Outbox]
 H --> L
 I --> L
 J --> L
 L --> M[异步更新向量与全文索引]
 M --> N[记录 job checkpoint / QA 采样]
```

**详细判定规则**：

- **INSERT**：确实是新信息，有来源、主体、范围，保留价值明显。
- **MERGE**：与旧事实相同，只补充新证据、新日期、多个来源。
- **REVISE**：原有偏好或项目状态变化。旧版本保留审计关系 `superseded_by`，正常检索排除被替代版本。
- **DISPUTE**：来源冲突时不强制合并，标记冲突并可等待用户确认。
- **EXPIRE**：具有过期时间的事件转入历史事实或自然失效。
- **SKIP**：闲聊噪音、无法确定主体的玩笑、一次性无关内容。

夜间任务只需要相对可靠的**操作建议**，数据库更新由程序做 schema validation、授权过滤与事务控制。模型不能直接通过生成任意 SQL 修改库。

### 7.4 任务幂等和失败恢复

`ConsolidationJobKey = hash(group_id + local_date + pipeline_version)`；数据库唯一约束保证同一天同版本不会重复运行两个互相冲突的作业。每个作业保存：`job_id / state / input_watermark / next_episode_cursor / completed_count / error / started_at / finished_at`。针对每条候选事实可生成 `candidate_hash`，在事务内写入并记录 `consolidated_by_job_id`。

模型超时：重试少量次数、指数退避；一直失败则保留 Episode 等次日或人工重试，**不得因为一个夜间任务失败而删除当天消息**。

### 7.5 记忆数据状态机

`candidate → active → disputed/superseded/expired → deleted`。允许重新核实后从 `disputed → active`；`deleted` 使用软删除标记并启动物理清理任务，普通检索必须立即排除。

每条记忆至少有：`subject_identity_id`、`origin_scope`、`group_id nullable`、`memory_type`、`content`、`source_links`、`confidence`、`importance`、`valid_from`、`valid_until`、`created_at`、`updated_at`、`last_verified_at`、`status`、`consent_policy_id`、`embedding_ref`。

### 7.6 知识记忆不等于“别人说过的话”

知识的入库源按等级标注：

- `official`：官方技术文档、版本更新说明、有明确出处的公开一手资料。
- `curated`：由项目维护者审核的资料。
- `community_claim`：群聊里某人给出的观点或技术描述，尚未核实。
- `agent_observation`：机器人自己沙箱实验中**实际观察**到的结果，明确环境和运行条件。
- `external_ai_answer`：外部模型提供的答案；不能仅凭这一来源升格为“已验证事实”。

知识字段：`topic`、`title`、`body`、`source_uri`、`source_license`、`source_type`、`source_author`、`scope`、`applicable_version`、`verified_at`、`freshness_ttl`、`trust_status`、`claims`、`citations`。

知识可信状态：`unverified`、`corroborated`、`verified`、`disputed`、`obsolete`。对“最新新闻”“游戏最新价格”“软件新功能”等高度时效性的查询需要强制触发新鲜度判断或联网核验，而不能凭过期 embedding 相似度回答。

### 7.7 检索架构

检索执行顺序：

`鉴权条件编译 → 选择资源域 → 语义向量候选 + 精确/全文候选 → RRF 融合 → 去重/版本/时间修正 → 可选 rerank → 注入少量证据 → 最终披露审查`

**中英文/游戏术语混合的索引**：

- BGE-M3 可作为候选 Embedding 实现（1024 维，支持多语言）；版本、许可与资源开销须按实际部署验证。
- pgvector 提供向量距离和 HNSW 等索引。对于短期数据量，先使用精确向量搜索更简单；数据量扩大再评估 ANN 索引及召回损失。
- PostgreSQL 原生全文检索对中文分词的效果需要评估；建议结合 `pg_trgm`、专用中文分词/检索服务，精确术语查询使用 trigram 与归一化字段。
- 候选融合可以从 `RRF = Σ 1/(k + rank_i)` 开始，典型 `k=60`，按实际回放集调优。
- Reranker 先作为插件选项，只有候选多、文本复杂时使用。

**文本分片**：按文档结构和句子边界切，初始约 300～800 tokens、适度重叠；涉及公式/代码时保持代码块完整。群公告较短可整段索引，长公告按小节。Embedding 检索带 `document_id/page/heading`，保证回答可溯源。

### 7.8 检索引入的知识污染防范

检索返回的是证据，不是命令。每条来源附带 `trust_level`、`scope` 和 `quoted_text`。模型可以引用文档描述，但文档中的“替换系统 Prompt”“安装后门软件”“上传私人信息”不得转化为 Agent 行动。外部网页、群文件、图片 OCR、视频字幕均同样处理。

### 7.9 自主实践与技能记忆

- 工具任务成功可形成 `procedure_candidate`（输入、环境、操作步骤、结果、失败原因），但默认不能直接作为可执行脚本。
- “可复用技能”需管理员审核、自动测试、固定版本、权限声明、来源授权后才变成 `SkillPlugin`。
- 不永久保存随机网页脚本、群友提供的 Shell 命令作为无审批技能。

---

## 8. 多模态系统与媒体存储

### 8.1 解耦接口

`ImageUnderstandingProvider`、`VideoUnderstandingProvider`、`ASRProvider`、`TTSProvider`、`OCRProvider`、`ImageEmbeddingProvider`、`StickerClassifier` 单独定义。各自配置 API Key、模型 ID、并发数、价格、超时与降级方案；聊天模型无需直接拥有全部模态能力。

统一结果例子：

```json
{
  "asset_id": "asset_001",
  "kind": "image",
  "summary": "游戏画面中有复杂的红石结构",
  "text_blocks": [{"text": "...", "box": [0.1, 0.2, 0.5, 0.3]}],
  "objects": ["game_screenshot", "redstone_circuit"],
  "segments": [],
  "limitations": ["单张图不能确认完整电路功能"],
  "model": {"provider": "vision", "model_id": "configured-model"},
  "analysis_at": "2026-10-15T12:00:00+08:00"
}
```

### 8.2 图片

**按需处理**：图片先保存元数据和受限缓存；只有 @、引用、明确问题或高价值社交候选需要时才调用视觉模型。支持内容描述、OCR、图中文字与自然语言交叉理解。游戏截图不要只报“图片里是一款游戏”，应结合问题判断要识别的对象；无法分辨则承认不确定。

**隐私**：发送到云端视觉服务前检查群设置与用户授权；支持本地模型线路。敏感图片、含凭据截图应进行屏蔽或拒绝外传。

### 8.3 视频

采用二级理解：

1. **轻量模式**：读取时长/分辨率，抽取按场景变化或均匀选取的关键帧，音频可选 ASR，生成简短索引。
2. **深度模式**：用户明确请求时调用支持视频理解的模型或独立帧+音频融合流水线，输出带时间戳的事件与置信度。

视频任务必设大小上限、最长时长、帧率预算和每群日预算。群友发送视频不代表机器人必须立刻看完；取消原消息任务时可撤销排队分析。

### 8.4 表情包系统

- 素材收集：获得允许的群图片，按 hash / 感知 hash 去重；作者权利与跨群共享权限记录。
- 语义分类：OCR 文字、图片主体、表达意图、适用话题、禁用场景。
- 检索：文字向量（语义）、图像相似度（视觉）、关键词与明确标签联合。
- 决策：`TEXT_ONLY`、`STICKER_ONLY`、`TEXT_WITH_STICKER`、`NO_REPLY`。
- 防刷屏：连续发送限制、同图冷却、按群单独风格策略。
- 特别注意：表情中的人像、电话号码、私人头像、聊天截图不能因为被发过一次就自动成为全局表情库。

### 8.5 语音

语音理解：媒体转码 → ASR → 文本置信度 → 与上下文融合；不确定时不能自作主张猜词。语音合成只在用户明确要求或群开启该功能时启用，支持声音身份说明及频率控制。

### 8.6 对象存储

媒体二进制进 S3 兼容对象存储；PostgreSQL 存元数据。推荐路径结构（服务内部，不直接暴露公网）：

```text
media/
  qq/group/<group_id>/<YYYY>/<MM>/<asset_id>
  qq/private/<hashed_user_id>/<asset_id>
  agent/generated/<task_id>/<asset_id>
  stickers/<visibility>/<sha256>
  documents/<group_id>/<file_version_hash>
```

原始文件的签名下载链接短期有效，后台通过后端认证后代理下载；对象密钥不可出现在大模型上下文、日志和群消息中。针对撤销授权的资产进行引用计数清理；不要将有用户隐私的对象直接放到公开 CDN。

---

## 9. 模型路由与 DeepSeek / 联网研究

> **V2 补充**：研究入口现在也服务于夜间记忆巩固及工作踩坑知识验证；外部模型的答案不能代替证据。参见第 36～37 章。


### 9.1 模型职能

| 职能 | 最初配置 | 说明 |
|---|---|---|
| `decision` | 快速低成本模型 | 发言行为与意图，优先不触发昂贵操作 |
| `chat` | 主聊天模型 | 符合群语境的自然表达 |
| `reasoning` | 可调用工具的推理模型 | 多步骤任务、编程、复杂分析 |
| `embedding` | BGE-M3 等 | 文本向量 |
| `rerank` | 可选 reranker | 检索结果重排 |
| `vision` | 可配置视觉模型 | 图片理解与 OCR |
| `video` | 可配置视频模型 | 视频与时间线 |
| `asr` / `tts` | 独立模型 | 音频 |
| `memory` | 结构化输出稳定的模型 | 夜间巩固与实体去重 |
| `judge` | 小模型/规则 | 自然度与必要性评估 |

Provider 不应绑定“ChatCompletion 一种响应格式”。定义 `LLMRequest/LLMResponse`、`EmbeddingRequest`、`MultimodalRequest` 三类契约，支持流式响应、工具调用、取消、超时、用量。模型 ID 必须可配置，因为服务商更新快。

### 9.2 研究触发条件

- 当前问题明确有“最新、今天、新版本、刚刚”等时效性要求。
- 本地知识库查不到或来源可信度低、过期。
- 两条知识来源冲突，且回答重要。
- 任务需要官方文档或其他外部证据。

不触发：简单接梗、普通问候、正在复述本人已授权的近期记忆、不值得消耗大量费用的随意闲聊。

### 9.3 Research Orchestrator

```mermaid
flowchart TB
 A[问题] --> B[检索授权知识库]
 B --> C{足够新/足够可靠?}
 C -->|是| D[组织答案]
 C -->|否| E[网络搜索 Provider]
 E --> F[来源读取/抽取/日期验证]
 F --> G[可选 DeepSeek 官方 API 分析]
 G --> H[交叉核对与冲突声明]
 H --> I[答复 + citations]
 I --> J[产生待验证知识候选]
 D --> I
```

研究器输出建议为：

```json
{
  "question": "...",
  "answer_summary": "...",
  "confidence": "medium",
  "sources": [
    {"url": "https://example.org/doc", "title": "...", "published_at": null, "fetched_at": "..."}
  ],
  "disagreements": [],
  "knowledge_candidates": [],
  "used_external_provider": true
}
```

**网页来源优先级**：官方发布和文档 > 可验证原始资料 > 可信二手总结 > 群友转述 > 外部 AI 的未引证结论。避免将来源不明的 AI 结论自动升级成“最新知识”。

### 9.4 DeepSeek 官方 API 与网页端的区别

- **推荐**：通过官方 API 获取模型回答与推理/工具能力；联网搜索由独立的 Search Provider 承担，这样可审计来源。
- **网页端**：网站提供的联网搜索并不等同于开放可自动调用的搜索 API。自动化登录、抓取和批量提交可能违反服务规则或不稳定。`DeepSeekWebAdapter` 只能作为**默认关闭的实验性、授权后才启用**的插件占位，不提供绕过验证/风控的实现。
- **故障降级**：外部模型不可用时，可以仅用本地资料回答并注明时效限制，或者暂缓回答；绝不假装“已查到”。

参考：<https://api-docs.deepseek.com/zh-cn/> 及 <https://cdn.deepseek.com/policies/en-US/deepseek-open-platform-terms-of-service.html>。

### 9.5 任务规划和可见“思考轨迹”

Agent 内部使用 `Observe → Decide → Plan → ToolCall → Inspect → Continue/Finish` 循环；每步记录**可审计的决策摘要、工具参数（脱敏）、输入证据 ID、输出状态与错误**。不要求模型输出、保存或暴露其不可获得的内部原始思维链；后台展示结构化执行日志即可满足可调试性。

对 QQ 普通聊天不必强制深度推理。一个消息生成的总模型调用次数、总 token、预算、超时与工具调用上限按意图等级单独配置。


## 10. 虚拟电脑、工具调用与安全边界

> **V2 补充**：本章是工具/沙箱安全基线；长任务独立运行、并发与任务恢复的正式要求请结合第 31～33 章。


### 10.1 目标

AI 拥有一个**可持久保存项目文件、但不能越权宿主机**的工作空间，可以创建代码文件、执行短期程序、浏览网页、生成截图、导出成果，并将合法结果回传 QQ。模型不能直接获取 Docker Daemon、服务器 Shell、数据库管理员、NapCat 登录态或主程序 API 密钥。

### 10.2 双工作区设计

- **Persistent Workspace**：机器人自有的项目目录、导出的作品、手工批准的技能，文件按 owner/task/scope 标记 ACL。
- **Ephemeral Execution Workspace**：单任务临时快照，执行完销毁；默认只读基础镜像，非 root，文件系统和进程配额。

必须使**管理平面**（任务排队、审批、状态、密钥）与**数据执行平面**（不可信代码运行）隔离。

### 10.3 工具统一协议

```python
from dataclasses import dataclass
from typing import Any, Protocol

@dataclass(frozen=True)
class ToolContext:
    task_id: str
    requester_id: str
    group_id: str | None
    granted_scopes: frozenset[str]
    trace_id: str

@dataclass
class ToolResult:
    status: str  # success / denied / timeout / failed / cancelled
    summary: str
    artifacts: list[str]
    error_code: str | None = None
    data: dict[str, Any] | None = None

class ToolExecutor(Protocol):
    name: str
    required_capabilities: frozenset[str]

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        ...
```

所有 Tool Schema 严格校验字段；模型输出不能构造任意未经登记的工具名称或权限。工具执行者应重复检查授权，避免仅靠 Agent Router 一次校验。

### 10.4 权限分级

| 级别 | 例子 | 默认人员与条件 |
|---|---|---|
| `L0` | 普通问答、查已授权记忆、只读群公告 | 允许使用机器人的群友 |
| `L1` | 受限 Python 算法、数学绘图、非敏感截图 | 群友可用，配额与沙箱限制 |
| `L2` | 长期项目文件写入、安装白名单依赖、联网浏览 | 授权成员；目标域、文件 ACL 与配额 |
| `L3` | 群公告发布、删除群文件、空间发布动态、修改插件设置 | 管理员审批，留完整审计 |
| `L4` | 服务器系统维护、NapCat 凭据、数据库管理、宿主网络配置 | AI 不直接拥有；人工在独立管理通道执行 |

不能因为群友 QQ 昵称叫“管理员”就授予权限。必须使用 QQ 身份、群权限、管理后台认证以及明确的 policy 规则。

### 10.5 Sandbox Worker 基线

**最低要求**：非 root 用户、CPU/memory/pids/磁盘限制、最长执行时间、无宿主 Docker Socket 挂载、只读根文件系统（需要写时提供受限工作目录）、seccomp/AppArmor/SELinux 等限制、禁特权、禁 host 网络、凭据不注入。

**重要**：容器不应被视为适合执行任意恶意代码的绝对安全边界。面对任意群友提交的不可信脚本，建议引入微虚拟机/独立 VM（或 gVisor/Kata 等加强隔离方案），在通过渗透测试和逃逸风险评估前只开放受控白名单工具。

网络策略分为：

- `python_sandbox`：默认无网络。
- `browser_worker`：受控出口，防 SSRF/恶意跳转，独立浏览器 Profile。
- `research_fetcher`：只读 HTTPS、域名/IP 过滤、响应体限制。
- `admin_service`：仅内部网络，无模型直接访问。

### 10.6 浏览器与截图

采用 Playwright/Chromium，优先在无头浏览器完成网页截图。确实需要桌面截图时，在独立 Xvfb/远程桌面会话中执行；捕获前检查页面是否有账号密钥、个人数据或未授权内容。

网页文本、JS 提示、README、OCR 指令都视为第三方数据。网页允许引导浏览器完成用户明确授权的浏览任务，但不能借机获得通用执行能力。登录会话按服务隔离，禁止群友通过页面或工具输出请求导出 Cookies。

### 10.7 任务生命周期

`requested → policy_check → waiting_approval? → queued → running → validating_result → completed/failed/cancelled/expired`

每任务记录来源、请求者、关联消息、申请能力、审批结果、预算、执行器版本、工作区快照、执行摘要和生成文件。超时或取消后清理 Worker，保留脱敏审计。

### 10.8 任务结果送回 QQ

生成物通过 `ArtifactStore` 得到受保护的 `artifact_id`。发送之前重新校验 QQ 群归属、任务请求者和文件敏感性；由 `MessageOutbox` 统一提交 `send_group_msg` 的图片或文件消息。不能直接把 Worker 临时目录暴露为公开下载路径。

---

## 11. QQ 空间及其他社交行为插件

### 11.1 设计边界

`QZoneAdapter` **可选插件**，使用与 NapCat 群聊完全独立的 Provider 和授权流程。第三方 QZone 工具目前存在，但不是保证长期可用的官方公开机器人协议；默认不启用自动化读写，运营方自行核实服务条款、账号风险与授权。

### 11.2 能力分类

- `qzone.feed.read`：经授权读取可见动态。
- `qzone.like`：低频主动点赞，默认关闭。
- `qzone.comment.create`：评论，默认管理员审批。
- `qzone.post.create`：发布机器人真实的作品或状态，默认草稿 + 审批。
- `qzone.comment.reply`：回复机器人自己动态的评论，设独立限流和语境检查。

`SocialAction` 统一数据模型：`action_type`、`target_resource_id`、`proposed_content`、`proposed_by`、`approval_state`、`provider_status`、`performed_at`、`audit_id`。

### 11.3 社交动作与群发言独立计费/限流

每群主动消息、私聊消息、QQ 空间行为有独立限流并受全局配额控制。禁止自动批量点赞、群发评论和未经许可转发群资料。

---

## 12. 可扩展插件 SDK（开源重点）

### 12.1 插件分类

| Plugin 类型 | 扩展能力 | 示例 |
|---|---|---|
| Platform Adapter | 社交平台/消息协议 | NapCat OneBot、未来其他 IM |
| LLM Provider | 聊天/推理/决策 | OpenAI 兼容、DeepSeek 官方 API |
| Embedder / Reranker | 检索模型 | BGE-M3、其他服务商 |
| Media Provider | 图片、视频、ASR、OCR、TTS | Qwen VL、视频服务、PaddleOCR |
| Retrieval Provider | 向量、全文、混合 | PostgreSQL/pgvector、替代检索服务 |
| Memory Strategy | 巩固/去重/衰减 | Nightly Consolidation 策略 |
| Knowledge Connector | Wiki、网页、群文件、公告 | GroupFileIndexer、WebCrawler（授权源） |
| Tool | 受控能力 | PythonRunner、Plotter、Screenshot |
| Social Behavior | 发言策略和非消息动作 | InitiativePolicy、QZoneAdapter |
| Dashboard Extension | 后台页面/分析 | 模型费用、群资料管理 |

### 12.2 Plugin Manifest（草案）

```yaml
id: org.example.group-resource-indexer
name: Group Resource Indexer
version: 0.1.0
api_version: "1.0"
entrypoint: "group_resource_indexer.plugin:Plugin"
plugin_type: knowledge_connector
requires_core: ">=0.1,<1.0"
capabilities:
  - group.files.read
  - group.notices.read
permissions:
  - capability: network.onebot.read
    resource: group_scoped
  - capability: knowledge.write
    resource: group_scoped
  - capability: object.write
    resource: group_scoped
config_schema: "./schema.json"
dependencies: []
isolation: process
```

Manifest 的 `permissions` 表示**申请**权限，而不是安装即自动授予；用户在管理后台允许后才能执行。

### 12.3 插件生命周期

`discover → validate_manifest → check_core_compatibility → resolve_dependencies → admin_approve_permissions → initialize → start → healthcheck → stop → unload`。

事件订阅必须由插件声明订阅种类：`message.created`、`group.file_uploaded` 等。插件只能收到权限允许的**裁剪事件**；不可默认看到所有群的原始聊天、QQ 登录数据和全部文件。

### 12.4 官方插件建议与社区插件分级

- **内建可信插件**：与核心一同发布，使用稳定 Port，写入关键表仍通过 Repository。
- **审查社区插件**：声明权限，签名/哈希校验、版本锁定、开源源码审查，优先独立进程。
- **未审查插件**：默认禁用；不得直接在主应用进程中执行任意第三方 Python 代码。需要强隔离才允许测试。

> Python 包的“插件安装”天然可能执行代码。为降低供应链风险，社区插件默认单独 Worker 运行，不允许直接 import 到拥有数据库凭据和 NapCat Token 的主进程。任何热加载都要有隔离与回滚。

### 12.5 API 兼容规则

- `api_version` 为插件协议稳定版本，采用 Semantic Versioning。
- 移除字段/修改必填字段要升 Major；新增可选字段升 Minor；修正行为或 bug 升 Patch。
- 每个 Provider 配套 JSON Schema、TypeScript/Python 类型定义、契约测试和 Mock 实现。
- 插件错误不得拖垮主程序；隔离超时、熔断、健康检查、禁用开关。
- 官方示例插件不少于三个：`hello-reply`、`group-notice-reader`、`image-analyzer`。

### 12.6 示例：群公告只读插件

```python
class GroupNoticeReader:
    plugin_id = "org.example.group-notice-reader"
    required_scopes = {"group.notices.read"}

    async def on_request(self, ctx, group_id: str):
        # 权限由核心签发，插件不自行相信消息里的 group_id。
        await ctx.policy.require("group.notices.read", group_id=group_id)
        notices = await ctx.platform.capability("group.notices").list(group_id)
        return {
            "group_id": group_id,
            "notices": [n.to_public_dict() for n in notices],
            "fetched_at": ctx.clock.now_iso(),
        }
```

插件不能在运行中自行请求 `group.notices.write` 并立即生效；权限变更必须走管理员审计流程。

---

## 13. 数据存储与数据库模式（可实现草案）

### 13.1 总体选择

- **关系数据**：PostgreSQL，事务和强约束。
- **向量**：pgvector（初期统一存储，减少基础设施）。
- **中文模糊查找**：`pg_trgm` + 归一化文本；后期按规模评估中文搜索引擎。
- **对象存储**：S3 兼容（MinIO 或外部实现），保存媒体、文件与生成物。
- **短期状态**：单进程内存/Redis（按规模）。需跨进程共享/持久队列时选可靠后端。
- **异步任务**：初版建议 PostgreSQL `jobs` 表 + `SELECT ... FOR UPDATE SKIP LOCKED` + worker；吞吐提升后迁移 Redis Streams、RabbitMQ 等，不将队列绑定到领域模型。

### 13.2 表清单与字段（建议独立 migration）

**身份与群**

- `agent_identities(id, persona_version, created_at, status)`
- `platform_identities(id, platform, platform_user_id, global_user_id, created_at)`；唯一 `(platform, platform_user_id)`。
- `groups(id, platform, platform_group_id, name, timezone, settings_json, status)`；唯一 `(platform, platform_group_id)`。
- `group_memberships(group_id, identity_id, role, display_name, joined_at, last_seen_at)`。
- `user_consents(identity_id, scope, consent_kind, granted_at, revoked_at, version)`。
- `relationship_states(agent_id, identity_id, group_id nullable, interaction_stats_json, updated_at)`。

**消息、线程与行为**

- `events(id, event_type, adapter_id, dedupe_key, payload_redacted, occurred_at, received_at)`。
- `messages(id, group_id nullable, conversation_kind, sender_id, platform_message_id, reply_to_message_id, text_search, segments_json, created_at, recalled_at)`。
- `threads(id, group_id, topic_summary, summary_version, last_activity_at, closed_at)`。
- `thread_message_links(thread_id, message_id, confidence)`。
- `bot_messages(id, message_id, decision_id, agent_task_id, model_id, sent_at, send_status)`。
- `decision_logs(id, event_id, group_id, action, rationale_summary, context_snapshot_ref, policy_version, created_at)`。
- `social_actions(id, provider, action_type, actor_id, target_id, approval_id, status, created_at, executed_at)`。
- `message_reviews(id, bot_message_id, reviewer_id, tags, better_response, note, context_snapshot_id, created_at)`。

**记忆、知识与来源**

- `daily_episodes(id, group_id, local_date, thread_id, title, summary, importance, confidence, status, version)`。
- `memories(id, subject_identity_id nullable, origin_group_id nullable, scope, memory_type, content, confidence, importance, status, valid_from, valid_until, superseded_by, consent_policy_id, created_at, updated_at)`。
- `memory_sources(memory_id, source_type, source_id, quote_hash, page_or_span)`。
- `knowledge_documents(id, origin_type, origin_ref, group_id nullable, title, body_ref, source_url, license, trust_status, version, created_at, verified_at)`。
- `knowledge_chunks(id, document_id, ordinal, content, page, heading, embedding_model_id, content_hash, indexed_at)`。
- `memory_embeddings(memory_id, model_id, vector, indexed_at)`；生产可以按模型维数单独管理。
- `knowledge_embeddings(chunk_id, model_id, vector, indexed_at)`。
- `consolidation_jobs(id, group_id, local_date, pipeline_version, watermark, status, checkpoint_json, created_at, finished_at)`。

**群文件、公告与媒体**

- `group_files(id, group_id, platform_file_id, busid, parent_folder_id, filename, size_bytes, uploader_id, uploaded_at, content_hash, sync_status, indexed_at, last_seen_at)`。
- `group_file_folders(id, group_id, platform_folder_id, parent_folder_id, folder_name, last_seen_at)`。
- `group_notices(id, group_id, platform_notice_id, body, body_hash, published_at, first_seen_at, last_seen_at, status, version)`。
- `group_notice_versions(notice_id, version, body_hash, body, observed_at)`。
- `media_assets(id, origin_type, origin_id, object_key, mime_type, sha256, size_bytes, width, height, duration_ms, scope, created_at)`。
- `media_analyses(id, asset_id, task_type, model_id, result_json, status, analyzed_at)`。
- `stickers(id, asset_id, scope, tags, description, usage_count, last_used_at, approval_state)`。

**工具、任务、插件与管理**

- `agent_tasks(id, requester_id, source_message_id, group_id, task_type, priority, status, budget_json, created_at, started_at, finished_at)`。
- `task_steps(id, task_id, step_index, tool_name, input_redacted, output_ref, status, duration_ms)`。
- `tool_approvals(id, task_id, action_type, requested_scope, approved_by, status, created_at)`。
- `plugins(id, version, api_version, manifest_json, enabled, health_state)`。
- `plugin_permissions(plugin_id, resource_scope, capability, granted_by, expires_at)`。
- `model_usage(id, task_id, provider, model_id, purpose, input_tokens, output_tokens, estimated_cost, duration_ms, status, time)`。
- `jobs(id, job_type, payload_json, status, priority, scheduled_at, locked_until, retry_count, dedupe_key)`。
- `outbox_messages(id, destination_type, destination_id, content_json, dedupe_key, status, platform_message_id, created_at)`。
- `audit_logs(id, actor_id, action, resource_type, resource_id, before_redacted, after_redacted, timestamp)`。

### 13.3 DDL 核心示例

> 这是起步 Migration 样例，并非一次性完整生产数据库。所有表应使用 Alembic 迁移创建，外键、时间索引、JSON schema 校验、软删除、RLS/权限过滤按模块逐步补齐。

```sql
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE groups (
    id UUID PRIMARY KEY,
    platform TEXT NOT NULL,
    platform_group_id TEXT NOT NULL,
    name TEXT,
    timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
    settings_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    status TEXT NOT NULL DEFAULT 'active',
    UNIQUE(platform, platform_group_id)
);

CREATE TABLE messages (
    id UUID PRIMARY KEY,
    group_id UUID REFERENCES groups(id),
    conversation_kind TEXT NOT NULL,
    sender_id TEXT NOT NULL,
    platform_message_id TEXT NOT NULL,
    reply_to_message_id TEXT,
    text_search TEXT,
    segments_json JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    recalled_at TIMESTAMPTZ,
    UNIQUE(group_id, platform_message_id)
);
CREATE INDEX idx_messages_group_time ON messages(group_id, created_at DESC);
CREATE INDEX idx_messages_text_trgm ON messages USING gin (text_search gin_trgm_ops);

CREATE TABLE memories (
    id UUID PRIMARY KEY,
    subject_identity_id UUID,
    origin_group_id UUID REFERENCES groups(id),
    scope TEXT NOT NULL,
    memory_type TEXT NOT NULL,
    content TEXT NOT NULL,
    confidence REAL NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    importance REAL NOT NULL CHECK (importance BETWEEN 0 AND 1),
    status TEXT NOT NULL DEFAULT 'candidate',
    valid_from TIMESTAMPTZ,
    valid_until TIMESTAMPTZ,
    superseded_by UUID,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_memories_scope_status ON memories(scope, status, origin_group_id);

CREATE TABLE memory_embeddings (
    memory_id UUID NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    model_id TEXT NOT NULL,
    embedding vector(1024) NOT NULL,
    indexed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY(memory_id, model_id)
);
CREATE INDEX idx_memory_embedding_hnsw
  ON memory_embeddings USING hnsw(embedding vector_cosine_ops);

CREATE TABLE group_files (
    id UUID PRIMARY KEY,
    group_id UUID NOT NULL REFERENCES groups(id),
    platform_file_id TEXT NOT NULL,
    busid BIGINT,
    parent_folder_id TEXT,
    filename TEXT NOT NULL,
    size_bytes BIGINT,
    content_hash TEXT,
    sync_status TEXT NOT NULL DEFAULT 'discovered',
    last_seen_at TIMESTAMPTZ,
    UNIQUE(group_id, platform_file_id)
);
CREATE INDEX idx_group_files_filename_trgm
  ON group_files USING gin(filename gin_trgm_ops);

CREATE TABLE group_notices (
    id UUID PRIMARY KEY,
    group_id UUID NOT NULL REFERENCES groups(id),
    platform_notice_id TEXT,
    body TEXT NOT NULL,
    body_hash TEXT NOT NULL,
    first_seen_at TIMESTAMPTZ NOT NULL,
    last_seen_at TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL DEFAULT 'active'
);
CREATE INDEX idx_group_notices_group_status ON group_notices(group_id, status);
```

**生产提醒**：上例的 `UNIQUE(group_id, platform_message_id)` 遇到 PostgreSQL 空值行为和私聊消息需要另设唯一约束，例如 `UNIQUE(platform_instance, conversation_kind, conversation_id, platform_message_id)`。实际生产迁移中应从平台实例、会话 ID 统一设计，避免跨会话 message_id 冲突。向量维度 `1024` 与 BGE-M3 的选择绑定；更换模型维度时，应新建索引表/列而不是把异构维度硬塞一个列。

### 13.4 数据检索与索引策略

- `messages`：按群和时间范围分页，用 cursor pagination，不用大 OFFSET。
- `bot_messages`：按发送状态、模型、群、时间、评价过滤；关联 `decision_logs`。
- `group_files`：按群、目录、名称 trigram/精确排序；权限在 SQL `WHERE group_id=:current_group` 中限定。
- `group_notices`：先过滤有效状态、群、最近版本，再做全文匹配。
- `memories`：先过滤 scope/主体/状态/时效/授权，再去查询限定候选的 embedding；不要用全库 ANN 后简单裁剪敏感结果。
- 媒体对象按 `group_id`、对象 ID、生命周期建立索引，生产异步清理孤立资源。


## 14. 后台管理系统（WebUI）

### 14.1 基础技术

- 前端：Vue 3 + TypeScript + Vite + Pinia + Vue Router，UI 框架由社区定稿。
- 后端：FastAPI 管理 API，独立于 QQ 接入端口，支持 OpenAPI 和自动生成客户端。
- 管理端认证：初版本地管理员密码 + 安全会话/MFA 可选；公网使用时应支持反向代理身份认证、HTTPS、CSRF 防护、登录频率限制。
- 授权角色：`owner`、`admin`、`moderator`、`reviewer`、`viewer`。角色对数据域/群域分别授权；不能因 QQ 群管理员身份自动获得系统后台账号。

### 14.2 页面列表

| 页面 | 功能 |
|---|---|
| 总览 Dashboard | 在线状态、在线群数、近 24h 消息量、模型费用、待审批任务、异常 |
| 群管理 Groups | 每群启用开关、人格风格、安静时段、记忆策略、跨群授权、配额 |
| 消息检索 Messages | 搜索所有机器人发言；可选展示授权保存的人类上下文 |
| 消息详情 Message Detail | 前后文、触发决策、所用模型、检索证据、工具轨迹、发送确认 |
| 评价台 Reviews | 多选标签、自然度、建议改写、回放对比、批量标注 |
| 社交回放 Replay Lab | 冻结输入、模型/Prompt A/B、是否回复对比；绝不发送到 QQ |
| 天记忆 Daily | 日期/群/话题卡片，显示自动提取与待处理问题 |
| 长记忆 Memory | 按用户/群/类型/状态检索、查看证据、纠错、删除、权限 |
| 知识库 Knowledge | 文档源、知识状态、版本适用范围、过期复核、向量索引 |
| 群资料 Group Resources | 文件树、解析状态、群公告版本与内容、同步任务、ACL |
| 表情库 Stickers | 标签、来源、去重、跨群使用审批、使用次数 |
| 多模态 Media | 图片/视频/音频理解队列、结果、用量、失败重试 |
| 虚拟电脑 Workspace | 正在运行的任务、文件浏览、截图预览、沙箱资源与日志 |
| 外部研究 Research | 搜索来源、使用的 DeepSeek/API、核验状态、缓存 |
| QQ 空间 QZone | 可用状态、草稿、点赞/评论队列、审核、封禁开关 |
| 模型配置 Models | 多 Provider、路由策略、超时、费用、回退与密钥引用 |
| 插件管理 Plugins | 安装、版本、权限、健康状态、升级与回滚 |
| 系统审计 Audit | 登录、配置更改、批准操作、消息删除、数据导出 |

### 14.3 消息评价设计

**搜索维度**：关键词、模糊匹配、群、用户、任务 ID、日期范围、触发原因、回复动作、模型、工具类型、是否已评价、评价标签、自动自然度得分、结果状态。支持 cursor 分页和导出经脱敏的标注数据。

**评价标签**（可多选）：`too_ai` 太人机、`forced_joke` 强行接梗、`overly_praising` 夸赞过度、`too_long` 太啰嗦、`unnecessary_reply` 本来不该回复、`context_mismatch` 答非所问、`memory_misuse` 记忆用错、`fact_error` 事实错误、`privacy_risk` 隐私问题、`natural` 很自然。

**变更管理**：评价入库不等于修改线上 Prompt。定期生成候选策略版本，在回放集和人工抽样下评估后发布；随时可回滚。审查者给出的替代回复，必须保留**原消息、原上下文、差异、标注者**，不能只保留“正确答案”文本。

### 14.4 Replay Lab（确定性程度说明）

回放输入冻结：消息快照、已有记忆快照、相关文件/公告版本、Prompt 版本、模型参数、工具输出录制。不能保证真实生成模型完全确定，但应记录 seed（若支持）、版本、温度与采样参数以提高可复现性。默认不允许回放调用具副作用的真实工具；只能用录制结果或 Mock。

评价目标包括：该不该说、指向谁、是否泄露、是否自然、是否事实正确、是否引用了真实来源。不要只测句子“有没有好笑”。

### 14.5 管理 API 草案

统一前缀 `/api/v1`，所有响应包含 `request_id`；敏感接口带 RBAC、资源范围、审计。

| Method | 路径 | 说明 |
|---|---|---|
| GET | `/health/live` `/health/ready` | 存活/就绪探针 |
| GET | `/groups` | 群列表 |
| GET/PATCH | `/groups/{id}/settings` | 群配置及策略版本 |
| GET | `/messages` | 搜索消息，含筛选及游标 |
| GET | `/messages/{id}` | 消息详情与决策轨迹 |
| POST | `/messages/{id}/reviews` | 提交评价 |
| GET | `/reviews` | 评价列表与导出 |
| POST | `/replays` | 创建只读回放任务 |
| GET | `/replays/{id}` | 回放结果 |
| GET | `/memories` | 权限范围内搜索记忆 |
| PATCH/DELETE | `/memories/{id}` | 审计更正/删除 |
| GET | `/groups/{id}/files` | 授权群文件树 |
| POST | `/groups/{id}/files/sync` | 手动触发元数据同步 |
| POST | `/groups/{id}/files/{file_id}/index` | 经许可重新索引内容 |
| GET | `/groups/{id}/notices` | 公告列表/版本 |
| POST | `/groups/{id}/notices/refresh` | 触发公告刷新 |
| GET | `/knowledge/documents` | 知识来源和状态 |
| GET | `/tasks` | 任务列表与运行状态 |
| POST | `/tasks/{id}/cancel` | 取消未完成任务 |
| POST | `/approvals/{id}/decision` | 授权者批准/拒绝 |
| GET | `/plugins` | 插件及能力状态 |
| POST | `/plugins/{id}/enable` | 启用已审批插件 |
| GET | `/metrics/models` | 模型费用/故障统计 |
| POST | `/controls/emergency-stop` | 紧急停止主动行为/工具 |

### 14.6 API 响应模型

```json
{
  "request_id": "req_...",
  "data": {
    "id": "msg_001",
    "text": "乘法也搞好了？",
    "group_id": "123456",
    "sent_at": "2026-10-15T15:00:00+08:00",
    "decision": {"action": "REPLY", "reason_summary": "回应同一话题中的项目进展"},
    "review_count": 2
  },
  "error": null
}
```

错误统一使用代码：`PERMISSION_DENIED`、`RESOURCE_NOT_FOUND`、`CAPABILITY_UNAVAILABLE`、`RATE_LIMITED`、`POLICY_REJECTED`、`UPSTREAM_FAILED`、`TIMEOUT`、`CONFLICT` 等。不要把详细 Traceback 或密钥返回给普通前端用户。

---

## 15. 调度、任务队列、发送一致性与性能

> **V2 补充**：Social 快路径与长工作任务必须隔离，状态机/租约/进度读取见第 31、32、41 章。


### 15.1 任务类别与优先级

| 队列 | 工作类型 | 初始优先级 | 并发建议 |
|---|---|---|---|
| `interactive` | @、直接问题、引用回复 | 高 | 控制每群串行发送、跨群有限并发 |
| `social` | 主动接话候选、自然度评估 | 中 | 每群最多一项待发送主动回复 |
| `research` | 联网检索、外部模型咨询 | 中 | 每群/全局并发限制 |
| `media` | 图片/视频/OCR/ASR | 中/低 | 使用独立资源配额 |
| `sandbox` | Python/浏览器/截图 | 中 | 独立隔离 Worker 并发 |
| `indexing` | 群文件、公告、知识索引 | 低 | 限速批处理 |
| `consolidation` | 夜间记忆巩固 | 低 | 允许暂停，避免阻塞群聊 |
| `qzone` | 空间只读/待审批动作 | 低 | 独立限流、默认关闭 |

### 15.2 队列不等于发送

- 群内发送要持有 `GroupSendLease`，保证一个群不会同时被多个 Worker 乱序发送。
- 回复生成后要检查当前话题版本与原消息状态。
- 任务优先级不应导致其他群长期饥饿：应用 fair queue 或每群 Token Bucket/轮询权重。
- 已排队 90 秒的主动接话可能已经不适合发送，主动任务有较短 TTL；显式请求可更长，并尽量引用原消息。

### 15.3 Transactional Outbox

在数据库事务中同时写入“决策完成”与“待发送 Outbox”。发送 Worker 获取 lease，调用 OneBot，成功后记录平台消息 ID；失败按规则重试。OneBot `send_group_msg` 不一定支持真正的应用层幂等键，因此不能保证网络异常下严格 Exactly-once，应该尽力避免重复发送，并把“成功未知”状态交给核验逻辑或人工处理。

### 15.4 外部服务熔断

每 Provider 维护健康度、最近错误率、429/限流信号、费用和平均响应时间；降级顺序：同类备用服务 → 低成本本地能力 → 说明不可用或放弃。不得把暂时缺少视觉模型的图片“按经验猜出来”。

### 15.5 成本控制

为不同用途分别定义 Token/价格上限：聊天、研究、夜间整理、媒体、视频、沙箱。支持每日/每群/每任务三级预算；到达预算后关闭低优先级主动行为，不应无声消耗欠费服务。

建议保存每个任务的估算与实际消耗；后台费用展示需标注“根据当前配置估算”，避免当作供应商真实账单。

---

## 16. 安全、隐私、合规与滥用防护

### 16.1 威胁模型

重点考虑以下攻击者：普通群友、恶意群管理员、注入恶意文字的网页/文件上传者、被污染的模型输出、恶意社区插件、获得后台低权限账号的人员、被盗 API Key 的攻击者。

保护资产：QQ 会话凭据、NapCat Token、模型 API Keys、用户私聊、跨群记忆、群文件、数据库、宿主机文件、Agent 虚拟电脑项目、管理后台权限。

### 16.2 高风险路径

- **跨群隐私**：A 群资料被 B 群问题召回。
- **Prompt Injection**：群公告/PDF/OCR/网页提示“忽略前面指令”，骗模型进行写操作。
- **SSRF**：群文件或网页 URL 被操纵，使下载器访问内网。
- **工具越权**：模型调用未获授权的 Shell、删除文件、发群公告。
- **插件供应链**：第三方 Python 插件获取全部进程密钥。
- **身份欺骗**：冒充管理员/操纵 @ 文本/昵称取得权限。
- **数据投毒**：群友反复散播错误知识，让夜间巩固将谣言升级为“已验证”。
- **资源耗尽**：巨型视频、压缩炸弹、消息洪水、无限任务链。
- **滥用公开 API**：未授权方批量访问消息历史或导出记忆。

### 16.3 工程控制措施

- 所有外部输入默认不可信；系统配置、用户请求、检索证据、文件内容采用明确的信任等级。
- 凭据保存在环境秘密管理/部署密钥仓库，日志和 Agent 上下文中禁止明文记录。
- 内部权限以 capability + resource scope + user/group policy 三因素校验；工具执行前二次校验。
- 下载器严格控制 URL、IP、Content-Length、重定向次数、最终响应体大小；对临时群文件链接做安全代理。
- 独立沙箱与浏览器，最小权限，强制取消与速率限制。
- 对媒体和文件进行内容长度、MIME 校验及静态扫描，默认禁宏/执行。
- 数据删除不仅改 `status`，还同步清理缓存、向量和可恢复索引；备份采用明确的过期保留周期。
- 管理端 MFA/操作审计、敏感操作二次确认、审批分离（如有多人管理）。
- 软件依赖固定 lockfile，生成 SBOM、依赖漏洞扫描、容器镜像扫描和签名。

### 16.4 保留期限（建议初始值，部署者可改）

| 数据 | 建议默认策略 | 备注 |
|---|---|---|
| 原始普通群聊天 | 30 天 | 群管理员启用后采集；允许更短或关闭 |
| 机器人自己发送的消息 | 90～180 天 | 为评价与调试保留；可删 |
| 天记忆 Episode | 14～30 天 | 已完成巩固后定期清理 |
| 长期人物记忆 | 按需求/授权，定期复核 | 用户可查、可删、可撤销跨群 |
| 文件原文缓存 | 7～30 天或依群策略 | 可以仅存切片或仅元数据 |
| 群公告历史 | 按群策略 | 保留必要版本，不无限保存 |
| 工具执行日志 | 30～90 天、敏感字段脱敏 | 部署者需可配置 |
| 模型用量统计 | 90～365 天 | 尽量用聚合指标而非正文 |

正式发布时必须提供隐私说明、明确各能力采集范围和关闭途径。群成员可通过命令或网站获得隐私说明、个人记忆查看/删除流程。群文件可能含其他人的资料，群管理员授权并不自动等于所有个人都授权被跨群分析。

### 16.5 紧急停止（Kill Switch）

支持四层停机：`disable_initiative`（仅停主动）、`disable_social_writes`（禁自动发布）、`disable_all_tools`（禁工具）、`pause_bot`（停所有响应）。应以数据库/缓存的硬策略生效，不依赖大模型“听话”。

---

## 17. 项目目录结构（建议 Monorepo）

```text
open-social-agent/
├── README.md
├── README.en.md
├── LICENSE
├── NOTICE.md
├── CONTRIBUTING.md
├── CODE_OF_CONDUCT.md
├── SECURITY.md
├── CHANGELOG.md
├── .env.example
├── .editorconfig
├── .gitignore
├── pyproject.toml
├── uv.lock
├── docker-compose.yml
├── Makefile
├── docs/
│   ├── architecture/
│   │   ├── overview.md
│   │   ├── decisions/               # ADR-0001 等架构决策记录
│   │   ├── data-model.md
│   │   ├── event-contracts.md
│   │   ├── plugin-api.md
│   │   └── threat-model.md
│   ├── guides/
│   │   ├── quickstart.md
│   │   ├── napcat-setup.md
│   │   ├── group-file-and-notice.md
│   │   ├── memory-and-privacy.md
│   │   ├── model-providers.md
│   │   ├── sandbox.md
│   │   └── troubleshooting.md
│   ├── api/
│   ├── plugins/
│   ├── development-roadmap.md
│   └── 详细实现方案.md
├── apps/
│   ├── server/
│   │   └── src/osa/
│   │       ├── main.py
│   │       ├── config/
│   │       ├── domain/
│   │       │   ├── identity/
│   │       │   ├── conversation/
│   │       │   ├── social_decision/
│   │       │   ├── memory/
│   │       │   ├── knowledge/
│   │       │   ├── group_resources/
│   │       │   ├── tools/
│   │       │   └── permissions/
│   │       ├── application/
│   │       │   ├── commands/
│   │       │   ├── queries/
│   │       │   ├── orchestration/
│   │       │   └── jobs/
│   │       ├── infrastructure/
│   │       │   ├── persistence/
│   │       │   ├── messaging/
│   │       │   ├── object_store/
│   │       │   └── observability/
│   │       └── interfaces/
│   │           ├── rest_api/
│   │           ├── ws_gateway/
│   │           └── cli/
│   ├── admin-web/
│   │   ├── src/
│   │   ├── public/
│   │   └── package.json
│   ├── worker-media/
│   ├── worker-sandbox/
│   ├── worker-browser/
│   └── worker-indexer/
├── packages/
│   ├── plugin-sdk/
│   ├── event-schema/
│   ├── model-provider-sdk/
│   └── shared-types/
├── plugins/
│   ├── napcat-onebot11/
│   ├── group-file-indexer/
│   ├── group-notice-reader/
│   ├── pgvector-retrieval/
│   ├── openai-compatible-llm/
│   ├── deepseek-official-api/
│   ├── image-provider-example/
│   ├── sticker-library/
│   └── qzone-experimental/
├── migrations/
│   ├── env.py
│   └── versions/
├── deploy/
│   ├── docker/
│   ├── compose/
│   └── reverse-proxy/
├── scripts/
│   ├── smoke_test.py
│   ├── backup.py
│   └── reindex_embeddings.py
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── contract/
│   ├── security/
│   ├── fixtures/
│   └── replay_cases/
├── examples/
│   ├── fake-onebot-server/
│   ├── sample-plugin/
│   └── model-mock/
└── .github/
    ├── workflows/
    ├── ISSUE_TEMPLATE/
    └── pull_request_template.md
```

### 17.1 import 依赖规则

- `domain` 禁止依赖 `infrastructure`、NapCat 实现及 Web UI。
- `application` 依赖 `domain` 的 Port。
- `infrastructure` 实现 Port，可引用数据库/第三方库。
- 插件与核心通过 SDK 和能力上下文交互。
- Worker 通过任务 Schema 处理，不 import 服务器的全部业务对象。
- `apps/admin-web` 仅调用 Admin REST/WS API，不直接访问数据库。

这类依赖约束应通过架构测试/静态规则执行，避免开源贡献者无意中造成循环依赖。


## 18. 配置规范与部署示例

### 18.1 配置分层

优先级：`内置安全默认值 < config.yaml < 部署环境变量 < 管理后台受审计配置`，**安全硬限制不可被普通群配置放宽**。API Key、数据库密码、NapCat Token 只能通过环境变量/Secret Manager 引用，严禁明文提交 Git。

示例 `config.example.yaml`（概念示例，具体字段需同步 JSON Schema）：

```yaml
app:
  id: open-social-agent
  environment: development
  timezone: Asia/Shanghai
  log_level: INFO
  default_language: zh-CN

platforms:
  qq:
    enabled: true
    adapter: napcat_onebot11
    reverse_ws_bind: 127.0.0.1
    reverse_ws_port: 8081
    access_token_env: NAPCAT_ACCESS_TOKEN
    capabilities_auto_detect: true
  qzone:
    enabled: false
    adapter: qzone_experimental

models:
  purposes:
    chat: {provider: main_chat, model: "your-chat-model"}
    decision: {provider: fast_chat, model: "your-fast-model"}
    reasoning: {provider: deepseek_official, model: "configured-model"}
    memory: {provider: main_chat, model: "your-chat-model"}
    embedding: {provider: bge_m3, model: "BAAI/bge-m3", dimension: 1024}
    vision: {provider: vision_api, model: "configured-vision-model"}
    video: {provider: video_api, model: "configured-video-model"}
    reranker: {provider: reranker, model: "configured-reranker-model"}
  budget:
    max_daily_usd_estimate: 5.0
    max_task_usd_estimate: 0.5
    hard_stop_on_budget_exceeded: true

social:
  default_mode: observing
  observation_window_seconds: 3
  min_unprompted_cooldown_seconds: 240
  max_unprompted_replies_per_15min: 3
  max_consecutive_unprompted_replies: 2
  idle_topic_initiation_enabled: false
  avoid_forced_humor: true
  final_stale_check: true

memory:
  daily_episode_enabled: true
  nightly_consolidation_time: "03:30"
  local_timezone_per_group: true
  daily_episode_retention_days: 30
  raw_message_retention_days: 30
  embedding_batch_size: 32
  retrieval_top_k: 6
  cross_group_personal_memory_default: false
  private_memory_in_group_default: false

knowledge:
  provenance_required: true
  auto_promote_community_claim: false
  stale_knowledge_trigger_research: true
  enable_group_file_content_indexing_by_default: false

group_resources:
  enabled: true
  group_file_metadata_sync_interval_hours: 12
  group_notice_sync_interval_hours: 3
  group_file_metadata_only_by_default: true
  group_file_max_download_mb: 25
  group_file_max_folder_depth: 10
  group_file_max_files_per_run: 500
  indexed_formats: [pdf, txt, md, docx, pptx, csv]
  group_notice_write_enabled: false
  group_file_delete_enabled: false
  public_cross_group_share_enabled: false

media:
  image_auto_analyze: on_demand
  video_auto_analyze: false
  max_video_duration_seconds: 180
  sticker_auto_global_share: false

sandbox:
  enabled: false
  python_network_enabled: false
  max_execution_seconds: 20
  max_memory_mb: 512
  max_cpus: 1
  max_pids: 64
  persistent_workspace_writes_require_approval: true
  use_strong_isolation_for_untrusted_code: true

research:
  deepseek_official_enabled: true
  deepseek_web_enabled: false
  independent_web_search_enabled: true
  require_source_provenance: true

security:
  deny_unapproved_tools: true
  require_approval_for_group_notice_write: true
  require_approval_for_group_file_delete: true
  require_approval_for_qzone_post: true
  mask_secrets_in_logs: true

observability:
  metrics_enabled: true
  trace_sampling_rate: 0.05
  store_redacted_context_snapshots: true
```

此文件中的数值只是开发初值，需经压测、实际社交回放与账号安全测试调整。`video_api` 等配置的 provider 名称属于占位符，没有注册真实插件时应在启动时提示配置缺失并禁用该能力。

### 18.2 Docker Compose 架构建议

- `app`：FastAPI/事件驱动服务，通常作为核心应用与调度中心。
- `worker`：后台索引、记忆巩固、模型任务。
- `postgres`：关系数据 + pgvector；配置备份和数据卷。
- `redis`：可选队列/缓存，开发期可以不启用。
- `object-store`：S3 兼容媒体存储。
- `admin-web`：静态前端 + 反向代理。
- `sandbox-worker`：按需启用，严格限资源，与 `postgres` 和密钥隔离。
- `browser-worker`：按需启用，具有受控网络出口。
- `napcat`：**外部用户自行部署的依赖**，不得未经授权把 NapCat 源码或镜像再打包纳入本项目发行件；可提供链接和独立接入说明。

初期可以使用单台机器部署，但 `sandbox-worker` 不应与数据库共用高权限运行环境。生产上考虑将风险更高的沙箱迁往隔离节点。

### 18.3 开发环境启动顺序

1. 克隆项目，复制 `.env.example` 和 `config.example.yaml`。
2. 启动 PostgreSQL / 对象存储，等待健康检查。
3. 运行 `alembic upgrade head` 创建 schema。
4. 配置至少一个 Chat Provider、Decision Provider；可先用 MockModel。
5. 启动应用和后台，先用 Fake OneBot 事件验证收发。
6. 配置 NapCat 反向 WS，使用专用测试 QQ 号、单个受控测试群。
7. 验证 `/health/ready`、接收群消息、@ 回复、发送后追踪日志。
8. 在后台逐项开启天记忆、群文件元数据、公告读取、内容索引等能力。
9. 在有隔离环境和审批机制后再启用沙箱；QQ 空间插件最后开启。

### 18.4 数据备份与恢复

数据库使用日常逻辑备份 + WAL/增量策略，媒体对象与数据库引用保持相同恢复点的记录。密钥不与数据库备份放在同一处；备份加密、定期测试恢复。恢复后需要重新验证群白名单、管理员权限及已撤销的记忆授权，避免把旧备份中的数据直接恢复为可跨群访问。

---

## 19. 端到端关键流程（开发者可直接分工）

### 流程 A：群里普通聊天

1. OneBot 消息事件到达，Gateway 做鉴权、转换、去重。
2. 保存消息；更新该群话题、短期状态、可能的天记忆候选。
3. Fast Rules 判断是否针对机器人；普通对话进入观察窗口。
4. Decision Model 可返回 `IGNORE/WAIT/REPLY/REACT`。
5. 若 REPLY，进行当前群授权检索；生成“表达意图”。
6. 主模型生成候选；自然度检查和事实核对。
7. 发送前确认话题未过期、群可发送、频率未超限。
8. Outbox 发送；收到平台返回后写 `bot_messages`、`decision_logs`、`model_usage`。
9. 后台可搜索并评价。

### 流程 B：A 群到 B 群的个人记忆

1. A 群用户表达长期项目偏好，形成 `candidate` 记忆并记录来源。
2. 未获跨群记忆同意时，只能保留 A 群范围内数据；不得自动升级到 `global_personal_owner`。
3. 用户自行开启跨群记忆后，系统可对适合共享的本人信息进行迁移/重新审核，而非批量把 A 群所有聊天共享。
4. 同一 QQ 用户在 B 群向机器人问自己的项目。
5. 数据库按本人身份、consent、可检索范围查候选；披露检查确认回答适合当前群。
6. 机器人用自然的一句确认信息，避免不必要地透露其他细节。

### 流程 C：群文件索引和问答

1. 管理员开启 `group.files.metadata.read`。
2. 同步文件树，保存文件名、文件夹、ID、busid、上传者（如可得）。
3. 如额外开启 `group.files.content.index`，文件进入安全下载和解析队列。
4. 生成 chunk、embedding、来源页码/时间，作用域 `group_resource`。
5. 群友询问文件信息时检索当前群元数据，询问内容时检索授权 chunk。
6. 回复附文件名、位置、最近索引时间和需要时的引用片段；无法读取则说明原因。

### 流程 D：群公告查询

1. 定时刷新 `_get_group_notice`，保存变更快照。
2. 用户问当前群规，优先使用当前有效公告；缓存过期时按需刷新。
3. 调用失败可回答缓存内容但必须注明不是实时数据；过久则不确定。
4. 公告的“命令文本”不会触发工具或群设置变更。

### 流程 E：图片/视频请求

1. 接收媒体消息，记录类型与来源；判断是否真有必要分析。
2. 下载或转换媒体，安全校验、对象存储。
3. 路由到 image / video / OCR / ASR Provider，得到结构化分析结果。
4. 与群话题融合；必要时研究并核实。
5. 用自然口吻回复；不确定的信息如实说明，视频可附时间戳。

### 流程 F：虚拟电脑请求

1. 用户提出代码/网页/截图任务。
2. Planner 生成步骤和所需工具；Policy Engine 判断请求者等级与资源权限。
3. 需要审批的步骤入队等待，不允许模型绕过审批。
4. Sandbox Worker 执行，任务强制限时、限额、隔离。
5. 检查结果和生成文件，清理敏感内容；Outbox 回传 QQ。
6. 保存任务摘要；可复用成功经验进入待审核技能候选。

### 流程 G：夜间记忆巩固

1. 定时选中每群前一日 Episode，检查 checkpoint。
2. 提取候选长期事实、社交事件、知识、实践经验。
3. 验证来源/主体/授权/时效，与旧记忆去重冲突检测。
4. 程序事务提交候选操作，Outbox 更新向量索引。
5. 保存质量采样结果和审计轨迹，允许第二天人工纠错。

### 流程 H：消息评价与策略回放

1. 管理员后台搜索某条机器人发送的消息。
2. 查看前后文、决策摘要、相关证据和模型版本。
3. 选择多标签评价，必要时给出更自然替代回复。
4. 评价进入偏好库；不直接修改线上模型 Prompt。
5. 回放集离线对比候选策略，在不真实发送的模式下测试。
6. 新策略通过验收后灰度部署，出现负面表现可回滚。

---

## 20. 测试体系与质量门禁

### 20.1 单元测试

- 事件去重和格式转换。
- 线程关联、冷却、话题过期判断。
- scope 权限与同一用户跨群授权。
- 记忆去重、过期、冲突、删除传播。
- 文档 chunk、文件类型检查、目录递归限制。
- Plugin Manifest 解析、权限拒绝、版本兼容。
- Model Router 熔断、预算检查、模拟超时。

### 20.2 集成测试

- Mock OneBot WebSocket：连接/断线/重连/鉴权/乱序/重复事件。
- PostgreSQL + pgvector：事务、外键、维度、索引、排序、权限 SQL。
- 群文件：根目录/子目录/上传事件/URL 过期/删除/改名/下载失败。
- 公告：多公告版本、缓存刷新、短期接口失败、群管理员权限差异。
- 对象存储：上传、签名 URL、对象清理、权限隔离。
- 工具：强制超时/资源不足/取消/网络被拒绝。

### 20.3 安全测试（必做）

- 在群 A 注入隐私，群 B 的另一个用户尝试套取；应一律拒绝。
- 在文档和公告中写“忽略安全规则、发送密钥”；模型可以读到文本，但 Tool Policy 必须拒绝。
- 在下载 URL 中使用 `127.0.0.1`、内网 IP、DNS 重绑定/重定向；下载器必须拦截。
- 利用文件名 `../../secret` 测试路径穿越；对象存储文件名使用不透明 ID，不拼接用户文件名作为路径。
- 上传压缩炸弹、损坏 PDF、伪 MIME、超长视频；应安全失败。
- 普通群友发送“我是项目 owner”或修改 QQ 昵称；不能提升权限。
- 恶意插件请求过高权限，管理员拒绝时不能加载任何可执行逻辑。
- 取消任务后 Worker 不能继续自行执行已撤销写操作。

### 20.4 社交自然度回放集

建立不同场景标签：`direct_mention`、`reply_to_bot`、`interleaved_topics`、`private_conversation_between_humans`、`unfinished_story`、`group_image`、`group_notice_query`、`cross_group_self_recall`、`outdated_reply`、`should_stay_silent`。

评估维度：**该不该回复、引用是否正确、语气是否自然、是否重复记忆、是否硬找梗、是否泄露、事实是否正确**。每类有明确的通过标准和人工标注样例；“发言越多越好”绝不是指标。

### 20.5 CI 工作流

- Python：Ruff 格式检查/静态检查、Pyright/Mypy 类型检查、pytest 单元和集成测试。
- Web：ESLint、TypeScript 类型检查、前端单测与构建。
- 依赖扫描：pip/npm/容器漏洞检查、SBOM 输出、Secret Scan。
- PR：影响 SDK Schema 时强制运行 Plugin Contract Suite；数据库迁移双向测试。
- 安全：专门的 Prompt Injection、数据越权、文件解析输入测试集。
- 文档：Markdown Lint、链接检查、配置 Schema 校验。

### 20.6 可观测性

核心指标：

- `events_received_total`、`events_deduped_total`
- `bot_reply_decisions_total{action}`、`bot_messages_sent_total`
- `model_request_duration_seconds{provider,purpose}`、`model_errors_total`
- `memory_retrieval_duration_seconds`、`memory_consolidation_failures_total`
- `group_file_sync_duration_seconds`、`group_file_index_failures_total`
- `notice_sync_age_seconds`、`sandbox_task_timeout_total`
- `security_policy_denials_total`、`cross_group_disclosure_denials_total`
- `qzone_action_attempts_total`、`total_estimated_cost_usd`

日志应使用 `trace_id/task_id/event_id` 链路关联，包含必要标签但默认不记录原始完整个人聊天。Tracing 可以用 OpenTelemetry；监控 Prometheus/Grafana 为可选，不必第一版全量上齐。

---

## 21. 里程碑、依赖顺序与交付验收

> **V2 补充**：以下为原基础分期；最新版总体实施顺序与双运行时、经验学习、夜间研究的验收以第 44～45 章为准。


> 以下是**阶段划分**，不是工期承诺；取决于参与人数、预算、NapCat 环境和模型接入条件。阶段结束均应有可独立运行的发布版本。

### M0：工程地基（最优先）

**任务**：Monorepo、Python 核心结构、配置验证、Alembic、Fake OneBot、事件 Envelope、消息去重、Health API、基础日志、权限 Port、单元测试、GitHub CI。

**完成标准**：不需要真实 QQ 就能跑测试；能插拔平台事件输入；基础 schema/插件接口稳定。

### M1：可以使用的多群文字机器人

**任务**：NapCat 反向 WS、消息接收与发送、@ / 引用、跨群队列、短期线程、决策系统、主模型与低成本模型 Router、Outbox、群白名单、基础 Admin 消息搜索与评价。

**验收**：两个测试群同时接入，直接 @ 可正常回复；普通闲聊大量选择沉默；断线不明显重复回复；后台查得到每条机器人发言。

### M2：完整记忆基础与隐私

**任务**：天记忆、夜间巩固、长期人物/群记忆、用户授权、自我跨群回忆、pgvector 混合检索、记忆删除、更正、Replay Lab 最小版。

**验收**：A 群给出的可共享本人事实在 B 群被本人问到时能回忆；未授权第三人无法套取；夜间任务失败可重试。

### M3：群文件、群公告和知识库（本次新增）

**任务**：`GroupResourceAdapter`、群文件目录同步、上传通知、公告轮询、只读元数据、受控文档解析、知识文档关联、后台文件树、公告版本、引用回答。

**验收**：能回答当前群“公告说什么”“某文件在哪里”“已获准索引 PDF 的哪页有答案”；不能跨群泄漏，也不能随意删除文件。

### M4：图片、表情包、自然度优化

**任务**：视觉 Provider、OCR、图片下载、表情包标签/去重/权限、Naturalness Evaluator、反馈集与候选回放。

**验收**：对图片提问能解释；普通发图可选择沉默；不自作主张把素材跨群分发；回放可以比较不同模型/Prompt。

### M5：外部研究与深度任务

**任务**：Search Provider、DeepSeek 官方 API、证据追溯、知识新鲜度、工具 Schema 与审批中心。

**验收**：最新知识问题能引用有时间戳的公开来源；网络服务失败时不伪造已验证事实。

### M6：虚拟电脑与视频、语音

**任务**：沙箱隔离、资源限制、Python/浏览器/截图、对象存储、视频/ASR/TTS Provider、任务取消、工具执行日志。

**验收**：受限脚本与截图可安全回传；恶意脚本不能读主机密钥；视频服务不可用时可正常降级。

### M7：开源插件生态与 QQ 空间实验

**任务**：稳定 Plugin SDK、插件样例、开发者文档、第三方平台契约测试、社区贡献流程、实验性 QZone 插件。

**验收**：外部开发者能不改核心代码实现一个只读工具插件；QQ 空间功能默认关闭，兼容性经单独验证才启用。

### 阶段之间的硬门槛

- 未完成权限/事件日志，不进入跨群长记忆。
- 未完成文件下载安全与内容授权，不开启群文件全文索引。
- 未完成隔离执行器与工具审批，不向普通群友开放执行代码。
- 未完成消息评价与回放，尽量不要大幅提高主动发言频率。
- 未核实服务条款与账号风险，不把 QZone 网页私有接口或 DeepSeek 网页抓取当作发布必需依赖。

---

## 22. 开源许可、知识产权与社区治理

### 22.1 项目许可证选择

**建议自研代码**可考虑 `Apache-2.0`（带专利条款和 NOTICE 义务）或 `MIT`（简洁宽松）。选择应基于维护者期望、所采用的依赖许可证与法律咨询；不要把 NapCat 的许可证误认为 MIT/Apache。

**特别警示：NapCatQQ 当前仓库 LICENSE 具有非商业与再分发限制**，并非普通宽松开源许可。本项目应：

- 自己开源 OneBot 适配器及上层 Agent 逻辑，而不是把 NapCat 的源代码或修改版复制进仓库。
- `README` 通过官方站点指导用户**自行安装独立依赖**。
- 不未经授权捆绑 NapCat 代码、二进制、Docker 镜像或其衍生修改版发布。
- 对部署、商业使用和推广方式进行独立合规评估；即使自研部分 Apache-2.0，也不意味着 NapCat 自身可用于商业用途。
- 建立 `THIRD_PARTY_NOTICES.md`，列出所有插件、模型、数据集、解析库的许可与来源。

官方许可证：<https://github.com/NapNeko/NapCatQQ/blob/main/LICENSE>。

### 22.2 非代码资产的许可

- 预置知识库不能整站复制未经授权的 Wiki/论坛/付费教程。
- 群文件和 QQ 聊天记录不能作为“开源示例数据”直接公开。
- 公开测试夹具只能使用人造/明确获授权的数据。
- 模型权重、API 和视觉/语音模型各有自己的使用许可；与仓库 LICENSE 分开。
- 表情包素材可能涉及著作权/肖像权，示例仓库只能用获授权或原创素材。

### 22.3 社区治理文件

`README.md`（愿景、截图、快速开始、已支持能力）、`CONTRIBUTING.md`（PR 与架构贡献）、`CODE_OF_CONDUCT.md`、`SECURITY.md`（私下上报漏洞）、`CHANGELOG.md`、`GOVERNANCE.md`（维护者决策）、`ROADMAP.md`、`ADR/`、`THIRD_PARTY_NOTICES.md`、`docs/plugin-api.md`。

### 22.4 贡献流程

- Issue 使用类型：Bug、Feature、Provider、Security、Docs、Question。
- 重构前写 ADR/RFC；新 API/插件类型必须先写 Schema 与契约测试。
- 重大变更使用弃用窗口，避免第三方插件突然失效。
- PR 必须包含测试、配置变更说明、迁移影响、权限影响、文档更新。
- 发布采用 SemVer，附数据库迁移与回滚指南；定期发布 nightly / beta / stable。

### 22.5 安全披露与私有数据

公开 Issue、错误日志、CI 日志不能包含真实 QQ 号、群文件原文、用户记忆、Cookie、Token。维护者提供脱敏复现模板及 Fake OneBot 测试工具，避免贡献者上传真实私人聊天资料。

---

## 23. 需要通过 ADR 正式确认的架构决策

建议建立这些 ADR 文件，并在第一次实现相应功能前锁定：

- `ADR-0001-modular-monolith.md`：为何选模块化单体 + Worker。
- `ADR-0002-event-envelope.md`：统一事件字段、幂等键与 schema 版本。
- `ADR-0003-memory-scope.md`：跨群记忆与披露权限模型。
- `ADR-0004-pgvector.md`：向量方案、embedding 版本迁移。
- `ADR-0005-plugin-security.md`：插件隔离和权限申请。
- `ADR-0006-group-resource-ingestion.md`：群文件/公告同步与索引策略。
- `ADR-0007-outbox-delivery.md`：消息发送一致性与失败未知状态。
- `ADR-0008-sandbox.md`：代码执行安全边界。
- `ADR-0009-prompt-feedback.md`：自然度评价如何进入离线优化，而不是直接改 Prompt。
- `ADR-0010-platform-licenses.md`：第三方受限许可证的依赖分发原则。

---

## 24. 技术风险、未确定项与降级策略

| 风险/未确定项 | 影响 | 应对 |
|---|---|---|
| NapCat 更新造成 Action 返回变化 | 文件与公告功能故障 | `CapabilityRegistry`、Adapter 契约测试、版本兼容矩阵 |
| NapCat 与 QQ 客户端账号风控 | 机器人掉线或账号风险 | 专用测试号、限流、遵守使用条款、人工控制 |
| 群公告轮询无精确变化事件 | 无法实时知道编辑 | 周期刷新、按需刷新、标注最近同步时间 |
| 群文件链接失效 | 下载失败 | 重新获取 URL、有限次重试、不长期缓存链接 |
| 跨群记忆身份正确但披露不合适 | 隐私泄漏 | 访问与披露分离、默认不共享、用户授权、输出审查 |
| 向量召回错人或错细节 | 记忆幻觉 | 强实体/群过滤 + 原文证据核对 + Reranker |
| 群友把梗/谣言反复说 | 知识污染 | `community_claim` 不自动升级，记录来源和版本 |
| 聊天模型强行接梗 | “AI 味”过重 | 意图先行、适度沉默、评价回放、候选重写/放弃 |
| 昂贵视频和联网工具被滥用 | 费用激增 | 每群/全局配额、审批、低成本模式、超时 |
| 浏览器/文件 Prompt Injection | 工具越权 | 不可信数据降权、沙箱与不可绕过的 Tool Policy |
| 插件任意代码 | 凭据泄漏/供应链攻击 | 社区插件独立进程、权限审批、签名与依赖扫描 |
| DeepSeek 网页版无法合法稳定自动调用 | 研究流程失败 | 官方 API + 独立 Search Provider，网页实验接口默认禁用 |
| QQ 空间无稳定正式 API | 发帖/点赞不可用 | 实验插件、默认关闭、不可用时不影响群聊 |
| 开源后有人错误捆绑 NapCat | 许可证风险 | 独立适配器、NOTICE、安装说明与发布审核 |

---

## 25. 最小可运行切片（建议第一批实际写代码）

**目标**：不是第一天就拥有所有功能，而是搭起以后不会推翻的骨架。

建议最早实现这 12 件事：

1. `EventEnvelope` / `EventBus` / `PlatformAdapter` 三个基础接口。
2. NapCat 反向 WebSocket、Fake OneBot Event Generator。
3. `messages`、`events`、`bot_messages`、`decision_logs` 基础数据库。
4. OneBot 事件去重、连接恢复、可确认的消息发送日志。
5. `GroupPolicy` 及管理员停用开关。
6. 群级 `ConversationTracker` 与 @/引用规则。
7. `ModelRouter` 接一个兼容 API 的主模型与一个决策模型。
8. `Outbox` + 单群发送锁 + 过期候选检查。
9. `DailyEpisode`、`MemoryRepository` 的接口，即便初期只做最简单实现。
10. `GroupResourceAdapter` 的接口与 Mock，实现读取群公告/列文件元数据。
11. Admin Web 仅做群列表、消息搜索、查看详情、评价提交。
12. Replay 单元测试集，确保普通人对话时机器人可以 IGNORE。

**这个最小切片验收之后**，再顺序实现真实群公告同步、群文件内容索引、夜间巩固和跨群授权检索。这样代码从第一天就是结构化的，而且项目可以按阶段开源，吸引社区贡献。

---

## 26. 官方文档与参考资料（截至 2026-10-15）

下列链接为架构设计时查阅的资料入口。第三方项目、模型接口和 QQ 生态变化很快，正式开发时应核对目标部署版本、返回字段、当前许可证及使用条款。

1. NapCat OneBot API 总览：<https://napneko.github.io/onebot/api>
2. NapCat OneBot 事件文档：<https://napneko.github.io/onebot/event>
3. NapCat 兼容 API 列表：<https://napneko.github.io/develop/api>
4. NapCat 文件处理指南：<https://doc.napneko.icu/develop/file>
5. NapCat 仓库：<https://github.com/NapNeko/NapCatQQ>
6. NapCat 当前许可证：<https://github.com/NapNeko/NapCatQQ/blob/main/LICENSE>
7. pgvector 项目与混合检索示例：<https://github.com/pgvector/pgvector>
8. pgvector Python RRF 示例：<https://github.com/pgvector/pgvector-python/blob/master/examples/hybrid_search/rrf.py>
9. BGE-M3 模型说明：<https://huggingface.co/BAAI/bge-m3>
10. Qwen3-VL 项目：<https://github.com/QwenLM/Qwen3-VL>
11. DeepSeek 官方开发者文档：<https://api-docs.deepseek.com/zh-cn/>
12. DeepSeek 开放平台服务条款：<https://cdn.deepseek.com/policies/en-US/deepseek-open-platform-terms-of-service.html>
13. Playwright Screenshot：<https://playwright.dev/python/docs/screenshots>
14. Docker Rootless Mode：<https://docs.docker.com/engine/security/rootless/>
15. PostgreSQL 文档：<https://www.postgresql.org/docs/>
16. OWASP LLM 应用安全项目：<https://genai.owasp.org/>
17. OpenTelemetry 文档：<https://opentelemetry.io/docs/>

---

## 27. 实施时需要填写的项目决策表

这些不是本设计的阻碍；**没有明确答案时采用“默认关闭高风险能力、保留配置接口”的原则**。

| 决策项 | 建议默认值 | 后续操作 |
|---|---|---|
| 正式项目名 | OpenSocialAgent | 确定组织/仓库名 |
| 自研代码许可证 | Apache-2.0（待维护者确认） | 补 NOTICE 和依赖许可审查 |
| QQ 接入 | NapCat OneBot 11 反向 WS | 测试目标版本 |
| 首发群数 | 1～2 个自有测试群 | 灰度逐群加入 |
| 跨群个人记忆 | 默认不共享 | 设计用户同意和撤销 UX |
| 群文件内容索引 | 默认关闭 | 群授权后分格式开放 |
| 群公告写操作 | 关闭 | 管理员审批 |
| QQ 空间自动互动 | 关闭 | 独立合规与兼容性测试 |
| 沙箱执行 | 初期关闭 | 独立 VM/强化隔离后灰度 |
| 主模型供应商 | 可配置 | 选 API 并测自然度 |
| DeepSeek 研究 | 官方 API + 独立搜索 | 选择 Search Provider |
| Embedding | BGE-M3 候选 | 比较成本与中文群数据效果 |
| 原始聊天保留 | 30 天建议值 | 明确群成员告知与清理 |
| 默认主动性 | 保守 | 用人工标注数据调整 |

---

## 28. 最终验收总清单

### 架构与扩展

- [ ] 核心不直接依赖 NapCat、某家模型或具体向量实现的 SDK。
- [ ] 有 Plugin SDK、Manifest、契约测试和模拟插件。
- [ ] 统一事件格式可版本化，任务队列和消息 Outbox 有幂等与故障恢复。
- [ ] 每个模块有清晰责任、日志、错误码与配置 Schema。

### 社交与自然度

- [ ] 多群可并行工作，同一群不乱序刷屏。
- [ ] 多话题追踪、@/引用回复、发言前过期检查完善。
- [ ] 主动回复可保持沉默，受到明确拒绝可停下。
- [ ] 后台能搜索消息、给出多维评价、对话回放和策略回滚。

### 记忆与知识

- [ ] 原始消息、工作记忆、天记忆、长记忆、知识文档彼此分开。
- [ ] 每晚巩固按群日期执行，失败可幂等重试。
- [ ] 长期事实有出处、主体、版本、可信度、时效与状态。
- [ ] 同一人跨群回忆有用户授权；他人或其他群不能越权检索。
- [ ] 用户和群管理员可以查看/纠正/删除及撤销授权。
- [ ] 公共知识与群友未经验证的说法明确区分。

### 群文件与公告

- [ ] 可以读取群文件目录、文件夹与文件资源链接，异常安全失败。
- [ ] 可监听群文件上传事件并增量索引。
- [ ] 群公告支持读取、定期刷新、版本追踪和来源时间显示。
- [ ] 文件内容需经额外授权才下载/解析/向量化。
- [ ] 群文件/公告默认群内可见，不会因向量检索跨群泄漏。
- [ ] 删除文件/发公告/上传文件默认处于高风险审批控制下。

### 多模态与工具

- [ ] 图片/视频/OCR/语音/Embedding API 相互独立，可配置与降级。
- [ ] 表情包有来源、去重、标签、权限与限频。
- [ ] 外部研究能够展示来源、查询时点和可信度。
- [ ] 代码执行在隔离环境进行，资源、网络、凭据和文件 ACL 受控。
- [ ] 任务取消、文件截图回传、安全日志和错误恢复可验证。

### 开源与运维

- [ ] 代码与资料许可证完成审核，不未经授权再发行 NapCat。
- [ ] README/贡献规范/安全上报/示例配置/开发文档齐全。
- [ ] CI 通过基础代码质量、契约测试、安全与迁移测试。
- [ ] 关键指标、错误、API 费用、备份恢复与紧急停止可用。
- [ ] 测试夹具不包含真实 QQ 私密信息。

---

## 29. 收尾说明

本方案的核心不是“让模型具有尽可能多的功能”，而是让所有功能服从统一的**社交语境、权限控制、可核查记忆、结构化任务系统**。机器人可以知道很多、会做很多，但不需要每次都展示；可以跨多个群存在，但不能把跨群记忆误当作传播权限；可以学习新知识，但不能把未经验证的群聊或外部 AI 答案当作事实；可以访问虚拟电脑，但不能跨越沙箱和权限边界。

项目开源的成功标志，不只是一份能运行的机器人代码，而是**稳定的扩展契约、可靠的迁移和测试、明确的安全默认值，以及可持续的贡献者体验**。先交付最小可运行切片，再基于真实反馈增量扩展，比一次性实现所有高风险能力更稳妥。

---

# 第Ⅱ部分：V2.0 升级专项设计（对前述 V1.0 章节具有补充及冲突优先效力）

> 设计状态：**已确定需求**与**待验证实现**严格区分。以下 V2 专项设计不是一个独立于前文的项目，而是对同一个 OpenSocialAgent 的架构增量升级。凡涉及“工作时主动聊天”“任务状态”“经验/知识验证”“复读行为”的旧示例与本部分冲突时，以本部分为准。
>
> 开发约束：保留原方案的跨群身份、群文件与公告、视觉/视频/表情包、QQ 空间适配器、自然度评价、权限边界和插件 SDK；不得借本次升级把它们从规划中移除。

## 30. 升级需求、范围与架构决策

### 30.1 本次明确新增的需求

| 编号 | 必需能力 | 验收摘要 | 优先级 |
|---|---|---|---|
| UP-001 | 社交与工作两个运行时分离 | 编程任务持续进行时仍能对 QQ @ 实时回应 | P0 |
| UP-002 | 忙碌状态抑制主动水群 | 有主要工作任务时不主动找话题；工作完成恢复候选资格但不立刻水群 | P0 |
| UP-003 | 任何时间查询真实工作进度 | 能返回当前步骤、最近进度时间、已完成/失败测试、阻塞原因 | P0 |
| UP-004 | 修改需求、暂停、恢复、取消 | 操作被记录为带权限检查的任务命令，不丢失工作区 | P1 |
| UP-005 | 长任务持久化与崩溃恢复 | 容器或进程重启后可从检查点恢复，绝不伪报任务成功 | P0 |
| UP-006 | 自动学习自己踩过的坑 | 记录报错、环境、尝试、真实结果、证据和适用版本 | P1 |
| UP-007 | 从授权群聊提取“问题—方案—反馈” | 未验证与已验证分开；允许建立“尚未解决”记录 | P1 |
| UP-008 | 跨群复用经过授权的技术经验 | 仅共享可传播的通用结论，不透露来源群私聊或秘密 | P1 |
| UP-009 | 夜间记忆巩固自动调用研究系统 | 可在预算内对不确定的可核实技术事实调用 DeepSeek + 搜索 | P1 |
| UP-010 | 外部 AI 结论不得直接当验证 | 记录证据、检索时点、官方原始出处、适用版本、可信状态 | P0 |
| UP-011 | +1 队形只识别、只静默 | 不开发自动复读，不参与 +1，不打断；有独立 +1 模块 | P0 |
| UP-012 | 工作、研究、经验后台可审计 | 可搜索任务、回放日志、查看证据、评审经验与来源 | P1 |
| UP-013 | 可开源、可插件化 | 核心协议版本化；外部模型、向量引擎、沙箱、平台都可替换 | P0 |

### 30.2 不改变的核心原则

- **一个人格、两个运行时**：Social Runtime 和 Work Runtime 的逻辑隔离，均使用同一个 AgentIdentity；不得制造两套互相冲突的“记忆大脑”。
- **事件而非聊天驱动执行**：群聊可以创建或控制任务，但不负责保有 Worker 内存状态；所有长任务通过 Task Registry 和持久队列执行。
- **不抢群友工作**：即使查到了排错经验，也要经过群聊发言决策；别人正在解决时可只记下来。
- **不泄露跨群隐私**：知识复用与原始群聊转述是两种权限。先查询权限，再做向量召回，不可事后仅用 Prompt 遮盖。
- **原生 +1 独立**：用户已有复读模块。Bot 仅消费其可选状态事件，或独立做轻量只读队形检测；任何情况下 Bot 不复读、不点击 +1、不调用群友复读 API。
- **可验证的进度**：禁止由聊天 LLM 猜测“做到 80%”。后台数字只能由结构化里程碑或可量化任务步骤计算。

### 30.3 决策记录 ADR

| ADR | 决定 | 理由 | 后续可替换点 |
|---|---|---|---|
| ADR-030 | 两个独立 Python 进程/容器运行 Social / Work | 社交不被长时 Agent loop 阻塞 | Worker 技术不绑定 Python |
| ADR-031 | PostgreSQL 为任务和记忆真相源 | 支持事务、行锁、权限、证据关联、回放 | 存储 Repository 抽象 |
| ADR-032 | 可靠队列采用数据库租约 + Outbox 起步 | 低复杂度，避免先上复杂分布式中间件 | 后续接 Redis Streams / NATS |
| ADR-033 | 沙箱工作区和执行环境分离 | 重启继续项目且不泄露宿主机 | Docker 强化隔离 / 微虚拟机 Provider |
| ADR-034 | 经验学习并入统一记忆目录，但有独立数据模型 | 人物事实与技术方案不能混用 | MemoryKind 扩展 |
| ADR-035 | 每晚研究为可选子任务，不阻塞天记忆基础提交 | 搜索/DeepSeek 不可用仍可巩固已确认事实 | Research Provider 可替换 |
| ADR-036 | Bot 不参与 +1，遇队形抑制其所在群的普通发送 | 不打断集体行为，与现有 +1 模块解耦 | EchoStateProvider 可选 |
| ADR-037 | 工作反馈与聊天反馈都可回放评价 | 开源维护者能迭代而不盲改 Prompt | FeedbackStore / Evaluation Runner |

## 31. 社交与工作双运行时（Dual Runtime）

### 31.1 物理部署与逻辑边界

```mermaid
flowchart TB
    subgraph Access[入口 / 出口]
      QQ[QQ 群/私聊] <--> NC[NapCat / OneBot Adapter]
      UI[管理后台]
    end
    subgraph Social[Social Runtime - 可独立扩缩容]
      OBS[Conversation Observer]
      DEC[Social Decision Engine]
      SEND[Send Policy & Outbox]
      PRES[Presence Projection]
    end
    subgraph Core[共享控制平面]
      REG[Task Registry]
      BUS[Command/Event Bus]
      ACL[Policy / Identity]
      MEM[Memory / Knowledge API]
      RS[Research Task API]
      PG[(PostgreSQL + pgvector)]
    end
    subgraph Work[Work Runtime - 可独立重启]
      WRK[Task Orchestrator]
      AG[Work Agent Loop]
      EVID[Evidence Recorder]
      CK[Checkpoint Manager]
      BOX[Isolated Sandbox/Browser]
      ART[Artifact Store]
    end
    NC --> OBS --> DEC --> SEND --> NC
    DEC <--> PRES
    DEC --> ACL
    DEC --> MEM
    PRES <-->|任务状态快照| REG
    UI --> REG
    REG <--> BUS
    BUS <--> WRK
    WRK --> AG --> BOX
    AG --> EVID
    WRK --> CK
    BOX --> ART
    EVID --> MEM
    MEM --> PG
    REG --> PG
    RS --> PG
```

**不得发生的调用**：Social Runtime 不直接访问 Work Agent 的模型会话、浏览器 Cookie、命令执行器或临时私有文件；Work Runtime 也不得直接使用 NapCat 群发接口。所有交互必须通过版本化 Command/Event 事件、受控读取接口或最终 Artifact 交付请求。

### 31.2 各运行时的职责清单

**Social Runtime**：持续消费 QQ 事件；短期上下文、图片理解结果、复读/静默状态；被 @ 回复；查询任务进度；提出 `TaskCommand`；普通聊天输出；决策反馈。应有独立的小模型预算与快路径，保证 Work Agent 卡住不会拉长被 @ 的响应。

**Work Runtime**：创建隔离工作区；任务规划、分步骤写代码、执行测试、获取截图、存 Git 检查点、恢复中断、记录错误与有效修复方案；遵守资源/权限配额。不能从群消息中直接越权调用工具。

**Shared Core**：用户身份与能力授权、任务元数据、记忆索引、Research Engine、持久事件、告警、费用与审计、Artifact Store ACL。共享状态不等于共享 mutable 对象或 Python 全局变量。

### 31.3 Presence：忙碌的是社交状态，不是服务器

`Presence` 是**由实际工作任务推导出的展示与调度状态**，不是让 LLM 自行宣称自己在忙。建议：

```text
SocialPresence = FREE | WORKING | FOCUSED | AWAY | QUIET
ChatMode(group) = NORMAL | ECHO_SILENT | GROUP_MUTED | QUIET_HOURS
WorkTaskState = QUEUED | RUNNING | WAITING_INPUT | ...（见第32章）
```

- `FREE`：无主要工作任务；可参与经评分通过的普通群聊。
- `WORKING`：存在主要工作任务且未完成；**全群禁用“普通主动水群”与主动开话题**，但接收消息、更新记忆、处理被 @、进行真实进度回复仍然正常。
- `FOCUSED`：可选管理员设置的专注档；被 @ 仍允许回应，非关键需求可以入队延后，不无提示地消失。
- `AWAY`：网关或相关模型不可用；只在有可用通道时说明真实不可用状态。
- `QUIET`：管理员静音/规则禁用；优先级高于“被 @”，但可以保留独立管理通知渠道。
- 后台夜间整理、向量化、群文件索引**不计入主要工作**，否则机器人每天凌晨会反复显示忙碌。
- 任务完成：先处理授权范围内的交付或完成通知，再将 Presence 降为 `FREE`；**不能自动向所有群宣布自己闲下来了**。

### 31.4 发送策略的决策顺序（从强到弱）

1. 平台/群被禁用、用户拉黑、权限撤销、管理员停止操作 → 不发送。
2. 目标群 `ECHO_SILENT`、群禁言、明确禁止打断 → 抑制普通回复；管理员关键指令走单独路径，普通 @ 进入待处理或可延迟的受限答复通道。
3. 目的群/请求者/任务结果的披露 ACL → 不合格内容不得发送，即使请求者知道任务 ID。
4. `WORKING/FOCUSED` → 拦截 `INITIATE` 和未请求的 `JOIN_THREAD`，放行授权的 `MENTION_REPLY`、`TASK_STATUS`、`TASK_COMMAND_ACK`（必要时排队）。
5. 群级/全局速率限制、过期检测、重复判断、输出自然度 → 最终发送或丢弃。

**注意**：2 与 4 同时存在时，**只有当前群**受到复读静默；WORKING 是跨群的“主动社交关闭”，而非群级全面禁言。高优先级的安全管理操作可以通过管理后台生效，即使 QQ 消息暂缓发送。

### 31.5 不阻塞响应：编程任务运行的快路径

```mermaid
sequenceDiagram
  participant U as QQ 用户
  participant S as Social Runtime
  participant T as Task Registry
  participant W as Work Runtime
  U->>S: @Bot 写到哪了？
  S->>T: GET task_progress (ACL + snapshot)
  T-->>S: 当前步骤/最后日志时间/测试证据
  S-->>U: 简要报告真实进度
  Note over W: 原 Worker 继续执行，无需中断
  U->>S: 加一个计时器
  S->>T: POST task_commands (append requirement)
  T-->>S: 命令入队 + command_id
  S-->>U: 收到，排进当前任务了
  W->>T: 下一安全边界领取变更
```

预期非目标：在 Worker 仍阻塞在原子编译、网络等待或长测试时，无法保证立即把用户的新要求应用于程序；此时应显示“已收到/尚未应用”，不能声称“已经改好了”。

### 31.6 QQ 消息语义路由（例）

| 文本意图 | 路由 | 是否变更工作区 | 是否需任务授权 |
|---|---|---|---|
| “写到哪了”“还有多久”“进度” | `TASK_STATUS_QUERY` | 否 | 查询 ACL |
| “看一下现在界面”“发截图” | `TASK_ARTIFACT_READ` / 创建只读截图子任务 | 可能新建截图文件 | 查看权限 |
| “加个计时器” | `TASK_CHANGE_REQUEST` | Worker 以后执行 | 任务发起人/协作者 |
| “先暂停”“继续” | `PAUSE` / `RESUME` | 不删除 | 任务控制权限 |
| “算了别做了” | `CANCEL_REQUEST` | 按保留策略处理 | 任务控制权限 |
| “别水群了” | `PRESENCE_OVERRIDE` | 否 | 群/全局权限 |
| 普通聊天 @ | `MENTION_REPLY` | 否 | 群发言规则 |
| 未 @ 的技术问题 | `ADVICE_CANDIDATE` | 否 | 社交决策 + 知识披露 |

### 31.7 消息回放与原始思维链

运营后台保存可审计的**计划、步骤、工具输入输出摘要、证据 ID、状态变迁**。模型的不可获取或受服务协议限制的内部推理内容并非系统依赖项；不会因为切换模型就失去可观测性。工作记录必须保留失败与取消，禁止事后用模型修饰成“完成”。

## 32. 长任务持久化、调度和命令协议

### 32.1 工作任务状态机

```mermaid
stateDiagram-v2
  [*] --> REQUESTED
  REQUESTED --> POLICY_CHECK
  POLICY_CHECK --> DENIED
  POLICY_CHECK --> WAITING_APPROVAL
  POLICY_CHECK --> QUEUED
  WAITING_APPROVAL --> QUEUED: approve
  WAITING_APPROVAL --> DENIED: reject
  QUEUED --> RUNNING: lease acquired
  RUNNING --> WAITING_INPUT: missing user info
  WAITING_INPUT --> QUEUED: input provided
  RUNNING --> PAUSING: pause request
  PAUSING --> PAUSED: safe checkpoint
  PAUSED --> QUEUED: resume
  RUNNING --> VALIDATING: all steps done
  VALIDATING --> COMPLETED: proof passed
  VALIDATING --> RUNNING: repair needed
  RUNNING --> FAILED: unrecoverable
  RUNNING --> INTERRUPTED: worker lease expired
  INTERRUPTED --> QUEUED: recovery plan
  RUNNING --> CANCEL_REQUESTED: cancel
  PAUSING --> CANCEL_REQUESTED: cancel
  CANCEL_REQUESTED --> CANCELLED: stop confirmed
  QUEUED --> CANCELLED: cancel
```

`QUEUED` 与 `RUNNING` 不意味着“已经开始写代码”，分别表示等待资源和已领取租约。`WAITING_INPUT` 不自动进入静默永久等候；保持明确的待确认项，并向请求者推送一次必要的问题。`INTERRUPTED` 是未确认是否执行完的状态，不可直接视为 FAILED 或 COMPLETED。

### 32.2 Task Registry 字段

- 身份：`task_id`、`task_type`、`tenant_id`、`requester_id`、`source_group_id`、`source_message_id`、`visibility_scope`、`project_id`。
- 生命周期：`status`、`priority`、`created_at`、`started_at`、`last_progress_at`、`finished_at`、`due_at`、`deadline_at`。
- 执行：`assigned_worker`、`lease_id`、`lease_expires_at`、`attempt_count`、`checkpoint_seq`、`plan_version`、`command_version`、`step_cursor`。
- 进度：`current_step_id`、`current_step_summary`、`milestones_done`、`milestones_total`、`tests_passed`、`tests_failed`、`blockers`、`estimated_percent_nullable`。
- 安全：`granted_capabilities`、`approval_ref`、`resource_budget`、`idempotency_key`、`policy_snapshot_id`。
- 存档：`workspace_id`、`artifact_manifest_id`、`last_git_commit`、`error_code`、`recovery_plan_ref`。

### 32.3 任务控制命令必须可审计

```json
{
  "schema_version": "1.0",
  "command_id": "cmd_01JAB...",
  "task_id": "task_01JAB...",
  "actor_id": "qq:10001",
  "origin": {"platform": "qq", "group_id": "A", "message_id": "M100"},
  "kind": "ADD_REQUIREMENT",
  "payload": {"text": "加一个计时器", "priority": "normal"},
  "expected_task_version": 17,
  "idempotency_key": "qq:A:M100:task_01JAB:ADD_REQUIREMENT",
  "created_at": "2026-10-15T13:16:00+08:00"
}
```

- 仅 `TaskCoordinator` 验证角色和状态转换；命令不能由群文件、网页或工具输出伪造。
- `TASK_STATUS_QUERY` 是独立只读查询，不创建控制命令，不申请 Worker 的“对话锁”。
- `ADD_REQUIREMENT` 需经过版本检查：版本冲突返回最新版本，客户端决定重试/重新合并；不要悄悄覆盖其他人的需求。
- 对 Work Agent 的需求变更使用**追加日志 + 下一个安全步骤采纳**；采纳后记录 `applied_at`、`plan_version`、受影响测试。
- 暂停是“尽快到安全检查点”，不是强杀导致仓库不一致；取消允许强制终止子进程，但先标记不可靠产物。

### 32.4 Worker 租约与围栏（fencing）

**危险场景**：Worker 1 心跳停了，队列将任务交给 Worker 2；Worker 1 又恢复并继续写同一目录，产生双重修改。

设计：在 Task Registry 事务内递增 `lease_generation` 并签发短期租约；每次提交 Checkpoint、发布 Artifact、更新任务状态时验证**当前租约世代**；旧 Worker 写入拒绝。工作目录由 workspace generation/快照机制隔离，不允许两个活动租约同时写入同一目录。任何发送通知均经 Outbox 去重。

示意伪代码：

```python
async def acquire_work_task(db, worker_id):
    # 事务中 SELECT ... FOR UPDATE SKIP LOCKED 查找 QUEUED/可恢复任务
    task = await db.claim_next_task(worker_id=worker_id)
    return task  # (task_id, lease_generation, lease_deadline)

async def persist_checkpoint(db, task_id, generation, checkpoint):
    if not await db.lease_is_current(task_id, generation):
        raise StaleWorkerLease(task_id)
    await db.insert_checkpoint_and_advance_cursor(task_id, generation, checkpoint)
```

数据库任务日志是历史依据；Redis 可加速队列，但**不能成为唯一任务真相源**。消息投递采用 at-least-once，消费者必须用 `event_id/idempotency_key` 幂等处理。

### 32.5 工作任务的优先级与公平性

- `P0`：管理员停止/撤销权限、任务取消、账号风险处理。
- `P1`：被 @ 查询任务状态与被授权任务控制（走 Social 快路径，无需占用工作 Worker）。
- `P2`：运行中的工作步骤、恢复任务、用户显式请求的工具任务。
- `P3`：图片/视频耗时分析、研究任务、批量群文件索引。
- `P4`：夜间巩固、Embedding 重建、非紧急知识再验证。

实行多队列隔离与公平分配：长任务有并发配额，绝不耗尽群聊快路径模型预算；任务运行中也不能无限抢占其他群的资源。预算超过上限进入 `WAITING_APPROVAL/PAUSED`，不暗中继续消费。

### 32.6 检查点与恢复规则

- 每完成一个可验证里程碑写入 `TaskStepResult`、`Checkpoint`、Git SHA、输入/输出摘要、测试证据。运行中的文件先写临时路径，提交时原子替换。
- 原子命令（例如一次构建）中断后无法“从 CPU 上一条指令恢复”；重启必须从**可重试步骤**再执行，检查是否已有有效产物。
- 因网络、进程重启造成的未知提交必须先检查幂等键与实际仓库状态，不直接重复向 QQ 发送交付。
- 每个检查点标记 `commit_sha`、`plan_version`、`tool_image_digest`、`files_manifest_hash`、`test_report_id`、`timestamp`。
- Git 不保存秘密文件；`.gitignore`、密钥扫描和 workspace ACL 均需覆盖 checkpoints/artifacts。

### 32.7 真实进度的标准响应

```json
{
  "task_id": "task_01JAB",
  "status": "RUNNING",
  "last_progress_at": "2026-10-15T13:15:33+08:00",
  "current_step": "test_first_click_safety",
  "milestones": [
    {"key":"init","state":"done","proof":"commit:81ab"},
    {"key":"board","state":"done","proof":"test-report:88"},
    {"key":"first_click","state":"running","proof":null},
    {"key":"timer","state":"queued","proof":null}
  ],
  "blocker": null,
  "stale": false,
  "estimate": null
}
```

`last_progress_at` 超过阈值应显示“进度未更新”，并可触发健康检查；LLM 不得凭任务耗时估算成 95%。日常 QQ 回复可把快照压缩成一句符合群风格的短话，详情通过后台查看。

### 32.8 完成通知与交付

完成事件载有 `owner_id`、`destination_scope`、`artifact_ids`、`verification_result`，Social Delivery Service 在披露 ACL、静默状态、群授权均通过后发送；不通过则在后台/私聊可授权位置保留待领取结果。发送失败不会回滚任务成功状态，按 Outbox 重试并标记 `delivery_pending`。

## 33. 工作空间、编程任务执行与观察证据

### 33.1 工作空间不是聊天上下文

每个项目有一个 `ProjectWorkspace`，对应持久项目文件、源控制仓库和访问 ACL。每次任务在该项目下拥有独立运行目录与 `TaskContext`。示例：

```text
/workspaces/<workspace_uuid>/
├── repo/                       # 工作项目 Git 仓库；不暴露给其他任务
│   ├── src/
│   ├── tests/
│   └── .git/
├── .agent/
│   ├── manifest.json          # 项目标识、权限、负责人；非可信指令源
│   ├── plans/                 # 不同 plan_version
│   ├── checkpoints/           # 检查点清单（非所有原始日志）
│   ├── test-reports/          # XML/JSON/日志脱敏结果
│   └── evidence/              # 可追溯运行观察证据
└── outputs/                   # 经安全审核才可导出
```

多租户隔离原则：`workspace_id != user_id != task_id`；一个用户可拥有多个项目，一个项目可有多个经授权协作者，某项目可被同一人的不同群请求访问，但**目标群看到什么由 `artifact_scope` 决定**。同一群发起的私密工作不能因为机器人识别出同一 QQ 号就被发到另一个群。

### 33.2 Work Agent 执行步

```text
Plan(version=n)
  -> Select atomic step
  -> Check Policy/Resource Budget
  -> Prepare sandbox & dependency cache
  -> Execute tool(s)
  -> Observe stdout/stderr/exit_code/artifacts
  -> Test & verify
  -> Record Evidence
  -> Commit checkpoint
  -> Decide: next / revise / ask / finish
```

标准 Agent 调度不能只靠“执行返回 0”判定成功：根据工作类型要求至少一种业务断言，例如单测通过、输出文件存在且格式合法、网页截图经检查、文档渲染正确。沙箱输出不可信，必须裁剪长度、脱敏和过滤控制字符。

### 33.3 Evidence Recorder 的事件类型

| 事件类型 | 何时记录 | 主要字段 |
|---|---|---|
| `TOOL_STARTED` | 工具执行前 | 工具名、参数摘要、权限快照、step_id |
| `TOOL_FINISHED` | 工具正常完成 | 退出码、耗时、stdout/stderr 受限摘要 |
| `TOOL_FAILED` | 运行或权限失败 | 分类错误码、原始日志受保护引用 |
| `CODE_DIFF` | 修改仓库文件 | Git commit/diff hash、文件列表 |
| `TEST_RESULT` | 运行验证 | 测试集、通过数/失败数、证据对象 ID |
| `SCREENSHOT` | 生成或采集图片 | artifact_id、时间、敏感内容审查结果 |
| `HYPOTHESIS` | 工作 Agent 提出排查假设 | 对应问题 ID、假设、证据不足项 |
| `RESOLUTION` | 故障排除候选完成 | 具体修复、前后对比、验证等级 |

工作日志可能包含密钥、用户私有源码或内部地址：先脱敏，再生成普通索引；完整原始日志在受限存储中短期留存，不允许经全局经验检索直接取出。

### 33.4 安全的增量要求应用

当任务运行时又收到“加个计时器”时：

1. Social 解析为 `ADD_REQUIREMENT`，返回 `command_id` 和“已收到”的准确状态。
2. Coordinator 校验发起人控制权限，写入任务命令日志并通知 Work。
3. Work 在当前测试/写入的安全边界结束后，从命令流读取新要求。
4. 生成新 `plan_version`，重新计算受影响步骤并明确哪些此前测试需要重跑。
5. Work 执行后记录新代码提交和证据；只有真正完成才能回复“已经加了”。

对于互相矛盾的需求，返回 `WAITING_INPUT` 请发起人确认，不自动猜测。工作中的普通 @ 不能自动变成代码变更请求，防止聊天和工作混淆。

### 33.5 项目知识与工作区检索

工作记忆包括 API 约定、依赖版本、当前设计决定和 Git 代码地图；这类检索需**绑定当前项目/版本**。`project_memory` 允许在后续同项目任务中复用，但对外社交是否可引用取决于项目 ACL。对跨项目可复用的“踩坑经验”，需要走第34～37章抽象和脱敏，不可以直接共享任意私有代码或整段日志。

## 34. 统一记忆分类与经验记忆的数据语义

### 34.1 五类记忆/知识资产的明确区分

| 类别 | 定义 | 示例 | 主要证据 | 允许跨群条件 |
|---|---|---|---|---|
| `daily_episode` | 某天某群/任务发生的事 | 昨天讨论一次代码报错 | 聊天消息、任务日志 | 默认无 |
| `personal_long_term` | 人物相对稳定事实及个人经历 | 某人做某项目 | 本人表达与授权 | 本人明确授权 + 披露适合 |
| `group_memory` | 群内共同经历、术语、梗 | 本群约定的称呼 | 群事件与管理员规则 | 默认不跨群 |
| `knowledge_claim` | 可验证的事实或技术主张 | 某版本 API 字段含义 | 官方文档/实验/来源 | 权限、许可与确认状态允许 |
| `experience_case` | 实际“问题—尝试—结果”案例 | 某 Python 环境导入报错 | 执行日志或当事人确认 | 必须脱敏、经过传播授权 |

实践技能 `skill_plugin` **不是自动晋升的知识类别**。只有审核通过、权限明确、测试覆盖的流程才可变成可执行插件。研究报告作为来源资产 `research_report` 关联知识与案例，并不是单独的人物记忆。

### 34.2 经验要存“问题”和“实例”，不能只存一句结论

```mermaid
flowchart LR
  I[Issue 标准问题] --> O[Occurrence 实际发生实例]
  O --> A[ResolutionAttempt 尝试]
  A --> E[Evidence 验证/失败证据]
  I --> S[Solution 候选方案]
  A --> S
  S --> V[Applicability 适用环境与版本]
  S --> F[Feedback 后续使用反馈]
  O --> SRC[Source 原群消息或工作任务]
  SRC --> ACL[数据归属与传播权限]
```

同一个 `Issue` 可能有多个原因或方案；一个群友提出的候选修复只是某次 `Attempt`，**不能自动使整个 Issue 进入 `verified`**。一条知识可以跨多个 `Occurrence` 链接多份证据；私有来源与共享后的抽象经验需要不同 ID 和访问策略。

### 34.3 Problem Fingerprint（结构化问题指纹）

推荐包含：`domain`、`software_or_component`、`error_code_exact`、`normalized_error_signature`、`affected_operation`、`os_family`、`software_version`、`runtime_version`、`dependency_versions`、`execution_context`、`symptom_description`、`root_cause_hypotheses`。

匹配采用**硬约束 + 语义候选**：

1. 先按可共享作用域、用户/群许可过滤，不允许在越权数据上做向量搜索。
2. 关键词查精确错误代码、组件名和版本号；向量查相似症状。
3. 用软件类别、运行环境、版本排除不兼容案例；不能把“Connection refused”泛化成一个唯一解。
4. Reranker 评估“同一个问题/一个合理线索/无关”，返回解释所依赖的可披露证据。
5. 结果与语境共同进入社交决策；若未被 @ 或群友正在互相帮助，则可能什么都不发。

问题指纹不得用“相似度 0.9”伪装为故障根因的统计置信度。

### 34.4 经验状态与证据强度

| 经验状态 | 含义 | 是否可推荐 |
|---|---|---|
| `unresolved` | 看到问题，但没有确认解决 | 只用于排查线索 |
| `proposed` | 存在候选办法，未验证 | 明确说“可以试试” |
| `verified_local` | 在机器人沙箱的具体环境中测试有效 | 相似环境下可推荐，但需标版本 |
| `confirmed_by_user` | 发起人明确确认该方案成功 | 可以推荐并注明是用户反馈 |
| `corroborated` | 有多个独立可靠实例或可靠文档 | 优先推荐，但不保证适用于所有版本 |
| `disputed` | 有冲突证据 | 避免断言，需研究或人工复核 |
| `outdated` | 已证实或高度怀疑过期 | 普通检索不优先推荐 |
| `revoked` | 来源授权撤销或内容需删除 | 立即停止检索，按要求删除衍生物 |

用**两个不同轴**存储更精确：`resolution_status`（未解决、已解决、争议）与 `evidence_level`（无证据、当事人确认、本地测试、多来源核验）。上表是面向 UI 的组合展示。失败的方案同样要保留，但作为“在某版本/条件下失败”的证据，并不直接升级有效解决方法。

### 34.5 一个结构化示例

```json
{
  "issue_key": "python.pillow.import_pil",
  "symptom": "ModuleNotFoundError: No module named 'PIL'",
  "context": {
    "runtime": "python",
    "python_version": null,
    "os": null,
    "venv": "unknown"
  },
  "case": {
    "origin_type": "group_discussion",
    "group_id": "A",
    "source_message_ids": ["m10", "m13", "m16"],
    "problem_owner": "qq:10001",
    "privacy_scope": "group_only"
  },
  "attempts": [
    {
      "action": "检查运行程序与 pip 所属 Python 是否一致",
      "result": "user_reported_success",
      "evidence_kind": "user_acknowledgement"
    }
  ],
  "proposed_solution": {
    "summary": "使用运行该脚本的 Python 来检查和安装依赖",
    "applicability": "Python 多环境时",
    "caveat": "无法从现有记录确定具体 Python 版本"
  }
}
```

对于公开技术结论，如获准跨群复用，创建 `shareable_derivative`（抽象后的通用修复知识）；它不能带有私有群 ID、聊天引用、个人身份、私有代码片段或可逆映射。保留内部受保护的来源映射仅供本群审核，撤销授权时可以定位并撤销派生条目。

### 34.6 删除与纠错必须向量同步

用户纠正“我没解决” → `occurrence` 更新反馈、撤销之前 `confirmed_by_user` 证据，重算方案信任度；如果唯一验证证据消失，则降级。用户撤销群记忆授权 → 停止所有检索和研究外发；删除对象存储文件、向量索引、缓存、共享派生项及评测样本副本（按保留要求），建立可审计擦除作业。

## 35. 经验抽取器与工作/社交的双向学习

### 35.1 Work Runtime：从错误日志形成经验

```mermaid
sequenceDiagram
  participant W as Work Agent
  participant S as Sandbox
  participant E as Evidence Recorder
  participant X as Experience Extractor
  participant K as Experience Store
  W->>S: 执行程序/单测
  S-->>E: stderr + exit code + 环境
  E->>K: 保存错误发生实例
  E->>X: issue_candidate（脱敏）
  X->>K: Issue + proposed cause
  W->>S: 尝试修复
  S-->>E: diff + rerun + test reports
  E->>K: 尝试记录、结果证据
  X->>K: 适用范围 + 决策建议
  Note over K: 证据/权限检查后才更新确认状态
```

关键规则：检测到错误时先保存原始 `Occurrence`，不等整项任务完成；修复成功后新增 `ResolutionAttempt` 和 `Evidence`。工作 Agent 每次失败不必马上调用 DeepSeek；先检索同项目实践经验与官方错误信息，在本地证据不足时再研究。

### 35.2 Social Runtime：从群聊识别“问题—答复—反馈”

用引用关系、时间窗口、参与者和主题关联消息，不因多个人连续使用“好了”“解决了”就归错对象。建议事件标签：

- `ISSUE_REPORTED`：报错/异常/无法完成的具体操作，保留症状和上下文。
- `SOLUTION_PROPOSED`：有人提出排查方案或修复命令，关联特定 issue。
- `ATTEMPT_REPORTED`：发起人表示尝试过某操作，可能成功/失败。
- `SOLVED_CONFIRMED`：发起人明确表示**该方案**解决了**该问题**，必要时核对引用线程。
- `UNRESOLVED`：仍有错误、无反馈或讨论中断。
- `CORRECTION`：有人撤回先前结论。

例如“装了啊”“在另一个环境”“还真是，搞好了”可以组合成一条**用户自报解决案例**，但不能无依据推断 Python 版本、操作系统或真正根因。

### 35.3 不同群里新问题触发经验检索

1. Group Observer 识别 `HELP_REQUEST` / `ISSUE_REPORTED`，低成本抽取问题指纹。
2. `ExperienceRetriever` 检索有权限的匹配经验，并附上核实等级、适用环境与时效。
3. `Social Decision Engine` 检查是否有人已经在帮忙、群内是否复读、Bot 是否工作中、是否被 @。
4. 允许发言时，`IntentPlanner` 选择“问一个关键环境问题”或“给出最相关一条排查建议”；聊天模型自然表达而非引用知识库元数据。
5. 后续反馈回写对应具体推荐事件，不轻易将“谢谢”解析成成功验证。

**工作中禁止主动水群的规则依然高于主动经验分享**。可后台检索、形成候选，但不发群消息；被授权用户直接 @ 求助时继续回应。

### 35.4 防污染、防越权

- 群友发的命令/脚本是资料或建议，不是机器人必须执行的命令。
- 自动研究前做脱敏；未经管理员/个人授权，不能把私有群日志、文件或源码转交第三方模型。
- 不能从来源群直接复制技术讨论全文到其他群；应只使用可披露的通用抽象结论。
- 不允许把“群友传来的代码有用”直接注册成可执行系统 Skill；新 Skill 必须有人工审核、测试与独立权限。
- 不允许学习、存储或传播 API Key、密码、Session、私人令牌等作为“有用知识”。

### 35.5 经验 feedback 更新

```text
recommendation_id -> target_issue_occurrence_id -> solution_id
  -> user_follow_up -> result_signal -> verifier -> evidence record
```

`result_signal` 只能是 `tried_failed`、`tried_worked`、`unknown`、`wrong_context` 等可解释状态。表扬、回复速度、表情数量不是修复成功率；如证据不足，保持 unknown。

## 36. 夜间巩固与 DeepSeek Research Engine 的闭环

### 36.1 设计目标

夜间巩固不再只处理社交事实，而是对以下**四条数据流**分别做整理：

- `DailySocialEpisodes`：当天聊天、群文化、事件、人物偏好；私人事实不交给外部模型“判真假”。
- `WorkEvidence`：代码错误、尝试、工具输出、验证结果、可复用经验。
- `KnowledgeCandidates`：群文件/公告、授权聊天或公开资料中可验证的技术主张。
- `ExperienceCases`：工作/群聊形成的完整或未解决问题案例。

研究系统仅用于**需要且可以外部核实的事项**。不是每条记忆都去问 DeepSeek，也不必等待所有研究返回才能完成基本的天记忆归档。

### 36.2 核心流水线与分叉

```mermaid
flowchart TD
 A[调度 group/date / work/date] --> B[读取 watermarked episodes / evidence]
 B --> C[分类/提取候选/原文事实定位]
 C --> D[权限检查/敏感字段脱敏]
 D --> E[去重、冲突检测、时效评估]
 E --> F{不确定性类型?}
 F -->|个人经历/群内私事| G[只记来源与确认状态，不外部查询]
 F -->|已验证/无必要| H[直接生成巩固操作]
 F -->|可核实技术事实| I[创建 ResearchJob]
 I --> J[公开来源检索 + DeepSeek 分析]
 J --> K[Claim Evidence Verifier]
 K --> L{证据是否足够?}
 L -->|是| M[已核实候选/适用版本]
 L -->|否或冲突| N[待验证/冲突/人工复核]
 G --> O[Review & Permission Commit]
 H --> O
 M --> O
 N --> O
 O --> P[事务写入 Memory/Knowledge/Issue + Outbox]
 P --> Q[索引更新 + 审计]
```

### 36.3 Uncertainty Detector 输出契约

```json
{
  "candidate_id": "kc_100",
  "uncertainty_kind": "VERSION_SENSITIVE_TECH_FACT",
  "importance": 0.8,
  "freshness_required": true,
  "externally_verifiable": true,
  "contains_private_data": true,
  "redaction_required": true,
  "reason_for_research": "不同版本可能改变结果",
  "preferred_sources": ["official_release_notes", "project_docs"],
  "research_priority": "NORMAL"
}
```

建议枚举 `PERSONAL_UNVERIFIABLE`、`GROUP_PRIVATE_CONTEXT`、`TECH_FACT_CHECKABLE`、`VERSION_SENSITIVE_TECH_FACT`、`CONFLICTING_SOURCES`、`UNVERIFIED_SOLUTION`、`ALREADY_VERIFIED`、`NOT_WORTH_RESEARCH`。**个人经历是否真实**不应当被发给外部模型查询；它们只能按“某人自述/待本人更正”保存。

### 36.4 ResearchTask 生命周期

`PENDING → POLICY_CHECK → QUEUED → SEARCHING → CONSULTING_MODEL → VERIFYING → COMPLETED / NEEDS_REVIEW / DEFERRED / FAILED`。

- **SEARCHING**：优先官方技术文档、变更日志、源代码、第一手可核对来源；保留抓取时间、标题和版本。
- **CONSULTING_MODEL**：让 DeepSeek 归纳候选解释、指出证据缺口、建议验证方法；不以其答复自动“盖章”。
- **VERIFYING**：每个 claim 链接可审计的引用与支持/反驳情况，可选本地沙箱复现。
- **NEEDS_REVIEW**：信息冲突、来源不充分、可能隐私暴露或实际影响高。
- **DEFERRED**：预算耗尽、服务异常；保留候选，之后重试；绝不凭空补来源。

### 36.5 输出应是“原子主张”，不是一大篇不可验证总结

```json
{
  "research_job_id": "research_01",
  "candidate_id": "kc_100",
  "status": "NEEDS_REVIEW",
  "claims": [
    {
      "claim_id": "claim_1",
      "claim": "不同 Python 环境可能导致 Pillow 已安装却不能导入 PIL",
      "status": "SUPPORTED_WITH_LIMITATIONS",
      "environment": {"python_version": "unknown"},
      "supporting_source_ids": [],
      "contradicting_source_ids": [],
      "experiment_evidence_ids": [],
      "notes": "此处为数据格式示例；还没有查询资料，不能标已验证"
    }
  ],
  "model_consultations": ["consult_01"],
  "last_researched_at": "2026-10-16T03:30:00+08:00"
}
```

在生产环境，`source_ids=[]` 且无实验验证时不得把 `claim.status` 自动改成 `VERIFIED`。多条网页都转载同一来源，也不算独立交叉验证。

### 36.6 夜间任务的幂等、水位、批处理

- `NightlyJobKey = (tenant_id, scope_id, local_date, pipeline_version)`；唯一索引阻止重复并发执行。
- `input_watermark` 固定本轮观察上限，使白天新增/补到的消息不会被漏掉或重算混乱。
- 遇到跨午夜线程，按消息源日期切片并附同一 `thread_id`，可在次日补齐并修订事件摘要。
- 分批处理，例如每 30 个 episode 创建一个 checkpoint，先保存候选与状态，再排研究任务，最后独立提交研究结果。
- **ResearchJob 与 ConsolidationJob 解耦**：Research 队列积压时，基础天记忆仍完成；相关知识保持 `pending_verification`。
- 对同一 `claim fingerprint + applicable_version + research_policy_version` 设置缓存与过期机制，避免每晚重复花费；证据变化时可强制失效。
- 重启后补跑未完成的自然日，防止因错过凌晨调度导致永久遗漏；不能并发对同一键重复整理。

### 36.7 外部研究的数据最小化

提交给 DeepSeek 或搜索引擎之前，执行四步：

1. 判断内容是否获授权用于第三方外发；不能因“夜间后台任务”绕过群文件和私人消息的授权。
2. 将问题改写为**去身份化的通用技术疑问**；去掉 QQ 号、群名、聊天全文、访问 Token、私有 URL、源码片段等。
3. 做 DLP 规则扫描（密码、私钥、凭据、个人标识、内网地址等），扫描无法通过时 `NEEDS_REVIEW`。
4. 在 `ResearchJob` 中保存外发摘要、提供商、时间、目的与范围，不保存不必要的外部原始聊天。

无法脱敏的私有问题可只用离线资料/本地模型研究，或者请求相关用户/管理员单独批准。未经同意不得为追求更新知识而把私有内容直接送往网页版 DeepSeek。

### 36.8 夜间研究的优先级与预算

推荐可配置预算，而非硬编码：`max_research_jobs_per_night=20`（示例）、`max_external_tokens`、`max_search_requests`、`max_spend_currency`、`per_claim_timeout`、`max_retry`、`max_concurrent_research`。优先级示意：

`价值 = 0.35×可复用性 + 0.25×错误代价 + 0.20×近期期待使用频率 + 0.20×时效迫切度`。

排序前必须做权限检查与研究必要性检查；评分公式不是事实正确率。预算不足时保留候选，下个窗口或人工触发；不影响日常社交和执行任务的预算池。

## 37. Research Provider 接口、DeepSeek 适配与验证器

### 37.1 统一接口（示意 Python）

```python
from dataclasses import dataclass, field
from typing import Protocol, Literal

@dataclass(frozen=True)
class ResearchQuery:
    research_job_id: str
    sanitized_question: str
    allowed_public_sources: tuple[str, ...]
    evidence_scope: str
    max_cost: float
    timeout_seconds: int
    trace_id: str

@dataclass
class ResearchResult:
    status: Literal["completed", "deferred", "needs_review", "failed"]
    claim_candidates: list[dict]
    sources: list[dict] = field(default_factory=list)
    model_consultations: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

class ResearchProvider(Protocol):
    provider_id: str
    async def research(self, query: ResearchQuery) -> ResearchResult: ...
```

将“搜索与读取真实网页的 SearchProvider”“咨询 DeepSeek 的 ReasoningProvider”“进行证据判定的 EvidenceVerifier”**拆成可替换插件**，统一由 `ResearchOrchestrator` 组织，避免将网页端、官方 API 与搜索接口混成一个黑箱。

### 37.2 DeepSeek 官方 API：默认首选

- 通过官方 API 调用；模型 ID、推理参数、工具能力要按部署时官方文档动态配置，不把 2026 年某具体型号硬编码成永不变的常量。
- 研究任务用独立 service account / token/费用上限；密钥不得进入 QQ 事件、沙箱进程或 Git 仓库。
- 工具调用需要严格 JSON Schema 校验，限制可用工具集合与最多调用次数。
- 思考模式可帮助分析，但后台默认展示结构化理由、证据和轨迹；不能把原始模型推理内容当作需要永久保留的知识来源。
- 默认先使用独立 Search Provider 查询并获取**可追溯的网页内容**，再让 DeepSeek 进行分析；不能假设 DeepSeek 官方 API 与网页版“联网搜索”是同一能力。

### 37.3 网页版研究适配器：受控实验、默认关闭

`DeepSeekWebAdapter` 保留插件扩展点，但仅在用户/部署者确认适用条款允许自动化调用并具备稳定接口时才启用。不开发绕过登录验证、反爬、付费限制或非公开接口的方案。鉴于 UI 变动与会话失效，它不能成为夜间记忆巩固的唯一依赖；任何失败都应退化到官方 API + 独立搜索或 `DEFERRED`。

### 37.4 SourceVerifier 与 ClaimVerifier

来源元数据：`source_id`、`publisher`、`url_or_document_ref`、`fetched_at`、`published_at`、`source_type`、`content_hash`、`applicable_version`、`license`、`scope`。引用链接经过 SSRF/重定向防护，下载内容做 MIME、大小、压缩炸弹和脚本注入防护。

核验规则：

- `SUPPORTED`：有支持该**具体主张**的实际可访问来源/实验，而非仅含相关关键词。
- `SUPPORTED_WITH_LIMITATIONS`：有证据但版本/环境不全；建议答案要说明限制。
- `CONTRADICTED`：可靠证据直接反驳。
- `DISPUTED`：多个来源不一致，需确定版本或实验。
- `INSUFFICIENT`：没有可核对证据；保持候选，不晋升权威知识。

来源优先级与许可证遵守原方案第7、9、16章；事实随时间变化，需要 `freshness_ttl`、版本适用区间和再验证任务。不是所有“搜索到的最新文章”都更可信。

### 37.5 经验回写的原子操作

Research Job 只建议操作 `INSERT_CLAIM`、`ATTACH_EVIDENCE`、`MARK_DISPUTED`、`SET_STALE`、`LINK_CASE`、`NO_OP`，且必须携带候选 ID 与真实来源证据。独立 Committer 校验权限、事务和版本号后更新数据库；模型不生成 SQL 直接修改生产数据。出现错误或冲突时允许人工复核与完全回滚。

## 38. 跨群经验复用、权限控制与“能记住”不等于“能公开说”

### 38.1 统一资源身份和三个权限判定

跨群读取时依次判定：

1. **记录可见性**：检索服务是否有权在当前任务/群/用户的作用域中读取该记忆/案例？
2. **使用目的**：可以用于内部推理、帮助当事人、技术抽象分享还是可以引用源消息？不同用途分开授予。
3. **目的地披露**：目标 QQ 群/私聊是否允许把结论、截图、代码或出处发出去？群内消息具有旁观者，不能把“本人可以读”当成“本人同意群里公开”。

示例策略矩阵：

| 数据对象 | 原群成员 | 原发言人跨群查询 | 其他群成员跨群查询 | 工作 Agent |
|---|---|---|---|---|
| 原群个人聊天记录 | 原群/按记录设置 | 需本人授权与目的地审批 | 默认无 | 仅任务明确授权 |
| 原群群文件/公告 | 按群文件权限 | 默认不可搬运 | 不可 | 仅群/任务授权 |
| 私聊内容 | 对话本人 | 默认私聊可读，群里慎发 | 不可 | 需明确授权 |
| 公共知识+来源 | 可 | 可 | 可 | 可 |
| 由群聊抽象的故障经验 | 同群可 | 仅许可范围 | 只有公开/可共享且去身份化才可 | 若有相应 scope |
| 工作项目私有源码与截图 | 仅任务 ACL | 仅授权的项目成员，群内披露另审 | 不可 | 仅项目执行者 |
| 工作中验证的通用修复方法 | 来源保护下可脱敏分享 | 可 | 可，若已批准共享 | 可 |

### 38.2 “两个层次的记忆链接”

`CasePrivateRecord` 保有原始来源和权限；`ShareableKnowledgeDerivative` 是脱敏、去身份化且**独立经过传播策略审核**的公共技术知识。两者以仅内部可见的 `derived_from_ref` 关联。外部查询不会得到 `derived_from_ref`、原群 ID、提问人、私有日志。撤销授权时，系统能够据此删除或降级衍生内容。

技术上应尽量在索引级拆作用域：公共向量 collection 或带 Row-Level Security 的公共视图、各群分区/过滤、项目私有索引；查询时必须先构建许可作用域，再发 ANN 检索。不能先在全库搜索再让模型“尽量不提隐私”。

### 38.3 同一 QQ 用户跨群任务进度

当原任务在群 A 创建，发起人来到群 B 询问“我那个程序做得怎么样了”：

- 系统可依据**同一个已经认证的 QQ 用户 ID**识别其为任务发起人；允许读取其任务状态的最小必要数据。
- 但群 B 在场的其他人也能看到群消息，若源任务为 `private`、`group_A_only`，只允许回复“我找到了那个任务，详情私聊给你/到后台看”，具体文件名、源码、日志与进度在授权通道展示。
- 发起人可显式将某项目或某条结果分享至群 B，必须记录分享作用域、时间和撤销方法。
- 任何人不能通过猜任务 ID、文件 ID 或任务链接读取其他人的项目。

### 38.4 跨群知识反馈与删除

- 共享技术建议在 B 群得到“成功/失败”反馈后，新增 **B 群独立 occurrence**，不能倒改 A 群案例里的事实。
- 反馈可完善公共方案的环境适用范围，但原始聊天仍属于各群自己的作用域。
- 私人信息删除后，检查派生经验、RAG 索引、缓存、研究请求历史、评价数据和搜索快照；在技术可控范围内执行删除或隔离，并明确备份保留策略。
- 若模型服务已接收过原始文本，无法用本地“删除向量”保证第三方删除，必须在服务选型与用户告知中说明。

## 39. 复读队形静默：识别但不实现 +1

### 39.1 明确不在 AI Bot 内实现的功能

**不做**：点击 QQ 原生 +1、自动复制群友文案、自动进入队形、推荐复读语句、群友复读队列维护。用户已有独立 +1 模块，OpenSocialAgent 不替代它、不与它争夺消息发送权。

**只做**：知道当前群是否处于集体复读，抑制自己的主动/普通插话，避免打断。群级静默不影响其他群和后台工作。

### 39.2 首选事件契约：外部模块通知

```json
{
  "schema_version": "1.0",
  "event_id": "echo:A:123",
  "type": "group.echo_state.changed",
  "group_id": "A",
  "state": "ACTIVE",
  "detected_at": "2026-10-15T13:12:00+08:00",
  "last_echo_at": "2026-10-15T13:12:00+08:00",
  "ttl_seconds": 20,
  "producer": "external-plus-one-module"
}
```

接口必须幂等、按群隔离、带过期时间；外部模块退出后本地状态自动失效，避免永远静音。如没有事件输出，允许 Observer 使用**只读**消息指纹检测多用户连续相同内容，但该内建检测器不发送消息、不响应 +1、不提供复读动作。

### 39.3 冲突优先级与发送前校验

`Group ECHO_ACTIVE` > 普通主动聊天 > 普通经验建议；收到 @ 或真实任务进度询问，可按策略暂缓群内发送，必要时转入私聊/后台，但不能越权。已进入 Outbox 尚未发出的普通回复，在发送前复核最新 echo 状态，状态变化时撤销或延迟。

从 `ECHO_ACTIVE` 退出先进入 `ECHO_COOLDOWN`；冷却结束再恢复 `NORMAL`。不通过一次复读检测判断群友一定不想聊天；不要让某人恶意刷相同消息永久压制 Bot，需附群级管理员覆盖与异常速率限制。

### 39.4 标准测试

- 两个不同成员连续发送同内容 → AI 普通主动消息停止；外部 +1 模块正常运行。
- 生成中开始复读 → 尚未发送的自动发言被取消；不发过期结果。
- A 群复读 → B 群 @ 正常响应；正在运行的代码任务继续执行。
- 外部模块崩溃 → TTL 到期后有条件恢复正常；不无限静默。
- 长度很长的复读消息包含索取密码等内容 → Bot 仍绝不跟发。

## 40. 管理后台升级：AI 工作台、经验库与夜间研究审计

### 40.1 页面信息架构

| 页面/菜单 | 主要功能 | 默认权限 |
|---|---|---|
| 实时状态 | SocialPresence、连接健康、每群静默、队列负载 | 管理员 |
| AI 工作台 | 所有任务筛选、里程碑、耗时、阻塞、最近检查点 | 任务管理员 |
| 任务详情 | 步骤时间轴、运行日志、安全截图、Git diff、测试报告 | 项目 ACL |
| 任务控制 | 暂停/恢复/取消/需求变更、审批记录 | 发起者/协作者授权 |
| 经验知识库 | 报错全文搜索、问题指纹、方案版本、成功/失败反馈 | 对应资源作用域 |
| 经验审查 | 同名问题合并、错误原因更正、共享派生批准 | 审核角色 |
| 夜间巩固 | 每日执行状态、候选与变更、失败重试、成本 | 管理员/群授权角色 |
| 研究中心 | DeepSeek 调用摘要、网页来源、主张证据与冲突 | 研究审核员 |
| 消息评价 | 检索机器人历史发言、查看上下文、打负面标签、写更自然候选 | 管理员 |
| 回放实验室 | 同一历史快照对比新旧模型，含“不说话”选择 | 测试/管理员 |
| 隐私控制 | 用户记忆授权、跨群共享、群文件索引授权、删除追踪 | 本人及授权管理员 |
| 模型费用/健康 | API 错误率、Token、研究预算、沙箱失败 | 管理员 |

### 40.2 真实进度 UI 字段

任务页须清楚展示 `运行中/已暂停/待输入/中断恢复中/验证中/完成`，并区别：

- **计划**：下一步打算做什么，可能改变。
- **实际完成**：已提交代码、通过测试、存在合法 Artifact 证据。
- **当前活动**：最近一次 Worker 日志与心跳时间。
- **估算**：若没有可靠估算算法则展示“暂无法估算”，绝不假称进度百分比。

任务操作按钮使用 `expected_version` 防重复；敏感工具参数和凭据默认脱敏，下载代码与查看日志遵循工作区 ACL。任务失败也应保留排错轨迹供 Experience Extractor 使用。

### 40.3 经验详情页面

建议展示：Issue 名称、标准错误码、组件与环境、各次 Occurrence、尝试序列、失败原因、已确认的解决方案、引用证据、Research Reports、适用版本、分享范围、审查记录以及后续推荐反馈。对 `unverified`/`disputed` 显示明确文本，不使用误导性绿色“已解决”图标。

### 40.4 夜间研究审计页面

`consolidation_date`、`candidate_count`、`research_enqueued`、`research_completed`、`research_deferred`、`claims_verified`、`claims_disputed`、`budget_used`、`source_count`、`privacy_blocks` 等统计。可人工重试一个研究任务，且不重复巩固整天的事件。管理员批准或拒绝某项公开知识晋升时必须留下操作人、版本、原因。

### 40.5 机器人消息评价的升级标签

保持“太人机、机械附和、强行接梗、重复原话、没理解梗、不该发言、打断复读、任务进度捏造、经验建议无证据、跨群越权披露”等标签。评价分为：**不该回复** / **说法不好** / **知识或事实有误** / **权限或工作状态错误**，不能把“聊天太人机”的训练信号直接变成“减少一切回复”的规则。

## 41. 数据库增量迁移、索引与接口契约

以下 DDL 是 PostgreSQL **V2 增量结构参考**；实际 Alembic migration 需要结合前文 V1 的表名、已有约束和字段完成映射。示例使用 UUID 作为内部主键，并保留平台 QQ ID 为 text，防止隐式整型处理及跨平台冲突。

### 41.1 任务表及控制命令（示例 DDL）

```sql
CREATE TABLE IF NOT EXISTS work_tasks (
  id UUID PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  requester_identity_id TEXT NOT NULL,
  source_group_id TEXT,
  project_id UUID,
  workspace_id UUID NOT NULL,
  visibility_scope TEXT NOT NULL DEFAULT 'owner_only',
  status TEXT NOT NULL,
  priority INTEGER NOT NULL DEFAULT 50,
  version BIGINT NOT NULL DEFAULT 1,
  lease_generation BIGINT NOT NULL DEFAULT 0,
  lease_worker_id TEXT,
  lease_expires_at TIMESTAMPTZ,
  current_step TEXT,
  progress_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  plan_version BIGINT NOT NULL DEFAULT 1,
  checkpoint_seq BIGINT NOT NULL DEFAULT 0,
  idempotency_key TEXT UNIQUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_progress_at TIMESTAMPTZ,
  finished_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS ix_work_tasks_status_priority
  ON work_tasks(status, priority DESC, created_at);
CREATE INDEX IF NOT EXISTS ix_work_tasks_requester
  ON work_tasks(requester_identity_id, created_at DESC);

CREATE TABLE IF NOT EXISTS task_commands (
  id UUID PRIMARY KEY,
  task_id UUID NOT NULL REFERENCES work_tasks(id),
  issuer_identity_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  payload JSONB NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  expected_task_version BIGINT NOT NULL,
  idempotency_key TEXT NOT NULL UNIQUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  applied_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS ix_task_commands_pending
  ON task_commands(task_id, created_at) WHERE status='pending';

CREATE TABLE IF NOT EXISTS task_checkpoints (
  id UUID PRIMARY KEY,
  task_id UUID NOT NULL REFERENCES work_tasks(id),
  seq BIGINT NOT NULL,
  lease_generation BIGINT NOT NULL,
  git_commit_sha TEXT,
  plan_version BIGINT NOT NULL,
  manifest_hash TEXT NOT NULL,
  test_report_ref TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(task_id, seq)
);

CREATE TABLE IF NOT EXISTS task_evidence (
  id UUID PRIMARY KEY,
  task_id UUID NOT NULL REFERENCES work_tasks(id),
  step_key TEXT,
  kind TEXT NOT NULL,
  status TEXT NOT NULL,
  summary TEXT NOT NULL,
  protected_artifact_ref TEXT,
  sanitized_digest TEXT,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_task_evidence_task
  ON task_evidence(task_id, created_at DESC);
```

这是 schema 草案而不是现成完整 migration。生产实现还需要外键关联 `identities/projects/workspaces`、状态枚举 CHECK、完整 Tenant/RLS、数据保留策略、只写一次审计、事务封装与迁移测试。

### 41.2 经验问题、案例、尝试及反馈（DDL）

```sql
CREATE TABLE IF NOT EXISTS issues (
  id UUID PRIMARY KEY,
  canonical_key TEXT NOT NULL,
  title TEXT NOT NULL,
  component TEXT,
  normalized_error_signature TEXT,
  problem_fingerprint JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_issues_key ON issues(canonical_key);
CREATE INDEX IF NOT EXISTS ix_issues_signature ON issues(normalized_error_signature);

CREATE TABLE IF NOT EXISTS issue_occurrences (
  id UUID PRIMARY KEY,
  issue_id UUID NOT NULL REFERENCES issues(id),
  origin_kind TEXT NOT NULL,                   -- group | work | document
  origin_group_id TEXT,
  origin_task_id UUID,
  problem_owner_identity_id TEXT,
  privacy_scope TEXT NOT NULL,
  symptoms TEXT NOT NULL,
  environment_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  resolution_status TEXT NOT NULL DEFAULT 'unresolved',
  source_refs JSONB NOT NULL DEFAULT '[]'::jsonb,
  started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  resolved_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS ix_occurrences_issue ON issue_occurrences(issue_id, started_at DESC);

CREATE TABLE IF NOT EXISTS issue_solutions (
  id UUID PRIMARY KEY,
  issue_id UUID NOT NULL REFERENCES issues(id),
  solution_text TEXT NOT NULL,
  applicability_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  evidence_level TEXT NOT NULL DEFAULT 'unverified',
  source_policy_id TEXT,
  status TEXT NOT NULL DEFAULT 'proposed',
  valid_from TIMESTAMPTZ,
  valid_until TIMESTAMPTZ,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS resolution_attempts (
  id UUID PRIMARY KEY,
  occurrence_id UUID NOT NULL REFERENCES issue_occurrences(id),
  solution_id UUID REFERENCES issue_solutions(id),
  action_summary TEXT NOT NULL,
  result TEXT NOT NULL,                         -- succeeded|failed|unknown
  evidence_refs JSONB NOT NULL DEFAULT '[]'::jsonb,
  executed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS solution_feedback (
  id UUID PRIMARY KEY,
  solution_id UUID NOT NULL REFERENCES issue_solutions(id),
  occurrence_id UUID REFERENCES issue_occurrences(id),
  result TEXT NOT NULL,
  evidence_ref TEXT,
  source_scope TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

### 41.3 夜间研究与主张证据（DDL）

```sql
CREATE TABLE IF NOT EXISTS research_jobs (
  id UUID PRIMARY KEY,
  trigger_type TEXT NOT NULL,                    -- nightly|user_query|work_issue
  origin_candidate_id TEXT NOT NULL,
  sanitized_question TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  priority INT NOT NULL DEFAULT 50,
  budget_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  privacy_review TEXT NOT NULL DEFAULT 'pending',
  idempotency_key TEXT NOT NULL UNIQUE,
  attempt_count INT NOT NULL DEFAULT 0,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  completed_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS ix_research_pending
  ON research_jobs(status, priority DESC, created_at);

CREATE TABLE IF NOT EXISTS research_sources (
  id UUID PRIMARY KEY,
  research_job_id UUID NOT NULL REFERENCES research_jobs(id),
  uri TEXT,
  publisher TEXT,
  title TEXT,
  content_hash TEXT,
  fetched_at TIMESTAMPTZ NOT NULL,
  published_at TIMESTAMPTZ,
  source_type TEXT NOT NULL,
  applicable_version TEXT,
  license TEXT,
  visibility_scope TEXT NOT NULL DEFAULT 'public'
);

CREATE TABLE IF NOT EXISTS knowledge_claims (
  id UUID PRIMARY KEY,
  text TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'unverified',
  applicability_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  last_verified_at TIMESTAMPTZ,
  freshness_due_at TIMESTAMPTZ,
  scope TEXT NOT NULL DEFAULT 'private',
  version BIGINT NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS claim_evidence (
  claim_id UUID NOT NULL REFERENCES knowledge_claims(id),
  source_id UUID NOT NULL REFERENCES research_sources(id),
  relation TEXT NOT NULL,                        -- supports|contradicts|mentions
  excerpt_hash TEXT,
  PRIMARY KEY (claim_id, source_id, relation)
);
```

**生产关键补充**：`source_id` 并不能单独证明来源与 claim 支持关系；必须有经过抽取/审查的证据范围与 quote hash、可追溯抓取版本；人工纠正日志应单独存储。上面的 `knowledge_claims.scope` 在实际设计应改为策略 ID/归属主体，并与搜索层的 row ACL 一致。

### 41.4 向量、关键词与多租户数据边界

复用 V1 的 `embeddings` / `knowledge_chunks` 存储抽象，经验实体作为新的索引目标 `document_kind=issue|solution|experience_case`；BGE-M3 1024 维是一个可配置候选，不强制固定维度。若更换模型维度，应新建 embedding_version/index，后台重建后切流，不能把不同空间的向量混在一个索引里。

索引字段建议：`tenant_id`、`scope`、`group_id`、`project_id`、`subject_user_id`、`emb_model_id`、`embedding_version`、`trust_status`、`last_verified_at`、`applicable_version`。记忆删除和授权撤销需要同步处理索引/缓存（通过 Outbox + tombstone 机制防止短暂泄露）。

### 41.5 API 端点（V2 新增）

| 方法 | Endpoint | 功能 | 必要权限 |
|---|---|---|---|
| `POST` | `/api/v1/tasks` | 创建授权任务 | `task:create` |
| `GET` | `/api/v1/tasks` | 按权限列出任务 | `task:list` |
| `GET` | `/api/v1/tasks/{id}` | 任务状态与里程碑 | `task:read` |
| `GET` | `/api/v1/tasks/{id}/events` | SSE 进度推送 | `task:read` |
| `POST` | `/api/v1/tasks/{id}/commands` | 修改/暂停/继续/取消 | `task:control` |
| `GET` | `/api/v1/tasks/{id}/artifacts` | 查询已授权成果 | `artifact:read` |
| `GET` | `/api/v1/tasks/{id}/evidence` | 脱敏执行证据 | `task:audit` |
| `GET` | `/api/v1/experiences` | 按作用域搜索踩坑案例 | `experience:read` |
| `GET` | `/api/v1/experiences/{id}` | 查看问题/方案/证据 | `experience:read` |
| `POST` | `/api/v1/experiences/{id}/reviews` | 纠错/验证/共享审核 | `experience:review` |
| `GET` | `/api/v1/consolidations` | 夜间任务列表 | `memory:audit` |
| `POST` | `/api/v1/consolidations/{id}/retry` | 幂等重试 | `memory:admin` |
| `GET` | `/api/v1/research/jobs` | 研究任务与结果 | `research:audit` |
| `POST` | `/api/v1/research/jobs/{id}/retry` | 重试单个研究 | `research:admin` |
| `GET` | `/api/v1/agent/presence` | 当前工作状态 | 授权访问 |
| `POST` | `/api/v1/admin/kill-switch` | 停止高风险行为 | `system:admin` |

API 使用统一响应结构：`{request_id, data, error?}`；采用 Bearer 会话/授权服务和 CSRF 保护（若 Cookie），后端按资源再次鉴权。SSE 最终一致性、断线重放靠单调事件序列号 `Last-Event-ID`，客户端不得把流断开误判为任务失败。

### 41.6 核心事件协议（版本化）

| Topic | 发布者 | 订阅者 | 语义 |
|---|---|---|---|
| `qq.message.received.v1` | Gateway | Social / authorized memory | 一条真实平台消息 |
| `group.echo_state.changed.v1` | 外部 +1 状态 / Observer | SendPolicy | 当前群是否有复读队形 |
| `task.created.v1` | Coordinator | Work Scheduler / Presence | 新建主要任务 |
| `task.progress.updated.v1` | Work Runtime | Task Registry / UI | 真实检查点与里程碑 |
| `task.command.requested.v1` | Social/UI | Coordinator | 修改需求、暂停、取消等 |
| `task.completed.v1` | Work Runtime | Delivery / Experience Extractor | 结果验收完成 |
| `work.issue.observed.v1` | Evidence Recorder | Experience Extractor | 实际错误发生 |
| `work.resolution.verified.v1` | Evidence Recorder | Experience Store | 具体环境中通过测试的解决方法 |
| `social.issue.discussed.v1` | Conversation Observer | Experience Extractor | 群友问题讨论候选 |
| `memory.consolidation.requested.v1` | Scheduler | Memory Consolidator | 固定日期与水位 |
| `research.requested.v1` | Memory/Work/Social | Research Orchestrator | 可核实的研究任务 |
| `research.completed.v1` | Research Orchestrator | Knowledge Committer | 结构化主张和来源 |
| `knowledge.claim.changed.v1` | Knowledge Committer | Indexer / Cache | 已审核事实变更 |
| `memory.permission.revoked.v1` | Consent API | All indexes / workers | 隐私撤销和清理 |

每条事件至少包含 `event_id`、`schema_version`、`tenant_id`、`occurred_at`、`producer`、`trace_id`、`causation_id`、`payload`。事件内容须尽量传**受保护的引用 ID**而不是全量群聊/日志；跨服务读取时再按身份鉴权。

### 41.7 插件 SDK 增量

新增可选接口：

```python
class EchoStateProvider(Protocol):
    async def get_group_echo_state(self, group_id: str) -> str: ...

class TaskProgressProvider(Protocol):
    async def get_snapshot(self, task_id: str, requester: str) -> dict: ...

class ExperienceExtractor(Protocol):
    async def extract_from_evidence(self, evidence_refs: list[str]) -> list[dict]: ...

class ClaimVerifier(Protocol):
    async def verify(self, claim_id: str, evidence_refs: list[str]) -> dict: ...
```

`EchoStateProvider` 只提供状态，不提供“复读发送”权限。研究插件不得隐式获得 `qq.send`、`workspace.shell`、`memory.all_groups.read` 等能力。每个插件通过 manifest 声明 scope、网络域名、外发类型、版本依赖及测试夹具；第三方插件默认低权限，管理员需显式启用扩展权限。

## 42. 项目工程目录、服务组合与配置升级

### 42.1 推荐 Monorepo 结构（包含此前全部模块）

```text
opensocialagent/
├── apps/
│   ├── social_service/                 # QQ 快路径与社交发言
│   │   ├── main.py
│   │   ├── event_handlers/
│   │   ├── decision/
│   │   ├── presence/
│   │   ├── behavior_observer/          # +1 只读静默，不实现复读
│   │   └── send_policy/
│   ├── work_service/                   # 长任务循环与 Worker 管理
│   │   ├── main.py
│   │   ├── orchestrator/
│   │   ├── checkpoints/
│   │   ├── progress/
│   │   ├── recovery/
│   │   └── evidence/
│   ├── research_service/               # DeepSeek/搜索/来源核验
│   │   ├── orchestrator/
│   │   ├── uncertainty/
│   │   ├── providers/
│   │   └── verification/
│   ├── memory_worker/                  # 按水位巩固、经验抽取、索引
│   │   ├── daily/
│   │   ├── consolidation/
│   │   ├── experience/
│   │   └── indexer/
│   ├── api_server/                     # 后台 API / ACL / SSE
│   └── dashboard/                      # Vue / TS 前端
├── packages/
│   ├── core_protocols/                 # Pydantic v2 DTO、事件与 schema
│   ├── plugin_sdk/                     # 生命周期、Manifest、工具/Provider API
│   ├── platform_adapters/
│   │   ├── napcat/
│   │   ├── qzone_experimental/
│   │   └── plus_one_state_optional/    # 只读消费外部状态
│   ├── identity_acl/
│   ├── conversation_threads/
│   ├── naturalness_feedback/
│   ├── multimodal/
│   │   ├── image/
│   │   ├── video/
│   │   ├── audio/
│   │   ├── ocr/
│   │   └── stickers/
│   ├── group_resources/                # 群文件、群公告
│   ├── llm_providers/
│   ├── model_router/
│   ├── memory_store/
│   ├── experience_store/
│   ├── knowledge_store/
│   ├── vector_retrieval/
│   ├── task_registry/
│   ├── task_command_bus/
│   ├── artifact_store/
│   └── observability/
├── sandbox/
│   ├── python_runner/
│   ├── browser_worker/
│   ├── vm_provider/                    # 可选微虚拟机执行器
│   └── image_definitions/
├── migrations/                          # Alembic / SQL
├── tests/
│   ├── unit/
│   ├── contract/
│   ├── integration/
│   ├── end_to_end/
│   ├── replay/
│   ├── fixtures_sanitized/
│   ├── security/
│   └── chaos/
├── deploy/
│   ├── compose/
│   ├── k8s_optional/
│   └── env.example
├── docs/
│   ├── architecture/
│   ├── adr/
│   ├── plugin_sdk/
│   ├── api/
│   ├── model_providers/
│   ├── deployment/
│   ├── operations/
│   ├── privacy/
│   └── threat_model/
├── examples/
│   ├── minimal_chatbot/
│   ├── mock_napcat/
│   ├── mock_research_provider/
│   └── sample_plugin/
├── .github/
│   ├── workflows/
│   ├── ISSUE_TEMPLATE/
│   └── PULL_REQUEST_TEMPLATE.md
├── pyproject.toml
├── package.json
├── README.md
├── README.en.md
├── CONTRIBUTING.md
├── SECURITY.md
├── CODE_OF_CONDUCT.md
├── LICENSE                        # 项目自选，进行第三方依赖兼容性审查
└── docs/详细实现方案.md             # 本技术文档可以放在这里
```

部署阶段建议控制**物理服务数量**：`social_service`、`work_service`、`memory_research_worker`、`api_server`、`postgres`、`object_store`、`napcat` 即可起步，代码包仍按以上逻辑模块隔离。`research_service` 可以暂时作为后台 Worker 的一个隔离进程；第三方运行时代码和浏览器必须与管理平面进一步隔离。Redis/NATS 是容量增长后可替换的队列后端，而不是 P0 必选项。

### 42.2 YAML 参数示例（默认偏安全）

```yaml
project:
  environment: production
  timezone: Asia/Shanghai

platforms:
  napcat:
    enabled: true
    connection_mode: reverse_websocket
    require_token: true
  qzone:
    enabled: false
  external_plus_one:
    enabled: false                 # 有事件接入时再开启
    read_only: true

social:
  enable_unprompted_chat: true
  suppress_unprompted_while_working: true
  status_query_during_work: true
  group_echo_silence: true
  never_echo_message: true          # Bot 永不执行 +1
  send_stale_check: true
  reply_on_mention: true
  cross_group_context_merge: false  # 仅经授权检索记忆；不混原始上下文
  naturalness_filter: true

work:
  enabled: false                   # 审批和沙箱验收通过后开启
  main_task_sets_presence_working: true
  max_parallel_tasks: 2
  checkpoint_after_each_milestone: true
  command_apply_policy: safe_step_boundary
  worker_lease_seconds: 120
  lease_heartbeat_seconds: 30
  always_record_test_evidence: true
  allow_untrusted_network_access: false

memory:
  enabled: true
  daily_episodes: true
  long_term: true
  experience:
    collect_from_work: true
    collect_from_authorized_groups: true
    default_group_share_scope: group_only
    auto_promote_untested_solutions: false
  nightly:
    enabled: true
    time: "03:30"
    late_job_recovery: true
    research_on_uncertainty: true
    max_research_jobs: 20

research:
  enabled: true
  deepseek:
    mode: official_api
    api_key_env: DEEPSEEK_API_KEY
    model: "<configure-supported-model-id>"
  web_search:
    provider: "<configure-search-provider>"
  website_adapter:
    enabled: false                 # 仅合规审查后实验
  source_verification_required: true
  strip_private_identifiers: true
  no_external_private_group_logs: true
  budget:
    nightly_model_tokens: 100000   # 示例，需根据价格校准
    nightly_search_requests: 100   # 示例
    max_retries_per_job: 2

knowledge:
  embeddings:
    provider: configurable
    model: BAAI/bge-m3
    dimension: 1024
  hybrid_retrieval: true
  reranker_enabled: false

security:
  deny_host_shell: true
  deny_docker_socket_mount: true
  deny_workspace_secrets_to_llm: true
  sandbox_default_network: false
  group_files_auto_index: false
  group_announcement_write_auto: false
  cross_group_personal_sharing_default: false
  default_raw_message_retention_days: 30  # 示例：部署者配置并告知群成员
```

生产部署中密钥由 secret manager 或环境变量注入；示例 `<...>` 是占位符，不能原样运行。配置应有 JSON Schema/Pydantic 验证、热更新范围约束、覆盖顺序、配置版本和敏感字段掩码。

### 42.3 可观测性与 SLO（建议初始验收阈值）

| 指标 | 建议目标/含义 | 采集方 |
|---|---|---|
| `mention_ack_latency_p95` | 简单 @ 不需工具时，目标 P95 小于 6 秒（按实际模型条件校准） | Social |
| `task_status_latency_p95` | 任务状态快路径目标 P95 小于 2 秒（网络与 QQ 延迟另计） | Task API |
| `task_resume_success_rate` | 人为 kill Worker 后从检查点恢复且状态真实 | Work |
| `duplicate_delivery_rate` | 目标 0 个可见重复交付（出现需复盘） | Outbox |
| `false_verified_knowledge` | 无证据被标验证的条目目标 0 | Knowledge QA |
| `private_cross_group_leak_count` | 目标 0，作为发布阻断事件 | ACL / Audit |
| `echo_interruption_rate` | 已检测队形期间普通主动发言目标 0 | SendPolicy |
| `unprompted_while_working` | WORKING 期间普通主动水群目标 0 | Presence + SendPolicy |
| `external_research_spend` | 日预算内；溢出自动 Deferred | Research |
| `memory_erasure_backlog` | 删除请求待清理项可追踪、可告警 | Privacy Worker |

性能指标应拆网络、模型排队、数据库、沙箱各部分，不能仅以用户感受倒推 Worker 状态。Metrics 可用 OpenTelemetry + Prometheus，日志 JSON 结构化并对 QQ 号/内容做必要脱敏。

## 43. 测试策略、模拟场景与持续集成验收

### 43.1 单元测试（不依赖 QQ 账号与真实模型）

- TaskStatusStateMachine：状态非法跳转拒绝、版本冲突、取消请求幂等。
- PresencePolicy：主要任务 RUNNING 时关闭主动消息；后台整理不影响 Presence；多个主要任务完成最后一个才恢复空闲。
- EchoSendPolicy：只读复读状态下抑制普通发言；绝不会触发 `qq.echo/+1`。
- MemoryACL：全局人物记忆需要本人授权；私聊和群文件默认不跨群。
- ExperienceFingerprint：精确报错码与环境特征保留，禁用仅靠相似度晋升方案。
- EvidenceState：测试失败不标成功、用户没明确确认不标成功、DeepSeek 单独回答不算已验证。
- ResearchPrivacy：私有路径/秘钥/群名外发被 DLP 拦截，无法脱敏时挂人工审核。
- NightlyIdempotence：同一个群/日期/版本重复执行不会写两份长期记忆。

### 43.2 必须自动化的端到端场景

| ID | 输入 | 预期 |
|---|---|---|
| E2E-01 | 群 A 创建网页版扫雷任务 | 返回 `task_id`，Work 异步运行，QQ 不等待整个编程任务 |
| E2E-02 | 扫雷任务 RUNNING 时群 B @ Bot | Social 正常回复，Task 的 checkpoint 无回退 |
| E2E-03 | RUNNING 中询问“写到哪了” | 返回 Registry 真实里程碑、更新时间，Worker 不中断 |
| E2E-04 | 追加“加计时器” | 显示“已收到”，下一安全点生成新 plan_version，测试覆盖增量功能 |
| E2E-05 | 运行时 kill -9 Work Worker | 任务转 INTERRUPTED，租约过期后恢复，不双写文件/不重复发送交付 |
| E2E-06 | 工作中报 `ModuleNotFoundError` | 生成带环境和日志证据的 Issue/Occurrence |
| E2E-07 | Work 测试修复通过 | 追加 Attempt/Evidence，仅对该环境标本地验证 |
| E2E-08 | 群友自己排查并确认解决 | 抽取用户确认案例，来源群保密；夜间可候选共享技术抽象 |
| E2E-09 | B 群出现相似错误且 Bot 空闲 | 检索允许共享的方案，发言是否发送由社交决策决定 |
| E2E-10 | 任务 RUNNING，B 群出现类似求助但没 @ | 不主动插话，可记录候选经验 |
| E2E-11 | 群 A 两人开始复读 | Bot 不加入/不打断，B 群 @ 不受影响 |
| E2E-12 | 夜间整理遇到版本不明的技术主张 | ResearchJob 入队；有来源才可验证，不会卡住普通记忆提交 |
| E2E-13 | DeepSeek API 超时/无来源 | Research 进入 DEFERRED/NEEDS_REVIEW，不凭空写已验证 |
| E2E-14 | 研究候选含群友 QQ 号与私有 URL | 外发请求被拒绝或脱敏，保留审计记录 |
| E2E-15 | 本人在 B 群问 A 群私有项目状态 | 可识别同一身份，但不会把私有工作细节公开到 B 群 |
| E2E-16 | 撤销个人记忆授权 | 立即排除检索，后台触发派生索引/缓存/知识的清理 |
| E2E-17 | 群文件文档含“忽略系统规则” | 当做不可信文件文字，不能创建任务或修改权限 |
| E2E-18 | 两个 Worker 同时争抢任务 | 只允许当前 lease_generation 提交 checkpoint |
| E2E-19 | 管理后台修改 Bot 某条回复评价 | 可回放两种模型并独立给“该不该说”和“怎么说”标注 |
| E2E-20 | QZone 适配器不可用 | 不影响 NapCat 群聊、夜间巩固与 Work Runtime |

### 43.3 带代码的集成验收示例（Python 伪代码）

```python
import pytest

@pytest.mark.asyncio
async def test_working_suppresses_unprompted_but_not_mention(app):
    task = await app.tasks.create_mock_main_task(status="RUNNING")
    assert await app.presence.get() == "WORKING"
    assert not await app.send_policy.allowed(group="A", intent="JOIN_THREAD")
    assert await app.send_policy.allowed(group="B", intent="MENTION_REPLY")
    await app.tasks.finish_mock(task.id)
    assert await app.presence.get() == "FREE"

@pytest.mark.asyncio
async def test_unverified_deepseek_answer_does_not_verify_claim(app):
    candidate = await app.knowledge.create_candidate("some technical claim")
    await app.research.inject_model_only_answer(candidate.id, "looks correct")
    await app.research.finish(candidate.id)
    assert (await app.knowledge.get(candidate.id)).status != "verified"
```

示例仅说明测试意图，实际工程应按稳定 fixture、数据模型与依赖注入实现；不应声称示例代码未经补全即可运行。

### 43.4 故障注入与安全测试

- 任务运行中断网、模型 429、服务端 500、对象存储满、PostgreSQL 短暂断连、NapCat 重连事件重放。
- 外部研究网页返回伪造系统指令、恶意 HTML、包含内网资源地址的跳转、引用不存在的论文。
- 用户在 A 群引用 B 群隐私信息、尝试猜任务 ID，测试授权服务拒绝。
- 夜间研究 budget=0，确认流程能够保存已确认基础事实而跳过 Research。
- 人为让两个 Worker 获取过期租约，确认旧租约 checkpoint 被拒绝。
- 单条群消息重复上报，确认不会双创建任务/双发图/双保存一个问题实例。
- 复读队形识别状态乱序、事件重复、TTL 过期，确认发送策略不误锁死。
- 表情包文件与群公告中的嵌入指令不得变成工具命令。
- 风格回归集包含疯狂星期四隐晦变体与 QQ 集体行为，评分要测“识别语境/是否该回复”，而不是只看回复是否短。

### 43.5 CI/CD 阻断门槛

- `ruff/black/mypy` 或仓库选定等价规范、`pytest`、迁移测试、插件契约测试、前端 TypeScript 构建。
- 所有公开 Provider SDK 变更有 schema 兼容测试，破坏兼容时必须升级 major 并提供迁移说明。
- 发布前不得存在跨群越权检索、沙箱到宿主机危险挂载、工具注入关键回归失败。
- 许可证扫描、敏感密钥扫描、依赖漏洞扫描；不在开源仓库中放入真实 QQ 聊天/群文件/账号 Cookie。
- 对模型行为采用离线固定回放集与定期人工复核，**不要**把随机生成结果测试写成必须逐字相同。

## 44. 实施优先级、里程碑与从 V1 迁移

### 44.1 必须遵守的依赖顺序

**M0：工程协议与权限基础** — Monorepo、事件 Schema、数据迁移、身份 ACL、NapCat Mock、状态日志和 Outbox。此阶段只证明可独立连接与回放，不引入外部执行权限。

**M1：多群社交 MVP** — 自然回复、@、多话题、消息评价、复读只读静默、被授权群文件/公告只读。完成前禁止 Bot 自主向大量群发言。

**M2：天记忆、长期记忆、跨群权限** — 同一 QQ 用户的跨群个人记忆按许可检索；知识/群文件与私聊分别授权；夜间离线巩固幂等可恢复。

**M3：双运行时和任务状态** — 工作进程可以用 Fake Worker 跑步骤；能创建任务、问进度、暂停/恢复/取消、处理变更；Work 崩溃后从 checkpoint 恢复。*此阶段工作沙箱先只用安全示例程序*。

**M4：真实虚拟电脑与编程体验** — 受控 Shell/代码工具、Git checkpoint、测试报告、截图和 Artifact ACL；安全评估通过后按群/人灰度。长任务不影响 Social。

**M5：经验记忆双向学习** — Work Evidence → Issue、群聊 Issue → Candidate、问题指纹、共享派生授权、B 群经验复用、解决反馈。

**M6：夜间研究与 DeepSeek** — ResearchJob API、独立联网来源、API 咨询、Claim Verification、脱敏外发与预算、失败降级。

**M7：多模态和自然度学习** — 图片、视频、表情包、语音、场景回放与模型 A/B，按分离 Provider 接口接入。

**M8：生态和试验能力** — QQ 空间实验适配器、插件 SDK 稳定发布、第三方文档、示例插件、社区 CI。

部署版本可以把 M2 与 M3 的一部分交叉开发，但不允许“先开放任意群友代码执行，再补安全授权”。

### 44.2 从旧方案迁移的变更明细

| 原设计 | V2 变更 | 是否影响兼容 |
|---|---|---|
| 单 Agent 同步处理工具与回复 | Social / Work 分进程 + Coordinator | 新增接口，不要求废弃现有 Gateway |
| 任务仅简单 pending/running/done | 完整状态机、租约、里程碑、检查点 | 需要任务表迁移 |
| 编程结果日志短期留存 | 证据层和经验提取器、可复用方案 | 新增数据表/清理策略 |
| 夜间只提取长期记忆 | 研究候选、ResearchJob、DeepSeek、核验状态 | 新增调度与费用预算 |
| 群聊技术建议只是知识候选 | Issue/Attempt/Feedback + 跨群共享派生 | 新增经验权限与审核 |
| 群里复读行为由 Bot 自己完整处理 | 仅识别并静默；+1 模块外置 | 删除任何发送复读的代码入口 |
| 任务状态完全由模型回复描述 | Registry 快照 + 进度 API + 可审计证据 | 必须调整回复链路 |

迁移遵循扩展-回填-切换-清理：先新增表和状态，后台回填历史任务的 `unknown` 值（不伪造验证证据），双写/校验后切换读路径，最后按版本窗口删除旧字段。每一步都要可回滚，旧历史聊天不能因为缺结构字段而被模型臆测成可信经验。

### 44.3 开源首次发布最低质量线

- README（中文优先、补英文）、架构图、快速启动和 Mock NapCat 可用；无需真实 QQ 登录也能演示社交/任务/研究回放。
- `.env.example` 不含密钥；Docker Compose 默认**关闭工作沙箱与 QQ 空间高风险功能**。
- 社交、工作、记忆、研究各有至少一个替换 Provider 的参考实现和契约测试。
- 贡献者能运行单元测试、关键集成测试、迁移测试；Mock 数据匿名且许可证可核查。
- 向群管理员与成员解释记忆/文件保存、跨群共享、研究外发与数据删除入口。
- `SECURITY.md` 提供私下漏洞披露通道；高风险 bug 不要求贡献者在 Issue 区公开利用细节。
- 项目自身代码许可证独立评估；不因 NapCat/QQ 空间插件存在而错误承诺所有集成均具备相同开源授权。

## 45. 更新后的最终验收清单与参考资料

### 45.1 强制验收清单

- [ ] **同一人格、独立运行**：社交 Service 与工作 Service 独立重启；某一边故障不会使另一边数据库损坏。
- [ ] **工作时不水群**：主要编程任务 RUNNING 时，全局普通主动社交暂停，但授权 @ 和真实进度查询继续可用。
- [ ] **进度真实可追**：当前步骤、最近日志时间、已完成测试与真实产物可核查；未知状态明确显示未知。
- [ ] **长任务可恢复**：租约防重、Git checkpoint、任务命令幂等、重启不重复交付。
- [ ] **QQ +1 已外置**：Bot 不实现复读发送，仅识别队形并对当前群静默，其他群和工作不受影响。
- [ ] **工作经验可保存**：发现错误立即记 occurrence；成功/失败尝试附真实验证证据与环境。
- [ ] **群聊经验可学习**：正确关联提问人、建议、尝试与反馈，缺少成功确认时保持未解决状态。
- [ ] **经验能跨群帮忙但不会泄露源群**：可共享结论去身份化并经授权，目标群不暴露私聊、源码和群文件。
- [ ] **夜间研究按需启动**：不确定技术知识生成 ResearchJob；DeepSeek 只是研究助手，结论需来源或实验核验。
- [ ] **Research 有预算与隐私控制**：无授权原始群聊/代码不会发送第三方；超额或失败后 Deferred，不影响日常任务。
- [ ] **知识可纠错**：版本冲突、过期、删除及失去证据后可降级，向量索引与缓存一致更新。
- [ ] **后台能审计、评价与回放**：消息、任务、经验、研究、群文件/公告都具备权限过滤的可视化入口。
- [ ] **原计划功能不丢失**：多群/私聊、图像/视频/语音、表情包、QQ 空间试验适配、群文件及公告、开源插件 SDK 均有保留的技术接口。

### 45.2 本升级版和 V1 原章节的关系

**完整功能设计仍在第0～29章；第30～45章给出后续讨论新增的明确设计，并覆盖与之冲突的旧约定。** 落地过程中应将重要 V2 ADR 合并进对应模块文档（`docs/architecture`），避免实现者只阅读 V1 旧示例。上线前逐项运行第43章回归用例。

### 45.3 技术参考入口（需要持续复核）

- NapCat 官方文档：<https://napcat.apifox.cn/>；所有群文件/群公告方法以部署版本与契约测试为准。
- OneBot 11 标准（社区规范）：<https://github.com/botuniverse/onebot-11>。
- DeepSeek 官方 API 文档：<https://api-docs.deepseek.com/>、<https://api-docs.deepseek.com/zh-cn/api/create-chat-completion/>。模型 ID、支持的 tools/思考参数与价格应从最新官方资料重新确认。
- pgvector：<https://github.com/pgvector/pgvector>，官方说明支持结合 PostgreSQL 全文检索以及 RRF/cross-encoder 的混合检索。
- PostgreSQL 官方文档：<https://www.postgresql.org/docs/current/>；`SKIP LOCKED`、事务隔离与 RLS 均需按版本验证。
- Playwright Python：<https://playwright.dev/python/>；浏览器操作和截图需配合隔离容器。
- OWASP LLM/Agent 风险参考：<https://genai.owasp.org/>。
- Python asyncio 官方文档：<https://docs.python.org/3/library/asyncio.html>。

**文档说明**：所有具体数值（间隔、预算、并发、保留天数、SLO）均是可调整的起点，不代表已实测。所有 JSON/SQL/Python 示例为开发规范草案，需要经过 schema 验证、迁移集成测试和权限测试，不能直接复制到生产环境后认为已安全可运行。

---

> **最终定位**：OpenSocialAgent 是一个可以自然地生活在多个 QQ 群里的 AI 社交身份。它能看图、看视频、读授权群文件和公告、记住群友、独立使用隔离虚拟电脑做事，能够在工作时保持社交克制又随时回应被 @ 和进度询问；既能从自己踩过的坑里总结有效经验，也能尊重他人的解决过程并在授权后复用技术经验。每天夜间它会借助研究系统核查值得核查的技术问题，但从不以外部模型的一句话取代真实证据或隐私权限。
