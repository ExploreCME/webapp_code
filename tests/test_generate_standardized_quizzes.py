import asyncio
from collections import Counter
from contextlib import redirect_stdout, redirect_stderr
from decimal import Decimal
import io
import json
from pathlib import Path
import random
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlparse

from pypdf import PdfReader

import generate_standardized_quizzes as generator
import export_standardized_quiz_pdf as exporter
import student_reports
from standardized_display import standardized_category_breakdown, standardized_label
from standardized_quizzes import STANDARDIZED_PREFIX, STANDARDIZED_QUIZZES
from standardizers import SOURCE_ORGAN_ALIASES, TASK_AREAS, storage_organ_system
import test_standardized_quizzes as quiz_tests


TOPICS = {
    "Cardiovascular System": ["Hypertension", "Heart failure", "Arrhythmia"],
    "Pulmonary System": ["Asthma", "Pneumonia"],
}
ORGANS = {"Cardiovascular System": 60, "Pulmonary System": 40}
TASKS = dict(zip(TASK_AREAS, (15, 15, 20, 10, 20, 15, 5)))


def question(item, stem):
    return {
        **item, "question_stem": stem,
        **{f"answer_choice_{letter}": f"Choice {letter}" for letter in "ABCDE"},
        "correct_answer": "B", "explanation": "Verified explanation",
        "difficulty_rating": 5,
    }


def make_versions():
    blueprints = generator.build_blueprints(TOPICS, ORGANS, TASKS, random.Random(42))
    return [[question(item, f"Question {version}-{index}")
             for index, item in enumerate(blueprint)]
            for version, blueprint in enumerate(blueprints)]


class GeneratorTests(unittest.TestCase):
    def test_source_alias_dry_run_preserves_each_selected_allocation_without_publication(self):
        topics = {source: [f"Specific disease {index}"]
                  for index, source in enumerate(SOURCE_ORGAN_ALIASES)}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "topics.json"
            path.write_text(json.dumps(topics))
            output = io.StringIO()
            with patch("builtins.input", side_effect=["4"] * 25 + list(map(str, TASKS.values()))), \
                    patch.object(generator.importlib, "import_module") as import_engine, \
                    patch.object(generator, "VersionPublisher") as publisher, redirect_stdout(output):
                self.assertEqual(generator.main([
                    "--quiz-name", "Source scopes", "--topics-file", str(path), "--dry-run",
                ]), 0)
            import_engine.assert_not_called()
            publisher.assert_not_called()
            for source in topics:
                self.assertEqual(output.getvalue().count(f"  {source}: "), 3)

    def test_custom_categories_load_prompt_and_allocate_without_remapping(self):
        topics = {
            "Cardiovascular System": ["Hypertension"],
            "Growth and Development": ["Developmental milestones", "Teething"],
            "Pediatric Renal Development": ["Kidney development"],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "topics.json"
            path.write_text(json.dumps(topics))
            self.assertEqual(generator.load_topics(path, allow_custom_categories=True), topics)
            with self.assertRaises(ValueError):
                generator.load_topics(path)
            for weights in ((0, 100, 0), (25, 50, 25)):
                with self.subTest(weights=weights):
                    answers = [str(value) for value in (*weights, *TASKS.values())]
                    with patch("builtins.input", side_effect=answers) as prompts, \
                            patch.object(generator.importlib, "import_module") as import_engine, \
                            patch.object(generator, "VersionPublisher") as publisher, \
                            redirect_stdout(io.StringIO()):
                        self.assertEqual(generator.main([
                            "--quiz-name", "Pediatrics", "--topics-file", str(path), "--dry-run",
                        ]), 0)
                    self.assertEqual(
                        [call.args[0] for call in prompts.call_args_list[:3]],
                        [f"{label} (%): " for label in topics],
                    )
                    import_engine.assert_not_called()
                    publisher.assert_not_called()
                    blueprints = generator.build_blueprints(
                        topics, dict(zip(topics, weights)), TASKS, random.Random(1),
                    )
                    self.assertEqual(len(blueprints), 3)
                    for blueprint in blueprints:
                        self.assertEqual(len(blueprint), 120)
                        self.assertEqual(Counter(item["organ_system"] for item in blueprint),
                                         {label: weight * 120 // 100
                                          for label, weight in zip(topics, weights) if weight})
                        self.assertEqual(Counter(item["task_area"] for item in blueprint),
                                         dict(zip(TASK_AREAS, (18, 18, 24, 12, 24, 18, 6))))

    def test_custom_loader_keeps_strict_json_and_label_validation(self):
        invalid = (
            '{}', '[]', '{"Growth": ["Teething"], "Growth": ["Feeding"]}',
            '{"Growth": ["Teething"], "growth": ["Feeding"]}',
            '{"": ["Teething"]}', '{" Growth": ["Teething"]}',
            json.dumps({"x" * 256: ["Teething"]}),
            '{"Growth": []}', '{"Growth": "Teething"}', '{"Growth": [null]}',
            '{"Growth": [""]}', '{"Growth": [" Teething"]}',
            json.dumps({"Growth": ["x" * 256]}),
            '{“Growth”: ["Teething"]}', '{"Growth": ["Teething"] "Renal": ["Kidney"]}',
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "topics.json"
            for content in invalid:
                with self.subTest(content=content), self.assertRaises(ValueError):
                    path.write_text(content)
                    generator.load_topics(path, allow_custom_categories=True)
            topics = {"x" * 255: ["Teething", "Teething"]}
            path.write_text(json.dumps(topics))
            self.assertEqual(generator.load_topics(path, allow_custom_categories=True),
                             {"x" * 255: ["Teething"]})

    def test_exact_percentage_marginals_and_random_topic_pairings(self):
        versions = generator.build_blueprints(TOPICS, ORGANS, TASKS, random.Random(19))
        for blueprint in versions:
            self.assertEqual(len(blueprint), 120)
            self.assertEqual(Counter(item["organ_system"] for item in blueprint), {
                "Cardiovascular System": 72, "Pulmonary System": 48,
            })
            self.assertEqual(Counter(item["task_area"] for item in blueprint), {
                label: count for label, count in zip(TASK_AREAS, (18, 18, 24, 12, 24, 18, 6))
            })
            for item in blueprint:
                self.assertIn(item["topic_area"], TOPICS[item["organ_system"]])
            self.assertGreater(len({item["topic_area"] for item in blueprint}), 2)
        self.assertNotEqual(versions[0], versions[1])
        self.assertEqual(versions, generator.build_blueprints(TOPICS, ORGANS, TASKS, random.Random(19)))

    def test_rounding_and_zero_percentages(self):
        self.assertEqual(generator.allocate({
            "a": Decimal("33.33"), "b": Decimal("33.33"), "c": Decimal("33.34"),
        }), {"a": 40, "b": 40, "c": 40})
        tasks = dict.fromkeys(TASK_AREAS, 0)
        tasks[TASK_AREAS[0]] = 100
        versions = generator.build_blueprints(
            TOPICS, {"Cardiovascular System": 100, "Pulmonary System": 0}, tasks, random.Random(1),
        )
        self.assertTrue(all(item["organ_system"] == "Cardiovascular System"
                            and item["task_area"] == TASK_AREAS[0]
                            for blueprint in versions for item in blueprint))

    def test_invalid_percentages_names_and_json_are_rejected(self):
        for values in ({"a": -1, "b": 101}, {"a": "NaN", "b": 100},
                       {"a": "Infinity", "b": 0}, {"a": "oops", "b": 100},
                       {"a": 50, "b": 49}, {"a": 100}, {"a": 0, "b": 0}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                generator.validate_percentages(["a", "b"], values)
        for name in ("", " " * 5, "a" * 250, "Standardized: Emergency Medicine"):
            with self.assertRaises(ValueError):
                generator.validate_name(name)
        self.assertEqual(generator.validate_name(" emergency medicine "), "Emergency Medicine")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "topics.json"
            for content in ('[]', '{"Cardiology": ["HTN"]}', '{"Pulmonary System": []}',
                            '{"Pulmonary System": ["Asthma"], "Pulmonary System": ["Pneumonia"]}'):
                path.write_text(content)
                with self.assertRaises(ValueError):
                    generator.load_topics(path)

    def test_prompt_retries_invalid_totals(self):
        with patch("builtins.input", side_effect=["10", "10", "25.5", "74.5"]), redirect_stdout(io.StringIO()):
            result = generator.prompt_percentages(["a", "b"], "Percentages")
        self.assertEqual(result, {"a": Decimal("25.5"), "b": Decimal("74.5")})

    def test_cli_prompts_and_dry_run_do_not_touch_api_or_database(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "topics.json"
            path.write_text(json.dumps(TOPICS))
            answers = ["emergency medicine", str(path), "60", "40", *map(str, TASKS.values())]
            output = io.StringIO()
            with patch("builtins.input", side_effect=answers), \
                    patch.object(generator.importlib, "import_module") as engine_import, \
                    redirect_stdout(output):
                self.assertEqual(generator.main(["--dry-run", "--seed", "3"]), 0)
            engine_import.assert_not_called()
            self.assertIn("Dry run: no API calls", output.getvalue())
            for version in range(1, 4):
                self.assertIn(f"Emergency Medicine version {version}: 120 questions", output.getvalue())

    def test_shortfall_exit_never_publishes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "topics.json"
            path.write_text(json.dumps(TOPICS))
            output = io.StringIO()
            with patch("builtins.input", side_effect=["60", "40", *map(str, TASKS.values()), "y"]), \
                    patch.dict("os.environ", {
                        "OPENAI_API_KEY": "test-only", "MYSQL_HOST": "test", "MYSQL_USER": "test",
                        "MYSQL_PASSWORD": "test-only", "MYSQL_DB": "test",
                    }), \
                    patch.object(generator, "VersionPublisher") as publisher, \
                    patch.object(generator.importlib, "import_module"), \
                    patch.object(generator, "generate_versions", new=AsyncMock(
                        side_effect=ValueError("Generation shortfall"))), \
                    redirect_stdout(output), redirect_stderr(output):
                self.assertEqual(generator.main([
                    "--quiz-name", "Emergency Medicine", "--topics-file", str(path),
                ]), 1)
            publisher.return_value.publish.assert_not_called()

    def test_preflight_detects_existing_versions_and_closes_connection(self):
        conn = MagicMock()
        cursor = conn.cursor.return_value.__enter__.return_value
        cursor.fetchone.side_effect = [
            {"ENGINE": "InnoDB"}, {"ENGINE": "InnoDB"},
            {"quiz_name": "Emergency Medicine version 1"},
        ]
        publisher = generator.VersionPublisher(lambda: conn, "Emergency Medicine")
        with self.assertRaisesRegex(ValueError, "already exist"):
            publisher.preflight()
        conn.close.assert_called_once()

    def test_preflight_requires_source_column_before_generation(self):
        conn = MagicMock()
        cursor = conn.cursor.return_value.__enter__.return_value
        cursor.execute.side_effect = RuntimeError("Unknown column source_organ_system")
        with self.assertRaisesRegex(RuntimeError, "source_organ_system"):
            generator.VersionPublisher(lambda: conn, "New quiz").preflight()
        self.assertIn("source_organ_system", cursor.execute.call_args.args[0])
        conn.close.assert_called_once()

    def test_display_fallback_does_not_invent_or_normalize_provenance(self):
        for source in (None, "", " \t\n"):
            self.assertEqual(standardized_label({
                "organ_system": "Reproductive System", "source_organ_system": source,
            }), "Reproductive System")
        self.assertEqual(standardized_label({"organ_system": "Legacy"}), "Legacy")
        for source in ("OB - Prenatal care/normal progancy", "Case & punctuation–", " Raw "):
            self.assertEqual(standardized_label({
                "organ_system": "Canonical", "source_organ_system": source,
            }), source)


class GenerationTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_source_aliases_keep_scope_until_validation_then_publish_canonical_organs(self):
        from test_question_verification import engine as shared_engine

        self.assertEqual(len(SOURCE_ORGAN_ALIASES), 25)
        topics = {source: [f"Specific disease {index}"]
                  for index, source in enumerate(SOURCE_ORGAN_ALIASES)}
        tasks = dict.fromkeys(TASK_AREAS, 0)
        tasks["Clinical Intervention"] = 100
        blueprints = generator.build_blueprints(
            topics, dict.fromkeys(topics, 4), tasks, random.Random(7),
        )
        expected_counts = generator.allocate(dict.fromkeys(topics, Decimal(4)))
        calls = Counter()
        sequence = 0

        async def responder(item_dict, num_needed, allowed_organ_systems, **kwargs):
            nonlocal sequence
            source = item_dict["organ_system"]
            self.assertIn(source, topics)
            self.assertEqual(item_dict["topic_area"], topics[source][0])
            self.assertEqual(set(allowed_organ_systems), set(topics))
            variant = source.swapcase().replace(" - ", "–").replace(" – ", "-")
            self.assertEqual(shared_engine.standardize_categories(
                variant, item_dict["task_area"], allowed_organ_systems=allowed_organ_systems,
            ), (source, item_dict["task_area"]))
            calls[source] += num_needed
            results = []
            for _ in range(num_needed):
                sequence += 1
                result = question(item_dict, f"Alias vignette {sequence}")
                result["organ_system"] = source.swapcase()
                results.append(result)
            return results

        engine = self.engine(responder)
        engine.standardize_categories = shared_engine.standardize_categories
        with patch.object(generator, "validate_question", wraps=generator.validate_question) as validate:
            versions = await generator.generate_versions(
                blueprints, engine, "test-only", "model", None, 3, 1,
            )
        self.assertEqual(sequence, 360)
        self.assertEqual(validate.call_count, 360)
        for call in validate.call_args_list:
            self.assertIn(call.args[1]["organ_system"], topics)
            self.assertEqual(set(call.kwargs["allowed_organ_systems"]), set(topics))
        self.assertEqual(calls, {source: count * 3 for source, count in expected_counts.items()})
        for blueprint, version in zip(blueprints, versions):
            self.assertEqual(Counter(item["organ_system"] for item in blueprint), expected_counts)
            for item, result in zip(blueprint, version):
                self.assertEqual(result["organ_system"], SOURCE_ORGAN_ALIASES[item["organ_system"]])
                self.assertEqual(result["source_organ_system"], item["organ_system"])
                self.assertEqual(result["topic_area"], item["topic_area"])
                self.assertEqual(result["task_area"], item["task_area"])
        db = quiz_tests.QuizDatabase()
        try:
            generator.VersionPublisher(lambda: db, "Source aliases").publish(versions)
            rows = db.connection.execute(
                "SELECT organ_system, topic_area, COUNT(*) AS n FROM question_bank "
                "GROUP BY organ_system, topic_area"
            ).fetchall()
            self.assertEqual(
                {(row["organ_system"], row["topic_area"]): row["n"] for row in rows},
                {(target, topics[source][0]): expected_counts[source] * 3
                 for source, target in SOURCE_ORGAN_ALIASES.items()},
            )
            self.assertEqual(db.connection.execute(
                "SELECT COUNT(*) AS n FROM standardized_quiz_versions"
            ).fetchone()["n"], 3)
        finally:
            db.connection.close()

    async def test_shared_storage_target_does_not_merge_source_allocations_or_custom_categories(self):
        sources = list(SOURCE_ORGAN_ALIASES)[1:3]
        topics = {source: [f"Disease in scope {index}"] for index, source in enumerate((
            *sources, "Psychiatry/Behavioral Science", "Growth and Development",
            "Pediatric Renal Development",
        ))}
        percentages = dict(zip(topics, (10, 20, 30, 25, 15)))
        blueprints = generator.build_blueprints(topics, percentages, TASKS, random.Random(9))
        sequence = 0

        async def responder(item_dict, num_needed, **kwargs):
            nonlocal sequence
            self.assertEqual(item_dict["topic_area"], topics[item_dict["organ_system"]][0])
            results = []
            for _ in range(num_needed):
                sequence += 1
                results.append(question(item_dict, f"Separate scope {sequence}"))
            return results

        versions = await generator.generate_versions(
            blueprints, self.engine(responder), "test-only", "model", None, 2, 1,
        )
        for blueprint, version in zip(blueprints, versions):
            self.assertEqual(Counter(item["organ_system"] for item in blueprint),
                             dict(zip(topics, (12, 24, 36, 30, 18))))
            self.assertEqual(Counter(result["organ_system"] for result in version), {
                "Psychiatry/Behavioral Science": 72,
                "Growth and Development": 30, "Pediatric Renal Development": 18,
            })
            self.assertEqual(Counter(result["topic_area"] for result in version),
                             {topics[source][0]: count for source, count
                              in zip(topics, (12, 24, 36, 30, 18))})
            self.assertEqual(Counter(result["task_area"] for result in version),
                             Counter(item["task_area"] for item in blueprint))

    async def test_wrong_source_scope_task_or_topic_is_rejected_before_storage_mapping(self):
        sources = list(SOURCE_ORGAN_ALIASES)[1:3]
        topics = {source: ["Shared disease"] for source in (*sources, "Psychiatry/Behavioral Science")}
        tasks = dict.fromkeys(TASK_AREAS, 0)
        tasks["Clinical Intervention"] = 100
        blueprints = generator.build_blueprints(
            topics, dict(zip(topics, (40, 40, 20))), tasks, random.Random(1),
        )
        for blueprint in blueprints:
            blueprint.sort(key=lambda item: list(topics).index(item["organ_system"]))
        for kind in ("broad-organ", "sibling-source", "unknown-organ", "wrong-task", "wrong-topic"):
            with self.subTest(kind=kind):
                calls = 0

                async def responder(item_dict, num_needed, **kwargs):
                    nonlocal calls
                    calls += 1
                    result = question(item_dict, f"Rejected {calls}")
                    if kind == "broad-organ":
                        result["organ_system"] = "Psychiatry/Behavioral Science"
                    elif kind == "sibling-source":
                        result["organ_system"] = next(source for source in sources
                                                      if source != item_dict["organ_system"])
                    elif kind == "unknown-organ":
                        result["organ_system"] = "Unselected category"
                    elif kind == "wrong-task":
                        result["task_area"] = TASK_AREAS[0]
                    else:
                        result["topic_area"] = "Unrequested disease"
                    return [result] * num_needed

                with patch.object(generator, "storage_organ_system") as storage, \
                        redirect_stdout(io.StringIO()), self.assertRaisesRegex(ValueError, "shortfall"):
                    await generator.generate_versions(
                        blueprints, self.engine(responder), "test-only", "model", None, 1, 2,
                    )
                self.assertEqual(calls, 2)
                storage.assert_not_called()

    async def test_real_engine_preserves_custom_labels_through_publication(self):
        from test_question_verification import engine

        for organ in ("Growth and Development", "Pediatric Renal Development"):
            with self.subTest(organ=organ):
                topics = {organ: ["Developmental milestones"]}
                tasks = dict.fromkeys(TASK_AREAS, 0)
                tasks["Clinical Intervention"] = 100
                blueprints = generator.build_blueprints(
                    topics, {organ: 100}, tasks, random.Random(1),
                )
                item = blueprints[0][0]
                sequence = 0
                verified_prompts = []
                generation_prompts = []

                async def api_response(**kwargs):
                    nonlocal sequence
                    prompt = kwargs["messages"][-1]["content"]
                    if "Create EXACTLY" in prompt:
                        generation_prompts.append(prompt)
                        self.assertIn(f'**Allowed Organ Systems**: ["{organ}"]', prompt)
                        count = int(prompt.split("Create EXACTLY ")[1].split()[0])
                        results = []
                        for _ in range(count):
                            sequence += 1
                            result = question(item, f"Distinct vignette {sequence}")
                            result["organ_system"] = organ.lower()
                            result["task_area"] = item["task_area"].lower()
                            results.append(result)
                        payload = {"questions": results}
                    elif "rate the difficulty" in prompt:
                        payload = {"difficulty": 5}
                    else:
                        verified_prompts.append(prompt)
                        payload = {"correct_answer": "B", "confidence_score": 9}
                    return SimpleNamespace(choices=[
                        SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload))),
                    ])

                clients = []
                def api_client(**kwargs):
                    client = SimpleNamespace(
                        chat=SimpleNamespace(completions=SimpleNamespace(
                            create=AsyncMock(side_effect=api_response))),
                        close=AsyncMock(),
                    )
                    clients.append(client)
                    return client

                with patch.object(engine, "AsyncOpenAI", side_effect=api_client), \
                        redirect_stdout(io.StringIO()):
                    versions = await generator.generate_versions(
                        blueprints, engine, "test-only", "model", None, 2, 1,
                    )
                self.assertEqual([len(version) for version in versions], [120] * 3)
                self.assertEqual(len(verified_prompts), 360 * engine.VERIFICATION_PASSES)
                self.assertTrue(generation_prompts)
                for version in versions:
                    for result in version:
                        self.assertEqual({key: result[key] for key in item}, item)
                        self.assertEqual(result["difficulty_rating"], 5)
                for client in clients:
                    client.close.assert_awaited_once()
                db = quiz_tests.QuizDatabase()
                try:
                    generator.VersionPublisher(lambda: db, "Pediatrics").publish(versions)
                    rows = db.connection.execute(
                        "SELECT organ_system, COUNT(*) AS n FROM question_bank GROUP BY organ_system"
                    ).fetchall()
                    self.assertEqual(rows, [{"organ_system": organ, "n": 360}])
                    self.assertEqual(db.connection.execute(
                        "SELECT COUNT(*) AS n FROM standardized_quiz_versions"
                    ).fetchone()["n"], 3)
                finally:
                    db.connection.close()

    def engine(self, responder):
        managers = []
        def manager(**kwargs):
            instance = SimpleNamespace(client=SimpleNamespace(close=AsyncMock()))
            managers.append(instance)
            return instance
        return SimpleNamespace(
            ConversationManager=manager, process_topic_batch=responder,
            standardize_categories=lambda organ, task, **kwargs: (organ, task), managers=managers,
        )

    async def test_shared_engine_validation_unique_stems_and_refills(self):
        sequence = 0
        active, max_active = 0, 0
        calls = Counter()
        async def responder(item_dict, num_needed, **kwargs):
            nonlocal sequence, active, max_active
            self.assertLessEqual(num_needed, generator.BATCH_SIZE)
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0)
            active -= 1
            key = tuple(item_dict.values())
            calls[key] += 1
            if calls[key] == 1:
                return []  # Refill the exact same slots; never relax categories.
            results = []
            for _ in range(num_needed):
                sequence += 1
                results.append(question(item_dict, f"Verified stem {sequence}"))
            return results
        engine = self.engine(responder)
        blueprints = generator.build_blueprints(TOPICS, ORGANS, TASKS, random.Random(3))
        versions = await generator.generate_versions(blueprints, engine, "test-only", "model", None, 3, 2)
        self.assertEqual([len(version) for version in versions], [120, 120, 120])
        self.assertLessEqual(max_active, 3)
        self.assertEqual(len({q["question_stem"] for version in versions for q in version}), 360)
        for blueprint, version in zip(blueprints, versions):
            for item, result in zip(blueprint, version):
                self.assertEqual({key: result[key] for key in item}, item)
        for manager in engine.managers:
            manager.client.close.assert_awaited_once()

    async def test_single_topic_supports_all_360_slots_in_small_batches(self):
        sequence = 0
        async def responder(item_dict, num_needed, **kwargs):
            nonlocal sequence
            self.assertLessEqual(num_needed, 5)
            result = []
            for _ in range(num_needed):
                sequence += 1
                result.append(question(item_dict, f"Unique question {sequence}"))
            return result
        engine = self.engine(responder)
        tasks = dict.fromkeys(TASK_AREAS, 0)
        tasks[TASK_AREAS[0]] = 100
        blueprints = generator.build_blueprints(
            {"Cardiovascular System": ["HTN"]}, {"Cardiovascular System": 100}, tasks, random.Random(1),
        )
        versions = await generator.generate_versions(blueprints, engine, "test-only", "model", None, 5, 2)
        self.assertEqual([len(version) for version in versions], [120, 120, 120])
        self.assertEqual(sequence, 360)

    async def test_duplicate_or_misclassified_output_causes_bounded_shortfall(self):
        for kind in ("duplicate", "wrong-task"):
            calls = 0
            async def responder(item_dict, num_needed, **kwargs):
                nonlocal calls
                calls += 1
                result = question(item_dict, "Repeated stem")
                if kind == "wrong-task":
                    result["task_area"] = "Invalid task"
                return [result] * num_needed
            engine = self.engine(responder)
            tasks = dict.fromkeys(TASK_AREAS, 0)
            tasks[TASK_AREAS[0]] = 100
            blueprints = generator.build_blueprints(
                {"Cardiovascular System": ["HTN"]}, {"Cardiovascular System": 100}, tasks, random.Random(1),
            )
            with redirect_stdout(io.StringIO()), self.assertRaisesRegex(ValueError, "shortfall"):
                await generator.generate_versions(blueprints, engine, "test-only", "model", None, 2, 2)
            self.assertEqual(calls, 2)
            for manager in engine.managers:
                manager.client.close.assert_awaited_once()


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.db = quiz_tests.QuizDatabase()
        self.addCleanup(self.db.connection.close)
        self.publisher = generator.VersionPublisher(lambda: self.db, "Emergency Medicine")

    def test_publishes_three_complete_ordered_versions_without_user_quizzes(self):
        versions = make_versions()
        self.publisher.publish(versions)
        rows = self.db.connection.execute(
            "SELECT * FROM standardized_quiz_versions ORDER BY quiz_name"
        ).fetchall()
        self.assertEqual([row["quiz_name"] for row in rows], [
            "Emergency Medicine version 1", "Emergency Medicine version 2", "Emergency Medicine version 3",
        ])
        ids = [question_id for row in rows for question_id in json.loads(row["question_ids"])]
        self.assertEqual(len(set(ids)), 360)
        self.assertTrue(all(question_id.startswith("std-") for question_id in ids))
        self.assertEqual(self.db.connection.execute("SELECT COUNT(*) AS n FROM question_bank").fetchone()["n"], 360)
        self.assertEqual(self.db.connection.execute("SELECT COUNT(*) AS n FROM user_quizzes").fetchone()["n"], 0)
        with self.assertRaises(sqlite3.IntegrityError):
            self.publisher.publish(make_versions())
        self.assertEqual(self.db.connection.execute("SELECT COUNT(*) AS n FROM question_bank").fetchone()["n"], 360)

    def test_publisher_defensively_maps_known_aliases_without_mutating_source_questions(self):
        labels = [*SOURCE_ORGAN_ALIASES, "Growth and Development", "Pediatric Renal Development"]
        versions = make_versions()
        expected = Counter()
        for index, result in enumerate(result for version in versions for result in version):
            source = labels[index % len(labels)]
            result["organ_system"] = source
            expected[storage_organ_system(source)] += 1
        self.publisher.publish(versions)
        rows = self.db.connection.execute(
            "SELECT organ_system, COUNT(*) AS n FROM question_bank GROUP BY organ_system"
        ).fetchall()
        self.assertEqual({row["organ_system"]: row["n"] for row in rows}, expected)
        self.assertEqual(
            [result["organ_system"] for version in versions for result in version],
            [labels[index % len(labels)] for index in range(360)],
        )
        self.assertEqual(self.db.connection.execute(
            "SELECT COUNT(*) AS n FROM question_bank WHERE source_organ_system IS NULL"
        ).fetchone()["n"], 360)

    def test_incomplete_or_duplicate_sets_never_open_publication_transaction(self):
        for versions in (make_versions()[:2], [[], [], []]):
            with self.assertRaises(ValueError):
                self.publisher.publish(versions)
        versions = make_versions()
        versions[1][0]["question_stem"] = "  " + versions[0][0]["question_stem"].replace(" ", "  ").upper() + "  "
        with self.assertRaises(ValueError):
            self.publisher.publish(versions)
        self.assertEqual(self.db.connection.execute(
            "SELECT COUNT(*) AS n FROM standardized_quiz_versions"
        ).fetchone()["n"], 0)

    def test_partial_database_failure_rolls_back_all_versions_and_questions(self):
        original = self.db.execute
        def execute(query, parameters=()):
            if "INSERT INTO standardized_quiz_versions" in query and parameters[0].endswith("version 2"):
                raise RuntimeError("database unavailable")
            return original(query, parameters)
        with patch.object(self.db, "execute", side_effect=execute):
            with self.assertRaises(RuntimeError):
                versions = make_versions()
                for version in versions:
                    for result in version:
                        result["source_organ_system"] = "Exact source heading"
                self.publisher.publish(versions)
        for table in ("question_bank", "standardized_quiz_versions"):
            self.assertEqual(self.db.connection.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"], 0)

    def test_begin_failure_is_preserved_and_connection_is_closed(self):
        conn = MagicMock()
        conn.begin.side_effect = RuntimeError("cannot start transaction")
        publisher = generator.VersionPublisher(lambda: conn, "Emergency Medicine")
        with self.assertRaisesRegex(RuntimeError, "cannot start transaction"):
            publisher.publish(make_versions())
        conn.rollback.assert_not_called()
        conn.close.assert_called_once()


class PublishedVersionFlowTests(unittest.TestCase):
    setUp = quiz_tests.StandardizedQuizTests.setUp
    login = quiz_tests.StandardizedQuizTests.login
    token = quiz_tests.StandardizedQuizTests.token
    start = quiz_tests.StandardizedQuizTests.start
    answer = quiz_tests.StandardizedQuizTests.answer

    def test_source_headings_from_generation_through_results_and_print_reports(self):
        topics = {label: ["Specific topic"] for label in (
            "OB - Prenatal care/normal progancy",
            "OB - Prenatal care/normal pregnancy",
            "OB - Pregnancy complications",
            'Custom <script>alert("x")</script> & heading',
        )}
        tasks = dict.fromkeys(TASK_AREAS, 0)
        tasks[TASK_AREAS[0]] = 100
        blueprints = generator.build_blueprints(
            topics, dict.fromkeys(topics, 25), tasks, random.Random(1),
        )
        sequence = 0

        async def responder(item_dict, num_needed, **kwargs):
            nonlocal sequence
            batch = []
            for _ in range(num_needed):
                sequence += 1
                result = question(item_dict, f"Source question {sequence}")
                result["organ_system"] = item_dict["organ_system"].swapcase()
                result["source_organ_system"] = "Untrusted model provenance"
                batch.append(result)
            return batch

        from test_question_verification import engine as shared_engine
        engine = GenerationTests().engine(responder)
        engine.standardize_categories = shared_engine.standardize_categories
        versions = asyncio.run(generator.generate_versions(
            blueprints, engine, "test-only", "model", None, 2, 1,
        ))
        generator.VersionPublisher(lambda: self.db, "Source labels").publish(versions)
        self.quiz_name = STANDARDIZED_PREFIX + "Source labels version 1"
        rows = self.db.connection.execute(
            "SELECT id, organ_system, source_organ_system FROM question_bank"
        ).fetchall()
        self.assertEqual(len(rows), 360)
        for row in rows:
            self.assertIn(row["source_organ_system"], topics)
            self.assertEqual(row["organ_system"], storage_organ_system(row["source_organ_system"]))
        reproductive = [row for row in rows if row["organ_system"] == "Reproductive System"]
        self.assertEqual({row["source_organ_system"] for row in reproductive}, {
            "OB - Prenatal care/normal pregnancy", "OB - Pregnancy complications",
        })
        response = self.start()
        attempt_id = parse_qs(urlparse(response.location).query)["attempt_id"][0]
        meta, questions = exporter.load_version(self.db, "Source labels", 1)
        for q in questions:
            correct = q["source_organ_system"] != "OB - Pregnancy complications"
            response = self.answer(attempt_id, choice="B" if correct else "A", question=q["id"])
            self.assertEqual(response.status_code, 302)

        breakdown = standardized_category_breakdown(self.db, attempt_id)
        self.assertEqual({row["label"]: (row["correct"], row["answered"]) for row in breakdown},
                         {label: (0 if label == "OB - Pregnancy complications" else 30, 30)
                          for label in topics})
        html = self.client.get(response.location).get_data(as_text=True)
        review = self.client.get("/review_attempt", query_string={"attempt_id": attempt_id}).text
        arguments = {"report_type": "standardized", "category": "Source labels"}
        preview = self.client.get("/student_reports/preview", query_string=arguments).text
        from markupsafe import escape
        for label in topics:
            for page in (html, review, preview):
                self.assertIn(str(escape(label)), page)
                self.assertNotIn('<script>alert("x")</script>', page)
        sections = student_reports.standardized_report(self.db, "student", "Source labels", "recent")
        self.assertEqual(sections[0]["student"], student_reports.metric(90, 120))
        organ_rows = sections[0]["groups"][0]["rows"]
        self.assertEqual(len(organ_rows), 4)
        for row in organ_rows:
            expected = student_reports.metric(
                0 if row["label"] == "OB - Pregnancy complications" else 30, 30,
            )
            self.assertEqual(row["student"], expected)
            self.assertEqual(row["platform"], expected)
        pdf = self.client.get("/student_reports/download", query_string=arguments)
        self.assertEqual(pdf.status_code, 200)
        text = "\n".join(page.extract_text() for page in PdfReader(io.BytesIO(pdf.data)).pages)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            exporter.build_pdf(path / "key.pdf", meta, questions)
            key_text = "\n".join(page.extract_text()
                                 for page in PdfReader(path / "key.pdf").pages)
            exporter.build_txt(path / "key.txt", meta, questions)
            for label in topics:
                self.assertIn(label, " ".join(text.split()))
                self.assertIn(label, " ".join(key_text.split()))
                self.assertIn(label, (path / "key.txt").read_text())
        self.assertNotIn("Untrusted model provenance", html + review + preview + text)

    def test_legacy_fallback_and_nonstandardized_isolation(self):
        versions = make_versions()
        for index, q in enumerate(versions[0]):
            q["source_organ_system"] = (None, "", " \t", "Exact Source")[index % 4]
        generator.VersionPublisher(lambda: self.db, "Emergency Medicine").publish(versions)
        self.quiz_name = STANDARDIZED_PREFIX + "Emergency Medicine version 1"
        response = self.start()
        attempt_id = parse_qs(urlparse(response.location).query)["attempt_id"][0]
        meta, questions = exporter.load_version(self.db, "Emergency Medicine", 1)
        for q in questions[:4]:
            self.answer(attempt_id, choice="B", question=q["id"])
        breakdown = standardized_category_breakdown(self.db, attempt_id)
        self.assertEqual(sum(row["answered"] for row in breakdown), 4)
        self.assertEqual({row["label"] for row in breakdown},
                         {q["organ_system"] for q in questions[:3]} | {"Exact Source"})
        self.assertIn("Exact Source", self.client.get("/review_attempt", query_string={
            "attempt_id": attempt_id,
        }).text)
        self.db.execute("UPDATE quiz_attempts SET quiz_name = %s, quiz_name_norm = %s",
                        ("Ordinary quiz", "ordinary quiz"))
        html = self.client.get("/quiz_results", query_string={
            "attempt_id": attempt_id, "quiz_name": "Ordinary quiz",
        }).text
        review = self.client.get("/review_attempt", query_string={"attempt_id": attempt_id}).text
        self.assertNotIn("Organ System Sub-Scores", html)
        self.assertNotIn("Exact Source", html + review)
        spoofed = self.client.get("/quiz_results", query_string={
            "attempt_id": attempt_id, "quiz_name": self.quiz_name,
        }).text
        self.assertNotIn("Organ System Sub-Scores", spoofed)
        self.assertNotIn("Exact Source", spoofed)

    def test_published_versions_are_immediately_visible_and_playable_for_all_accounts(self):
        # Reuse the existing SQLite-backed flow fixture and real shared templates.
        generator.VersionPublisher(lambda: self.db, "Emergency Medicine").publish(make_versions())
        self.db.connection.execute("UPDATE users SET has_paid = 0")
        self.db.connection.execute("INSERT INTO users VALUES ('faculty', 0, 'Y', 'NONE')")
        self.db.connection.commit()
        self.quiz_name = STANDARDIZED_PREFIX + "Emergency Medicine version 1"
        for username in ("student", "faculty"):
            with self.subTest(username=username):
                self.login(username)
                category = self.client.get("/standardized_quizzes/Emergency%20Medicine")
                self.assertEqual(category.status_code, 200)
                html = category.get_data(as_text=True)
                self.assertEqual(html.count('class="quiz-card"'), 3)
                for version in range(1, 4):
                    self.assertIn(f"Emergency Medicine version {version}", html)
                response = self.start()
                attempt_id = parse_qs(urlparse(response.location).query)["attempt_id"][0]
                page = self.client.get(response.location).get_data(as_text=True)
                self.assertIn("Question <strong>1</strong> of <strong>120</strong>", page)
                row = self.db.connection.execute(
                    "SELECT question_ids FROM standardized_quiz_versions WHERE quiz_name = ?",
                    ("Emergency Medicine version 1",),
                ).fetchone()
                ids = json.loads(row["question_ids"])
                self.assertIn(f'value="{ids[0]}"', page)
                response = self.answer(attempt_id, choice="B", question=ids[0])
                self.assertIn("/quiz_results", response.location)
                self.assertIn("100.0%", self.client.get(response.location).get_data(as_text=True))
                review = self.client.get("/review_attempt", query_string={"attempt_id": attempt_id})
                self.assertIn("Verified explanation", review.get_data(as_text=True))
        self.login("student")
        self.assertEqual(self.client.get("/review_attempt", query_string={"attempt_id": attempt_id}).status_code, 404)

    def test_unpublished_and_guest_category_access(self):
        self.assertEqual(self.client.get("/standardized_quizzes/Unknown").status_code, 404)
        self.assertEqual(self.client.get("/quiz_mode", query_string={
            "quiz_name": STANDARDIZED_PREFIX + "Emergency Medicine version 1",
        }).status_code, 404)
        self.login("guest_test")
        self.assertEqual(self.client.get("/standardized_quizzes/Emergency%20Medicine").status_code, 302)


if __name__ == "__main__":
    unittest.main()
