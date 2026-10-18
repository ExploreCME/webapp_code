"""Development layout previews must not unlock production or PAXcel access."""

import os
import unittest
from unittest.mock import MagicMock, patch

from flask import g

with patch("os.makedirs"):
    import display_database

from rnxcel_storage import RNXcelStore


class RNXcelDevBypassTests(unittest.TestCase):
    def setUp(self):
        self.app = display_database.app
        previous = self.app.config.copy()
        self.addCleanup(lambda: (self.app.config.clear(), self.app.config.update(previous)))
        self.storage = MagicMock(spec=RNXcelStore)
        self.storage.has_entitlement.return_value = True
        self.storage.has_consent.return_value = True
        self.app.config.update(
            TESTING=True, DEBUG=False, SECRET_KEY="nursing-preview-test-only",
            RNXCEL_DEV_BYPASS=False, RNXCEL_TEST_STORE=self.storage,
        )
        self.client = self.app.test_client()
        self._patch(patch.dict(os.environ, RNXCEL_DEV_BYPASS="0"))
        self._patch(patch.object(display_database, "MAINTENANCE_MODE", False))
        self.decoder = self._patch(patch.object(
            display_database, "decode_clerk_token", return_value={"sub": "verified-owner"},
        ))
        self.real_storage = self._patch(patch(
            "rnxcel_routes.RNXcelStore", return_value=self.storage,
        ))
        self.database = self._patch(patch.object(
            display_database, "get_db_connection",
            side_effect=AssertionError("Layout preview must not open the database"),
        ))

    def _patch(self, patcher):
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def test_disabled_bypass_preserves_auth_entitlement_and_consent(self):
        response = self.client.get("/rnxcel/dashboard")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/rnxcel/sign-in", response.location)
        self.client.set_cookie("__session", "signed-token")
        self.storage.has_entitlement.return_value = False
        self.assertEqual(self.client.get("/rnxcel/dashboard").status_code, 403)
        self.storage.has_consent.assert_not_called()
        self.storage.has_entitlement.assert_called_with("verified-owner")
        self.storage.has_entitlement.return_value = True
        self.storage.has_consent.return_value = False
        response = self.client.get("/rnxcel/dashboard")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/rnxcel/consent", response.location)
        self.storage.has_consent.assert_called_with("verified-owner")
        self.storage.published.assert_not_called()

    def test_unsigned_layouts_and_navigation_need_no_storage(self):
        self.app.config["RNXCEL_DEV_BYPASS"] = True
        for path in (
            "/rnxcel", "/rnxcel/dashboard", "/rnxcel/practice",
            "/rnxcel/cases", "/rnxcel/bank", "/rnxcel/attempts", "/rnxcel/progress",
        ):
            with self.subTest(path=path), self.client:
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(g.rnxcel_owner, "dev-rnxcel")
                self.assertIn('href="/rnxcel/dashboard"', response.text)
                self.assertNotIn("@clerk/clerk-js", response.text)
        self.client.set_cookie("__session", "unverified-preview-cookie")
        dashboard = self.client.get("/rnxcel/dashboard")
        for path in ("practice", "cases", "bank", "attempts", "progress"):
            self.assertIn(f'href="/rnxcel/{path}"', dashboard.text)
        self.assertIn("disabled", self.client.get("/rnxcel/practice").text)
        self.decoder.assert_not_called()
        self.assertEqual(self.storage.mock_calls, [])
        self.real_storage.assert_not_called()
        self.database.assert_not_called()

    def test_environment_flag_requires_debug_or_testing(self):
        self.app.config.pop("RNXCEL_DEV_BYPASS")
        with patch.dict(os.environ, RNXCEL_DEV_BYPASS="1"):
            for testing, debug, expected in (
                (False, False, 302), (False, True, 200), (True, False, 200),
            ):
                with self.subTest(testing=testing, debug=debug):
                    self.app.config.update(TESTING=testing, DEBUG=debug)
                    self.assertEqual(
                        self.client.get("/rnxcel/dashboard").status_code, expected,
                    )

    def test_config_flag_cannot_bypass_production_checks(self):
        self.app.config.update(TESTING=False, DEBUG=False, RNXCEL_DEV_BYPASS=True)
        self.assertEqual(self.client.get("/rnxcel/dashboard").status_code, 302)
        self.client.set_cookie("__session", "signed-token")
        self.storage.has_entitlement.return_value = False
        self.assertEqual(self.client.get("/rnxcel/dashboard").status_code, 403)
        self.storage.has_entitlement.assert_called_with("verified-owner")

    def test_flag_is_explicit_and_config_can_override_environment(self):
        with patch.dict(os.environ, RNXCEL_DEV_BYPASS="1"):
            for value in (False, "0", "false", "yes"):
                with self.subTest(value=value):
                    self.app.config["RNXCEL_DEV_BYPASS"] = value
                    self.assertEqual(self.client.get("/rnxcel/dashboard").status_code, 302)
        self.app.config.pop("RNXCEL_DEV_BYPASS")
        self.assertEqual(self.client.get("/rnxcel/dashboard").status_code, 302)

    def test_preview_rejects_writes_and_has_no_saved_attempts(self):
        self.app.config["RNXCEL_DEV_BYPASS"] = True
        attempt = "/rnxcel/attempts/00000000-0000-0000-0000-000000000001"
        for path in ("/rnxcel/consent", "/rnxcel/attempts", attempt + "/answer"):
            with self.subTest(path=path):
                self.assertEqual(self.client.post(path).status_code, 405)
        for suffix in ("", "/results", "/review"):
            self.assertEqual(self.client.get(attempt + suffix).status_code, 404)
        self.assertEqual(self.storage.mock_calls, [])
        self.assertEqual(self.client.get("/rnxcel/practice/not-a-format").status_code, 404)

    def test_preview_is_request_scoped_and_does_not_unlock_paxcel(self):
        baseline = self.client.get("/student_dashboard")
        self.assertEqual(baseline.status_code, 302)
        with self.client.session_transaction() as session:
            previous = dict(session)
        self.app.config["RNXCEL_DEV_BYPASS"] = True
        self.assertEqual(self.client.get("/rnxcel/dashboard").status_code, 200)
        with self.client.session_transaction() as session:
            for key in ("username", "has_paid", "is_premium", "accepted_eula"):
                self.assertEqual(session.get(key), previous.get(key))
        with self.client:
            response = self.client.get("/student_dashboard")
            self.assertEqual(response.status_code, baseline.status_code)
            self.assertEqual(response.location, baseline.location)
            self.assertFalse(hasattr(g, "rnxcel_owner"))
        self.app.config["RNXCEL_DEV_BYPASS"] = False
        response = self.client.get("/rnxcel/dashboard")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/rnxcel/sign-in", response.location)

    def test_preview_does_not_bypass_global_maintenance(self):
        self.app.config["RNXCEL_DEV_BYPASS"] = True
        with patch.object(display_database, "MAINTENANCE_MODE", True):
            self.assertEqual(self.client.get("/rnxcel/dashboard").status_code, 503)
        self.assertEqual(self.storage.mock_calls, [])
