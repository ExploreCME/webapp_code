"""Shared player contracts, using nonclinical presentation fixtures only."""

from hashlib import sha256
from html.parser import HTMLParser
from pathlib import Path
import unittest
from urllib.parse import urlencode

from jinja2 import ChoiceLoader, DictLoader, Environment, FileSystemLoader, StrictUndefined
from markupsafe import Markup


TEMPLATES = Path(__file__).resolve().parents[1] / "templates" / "template standardization"
BASE = (
    "{% block title %}{% endblock %}{% block head %}{% endblock %}"
    "{% block app_header %}APP_HEADER{% endblock %}"
    "{% block content %}{% endblock %}"
)


def environment():
    env = Environment(
        loader=ChoiceLoader([
            DictLoader({"base.html": BASE, "rnxcel_base.html": '{% extends "base.html" %}'}),
            FileSystemLoader(TEMPLATES),
        ]),
        autoescape=True,
        undefined=StrictUndefined,
    )
    env.globals["url_for"] = lambda endpoint, **values: (
        "/" + endpoint.replace(".", "/") + "?" + urlencode(sorted(values.items()))
    )
    return env


def paxcel_context(**changes):
    context = dict(
        question=dict(
            id=7, question_stem="<p>NONCLINICAL toy sorting stem</p>",
            correct_answer="B", explanation="<p>Private toy rationale</p>",
            correct=4, incorrect=2, choice_a_count=1, choice_b_count=4,
            choice_c_count=1, choice_d_count=0, choice_e_count=0,
            answer_choice_A="Amber", answer_choice_B="Blue",
            answer_choice_C="None", answer_choice_D="", answer_choice_E=None,
        ),
        current_question_number=2, total_questions=3, mode="study", answered=False,
        previous_choice=None, quiz_name="Fixture", attempt_id="attempt-1",
        next_q_id=3, prev_q_id=1, is_standardized=False, exit_url=None,
        session={"standardized_csrf_token": "fixture-csrf"},
        grid_data=[
            dict(status=status, is_current=index == 1, question_id=index + 1,
                 question_number=index + 1)
            for index, status in enumerate(("answered", "unanswered", "correct", "incorrect"))
        ],
    )
    context.update(changes)
    return context


PAXCEL_SCENARIOS = [
    dict(mode=mode, answered=answered, previous_choice="A" if answered else None)
    for mode in ("study", "study_no_exp", "test")
    for answered in (False, True)
] + [
    dict(mode="test", next_q_id="NEXT_MODULE", is_standardized=True, exit_url="/modules"),
    dict(mode="test", next_q_id=None, prev_q_id=None),
    dict(answered=True, previous_choice="B", next_q_id="NEXT_MODULE"),
    dict(answered=True, previous_choice="B", next_q_id=None),
    dict(answered=True, next_q_id=None, attempt_id=None, is_standardized=True),
    dict(question={
        **paxcel_context()["question"],
        "answer_choice_A": "<i>Untrusted toy tile</i>",
        "answer_choice_B": Markup("<em>Trusted toy tile</em>"),
        "answer_choice_C": "Toy & 'tile'",
        "answer_choice_E": "Extra toy tile",
    }),
]


class Elements(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.elements = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))

    def named(self, tag):
        return [attrs for name, attrs in self.elements if name == tag]


class SharedPlayerPresentationTests(unittest.TestCase):
    def nursing(self, kind="single-best-answer", **changes):
        item = dict(
            stem="NONCLINICAL toy sorting: <never markup>", type=kind,
            case_title=None, exhibits=[],
            options=[
                {"id": "stable-blue", "text": "Blue toy tile"},
                {"id": "stable-amber", "text": "Amber toy tile"},
                {"id": "tile with space", "text": "<untrusted option>"},
            ],
        )
        item.update(changes)
        return environment().get_template("rnxcel_player.html").render(
            attempt=dict(id="nursing-only", current_index=1, total_items=4),
            item=item, csrf_token="nursing-csrf",
        )

    def test_paxcel_original_rendering_snapshots(self):
        # Original index.html from ac39dc755ff9b7106f2ce797ad3029161e8fae26,
        # rendered with this same Jinja loader/context, including every script byte.
        # Fixed digests intentionally do not depend on the checkout's moving HEAD.
        expected = [
            "08e134f368f5b41a7e747a3aafec401b9014fa2389732987ae0e8080c876a4d4",
            "4eb2401242c5857d9c5e3bcd8f07fad15755b00aabcdd36ab0d0e402a501a3c9",
            "4f216b67337c1cc77bb697453cb7b992bd4dd36d9a07e25f5871bea05f618b11",
            "a944a9f36601ca4f1596b830569a167ba15c42e939f39299f3ad5b84b3a3a575",
            "4e548ed9127796fb74809e8c8e03bc29cc995873526a94355f18a2667e866acd",
            "76410bb126227a34472a0bbeb6975f9270ed25be93ff2c593fe47a8adb5c0f29",
            "d2ae74e549ce7192aa5490c7d18679a83cc54721cbc1c340c369ad682dfbf5db",
            "12ed57616ae3c55751fed791216456f7852b5e2c277d353f7c65c9407c8d449c",
            "f6f59787812e633827278f48029b1f40c0684b72fb31a7db7fc3e5df00f36e96",
            "5cf5dc802badc35a4de1fe7eb92900d57d707f32ef561d5c9003691a96120d79",
            "43fac9a14f1c69c3aafc05a4e86903da35c2dd48ed66cd31bfd5a113da5a7cd0",
            "2dda0186c012c31d5714bb0b6422dd749d4315643eca508917398a090cb31261",
        ]
        self.assertEqual(len(expected), len(PAXCEL_SCENARIOS))
        for scenario, digest in zip(PAXCEL_SCENARIOS, expected):
            with self.subTest(scenario=scenario):
                html = environment().get_template("index.html").render(paxcel_context(**scenario))
                self.assertEqual(sha256(html.encode()).hexdigest(), digest)

    def test_paxcel_answer_autoescape_and_trusted_markup_are_preserved(self):
        html = environment().get_template("index.html").render(
            paxcel_context(**PAXCEL_SCENARIOS[-1])
        )
        self.assertIn("&lt;i&gt;Untrusted toy tile&lt;/i&gt;", html)
        self.assertNotIn("<i>Untrusted toy tile</i>", html)
        self.assertIn("<em>Trusted toy tile</em>", html)
        self.assertIn("Toy &amp; &#39;tile&#39;", html)

    def test_nursing_uses_shared_fullscreen_frame_and_no_pa_context(self):
        html = self.nursing()
        elements = Elements(html)
        classes = {
            part for _, attrs in elements.elements
            for part in attrs.get("class", "").split()
        }
        for name in (
            "quiz-viewport", "quiz-header", "quiz-body", "quiz-sidebar",
            "question-pane", "answers-pane", "quiz-footer", "choice-label",
        ):
            self.assertIn(name, classes)
        for marker in ("APP_HEADER", "<script", "rateQuestion(", "/quiz/", "user_choice",
                       "standardized_csrf_token", "Community Stats", "correct_option_ids"):
            self.assertNotIn(marker, html)
        self.assertIn("&lt;never markup&gt;", html)
        self.assertIn("&lt;untrusted option&gt;", html)

    def test_nursing_form_posts_only_nursing_state_and_endpoint(self):
        elements = Elements(self.nursing())
        self.assertEqual(len(elements.named("form")), 1)
        form = elements.named("form")[0]
        self.assertEqual(form["method"].lower(), "post")
        self.assertEqual(form["action"], "/rnxcel/answer?attempt_id=nursing-only")
        hidden = {
            item["name"]: item["value"] for item in elements.named("input")
            if item["type"] == "hidden"
        }
        self.assertEqual(hidden, {"csrf_token": "nursing-csrf", "index": "1"})

    def test_nursing_mobile_height_tracks_browser_chrome_without_keyboard_traps(self):
        html = self.nursing()
        self.assertIn(".rn-player.quiz-viewport { height: 100vh; height: 100dvh; }", html)
        self.assertNotIn("<script", html)
        for _, attrs in Elements(html).elements:
            self.assertNotIn("onkeydown", attrs)
            self.assertNotIn("onkeypress", attrs)
            self.assertLessEqual(int(attrs.get("tabindex", "0")), 0)

    def test_nursing_choices_preserve_stable_ids_and_multiple_selection(self):
        for kind, input_type in (
            ("single-best-answer", "radio"), ("select-all-that-apply", "checkbox"),
        ):
            with self.subTest(kind=kind):
                elements = Elements(self.nursing(kind))
                options = [item for item in elements.named("input") if item["name"] == "selection"]
                self.assertEqual([item["type"] for item in options], [input_type] * 3)
                self.assertEqual(
                    [item["value"] for item in options],
                    ["stable-blue", "stable-amber", "tile with space"],
                )
                self.assertTrue(all(" " not in item["id"] for item in options))
                self.assertEqual(len({item["id"] for item in options}), 3)
                self.assertEqual(
                    {item["for"] for item in elements.named("label")},
                    {item["id"] for item in options},
                )
                self.assertTrue(all(("required" in item) == (input_type == "radio") for item in options))
                self.assertTrue(all("checked" not in item and "disabled" not in item for item in options))
                reordered = Elements(self.nursing(kind, options=[
                    dict(id=option["value"], text="Toy tile") for option in reversed(options)
                ]))
                self.assertEqual(
                    {item["value"]: item["id"] for item in options},
                    {
                        item["value"]: item["id"] for item in reordered.named("input")
                        if item["name"] == "selection"
                    },
                )

    def test_nursing_map_is_noninteractive_saved_current_locked(self):
        elements = Elements(self.nursing())
        entries = [attrs for _, attrs in elements.elements if attrs.get("role") == "listitem"]
        self.assertEqual(
            [entry["aria-label"] for entry in entries],
            ["Item 1: saved", "Item 2: current", "Item 3: locked", "Item 4: locked"],
        )
        self.assertEqual(entries[1]["aria-current"], "step")
        self.assertTrue(all("href" not in entry and "onclick" not in entry for entry in entries))

    def test_case_displays_only_released_exhibits_and_never_answer_keys(self):
        html = self.nursing(
            case_title="Toy case", stage_index=1, stage_count=6,
            exhibits=[
                dict(timestamp="08:00", text="Released toy inventory"),
                dict(timestamp="09:00", text="Released second inventory"),
            ],
            correct_option_ids=["SECRET-KEY"], rationale="SECRET-RATIONALE",
            future_exhibits=[dict(timestamp="10:00", text="UNRELEASED-INVENTORY")],
        )
        self.assertIn("Case stage 2 of 6", html)
        for text in ("Released toy inventory", "Released second inventory", "08:00", "09:00"):
            self.assertEqual(html.count(text), 1)
        for text in ("SECRET-KEY", "SECRET-RATIONALE", "UNRELEASED-INVENTORY", "10:00"):
            self.assertNotIn(text, html)


if __name__ == "__main__":
    unittest.main()
