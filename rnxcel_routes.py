"""Nursing-only routes; no PAXcel banks, entitlement flags, or generators."""

import os
import secrets
from collections import defaultdict
from urllib.parse import parse_qsl, unquote, urlsplit

from flask import (
    Blueprint, abort, current_app, g, redirect, render_template, request,
    session, url_for,
)
from werkzeug.exceptions import HTTPException

from rnxcel_catalog import (
    BLUEPRINT_VERSION, CLINICAL_JUDGMENT, EXAM_PROFILE, ITEM_EXPERIENCES,
    OBJECTIVES, POPULATIONS, RNXCEL_CATEGORIES, SETTINGS, eligible_versions,
    validate_filters,
)
from rnxcel_engine import flatten, learner_item, review
from rnxcel_storage import RNXcelConflict, RNXcelStore, RNXcelUnavailable


rnxcel_bp = Blueprint("rnxcel", __name__, url_prefix="/rnxcel")


def store():
    # Test fixtures cannot be enabled by an environment variable or public route.
    if current_app.testing and current_app.config.get("RNXCEL_TEST_STORE") is not None:
        return current_app.config["RNXCEL_TEST_STORE"]
    return RNXcelStore()


def safe_return(value):
    """Only nursing-local paths; reject encoded separators and control characters."""
    if not isinstance(value, str):
        return url_for("rnxcel.dashboard")
    decoded = unquote(value)
    try:
        parsed = urlsplit(decoded)
    except ValueError:
        return url_for("rnxcel.dashboard")
    if (
        parsed.scheme or parsed.netloc or "\\" in decoded
        or any(ord(char) < 32 or ord(char) == 127 for char in decoded)
        or not (parsed.path == "/rnxcel" or parsed.path.startswith("/rnxcel/"))
        or any(segment in (".", "..") for segment in parsed.path.split("/"))
        or parsed.path in ("/rnxcel/sign-in", "/rnxcel/consent")
        or decoded != value
    ):
        return url_for("rnxcel.dashboard")
    try:
        endpoint, arguments = current_app.url_map.bind("").match(parsed.path, method="GET")
    except HTTPException:
        return url_for("rnxcel.dashboard")
    allowed = {
        "rnxcel.landing", "rnxcel.dashboard", "rnxcel.setup", "rnxcel.practice",
        "rnxcel.library", "rnxcel.attempts", "rnxcel.play", "rnxcel.results",
        "rnxcel.review_attempt", "rnxcel.progress", "rnxcel.bank",
    }
    if endpoint not in allowed:
        return url_for("rnxcel.dashboard")
    query = parse_qsl(parsed.query, keep_blank_values=True)
    setup_fields = {
        "category", "objective", "experience", "population", "setting",
        "clinical_judgment", "count", "mode", "case_version",
    }
    if query and (
        endpoint != "rnxcel.setup" or len(query) != len(dict(query))
        or any(name not in setup_fields for name, _ in query)
    ):
        return url_for("rnxcel.dashboard")
    # Rebuild a known endpoint; user input cannot set host, scheme or Flask URL options.
    return url_for(endpoint, **arguments, **dict(query))


def csrf():
    state = session.get("rnxcel_csrf", {})
    if state.get("owner") != g.rnxcel_owner:
        state = {"owner": g.rnxcel_owner, "token": secrets.token_urlsafe(32)}
        session["rnxcel_csrf"] = state
    return state["token"]


@rnxcel_bp.before_request
def nursing_guard():
    if request.endpoint in ("rnxcel.landing", "rnxcel.signin"):
        return
    if request.endpoint == "rnxcel.practice" and request.view_args["item_type"] not in {
        experience.slug for experience in ITEM_EXPERIENCES
    }:
        abort(404)
    # Import lazily to reuse verified JWKS decoding without circular registration.
    from display_database import decode_clerk_token

    try:
        token = request.cookies.get("__session")
        owner = decode_clerk_token(token).get("sub") if token else None
        if (
            not isinstance(owner, str) or not owner.strip() or owner != owner.strip()
            or len(owner) > 255 or any(ord(char) < 32 for char in owner)
        ):
            owner = None
    except Exception:
        owner = None
    if not owner:
        return redirect(url_for("rnxcel.signin", next=safe_return(request.full_path.rstrip("?"))))
    g.rnxcel_owner = owner
    if request.method == "POST":
        state = session.get("rnxcel_csrf", {})
        supplied = request.form.get("csrf_token", "")
        if (
            state.get("owner") != owner or not state.get("token")
            or not secrets.compare_digest(state["token"], supplied)
        ):
            abort(400, description="Invalid nursing form token. Reload the page and try again.")
    if not store().has_entitlement(owner):
        return page("rnxcel_access.html"), 403
    if request.endpoint != "rnxcel.consent" and not store().has_consent(owner):
        return redirect(url_for("rnxcel.consent", next=safe_return(request.full_path.rstrip("?"))))


@rnxcel_bp.after_request
def nursing_private_cache(response):
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Vary"] = "Cookie"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    return response


def page(template, **context):
    return render_template(
        template, exam_profile=EXAM_PROFILE, blueprint_version=BLUEPRINT_VERSION,
        experiences=ITEM_EXPERIENCES, categories=RNXCEL_CATEGORIES,
        objectives=OBJECTIVES, populations=POPULATIONS, settings=SETTINGS,
        clinical_judgment=CLINICAL_JUDGMENT,
        signed_in=bool(getattr(g, "rnxcel_owner", None)),
        csrf_token=csrf() if getattr(g, "rnxcel_owner", None) else None,
        **context,
    )


@rnxcel_bp.errorhandler(RNXcelUnavailable)
def storage_unavailable(error):
    current_app.logger.warning("RNXcel storage unavailable: %s", error.reason)
    return page(
        "rnxcel_error.html", status=503,
        message="Nursing storage is unavailable. Saved progress cannot be confirmed. Please try again later.",
    ), 503


@rnxcel_bp.errorhandler(RNXcelConflict)
def conflict(error):
    return page(
        "rnxcel_error.html", status=409,
        message="This request conflicts with available content or saved progress. "
        "Refresh setup or resume your attempt from history.",
    ), 409


@rnxcel_bp.errorhandler(ValueError)
def invalid_input(error):
    return page("rnxcel_error.html", status=400, message=str(error)), 400


@rnxcel_bp.route("")
@rnxcel_bp.route("/")
def landing():
    return page("rnxcel.html")


@rnxcel_bp.route("/sign-in")
def signin():
    return page(
        "rnxcel_signin.html",
        clerk_publishable_key=os.getenv("CLERK_PUBLISHABLE_KEY", ""),
        return_to=safe_return(request.args.get("next")),
    )


@rnxcel_bp.route("/consent", methods=["GET", "POST"])
def consent():
    destination = safe_return(request.values.get("next"))
    if request.method == "POST":
        if request.form.get("agree") != "yes":
            raise ValueError("You must accept the terms to continue.")
        store().accept_consent(g.rnxcel_owner)
        return redirect(destination, code=303)
    return page("rnxcel_consent.html", return_to=destination)


def summary(attempt):
    return {
        key: attempt.get(key)
        for key in ("id", "status", "current_index", "created_at", "completed_at")
    } | {
        "total_items": len(flatten(attempt["snapshot"])),
        "earned": sum(response["earned"] for response in attempt["responses"])
        if attempt["status"] == "completed" else None,
        "possible": sum(response["possible"] for response in attempt["responses"])
        if attempt["status"] == "completed" else None,
    }


def owned_attempt(attempt_id):
    attempt = store().get(g.rnxcel_owner, attempt_id)
    if attempt is None:
        abort(404)
    return attempt


def item_count(versions):
    return sum(6 if version["type"] == "unfolding-case-study" else 1 for version in versions)


def choose_versions(versions, count):
    """Fill an exact requested scored-item count without splitting a case."""
    choices = {0: []}
    for version in versions:
        size = 6 if version["type"] == "unfolding-case-study" else 1
        for previous in sorted(tuple(choices), reverse=True):
            total = previous + size
            if total <= count and total not in choices:
                choices[total] = choices[previous] + [version]
    return choices.get(count, [])


def setup_data(values):
    for key in values:
        if len(values.getlist(key)) != 1:
            raise ValueError("Duplicate setup fields are not allowed.")
    filters = validate_filters(values)
    published = store().published()
    case_version = values.get("case_version", "")
    if case_version:
        published = [
            version for version in published
            if version["version_id"] == case_version
            and version["type"] == "unfolding-case-study"
        ]
        if not published:
            raise ValueError("The selected nursing case is no longer available.")
    filters["case_version"] = case_version
    versions = eligible_versions(published, filters)
    selected = choose_versions(versions, filters["count"])
    return filters, versions, selected


@rnxcel_bp.route("/dashboard")
def dashboard():
    versions = store().published()
    return page(
        "rnxcel_dashboard.html", available=item_count(versions),
        case_count=sum(version["type"] == "unfolding-case-study" for version in versions),
        history=[summary(attempt) for attempt in store().history(g.rnxcel_owner)],
    )


@rnxcel_bp.route("/practice/<item_type>")
def practice(item_type):
    if item_type not in {experience.slug for experience in ITEM_EXPERIENCES}:
        abort(404)
    return redirect(url_for("rnxcel.setup", experience=item_type), code=302)


@rnxcel_bp.route("/practice")
def setup():
    filters, versions, selected = setup_data(request.args)
    return page(
        "rnxcel_setup.html", filters=filters, available=item_count(versions),
        container_count=len(versions), start_count=item_count(selected),
    )


@rnxcel_bp.route("/attempts", methods=["POST"])
def start():
    filters, versions, selected = setup_data(request.form)
    if not selected:
        raise ValueError(
            "No eligible items fit this request. Cases require six items; "
            "choose an available count. No attempt was created."
        )
    attempt = store().create(g.rnxcel_owner, selected)
    return redirect(url_for("rnxcel.play", attempt_id=attempt["id"]), code=303)


@rnxcel_bp.route("/cases")
def library():
    cases = [
        {"title": version["title"], "version_id": version["version_id"]}
        for version in store().published() if version["type"] == "unfolding-case-study"
    ]
    return page("rnxcel_library.html", cases=cases)


@rnxcel_bp.route("/bank")
def bank():
    versions = store().published()
    counts = {
        category.id: item_count(eligible_versions(
            versions, validate_filters({"category": category.id}),
        ))
        for category in RNXCEL_CATEGORIES
    }
    return page("rnxcel_bank.html", counts=counts, available=item_count(versions))


@rnxcel_bp.route("/attempts")
def attempts():
    return page(
        "rnxcel_attempts.html",
        history=[summary(attempt) for attempt in store().history(g.rnxcel_owner)],
    )


@rnxcel_bp.route("/attempts/<attempt_id>")
def play(attempt_id):
    attempt = owned_attempt(attempt_id)
    if attempt["status"] == "completed":
        return redirect(url_for("rnxcel.results", attempt_id=attempt_id))
    # No stage parameter is accepted: the stored index alone selects the release.
    if request.args:
        abort(400)
    return page(
        "rnxcel_player.html", attempt=summary(attempt),
        item=learner_item(attempt["snapshot"], attempt["current_index"]),
    )


@rnxcel_bp.route("/attempts/<attempt_id>/answer", methods=["POST"])
def answer(attempt_id):
    if len(request.form.getlist("index")) != 1:
        raise ValueError("Supply one saved item index.")
    try:
        index = int(request.form["index"])
    except (ValueError, KeyError):
        raise ValueError("Invalid saved item index.") from None
    attempt = store().answer(
        g.rnxcel_owner, attempt_id, index, request.form.getlist("selection"),
    )
    if attempt is None:
        abort(404)
    endpoint = "rnxcel.results" if attempt["status"] == "completed" else "rnxcel.play"
    return redirect(url_for(endpoint, attempt_id=attempt_id), code=303)


def completed_page(attempt_id, include_review=False):
    attempt = owned_attempt(attempt_id)
    if attempt["status"] != "completed":
        return redirect(url_for("rnxcel.play", attempt_id=attempt_id))
    return page(
        "rnxcel_results.html", attempt=summary(attempt),
        review_items=review(attempt["snapshot"], attempt["responses"]) if include_review else None,
    )


@rnxcel_bp.route("/attempts/<attempt_id>/results")
def results(attempt_id):
    return completed_page(attempt_id)


@rnxcel_bp.route("/attempts/<attempt_id>/review")
def review_attempt(attempt_id):
    return completed_page(attempt_id, include_review=True)


@rnxcel_bp.route("/progress")
def progress():
    reports = {name: defaultdict(lambda: {"earned": 0, "possible": 0, "items": 0})
               for name in ("Category", "Objective", "Response format", "Clinical judgment")}
    completed = [
        attempt for attempt in store().history(g.rnxcel_owner)
        if attempt["status"] == "completed"
    ]
    for attempt in completed:
        for item, response in zip(flatten(attempt["snapshot"]), attempt["responses"]):
            dimensions = {
                "Category": [item["category_id"]],
                "Objective": [item["objective_id"]],
                "Response format": [item["type"]],
                "Clinical judgment": item.get("tags", {}).get("clinical_judgment", []),
            }
            for dimension, labels in dimensions.items():
                for label in labels:
                    row = reports[dimension][label]
                    row["earned"] += response["earned"]
                    row["possible"] += response["possible"]
                    row["items"] += 1
    return page(
        "rnxcel_progress.html", completed_count=len(completed),
        reports={name: [dict(label=label, **row) for label, row in sorted(rows.items())]
                 for name, rows in reports.items()},
    )
