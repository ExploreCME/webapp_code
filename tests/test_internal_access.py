"""Internal credentials must never authorize production or persist in sessions."""

import json
import os
from pathlib import Path
import secrets
import unittest
from unittest.mock import patch

from flask import Blueprint, Flask, abort, render_template

from internal_access import (
    ENVIRONMENT_KEYS, approved_request, enabled, init_internal_access,
)


class InternalAccessTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.token = secrets.token_urlsafe(32)

    def app(self, **environment):
        os.environ.update(environment)
        app = Flask(__name__, template_folder=str(
            Path(__file__).resolve().parents[1] / "templates" / "template standardization"
        ))
        app.config.update(TESTING=True, SECRET_KEY="internal-mode-tests-only")
        init_internal_access(app)
        nursing = Blueprint("rnxcel", __name__)

        @nursing.route("/protected")
        def protected():
            if not approved_request():
                abort(403)
            return render_template("internal_access_banner.html")

        app.register_blueprint(nursing)
        return app

    def configured_app(self, **environment):
        return self.app(**{
            "FLASK_ENV": "development",
            "DEV_INTERNAL_ACCESS_MODE": "true",
            "DEV_INTERNAL_ACCESS_TOKEN": self.token,
            **environment,
        })

    def test_default_is_off_even_with_credential(self):
        app = self.app(FLASK_ENV="development", DEV_INTERNAL_ACCESS_TOKEN=self.token)
        self.assertFalse(enabled(app))
        self.assertEqual(app.test_client().get(
            "/protected", headers={"X-Internal-Access": self.token},
        ).status_code, 403)

    def test_enabled_request_is_audited_without_credentials_or_query(self):
        with self.assertLogs(level="WARNING") as startup:
            app = self.configured_app()
        self.assertIn("Internal Development Access Mode Active", startup.output[0])
        client = app.test_client()
        with self.assertLogs(app.logger, level="INFO") as audit:
            response = client.get(
                "/protected?private=query",
                headers={"X-Internal-Access": self.token, "X-Forwarded-For": "203.0.113.5"},
                environ_overrides={"REMOTE_ADDR": "127.0.0.2"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertIn("Internal Development Access Mode Active (Non-Production)", response.text)
        entry = json.loads(audit.records[0].getMessage())
        self.assertEqual(entry["event"], "internal_access_granted")
        self.assertEqual(entry["route"], "/protected")
        self.assertEqual(entry["client_ip"], "127.0.0.2")
        self.assertTrue(entry["timestamp"].endswith("+00:00"))
        self.assertNotIn(self.token, audit.output[0])
        self.assertNotIn("private", audit.output[0])
        self.assertEqual(response.headers["Cache-Control"], "private, no-store")
        self.assertIn("X-Internal-Access", response.vary)
        with client.session_transaction() as session:
            self.assertEqual(dict(session), {})
        self.assertEqual(client.get("/protected").status_code, 403)

    def test_invalid_missing_and_query_credentials_are_denied(self):
        client = self.configured_app().test_client()
        for headers in ({}, {"X-Internal-Access": "wrong"}, {"X-Internal-Access": "é"}):
            with self.subTest(headers=headers):
                self.assertEqual(client.get(
                    "/protected", headers=headers,
                ).status_code, 403)
        self.assertEqual(client.get(
            "/protected", query_string={"X-Internal-Access": self.token},
        ).status_code, 403)

    def test_all_production_markers_override_development_and_flags(self):
        for key in ENVIRONMENT_KEYS:
            for value in ("production", " PRODUCTION ", "prod"):
                for source in ("environment", "config"):
                    with self.subTest(key=key, value=value, source=source):
                        os.environ.clear()
                        app = self.configured_app()
                        if source == "environment":
                            os.environ[key] = value
                        else:
                            app.config[key] = value
                        app.config.update(DEBUG=True, TESTING=True)
                        self.assertFalse(enabled(app))
                        self.assertEqual(app.test_client().get(
                            "/protected", headers={"X-Internal-Access": self.token},
                        ).status_code, 403)

    def test_startup_production_forces_config_off(self):
        for key in ENVIRONMENT_KEYS:
            with self.subTest(key=key):
                os.environ.clear()
                app = self.configured_app(**{key: "production"})
                self.assertIs(app.config["DEV_INTERNAL_ACCESS_MODE"], False)

    def test_missing_unknown_environment_or_empty_secret_fail_closed(self):
        for environment in (
            {"FLASK_ENV": ""}, {"FLASK_ENV": "unknown"},
            {"DEV_INTERNAL_ACCESS_TOKEN": ""},
            {"DEV_INTERNAL_ACCESS_TOKEN": "   "},
        ):
            with self.subTest(environment=environment):
                os.environ.clear()
                self.assertFalse(enabled(self.configured_app(**environment)))

    def test_explicit_nonproduction_does_not_require_debug(self):
        for value in ("development", "testing", "staging", "local"):
            with self.subTest(value=value):
                app = self.configured_app(FLASK_ENV=value)
                app.config.update(DEBUG=False, TESTING=False)
                self.assertTrue(enabled(app))
