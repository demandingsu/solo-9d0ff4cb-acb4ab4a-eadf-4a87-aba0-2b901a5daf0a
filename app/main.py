"""FastAPI application entrypoint."""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .db import init_db
from .errors import DomainError, StaleVersionError
from .routers import router


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Idempotent DDL: safe with multiple replicas starting concurrently.
    init_db()
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title="田间湿度质控平台",
        version="1.0.0",
        lifespan=lifespan,
        description=(
            "传感器湿度读数上传、勘误/作废/迟到处理，以及训练前的湿度突跳"
            "复核 API。支持乐观并发控制、快照一致性查询与历史回放。\n\n"
            "并发约定：写请求可携带 `expected_version` / `base_version`，"
            "基于过期快照的提交返回 409 且不改写任何既有结论。"
        ),
    )

    @app.exception_handler(DomainError)
    async def domain_error_handler(request: Request, exc: DomainError):  # type: ignore[no-untyped-def]
        body: dict = {"detail": str(exc)}
        if isinstance(exc, StaleVersionError):
            body["submitted_version"] = exc.submitted
            body["current_version"] = exc.current
        return JSONResponse(status_code=exc.status_code, content=body)

    app.include_router(router, prefix="/api/v1")

    @app.get("/health", tags=["meta"])
    def health():
        return {"status": "ok"}

    return app


app = create_app()
