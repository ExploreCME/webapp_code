import unittest
from unittest.mock import MagicMock, patch

with patch("os.makedirs"):
    import display_database
    import paxcel_game

QUESTION = {
    "id": "q-123",
    "organ_system": "Cardiovascular System",
    "task_area": "Clinical Intervention",
    "topic_area": "Hypertension",
    "question_stem": "A 54-year-old man presents with blood pressure of 162/98 mmHg.",
    "answer_choice_A": "Lisinopril",
    "answer_choice_B": "Metoprolol",
    "answer_choice_C": "Furosemide",
    "answer_choice_D": "Clonidine",
    "answer_choice_E": "",
    "correct_answer": "a",
    "explanation": "ACE inhibitors are first-line therapy.",
}


class PaxcelGameTests(unittest.TestCase):
    def setUp(self):
        self.app = display_database.app
        config = self.app.config.copy()
        self.addCleanup(lambda: self.app.config.update(config))
        self.app.config.update(TESTING=True)
        self.client = self.app.test_client()
        self.db = MagicMock()
        self.cursor = self.db.cursor.return_value
        self.cursor.__enter__.return_value = self.cursor
        self.cursor.fetchone.side_effect = [{"c": 50}, QUESTION]
        for patcher in (
            patch.object(display_database, "MAINTENANCE_MODE", False),
            patch.object(paxcel_game, "get_db_connection", return_value=self.db),
            patch.object(paxcel_game.random, "randrange", return_value=7),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_start_page_has_headline_and_go_button(self):
        response = self.client.get("/paxcel/the_game")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("PANCE prep", html)
        self.assertIn("one random question at a time", html)
        self.assertIn('href="/paxcel/the_game/play"', html)
        self.assertIn(">GO</a>", html)
        self.assertIn("no-store", response.headers["Cache-Control"])

    def test_play_renders_random_question_in_quiz_player(self):
        response = self.client.get("/paxcel/the_game/play")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('class="quiz-viewport game-viewport"', html)
        self.assertIn('id="gameOverlay"', html)
        self.assertIn(QUESTION["question_stem"], html)
        for letter in "ABCD":
            self.assertIn(f'value="{letter}"', html)
        self.assertNotIn('value="E"', html)
        self.assertIn('const correct = "A";', html)
        self.assertIn(QUESTION["explanation"], html)
        self.assertNotIn("rateQuestion(", html)
        offset_sql, offset_params = self.cursor.execute.call_args_list[1].args
        self.assertIn("LIMIT 1 OFFSET %s", offset_sql)
        self.assertEqual(offset_params[-1], 7)
        paxcel_game.random.randrange.assert_called_once_with(50)
        self.db.close.assert_called_once()

    def test_play_handles_empty_question_bank(self):
        self.cursor.fetchone.side_effect = [{"c": 0}]
        response = self.client.get("/paxcel/the_game/play")
        self.assertEqual(response.status_code, 200)
        self.assertIn("No questions are available", response.get_data(as_text=True))

    def test_answers_follow_stem_and_only_selection_reveals_feedback(self):
        response = self.client.get("/paxcel/the_game/play?timer=1")
        html = response.get_data(as_text=True)
        self.assertLess(html.index(QUESTION["question_stem"]), html.index('value="A"'))
        self.assertLess(html.index('value="D"'), html.index('id="gameExplanation" hidden'))
        self.assertIn('.quiz-body { display: block; overflow-y: auto; }', html)
        self.assertIn("input.addEventListener('change', () => reveal(input.value))", html)
        for removed in ("gameTimer", "timerSeconds", "startTimer", "setInterval",
                        "revealBtn", "Reveal Answer", "reveal(null)"):
            self.assertNotIn(removed, html)


if __name__ == "__main__":
    unittest.main()
