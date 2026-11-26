import json
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import verify_question_to_file as script


class VerifyQuestionToFileTests(unittest.TestCase):
    def test_each_attempt_is_independent_and_all_output_is_written(self):
        responses = iter([
            json.dumps({"correct_answer": "B", "confidence_score": 9, "explanation": "First reason."}),
            "not json",  # unusable response is retried within the same attempt
            json.dumps({"correct_answer": "Choice C", "confidence_score": "80%", "explanation": "Second reason."}),
        ])
        managers = []

        def make_manager(**kwargs):
            manager = MagicMock(client=MagicMock(close=AsyncMock()))
            manager.chat_completion = AsyncMock(side_effect=lambda *a, **k: next(responses))
            managers.append((kwargs, manager))
            return manager

        inputs = iter(["A 58-year-old man has chest pressure.", *(f"Choice {l}" for l in "ABCDE"), "0", "2"])
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(script, "ConversationManager", side_effect=make_manager), \
                patch.dict(os.environ, {"OPENAI_API_KEY": "test-key", "OPENAI_MODEL": "env-model"}):
            output = os.path.join(tmp, "out.txt")
            code = script.main(["-o", output, "--concurrency", "1"], input_fn=lambda _: next(inputs))
            with open(output, encoding="utf-8") as f:
                text = f.read()

        self.assertEqual(code, 0)
        self.assertEqual([kwargs for kwargs, _ in managers], [{"api_key": "test-key", "model": "env-model"}] * 2)
        for _, manager in managers:
            manager.client.close.assert_awaited_once()
        prompt = managers[0][1].chat_completion.call_args.args[0]
        self.assertIn(script.PANCE_Verification_TASK, prompt)
        self.assertIn("E: Choice E", prompt)
        self.assertIn("Attempts: 2", text)
        self.assertIn("Correct Answer: B. Choice B", text)
        self.assertIn("Explanation: First reason.", text)
        self.assertIn("Correct Answer: C. Choice C", text)
        self.assertIn("Confidence: 8/10", text)
        self.assertIn("Retried/unusable responses: malformed_json", text)
        self.assertIn("Most frequent answer: B, C (1/2)", text)

    def test_attempt_fails_after_exhausting_unusable_responses(self):
        manager = MagicMock(client=MagicMock(close=AsyncMock()))
        manager.chat_completion = AsyncMock(return_value="{}")
        question = {"question_stem": "Q", **{f"answer_choice_{l}": l * 3 for l in "ABCDE"}}
        with patch.object(script, "ConversationManager", return_value=manager):
            result = script.asyncio.run(script.run_attempt(1, question, "key", "model"))

        self.assertIsNone(result["answer"])
        self.assertEqual(manager.chat_completion.await_count, script.MAX_UNUSABLE_VERIFIER_RESPONSES + 1)
        self.assertIn("FAILED", script.format_report(question, [result], "model"))


if __name__ == "__main__":
    unittest.main()
