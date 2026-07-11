from __future__ import annotations

import asyncio
import shutil
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Response, status
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from ..errors import DouyinRecorderError
from ..settings import Settings
from .auth import BasicAuthMiddleware, SecurityHeadersMiddleware
from .scheduler import ClientFactory, TaskScheduler
from .schemas import (
    InspectRequest,
    InspectResponse,
    SystemInfo,
    TaskConfig,
    TaskCreate,
    TaskRecord,
    TaskUpdate,
)
from .store import TaskStore


def _not_found(task_id: str) -> HTTPException:
    return HTTPException(status_code=404, detail=f"任务不存在: {task_id}")


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
        title="Douyin Live Recorder",
        version="0.2.0",
        docs_url="/api/docs",
        redoc_url=None,
        lifespan=lifespan,
    )
    app.add_middleware(
        BasicAuthMiddleware,
        username=settings.web_username,
        password=settings.web_password,
    )
    app.add_middleware(SecurityHeadersMiddleware)
    app.mount("/static", StaticFiles(directory=static_dir), name="static")
    app.state.settings = settings
    app.state.store = store
    app.state.scheduler = scheduler

    @app.get("/health", include_in_schema=False)
    async def health() -> dict[str, str]:
        return {"status": "ok"}

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
        updated = await store.update_config(task_id, changes)
        if current.enabled:
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
