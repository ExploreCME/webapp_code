"""Shared report contracts and PA presentation regressions (no database required)."""

from html.parser import HTMLParser
import hashlib
import json
from pathlib import Path
import re
import unittest

from jinja2 import ChoiceLoader, DictLoader, Environment, FileSystemLoader

from rnxcel_catalog import (
    CLINICAL_JUDGMENT, ITEM_EXPERIENCES, OBJECTIVES, RNXCEL_CATEGORIES,
)


ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "templates" / "template standardization"
BASES = {
    "base.html": (
        "<html><head>{% block head %}{% endblock %}</head>"
        "<body>{% block content %}{% endblock %}</body></html>"
    ),
    "rnxcel_base.html": (
        "<html><head>{% block nursing_head %}{% endblock %}</head>"
        '<body><div class="rnxcel">{% block nursing_content %}'
        "{% endblock %}</div></body></html>"
    ),
}

# Normalized DOM/CSS from the original PA templates at
# ac39dc755ff9b7106f2ce797ad3029161e8fae26, rendered with the contexts below.
# Fixed digests keep these regressions independent of git history after merging.
# Standardized history includes CSRF tokens in the resume forms.
PA_RENDER_DIGESTS = {
    "history-False-0": "61961d80d03fad33c8c21b2306d68f43fcb11c50d61973f2c6642a53c2cd3cac",
    "history-False-3": "63403c74d6fab423165f6b79c16fa570a45214ff9a5b2217c77ef06407db6962",
    "history-True-0": "5a94855780c256988384062be386c3d7946aabf853a0dd30afb017bb7a36a099",
    "history-True-3": "d5c700cbaa974f9f960f97712e7c63eb200f3b56e27ef297c2089f6b844b033c",
    "results-0-False": "d768729ac421ee8148ef940f71833c6a84e80ca4a02db75f3718d543b0058221",
    "results-0-True": "33aad0930be99e901b643cc1dedecdd61b69cfad36fca765a4bc3a7c22acff15",
    "results-59-False": "248c6acbee98e61e6691d5d6ae501b1ca4e6023fb0340b66b80d05f31fe88e3c",
    "results-59-True": "a1a611b38c580a61e85ca141309207766ac9863efc65aaa7c7cc701fb202b878",
    "results-60-False": "48d58a6f175f163e5fadd87fd6bb90db478e8dc5fd793c384b8e69c8cb1b323d",
    "results-60-True": "c5c56aa7d491071d4bd86239da866712b67b64b8cf660b5061571130c8976de0",
    "results-79-False": "f095b2fbbafce5ad694dbfb97e3e43faa46343cd400c72968fd996b4380d3ceb",
    "results-79-True": "d339e55f0141467eb7c67150028760a5b5705f33df4f302e34dd62e7e90738aa",
    "results-80-False": "e295981ba0463a3474fa94e53166ff4198f57bf79144d177e75e1e350f1aaded",
    "results-80-True": "54165e787393518fe8620fc9ebe8cb8b4d1cee88f02ec1fc7395f8e0b605d91d",
    "results-100-False": "27f5b99cc7920f25b8b3895422dff79e037767aecb08460d54337ffd59cfe724",
    "results-100-True": "6a971bda90273c6137f1e63e5c119b71dd09ce691065de2f8d1593288817f455",
    "review-False-0": "6a4e128957cfeca5b4e4067b4a6c3b08d41f11e98ed39dd47fc64a29ec2f0ec6",
    "review-False-3": "75c0f314edcf7b2eb3acb3a888582e6aec7ab2bca3c46c10bce5d6712eb5a55d",
    "review-True-0": "0d7a89c008902390ffedef5669dde732525eac460956146d8a8428ca523fcf8e",
    "review-True-3": "ad02db36f38682303b960ed0ba772c2291d2c77c6a97005990c3da627e1c54f4",
}


def environment(overrides=None):
    env = Environment(
        loader=ChoiceLoader([
            DictLoader({**BASES, **(overrides or {})}),
            FileSystemLoader(TEMPLATES),
        ]),
        autoescape=True,
    )
    env.globals["url_for"] = lambda endpoint, **kw: (
        "/" + endpoint + "?" + "&".join(f"{key}={value}" for key, value in kw.items())
    )
    return env


class Presentation(HTMLParser):
    """Compare semantic markup, ignoring indentation and stylesheet boundaries."""

    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.markup = []
        self.css = []
        self.in_style = False
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        if tag == "style":
            self.in_style = True
            return
        normalized = []
        for key, value in attrs:
            value = value or ""
            value = re.sub(r"\s+", "" if key == "style" else " ", value).strip()
            normalized.append((key, value))
        self.markup.append(("start", tag, sorted(normalized)))

    def handle_endtag(self, tag):
        if tag == "style":
            self.in_style = False
        else:
            self.markup.append(("end", tag))

    def handle_data(self, data):
        if self.in_style:
            self.css.append(re.sub(r"\s+", "", data))
        elif data.strip():
            self.markup.append(("text", " ".join(data.split())))


class SharedReportPresentationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env = environment()

    def render(self, name, **context):
        return self.env.get_template(name).render(
            categories=RNXCEL_CATEGORIES, objectives=OBJECTIVES,
            experiences=ITEM_EXPERIENCES, clinical_judgment=CLINICAL_JUDGMENT,
            **context,
        )

    def assert_pa_equivalent(self, baseline_key, name, **context):
        after = Presentation(self.env.get_template(name).render(**context))
        digest = hashlib.sha256(json.dumps(
            (after.markup, "".join(after.css)), ensure_ascii=False,
        ).encode()).hexdigest()
        self.assertEqual(PA_RENDER_DIGESTS[baseline_key], digest)

    def test_pa_history_matches_original_for_empty_resume_and_review(self):
        for standardized in (False, True):
            for attempts in ([], [
                dict(start_time="Today", mode=mode, score_display=score,
                     status=status, attempt_id=index)
                for index, (mode, score, status) in enumerate([
                    ("test", "In progress", "in_progress"),
                    ("study_exp", "Incomplete", "incomplete"),
                    ("study_no_exp", "2 / 3", "complete"),
                ])
            ]):
                with self.subTest(standardized=standardized, attempts=attempts):
                    self.assert_pa_equivalent(
                        f"history-{standardized}-{len(attempts)}",
                        "quiz_attempts.html", quiz_name="Quiz <demo>", attempts=attempts,
                        is_standardized=standardized,
                        session={"standardized_csrf_token": "test-token"},
                    )

    def test_pa_result_card_css_scores_links_and_remediation_match_original(self):
        for percentage in (0, 59, 60, 79, 80, 100):
            for standardized, remediation in ((False, False), (True, True)):
                with self.subTest(percentage=percentage, remediation=remediation):
                    self.assert_pa_equivalent(
                        f"results-{percentage}-{standardized}",
                        "quiz_results.html", quiz_name="Quiz <demo>",
                        attempt=dict(percentage=percentage, correct=3, incorrect=2,
                                     attempt_id="pa-attempt"),
                        is_standardized=standardized, is_remediation=remediation,
                        module_breakdown=[
                            dict(topic_area=f"Module {score}", correct=1, answered=2,
                                 percentage=score)
                            for score in (30, 70, 90)
                        ],
                    )

    def test_pa_answer_selection_explanation_and_navigation_match_original(self):
        responses = [
            dict(question_stem="Stem <unsafe>", answer_choice_A="First",
                 answer_choice_B="Second", answer_choice_C="Third",
                 answer_choice_D="", answer_choice_E="Fifth",
                 user_choice=selected, correct_answer=" b ", explanation=explanation,
                 is_correct=selected.strip().upper() == "B", submitted_time="Now")
            for selected, explanation in [
                (" b ", "Explanation <unsafe>"), ("a", ""), ("", None),
            ]
        ]
        for faculty_return_url in (None, "/faculty/review"):
            for recorded in ([], responses):
                with self.subTest(faculty=faculty_return_url, responses=recorded):
                    self.assert_pa_equivalent(
                        f"review-{bool(faculty_return_url)}-{len(recorded)}",
                        "review_attempt.html",
                        attempt=dict(quiz_name="Quiz", start_time="Today", mode="study_exp"),
                        responses=recorded, faculty_return_url=faculty_return_url,
                    )

    def test_shared_components_are_consumed_by_both_products(self):
        for shared, consumers in {
            "shared_report_table.html": [
                "quiz_attempts.html", "rnxcel_attempts.html", "rnxcel_progress.html",
            ],
            "shared_report_results.html": ["quiz_results.html", "rnxcel_results.html"],
            "shared_report_results_styles.html": ["quiz_results.html", "rnxcel_results.html"],
            "shared_report_answers.html": ["review_attempt.html", "rnxcel_results.html"],
        }.items():
            for consumer in consumers:
                with self.subTest(shared=shared, consumer=consumer):
                    self.assertIn(shared, (TEMPLATES / consumer).read_text())
        pa = self.render(
            "quiz_results.html", quiz_name="Quiz",
            attempt=dict(percentage=50, correct=1, incorrect=1, attempt_id="pa"),
        )
        rn = self.render(
            "rnxcel_results.html", attempt=self.attempt(), review_items=None,
        )
        for css_class in ("results-wrapper", "results-card", "score-circle", "stats-grid",
                          "stat-box", "stat-value", "stat-label"):
            self.assertIn(css_class, pa)
            self.assertIn(css_class, rn)
        self.assertIn(".rnxcel .results-wrapper", rn)
        self.assertIn(".rnxcel .results-wrapper *", rn)
        self.assertIn("box-sizing: content-box", rn)

    def test_shared_macros_autoescape_untrusted_values_exactly_once(self):
        html = self.env.from_string(
            '{% from "shared_report_answers.html" import answer_option, explanation %}'
            '{% from "shared_report_results.html" import results_card %}'
            "{{ answer_option('A', text, true, true) }}"
            "{{ explanation(text) }}"
            "{% call results_card(text, text, 50, [(1, text, '')]) %}{% endcall %}"
        ).render(text="<script>unsafe & authored</script>")
        self.assertEqual(
            html.count("&lt;script&gt;unsafe &amp; authored&lt;/script&gt;"), 5,
        )
        self.assertNotIn("<script>", html)
        self.assertNotIn("&amp;lt;", html)

    @staticmethod
    def attempt(**overrides):
        return dict(
            id="rn-attempt", status="completed", earned=1, possible=2,
            total_items=3, current_index=3, created_at="Today", **overrides,
        )

    def test_nursing_percentages_are_points_based_zero_safe_and_ungraded(self):
        for earned, possible, percentage in ((1, 2, "50.0"), (2, 3, "66.7"), (0, 0, "0")):
            attempt = self.attempt()
            attempt.update(earned=earned, possible=possible)
            result = self.render("rnxcel_results.html", attempt=attempt, review_items=None)
            history = self.render("rnxcel_attempts.html", history=[attempt])
            for html in (result, history):
                self.assertIn(f"{percentage}%", html)
                self.assertIn("not an NCLEX score or a pass prediction", html)
                self.assertNotIn("class=\"stat-box correct-box\"", html)
                self.assertNotIn("class=\"stat-box incorrect-box\"", html)
                self.assertNotIn('class="score-circle excellent', html)
                self.assertNotIn('class="score-circle needs-work', html)
            self.assertIn("Points earned", result)
            self.assertIn("Points possible", result)
            self.assertIn("/rnxcel.review_attempt?attempt_id=rn-attempt", result)

    def test_nursing_completed_review_supports_all_options_and_multiselect(self):
        for option_count in (2, 6, 9):
            with self.subTest(options=option_count):
                item = dict(
                    type="select-all-that-apply", case_title="Toy case <unsafe>",
                    stage_index=1, stage_count=6,
                    exhibits=[dict(timestamp="Stage 2", text="EXHIBIT <unsafe>")],
                    stem="Nonclinical toy sorting",
                    options=[dict(id=f"option-{i}", text=f"Toy choice {i}")
                             for i in range(option_count)],
                    selections=["option-0", f"option-{option_count - 1}"],
                    correct_option_ids=["option-1", f"option-{option_count - 1}"],
                    earned=1, possible=2, rationale="RATIONALE <unsafe>",
                    category_id="MOC", objective_id="MOC-01",
                )
                result = self.render(
                    "rnxcel_results.html", attempt=self.attempt(), review_items=[item],
                )
                self.assertEqual(result.count("<li "), option_count)
                self.assertEqual(result.count("Your Selection"), 2)
                self.assertEqual(
                    result.count("Correct Answer"), len(set(item["correct_option_ids"])),
                )
                self.assertIn("background-color: #d4edda", result)
                self.assertIn("background-color: #f8d7da", result)
                self.assertIn("Case stage 2 of 6", result)
                self.assertIn("EXHIBIT &lt;unsafe&gt;", result)
                self.assertIn("RATIONALE &lt;unsafe&gt;", result)
                self.assertNotIn("<unsafe>", result)
                self.assertIn("Explanation:", self.render(
                    "review_attempt.html",
                    attempt=dict(quiz_name="Quiz", start_time="Today", mode="test"),
                    responses=[dict(explanation="PA explanation")],
                ))

    def test_incomplete_attempt_never_renders_supplied_review_or_score(self):
        attempt = self.attempt()
        attempt["status"] = "active"
        html = self.render(
            "rnxcel_results.html", attempt=attempt,
            review_items=[dict(stem="PRIVATE-STEM", rationale="PRIVATE-RATIONALE",
                               exhibits=[dict(text="PRIVATE-EXHIBIT")])],
        )
        self.assertNotIn("PRIVATE", html)
        self.assertNotIn("score-percentage", html.split("</head>")[1])
        self.assertIn("Complete this attempt before viewing results", html)
        self.assertIn("/rnxcel.play?attempt_id=rn-attempt", html)
        history = self.render("rnxcel_attempts.html", history=[attempt])
        self.assertIn("Not complete", history)
        self.assertIn("Resume practice", history)
        self.assertNotIn("50.0%", history)

    def test_progress_resolves_context_labels_and_preserves_unknown_values(self):
        reports = {
            group: [dict(label=label, earned=1, possible=2, items=3)]
            for group, label in [
                ("Category", "MOC"), ("Objective", "MOC-01"),
                ("Response format", "unfolding-case-study"),
                ("Clinical judgment", "recognize-cues"), ("Unknown", "new-area"),
            ]
        }
        html = self.render("rnxcel_progress.html", completed_count=1, reports=reports)
        for label in ("Management of Care", OBJECTIVES["MOC-01"],
                      "Unfolding case studies", "Recognize cues", "new-area"):
            self.assertIn(label, html)
        self.assertEqual(html.count("<table>"), 5)
        self.assertEqual(html.count("50.0%"), 5)
        reports["Category"][0].update(earned=0, possible=0)
        self.assertIn(">0%</td>", self.render(
            "rnxcel_progress.html", completed_count=1, reports=reports,
        ))

    def test_truthful_empty_states(self):
        self.assertIn("No attempts yet", self.render("rnxcel_attempts.html", history=[]))
        self.assertIn("No progress reports yet", self.render(
            "rnxcel_progress.html", completed_count=0, reports={},
        ))
        self.assertIn("No completed practice in this group yet", self.render(
            "rnxcel_progress.html", completed_count=0, reports={"Category": []},
        ))
        self.assertIn("No review items are available", self.render(
            "rnxcel_results.html", attempt=self.attempt(), review_items=[],
        ))

    def test_pa_newline_conventions_are_preserved(self):
        for name in ("quiz_attempts.html", "quiz_results.html", "review_attempt.html"):
            current = (TEMPLATES / name).read_bytes()
            self.assertEqual(current.count(b"\r\n"), current.count(b"\n"), name)
            self.assertFalse(current.endswith(b"\n"), name)


if __name__ == "__main__":
    unittest.main()
