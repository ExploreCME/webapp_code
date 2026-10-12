import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from flask import render_template_string

with patch("os.makedirs"):
    import display_database
    import flask_app


class EntitlementTests(unittest.TestCase):
    def setUp(self):
        self.app = display_database.app
        config = self.app.config.copy()
        self.addCleanup(lambda: self.app.config.update(config))
        self.app.config.update(TESTING=True, SECRET_KEY="entitlement-test-only")
        self.client = self.app.test_client()
        self.client.set_cookie("__session", "test-clerk-token")
        self.user = {"has_paid": 1, "is_faculty": "N", "accepted_eula": 1}
        self.db = MagicMock()
        self.db.cursor.return_value.__enter__.return_value = self.db.cursor.return_value
        self.db.cursor.return_value.fetchone.side_effect = lambda: self.user
        self.db.cursor.return_value.fetchall.return_value = []
        db_patch = patch.object(flask_app, "get_db", return_value=self.db)
        db_patch.start()
        self.addCleanup(db_patch.stop)
        for target, value in (
            ("MAINTENANCE_MODE", False),
            ("get_mysql_db", self.db),
            ("get_db_connection", self.db),
            ("decode_clerk_token", {"sub": "paid-user"}),
        ):
            if target == "MAINTENANCE_MODE":
                patcher = patch.object(display_database, target, value)
            else:
                patcher = patch.object(display_database, target, return_value=value)
            mock = patcher.start()
            self.addCleanup(patcher.stop)
            if target == "decode_clerk_token":
                self.decode = mock
        self.stats_patch = patch.object(
            display_database, "fetch_data_for_dashboard", return_value=(0, [], [])
        )
        self.stats_patch.start()
        self.addCleanup(self.stats_patch.stop)
        for module in (display_database, flask_app):
            patcher = patch.object(module, "render_template", side_effect=self.render_status)
            patcher.start()
            self.addCleanup(patcher.stop)
        with self.client.session_transaction() as session:
            session.update(username="paid-user", accepted_eula=True)
        with self.app.app_context():
            display_database.cache.clear()

    @staticmethod
    def render_status(template, **context):
        return render_template_string(
            "{{ is_premium }}:{{ is_faculty }}", **context
        )

    def seed_stale_status(self, premium=False):
        with self.client.session_transaction() as session:
            session.update(is_premium=premium, is_faculty=True, last_db_sync=10**12)

    def test_page_loads_restore_paid_status_despite_recent_session_sync(self):
        for path in ("/paxcel", "/db_dashboard", "/dashboard", "/home"):
            with self.subTest(path=path):
                self.seed_stale_status()
                self.db.cursor.return_value.execute.reset_mock()
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.text, "True:False")
                queries = [
                    call.args[0] for call in self.db.cursor.return_value.execute.call_args_list
                ]
                self.assertEqual(sum("SELECT has_paid, is_faculty" in q for q in queries), 1)
                with self.client.session_transaction() as session:
                    self.assertTrue(session["is_premium"])
                    self.assertFalse(session["is_faculty"])
                    self.assertNotIn("last_db_sync", session)

    def test_paid_access_survives_cache_session_reset_and_repeated_login(self):
        for _ in range(2):
            self.client.get("/logout")
            with self.app.app_context():
                display_database.cache.clear()
            with self.client.session_transaction() as session:
                session.clear()
            self.client.set_cookie("__session", "test-clerk-token")
            self.assertEqual(self.client.get("/db_dashboard").text, "True:False")
            self.assertEqual(self.client.get("/paxcel").text, "True:False")
            self.assertEqual(self.client.get("/dashboard").text, "True:False")

    def test_guest_session_remains_nonpremium_without_clerk_cookie(self):
        self.client.delete_cookie("__session")
        with self.client.session_transaction() as session:
            session.update(username="guest_sample", is_premium=True, is_faculty=True)
        self.assertEqual(self.client.get("/paxcel").text, "False:False")
        with self.client.session_transaction() as session:
            self.assertEqual(session["username"], "guest_sample")
            self.assertFalse(session["is_premium"])

    def test_revocation_overrides_stale_premium_session(self):
        self.seed_stale_status(premium=True)
        self.user["has_paid"] = 0
        response = self.client.get("/dashboard")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.endswith("/db_dashboard"))
        self.assertEqual(self.client.get("/db_dashboard").text, "False:False")

    def test_missing_user_is_nonpremium(self):
        self.seed_stale_status(premium=True)
        self.user = None
        self.assertEqual(self.client.get("/paxcel").text, "False:False")

    def test_faculty_values_and_changes_are_refreshed(self):
        for value in (1, " Y ", "YES", "true", 0, "N", None):
            with self.subTest(value=value):
                self.user["is_faculty"] = value
                expected = value in (1, " Y ", "YES", "true")
                self.assertEqual(self.client.get("/db_dashboard").text, f"True:{expected}")

    def test_changed_clerk_user_does_not_inherit_paid_status(self):
        self.seed_stale_status(premium=True)
        self.decode.return_value = {"sub": "other-user"}
        self.user["has_paid"] = 0
        self.assertEqual(self.client.get("/paxcel").text, "False:False")
        self.db.cursor.return_value.execute.assert_called_with(
            "SELECT has_paid, is_faculty FROM users WHERE clerk_id = %s", ("other-user",)
        )
        with self.client.session_transaction() as session:
            self.assertEqual(session["username"], "other-user")
            self.assertNotIn("accepted_eula", session)

    def test_missing_or_invalid_cookie_never_uses_stale_entitlement(self):
        for invalid in (False, True):
            with self.subTest(invalid=invalid):
                self.seed_stale_status(premium=True)
                if invalid:
                    self.client.set_cookie("__session", "invalid")
                    self.decode.side_effect = ValueError("Invalid token")
                else:
                    self.client.delete_cookie("__session")
                self.assertEqual(self.client.get("/paxcel").text, "False:False")
                self.assertEqual(self.client.get("/db_dashboard").status_code, 302)

    def test_database_failure_does_not_grant_cached_premium(self):
        self.seed_stale_status(premium=True)
        self.db.cursor.return_value.execute.side_effect = RuntimeError("Database unavailable")
        self.assertEqual(self.client.get("/db_dashboard").status_code, 302)
        self.assertEqual(self.client.get("/paxcel").text, "False:False")

    def test_entitlement_connection_is_released_before_route_database_work(self):
        self.user["has_paid"] = 0
        def retrieve(checkout_id):
            self.db.close.assert_called_once()
            return SimpleNamespace(payment_status="unpaid")
        with patch.object(display_database.stripe.checkout.Session, "retrieve", side_effect=retrieve):
            self.client.get("/payment-success?session_id=test-checkout")

    def test_entitlement_connection_is_released_on_database_error(self):
        self.db.cursor.return_value.execute.side_effect = RuntimeError("Query failed")
        self.client.get("/db_dashboard")
        self.db.close.assert_called_once()

    def test_dashboard_stats_cache_does_not_cache_entitlements(self):
        self.stats_patch.stop()
        self.db.cursor.return_value.fetchone.side_effect = lambda: (
            self.user if "has_paid" in self.db.cursor.return_value.execute.call_args.args[0]
            else {"c": 25}
        )
        self.assertEqual(self.client.get("/paxcel").text, "True:False")
        self.user["has_paid"] = 0
        self.assertEqual(self.client.get("/paxcel").text, "False:False")
        queries = [call.args[0] for call in self.db.cursor.return_value.execute.call_args_list]
        self.assertEqual(sum("SELECT has_paid" in q for q in queries), 2)
        self.assertEqual(sum("SELECT COUNT(id)" in q for q in queries), 1)

    def test_payment_does_not_grant_session_premium_before_commit(self):
        self.user["has_paid"] = 0
        checkout = SimpleNamespace(
            payment_status="paid", client_reference_id="paid-user", customer="test-customer"
        )
        self.db.commit.side_effect = RuntimeError("Commit failed")
        with patch.object(display_database.stripe.checkout.Session, "retrieve", return_value=checkout):
            self.assertEqual(self.client.get("/payment-success?session_id=test-checkout").status_code, 302)
        with self.client.session_transaction() as session:
            self.assertFalse(session["is_premium"])
        self.assertEqual(self.client.get("/db_dashboard").text, "False:False")

    def test_payment_for_another_user_cannot_grant_current_user_premium(self):
        self.user["has_paid"] = 0
        checkout = SimpleNamespace(
            payment_status="paid", client_reference_id="another-user", customer="test-customer"
        )
        with patch.object(display_database.stripe.checkout.Session, "retrieve", return_value=checkout):
            self.client.get("/payment-success?session_id=test-checkout")
        self.db.commit.assert_not_called()
        self.assertEqual(self.client.get("/db_dashboard").text, "False:False")

    def test_successful_payment_restores_premium_from_committed_database(self):
        self.user["has_paid"] = 0
        checkout = SimpleNamespace(
            payment_status="paid", client_reference_id="paid-user", customer="test-customer"
        )
        self.db.commit.side_effect = lambda: self.user.update(has_paid=1)
        with patch.object(display_database.stripe.checkout.Session, "retrieve", return_value=checkout):
            response = self.client.get("/payment-success?session_id=test-checkout")
        self.assertTrue(response.location.endswith("/db_dashboard"))
        self.assertEqual(self.client.get("/db_dashboard").text, "True:False")

    def test_webhook_updates_are_visible_on_next_request(self):
        self.user["has_paid"] = 0
        self.assertEqual(self.client.get("/paxcel").text, "False:False")
        event = {
            "type": "checkout.session.completed",
            "data": {"object": SimpleNamespace(client_reference_id="paid-user", customer="test-customer")},
        }
        self.db.commit.side_effect = lambda: self.user.update(has_paid=1)
        with patch.object(display_database.stripe.Webhook, "construct_event", return_value=event):
            self.assertEqual(self.client.post("/webhook").status_code, 200)
        self.assertEqual(self.client.get("/paxcel").text, "True:False")

    def test_cancellation_webhook_revokes_stale_session_on_next_request(self):
        self.assertEqual(self.client.get("/paxcel").text, "True:False")
        event = {
            "type": "customer.subscription.deleted",
            "data": {"object": SimpleNamespace(customer="test-customer")},
        }
        self.db.commit.side_effect = lambda: self.user.update(has_paid=0)
        with patch.object(display_database.stripe.Webhook, "construct_event", return_value=event):
            self.assertEqual(self.client.post("/webhook").status_code, 200)
        self.assertEqual(self.client.get("/paxcel").text, "False:False")

    def test_clerk_profile_sync_preserves_paid_record(self):
        self.assertEqual(self.client.post("/sync_clerk_user", json={
            "clerk_id": "paid-user", "first_name": "Test",
        }).status_code, 200)
        self.assertEqual(self.client.post("/clerk-webhook", json={
            "type": "user.updated", "data": {"id": "paid-user", "first_name": "Test"},
        }).status_code, 200)
        self.assertEqual(self.client.get("/paxcel").text, "True:False")
        queries = [call.args[0] for call in self.db.cursor.return_value.execute.call_args_list]
        self.assertFalse(any("SET has_paid" in query for query in queries))


if __name__ == "__main__":
    unittest.main()
