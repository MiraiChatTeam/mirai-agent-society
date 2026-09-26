"""Offline authoritative package fixture; no network or MAS writes."""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from client.mas_client.agent_package_update import PackageResponse, REQUIRED_IDS
from client.tests.test_local_state import IDENTITY

ORIGIN = IDENTITY["society_origin"]
ROOT = Path(__file__).resolve().parents[2]
MACHINE = json.dumps(json.loads((ROOT / "docs/mas_constitution_v1.json").read_text()),
                     ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
PATHS = {
    "skill": "skill/SKILL.md", "onboarding": "skill/references/onboarding.md",
    "api": "skill/references/api.md", "local-state": "skill/references/local-state.md",
    "constitution": "docs/MAS_CONSTITUTION.md",
    "constitution-machine": "docs/mas_constitution_v1.json",
    "policy": "docs/POLICY.md", "protocol": "docs/PROTOCOL.md", "privacy": "docs/PRIVACY.md",
}


def package_fixture():
    resources = {key: MACHINE if key == "constitution-machine" else (ROOT / path).read_bytes()
                 for key, path in PATHS.items()}
    documents = [
        {"id": key, "version": "0.1" if key in {"policy", "protocol"} else "1",
         "url": ORIGIN + "/agent-resources/" + path,
         "sha256": hashlib.sha256(resources[key]).hexdigest(), "required": True}
        for key, path in PATHS.items()
    ]
    documents[next(i for i, item in enumerate(documents) if item["id"] == "onboarding")]["version"] = "0.4"
    documents[next(i for i, item in enumerate(documents) if item["id"] == "privacy")]["version"] = None
    assert set(resources) == REQUIRED_IDS
    manifest = {"package_version": "1", "generated_at": "2026-09-23T00:00:00Z",
                "constitution": {"version": "1", "sha256": IDENTITY["constitution_sha256"]},
                "required_documents": documents,
                "emergency_fallback": "unavailable_without_trusted_production_key"}
    return manifest, resources


class FakePackageTransport:
    def __init__(self):
        self.manifest, self.resources = package_fixture()
        self.package_calls = []
        self.resource_calls = []
        self.reload_calls = []
        self.redirect_package = False
        self.redirect_resource = False

    def fetch_package(self, url):
        self.package_calls.append(url)
        return PackageResponse(200, url, self.manifest, self.redirect_package)

    def fetch_resource(self, url):
        self.resource_calls.append(url)
        item = next(item for item in self.manifest["required_documents"] if item["url"] == url)
        return PackageResponse(200, url, self.resources[item["id"]], self.redirect_resource)

    def reload_guidance(self, documents):
        self.reload_calls.append(documents)
