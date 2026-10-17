"""Isolated nonclinical screen fixtures; optionally export static comparison HTML.

Set RNXCEL_VISUAL_OUTPUT to an absolute, disposable output directory to export
before/after nursing and PA screens. No application fixture routes are installed.
The fixed baseline is used only for optional evidence, never test assertions.
"""

import os
from pathlib import Path
import re
import subprocess
import unittest

from jinja2 import ChoiceLoader, DictLoader

import test_rnxcel_routes as nursing_routes


BASELINE = "ac39dc755ff9b7106f2ce797ad3029161e8fae26"
ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_PREFIX = "templates/template standardization/"


def static_html(html):
    html = re.sub(r"<script\b[^>]*>.*?</script[^>]*>", "", html,
                  flags=re.IGNORECASE | re.DOTALL)
    return re.sub(
        r"""(\b(?:src|href)\s*=\s*["'])/static/""",
        lambda match: match[1] + (ROOT / "static").as_uri() + "/",
        html, flags=re.IGNORECASE,
    )


class RNXcelVisualTests(unittest.TestCase):
    def setUp(self):
        # Compose, rather than inherit, so unittest does not rerun the route suite.
        self.fixture = nursing_routes.RNXcelEndToEndTests(methodName="runTest")
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.app = self.fixture.app
        self.client = self.fixture.client
        self.output = None
        output = os.environ.get("RNXCEL_VISUAL_OUTPUT")
        if output:
            self.output = Path(output)
            if not self.output.is_absolute():
                raise ValueError("RNXCEL_VISUAL_OUTPUT must be an absolute path")
            self.output.mkdir(parents=True, exist_ok=True)

    @classmethod
    def baseline_environment(cls, app):
        if not hasattr(cls, "_baseline_sources"):
            try:
                names = subprocess.check_output(
                    ["git", "ls-tree", "-r", "--name-only", BASELINE, "--", TEMPLATE_PREFIX],
                    cwd=ROOT, text=True, stderr=subprocess.DEVNULL,
                ).splitlines()
            except (OSError, subprocess.CalledProcessError):
                cls._baseline_sources = None
                return None
            # Overlay every original template, including all base/rnxcel sources,
            # so an original include never accidentally resolves to a new partial.
            cls._baseline_sources = {
                name.removeprefix(TEMPLATE_PREFIX): subprocess.check_output(
                    ["git", "show", f"{BASELINE}:{name}"], cwd=ROOT, text=True,
                )
                for name in names if name.endswith(".html")
            }
        if not cls._baseline_sources:
            return None
        return app.jinja_env.overlay(loader=ChoiceLoader([
            DictLoader(cls._baseline_sources), app.jinja_env.loader,
        ]))

    def export(self, name, template, context):
        if self.output is None:
            return
        with self.app.test_request_context("/"):
            context = dict(context, clerk_publishable_key="")
            for revision, environment in (
                ("before", self.baseline_environment(self.app)),
                ("after", self.app.jinja_env),
            ):
                if environment is None:
                    continue
                html = environment.get_template(template).render(context)
                # Evidence is inert: no Clerk initialization, fetches, or writes.
                html = static_html(html)
                (self.output / f"{name}-{revision}.html").write_text(html, encoding="utf-8")

    def test_static_exports_strip_malformed_script_end_tags_but_preserve_escaped_text(self):
        escaped = "&lt;script&gt;NONCLINICAL authored text&lt;/script&gt;"
        html = (
            '<p>' + escaped + '</p>'
            '<ScRiPt src="https://invalid.example/clerk.js">live()</ScRiPt\t\n bar>'
            '<script>another()</script extra><p>Safe fixture text</p>'
        )
        self.assertEqual(
            static_html(html), f"<p>{escaped}</p><p>Safe fixture text</p>",
        )

    def test_static_export_assets_resolve_from_file_urls_without_a_server(self):
        asset = ROOT / "static" / "ExploreCME_pricing.png"
        self.assertTrue(asset.is_file())
        self.assertEqual(
            static_html('<img src="/static/ExploreCME_pricing.png" alt="Fixture">'),
            f'<img src="{asset.as_uri()}" alt="Fixture">',
        )

    def screen(self, name, path, *markers):
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200, response.text)
        for marker in markers:
            self.assertIn(marker, response.text)
        template, context = self.fixture.templates[-1]
        self.export(f"rn-{name}", template, context)
        return response, context

    def publish(self):
        versions = [
            nursing_routes.authored_item("visual-sba"),
            nursing_routes.authored_item("visual-sata", "select-all-that-apply"),
            nursing_routes.authored_case(),
        ]
        self.fixture.publish(*versions)
        return versions

    def test_empty_dashboard_setup_history_and_progress(self):
        _, dashboard = self.screen("dashboard-empty", "/rnxcel/dashboard",
                                   "No available content", "No attempts yet")
        self.assertEqual(dashboard["available"], 0)
        for experience in ("single-best-answer", "select-all-that-apply", "unfolding-case-study"):
            response, context = self.screen(
                f"setup-empty-{experience}", f"/rnxcel/practice?experience={experience}",
            )
            self.assertEqual(context["available"], 0)
            self.assertEqual(context["start_count"], 0)
            self.assertRegex(response.text, r"<button[^>]*disabled")
        _, history = self.screen("history-empty", "/rnxcel/attempts")
        self.assertEqual(history["history"], [])
        _, progress = self.screen("progress-empty", "/rnxcel/progress")
        self.assertEqual(progress["completed_count"], 0)

    def test_populated_dashboard_setup_history_and_progress(self):
        versions = self.publish()
        path = self.fixture.start()
        self.assertEqual(self.fixture.answer(path, 0, ["tile-a"]).status_code, 303)
        active = self.fixture.start("select-all-that-apply")
        _, dashboard = self.screen("dashboard-populated", "/rnxcel/dashboard", active, path)
        self.assertEqual(dashboard["available"], 8)
        self.assertEqual(len(dashboard["history"]), 2)
        for version in versions:
            count = 6 if "stages" in version else 1
            _, context = self.screen(
                f"setup-populated-{version['type']}",
                f"/rnxcel/practice?experience={version['type']}&count={count}",
            )
            self.assertGreater(context["available"], 0)
            self.assertGreater(context["start_count"], 0)
        _, history = self.screen("history-populated", "/rnxcel/attempts", active, path)
        self.assertEqual({attempt["status"] for attempt in history["history"]},
                         {"active", "completed"})
        _, progress = self.screen("progress-populated", "/rnxcel/progress")
        self.assertEqual(progress["completed_count"], 1)
        self.assertTrue(any(progress["reports"].values()))

    def test_sba_sata_and_first_and_later_case_players(self):
        versions = self.publish()
        for version, input_type in zip(versions[:2], ("radio", "checkbox")):
            path = self.fixture.start(version["type"])
            response, context = self.screen(f"player-{version['type']}", path, version["stem"])
            self.assertIn(f'type="{input_type}"', response.text)
            self.assertEqual(context["attempt"]["current_index"], 0)
            self.fixture.assert_no_feedback(response, versions)
        case = versions[2]
        path = self.fixture.start("unfolding-case-study", 6)
        for index in range(3):
            if index in (0, 2):
                response, context = self.screen(
                    f"player-case-stage-{index + 1}", path, case["stages"][index]["item"]["stem"],
                )
                self.assertEqual(context["attempt"]["current_index"], index)
                self.fixture.assert_no_feedback(response, versions)
                for position, stage in enumerate(case["stages"]):
                    text = stage["exhibits"][0]["text"]
                    if position <= index:
                        self.assertIn(text, response.text)
                    else:
                        self.assertNotIn(text, response.text)
            self.assertEqual(self.fixture.answer(
                path, index, case["stages"][index]["item"]["correct_option_ids"],
            ).status_code, 303)

    def test_completed_results_and_review_for_all_response_formats(self):
        for version in self.publish():
            items = [stage["item"] for stage in version["stages"]] if "stages" in version else [version]
            path = self.fixture.start(version["type"], len(items))
            for index, item in enumerate(items):
                self.assertEqual(self.fixture.answer(
                    path, index, item["correct_option_ids"],
                ).status_code, 303)
            response, context = self.screen(
                f"results-{version['type']}", path + "/results", "Points earned",
            )
            expected = sum(len(item["correct_option_ids"]) for item in items)
            self.assertEqual(context["attempt"]["earned"], expected)
            self.assertEqual(context["attempt"]["possible"], expected)
            for item in items:
                self.assertNotIn(item["rationale"], response.text)
            review, context = self.screen(
                f"review-{version['type']}", path + "/review", "Rationale",
            )
            self.assertEqual(len(context["review_items"]), len(items))
            for item in items:
                self.assertIn(item["rationale"], review.text)

    def test_pa_template_fixture_exports_without_pa_routes_or_storage(self):
        attempt = dict(
            attempt_id="nonclinical-pa-fixture", quiz_name="NONCLINICAL toy sorting",
            start_time="2026-01-01 09:00", mode="study_exp", status="completed",
            score_display="1 / 2", percentage=50, correct=1, incorrect=1,
        )
        question = dict(
            id=1, question_stem="NONCLINICAL TEST ONLY: sort the toy tiles.",
            answer_choice_A="Amber toy tile", answer_choice_B="Blue toy tile",
            answer_choice_C="Copper toy tile", answer_choice_D="", answer_choice_E="",
            correct_answer="A", explanation="Toy sorting fixture rationale.",
            correct=1, incorrect=1, choice_a_count=1, choice_b_count=1,
            choice_c_count=0, choice_d_count=0, choice_e_count=0,
        )
        common = dict(
            session={}, is_premium=True, is_faculty=False, is_app=False,
            clerk_publishable_key="", upcoming_assignments=[],
            quiz_name=attempt["quiz_name"], attempt=attempt, is_standardized=False,
        )
        scenarios = {
            "dashboard": ("student_dashboard.html", {}),
            "setup": ("custom_quiz.html", dict(
                organ_data={"Toy colors": ["Amber", "Blue"]}, task_areas=["Sort toy tiles"],
            )),
            "history": ("quiz_attempts.html", dict(attempts=[
                attempt, dict(attempt, attempt_id="nonclinical-pa-active", status="in_progress"),
            ])),
            "results": ("quiz_results.html", dict(is_remediation=False, module_breakdown=[])),
            "review": ("review_attempt.html", dict(
                faculty_return_url=None,
                responses=[dict(question, user_choice="B", is_correct=False,
                                submitted_time="2026-01-01 09:01")],
            )),
            "player": ("index.html", dict(
                question=question, current_question_number=1, total_questions=2,
                mode="study", answered=False, previous_choice=None,
                attempt_id=attempt["attempt_id"], next_q_id=2, prev_q_id=None,
                exit_url=None, grid_data=[
                    dict(status="unanswered", is_current=index == 0,
                         question_id=index + 1, question_number=index + 1)
                    for index in range(2)
                ],
            )),
        }
        with self.app.test_request_context("/"):
            for name, (template, extra) in scenarios.items():
                with self.subTest(screen=name):
                    context = dict(common, **extra)
                    html = self.app.jinja_env.get_template(template).render(context)
                    self.assertIn("<!DOCTYPE html>", html)
                    self.assertNotIn("PRIVATE-RATIONALE-", html)
                    self.export(f"pa-{name}", template, context)
        self.assertEqual(self.fixture.templates, [])
        self.assertFalse(any("pa_" in query.lower() for query in self.fixture.queries))


if __name__ == "__main__":
    unittest.main()
