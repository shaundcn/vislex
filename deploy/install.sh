#!/bin/sh
set -eu

VISLEX_REPOSITORY="shaundcn/vislex"
VISLEX_DEPLOY_REF="${VISLEX_DEPLOY_REF:-main}"
VISLEX_COMPOSE_URL="${VISLEX_COMPOSE_URL:-https://raw.githubusercontent.com/${VISLEX_REPOSITORY}/${VISLEX_DEPLOY_REF}/deploy/compose.yaml}"
VISLEX_DIR="${VISLEX_DIR:-${HOME:?HOME is required}/vislex}"

fail() {
    printf 'Vislex 安装失败：%s\n' "$*" >&2
    exit 1
}

need_command() {
    command -v "$1" >/dev/null 2>&1 || fail "缺少命令：$1"
}

prepare_directory() {
    directory_path="$1"
    label="$2"
    [ ! -L "$directory_path" ] ||
        fail "${label} 不能是符号链接：$directory_path"
    if [ -e "$directory_path" ] && [ ! -d "$directory_path" ]; then
        fail "${label} 已存在但不是目录：$directory_path"
    fi
    mkdir -p "$directory_path"
}

need_command curl
need_command docker
need_command mktemp
need_command awk
need_command sed

docker compose version >/dev/null 2>&1 || fail "需要 Docker Compose v2"
docker info >/dev/null 2>&1 || fail "无法连接 Docker；请确认 Docker 已启动且当前用户有权限"

[ ! -L "$VISLEX_DIR" ] || fail "安装目录不能是符号链接：$VISLEX_DIR"
mkdir -p "$VISLEX_DIR"
VISLEX_DIR="$(cd "$VISLEX_DIR" && pwd -P)"

env_path="${VISLEX_DIR}/.env"
if [ -e "$env_path" ] && [ ! -f "$env_path" ]; then
    fail ".env 已存在但不是普通文件：$env_path"
fi
[ ! -L "$env_path" ] || fail ".env 不能是符号链接：$env_path"

compose_path="${VISLEX_DIR}/compose.yaml"
[ ! -L "$compose_path" ] || fail "compose.yaml 不能是符号链接：$compose_path"
if [ -e "$compose_path" ] && [ ! -f "$compose_path" ]; then
    fail "compose.yaml 已存在但不是普通文件：$compose_path"
fi

if [ ! -f "$compose_path" ]; then
    legacy_layout=false
    if [ -f "$env_path" ] || [ -e "${VISLEX_DIR}/input" ] ||
        [ -e "${VISLEX_DIR}/output" ] || [ -e "${VISLEX_DIR}/data" ]; then
        legacy_layout=true
    fi

    compose_part="${VISLEX_DIR}/.compose.yaml.vislex-$$.part"
    download_part="${VISLEX_DIR}/.compose-download.vislex-$$.part"
    trap 'rm -f "${compose_part:-}" "${download_part:-}"' EXIT HUP INT TERM
    curl --fail --silent --show-error --location \
        --proto '=https' --tlsv1.2 \
        "$VISLEX_COMPOSE_URL" >"$download_part"
    [ -s "$download_part" ] || fail "下载的 Compose YAML 为空"

    if [ "$legacy_layout" = true ]; then
        prepare_directory "${VISLEX_DIR}/input" "旧 input 目录"
        prepare_directory "${VISLEX_DIR}/output" "旧 output 目录"
        prepare_directory "${VISLEX_DIR}/data" "旧 data 目录"
        sed \
            -e 's#- \./输入文件夹:/app/input#- "${VISLEX_INPUT_DIR:-./input}:/app/input"#' \
            -e 's#- \./输出文件夹:/app/output#- "${VISLEX_OUTPUT_DIR:-./output}:/app/output"#' \
            -e 's#- \./数据文件夹:/app/data#- "${VISLEX_DATA_DIR:-./data}:/app/data"#' \
            "$download_part" >"$compose_part"
    else
        prepare_directory "${VISLEX_DIR}/输入文件夹" "输入文件夹"
        prepare_directory "${VISLEX_DIR}/输出文件夹" "输出文件夹"
        prepare_directory "${VISLEX_DIR}/数据文件夹" "数据文件夹"
        mv "$download_part" "$compose_part"
        download_part=""
    fi

    compose_output="$(
        docker compose \
            --project-directory "$VISLEX_DIR" \
            -f "$compose_part" \
            config
    )" || fail "下载的 Compose YAML 校验失败"
    resolved_image="$(
        printf '%s\n' "$compose_output" |
            awk '$1 == "image:" { print $2; exit }'
    )"
    case "$resolved_image" in
        shaundcn/vislex:1.1.1 | docker.io/shaundcn/vislex:1.1.1) ;;
        *) fail "Compose YAML 使用了非预期镜像：${resolved_image:-未知}" ;;
    esac

    chmod 0644 "$compose_part"
    mv "$compose_part" "$compose_path"
fi

compose() {
    docker compose \
        --project-directory "$VISLEX_DIR" \
        -f "$compose_path" \
        "$@"
}

compose pull
compose up -d

attempt=0
while [ "$attempt" -lt 30 ]; do
    if compose exec -T vislex python -c \
        "import os, urllib.request; port=os.getenv('UVICORN_PORT', '8000'); urllib.request.urlopen(f'http://127.0.0.1:{port}/healthz', timeout=3)" \
        >/dev/null 2>&1; then
        service_port="$(
            compose exec -T vislex python -c \
                "import os; print(os.getenv('UVICORN_PORT', '8000'))" \
                2>/dev/null || true
        )"
        case "$service_port" in
            '' | *[!0-9]*) service_port=8000 ;;
        esac
        published="$(compose port vislex "$service_port" 2>/dev/null || true)"
        published_port="${published##*:}"
        case "$published_port" in
            '' | *[!0-9]*) published_port="$service_port" ;;
        esac
        printf 'Vislex 已安装并通过健康检查。\n'
        printf '访问地址：http://NAS局域网IP:%s/\n' "$published_port"
        printf '安装目录：%s\n' "$VISLEX_DIR"
        exit 0
    fi
    attempt=$((attempt + 1))
    sleep 2
done

compose ps >&2 || true
fail "容器未在 60 秒内通过内部健康检查"
