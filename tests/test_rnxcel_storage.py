from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
import re
import sqlite3
import unittest
from unittest.mock import MagicMock, patch
from uuid import uuid4

from flask import Flask

import rnxcel_storage as storage


def authored(version_id="version-1", sata=False):
    return {
        "version_id": version_id, "exam_profile": "NCLEX_RN_2026",
        "type": "select-all-that-apply" if sata else "single-best-answer",
        "category_id": "SIC", "objective_id": "SIC-01",
        "stem": "Select the marked symbol.",
        "options": [{"id": "a", "text": "Circle"}, {"id": "b", "text": "Square"},
                    {"id": "c", "text": "Triangle"}],
        "correct_option_ids": ["a", "b"] if sata else ["a"],
        "rationale": "Private fixture explanation",
        "scoring": "plus-minus" if sata else "single",
        "tags": {name: [] for name in (
            "population", "setting", "specialty", "organ_topics",
            "nursing_tasks", "clinical_judgment",
        )},
        "provenance": {"reviewed": "Fixture editor"},
    }


SCHEMA = """
CREATE TABLE rnxcel_entitlements (
    owner_id TEXT PRIMARY KEY, is_active INTEGER NOT NULL DEFAULT 0, expires_at TEXT
);
CREATE TABLE rnxcel_consents (
    owner_id TEXT PRIMARY KEY, accepted_at TEXT NOT NULL
);
CREATE TABLE rnxcel_item_versions (
    version_id TEXT PRIMARY KEY, publication_status TEXT NOT NULL,
    exam_profile TEXT NOT NULL, authored_json TEXT NOT NULL
);
CREATE TABLE rnxcel_attempts (
    id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, snapshot_json TEXT NOT NULL,
    responses_json TEXT NOT NULL, current_index INTEGER NOT NULL,
    status TEXT NOT NULL, created_at TEXT NOT NULL, completed_at TEXT
);
"""


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.testing = True
        self.context = self.app.app_context()
        self.context.push()
        self.addCleanup(self.context.pop)
        self.uri = f"file:rnxcel-{uuid4()}?mode=memory&cache=shared"
        self.keeper = sqlite3.connect(self.uri, uri=True)
        self.addCleanup(self.keeper.close)
        self.keeper.executescript(SCHEMA)
        self.store = storage.RNXcelStore(self.connect, dialect="sqlite")
        self.first = authored()
        self.publish(self.first)

    def connect(self):
        return sqlite3.connect(self.uri, uri=True, timeout=10)

    def publish(self, version, status="published"):
        self.keeper.execute(
            "INSERT INTO rnxcel_item_versions VALUES (?, ?, ?, ?)",
            (version["version_id"], status, version["exam_profile"], json.dumps(version)),
        )
        self.keeper.commit()

    def attempt(self, owner="alice"):
        return self.store.create(owner, self.store.published())

    def test_empty_database_has_no_generated_content(self):
        self.keeper.execute("DELETE FROM rnxcel_item_versions")
        self.keeper.commit()
        self.assertEqual(self.store.published(), [])
        with self.assertRaises(ValueError):
            self.store.create("alice", [])
        self.assertEqual(self.store.history("alice"), [])

    def test_entitlement_is_active_unexpired_owner_specific_and_parameterized(self):
        self.assertFalse(self.store.has_entitlement("alice"))
        self.keeper.executemany(
            "INSERT INTO rnxcel_entitlements VALUES (?, ?, ?)",
            [
                ("alice", 1, None), ("inactive", 0, None),
                ("expired", 1, "2000-01-01T00:00:00"),
                ("future", 1, "2999-01-01T00:00:00"),
                ("' OR 1=1 --", 1, None),
            ],
        )
        self.keeper.commit()
        for owner in ("alice", "future", "' OR 1=1 --"):
            self.assertTrue(self.store.has_entitlement(owner))
        for owner in ("Alice", "bob", "inactive", "expired", "' OR 1=1 -- "):
            if owner.endswith(" "):
                with self.assertRaises(ValueError):
                    self.store.has_entitlement(owner)
            else:
                self.assertFalse(self.store.has_entitlement(owner))
        self.keeper.execute("UPDATE rnxcel_entitlements SET is_active = 0 WHERE owner_id = 'alice'")
        self.keeper.commit()
        self.assertFalse(self.store.has_entitlement("alice"))

    def test_missing_entitlement_migration_fails_closed(self):
        self.keeper.execute("DROP TABLE rnxcel_entitlements")
        self.keeper.commit()
        with self.assertLogs(storage.logger, level="ERROR"), self.assertRaises(
            storage.RNXcelUnavailable
        ) as caught:
            self.store.has_entitlement("alice")
        self.assertEqual(caught.exception.reason, "missing_migration")

    def test_mysql_entitlement_query_is_bound_and_connection_is_released(self):
        connection = MagicMock()
        mysql = storage.RNXcelStore(lambda: connection)
        with patch.object(mysql, "_query", return_value={"owner_id": "alice"}) as query:
            self.assertTrue(mysql.has_entitlement("alice"))
        sql = query.call_args.args[1]
        owner, now = query.call_args.args[2]
        self.assertEqual(owner, "alice")
        self.assertIsNone(now.tzinfo)
        self.assertIn("owner_id = %s AND is_active = 1", sql)
        self.assertIn("expires_at > %s", sql)
        self.assertNotIn("users", sql)
        connection.close.assert_called_once_with()

    def test_nursing_consent_is_owner_isolated_and_idempotent(self):
        self.assertFalse(self.store.has_consent("alice"))
        self.assertFalse(self.store.has_consent("bob"))
        self.assertIsNone(self.store.accept_consent("alice"))
        self.assertTrue(self.store.has_consent("alice"))
        self.assertFalse(self.store.has_consent("Alice"))
        self.assertFalse(self.store.has_consent("bob"))
        first = self.keeper.execute(
            "SELECT accepted_at FROM rnxcel_consents WHERE owner_id = ?", ("alice",),
        ).fetchone()[0]
        self.store.accept_consent("alice")
        rows = self.keeper.execute(
            "SELECT owner_id, accepted_at FROM rnxcel_consents",
        ).fetchall()
        self.assertEqual(rows, [("alice", first)])
        self.store.accept_consent("bob")
        self.assertTrue(self.store.has_consent("bob"))
        self.assertEqual(self.store.history("alice"), [])

    def test_consent_owner_is_parameterized_and_validated(self):
        owner = "' OR 1=1 --"
        self.assertFalse(self.store.has_consent(owner))
        self.store.accept_consent(owner)
        self.assertTrue(self.store.has_consent(owner))
        self.assertFalse(self.store.has_consent("alice"))
        for invalid in (None, "", [], True, "alice "):
            for method in (self.store.has_consent, self.store.accept_consent):
                with self.subTest(owner=invalid, method=method), self.assertRaises(ValueError):
                    method(invalid)

    def test_missing_consent_migration_fails_closed(self):
        self.keeper.execute("DROP TABLE rnxcel_consents")
        self.keeper.commit()
        for method in (self.store.has_consent, self.store.accept_consent):
            with self.subTest(method=method), self.assertLogs(
                storage.logger, level="ERROR"
            ), self.assertRaises(storage.RNXcelUnavailable) as caught:
                method("alice")
            self.assertEqual(caught.exception.reason, "missing_migration")

    def test_mysql_consent_uses_transactional_upsert_without_shared_user_writes(self):
        connection = MagicMock()
        cursor = connection.cursor.return_value
        cursor.description = None
        mysql = storage.RNXcelStore(lambda: connection)
        self.assertIsNone(mysql.accept_consent("alice"))
        sql, parameters = cursor.execute.call_args.args
        self.assertIn("INSERT INTO rnxcel_consents", sql)
        self.assertIn("ON DUPLICATE KEY UPDATE accepted_at = accepted_at", sql)
        self.assertEqual(parameters[0], "alice")
        self.assertNotIn("users", sql)
        connection.begin.assert_called_once_with()
        connection.commit.assert_called_once_with()
        connection.rollback.assert_not_called()

    def test_sqlite_requires_explicit_testing_context_and_factory(self):
        with self.assertRaises(ValueError):
            storage.RNXcelStore(dialect="sqlite")
        self.app.testing = False
        with self.assertRaises(ValueError):
            storage.RNXcelStore(self.connect, dialect="sqlite")
        with self.assertRaises(ValueError):
            self.store.published()
        self.app.testing = True
        self.context.pop()
        try:
            with self.assertRaises(ValueError):
                storage.RNXcelStore(self.connect, dialect="sqlite")
        finally:
            self.context.push()

    def test_snapshots_are_private_detached_and_survive_retirement(self):
        attempt = self.attempt()
        self.assertEqual(attempt["snapshot"], [self.first])
        self.assertEqual(attempt["responses"], [])
        self.assertEqual(attempt["current_index"], 0)
        self.assertEqual(attempt["status"], "active")
        self.assertIsNone(attempt["completed_at"])
        self.assertEqual(str(uuid4()).count("-"), attempt["id"].count("-"))
        attempt["snapshot"][0]["rationale"] = "Client mutation"
        self.keeper.execute(
            "UPDATE rnxcel_item_versions SET authored_json = ?, publication_status = ?",
            ("{}", "retired"),
        )
        self.keeper.commit()
        persisted = self.store.get("alice", attempt["id"])
        self.assertEqual(persisted["snapshot"], [self.first])
        completed = self.store.answer("alice", attempt["id"], 0, ["a"])
        self.assertEqual(completed["status"], "completed")
        self.assertIsNotNone(completed["completed_at"])

    def test_owner_isolation(self):
        alice, bob = self.attempt(), self.attempt("bob")
        self.assertIsNone(self.store.get("bob", alice["id"]))
        self.assertIsNone(self.store.get("alice", bob["id"]))
        self.assertEqual([a["id"] for a in self.store.history("alice")], [alice["id"]])
        self.assertIsNone(self.store.answer("bob", alice["id"], 0, ["a"]))
        self.assertIsNone(self.store.answer("bob", str(uuid4()), 0, ["a"]))
        self.assertEqual(self.store.get("alice", alice["id"])["responses"], [])

    def test_owner_is_case_sensitive_and_parameterized(self):
        upper = self.attempt("Alice")
        self.assertIsNone(self.store.get("alice", upper["id"]))
        malicious = self.attempt("' OR 1=1 --")
        self.assertEqual(len(self.store.history("' OR 1=1 --")), 1)
        self.assertEqual(self.store.get("' OR 1=1 --", malicious["id"])["id"], malicious["id"])

    def test_create_rechecks_stale_missing_and_unpublished_content(self):
        selected = self.store.published()
        changed = deepcopy(selected)
        changed[0]["stem"] = "Injected stem"
        with self.assertRaises(storage.RNXcelConflict):
            self.store.create("alice", changed)
        self.keeper.execute("UPDATE rnxcel_item_versions SET publication_status = 'retired'")
        self.keeper.commit()
        with self.assertRaises(storage.RNXcelConflict):
            self.store.create("alice", selected)
        self.keeper.execute("DELETE FROM rnxcel_item_versions")
        self.keeper.commit()
        with self.assertRaises(storage.RNXcelConflict):
            self.store.create("alice", selected)
        self.assertEqual(self.store.history("alice"), [])

    def test_create_preserves_selection_order_and_rejects_duplicate_versions(self):
        second = authored("version-2")
        self.publish(second)
        attempt = self.store.create("alice", [second, self.first])
        self.assertEqual([v["version_id"] for v in attempt["snapshot"]],
                         ["version-2", "version-1"])
        with self.assertRaises(ValueError):
            self.store.create("alice", [self.first, self.first])

    def test_answer_is_idempotent_and_changed_retries_conflict_after_completion(self):
        attempt = self.attempt()
        first = self.store.answer("alice", attempt["id"], 0, ["a"])
        retry = self.store.answer("alice", attempt["id"], 0, ["a"])
        self.assertEqual(first, retry)
        self.assertEqual(first["responses"], [
            {"index": 0, "selections": ["a"], "earned": 1, "possible": 1},
        ])
        with self.assertRaises(storage.RNXcelConflict):
            self.store.answer("alice", attempt["id"], 0, ["b"])
        self.assertEqual(self.store.get("alice", attempt["id"]), first)

    def test_sata_retry_order_is_irrelevant(self):
        version = authored("sata", sata=True)
        self.publish(version)
        attempt = self.store.create("alice", [version])
        first = self.store.answer("alice", attempt["id"], 0, ["b", "a"])
        self.assertEqual(first["responses"][0]["selections"], ["a", "b"])
        self.assertEqual(first, self.store.answer("alice", attempt["id"], 0, ["a", "b"]))

    def test_no_skipping_and_invalid_submissions_do_not_advance(self):
        second = authored("version-2")
        self.publish(second)
        attempt = self.attempt()
        for index, selections in (
            (1, ["a"]), (-1, ["a"]), (2, ["a"]), (True, ["a"]), ("0", ["a"]),
            (0, []), (0, ["a", "a"]), (0, ["a", "b"]), (0, ["unknown"]), (0, None),
        ):
            with self.subTest(index=index, selections=selections), self.assertRaises(ValueError):
                self.store.answer("alice", attempt["id"], index, selections)
            self.assertEqual(self.store.get("alice", attempt["id"])["current_index"], 0)
        first = self.store.answer("alice", attempt["id"], 0, ["b"])
        self.assertEqual(first["status"], "active")
        self.assertEqual(first["current_index"], 1)
        second = self.store.answer("alice", attempt["id"], 1, ["a"])
        self.assertEqual(second["status"], "completed")
        self.assertEqual(len(second["responses"]), 2)

    def test_future_stage_always_conflicts_without_scoring_hidden_options(self):
        self.publish(authored("version-2"))
        attempt = self.attempt()
        for selections in (None, [], ["unknown"], ["a"], ["a", "a"], {}):
            with self.subTest(selections=selections), patch.object(
                storage.engine, "score", side_effect=AssertionError("Hidden stage scored"),
            ) as score:
                with self.assertRaises(storage.RNXcelConflict):
                    self.store.answer("alice", attempt["id"], 1, selections)
                score.assert_not_called()
        self.assertEqual(self.store.get("alice", attempt["id"])["current_index"], 0)

    def test_changed_retry_conflicts_even_if_new_selection_is_invalid(self):
        attempt = self.attempt()
        saved = self.store.answer("alice", attempt["id"], 0, ["a"])
        for selections in (None, [], ["unknown"], ["a", "a"], {}):
            with self.subTest(selections=selections), self.assertRaises(storage.RNXcelConflict):
                self.store.answer("alice", attempt["id"], 0, selections)
        self.assertEqual(self.store.get("alice", attempt["id"]), saved)

    def test_published_ignores_drafts_and_other_profiles(self):
        self.publish(authored("draft"), "draft")
        other = authored("other")
        other["exam_profile"] = "NCLEX_PN_2026"
        self.publish(other)
        self.assertEqual(self.store.published(), [self.first])

    def test_malformed_published_content_is_logged_and_never_used(self):
        self.keeper.execute(
            "INSERT INTO rnxcel_item_versions VALUES (?, ?, ?, ?)",
            ("invalid", "published", "NCLEX_RN_2026", "{"),
        )
        self.keeper.commit()
        with self.assertLogs(storage.logger, level="ERROR"):
            self.assertEqual(self.store.published(), [self.first])
        self.keeper.execute("DELETE FROM rnxcel_item_versions WHERE version_id = 'version-1'")
        self.keeper.commit()
        with self.assertLogs(storage.logger, level="ERROR"), self.assertRaises(
            storage.RNXcelUnavailable
        ) as caught:
            self.store.published()
        self.assertEqual(caught.exception.reason, "content_unavailable")

    def test_mismatching_version_metadata_cannot_start_attempt(self):
        self.keeper.execute(
            "UPDATE rnxcel_item_versions SET authored_json = ?", (json.dumps(authored("fake")),),
        )
        self.keeper.commit()
        with self.assertRaises(storage.RNXcelUnavailable) as caught:
            self.store.create("alice", [self.first])
        self.assertEqual(caught.exception.reason, "content_unavailable")
        self.assertEqual(self.store.history("alice"), [])

    def test_missing_migration_is_distinct_from_database_outage(self):
        self.keeper.execute("DROP TABLE rnxcel_item_versions")
        self.keeper.commit()
        with self.assertLogs(storage.logger, level="ERROR"), self.assertRaises(
            storage.RNXcelUnavailable
        ) as caught:
            self.store.published()
        self.assertEqual(caught.exception.reason, "missing_migration")
        broken = storage.RNXcelStore(
            lambda: (_ for _ in ()).throw(sqlite3.OperationalError("connection refused")),
            dialect="sqlite",
        )
        with self.assertLogs(storage.logger, level="ERROR"), self.assertRaises(
            storage.RNXcelUnavailable
        ) as caught:
            broken.published()
        self.assertEqual(caught.exception.reason, "database_error")

    def test_database_write_failure_rolls_back(self):
        self.keeper.execute(
            "CREATE TRIGGER fail_attempt BEFORE INSERT ON rnxcel_attempts "
            "BEGIN SELECT RAISE(ABORT, 'simulated write failure'); END"
        )
        self.keeper.commit()
        with self.assertLogs(storage.logger, level="ERROR"), self.assertRaises(
            storage.RNXcelUnavailable
        ):
            self.attempt()
        self.assertEqual(self.store.history("alice"), [])

    def test_corrupt_attempt_fails_closed(self):
        attempt = self.attempt()
        self.keeper.execute(
            "UPDATE rnxcel_attempts SET responses_json = ?, current_index = 1",
            ('[{"index":0,"selections":["a"],"earned":999,"possible":1}]',),
        )
        self.keeper.commit()
        with self.assertRaises(storage.RNXcelUnavailable) as caught:
            self.store.get("alice", attempt["id"])
        self.assertEqual(caught.exception.reason, "content_unavailable")

    def test_input_identity_validation(self):
        for owner in (None, "", [], True, "x" * 256, "alice ", " alice"):
            with self.subTest(owner=owner), self.assertRaises(ValueError):
                self.store.history(owner)
        for attempt_id in (None, "", "not-a-uuid", 1, "' OR 1=1"):
            with self.subTest(id=attempt_id), self.assertRaises(ValueError):
                self.store.get("alice", attempt_id)
        self.assertIsNone(self.store.get("alice", str(uuid4())))

    def test_concurrent_retries_are_serialized_without_duplicate_responses(self):
        path = Path(__file__).resolve().parent / f".rnxcel-{uuid4()}.sqlite"
        self.addCleanup(lambda: path.unlink(missing_ok=True))

        def connect():
            return sqlite3.connect(path, timeout=10)

        initial = connect()
        try:
            initial.executescript(SCHEMA)
            initial.execute(
                "INSERT INTO rnxcel_item_versions VALUES (?, ?, ?, ?)",
                ("version-1", "published", "NCLEX_RN_2026", json.dumps(self.first)),
            )
            initial.commit()
        finally:
            initial.close()
        store = storage.RNXcelStore(connect, dialect="sqlite")
        attempt = store.create("alice", [self.first])

        def answer(_):
            with self.app.app_context():
                return store.answer("alice", attempt["id"], 0, ["a"])

        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(answer, range(4)))
        self.assertTrue(all(result == results[0] for result in results))
        self.assertEqual(len(store.get("alice", attempt["id"])["responses"]), 1)

    def test_case_stage_answering_is_sequential_and_snapshot_survives_content_deletion(self):
        case = {
            "version_id": "case-1", "exam_profile": "NCLEX_RN_2026",
            "type": "unfolding-case-study", "title": "Sequential symbol fixture",
            "stages": [
                {"id": f"stage-{index}", "exhibits": [
                    {"timestamp": f"10:{index:02d}", "text": f"Stage {index}"},
                ], "item": authored(f"item-{index}")}
                for index in range(6)
            ],
        }
        self.publish(case)
        attempt = self.store.create("alice", [case])
        self.keeper.execute("DELETE FROM rnxcel_item_versions")
        self.keeper.commit()
        with self.assertRaises(storage.RNXcelConflict):
            self.store.answer("alice", attempt["id"], 5, ["a"])
        for index in range(6):
            attempt = self.store.answer("alice", attempt["id"], index, ["a"])
            self.assertEqual(attempt["current_index"], index + 1)
            self.assertEqual(
                attempt["status"], "completed" if index == 5 else "active",
            )
        self.assertEqual(len(attempt["responses"]), 6)

    def test_mysql_uses_default_pool_and_row_locks(self):
        connection = MagicMock()
        cursor = connection.cursor.return_value
        cursor.description = [
            (key,) for key in ("version_id", "publication_status", "exam_profile", "authored_json")
        ]
        cursor.fetchall.return_value = []
        factory = MagicMock(return_value=connection)
        with patch.dict("sys.modules", {"db_pool": MagicMock(get_db_connection=factory)}):
            mysql = storage.RNXcelStore()
            with self.assertRaises(storage.RNXcelConflict):
                mysql.create("alice", [self.first])
        factory.assert_called_once_with()
        connection.begin.assert_called_once_with()
        self.assertIn("FOR UPDATE", cursor.execute.call_args.args[0])
        self.assertIn("%s", cursor.execute.call_args.args[0])
        connection.rollback.assert_called_once_with()
        connection.commit.assert_not_called()
        connection.close.assert_called_once_with()

    def test_mysql_answer_locks_owner_row_and_commits(self):
        attempt = self.attempt()
        row = {
            "id": attempt["id"], "owner_id": "alice",
            "snapshot_json": json.dumps(attempt["snapshot"]), "responses_json": "[]",
            "current_index": 0, "status": "active",
            "created_at": attempt["created_at"], "completed_at": None,
        }
        completed = dict(row, current_index=1, status="completed",
                         completed_at="2026-10-17T12:00:00",
                         responses_json=json.dumps([
                             {"index": 0, "selections": ["a"], "earned": 1, "possible": 1},
                         ]))
        connection = MagicMock()
        cursor = connection.cursor.return_value
        cursor.description = [(key,) for key in row]
        cursor.fetchall.side_effect = [[row], [], [completed]]
        mysql = storage.RNXcelStore(lambda: connection)
        result = mysql.answer("alice", attempt["id"], 0, ["a"])
        self.assertEqual(result["status"], "completed")
        calls = cursor.execute.call_args_list
        self.assertIn("FOR UPDATE", calls[0].args[0])
        self.assertEqual(calls[0].args[1], ("alice", attempt["id"]))
        self.assertIn("WHERE owner_id = %s AND id = %s", calls[1].args[0])
        connection.begin.assert_called_once_with()
        connection.commit.assert_called_once_with()
        connection.rollback.assert_not_called()

    def test_mysql_create_locks_publication_and_commits_authoritative_snapshot(self):
        connection = MagicMock()
        cursor = connection.cursor.return_value
        saved = {}

        def execute(sql, parameters):
            if sql.startswith("SELECT version_id"):
                row = {
                    "version_id": self.first["version_id"],
                    "publication_status": "published", "exam_profile": "NCLEX_RN_2026",
                    "authored_json": json.dumps(self.first),
                }
                cursor.description = [(key,) for key in row]
                cursor.fetchall.return_value = [row]
            elif sql.startswith("INSERT INTO rnxcel_attempts"):
                saved.update(zip(
                    ("id", "owner_id", "snapshot_json", "responses_json", "current_index",
                     "status", "created_at", "completed_at"), parameters,
                ))
                cursor.description = None
            elif sql.startswith("SELECT id"):
                cursor.description = [(key,) for key in saved]
                cursor.fetchall.return_value = [dict(saved)]
            else:
                raise AssertionError(f"Unexpected SQL: {sql}")

        cursor.execute.side_effect = execute
        mysql = storage.RNXcelStore(lambda: connection)
        result = mysql.create("alice", [self.first])
        self.assertEqual(result["snapshot"], [self.first])
        self.assertEqual(result["status"], "active")
        calls = cursor.execute.call_args_list
        self.assertIn("FOR UPDATE", calls[0].args[0])
        self.assertEqual(calls[0].args[1], ("version-1",))
        self.assertIn("INSERT INTO rnxcel_attempts", calls[1].args[0])
        self.assertEqual(calls[2].args[1], ("alice", result["id"]))
        connection.begin.assert_called_once_with()
        connection.commit.assert_called_once_with()
        connection.rollback.assert_not_called()
        connection.close.assert_called_once_with()

    def test_mysql_answer_write_error_rolls_back_without_commit(self):
        attempt = self.attempt()
        row = {
            "id": attempt["id"], "owner_id": "alice",
            "snapshot_json": json.dumps(attempt["snapshot"]), "responses_json": "[]",
            "current_index": 0, "status": "active",
            "created_at": attempt["created_at"], "completed_at": None,
        }
        connection = MagicMock()
        cursor = connection.cursor.return_value
        cursor.description = [(key,) for key in row]
        cursor.fetchall.return_value = [row]
        cursor.execute.side_effect = [None, Exception(2013, "Connection lost during write")]
        mysql = storage.RNXcelStore(lambda: connection)
        with self.assertLogs(storage.logger, level="ERROR"), self.assertRaises(
            storage.RNXcelUnavailable
        ) as caught:
            mysql.answer("alice", attempt["id"], 0, ["a"])
        self.assertEqual(caught.exception.reason, "database_error")
        self.assertIn("FOR UPDATE", cursor.execute.call_args_list[0].args[0])
        connection.begin.assert_called_once_with()
        connection.rollback.assert_called_once_with()
        connection.commit.assert_not_called()
        connection.close.assert_called_once_with()

    def test_migration_only_creates_empty_nursing_tables(self):
        path = Path(__file__).resolve().parents[1] / "migrations/20261017_add_rnxcel.sql"
        sql = re.sub(r"--[^\n]*", "", path.read_text(encoding="utf-8"))
        statements = [statement.strip() for statement in sql.split(";") if statement.strip()]
        names = []
        for statement in statements:
            match = re.match(
                r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+(rnxcel_\w+)\s*\(",
                statement, re.IGNORECASE,
            )
            self.assertIsNotNone(match, f"Non-additive or non-nursing SQL: {statement}")
            names.append(match.group(1))
            self.assertIn("ENGINE=InnoDB", statement)
        self.assertCountEqual(
            names, ["rnxcel_item_versions", "rnxcel_attempts", "rnxcel_consents"],
        )
        self.assertNotRegex(sql, r"(?i)\b(INSERT|UPDATE|DELETE|TRUNCATE|ALTER|DROP|TRIGGER)\b")

    def test_mysql_missing_table_and_operational_error_are_distinct(self):
        for error, expected in (
            (Exception(1146, "table missing"), "missing_migration"),
            (Exception(2003, "connection refused"), "database_error"),
            (Exception(), "database_error"),
        ):
            connection = MagicMock()
            connection.cursor.return_value.execute.side_effect = error
            mysql = storage.RNXcelStore(lambda: connection)
            with self.subTest(reason=expected), self.assertLogs(
                storage.logger, level="ERROR"
            ), self.assertRaises(storage.RNXcelUnavailable) as caught:
                mysql.published()
            self.assertEqual(caught.exception.reason, expected)
            connection.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
