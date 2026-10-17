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
        self.paths = ["/rnxcel", "/rnxcel/"] + [
            f"/rnxcel/practice/{item.slug}" for item in ITEM_EXPERIENCES
        ]

    def test_shell_and_all_experiences_render_empty_states(self):
        for path in self.paths:
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                html = response.text
                self.assertIn("RNXcel", html)
                self.assertIn("NCLEX-RN", html)
                self.assertIn("No available content yet.", html)
                self.assertIn("No categories available yet.", html)
                self.assertIn('href="/"', html)
                for item in ITEM_EXPERIENCES:
                    self.assertIn(f'href="/rnxcel/practice/{item.slug}"', html)
                for marker in (
                    "@clerk/clerk-js", "clerkPublishableKey", '<nav class="navbar">',
                    'href="/student_dashboard"', 'href="/faculty_dashboard"',
                    'action="/create-customer-portal-session"', "<form", "9876",
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

    def test_unknown_experiences_do_not_fall_back_to_paxcel(self):
        for item_type in ("paxcel", "single_answer", "unknown"):
            response = self.client.get(f"/rnxcel/practice/{item_type}")
            self.assertEqual(response.status_code, 404)
            self.assertNotIn("Location", response.headers)
        display_database.fetch_data_for_dashboard.assert_not_called()
        display_database.get_db_connection.assert_not_called()

    def test_practice_scaffold_cannot_submit_attempts(self):
        for item in ITEM_EXPERIENCES:
            response = self.client.post(
                f"/rnxcel/practice/{item.slug}", json={"answer": "test-option"},
            )
            self.assertEqual(response.status_code, 405)
        display_database.get_db_connection.assert_not_called()

    def test_global_maintenance_mode_still_applies(self):
        with patch.object(display_database, "MAINTENANCE_MODE", True):
            response = self.client.get("/rnxcel")
        self.assertEqual(response.status_code, 503)


class RNXcelContractTests(unittest.TestCase):
    def test_catalog_is_empty_and_experiences_are_explicit(self):
        self.assertEqual(RNXCEL_CATEGORIES, ())
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
