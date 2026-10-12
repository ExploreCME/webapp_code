import asyncio
from concurrent.futures import ThreadPoolExecutor
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("fill_database_topics", ROOT / "fill_database_topics.py")
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)
SYSTEM = "Pulmonary System"


def question(item, stem="A clinical vignette"):
    return {**item, "question_stem": stem,
            **{f"answer_choice_{letter}": f"Choice {letter}" for letter in "ABCDE"},
            "correct_answer": "A", "explanation": "Explanation", "difficulty_rating": 5}


def engine_mock():
    managers = [SimpleNamespace(client=SimpleNamespace(close=AsyncMock())) for _ in range(2)]
    engine = SimpleNamespace(
        ConversationManager=MagicMock(side_effect=managers),
        process_topic_batch=AsyncMock(),
        standardize_categories=lambda organ, task: (organ, task),
        DEFAULT_MODEL="repository-model",
    )
    return engine, managers


class SelectionTests(unittest.TestCase):
    def test_help_and_dry_run_without_optional_dependencies_or_credentials(self):
        for arguments in (["--help"], ["--organ-system", SYSTEM, "--topic", "Asthma", "--dry-run"],
                          ["--list-systems"]):
            result = subprocess.run(
                [sys.executable, "-S", str(ROOT / "fill_database_topics.py"), *arguments],
                env={"PATH": os.environ.get("PATH", "")}, capture_output=True, text=True,
                timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            if "--dry-run" in arguments:
                self.assertIn("Target: 7", result.stdout)
                self.assertIn("no API calls", result.stdout)

    def test_invalid_arguments(self):
        for arguments in (
            ["--questions-per-task", "0"], ["--concurrency", "-1"],
            ["--concurrency", "many"], ["--organ-system", "pulmonary"],
            ["--all", "--organ-system", SYSTEM],
        ):
            with self.subTest(arguments=arguments), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    cli.parser().parse_args(arguments)
                self.assertEqual(error.exception.code, 2)

    def test_json_validation_and_deduplication(self):
        invalid = [[], {}, {SYSTEM: []}, {SYSTEM: "Asthma"}, {"Misc": ["Asthma"]},
                   {SYSTEM: [""]}, {SYSTEM: [" Asthma"]}, {SYSTEM: [3]},
                   {SYSTEM: ["x" * 256]}]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "topics.json"
            for data in invalid:
                path.write_text(json.dumps(data), encoding="utf-8")
                with self.subTest(data=data), self.assertRaises(ValueError):
                    cli.load_topics(path)
            path.write_text(json.dumps({SYSTEM: ["Asthma", "Asthma", "COPD"]}), encoding="utf-8")
            self.assertEqual(cli.load_topics(path), {SYSTEM: ["Asthma", "COPD"]})
            path.write_text('{"Pulmonary System":["Asthma"],"Pulmonary System":["COPD"]}')
            with self.assertRaisesRegex(ValueError, "Duplicate JSON key"):
                cli.load_topics(path)
            path.write_text("{invalid")
            with self.assertRaises(ValueError):
                cli.load_topics(path)

    def test_unknown_selection_and_noninteractive_input(self):
        data = {SYSTEM: ["Asthma"]}
        for arguments in (["--organ-system", "Renal System"], ["--all", "--topic", "Unknown"], []):
            with patch.object(sys, "stdin") as stdin:
                stdin.isatty.return_value = False
                with self.assertRaises(ValueError):
                    cli.select_topics(cli.parser().parse_args(arguments), data)

    def test_interactive_menu_and_topic_filter(self):
        data = {SYSTEM: ["Asthma", "COPD"], "Renal System": ["AKI"]}
        args = cli.parser().parse_args(["--topic", "Asthma", "--topic", "Asthma"])
        with patch.object(sys, "stdin") as stdin, patch("builtins.input", return_value="1"), \
                contextlib.redirect_stdout(io.StringIO()):
            stdin.isatty.return_value = True
            self.assertEqual(cli.select_topics(args, data), [(SYSTEM, "Asthma")])
        with patch.object(sys, "stdin") as stdin, patch("builtins.input", return_value="0"), \
                contextlib.redirect_stdout(io.StringIO()):
            stdin.isatty.return_value = True
            with self.assertRaises(ValueError):
                cli.select_topics(args, data)

    def test_two_topics_target_fourteen_without_runtime_imports(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "topics.json"
            path.write_text(json.dumps({SYSTEM: ["Asthma", "COPD"]}))
            with patch.object(cli.importlib, "import_module", side_effect=AssertionError), \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(cli.main(["--all", "--topics-file", str(path), "--dry-run"]), 0)
            self.assertIn("Target: 14", output.getvalue())


class ValidationTests(unittest.TestCase):
    def test_malformed_questions_and_wrong_categories(self):
        item = {"organ_system": SYSTEM, "topic_area": "Asthma", "task_area": cli.TASK_AREAS[0]}
        invalid = [None, {}, *[
            {**question(item), field: value} for field, value in (
                ("question_stem", " "), ("answer_choice_E", ""),
                ("correct_answer", "AB"), ("correct_answer", "F"), ("explanation", None),
                ("difficulty_rating", True), ("difficulty_rating", 0),
                ("difficulty_rating", 11), ("difficulty_rating", "5"),
                ("organ_system", "Renal System"), ("organ_system", "Pulmonry"),
                ("task_area", cli.TASK_AREAS[1]), ("topic_area", "COPD"),
            )
        ]]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                cli.validate_question(value, item, lambda organ, task: (organ, task))
        valid = {**question(item), "organ_system": " pulmonary system "}
        self.assertEqual(cli.validate_question(valid, item, lambda organ, task: (organ, task))[
            "organ_system"], SYSTEM)
        with self.assertRaises(ValueError):
            cli.validate_question(question(item), item, lambda organ, task: ("Renal System", task))


class GenerationTests(unittest.IsolatedAsyncioTestCase):
    async def run_generation(self, process=None, save=None, arguments=None):
        engine, managers = engine_mock()
        engine.process_topic_batch.side_effect = process or (
            lambda **kwargs: [question(kwargs["item_dict"])])
        writer = MagicMock()
        writer.save.side_effect = save or (
            lambda questions: {"inserted": len(questions), "reused": 0,
                               "mapped": len(questions), "persisted": len(questions)})
        args = cli.parser().parse_args(arguments or [])
        with contextlib.redirect_stdout(io.StringIO()) as output, \
                contextlib.redirect_stderr(io.StringIO()):
            status = await cli.generate(args, [(SYSTEM, "Asthma"), (SYSTEM, "COPD")],
                                        engine, writer, "test-key", "chosen-model")
        return status, output.getvalue(), engine, writer, managers

    async def test_counts_and_bounded_concurrency(self):
        active = peak = 0

        async def process(**kwargs):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.001)
            active -= 1
            return [question(kwargs["item_dict"], f"Stem {i}") for i in range(kwargs["num_needed"])]

        status, output, engine, writer, managers = await self.run_generation(
            process, arguments=["--concurrency", "3", "--questions-per-task", "2"])
        self.assertEqual(status, 0)
        self.assertEqual(peak, 3)
        self.assertEqual(engine.process_topic_batch.call_count, 14)
        self.assertEqual(writer.save.call_count, 14)
        self.assertIn("requested=28, generated=28", output)
        self.assertIn("persisted=28", output)
        for manager in managers:
            manager.client.close.assert_awaited_once()
        self.assertEqual(engine.ConversationManager.call_args.kwargs,
                         {"api_key": "test-key", "model": "chosen-model"})

    async def test_one_failure_preserves_other_batches(self):
        async def process(**kwargs):
            item = kwargs["item_dict"]
            if item["topic_area"] == "Asthma" and item["task_area"] == cli.TASK_AREAS[0]:
                raise RuntimeError("secret must not be logged")
            return [question(item)]
        status, output, _, writer, _ = await self.run_generation(process)
        self.assertEqual(status, 1)
        self.assertEqual(writer.save.call_count, 13)
        self.assertIn("failed=1, shortfall=1", output)

    async def test_empty_batch_shortfall(self):
        status, output, _, writer, _ = await self.run_generation(lambda **kwargs: [])
        self.assertEqual(status, 1)
        writer.save.assert_not_called()
        self.assertIn("shortfall=14", output)

    async def test_malformed_and_duplicate_results(self):
        status, output, _, writer, _ = await self.run_generation(
            lambda **kwargs: [None, question(kwargs["item_dict"]), question(kwargs["item_dict"])])
        self.assertEqual(status, 1)
        self.assertEqual(writer.save.call_count, 14)
        self.assertIn("rejected=28", output)

    async def test_shortfall_reports_discard_reasons_per_job(self):
        def process(**kwargs):
            item = kwargs["item_dict"]
            if item["topic_area"] == "Asthma":
                kwargs["stats"]["verifier_disagreed"] = 1
                return []
            return [{**question(item), "topic_area": "Asthma"}]
        status, output, _, _, _ = await self.run_generation(process)
        self.assertEqual(status, 1)
        self.assertIn(f"Shortfall: {SYSTEM} / Asthma / {cli.TASK_AREAS[0]}: accepted 0/1 "
                      "(discard reasons: verifier_disagreed=1)", output)
        self.assertIn(f"Rejected: {SYSTEM} / COPD / {cli.TASK_AREAS[0]}: Wrong topic_area", output)
        self.assertNotIn("/ COPD / " + cli.TASK_AREAS[0] + ": accepted 0/1 (", output)
        self.assertIn("Discard reasons: rejected: Wrong topic_area=7, verifier_disagreed=7", output)

    async def test_nonlist_results(self):
        status, output, _, writer, _ = await self.run_generation(lambda **kwargs: {})
        self.assertEqual(status, 1)
        self.assertIn("failed=14", output)
        writer.save.assert_not_called()

    async def test_persistence_failure_is_nonzero(self):
        def save(questions):
            raise RuntimeError("DB unavailable")
        status, output, _, writer, _ = await self.run_generation(save=save)
        self.assertEqual(status, 1)
        self.assertEqual(writer.save.call_count, 14)
        self.assertIn("persisted=0, failed=14", output)

    async def test_later_persistence_failure_preserves_committed_counts(self):
        calls = 0
        def save(questions):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("second batch failed")
            return {"inserted": 1, "reused": 0, "mapped": 1, "persisted": 1}
        status, output, _, _, _ = await self.run_generation(
            save=save, arguments=["--concurrency", "1"])
        self.assertEqual(status, 1)
        self.assertIn("persisted=13, failed=1", output)

    async def test_second_manager_construction_failure_closes_first(self):
        engine, managers = engine_mock()
        engine.ConversationManager.side_effect = [managers[0], RuntimeError("constructor")]
        with self.assertRaises(RuntimeError):
            await cli.generate(cli.parser().parse_args([]), [(SYSTEM, "Asthma")],
                               engine, MagicMock(), "test-key", "model")
        managers[0].client.close.assert_awaited_once()

    async def test_close_failure_still_closes_second_client(self):
        engine, managers = engine_mock()
        managers[0].client.close.side_effect = RuntimeError("close")
        engine.process_topic_batch.return_value = []
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(await cli.generate(cli.parser().parse_args([]), [(SYSTEM, "Asthma")],
                                               engine, MagicMock(), "test-key", "model"), 1)
        managers[1].client.close.assert_awaited_once()

    async def test_cancellation_drains_workers_and_closes_clients(self):
        engine, managers = engine_mock()
        started = asyncio.Event()
        stopped = []

        async def process(**kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.append(True)
        engine.process_topic_batch.side_effect = process
        task = asyncio.create_task(cli.generate(
            cli.parser().parse_args(["--concurrency", "2"]), [(SYSTEM, "Asthma")],
            engine, MagicMock(), "test-key", "model"))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(len(stopped), 2)
        for manager in managers:
            manager.client.close.assert_awaited_once()

    async def test_fatal_worker_error_cancels_others(self):
        engine, managers = engine_mock()
        # A BaseException other than KeyboardInterrupt avoids interrupting the test loop.
        class Fatal(BaseException):
            pass
        engine.process_topic_batch.side_effect = Fatal()
        with self.assertRaises(Fatal):
            await cli.generate(cli.parser().parse_args([]), [(SYSTEM, "Asthma")],
                               engine, MagicMock(), "test-key", "model")
        for manager in managers:
            manager.client.close.assert_awaited_once()

    async def test_cancelled_database_thread_is_drained(self):
        started = threading.Event()
        release = threading.Event()
        completed = threading.Event()
        def save():
            started.set()
            release.wait(timeout=5)
            completed.set()
        with ThreadPoolExecutor(max_workers=1) as executor:
            task = asyncio.create_task(cli.thread_call(executor, save))
            while not started.is_set():
                await asyncio.sleep(0.001)
            task.cancel()
            await asyncio.sleep(0.001)
            self.assertFalse(task.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(completed.is_set())


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.conn = MagicMock()
        self.cursor = self.conn.cursor.return_value.__enter__.return_value
        self.writer = cli.DatabaseWriter(lambda: self.conn, "test-user", "test-quiz")
        self.item = {"organ_system": SYSTEM, "topic_area": "Asthma", "task_area": cli.TASK_AREAS[0]}

    def test_conflicting_quiz_requeries_actual_id_and_inserts_strictly(self):
        self.cursor.fetchone.side_effect = [{"quiz_id": "actual-quiz"}, None, None]
        self.cursor.rowcount = 0
        counts = self.writer.save([question(self.item)])
        calls = self.cursor.execute.call_args_list
        self.assertIn("ON DUPLICATE KEY UPDATE", calls[0].args[0])
        self.assertIn("SELECT quiz_id", calls[1].args[0])
        inserted_id = calls[3].args[1][0]
        self.assertEqual(calls[-1].args[1], ("actual-quiz", inserted_id))
        self.assertTrue(all("IGNORE" not in call.args[0] for call in calls))
        self.assertEqual(counts, {"inserted": 1, "reused": 0, "mapped": 1, "persisted": 1})
        self.conn.begin.assert_called_once()
        self.conn.commit.assert_called_once()
        self.conn.rollback.assert_not_called()
        self.conn.close.assert_called_once()
        self.conn.autocommit.assert_not_called()
        self.assertLess(self.conn.mock_calls.index(unittest.mock.call.begin()),
                        self.conn.mock_calls.index(unittest.mock.call.cursor()))

    def test_existing_question_maps_actual_existing_id(self):
        self.cursor.fetchone.side_effect = [
            {"quiz_id": "actual-quiz"}, {"id": "existing-question"}, None]
        counts = self.writer.save([question(self.item)])
        self.assertEqual(counts["reused"], 1)
        self.assertEqual(counts["inserted"], 0)
        self.assertEqual(self.cursor.execute.call_args.args[1], ("actual-quiz", "existing-question"))

    def test_already_mapped_question_not_counted_as_new_mapping(self):
        self.cursor.fetchone.side_effect = [
            {"quiz_id": "actual-quiz"}, {"id": "existing"}, {"question_id": "existing"}]
        counts = self.writer.save([question(self.item)])
        self.assertEqual(counts["mapped"], 0)
        self.assertEqual(counts["persisted"], 1)

    def test_strict_insert_and_mapping_failures_rollback_entire_batch(self):
        for failing_statement in ("INSERT INTO question_bank", "INSERT INTO quiz_questions_map"):
            with self.subTest(failing_statement=failing_statement):
                self.conn.reset_mock()
                self.cursor.fetchone.side_effect = [{"quiz_id": "quiz"}, None, None]
                def execute(sql, params):
                    if sql.startswith(failing_statement):
                        raise RuntimeError("constraint/truncation error")
                self.cursor.execute.side_effect = execute
                with self.assertRaises(RuntimeError):
                    self.writer.save([question(self.item)])
                self.conn.rollback.assert_called_once()
                self.conn.commit.assert_not_called()
                self.conn.close.assert_called_once()

    def test_missing_quiz_row_never_maps_new_uuid(self):
        self.cursor.fetchone.return_value = None
        with self.assertRaisesRegex(RuntimeError, "resolve"):
            self.writer.save([question(self.item)])
        self.assertEqual(self.cursor.execute.call_count, 2)
        self.conn.rollback.assert_called_once()

    def test_preflight_is_read_only_and_releases_connection(self):
        self.cursor.fetchone.return_value = {"ENGINE": "InnoDB"}
        self.cursor.fetchall.side_effect = [
            [{"Non_unique": 0, "Key_name": "quiz", "Column_name": field}
             for field in ("username", "quiz_name")],
            [{"Non_unique": 0, "Key_name": "map", "Column_name": field}
             for field in ("quiz_id", "question_id")],
        ]
        self.writer.preflight()
        self.conn.begin.assert_not_called()
        self.conn.commit.assert_not_called()
        self.conn.close.assert_called_once()
        self.assertTrue(all(call.args[0].startswith(("SELECT", "SHOW"))
                            for call in self.cursor.execute.call_args_list))

    def test_preflight_rejects_nontransactional_tables_or_missing_unique_keys(self):
        self.cursor.fetchone.return_value = {"ENGINE": "MyISAM"}
        with self.assertRaisesRegex(RuntimeError, "InnoDB"):
            self.writer.preflight()
        self.conn.close.assert_called_once()
        self.conn.reset_mock()
        self.cursor.fetchone.return_value = {"ENGINE": "InnoDB"}
        self.cursor.fetchall.return_value = []
        with self.assertRaisesRegex(RuntimeError, "unique key"):
            self.writer.preflight()
        self.conn.close.assert_called_once()

    def test_writer_uses_shared_autocommit_pool_and_preserves_tls_port(self):
        modules = {name: MagicMock() for name in
                   ("pymysql", "dbutils", "dbutils.pooled_db", "dotenv")}
        pool_spec = importlib.util.spec_from_file_location("test_topic_db_pool", ROOT / "db_pool.py")
        pool_module = importlib.util.module_from_spec(pool_spec)
        with patch.dict(sys.modules, modules):
            pool_spec.loader.exec_module(pool_module)
        pool_factory = modules["dbutils.pooled_db"].PooledDB
        pool_factory.return_value.connection.return_value = self.conn
        env = {
            "MYSQL_HOST": "database.example.test", "MYSQL_USER": "test-user",
            "MYSQL_PASSWORD": "test-only", "MYSQL_DB": "test-db",
            "MYSQL_PORT": "25060", "MYSQL_SSL_CA": "/tmp/test-ca.pem",
        }
        self.cursor.fetchone.side_effect = [{"quiz_id": "quiz"}, None, None]
        with patch.dict(os.environ, env, clear=True):
            writer = cli.DatabaseWriter(pool_module.get_db_connection, "test-user", "test-quiz")
            writer.save([question(self.item)])
        kwargs = pool_factory.call_args.kwargs
        self.assertTrue(kwargs["autocommit"])
        self.assertEqual(kwargs["port"], 25060)
        self.assertEqual(kwargs["ssl"], {"ca": env["MYSQL_SSL_CA"]})
        self.conn.begin.assert_called_once()
        self.conn.commit.assert_called_once()
        self.conn.close.assert_called_once()


class RuntimeTests(unittest.TestCase):
    def test_env_loaded_before_engine_and_explicit_key_passed(self):
        engine, _ = engine_mock()
        dotenv = SimpleNamespace(load_dotenv=MagicMock())
        db = SimpleNamespace(get_db_connection=MagicMock())
        env = dict.fromkeys(("MYSQL_HOST", "MYSQL_USER", "MYSQL_PASSWORD", "MYSQL_DB"), "test-only")
        env["MY_API_KEY"] = "test-only"
        def imported(name):
            dotenv.load_dotenv.assert_called_once()
            return engine
        with patch.dict(os.environ, env, clear=True), \
                patch.dict(sys.modules, {"dotenv": dotenv, "db_pool": db}), \
                patch.object(cli.DatabaseWriter, "preflight") as preflight, \
                patch.object(cli.importlib, "import_module", side_effect=imported), \
                patch.object(cli, "generate", new=AsyncMock(return_value=0)) as generate, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["--organ-system", SYSTEM, "--topic", "Asthma",
                                       "--env-file", str(ROOT / "README.md"), "--model", "chosen"]), 0)
        preflight.assert_called_once()
        self.assertEqual(generate.call_args.args[-2:], ("test-only", "chosen"))

    def test_missing_credentials_never_import_engine_or_connect(self):
        with patch.dict(os.environ, {}, clear=True), \
                patch.dict(sys.modules, {"dotenv": SimpleNamespace(load_dotenv=MagicMock())}), \
                patch.object(cli.importlib, "import_module") as imported, \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(cli.main(["--all"]), 1)
        imported.assert_not_called()
        self.assertIn("Missing configuration", errors.getvalue())

    def test_preflight_failure_prevents_paid_generation_and_redacts_error(self):
        env = dict.fromkeys(("MYSQL_HOST", "MYSQL_USER", "MYSQL_PASSWORD", "MYSQL_DB",
                             "OPENAI_API_KEY"), "test-only")
        with patch.dict(os.environ, env, clear=True), \
                patch.dict(sys.modules, {
                    "dotenv": SimpleNamespace(load_dotenv=MagicMock()),
                    "db_pool": SimpleNamespace(get_db_connection=MagicMock()),
                }), patch.object(cli.DatabaseWriter, "preflight",
                                 side_effect=RuntimeError("sensitive-value")), \
                patch.object(cli.importlib, "import_module") as imported, \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(cli.main(["--all"]), 1)
        imported.assert_not_called()
        self.assertNotIn("sensitive-value", errors.getvalue())

    def test_keyboard_interrupt_returns_130_before_generation(self):
        env = dict.fromkeys(("MYSQL_HOST", "MYSQL_USER", "MYSQL_PASSWORD", "MYSQL_DB",
                             "OPENAI_API_KEY"), "test-only")
        with patch.dict(os.environ, env, clear=True), \
                patch.dict(sys.modules, {
                    "dotenv": SimpleNamespace(load_dotenv=MagicMock()),
                    "db_pool": SimpleNamespace(get_db_connection=MagicMock()),
                }), patch.object(cli.DatabaseWriter, "preflight", side_effect=KeyboardInterrupt), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["--all"]), 130)


if __name__ == "__main__":
    unittest.main()
