# V3 模型配置

三个核心角色：`decision`、`thinking`、`reply`。附属角色：DeepSeek 网页研究、`memory`、Embedding、图像和实际音视频理解。编程代理及独立 Observer 是专业接口，无 TTS。

`.env` 可选：

```dotenv
OSA_V3_ENABLED=true
OSA_DECISION_MODE=hybrid
OSA_DECISION_MODEL=供应商支持的决策模型
OSA_THINKING_MODEL=供应商支持的思考模型
OSA_REPLY_MODEL=供应商支持的表达模型
OSA_MEMORY_MODEL=供应商支持的整理模型
OSA_OBSERVER_MODEL=独立审查模型
OSA_OBSERVER_BASE_URL=https://你的供应商/v1
OSA_OBSERVER_API_KEY=独立审查密钥
OSA_MAX_DAILY_OBSERVER_CALLS=30
```

核心角色留空共用 `OSA_CHAT_MODEL` 的供应商配置，调用目的和提示仍分离。后台「模型路由」可为 decision/thinking/reply/memory 分别配置 `base_url/model/api_key_env`；密钥只引用已配置的 `OSA_PROVIDER_KEY_*` 环境变量。新变量进入容器需要重建/重启应用；页面不保存和返回密钥明文。

Observer 使用自己的环境配置和每日预算，不随普通聊天供应商切换。留空 Observer 模型时仅返回固定快照，无模型审查。工作模型用 `OSA_WORK_MODEL_API_KEY / BASE_URL / MODEL`，与 Thinking 和 Reply 独立。

默认普通模式每段 Thinking 最多 12 步 / 75 秒，低内存 6 步 / 45 秒，由启动器配置。达到限制后本轮失败/沉默，不无限占用会话。直接启动 Python 可设置 `OSA_THINKING_MAX_STEPS / SECONDS`。所有会话仍受每日模型调用预算和熔断控制。

Decision 本地实现是启发式规则，`calibrated=false`；0.8 只是回退阈值。LLM 自评分不会成为统计概率。被 @ 不受普通主动参与阈值拒绝，仍受 ACL、群开关和复读等硬规则约束。
