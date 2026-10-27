# 4 核 / 8 GB RAM / 1 TB Linux 部署

0.4.0 根据 Design V3 增加独立 Social / Work 进程、任务注册表和工作台。新安装默认 `normal + PostgreSQL + work + sandbox`；每个编程任务仍需要后台审批。普通聊天、DeepSeek 网页登录、群授权与模型供应商仍需你配置。

## 首次安装

建议 x86_64 Linux、Docker Engine、Docker Compose **2.20+**、宿主机 Python **3.10+**。容器内为 Python 3.12，不需要显卡，也不把大模型部署到本机。

```bash
cd open-social-agent
python3 scripts/init_env.py
```

编辑 `.env`，填写真实值：

```dotenv
OSA_BOT_ID=你的机器人QQ号
OSA_ALLOWED_GROUPS=["授权群号"]
OSA_DEMO_MODE=false
OSA_MODEL_PROVIDER=openai
OSA_MODEL_BASE_URL=https://你的OpenAI兼容服务/v1
OSA_MODEL_API_KEY=你的密钥
OSA_CHAT_MODEL=供应商实际支持的模型
```

初始化脚本还会生成 PostgreSQL 密码、独立的管理员/NapCat/Worker/复读状态 Token。不要把真实 `.env` 发给别人。工作模型可另设 `OSA_WORK_MODEL_API_KEY`、`OSA_WORK_MODEL_BASE_URL`、`OSA_WORK_MODEL`，否则沿用聊天服务；两类请求的程序预算分别计算，供应商账单是否分开取决于账号。`OSA_WORK_MAX_TOKENS` 默认 8192，需要与所用模型的输出限制匹配。

检查根目录 `startup.ini`：

```ini
[runtime]
mode = normal
sandbox = true
project = osa-lowmem
storage = postgres
work = true
```

`project` 沿用历史名称以兼容原有数据卷，名称与当前资源模式无关，通常不要修改。

```bash
python3 start.py check
python3 start.py up
python3 start.py status
python3 start.py logs
```

启动器串行构建镜像及受限 Python Runner；PostgreSQL 健康检查通过后再启动应用与工作进程。首次构建需要互联网连接和一定时间。`check` 只验证配置，不能证明 Docker 运行、数据库或账号已经联通。

8 GB 普通模式不强制配置 Swap；可额外准备 2～4 GB 作为峰值缓冲。不要把所有内存留给 Chromium：系统、NapCat、数据库、截图与解析也有占用。初始主要工作并发为 **1**，消息并发 4、其他后台任务并发 3。先按这一配置压测，再考虑加并发。

## 控制台、QQ 和网页登录

8080 默认只绑定服务器本机。在自己电脑执行：

```bash
ssh -L 8080:127.0.0.1:8080 用户名@服务器IP
```

打开 `http://127.0.0.1:8080/`，输入 `.env` 的 `OSA_ADMIN_TOKEN`。NapCat 仍独立安装：宿主机 NapCat 用 `ws://127.0.0.1:8080/onebot/v11/ws`，Token 对应 `OSA_NAPCAT_TOKEN`。另一容器的 localhost 指向它自身，需要接入 `osa-lowmem_edge` 后连接 `ws://app:8080/onebot/v11/ws`。

DeepSeek 工作区继续提供可交互网页，用于首次扫码、人机验证和查看机器人问答。确认网页的联网搜索已开启；网站失效时等待人工或延后，不静默改用 API 研究。

在群策略里按需开启：

```json
{
  "sandbox": true,
  "research": true,
  "memory_extract": true,
  "experience_collect": true,
  "experience_embedding": false,
  "research_external": false,
  "echo_silence": true
}
```

界面编辑完整策略时保留其他字段。`research_external` 是夜间外发研究的额外授权；默认关闭。`experience_embedding` 另行授权把去身份化的技术指纹/候选方案发送给配置的 Embedding 服务，默认关闭。私人原始聊天、私有代码与日志不会自动作为夜间研究提问外发。

## 试运行一个工作任务

```text
@机器人 /工作 编写一个 Python 计算器并包含单元测试
@机器人 /进度 任务ID
@机器人 /追加需求 任务ID 增加除零异常测试
@机器人 /暂停工作 任务ID
@机器人 /继续工作 任务ID
@机器人 /取消工作 任务ID
```

创建后在 **AI 工作台**核对需求并批准。Work 在独立进程生成项目、写 Git 检查点、请求隔离 Runner 执行测试、归档成果。QQ 社交进程继续处理消息。工作中抑制主动水群，授权 @ 与进度仍走正常回复路径；复读群的静默优先级更高。

进度依据真实里程碑和测试报告，不按已耗时间猜百分比。无法估算完成时间就明确显示未知。失败、待输入和中断不会伪装成完成；完成项目可在后台下载源文件及测试报告，原会话会收到经 Outbox 发送的通知。

当前执行类型是 **Python 项目，可附静态 HTML/JS/CSS 文件**，Runner 不安装任意依赖、不访问网络、不执行宿主机 Shell。静态网页资产存在不等于浏览器功能验收通过。通用 Node/npm 构建、任意 Shell 项目、完整桌面 VM 和 Vue 前端重写不属于这个执行器的现成能力。

## 1 TB 存储布局

数据默认由 Docker 命名卷持久化：

| 数据 | 默认卷名 |
|---|---|
| PostgreSQL（核心、任务与经验） | `osa-lowmem_pgdata` |
| 附件与备份 | `osa-lowmem_appdata` |
| 工作源码、Git、成果 | `osa-lowmem_workspaces` |
| Chromium 登录与网页问答缓存 | `osa-lowmem_browserdata` |
| SQLite 控制平面（兼容模式） | `osa-lowmem_controldata` |

**确认 Docker 的实际数据目录在有空间的磁盘上**，1 TB 独立盘不会自动被项目使用：

```bash
docker info --format '{{.DockerRootDir}}'
df -h
lsblk -f
```

不要直接运行格式化/分区命令。需要迁移 Docker 数据目录时，按你的发行版步骤停止 Docker、备份并迁移；本程序不改主机分区。

可把系统/镜像、运行数据、备份分别预算为约 100 GB / 650 GB / 150 GB，留余量；这是规划建议，不会自动分配空间。容量够大也要设置保留期，长期记忆与工作仓库的增长需单独监控。

## 备份、停止和升级

```bash
python3 start.py backup
```

会短暂停止 Social、Work 和浏览器写入，备份数据库、附件、工作目录和浏览器卷，成功后启动原服务。文件在 appdata 卷的 `backups` 目录；包含私密数据，应另存到备份盘或其他主机。失败时停止后续步骤，保留数据卷，处理错误后再 `python3 start.py up`。该备份流程在当前环境未实机演练，正式使用必须验证恢复。

正常停止使用 `python3 start.py down`，不会删除卷。不要手动加 `-v`。升级前备份并保留原 `.env`、项目名和存储选择。

**旧版本有数据时：** 原 `startup.ini` 没有 storage/work 项会沿用 SQLite、关闭代码工作能力；不要用新模板直接覆盖。若希望保留旧 SQLite 数据，显式写 `storage = sqlite`，可以同时切普通模式与启用 work。改为 PostgreSQL 前必须做并验证数据迁移；本版不会自动执行 SQLite → PostgreSQL ETL，也不会把新空库当成已迁移。

## 实机验收

依次验证 QQ 连接、普通 @、网页登录、小研究、带测试的工作任务、任务控制、重启恢复、数据权限和备份恢复。用 `docker stats`、`free -h`、`df -h` 观察整个主机，包含 NapCat。8 GB 下的实际峰值、网页版本兼容、外部模型输出与供应商费用仍需要实测。

## V3 编程与审查

startup.ini 的 `coding=builtin` 保留默认 Python 工作路径；改为 `coding=opencode` 后启动器构建锁定的编码镜像和逐工作区网关。仍需专用编程模型配置和任务审批。后台新增 Agent 运行配置与只读审查；Observer 独立密钥留空则只给源码快照。见 [编程部署](coding-provider.md)、[模型配置](model-routing.md) 和 [V3 实现边界](v3-implementation.md)。
