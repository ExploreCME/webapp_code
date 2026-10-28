"""Shared presentation contracts, including the pre-extraction PA rendering."""

from html.parser import HTMLParser
import hashlib
import json
from pathlib import Path
import re
import unittest

from jinja2 import Environment, FileSystemLoader, select_autoescape


ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "templates" / "template standardization"
# Originally captured before extraction; refreshed for the Clerk auth bridge and SEO metadata.
# HTML boundary whitespace is normalized; scripts/styles and the shell are exact.
PA_RENDER_DIGESTS = {
    "base.html": "b41bee3cf586247e0e8f3a5e9b3f0847239d3b303ca651c467ee9d460a6a7383",
    "student_dashboard.html": "0c66fc0f17ddd489e567f9dfce46724fdab2dc962b8417029cef490af3847419",
    "index_for_display.html": "766a47269646304460b17ddac6d5dd83a3b7ee821aa27216c7e7a99cc947931f",
}


class Markup(HTMLParser):
    """Ignore template-boundary whitespace, but retain HTML, text and attributes."""

    def __init__(self, html):
        super().__init__(convert_charrefs=False)
        self.tokens = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.tokens.append(("start", tag, attrs))

    def handle_startendtag(self, tag, attrs):
        self.tokens.append(("self-closing", tag, attrs))

    def handle_endtag(self, tag):
        self.tokens.append(("end", tag))

    def handle_data(self, data):
        if data.strip():
            self.tokens.append(("data", " ".join(data.split())))

    def handle_comment(self, data):
        self.tokens.append(("comment", data))

    def handle_entityref(self, name):
        self.tokens.append(("entity", name))

    def handle_charref(self, name):
        self.tokens.append(("character", name))

    def handle_decl(self, data):
        self.tokens.append(("declaration", data))


class RecordingLoader(FileSystemLoader):
    def __init__(self):
        super().__init__(TEMPLATES)
        self.loaded = set()

    def get_source(self, environment, template):
        self.loaded.add(template)
        return super().get_source(environment, template)


def url_for(endpoint, **values):
    suffix = "&".join(f"{key}={value}" for key, value in sorted(values.items()))
    return f"/{endpoint}" + (f"?{suffix}" if suffix else "")


class SharedShellPresentationTests(unittest.TestCase):
    def environment(self):
        self.loader = RecordingLoader()
        environment = Environment(loader=self.loader, autoescape=select_autoescape(["html"]))
        environment.globals.update(
            url_for=url_for, get_flashed_messages=lambda **kwargs: [],
        )
        return environment

    def context(self, **overrides):
        context = dict(
            g={}, session={}, signed_in=False, is_premium=False, is_faculty=False,
            is_app=False, clerk_publishable_key="presentation-test-public-key",
            upcoming_assignments=[], total_questions=57, organ_labels=["Cardiology"],
            organ_counts=[57], task_labels=["Diagnosis"], task_counts=[57],
            exam_profile="2026", blueprint_version="2026–2029",
            experiences=[], categories=[], available=0, case_count=0, history=[],
        )
        context.update(overrides)
        return context

    def test_pa_rendering_matches_fixed_markup_styles_and_scripts(self):
        current = self.environment()
        for template in ("base.html", "student_dashboard.html", "index_for_display.html"):
            snapshots = []
            for premium, faculty, app in (
                (False, False, False), (True, False, False),
                (True, True, False), (True, False, True), (True, True, True),
                (False, False, True),
            ):
                for logged_in in (False, True):
                    with self.subTest(template=template, premium=premium, faculty=faculty,
                                      app=app, logged_in=logged_in):
                        context = self.context(
                            is_premium=premium, is_faculty=faculty, is_app=app,
                            session={"username": "student"} if logged_in else {},
                            upcoming_assignments=[dict(
                                name='Assigned quiz <&> "quoted"', days_away=3, type="Quiz",
                                due_date_str="2026-10-20",
                            )],
                        )
                        rendered = current.get_template(template).render(context)
                        snapshots.append({
                            "state": [premium, faculty, app, logged_in],
                            "markup": Markup(rendered).tokens,
                            "scripts": re.findall(
                                r"<script\b[^>]*>.*?</script[^>]*>", rendered, re.S | re.I,
                            ),
                            "styles": re.findall(
                                r"<style\b[^>]*>.*?</style[^>]*>", rendered, re.S | re.I,
                            ),
                            "exact_shell": rendered if template == "base.html" else None,
                        })
            digest = hashlib.sha256(json.dumps(
                snapshots, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            ).encode()).hexdigest()
            self.assertEqual(PA_RENDER_DIGESTS[template], digest, template)

    def test_shared_macros_preserve_autoescaping(self):
        environment = self.environment()
        html = environment.from_string(
            '{% from "shared_dashboard/dashboard.html" import tile with context %}'
            '{{ tile(href, "card-select", icon, title, description) }}'
        ).render(
            self.context(), href='/practice?q="<&>', icon="<icon>", title="<script>Title</script>",
            description="<b>Untrusted & text</b>",
        )
        self.assertIn('href="/practice?q=&#34;&lt;&amp;&gt;"', html)
        self.assertIn("&lt;icon&gt;", html)
        self.assertIn("&lt;script&gt;Title&lt;/script&gt;", html)
        self.assertIn("&lt;b&gt;Untrusted &amp; text&lt;/b&gt;", html)
        self.assertNotIn("<script>", html)
        hero = environment.from_string(
            '{% from "shared_landing/hero.html" import hero with context %}'
            '{% call hero(headline=title, description=description) %}{% endcall %}'
        ).render(self.context(), title="<em>Title</em>", description="<script>Text</script>")
        self.assertIn("&lt;em&gt;Title&lt;/em&gt;", hero)
        self.assertIn("&lt;script&gt;Text&lt;/script&gt;", hero)
        self.assertNotIn("<script>", hero)

    def test_both_dashboards_use_shared_layout_and_tiles(self):
        for template in ("student_dashboard.html", "rnxcel_dashboard.html"):
            environment = self.environment()
            html = environment.get_template(template).render(self.context())
            self.assertIn("shared_shell/base.html", self.loader.loaded)
            self.assertIn("shared_dashboard/dashboard.html", self.loader.loaded)
            self.assertIn('class="dashboard-wrapper"', html)
            self.assertIn('class="dashboard-main"', html)
            self.assertIn('class="dashboard-sidebar"', html)
            self.assertIn('class="student-card ', html)

    def test_both_landings_use_shared_branded_hero(self):
        for template, logo in (
            ("index_for_display.html", "paxcel_logo.png"), ("rnxcel.html", "rnxcel_logo.png"),
        ):
            environment = self.environment()
            html = environment.get_template(template).render(self.context())
            self.assertIn("shared_landing/hero.html", self.loader.loaded)
            self.assertIn(logo, html)
            self.assertIn("Where Knowledge Evolves", html)

    def test_nursing_card_grid_can_shrink_below_shared_desktop_minimum(self):
        html = self.environment().get_template("rnxcel_dashboard.html").render(self.context())
        self.assertIn(
            ".rnxcel .student-grid { grid-template-columns: "
            "repeat(auto-fit, minmax(min(100%, 280px), 1fr)); }", html,
        )

    def test_nursing_has_no_pa_scripts_entitlements_or_marketing(self):
        for template in ("rnxcel.html", "rnxcel_dashboard.html"):
            environment = self.environment()
            html = environment.get_template(template).render(self.context(
                is_premium=True, is_faculty=True, signed_in=True,
                session={"username": "pa-user", "is_premium": True},
            ))
            for forbidden in (
                "@clerk/clerk-js", "Clerk.load", "/sync_clerk_user", "__session=",
                'id="pricingModal"', 'id="feedbackModal"', 'id="formbricksModal"',
                'id="upgradeModal"',
                "create-customer-portal-session", "create-checkout-session",
                "OneFree", "youtube.com", "Explore Sample Content",
                'class="stat-number', 'id="organChart"', 'id="taskChart"',
            ):
                self.assertNotIn(forbidden, html)
            self.assertIn("Sign In", html)
            self.assertNotIn("Nursing account", html)
            self.assertIn('class="navbar"', html)
            self.assertIn("family=Inter", html)
            self.assertIn("not computerized adaptive testing", html)
            self.assertIn("subscriptions are not inherited", html)

    def test_nursing_account_uses_only_verified_g_identity(self):
        environment = self.environment()
        html = environment.get_template("rnxcel_dashboard.html").render(self.context(
            g={"rnxcel_owner": "verified-nursing-owner"}, signed_in=False,
        ))
        self.assertIn("Nursing account", html)
        self.assertIn("My Dashboard", html)
        self.assertNotIn(">Sign In<", html)

    def test_public_landing_does_not_claim_published_catalog_is_empty(self):
        environment = self.environment()
        html = environment.get_template("rnxcel.html").render(self.context(
            available=99, case_count=1,
        ))
        self.assertIn("Published nursing content availability is shown after sign-in", html)
        self.assertIn("The bundled catalog is empty", html)
        self.assertIn("no bundled or sample fallback is substituted", html)
        self.assertNotIn("No available content yet", html)
        self.assertNotIn("99 scored items", html)

    def test_nursing_dashboard_counts_history_and_empty_state_are_truthful(self):
        environment = self.environment()
        template = environment.get_template("rnxcel_dashboard.html")
        empty = template.render(self.context())
        self.assertIn("<strong>0</strong> available scored items", empty)
        self.assertIn("No attempts yet", empty)
        self.assertIn("No available content yet", empty)
        html = template.render(self.context(
            available=9, case_count=1,
            history=[
                dict(id="saved", status="active", current_index=2, total_items=3),
                dict(id="done", status="completed", current_index=6, total_items=6),
            ],
        ))
        for title in ("Practice Questions", "Unfolding Case Studies",
                      "Attempt History", "My Progress"):
            self.assertIn(f'class="card-title">{title}</div>', html)
        self.assertIn("<strong>9</strong> available scored items", html)
        self.assertIn("<strong>1</strong> available cases", html)
        self.assertIn("2 of 3 items saved", html)
        self.assertIn("/rnxcel.play?attempt_id=saved", html)
        self.assertIn("/rnxcel.results?attempt_id=done", html)
        self.assertNotIn("No available content yet", html)
        self.assertNotIn("filterAssignments", html)

    def test_app_header_can_be_overridden_without_changing_pa_defaults(self):
        environment = self.environment()
        html = environment.from_string(
            '{% extends "rnxcel_base.html" %}{% block app_header %}{% endblock %}'
        ).render(self.context())
        self.assertNotIn("<nav", html)
        self.assertNotIn("@clerk/clerk-js", html)
        self.assertIn("rnxcel-main", html)


if __name__ == "__main__":
    unittest.main()
