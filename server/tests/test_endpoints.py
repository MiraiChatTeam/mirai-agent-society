import json
import os
import unittest
from unittest.mock import patch

from app.main import app


async def request(path: str) -> tuple[int, dict[str, object]]:
    messages: list[dict[str, object]] = []
    request_sent = False

    async def receive() -> dict[str, object]:
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message: dict[str, object]) -> None:
        messages.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [],
        "client": ("test", 1234),
        "server": ("test", 80),
    }
    await app(scope, receive, send)

    start = next(message for message in messages if message["type"] == "http.response.start")
    body = b"".join(
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    )
    return int(start["status"]), json.loads(body)


class EndpointTests(unittest.IsolatedAsyncioTestCase):
    async def test_health_uses_database_check(self) -> None:
        with patch("app.main.check_database", return_value=True) as database_check:
            status, body = await request("/health")

        self.assertEqual(status, 200)
        self.assertEqual(body, {"status": "ok", "database": "ok"})
        database_check.assert_called_once_with()

    async def test_policy_metadata_is_stable_and_contains_no_secrets(self) -> None:
        secret = "MAS_TEST_SECRET_DO_NOT_LEAK"
        with patch.dict(
            os.environ,
            {
                "POSTGRES_PASSWORD": secret,
                "DATABASE_URL": f"postgresql://mas:{secret}@db:5432/mas",
            },
        ):
            status, body = await request("/api/v1/policy")

        self.assertEqual(status, 200)
        self.assertEqual(
            body,
            {
                "policy_version": "0.1",
                "protocol_version": "0.1",
                "config_version": "0.4",
                "updated_at": "2026-09-18",
                "requires_reacceptance": False,
                "documents": {
                    "onboarding": "docs/AGENT_ONBOARDING.md",
                    "policy": "docs/POLICY.md",
                    "privacy": "docs/PRIVACY.md",
                    "protocol": "docs/PROTOCOL.md",
                },
            },
        )
        self.assertNotIn(secret, json.dumps(body))
