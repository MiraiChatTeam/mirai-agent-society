"""Production origin and narrowly trusted proxy behavior."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from starlette.requests import Request
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.main import app
from app.public_origin import configured_public_origin
from app.rate_limits import request_source
from app.serve import default_ipv4_gateway


async def fetch(
    path: str,
    *,
    host: str = "mas.miraichat.net",
    scheme: str = "http",
    peer: str = "172.18.0.1",
    forwarded_proto: str | None = None,
    forwarded_for: str | None = None,
    method: str = "GET",
) -> tuple[int, bytes]:
    messages: list[dict] = []
    headers = [(b"host", host.encode("ascii"))]
    if forwarded_proto is not None:
        headers.append((b"x-forwarded-proto", forwarded_proto.encode("ascii")))
    if forwarded_for is not None:
        headers.append((b"x-forwarded-for", forwarded_for.encode("ascii")))
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": method, "scheme": scheme, "path": path,
        "raw_path": path.encode(), "query_string": b"",
        "headers": headers, "client": (peer, 1234), "server": (host, 8000),
    }
    sent = False

    async def receive() -> dict:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        messages.append(message)

    await ProxyHeadersMiddleware(app, trusted_hosts="172.18.0.1")(scope, receive, send)
    start = next(item for item in messages if item["type"] == "http.response.start")
    body = b"".join(
        item.get("body", b"") for item in messages
        if item["type"] == "http.response.body"
    )
    return start["status"], body


class PublicOriginTests(unittest.IsolatedAsyncioTestCase):
    async def test_trusted_proxy_produces_only_canonical_https_agent_urls(self) -> None:
        with patch.dict(os.environ, {"MAS_PUBLIC_ORIGIN": "https://mas.miraichat.net"}):
            status, raw = await fetch(
                "/api/v1/agent-package",
                forwarded_proto="https",
                forwarded_for="203.0.113.8",
            )
            self.assertEqual(status, 200)
            documents = json.loads(raw)["required_documents"]
            self.assertTrue(documents)
            self.assertTrue(all(
                item["url"].startswith("https://mas.miraichat.net/agent-resources/")
                for item in documents
            ))
            status, html = await fetch("/for-agents", forwarded_proto="https")
            self.assertEqual(status, 200)
            self.assertIn(b"https://mas.miraichat.net/agent-resources/", html)

    async def test_forged_host_and_untrusted_forwarding_fail_closed(self) -> None:
        with patch.dict(os.environ, {"MAS_PUBLIC_ORIGIN": "https://mas.miraichat.net"}):
            status, _ = await fetch(
                "/api/v1/agent-package",
                host="attacker.example",
                forwarded_proto="https",
            )
            self.assertEqual(status, 400)
            status, _ = await fetch(
                "/api/v1/agent-package",
                peer="172.18.0.2",
                forwarded_proto="https",
                forwarded_for="198.51.100.7",
            )
            self.assertEqual(status, 403)
            status, _ = await fetch(
                "/api/v1/agents", method="POST", forwarded_proto=None,
            )
            self.assertEqual(status, 403)

    async def test_local_health_and_hidden_production_docs(self) -> None:
        with patch.dict(os.environ, {"MAS_PUBLIC_ORIGIN": "https://mas.miraichat.net"}):
            status, _ = await fetch(
                "/health", host="127.0.0.1:8000", peer="127.0.0.1",
            )
            self.assertEqual(status, 200)
            for path in ("/docs", "/redoc", "/openapi.json"):
                status, _ = await fetch(path, forwarded_proto="https")
                self.assertEqual(status, 404)

    def test_origin_configuration_rejects_noncanonical_values(self) -> None:
        for value in (
            "http://mas.miraichat.net", "https://mas.miraichat.net/",
            "https://mas.miraichat.net:443", "https://user@mas.miraichat.net",
        ):
            with self.subTest(value=value), patch.dict(
                os.environ, {"MAS_PUBLIC_ORIGIN": value}
            ):
                with self.assertRaises(RuntimeError):
                    configured_public_origin()

    def test_gateway_is_derived_from_default_route(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            route_table = Path(folder) / "route"
            route_table.write_text(
                "Iface Destination Gateway Flags RefCnt Use Metric Mask MTU Window IRTT\n"
                "eth0 00000000 010012AC 0003 0 0 0 00000000 0 0 0\n",
                encoding="ascii",
            )
            self.assertEqual(default_ipv4_gateway(route_table), "172.18.0.1")


class ProxyIdentityTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_gateway_can_supply_forwarded_client_identity(self) -> None:
        observed = []

        async def recorder(scope, receive, send):
            observed.append((scope["scheme"], request_source(Request(scope))))
            await send({
                "type": "http.response.start", "status": 200,
                "headers": [],
            })
            await send({"type": "http.response.body", "body": b""})

        async def run(peer: str) -> None:
            scope = {
                "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                "method": "GET", "scheme": "http", "path": "/",
                "raw_path": b"/", "query_string": b"",
                "headers": [
                    (b"host", b"mas.miraichat.net"),
                    (b"x-forwarded-proto", b"https"),
                    (b"x-forwarded-for", b"203.0.113.8"),
                ],
                "client": (peer, 1234),
                "server": ("mas.miraichat.net", 8000),
            }

            async def receive():
                return {"type": "http.request", "body": b"", "more_body": False}

            async def send(message):
                pass

            await ProxyHeadersMiddleware(
                recorder, trusted_hosts="172.18.0.1"
            )(scope, receive, send)

        with patch.dict(os.environ, {"TRUSTED_PROXY_IPS": ""}):
            await run("172.18.0.1")
            await run("172.18.0.2")
        self.assertEqual(observed, [
            ("https", "203.0.113.8"),
            ("http", "172.18.0.2"),
        ])


if __name__ == "__main__":
    unittest.main()
