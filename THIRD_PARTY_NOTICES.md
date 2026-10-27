# 第三方依赖说明

运行依赖由 pyproject.toml 声明，通过包管理器独立安装；本仓库不复制第三方实现或模型权重。各依赖许可证以安装版本附带的 LICENSE 和上游仓库为准。

NapCat 是用户独立安装的 OneBot 平台依赖，本项目只实现协议适配，不捆绑其代码、二进制或镜像。其使用与分发限制应独立核查：https://github.com/NapNeko/NapCatQQ/blob/main/LICENSE 。

OpenAI / DeepSeek 等服务需要用户自行配置 API 凭据并遵循供应商条款。前端不依赖外部字体、图片、CDN 或第三方 UI 框架。

自研代码的公开发行许可证由项目所有者在实际发布前选定；本交付没有代替所有者作开源许可承诺。


## 可选 OpenCode

OpenCode 1.2.27（MIT）：https://github.com/anomalyco/opencode/tree/v1.2.27 。本源码包包含原创适配器与 Docker 构建配置，没有内嵌 OpenCode 源码或二进制；选择 coding=opencode 时镜像从 npm 安装锁定的 opencode-ai 包。对应包及其依赖的许可证随各自发行物提供。
