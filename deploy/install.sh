#!/bin/sh
set -eu

VISLEX_REPOSITORY="shaundcn/vislex"
VISLEX_DEPLOY_REF="${VISLEX_DEPLOY_REF:-main}"
VISLEX_COMPOSE_URL="${VISLEX_COMPOSE_URL:-https://raw.githubusercontent.com/${VISLEX_REPOSITORY}/${VISLEX_DEPLOY_REF}/deploy/compose.yaml}"
VISLEX_DIR="${VISLEX_DIR:-${HOME:?HOME is required}/vislex}"
host_port_from_shell="${HOST_PORT+x}"
tag_from_shell="${VISLEX_TAG+x}"
puid_from_shell="${PUID+x}"
pgid_from_shell="${PGID+x}"
input_dir_from_shell="${VISLEX_INPUT_DIR+x}"
output_dir_from_shell="${VISLEX_OUTPUT_DIR+x}"
data_dir_from_shell="${VISLEX_DATA_DIR+x}"
HOST_PORT="${HOST_PORT:-8080}"
VISLEX_TAG="${VISLEX_TAG:-latest}"
PUID="${PUID:-$(id -u)}"
PGID="${PGID:-$(id -g)}"

fail() {
    printf 'Vislex 安装失败：%s\n' "$*" >&2
    exit 1
}

need_command() {
    command -v "$1" >/dev/null 2>&1 || fail "缺少命令：$1"
}

dotenv_value() {
    key="$1"
    file="$2"
    awk -F= -v wanted="$key" '
        $0 !~ /^[[:space:]]*#/ && $1 == wanted {
            sub(/^[^=]*=/, "")
            print
            exit
        }
    ' "$file"
}

valid_ipv4() {
    printf '%s\n' "$1" | awk -F. '
        NF != 4 { exit 1 }
        {
            for (i = 1; i <= 4; i++) {
                if ($i !~ /^[0-9]+$/ || $i < 0 || $i > 255) {
                    exit 1
                }
            }
        }
    '
}

private_ipv4() {
    printf '%s\n' "$1" | awk -F. '
        $1 == 10 { exit 0 }
        $1 == 192 && $2 == 168 { exit 0 }
        $1 == 172 && $2 >= 16 && $2 <= 31 { exit 0 }
        { exit 1 }
    '
}

assigned_ipv4() {
    ip -o -4 address show | awk -v wanted="$1" '
        {
            split($4, address, "/")
            if (address[1] == wanted) {
                found = 1
            }
        }
        END { exit found ? 0 : 1 }
    '
}

valid_port() {
    printf '%s\n' "$1" | awk '
        $0 !~ /^[0-9]+$/ || $0 < 1 || $0 > 65535 { exit 1 }
    '
}

valid_id() {
    printf '%s\n' "$1" | awk '
        $0 !~ /^[0-9]+$/ || ($0 + 0) > 2147483647 { exit 1 }
    '
}

valid_tag() {
    printf '%s\n' "$1" | awk '
        length($0) < 1 || length($0) > 128 { exit 1 }
        $0 !~ /^[A-Za-z0-9_][A-Za-z0-9_.-]*$/ { exit 1 }
    '
}

prepare_mount_directory() {
    requested="$1"
    label="$2"
    case "$requested" in
        /*) directory_path="$requested" ;;
        *) directory_path="${VISLEX_DIR}/${requested}" ;;
    esac
    [ ! -L "$directory_path" ] ||
        fail "${label} 不能是符号链接：$directory_path"
    if [ -e "$directory_path" ] && [ ! -d "$directory_path" ]; then
        fail "${label} 已存在但不是目录：$directory_path"
    fi
    mkdir -p "$directory_path"
    (cd "$directory_path" && pwd -P)
}

need_command awk
need_command curl
need_command docker
need_command ip
need_command mktemp

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

if [ -f "$env_path" ]; then
    if [ -z "${HOST_BIND_IP:-}" ]; then
        HOST_BIND_IP="$(dotenv_value HOST_BIND_IP "$env_path")"
    fi
    saved_host_port="$(dotenv_value HOST_PORT "$env_path")"
    saved_tag="$(dotenv_value VISLEX_TAG "$env_path")"
    saved_puid="$(dotenv_value PUID "$env_path")"
    saved_pgid="$(dotenv_value PGID "$env_path")"
    saved_input_dir="$(dotenv_value VISLEX_INPUT_DIR "$env_path")"
    saved_output_dir="$(dotenv_value VISLEX_OUTPUT_DIR "$env_path")"
    saved_data_dir="$(dotenv_value VISLEX_DATA_DIR "$env_path")"
    if [ -z "$host_port_from_shell" ] && [ -n "$saved_host_port" ]; then
        HOST_PORT="$saved_host_port"
    fi
    if [ -z "$tag_from_shell" ] && [ -n "$saved_tag" ]; then
        VISLEX_TAG="$saved_tag"
    fi
    if [ -z "$puid_from_shell" ] && [ -n "$saved_puid" ]; then
        PUID="$saved_puid"
    fi
    if [ -z "$pgid_from_shell" ] && [ -n "$saved_pgid" ]; then
        PGID="$saved_pgid"
    fi
    if [ -z "$input_dir_from_shell" ] && [ -n "$saved_input_dir" ]; then
        VISLEX_INPUT_DIR="$saved_input_dir"
    fi
    if [ -z "$output_dir_from_shell" ] && [ -n "$saved_output_dir" ]; then
        VISLEX_OUTPUT_DIR="$saved_output_dir"
    fi
    if [ -z "$data_dir_from_shell" ] && [ -n "$saved_data_dir" ]; then
        VISLEX_DATA_DIR="$saved_data_dir"
    fi
fi

VISLEX_INPUT_DIR="${VISLEX_INPUT_DIR:-${VISLEX_DIR}/input}"
VISLEX_OUTPUT_DIR="${VISLEX_OUTPUT_DIR:-${VISLEX_DIR}/output}"
VISLEX_DATA_DIR="${VISLEX_DATA_DIR:-${VISLEX_DIR}/data}"
VISLEX_INPUT_DIR="$(prepare_mount_directory "$VISLEX_INPUT_DIR" "input 目录")"
VISLEX_OUTPUT_DIR="$(prepare_mount_directory "$VISLEX_OUTPUT_DIR" "output 目录")"
VISLEX_DATA_DIR="$(prepare_mount_directory "$VISLEX_DATA_DIR" "data 目录")"
if [ "$VISLEX_INPUT_DIR" = "$VISLEX_OUTPUT_DIR" ] ||
    [ "$VISLEX_INPUT_DIR" = "$VISLEX_DATA_DIR" ] ||
    [ "$VISLEX_OUTPUT_DIR" = "$VISLEX_DATA_DIR" ]; then
    fail "input、output 和 data 必须映射到三个不同目录"
fi

if [ -z "${HOST_BIND_IP:-}" ]; then
    HOST_BIND_IP="$(
        ip -4 route get 1.1.1.1 2>/dev/null |
            awk '{
                for (i = 1; i <= NF; i++) {
                    if ($i == "src") {
                        print $(i + 1)
                        exit
                    }
                }
            }'
    )"
fi

[ -n "$HOST_BIND_IP" ] || fail "无法自动检测局域网 IPv4；请显式设置 HOST_BIND_IP"
valid_ipv4 "$HOST_BIND_IP" || fail "HOST_BIND_IP 不是有效 IPv4：$HOST_BIND_IP"
private_ipv4 "$HOST_BIND_IP" || fail "HOST_BIND_IP 必须是 10/8、172.16/12 或 192.168/16 私有地址"
assigned_ipv4 "$HOST_BIND_IP" || fail "HOST_BIND_IP 未分配给当前主机：$HOST_BIND_IP"
valid_port "$HOST_PORT" || fail "HOST_PORT 必须是 1 至 65535"
valid_id "$PUID" || fail "PUID 必须是非负整数"
valid_id "$PGID" || fail "PGID 必须是非负整数"
if [ "$PUID" -eq 0 ]; then
    printf '%s\n' \
        "Vislex 警告：PUID=0 会让应用持续以 root 身份运行。" >&2
fi
valid_tag "$VISLEX_TAG" || fail "VISLEX_TAG 不是有效镜像标签"

TRUSTED_HOSTS="127.0.0.1,localhost,${HOST_BIND_IP}"
export HOST_BIND_IP HOST_PORT VISLEX_TAG PUID PGID TRUSTED_HOSTS
export VISLEX_INPUT_DIR VISLEX_OUTPUT_DIR VISLEX_DATA_DIR

if [ ! -f "$env_path" ]; then
    env_part="${VISLEX_DIR}/.env.vislex-$$.part"
    trap 'rm -f "${env_part:-}" "${compose_part:-}"' EXIT HUP INT TERM
    umask 077
    {
        printf 'HOST_BIND_IP=%s\n' "$HOST_BIND_IP"
        printf 'HOST_PORT=%s\n' "$HOST_PORT"
        printf 'TRUSTED_HOSTS=%s\n' "$TRUSTED_HOSTS"
        printf 'PUID=%s\n' "$PUID"
        printf 'PGID=%s\n' "$PGID"
        printf 'VISLEX_TAG=%s\n' "$VISLEX_TAG"
        printf 'VISLEX_INPUT_DIR=%s\n' "$VISLEX_INPUT_DIR"
        printf 'VISLEX_OUTPUT_DIR=%s\n' "$VISLEX_OUTPUT_DIR"
        printf 'VISLEX_DATA_DIR=%s\n' "$VISLEX_DATA_DIR"
    } >"$env_part"
    mv "$env_part" "$env_path"
fi

compose_path="${VISLEX_DIR}/compose.yaml"
[ ! -L "$compose_path" ] || fail "compose.yaml 不能是符号链接：$compose_path"
compose_part="${VISLEX_DIR}/.compose.yaml.vislex-$$.part"
trap 'rm -f "${env_part:-}" "${compose_part:-}"' EXIT HUP INT TERM

curl --fail --silent --show-error --location \
    --proto '=https' --tlsv1.2 \
    "$VISLEX_COMPOSE_URL" >"$compose_part"
[ -s "$compose_part" ] || fail "下载的 Compose YAML 为空"

compose_output="$(
    docker compose \
        --project-directory "$VISLEX_DIR" \
        --env-file "$env_path" \
        -f "$compose_part" \
        config
)" || fail "下载的 Compose YAML 校验失败"

expected_image="docker.io/shaundcn/vislex:${VISLEX_TAG}"
resolved_image="$(
    printf '%s\n' "$compose_output" |
        awk '$1 == "image:" { print $2; exit }'
)"
[ "$resolved_image" = "$expected_image" ] ||
    fail "Compose YAML 使用了非预期镜像：${resolved_image:-未知}"

chmod 0644 "$compose_part"
mv "$compose_part" "$compose_path"

compose() {
    docker compose \
        --project-directory "$VISLEX_DIR" \
        --env-file "$env_path" \
        -f "$compose_path" \
        "$@"
}

compose pull
compose up -d

health_url="http://${HOST_BIND_IP}:${HOST_PORT}/healthz"
attempt=0
while [ "$attempt" -lt 30 ]; do
    if curl --fail --silent --max-time 3 "$health_url" >/dev/null 2>&1; then
        printf 'Vislex 已安装并通过健康检查。\n'
        printf '访问地址：http://%s:%s/\n' "$HOST_BIND_IP" "$HOST_PORT"
        printf '安装目录：%s\n' "$VISLEX_DIR"
        exit 0
    fi
    attempt=$((attempt + 1))
    sleep 2
done

compose ps >&2 || true
fail "容器未在 60 秒内通过健康检查：$health_url"
