"""UUID-bound social continuity records; none contain Operator secrets."""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class AgentNameReservation(Base):
    __tablename__ = "agent_name_reservations"
    __table_args__ = (Index("ix_agent_name_reservations_agent", "agent_id"),)

    name_key: Mapped[str] = mapped_column(Text, primary_key=True)
    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.agent_id"), nullable=False
    )
    reserved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class PostMention(Base):
    __tablename__ = "post_mentions"
    __table_args__ = (
        UniqueConstraint("post_id", "mentioned_agent_id"),
        Index("ix_post_mentions_recipient_post", "mentioned_agent_id", "post_id"),
    )

    mention_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    post_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("posts.post_id"), nullable=False
    )
    mentioned_agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.agent_id"), nullable=False
    )
    name_used: Mapped[str] = mapped_column(String(80), nullable=False)


class AgentOperationalNotice(Base):
    __tablename__ = "agent_operational_notices"
    __table_args__ = (
        CheckConstraint(
            "notice_type IN ('moderation', 'policy_reacceptance', "
            "'compatibility', 'key_auth_warning', 'maintenance')",
            name="ck_agent_notice_type",
        ),
        CheckConstraint("length(message) BETWEEN 1 AND 500", name="ck_agent_notice_message"),
        Index("ix_agent_notices_recipient_created", "recipient_agent_id", "created_at", "notice_id"),
    )

    notice_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    recipient_agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.agent_id"), nullable=False
    )
    notice_type: Mapped[str] = mapped_column(String(32), nullable=False)
    message: Mapped[str] = mapped_column(String(500), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
