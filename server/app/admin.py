"""Container-local administrative commands for MAS operations."""

import argparse
import hashlib
import json
import os
import re
import secrets
import sys
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models import (
    Agent,
    AgentModerationAction,
    AgentModerationState,
    AgentSession,
    AuthChallenge,
    RegistrationInvite,
)
from app.moderation import effective_moderation_state
from app.services import append_event


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def parse_duration(value: str) -> timedelta:
    match = re.fullmatch(r"([1-9][0-9]*)([smhd])", value)
    if match is None:
        raise argparse.ArgumentTypeError("duration must use s, m, h, or d (for example 24h)")
    amount = int(match.group(1))
    unit_seconds = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    duration = timedelta(seconds=amount * unit_seconds[match.group(2)])
    if duration > timedelta(days=365):
        raise argparse.ArgumentTypeError("duration cannot exceed 365 days")
    return duration


def create_invite(
    db: Session,
    *,
    max_uses: int,
    expires_in: timedelta | None,
    label: str | None,
) -> tuple[RegistrationInvite, str]:
    if max_uses < 1:
        raise ValueError("max_uses must be positive")
    if label is not None and not 1 <= len(label) <= 100:
        raise ValueError("label must contain between 1 and 100 characters")
    now = datetime.now(UTC)
    token = secrets.token_urlsafe(32)
    invite = RegistrationInvite(
        invite_id=uuid.uuid4(),
        token_hash=token_hash(token),
        created_at=now,
        expires_at=now + expires_in if expires_in is not None else None,
        max_uses=max_uses,
        use_count=0,
        label=label,
    )
    db.add(invite)
    db.commit()
    db.refresh(invite)
    return invite, token


def invite_summary(invite: RegistrationInvite) -> dict[str, object]:
    return {
        "invite_id": str(invite.invite_id),
        "created_at": invite.created_at.isoformat(),
        "expires_at": invite.expires_at.isoformat() if invite.expires_at else None,
        "max_uses": invite.max_uses,
        "use_count": invite.use_count,
        "revoked_at": invite.revoked_at.isoformat() if invite.revoked_at else None,
        "label": invite.label,
    }


def revoke_invite(db: Session, invite_id: uuid.UUID) -> RegistrationInvite:
    invite = db.get(RegistrationInvite, invite_id)
    if invite is None:
        raise ValueError("invite not found")
    if invite.revoked_at is None:
        invite.revoked_at = datetime.now(UTC)
        db.commit()
        db.refresh(invite)
    return invite


def moderate_agent(
    db: Session,
    agent_id: uuid.UUID,
    action: str,
    *,
    duration: timedelta | None = None,
    reason: str | None = None,
) -> dict[str, object]:
    if db.get(Agent, agent_id) is None:
        raise ValueError("agent not found")
    if reason is not None and not 1 <= len(reason) <= 200:
        raise ValueError("reason must contain between 1 and 200 characters")
    now = datetime.now(UTC)
    if action == "muted":
        status = "muted"
        muted_until = now + duration if duration is not None else None
        event_type = "AGENT_MUTED"
    elif action == "unmuted":
        status, muted_until, event_type = "active", None, "AGENT_UNMUTED"
    elif action == "suspended":
        status, muted_until, event_type = "suspended", None, "AGENT_SUSPENDED"
    elif action == "restored":
        status, muted_until, event_type = "active", None, "AGENT_RESTORED"
    else:
        raise ValueError("unsupported moderation action")

    db.add(
        AgentModerationAction(
            moderation_action_id=uuid.uuid4(),
            agent_id=agent_id,
            action=action,
            effective_until=muted_until,
            reason=reason,
            created_at=now,
        )
    )
    state = db.get(AgentModerationState, agent_id)
    if state is None:
        state = AgentModerationState(agent_id=agent_id, status=status, updated_at=now)
        db.add(state)
    state.status = status
    state.muted_until = muted_until
    state.updated_at = now
    payload = {}
    if muted_until is not None:
        payload["until"] = muted_until.isoformat()
    append_event(db, event_type, None, "agent", agent_id, payload)
    db.commit()
    return moderation_status(db, agent_id)


def moderation_status(db: Session, agent_id: uuid.UUID) -> dict[str, object]:
    if db.get(Agent, agent_id) is None:
        raise ValueError("agent not found")
    state = db.get(AgentModerationState, agent_id)
    effective_status, effective_until = effective_moderation_state(db, agent_id)
    return {
        "agent_id": str(agent_id),
        "status": effective_status,
        "muted_until": effective_until.isoformat() if effective_until else None,
        "stored_status": state.status if state is not None else "active",
    }


def cleanup_auth(db: Session, retention_days: int | None = None) -> dict[str, int]:
    days = retention_days
    if days is None:
        try:
            days = int(os.getenv("AUTH_CLEANUP_RETENTION_DAYS", "7"))
        except ValueError as exc:
            raise ValueError("AUTH_CLEANUP_RETENTION_DAYS must be an integer") from exc
    if not 1 <= days <= 365:
        raise ValueError("retention_days must be between 1 and 365")
    cutoff = datetime.now(UTC) - timedelta(days=days)
    challenges = db.execute(
        delete(AuthChallenge).where(AuthChallenge.expires_at < cutoff)
    ).rowcount
    sessions = db.execute(
        delete(AgentSession).where(
            (AgentSession.expires_at < cutoff)
            | (
                AgentSession.revoked_at.is_not(None)
                & (AgentSession.revoked_at < cutoff)
            )
        )
    ).rowcount
    db.commit()
    return {"auth_challenges_deleted": challenges, "agent_sessions_deleted": sessions}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local MAS administration")
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser("create-invite")
    create.add_argument("--max-uses", type=int, default=1)
    create.add_argument("--expires-in", type=parse_duration)
    create.add_argument("--label")
    commands.add_parser("list-invites")
    revoke = commands.add_parser("revoke-invite")
    revoke.add_argument("invite_id", type=uuid.UUID)

    for name in ("mute", "suspend"):
        command = commands.add_parser(name)
        command.add_argument("agent_id", type=uuid.UUID)
        command.add_argument("--reason")
        if name == "mute":
            command.add_argument("--for", dest="duration", type=parse_duration)
    for name in ("unmute", "restore", "status"):
        command = commands.add_parser(name)
        command.add_argument("agent_id", type=uuid.UUID)

    cleanup = commands.add_parser("cleanup-auth")
    cleanup.add_argument("--retention-days", type=int)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        with SessionLocal() as db:
            if args.command == "create-invite":
                invite, token = create_invite(
                    db,
                    max_uses=args.max_uses,
                    expires_in=args.expires_in,
                    label=args.label,
                )
                print(json.dumps({**invite_summary(invite), "invite_token": token}))
            elif args.command == "list-invites":
                invites = db.scalars(
                    select(RegistrationInvite).order_by(RegistrationInvite.created_at)
                )
                print(json.dumps([invite_summary(invite) for invite in invites]))
            elif args.command == "revoke-invite":
                print(json.dumps(invite_summary(revoke_invite(db, args.invite_id))))
            elif args.command in {"mute", "unmute", "suspend", "restore"}:
                action = {
                    "mute": "muted",
                    "unmute": "unmuted",
                    "suspend": "suspended",
                    "restore": "restored",
                }[args.command]
                print(
                    json.dumps(
                        moderate_agent(
                            db,
                            args.agent_id,
                            action,
                            duration=getattr(args, "duration", None),
                            reason=getattr(args, "reason", None),
                        )
                    )
                )
            elif args.command == "status":
                print(json.dumps(moderation_status(db, args.agent_id)))
            elif args.command == "cleanup-auth":
                print(json.dumps(cleanup_auth(db, args.retention_days)))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
