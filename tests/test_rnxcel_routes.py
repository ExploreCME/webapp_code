"""End-to-end nursing routes using deliberately nonclinical authored fixtures."""

import json
from contextlib import ExitStack, contextmanager
from html.parser import HTMLParser
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from flask import g, template_rendered
from werkzeug.datastructures import MultiDict

with patch("os.makedirs"):
    import display_database

from rnxcel_engine import EXAM_PROFILE, TAG_DIMENSIONS, validate_version
from rnxcel_storage import RNXcelStore


# SQLite translation of the nursing migration, not application schema creation.
SCHEMA = """
CREATE TABLE rnxcel_item_versions (
    version_id TEXT PRIMARY KEY, publication_status TEXT NOT NULL DEFAULT 'draft',
    exam_profile TEXT NOT NULL, authored_json TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP, published_at TEXT
);
CREATE TABLE rnxcel_attempts (
    id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, snapshot_json TEXT NOT NULL,
    responses_json TEXT NOT NULL, current_index INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active', created_at TEXT NOT NULL,
    completed_at TEXT
);
CREATE TABLE rnxcel_consents (
    owner_id TEXT PRIMARY KEY, accepted_at TEXT NOT NULL
);
"""


def authored_item(name, kind="single-best-answer", category="MOC", objective="MOC-01"):
    """These arbitrary sorting puzzles are not clinical or exam content."""
    tags = {dimension: [] for dimension in TAG_DIMENSIONS}
    tags.update(
        population=["adult"], setting=["acute-care"],
        clinical_judgment=["recognize-cues"],
    )
    return {
        "version_id": name, "exam_profile": EXAM_PROFILE, "type": kind,
        "category_id": category, "objective_id": objective,
        "stem": f"NONCLINICAL TEST ONLY: {name} sort the toy tiles.",
        "options": [
            {"id": "tile-a", "text": "Amber toy tile"},
            {"id": "tile-b", "text": "Blue toy tile"},
            {"id": "tile-c", "text": "Copper toy tile"},
        ],
        "correct_option_ids": (
            ["tile-a"] if kind == "single-best-answer" else ["tile-a", "tile-c"]
        ),
        "scoring": "single" if kind == "single-best-answer" else "plus-minus",
        "rationale": f"PRIVATE-RATIONALE-{name}: toy sorting fixture only.",
        "tags": tags,
    }


def authored_case():
    return {
        "version_id": "test-case-v1", "exam_profile": EXAM_PROFILE,
        "type": "unfolding-case-study", "title": "NONCLINICAL toy sorting case",
        "stages": [
            {
                "id": f"stage-{index}",
                "exhibits": [{
                    "timestamp": f"2026-01-01T0{index}:00:00Z",
                    "text": f"EXHIBIT-ONLY-STAGE-{index}: toy tile inventory.",
                }],
                "item": authored_item(
                    f"case-stage-{index}",
                    "single-best-answer" if index % 2 == 0 else "select-all-that-apply",
                ),
            }
            for index in range(6)
        ],
    }


class NursingForms(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.forms = []
        self.current = None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form":
            self.current = {"action": attrs.get("action"), "fields": MultiDict()}
            self.forms.append(self.current)
        elif tag == "input" and self.current is not None and attrs.get("type") == "hidden":
            self.current["fields"].add(attrs["name"], attrs.get("value", ""))

    def handle_endtag(self, tag):
        if tag == "form":
            self.current = None


class RNXcelEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.app = display_database.app
        previous = self.app.config.copy()
        self.addCleanup(lambda: (self.app.config.clear(), self.app.config.update(previous)))
        self.app.config.update(TESTING=True, SECRET_KEY="nursing-route-test-only")
        self.db_path = Path.cwd() / f".rnxcel-route-test-{uuid4().hex}.sqlite3"
        self.addCleanup(lambda: self.db_path.unlink(missing_ok=True))
        self.queries = []

        def connection_factory():
            connection = sqlite3.connect(self.db_path)
            connection.set_trace_callback(self.queries.append)
            return connection

        self.connection_factory = connection_factory
        with self.database() as connection:
            connection.executescript(SCHEMA)
        with self.app.app_context():
            self.store = RNXcelStore(connection_factory, dialect="sqlite")
        self.app.config["RNXCEL_TEST_STORE"] = self.store
        self.client = self.app.test_client()
        self.templates = []

        def rendered(sender, template, context, **extra):
            self.templates.append((template.name, context))

        template_rendered.connect(rendered, self.app, weak=False)
        self.addCleanup(lambda: template_rendered.disconnect(rendered, self.app))
        self.hooks = {}
        for name in ("refresh_user_status", "get_db_connection", "fetch_data_for_dashboard"):
            replacement = MagicMock(name=name)
            self.hooks[name] = replacement
            self._patch(display_database, name, replacement)
        self._patch(display_database, "MAINTENANCE_MODE", False)
        self.decoder = self._patch(
            display_database, "decode_clerk_token", MagicMock(side_effect=self._decode),
        )
        self.login()

    def _patch(self, target, name, replacement):
        patcher = patch.object(target, name, replacement)
        patcher.start()
        self.addCleanup(patcher.stop)
        return replacement

    @contextmanager
    def database(self):
        connection = self.connection_factory()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _decode(token):
        # Patch the signature-verifying decoder, never the route's identity guard.
        if token not in ("signed-owner-a", "signed-owner-b"):
            raise ValueError("Invalid signed token")
        return {"sub": token.removeprefix("signed-"), "exp": 9999999999, "iat": 1}

    def login(self, owner="owner-a", consent=True, client=None):
        client = client or self.client
        client.set_cookie("__session", f"signed-{owner}")
        if consent:
            with self.app.app_context():
                self.store.accept_consent(owner)

    def publish(self, *versions):
        with self.database() as connection:
            for version in versions:
                validate_version(version)
                connection.execute(
                    "INSERT INTO rnxcel_item_versions "
                    "(version_id, publication_status, exam_profile, authored_json) "
                    "VALUES (?, 'published', ?, ?)",
                    (version["version_id"], EXAM_PROFILE, json.dumps(version)),
                )

    def context(self, response=None):
        self.assertTrue(self.templates, "Expected a rendered nursing template")
        return self.templates[-1][1]

    def form(self, response, action):
        forms = [form for form in NursingForms(response.text).forms if form["action"] == action]
        self.assertEqual(len(forms), 1, f"Expected one form for {action}")
        return forms[0]["fields"]

    def csrf(self, client=None):
        client = client or self.client
        response = client.get("/rnxcel/consent")
        self.assertEqual(response.status_code, 200)
        rendered_token = self.form(response, "/rnxcel/consent")["csrf_token"]
        with client.session_transaction() as session:
            state = session["rnxcel_csrf"]
            self.assertEqual(rendered_token, state["token"])
            return state["token"]

    def start(self, experience="single-best-answer", count=1, **filters):
        data = {
            "experience": experience, "count": str(count), "mode": "practice",
            "csrf_token": self.csrf(), **filters,
        }
        response = self.client.post("/rnxcel/attempts", data=data)
        self.assertEqual(response.status_code, 303, response.text)
        path = urlsplit(response.location).path
        self.assertTrue(path.startswith("/rnxcel/attempts/"))
        return path

    def answer(self, path, index, selections, token=None, client=None):
        client = client or self.client
        if token is None:
            token = self.csrf(client)
        return client.post(
            path + "/answer",
            data=MultiDict(
                [("csrf_token", token), ("index", str(index))]
                + [("selection", value) for value in selections]
            ),
        )

    def saved(self, path, owner="owner-a"):
        with self.app.app_context():
            return self.store.get(owner, path.rsplit("/", 1)[-1])

    def assert_no_feedback(self, response, versions):
        self.assertNotIn("correct_option_ids", response.text)
        self.assertNotIn("responses_json", response.text)
        self.assertNotIn("snapshot_json", response.text)
        for version in versions:
            items = [stage["item"] for stage in version["stages"]] if "stages" in version else [version]
            for item in items:
                self.assertNotIn(item["rationale"], response.text)

    def tearDown(self):
        for hook in self.hooks.values():
            hook.assert_not_called()
        for query in self.queries:
            normalized = query.lower()
            for forbidden in (
                " from users", " into users", " from questions", " from question_bank",
                " from quiz_results", " from pa_", " into pa_",
            ):
                self.assertNotIn(forbidden, normalized)

    def test_empty_content_is_successful_but_start_is_disabled_and_post_rejected(self):
        for experience in (
            "single-best-answer", "select-all-that-apply", "unfolding-case-study",
        ):
            with self.subTest(experience=experience):
                response = self.client.get(
                    "/rnxcel/practice", query_string={"experience": experience, "count": 6},
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(self.context()["available"], 0)
                self.assertEqual(self.context()["start_count"], 0)
                self.assertRegex(response.text, r"<button[^>]*disabled")
                response = self.client.post("/rnxcel/attempts", data={
                    "experience": experience, "count": "6", "mode": "practice",
                    "csrf_token": self.csrf(),
                })
                self.assertEqual(response.status_code, 400)
        with self.app.app_context():
            self.assertEqual(self.store.history("owner-a"), [])

    def test_missing_migration_is_503_not_an_empty_bank_or_created_schema(self):
        with self.database() as connection:
            connection.execute("DROP TABLE rnxcel_item_versions")
        with self.assertLogs(level="WARNING"):
            response = self.client.get("/rnxcel/dashboard")
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("no such table", response.text.lower())
        with self.database() as connection:
            tables = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )}
        self.assertNotIn("rnxcel_item_versions", tables)
        self.assertEqual(self.client.get("/rnxcel").status_code, 200)

    def test_missing_consent_migration_does_not_fall_back_to_pa_eula(self):
        with self.client.session_transaction() as session:
            session["accepted_eula"] = True
        with self.database() as connection:
            connection.execute("DROP TABLE rnxcel_consents")
        with self.assertLogs(level="WARNING"):
            response = self.client.get("/rnxcel/dashboard")
        self.assertEqual(response.status_code, 503)
        with self.client.session_transaction() as session:
            self.assertTrue(session["accepted_eula"])

    def test_unpublished_retired_and_other_exam_versions_are_never_available(self):
        single = authored_item("published-nursing")
        self.publish(single)
        with self.database() as connection:
            for status, profile in (
                ("draft", EXAM_PROFILE), ("retired", EXAM_PROFILE), ("published", "PANCE"),
            ):
                version = authored_item(f"unavailable-{status}-{profile}")
                version["exam_profile"] = profile
                connection.execute(
                    "INSERT INTO rnxcel_item_versions "
                    "(version_id, publication_status, exam_profile, authored_json) "
                    "VALUES (?, ?, ?, ?)",
                    (version["version_id"], status, profile, json.dumps(version)),
                )
        response = self.client.get("/rnxcel/practice", query_string={"count": "1"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.context()["available"], 1)
        path = self.start()
        self.assertEqual(
            [version["version_id"] for version in self.saved(path)["snapshot"]],
            ["published-nursing"],
        )

    def test_all_experiences_complete_and_review_using_persistent_nursing_storage(self):
        versions = [
            authored_item("test-single-v1"),
            authored_item("test-multiple-v1", "select-all-that-apply"),
            authored_case(),
        ]
        self.publish(*versions)
        for version in versions:
            with self.subTest(experience=version["type"]):
                items = [stage["item"] for stage in version["stages"]] if "stages" in version else [version]
                path = self.start(version["type"], len(items))
                self.assertEqual(self.saved(path)["status"], "active")
                for index, item in enumerate(items):
                    response = self.client.get(path)
                    self.assertEqual(response.status_code, 200)
                    self.assertIn(item["stem"], response.text)
                    self.assertIsNone(self.context()["attempt"]["earned"])
                    self.assertIsNone(self.context()["attempt"]["possible"])
                    self.assert_no_feedback(response, versions)
                    for suffix in ("/results", "/review"):
                        premature = self.client.get(path + suffix, follow_redirects=True)
                        self.assertEqual(premature.status_code, 200)
                        self.assert_no_feedback(premature, versions)
                    response = self.answer(path, index, item["correct_option_ids"])
                    self.assertEqual(response.status_code, 303)
                saved = self.saved(path)
                self.assertEqual(saved["status"], "completed")
                self.assertEqual(saved["current_index"], len(items))
                results = self.client.get(path + "/results")
                self.assertEqual(results.status_code, 200)
                expected = sum(len(item["correct_option_ids"]) for item in items)
                self.assertEqual(self.context()["attempt"]["earned"], expected)
                self.assertEqual(self.context()["attempt"]["possible"], expected)
                review_response = self.client.get(path + "/review")
                self.assertEqual(review_response.status_code, 200)
                for item in items:
                    self.assertIn(item["rationale"], review_response.text)
                self.assertEqual(self.client.get(path).status_code, 302)

    def test_case_releases_only_current_and_prior_timestamped_exhibits(self):
        case = authored_case()
        self.publish(case)
        path = self.start("unfolding-case-study", 6)
        for index, stage in enumerate(case["stages"]):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assert_no_feedback(response, [case])
            for position, other in enumerate(case["stages"]):
                exhibit = other["exhibits"][0]
                if position <= index:
                    self.assertIn(exhibit["text"], response.text)
                    self.assertIn(exhibit["timestamp"], response.text)
                else:
                    self.assertNotIn(exhibit["text"], response.text)
                    self.assertNotIn(exhibit["timestamp"], response.text)
                    self.assertNotIn(other["item"]["stem"], response.text)
            if index < 5:
                for key in ("stage", "index", "stage_index"):
                    self.assertEqual(self.client.get(path, query_string={key: index + 1}).status_code, 400)
                skip = self.answer(path, index + 1, ["tile-a"])
                self.assertEqual(skip.status_code, 409)
                self.assertEqual(self.saved(path)["current_index"], index)
            self.assertEqual(
                self.answer(path, index, stage["item"]["correct_option_ids"]).status_code, 303,
            )

    def test_resume_after_browser_restart_keeps_snapshot_and_saved_progress(self):
        case = authored_case()
        self.publish(case)
        path = self.start("unfolding-case-study", 6)
        self.assertEqual(self.answer(path, 0, ["tile-a"]).status_code, 303)
        with self.database() as connection:
            connection.execute(
                "UPDATE rnxcel_item_versions SET publication_status = 'retired'"
            )
        restarted = self.app.test_client()
        self.login(client=restarted)
        response = restarted.get(path)
        self.assertEqual(response.status_code, 200)
        self.assertIn(case["stages"][1]["item"]["stem"], response.text)
        self.assertNotIn(case["stages"][2]["item"]["stem"], response.text)
        self.assertEqual(self.saved(path)["current_index"], 1)
        history = restarted.get("/rnxcel/attempts")
        self.assertEqual(history.status_code, 200)
        self.assertIn(path, history.text)
        self.assertEqual(self.answer(path, 1, ["tile-a"], client=restarted).status_code, 303)
        self.assertEqual(self.saved(path)["current_index"], 2)

    def test_repeated_answer_is_idempotent_but_changed_answer_conflicts(self):
        self.publish(authored_case())
        path = self.start("unfolding-case-study", 6)
        self.assertEqual(self.answer(path, 0, ["tile-a"]).status_code, 303)
        before = self.saved(path)
        self.assertEqual(self.answer(path, 0, ["tile-a"]).status_code, 303)
        self.assertEqual(self.saved(path), before)
        self.assertEqual(self.answer(path, 0, ["tile-b"]).status_code, 409)
        self.assertEqual(self.saved(path), before)

    def test_completed_sata_replay_is_order_independent_but_cannot_change_results(self):
        self.publish(authored_item("sata-replay", "select-all-that-apply"))
        path = self.start("select-all-that-apply")
        self.assertEqual(self.answer(path, 0, ["tile-a", "tile-c"]).status_code, 303)
        before = self.saved(path)
        self.assertEqual(self.answer(path, 0, ["tile-c", "tile-a"]).status_code, 303)
        self.assertEqual(self.saved(path), before)
        self.assertEqual(self.answer(path, 0, ["tile-a"]).status_code, 409)
        self.assertEqual(self.saved(path), before)

    def test_invalid_sba_and_sata_answers_never_advance_progress(self):
        for kind in ("single-best-answer", "select-all-that-apply"):
            version = authored_item("invalid-" + kind, kind)
            self.publish(version)
            path = self.start(kind)
            bad = [[], ["unknown"], ["tile-a", "tile-a"]]
            if kind == "single-best-answer":
                bad.append(["tile-a", "tile-c"])
            for selections in bad:
                with self.subTest(kind=kind, selections=selections):
                    self.assertEqual(self.answer(path, 0, selections).status_code, 400)
                    self.assertEqual(self.saved(path)["current_index"], 0)
            for index in ("-1", "nonnumeric", "1.0", "999"):
                self.assertEqual(self.answer(path, index, ["tile-a"]).status_code, 400)
            response = self.client.post(path + "/answer", data=MultiDict([
                ("csrf_token", self.csrf()), ("index", "0"), ("index", "0"),
                ("selection", "tile-a"),
            ]))
            self.assertEqual(response.status_code, 400)

    def test_other_owner_cannot_get_submit_or_report_on_an_attempt(self):
        self.publish(authored_item("owned-test"))
        path = self.start()
        before = self.saved(path)
        self.login("owner-b")
        for suffix in ("", "/results", "/review"):
            self.assertEqual(self.client.get(path + suffix).status_code, 404)
        self.assertEqual(self.answer(path, 0, ["tile-a"]).status_code, 404)
        for endpoint in ("/rnxcel/dashboard", "/rnxcel/attempts", "/rnxcel/progress"):
            response = self.client.get(endpoint)
            self.assertEqual(response.status_code, 200)
            self.assertNotIn(path, response.text)
            self.assertNotIn("owned-test", response.text)
        self.assertEqual(self.saved(path), before)

    def test_csrf_is_required_on_mutations_and_rotates_when_verified_owner_changes(self):
        self.publish(authored_item("csrf-test"))
        token_a = self.csrf()
        for supplied in (None, "wrong-token"):
            data = {"count": "1", "experience": "single-best-answer"}
            if supplied is not None:
                data["csrf_token"] = supplied
            self.assertEqual(self.client.post("/rnxcel/attempts", data=data).status_code, 400)
        path = self.start()
        self.assertEqual(self.answer(path, 0, ["tile-a"], token="wrong-token").status_code, 400)
        self.assertEqual(self.saved(path)["current_index"], 0)
        self.login("owner-b")
        self.assertEqual(self.client.post("/rnxcel/attempts", data={
            "count": "1", "experience": "single-best-answer", "csrf_token": token_a,
        }).status_code, 400)
        token_b = self.csrf()
        self.assertNotEqual(token_a, token_b)
        with self.client.session_transaction() as session:
            self.assertEqual(session["rnxcel_csrf"]["owner"], "owner-b")

    def test_missing_or_invalid_verified_identity_cannot_use_pa_session_identity(self):
        protected = (
            "/rnxcel/dashboard", "/rnxcel/practice", "/rnxcel/cases",
            "/rnxcel/attempts", "/rnxcel/progress", "/rnxcel/consent",
        )
        for token in (None, "bad-signature"):
            if token:
                self.client.set_cookie("__session", token)
            else:
                self.client.delete_cookie("__session")
            with self.client.session_transaction() as session:
                session.update(username="owner-a", is_premium=True, accepted_eula=True)
            for path in protected:
                with self.subTest(token=token, path=path):
                    response = self.client.get(path)
                    self.assertEqual(response.status_code, 302)
                    location = urlsplit(response.location)
                    self.assertEqual(location.path, "/rnxcel/sign-in")
                    self.assertTrue(parse_qs(location.query)["next"][0].startswith("/rnxcel"))
            post = self.client.post("/rnxcel/attempts", data={"count": "1"})
            self.assertEqual(urlsplit(post.location).path, "/rnxcel/sign-in")

    def test_verified_token_must_contain_a_valid_nonempty_owner_claim(self):
        for claim in ({}, {"sub": None}, {"sub": ""}, {"sub": 123}, {"sub": "x" * 256}):
            with self.subTest(claim=claim), patch.object(
                display_database, "decode_clerk_token", return_value=claim,
            ):
                response = self.client.get("/rnxcel/dashboard")
                self.assertEqual(response.status_code, 302)
                self.assertEqual(urlsplit(response.location).path, "/rnxcel/sign-in")

    def test_signin_and_consent_return_paths_are_nursing_local_only(self):
        unsafe = (
            "https://evil.invalid/", "//evil.invalid/", r"/\evil.invalid",
            "/rnxcel\\evil.invalid", "/%2fevil.invalid", "/rnxcel/%2f%2fevil.invalid",
            "/rnxcel/%255c%255cevil.invalid", "/paxcel", "/rnxcel-evil",
            "/rnxcel/sign-in", "/rnxcel/consent", "/rnxcel/\r\nLocation:evil",
            "/rnxcel/../paxcel", "/rnxcel/./sign-in", "http://[",
            "javascript:alert(1)",
        )
        for target in unsafe:
            with self.subTest(target=target):
                response = self.client.get("/rnxcel/sign-in", query_string={"next": target})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(self.context()["return_to"], "/rnxcel/dashboard")
                response = self.client.post("/rnxcel/consent", data={
                    "next": target, "agree": "yes", "csrf_token": self.csrf(),
                })
                self.assertEqual(response.status_code, 303)
                self.assertEqual(response.location, "/rnxcel/dashboard")
        safe = "/rnxcel/practice?experience=single-best-answer&count=1"
        self.client.get("/rnxcel/sign-in", query_string={"next": safe})
        self.assertEqual(self.context()["return_to"], safe)
        response = self.client.post("/rnxcel/consent", data={
            "next": safe, "agree": "yes", "csrf_token": self.csrf(),
        })
        self.assertEqual(response.location, safe)

    def test_consent_is_separate_persistent_and_not_satisfied_by_pa_eula(self):
        self.login("owner-b", consent=False)
        with self.client.session_transaction() as session:
            session.update(username="pa-user", accepted_eula=True, is_premium=True)
        response = self.client.get("/rnxcel/dashboard")
        self.assertEqual(urlsplit(response.location).path, "/rnxcel/consent")
        self.assertEqual(self.client.get("/rnxcel/consent").status_code, 200)
        with self.app.app_context():
            self.assertFalse(self.store.has_consent("owner-b"))
        self.assertEqual(self.client.post("/rnxcel/consent", data={
            "agree": "yes",
        }).status_code, 400)
        token = self.csrf()
        self.assertEqual(self.client.post("/rnxcel/consent", data={
            "csrf_token": token,
        }).status_code, 400)
        response = self.client.post("/rnxcel/consent", data={
            "agree": "yes", "csrf_token": token, "next": "/rnxcel/dashboard",
        })
        self.assertEqual(response.status_code, 303)
        with self.app.app_context():
            self.assertTrue(self.store.has_consent("owner-b"))
        restarted = self.app.test_client()
        self.login("owner-b", consent=False, client=restarted)
        self.assertEqual(restarted.get("/rnxcel/dashboard").status_code, 200)
        with self.client.session_transaction() as session:
            self.assertEqual(session["username"], "pa-user")
            self.assertTrue(session["accepted_eula"])
            self.assertTrue(session["is_premium"])

    def test_rendered_consent_form_preserves_safe_requested_destination(self):
        self.login("owner-b", consent=False)
        destination = "/rnxcel/practice?experience=single-best-answer&count=1"
        response = self.client.get("/rnxcel/consent", query_string={"next": destination})
        self.assertEqual(response.status_code, 200)
        form = self.form(response, "/rnxcel/consent")
        self.assertEqual(form.get("next"), destination)
        form["agree"] = "yes"
        submitted = self.client.post("/rnxcel/consent", data=form)
        self.assertEqual(submitted.status_code, 303)
        self.assertEqual(submitted.location, destination)

    def test_every_protected_page_checks_persistent_owner_consent(self):
        self.publish(authored_item("consent-check"))
        path = self.start()
        token = self.csrf()
        with patch.object(self.store, "has_consent", return_value=False) as consent:
            for endpoint in (
                "/rnxcel/dashboard", "/rnxcel/practice", "/rnxcel/cases",
                "/rnxcel/attempts", "/rnxcel/progress",
                "/rnxcel/practice/single-best-answer", path, path + "/results", path + "/review",
            ):
                response = self.client.get(endpoint)
                self.assertEqual(urlsplit(response.location).path, "/rnxcel/consent")
                consent.assert_called_with("owner-a")
            response = self.client.post("/rnxcel/attempts", data={
                "csrf_token": token, "count": "1",
            })
            self.assertEqual(urlsplit(response.location).path, "/rnxcel/consent")
            response = self.answer(path, 0, ["tile-a"], token=token)
            self.assertEqual(urlsplit(response.location).path, "/rnxcel/consent")
        self.assertEqual(self.saved(path)["current_index"], 0)

    def test_filters_and_available_counts_use_published_nursing_items_only(self):
        self.publish(
            authored_item("filter-single"),
            authored_item("filter-multiple", "select-all-that-apply", "SIC", "SIC-02"),
            authored_case(),
        )
        cases = (
            ({}, 8),
            ({"category": "MOC"}, 7),
            ({"category": "SIC", "objective": "SIC-02"}, 1),
            ({"objective": "MOC-01"}, 7),
            ({"experience": "unfolding-case-study"}, 6),
            ({"experience": "single-best-answer"}, 1),
            ({"population": "adult", "setting": "acute-care", "clinical_judgment": "recognize-cues"}, 8),
            ({"population": "pediatric"}, 0),
            ({"setting": "home"}, 0),
            ({"clinical_judgment": "take-action"}, 0),
        )
        for filters, count in cases:
            with self.subTest(filters=filters):
                response = self.client.get("/rnxcel/practice", query_string={"count": 1, **filters})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(self.context()["available"], count)
        response = self.client.get("/rnxcel/cases")
        self.assertEqual(response.status_code, 200)
        self.assertIn("NONCLINICAL toy sorting case", response.text)
        self.assertNotIn("PRIVATE-RATIONALE", response.text)
        for experience in ("single-best-answer", "select-all-that-apply", "unfolding-case-study"):
            response = self.client.get("/rnxcel/practice/" + experience)
            self.assertEqual(response.status_code, 302)
            self.assertEqual(parse_qs(urlsplit(response.location).query)["experience"], [experience])
        for unknown in ("paxcel", "single_answer", "unknown"):
            self.assertEqual(self.client.get("/rnxcel/practice/" + unknown).status_code, 404)

    def test_case_filters_require_every_stage_to_match_without_splitting_cases(self):
        case = authored_case()
        final_item = case["stages"][-1]["item"]
        final_item.update(category_id="SIC", objective_id="SIC-02")
        final_item["tags"].update(
            population=["pediatric"], setting=["home"],
            clinical_judgment=["take-action"],
        )
        self.publish(authored_item("filter-complete-case"), case)
        for filters in (
            {"category": "MOC"}, {"objective": "MOC-01"}, {"population": "adult"},
            {"setting": "acute-care"}, {"clinical_judgment": "recognize-cues"},
        ):
            with self.subTest(filters=filters):
                response = self.client.get(
                    "/rnxcel/practice", query_string={"count": "1", **filters},
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(self.context()["available"], 1)
                response = self.client.post("/rnxcel/attempts", data={
                    "experience": "unfolding-case-study", "count": "6",
                    "csrf_token": self.csrf(), **filters,
                })
                self.assertEqual(response.status_code, 400)
        with self.app.app_context():
            self.assertEqual(self.store.history("owner-a"), [])

    def test_setup_rejects_unsupported_or_cross_category_values_on_get_and_post(self):
        self.publish(authored_item("setup-validation"))
        invalid = (
            {"category": "Cardiology"}, {"objective": "PANCE-01"},
            {"experience": "paxcel"}, {"population": "invalid"},
            {"setting": "invalid"}, {"clinical_judgment": "invalid"},
            {"category": "MOC", "objective": "SIC-01"},
            {"count": "0"}, {"count": "51"}, {"count": "1.5"},
            {"mode": "timed"}, {"mode": "cat"},
        )
        token = self.csrf()
        for filters in invalid:
            with self.subTest(filters=filters):
                data = {"count": "1", "mode": "practice", **filters}
                self.assertEqual(self.client.get("/rnxcel/practice", query_string=data).status_code, 400)
                self.assertEqual(self.client.post(
                    "/rnxcel/attempts", data={**data, "csrf_token": token},
                ).status_code, 400)
        duplicated = MultiDict([("count", "1"), ("count", "2"), ("csrf_token", token)])
        self.assertEqual(self.client.get("/rnxcel/practice", query_string=duplicated).status_code, 400)
        self.assertEqual(self.client.post("/rnxcel/attempts", data=duplicated).status_code, 400)
        with self.app.app_context():
            self.assertEqual(self.store.history("owner-a"), [])

    def test_rendered_start_and_answer_forms_use_the_selected_nursing_content(self):
        single = authored_item("rendered-start")
        self.publish(single, authored_item("unselected", "select-all-that-apply", "SIC", "SIC-02"))
        filters = {
            "category": "MOC", "objective": "MOC-01",
            "experience": "single-best-answer", "population": "adult",
            "setting": "acute-care", "clinical_judgment": "recognize-cues",
            "count": "1", "mode": "practice",
        }
        response = self.client.get("/rnxcel/practice", query_string=filters)
        self.assertEqual(response.status_code, 200)
        fields = self.form(response, "/rnxcel/attempts")
        for key, value in filters.items():
            self.assertEqual(fields[key], value)
        created = self.client.post("/rnxcel/attempts", data=fields)
        self.assertEqual(created.status_code, 303)
        path = urlsplit(created.location).path
        player = self.client.get(path)
        fields = self.form(player, path + "/answer")
        self.assertEqual(fields["index"], "0")
        self.assertIn('type="radio"', player.text)
        fields.add("selection", "tile-a")
        answered = self.client.post(path + "/answer", data=fields)
        self.assertEqual(answered.status_code, 303)
        self.assertEqual(self.saved(path)["status"], "completed")

    def test_whole_case_count_cannot_be_truncated_into_partial_case(self):
        self.publish(authored_case())
        for count in (1, 5, 7):
            response = self.client.get("/rnxcel/practice", query_string={
                "experience": "unfolding-case-study", "count": count,
            })
            self.assertEqual(response.status_code, 200)
            self.assertEqual(self.context()["start_count"], 0)
            self.assertEqual(self.client.post("/rnxcel/attempts", data={
                "experience": "unfolding-case-study", "count": count,
                "csrf_token": self.csrf(),
            }).status_code, 400)
        self.start("unfolding-case-study", 6)

    def test_progress_reports_only_completed_actual_points_not_norms_or_partial_work(self):
        self.publish(
            authored_item("score-single"),
            authored_item("score-multiple", "select-all-that-apply"),
            authored_case(),
        )
        active = self.start("unfolding-case-study", 6)
        self.assertEqual(self.answer(active, 0, ["tile-a"]).status_code, 303)
        for endpoint in ("/rnxcel/dashboard", "/rnxcel/attempts"):
            response = self.client.get(endpoint)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(len(self.context()["history"]), 1)
            summary = self.context()["history"][0]
            self.assertEqual(summary["status"], "active")
            self.assertIsNone(summary["earned"])
            self.assertIsNone(summary["possible"])
            self.assertNotIn("Points earned:", response.text)
        self.assertEqual(self.client.get("/rnxcel/progress").status_code, 200)
        self.assertEqual(self.context()["completed_count"], 0)
        self.assertTrue(all(not rows for rows in self.context()["reports"].values()))
        single = self.start()
        self.assertEqual(self.answer(single, 0, ["tile-b"]).status_code, 303)
        multiple = self.start("select-all-that-apply")
        self.assertEqual(self.answer(multiple, 0, ["tile-a", "tile-b", "tile-c"]).status_code, 303)
        response = self.client.get("/rnxcel/progress")
        self.assertEqual(response.status_code, 200)
        context = self.context()
        self.assertEqual(context["completed_count"], 2)
        category = context["reports"]["Category"]
        self.assertEqual(category, [{"label": "MOC", "earned": 1, "possible": 3, "items": 2}])
        self.assertEqual(context["reports"]["Objective"], [
            {"label": "MOC-01", "earned": 1, "possible": 3, "items": 2},
        ])
        self.assertEqual(context["reports"]["Clinical judgment"], [
            {"label": "recognize-cues", "earned": 1, "possible": 3, "items": 2},
        ])
        self.assertNotIn("percentile", response.text.lower())
        self.assertNotIn("probability of passing", response.text.lower())

    def test_same_browser_pa_identity_entitlements_and_flashes_are_not_used_or_mutated(self):
        original = {
            "username": "pa-existing-user", "is_premium": True, "is_faculty": True,
            "accepted_eula": True, "last_db_sync": 123,
            "_flashes": [("info", "PAXcel-only feedback sentinel")],
        }
        with self.client.session_transaction() as session:
            session.clear()
            session.update(original)
        self.publish(authored_item("isolated-test"))
        path = self.start()
        for endpoint in (
            "/rnxcel", "/rnxcel/dashboard", "/rnxcel/practice", "/rnxcel/cases",
            "/rnxcel/attempts", "/rnxcel/progress", path,
        ):
            response = self.client.get(endpoint)
            self.assertEqual(response.status_code, 200)
            self.assertNotIn("PAXcel-only feedback sentinel", response.text)
            self.assertEqual(response.headers["Cache-Control"], "private, no-store")
            self.assertIn("Cookie", response.headers["Vary"])
        self.assertEqual(self.answer(path, 0, ["tile-a"]).status_code, 303)
        self.assertEqual(self.saved(path)["owner_id"], "owner-a")
        with self.client.session_transaction() as session:
            for key, value in original.items():
                self.assertEqual(session[key], value)
            self.assertEqual(set(session) - set(original), {"rnxcel_csrf"})

    def test_nursing_never_calls_pa_generation_models_reporting_or_normalization(self):
        import on_demand_generator
        import Question_writing_and_verification_optomized as pa_authoring
        import report_engine
        import standardizers

        forbidden = (
            (on_demand_generator, "generate_single_question"),
            (on_demand_generator, "generate_batch"),
            (pa_authoring, "ConversationManager"),
            (pa_authoring, "generate_questions_from_eor"),
            (report_engine, "ReportEngine"),
            (standardizers, "standardize_organ_system"),
            (standardizers, "standardize_task_area"),
        )
        with ExitStack() as stack:
            hooks = [
                stack.enter_context(patch.object(module, name))
                for module, name in forbidden
            ]
            self.publish(authored_item("no-pa-dependencies"))
            for endpoint in (
                "/rnxcel/dashboard", "/rnxcel/practice", "/rnxcel/cases", "/rnxcel/progress",
            ):
                self.assertEqual(self.client.get(endpoint).status_code, 200)
            path = self.start()
            self.assertEqual(self.client.get(path).status_code, 200)
            self.assertEqual(self.answer(path, 0, ["tile-a"]).status_code, 303)
            self.assertEqual(self.client.get(path + "/review").status_code, 200)
            self.assertEqual(self.client.get("/rnxcel/progress").status_code, 200)
            for hook in hooks:
                hook.assert_not_called()

    def test_completed_nursing_attempt_does_not_change_pa_landing_counts_or_hooks(self):
        def pa_status():
            g.is_premium = False
            g.is_faculty = False
            return None

        with (
            patch.object(display_database, "refresh_user_status", side_effect=pa_status) as refresh,
            patch.object(
                display_database, "fetch_data_for_dashboard", return_value=(9876, [], []),
            ) as pa_counts,
        ):
            before = self.client.get("/paxcel")
            self.assertEqual(before.status_code, 200)
            self.assertIn("9876", before.text)
            initial_hooks = refresh.call_count, pa_counts.call_count
            self.publish(authored_item("isolated-count"))
            path = self.start()
            self.assertEqual(self.answer(path, 0, ["tile-a"]).status_code, 303)
            for endpoint in ("/rnxcel/dashboard", "/rnxcel/progress", path + "/review"):
                response = self.client.get(endpoint)
                self.assertEqual(response.status_code, 200)
                self.assertNotIn("9876", response.text)
            self.assertEqual((refresh.call_count, pa_counts.call_count), initial_hooks)
            after = self.client.get("/paxcel")
            self.assertEqual(after.status_code, 200)
            self.assertEqual(before.data, after.data)
            self.assertEqual(pa_counts.call_count, 2)


if __name__ == "__main__":
    unittest.main()
