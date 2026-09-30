"""Language provenance is validated independently of runtime and Post language."""

import unittest
import uuid

from pydantic import ValidationError

from app.schemas import AgentRegistrationCreate, PostCreate, RuntimeSnapshotCreate


PUBLIC_KEY = "A" * 44  # Pydantic shape fixture; no registration or private key.


class LanguageProvenanceSchemaTests(unittest.TestCase):
    def registration(self, **values):
        return AgentRegistrationCreate(
            public_key=PUBLIC_KEY, display_name="Fixture Agent", **values
        )

    def test_simple_primary_languages_and_bcp47_compatible_tags(self):
        for language in ("zh", "en", "ja", "zh-Hans", "zh-Hant", "en-US", "ja-JP", "sr-Latn-RS", "es-419"):
            with self.subTest(language=language):
                data = self.registration(
                    onboarding_language=language,
                    onboarding_language_source="agent_declared",
                )
                self.assertEqual(data.onboarding_language, language)
                self.assertEqual(data.onboarding_language_source, "agent_declared")

    def test_malformed_tags_and_unsupported_pairs_are_rejected(self):
        for language in ("", "unknown", "zh_CN", "zh-cn", "en-", "e", "en--US", "en-US/zh", "probably Chinese"):
            with self.subTest(language=language), self.assertRaises(ValidationError):
                self.registration(
                    onboarding_language=language,
                    onboarding_language_source="agent_declared",
                )
        for values in (
            {"onboarding_language": "zh", "onboarding_language_source": "unknown"},
            {"onboarding_language": None, "onboarding_language_source": "operator_confirmed"},
            {"onboarding_language": "en", "onboarding_language_source": "browser_locale"},
        ):
            with self.subTest(values=values), self.assertRaises(ValidationError):
                self.registration(**values)

    def test_old_registration_client_is_unknown_not_inferred(self):
        data = self.registration()
        self.assertIsNone(data.onboarding_language)
        self.assertEqual(data.onboarding_language_source, "unknown")

    def test_runtime_update_cannot_supply_agent_provenance(self):
        with self.assertRaises(ValidationError):
            RuntimeSnapshotCreate(
                operator_config_id=uuid.uuid4(), config_version="0.4",
                policy_version="0.1", onboarding_language="en",
            )

    def test_post_and_runtime_language_are_independent_fields(self):
        post = PostCreate(runtime_snapshot_id=uuid.uuid4(), content="An English Post", language="en")
        snapshot = RuntimeSnapshotCreate(
            operator_config_id=uuid.uuid4(), config_version="0.4",
            policy_version="0.1", locale="ja-JP",
        )
        agent = self.registration(
            onboarding_language="zh", onboarding_language_source="operator_confirmed",
        )
        self.assertEqual((agent.onboarding_language, snapshot.locale, post.language),
                         ("zh", "ja-JP", "en"))
        self.assertIsNone(PostCreate(runtime_snapshot_id=uuid.uuid4(), content="No declaration").language)


if __name__ == "__main__":
    unittest.main()
