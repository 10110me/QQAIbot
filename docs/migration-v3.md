# 从 0.3.x 升级到 0.4.0

保留原 `.env`、`startup.ini` 的 project/storage、DockerRootDir 与所有数据卷。先使用原版完成并复制一份备份，再停止原 app/work-worker。不要重新初始化或覆盖凭据文件，不要改变 SQLite/PostgreSQL 选择来“完成迁移”。

替换程序后，对照 `.env.example` 补齐 `OSA_V3_ENABLED=true` 等新增项。startup.ini 可增加 `coding=builtin`；首次升级保留默认执行器，之后再单独验证 `coding=opencode`。原来的普通/低内存开关不变。

Docker：`python3 start.py check` 后 `python3 start.py up`，应用自动执行 Alembic 至 0005。直接运行 Python：先安装 requirements.txt，再 `alembic upgrade head`。V3 还需 `OSA_V2_ENABLED=true`；独立 SQLite 控制文件由 Registry 事务初始化。

0005 增加投影、事件、唤醒、会话/回执、提供者绑定、审查表，V2 工作任务仍是事实源。已有任务投影不改变 owner/group/权限。没有自动 SQLite→PostgreSQL ETL。

确认旧任务、群策略、网页登录和控制台权限后创建一项小任务，审批、查询进度、暂停/恢复、下载成果并人工查看测试。然后配置独立 Observer；最后才启用可选 OpenCode 并验证其容器隔离、版本、取消及备份恢复。

需回滚时先停止新版进程，保留新备份，关闭 V3 并选择 builtin，再回到原程序/配置。若要执行 Alembic `downgrade 0004`，它会移除新增 V3 会话/审查/事件表；请先导出这些审计资料，且不能让任何 V3 Worker 继续访问数据库。V2 任务表保留；不把 OpenCode 的未知会话直接转为重新提交。数据卷不会随 `start.py down` 删除。
