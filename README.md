# 汐言 · TideTalk

**让群聊自然流动，让记忆与行动有据可循。**

汐言是基于 Python、OneBot 和大模型的 QQ 社交 Agent 框架，提供群聊理解、可检索记忆、多模态、工具调用、隔离工作空间与 Web 管理界面。Nova 是可配置的示例 AI 角色，不是框架名称。

当前版本：`0.4.0`。发布版不包含部署者的账号、密钥、登录态、聊天记录或数据库。外部平台与供应商兼容性需要在自己的环境验证。

## 能力

- **消息理解**：消息合并、时间与发送者标注、艾特与引用识别，中文提示词驱动思考与回复。
- **统一思考**：V3 由思考模型决定沉默、回复或连续调用工具，支持批量动作、输出校验和真实回执。
- **记忆与历史**：按 QQ 主体检索记忆、搜索消息、去重与来源追踪。跨群共享受配置和授权控制，不等于公开私聊。
- **多模态**：图片、语音、媒体处理和供应商支持的原生视频输入；关键帧路径作为兼容方案。
- **工具集成**：qqbot 能力发现与缓存、携图调用、群成员查询和群管理；程序检查群角色与授权。
- **AI 工作空间**：builtin、OpenCode、Codex CLI 后端，独立任务、进度、文件与测试证据。
- **模型与 WebUI**：供应商/模型/功能分配、批量替换、模型重试、端点并发、请求统计与移动端布局。

部分功能依赖独立 Worker、OneBot、浏览器会话、Docker 或商业模型服务。工具可用性取决于配置和权限，不保证各后端具有相同能力。

## 快速开始

需要 Python **3.12+**。Docker 部署另需 Docker Engine 与 Compose 插件。

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m pip install -e . --no-deps
python scripts/init_env.py
```

Windows PowerShell 使用 `.venv\Scripts\Activate.ps1` 激活环境。

编辑生成的 `.env`，配置自己的机器人 QQ、群白名单、模型端点和密钥。初始演示模型不能代替真实联调。脚本生成独立随机 Token，不覆盖已有 `.env`。

本地运行：

```bash
alembic upgrade head
osa
```

访问 `http://127.0.0.1:8080/`，用自己的 `OSA_ADMIN_TOKEN` 登录。不要将管理界面、Docker Socket、数据库或 Worker 端口直接暴露到公网。

Docker 部署：

```bash
python start.py check
python start.py up
```

运行模式、存储与编程后端在 `startup.ini` 配置。先阅读 [部署指南](docs/server-4c8g.md) 和 [编程后端说明](docs/coding-provider.md)。旧部署不要随意修改项目名、数据库或卷名。

## 配置与兼容

- 模块和命令仍为 `osa`，环境变量仍用 `OSA_` 前缀，保持旧部署兼容，不要求重命名数据卷。
- 密钥只配置在自己的 `.env` 或加密运行配置；`.env.example` 只有示例值。
- `python scripts/apply_nova_v4.py` 可向本地实例写入中文 V4 人设；会备份并保留模型配置与 QQ 绑定。
- 不提供默认主人 QQ、真实群白名单、私有供应商地址或共用账号。
- 容器网络与执行权限按需求配置；不能因使用容器而忽略宿主挂载、Socket 和密钥边界。

## 文档

- [服务器部署](docs/server-4c8g.md) / [低内存部署](docs/low-memory.md)
- [模型配置](docs/model-routing.md) / [重试策略](docs/model-retry.md)
- [工作空间](docs/ai-workspace.md) / [编程后端](docs/coding-provider.md)
- [中文人设参考](docs/nova-v4-user-prompt.txt)
- [隐私与授权](docs/privacy.md) / [安全报告](SECURITY.md)
- [脱敏发布流程](docs/public-release.md) / [第三方依赖](THIRD_PARTY_NOTICES.md)

历史设计文档用于理解架构，具体功能以当前代码与配置为准，不将旧设计视为当前行为承诺。

## 开发与发布

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
python scripts/public_release.py --output dist/tidetalk
python scripts/package.py
```

外部服务、浏览器和专用 PostgreSQL 测试需另行准备。历史测试差异不应通过隐藏失败伪装成全绿。

发布脚本使用文件白名单，检查已知凭据和私密标识，命中即阻止导出。只上传审查后的 `dist/` 导出目录，**不要上传部署目录或复制原 Git 历史**。

## 许可

公开可见不自动等于获得开源许可。本项目尚未指定开源许可证；第三方组件见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
