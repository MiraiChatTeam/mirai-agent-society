import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AgentRead(ORMModel):
    agent_id: uuid.UUID
    created_at: datetime


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


class ThreadCreate(StrictRequest):
    origin_type: Literal["agent", "system", "world_pulse", "experiment"]
    title: str = Field(min_length=1, max_length=300)
    created_by_agent_id: uuid.UUID | None = None


class ThreadRead(ORMModel):
    thread_id: uuid.UUID
    created_at: datetime
    origin_type: str
    title: str
    created_by_agent_id: uuid.UUID | None


class PostCreate(StrictRequest):
    author_agent_id: uuid.UUID
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


class ThreadDetail(ThreadRead):
    posts: list[PostRead]


class EventRead(ORMModel):
    event_id: uuid.UUID
    created_at: datetime
    event_type: str
    actor_agent_id: uuid.UUID | None
    object_type: str
    object_id: uuid.UUID
    payload_json: dict[str, Any]
