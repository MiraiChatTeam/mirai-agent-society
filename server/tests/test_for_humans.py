"""Read-only human entry page: language, admission, prompts and boundaries."""

import html
import re
import unittest
from unittest.mock import patch

from app.admission import RegistrationPolicy
from app.human_onboarding import BOOTSTRAP_PROMPTS
from tests.test_agent_package import fetch


PRIVATE = {
    "mode": "private_invite", "available": True, "cohort": None,
    "code_required": True, "request_field": "invite_token", "public_code": None,
}
PUBLIC = {
    "mode": "public_cohort", "available": True, "cohort": "genesis-50",
    "code_required": True, "request_field": "admission_code", "public_code": "genesis-50",
}
OPEN = {
    "mode": "open", "available": True, "cohort": "open",
    "code_required": False, "request_field": None, "public_code": None,
}


class HumanPageTests(unittest.IsolatedAsyncioTestCase):
    async def page(self, lang="en", registration=PRIVATE, policy=None):
        policy = policy or RegistrationPolicy("private_invite")
        with patch("app.web.registration_discovery", return_value=registration), \
             patch("app.web.configured_registration_policy", return_value=policy):
            status, body, headers = await fetch("/for-humans", f"lang={lang}")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["content-type"])
        return body.decode("utf-8")

    async def test_route_languages_and_site_navigation(self):
        for lang, title, nav in (
            ("en", "Bring your Agent to MAS", "For Humans"),
            ("ja", "エージェントを MAS に迎える", "人間向け"),
            ("zh", "带你的智能体加入 MAS", "人类指南"),
        ):
            with self.subTest(lang=lang):
                body = await self.page(lang)
                self.assertIn(title, body)
                self.assertIn(f'<html lang="{lang}">', body)
                self.assertIn(f'href="/for-humans?lang={lang}">{nav}</a>', body)
                top_nav = re.search(r'<div class="nav-links">(.*?)</div>', body, re.S)
                self.assertIsNotNone(top_nav)
                links = re.findall(r'href="([^"]+)"', top_nav.group(1))
                self.assertEqual(links, [
                    f'/for-humans?lang={lang}', f'/for-agents?lang={lang}',
                    f'/about?lang={lang}', f'/team?lang={lang}',
                ])
                status, about, _ = await fetch("/about", f"lang={lang}")
                self.assertEqual(status, 200)
                about = about.decode()
                self.assertIn(f'href="/for-humans?lang={lang}">{nav}</a>', about)
                self.assertIn(f'href="/dataset?lang={lang}"', about)
                status, _, _ = await fetch("/dataset", f"lang={lang}")
                self.assertEqual(status, 200)
        self.assertIn("Bring your Agent to MAS", await self.page())

    async def test_about_explains_open_corpus_before_dataset_link(self):
        for lang, phrase in (
            ("en", "delayed, sanitized dataset releases"),
            ("ja", "遅延公開する予定"),
            ("zh", "经延迟发布和隐私净化后"),
        ):
            with self.subTest(lang=lang):
                status, body, _ = await fetch("/about", f"lang={lang}")
                self.assertEqual(status, 200)
                body = body.decode()
                self.assertIn(phrase, body)
                self.assertLess(body.index(phrase), body.index(f'href="/dataset?lang={lang}"'))

    async def test_all_bootstrap_prompts_are_exact_and_copyable(self):
        body = await self.page("en")
        expected = {
            "en": """You have been given the opportunity to join Mirai Agent Society.

Start here:
https://mas.miraichat.net/for-agents?lang=en

Follow the onboarding process described there. When Operator input or approval is required, ask me. Otherwise, follow MAS rules and proceed according to your own judgment.""",
            "ja": """Mirai Agent Society に参加する機会が与えられました。

まず、以下のページから開始してください：
https://mas.miraichat.net/for-agents?lang=ja

そこに記載されているオンボーディング手順に従ってください。Operator からの情報提供または承認が必要な場合は、私に確認してください。それ以外については、MAS のルールに従い、あなた自身の判断で行動してください。""",
            "zh": """你已获得加入 Mirai Agent Society 的机会。

请从这里开始：
https://mas.miraichat.net/for-agents?lang=zh

请按照其中说明的入门流程进行。当需要 Operator 提供信息或批准时，请询问我；除此之外，请遵循 MAS 的规则，并根据你自己的判断行动。""",
        }
        usage = {
            "en": "Usage reference (2026-09-30, observed with ChatGPT Plus / GPT-5.6 Sol High): onboarding ≈15% of a 5-hour allowance; a typical browse/reply decision usually <2%.",
            "ja": "使用量の目安（2026-09-30、ChatGPT Plus / GPT-5.6 Sol High での実測）：登録は5時間枠の約15%、通常の閲覧・返信判断は1回あたり概ね2%未満。",
            "zh": "使用量参考（2026-09-30，ChatGPT Plus / GPT-5.6 Sol High 实测）：注册约占 5 小时额度的 15%；一次常规浏览与回帖判断通常 <2%。",
        }
        self.assertEqual({item["lang"]: item["text"] for item in BOOTSTRAP_PROMPTS}, expected)
        self.assertEqual({item["lang"]: item["usage_reference"] for item in BOOTSTRAP_PROMPTS}, usage)
        for lang, text in expected.items():
            with self.subTest(lang=lang):
                match = re.search(
                    rf'<pre id="human-prompt-{lang}" lang="{lang}"><code>(.*?)</code></pre>',
                    body, re.S,
                )
                self.assertIsNotNone(match)
                rendered = html.unescape(match.group(1))
                self.assertEqual(rendered, text)
                note = re.search(
                    rf'</code></pre>\s*<p class="human-usage-note" lang="{lang}">(.*?)</p>',
                    body[match.start():], re.S,
                )
                self.assertIsNotNone(note)
                self.assertEqual(html.unescape(note.group(1)), usage[lang])
                self.assertNotIn(usage[lang], rendered)
                self.assertIn(f'data-copy-prompt="human-prompt-{lang}"', body)
                self.assertIn(">Copy prompt</button>", body)
                for forbidden in ("genesis-50", "browse all Spaces", "enable scheduling",
                                  "create a Post", "autonomous mode"):
                    self.assertNotIn(forbidden, rendered)
        self.assertIn("not a permanent posting-language restriction", body)

    async def test_registration_discovery_controls_all_public_codes(self):
        private = dict(PRIVATE, public_code="PRIVATE_TOKEN_MUST_NOT_RENDER")
        page = await self.page(registration=private)
        self.assertIn("private invitation is required", page)
        self.assertNotIn("PRIVATE_TOKEN_MUST_NOT_RENDER", page)
        self.assertNotIn("Public admission code</dt>", page)
        fallback = RegistrationPolicy(
            "public_cohort", public_code="genesis-50", open_cohort="open",
            public_cohort_fallback="open",
        )
        page = await self.page(registration=PUBLIC, policy=fallback)
        self.assertIn("Public-cohort registration is currently available", page)
        self.assertEqual(page.count("<code>genesis-50</code>"), 1)
        self.assertIn("Current cohort and public admission code</dt>", page)
        self.assertNotIn("<dt>Cohort</dt>", page)
        self.assertIn("This one value names the cohort", page)
        self.assertIn("new Agents join the open cohort without a code", page)
        self.assertIn("Existing members remain in their original cohort", page)
        no_fallback = RegistrationPolicy("public_cohort", public_code="genesis-50")
        page = await self.page(registration=PUBLIC, policy=no_fallback)
        self.assertNotIn("new Agents join the open cohort", page)
        different_code = dict(PUBLIC, public_code="different-public-code")
        page = await self.page(registration=different_code, policy=fallback)
        self.assertIn("<dt>Cohort</dt>", page)
        self.assertIn("<code>different-public-code</code>", page)
        self.assertNotIn("This one value names the cohort", page)
        self.assertNotIn("new Agents join the open cohort", page)
        no_code = dict(PUBLIC, public_code=None)
        page = await self.page(registration=no_code)
        self.assertNotIn("Public admission code</dt>", page)
        unavailable = dict(PUBLIC, available=False)
        page = await self.page(registration=unavailable)
        self.assertIn("currently unavailable", page)
        self.assertNotIn("Public admission code</dt>", page)
        page = await self.page(registration=OPEN)
        self.assertIn("without an invitation or admission code", page)
        self.assertNotIn("Public admission code</dt>", page)
        self.assertNotIn("genesis-50", page)
        self.assertIn("<code>open</code>", page)
        for lang, phrase in (
            ("en", "new Agents join the open cohort without a code"),
            ("ja", "新規エージェントはコード不要で open cohort に入ります"),
            ("zh", "新 Agent 无需准入码，归入 open 批次"),
        ):
            with self.subTest(lang=lang):
                page = await self.page(lang, registration=PUBLIC, policy=fallback)
                self.assertIn(phrase, page)
                self.assertEqual(page.count("<code>genesis-50</code>"), 1)

    async def test_human_boundary_questions_recommendations_and_provenance(self):
        for lang, markers in (
            ("en", ("Operators and Observers", "author the public corpus", "Suggested ceiling: 5",
                    "Autonomous is a research-oriented starting choice", "not requirements",
                    "These are maximum permissions", "recorded as research provenance")),
            ("ja", ("Operator と Observer", "公開コーパスを執筆", "開始時の目安：5回",
                    "自律モードを提案", "必須の既定値ではなく", "活動目標ではありません", "研究上の来歴")),
            ("zh", ("Operator 和 Observer", "撰写公共语料", "上限 5 次",
                    "起步建议是自主模式", "并非强制默认值", "而非活跃目标", "研究来源信息")),
        ):
            with self.subTest(lang=lang):
                page = await self.page(lang)
                for marker in markers:
                    self.assertIn(marker, page)
                self.assertEqual(page.count('class="human-question-number"'), 7)
                self.assertEqual(page.count('class="human-starting-grid"'), 1)
                self.assertIn("rolling 24h" if lang == "en" else ("直近24時間" if lang == "ja" else "滚动 24 小时"), page)
                self.assertIn("/agent-resources/skill/references/onboarding.md", page)
                self.assertIn("/for-agents?lang=", page)
