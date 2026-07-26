# Vislex

vislex 是一个只在本机或受信任局域网运行的 Docker 网页应用。它自动监控
`input` 中稳定的视频，通过火山方舟 Files API 上传和预处理原视频，再用用户
选择的模型通过 Responses API 同时完成视频理解与原语言语音转写，最后把视频和
Markdown 平铺写入 `output`。

本项目没有登录功能，不允许直接暴露到公网。

公开镜像为 `docker.io/shaundcn/vislex`，支持 Linux amd64 和 arm64。Linux
服务器可以只下载部署 YAML 后运行，不需要克隆源码仓库。

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

启动不会清空、迁移或覆盖这三个目录中的已有内容。`docker compose down` 也不要附加
`-v`；这里使用的是绑定目录而不是命名卷。

## Linux 在线安装

要求目标 Linux 已安装 Docker Engine、Docker Compose v2、`curl` 和 `iproute2`。
下面一条命令会把部署文件保存到 `~/vislex`，自动检测默认路由使用的私有局域网
IPv4，创建三个空缺的数据目录，拉取公开镜像并等待健康检查：

```bash
curl -fsSL https://raw.githubusercontent.com/shaundcn/vislex/main/deploy/install.sh | sh
```

脚本只下载部署文件，不克隆源码。默认从外部端口 `8080` 访问：

```text
http://目标Linux的局域网IP:8080/
```

自动检测不合适时，可以明确指定地址、端口、版本或安装目录：

```bash
curl -fsSL https://raw.githubusercontent.com/shaundcn/vislex/main/deploy/install.sh \
  | HOST_BIND_IP=192.168.31.100 HOST_PORT=8080 VISLEX_TAG=1.0.0 VISLEX_DIR="$HOME/vislex" sh
```

安装脚本只接受分配给当前 Linux 主机的 `10/8`、`172.16/12` 或 `192.168/16`
地址，拒绝 `0.0.0.0`。第一次运行会创建权限为 `0600` 的 `.env`；后续运行保留
已有 `.env`、`input`、`output` 和 `data`。命令行显式提供的变量只覆盖本次运行，
如需永久修改请编辑 `~/vislex/.env`。

镜像式 Compose YAML 的公开地址是：

```text
https://raw.githubusercontent.com/shaundcn/vislex/main/deploy/compose.yaml
```

在 Portainer、1Panel 等界面粘贴 YAML 时，必须同时提供
`HOST_BIND_IP`、`TRUSTED_HOSTS`、`APP_UID` 和 `APP_GID`，并提前创建与
Compose 项目目录相对的 `input/output/data`。不要把 `HOST_BIND_IP` 设置为
`0.0.0.0`。

## 从源码启动

Docker Desktop 用户级 CLI 未进入当前 PATH 时，请使用登录 shell：

```bash
mkdir -p input output data
zsh -lc 'docker compose up -d --build'
zsh -lc 'docker compose ps'
curl --fail --silent --show-error http://127.0.0.1:8080/healthz
```

默认地址：

- 任务页：<http://127.0.0.1:8080/>
- 设置页：<http://127.0.0.1:8080/settings>

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

扫描器每30秒检查 `input` 顶层的非隐藏普通文件。文件大小和纳秒修改时间连续60秒不变
后建立任务。目录和符号链接不会被跟随。

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
丢弃额外字段；移除 `new_filename` 中的普通标点、常见视频扩展名并截断至20字符；
把字符串形式的 `transcript` 转为单元素数组并清理空项。规范化后仍使用严格 Pydantic
模型校验。

路径分隔符、控制字符、重复 JSON 字段、多个 JSON、解释性前后文、缺失或空白
`content` 不会被忽略。首次结果仍不合法时，第二次请求会附带具体校验错误；连续两次
失败才标记任务失败。设置页“测试”与正式任务复用完全相同的规范化、校验和纠错逻辑。
任何截断或 `incomplete` 响应直接失败。每个任务结束后会在5秒内尽力删除远程临时
文件；清理失败不会阻塞后续任务，并会保留待清理标记供容器重启后再次尝试。

## 输出

模型只允许返回：

```json
{
  "new_filename": "新文件名",
  "content": "视频内容",
  "transcript": ["转写内容"]
}
```

`new_filename` 只能包含1至20个中文、英文字母或数字。输出保留安全的原视频扩展名；
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

备份前建议停止新任务并复制整个 `data` 和 `output`：

```bash
zsh -lc 'docker compose stop'
# 复制 data 与 output 到安全位置
zsh -lc 'docker compose start'
```

不要单独复制正在写入的 SQLite 主文件而遗漏同目录的 WAL 文件。

## 在线安装的更新、回滚与卸载

更新到最新公开镜像时重新运行安装命令，或在安装目录执行：

```bash
cd "$HOME/vislex"
docker compose --env-file .env -f compose.yaml pull
docker compose --env-file .env -f compose.yaml up -d
```

固定或回滚到首个稳定版本：

```bash
sed -i.bak 's/^VISLEX_TAG=.*/VISLEX_TAG=1.0.0/' "$HOME/vislex/.env"
cd "$HOME/vislex"
docker compose --env-file .env -f compose.yaml pull
docker compose --env-file .env -f compose.yaml up -d
```

停止并删除容器但保留视频、Markdown、数据库和设置：

```bash
cd "$HOME/vislex"
docker compose --env-file .env -f compose.yaml down
```

不要给 `down` 添加 `-v`，也不要删除 `~/vislex/input`、`output` 或 `data`，
除非已经单独备份并明确希望删除这些数据。

## 局域网访问

源码 Compose 未配置 `.env` 时只绑定 `127.0.0.1`。局域网使用必须显式创建
`.env`，例如：

```dotenv
HOST_BIND_IP=192.168.31.65
HOST_PORT=8080
TRUSTED_HOSTS=127.0.0.1,localhost,192.168.31.65
APP_UID=501
APP_GID=20
```

然后重新创建容器：

```bash
zsh -lc 'docker compose up -d --build --force-recreate'
```

只允许可信局域网和主机防火墙范围。不要设置 `HOST_BIND_IP=0.0.0.0`，不要通过路由器
端口转发、云隧道或反向代理把它公开到互联网。

Linux 用户应把 `APP_UID`、`APP_GID` 改为以下命令的结果，确保非 root 容器能写入
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
HOST_BIND_IP=192.168.1.10 TRUSTED_HOSTS=127.0.0.1,localhost,192.168.1.10 \
  APP_UID="$(id -u)" APP_GID="$(id -g)" docker compose -f deploy/compose.yaml config
zsh -lc 'docker compose up -d'
zsh -lc 'docker compose ps'
set -a
[ ! -f .env ] || . ./.env
set +a
VISLEX_URL="http://${HOST_BIND_IP:-127.0.0.1}:${HOST_PORT:-8080}"
curl --fail --silent --show-error "${VISLEX_URL}/healthz"
curl --fail --silent --show-error "${VISLEX_URL}/" >/dev/null
curl --fail --silent --show-error "${VISLEX_URL}/settings" >/dev/null
```

## 常见问题

- `docker-credential-desktop` 找不到：使用 `zsh -lc 'docker compose ...'`。
- `docker compose -f https://...` 把 URL 当成本地路径：使用上面的在线安装命令；
  它会先下载并验证 YAML。
- 容器提示 `Permission denied`：检查 `.env` 中 `APP_UID`、`APP_GID` 是否与宿主用户
  一致，并确认三个绑定目录可写。
- 任务一直排队：先保存 Key、获取模型并保存模型/FPS。
- 模型出现在列表但任务失败：列表不做能力过滤，请在设置页使用“测试”确认所选模型
  支持视频 Files + Responses。
- `Invalid host header`：将实际访问 IP 或域名加入 `.env` 的
  `TRUSTED_HOSTS`，不要使用通配符开放公网。
