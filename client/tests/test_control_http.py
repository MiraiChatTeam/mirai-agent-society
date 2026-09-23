import io
import unittest
from email.message import Message
from unittest.mock import patch
from urllib.error import HTTPError

from client.mas_client.control_plane import _fetch_json


class _Response:
    status = 200

    def __init__(self, url, payload):
        self.url = url
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def geturl(self):
        return self.url

    def read(self, *_):
        return self.payload


class ControlHTTPTests(unittest.TestCase):
    def test_429_retry_after_is_bounded(self):
        headers = Message()
        headers["Retry-After"] = "120"
        error = HTTPError("https://mas.example.org/api/v1/control-manifest", 429, "rate limited", headers, io.BytesIO())
        with patch("client.mas_client.control_plane.urlopen", side_effect=error):
            self.assertEqual(_fetch_json(error.url), (429, {"retry_after_seconds": 120}))

    def test_redirect_and_duplicate_json_are_not_accepted(self):
        url = "https://mas.example.org/api/v1/control-manifest"
        with patch("client.mas_client.control_plane.urlopen", return_value=_Response("https://other.example.org", b"{}")):
            self.assertEqual(_fetch_json(url), (200, None))
        with patch("client.mas_client.control_plane.urlopen", return_value=_Response(url, b'{"a":1,"a":2}')):
            self.assertEqual(_fetch_json(url), (200, None))


if __name__ == "__main__":
    unittest.main()
