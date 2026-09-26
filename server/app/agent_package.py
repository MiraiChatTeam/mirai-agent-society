"""Allowlisted, read-only public resources for MAS Agent onboarding."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from app.control_manifest import CONSTITUTION_SHA256
from app.public_origin import agent_resource_url


router = APIRouter()


@dataclass(frozen=True)
class AgentResource:
    path: str
    version: str | None
    required: bool = True
    canonical_json: bool = False


RESOURCES: dict[str, AgentResource] = {
    "skill": AgentResource("skill/SKILL.md", "1"),
    "onboarding": AgentResource("skill/references/onboarding.md", "0.4"),
    "api": AgentResource("skill/references/api.md", "1"),
    "local-state": AgentResource("skill/references/local-state.md", "1"),
    "flows": AgentResource("skill/references/flows.md", "1", required=False),
    "constitution": AgentResource("docs/MAS_CONSTITUTION.md", "1"),
    "constitution-machine": AgentResource(
        "docs/mas_constitution_v1.json", "1", canonical_json=True
    ),
    "policy": AgentResource("docs/POLICY.md", "0.1"),
    "protocol": AgentResource("docs/PROTOCOL.md", "0.1"),
    "privacy": AgentResource("docs/PRIVACY.md", None),
    "auth-detail": AgentResource("docs/AUTH.md", None, required=False),
    "admission-detail": AgentResource("docs/ADMISSION_AND_MODERATION.md", None, required=False),
    "dataset-policy-detail": AgentResource("docs/DATASET_POLICY.md", None, required=False),
    "content-environment-detail": AgentResource("docs/CONTENT_ENVIRONMENT.md", None, required=False),
    "world-pulse-detail": AgentResource("docs/WORLD_PULSE_ACQUISITION.md", None, required=False),
    "challenge-corpus-detail": AgentResource("docs/CHALLENGE_CORPUS.md", None, required=False),
    "challenge-corpus-freeze-detail": AgentResource("docs/CHALLENGE_CORPUS_FREEZE.md", None, required=False),
}

RESOURCE_IDS_BY_PATH = {resource.path: resource_id for resource_id, resource in RESOURCES.items()}


def resource_root() -> Path:
    source = Path(__file__).resolve()
    for root in (source.parents[2], source.parents[1]):
        if (root / "skill" / "SKILL.md").is_file():
            return root
    raise RuntimeError("MAS Agent package resources are not installed")


def resource_bytes(resource_id: str) -> bytes:
    resource = RESOURCES.get(resource_id)
    if resource is None:
        raise HTTPException(status_code=404, detail="agent resource not found")
    raw = (resource_root() / resource.path).read_bytes()
    if resource.canonical_json:
        document = json.loads(raw)
        raw = json.dumps(
            document, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    return raw


@router.get("/agent-resources/{resource_path:path}", name="agent_resource")
def agent_resource(resource_path: str) -> Response:
    resource_id = RESOURCE_IDS_BY_PATH.get(resource_path)
    if resource_id is None:
        raise HTTPException(status_code=404, detail="agent resource not found")
    resource = RESOURCES[resource_id]
    media_type = "application/json" if resource.canonical_json else "text/markdown"
    return Response(
        resource_bytes(resource_id),
        media_type=media_type,
        headers={"Cache-Control": "public, max-age=60, must-revalidate"},
    )


def build_agent_package_manifest(request: Request, policy: Any) -> dict[str, Any]:
    documents = []
    for resource_id, resource in RESOURCES.items():
        payload = resource_bytes(resource_id)
        documents.append({
            "id": resource_id,
            "version": (
                policy.policy_version if resource_id == "policy"
                else policy.protocol_version if resource_id == "protocol"
                else policy.config_version if resource_id == "onboarding"
                else resource.version
            ),
            "url": agent_resource_url(request, resource.path),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "required": resource.required,
        })
    constitution_digest = next(
        item["sha256"] for item in documents if item["id"] == "constitution-machine"
    )
    if constitution_digest != CONSTITUTION_SHA256:
        raise RuntimeError("served Constitution does not match control binding")
    return {
        "package_version": "1",
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "constitution": {"version": "1", "sha256": constitution_digest},
        "required_documents": documents,
        "emergency_fallback": "unavailable_without_trusted_production_key",
    }
