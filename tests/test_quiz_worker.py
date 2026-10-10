import importlib.util
import os
import json
import tempfile
from contextlib import contextmanager
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

    def test_document_download_and_artifacts_use_temporary_paths(self):
        task = {**self.task, "task_type": "document", "payload": json.dumps({
            "object_key": "test-key", "owner": "test-user", "quiz_id": 2,
            "num_per_topic": 1, "use_existing": True,
        })}
        paths = []

        @contextmanager
        def download(payload, username):
            self.assertEqual(payload["owner"], username)
            with tempfile.TemporaryDirectory() as directory:
                paths.append(directory)
                yield os.path.join(directory, "source.pdf")

        def extract(filepath, name, output):
            Path(output).write_text('{"extracted_topics": []}\n')

        def generate(source, name, output, *args):
            Path(output).write_text('[]')

        self.worker.eor_extractor.extract_topics_from_eor_file.side_effect = extract
        self.worker.question_generator.generate_questions_from_eor.side_effect = generate
        with patch.object(self.worker, "download_document", side_effect=download), \
                patch.object(self.worker, "upload_json_artifact") as upload:
            self.run_worker([self.connection(task), self.connection()])
        self.assertEqual(upload.call_count, 2)
        self.assertEqual([call.args[3] for call in upload.call_args_list],
                         ["extracted_topics.json", "output.json"])
        self.assertFalse(os.path.exists(paths[0]))

    def test_failed_document_keeps_original_and_cleans_temporary_copy(self):
        task = {**self.task, "task_type": "document", "payload": "{}"}
        paths = []

        @contextmanager
        def download(*args):
            with tempfile.TemporaryDirectory() as directory:
                paths.append(directory)
                yield os.path.join(directory, "source.pdf")

        self.worker.eor_extractor.extract_topics_from_eor_file.side_effect = RuntimeError("failed")
        failed = self.connection()
        with patch.object(self.worker, "download_document", side_effect=download), \
                patch.object(self.worker, "upload_json_artifact") as upload:
            self.run_worker([self.connection(task), failed])
        self.assertFalse(os.path.exists(paths[0]))
        upload.assert_not_called()
        self.assertIn("'failed'", failed.cursor.return_value.execute.call_args.args[0])

    def test_missing_generated_json_is_not_marked_completed(self):
        task = {**self.task, "task_type": "document", "payload": "{}"}
        failed = self.connection()
        with tempfile.TemporaryDirectory() as directory:
            @contextmanager
            def download(*args):
                yield os.path.join(directory, "source.pdf")
            with patch.object(self.worker, "download_document", side_effect=download):
                self.run_worker([self.connection(task), failed])
        self.assertIn("'failed'", failed.cursor.return_value.execute.call_args.args[0])

    def test_artifact_storage_failure_does_not_fail_committed_quiz(self):
        task = {**self.task, "task_type": "document", "payload": "{}"}
        complete = self.connection()
        with tempfile.TemporaryDirectory() as directory:
            @contextmanager
            def download(*args):
                yield os.path.join(directory, "source.pdf")
            def extract(filepath, name, output):
                Path(output).write_text('{"extracted_topics": []}\n')
            def generate(source, name, output, *args):
                Path(output).write_text('[]')
            self.worker.eor_extractor.extract_topics_from_eor_file.side_effect = extract
            self.worker.question_generator.generate_questions_from_eor.side_effect = generate
            with patch.object(self.worker, "download_document", side_effect=download), \
                    patch.object(self.worker, "upload_json_artifact", side_effect=RuntimeError("S3 unavailable")):
                self.run_worker([self.connection(task), complete])
        self.assertIn("'completed'", complete.cursor.return_value.execute.call_args.args[0])

    def test_remediation_uses_downloaded_pdf(self):
        task = {**self.task, "task_type": "remediation_pdf",
                "payload": '{"remediation_name": "test-remediation"}'}
        @contextmanager
        def download(*args):
            yield "/tmp/test-source.pdf"
        self.worker.start_remediation_job.return_value = ["module-id"]
        with patch.object(self.worker, "download_document", side_effect=download):
            self.run_worker([self.connection(task), self.connection()])
        self.worker.start_remediation_job.assert_called_once_with(
            "/tmp/test-source.pdf", "test-user", "test-remediation")

    def test_lost_claim_does_not_generate(self):
        conn = self.connection(self.task)
        conn.cursor.return_value.rowcount = 0
        self.run_worker([conn])
        self.worker.on_demand_generator.generate_batch.assert_not_called()
        conn.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
