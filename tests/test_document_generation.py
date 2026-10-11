import asyncio
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch


ROOT = Path(__file__).resolve().parents[1]


class DocumentGenerationTests(unittest.TestCase):
    def load(self, name):
        dependencies = {key: MagicMock() for key in ("openai", "db_pool", "dotenv")}
        with patch.dict(sys.modules, dependencies):
            spec = importlib.util.spec_from_file_location(name, ROOT / (name + ".py"))
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        return module

    def test_extraction_accepts_existing_file_id_and_does_not_reupload(self):
        extractor = self.load("EOR_Extraction_optimized")
        manager = MagicMock()
        with patch.object(extractor, "ConversationManager", return_value=manager), \
                patch.object(extractor, "eor_extract_information") as extract:
            extractor.extract_topics_from_openai_file("file-test", "quiz", "/tmp/topics.json")
        extract.assert_called_once_with(manager, "file-test", output_file_path="/tmp/topics.json")
        manager.client.files.create.assert_not_called()
        manager.client.files.delete.assert_not_called()
        manager.client.close.assert_called_once()

    def test_extraction_error_propagates_and_closes_client(self):
        extractor = self.load("EOR_Extraction_optimized")
        manager = MagicMock()
        with patch.object(extractor, "ConversationManager", return_value=manager), \
                patch.object(extractor, "eor_extract_information", side_effect=RuntimeError("failed")):
            with self.assertRaises(RuntimeError):
                extractor.extract_topics_from_openai_file("file-test", "quiz", "/tmp/topics.json")
        manager.client.close.assert_called_once()

    def test_missing_or_empty_topics_fail_before_creating_clients(self):
        generator = self.load("Question_writing_and_verification_optomized")
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(generator, "ConversationManager") as manager:
            missing = str(Path(directory) / "missing.json")
            with self.assertRaises(FileNotFoundError):
                asyncio.run(generator.generate_questions_from_eor_async(
                    missing, "quiz", None, "owner", 1, True))
            empty = Path(directory) / "empty.json"
            empty.write_text('{"extracted_topics": []}\n')
            with self.assertRaises(RuntimeError):
                asyncio.run(generator.generate_questions_from_eor_async(
                    str(empty), "quiz", None, "owner", 1, True))
        manager.assert_not_called()

    def test_generation_database_failure_reaches_worker_and_closes_clients(self):
        generator = self.load("Question_writing_and_verification_optomized")
        manager = MagicMock()
        manager.client.close = AsyncMock()
        generator.get_async_db_connection = AsyncMock(side_effect=RuntimeError("db unavailable"))
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(generator, "ConversationManager", return_value=manager):
            topics = Path(directory) / "topics.json"
            topics.write_text(json.dumps({"extracted_topics": [{"percentage": 100}]}) + "\n")
            with self.assertRaisesRegex(RuntimeError, "db unavailable"):
                asyncio.run(generator.generate_questions_from_eor_async(
                    str(topics), "quiz", None, "owner", 1, True))
        self.assertEqual(manager.client.close.await_count, 2)

    def test_per_topic_counts_and_percentage_allocation_reach_generation(self):
        for use_existing in (False, True):
            with self.subTest(use_existing=use_existing):
                generator = self.load("Question_writing_and_verification_optomized")
                manager = MagicMock()
                manager.client.close = AsyncMock()
                conn = MagicMock()
                cursor = AsyncMock()
                cursor.fetchall.return_value = []
                cursor.rowcount = 100
                conn.cursor.return_value.__aenter__.return_value = cursor
                conn.commit = AsyncMock()
                generator.get_async_db_connection = AsyncMock(return_value=conn)
                items = [
                    {"organ_system": "Cardiovascular System", "topic_area": "Heart failure"},
                    {"organ_system": "Pulmonary System", "topic_area": "Asthma"},
                    {"organ_system": "Endocrine System", "topic_area": "Diabetes",
                     "percentage": 50},
                    {"organ_system": "Neurologic System", "topic_area": "Headache",
                     "percentage": 0},
                ]

                async def generate(sem, item, count, *args):
                    return [{
                        **item, "question_stem": f"{item['topic_area']} {index}"
                    } for index in range(count)]

                with tempfile.TemporaryDirectory() as directory, \
                        patch.object(generator, "ConversationManager", return_value=manager), \
                        patch.object(generator, "sem_process_topic_batch",
                                     side_effect=generate) as batch:
                    topics = Path(directory) / "topics.json"
                    topics.write_text(json.dumps({"extracted_topics": items}) + "\n")
                    asyncio.run(generator.generate_questions_from_eor_async(
                        str(topics), "quiz", None, "owner", 4, use_existing, "quiz-id"))

                self.assertEqual([call.args[2] for call in batch.call_args_list], [4, 4, 2, 1])
                inserted = cursor.executemany.call_args_list[0].args[1]
                mapped = cursor.executemany.call_args_list[1].args[1]
                self.assertEqual(len(inserted), 11)
                self.assertEqual(len(mapped), 11)
                if use_existing:
                    limits = [call.args[1][-1] for call in cursor.execute.call_args_list]
                    self.assertEqual(limits, [4, 4, 2, 1])
                conn.commit.assert_awaited_once()
                self.assertEqual(generator.get_async_db_connection.await_count, 2)


if __name__ == "__main__":
    unittest.main()
