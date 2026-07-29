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
- 源码 Compose 和 NAS YAML 默认只绑定 `127.0.0.1`；局域网部署必须自动检测或
  显式设置私有 IPv4。不得把无登录的服务直接暴露到公网。
- 未经明确要求，不提交、推送、改远端或执行破坏性 Git 操作。

## 当前状态与下一步

- 截至 2026-07-29，当前源码目标版本为 `1.1.0`，新增 `input` 最多3层子目录扫描；
  本地 `1.1.0` 容器健康，编译及 61 项测试通过，现有 SQLite 架构和 19 条成功任务
  保持完整。
- GitHub 公开仓库为 `shaundcn/vislex`；`1.1.0` 源码与 RAW 在线安装文件已同步到
  `main`，发布标签为 `v1.1.0`。
- Docker Hub 的 `shaundcn/vislex:1.1.0` 与 `latest` 均包含 `linux/amd64` 和
  `linux/arm64`；`1.0.1` 保留为不可覆盖的上一固定版本。
- 在线安装入口和镜像式 Compose 位于 `deploy/`；安装脚本自动检测私有局域网 IPv4，
  并持久化自定义端口、PUID/PGID 和三个映射目录。
- 后续修改先运行 README 中的验证，再按用户明确要求提交或推送。
