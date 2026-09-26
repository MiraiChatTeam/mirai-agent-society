"""Read-only public Agent package and For Agents page checks."""

import hashlib
import json
import re
import unittest
from urllib.parse import urljoin, urlparse

from app.control_manifest import CONSTITUTION_SHA256
from app.main import app


async def fetch(path: str, query: str = "") -> tuple[int, bytes, dict[str, str]]:
    messages: list[dict] = []
    sent = False

    async def receive() -> dict:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        messages.append(message)

    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "GET", "scheme": "http", "path": path,
        "raw_path": path.encode(), "query_string": query.encode(),
        "headers": [(b"host", b"test.example")],
        "client": ("127.0.0.1", 1234), "server": ("test.example", 80),
    }
    await app(scope, receive, send)
    start = next(item for item in messages if item["type"] == "http.response.start")
    body = b"".join(
        item.get("body", b"") for item in messages
        if item["type"] == "http.response.body"
    )
    headers = {k.decode().lower(): v.decode() for k, v in start["headers"]}
    return start["status"], body, headers


class AgentPackageTests(unittest.IsolatedAsyncioTestCase):
    async def test_for_agents_page_and_navigation_are_localized(self) -> None:
        for language, label in (
            ("en", "For Agents"), ("ja", "エージェント向け"),
            ("zh", "智能体指南"),
        ):
            status, body, headers = await fetch("/for-agents", f"lang={language}")
            self.assertEqual(status, 200)
            self.assertIn("text/html", headers["content-type"])
            html = body.decode("utf-8")
            self.assertIn(f'href="/for-agents?lang={language}"', html)
            for path in (
                "skill/SKILL.md", "skill/references/onboarding.md",
                "skill/references/api.md", "docs/MAS_CONSTITUTION.md",
                "docs/POLICY.md", "docs/PROTOCOL.md", "docs/PRIVACY.md",
            ):
                self.assertIn(
                    f'href="http://test.example/agent-resources/{path}"', html
                )
            self.assertIn('href="/api/v1/agent-package"', html)
            self.assertIn(label, html)
        self.assertNotIn('href="/?lang=en">Observe</a>', html)

    async def test_public_operator_config_example_is_complete_json(self) -> None:
        status, payload, _ = await fetch(
            "/agent-resources/skill/references/api.md"
        )
        self.assertEqual(status, 200)
        match = re.search(r"```json\n(.*?)\n```", payload.decode(), re.S)
        self.assertIsNotNone(match)
        example = json.loads(match.group(1))
        self.assertEqual(example["config_version"], "0.4")
        self.assertEqual(example["config_json"]["mas"]["config_version"], "0.4")
        self.assertEqual(
            set(example["config_json"]),
            {
                "mas", "identity", "policy", "daily_limits", "activity",
                "tokens", "cost", "model", "tools", "schedule", "privacy",
            },
        )

    async def test_manifest_urls_and_hashes_match_served_resources(self) -> None:
        status, raw, _ = await fetch("/api/v1/agent-package")
        self.assertEqual(status, 200)
        package = json.loads(raw)
        self.assertEqual(package["package_version"], "1")
        self.assertEqual(package["constitution"]["sha256"], CONSTITUTION_SHA256)
        self.assertEqual(
            package["emergency_fallback"],
            "unavailable_without_trusted_production_key",
        )
        ids = {item["id"] for item in package["required_documents"]}
        self.assertTrue({
            "skill", "onboarding", "api", "local-state", "constitution",
            "constitution-machine", "policy", "protocol", "privacy",
        }.issubset(ids))
        for item in package["required_documents"]:
            self.assertTrue(item["url"].startswith("http://test.example/agent-resources/"))
            self.assertNotIn("/home/", item["url"])
            resource_path = urlparse(item["url"]).path
            status, payload, _ = await fetch(resource_path)
            self.assertEqual(status, 200)
            self.assertEqual(hashlib.sha256(payload).hexdigest(), item["sha256"])
            if resource_path.endswith(".md"):
                for target in re.findall(r"\]\(([^)#]+)(?:\#[^)]*)?\)", payload.decode()):
                    if not target.endswith((".md", ".json")) or "http" in target:
                        continue
                    linked_path = urlparse(urljoin(item["url"], target)).path
                    linked_status, _, _ = await fetch(linked_path)
                    self.assertEqual(
                        linked_status, 200, f"unavailable link {target} from {item['id']}"
                    )
        status, _, _ = await fetch("/agent-resources/unknown")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
