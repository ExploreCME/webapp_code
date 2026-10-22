"""Request-scoped internal RNXcel access, restricted to explicit non-production."""

from datetime import datetime, timezone
import json
import os
import secrets

from flask import current_app, g, request


ENVIRONMENT_KEYS = ("FLASK_ENV", "ENV", "APP_ENV", "ENVIRONMENT")
NON_PRODUCTION = {"development", "dev", "local", "testing", "test", "staging"}
INTERNAL_OWNER = "dev-internal-rnxcel"


def environment_values(app):
    return {
        str(value).strip().lower()
        for key in ENVIRONMENT_KEYS
        for value in (os.getenv(key), app.config.get(key))
        if value is not None and str(value).strip()
    }


def is_production(app):
    return bool(environment_values(app) & {"production", "prod"})


def enabled(app=None):
    app = app or current_app
    environments = environment_values(app)
    return (
        app.config.get("DEV_INTERNAL_ACCESS_MODE") is True
        and bool(environments)
        and environments <= NON_PRODUCTION
    )


def approved_request():
    return enabled() and getattr(g, "internal_access_granted", False)


def init_internal_access(app):
    requested = os.getenv("DEV_INTERNAL_ACCESS_MODE", "false").strip().lower() in {
        "1", "true", "yes", "on",
    }
    app.config["DEV_INTERNAL_ACCESS_MODE"] = requested
    app.config["DEV_INTERNAL_ACCESS_TOKEN"] = os.getenv("DEV_INTERNAL_ACCESS_TOKEN", "")
    if not enabled(app) or not app.config["DEV_INTERNAL_ACCESS_TOKEN"].strip():
        app.config["DEV_INTERNAL_ACCESS_MODE"] = False
        if requested:
            app.logger.warning(
                "Internal development access disabled: explicit non-production "
                "environment and DEV_INTERNAL_ACCESS_TOKEN are required."
            )
    else:
        app.logger.warning(
            "Internal Development Access Mode Active (Non-Production): "
            "token-approved RNXcel requests skip sign-in and purchase checks."
        )

    @app.before_request
    def approve_internal_request():
        g.internal_access_granted = False
        if not enabled() or request.blueprint != "rnxcel":
            return
        expected = current_app.config.get("DEV_INTERNAL_ACCESS_TOKEN", "")
        supplied = request.headers.get("X-Internal-Access", "")
        if expected.strip() and supplied and secrets.compare_digest(
            expected.encode("utf-8"), supplied.encode("utf-8")
        ):
            g.internal_access_granted = True
            current_app.logger.warning(json.dumps({
                "event": "internal_access_granted",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "route": request.path,
                "client_ip": request.remote_addr,
                "method": request.method,
            }))

    @app.context_processor
    def internal_access_context():
        return {"dev_internal_access_mode": enabled()}

    @app.after_request
    def internal_access_cache(response):
        if enabled():
            response.headers["Cache-Control"] = "private, no-store"
            response.vary.add("X-Internal-Access")
        return response
