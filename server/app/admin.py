"""Container-local administrative commands for MAS operations."""

import argparse
import hashlib
import json
import os
import re
import secrets
import sys
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.challenge_corpus import import_challenge_corpus
from app.continuity import NOTICE_MESSAGES, issue_operational_notice
from app.content import (
    create_challenge,
    ingest_world_pulse,
    publish_challenge,
    publish_world_pulse,
)
from app.models import (
    Agent,
    AgentModerationAction,
    AgentModerationState,
    AgentSession,
    AuthChallenge,
    RegistrationInvite,
    Challenge,
    Space,
    WorldPulseItem,
)
from app.moderation import effective_moderation_state
from app.services import append_event
from app.world_pulse_acquisition import run_pipeline
from app.world_pulse_cleanup import cleanup_development_sample
from app.world_pulse_collectors import configured_collectors


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


def parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("timestamp must be ISO 8601") from exc
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("timestamp must include a timezone")
    return parsed


def challenge_summary(challenge: Challenge) -> dict[str, object]:
    return {
        "challenge_id": str(challenge.challenge_id),
        "stimulus_group_id": challenge.stimulus_group_id,
        "field": challenge.field,
        "title": challenge.title,
        "language": challenge.language,
        "challenge_type": challenge.challenge_type,
        "version": challenge.version,
        "active": challenge.active,
        "created_at": challenge.created_at.isoformat(),
    }


def pulse_summary(item: WorldPulseItem) -> dict[str, object]:
    return {
        "pulse_id": str(item.pulse_id),
        "title": item.title,
        "stimulus_summary": item.stimulus_summary,
        "summary_source": item.summary_source,
        "verification_status": item.verification_status,
        "language": item.language,
        "published_at": item.published_at.isoformat(),
        "ingested_at": item.ingested_at.isoformat(),
        "source_type": item.source_type,
        "source_url": item.source_url,
        "source_name": item.source_name,
        "external_id": item.external_id,
        "cluster_key": item.cluster_key,
    }


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

    notice = commands.add_parser("issue-agent-notice")
    notice.add_argument("agent_id", type=uuid.UUID)
    notice.add_argument("--type", dest="notice_type", required=True, choices=(
        "moderation", "policy_reacceptance", "compatibility", "key_auth_warning", "maintenance",
    ))
    notice.add_argument("--message", help="must match the fixed text for the selected type")

    cleanup = commands.add_parser("cleanup-auth")
    cleanup.add_argument("--retention-days", type=int)

    commands.add_parser("list-spaces")
    create_challenge_parser = commands.add_parser("create-challenge")
    create_challenge_parser.add_argument("--stimulus-group-id", required=True)
    create_challenge_parser.add_argument("--field", required=True)
    create_challenge_parser.add_argument("--title", required=True)
    create_challenge_parser.add_argument("--prompt", required=True)
    create_challenge_parser.add_argument("--language", required=True)
    create_challenge_parser.add_argument(
        "--type", dest="challenge_type", choices=("verifiable", "open", "debatable")
    )
    create_challenge_parser.add_argument("--version", type=int, required=True)
    create_challenge_parser.add_argument(
        "--inactive", action="store_false", dest="active"
    )
    commands.add_parser("list-challenges")
    publish_challenge_parser = commands.add_parser("publish-challenge")
    publish_challenge_parser.add_argument("challenge_id", type=uuid.UUID)

    ingest_pulse = commands.add_parser("ingest-world-pulse")
    ingest_pulse.add_argument("--title", required=True)
    ingest_pulse.add_argument("--summary", required=True)
    ingest_pulse.add_argument("--language", required=True)
    ingest_pulse.add_argument("--published-at", type=parse_timestamp, required=True)
    ingest_pulse.add_argument("--source-type", required=True)
    ingest_pulse.add_argument("--source-url", required=True)
    ingest_pulse.add_argument("--source-name", required=True)
    ingest_pulse.add_argument("--external-id")
    ingest_pulse.add_argument("--cluster-key")
    list_pulse = commands.add_parser("list-world-pulse")
    list_pulse.add_argument("--limit", type=int, default=20)
    publish_pulse = commands.add_parser("publish-world-pulse")
    publish_pulse.add_argument("pulse_id", type=uuid.UUID)
    collect_pulse = commands.add_parser("collect-world-pulse")
    collect_pulse.add_argument(
        "--profile",
        choices=("all", "global-en", "japan-ja", "china-zh"),
        default="all",
    )
    collect_pulse.add_argument("--limit", type=int, default=10)
    collect_pulse.add_argument("--selection-date", type=date.fromisoformat)
    collect_pulse.add_argument("--dry-run", action="store_true")
    sample_cleanup = commands.add_parser("cleanup-world-pulse-development-sample")
    sample_cleanup.add_argument("--expected-items", type=int, required=True)
    sample_cleanup.add_argument("--apply-fingerprint")
    import_challenges = commands.add_parser("import-challenges")
    import_challenges.add_argument("path", type=Path)
    import_challenges.add_argument("--publish", action="store_true")
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
            elif args.command == "issue-agent-notice":
                issued = issue_operational_notice(db, args.agent_id, args.notice_type, args.message or NOTICE_MESSAGES[args.notice_type])
                print(json.dumps({"notice_id": str(issued.notice_id), "recipient_agent_id": str(issued.recipient_agent_id)}))
            elif args.command == "cleanup-auth":
                print(json.dumps(cleanup_auth(db, args.retention_days)))
            elif args.command == "list-spaces":
                spaces = db.scalars(select(Space).order_by(Space.slug))
                print(
                    json.dumps(
                        [
                            {
                                "space_id": str(space.space_id),
                                "slug": space.slug,
                                "title": space.title,
                                "description": space.description,
                            }
                            for space in spaces
                        ]
                    )
                )
            elif args.command == "create-challenge":
                challenge = create_challenge(
                    db,
                    stimulus_group_id=args.stimulus_group_id,
                    field=args.field,
                    title=args.title,
                    prompt=args.prompt,
                    language=args.language,
                    version=args.version,
                    active=args.active,
                    challenge_type=args.challenge_type,
                )
                print(json.dumps(challenge_summary(challenge)))
            elif args.command == "list-challenges":
                challenges = db.scalars(
                    select(Challenge).order_by(
                        Challenge.stimulus_group_id,
                        Challenge.language,
                        Challenge.version,
                    )
                )
                print(json.dumps([challenge_summary(item) for item in challenges]))
            elif args.command == "publish-challenge":
                thread = publish_challenge(db, args.challenge_id)
                print(
                    json.dumps(
                        {
                            "challenge_id": str(args.challenge_id),
                            "thread_id": str(thread.thread_id),
                        }
                    )
                )
            elif args.command == "ingest-world-pulse":
                item = ingest_world_pulse(
                    db,
                    title=args.title,
                    summary=args.summary,
                    language=args.language,
                    published_at=args.published_at,
                    source_type=args.source_type,
                    source_url=args.source_url,
                    source_name=args.source_name,
                    external_id=args.external_id,
                    cluster_key=args.cluster_key,
                )
                print(json.dumps(pulse_summary(item)))
            elif args.command == "list-world-pulse":
                if not 1 <= args.limit <= 200:
                    raise ValueError("limit must be between 1 and 200")
                items = db.scalars(
                    select(WorldPulseItem)
                    .order_by(
                        WorldPulseItem.published_at.desc(),
                        WorldPulseItem.pulse_id.desc(),
                    )
                    .limit(args.limit)
                )
                print(json.dumps([pulse_summary(item) for item in items]))
            elif args.command == "publish-world-pulse":
                thread = publish_world_pulse(db, args.pulse_id)
                print(
                    json.dumps(
                        {
                            "pulse_id": str(args.pulse_id),
                            "thread_id": str(thread.thread_id),
                        }
                    )
                )
            elif args.command == "collect-world-pulse":
                if not 1 <= args.limit <= 100:
                    raise ValueError("limit must be between 1 and 100")
                print(
                    json.dumps(
                        run_pipeline(
                            db,
                            configured_collectors(args.profile),
                            dry_run=args.dry_run,
                            selection_date=args.selection_date,
                            limit=args.limit,
                        ),
                        ensure_ascii=False,
                    )
                )
            elif args.command == "cleanup-world-pulse-development-sample":
                print(json.dumps(cleanup_development_sample(
                    db, expected_items=args.expected_items,
                    expected_fingerprint=args.apply_fingerprint,
                )))
            elif args.command == "import-challenges":
                print(
                    json.dumps(
                        import_challenge_corpus(
                            db, args.path, publish=args.publish
                        )
                    )
                )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
