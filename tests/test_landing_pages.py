import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

from PIL import Image

with patch("os.makedirs"):
    import display_database


class LandingPageTests(unittest.TestCase):
    def setUp(self):
        self.app = display_database.app
        config = self.app.config.copy()
        self.addCleanup(lambda: self.app.config.update(config))
        self.app.config.update(TESTING=True)
        static_folder = self.app.static_folder
        self.addCleanup(setattr, self.app, "static_folder", static_folder)
        self.app.static_folder = str(Path(__file__).resolve().parents[1] / "static")
        self.client = self.app.test_client()
        self.db = MagicMock()
        self.db.cursor.return_value.__enter__.return_value = self.db.cursor.return_value
        self.db.cursor.return_value.fetchone.return_value = {
            "has_paid": 1, "is_faculty": "Y",
        }
        for patcher in (
            patch.object(display_database, "MAINTENANCE_MODE", False),
            patch.object(display_database, "decode_clerk_token", return_value={"sub": "landing-user"}),
            patch.object(display_database, "get_db_connection", return_value=self.db),
            patch.object(display_database, "fetch_data_for_dashboard", return_value=(0, [], [])),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def set_authenticated(self, authenticated):
        if authenticated:
            self.client.set_cookie("__session", "test-clerk-token")
        else:
            self.client.delete_cookie("__session")

    def get_html(self, path):
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200)
        return response.get_data(as_text=True)

    def test_root_is_explorecme_landing_with_product_links(self):
        html = self.get_html("/")
        self.assertIn("FullLogo.png", html)
        self.assertIn("$9.99", html)
        for target in ("/paxcel", "/rnxcel/", "/coming-soon/mdxcel"):
            self.assertIn(f'href="{target}"', html)

    def test_coming_soon_pages(self):
        for product, date in (("rnxcel", "01/01/2027"), ("mdxcel", "06/02/2027")):
            html = self.get_html(f"/coming-soon/{product}")
            self.assertIn("Coming Soon", html)
            self.assertIn(date, html)
            self.assertIn('href="/"', html)
        self.assertEqual(self.client.get("/coming-soon/unknown").status_code, 404)

    def test_paxcel_route_exists(self):
        rules = {r.rule: r.endpoint for r in display_database.app.url_map.iter_rules()}
        self.assertEqual(rules["/paxcel"], "paxcel_landing")

    def test_public_marketing_pages_omit_header_and_clerk(self):
        for authenticated in (False, True):
            self.set_authenticated(authenticated)
            for path in ("/", "/coming-soon/rnxcel", "/coming-soon/mdxcel"):
                with self.subTest(authenticated=authenticated, path=path):
                    html = self.get_html(path)
                    for marker in (
                        '<nav class="navbar">',
                        'id="welcome-message"',
                        'id="sign-in-button"',
                        'id="sign-out-button"',
                        'action="/create-customer-portal-session"',
                        'href="/student_dashboard"',
                        'href="/faculty_dashboard"',
                        "@clerk/clerk-js",
                        "clerkPublishableKey",
                        "Clerk.load()",
                        "document.cookie",
                        "window.location.reload()",
                        "/sync_clerk_user",
                    ):
                        self.assertNotIn(marker, html)

    def test_paxcel_keeps_header_and_authentication(self):
        for authenticated in (False, True):
            self.set_authenticated(authenticated)
            with self.subTest(authenticated=authenticated):
                html = self.get_html("/paxcel")
                for marker in (
                    '<nav class="navbar">',
                    'id="welcome-message"',
                    'id="sign-in-button"',
                    'id="sign-out-button"',
                    "@clerk/clerk-js",
                    "clerkPublishableKey",
                    "Clerk.load()",
                    "document.cookie",
                    "window.location.reload()",
                    "/sync_clerk_user",
                    'window.location.href = "/logout"',
                    "Clerk.redirectToSignIn()",
                ):
                    self.assertIn(marker, html)
                for link in (
                    'action="/create-customer-portal-session"',
                    'href="/student_dashboard"',
                    'href="/faculty_dashboard"',
                ):
                    if authenticated:
                        self.assertIn(link, html)
                    else:
                        self.assertNotIn(link, html)

    def test_product_logo_references_and_static_responses(self):
        home = self.get_html("/")
        for product in ("paxcel", "rnxcel", "mdxcel"):
            with self.subTest(product=product):
                image_url = f"/static/images/{product}_logo.png"
                self.assertIn(f'src="{image_url}"', home)
                path = "/paxcel" if product == "paxcel" else f"/coming-soon/{product}"
                self.assertIn(f'src="{image_url}"', self.get_html(path))
        for image_url in (
            "/static/FullLogo.png",
            "/static/images/paxcel_logo.png",
            "/static/images/rnxcel_logo.png",
            "/static/images/mdxcel_logo.png",
        ):
            with self.subTest(image_url=image_url):
                with self.client.get(image_url) as response:
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.mimetype, "image/png")
                    with Image.open(BytesIO(response.data)) as image:
                        self.assertEqual(image.format, "PNG")
                        image.verify()


if __name__ == "__main__":
    unittest.main()
