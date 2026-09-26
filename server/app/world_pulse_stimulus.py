"""Small, source-grounded World Pulse context; no article-body access or LLM.

Only fixed trend templates and a narrow set of verifiable RSS metadata patterns
produce text. Ambiguous news metadata deliberately yields no summary.
"""

from __future__ import annotations

import re
from collections.abc import Callable


MAX_STIMULUS_CHARS = 800
SUMMARY_SOURCES = frozenset({
    "feed_metadata", "publisher_page", "trend_context", "unavailable",
})


def _bounded(summary: str | None, source: str) -> tuple[str | None, str]:
    if summary is None or len(summary) > MAX_STIMULUS_CHARS:
        return None, "unavailable"
    return summary, source


def trend_context(title: str, *, profile: str, language: str) -> tuple[str | None, str]:
    """Describe a search trend without implying a verified news event."""
    if profile == "global-en" and language == "en":
        return _bounded(
            f"At acquisition, {title} appeared among Google Trends search topics in "
            "the United States. This public feed records search interest, not a "
            "verified news event. The feed does not establish why the term is trending.",
            "trend_context",
        )
    if profile == "japan-ja" and language == "ja":
        return _bounded(
            f"取得時点で「{title}」は Google Trends の日本向け公開フィードに検索トレンドとして掲載されていました。"
            "これは検索関心を示す記録であり、特定のニュース事象を確認するものではありません。"
            "このフィードだけでは、検索関心が高まった理由は分かりません。",
            "trend_context",
        )
    if profile == "china-zh" and language == "zh":
        return _bounded(
            f"采集时，“{title}”出现在 Google Trends 的公开搜索趋势中。"
            "这条记录反映搜索关注度，并不证实某一具体新闻事件。"
            "该公开趋势资料未说明搜索关注度上升的原因。",
            "trend_context",
        )
    return None, "unavailable"


def _english_feed_context(title: str, description: str, source_name: str) -> str | None:
    """Paraphrase only explicitly recognized metadata facts, never arbitrary text."""
    heading = title.casefold()
    details = description.casefold()
    if (
        "openai agent" in heading and "australian government website" in heading
        and "pm says" in heading and "albanese" in details
        and "sam altman" in details and "three months after the breach in june" in details
    ):
        return (
            f"{source_name} reports that Australia's prime minister described an OpenAI agent "
            "as having infiltrated a government website. The public feed says Albanese "
            "raised his concern with Sam Altman after authorities learned of the June breach three months later."
        )
    if (
        "ai superpower ambitions" in heading and "trump and xi" in heading
        and "us and china" in details and "ai supremacy" in details
        and "human control" in details
    ):
        return (
            f"{source_name} reports that AI ambitions feature in the meeting between Trump and Xi. "
            "The public feed describes US-China competition over AI leadership alongside efforts to retain human control."
        )
    if "launch" in title.casefold() and re.search(
        r"\bmission controllers confirmed (?:a|the) successful launch\b",
        description,
        re.IGNORECASE,
    ):
        return (
            f"The {source_name} public feed reports a launch and attributes "
            "confirmation of its success to mission controllers."
        )
    if "US and Iran" in title and "talks" in title.casefold() and re.search(
        r"\btalks are the first since a ceasefire collapsed in June\b",
        description,
        re.IGNORECASE,
    ) and re.search(r"\bsides exchanging fire intermittently\b", description, re.IGNORECASE):
        return (
            f"The {source_name} public feed reports talks between the US and "
            "Iran. Its description places the meeting after a June ceasefire "
            "collapse and notes intermittent exchanges of fire between the "
            "sides."
        )
    return None


def derive_publisher_stimulus(
    *, title: str, description: str, source_name: str, language: str
) -> tuple[str | None, str]:
    """Paraphrase only narrowly corroborated facts from public HEAD metadata."""
    if language != "en":
        return None, "unavailable"
    heading = title.casefold()
    details = description.casefold()
    if "hurricane" in heading and "downgraded" in heading and "mexico" in heading and (
        "warm waters" in details and "el niño" in details
    ):
        return _bounded(
            f"{source_name} reports that the hurricane was downgraded while remaining powerful near Mexico. "
            "The public publisher metadata links its strength to unusually warm Pacific waters associated with El Niño.",
            "publisher_page",
        )
    if "tigray" in heading and "airport" in heading and (
        "tplf" in details and "return to war" in details
    ):
        return _bounded(
            f"{source_name} reports, citing residents, that Tigray forces took an airport in northern Ethiopia. "
            "Its public page metadata notes concern about renewed conflict amid rising tension between the government and TPLF.",
            "publisher_page",
        )
    if "venezuelan president" in heading and "maduro" in heading and (
        "rodriguez" in details and "arrested" in details
    ):
        return _bounded(
            f"{source_name} reports a meeting between Trump and the US-backed Venezuelan leader after Maduro was seized. "
            "The public page metadata identifies Rodriguez as the leader chosen after Maduro was arrested and taken to the United States.",
            "publisher_page",
        )
    if "ethiopia and tigray" in heading and "offensives" in heading and (
        "airports" in details and "drone strikes" in details
    ):
        return _bounded(
            f"{source_name} reports opposing claims of offensives involving Ethiopia and Tigray. "
            "Its public page metadata says local authorities reportedly took control of Tigray airports after reports of recent drone strikes.",
            "publisher_page",
        )
    if "spain" in heading and "housing" in heading and (
        "maricarmen" in details and "rent set by" in details
    ):
        return _bounded(
            f"{source_name} presents an eviction involving an older woman as part of a report on housing pressure in Spain. "
            "The public page metadata says the Madrid tenant could not meet the rent set by a company that had recently bought her flat.",
            "publisher_page",
        )
    return None, "unavailable"


def derive_stimulus(
    *,
    title: str,
    description: str | None,
    source_name: str,
    source_type: str,
    adapter: str,
    profile: str,
    language: str,
    publisher_page_fetch: Callable[[str], str | None] | None = None,
    source_url: str | None = None,
) -> tuple[str | None, str]:
    """Use public feed metadata; optional publisher context fails closed.

    The live pipeline enriches selected items with its separate bounded
    publisher client. This injection point remains for isolated callers/tests.
    """
    if source_type == "google_trends":
        return trend_context(title, profile=profile, language=language)
    if source_type != "news" or language != "en":
        return None, "unavailable"
    summary = _english_feed_context(title, description or "", source_name)
    if summary is not None:
        return _bounded(summary, "feed_metadata")
    if adapter.startswith("google-news-") and publisher_page_fetch and source_url:
        try:
            page_context = publisher_page_fetch(source_url)
        except Exception:
            return None, "unavailable"
        summary = _english_feed_context(title, page_context or "", source_name)
        if summary is not None:
            return _bounded(summary, "publisher_page")
    return None, "unavailable"
