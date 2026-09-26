"""Bounded, robots-aware publisher HEAD metadata access; never parse article body."""

from __future__ import annotations

import html
import http.client
import ipaddress
import json
import re
import socket
import ssl
import unicodedata
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser


USER_AGENT = "MiraiAgentSociety-WorldPulse/0.2"
MAX_HEAD_BYTES = 65_536
MAX_ROBOTS_BYTES = 65_536
TIMEOUT_SECONDS = 5
MAX_REDIRECTS = 2


def _public_https(url: str) -> tuple[str, str, str]:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("publisher URL must be public HTTPS without credentials")
    if parsed.port not in (None, 443):
        raise ValueError("publisher URL must use HTTPS port 443")
    host = parsed.hostname.lower()
    if host == "localhost" or host.endswith(".localhost"):
        raise ValueError("localhost is not a publisher")
    try:
        literal_address = ipaddress.ip_address(host)
    except ValueError:
        literal_address = None
    if literal_address is not None and not literal_address.is_global:
        raise ValueError("publisher IP is not public")
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    return host, path, f"https://{host}{path}"


def _pinned_ip(host: str) -> str:
    records = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    addresses = [record[4][0] for record in records]
    if not addresses or any(not ipaddress.ip_address(addr).is_global for addr in addresses):
        raise ValueError("publisher DNS is not exclusively public")
    return addresses[0]


class _PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host: str, ip: str):
        super().__init__(host, timeout=TIMEOUT_SECONDS, context=ssl.create_default_context())
        self._ip = ip

    def connect(self) -> None:
        raw = socket.create_connection((self._ip, 443), self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except Exception:
            raw.close()
            raise


def _request(url: str, *, robots: bool) -> tuple[int, dict[str, str], bytes]:
    host, path, _ = _public_https(url)
    connection = _PinnedHTTPS(host, _pinned_ip(host))
    try:
        connection.request(
            "GET", path,
            headers={
                "Host": host, "User-Agent": USER_AGENT,
                "Accept": "text/plain" if robots else "text/html",
                "Accept-Encoding": "identity",
            },
        )
        response = connection.getresponse()
        headers = {key.lower(): value for key, value in response.getheaders()}
        limit = MAX_ROBOTS_BYTES if robots else MAX_HEAD_BYTES
        if robots:
            content = response.read(limit + 1)
        else:
            chunks: list[bytes] = []
            total = 0
            while total < limit:
                chunk = response.read(min(4096, limit - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                lower = b"".join(chunks).lower()
                if b"</head" in lower or b"<body" in lower:
                    break
            content = b"".join(chunks)
        if len(content) > limit:
            raise ValueError("publisher response exceeds metadata limit")
        return response.status, headers, content
    finally:
        connection.close()


def _robots_allowed(url: str) -> bool:
    host, _, clean_url = _public_https(url)
    status, headers, content = _request(f"https://{host}/robots.txt", robots=True)
    if status != 200 or len(content) >= MAX_ROBOTS_BYTES:
        return False
    if "text" not in headers.get("content-type", "").lower():
        return False
    parser = RobotFileParser()
    parser.parse(content.decode("utf-8", "replace").splitlines())
    return parser.can_fetch(USER_AGENT, clean_url)


def _clean_description(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = unicodedata.normalize("NFKC", html.unescape(value))
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not 35 <= len(cleaned) <= 600:
        return None
    return cleaned


def _jsonld_description(value: object) -> str | None:
    if isinstance(value, dict):
        direct = _clean_description(value.get("description"))
        if direct:
            return direct
        graph = value.get("@graph")
        if isinstance(graph, list):
            for node in graph[:10]:
                result = _jsonld_description(node)
                if result:
                    return result
    elif isinstance(value, list):
        for node in value[:10]:
            result = _jsonld_description(node)
            if result:
                return result
    return None


class _HeadMetadata(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.in_head = False
        self.in_jsonld = False
        self.jsonld_parts: list[str] = []
        self.values: dict[str, str] = {}
        self.jsonld: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag == "head":
            self.in_head = True
        if tag == "body":
            self.in_head = False
        if not self.in_head:
            return
        attrs_map = {key.lower(): value for key, value in attrs if value is not None}
        if tag == "meta":
            key = (attrs_map.get("name") or attrs_map.get("property") or "").lower()
            if key in {"description", "og:description", "twitter:description"}:
                value = _clean_description(attrs_map.get("content"))
                if value:
                    self.values.setdefault(key, value)
        if tag == "script" and attrs_map.get("type", "").lower() == "application/ld+json":
            self.in_jsonld = True
            self.jsonld_parts = []

    def handle_data(self, data: str) -> None:
        if self.in_head and self.in_jsonld and sum(map(len, self.jsonld_parts)) < 8192:
            self.jsonld_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script" and self.in_jsonld:
            self.jsonld.append("".join(self.jsonld_parts)[:8192])
            self.in_jsonld = False
        elif tag.lower() == "head":
            self.in_head = False

    def description(self) -> str | None:
        for key in ("description", "og:description", "twitter:description"):
            if key in self.values:
                return self.values[key]
        for payload in self.jsonld:
            try:
                result = _jsonld_description(json.loads(payload))
            except (ValueError, TypeError):
                continue
            if result:
                return result
        return None


class PublisherMetadataClient:
    """Fetch only public page HEAD metadata, failing closed per item."""

    def fetch(self, source_url: str, *, aggregator: bool = False) -> str | None:
        try:
            current = source_url
            for _ in range(MAX_REDIRECTS + 1):
                if not _robots_allowed(current):
                    return None
                status, headers, content = _request(current, robots=False)
                if status in {301, 302, 303, 307, 308}:
                    location = headers.get("location")
                    if not location:
                        return None
                    current = urljoin(current, location)
                    continue
                if status != 200 or "text/html" not in headers.get("content-type", "").lower():
                    return None
                host, _, _ = _public_https(current)
                if aggregator and host == "news.google.com":
                    return None
                parser = _HeadMetadata()
                parser.feed(content.decode("utf-8", "replace"))
                return parser.description()
        except (OSError, ValueError, RuntimeError, ssl.SSLError, http.client.HTTPException):
            return None
        return None
