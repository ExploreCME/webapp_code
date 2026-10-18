import unittest
from dataclasses import FrozenInstanceError
from unittest.mock import MagicMock, patch

with patch("os.makedirs"):
    import display_database

from rnxcel_catalog import (
    EXAM_PROFILE, ITEM_EXPERIENCES, RNXCEL_CATEGORIES, RNXCEL_ITEMS,
    CaseStage, Option, SelectAllThatApply, SingleBestAnswer, UnfoldingCaseStudy,
)


class RNXcelRouteTests(unittest.TestCase):
    def setUp(self):
        self.app = display_database.app
        config = self.app.config.copy()
        self.addCleanup(lambda: self.app.config.update(config))
        self.app.config.update(TESTING=True, SECRET_KEY="rnxcel-test-only")
        self.client = self.app.test_client()
        for name, replacement in (
            ("MAINTENANCE_MODE", False),
            ("refresh_user_status", MagicMock(wraps=display_database.refresh_user_status)),
            ("get_db_connection", MagicMock()),
            ("fetch_data_for_dashboard", MagicMock(return_value=(9876, [], []))),
        ):
            patcher = patch.object(display_database, name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.paths = ["/rnxcel", "/rnxcel/"]

    def test_rnxcel_selection_and_legacy_link_open_the_nursing_landing(self):
        home = self.client.get("/")
        self.assertEqual(home.status_code, 200)
        self.assertIn('href="/rnxcel/"', home.text)
        for path in ("/rnxcel/", "/coming-soon/rnxcel"):
            response = self.client.get(path, follow_redirects=True)
            self.assertEqual(response.status_code, 200)
            self.assertIn("Nursing practice, with purpose.", response.text)
            self.assertEqual(response.request.path.rstrip("/"), "/rnxcel")

    def test_public_landing_describes_nursing_without_paxcel_dependencies(self):
        for path in self.paths:
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                html = response.text
                self.assertIn("RNXcel", html)
                self.assertIn("NCLEX-RN", html)
                self.assertIn('href="/"', html)
                for category in RNXCEL_CATEGORIES:
                    self.assertIn(category.label, html)
                    self.assertIn(f'href="/rnxcel/practice?category={category.id}"', html)
                for marker in (
                    "@clerk/clerk-js", "clerkPublishableKey", '<nav class="navbar">',
                    'href="/student_dashboard"', 'href="/faculty_dashboard"',
                    'action="/create-customer-portal-session"', "9876",
                ):
                    self.assertNotIn(marker, html)
        display_database.fetch_data_for_dashboard.assert_not_called()
        display_database.get_db_connection.assert_not_called()
        display_database.refresh_user_status.assert_not_called()

    def test_visits_do_not_read_or_mutate_paxcel_identity_or_entitlements(self):
        for username in ("paid-user", "unpaid-user", "guest_sample"):
            for premium in (False, True):
                with self.subTest(username=username, premium=premium):
                    self.client.set_cookie("__session", "invalid-or-unrelated-token")
                    with self.client.session_transaction() as session:
                        session.clear()
                        session.update(
                            username=username, is_premium=premium, is_faculty=True,
                            accepted_eula=True, last_db_sync=123,
                        )
                        original = dict(session)
                    for path in self.paths:
                        response = self.client.get(path)
                        self.assertEqual(response.status_code, 200)
                        self.assertNotIn("Set-Cookie", response.headers)
                    with self.client.session_transaction() as session:
                        self.assertEqual(dict(session), original)
        display_database.refresh_user_status.assert_not_called()
        display_database.get_db_connection.assert_not_called()
        display_database.fetch_data_for_dashboard.assert_not_called()

    def test_paxcel_still_uses_its_own_counts_and_hooks_after_rnxcel_visit(self):
        before = self.client.get("/paxcel")
        self.assertEqual(before.status_code, 200)
        self.assertIn("9876", before.text)
        self.assertIn("@clerk/clerk-js", before.text)
        refresh_calls = display_database.refresh_user_status.call_count
        self.client.get("/rnxcel")
        self.assertEqual(display_database.refresh_user_status.call_count, refresh_calls)
        after = self.client.get("/paxcel")
        self.assertEqual(before.data, after.data)
        self.assertEqual(display_database.fetch_data_for_dashboard.call_count, 2)

    def test_paxcel_flash_messages_are_not_displayed_or_consumed_by_rnxcel(self):
        message = "PAXcel-only quiz feedback"
        with self.client.session_transaction() as session:
            session["_flashes"] = [("info", message)]
        for path in self.paths:
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertNotIn(message, response.text)
            self.assertNotIn("Set-Cookie", response.headers)
            with self.client.session_transaction() as session:
                self.assertEqual(session["_flashes"], [("info", message)])
        self.assertIn(message, self.client.get("/paxcel").text)
        with self.client.session_transaction() as session:
            self.assertNotIn("_flashes", session)

    def test_global_maintenance_mode_still_applies(self):
        with patch.object(display_database, "MAINTENANCE_MODE", True):
            response = self.client.get("/rnxcel")
        self.assertEqual(response.status_code, 503)


class RNXcelContractTests(unittest.TestCase):
    def test_editorial_catalog_has_categories_but_no_seeded_items(self):
        self.assertEqual(
            {category.id for category in RNXCEL_CATEGORIES},
            {"MOC", "SIC", "HPM", "PSI", "BCC", "PPT", "RRP", "PAD"},
        )
        self.assertEqual(sum(category.target_weight for category in RNXCEL_CATEGORIES), 100)
        self.assertEqual(RNXCEL_ITEMS, ())
        self.assertEqual(
            {item.slug for item in ITEM_EXPERIENCES},
            {"single-best-answer", "select-all-that-apply", "unfolding-case-study"},
        )
        with self.assertRaises(FrozenInstanceError):
            ITEM_EXPERIENCES[0].slug = "paxcel"

    def test_draft_contracts_distinguish_answers_and_ordered_case_stages(self):
        options = (Option("a", "Test option A"), Option("b", "Test option B"))
        single = SingleBestAnswer("test-single", "test-category", "Test stem", options, "a")
        multiple = SelectAllThatApply(
            "test-multiple", "test-category", "Test stem", options, ("a", "b"),
        )
        case = UnfoldingCaseStudy(
            "test-case", "test-category", "Test case",
            (
                CaseStage("stage-1", ("First test exhibit",), single),
                CaseStage("stage-2", ("Later test exhibit",), multiple),
            ),
        )
        self.assertEqual(single.correct_option_id, "a")
        self.assertEqual(multiple.correct_option_ids, ("a", "b"))
        self.assertEqual(case.stages[0].item, single)
        self.assertEqual(case.stages[1].item, multiple)
        self.assertNotIn("Later test exhibit", case.stages[0].exhibits)
        for item, experience in zip((single, multiple, case), ITEM_EXPERIENCES):
            self.assertEqual(item.exam_profile, EXAM_PROFILE)
            self.assertEqual(item.item_type, experience.slug)
            with self.assertRaises(FrozenInstanceError):
                item.exam_profile = "PANCE"
        self.assertEqual(RNXCEL_ITEMS, ())


if __name__ == "__main__":
    unittest.main()
