# 0.4.0 / V3

当前新部署见 [4 核 8 GB 指南](server-4c8g.md)，新增模型/编程配置见 [V3 对照](v3-implementation.md)，旧项目升级见 [迁移步骤](migration-v3.md)。下面保留旧 Compose 的参考说明；当前 runtime 默认 PostgreSQL，所有研究经 DeepSeek 网页。

# 0.3.0

优先使用 [4 核 8 GB 新服务器指南](server-4c8g.md)，V2 实现及限制见 [实现对照](v2-implementation.md)。以下旧配置说明仍保留，存储切换需要独立迁移。

# 部署补充

普通与低内存模式统一由 [startup.ini 与 start.py](startup-modes.md) 管理。低配 Linux（1 核 / 1 GB）选择 lowmem，需要 Swap。两种模式共用 SQLite 和数据卷；原 PostgreSQL Compose 保留供旧部署使用。

## 外部依赖

基础：Python 3.12+、requirements.txt。完整服务推荐 Docker Compose，使用 PostgreSQL+pgvector、browser-worker、indexer-worker、screenshot-worker、SearXNG，以及按 profile 开启的 sandbox controller。

主应用一个副本；8080 默认仅绑定宿主机 127.0.0.1。为远程 NapCat 和控制台配置可信网络/TLS。VNC/API Worker 不发布外网端口。

Docker 网络把数据库、浏览器、解析、截图、沙箱、搜索拆开，解析器所在网络无外部访问。主应用是受信任的桥接点；用户代码容器 network=none。sandbox controller 需要 Docker Socket，应放在专用执行主机或 VM，不视为任意代码可访问的服务。

本地 Python app 连接容器 Worker 时，可写一个本机覆盖 Compose 文件，仅把 Worker API 映射到 127.0.0.1。完整 Compose 中的服务地址自动覆盖 .env 的 localhost 示例。

## 迁移/恢复

0.1 升级：备份数据库和对象目录，安装新版依赖，补齐 .env.example 新键，运行 alembic upgrade head，再启动。PostgreSQL 需要具备 pgvector 扩展的实例和安装扩展的权限。

SQLite 备份用 sqlite3 backup API；PostgreSQL 备份需要 pg_dump。恢复前停止主应用，恢复 DB 和关联对象目录；S3 使用自身备份/版本化。profile 登录卷和 DeepSeek 网站历史有独立生命周期。

## 供应商配置

聊天、记忆/决策、视觉/ASR/TTS、Embedding 服务可分别配置。OpenAI兼容仅表示协议形状，供应商不一定同时实现所有端点。媒体服务与群开关都需开启；缺少视觉模型不会根据经验猜图。

DeepSeek 网页研究始终经过可见浏览器；官方 API 仅可作为普通聊天等其他用途，不能在网页登录失败时静默替代研究。QZone 服务需满足 docs/full-features.md 的显式契约。

## 已知实现边界

本版不是分布式系统，不提供账单级价格核对。向量使用授权集合里的真实距离计算，尚未为异构维度自动生成 ANN 索引。自动巩固依赖模型结构化输出，失败保留源消息并有限重试，不保证每次摘要都正确；使用后台查看/纠正/删除。

浏览器 Worker 的稳定文本完成判据和选择器要按网页版本实测；人机验证由你手动完成。代码隔离基于 Docker 配额和权限，不是绝对不可逃逸的 VM。服务镜像与依赖在当前环境没有下载构建，正式部署后应固定验证过的锁文件和镜像摘要。
