import logging
from typing import Literal

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError

from app.auth import router as auth_router
from app.db import check_database
from app.routes import router as research_router
from app.services import APIError, api_error_response

logger = logging.getLogger(__name__)

app = FastAPI(title="Mirai Agent Society", version="0.3.6")
app.add_exception_handler(APIError, api_error_response)
app.include_router(auth_router)
app.include_router(research_router)


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
