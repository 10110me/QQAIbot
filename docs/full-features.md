# 完整能力与 API

所有 /api/v1 路径要求 Authorization: Bearer TOKEN。owner 具有全局权限；群域角色仅获得限定的消息/群管理能力。

| 能力 | API |
|---|---|
| 实时 DeepSeek 桌面 | POST /browser/session；GET /browser/status；POST /browser/open-login |
| 研究 | POST /research；GET /research/history |
| 任务 | GET /tasks、/tasks/{id}；POST /tasks/{id}/cancel |
| 审批 | GET /approvals；POST /approvals/{id}/decision |
| Python | POST /sandbox |
| 截图 | POST /screenshots |
| 知识 | GET/POST /knowledge/documents；DELETE /knowledge/documents/{id}；POST /knowledge/upload |
| 群文件全文 | POST /groups/{group}/files/{file}/index |
| 情景摘要 | GET /episodes |
| 媒体 | GET /media；POST /media/upload；POST /media/{id}/analyze；GET /media/{id}/content；DELETE /media/{id} |
| TTS | POST /media/speech |
| 表情 | GET /stickers；POST /stickers/{asset_id} |
| 插件 | GET/POST /plugins；POST /plugins/{id}/enable、disable、run |
| 模型 | GET/POST /models；GET /metrics/models |
| 数据 | GET /exports/user/{qq}；DELETE /users/{qq}/data；POST /maintenance/cleanup |
| QQ 空间 | GET /qzone/timeline；POST /qzone/actions |

## 任务与一致性

任务状态包括 queued、awaiting_approval、running、retry、succeeded、failed、cancelled、rejected、unknown。waiting_human 是持久化任务中的 external_status，表示浏览器需要扫码或验证。研究任务占用一个研究槽位，不阻塞所有索引和整理槽位。

审批记录绑定完整操作参数和任务 ID。已批准操作不能重复投票。代码、社交写等不自动重试；解析/同步/巩固只进行有限重试。远程执行采用稳定 ID，重启不能确认时记录 unknown，不重新运行用户代码。

完成后创建独立、幂等的 task.result 和 Outbox，引用原始 QQ 消息。发送前仍检查原消息、群策略、授权、知识版本、暂停和产物作用域。取消或撤回会撤销后续发送；已发给外部服务的请求不能被追回。

## 知识与记忆

PostgreSQL 迁移安装 pgvector，使用按模型/维度记录的向量；在明确授权的 ID 范围里计算余弦排序。SQLite 用 JSON 向量和 Python 余弦计算，适合开发；大量数据应增加按固定维度的 HNSW 等索引。

个人记忆没有自动获得 Embedding 授权。用户执行 /记忆 开启向量 后，仅对可检索/可披露的本人记忆建立索引；撤销会清理相应向量。向量不是事实真相。

文件下载拒绝私网、保留地址、凭据 URL、异常端口和重定向越权；解析器在独立 Worker 子进程有 CPU/内存/输出限制。文件格式仅开放文本、PDF、DOCX，不解包任意压缩文件或执行宏。只有群内开启全文授权才索引。

自动巩固只接受能追溯到同一主体本人的源陈述，低置信度、伪造主体和无效来源会被拒绝。不会自动将聊天事实或群文件发布成公共知识。公共文本导入需要 owner 显式 verified 和 license 元数据。

## QZone 外部适配器契约

需用户提供合法、已授权的适配器服务：

- GET /timeline → {items:[...]}。
- POST /actions，参数 {action:post|comment|like, arguments:{...}, request_id:string}。
- Authorization 使用 OSA_QZONE_ADAPTER_TOKEN，写操作带 Idempotency-Key。

本项目实现了客户端、管理界面、审批和任务轨迹，不声称捆绑一个已实机验证的 QQ 空间私有接口。未配置时明确拒绝执行，不影响群聊。

## 模型、对象和隔离

模型路由按用途替换 Provider，密钥只引用 OSA_MODEL_API_KEY 或 OSA_PROVIDER_KEY_* 环境绑定。用量保留调用、Token 和耗时；不是供应商账单。价格估算未配置时返回 null。

对象支持本地不透明 ID 或 S3。下载通过已认证的记录 ID，不用用户文件名拼路径。正文始终作为下载附件返回，不在同源执行 HTML/SVG。

本版单应用副本，不包含分布式连接迁移、跨实例租约或自动水平扩容。组件、端口和持久化逻辑已经实现，真实外部平台仍需联调。
