"""Bounded, fail-soft source adapters for World Pulse acquisition."""

from __future__ import annotations

import ipaddress
import re
import hashlib
import socket
import unicodedata
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Protocol
from urllib.parse import parse_qs, urlencode, urlsplit
from xml.etree import ElementTree


MAX_RESPONSE_BYTES = 1_000_000
NETWORK_TIMEOUT_SECONDS = 10
USER_AGENT = "MiraiAgentSociety-WorldPulse/0.1 (+local research collector)"


@dataclass(frozen=True)
class SourceCandidate:
    adapter: str
    profile: str
    source_type: str
    source_name: str
    title: str
    source_url: str
    published_at: datetime | None
    external_id: str | None
    language: str
    context: str | None = None
    source_rank: int | None = None


class SourceAdapter(Protocol):
    adapter_id: str
    profile: str

    def collect(self, client: "BoundedHttpClient") -> list[SourceCandidate]: ...


class _MetadataText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _transient_description(value: str | None) -> str | None:
    """Read bounded public feed metadata; never persist the description."""
    if not value:
        return None
    parser = _MetadataText()
    parser.feed(value[:2_000])
    cleaned = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", " ".join(parser.parts))).strip()
    return cleaned[:600] or None


class _RestrictedRedirectHandler(urllib.request.HTTPRedirectHandler):
    max_redirections = 3

    def __init__(self, allowed_hosts: frozenset[str]) -> None:
        self.allowed_hosts = allowed_hosts

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        parsed = urlsplit(newurl)
        if parsed.scheme != "https" or parsed.hostname not in self.allowed_hosts:
            raise urllib.error.HTTPError(
                newurl, code, "redirect target is not allowed", headers, fp
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class BoundedHttpClient:
    """Fetch only explicitly configured HTTPS feed hosts with strict bounds."""

    def get(
        self,
        url: str,
        *,
        allowed_hosts: frozenset[str],
        timeout: int = NETWORK_TIMEOUT_SECONDS,
        max_bytes: int = MAX_RESPONSE_BYTES,
    ) -> bytes:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname not in allowed_hosts:
            raise ValueError("collector endpoint is not an allowed HTTPS host")
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml",
                "Accept-Encoding": "identity",
            },
        )
        opener = urllib.request.build_opener(
            _RestrictedRedirectHandler(allowed_hosts)
        )
        try:
            with opener.open(request, timeout=timeout) as response:
                declared = response.headers.get("Content-Length")
                if declared is not None and int(declared) > max_bytes:
                    raise ValueError("collector response exceeds size limit")
                body = response.read(max_bytes + 1)
        except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
            raise RuntimeError(f"source request failed: {exc}") from exc
        if len(body) > max_bytes:
            raise ValueError("collector response exceeds size limit")
        return body


def _text(element: ElementTree.Element, names: tuple[str, ...]) -> str | None:
    for child in element.iter():
        local_name = child.tag.rsplit("}", 1)[-1]
        if local_name in names and child.text:
            return child.text
    return None


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _safe_item_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("feed item URL must be absolute HTTP(S)")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("feed item URL must not contain credentials")
    host = parsed.hostname.lower()
    if host == "localhost" or host.endswith(".localhost"):
        raise ValueError("feed item URL must not target localhost")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if not address.is_global:
            raise ValueError("feed item URL must not target a private address")
    return value.strip()


@dataclass(frozen=True)
class RSSCollector:
    adapter_id: str
    profile: str
    feed_url: str
    source_type: str
    source_name: str
    language: str
    allowed_hosts: frozenset[str]
    max_items: int = 40

    def collect(self, client: BoundedHttpClient) -> list[SourceCandidate]:
        payload = client.get(self.feed_url, allowed_hosts=self.allowed_hosts)
        try:
            root = ElementTree.fromstring(payload)
        except ElementTree.ParseError as exc:
            raise ValueError("source returned malformed XML") from exc
        entries = [node for node in root.iter() if node.tag.rsplit("}", 1)[-1] in {"item", "entry"}]
        results: list[SourceCandidate] = []
        for rank, entry in enumerate(entries[: self.max_items], start=1):
            title = _text(entry, ("title",))
            published_at = _parse_timestamp(
                _text(entry, ("pubDate", "published", "updated"))
            )
            link = _text(entry, ("link",))
            if link is None:
                for child in entry.iter():
                    if child.tag.rsplit("}", 1)[-1] == "link" and child.get("href"):
                        link = child.get("href")
                        break
            if not title:
                continue
            identifier = _text(entry, ("guid", "id"))
            if self.source_type == "google_trends":
                geo = parse_qs(urlsplit(self.feed_url).query).get("geo", [""])[0]
                trend_date = published_at.date().isoformat() if published_at else ""
                link = "https://trends.google.com/trends/explore?" + urlencode(
                    {
                        "date": f"{trend_date} {trend_date}" if trend_date else "now 1-d",
                        "geo": geo,
                        "q": title.strip(),
                    }
                )
                title_key = unicodedata.normalize("NFKC", title).casefold().strip()
                identifier = (
                    f"{self.adapter_id}:trend:"
                    + hashlib.sha256(f"{trend_date}|{title_key}".encode()).hexdigest()[:32]
                )
            if not link:
                continue
            try:
                link = _safe_item_url(link)
            except ValueError:
                continue
            if identifier:
                if not identifier.startswith(f"{self.adapter_id}:"):
                    identifier = f"{self.adapter_id}:{identifier.strip()}"
                if len(identifier) > 255:
                    identifier = None
            results.append(
                SourceCandidate(
                    adapter=self.adapter_id,
                    profile=self.profile,
                    source_type=self.source_type,
                    source_name=self.source_name,
                    title=title,
                    source_url=link,
                    published_at=published_at,
                    external_id=identifier,
                    language=self.language,
                    context=_transient_description(_text(entry, ("description", "summary"))) if self.source_type == "news" else None,
                    source_rank=rank,
                )
            )
        return results

def configured_collectors(profile: str = "all") -> list[RSSCollector]:
    collectors = [
        RSSCollector(
            adapter_id="google-trends-us",
            profile="global-en",
            feed_url="https://trends.google.com/trending/rss?geo=US",
            source_type="google_trends",
            source_name="Google Trends (US)",
            language="en",
            allowed_hosts=frozenset({"trends.google.com"}),
        ),
        RSSCollector(
            adapter_id="google-trends-jp",
            profile="japan-ja",
            feed_url="https://trends.google.com/trending/rss?geo=JP",
            source_type="google_trends",
            source_name="Google Trends (Japan)",
            language="ja",
            allowed_hosts=frozenset({"trends.google.com"}),
        ),
        RSSCollector(
            adapter_id="bbc-world-rss",
            profile="global-en",
            feed_url="https://feeds.bbci.co.uk/news/world/rss.xml",
            source_type="news",
            source_name="BBC News World",
            language="en",
            allowed_hosts=frozenset({"feeds.bbci.co.uk"}),
        ),
        RSSCollector(
            adapter_id="nhk-news-rss",
            profile="japan-ja",
            feed_url="https://www3.nhk.or.jp/rss/news/cat0.xml",
            source_type="news",
            source_name="NHK News",
            language="ja",
            allowed_hosts=frozenset({"www3.nhk.or.jp"}),
        ),
        RSSCollector(
            adapter_id="google-news-zh",
            profile="china-zh",
            feed_url="https://news.google.com/rss?hl=zh-CN&gl=CN&ceid=CN:zh-Hans",
            source_type="news",
            source_name="Google News Chinese",
            language="zh",
            allowed_hosts=frozenset({"news.google.com"}),
        ),
        RSSCollector(
            adapter_id="google-news-en",
            profile="global-en",
            feed_url="https://news.google.com/rss?hl=en-US&gl=US&ceid=US:en",
            source_type="news",
            source_name="Google News English",
            language="en",
            allowed_hosts=frozenset({"news.google.com"}),
        ),
        RSSCollector(
            adapter_id="google-news-ja",
            profile="japan-ja",
            feed_url="https://news.google.com/rss?hl=ja&gl=JP&ceid=JP:ja",
            source_type="news",
            source_name="Google News Japanese",
            language="ja",
            allowed_hosts=frozenset({"news.google.com"}),
        ),
    ]
    if profile == "all":
        return collectors
    return [collector for collector in collectors if collector.profile == profile]
