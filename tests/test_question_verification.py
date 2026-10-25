import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock, patch


ROOT = Path(__file__).resolve().parents[1]
dependencies = {key: MagicMock() for key in ("openai", "db_pool", "dotenv")}
with patch.dict(sys.modules, dependencies):
    spec = importlib.util.spec_from_file_location(
        "Question_writing_and_verification_optomized",
        ROOT / "Question_writing_and_verification_optomized.py")
    engine = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(engine)

ITEM = {"organ_system": "Hematologic System", "topic_area": "Iron Deficiency Anemia",
        "task_area": "Clinical Intervention"}


def question(**overrides):
    data = {**ITEM, "question_stem": "A vignette",
            **{f"answer_choice_{letter}": f"Choice {letter}" for letter in "ABCDE"},
            "correct_answer": "B", "explanation": "Because."}
    data.update(overrides)
    return data


class FakeManager:
    """Replays scripted responses; Exception instances are raised."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.prompts = []

    def build_PANCE_Verification_prompt(self, q_data):
        return "verify"

    def build_PANCE_Difficulty_prompt(self, q_data):
        return "difficulty"

    def build_PANCE_batch_prompt(self, organ, topic, task, count, summary=None):
        return f"generate {organ} {topic} {task} {count}"

    async def chat_completion(self, prompt, max_tokens=None):
        self.prompts.append(prompt)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response if isinstance(response, str) or response is None else json.dumps(response)


def verdict(answer="B", confidence=9):
    return {"correct_answer": answer, "confidence_score": confidence}


class SelectedCategoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_selected_labels_are_json_escaped_and_topics_are_not_defaulted(self):
        item = {**ITEM, "organ_system": 'Growth: "Early Years"', "topic_area": "string"}
        generator = FakeManager([{"questions": [question(**item)]}])
        generator.build_PANCE_batch_prompt = (
            engine.ConversationManager.build_PANCE_batch_prompt.__get__(generator)
        )
        with contextlib.redirect_stdout(io.StringIO()):
            results = await engine.process_topic_batch(
                item, 1, generator, FakeManager([verdict()] * 3 + [{"difficulty": 5}]),
                allowed_organ_systems=(item["organ_system"],),
            )
        self.assertEqual({key: results[0][key] for key in item}, item)
        prompt = generator.prompts[0]
        self.assertIn("Target Diagnosis/Topic: string", prompt)
        example = prompt.split("Output your response as valid JSON with this exact structure:\n")[1]
        example = json.loads(example.split("\nRespond with JSON only.")[0])
        self.assertEqual(example["questions"][0]["organ_system"], item["organ_system"])

    async def test_wrong_or_missing_classifications_are_not_overwritten(self):
        item = {**ITEM, "organ_system": "Growth and Development"}
        allowed = ("Growth and Development", "Pediatric Renal Development", "Renal System")
        for overrides in (
            {"organ_system": None}, {"organ_system": ""}, {"organ_system": "Misc"},
            {"organ_system": "Renal System"}, {"organ_system": "Unrelated"},
            {"organ_system": "Pediatric Renal Development"},
            {"task_area": None}, {"task_area": "diagnosis"},
            {"task_area": "Pharmaceutical Therapeutics"}, {"topic_area": None},
            {"topic_area": "Unrelated"}, {"topic_area": "string"},
        ):
            with self.subTest(overrides=overrides):
                candidate = question(**item)
                candidate.update(overrides)
                generator = FakeManager([{"questions": [candidate]}] * 4)
                generator.build_PANCE_batch_prompt = (
                    engine.ConversationManager.build_PANCE_batch_prompt.__get__(generator)
                )
                verifier = FakeManager([])
                stats = {}
                with contextlib.redirect_stdout(io.StringIO()):
                    results = await engine.process_topic_batch(
                        item, 1, generator, verifier, stats=stats,
                        allowed_organ_systems=allowed,
                    )
                self.assertEqual(results, [])
                self.assertEqual(stats, {"generator_wrong_classification": 4})
                self.assertEqual(verifier.prompts, [])

    async def test_custom_categories_still_require_verifier_agreement(self):
        item = {**ITEM, "organ_system": "Growth and Development"}
        generator = FakeManager([{"questions": [question(**item)]}] * 4)
        generator.build_PANCE_batch_prompt = (
            engine.ConversationManager.build_PANCE_batch_prompt.__get__(generator)
        )
        stats = {}
        with contextlib.redirect_stdout(io.StringIO()):
            results = await engine.process_topic_batch(
                item, 1, generator, FakeManager([verdict("D")] * 4), stats=stats,
                allowed_organ_systems=(item["organ_system"],),
            )
        self.assertEqual(results, [])
        self.assertEqual(stats, {"verifier_disagreed": 4})

    def test_selected_normalization_and_validator_remain_strict(self):
        from fill_database_topics import validate_question

        organ = "Pediatric Renal Development"
        allowed = ("Growth and Development", organ)
        item = {**ITEM, "organ_system": organ}
        candidate = question(**item, difficulty_rating=5)
        self.assertEqual(engine.standardize_categories(organ, ITEM["task_area"])[0], "Renal System")
        self.assertEqual(engine.standardize_categories(
            "growth and development", ITEM["task_area"], allowed_organ_systems=allowed,
        )[0], "Growth and Development")
        with self.assertRaises(ValueError):
            validate_question(candidate, item, engine.standardize_categories)
        self.assertEqual(validate_question(
            {**candidate, "organ_system": organ.lower()}, item, engine.standardize_categories,
            allowed_organ_systems=allowed,
        ), candidate)
        for overrides in (
            {"organ_system": "Renal System"}, {"organ_system": None},
            {"organ_system": "Growth and Development"}, {"task_area": "diagnosis"},
            {"topic_area": "Unrelated"}, {"correct_answer": "F"},
            {"difficulty_rating": True}, {"difficulty_rating": 11},
            {"explanation": ""}, {"answer_choice_E": None},
        ):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                validate_question(
                    {**candidate, **overrides}, item, engine.standardize_categories,
                    allowed_organ_systems=allowed,
                )


class ParsingTests(unittest.TestCase):
    def test_extract_correct_letter_does_not_default_to_a(self):
        q_data = question()
        self.assertIsNone(engine.extract_correct_letter("", q_data))
        self.assertIsNone(engine.extract_correct_letter("Unrelated text", q_data))
        self.assertEqual(engine.extract_correct_letter("b", q_data), "B")
        self.assertEqual(engine.extract_correct_letter("Option C", q_data), "C")
        self.assertEqual(engine.extract_correct_letter("choice d", q_data), "D")
        self.assertEqual(engine.extract_correct_letter(
            "Surgical option", question(answer_choice_C="Surgical option")), "C")

    def test_parse_score(self):
        for raw, expected in ((9, 9), ("8", 8), (0.9, 9), ("90%", 9), ("9/10", 9),
                              ("85/100", 8.5), (95, 9.5), ("7.5", 7.5), (0, 0),
                              (1, 1), ("1", 1), (1.0, 10), ("1.0", 10)):
            with self.subTest(raw=raw):
                self.assertAlmostEqual(engine.parse_score(raw), expected)
        self.assertEqual(engine.parse_score(1.0, allow_fraction=False), 1)
        self.assertEqual(engine.parse_score("80%", allow_fraction=False), 8)
        self.assertIsNone(engine.parse_score(15, allow_fraction=False))
        for raw in (None, True, "high", "", [], 150, -1, float("nan"), "15/10"):
            with self.subTest(raw=raw):
                self.assertIsNone(engine.parse_score(raw))

    def test_parse_json_object(self):
        self.assertEqual(engine.parse_json_object(None), (None, "empty_response"))
        self.assertEqual(engine.parse_json_object("  "), (None, "empty_response"))
        self.assertEqual(engine.parse_json_object("{bad"), (None, "malformed_json"))
        self.assertEqual(engine.parse_json_object("[1]"), (None, "malformed_json"))
        self.assertEqual(engine.parse_json_object('```json\n{"a": 1}\n```'), ({"a": 1}, None))

    def test_shuffle_does_not_mutate_input(self):
        original = question()
        snapshot = dict(original)
        with contextlib.redirect_stdout(io.StringIO()):
            shuffled = engine.shuffle_answer_choices(original, "E", label="x")
        self.assertEqual(original, snapshot)
        self.assertEqual(shuffled["correct_answer"], "E")
        self.assertEqual(shuffled["answer_choice_E"], "Choice B")


class VerificationTests(unittest.IsolatedAsyncioTestCase):
    async def verify(self, responses, q_data=None):
        stats = {}
        q_data = q_data or question()
        snapshot = dict(q_data)
        manager = FakeManager(responses)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            result = await engine.Question_Verification(manager, q_data, label="L", stats=stats)
        self.assertEqual(q_data, snapshot)
        return result, stats, output.getvalue(), manager

    async def test_valid_question_passes_with_normalized_scores(self):
        (ok, verified), stats, _, manager = await self.verify(
            [verdict(), verdict("b", "9/10"), verdict("B", 0.95), {"difficulty": "6"}])
        self.assertTrue(ok)
        self.assertEqual(verified["difficulty_rating"], 6)
        self.assertEqual(verified["correct_answer"], "B")
        self.assertEqual(stats, {})
        self.assertEqual(len(manager.prompts), 4)

    async def test_disagreement_is_logged_and_stops_early(self):
        (ok, verified), stats, output, manager = await self.verify([verdict(), verdict("C")])
        self.assertFalse(ok)
        self.assertIsNone(verified)
        self.assertEqual(stats, {"verifier_disagreed": 1})
        self.assertIn("[L]", output)
        self.assertIn("verifier answer C", output)
        self.assertEqual(len(manager.prompts), 2)

    async def test_low_confidence(self):
        (ok, _), stats, output, _ = await self.verify([verdict(confidence=5)])
        self.assertFalse(ok)
        self.assertEqual(stats, {"verifier_low_confidence": 1})
        self.assertIn("confidence 5", output)

    async def test_unusable_responses_are_retried_then_reported(self):
        (ok, _), stats, output, _ = await self.verify(
            ["not json", RuntimeError("secret-detail"), verdict(confidence="very")])
        self.assertFalse(ok)
        self.assertEqual(stats, {engine.RETRIED_VERIFIER_RESPONSES: 2,
                                 "verifier_confidence_malformed": 1})
        self.assertIn("verifier_malformed_json", output)
        self.assertIn("verifier_confidence_malformed", output)
        self.assertIn("verifier_api_error", output)
        self.assertNotIn("secret-detail", output)

    async def test_transient_unusable_response_does_not_discard_valid_question(self):
        (ok, _), stats, _, _ = await self.verify(
            [verdict(), "", verdict(), verdict(), {"difficulty": 4}])
        self.assertTrue(ok)
        self.assertEqual(stats, {engine.RETRIED_VERIFIER_RESPONSES: 1})

    async def test_unparseable_generator_answer(self):
        (ok, _), stats, _, manager = await self.verify([], question(correct_answer="??"))
        self.assertFalse(ok)
        self.assertEqual(stats, {"generator_answer_unparseable": 1})
        self.assertEqual(manager.prompts, [])

    async def test_unparseable_verifier_answer(self):
        responses = [verdict("no idea")] * 3
        (ok, _), stats, output, _ = await self.verify(responses)
        self.assertFalse(ok)
        self.assertEqual(stats, {engine.RETRIED_VERIFIER_RESPONSES: 2, "verifier_answer_unparseable": 1})
        self.assertIn("'no idea'", output)

    async def test_malformed_difficulty(self):
        (ok, _), stats, _, _ = await self.verify(
            [verdict()] * 3 + [{"difficulty": "hard"}, "nope"])
        self.assertFalse(ok)
        self.assertEqual(stats, {"difficulty_malformed_json": 1})


class BatchTests(unittest.IsolatedAsyncioTestCase):
    async def run_batch(self, generator_responses, verifier_responses, num_needed=2, item=None):
        stats = {}
        item = dict(item or ITEM)
        snapshot = dict(item)
        generator, verifier = FakeManager(generator_responses), FakeManager(verifier_responses)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            results = await engine.process_topic_batch(item, num_needed, generator, verifier, stats=stats)
        self.assertEqual(item, snapshot)
        return results, stats, output.getvalue()

    async def test_requested_metadata_is_preserved(self):
        generated = {"questions": [question(organ_system="Cardiovascular System",
                                            topic_area="Anemia of Chronic Disease",
                                            task_area="Pharmaceutical Therapeutics"),
                                   question(topic_area="string")]}
        passing = [verdict()] * 3 + [{"difficulty": 5}]
        results, stats, output = await self.run_batch([generated], passing * 2)
        self.assertEqual(len(results), 2)
        for result in results:
            self.assertEqual({key: result[key] for key in ITEM}, ITEM)
            self.assertIsInstance(result["difficulty_rating"], int)
        self.assertEqual(stats, {})
        self.assertIn("Generator labeled topic_area as 'Anemia of Chronic Disease'", output)
        self.assertIn("Generator labeled organ_system as 'Cardiovascular System'", output)

    async def test_formatting_only_label_differences_are_not_logged(self):
        generated = {"questions": [question(organ_system="hematologic", task_area="clinical intervention")]}
        results, _, output = await self.run_batch(
            [generated], [verdict()] * 3 + [{"difficulty": 5}], num_needed=1)
        self.assertEqual({key: results[0][key] for key in ITEM}, ITEM)
        self.assertNotIn("Generator labeled", output)

    async def test_partial_failures_keep_verified_questions(self):
        generated = {"questions": [question(), question(question_stem="Second")]}
        verifier = [verdict()] * 3 + [{"difficulty": 5}] + [verdict("D")]
        results, stats, output = await self.run_batch(
            [generated, RuntimeError("down"), "garbage", {"questions": "x"}], verifier)
        self.assertEqual(len(results), 1)
        self.assertEqual(stats, {"verifier_disagreed": 1, "generator_api_error": 1,
                                 "generator_malformed_json": 2})
        self.assertIn("Returning 1/2 questions", output)
        self.assertIn("verifier_disagreed=1", output)

    async def test_reasons_log_only_this_call(self):
        stats = {"verifier_disagreed": 5}
        with contextlib.redirect_stdout(io.StringIO()) as output:
            await engine.process_topic_batch(
                dict(ITEM), 1, FakeManager([{"questions": [question()]}] + ["x"] * 3),
                FakeManager([verdict("D")]), stats=stats)
        self.assertEqual(stats["verifier_disagreed"], 6)
        self.assertIn("Reasons so far: generator_malformed_json=3, verifier_disagreed=1", output.getvalue())

    async def test_non_object_questions_are_counted(self):
        results, stats, _ = await self.run_batch(
            [{"questions": ["bad"]}] * 4, [], num_needed=1)
        self.assertEqual(results, [])
        self.assertEqual(stats, {"generator_invalid_question": 4})


if __name__ == "__main__":
    unittest.main()
