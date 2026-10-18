import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import on_demand_generator as generator
import Question_writing_and_verification_optomized as engine
from standardizers import TASK_AREAS


class OnDemandGenerationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.manager = MagicMock()
        self.manager.client.close = AsyncMock()
        self.conn = MagicMock()
        self.conn.commit = AsyncMock()
        self.conn.rollback = AsyncMock()
        self.cursor = AsyncMock()
        self.conn.cursor.return_value.__aenter__.return_value = self.cursor
        self.requests = [
            {"organ_system": "Hematologic System", "task_area": TASK_AREAS[6], "topic_area": "Anemia"},
            {"organ_system": "Pulmonary System", "task_area": TASK_AREAS[2], "topic_area": "any"},
        ]
        self.questions = [
            {**request, "question_stem": f"Vignette {index}",
             **{f"answer_choice_{letter}": f"Choice {letter}" for letter in "ABCDE"},
             "correct_answer": "B", "explanation": "Because.", "difficulty_rating": 3}
            for index, request in enumerate(self.requests)
        ]
        self.db = AsyncMock(return_value=self.conn)
        self.patches = [
            patch.object(generator, "ConversationManager", return_value=self.manager),
            patch.object(generator, "get_db_connection", self.db),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    async def run_batch(self):
        await generator.async_generate_batch("test-user", "quiz", self.requests, quiz_id="quiz-id")

    async def test_prompts_and_persisted_metadata_honor_resolved_categories(self):
        self.manager.chat_completion = AsyncMock(side_effect=[
            json.dumps({"questions": [{**question, "organ_system": "Misc", "task_area": "Any"}]})
            for question in self.questions
        ])

        async def verify(manager, question, **kwargs):
            return True, question

        async def insert(sql, rows):
            self.cursor.rowcount = len(rows)

        self.cursor.executemany.side_effect = insert
        with patch.object(engine, "Question_Verification", side_effect=verify):
            await self.run_batch()

        prompts = self.manager.build_PANCE_batch_prompt.call_args_list
        self.assertCountEqual(
            [call.args[:3] for call in prompts],
            [(item["organ_system"],
              item["topic_area"] if item["topic_area"] != "any" else engine.DEFAULT_TOPIC_AREA,
              item["task_area"]) for item in self.requests],
        )
        inserted = self.cursor.executemany.call_args_list[0].args[1]
        self.assertCountEqual(
            [(row[1], row[2]) for row in inserted],
            [(item["organ_system"], item["task_area"]) for item in self.requests],
        )
        self.assertEqual(len(self.cursor.executemany.call_args_list[1].args[1]), 2)
        self.conn.commit.assert_awaited_once()
        self.conn.close.assert_called_once()
        self.assertEqual(self.manager.client.close.await_count, 2)

    async def test_partial_generation_cannot_complete_a_short_quiz(self):
        with patch.object(generator, "process_topic_batch", new=AsyncMock(return_value=[])):
            with self.assertRaisesRegex(RuntimeError, "Generated 0/2"):
                await self.run_batch()
        self.db.assert_not_awaited()
        self.assertEqual(self.manager.client.close.await_count, 2)

    def test_wildcard_metadata_uses_model_classification_instead_of_misc_any(self):
        for wildcard in ("any", " ANY ", "", " ", None):
            with self.subTest(wildcard=wildcard):
                result = engine._apply_requested_metadata(
                    self.questions[0], wildcard, wildcard, wildcard, "test")
                for field in ("organ_system", "task_area", "topic_area"):
                    self.assertEqual(result[field], self.questions[0][field])

    def test_invalid_task_metadata_always_resolves_to_a_canonical_task(self):
        invalid_tasks = (
            None, "", " ", "str", "string", "none", " ANY ",
            "General Medical Knowledge", "unknown category", "Professional Practice",
        )
        for requested in invalid_tasks:
            for llm_task in invalid_tasks:
                with self.subTest(requested=requested, llm_task=llm_task):
                    question = {**self.questions[0], "task_area": llm_task}
                    with patch.object(engine.random, "choice", return_value=TASK_AREAS[4]) as choice:
                        result = engine._apply_requested_metadata(
                            question, "any", requested, "any", "test")
                    choice.assert_called_once_with(TASK_AREAS)
                    self.assertIn(result["task_area"], TASK_AREAS)
                    self.assertNotEqual(result["task_area"], "General Medical Knowledge")
                    self.assertEqual(result["organ_system"], question["organ_system"])
                    self.assertEqual(result["topic_area"], question["topic_area"])
                    self.assertEqual(question["task_area"], llm_task)

    def test_valid_requested_or_model_task_is_preserved_before_random_fallback(self):
        for task in TASK_AREAS:
            for requested, llm_task in (
                (task, TASK_AREAS[0]), ("General Medical Knowledge", task),
                ("any", task), (None, task),
            ):
                with self.subTest(requested=requested, llm_task=llm_task):
                    with patch.object(engine.random, "choice") as choice:
                        result = engine._apply_requested_metadata(
                            {**self.questions[0], "task_area": llm_task},
                            "hematologic", requested, None, "test")
                    self.assertEqual(result["task_area"], task)
                    self.assertEqual(result["organ_system"], "Hematologic System")
                    choice.assert_not_called()
        self.assertEqual(
            engine._resolve_task_area("Using Laboratory and Diagnostic Studies"),
            TASK_AREAS[1],
        )

    async def test_generic_and_placeholder_tasks_are_canonical_before_bank_insert(self):
        self.requests[0]["task_area"] = "General Medical Knowledge"
        self.requests[1]["task_area"] = "any"
        self.manager.chat_completion = AsyncMock(side_effect=[
            json.dumps({"questions": [{**question, "task_area": "General Medical Knowledge"}]})
            for question in self.questions
        ])

        async def verify(manager, question, **kwargs):
            return True, question

        async def insert(sql, rows):
            self.cursor.rowcount = len(rows)

        self.cursor.executemany.side_effect = insert
        with patch.object(engine, "Question_Verification", side_effect=verify):
            await self.run_batch()

        inserted = self.cursor.executemany.call_args_list[0].args[1]
        self.assertEqual(len(inserted), 2)
        for row in inserted:
            self.assertIn(row[2], TASK_AREAS)
            self.assertNotEqual(row[2], "General Medical Knowledge")
        self.assertCountEqual(
            [row[1] for row in inserted],
            [item["organ_system"] for item in self.requests],
        )
        self.conn.commit.assert_awaited_once()

    async def test_duplicate_generation_cannot_reduce_quiz_count(self):
        async def generate(*args):
            return [dict(self.questions[0])]

        with patch.object(generator, "process_topic_batch", side_effect=generate):
            with self.assertRaisesRegex(RuntimeError, "duplicate questions"):
                await self.run_batch()
        self.db.assert_not_awaited()

    async def test_ignored_bank_inserts_resolve_every_id_before_mapping(self):
        async def generate(item, count, *args):
            return [dict(question) for question in self.questions
                    if question["organ_system"] == item["organ_system"]]

        async def insert(sql, rows):
            self.cursor.rowcount = 1 if "question_bank" in sql else len(rows)

        async def select(sql, params):
            self.cursor.rowcount = 1

        self.cursor.executemany.side_effect = insert
        self.cursor.execute.side_effect = select
        self.cursor.fetchone.side_effect = [{"id": "existing-one"}, {"id": "existing-two"}]
        with patch.object(generator, "process_topic_batch", side_effect=generate):
            await self.run_batch()
        mappings = self.cursor.executemany.call_args_list[1].args[1]
        self.assertEqual(mappings, [("quiz-id", "existing-one"), ("quiz-id", "existing-two")])
        self.conn.commit.assert_awaited_once()

    async def test_incomplete_mapping_rolls_back_and_propagates_failure(self):
        async def generate(item, count, *args):
            return [dict(question) for question in self.questions
                    if question["organ_system"] == item["organ_system"]]

        async def insert(sql, rows):
            self.cursor.rowcount = len(rows) if "question_bank" in sql else 1

        self.cursor.executemany.side_effect = insert
        with patch.object(generator, "process_topic_batch", side_effect=generate):
            with self.assertRaisesRegex(RuntimeError, "Not all generated questions"):
                await self.run_batch()
        self.conn.commit.assert_not_awaited()
        self.conn.rollback.assert_awaited_once()
        self.conn.close.assert_called_once()

    async def test_unresolved_bank_insert_rolls_back_instead_of_mapping_a_missing_id(self):
        async def generate(item, count, *args):
            return [dict(question) for question in self.questions
                    if question["organ_system"] == item["organ_system"]]

        self.cursor.rowcount = 0
        self.cursor.fetchone.return_value = None
        with patch.object(generator, "process_topic_batch", side_effect=generate):
            with self.assertRaisesRegex(RuntimeError, "could not be resolved"):
                await self.run_batch()
        self.assertEqual(self.cursor.executemany.await_count, 1)
        self.conn.commit.assert_not_awaited()
        self.conn.rollback.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
