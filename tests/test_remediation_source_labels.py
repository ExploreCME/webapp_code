import asyncio
import json
from html.parser import HTMLParser
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from flask import Flask, g, session
from jinja2 import ChoiceLoader, DictLoader, FileSystemLoader

import create_remediation_assignment as remediation
import flask_app
from remediation_display import remediation_groups, remediation_label


ROOT = Path(__file__).resolve().parents[1]
TASK = "Formulating the Most Likely Diagnosis"
PRENATAL = "OB - Prenatal care/normal pregnancy"


class AsyncSQLite:
    """Exercise production insert/lookup SQL against an isolated in-memory DB."""

    def __init__(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE remediation_assignments (
                id TEXT PRIMARY KEY, username TEXT, username_norm TEXT,
                remediation_name TEXT, remediation_name_norm TEXT,
                organ_system TEXT, organ_system_norm TEXT, task_area TEXT,
                task_area_norm TEXT, topic_area TEXT, topic_area_norm TEXT,
                summary TEXT, diagram_code TEXT, diagram_explanation TEXT,
                created_date TEXT, source_organ_system TEXT);
            CREATE TABLE remediation_questions (
                id TEXT PRIMARY KEY, assignment_id TEXT, question_stem TEXT,
                answer_choice_A TEXT, answer_choice_B TEXT, answer_choice_C TEXT,
                answer_choice_D TEXT, answer_choice_E TEXT, correct_answer TEXT,
                explanation TEXT, difficulty_rating INTEGER);
        """)
        self.result = None

    def cursor(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def execute(self, sql, params):
        self.result = self.db.execute(sql.replace("%s", "?"), params)

    async def executemany(self, sql, params):
        self.db.executemany(sql.replace("%s", "?"), params)

    async def fetchone(self):
        row = self.result.fetchone()
        return dict(row) if row else None

    async def commit(self):
        self.db.commit()

    async def rollback(self):
        self.db.rollback()

    def close(self):
        pass


class ModuleLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
        self.headings = []
        self.in_heading = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "a":
            self.links.append(attrs.get("href", ""))
        if attrs.get("class") == "remediation-source-heading":
            self.in_heading = True

    def handle_endtag(self, tag):
        if tag == "h3":
            self.in_heading = False

    def handle_data(self, data):
        if self.in_heading:
            self.headings.append(data)


class RemediationSourceLabelTests(unittest.TestCase):
    def setUp(self):
        self.conn = AsyncSQLite()
        self.addCleanup(self.conn.db.close)
        patcher = patch.object(remediation, "get_async_db_connection",
                               AsyncMock(return_value=self.conn))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.app = Flask(__name__)
        self.app.secret_key = "source-label-test-only"
        self.app.register_blueprint(flask_app.quiz_bp)
        self.app.jinja_loader = ChoiceLoader([
            DictLoader({"base.html": "{% block content %}{% endblock %}"}),
            FileSystemLoader(str(ROOT / "templates" / "template standardization")),
        ])

    def item(self, label=PRENATAL, organ="Reproductive System"):
        return remediation.EORExtractionItem(
            organ_system=organ, source_organ_system=label,
            task_area="Formulating Most Likely Diagnosis", topic_area="Specific source condition")

    def manager(self, items):
        manager = remediation.ConversationManager.__new__(remediation.ConversationManager)
        manager.system_message = "Extract"
        manager.client = MagicMock()
        manager.client.close = AsyncMock()
        manager.client.files.create = AsyncMock(return_value=SimpleNamespace(id="file-local"))
        manager.client.files.delete = AsyncMock()
        manager.client.beta.chat.completions.parse = AsyncMock(return_value=SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(
                parsed=remediation.EORResponse(extracted_topics=items)))]))
        manager.chat_completion = AsyncMock(side_effect=lambda *args: {"summary": "Scoped summary"})
        return manager

    def modules(self):
        return [dict(row) for row in self.conn.db.execute(
            "SELECT * FROM remediation_assignments ORDER BY rowid")]

    def render_module_route(self, modules):
        cursor = MagicMock()
        cursor.fetchall.return_value = modules
        cursor.fetchone.side_effect = [
            row for _ in modules for row in ({"c": 1}, {"a": 0, "cor": 0})]
        db = MagicMock()
        db.cursor.return_value = cursor
        with self.app.test_request_context("/remediation/modules/Upload?attempt_id=attempt"):
            session["username"] = "owner"
            g.db = db
            html = flask_app.remediation_modules("Upload")
        query, params = cursor.execute.call_args_list[0].args
        self.assertIn("source_organ_system", query)
        self.assertEqual(params, ("Upload",))
        return html, cursor

    def test_extraction_generation_save_reload_list_and_select_separate_sources(self):
        labels = [PRENATAL, "OB - Pregnancy complications",
                  "Psychiatry - Anxiety", "Psychiatry - Mood disorders"]
        items = [self.item(label, "Psychiatry/Behavioral Science" if label.startswith("Psych")
                           else "Reproductive System") for label in labels]
        manager = self.manager(items)
        verifier = MagicMock(chat_completion=AsyncMock(
            return_value={"is_valid": True, "confidence_score": 9}))
        verifier.client.close = AsyncMock()
        core = MagicMock()
        core.client.close = AsyncMock()

        async def questions(**kwargs):
            item = kwargs["item_dict"]
            self.assertEqual(kwargs["allowed_organ_systems"], (item["organ_system"],))
            return [{**item, "question_stem": item["organ_system"], "correct_answer": "A"}]

        batch = AsyncMock(side_effect=questions)
        with patch.object(remediation, "ConversationManager", side_effect=[manager, verifier]), \
                patch.object(remediation, "CoreConversationManager", return_value=core) as core_factory, \
                patch.object(remediation, "process_topic_batch", batch), \
                patch.object(remediation.difflib, "get_close_matches") as fuzzy:
            ids = asyncio.run(remediation.background_process_document(
                None, "owner", "Upload", uploaded_file_id="file-worker"))
        fuzzy.assert_not_called()
        manager.client.files.create.assert_not_called()
        manager.client.beta.chat.completions.parse.assert_awaited_once()
        prompts = [call.args[0] for call in manager.chat_completion.call_args_list]
        self.assertTrue(any(PRENATAL in prompt for prompt in prompts))
        verification_contexts = [call.kwargs.get("system_message", "")
                                 for call in core_factory.call_args_list]
        self.assertTrue(any(PRENATAL in context for context in verification_contexts))
        modules = self.modules()
        self.assertEqual(set(ids), {m["id"] for m in modules})
        self.assertEqual({m["source_organ_system"] for m in modules}, set(labels))
        self.assertEqual({m["organ_system"] for m in modules},
                         {"Reproductive System", "Psychiatry/Behavioral Science"})
        html, cursor = self.render_module_route(modules)
        parsed = ModuleLinks()
        parsed.feed(html)
        self.assertCountEqual(parsed.headings, labels)
        self.assertNotIn(">Reproductive System<", html)
        self.assertEqual(len(list(remediation_groups(modules))), 4)
        for module in modules:
            self.assertTrue(any("/remediation/review/" + module["id"] + "?" in url
                                for url in parsed.links))
            question = self.conn.db.execute(
                "SELECT * FROM remediation_questions WHERE assignment_id = ?",
                (module["id"],)).fetchone()
            self.assertEqual(question["question_stem"], module["source_organ_system"])
            # Progress and selection are still keyed by exact module IDs, not labels.
            self.assertIn((module["id"],), [c.args[1] for c in cursor.execute.call_args_list])
            question_cursor = MagicMock()
            question_cursor.fetchone.return_value = {"remediation_name": "Upload"}
            question_cursor.fetchall.return_value = [dict(question)]
            db = MagicMock()
            db.cursor.return_value = question_cursor
            with self.app.test_request_context("/remediation/questions/" + module["id"]), \
                    patch.object(flask_app, "render_template", return_value="question") as render:
                session["username"] = "owner"
                g.db = db
                flask_app.remediation_question_page(module["id"])
            self.assertEqual(render.call_args.kwargs["question"]["id"], question["id"])
            self.assertEqual(render.call_args.kwargs["total_questions"], 1)
            question_query = question_cursor.execute.call_args_list[1]
            self.assertIn("WHERE assignment_id = %s", question_query.args[0])
            self.assertEqual(question_query.args[1], (module["id"],))

    def test_source_wins_and_legacy_null_empty_missing_fall_back(self):
        for source in (None, "", "   "):
            module = {"organ_system": "Reproductive System", "source_organ_system": source}
            self.assertEqual(remediation_label(module), "Reproductive System")
        self.assertEqual(remediation_label({"organ_system": "Renal System"}), "Renal System")
        modules = [
            dict(id=str(i), topic_area="Topic", task_area=TASK, organ_system="Reproductive System",
                 source_organ_system=source)
            for i, source in enumerate([PRENATAL, None, "", "   "])]
        html, _ = self.render_module_route(modules)
        parsed = ModuleLinks()
        parsed.feed(html)
        self.assertEqual(parsed.headings, [PRENATAL, "Reproductive System"])
        self.assertEqual(self.item().organ_system, "Reproductive System")
        item = remediation.EORExtractionItem(
            organ_system="Reproductive System", task_area="Formulating Most Likely Diagnosis",
            topic_area="Topic")
        self.assertEqual(item.source_organ_system, "")

    def test_cache_never_borrows_source_or_other_upload_owner_provenance(self):
        data = {"organ_system": "Reproductive System", "task_area": TASK, "topic_area": "Topic"}
        ids = asyncio.run(remediation.save_to_database("owner", [
            data, {**data, "source_organ_system": PRENATAL}], "Upload"))

        def lookup(**kwargs):
            return asyncio.run(remediation.check_existing_remediation(
                self.conn, data["organ_system"], TASK, "Topic", **kwargs))

        self.assertEqual(lookup(username="owner", remediation_name="Upload"), {"id": ids[0]})
        for kwargs in (
            {"username": "other", "remediation_name": "Upload"},
            {"username": "owner", "remediation_name": "Other upload"},
            {"username": "owner", "remediation_name": "Upload", "source_organ_system": PRENATAL},
            {"username": "owner", "remediation_name": "Upload",
             "source_organ_system": "OB - Pregnancy complications"},
            {},
        ):
            self.assertIsNone(lookup(**kwargs))
        self.assertIsNone(self.modules()[0]["source_organ_system"])
        self.conn.db.execute("DELETE FROM remediation_assignments WHERE id = ?", (ids[0],))
        self.assertIsNone(lookup(username="owner", remediation_name="Upload"))
        self.assertEqual(self.modules()[0]["source_organ_system"], PRENATAL)

    def test_local_file_path_uses_same_source_preserving_extraction(self):
        manager = self.manager([self.item()])
        verifier = self.manager([])
        result = {"organ_system": "Reproductive System", "source_organ_system": PRENATAL}
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(remediation, "ConversationManager", side_effect=[manager, verifier]), \
                patch.object(remediation, "sem_process_topic_set", AsyncMock(return_value=result)), \
                patch.object(remediation.difflib, "get_close_matches") as fuzzy:
            path = Path(directory) / "source.pdf"
            path.write_bytes(b"mock-pdf")
            ids = asyncio.run(remediation.background_process_document(
                str(path), "owner", "Local Upload"))
        fuzzy.assert_not_called()
        self.assertEqual(self.modules()[0]["source_organ_system"], PRENATAL)
        self.assertEqual(len(ids), 1)
        manager.client.files.create.assert_awaited_once()
        manager.client.files.delete.assert_awaited_once_with("file-local")

    def test_malicious_source_heading_is_escaped_in_all_remediation_surfaces(self):
        label = '<img src=x onerror="alert(1)"> & </script><script>alert(2)</script>'
        module = dict(id="safe-id", organ_system="Reproductive System", source_organ_system=label,
                      topic_area="Topic", task_area=TASK, remediation_name="Upload",
                      summary="Summary", diagram_code="", questions=[], answered=0,
                      total=1, is_complete=False)
        asyncio.run(remediation.save_to_database(
            "owner", [{key: value for key, value in module.items() if key != "id"}], "Upload"))
        # Reload the untrusted value from storage, as the production views do.
        module["source_organ_system"] = self.modules()[0]["source_organ_system"]
        import display_database
        import faculty_routes
        self.app.register_blueprint(faculty_routes.faculty_bp)
        templates = [
            "remediation_module_list.html", "remediation_flashcard.html",
            "faculty_review_remediation_details.html",
            "faculty_review_remediation_questions.html", "faculty_remediation_details.html"]
        with self.app.test_request_context():
            for template in templates:
                with self.subTest(template=template):
                    html = self.app.jinja_env.get_template(template).render(
                        modules=[module], assignment=module, module=module, questions=[],
                        remediation_name="Upload", attempt_id="attempt", all_complete=False)
                    self.assertNotIn(label, html)
                    self.assertNotIn("<img src=x", html)
                    self.assertIn("&lt;img", html)
                    self.assertNotIn("</script><script>alert(2)", html)

    @unittest.skipUnless(shutil.which("node"), "Node is required for the live-search behavior test")
    def test_live_search_distinguishes_source_headings_and_hides_empty_groups(self):
        template = (ROOT / "templates/template standardization" /
                    "faculty_review_remediation_details.html").read_text()
        script = re.search(r"<script>(.*?)</script>", template, re.S).group(1)
        harness = """
            const assert = require('node:assert/strict');
            let onInput;
            const input = {value: '', addEventListener: (_, fn) => onInput = fn};
            const noResults = {style: {}};
            const items = LABELS.map(name => ({dataset: {name}, style: {}}));
            const groups = items.map(item => ({
                style: {}, querySelectorAll: () => [item]
            }));
            const document = {
                addEventListener: (_, fn) => fn(),
                getElementById: id => id === 'module-search' ? input : noResults,
                querySelectorAll: selector => selector === '.module-item' ? items : groups
            };
        """.replace("LABELS", json.dumps([PRENATAL, "OB - Pregnancy complications"]))
        assertions = """
            input.value = 'prenatal care/normal pregnancy';
            onInput.call(input);
            assert.deepEqual(items.map(i => i.style.display), ['', 'none']);
            assert.deepEqual(groups.map(g => g.style.display), ['', 'none']);
            input.value = 'pregnancy complications';
            onInput.call(input);
            assert.deepEqual(groups.map(g => g.style.display), ['none', '']);
            input.value = 'missing heading';
            onInput.call(input);
            assert.equal(noResults.style.display, 'block');
            input.value = '';
            onInput.call(input);
            assert.deepEqual(groups.map(g => g.style.display), ['', '']);
        """
        result = subprocess.run(["node", "-e", harness + script + assertions],
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
