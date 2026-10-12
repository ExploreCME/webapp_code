import json
import unittest
from unittest.mock import MagicMock, patch

from flask import Flask, session
from jinja2 import ChoiceLoader, DictLoader

import flask_app
from standardizers import TASK_AREAS, TASK_ALIASES


ORGAN = "Hematologic System"
TASK = TASK_AREAS[6]
LEGACY_TASK = "Applying Basic Scientific Concepts"


class BlueprintQuizTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.secret_key = "blueprint-test-only"
        self.app.register_blueprint(flask_app.quiz_bp)
        self.app.jinja_loader = ChoiceLoader([
            DictLoader({"base.html": "{% block content %}{% endblock %}"}),
            self.app.jinja_loader,
        ])
        self.conn = MagicMock()
        self.cursor = self.conn.cursor.return_value
        self.db_patch = patch.object(flask_app, "get_db", return_value=self.conn)
        self.unique_patch = patch.object(flask_app, "is_quiz_name_unique", return_value=True)
        self.db_patch.start()
        self.unique_patch.start()
        self.addCleanup(self.db_patch.stop)
        self.addCleanup(self.unique_patch.stop)

    def row(self, question_id, organ=ORGAN, task=TASK, topic="Anemia"):
        return {"id": question_id, "os": organ, "ta": task, "to_area": topic}

    def item(self, organ=ORGAN, task=TASK, topic="any"):
        return {"organ_system": organ, "task_area": task, "topic_area": topic}

    def batch(self, blueprint, rows, use_existing=True, used_ids=()):
        self.cursor.reset_mock()
        self.cursor.fetchall.side_effect = [
            [{"question_id": question_id} for question_id in used_ids],
            rows,
        ]
        with self.app.test_request_context("/build_quiz_batch", method="POST", json={
            "quiz_name": "hematology quiz", "blueprint": blueprint,
            "use_existing": use_existing,
        }):
            session["username"] = "test-user"
            result = flask_app.build_quiz_batch().get_json()
        mappings = [
            mapping for call in self.cursor.executemany.call_args_list
            for mapping in call.args[1]
        ]
        tasks = [
            json.loads(call.args[1][3]) for call in self.cursor.execute.call_args_list
            if "INSERT INTO quiz_generation_tasks" in call.args[0]
        ]
        self.assertEqual(len(mappings), result["duplicated"])
        self.assertEqual(len({mapping[1] for mapping in mappings}), len(mappings))
        self.assertEqual(result["background_started"], bool(tasks))
        self.assertEqual(result["generated"], sum(len(task["generation_requests"]) for task in tasks))
        self.assertEqual(result["duplicated"] + result["generated"], len(blueprint))
        return result, [mapping[1] for mapping in mappings], tasks

    def test_hematology_scientific_concepts_across_topics_reuses_all(self):
        rows = [self.row(str(i), topic=topic) for i, topic in enumerate(
            ("Anemia", "Leukemia", "Lymphoma", "Platelet disorders"))]
        result, selected, tasks = self.batch([self.item(task=LEGACY_TASK)] * 4, rows)
        self.assertEqual(result, {
            "status": "success", "duplicated": 4, "generated": 0,
            "background_started": False,
        })
        self.assertCountEqual(selected, [row["id"] for row in rows])
        self.assertEqual(tasks, [])

    def test_all_task_aliases_match_in_both_directions_with_case_and_whitespace(self):
        for alias, canonical in TASK_ALIASES.items():
            for request_task, db_task in ((alias, canonical), (canonical, alias)):
                with self.subTest(request_task=request_task, db_task=db_task):
                    result, selected, _ = self.batch(
                        [self.item(organ="  hEmAtOlOgY ", task=f" {request_task.upper()} ")],
                        [self.row("match", organ=" hematologic system ", task=f" {db_task.lower()} ")],
                    )
                    self.assertEqual(result["generated"], 0)
                    self.assertEqual(selected, ["match"])

    def test_unrelated_organs_and_tasks_never_fill_shortage(self):
        rows = [
            self.row("correct"),
            self.row("wrong-organ", organ="Pulmonary System"),
            self.row("wrong-task", task=TASK_AREAS[2]),
        ]
        result, selected, tasks = self.batch([self.item(task=LEGACY_TASK)] * 3, rows)
        self.assertEqual(selected, ["correct"])
        self.assertEqual(result["generated"], 2)
        self.assertEqual(tasks[0]["generation_requests"], [self.item()] * 2)

    def test_unknown_organs_are_not_relaxed_to_misc(self):
        rows = [self.row("misc", organ="Misc"), self.row("unknown", organ="unknown category")]
        result, selected, tasks = self.batch(
            [self.item(organ="another category")], rows,
        )
        self.assertEqual(selected, [])
        self.assertEqual(result["generated"], 1)
        self.assertEqual(tasks[0]["generation_requests"], [self.item(organ="another category")])
        result, selected, _ = self.batch(
            [self.item(organ=" UNKNOWN CATEGORY ")], rows,
        )
        self.assertEqual(selected, ["unknown"])
        self.assertEqual(result["generated"], 0)

    def test_topic_filter_is_strict_but_case_and_whitespace_insensitive(self):
        result, selected, tasks = self.batch(
            [self.item(topic="  ANEMIA ")] * 2,
            [self.row("correct", topic=" anemia "), self.row("wrong-topic", topic="Leukemia")],
        )
        self.assertEqual(selected, ["correct"])
        self.assertEqual(result["generated"], 1)
        self.assertEqual(tasks[0]["generation_requests"], [self.item(topic="ANEMIA")])

    def test_any_blank_none_and_absent_topics_are_wildcards(self):
        for topic in ("any", " ANY ", "", "  ", None, "absent"):
            with self.subTest(topic=topic):
                item = self.item(topic=topic)
                if topic == "absent":
                    item.pop("topic_area")
                result, selected, _ = self.batch(
                    [item] * 2,
                    [self.row("one", topic="Anemia"), self.row("two", topic="Leukemia")],
                )
                self.assertEqual(result["generated"], 0)
                self.assertCountEqual(selected, ["one", "two"])

    def test_wildcard_organ_and_task_do_not_default_to_misc_or_diagnosis(self):
        for wildcard in ("any", " ANY ", "", " ", None):
            for item in (
                self.item(organ=wildcard), self.item(task=wildcard),
                self.item(organ=wildcard, task=wildcard),
            ):
                with self.subTest(item=item):
                    result, _, _ = self.batch([item], [self.row("match")])
                    self.assertEqual(result["generated"], 0)
        result, _, _ = self.batch([{}], [self.row("match")])
        self.assertEqual(result["generated"], 0)

    def test_duplicate_rows_and_already_used_ids_are_not_reused(self):
        result, selected, _ = self.batch(
            [self.item()] * 3,
            [self.row("used"), self.row("one"), self.row("one"), self.row("two")],
            used_ids=("used",),
        )
        self.assertCountEqual(selected, ["one", "two"])
        self.assertEqual(result["generated"], 1)

    def test_same_questions_can_be_reused_across_quizzes(self):
        for _ in range(2):
            result, selected, _ = self.batch([self.item()], [self.row("one")])
            self.assertEqual(selected, ["one"])
            self.assertEqual(result["generated"], 0)

    def test_wildcard_cannot_consume_only_specific_match(self):
        with patch.object(flask_app.random, "shuffle", side_effect=lambda values: values.sort()):
            result, selected, _ = self.batch(
                [self.item(topic="any"), self.item(topic="Anemia")],
                [self.row("anemia"), self.row("leukemia", topic="Leukemia")],
            )
        self.assertEqual(result["generated"], 0)
        self.assertCountEqual(selected, ["anemia", "leukemia"])

    def test_overlapping_organ_and_task_slots_exhaust_compatible_rows(self):
        with patch.object(flask_app.random, "shuffle", side_effect=lambda values: values.sort(reverse=True)):
            result, selected, _ = self.batch(
                [self.item(task="any"), self.item(organ="any")],
                [self.row("shared"), self.row("organ-only", task=TASK_AREAS[2])],
            )
        self.assertEqual(result["generated"], 0)
        self.assertCountEqual(selected, ["shared", "organ-only"])

    def test_create_new_bypasses_existing_pool(self):
        result, selected, tasks = self.batch(
            [self.item(task=LEGACY_TASK)] * 2, [self.row("one"), self.row("two")],
            use_existing=False,
        )
        self.assertEqual(selected, [])
        self.assertEqual(result["generated"], 2)
        self.assertEqual(tasks[0]["generation_requests"], [self.item()] * 2)
        self.assertFalse(any("FROM question_bank" in call.args[0]
                             for call in self.cursor.execute.call_args_list))

    def test_generation_fallback_uses_canonical_tasks_and_wildcard_topic(self):
        for fallback in TASK_AREAS:
            with self.subTest(fallback=fallback), \
                    patch.object(flask_app.random, "choice", return_value=fallback):
                _, _, tasks = self.batch([self.item(task="any", topic=None)], [])
                self.assertEqual(tasks[0]["generation_requests"], [self.item(task=fallback)])

    def test_empty_blueprint_neither_reads_bank_nor_queues_generation(self):
        result, selected, tasks = self.batch([], [])
        self.assertEqual(result["generated"], 0)
        self.assertEqual(selected, [])
        self.assertEqual(tasks, [])
        self.assertFalse(any("FROM question_bank" in call.args[0]
                             for call in self.cursor.execute.call_args_list))

    def form(self, task_label, use_existing="yes", topic=None, faculty=True, extra=None):
        field = task_label.replace(" ", "_").replace(",", "")
        data = {
            "quiz_name": "hematology quiz", "num_questions": "3",
            "organ_pct_Hematologic_System": "100", f"task_pct_{field}": "100",
            "use_existing": use_existing,
        }
        if topic is not None:
            data["organ_topics_Hematologic_System"] = topic
        data.update(extra or {})
        with self.app.test_request_context("/custom_quiz", method="POST", data=data):
            session["username"] = "test-user"
            session["is_faculty"] = faculty
            self.cursor.fetchone.return_value = None
            return flask_app.custom_quiz().get_json()

    def test_canonical_and_legacy_percentage_keys_create_canonical_blueprints(self):
        for label, canonical in [(task, task) for task in TASK_AREAS] + list(TASK_ALIASES.items()):
            with self.subTest(label=label):
                result = self.form(label)
                self.assertEqual(result["blueprint"], [self.item(task=canonical)] * 3)
                self.assertTrue(result["use_existing"])

    def test_canonical_field_takes_precedence_over_legacy_alias(self):
        result = self.form(LEGACY_TASK, extra={
            "task_pct_Applying_Foundational_Scientific_Concepts": "0",
            "task_pct_Clinical_Intervention": "100",
        })
        self.assertEqual(result["blueprint"], [self.item(task=TASK_AREAS[4])] * 3)

    def test_form_preference_and_topics_reach_batch(self):
        for preference in ("yes", "no"):
            with self.subTest(preference=preference):
                form_result = self.form(LEGACY_TASK, preference, topic="Anemia")
                self.assertEqual(form_result["blueprint"], [self.item(topic="Anemia")] * 3)
                self.assertEqual(form_result["use_existing"], preference == "yes")
                result, _, _ = self.batch(
                    form_result["blueprint"], [self.row(str(i)) for i in range(3)],
                    use_existing=form_result["use_existing"],
                )
                self.assertEqual(result["generated"], 0 if preference == "yes" else 3)
        self.assertTrue(self.form(LEGACY_TASK, "no", faculty=False)["use_existing"])

    def test_rendered_form_uses_canonical_fields_and_reuse_progress_contract(self):
        with self.app.test_request_context("/custom_quiz"):
            session["username"] = "test-user"
            session["is_faculty"] = True
            html = flask_app.custom_quiz()
        for task in TASK_AREAS:
            field = task.replace(" ", "_").replace(",", "")
            self.assertIn(f'name="task_pct_{field}"', html)
        self.assertIn("use_existing: data.use_existing", html)
        self.assertIn("if (batchData.background_started)", html)
        self.assertIn("Pulled from Database: ${batchData.duplicated}", html)
        self.assertIn("AI Generating: ${batchData.generated}", html)
        self.assertIn("Assembling ${blueprint.length} Questions", html)


if __name__ == "__main__":
    unittest.main()
