import copy
import unittest
from urllib.parse import parse_qs, urlsplit
from unittest.mock import MagicMock, patch

from flask import template_rendered

import display_database
import flask_app
import preview_app


class PublicPreviewTests(unittest.TestCase):
    def setUp(self):
        self.app = display_database.app
        config = self.app.config.copy()
        self.addCleanup(lambda: self.app.config.update(config))
        self.app.config.update(TESTING=True, SECRET_KEY="public-preview-test-only")
        self.client = self.app.test_client()
        self.paid = False
        self.allow_writes = False
        self.attempts = {}
        self.responses = {}
        self.rows = []
        self.queries = []
        self.missing = None
        self.db = MagicMock()
        self.cursor = self.db.cursor.return_value
        self.cursor.__enter__.return_value = self.cursor
        self.cursor.execute.side_effect = self.execute
        self.cursor.fetchone.side_effect = lambda: copy.deepcopy(self.rows[0]) if self.rows else None
        self.cursor.fetchall.side_effect = lambda: copy.deepcopy(self.rows)
        self.contexts = []

        def capture(sender, template, context, **extra):
            self.contexts.append((template.name, context))

        template_rendered.connect(capture, self.app, weak=False)
        self.addCleanup(template_rendered.disconnect, capture, self.app)
        for patcher in (
            patch.object(display_database, "MAINTENANCE_MODE", False),
            patch.object(display_database, "decode_clerk_token", return_value={"sub": "real-user"}),
            patch.object(display_database, "get_db_connection", return_value=self.db),
            patch.object(flask_app, "get_db", return_value=self.db),
            patch.object(preview_app, "get_db", return_value=self.db),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        with self.app.app_context():
            display_database.cache.clear()

    @staticmethod
    def question(qid, assignment_id=None):
        question = dict(
            id=qid, question_stem=f"Sample question {qid}", correct_answer="B",
            answer_choice_A="First", answer_choice_B="Second", answer_choice_C="Third",
            answer_choice_D="", answer_choice_E="", explanation="Sample explanation",
            organ_system="Cardiovascular System", task_area="Diagnosis", topic_area="Sample topic",
        )
        if assignment_id:
            question["assignment_id"] = assignment_id
        return question

    def execute(self, sql, params=()):
        sql = " ".join(sql.split())
        self.queries.append((sql, params))
        self.rows = []
        if not sql.startswith("SELECT"):
            if not self.allow_writes:
                self.fail(f"Unexpected learning/user write: {sql}")
            if sql.startswith("INSERT INTO quiz_attempts"):
                self.attempts[params[0]] = dict(
                    attempt_id=params[0], username=params[1], quiz_name=params[3],
                    mode=params[5], start_time=params[6],
                )
            elif "INSERT INTO attempt_responses" in sql:
                self.responses[params[1]] = dict(
                    attempt_id=params[0], question_id=params[1], user_choice=params[2],
                    correct_answer=params[3], is_correct=params[4],
                )
            else:
                self.fail(f"Unexpected normal write: {sql}")
        elif "SELECT has_paid, is_faculty" in sql:
            self.rows = [dict(has_paid=int(self.paid), is_faculty="N")]
        elif "SELECT quiz_id FROM user_quizzes" in sql:
            self.assertEqual(params, ("EOR 1 Quiz",))
            self.assertIn("BINARY quiz_name", sql)
            self.rows = [] if self.missing == "quiz" else [dict(quiz_id="sample-quiz")]
        elif "SELECT qb.*" in sql:
            self.assertEqual(params, ("sample-quiz",))
            self.rows = [] if self.missing == "questions" else [self.question(1), self.question(2)]
        elif "SELECT * FROM remediation_assignments" in sql:
            self.assertEqual(params, ("EOR 1 Remediation",))
            self.assertIn("BINARY remediation_name", sql)
            if self.missing != "remediation":
                self.rows = [dict(id=mid, remediation_name="EOR 1 Remediation",
                                  topic_area=mid, task_area="Diagnosis", summary="Sample review")
                             for mid in ("module-a", "module-b")]
        elif "SELECT * FROM remediation_questions" in sql:
            self.rows = [self.question("rq-" + params[0], params[0])]
        elif "FROM quiz_attempts WHERE attempt_id" in sql:
            attempt = self.attempts.get(params[0])
            self.rows = [attempt] if attempt else []
        elif "SELECT correct_answer FROM question_bank" in sql:
            self.rows = [dict(correct_answer="B")]
        elif "FROM quiz_attempts qa LEFT JOIN attempt_responses" in sql:
            attempt = self.attempts.get(params[0])
            if attempt:
                self.rows = [dict(attempt, questions_answered=len(self.responses),
                                  correct_answers=sum(r["is_correct"] for r in self.responses.values()))]

    def sign_in(self, paid=False):
        self.paid = paid
        self.client.set_cookie("__session", "verified-by-test-stub")
        with self.client.session_transaction() as session:
            session.update(username="real-user", accepted_eula=True, is_premium=paid)

    def assert_unsaved(self):
        self.db.commit.assert_not_called()
        self.cursor.executemany.assert_not_called()
        self.assertFalse(self.attempts)
        self.assertFalse(self.responses)
        with self.client.session_transaction() as session:
            self.assertFalse(session.get("username", "").startswith("guest_"))
            if self.client.get_cookie("__session"):
                self.assertEqual(session["username"], "real-user")
                self.assertEqual(session["is_premium"], self.paid)
        self.assertTrue(all(sql.startswith("SELECT") for sql, _ in self.queries))

    def begin(self, kind, mode="study_exp"):
        response = self.client.get(f"/preview/take/{kind}", query_string={"mode": mode})
        self.assertEqual(response.status_code, 302)
        args = {key: values[0] for key, values in parse_qs(urlsplit(response.location).query).items()}
        self.assertTrue(args["attempt_id"].startswith("preview_"))
        page = self.client.get(response.location, follow_redirects=True)
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.headers["Cache-Control"], "no-store")
        self.assertEqual(page.headers["Referrer-Policy"], "same-origin")
        self.assertNotIn("Rate this question:", page.text)
        self.assertNotIn("Sample explanation", page.text)
        self.assertEqual(self.contexts[-1][0], "index.html")
        return args["attempt_id"], self.contexts[-1][1]

    def submit(self, context, choice="B"):
        return self.client.post("/submit-answer", data={
            "question_id": context["question"]["id"], "user_choice": choice,
            "quiz_name": context["quiz_name"], "attempt_id": context["attempt_id"],
            "mode": context["mode"], "next_q_id": context["next_q_id"],
        })

    def test_landing_menu_and_legacy_entry_links(self):
        self.assertIn('href="/preview"', self.client.get("/paxcel").text)
        menu = self.client.get("/preview").text
        for kind in ("quiz", "remediation"):
            self.assertIn(f'href="/preview/take/{kind}"', menu)
            self.assertNotIn(f"/preview/setup/{kind}", menu)
            self.assertEqual(self.client.get(f"/preview/setup/{kind}").location,
                             f"/preview/take/{kind}?mode=study_no_exp")
        self.assert_unsaved()

    def test_navigation_previous_answers_and_restarts_are_isolated(self):
        token, context = self.begin("quiz")
        self.submit(context)
        page = self.client.get("/question_page", query_string={
            "attempt_id": token, "quiz_name": "EOR 1 Quiz", "q": "1",
        })
        self.assertEqual(page.status_code, 200)
        context = self.contexts[-1][1]
        self.assertEqual(context["previous_choice"], "B")
        self.assertEqual(context["grid_data"][0]["status"], "correct")
        self.assertEqual(context["next_q_id"], 2)
        page = self.client.get("/question_page", query_string={
            "attempt_id": token, "quiz_name": "EOR 1 Quiz", "q": "2",
        })
        self.assertEqual(page.status_code, 200)
        self.assertEqual(self.contexts[-1][1]["prev_q_id"], 1)
        new_token, context = self.begin("remediation")
        self.assertNotEqual(token, new_token)
        self.assertIsNone(context["previous_choice"])
        self.assertEqual(self.client.get("/quiz_results", query_string={"attempt_id": token}).status_code, 404)
        self.assert_unsaved()

    def test_complete_both_samples_for_anonymous_unpaid_and_paid_visitors(self):
        for identity in ("anonymous", "unpaid", "paid"):
            for kind in ("quiz", "remediation"):
                for mode in preview_app.PREVIEW_MODES:
                    with self.subTest(identity=identity, kind=kind, mode=mode):
                        self.client = self.app.test_client()
                        if identity != "anonymous":
                            self.sign_in(paid=identity == "paid")
                        token, context = self.begin(kind, mode)
                        self.assertNotIn("fetch('/sync_clerk_user'", self.client.get(
                            "/question_page" if kind == "quiz" else "/remediation/questions/module-a",
                            query_string=dict(attempt_id=token, quiz_name=context["quiz_name"], mode=mode),
                        ).text)
                        for index in range(2):
                            if kind == "remediation":
                                module = ("module-a", "module-b")[index]
                                page = self.client.get(f"/remediation/questions/{module}",
                                                       query_string={"attempt_id": token})
                                self.assertEqual(page.status_code, 200)
                                context = self.contexts[-1][1]
                            response = self.submit(context, "B" if index == 0 else "A")
                            self.assertEqual(response.status_code, 302)
                            page = self.client.get(response.location, follow_redirects=True)
                            self.assertEqual(page.status_code, 200)
                            if mode != "test":
                                self.assertIn("Correct!" if index == 0 else "Incorrect.", page.text)
                                self.assertEqual("Sample explanation" in page.text, mode == "study_exp")
                            if kind == "quiz" and index == 0:
                                page = self.client.get("/question_page", query_string={
                                    "attempt_id": token, "quiz_name": "EOR 1 Quiz", "go_to_num": "2",
                                })
                                self.assertEqual(page.status_code, 200)
                                context = self.contexts[-1][1]
                            self.assert_unsaved()
                        if kind == "remediation":
                            page = self.client.get("/remediation/modules/EOR 1 Remediation",
                                                   query_string={"attempt_id": token})
                            self.assertIn("All modules are complete!", page.text)
                            flashcard = self.client.get("/remediation/review/module-b",
                                                        query_string={"attempt_id": token})
                            self.assertEqual(flashcard.status_code, 200)
                        results = self.client.get("/quiz_results", query_string={"attempt_id": token})
                        self.assertEqual(results.status_code, 200)
                        self.assertEqual(self.contexts[-1][1]["attempt"]["percentage"], 50)
                        review = self.client.get("/review_attempt", query_string={"attempt_id": token})
                        self.assertEqual(review.status_code, 200)
                        self.assertEqual(review.text.count("Sample explanation"), 2)
                        self.assert_unsaved()

    def test_tampering_cannot_expand_sample_authority(self):
        for kind in ("quiz", "remediation"):
            token, context = self.begin(kind)
            name = context["quiz_name"]
            for changes in (
                {"quiz_name": "Other paid quiz"}, {"q": "unrelated-question"},
                {"attempt_id": "preview_forged"}, {"mode": "study_missed"},
                {"mode": "invalid"}, {"resume_attempt_id": "real-attempt"},
            ):
                args = dict(attempt_id=token, quiz_name=name)
                args.update(changes)
                self.assertIn(self.client.get("/question_page", query_string=args).status_code, (400, 404))
            for path in ("/remediation/questions/private", "/remediation/review/private",
                         "/remediation/modules/Other remediation"):
                self.assertEqual(self.client.get(path, query_string={"attempt_id": token}).status_code, 404)
            data = dict(attempt_id=token, quiz_name=name, mode="study_exp",
                        question_id="other-question", user_choice="A")
            self.assertEqual(self.client.post("/submit-answer", data=data).status_code, 404)
            data["question_id"] = context["question"]["id"]
            data["next_q_id"] = "private-question"
            self.assertEqual(self.client.post("/submit-answer", data=data).status_code, 404)
            other_client = self.app.test_client()
            self.assertEqual(other_client.get("/quiz_results", query_string={"attempt_id": token}).status_code, 404)
            self.assertEqual(self.client.post("/rate_question?attempt_id=" + token,
                                             json={"question_id": 1, "rating_type": "up"}).status_code, 400)
            self.assert_unsaved()

    def test_missing_invalid_and_expired_samples_fail_safely(self):
        for path, status in (("/preview/take/invalid", 404), ("/preview/take/quiz?mode=bad", 400),
                             ("/preview/take/quiz?mode=study_missed", 400)):
            self.assertEqual(self.client.get(path).status_code, status)
        for missing, kind in (("quiz", "quiz"), ("questions", "quiz"), ("remediation", "remediation")):
            self.missing = missing
            self.assertEqual(self.client.get(f"/preview/take/{kind}").status_code, 404)
        self.missing = None
        token, _ = self.begin("quiz")
        with self.app.app_context():
            display_database.cache.delete(token)
        self.assertEqual(self.client.get("/quiz_results", query_string={"attempt_id": token}).status_code, 410)
        self.assert_unsaved()

    def test_public_flags_never_authorize_arbitrary_content_or_real_attempts(self):
        for signed_in in (False, True):
            if signed_in:
                self.sign_in()
            for path in ("/question_page?quiz_name=Private", "/remediation/questions/private"):
                self.assertIn(self.client.get(path).status_code, (302, 403))
            self.assertEqual(self.client.get("/quiz_mode?is_preview=true&quiz_name=Private").status_code, 400)
            self.assertEqual(self.client.post("/start_quiz", data={
                "is_preview": "true", "quiz_name": "EOR 1 Quiz", "resume_attempt_id": "real",
            }).status_code, 400)
            for name, kind in (("EOR 1 Quiz", "quiz"), ("EOR 1 Remediation", "remediation")):
                response = self.client.post("/start_quiz", data={
                    "is_preview": "true", "quiz_name": name, "mode": "test",
                })
                self.assertEqual(response.location, f"/preview/take/{kind}?mode=test")
        self.assert_unsaved()

    def test_normal_authenticated_attempt_still_saves_after_preview(self):
        self.sign_in(paid=True)
        self.begin("quiz")
        self.allow_writes = True
        response = self.client.post("/start_quiz", data={"quiz_name": "Normal quiz", "mode": "test"})
        self.assertEqual(response.status_code, 302)
        attempt_id = next(iter(self.attempts))
        self.assertFalse(attempt_id.startswith("preview_"))
        response = self.client.post("/submit-answer", data={
            "question_id": "normal-question", "user_choice": "B", "quiz_name": "Normal quiz",
            "attempt_id": attempt_id, "mode": "test",
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.responses["normal-question"]["is_correct"], 1)
        self.assertEqual(self.db.commit.call_count, 2)
        page = self.client.get(response.location)
        self.assertEqual(page.status_code, 200)
        self.assertEqual(self.contexts[-1][1]["attempt"]["percentage"], 100)
        self.attempts[attempt_id]["username"] = "other-user"
        self.assertEqual(self.client.post("/submit-answer", data={
            "question_id": "normal-question", "user_choice": "A", "quiz_name": "Normal quiz",
            "attempt_id": attempt_id, "mode": "test",
        }).status_code, 404)
        self.assertEqual(self.db.commit.call_count, 2)


if __name__ == "__main__":
    unittest.main()
