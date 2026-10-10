import importlib.util
import os
import json
import tempfile
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock, patch


ROOT = Path(__file__).resolve().parents[1]


def load_module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class QuizWorkerTests(unittest.TestCase):
    def setUp(self):
        import document_storage
        self.document_storage = document_storage
        modules = {
            name: MagicMock()
            for name in (
                "pymysql", "dbutils", "dbutils.pooled_db", "dotenv",
                "EOR_Extraction_optimized",
                "Question_writing_and_verification_optomized",
                "on_demand_generator", "create_remediation_assignment",
            )
        }
        with patch.dict(sys.modules, modules), patch.object(sys, "stdout"), \
                patch.object(sys, "stderr"):
            self.db_pool = load_module("db_pool")
            with patch.dict(sys.modules, {"db_pool": self.db_pool}):
                self.worker = load_module("quiz_worker")
        self.pool_factory = modules["dbutils.pooled_db"].PooledDB
        self.task = {
            "id": 1, "username": "test-user", "quiz_name": "test-quiz",
            "task_type": "blueprint",
            "payload": '{"generation_requests": [], "quiz_id": 2}',
        }
        self.original_validated_file_id = self.worker.validated_file_id
        self.original_delete_document = self.worker.delete_document
        validation_patch = patch.object(
            self.worker, "validated_file_id", return_value="file-test")
        delete_patch = patch.object(self.worker, "delete_document")
        directory_patch = patch.object(
            self.worker, "TemporaryDirectory",
            side_effect=lambda **kwargs: tempfile.TemporaryDirectory(dir=ROOT, **kwargs),
        )
        self.validated_file_id = validation_patch.start()
        self.delete_document = delete_patch.start()
        directory_patch.start()
        for patcher in (validation_patch, delete_patch, directory_patch):
            self.addCleanup(patcher.stop)

    def document_task(self, task_type="document"):
        return {**self.task, "task_type": task_type, "payload": json.dumps({
            "openai_file_id": "file-test", "owner": "test-user", "quiz_id": 2,
            "num_per_topic": 1, "use_existing": True,
            "remediation_name": "test-remediation",
        })}

    def connection(self, task=None):
        conn = MagicMock(spec=["cursor", "commit", "close"])
        conn.cursor.return_value.fetchone.return_value = task
        conn.cursor.return_value.rowcount = 1
        return conn

    def run_worker(self, connections):
        with patch.object(self.worker, "get_db_connection",
                          side_effect=[*connections, KeyboardInterrupt]) as get_conn, \
                patch.object(self.worker.time, "sleep") as sleep, \
                patch("builtins.print"):
            with self.assertRaises(KeyboardInterrupt):
                self.worker.process_tasks()
        return get_conn, sleep

    def test_worker_imports_shared_connection_factory(self):
        self.assertIs(self.worker.get_db_connection, self.db_pool.get_db_connection)

    def test_worker_imports_openai_document_helpers(self):
        self.assertIs(self.original_validated_file_id, self.document_storage.validated_file_id)
        self.assertIs(self.original_delete_document, self.document_storage.delete_document)

    def test_shared_pool_uses_host_port_and_tls(self):
        env = {
            "MYSQL_HOST": "database.example.test", "MYSQL_PORT": "25060",
            "MYSQL_USER": "test-user", "MYSQL_PASSWORD": "test-only",
            "MYSQL_DB": "test-db", "MYSQL_SSL_DISABLED": "false",
            "MYSQL_SSL_CA": "/tmp/test-ca.pem",
        }
        with patch.dict(os.environ, env, clear=True):
            conn = self.worker.get_db_connection()
            self.assertIs(conn, self.pool_factory.return_value.connection.return_value)
            self.worker.get_db_connection()
        self.pool_factory.assert_called_once()
        kwargs = self.pool_factory.call_args.kwargs
        self.assertEqual(kwargs["host"], env["MYSQL_HOST"])
        self.assertEqual(kwargs["port"], 25060)
        self.assertEqual(kwargs["ssl"], {"ca": env["MYSQL_SSL_CA"]})

    def test_missing_configuration_does_not_connect_to_localhost(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "MYSQL_HOST"):
                self.worker.get_db_connection()
        self.pool_factory.assert_not_called()

    def test_success_releases_connection_before_generation(self):
        fetch_conn = self.connection(self.task)
        complete_conn = self.connection()

        def generate(*args, **kwargs):
            fetch_conn.close.assert_called_once()

        self.worker.on_demand_generator.generate_batch.side_effect = generate
        self.run_worker([fetch_conn, complete_conn])
        complete_conn.cursor.return_value.execute.assert_called_once_with(
            "UPDATE quiz_generation_tasks SET status = 'completed', updated_at = NOW() WHERE id = %s",
            (1,),
        )
        fetch_conn.close.assert_called_once()
        complete_conn.close.assert_called_once()

    def test_empty_queue_returns_connection_once(self):
        conn = self.connection()
        with patch.object(self.worker, "get_db_connection", return_value=conn), \
                patch.object(self.worker.time, "sleep", side_effect=KeyboardInterrupt), \
                patch("builtins.print"):
            with self.assertRaises(KeyboardInterrupt):
                self.worker.process_tasks()
        conn.close.assert_called_once()

    def test_generation_failure_records_status_with_fresh_connection(self):
        fetch_conn = self.connection(self.task)
        failed_conn = self.connection()
        self.worker.on_demand_generator.generate_batch.side_effect = RuntimeError("generation failed")
        _, sleep = self.run_worker([fetch_conn, failed_conn])
        failed_conn.cursor.return_value.execute.assert_called_once_with(
            "UPDATE quiz_generation_tasks SET status = 'failed', updated_at = NOW() WHERE id = %s",
            (1,),
        )
        failed_conn.close.assert_called_once()
        sleep.assert_called_once_with(5)

    def test_broken_connection_is_replaced_for_failure_update(self):
        fetch_conn = self.connection(self.task)
        failed_conn = self.connection()
        fetch_conn.cursor.return_value.execute.side_effect = [None, RuntimeError("connection lost")]
        self.run_worker([fetch_conn, failed_conn])
        fetch_conn.close.assert_called_once()
        failed_conn.cursor.return_value.execute.assert_called_once()
        failed_conn.close.assert_called_once()

    def test_connection_failure_without_task_waits_before_retry(self):
        get_conn, sleep = self.run_worker([RuntimeError("database unavailable")])
        self.assertEqual(get_conn.call_count, 2)
        sleep.assert_called_once_with(5)

    def test_close_failure_still_allows_failure_status_update(self):
        fetch_conn = self.connection(self.task)
        failed_conn = self.connection()
        fetch_conn.cursor.return_value.execute.side_effect = [None, RuntimeError("connection lost")]
        fetch_conn.close.side_effect = RuntimeError("close failed")
        self.run_worker([fetch_conn, failed_conn])
        fetch_conn.close.assert_called_once()
        failed_conn.cursor.return_value.execute.assert_called_once()
        failed_conn.close.assert_called_once()

    def test_cleanup_error_does_not_stop_worker(self):
        fetch_conn = self.connection(self.task)
        complete_conn = self.connection()
        complete_conn.close.side_effect = RuntimeError("close failed")
        self.run_worker([fetch_conn, complete_conn])
        complete_conn.close.assert_called_once()

    def test_document_uses_openai_file_and_cleans_temporary_topics(self):
        task = self.document_task()
        paths = []
        fetch = self.connection(task)
        complete = self.connection()

        def extract(file_id, name, output):
            fetch.close.assert_called_once()
            self.assertEqual((file_id, name), ("file-test", "test-quiz"))
            self.assertTrue(Path(output).parent.is_dir())
            self.assertEqual(Path(output).name, "extracted_topics.json")
            paths.append(output)
            Path(output).write_text('{"extracted_topics": []}\n')

        def generate(source, name, output, *args):
            self.assertTrue(Path(source).is_file())
            self.assertIsNone(output)

        self.worker.eor_extractor.extract_topics_from_openai_file.side_effect = extract
        self.worker.question_generator.generate_questions_from_eor.side_effect = generate
        self.run_worker([fetch, complete])
        self.validated_file_id.assert_called_once_with(json.loads(task["payload"]), "test-user")
        self.worker.question_generator.generate_questions_from_eor.assert_called_once_with(
            paths[0], "test-quiz", None, "test-user", 1, True, 2)
        self.assertFalse(Path(paths[0]).parent.exists())
        self.delete_document.assert_called_once_with(json.loads(task["payload"]), "test-user")
        self.assertIn("'completed'", complete.cursor.return_value.execute.call_args.args[0])

    def test_extraction_failure_deletes_openai_file_and_temporary_topics(self):
        task = self.document_task()
        paths = []

        def extract(file_id, name, output):
            paths.append(output)
            Path(output).write_text("partial extraction")
            raise RuntimeError("failed")

        self.worker.eor_extractor.extract_topics_from_openai_file.side_effect = extract
        failed = self.connection()
        self.run_worker([self.connection(task), failed])
        self.assertFalse(Path(paths[0]).parent.exists())
        self.worker.question_generator.generate_questions_from_eor.assert_not_called()
        self.delete_document.assert_called_once_with(json.loads(task["payload"]), "test-user")
        self.assertIn("'failed'", failed.cursor.return_value.execute.call_args.args[0])

    def test_generation_failure_deletes_openai_file(self):
        task = self.document_task()
        failed = self.connection()
        self.worker.question_generator.generate_questions_from_eor.side_effect = RuntimeError("failed")
        self.run_worker([self.connection(task), failed])
        self.delete_document.assert_called_once_with(json.loads(task["payload"]), "test-user")
        self.assertIn("'failed'", failed.cursor.return_value.execute.call_args.args[0])

    def test_openai_cleanup_failure_does_not_fail_completed_quiz(self):
        task = self.document_task()
        complete = self.connection()
        self.delete_document.side_effect = RuntimeError("OpenAI unavailable")
        self.run_worker([self.connection(task), complete])
        self.delete_document.assert_called_once()
        complete.cursor.return_value.execute.assert_called_once()
        self.assertIn("'completed'", complete.cursor.return_value.execute.call_args.args[0])

    def test_legacy_document_validation_failure_does_not_generate(self):
        for task_type in ("document", "remediation_pdf"):
            with self.subTest(task_type=task_type):
                task = {**self.task, "task_type": task_type,
                        "payload": '{"filepath": "/home/old/report.pdf"}'}
                self.validated_file_id.side_effect = ValueError("Please reupload the PDF")
                self.delete_document.reset_mock()
                failed = self.connection()
                self.run_worker([self.connection(task), failed])
                self.worker.eor_extractor.extract_topics_from_openai_file.assert_not_called()
                self.worker.question_generator.generate_questions_from_eor.assert_not_called()
                self.worker.start_remediation_job.assert_not_called()
                self.delete_document.assert_called_once_with(json.loads(task["payload"]), "test-user")
                self.assertIn("'failed'", failed.cursor.return_value.execute.call_args.args[0])

    def test_remediation_uses_uploaded_openai_file(self):
        task = self.document_task("remediation_pdf")
        self.worker.start_remediation_job.return_value = ["module-id"]
        complete = self.connection()
        self.run_worker([self.connection(task), complete])
        self.worker.start_remediation_job.assert_called_once_with(
            None, "test-user", "test-remediation", uploaded_file_id="file-test")
        self.delete_document.assert_called_once_with(json.loads(task["payload"]), "test-user")
        self.assertIn("'completed'", complete.cursor.return_value.execute.call_args.args[0])

    def test_remediation_failure_deletes_openai_file(self):
        task = self.document_task("remediation_pdf")
        for result in (RuntimeError("generation failed"), []):
            with self.subTest(result=result):
                self.worker.start_remediation_job.side_effect = (
                    result if isinstance(result, Exception) else None)
                self.worker.start_remediation_job.return_value = result
                self.delete_document.reset_mock()
                failed = self.connection()
                self.run_worker([self.connection(task), failed])
                self.delete_document.assert_called_once_with(json.loads(task["payload"]), "test-user")
                self.assertIn("'failed'", failed.cursor.return_value.execute.call_args.args[0])

    def test_lost_claim_does_not_generate(self):
        conn = self.connection(self.task)
        conn.cursor.return_value.rowcount = 0
        self.run_worker([conn])
        self.worker.on_demand_generator.generate_batch.assert_not_called()
        conn.close.assert_called_once()

    def test_lost_document_claim_does_not_generate_or_delete(self):
        for task_type in ("document", "remediation_pdf"):
            with self.subTest(task_type=task_type):
                conn = self.connection(self.document_task(task_type))
                conn.cursor.return_value.rowcount = 0
                self.run_worker([conn])
                self.validated_file_id.assert_not_called()
                self.worker.eor_extractor.extract_topics_from_openai_file.assert_not_called()
                self.worker.question_generator.generate_questions_from_eor.assert_not_called()
                self.worker.start_remediation_job.assert_not_called()
                self.delete_document.assert_not_called()
                conn.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
