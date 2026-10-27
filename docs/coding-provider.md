# 编程代理与部署

默认 `startup.ini` 的 `coding=builtin` 使用现有 Python 编程执行器。可选后端包括 OpenCode 和 Codex CLI：

```ini
[runtime]
mode = normal
sandbox = true
project = osa-lowmem
storage = postgres
work = true
coding = opencode
```

将 `coding` 改成 `codex` 即使用 Codex CLI。默认沿用网页“模型配置”中工作用途选中的实际模型 ID、供应商地址和密钥，无须再填独立密钥。该供应商只有 Chat Completions 接口时，工作区容器内的协议桥将 Codex 的 Responses 请求转为 Chat Completions，并把回复转换回来。当前所选模型已验证支持 Chat 工具调用。`OSA_CODEX_API_KEY`、`OSA_CODEX_BASE_URL`、`OSA_CODEX_MODEL` 仅用于显式覆盖为原生 Responses 端点；本机 CC Switch 配置不会自动进入 NAS 容器。

Codex CLI 读取每个工作区独立 HOME 中的 `config.toml`：`model` 选择实际请求模型，`model_provider` 指向自定义供应商，供应商项写入 `base_url`、`wire_api = "responses"` 与密钥环境变量名。不会将密钥写入配置文件。CC Switch 的 Chat 模式也通过本地路由双向转换协议，仅修改模型名称不能解决 `/responses` 404。工作区桥接固定使用 LiteLLM Core 1.106.0、兼容的 MCP SDK 2.3.0，并校验 LiteLLM Core Linux x64 wheel 的 SHA-256；不会安装受影响的 1.107.0 版本。

Codex 工作区容器以 `osa-cx-<项目名哈希>-<workspace_id>` 命名，使用独立代码卷和 HOME 卷。切换后端不会把 OpenCode 的会话历史自动转换为 Codex 会话；已有任务应先完成或停止并备份。Codex CLI 运行在隔离容器中，具有容器内完整工具权限，但不持有 Docker Socket。

在网页“模型配置”中选择工作模型后，设置 `coding=codex` 并运行 `python3 start.py up`。启动器构建 `osa-codex-runner:0.147.0` 并启动 `codex-gateway`；首次构建需要 npm/PyPI/镜像网络。OpenCode 模式则构建 `osa-opencode-runner:1.2.27` 并启动 `opencode-gateway`。普通模式 Codex 编码容器最多 1536 MB RAM、1 CPU，OpenCode 最多 768 MB，且最多一个活跃项目；闲置 180 秒且确认会话空闲后停止并删除容器，源码和会话数据卷保留。每 30 秒检查一次，已退出容器也会回收；会话忙碌或状态无法核实时不回收。继续原项目时，按同一 workspace_id 重建容器并挂回原数据卷，不新建任务，不丢失源码或会话。低内存模式 Codex 容器限 512 MB、OpenCode 限 384 MB，仍优先使用 builtin；1 GB 主机的 OpenCode/Codex 稳定性没有压测结论。

每个项目使用独立代码、HOME 和 OpenCode 会话数据库。主程序、Worker、网关、编码容器的职责分开；只有受信任网关持有 Docker Socket。子容器密码按工作区派生，不能用于调用网关或其他项目。无公开网关端口；不用把密码、4096 或 Docker Socket 暴露到公网。

网关默认使用 Worker Token 作为内部入口密码，可另设 `OSA_OPENCODE_PASSWORD`（至少 24 位）。这与子容器的派生密码不同。更换内部密码后会影响旧子容器鉴权，升级/轮换前先备份并停止任务，不能只覆盖密钥后假定旧会话仍可用。

OpenCode 在每个独立编码容器内拥有完整工具权限，可使用 Shell、外部目录、网页和网络下载；容器的独立网络、代码卷、HOME 卷、只读根文件系统、丢弃的 Linux capabilities 和无 Docker Socket 才是安全边界。平台仅取其最终文件 bundle，另行在无网络 Python 沙箱运行测试。项目成功必须有实际报告和完整成果，空闲状态及文字声明都不等于完成。暂停是安全边界暂停，进行中显示 PAUSING；取消与状态未知会保持真实待确认状态。

如果使用已有隔离服务器，可设置：

```dotenv
OSA_OPENCODE_MANAGED_GATEWAY=false
OSA_OPENCODE_ENDPOINT=https://你的受控网关/projects/{workspace_id}
OSA_OPENCODE_PASSWORD=独立密钥
OSA_OPENCODE_USERNAME=opencode
OSA_OPENCODE_VERSION=1.2.27
OSA_OPENCODE_ISOLATION_CONFIRMED=true
```

外部网关必须将每个 workspace 路由到独立文件系统和进程；路径占位符本身不会创造隔离。外部提供者的项目磁盘清理、备份与权限由部署者完成。当前启动器仍启用内置网关 profile；使用外部环境时可单独管理该可选服务。

接口依据 [OpenCode Server 文档](https://opencode.ai/docs/server/) 和 [v1.2.27 官方路由源码](https://github.com/anomalyco/opencode/blob/v1.2.27/packages/opencode/src/server/routes/session.ts)实现。运行前检查 `/global/health` 返回的版本，未知版本不自动接受；真实服务还需目标主机联调。

`python3 start.py backup` 在 OpenCode 模式下先停应用/Worker，再由网关停止并回收编码子容器，直接按当前项目命名空间枚举 HOME/代码数据卷并归档到主数据卷的 `backups/`；不依赖已回收的容器列表，随后备份核心数据和浏览器并恢复服务。外部服务器不在此归档范围。请复制备份到服务器之外，并在隔离环境演练恢复；恢复 OpenCode 还需要恢复原卷名、工作区映射、Registry 和入口密钥。

`down` 保留项目数据卷，网关正常退出会停止并删除子容器。宿主机/控制器异常退出后，下一次网关清理会回收已退出容器。数据卷不自动删除；不要执行 `docker volume prune` 或 `docker system prune --volumes`，因为未被容器挂载的卷仍可能是用户项目。

切换 coding Provider 前先完成或明确停止已有工作并备份；这与 normal/lowmem 的资源切换不同，不自动替换进行中的远端会话。
