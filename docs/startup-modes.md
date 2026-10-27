# 0.4.0 / V3

新增 `coding=builtin/opencode`，默认 builtin；opencode 需要 work=true 和 sandbox=true，自动启用可选隔离网关及锁定的编码镜像。见 [编程配置](coding-provider.md)。普通模式 Thinking 限 12 步 / 75 秒，低内存 6 步 / 45 秒；不改变已有存储和授权。

# 0.3.0 更新

新 4 核 8 GB 安装默认 normal、PostgreSQL、work 与 sandbox，见 [新服务器部署](server-4c8g.md)。资源模式仍是下面两种；旧配置缺少 storage/work 时保持 SQLite 与 work=false。新增 storage=sqlite/postgres、work=true/false，work=true 需要 sandbox=true；改变存储必须先迁移数据。

# 在配置文件选择运行模式

从 0.2.2 起，推荐统一使用项目根目录的 `startup.ini` 和 `start.py`。宿主机需要 Python 3.10+ 标准库、Docker Engine 和 Compose v2，不用在宿主机安装 requirements.txt。容器内依然是 Python 3.12。

## 配置

```ini
[runtime]
mode = normal
sandbox = true
project = osa-lowmem
storage = postgres
work = true
coding = builtin
```

| 配置项 | 含义 |
|---|---|
| `mode = normal` | 普通模式，提高并发和内存限额，DeepSeek 浏览器保持运行，建议 8 GB RAM |
| `mode = lowmem` | 低内存模式，面向 1 核 1 GB、低频串行使用，需要至少 4 GiB Swap |
| `sandbox = true` | 启用隔离 Python/插件执行，自动构建 Runner 并启用 controller；原有授权与审批仍需配置 |
| `project` | 两种模式共用的 Docker 项目名，沿用之前低内存部署名，通常不要修改 |

0.3.0 新模板默认 `normal`，适配新的 4 核 / 8 GB 服务器；旧 1 GB 主机显式选 `lowmem`。拼写错误或无效配置会停止启动，不会自动猜测成另一个模式。QQ、模型 API、群白名单、Worker Token 等仍在 `.env`，凭据不会写进启动配置。

| 行为 | normal | lowmem |
|---|---|---|
| 消息并发 / 后台任务并发 | 4 / 3 | 1 / 1 |
| 数据库连接 | 5 | 1 |
| DeepSeek 浏览器 | 保持运行 | 需要时唤醒、空闲后休眠 |
| 重任务 | 独立 Worker 可并行 | 共用资源锁，串行运行 |
| 单文件上限 | 20 MB | 5 MB |
| Chromium 容器 RAM 上限 | 2 GiB | 448 MiB，另有 Swap 限额 |
| 数据库、附件、网页登录 | 共用所选数据库 / 数据卷 | 共用相同数据（storage 不随 mode 变化） |

两种模式都使用真实 DeepSeek 网页做研究，数据库类型由独立 storage 配置决定。网页中的“联网搜索”需人工确认开启；没有常驻 SearXNG 服务，问答历史、扫码验证、权限、审批、文件解析及媒体等原有流程保留。外部模型和媒体供应商依然需要配置。

模式统一管理资源限额与并发，启动时会覆盖 `.env` 里的并发数等资源项。记录保留期继续由 `.env` 的 `OSA_RETENTION_DAYS` 控制，默认 30 天，**不会因为选择 lowmem 自动改为 7 天**。

## 首次启动

```bash
cd open-social-agent
python3 scripts/init_env.py
# 编辑 .env，填写真实 QQ、群白名单、聊天 API 并关闭演示模式。
# 编辑 startup.ini，选择 mode。
```

低内存主机先配置 Swap：

```bash
sudo bash scripts/setup_swap.sh
```

然后统一启动：

```bash
python3 start.py check
python3 start.py up
```

`check` 只验证配置，不启动容器，也不输出凭据。`up` 在低内存模式下先做只读资源检查，再逐个构建镜像、启动服务；缺少 Swap 或 Docker 限额能力会停止后续步骤。它不会自动修改主机 Swap。开启 sandbox 后，启动器也会构建 Runner，无需另外手动写 profile 命令。

控制台默认只绑定服务器 127.0.0.1:8080。远程使用 SSH 转发：

```bash
ssh -L 8080:127.0.0.1:8080 用户名@服务器IP
```

自己的浏览器访问 `http://127.0.0.1:8080/`。QQ/NapCat 接入及首次 DeepSeek 登录见 low-memory.md。WebUI“系统设置”中的 `runtime_mode` 显示实际生效的模式。

## 切换模式

修改 `startup.ini` 的 `mode`，再次执行同一条命令：

```bash
python3 start.py up
```

启动器会重建需要更新的镜像并重建配置发生变化的容器。固定的项目名、数据卷和存储选择保证两种模式继续使用原来的记忆、附件和登录资料；网站登录也可能被 DeepSeek 自身过期，此时需再次登录。

**这是重启生效的配置，不是运行中热切换。** 切换前让正在生成或发送的任务完成；应用的恢复规则仍会把无法确认的外部操作标记为未知，不能保证切换过程没有短暂停顿。

## 其他命令

```bash
python3 start.py status        # 查看容器状态
python3 start.py logs          # 查看最近日志
python3 start.py build         # 只构建镜像
python3 start.py up --no-build # 已构建镜像时跳过构建，仍应用当前模式
python3 start.py down          # 停止服务，保留数据卷
python3 start.py backup        # 停止写入、备份、成功后启动原服务
```

这些命令都读同一个配置文件。可以用 `--config /路径/另一份.ini` 指定配置；项目目录和凭据文件仍固定为程序所在目录。启动器不会执行删除数据卷的命令。

## 旧部署升级

- 0.2.1 的低内存项目默认名也是 `osa-lowmem`。保持该名字和原 `.env`，从原项目目录升级，可继续使用 `osa-lowmem_appdata`、`osa-lowmem_browserdata` 等数据卷；先备份。
- 之前使用其他 `-p` 名称启动过时，`startup.ini` 的 `project` 必须填写那个名字，否则 Docker 会创建另一套数据卷。
- 之前的 PostgreSQL + pgvector 部署属于另一套存储配置，本启动器不会把 PostgreSQL 数据自动导入 SQLite。继续使用原 Compose，或先制定和验证迁移方案。
- 原始 Compose 文件保留供旧部署使用；统一启动器使用 `docker-compose.runtime.yml`，不要再手动叠加基础/完整版配置，也不要绕开启动器直接启动 runtime.yml；该文件要求启动器注入模式参数。

本次完成配置渲染和流程测试，未在实际服务器启动 Docker，也未完成 1 核 1 GB 的真实峰值压测。模式内的内存值是限额，不是实测占用。低内存仍适合低频使用，Swap 和 NapCat 的整体占用需实机验收。
