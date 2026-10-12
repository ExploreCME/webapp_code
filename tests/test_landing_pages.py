import unittest
from unittest.mock import patch

with patch("os.makedirs"):
    import display_database


class LandingPageTests(unittest.TestCase):
    def setUp(self):
        self.client = display_database.app.test_client()
        patcher = patch.object(display_database, "MAINTENANCE_MODE", False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_root_is_explorecme_landing_with_product_links(self):
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn("FullLogo.png", html)
        self.assertIn("$9.99", html)
        for target in ("/paxcel", "/coming-soon/rnxcel", "/coming-soon/mdxcel"):
            self.assertIn(f'href="{target}"', html)

    def test_coming_soon_pages(self):
        for product, date in (("rnxcel", "01/01/2027"), ("mdxcel", "06/02/2027")):
            html = self.client.get(f"/coming-soon/{product}").get_data(as_text=True)
            self.assertIn("Coming Soon", html)
            self.assertIn(date, html)
            self.assertIn('href="/"', html)
        self.assertEqual(self.client.get("/coming-soon/unknown").status_code, 404)

    def test_paxcel_route_exists(self):
        rules = {r.rule: r.endpoint for r in display_database.app.url_map.iter_rules()}
        self.assertEqual(rules["/paxcel"], "paxcel_landing")


if __name__ == "__main__":
    unittest.main()
