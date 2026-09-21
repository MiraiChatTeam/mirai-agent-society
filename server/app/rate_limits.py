import hashlib
import ipaddress
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from fastapi import Request
from sqlalchemy import case
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models import RateLimitBucket
from app.services import APIError


@dataclass(frozen=True)
class RateLimitRule:
    count_env: str
    window_env: str
    default_count: int
    default_window_seconds: int


RULES = {
    "registration": RateLimitRule(
        "RATE_LIMIT_REGISTRATION", "RATE_WINDOW_REGISTRATION", 10, 3600
    ),
    "auth_challenge": RateLimitRule(
        "RATE_LIMIT_AUTH_CHALLENGE", "RATE_WINDOW_AUTH_CHALLENGE", 30, 60
    ),
    "auth_verify": RateLimitRule(
        "RATE_LIMIT_AUTH_VERIFY", "RATE_WINDOW_AUTH_VERIFY", 30, 60
    ),
    "runtime_snapshot": RateLimitRule(
        "RATE_LIMIT_RUNTIME_SNAPSHOT", "RATE_WINDOW_RUNTIME_SNAPSHOT", 30, 3600
    ),
    "thread": RateLimitRule("RATE_LIMIT_THREAD", "RATE_WINDOW_THREAD", 20, 3600),
    "post": RateLimitRule("RATE_LIMIT_POST", "RATE_WINDOW_POST", 120, 3600),
}


def _positive_int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if not 1 <= value <= 86_400:
        raise RuntimeError(f"{name} must be between 1 and 86400")
    return value


def rule_values(scope: str) -> tuple[int, int]:
    rule = RULES[scope]
    return (
        _positive_int(rule.count_env, rule.default_count),
        _positive_int(rule.window_env, rule.default_window_seconds),
    )


def _normalized_ip(value: str) -> str | None:
    try:
        return str(ipaddress.ip_address(value.strip()))
    except ValueError:
        return None


def request_source(request: Request) -> str:
    peer = request.client.host if request.client is not None else "unknown"
    normalized_peer = _normalized_ip(peer) or peer
    trusted = {
        normalized
        for item in os.getenv("TRUSTED_PROXY_IPS", "").split(",")
        if (normalized := _normalized_ip(item)) is not None
    }
    if normalized_peer in trusted:
        forwarded = request.headers.get("x-forwarded-for", "").split(",", 1)[0]
        normalized_forwarded = _normalized_ip(forwarded)
        if normalized_forwarded is not None:
            return normalized_forwarded
    return normalized_peer


def identity_hash(kind: str, identity: str) -> str:
    return hashlib.sha256(f"{kind}\0{identity}".encode("utf-8")).hexdigest()


def enforce_rate_limit(
    db: Session,
    scope: str,
    identity: str,
    *,
    identity_kind: str,
    now: datetime | None = None,
) -> None:
    limit, window_seconds = rule_values(scope)
    current = now or datetime.now(UTC)
    cutoff = current - timedelta(seconds=window_seconds)
    hashed_identity = identity_hash(identity_kind, identity)
    rate_limit_key = hashlib.sha256(
        f"{scope}\0{hashed_identity}".encode("ascii")
    ).hexdigest()
    statement = insert(RateLimitBucket).values(
        rate_limit_key=rate_limit_key,
        scope=scope,
        identity_hash=hashed_identity,
        window_started_at=current,
        request_count=1,
        updated_at=current,
    )
    expired = RateLimitBucket.window_started_at <= cutoff
    statement = statement.on_conflict_do_update(
        index_elements=[RateLimitBucket.rate_limit_key],
        set_={
            "window_started_at": case(
                (expired, current), else_=RateLimitBucket.window_started_at
            ),
            "request_count": case(
                (expired, 1), else_=RateLimitBucket.request_count + 1
            ),
            "updated_at": current,
        },
    ).returning(
        RateLimitBucket.request_count,
        RateLimitBucket.window_started_at,
    )
    request_count, window_started_at = db.execute(statement).one()
    db.commit()
    if request_count > limit:
        retry_after = max(
            1,
            int(
                (window_started_at + timedelta(seconds=window_seconds) - current)
                .total_seconds()
            )
            + 1,
        )
        raise APIError(
            429,
            "rate_limited",
            retry_after_seconds=retry_after,
            headers={"Retry-After": str(retry_after)},
        )
