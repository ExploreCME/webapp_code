import os
from pathlib import Path
import subprocess
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

class CheckoutTests(unittest.TestCase):
    def setUp(self):
        with patch("os.makedirs"):
            import display_database
        self.backend = display_database
        self.app = display_database.app
        config = self.app.config.copy()
        self.addCleanup(lambda: self.app.config.update(config))
        self.app.config.update(TESTING=True, SECRET_KEY="checkout-test-only")
        self.client = self.app.test_client()
        self.client.set_cookie("__session", "test-clerk-token")
        db = MagicMock()
        db.cursor.return_value.__enter__.return_value.fetchone.return_value = {
            "has_paid": 0, "is_faculty": "N",
        }
        for patcher in (
            patch.object(display_database, "MAINTENANCE_MODE", False),
            patch.object(display_database, "get_db_connection", return_value=db),
            patch.object(display_database, "decode_clerk_token", return_value={"sub": "checkout-user"}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.create = self.enterContext(patch.object(display_database.stripe.checkout.Session, "create"))

    def assert_unavailable(self, response):
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.location, "/paxcel")
        with self.client.session_transaction() as session:
            self.assertIn(
                ("error", "Checkout is temporarily unavailable. Please try again later."),
                session["_flashes"],
            )

    def test_missing_secret_does_not_call_stripe(self):
        with patch.dict(os.environ, {}, clear=True):
            with patch.object(self.backend.stripe, "api_key", os.getenv("STRIPE_SECRET_KEY")):
                self.assert_unavailable(self.client.post("/create-checkout-session"))
        self.create.assert_not_called()

    def test_secret_is_initialized_from_server_environment(self):
        for value in (None, "test-server-key"):
            with self.subTest(configured=value is not None):
                env = dict(os.environ, FLASK_ENV="testing")
                env.pop("STRIPE_SECRET_KEY", None)
                if value:
                    env["STRIPE_SECRET_KEY"] = value
                subprocess.run(
                    [sys.executable, "-c", (
                        "import os; from unittest.mock import patch\n"
                        "with patch('dotenv.load_dotenv'), patch('os.makedirs'):\n"
                        "    import display_database\n"
                        "assert display_database.stripe.api_key == os.getenv('STRIPE_SECRET_KEY')\n"
                    )],
                    cwd=Path(__file__).resolve().parents[1], env=env,
                    capture_output=True, text=True, check=True, timeout=15,
                )

    def test_success_redirects_to_stripe_checkout(self):
        self.create.return_value = SimpleNamespace(url="https://checkout.stripe.com/test-checkout")
        with patch.object(self.backend.stripe, "api_key", "test-server-key"):
            response = self.client.post("/create-checkout-session")
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.location, self.create.return_value.url)
        self.create.assert_called_once()
        args = self.create.call_args.kwargs
        self.assertEqual(args["client_reference_id"], "checkout-user")
        self.assertEqual(args["mode"], "subscription")
        self.assertEqual(args["line_items"], [
            {"price": "price_1U8XG31IrD5jMp4rgoj4dYGg", "quantity": 1},
        ])

    def test_misconfigured_secret_and_api_errors_are_safe(self):
        for error in (
            self.backend.stripe.AuthenticationError("private Stripe diagnostic"),
            RuntimeError("private internal diagnostic"),
        ):
            with self.subTest(error=type(error).__name__):
                self.create.side_effect = error
                with patch.object(self.backend.stripe, "api_key", "invalid-server-key"):
                    response = self.client.post("/create-checkout-session")
                self.assert_unavailable(response)
                self.assertNotIn("private", response.text)
                with self.client.session_transaction() as session:
                    self.assertNotIn("private", str(session["_flashes"]))

    def test_missing_secret_is_logged(self):
        with patch.object(self.backend.stripe, "api_key", None):
            with self.assertLogs(self.app.logger, level="ERROR") as logs:
                self.assert_unavailable(self.client.post("/create-checkout-session"))
        self.assertIn("STRIPE_SECRET_KEY missing/empty", "\n".join(logs.output))

    def test_token_without_sub_asks_user_to_sign_in_again(self):
        self.backend.decode_clerk_token.side_effect = [{"sub": "checkout-user"}, {}]
        with patch.object(self.backend.stripe, "api_key", "test-server-key"):
            with self.assertLogs(self.app.logger, level="WARNING") as logs:
                response = self.client.post("/create-checkout-session")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.location, "/paxcel")
        self.assertIn("missing sub", "\n".join(logs.output))
        with self.client.session_transaction() as session:
            self.assertIn(("error", "Please sign in again and retry checkout."), session["_flashes"])
        self.create.assert_not_called()

    def test_stripe_error_diagnostics_are_logged_without_secret(self):
        self.create.side_effect = self.backend.stripe.error.InvalidRequestError(
            "No such price", param="line_items[0][price]", code="resource_missing",
        )
        with patch.object(self.backend.stripe, "api_key", "sk_live_secretvalue"):
            with self.assertLogs(self.app.logger, level="INFO") as logs:
                self.assert_unavailable(self.client.post("/create-checkout-session"))
        output = "\n".join(logs.output)
        self.assertIn("livemode_guess=True", output)
        self.assertIn("price_id=price_1U8XG31IrD5jMp4rgoj4dYGg", output)
        self.assertIn("type=InvalidRequestError code=resource_missing param=line_items[0][price]", output)
        self.assertNotIn("secretvalue", output)
        with self.client.session_transaction() as session:
            self.assertNotIn("No such price", str(session["_flashes"]))

    def test_institutional_access_does_not_need_api_key(self):
        with patch.object(self.backend.stripe, "api_key", None):
            response = self.client.get("/institutional-access")
        self.assertEqual(response.status_code, 303)
        self.assertEqual(
            response.location,
            "https://buy.stripe.com/28E00b3adbt58IR0hvdfG01?client_reference_id=checkout-user",
        )
        self.create.assert_not_called()

    def test_signed_out_checkout_still_requires_login(self):
        self.client.delete_cookie("__session")
        response = self.client.post("/create-checkout-session")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.location, "/paxcel")
        self.create.assert_not_called()


if __name__ == "__main__":
    unittest.main()
