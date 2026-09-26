"""服务端业务模块。"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import queue_services
from .core.workqueue import QueueError
from .routers import router

app = FastAPI(
    title="Practice Hours Guard",
    version="0.1.0",
    description=(
        "Event-sourced practice-hours compliance service. Check-ins, mentor "
        "confirmations and leave corrections are append-only; compliance is "
        "derived by replay and can be frozen into an immutable snapshot."
    ),
)

app.include_router(router)


@app.exception_handler(queue_services.ItemNotFoundError)
@app.exception_handler(queue_services.RuleNotFoundError)
@app.exception_handler(queue_services.QueueEmptyError)
async def queue_not_found_handler(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.exception_handler(queue_services.NoActiveRuleError)
@app.exception_handler(queue_services.ItemExistsError)
@app.exception_handler(queue_services.RuleConflictError)
@app.exception_handler(queue_services.LeaseConflictError)
async def queue_conflict_handler(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": str(exc)})


@app.exception_handler(QueueError)
async def queue_domain_handler(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(status_code=422, content={"detail": str(exc)})


@app.get("/health", tags=["meta"])
def health() -> dict[str, str]:
    return {"status": "ok"}
