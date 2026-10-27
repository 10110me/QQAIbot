# 0.3.0 与新服务器

你现在的 4 核 / 8 GB / 1 TB 主机优先使用 [新部署指南](server-4c8g.md) 的 normal 模式。下面说明保留给 1 GB 主机；若新装在 1 GB 主机，选 lowmem、storage=sqlite，可先 work=false、sandbox=false。已有 PostgreSQL 数据不能仅改 storage 就搬到 SQLite。

# 1 核 / 1 GB Linux 部署

0.2.2 起统一使用 `startup.ini` 的 `mode = lowmem` 和 `python3 start.py up`；完整双模式说明见 [启动配置](startup-modes.md)。

本配置面向少量群、低频使用。保留现有功能代码和权限流程，通过 SQLite、单并发、按需浏览器和重任务互斥节省内存。**需要 SSD 上至少 4 GiB Swap，不承诺所有页面和文件都能在 1 GB 上成功处理。** Swap 是慢速内存兜底，不会增加 CPU 算力。DeepSeek 网页自身的内存占用也不受本项目控制。

当前已验证代码行为和 Compose 静态配置，**尚未在真实 1 核 / 1 GB 主机完成容器压测，也未实测当前 DeepSeek DOM**。不能把下面的容器限额当成实测占用或稳定性保证。常驻 QQ/NapCat 客户端的资源需要另算；若也放在这台主机，必须一起观察。

## 改动

| 部分 | 低内存方式 |
|---|---|
| 数据库 | 本地持久化 SQLite，WAL，1 个连接，2 MiB 每连接页缓存；向量分批计算余弦，保留作用域检查 |
| 主应用 | 单进程、消息并发 1、后台任务并发 1；外部模型计算仍使用配置的 API |
| DeepSeek | 空闲时不运行 Chromium，任务或实时桌面唤醒，空闲约 30～45 秒释放；登录 profile 与问答数据库保留 |
| 人工登录 | 研究等待扫码/验证时让出后台任务名额，定时检查同一远端任务，避免重复提交 |
| 截图 | 独立、无登录资料的浏览器，收到截图才启动，完成或失败均关闭 |
| 重任务 | DeepSeek、截图、文件解析、视频和 Python 执行共享 Linux 文件锁；进程退出自动释放 |
| 搜索 | 省去常驻 SearXNG 容器；全部研究仍走 DeepSeek 网页，在该网页手动开启联网搜索 |
| 解析/视频 | 独立进程、内存边界、FFmpeg 单线程，容器间仍隔离网络和账号资料 |
| 附件/记录 | 单个下载默认最多 5 MB；原始记录和媒体按 .env 的保留期清理（默认 30 天），长期记忆不随之自动删除 |
| 容器 | 分别设置 RAM、RAM+Swap、PID 和 CPU 限额；日志轮转，限制磁盘累积 |

只减少并发和资源上限，没有用模拟结果代替真实服务。普通聊天、记忆、WebUI、研究、文件知识、媒体、截图、审批和插件仍使用原有流程；视觉/音频、Embedding、QQ 空间仍需要原来要求的外部供应商/适配器和权限。Python 与插件执行仍需显式开启 sandbox。

大文件、复杂 PDF、大型 Python 计算、多个群同时高频使用不适合此配置；超出限额会明确失败/等待人工。不要增大并发来解决排队。

## 安装

建议 Debian 12 / Ubuntu 22.04+ 的 x86_64 Linux、Docker Engine 与 Compose v2。其他架构必须先确认 Playwright 镜像支持。宿主机只需 Python 3 运行两个标准库脚本，不需要在宿主机安装 requirements.txt；容器内部仍使用 Python 3.12。

项目分区建议留至少 20 GiB 空间用于镜像、构建和运行数据，另有 4 GiB Swap。首次构建可能比较慢，按服务逐个构建，不要开启并行构建。

```bash
cd open-social-agent
python3 scripts/init_env.py
```

编辑 `.env`，填真实配置（机器人号和群号只能是数字字符串）：

```dotenv
OSA_BOT_ID=你的机器人QQ号
OSA_ALLOWED_GROUPS=["你的群号"]
OSA_DEMO_MODE=false
OSA_MODEL_PROVIDER=openai
OSA_MODEL_BASE_URL=https://api.deepseek.com
OSA_CHAT_MODEL=deepseek-chat
OSA_MODEL_API_KEY=你的供应商API密钥
```

网页 DeepSeek 登录与普通聊天 API 是两个独立配置。此配置不需要 `OSA_DB_PASSWORD`。不要把 `.env` 或真实凭据发给别人。

检查 Swap：

```bash
free -h
swapon --show
```

没有至少 4 GiB Swap 时，可在**你的 Linux 主机**显式执行下面的脚本。它会创建 `/swapfile-osa`，启用后加入 `/etc/fstab`，需要 root。已有同名文件不会覆盖；只自动处理 ext4/xfs，其他文件系统请按对应系统说明创建 Swap。禁止 Swap 的 VPS 套餐需要服务商开放或升级，脚本不能突破此限制。

```bash
sudo bash scripts/setup_swap.sh
python3 scripts/check_lowmem.py
```

前置检查通过后，串行构建并启动：

```bash
python3 start.py build
python3 start.py up --no-build
python3 start.py status
python3 start.py logs
```

启动器使用 `docker-compose.runtime.yml`，无需手选 Compose 文件。`startup.ini` 默认项目名为 `osa-lowmem`，两个模式共用 SQLite 数据，不会自动迁移之前 PostgreSQL 项目里的数据。已有数据库先备份，并明确制定数据迁移方案；不要把新 SQLite 空库当成已经迁移成功。

## 连接控制台与 QQ

8080 默认只绑定服务器 `127.0.0.1`。在自己的电脑执行：

```bash
ssh -L 8080:127.0.0.1:8080 用户名@服务器IP
```

然后在自己的浏览器打开 `http://127.0.0.1:8080/`，使用 `.env` 的 `OSA_ADMIN_TOKEN`。

NapCat 仍需独立安装，使用反向 WS `/onebot/v11/ws` 和 `.env` 的 `OSA_NAPCAT_TOKEN`。宿主机安装的 NapCat 可连接 `ws://127.0.0.1:8080/onebot/v11/ws`；其他容器中的 `127.0.0.1` 指向其自身，需把受信任 NapCat 容器接入 `osa-lowmem_edge` 网络，然后连接 `ws://app:8080/onebot/v11/ws`。不要为了方便直接公开无保护的管理端口。

进入“DeepSeek 工作区” → 连接实时浏览器 → 打开登录页面，手动扫码并完成人机验证。Linux 宿主机不用另装桌面环境。浏览器首次唤醒可能需要几十秒；桌面先出现空白背景时稍等并查看状态，不要连续重复提交。

在 DeepSeek 页面手动确认“联网搜索”已开启；选择器确认机制仍可配置。不能由打开网页推断网站已经联网检索。不要将没有来源的模型回答标记为已核实资料。

实时桌面连接期间会保活浏览器，并占用重任务名额。看完后点击新增的**“断开实时浏览器”**，问答历史随时可以单独查看。截图、文件解析或 Python 排队时，先断开桌面；长时间不释放名额可能触发等待超时。后台正常研究不需要始终开着桌面。

在群策略里按需要启用 `research`、`file_content_index`、`media_read` 等权限。若 Swap 下生成较慢，可把对应群 `reply_ttl` 调到 600 秒，避免过期回复被系统安全丢弃。按需开启媒体接口和模型配置，低内存模式不会替你填供应商凭据。

## 开启 Python 与插件执行

在 `startup.ini` 设置 `sandbox = true`；`.env` 可设 `OSA_SANDBOX_HOST_DIR=/opt/osa/sandbox-jobs`（Docker 守护进程所在宿主机的绝对路径）。然后：

```bash
python3 start.py up
```

每段用户代码依然在无网络、只读根目录、无 Docker Socket 的独立容器执行，低内存配置限制为 128 MiB RAM / 384 MiB RAM+Swap。简单 Python 可用，大计算或大型绘图可能因限额失败；任务结果不会假装成功。controller 仍是受信任、可访问 Docker Socket 的组件，不向公网发布。

## 实机验收与排查

```bash
docker stats --no-stream
free -h
vmstat 1
python3 start.py status
```

先验证 QQ 收发、重启后记忆保留，再验证 DeepSeek 登录与一条研究；随后依次测试小文件、截图、媒体和简单 Python。每次只做一项，观察所有容器和 NapCat 总体占用。

容器反复重启时用 `docker inspect 容器名 --format '{{.State.OOMKilled}}'` 检查内存终止。`vmstat` 中持续大量 `si/so`、控制台卡顿或延迟不可接受，说明已经超出当前主机的可用能力；优先减少群流量、关闭闲置实时桌面/其他程序。必要时把浏览器迁到其他设备或升级 RAM。不能只通过放大容器上限保证稳定。

数据位于 `osa-lowmem_appdata` 与 `osa-lowmem_browserdata` 卷。日常停止用 `python3 start.py down`，**不要加 `-v`，它会删除数据卷**。SQLite 与附件应一起备份；使用 SQLite backup API 保证 WAL 一致性，不能只复制正在运行的主 DB 文件。DeepSeek 网站账号自己的聊天记录另行管理。
