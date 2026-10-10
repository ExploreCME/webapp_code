import io
import json
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from flask import Flask, session

import flask_app

with patch.dict(sys.modules, {"display_database": SimpleNamespace(login_required=lambda fn: fn)}):
    import faculty_routes


class DocumentRouteTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.secret_key = "route-test-only"
        self.app.register_blueprint(flask_app.quiz_bp)
        self.app.register_blueprint(faculty_routes.faculty_bp)
        self.conn = MagicMock()
        self.conn.cursor.return_value.fetchone.return_value = None
        self.conn.cursor.return_value.__enter__.return_value = self.conn.cursor.return_value
        self.document = {
            "openai_file_id": "file-test",
            "owner": "test-user",
            "original_filename": "report.pdf",
            "size_bytes": 100,
            "uploaded_at": "2026-10-10T00:00:00+00:00",
            "expires_at": "2026-10-11T00:00:00+00:00",
        }

    def context(self, path="/generate_from_document", data=None):
        context = self.app.test_request_context(path, method="POST", data=data or {
            "file": (io.BytesIO(b"pdf"), "report.pdf"),
            "quiz_name": "test quiz", "csrf_token": "test-token",
        })
        context.push()
        self.addCleanup(context.pop)
        session["username"] = "test-user"
        session["document_csrf_token"] = "test-token"

    def test_document_stored_before_quiz_and_task_commit(self):
        self.context()
        events = []
        def upload(*args):
            events.append("upload")
            return self.document
        self.conn.cursor.return_value.execute.side_effect = lambda *args: events.append("sql")
        with patch.object(flask_app, "get_db", return_value=self.conn), \
                patch.object(flask_app, "is_quiz_name_unique", return_value=True), \
                patch.object(flask_app, "upload_document", side_effect=upload):
            response = flask_app.generate_from_document()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(events[0], "upload")
        self.conn.begin.assert_called_once()
        self.conn.commit.assert_called_once()
        payload = json.loads(self.conn.cursor.return_value.execute.call_args.args[1][3])
        self.assertEqual(payload["owner"], "test-user")
        self.assertEqual(payload["openai_file_id"], self.document["openai_file_id"])
        self.assertEqual(payload["expires_at"], self.document["expires_at"])
        self.assertNotIn("object_key", payload)
        self.assertNotIn("filepath", payload)

    def test_storage_failure_never_queues(self):
        self.context()
        with patch.object(flask_app, "get_db", return_value=self.conn), \
                patch.object(flask_app, "is_quiz_name_unique", return_value=True), \
                patch.object(flask_app, "upload_document", side_effect=ValueError("Invalid PDF")):
            flask_app.generate_from_document()
        self.conn.cursor.return_value.execute.assert_not_called()
        self.conn.commit.assert_not_called()

    def test_queue_failure_rolls_back_quiz(self):
        self.context()
        self.conn.cursor.return_value.execute.side_effect = RuntimeError("db unavailable")
        with patch.object(flask_app, "get_db", return_value=self.conn), \
                patch.object(flask_app, "is_quiz_name_unique", return_value=True), \
                patch.object(flask_app, "upload_document", return_value=self.document):
            flask_app.generate_from_document()
        self.conn.rollback.assert_called_once()
        self.conn.commit.assert_not_called()

    def test_upload_requires_csrf(self):
        self.context()
        session.pop("document_csrf_token")
        with patch.object(flask_app, "upload_document") as upload:
            self.assertEqual(flask_app.generate_from_document().status_code, 400)
        upload.assert_not_called()

    def test_document_retry_endpoint_is_removed(self):
        self.assertFalse(any(
            rule.endpoint == "quiz.retry_document" or rule.rule == "/documents/<int:task_id>/retry"
            for rule in self.app.url_map.iter_rules()
        ))
        response = self.app.test_client().post("/documents/42/retry")
        self.assertEqual(response.status_code, 404)

    def test_home_lists_owner_documents_without_scanning_disk(self):
        with self.app.test_request_context("/home"):
            session["username"] = "test-user"
            with patch.object(flask_app, "get_db", return_value=self.conn), \
                    patch.object(flask_app, "list_documents", return_value=[self.document]) as listing, \
                    patch.object(flask_app, "render_template", return_value="OK") as render, \
                    patch.object(flask_app.os, "listdir", side_effect=AssertionError("local scan")):
                flask_app.home()
            listing.assert_called_once_with(self.conn.cursor.return_value, "test-user")
            self.assertEqual(render.call_args.kwargs["documents"], [self.document])

    def test_faculty_upload_uses_same_storage_metadata(self):
        self.context("/faculty/create_remediation", {
            "eor_pdf": (io.BytesIO(b"pdf"), "REPORT.PDF"),
            "remediation_name": "remediation", "csrf_token": "test-token",
        })
        with patch.object(faculty_routes, "is_user_faculty", return_value=True), \
                patch.object(faculty_routes, "is_assignment_name_unique", return_value=True), \
                patch.object(faculty_routes, "get_mysql_connection", return_value=self.conn), \
                patch.object(faculty_routes, "upload_document", return_value=self.document):
            response = faculty_routes.create_remediation()
        self.assertEqual(response.status_code, 302)
        payload = json.loads(self.conn.cursor.return_value.execute.call_args.args[1][2])
        self.assertEqual(payload["openai_file_id"], self.document["openai_file_id"])
        self.assertEqual(payload["expires_at"], self.document["expires_at"])
        self.assertEqual(payload["owner"], "test-user")
        self.assertNotIn("object_key", payload)
        self.assertEqual(payload["remediation_name"], "remediation")
        self.assertNotIn("filepath", payload)
        self.conn.commit.assert_called_once()

    def test_faculty_upload_requires_csrf(self):
        self.context("/faculty/create_remediation", {
            "eor_pdf": (io.BytesIO(b"pdf"), "report.pdf"),
            "remediation_name": "remediation", "csrf_token": "wrong-token",
        })
        with patch.object(faculty_routes, "is_user_faculty", return_value=True), \
                patch.object(faculty_routes, "upload_document") as upload:
            response = faculty_routes.create_remediation()
        self.assertEqual(response.status_code, 400)
        upload.assert_not_called()

    def test_faculty_upload_failure_never_queues(self):
        self.context("/faculty/create_remediation", {
            "eor_pdf": (io.BytesIO(b"pdf"), "report.pdf"),
            "remediation_name": "remediation", "csrf_token": "test-token",
        })
        with patch.object(faculty_routes, "is_user_faculty", return_value=True), \
                patch.object(faculty_routes, "is_assignment_name_unique", return_value=True), \
                patch.object(faculty_routes, "get_mysql_connection", return_value=self.conn), \
                patch.object(faculty_routes, "upload_document", side_effect=ValueError("Invalid PDF")):
            faculty_routes.create_remediation()
        self.conn.cursor.return_value.execute.assert_not_called()
        self.conn.commit.assert_not_called()
        self.conn.rollback.assert_called_once()
        self.conn.close.assert_called_once()

    def test_faculty_queue_failure_rolls_back(self):
        self.context("/faculty/create_remediation", {
            "eor_pdf": (io.BytesIO(b"pdf"), "report.pdf"),
            "remediation_name": "remediation", "csrf_token": "test-token",
        })
        self.conn.cursor.return_value.execute.side_effect = RuntimeError("db unavailable")
        with patch.object(faculty_routes, "is_user_faculty", return_value=True), \
                patch.object(faculty_routes, "is_assignment_name_unique", return_value=True), \
                patch.object(faculty_routes, "get_mysql_connection", return_value=self.conn), \
                patch.object(faculty_routes, "upload_document", return_value=self.document):
            faculty_routes.create_remediation()
        self.conn.rollback.assert_called_once()
        self.conn.commit.assert_not_called()
        self.conn.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
