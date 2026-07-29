from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import mimetypes
import os
import secrets
import stat
import tempfile
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlencode

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .ark import (
    ArkClient,
    ArkError,
    create_smoke_test_video,
    masked_api_key,
    read_api_key,
    save_api_key,
    validate_model_id,
)
from .config import CHINA_TIMEZONE, AppConfig, validate_fps
from .db import Database, utc_now
from .defaults import (
    ACTIVE_STATUSES,
    API_TEST_PROMPT,
    DEFAULT_PROMPT,
    STATUS_LABELS,
    TASK_STATUSES,
)
from .media import MediaError, read_regular_text, safe_regular_output
from .pipeline import request_valid_analysis, retry_failed_task
from .runtime import ApplicationRuntime


BASE_DIR = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(BASE_DIR / "templates"))
CSRF_COOKIE = "vislex_csrf"
MAX_FORM_BYTES = 64 * 1024
TASKS_PER_PAGE = 200


def create_app(config: AppConfig | None = None) -> FastAPI:
    app_config = config or AppConfig.from_environment()
    app_config.ensure_directories()
    database = Database(app_config.database_path)
    database.initialize()
    runtime = ApplicationRuntime(app_config, database)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await runtime.start()
        try:
            yield
        finally:
            await runtime.stop()

    application = FastAPI(
        title="vislex",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    application.state.config = app_config
    application.state.database = database
    application.state.runtime = runtime
    application.add_middleware(
        TrustedHostMiddleware, allowed_hosts=list(app_config.trusted_hosts)
    )
    application.mount(
        "/static",
        StaticFiles(directory=str(BASE_DIR / "static"), check_dir=True),
        name="static",
    )

    @application.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'none'; style-src 'self'; "
            "img-src 'self' data:; media-src 'self'; object-src 'none'; "
            "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=(), payment=()"
        )
        if not request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @application.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request, status: str = "", page: int = 1):
        selected_status = status if status in TASK_STATUSES else ""
        task_total = database.task_count(selected_status or None)
        page_total = max(1, (task_total + TASKS_PER_PAGE - 1) // TASKS_PER_PAGE)
        current_page = min(max(page, 1), page_total)
        rows = database.list_tasks(
            selected_status or None,
            limit=TASKS_PER_PAGE,
            offset=(current_page - 1) * TASKS_PER_PAGE,
        )
        tasks: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            local_time = _display_datetime(str(row["updated_at"]))
            item["updated_date"] = local_time[:10]
            item["updated_clock"] = local_time[11:19]
            tasks.append(item)
        counts = database.status_counts()
        cards = {
            "success": counts["success"],
            "failed": counts["failed"],
            "queued": counts["queued"],
            "processing": sum(counts[item] for item in ACTIVE_STATUSES),
            "ignored": counts["ignored"],
        }
        return _template_response(
            request,
            database,
            "dashboard.html",
            {
                "tasks": tasks,
                "cards": cards,
                "status_labels": STATUS_LABELS,
                "status_options": TASK_STATUSES,
                "selected_status": selected_status,
                "current_page": current_page,
                "page_total": page_total,
                "task_total": task_total,
                "previous_page_url": (
                    _dashboard_path(selected_status, current_page - 1)
                    if current_page > 1
                    else ""
                ),
                "next_page_url": (
                    _dashboard_path(selected_status, current_page + 1)
                    if current_page < page_total
                    else ""
                ),
                "notice": _message(request, "notice"),
                "error": _message(request, "error"),
            },
        )

    @application.post("/actions/scan")
    async def scan_now(request: Request):
        form = await _verified_form(request, database)
        del form
        runtime.request_scan()
        return _redirect("/", notice="已请求立即扫描")

    @application.post("/tasks/{task_id}/retry")
    async def retry_task(request: Request, task_id: int):
        form = await _verified_form(request, database)
        del form
        ok, message = retry_failed_task(app_config, database, task_id)
        if ok:
            runtime.request_scan()
            return _redirect("/", notice=message)
        return _redirect("/", error=message)

    @application.get("/settings", response_class=HTMLResponse)
    async def settings_page(request: Request):
        try:
            masked_key = masked_api_key(app_config)
        except ArkError:
            masked_key = ""
        settings = database.get_settings(
            (
                "prompt",
                "model_id",
                "video_fps",
                "models_updated_at",
            )
        )
        return _template_response(
            request,
            database,
            "settings.html",
            {
                "api_key_masked": masked_key,
                "models": database.cached_models(),
                "selected_model": settings.get("model_id", ""),
                "video_fps": settings.get("video_fps", "0.3"),
                "models_updated_at": settings.get("models_updated_at", ""),
                "prompt": settings.get("prompt", DEFAULT_PROMPT),
                "notice": _message(request, "notice"),
                "error": _message(request, "error"),
            },
        )

    @application.post("/settings/api-key/save")
    async def save_key(request: Request):
        form = await _verified_form(request, database)
        try:
            save_api_key(app_config, _one(form, "api_key"))
        except (ArkError, OSError) as exc:
            return _redirect("/settings", error=_public_error(exc))
        return _redirect("/settings", notice="API Key 已安全保存，未调用接口")

    @application.post("/settings/api-key/test")
    async def test_key(request: Request):
        form = await _verified_form(request, database)
        entered_key = _one(form, "api_key").strip()
        try:
            key = entered_key or read_api_key(app_config) or ""
            if not key:
                raise ArkError("请输入 API Key，或先保存 API Key")
            model = _validate_selected_model(_one(form, "model_id"))
            fps = validate_fps(_one(form, "video_fps"))
            await _run_api_test(app_config, key, model, fps)
        except (ArkError, OSError, ValueError) as exc:
            message = _public_error(exc, entered_key)
            return _redirect("/settings", error=f"测试失败：{message}")
        return _redirect(
            "/settings",
            notice="测试成功：已完成视频生成、上传、预处理和 Responses 调用；未保存测试值",
        )

    @application.post("/settings/models/fetch")
    async def fetch_models(request: Request):
        form = await _verified_form(request, database)
        del form
        try:
            key = read_api_key(app_config) or ""
            if not key:
                raise ArkError("请先保存 API Key")
            async with ArkClient(app_config, key) as client:
                models = await client.list_models()
            database.set_settings(
                {
                    "models_json": json.dumps(models, ensure_ascii=False),
                    "models_updated_at": utc_now(),
                }
            )
        except (ArkError, OSError) as exc:
            return _redirect("/settings", error=_public_error(exc))
        return _redirect("/settings", notice=f"已缓存 {len(models)} 个模型")

    @application.post("/settings/model/save")
    async def save_model_settings(request: Request):
        form = await _verified_form(request, database)
        try:
            model = _validate_selected_model(_one(form, "model_id"))
            models = database.cached_models()
            if model not in models:
                raise ValueError("模型不在已缓存列表中，请先获取模型")
            fps = validate_fps(_one(form, "video_fps"))
            database.set_settings(
                {"model_id": model, "video_fps": f"{fps:g}"}
            )
        except ValueError as exc:
            return _redirect("/settings", error=_public_error(exc))
        return _redirect("/settings", notice="模型和抽帧频率已保存")

    @application.post("/settings/prompt/save")
    async def save_prompt(request: Request):
        form = await _verified_form(request, database)
        prompt = _one(form, "prompt").replace("\r\n", "\n").replace("\r", "\n")
        if not prompt.strip():
            return _redirect("/settings", error="提示词不能为空")
        if len(prompt) > 40_000:
            return _redirect("/settings", error="提示词不能超过40000个字符")
        database.set_settings({"prompt": prompt.strip()})
        return _redirect("/settings", notice="提示词已保存")

    @application.post("/settings/prompt/reset")
    async def reset_prompt(request: Request):
        form = await _verified_form(request, database)
        del form
        database.set_settings({"prompt": DEFAULT_PROMPT})
        return _redirect("/settings", notice="已恢复默认提示词")

    def successful_task(task_id: int):
        task = database.get_task(task_id)
        if task is None or task["status"] != "success":
            raise HTTPException(status_code=404, detail="文件不存在")
        return task

    def task_output(task: Any, field: str) -> Path:
        path = safe_regular_output(app_config.output_dir, task[field])
        if path is None:
            raise HTTPException(status_code=404, detail="文件不存在")
        return path

    @application.get("/tasks/{task_id}/preview", response_class=HTMLResponse)
    @application.get("/tasks/{task_id}/markdown", response_class=HTMLResponse)
    async def task_preview(request: Request, task_id: int):
        task = successful_task(task_id)
        video_path = task_output(task, "video_output_path")
        markdown_path = task_output(task, "md_output_path")
        try:
            text = read_regular_text(markdown_path, 5 * 1024 * 1024)
        except (OSError, UnicodeError, MediaError) as exc:
            raise HTTPException(status_code=404, detail="无法读取文件") from exc
        return _template_response(
            request,
            database,
            "markdown.html",
            {
                "task_id": task_id,
                "markdown_text": text,
                "video_filename": video_path.name,
                "markdown_filename": markdown_path.name,
                "video_content_type": (
                    mimetypes.guess_type(video_path.name)[0] or "video/mp4"
                ),
            },
        )

    @application.api_route("/tasks/{task_id}/video", methods=["GET", "HEAD"])
    async def video_file(request: Request, task_id: int):
        task = successful_task(task_id)
        path = task_output(task, "video_output_path")
        return await _serve_video(request, path)

    @application.api_route(
        "/tasks/{task_id}/video/download", methods=["GET", "HEAD"]
    )
    async def download_video(request: Request, task_id: int):
        task = successful_task(task_id)
        path = task_output(task, "video_output_path")
        return await _serve_video(request, path, download=True)

    @application.get("/tasks/{task_id}/markdown/download")
    async def download_markdown(task_id: int):
        task = successful_task(task_id)
        path = task_output(task, "md_output_path")
        try:
            text = read_regular_text(path, 5 * 1024 * 1024)
        except (OSError, UnicodeError, MediaError) as exc:
            raise HTTPException(status_code=404, detail="无法读取文件") from exc
        return Response(
            content=text.encode("utf-8"),
            media_type="text/markdown; charset=utf-8",
            headers={
                "Content-Disposition": (
                    f"attachment; filename*=UTF-8''{quote(path.name)}"
                )
            },
        )

    @application.get("/healthz")
    async def healthz():
        healthy = database.health_check() and runtime.is_healthy()
        return JSONResponse(
            {"status": "ok" if healthy else "unhealthy"},
            status_code=200 if healthy else 503,
        )

    return application


async def _run_api_test(
    config: AppConfig, api_key: str, model: str, fps: float
) -> None:
    with tempfile.TemporaryDirectory(prefix="vislex-test-") as temporary:
        video = Path(temporary) / "vislex-api-test.mp4"
        await create_smoke_test_video(video)
        client = ArkClient(config, api_key)
        file_id = ""
        try:
            upload = await client.upload_file(video, model, fps)
            file_id = await client.wait_until_file_ready(upload)
            await request_valid_analysis(
                client, file_id, API_TEST_PROMPT, model
            )
        finally:
            cleanup_id = file_id or client.last_uploaded_file_id or ""
            if cleanup_id:
                await client.delete_file(cleanup_id)
            await client.close()


def _template_response(
    request: Request,
    database: Database,
    template_name: str,
    context: dict[str, Any],
) -> HTMLResponse:
    token = request.cookies.get(CSRF_COOKIE, "")
    if not _valid_csrf(token, database):
        token = _new_csrf(database)
    payload = {"request": request, "csrf_token": token, **context}
    response = TEMPLATES.TemplateResponse(
        request=request, name=template_name, context=payload
    )
    response.set_cookie(
        CSRF_COOKIE,
        token,
        max_age=30 * 24 * 60 * 60,
        httponly=True,
        samesite="strict",
        secure=False,
        path="/",
    )
    return response


async def _verified_form(
    request: Request, database: Database
) -> dict[str, list[str]]:
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip()
    if content_type != "application/x-www-form-urlencoded":
        raise HTTPException(status_code=415, detail="只接受表单请求")
    declared_length = request.headers.get("content-length")
    if declared_length:
        try:
            parsed_length = int(declared_length)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Content-Length 格式错误") from exc
        if parsed_length < 0:
            raise HTTPException(status_code=400, detail="Content-Length 格式错误")
        if parsed_length > MAX_FORM_BYTES:
            raise HTTPException(status_code=413, detail="表单过大")
    body_buffer = bytearray()
    async for chunk in request.stream():
        if len(body_buffer) + len(chunk) > MAX_FORM_BYTES:
            raise HTTPException(status_code=413, detail="表单过大")
        body_buffer.extend(chunk)
    body = bytes(body_buffer)
    try:
        decoded = body.decode("utf-8")
        form = parse_qs(
            decoded,
            keep_blank_values=True,
            strict_parsing=False,
            max_num_fields=20,
        )
    except (UnicodeDecodeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="表单格式错误") from exc
    candidate = _one(form, "csrf_token")
    cookie = request.cookies.get(CSRF_COOKIE, "")
    if (
        not candidate
        or not cookie
        or not hmac.compare_digest(candidate, cookie)
        or not _valid_csrf(candidate, database)
    ):
        raise HTTPException(status_code=403, detail="CSRF 校验失败")
    return form


def _new_csrf(database: Database) -> str:
    nonce = secrets.token_urlsafe(24)
    secret = database.get_setting("csrf_secret")
    signature = hmac.new(
        secret.encode("utf-8"), nonce.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return f"{nonce}.{signature}"


def _valid_csrf(token: str, database: Database) -> bool:
    if not token or len(token) > 256 or "." not in token:
        return False
    nonce, signature = token.rsplit(".", 1)
    if not nonce or len(signature) != 64:
        return False
    expected = hmac.new(
        database.get_setting("csrf_secret").encode("utf-8"),
        nonce.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(signature, expected)


def _one(form: dict[str, list[str]], key: str) -> str:
    values = form.get(key) or [""]
    return values[-1]


def _redirect(path: str, *, notice: str = "", error: str = "") -> RedirectResponse:
    if notice:
        separator = "&" if "?" in path else "?"
        path = f"{path}{separator}notice={quote(notice[:800])}"
    elif error:
        separator = "&" if "?" in path else "?"
        path = f"{path}{separator}error={quote(error[:800])}"
    return RedirectResponse(path, status_code=303)


def _message(request: Request, name: str) -> str:
    return request.query_params.get(name, "")[:800]


def _public_error(error: BaseException, secret: str = "") -> str:
    message = str(error).replace("\r", " ").replace("\n", " ").strip()
    if secret:
        message = message.replace(secret, "***")
    return message[:800] or type(error).__name__


def _validate_selected_model(value: str) -> str:
    try:
        return validate_model_id(value)
    except ArkError as exc:
        message = "请选择模型" if not value.strip() else str(exc)
        raise ValueError(message) from exc


def _display_datetime(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.astimezone(CHINA_TIMEZONE).isoformat(
            sep=" ", timespec="seconds"
        )
    except ValueError:
        return value


def _dashboard_path(status: str, page: int) -> str:
    parameters: dict[str, str | int] = {"page": max(page, 1)}
    if status in TASK_STATUSES:
        parameters["status"] = status
    return f"/?{urlencode(parameters)}"


async def _serve_video(
    request: Request, path: Path, *, download: bool = False
) -> Response:
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise HTTPException(status_code=404, detail="文件不存在")
    size = metadata.st_size
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Type": content_type,
        "Content-Disposition": (
            f"{'attachment' if download else 'inline'}; "
            f"filename*=UTF-8''{quote(path.name)}"
        ),
    }
    range_header = request.headers.get("range")
    start, end, partial = _parse_range(range_header, size)
    if partial:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    length = max(0, end - start + 1)
    headers["Content-Length"] = str(length)
    status_code = 206 if partial else 200
    if request.method == "HEAD":
        return Response(status_code=status_code, headers=headers)
    return StreamingResponse(
        _video_chunks(path, start, length),
        status_code=status_code,
        headers=headers,
        media_type=content_type,
    )


def _parse_range(value: str | None, size: int) -> tuple[int, int, bool]:
    if not value:
        return 0, size - 1, False
    if not value.startswith("bytes=") or "," in value:
        raise HTTPException(
            status_code=416, headers={"Content-Range": f"bytes */{size}"}
        )
    specification = value[6:].strip()
    if "-" not in specification:
        raise HTTPException(
            status_code=416, headers={"Content-Range": f"bytes */{size}"}
        )
    left, right = specification.split("-", 1)
    try:
        if not left:
            suffix = int(right)
            if suffix <= 0:
                raise ValueError
            start = max(0, size - suffix)
            end = size - 1
        else:
            start = int(left)
            end = int(right) if right else size - 1
    except ValueError as exc:
        raise HTTPException(
            status_code=416, headers={"Content-Range": f"bytes */{size}"}
        ) from exc
    if size <= 0 or start < 0 or start >= size or end < start:
        raise HTTPException(
            status_code=416, headers={"Content-Range": f"bytes */{size}"}
        )
    return start, min(end, size - 1), True


async def _video_chunks(path: Path, start: int, length: int):
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        os.lseek(descriptor, start, os.SEEK_SET)
        remaining = length
        while remaining > 0:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk
            await asyncio.sleep(0)
    finally:
        os.close(descriptor)
