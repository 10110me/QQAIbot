"""Chinese console labels for both existing and newly recorded log entries."""
import re

EVENTS = {
    'image_description_ready':'图片识别完成，描述已回填聊天上下文',
    'image_description_failed':'图片识别失败，未猜测图片内容',
    'image_source_unavailable':'原始图片暂时无法获取',
    'agent_tool_request':'思考后开始调用工具', 'agent_tool_returned':'工具执行已返回',
    'workspace_action_recovered':'已恢复工作动作 JSON 结构','model_cooldown':'模型暂时不可用，使用已配置的备用模型',
    'conversation_batch_started':'开始处理一批群聊消息',
    'conversation_batch_waiting':'等待群友说完后重新判断',
    'conversation_batch_finished':'本轮群聊处理完成',
    'conversation_batch_failed':'本轮群聊处理失败',
    'conversation_batch_expired':'过期闲聊已并入历史，优先处理最新话题',
    'thinking_protocol_repair':'思考输出格式有误，正在纠正动作协议',
    'persona_reflection':'已处理 Nova 的性格成长笔记',
    'persona_reflection_failed':'性格成长笔记保存失败',
    'required_search_not_executed':'模型未执行要求的检索，回答已拦截',

    "work_input_ready":"工作附件读取完成", "work_input_failed":"工作附件读取失败",
    "model_request":"正在请求模型", "model_response":"已收到模型回复", "model_response_failed":"模型请求失败",
    "model_retry_scheduled":"模型请求将重试", "model_retry":"模型请求正在重试", "model_failover":"模型失败，切换备用模型",
    "thinking_failed":"思考流程失败",
    "application_started": "机器人服务已启动", "application_stopped": "机器人服务已停止",
    "message_ingested": "已接收消息", "message_sent": "回复已发送",
    "decision_recorded": "已记录回复决策", "outbox_finished": "发送任务已结束",
    "model_completed": "模型调用完成", "model_attempt_failed": "模型调用失败",
    "group_allowlist_updated": "群聊白名单已更新", "console_request": "管理接口访问",
    "api_failed": "接口处理失败", "malformed_onebot_event": "收到的 QQ 事件格式无效",
    "event_rejected_too_long": "消息过长，已忽略", "event_failed": "消息处理失败",
    "onebot_disconnected": "QQ 连接已断开", "onebot_connection_failed": "QQ 连接异常",
    "inbox_task_failed": "消息处理任务异常", "inbox_claim_failed": "读取待处理消息失败",
    "outbox_loop_failed": "回复发送循环异常", "browser_relay_failed": "浏览器连接转发失败",
}
FIELDS = {
    'tool':'工具',
    'closing_count':'补齐括号数',
    'message_count':'合并消息数','trigger':'触发原因','wait_seconds':'等待秒数',
    'cooldown_seconds':'下次普通决策等待秒数','step_count':'思考步骤数',
    'queue_age':'排队秒数',

    "groups":"白名单群数", "conversation":"会话", "event_id":"事件编号", "outbox_id":"发送任务编号",
    "purpose":"用途", "provider":"服务商", "model":"模型", "duration":"耗时（秒）",
    "input_tokens":"输入 Token", "output_tokens":"输出 Token", "attempt":"尝试次数",
    "error_type":"错误类型", "error":"原因", "status":"状态", "action":"动作", "reason":"原因",
    "method":"请求方法", "path":"接口路径", "added":"新增群", "removed":"移除群", "pipe":"转发通道",
}
VALUES = {
    'batch_included':'已合并到同一轮处理','message_count':'达到消息数量',
    'batch_expired':'过期闲聊无需再逐条回复','followup':'同一用户的连续补充',
    'max_wait':'达到最长等待时间','idle_compensation':'空窗补偿',
    'wait_timeout':'暂缓等待结束','direct':'点名或直接请求','policy':'群策略过滤',
    'SILENT':'保持沉默','THINK':'进入思考','SKIP':'本轮跳过','DEFER':'暂缓后复评',

    "None":"无", "pending":"等待处理", "processing":"处理中", "done":"已处理",
    "sent":"已发送", "sending":"发送中", "failed":"失败", "unknown":"结果未知", "cancelled":"已取消",
    "REPLY":"回复", "IGNORE":"不回复", "WAIT":"等待", "REACT":"回应", "INITIATE":"主动发言", "TASK":"任务",
    "work":"AI 工作", "observer":"代码审查", "chat":"聊天回复", "reply":"回复生成", "decision":"回复判断", "thinking":"思考", "planner":"规划",
    "embedding":"向量生成", "vision":"图片理解", "summary":"摘要", "memory":"记忆处理",
    "conversation_disabled":"该会话已停用", "non_message_event":"非聊天消息事件",
    "private_request":"收到私聊请求", "direct_mention":"有人 @ 机器人", "reply_to_bot":"有人回复机器人",
    "explicit_task_request":"收到明确的工作或研究命令",
    "humans_conversation":"群友正在交谈", "destination_no_longer_allowed":"目标群已从白名单移除",
    "name_call":"有人呼叫机器人名字或别名", "work_request":"识别为 AI 工作任务", "echo_silent":"群聊处于防复读静默期",
    "attachment_received":"已接收附件，等待需求消息", "work_mode_selected":"已完成工作模式评估",
    "send_policy_denied":"群策略或全局暂停禁止回复", "source_recalled":"原消息已撤回",
    "v3_skip":"智能体判断跳过", "v3_defer":"智能体暂缓处理",
    "stale_or_policy_denied":"消息已过期或被群策略拦截", "never_echo":"已拦截复读",
    "reply_expired":"主动回复超过有效期，已取消发送；直接请求会保留并引用原消息", "send_policy_denied":"当前群策略或暂停状态不允许发送",
    "memory_authorization_changed":"记忆授权已变化，已取消发送", "evidence_or_permissions_changed":"引用证据或功能权限已变化，已取消发送",
    "resource_permission_changed":"群公告或文件权限已变化，已取消发送", "initiative_no_longer_relevant":"最新聊天已不适合本次接话，回复模型选择保持安静",
    "output_policy_rejected":"输出内容被过滤规则拦截", "delivery_unknown":"发送结果无法确认",
    "permission_revoked":"发送权限已撤销", "upstream_unavailable":"上游服务不可用",
    "dispatcher_cancelled":"发送任务被中止", "process_restarted_during_send":"发送期间服务重启，结果待确认",
    "TimeoutError":"请求超时", "ReadTimeout":"读取响应超时", "ConnectTimeout":"连接超时", "ParserRejected":"文件格式不受支持",
    "ConnectError":"连接失败", "HTTPStatusError":"接口返回异常状态码", "ValueError":"数据格式或参数无效",
    "KeyError":"缺少必要数据", "PermissionError":"权限不足", "UpstreamUnavailable":"上游服务不可用",
    "WebSocketDisconnect":"连接已断开", "RuntimeError":"运行异常",
}
SOURCES = {
    "osa.main":"主服务", "osa.application.runtime":"消息处理与发送",
    "osa.infrastructure.configured_models":"模型调用", "osa.infrastructure.models":"模型服务",
    "osa.infrastructure.memory_store":"演示数据存储", "osa.infrastructure.store":"数据存储",
    "osa.interfaces.operations":"群聊与运维管理", "osa.interfaces.browser":"浏览器连接",
    "osa.application.features":"扩展功能", "osa.application.v3":"智能体服务",
    "osa.application.v2":"任务与记忆服务",
}


def chinese_source(source: str) -> str:
    for prefix, label in SOURCES.items():
        if source == prefix or source.startswith(prefix + "."):
            return label
    return source


def chinese_message(message: str) -> str:
    event, separator, tail = message.partition(" ")
    if event not in EVENTS:
        return message
    # Translate semantic values only; identifiers and model names stay intact.
    tail = re.sub(r"\b(action|reason|purpose|error|error_type|status)=([^\s]+)",
                  lambda m: m[1]+"="+VALUES.get(m[2],m[2]), tail)
    tail = re.sub(r"\b(group|private):", lambda m: "群聊：" if m[1]=="group" else "私聊：", tail)
    tail = re.sub(r"\b([a-z_]+)=", lambda m: FIELDS.get(m[1],m[1])+"：", tail)
    return EVENTS[event] + (" · " + tail if separator else "")
