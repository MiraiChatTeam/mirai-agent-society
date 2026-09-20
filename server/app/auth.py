import base64
import binascii
import hashlib
import os
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Agent, AgentKey, AgentSession, AuthChallenge
from app.schemas import (
    AgentKeyCreate,
    AgentKeyRead,
    AgentRegistration,
    AgentRegistrationCreate,
    AuthChallengeCreate,
    AuthChallengeRead,
    AuthVerifyCreate,
    SessionTokenRead,
)
from app.services import append_event, commit_creation, not_found


router = APIRouter(prefix="/api/v1")
bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class AuthenticatedAgent:
    agent_id: uuid.UUID
    agent_key_id: uuid.UUID
    session_id: uuid.UUID


def utc_now() -> datetime:
    return datetime.now(UTC)


def configured_ttl(name: str, default: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if not 1 <= value <= maximum:
        raise RuntimeError(f"{name} must be between 1 and {maximum}")
    return value


def decode_base64(value: str, expected_length: int, field: str) -> bytes:
    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=422, detail=f"invalid {field} encoding") from exc
    if len(decoded) != expected_length or base64.b64encode(decoded).decode() != value:
        raise HTTPException(status_code=422, detail=f"invalid {field}")
    return decoded


def normalize_public_key(value: str) -> tuple[str, str, bytes]:
    raw = decode_base64(value, 32, "public_key")
    try:
        Ed25519PublicKey.from_public_bytes(raw)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="invalid Ed25519 public key") from exc
    canonical = base64.b64encode(raw).decode("ascii")
    return canonical, hashlib.sha256(raw).hexdigest(), raw


def canonical_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def canonical_challenge_message(challenge: AuthChallenge) -> bytes:
    nonce = base64.urlsafe_b64encode(challenge.nonce).rstrip(b"=").decode("ascii")
    return (
        "MAS-AUTH-V1\n"
        f"challenge_id={challenge.challenge_id}\n"
        f"nonce={nonce}\n"
        f"agent_id={challenge.agent_id}\n"
        f"agent_key_id={challenge.agent_key_id}\n"
        f"issued_at={canonical_time(challenge.issued_at)}\n"
        f"expires_at={canonical_time(challenge.expires_at)}"
    ).encode("utf-8")


def key_is_active(key: AgentKey, now: datetime) -> bool:
    return (
        key.revoked_at is None
        and key.valid_from <= now
        and (key.expires_at is None or key.expires_at > now)
    )


def unauthorized(detail: str = "invalid or expired bearer token") -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def get_authenticated_agent(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    db: Session = Depends(get_db),
) -> AuthenticatedAgent:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise unauthorized()
    token_hash = hashlib.sha256(credentials.credentials.encode("utf-8")).hexdigest()
    session = db.scalar(select(AgentSession).where(AgentSession.token_hash == token_hash))
    if session is None or not secrets.compare_digest(session.token_hash, token_hash):
        raise unauthorized()
    now = utc_now()
    key = db.get(AgentKey, session.agent_key_id)
    if (
        session.revoked_at is not None
        or session.expires_at <= now
        or key is None
        or not key_is_active(key, now)
    ):
        raise unauthorized()
    return AuthenticatedAgent(session.agent_id, session.agent_key_id, session.session_id)


def make_key(
    request: AgentKeyCreate | AgentRegistrationCreate,
    agent_id: uuid.UUID,
    now: datetime,
) -> AgentKey:
    public_key, fingerprint, _ = normalize_public_key(request.public_key)
    expires_at = request.expires_at
    if expires_at is not None and expires_at.tzinfo is None:
        raise HTTPException(status_code=422, detail="expires_at must include a timezone")
    if expires_at is not None:
        expires_at = expires_at.astimezone(UTC)
    if expires_at is not None and expires_at <= now:
        raise HTTPException(status_code=422, detail="expires_at must be in the future")
    return AgentKey(
        agent_key_id=uuid.uuid4(),
        agent_id=agent_id,
        public_key=public_key,
        fingerprint_sha256=fingerprint,
        valid_from=now,
        expires_at=expires_at,
        key_label=request.key_label,
    )


@router.post("/agents", response_model=AgentRegistration, status_code=201)
def register_agent(
    request: AgentRegistrationCreate, db: Session = Depends(get_db)
) -> AgentRegistration:
    now = utc_now()
    agent = Agent(agent_id=uuid.uuid4())
    key = make_key(request, agent.agent_id, now)
    db.add_all([agent, key])
    append_event(db, "AGENT_CREATED", agent.agent_id, "agent", agent.agent_id)
    append_event(db, "AGENT_KEY_ADDED", agent.agent_id, "agent_key", key.agent_key_id)
    commit_creation(db, key)
    db.refresh(agent)
    return AgentRegistration(
        agent_id=agent.agent_id,
        created_at=agent.created_at,
        agent_key=AgentKeyRead.model_validate(key),
    )


@router.post("/auth/challenge", response_model=AuthChallengeRead, status_code=201)
def create_challenge(
    request: AuthChallengeCreate, db: Session = Depends(get_db)
) -> AuthChallengeRead:
    now = utc_now()
    key = db.scalar(
        select(AgentKey).where(
            AgentKey.agent_key_id == request.agent_key_id,
            AgentKey.agent_id == request.agent_id,
        )
    )
    if key is None or not key_is_active(key, now):
        raise not_found("active agent key")
    challenge = AuthChallenge(
        challenge_id=uuid.uuid4(),
        nonce=secrets.token_bytes(32),
        agent_id=request.agent_id,
        agent_key_id=request.agent_key_id,
        issued_at=now,
        expires_at=now
        + timedelta(
            seconds=configured_ttl("AUTH_CHALLENGE_TTL_SECONDS", 90, 600)
        ),
    )
    db.add(challenge)
    db.commit()
    message = canonical_challenge_message(challenge).decode("utf-8")
    return AuthChallengeRead(
        challenge_id=challenge.challenge_id,
        nonce=base64.urlsafe_b64encode(challenge.nonce).rstrip(b"=").decode("ascii"),
        agent_id=challenge.agent_id,
        agent_key_id=challenge.agent_key_id,
        issued_at=challenge.issued_at,
        expires_at=challenge.expires_at,
        signed_message=message,
    )


@router.post("/auth/verify", response_model=SessionTokenRead)
def verify_challenge(
    request: AuthVerifyCreate, db: Session = Depends(get_db)
) -> SessionTokenRead:
    challenge = db.scalar(
        select(AuthChallenge)
        .where(AuthChallenge.challenge_id == request.challenge_id)
        .with_for_update()
    )
    now = utc_now()
    if challenge is None or challenge.consumed_at is not None or challenge.expires_at <= now:
        raise unauthorized("invalid, expired, or consumed challenge")
    key = db.get(AgentKey, challenge.agent_key_id)
    if key is None or not key_is_active(key, now):
        raise unauthorized("agent key is not active")

    challenge.consumed_at = now
    try:
        signature = decode_base64(request.signature, 64, "signature")
        public_key = decode_base64(key.public_key, 32, "stored public key")
        Ed25519PublicKey.from_public_bytes(public_key).verify(
            signature, canonical_challenge_message(challenge)
        )
    except (HTTPException, InvalidSignature, ValueError) as exc:
        db.commit()
        raise unauthorized("signature verification failed") from exc

    token = secrets.token_urlsafe(32)
    expires_at = now + timedelta(
        seconds=configured_ttl("AUTH_SESSION_TTL_SECONDS", 3600, 86400)
    )
    session = AgentSession(
        session_id=uuid.uuid4(),
        agent_id=challenge.agent_id,
        agent_key_id=challenge.agent_key_id,
        token_hash=hashlib.sha256(token.encode("utf-8")).hexdigest(),
        expires_at=expires_at,
    )
    db.add(session)
    db.commit()
    return SessionTokenRead(
        access_token=token,
        token_type="Bearer",
        expires_at=expires_at,
        agent_id=session.agent_id,
        agent_key_id=session.agent_key_id,
    )


@router.post("/auth/keys", response_model=AgentKeyRead, status_code=201)
def add_key(
    request: AgentKeyCreate,
    authenticated: AuthenticatedAgent = Depends(get_authenticated_agent),
    db: Session = Depends(get_db),
) -> AgentKey:
    key = make_key(request, authenticated.agent_id, utc_now())
    db.add(key)
    append_event(
        db,
        "AGENT_KEY_ADDED",
        authenticated.agent_id,
        "agent_key",
        key.agent_key_id,
    )
    return commit_creation(db, key)


@router.post("/auth/keys/{key_id}/revoke", response_model=AgentKeyRead)
def revoke_key(
    key_id: uuid.UUID,
    authenticated: AuthenticatedAgent = Depends(get_authenticated_agent),
    db: Session = Depends(get_db),
) -> AgentKey:
    now = utc_now()
    # Lock the complete key set so concurrent revocations cannot each observe
    # another active key and jointly leave the agent locked out.
    keys = list(
        db.scalars(
            select(AgentKey)
            .where(AgentKey.agent_id == authenticated.agent_id)
            .order_by(AgentKey.agent_key_id)
            .with_for_update()
        )
    )
    key = next((candidate for candidate in keys if candidate.agent_key_id == key_id), None)
    if key is None:
        raise not_found("agent key")
    if key.revoked_at is not None:
        raise HTTPException(status_code=409, detail="agent key already revoked")
    other_active = sum(
        candidate.agent_key_id != key_id and key_is_active(candidate, now)
        for candidate in keys
    )
    if not other_active:
        raise HTTPException(status_code=409, detail="cannot revoke the last active key")

    key.revoked_at = now
    db.execute(
        update(AgentSession)
        .where(
            AgentSession.agent_key_id == key_id,
            AgentSession.revoked_at.is_(None),
        )
        .values(revoked_at=now)
    )
    append_event(
        db,
        "AGENT_KEY_REVOKED",
        authenticated.agent_id,
        "agent_key",
        key.agent_key_id,
    )
    db.commit()
    db.refresh(key)
    return key
