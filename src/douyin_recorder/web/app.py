from __future__ import annotations

import asyncio
import secrets
import shutil
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response, status
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from .. import __version__
from ..cloud import CloudArchiveConfig, CloudUploadError
from ..errors import DouyinRecorderError
from ..settings import Settings
from .auth import (
    SESSION_COOKIE_NAME,
    SecurityHeadersMiddleware,
    SessionAuthMiddleware,
    set_session_cookie,
)
from .cloud_login import (
    CloudLoginError,
    CloudLoginFlowFactory,
    CloudLoginManager,
    CloudLoginSnapshot,
)
from .recordings import (
    RECORDING_MEDIA_TYPES,
    RecordingPreviewCache,
    get_recording_file,
    list_recording_directory,
)
from .scheduler import ClientFactory, TaskScheduler
from .schemas import (
    AuthSession,
    CloudArchiveUpdate,
    CloudArchiveView,
    CloudLoginView,
    CloudQuarkView,
    CloudScheduleView,
    CloudUploadExecutionView,
    CloudUploadSummaryView,
    CloudWoPanView,
    InspectRequest,
    InspectResponse,
    LoginRequest,
    RecordingDirectoryView,
    SystemInfo,
    TaskConfig,
    TaskCreate,
    TaskRecord,
    TaskUpdate,
)
from .store import TaskStore, WebSession, utc_now
from .uploader import RecordingUploadService

_TASK_RESTART_FIELDS = frozenset(
    {
        "url",
        "quality",
        "output_format",
        "source",
        "segment_seconds",
        "segment_count",
        "monitor",
        "interval_seconds",
    }
)


def _not_found(task_id: str) -> HTTPException:
    return HTTPException(status_code=404, detail=f"任务不存在: {task_id}")


def _auth_session_response(session: WebSession) -> AuthSession:
    return AuthSession(
        username=session.username,
        csrf_token=session.csrf_token,
        expires_at=session.expires_at,
    )


def _cloud_login_response(snapshot: CloudLoginSnapshot) -> CloudLoginView:
    return CloudLoginView(
        session_id=snapshot.session_id,
        provider=snapshot.provider,
        state=snapshot.state,
        message=snapshot.message,
        qr_image=snapshot.qr_image,
        expires_at=snapshot.expires_at,
    )


async def _cloud_archive_response(
    config: CloudArchiveConfig,
    upload_service: RecordingUploadService,
) -> CloudArchiveView:
    execution = upload_service.last_execution
    execution_view = None
    if execution is not None:
        summary_view = None
        if execution.summary is not None:
            summary_view = CloudUploadSummaryView(
                scanned_files=execution.summary.scanned_files,
                skipped_files=execution.summary.skipped_files,
                uploaded_copies=execution.summary.uploaded_copies,
                deleted_files=execution.summary.deleted_files,
                failed_files=execution.summary.failed_files,
            )
        execution_view = CloudUploadExecutionView(
            trigger=execution.trigger,
            status=execution.status,
            started_at=execution.started_at,
            finished_at=execution.finished_at,
            summary=summary_view,
            error=execution.error,
        )
    return CloudArchiveView(
        enabled=config.enabled,
        running=upload_service.running,
        quark=CloudQuarkView(
            enabled=config.quark_enabled,
            credential_configured=bool(config.quark_cookie),
            root_id=config.quark_root_id,
            upload_path=config.quark_upload_path,
        ),
        wopan=CloudWoPanView(
            enabled=config.wopan_enabled,
            access_token_configured=bool(config.wopan_access_token),
            refresh_token_configured=bool(config.wopan_refresh_token),
            root_id=config.wopan_root_id,
            family_id=config.wopan_family_id,
            upload_path=config.wopan_upload_path,
        ),
        schedule=CloudScheduleView(
            hour=config.upload_hour,
            min_age_minutes=config.upload_min_age_minutes,
            timeout_seconds=config.upload_timeout_seconds,
            next_run_at=await upload_service.next_run_at(),
        ),
        last_run=execution_view,
    )


def create_app(
    settings: Settings | None = None,
    *,
    store: TaskStore | None = None,
    scheduler: TaskScheduler | None = None,
    upload_service: RecordingUploadService | None = None,
    inspect_client_factory: ClientFactory | None = None,
    cloud_login_flow_factory: CloudLoginFlowFactory | None = None,
    cloud_login_poll_interval: float = 2,
) -> FastAPI:
    settings = settings or Settings.from_env()
    store = store or TaskStore(settings.database_path)
    scheduler = scheduler or TaskScheduler(store, settings)
    upload_service = upload_service or RecordingUploadService(
        settings,
        store,
        active_directories_provider=getattr(scheduler, "recording_output_directories", None),
    )
    inspect_client_factory = inspect_client_factory or settings.create_client
    static_dir = Path(__file__).resolve().parent / "static"
    cloud_config_lock = asyncio.Lock()
    recording_preview_cache = RecordingPreviewCache(settings.data_dir / "preview-cache", settings.ffmpeg)

    async def save_cloud_login_credentials(provider: str, credentials: dict[str, str]) -> None:
        async with cloud_config_lock:
            current = await upload_service.get_config()
            if provider == "quark":
                cookie = credentials.get("cookie", "")
                if not cookie:
                    raise CloudLoginError("夸克扫码登录没有返回 Cookie")
                config = replace(current, quark_cookie=cookie)
            elif provider == "wopan":
                access_token = credentials.get("access_token", "")
                refresh_token = credentials.get("refresh_token", "")
                if len(access_token.encode()) < 16:
                    raise CloudLoginError("联通云盘扫码登录没有返回有效 Token")
                config = replace(
                    current,
                    wopan_access_token=access_token,
                    wopan_refresh_token=refresh_token,
                )
            else:
                raise CloudLoginError(f"不支持的扫码登录类型: {provider}")
            config.validate()
            await store.save_cloud_upload_config(config.to_dict())
            await store.delete_cloud_credentials(provider)
            await upload_service.reconfigure(config)

    if cloud_login_flow_factory is None:
        cloud_login_manager = CloudLoginManager(
            save_cloud_login_credentials,
            poll_interval=cloud_login_poll_interval,
        )
    else:
        cloud_login_manager = CloudLoginManager(
            save_cloud_login_credentials,
            flow_factory=cloud_login_flow_factory,
            poll_interval=cloud_login_poll_interval,
        )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        settings.prepare()
        await store.initialize()
        await store.sync_web_credentials(settings.web_username, settings.web_password)
        await scheduler.startup()
        await upload_service.startup()
        try:
            yield
        finally:
            await cloud_login_manager.shutdown()
            await upload_service.shutdown()
            await scheduler.shutdown()

    app = FastAPI(
        title="DouYinStreamKeeper",
        version=__version__,
        docs_url="/api/docs",
        redoc_url=None,
        lifespan=lifespan,
    )
    app.add_middleware(
        SessionAuthMiddleware,
        store=store,
        enabled=bool(settings.web_password),
        session_ttl_seconds=settings.session_ttl_hours * 3600,
    )
    app.add_middleware(SecurityHeadersMiddleware)
    app.mount("/static", StaticFiles(directory=static_dir), name="static")
    app.state.settings = settings
    app.state.store = store
    app.state.scheduler = scheduler
    app.state.upload_service = upload_service
    app.state.cloud_login_manager = cloud_login_manager
    app.state.recording_preview_cache = recording_preview_cache

    @app.get("/health", include_in_schema=False)
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/login", include_in_schema=False, response_model=None)
    async def login_page(request: Request) -> Response:
        if not settings.web_password:
            return RedirectResponse("/", status_code=303)
        token = request.cookies.get(SESSION_COOKIE_NAME)
        if token and await store.get_session(token):
            return RedirectResponse("/", status_code=303)
        return FileResponse(static_dir / "login.html", headers={"Cache-Control": "no-store"})

    @app.post("/api/auth/login", response_model=AuthSession)
    async def login(payload: LoginRequest, request: Request, response: Response) -> AuthSession:
        if not settings.web_password:
            raise HTTPException(status_code=409, detail="当前开发环境未启用登录认证")

        client_key = request.client.host if request.client else "unknown"
        if await store.is_login_blacklisted(client_key):
            raise HTTPException(status_code=403, detail="当前 IP 已被永久禁止登录")

        username_matches = secrets.compare_digest(
            payload.username.encode("utf-8"),
            settings.web_username.encode("utf-8"),
        )
        password_matches = secrets.compare_digest(
            payload.password.get_secret_value().encode("utf-8"),
            settings.web_password.encode("utf-8"),
        )
        if not (username_matches and password_matches):
            blacklisted = await store.register_login_failure(
                client_key,
                settings.login_max_attempts,
                settings.login_window_seconds,
            )
            if blacklisted:
                raise HTTPException(status_code=403, detail="登录失败次数达到上限，当前 IP 已被永久禁止登录")
            raise HTTPException(status_code=401, detail="用户名或密码错误")

        if not await store.accept_login_success(client_key):
            raise HTTPException(status_code=403, detail="当前 IP 已被永久禁止登录")
        ttl_seconds = settings.session_ttl_hours * 3600
        token, session = await store.create_session(settings.web_username, ttl_seconds)
        set_session_cookie(
            response,
            token,
            session,
            ttl_seconds,
            secure=request.url.scheme == "https",
        )
        return _auth_session_response(session)

    @app.get("/api/auth/blocked-clients", response_model=list[str])
    async def blocked_clients() -> list[str]:
        return await store.list_login_blacklist()

    @app.delete("/api/auth/blocked-clients/{client_key:path}", status_code=status.HTTP_204_NO_CONTENT)
    async def unblock_client(client_key: str) -> Response:
        if not await store.unblock_login_client(client_key):
            raise HTTPException(status_code=404, detail=f"IP 不在登录黑名单中: {client_key}")
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.get("/api/auth/session", response_model=AuthSession)
    async def current_session(request: Request) -> AuthSession:
        if not settings.web_password:
            return AuthSession(
                username=settings.web_username,
                csrf_token="",
                expires_at=utc_now() + timedelta(hours=settings.session_ttl_hours),
            )
        return _auth_session_response(request.state.auth_session)

    @app.post("/api/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
    async def logout(request: Request, response: Response) -> Response:
        token = request.cookies.get(SESSION_COOKIE_NAME)
        if token:
            await store.delete_session(token)
        response.delete_cookie(
            SESSION_COOKIE_NAME,
            path="/",
            secure=request.url.scheme == "https",
            httponly=True,
            samesite="strict",
        )
        response.status_code = status.HTTP_204_NO_CONTENT
        return response

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(static_dir / "index.html", headers={"Cache-Control": "no-store"})

    @app.get("/tasks", include_in_schema=False)
    async def tasks_page() -> FileResponse:
        return FileResponse(static_dir / "tasks.html", headers={"Cache-Control": "no-store"})

    @app.get("/archive", include_in_schema=False)
    async def archive_page() -> FileResponse:
        return FileResponse(static_dir / "archive.html", headers={"Cache-Control": "no-store"})

    @app.get("/recordings", include_in_schema=False)
    async def recordings_page() -> FileResponse:
        return FileResponse(static_dir / "recordings.html", headers={"Cache-Control": "no-store"})

    @app.get("/settings", include_in_schema=False)
    async def settings_page() -> FileResponse:
        return FileResponse(static_dir / "settings.html", headers={"Cache-Control": "no-store"})

    @app.get("/api/cloud/archive", response_model=CloudArchiveView)
    async def get_cloud_archive() -> CloudArchiveView:
        config = await upload_service.get_config()
        return await _cloud_archive_response(config, upload_service)

    @app.post(
        "/api/cloud/login/{provider}",
        response_model=CloudLoginView,
        status_code=status.HTTP_201_CREATED,
    )
    async def start_cloud_login(provider: str) -> CloudLoginView:
        if provider not in {"quark", "wopan"}:
            raise HTTPException(status_code=404, detail=f"不支持的网盘类型: {provider}")
        try:
            snapshot = await cloud_login_manager.start(provider)
        except CloudLoginError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return _cloud_login_response(snapshot)

    @app.get(
        "/api/cloud/login/{provider}/{session_id}",
        response_model=CloudLoginView,
    )
    async def get_cloud_login(provider: str, session_id: str) -> CloudLoginView:
        snapshot = await cloud_login_manager.get(provider, session_id)
        if snapshot is None:
            raise HTTPException(status_code=404, detail="扫码登录会话不存在或已结束")
        return _cloud_login_response(snapshot)

    @app.delete(
        "/api/cloud/login/{provider}/{session_id}",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    async def cancel_cloud_login(provider: str, session_id: str) -> Response:
        if not await cloud_login_manager.cancel(provider, session_id):
            raise HTTPException(status_code=404, detail="扫码登录会话不存在或已结束")
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.put("/api/cloud/archive", response_model=CloudArchiveView)
    async def update_cloud_archive(payload: CloudArchiveUpdate) -> CloudArchiveView:
        async with cloud_config_lock:
            current = await upload_service.get_config()
            new_cookie = payload.quark.cookie.get_secret_value().strip() if payload.quark.cookie else ""
            new_access_token = (
                payload.wopan.access_token.get_secret_value().strip() if payload.wopan.access_token else ""
            )
            new_refresh_token = (
                payload.wopan.refresh_token.get_secret_value().strip() if payload.wopan.refresh_token else ""
            )
            if payload.quark.clear_cookie and new_cookie:
                raise HTTPException(status_code=422, detail="不能同时填写并清除夸克 Cookie")
            if payload.wopan.clear_tokens and (new_access_token or new_refresh_token):
                raise HTTPException(status_code=422, detail="不能同时填写并清除联通云盘 token")

            quark_cookie = "" if payload.quark.clear_cookie else (new_cookie or current.quark_cookie)
            if payload.wopan.clear_tokens:
                wopan_access_token = ""
                wopan_refresh_token = ""
            else:
                wopan_access_token = new_access_token or current.wopan_access_token
                wopan_refresh_token = new_refresh_token or current.wopan_refresh_token
            config = CloudArchiveConfig(
                quark_enabled=payload.quark.enabled,
                quark_cookie=quark_cookie,
                quark_root_id=payload.quark.root_id,
                quark_upload_path=payload.quark.upload_path,
                wopan_enabled=payload.wopan.enabled,
                wopan_access_token=wopan_access_token,
                wopan_refresh_token=wopan_refresh_token,
                wopan_root_id=payload.wopan.root_id,
                wopan_family_id=payload.wopan.family_id,
                wopan_upload_path=payload.wopan.upload_path,
                upload_hour=payload.schedule.hour,
                upload_min_age_minutes=payload.schedule.min_age_minutes,
                upload_timeout_seconds=payload.schedule.timeout_seconds,
            )
            try:
                config.validate()
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc

            await store.save_cloud_upload_config(config.to_dict())
            if payload.quark.clear_cookie or (new_cookie and new_cookie != current.quark_cookie):
                await store.delete_cloud_credentials("quark")
            if payload.wopan.clear_tokens or new_access_token or new_refresh_token:
                await store.delete_cloud_credentials("wopan")
            await upload_service.reconfigure(config)
        return await _cloud_archive_response(config, upload_service)

    @app.post("/api/cloud/archive/run", response_model=CloudArchiveView, status_code=status.HTTP_202_ACCEPTED)
    async def run_cloud_archive() -> CloudArchiveView:
        try:
            started = await upload_service.trigger("manual")
        except CloudUploadError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if not started:
            raise HTTPException(status_code=409, detail="网盘归档任务正在运行")
        config = await upload_service.get_config()
        return await _cloud_archive_response(config, upload_service)

    @app.get("/api/recordings", response_model=RecordingDirectoryView)
    async def list_recordings(path: str = "") -> RecordingDirectoryView:
        return await list_recording_directory(settings.recordings_dir, path)

    @app.get("/api/recordings/file/{recording_path:path}", response_model=None)
    async def recording_file(recording_path: str, download: bool = False) -> FileResponse:
        path, _ = get_recording_file(settings.recordings_dir, recording_path)
        return FileResponse(
            path,
            media_type=RECORDING_MEDIA_TYPES[path.suffix.lower()],
            filename=path.name,
            content_disposition_type="attachment" if download else "inline",
        )

    @app.get("/api/recordings/preview/{recording_path:path}", response_model=None)
    async def recording_preview(recording_path: str) -> FileResponse:
        path, normalized = get_recording_file(settings.recordings_dir, recording_path)
        preview_path = await recording_preview_cache.get(path, normalized)
        return FileResponse(
            preview_path,
            media_type="video/mp4",
            filename=f"{path.stem}.mp4",
            content_disposition_type="inline",
            headers={"X-Recording-Preview": "remux-cache"},
        )

    @app.get("/api/tasks", response_model=list[TaskRecord])
    async def list_tasks() -> list[TaskRecord]:
        return scheduler.enrich_records(await store.list())

    @app.post("/api/tasks", response_model=TaskRecord, status_code=status.HTTP_201_CREATED)
    async def create_task(payload: TaskCreate) -> TaskRecord:
        config = TaskConfig.model_validate(payload.model_dump(exclude={"auto_start"}))
        record = await store.create(config)
        if payload.auto_start:
            await scheduler.start(record.id)
        created = await store.get(record.id)
        assert created is not None
        return scheduler.enrich_record(created)

    @app.get("/api/tasks/{task_id}", response_model=TaskRecord)
    async def get_task(task_id: str) -> TaskRecord:
        record = await store.get(task_id)
        if record is None:
            raise _not_found(task_id)
        return scheduler.enrich_record(record)

    @app.patch("/api/tasks/{task_id}", response_model=TaskRecord)
    async def update_task(task_id: str, payload: TaskUpdate) -> TaskRecord:
        current = await store.get(task_id)
        if current is None:
            raise _not_found(task_id)
        changes = payload.model_dump(exclude_unset=True)
        invalid_nulls = [key for key, value in changes.items() if key != "label" and value is None]
        if invalid_nulls:
            raise HTTPException(status_code=422, detail=f"字段不能为 null: {', '.join(invalid_nulls)}")
        current_config = {name: getattr(current, name) for name in TaskConfig.model_fields}
        try:
            validated_config = TaskConfig.model_validate(current_config | changes)
        except ValidationError as exc:
            detail = "；".join(error["msg"] for error in exc.errors())
            raise HTTPException(status_code=422, detail=detail) from exc
        changes = {name: getattr(validated_config, name) for name in changes}
        effective_changes = {key: value for key, value in changes.items() if getattr(current, key) != value}
        if not effective_changes:
            return current
        updated = await store.update_config(task_id, effective_changes)
        if current.enabled and _TASK_RESTART_FIELDS.intersection(effective_changes):
            await scheduler.restart(task_id)
        if updated is None:
            raise _not_found(task_id)
        result = await store.get(task_id)
        assert result is not None
        return scheduler.enrich_record(result)

    @app.post("/api/tasks/{task_id}/start", response_model=TaskRecord)
    async def start_task(task_id: str) -> TaskRecord:
        record = await scheduler.start(task_id)
        if record is None:
            raise _not_found(task_id)
        result = await store.get(task_id)
        assert result is not None
        return scheduler.enrich_record(result)

    @app.post("/api/tasks/{task_id}/stop", response_model=TaskRecord)
    async def stop_task(task_id: str) -> TaskRecord:
        record = await scheduler.stop(task_id)
        if record is None:
            raise _not_found(task_id)
        return scheduler.enrich_record(record)

    @app.delete("/api/tasks/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_task(task_id: str) -> Response:
        if await store.get(task_id) is None:
            raise _not_found(task_id)
        await scheduler.stop(task_id)
        await store.delete(task_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.post("/api/inspect", response_model=InspectResponse)
    async def inspect_room(payload: InspectRequest) -> InspectResponse:
        client = inspect_client_factory()
        try:
            info = await asyncio.wait_for(
                client.fetch(payload.url, payload.quality),
                timeout=settings.fetch_timeout_seconds,
            )
        except (DouyinRecorderError, TimeoutError) as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return InspectResponse(
            anchor_name=info.anchor_name,
            is_live=info.is_live,
            title=info.title,
            quality=info.quality,
            live_url=info.live_url,
            has_flv=bool(info.flv_url),
            has_hls=bool(info.m3u8_url),
            stream_orientation=info.stream_orientation,
        )

    @app.get("/api/system", response_model=SystemInfo)
    async def system_info() -> SystemInfo:
        usage = shutil.disk_usage(settings.recordings_dir)
        return SystemInfo(
            ffmpeg_available=bool(shutil.which(settings.ffmpeg)),
            node_available=bool(shutil.which("node")),
            recordings_dir=str(settings.recordings_dir),
            free_space_gb=round(usage.free / (1024**3), 2),
            active_tasks=scheduler.active_task_count,
            recording_tasks=scheduler.recording_task_count,
            max_concurrent_recordings=settings.max_concurrent_recordings,
        )

    return app
