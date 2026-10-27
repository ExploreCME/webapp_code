"""SEO contracts for the shared shell, without application or database setup."""

from html.parser import HTMLParser
from pathlib import Path
import unittest

from flask import Flask, render_template, render_template_string

from remediation_display import remediation_groups, remediation_label


TEMPLATES = Path(__file__).resolve().parents[1] / "templates" / "template standardization"
UTILITY_TEMPLATES = (
    "login.html", "upload.html", "dashboard.html", "student_dashboard.html",
    "faculty_dashboard.html", "db_stats.html", "rnxcel_dashboard.html",
    "rnxcel_signin.html", "rnxcel_progress.html",
)


class Metadata(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.meta = {}
        self.canonicals = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "meta":
            key = attrs.get("name", attrs.get("property"))
            self.meta.setdefault(key, []).append(attrs.get("content"))
        elif tag == "link" and attrs.get("rel") == "canonical":
            self.canonicals.append(attrs.get("href"))


class SEOTemplateTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__, template_folder=str(TEMPLATES))
        self.app.jinja_env.filters.update(
            remediation_groups=remediation_groups, remediation_label=remediation_label,
        )
        # Only the static endpoint needs real URL building for metadata checks.
        self.app.url_build_error_handlers.append(
            lambda error, endpoint, values: "/" + endpoint.replace(".", "/")
        )
        self.context = dict(
            is_premium=False, is_faculty=False, is_app=False, upcoming_assignments=[],
            total_questions=0, organ_labels=[], organ_counts=[], task_labels=[],
            task_counts=[], experiences=[], categories=[], available=0, case_count=0,
            history=[], reports={}, product_name="MDXcel", product_tagline="MD platform",
        )

    def render(self, template, path="/", base_url="https://example.test"):
        with self.app.test_request_context(path, base_url=base_url):
            return Metadata(render_template(template, **self.context))

    def assert_metadata(self, metadata, robots, canonical):
        self.assertEqual(metadata.meta["robots"], [robots])
        self.assertEqual(metadata.canonicals, [canonical])
        self.assertEqual(len(metadata.meta["description"]), 1)
        for key in (
            "og:type", "og:title", "og:description", "og:url", "og:image",
            "twitter:card", "twitter:title", "twitter:description", "twitter:image",
        ):
            self.assertEqual(len(metadata.meta[key]), 1, key)
            self.assertTrue(metadata.meta[key][0], key)
        self.assertEqual(metadata.meta["og:url"], [canonical])
        for key in ("og:image", "twitter:image"):
            self.assertEqual(metadata.meta[key], ["https://example.test/static/FullLogo.png"])

    def test_all_standardized_templates_compile(self):
        for template in self.app.jinja_env.list_templates():
            if template.endswith(".html"):
                with self.subTest(template=template):
                    self.app.jinja_env.get_template(template)

    def test_public_pages_remain_indexable_with_unique_metadata(self):
        for template, path in (
            ("base.html", "/"),
            ("explorecme_home.html", "/"),
            ("index_for_display.html", "/paxcel"),
            ("rnxcel.html", "/rnxcel/"),
            ("coming_soon.html", "/coming-soon/mdxcel"),
        ):
            with self.subTest(template=template):
                metadata = self.render(template, path + "?utm_source=test&ref=campaign")
                self.assert_metadata(metadata, "index,follow", "https://example.test" + path)

    def test_confirmed_utility_pages_are_noindex(self):
        for template in UTILITY_TEMPLATES:
            with self.subTest(template=template):
                path = "/" + template.removesuffix(".html")
                metadata = self.render(template, path + "?page=2")
                self.assert_metadata(metadata, "noindex,follow", "https://example.test" + path)

    def test_canonical_keeps_request_origin_and_path_without_query(self):
        for base_url in ("http://localhost:5000", "https://staging.example.test"):
            with self.subTest(base_url=base_url):
                metadata = self.render("base.html", "/some/path/?utm_source=test", base_url)
                self.assertEqual(metadata.canonicals, [base_url + "/some/path/"])
                self.assertEqual(metadata.meta["og:url"], metadata.canonicals)

    def test_social_defaults_follow_title_and_description_overrides_and_escape_values(self):
        with self.app.test_request_context("/", base_url="https://example.test"):
            html = render_template_string(
                '{% extends "base.html" %}'
                '{% block title %}{{ title }}{% endblock %}'
                '{% block description %}{{ description }}{% endblock %}',
                title='Title "<&>', description='Description "<&>',
            )
        metadata = Metadata(html)
        for key in ("og:title", "twitter:title"):
            self.assertEqual(metadata.meta[key], ['Title "<&>'])
        for key in ("description", "og:description", "twitter:description"):
            self.assertEqual(metadata.meta[key], ['Description "<&>'])
        self.assertNotIn('content="Title "<&>', html)

    def test_child_can_override_canonical_and_each_social_default(self):
        overrides = dict(
            robots="noindex,follow", canonical_url="https://example.test/preferred",
            og_type="article", og_title="OG title", og_description="OG description",
            og_image="https://example.test/og.png", twitter_card="summary",
            twitter_title="Twitter title", twitter_description="Twitter description",
            twitter_image="https://example.test/twitter.png",
        )
        template = '{% extends "base.html" %}' + "".join(
            "{% block " + block + " %}" + value + "{% endblock %}"
            for block, value in overrides.items()
        )
        with self.app.test_request_context("/?tracking=test"):
            metadata = Metadata(render_template_string(template))
        self.assertEqual(metadata.meta["robots"], ["noindex,follow"])
        self.assertEqual(metadata.canonicals, [overrides["canonical_url"]])
        self.assertEqual(metadata.meta["og:url"], metadata.canonicals)
        for block, value in overrides.items():
            if block.startswith(("og_", "twitter_")):
                self.assertEqual(metadata.meta[block.replace("_", ":", 1)], [value])

    def test_pa_landing_keeps_existing_social_copy(self):
        metadata = self.render("index_for_display.html", "/paxcel")
        for prefix in ("og", "twitter"):
            self.assertEqual(metadata.meta[prefix + ":title"], [
                "PANCE Quiz App | Board Review for PA Students",
            ])
            self.assertEqual(metadata.meta[prefix + ":description"], [
                "A powerful, personalized quiz application designed for Physician Assistant "
                "Students to master the PANCE and track their progress.",
            ])


if __name__ == "__main__":
    unittest.main()
