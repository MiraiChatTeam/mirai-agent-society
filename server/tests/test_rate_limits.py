import unittest
from unittest.mock import patch

from starlette.requests import Request

from app.admin import parse_duration
from app.rate_limits import request_source


def make_request(peer: str, forwarded_for: str | None = None) -> Request:
    headers = []
    if forwarded_for is not None:
        headers.append((b"x-forwarded-for", forwarded_for.encode("ascii")))
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/",
            "headers": headers,
            "client": (peer, 1234),
        }
    )


class RequestSourceTests(unittest.TestCase):
    def test_forwarded_header_is_ignored_from_untrusted_peer(self) -> None:
        with patch.dict("os.environ", {"TRUSTED_PROXY_IPS": ""}):
            self.assertEqual(
                request_source(make_request("127.0.0.1", "198.51.100.7")),
                "127.0.0.1",
            )

    def test_forwarded_header_is_used_only_from_explicit_trusted_peer(self) -> None:
        with patch.dict("os.environ", {"TRUSTED_PROXY_IPS": "127.0.0.1"}):
            self.assertEqual(
                request_source(make_request("127.0.0.1", "198.51.100.7, 10.0.0.1")),
                "198.51.100.7",
            )

    def test_invalid_forwarded_header_falls_back_to_peer(self) -> None:
        with patch.dict("os.environ", {"TRUSTED_PROXY_IPS": "127.0.0.1"}):
            self.assertEqual(
                request_source(make_request("127.0.0.1", "not-an-ip")),
                "127.0.0.1",
            )


class AdminDurationTests(unittest.TestCase):
    def test_duration_units(self) -> None:
        self.assertEqual(parse_duration("24h").total_seconds(), 86400)

    def test_invalid_duration_is_rejected(self) -> None:
        with self.assertRaises(Exception):
            parse_duration("tomorrow")
