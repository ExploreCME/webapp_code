"""Shared setup rendering and the nursing-only filter contract."""

import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import unittest

from jinja2 import ChoiceLoader, DictLoader, Environment, FileSystemLoader

import test_rnxcel_routes as nursing_routes


TEMPLATES = Path(__file__).resolve().parents[1] / "templates/template standardization"


class SemanticHTML(HTMLParser):
    def __init__(self, source):
        super().__init__(convert_charrefs=False)
        self.events = []
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        self.events.append(("start", tag, attrs))

    def handle_endtag(self, tag):
        self.events.append(("end", tag))

    def handle_data(self, data):
        if data.strip():
            self.events.append(("text", re.sub(r"\s+", " ", data).strip()))

    def handle_comment(self, data):
        self.events.append(("comment", data.strip()))

    def handle_entityref(self, name):
        self.events.append(("entity", name))


class SharedSetupTests(unittest.TestCase):
    def test_paxcel_setup_rendering_is_unchanged(self):
        environment = Environment(
            loader=ChoiceLoader([
                DictLoader({"base.html": "{% block content %}{% endblock %}"}),
                FileSystemLoader(TEMPLATES),
            ]),
            autoescape=True,
        )
        environment.globals["url_for"] = lambda endpoint, **kwargs: "/" + endpoint
        # Semantic HTML fingerprints from the unmodified main revision ac39dc7.
        # Include all scripts, styles, form controls, comments and faculty branches.
        expected = {
            False: "ad7b5fd3fe2a482445c78efcbb652b79aae05ca4955cffc29a368347b9010c5d",
            True: "d8018f839cc99c4f81543a2b5b049ce31816216292786916e0a65d56b4f3a394",
        }
        for faculty, digest in expected.items():
            with self.subTest(faculty=faculty):
                html = environment.get_template("custom_quiz.html").render(
                    is_faculty=faculty,
                    organ_data={"Toy System": ["Tile B", "Tile A"]},
                    task_areas=["Toy Task"],
                )
                actual = hashlib.sha256(
                    json.dumps(SemanticHTML(html).events, ensure_ascii=True).encode()
                ).hexdigest()
                self.assertEqual(actual, digest)

    def test_both_products_consume_shared_markup_and_styles(self):
        for name in ("custom_quiz.html", "rnxcel_setup.html"):
            source = (TEMPLATES / name).read_text()
            self.assertIn('from "shared_setup.html"', source)
            self.assertIn('include "shared_setup_styles.html"', source)
            for macro in ("panel()", "tracker()", "actions()"):
                self.assertIn("call " + macro, source)


class NursingSetupPresentationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = nursing_routes.RNXcelEndToEndTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.client = self.fixture.client

    def test_objectives_match_category_without_percentage_or_multi_select(self):
        response = self.client.get("/rnxcel/practice?category=MOC&objective=MOC-01")
        self.assertEqual(response.status_code, 200)
        html = response.text
        self.assertIn('value="MOC-01"', html)
        self.assertNotIn('value="SIC-01"', html)
        self.assertIn("Consent/ethics/rights", html)
        self.assertNotIn("multiple>", html)
        self.assertNotIn("organ_pct_", html)
        self.assertNotIn("/generate", html)
        self.assertNotIn("/create-customer-portal-session", html)
        self.assertIn("indivisible groups of six", html)
        self.assertIn("Start practice", html)
        self.assertIn(" disabled", html)
        invalid = self.client.get("/rnxcel/practice?category=MOC&objective=SIC-01")
        self.assertEqual(invalid.status_code, 400)

    def test_shared_panels_preserve_selected_case_and_exact_count(self):
        self.fixture.publish(
            nursing_routes.authored_case(), nursing_routes.authored_item("standalone")
        )
        response = self.client.get(
            "/rnxcel/practice?experience=unfolding-case-study&count=6"
            "&case_version=test-case-v1"
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn('class="tracker-bar"', response.text)
        self.assertIn('class="card-section"', response.text)
        self.assertIn('class="grid-box"', response.text)
        self.assertIn('name="case_version" value="test-case-v1"', response.text)
        self.assertIn("Ready to start 6 scored items", response.text)
        self.assertNotIn(" disabled", response.text)
        no_fit = self.client.get(
            "/rnxcel/practice?experience=unfolding-case-study&count=5"
            "&case_version=test-case-v1"
        )
        self.assertIn("No session fits this request", no_fit.text)
        self.assertIn(" disabled", no_fit.text)
