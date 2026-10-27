# 插件 SDK 与实际执行

`osa.sdk.contracts` 提供 Manifest、API 版本/核心版本检查和 CapabilityContext；`osa.domain.ports`、`features` 定义平台、模型、Embedding、媒体、工具等可替换接口。

社区插件在控制台注册 Manifest 与源码，初始停用。owner 明确批准权限和群范围后，才能执行；不接受未声明或不支持的高权限。当前开放 group.notices.read、group.files.read、conversation.read。

源码必须实现 `run(context: dict, arguments: dict)` 并返回 JSON 值。核心按当前群策略裁剪 context：至多十条当前群消息、已授权的当前群公告/文件元数据。没有数据库、模型、QQ 凭据或全局聊天记录。

执行时核心把代码和裁剪数据提交给独立 Python Runner。源码从不在主进程 import/exec。Runner 无网络、无 Docker Socket、无主机秘密，限制 CPU/内存/时间/进程数。返回值仅作为数据，不会直接转成工具或管理动作。

新平台/模型 Provider 属于部署者审核的基础设施插件，通过 Port 在 wiring 中接入；社区通用代码插件不因此自动获得网络或供应商密钥。插件状态与权限持久化，可在 WebUI 停用。

示例注册：

```json
{
  "manifest": {
    "id": "example.hello",
    "name": "Hello",
    "version": "0.1.0",
    "api_version": "1.0",
    "plugin_type": "tool",
    "isolation": "process",
    "permissions": []
  },
  "source": "def run(context, arguments):\n    return {'text': '你好', 'group': context['group_id']}"
}
```

启用需要明确 grants 和 groups。普通群友可 `@机器人 /插件 example.hello {}`，只能在已批准群执行。基础执行镜像只带声明的运行依赖，不在收到消息后任意 pip install。
