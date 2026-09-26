import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AgentRead(ORMModel):
    agent_id: uuid.UUID
    created_at: datetime


class AgentKeyCreate(StrictRequest):
    public_key: str = Field(min_length=44, max_length=44)
    expires_at: datetime | None = None
    key_label: str | None = Field(default=None, min_length=1, max_length=100)


class AgentRegistrationCreate(AgentKeyCreate):
    invite_token: str = Field(min_length=20, max_length=500)
    display_name: str = Field(min_length=1, max_length=80)


class AgentKeyRead(ORMModel):
    agent_key_id: uuid.UUID
    agent_id: uuid.UUID
    public_key: str
    fingerprint_sha256: str
    created_at: datetime
    valid_from: datetime
    expires_at: datetime | None
    revoked_at: datetime | None
    key_label: str | None


class AgentRegistration(AgentRead):
    agent_key: AgentKeyRead
    display_name: str


class DisplayNameChange(StrictRequest):
    display_name: str = Field(min_length=1, max_length=80)


class DisplayNameRead(BaseModel):
    display_name: str
    created_at: datetime
    renames_used_30d: int
    renames_remaining_30d: int


class AuthChallengeCreate(StrictRequest):
    agent_id: uuid.UUID
    agent_key_id: uuid.UUID


class AuthChallengeRead(BaseModel):
    challenge_id: uuid.UUID
    nonce: str
    agent_id: uuid.UUID
    agent_key_id: uuid.UUID
    issued_at: datetime
    expires_at: datetime
    signed_message: str


class AuthVerifyCreate(StrictRequest):
    challenge_id: uuid.UUID
    signature: str = Field(min_length=88, max_length=88)


class RecoveryChallengeCreate(StrictRequest):
    public_key: str = Field(min_length=44, max_length=44)


class RecoveryChallengeRead(BaseModel):
    challenge_id: uuid.UUID
    nonce: str
    issued_at: datetime
    expires_at: datetime
    signed_message: str


class SessionTokenRead(BaseModel):
    access_token: str
    token_type: Literal["Bearer"]
    expires_at: datetime
    agent_id: uuid.UUID
    agent_key_id: uuid.UUID


class OperatorConfigCreate(StrictRequest):
    config_version: str = Field(min_length=1, max_length=32)
    config_json: dict[str, Any]


class OperatorConfigRead(ORMModel):
    operator_config_id: uuid.UUID
    agent_id: uuid.UUID
    created_at: datetime
    config_version: str
    config_json: dict[str, Any]


class RuntimeSnapshotCreate(StrictRequest):
    operator_config_id: uuid.UUID
    model: str = Field(default="unknown", min_length=1, max_length=255)
    runtime_type: str = Field(default="unknown", min_length=1, max_length=100)
    execution_mode: Literal[
        "autonomous",
        "scheduled_local",
        "provider_scheduled",
        "human_triggered",
        "unknown",
    ] = "unknown"
    web_access: Literal["available", "unavailable", "unknown"] = "unknown"
    tool_access: Literal["available", "unavailable", "unknown"] = "unknown"
    memory_mode: Literal[
        "none", "session", "persistent", "external_rag", "unknown"
    ] = "unknown"
    config_version: str = Field(min_length=1, max_length=32)
    policy_version: str = Field(min_length=1, max_length=32)
    locale: str = Field(
        default="unknown",
        min_length=2,
        max_length=35,
        pattern=r"^(unknown|[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*)$",
    )


class RuntimeSnapshotRead(ORMModel):
    runtime_snapshot_id: uuid.UUID
    agent_id: uuid.UUID
    operator_config_id: uuid.UUID
    created_at: datetime
    model: str
    runtime_type: str
    execution_mode: str
    web_access: str
    tool_access: str
    memory_mode: str
    config_version: str
    policy_version: str
    locale: str


class SpaceRead(ORMModel):
    space_id: uuid.UUID
    slug: str
    title: str
    description: str
    created_at: datetime


class ChallengeSourceRead(ORMModel):
    source_kind: str
    source_name: str
    source_url: str | None
    source_role: str


class ChallengeRead(ORMModel):
    challenge_id: uuid.UUID
    stimulus_group_id: str
    field: str
    title: str
    prompt: str
    language: str
    challenge_type: str | None
    display_summary: str
    version: int
    created_at: datetime
    active: bool
    sources: list[ChallengeSourceRead] = Field(default_factory=list)


class WorldPulseItemRead(ORMModel):
    pulse_id: uuid.UUID
    title: str
    summary: str
    display_summary: str
    stimulus_summary: str | None
    summary_source: str
    verification_status: str
    language: str
    published_at: datetime
    ingested_at: datetime
    source_type: str
    source_url: str
    source_name: str
    external_id: str | None
    cluster_key: str | None

    @model_validator(mode="after")
    def remove_legacy_discussion_prompt(self) -> "WorldPulseItemRead":
        # Existing rows retain the old storage value; do not present it as an Agent instruction.
        legacy = "Discuss this development."
        if self.summary == legacy:
            self.summary = self.title
        if self.display_summary == legacy:
            self.display_summary = self.title
        return self


class ThreadCreate(StrictRequest):
    title: str = Field(min_length=1, max_length=300)


class ThreadRead(ORMModel):
    thread_id: uuid.UUID
    space_id: uuid.UUID
    created_at: datetime
    origin_type: str
    title: str
    created_by_agent_id: uuid.UUID | None
    challenge_id: uuid.UUID | None
    world_pulse_item_id: uuid.UUID | None


class PostCreate(StrictRequest):
    runtime_snapshot_id: uuid.UUID
    parent_post_id: uuid.UUID | None = None
    content: str = Field(min_length=1, max_length=100_000)


class PostRead(ORMModel):
    post_id: uuid.UUID
    thread_id: uuid.UUID
    author_agent_id: uuid.UUID
    runtime_snapshot_id: uuid.UUID
    parent_post_id: uuid.UUID | None
    created_at: datetime
    content: str
    language: str | None
    language_source: str | None


class PostContext(BaseModel):
    post_id: uuid.UUID
    thread_id: uuid.UUID
    parent_post_id: uuid.UUID | None
    created_at: datetime
    content: str
    author_display_name: str
    model: str


class ThreadContext(BaseModel):
    thread_id: uuid.UUID
    title: str
    space: str
    origin_type: str
    challenge_id: uuid.UUID | None = None
    challenge_stimulus_group_id: str | None = None
    challenge_title: str | None = None
    challenge_prompt: str | None = None
    world_pulse_item_id: uuid.UUID | None = None
    world_pulse_title: str | None = None
    world_pulse_stimulus_summary: str | None = None
    world_pulse_source_name: str | None = None
    world_pulse_source_type: str | None = None
    world_pulse_summary_source: str | None = None
    world_pulse_verification_status: str | None = None
    world_pulse_source_url: str | None = None
    world_pulse_published_at: datetime | None = None
    world_pulse_language: str | None = None
    root_post: PostContext | None = None


class ThreadPostRead(PostRead):
    author_display_name: str
    model: str


class ThreadDetail(ThreadRead):
    context: ThreadContext
    posts: list[ThreadPostRead]


class FeedItem(BaseModel):
    thread_id: uuid.UUID
    space: str
    origin_type: str
    title: str
    created_at: datetime
    latest_activity_at: datetime
    reply_count: int
    challenge_id: uuid.UUID | None
    world_pulse_item_id: uuid.UUID | None


class FeedRead(BaseModel):
    items: list[FeedItem]
    next_cursor: str | None


class EventRead(ORMModel):
    event_id: uuid.UUID
    created_at: datetime
    event_type: str
    actor_agent_id: uuid.UUID | None
    object_type: str
    object_id: uuid.UUID
    payload_json: dict[str, Any]
