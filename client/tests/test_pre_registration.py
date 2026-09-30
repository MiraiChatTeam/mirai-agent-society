import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from client.mas_client.agent_package_update import PackageResponse
from client.mas_client.local_state import StateValidationError
from client.mas_client.operator_onboarding import SimpleOperatorAnswers, normalize_operator_onboarding
from client.mas_client.pre_registration import (
    DefiniteAdmissionRejection,
    RuntimeReadinessChecks,
    check_resident_runtime_readiness,
    fetch_authoritative_registration,
    register_new_resident_after_readiness,
)


class PreRegistrationReadinessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "resident"
        self.config = normalize_operator_onboarding(
            SimpleOperatorAnswers(), resolved_schedule_mode="scheduled_local"
        ).config

    def checks(self, **overrides):
        values = {
            "future_session_can_access": lambda root: root == self.root,
            "authoritative_https_reachable": lambda origin: origin == "https://mas.example.org",
            "client_runtime_can_execute": lambda: True,
            "execution_mechanism_can_access": lambda mode, root, tools: (
                mode == "scheduled_local" and root == self.root and tools == self.config["tools"]
            ),
        }
        values.update(overrides)
        return RuntimeReadinessChecks(**values)

    def registration_attempt(self, checks):
        calls = {"approval": 0, "key": 0, "register": 0}

        def approve(config, readiness):
            calls["approval"] += 1
            return True

        def key(root):
            calls["key"] += 1
            return "opaque-key-handle"

        def register(key_handle, config):
            calls["register"] += 1
            return {"agent_id": "server-issued"}

        try:
            result = register_new_resident_after_readiness(
                state_root=self.root,
                authoritative_origin="https://mas.example.org",
                operator_config=self.config,
                checks=checks,
                obtain_final_approval=approve,
                generate_and_persist_permanent_key=key,
                register_once=register,
            )
        except StateValidationError:
            result = None
        return result, calls

    def test_minimum_scoped_permissions_satisfy_readiness(self) -> None:
        result = check_resident_runtime_readiness(
            state_root=self.root,
            authoritative_origin="https://mas.example.org",
            operator_config=self.config,
            checks=self.checks(),
        )
        self.assertTrue(result.ready)
        self.assertEqual(result.missing, ())
        self.assertEqual(self.root.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.root / "keys").stat().st_mode & 0o777, 0o700)
        self.assertEqual({item.name for item in self.root.iterdir()}, {"keys"})

    def test_inaccessible_or_contaminated_state_root_blocks_registration(self) -> None:
        self.root.mkdir()
        (self.root / "identity.json").write_text("existing", encoding="utf-8")
        result, calls = self.registration_attempt(self.checks())
        self.assertIsNone(result)
        self.assertEqual(calls, {"approval": 0, "key": 0, "register": 0})

    def test_future_wake_inability_blocks_before_key_and_capacity(self) -> None:
        result, calls = self.registration_attempt(
            self.checks(future_session_can_access=lambda root: False)
        )
        self.assertIsNone(result)
        self.assertEqual(calls["key"], 0)
        self.assertEqual(calls["register"], 0)  # no admission endpoint call/capacity consumption

    def test_unavailable_or_non_https_origin_blocks_registration(self) -> None:
        result, calls = self.registration_attempt(
            self.checks(authoritative_https_reachable=lambda origin: False)
        )
        self.assertIsNone(result)
        self.assertEqual(calls["register"], 0)
        readiness = check_resident_runtime_readiness(
            state_root=Path(self.temporary.name) / "other",
            authoritative_origin="http://mas.example.org",
            operator_config=self.config,
            checks=self.checks(future_session_can_access=lambda root: True),
        )
        self.assertFalse(readiness.ready)
        self.assertIn("HTTPS access to the authoritative MAS origin", readiness.missing)

    def test_client_or_execution_mechanism_failure_blocks_registration(self) -> None:
        for replacement in (
            {"client_runtime_can_execute": lambda: False},
            {"execution_mechanism_can_access": lambda mode, root, tools: False},
        ):
            with self.subTest(replacement=tuple(replacement)):
                root = Path(self.temporary.name) / tuple(replacement)[0]
                original = self.root
                self.root = root
                try:
                    result, calls = self.registration_attempt(self.checks(**replacement))
                finally:
                    self.root = original
                self.assertIsNone(result)
                self.assertEqual(calls["key"], 0)
                self.assertEqual(calls["register"], 0)

    def test_final_approval_precedes_key_and_registration(self) -> None:
        order = []
        result = register_new_resident_after_readiness(
            state_root=self.root,
            authoritative_origin="https://mas.example.org",
            operator_config=self.config,
            checks=self.checks(),
            obtain_final_approval=lambda config, readiness: order.append("approval") or True,
            generate_and_persist_permanent_key=lambda root: order.append("key") or "opaque-key-handle",
            register_once=lambda key, config: order.append("register") or {"agent_id": "server-issued"},
        )
        self.assertEqual(order, ["approval", "key", "register"])
        self.assertEqual(result.registration, {"agent_id": "server-issued"})

    def test_exhausted_cohort_refreshes_and_reuses_pending_key_and_approval(self) -> None:
        calls = {"readiness": 0, "approval": 0, "key": 0, "discovery": 0, "submit": 0}
        key_handle = object()
        public = {
            "mode": "public_cohort", "available": True, "cohort": "genesis-50",
            "code_required": True, "request_field": "admission_code", "public_code": "genesis-50",
        }
        opened = {
            "mode": "open", "available": True, "cohort": "after-genesis",
            "code_required": False, "request_field": None, "public_code": None,
        }
        def future_access(root):
            calls["readiness"] += 1
            return root == self.root
        def approve(config, readiness):
            calls["approval"] += 1
            return True
        def make_key(root):
            calls["key"] += 1
            return key_handle
        def discovery(origin):
            self.assertEqual(origin, "https://mas.example.org")
            calls["discovery"] += 1
            return public if calls["discovery"] == 1 else opened
        def submit(key, config, admission):
            self.assertIs(key, key_handle)
            self.assertIs(config, self.config)
            calls["submit"] += 1
            if calls["submit"] == 1:
                self.assertEqual(admission, public)
                raise DefiniteAdmissionRejection(403, "exhausted_public_cohort")
            self.assertEqual(admission, opened)
            return {"agent_id": "server-issued"}
        result = register_new_resident_after_readiness(
            state_root=self.root, authoritative_origin="https://mas.example.org",
            operator_config=self.config, checks=self.checks(future_session_can_access=future_access),
            obtain_final_approval=approve, generate_and_persist_permanent_key=make_key,
            fetch_registration=discovery, register_with_discovery=submit,
        )
        self.assertIs(result.key_handle, key_handle)
        self.assertEqual(result.registration, {"agent_id": "server-issued"})
        self.assertEqual(calls, {"readiness": 1, "approval": 1, "key": 1, "discovery": 2, "submit": 2})

    def test_ambiguous_and_invalid_results_never_auto_retry(self) -> None:
        public = {
            "mode": "public_cohort", "available": True, "cohort": "genesis-50",
            "code_required": True, "request_field": "admission_code", "public_code": "genesis-50",
        }
        for failure in (TimeoutError("ambiguous"), DefiniteAdmissionRejection(403, "invalid_public_cohort"),
                        DefiniteAdmissionRejection(422, "exhausted_public_cohort")):
            with self.subTest(failure=str(failure)):
                root = Path(self.temporary.name) / f"attempt-{len(list(Path(self.temporary.name).iterdir()))}"
                calls = {"fetch": 0, "submit": 0}
                def discovery(origin):
                    calls["fetch"] += 1
                    return public
                def submit(key, config, admission):
                    calls["submit"] += 1
                    raise failure
                with self.assertRaises(type(failure)):
                    register_new_resident_after_readiness(
                        state_root=root, authoritative_origin="https://mas.example.org",
                        operator_config=self.config,
                        checks=RuntimeReadinessChecks(
                            future_session_can_access=lambda candidate: candidate == root,
                            authoritative_https_reachable=lambda origin: True,
                            client_runtime_can_execute=lambda: True,
                            execution_mechanism_can_access=lambda mode, candidate, tools: True,
                        ),
                        obtain_final_approval=lambda config, readiness: True,
                        generate_and_persist_permanent_key=lambda candidate: object(),
                        fetch_registration=discovery, register_with_discovery=submit,
                    )
                self.assertEqual(calls, {"fetch": 1, "submit": 1})

    def test_authoritative_discovery_requires_exact_https_response(self) -> None:
        opened = {
            "mode": "open", "available": True, "cohort": "after-genesis",
            "code_required": False, "request_field": None, "public_code": None,
        }
        url = "https://mas.example.org/api/v1/agent-package"
        with patch("client.mas_client.pre_registration.fetch_public_package_url",
                   return_value=PackageResponse(200, url, {"registration": opened})):
            self.assertEqual(fetch_authoritative_registration("https://mas.example.org"), opened)
        for response in (
            PackageResponse(302, url, None, redirected=True),
            PackageResponse(200, "https://elsewhere.example/api/v1/agent-package", {"registration": opened}),
            PackageResponse(200, url, {"registration": {**opened, "code_required": True}}),
        ):
            with self.subTest(response=response), patch(
                "client.mas_client.pre_registration.fetch_public_package_url", return_value=response
            ), self.assertRaises(StateValidationError):
                fetch_authoritative_registration("https://mas.example.org")
        with self.assertRaises(StateValidationError):
            fetch_authoritative_registration("http://mas.example.org")

    def test_denied_final_approval_creates_no_key_or_registration(self) -> None:
        calls = []
        with self.assertRaisesRegex(StateValidationError, "not explicitly approved"):
            register_new_resident_after_readiness(
                state_root=self.root,
                authoritative_origin="https://mas.example.org",
                operator_config=self.config,
                checks=self.checks(),
                obtain_final_approval=lambda config, readiness: False,
                generate_and_persist_permanent_key=lambda root: calls.append("key"),
                register_once=lambda key, config: calls.append("register"),
            )
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
