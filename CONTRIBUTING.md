# 开发约定

Python 3.12+。在项目目录执行 `uv sync`、`uv run python -m unittest discover -s tests -v`。

保持内层无第三方 SDK 依赖；新增基础设施通过 Protocol 注入。数据库变更新增 Alembic revision，不修改已发布迁移。权限变更必须同时添加越权拒绝测试，不把权限检查交给 Prompt。

FakeModel、模拟 QQ 和测试数据仅使用人造内容。不要提交 .env、真实 QQ 记录、模型密钥或群文件。PR 说明行为变化、配置、迁移和实际验证结果。
