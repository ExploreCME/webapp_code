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
        dependencies = {key: MagicMock() for key in ("openai", "aiomysql", "dotenv")}
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
        generator.aiomysql.connect = AsyncMock(side_effect=RuntimeError("db unavailable"))
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(generator, "ConversationManager", return_value=manager):
            topics = Path(directory) / "topics.json"
            topics.write_text(json.dumps({"extracted_topics": [{"percentage": 100}]}) + "\n")
            with self.assertRaisesRegex(RuntimeError, "db unavailable"):
                asyncio.run(generator.generate_questions_from_eor_async(
                    str(topics), "quiz", None, "owner", 1, True))
        self.assertEqual(manager.client.close.await_count, 2)


if __name__ == "__main__":
    unittest.main()
