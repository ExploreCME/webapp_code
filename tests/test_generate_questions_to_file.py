import json
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import generate_questions_to_file as script
import Question_writing_and_verification_optomized as engine


class GenerateQuestionsToFileTests(unittest.TestCase):
    def test_parse_request_normalizes_categories(self):
        self.assertEqual(
            script.parse_request("Women's Health: abruptio placentae: formulating the most likel diagnosis."),
            {"organ_system": "Reproductive System", "topic_area": "abruptio placentae",
             "task_area": "Formulating the Most Likely Diagnosis"})
        for bad in ("Cardiology only: angina", "Nonsense: topic: clinical intervention"):
            with self.assertRaises(ValueError):
                script.parse_request(bad)

    def test_main_runs_real_pipeline_and_writes_file_without_database(self):
        question = {
            "question_stem": "A 58-year-old man has exertional chest pressure.",
            **{f"answer_choice_{letter}": f"Choice {letter}" for letter in "ABCDE"},
            "correct_answer": "B", "explanation": "Because.",
        }

        async def chat(prompt, max_tokens=None):
            if "Create EXACTLY" in prompt:
                return json.dumps({"questions": [question]})
            if "rate the difficulty" in prompt:
                return json.dumps({"difficulty": 6})
            return json.dumps({"correct_answer": "B", "confidence_score": 9})

        manager = engine.ConversationManager(api_key="test-key")
        manager.chat_completion = AsyncMock(side_effect=chat)
        manager.client = MagicMock(close=AsyncMock())
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(script, "ConversationManager", return_value=manager) as manager_factory, \
                patch.object(engine, "get_async_db_connection", side_effect=AssertionError("DB used")), \
                patch.dict(os.environ, {"OPENAI_API_KEY": " env-test-key ", "OPENAI_MODEL": "env-model"}):
            output = os.path.join(tmp, "out.txt")
            code = script.main(["Cardiovascular system: stable angina pectoris: using diagnostics and laboratory studies",
                                "-o", output])
            with open(output, encoding="utf-8") as f:
                text = f.read()

        self.assertEqual(code, 0)
        self.assertIn("Total Questions: 1", text)
        self.assertIn("Organ System: Cardiovascular System", text)
        self.assertIn("Task Area: Using Diagnostic and Laboratory Studies", text)
        self.assertIn("Difficulty: 6", text)
        self.assertIn(question["question_stem"], text)
        self.assertEqual(
            [call.kwargs for call in manager_factory.call_args_list],
            [{"api_key": "env-test-key", "model": "env-model"}] * 2,
        )

    def test_cli_model_overrides_environment(self):
        manager = MagicMock(client=MagicMock(close=AsyncMock()))
        with patch.object(script, "ConversationManager", return_value=manager) as manager_factory, \
                patch.object(script, "process_topic_batch", new=AsyncMock(return_value=[])), \
                patch.dict(os.environ, {"OPENAI_API_KEY": "test-key", "OPENAI_MODEL": "env-model"}), \
                patch.object(script, "write_questions"):
            script.main(["Cardiovascular system: angina: clinical intervention", "--model", "cli-model"])

        self.assertEqual(
            [call.kwargs["model"] for call in manager_factory.call_args_list],
            ["cli-model", "cli-model"],
        )


if __name__ == "__main__":
    unittest.main()
