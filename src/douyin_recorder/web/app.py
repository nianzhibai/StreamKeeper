from __future__ import annotations

import asyncio
import secrets
import shutil
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response, status
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from ..errors import DouyinRecorderError
from ..settings import Settings
from .auth import (
    SESSION_COOKIE_NAME,
    LoginRateLimiter,
    SecurityHeadersMiddleware,
    SessionAuthMiddleware,
)
from .scheduler import ClientFactory, TaskScheduler
from .schemas import (
    AuthSession,
    InspectRequest,
    InspectResponse,
    LoginRequest,
    SystemInfo,
    TaskConfig,
    TaskCreate,
    TaskRecord,
    TaskUpdate,
)
from .store import TaskStore, WebSession, utc_now

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


def create_app(
    settings: Settings | None = None,
    *,
    store: TaskStore | None = None,
    scheduler: TaskScheduler | None = None,
    inspect_client_factory: ClientFactory | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    store = store or TaskStore(settings.database_path)
    scheduler = scheduler or TaskScheduler(store, settings)
    inspect_client_factory = inspect_client_factory or settings.create_client
    static_dir = Path(__file__).resolve().parent / "static"
    login_limiter = LoginRateLimiter(settings.login_max_attempts, settings.login_window_seconds)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        settings.prepare()
        await store.initialize()
        await scheduler.startup()
        try:
            yield
        finally:
            await scheduler.shutdown()

    app = FastAPI(
        title="DouYinStreamKeeper",
        version="0.4.0",
        docs_url="/api/docs",
        redoc_url=None,
        lifespan=lifespan,
    )
    app.add_middleware(
        SessionAuthMiddleware,
        store=store,
        enabled=bool(settings.web_password),
    )
    app.add_middleware(SecurityHeadersMiddleware)
    app.mount("/static", StaticFiles(directory=static_dir), name="static")
    app.state.settings = settings
    app.state.store = store
    app.state.scheduler = scheduler
    app.state.login_limiter = login_limiter

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
        retry_after = await login_limiter.retry_after(client_key)
        if retry_after is not None:
            raise HTTPException(
                status_code=429,
                detail="登录尝试过于频繁，请稍后重试",
                headers={"Retry-After": str(retry_after)},
            )

        username_matches = secrets.compare_digest(
            payload.username.encode("utf-8"),
            settings.web_username.encode("utf-8"),
        )
        password_matches = secrets.compare_digest(
            payload.password.get_secret_value().encode("utf-8"),
            settings.web_password.encode("utf-8"),
        )
        if not (username_matches and password_matches):
            await login_limiter.register_failure(client_key)
            raise HTTPException(status_code=401, detail="用户名或密码错误")

        await login_limiter.reset(client_key)
        ttl_seconds = settings.session_ttl_hours * 3600
        token, session = await store.create_session(settings.web_username, ttl_seconds)
        response.set_cookie(
            key=SESSION_COOKIE_NAME,
            value=token,
            max_age=ttl_seconds,
            expires=session.expires_at,
            path="/",
            secure=request.url.scheme == "https",
            httponly=True,
            samesite="strict",
        )
        return _auth_session_response(session)

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

    @app.get("/api/tasks", response_model=list[TaskRecord])
    async def list_tasks() -> list[TaskRecord]:
        return await store.list()

    @app.post("/api/tasks", response_model=TaskRecord, status_code=status.HTTP_201_CREATED)
    async def create_task(payload: TaskCreate) -> TaskRecord:
        config = TaskConfig.model_validate(payload.model_dump(exclude={"auto_start"}))
        record = await store.create(config)
        if payload.auto_start:
            await scheduler.start(record.id)
        created = await store.get(record.id)
        assert created is not None
        return created

    @app.get("/api/tasks/{task_id}", response_model=TaskRecord)
    async def get_task(task_id: str) -> TaskRecord:
        record = await store.get(task_id)
        if record is None:
            raise _not_found(task_id)
        return record

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
        return result

    @app.post("/api/tasks/{task_id}/start", response_model=TaskRecord)
    async def start_task(task_id: str) -> TaskRecord:
        record = await scheduler.start(task_id)
        if record is None:
            raise _not_found(task_id)
        result = await store.get(task_id)
        assert result is not None
        return result

    @app.post("/api/tasks/{task_id}/stop", response_model=TaskRecord)
    async def stop_task(task_id: str) -> TaskRecord:
        record = await scheduler.stop(task_id)
        if record is None:
            raise _not_found(task_id)
        return record

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
