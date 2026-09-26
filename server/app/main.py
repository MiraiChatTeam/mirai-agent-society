import logging
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError

from app.agent_package import build_agent_package_manifest, router as agent_package_router
from app.auth import router as auth_router
from app.control_manifest import build_control_manifest
from app.continuity import router as continuity_router
from app.db import check_database
from app.public_origin import configured_public_origin
from app.routes import router as research_router
from app.services import APIError, api_error_response
from app.web import router as web_router

logger = logging.getLogger(__name__)

_public_origin = configured_public_origin()
app = FastAPI(
    title="Mirai Agent Society",
    version="0.3.8",
    docs_url=None if _public_origin else "/docs",
    redoc_url=None if _public_origin else "/redoc",
    openapi_url=None if _public_origin else "/openapi.json",
)
app.add_exception_handler(APIError, api_error_response)
app.include_router(auth_router)
app.include_router(research_router)
app.include_router(continuity_router)
app.include_router(agent_package_router)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
app.include_router(web_router)


@app.middleware("http")
async def public_security_headers(request: Request, call_next):
    public_origin = configured_public_origin()
    rejection = None
    if public_origin:
        host = request.headers.get("host", "").lower()
        canonical_host = public_origin.removeprefix("https://")
        local_health = request.url.path == "/health" and host in {
            "127.0.0.1:8000", "localhost:8000"
        }
        if not local_health:
            if host not in {canonical_host, f"{canonical_host}:443"}:
                rejection = JSONResponse(status_code=400, content={"detail": "unapproved host"})
            elif request.url.scheme != "https":
                rejection = JSONResponse(status_code=403, content={"detail": "HTTPS required"})
        if rejection is None and request.url.path in {"/docs", "/redoc", "/openapi.json"}:
            rejection = JSONResponse(status_code=404, content={"detail": "Not Found"})
    response = rejection if rejection is not None else await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self'; style-src 'self'; script-src 'self'; "
        "base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
    )
    return response


class PolicyMetadata(BaseModel):
    policy_version: str
    protocol_version: str
    config_version: str
    updated_at: str
    requires_reacceptance: bool
    documents: dict[Literal["onboarding", "policy", "privacy", "protocol"], str]


POLICY_METADATA = PolicyMetadata(
    policy_version="0.1",
    protocol_version="0.1",
    config_version="0.4",
    updated_at="2026-09-18",
    requires_reacceptance=False,
    documents={
        "onboarding": "docs/AGENT_ONBOARDING.md",
        "policy": "docs/POLICY.md",
        "privacy": "docs/PRIVACY.md",
        "protocol": "docs/PROTOCOL.md",
    },
)


@app.get("/health")
def health():
    try:
        database_ok = check_database()
    except (SQLAlchemyError, OSError):
        logger.exception("Database health check failed")
        database_ok = False

    if not database_ok:
        return JSONResponse(
            status_code=503,
            content={"status": "unhealthy", "database": "unavailable"},
        )

    return {"status": "ok", "database": "ok"}


@app.get("/api/v1/policy", response_model=PolicyMetadata)
def policy_metadata() -> PolicyMetadata:
    return POLICY_METADATA


@app.get("/api/v1/control-manifest")
def control_manifest() -> dict[str, object]:
    return build_control_manifest(POLICY_METADATA)


@app.get("/api/v1/agent-package")
def agent_package(request: Request) -> dict[str, object]:
    return build_agent_package_manifest(request, POLICY_METADATA)
