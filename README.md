# Vislex

vislex 是一个只在本机或受信任局域网运行的 Docker 网页应用。它自动监控
`input` 中稳定的视频，通过火山方舟 Files API 上传和预处理原视频，再用用户
选择的模型通过 Responses API 同时完成视频理解与原语言语音转写，最后把视频和
Markdown 平铺写入 `output`。

本项目没有登录功能，不允许直接暴露到公网。

公开镜像为 `docker.io/shaundcn/vislex`，支持 Linux amd64 和 arm64。Linux
服务器可以只下载部署 YAML 运行，不需要克隆源码仓库。

当前源码版本：`1.1.3`。Docker Hub 稳定版本：`1.1.3`。

## 技术与限制

- Python 3.12、FastAPI、Uvicorn、SQLite、httpx、Pydantic、Jinja2。
- 原生 HTML/CSS；没有 JavaScript、Node.js、前端框架、Redis或独立 ASR。
- FFmpeg/ffprobe 只用于检查视频和生成设置页的一秒 API 测试视频；任务视频不压缩、
  不转码、不切片。
- 单 Uvicorn 进程、单任务工作循环，任务严格串行。
- 方舟地址固定为 `https://ark.cn-beijing.volces.com/api/v3`。
- 单个视频不得超过30分钟或500,000,000字节。

## 目录

```text
.
├── app/                 FastAPI 应用、模板和样式
├── tests/               不调用真实模型的标准库测试
├── deploy/              在线安装脚本和镜像式 Compose YAML
├── .github/workflows/   Docker Hub 多架构发布工作流
├── input/               待处理视频（运行时创建，不纳入 Git）
├── output/              视频和 Markdown（运行时创建，不纳入 Git）
├── data/                SQLite 和 API Key（运行时创建，不纳入 Git）
├── Dockerfile
├── docker-compose.yml   本地源码构建
└── LICENSE
```

Compose 使用以下固定挂载：

```text
./input  → /app/input
./output → /app/output
./data   → /app/data
```

启动不会清空、迁移或覆盖这三个目录中的已有内容。`docker compose down`
也不要附加 `-v`；这里使用的是绑定目录而不是命名卷。

## NAS 与 Linux YAML 安装

飞牛 OS、Portainer、1Panel 或其他支持 Docker Compose 的 NAS 可以直接使用下面的
公开 YAML，不需要克隆源码：

```text
https://raw.githubusercontent.com/shaundcn/vislex/main/deploy/compose.yaml
```

YAML 内容保持极简：

```yaml
services:
  vislex:
    image: shaundcn/vislex:1.1.3
    ports:
      - "9602:9602"
    volumes:
      - ./输入文件夹:/app/input
      - ./输出文件夹:/app/output
      - ./数据文件夹:/app/data
```

宿主机和容器统一使用 `9602`，默认目录是 Compose 项目目录下的
`输入文件夹/输出文件夹/数据文件夹`。NAS 已有目录只需替换每条挂载冒号左侧的路径，
例如：

```yaml
volumes:
  - /你的待处理视频目录:/app/input
  - /你的Markdown归档目录:/app/output
  - /你的应用数据目录:/app/data
```

一条命令在线安装会创建但不清空三个中文目录并启动服务：

```bash
curl -fsSL https://raw.githubusercontent.com/shaundcn/vislex/main/deploy/install.sh | sh
```

安装器不会覆盖已有 `compose.yaml` 或 `.env`。检测到旧版英文
`input/output/data` 且缺少 Compose 时，会继续映射旧目录，不会自动迁移到中文目录。

也可以手动下载并运行YAML：

```bash
mkdir -p "$HOME/vislex"
cd "$HOME/vislex"
curl -fsSL https://raw.githubusercontent.com/shaundcn/vislex/main/deploy/compose.yaml \
  -o compose.yaml
docker compose -f compose.yaml up -d
```

访问地址为 `http://NAS局域网IP:9602/`。`9602:9602` 会监听 NAS 的全部网络接口；
这个简化 YAML 只用于可信局域网或登录后的 FNConnect，禁止公网端口转发、公共反向
代理或直接暴露公网。它不包含自动拉取、自动重启、健康检查、`init`、固定运行用户、
只读根文件系统或额外容器安全限制。

### FNConnect

先在 FNConnect 中登录 NAS，再进入 fnOS 的 Docker 页面并点击 Vislex 的 `9602`
映射端口。Vislex 不需要配置 FNConnect 域名，也不检查请求的 Host；访问控制完全依赖
NAS/FNConnect，因此不要把代理地址作为无保护的公开站点分享。

### 自定义端口和权限

自定义端口时，宿主机端口、容器端口和 `UVICORN_PORT` 必须相同，例如：

```yaml
ports:
  - "9090:9090"
environment:
  UVICORN_PORT: "9090"
```

镜像默认以内部身份 `10001:10001` 运行。NAS 挂载目录出现权限错误时，可按目录所有者
设置 `PUID`、`PGID`；镜像入口会先处理挂载目录，再降权运行：

```yaml
environment:
  PUID: "1000"
  PGID: "1000"
```

不要长期使用 `PUID=0`、`PGID=0`，这会让应用和 FFmpeg 持续以 root 身份运行。

## 从源码启动

Docker Desktop 用户级 CLI 未进入当前 PATH 时，请使用登录 shell：

```bash
mkdir -p input output data
zsh -lc 'docker compose up -d --build'
zsh -lc 'docker compose ps'
curl --fail --silent --show-error http://127.0.0.1:9602/healthz
```

默认地址：

- 任务页：<http://127.0.0.1:9602/>
- 设置页：<http://127.0.0.1:9602/settings>

健康响应为：

```json
{"status":"ok"}
```

## 首次设置

1. 在设置页输入火山方舟 API Key，点击“保存”。保存操作不会调用接口。
2. 点击“获取模型”。该操作只使用已经保存的 Key，缓存服务端返回的完整模型列表，
   不按视频能力过滤。
3. 选择模型与 0.2–5 范围内的抽帧频率，点击“保存设置”。默认 FPS 为 0.3。
4. 如需验证模型视频能力，可输入新 Key 或使用已保存 Key，选择当前模型和 FPS 后点击
   “测试”。测试会生成一秒临时视频，依次调用 Files、预处理和 Responses，并产生正常
   的按量费用；测试值不会保存。

API Key 原子写入 `data/ark_api_key` 并设置为 `0600`。网页只展示首4位、尾4位和
中间星号；短 Key 全部隐藏。Key 不写入 SQLite、日志、任务错误、Markdown或网页响应。

## 自动处理

源码 `1.1.3` 的扫描器每30秒检查 `input` 顶层及最多3层非隐藏子目录中的普通文件，
例如会扫描 `input/一层/二层/三层/视频.mp4`，不会进入第4层子目录。文件大小和纳秒
修改时间连续60秒不变后建立任务。隐藏文件、隐藏目录和符号链接不会被跟随；任务页和
任务页的“原文件”对嵌套视频显示相对于 `input` 的路径，便于区分
同名文件；Markdown frontmatter 的 `source` 只保留最后的文件名和扩展名。

状态依次为：

```text
queued → checking → uploading → processing → moving → success
                                           ↘ failed
                         checking          ↘ ignored
```

- 没有 Key 或模型时，稳定文件可以入队，但工作循环不会领取。
- 领取任务时，当前提示词、模型和 FPS 在一个 SQLite 事务中写入任务快照。
- 处理中修改设置不会影响该任务。
- 失败任务在网页点击“重试”后清空旧快照，并在下次领取时使用最新设置。
- `ignored` 和 `failed` 的源视频保留在 `input`。
- 只有视频和 Markdown 都已 `fsync`、原子发布并把两者的 SHA-256 校验值写入
  SQLite 后，才会把源视频原子隔离、复核文件身份并安全删除；并发放入的同名新文件
  不会被删除。

网络错误、HTTP 429 和 5xx 在首次请求后最多重试3次。Files 预处理每3秒轮询一次，
最长等待30分钟。模型结果会先进行安全规范化：允许整个响应由单个 JSON 代码块包裹；
丢弃普通额外字段；移除 `title` 中的普通标点、常见视频扩展名并截断至20字符；
把字符串形式的 `transcript` 转为单元素数组并清理空项。规范化后仍使用严格 Pydantic
模型校验。

路径分隔符、控制字符、重复 JSON 字段、多个 JSON、解释性前后文、缺失或空白
`content` 不会被忽略。旧字段 `new_filename` 无论单独出现还是与 `title` 同时出现，
都不会被转换或忽略，而是触发一次模型纠错。首次结果仍不合法时，第二次请求会
附带脱敏的校验错误；连续两次失败才标记任务失败。设置页“测试”与正式任务
复用完全相同的规范化、校验和纠错逻辑。
任何截断或 `incomplete` 响应直接失败。每个任务结束后会在5秒内尽力删除远程临时
文件；清理失败不会阻塞后续任务，并会保留待清理标记供容器重启后再次尝试。

## 输出

模型只允许返回：

```json
{
  "title": "新文件名",
  "content": "视频内容",
  "transcript": ["转写内容"]
}
```

`title` 只能包含1至20个中文、英文字母或数字。输出保留安全的原视频扩展名；
扩展名只允许最多10个英文字母或数字，空扩展名也可使用，`.md`、控制字符和 Markdown
结构字符会被拒绝。重名时追加 `-2`、`-3`，并截断主体以确保总主体仍不超过20字符。

```text
output/
├── 视频标题.mp4
├── 视频标题.md
├── 视频标题-2.mov
└── 视频标题-2.md
```

输出目录不创建子目录。模型文本会先进行 HTML 和 Markdown 转义，仅保留 `content`
中行首的短横线列表。无语音时 Markdown 写入“无可转写语音。”。

新生成 Markdown 的 frontmatter 固定为：

```markdown
---
title: "模型返回的title"
tags:
source: "原文件.mp4"
created: 2026-08-02
---
```

`created` 使用 Asia/Shanghai 日期并在任务进入 `moving` 时写入 SQLite；
中断恢复不会重新计算日期。重名产生 `-2` 时，`title` 仍是模型原标题，
视频嵌入链接使用实际带后缀的文件名。已存在的 `output` 视频和 Markdown 不会被
迁移或重写。

任务页每页显示200项，并提供无 JavaScript 的上一页、下一页导航。
成功任务可直接预览、下载视频或下载 Markdown。预览页在桌面端约按 70%/30%
并排显示可自适应、可全屏的视频与 Markdown，在手机端改为单列；视频接口支持
`HEAD` 和单段 HTTP Range 请求。

## 重启恢复与备份

SQLite 使用 WAL 和 `synchronous=FULL`。重启时，vislex：

- 只清理名称匹配 `.vislex-task-*.part` 的应用临时文件；
- 恢复安全删除过程中留下的 `.vislex-delete-*.part`，只有输出哈希通过后才完成删除；
- 保留处理中任务原有的模型、提示词和 FPS 快照；
- 已保存合法模型结果的 `moving` 任务直接恢复本地输出，不再次调用模型；
- 两个最终文件已经完成且源文件仍存在时，会逐字节核对输出后完成删除和状态收尾；
- 源文件已经不存在时，只有视频和 Markdown 都与删除前保存的 SHA-256 一致才会标记成功；
- 不修改任何已有成功输出。

新的 `data` 目录会创建 `tasks` 和 `settings` 两张业务表，并设置
`PRAGMA user_version=1`。初始化只使用“不存在则创建”和“不存在则写入默认设置”，
不包含删表、清空任务或重置设置的逻辑；重复启动会保留已有内容。

备份前建议停止新任务并复制整个 `data` 和 `output`：

```bash
zsh -lc 'docker compose stop'
# 复制 data 与 output 到安全位置
zsh -lc 'docker compose start'
```

不要单独复制正在写入的 SQLite 主文件而遗漏同目录的 WAL 文件。

## YAML 安装的更新、回滚与卸载

重新拉取当前YAML固定的镜像：

```bash
cd "$HOME/vislex"
docker compose -f compose.yaml pull
docker compose -f compose.yaml up -d
```

`1.1.3` 继续使用 `9602`。从 `1.1.2` 升级只需修改镜像标签并重新
创建容器；如果从 `1.1.1` 升级，还要把端口映射两侧从 `8000` 同时改为
`9602`。上一稳定版本为 `shaundcn/vislex:1.1.2`：

```yaml
services:
  vislex:
    image: shaundcn/vislex:1.1.2
```

停止并删除容器但保留视频、Markdown、数据库和设置：

```bash
cd "$HOME/vislex"
docker compose -f compose.yaml down
```

不要给 `down` 添加 `-v`，也不要删除中文目录或旧版 `input/output/data`，除非已经
单独备份并明确希望删除这些数据。

## 局域网访问

源码 Compose 未配置 `.env` 时只绑定 `127.0.0.1`。局域网使用必须显式创建
`.env`，例如：

```dotenv
HOST_BIND_IP=192.168.31.65
HOST_PORT=9602
PUID=501
PGID=20
```

然后重新创建容器：

```bash
zsh -lc 'docker compose up -d --build --force-recreate'
```

只允许可信局域网和主机防火墙范围。不要设置 `HOST_BIND_IP=0.0.0.0`，不要通过路由器
端口转发、云隧道或反向代理把它公开到互联网。

Linux 用户应把 `PUID`、`PGID` 改为以下命令的结果，确保降权后的容器能写入
绑定目录：

```bash
id -u
id -g
```

## 验证

测试使用临时目录与 `httpx.MockTransport`，不会读取或写入实际的
`input/output/data`，也不会调用真实模型：

```bash
zsh -lc 'docker compose build'
zsh -lc 'docker run --rm vislex:local python -m compileall app'
zsh -lc 'docker run --rm -v "$PWD:/workspace:ro" -w /workspace vislex:local python -m unittest discover -s tests -v'
zsh -lc 'docker compose config'
sh -n deploy/install.sh
docker compose -f deploy/compose.yaml config
VISLEX_VERIFY_DIR="$(mktemp -d)"
mkdir -p "${VISLEX_VERIFY_DIR}/input" "${VISLEX_VERIFY_DIR}/output" \
  "${VISLEX_VERIFY_DIR}/data"
docker run --rm -d --name vislex-verify-113 \
  -p 127.0.0.1:19602:9602 \
  -v "${VISLEX_VERIFY_DIR}/input:/app/input" \
  -v "${VISLEX_VERIFY_DIR}/output:/app/output" \
  -v "${VISLEX_VERIFY_DIR}/data:/app/data" \
  vislex:local
curl --retry 10 --retry-delay 1 --retry-connrefused \
  --fail --silent --show-error http://127.0.0.1:19602/healthz
curl --fail --silent --show-error http://127.0.0.1:19602/ >/dev/null
curl --fail --silent --show-error http://127.0.0.1:19602/settings >/dev/null
docker stop vislex-verify-113
```

## 常见问题

- `docker-credential-desktop` 找不到：使用 `zsh -lc 'docker compose ...'`。
- `docker compose -f https://...` 把 URL 当成本地路径：先下载 YAML，或在 NAS 的
  Compose 页面直接粘贴。
- 容器提示 `Permission denied` 或 `unable to open database file`：确认
  `PUID/PGID` 与映射目录所有者一致，并查看启动日志中的“Vislex 运行用户”。
- 任务一直排队：先保存 Key、获取模型并保存模型/FPS。
- 模型出现在列表但任务失败：列表不做能力过滤，请在设置页使用“测试”确认所选模型
  支持视频 Files + Responses。
- FNConnect 返回502：确认从已登录的fnOS Docker页面进入，并检查端口映射的左右两侧
  是否相同；公开YAML应显示 `9602:9602`。
