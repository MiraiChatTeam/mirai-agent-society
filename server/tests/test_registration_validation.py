"""Registration validation never reflects credential-bearing request input."""

import json
import unittest

from app.main import app


async def register_invalid(payload: dict[str, object]) -> tuple[int, dict[str, object]]:
    body = json.dumps(payload).encode("utf-8")
    messages: list[dict] = []
    received = False

    async def receive() -> dict:
        nonlocal received
        if not received:
            received = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        messages.append(message)

    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "POST", "scheme": "http", "path": "/api/v1/agents",
        "raw_path": b"/api/v1/agents", "query_string": b"",
        "headers": [(b"host", b"test.example"), (b"content-type", b"application/json")],
        "client": ("127.0.0.1", 1234), "server": ("test.example", 80),
    }
    await app(scope, receive, send)
    status = next(m["status"] for m in messages if m["type"] == "http.response.start")
    response = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    return status, json.loads(response)


class RegistrationValidationTests(unittest.IsolatedAsyncioTestCase):
    async def test_sensitive_input_never_echoed_in_validation_responses(self) -> None:
        secret = "synthetic-invite-" + "x" * 32
        key = "synthetic-public-key-" + "y" * 32
        public_code = "synthetic-public-cohort-code"
        cases = [
            ({"invite_token": secret, "public_key": key}, "display_name"),
            ({"invite_token": secret, "public_key": "malformed", "display_name": "Ada"}, "public_key"),
            ({"invite_token": secret, "public_key": key, "display_name": "Ada", "expires_at": "malformed-optional"}, "expires_at"),
            ({"invite_token": secret, "public_key": key, "display_name": "Ada", "unexpected": "hidden-value"}, "body"),
            ({"admission_code": public_code, "public_key": key}, "display_name"),
            ({"admission_code": public_code, "public_key": "A" * 44, "display_name": "Ada", "public_cohort_fallback": "open"}, "body"),
            ({"invite_token": secret, "public_key": "A" * 44, "display_name": "Ada",
              "onboarding_language": "zh_CN", "onboarding_language_source": "agent_declared"}, "body"),
        ]
        for payload, field in cases:
            with self.subTest(field=field):
                status, response = await register_invalid(payload)
                self.assertEqual(status, 422)
                serialized = json.dumps(response)
                for sensitive in (secret, public_code, key, "malformed-optional", "hidden-value"):
                    self.assertNotIn(sensitive, serialized)
                self.assertIn(field, [error["loc"][1] for error in response["detail"]])
                self.assertTrue(all(set(error) == {"loc", "type", "msg"} for error in response["detail"]))
