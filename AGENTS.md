# Vislex 项目规则

## 定位

- Vislex 是仅供本机或受信任局域网使用的视频理解、转写与 Markdown 归档应用。
- [README.md](README.md) 是安装、操作、数据恢复和安全说明的唯一用户文档。

## 运行与验证

- 启动：`zsh -lc 'docker compose up -d --build'`
- 健康检查地址以本机 `.env` 的 `HOST_BIND_IP` 和 `HOST_PORT` 为准。
- 编译与测试命令以 README“验证”一节为准。
- 未经用户明确授权并提供可用 API Key，不得调用真实火山方舟模型或上传视频。

## 技术边界

- 使用 Python 3.12、FastAPI、SQLite、FFmpeg、httpx、Pydantic、Jinja2、
  原生 HTML/CSS 和 Docker Compose。
- 不引入 JavaScript、Node.js、前端框架、Redis、对象存储或独立 ASR。
- 保持单进程、单工作线程、任务串行；SQLite 业务表只使用 `tasks` 和 `settings`。

## 数据与安全

- `input/`、`output/`、`data/` 和 `video/` 是用户或运行时数据，不得清空、覆盖或迁移。
- `output/` 必须保持扁平；迁移和恢复逻辑必须保留已有合法数据。
- 源码 Compose 默认只绑定 `127.0.0.1`。公开 NAS YAML 按产品要求使用
  `8000:8000` 监听全部接口，只允许受信任局域网或登录后的 FNConnect 使用；不得把
  无登录的服务直接暴露到公网。
- 未经明确要求，不提交、推送、改远端或执行破坏性 Git 操作。

## 当前状态与下一步

- 截至 2026-08-01，当前稳定版本为 `1.1.1`；移除了 Host 校验，公开 NAS YAML使用
  相同的内外8000端口和中文相对目录。编译、62项测试、8000/9090容器验证和公开
  RAW无克隆安装均已通过；SQLite架构未修改，真实方舟API未调用。
- GitHub 公开仓库为 `shaundcn/vislex`；实现提交为 `6ed8663`，发布标签为
  `v1.1.1`，main与标签工作流均成功。
- Docker Hub 的 `shaundcn/vislex:1.1.1` 与 `latest` 共享 manifest digest
  `sha256:cdd8fda977c5d471696efaa4d470c1cacdc254854a458cfafa13d4a102428d3f`，包含
  `linux/amd64` 和 `linux/arm64`；两种架构均已匿名拉取并通过 `/healthz`。
- 在线安装入口和镜像式 Compose 位于 `deploy/`；新安装创建中文目录，已有 Compose、
  `.env` 和旧英文数据目录保持原样。
- 后续修改先运行 README 中的验证，再按用户明确要求提交或推送。
