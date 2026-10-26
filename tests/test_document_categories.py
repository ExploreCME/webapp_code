import asyncio
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from standardizers import (
    SOURCE_ORGAN_ALIASES, source_organ_system, standardize_organ_system,
    storage_organ_system,
)


ROOT = Path(__file__).resolve().parents[1]
TASK = "Formulating the Most Likely Diagnosis"


def load(name):
    with patch.dict(sys.modules, {key: MagicMock() for key in ("openai", "db_pool", "dotenv")}):
        spec = importlib.util.spec_from_file_location(name, ROOT / (name + ".py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


def connection():
    conn = MagicMock()
    cursor = AsyncMock()
    cursor.fetchall.return_value = []
    cursor.rowcount = 1000
    conn.cursor.return_value.__aenter__.return_value = cursor
    conn.commit = AsyncMock()
    conn.rollback = AsyncMock()
    return conn, cursor


def question(item):
    return {
        **item, "question_stem": "A unique clinical vignette",
        **{f"answer_choice_{letter}": f"Choice {letter}" for letter in "ABCDE"},
        "correct_answer": "A", "explanation": "Because", "difficulty_rating": 5,
    }


class DocumentCategoryTests(unittest.TestCase):
    def test_all_explicit_mappings_and_typography(self):
        expected_groups = {
            "Neurologic System": ("Neurology/Developmental", "Neurologic/Neurosurgery"),
            "Psychiatry/Behavioral Science": (
                "Anxiety disorders; Trauma- and stress-related disorders",
                "Depressive disorders; Bipolar and related disorders",
                "Substance-related disorders",
                "Schizophrenia spectrum and other psychotic disorders",
                "Disruptive, impulse-control, and conduct disorders; Neurodevelopmental disorders",
                "Feeding or eating disorders",
                "Personality disorders; Obsessive-compulsive and related disorders",
                "Somatic symptom and related disorders; Nonadherence to medical treatment",
                "Paraphilic disorders; Sexual dysfunctions",
            ),
            "Reproductive System": (
                "OB - Prenatal care/normal pregnancy", "GYN – Menstruation",
                "OB - Pregnancy complications", "GYN – Infections", "GYN – Neoplasms",
                "GYN - Disorders of the breast", "OB - Labor and delivery complications",
                "OB - Postpartum care", "GYN – Other", "GYN - Structural abnormalities",
                "Breast Surgery",
            ),
            "Pulmonary System": ("Pulmonary/Thoracic Surgery",),
            "Misc": ("Trauma/Acute Care", "Pain Medicine/Anesthesia"),
        }
        self.assertEqual(SOURCE_ORGAN_ALIASES, {
            source: canonical for canonical, sources in expected_groups.items() for source in sources
        })
        for source, canonical in SOURCE_ORGAN_ALIASES.items():
            variants = (source, f"  {source.upper()}  ", source.replace("–", "-"),
                        source.replace("-", " – "), source.replace(" ", "  "))
            for variant in variants:
                with self.subTest(source=source, variant=variant):
                    self.assertEqual(standardize_organ_system(variant), canonical)
                    self.assertEqual(source_organ_system(variant), source)
                    self.assertEqual(storage_organ_system(variant), canonical)
        for source, canonical in (
            ("GYN – Infections", "Reproductive System"),
            ("Disruptive, impulse-control, and conduct disorders; Neurodevelopmental disorders",
             "Psychiatry/Behavioral Science"),
            ("Somatic symptom and related disorders; Nonadherence to medical treatment",
             "Psychiatry/Behavioral Science"),
            ("Breast Surgery", "Reproductive System"), ("Pain Medicine/Anesthesia", "Misc"),
        ):
            self.assertEqual(standardize_organ_system(source), canonical)
        for label in ("Breast", "Pain", "Trauma", "Growth and Development", "GYN – Infection"):
            self.assertEqual(storage_organ_system(label), label)

    def test_upload_extraction_retains_every_source_and_exact_topic(self):
        extractor = load("EOR_Extraction_optimized")
        remediation = load("create_remediation_assignment")
        items = [{"organ_system": source, "task_area": "Formulating Most Likely Diagnosis",
                  "topic_area": f"Specific source topic {index}"}
                 for index, source in enumerate(SOURCE_ORGAN_ALIASES)]
        for module in (extractor, remediation):
            parsed = module.EORResponse(extracted_topics=items)
            self.assertEqual(len(parsed.extracted_topics), 25)
            schema = module.EORResponse.model_json_schema()
            self.assertIn("GYN – Infections", json.dumps(schema, ensure_ascii=False))
        manager = MagicMock()
        manager.structured_completion.return_value = extractor.EORResponse(extracted_topics=items)
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(extractor.difflib, "get_close_matches") as fuzzy:
            output = Path(directory) / "topics.json"
            extractor.eor_extract_information(manager, "file-upload", str(output))
            extracted = json.loads(output.read_text())["extracted_topics"]
        fuzzy.assert_not_called()
        self.assertEqual([item["organ_system"] for item in extracted], list(SOURCE_ORGAN_ALIASES))
        self.assertEqual([item["topic_area"] for item in extracted],
                         [item["topic_area"] for item in items])
        manager = object.__new__(remediation.ConversationManager)
        manager.system_message = "Educator"
        manager.client = MagicMock()
        manager.client.beta.chat.completions.parse = AsyncMock(return_value=SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(
                parsed=remediation.EORResponse(extracted_topics=items)))]))
        with patch.object(remediation.difflib, "get_close_matches") as fuzzy:
            result = asyncio.run(manager.extract_from_file("file-upload"))
        fuzzy.assert_not_called()
        self.assertEqual([item.organ_system for item in result], list(SOURCE_ORGAN_ALIASES))
        self.assertEqual([item.topic_area for item in result],
                         [item["topic_area"] for item in items])

    def test_selected_alias_variants_are_consistent_without_collapsing_other_labels(self):
        from fill_database_topics import load_topics, validate_question

        engine = load("Question_writing_and_verification_optomized")
        for source in SOURCE_ORGAN_ALIASES:
            variant = source.replace("–", "-").replace("-", " – ").upper()
            item = {"organ_system": source, "task_area": TASK, "topic_area": "Specific disease"}
            with self.subTest(source=source):
                validated = validate_question(
                    question({**item, "organ_system": f"  {variant}  "}), item,
                    engine.standardize_categories, allowed_organ_systems=(source,),
                )
                self.assertEqual(validated["organ_system"], source)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "topics.json"
            for pair in (("GYN – Infections", "GYN - Infections"),
                         ("Breast Surgery", "BREAST  SURGERY")):
                path.write_text(json.dumps({label: ["Topic"] for label in pair}))
                with self.subTest(pair=pair), self.assertRaises(ValueError):
                    load_topics(path, allow_custom_categories=True)
            labels = ("Growth and Development", "Growth  and Development",
                      "Custom - Scope", "Custom – Scope")
            path.write_text(json.dumps({label: ["Topic"] for label in labels}))
            self.assertEqual(tuple(load_topics(path, allow_custom_categories=True)), labels)
            for label in labels:
                self.assertEqual(engine.standardize_categories(
                    label, TASK, allowed_organ_systems=labels)[0], label)

    def test_uploaded_quiz_preserves_separate_sources_and_stores_canonical(self):
        engine = load("Question_writing_and_verification_optomized")
        conn, cursor = connection()
        manager = MagicMock()
        manager.client.close = AsyncMock()
        items = [{"organ_system": source, "task_area": TASK, "topic_area": f"Topic {index}"}
                 for index, source in enumerate(SOURCE_ORGAN_ALIASES)]

        async def generate(sem, item, count, *args):
            return [question(item) for _ in range(count)]

        with tempfile.TemporaryDirectory() as directory, \
                patch.object(engine, "ConversationManager", return_value=manager), \
                patch.object(engine, "get_async_db_connection", AsyncMock(return_value=conn)), \
                patch.object(engine, "sem_process_topic_batch", side_effect=generate) as batch:
            path = Path(directory) / "topics.json"
            path.write_text(json.dumps({"extracted_topics": items}))
            asyncio.run(engine.generate_questions_from_eor_async(
                str(path), "quiz", None, "owner", 2, True, "quiz-id"))
        self.assertEqual([call.args[1]["organ_system"] for call in batch.call_args_list],
                         list(SOURCE_ORGAN_ALIASES))
        cursor.execute.assert_not_awaited()
        stored = cursor.executemany.call_args_list[0].args[1]
        by_topic = {row[3]: row[1] for row in stored}
        self.assertEqual(by_topic, {item["topic_area"]: SOURCE_ORGAN_ALIASES[item["organ_system"]]
                                    for item in items})
        self.assertEqual(len(stored), 50)
        conn.commit.assert_awaited_once()

    def test_remediation_generation_scope_and_canonical_persistence(self):
        remediation = load("create_remediation_assignment")
        for source, canonical in SOURCE_ORGAN_ALIASES.items():
            with self.subTest(source=source):
                manager, verifier, core = MagicMock(), MagicMock(), MagicMock()
                manager.chat_completion = AsyncMock(side_effect=[{"summary": "Scoped summary"}, {}])
                verifier.chat_completion = AsyncMock(return_value={"is_valid": True, "confidence_score": 9})
                core.client.close = AsyncMock()
                item = SimpleNamespace(organ_system=source, task_area=TASK, topic_area="Specific disease")
                engine_batch = AsyncMock(return_value=[question(vars(item))])
                conn, cursor = connection()
                with patch.object(remediation, "CoreConversationManager", return_value=core), \
                        patch.object(remediation, "process_topic_batch", engine_batch), \
                        patch.object(remediation, "get_async_db_connection", AsyncMock(return_value=conn)):
                    result = asyncio.run(remediation.process_topic_set(manager, verifier, item))
                    asyncio.run(remediation.save_to_database("owner", [result], "remediation"))
                self.assertIn(source, manager.chat_completion.call_args_list[0].args[0])
                self.assertIn("Specific disease", verifier.chat_completion.call_args.args[0])
                self.assertEqual(engine_batch.call_args.kwargs["item_dict"]["topic_area"], "Specific disease")
                self.assertEqual(result["organ_system"], canonical)
                self.assertEqual(result["questions"][0]["organ_system"], canonical)
                row = cursor.executemany.call_args_list[0].args[1][0]
                self.assertEqual((row[5], row[9]), (canonical, "Specific disease"))

    def test_upload_alias_metadata_mismatch_is_rejected_before_verification(self):
        engine = load("Question_writing_and_verification_optomized")
        for source in SOURCE_ORGAN_ALIASES:
            item = {"organ_system": source, "task_area": TASK, "topic_area": "Specific disease"}
            for mismatch in (
                {"organ_system": SOURCE_ORGAN_ALIASES[source]},
                {"organ_system": "Growth and Development"},
                {"task_area": "Clinical Intervention"}, {"topic_area": "Different disease"},
            ):
                with self.subTest(source=source, mismatch=mismatch):
                    manager, verifier = MagicMock(), MagicMock()
                    manager.chat_completion = AsyncMock(return_value=json.dumps(
                        {"questions": [question({**item, **mismatch})]}))
                    with contextlib.redirect_stdout(io.StringIO()), \
                            patch.object(engine, "Question_Verification", AsyncMock()) as verify:
                        result = asyncio.run(engine.process_topic_batch(item, 1, manager, verifier))
                    self.assertEqual(result, [])
                    self.assertEqual(manager.chat_completion.await_count, 4)
                    verify.assert_not_awaited()

    def test_alias_generation_and_verification_keep_source_scope(self):
        engine = load("Question_writing_and_verification_optomized")
        for source in SOURCE_ORGAN_ALIASES:
            with self.subTest(source=source):
                item = {"organ_system": source, "task_area": TASK, "topic_area": "Specific disease"}
                manager = object.__new__(engine.ConversationManager)
                verifier = object.__new__(engine.ConversationManager)
                manager.chat_completion = AsyncMock(return_value=json.dumps(
                    {"questions": [question(item)]}))
                verifier.chat_completion = AsyncMock(side_effect=[
                    json.dumps({"correct_answer": "A", "confidence_score": 9}),
                ] * engine.VERIFICATION_PASSES + [json.dumps({"difficulty": 5})])
                with contextlib.redirect_stdout(io.StringIO()):
                    result = asyncio.run(engine.process_topic_batch(item, 1, manager, verifier))
                self.assertEqual(result[0]["organ_system"], source)
                self.assertEqual(result[0]["topic_area"], "Specific disease")
                self.assertIn(source, manager.chat_completion.call_args.args[0])
                for call in verifier.chat_completion.call_args_list[:engine.VERIFICATION_PASSES]:
                    self.assertIn(source, call.args[0])
                    self.assertIn("Specific disease", call.args[0])

    def test_background_remediation_does_not_reuse_broader_canonical_content(self):
        remediation = load("create_remediation_assignment")
        conn, cursor = connection()
        manager = MagicMock()
        manager.client.close = AsyncMock()
        sources = ("GYN – Infections", "Breast Surgery")
        manager.extract_from_file = AsyncMock(return_value=[
            SimpleNamespace(organ_system=source, task_area=TASK, topic_area="Shared topic")
            for source in sources
        ])
        generated_sources = []

        async def generate(sem, manager, verifier, item):
            generated_sources.append(item.organ_system)
            return {"organ_system": item.organ_system, "task_area": item.task_area,
                    "topic_area": item.topic_area, "summary": item.organ_system,
                    "questions": [question(vars(item))]}

        with patch.object(remediation, "ConversationManager", return_value=manager), \
                patch.object(remediation, "get_async_db_connection", AsyncMock(return_value=conn)), \
                patch.object(remediation, "check_existing_remediation", AsyncMock()) as cache, \
                patch.object(remediation, "sem_process_topic_set", side_effect=generate):
            ids = asyncio.run(remediation.background_process_document(
                None, "owner", "remediation", uploaded_file_id="file-upload"))
        self.assertCountEqual(generated_sources, sources)
        cache.assert_not_awaited()
        self.assertEqual(len(ids), 2)
        rows = cursor.executemany.call_args_list[0].args[1]
        self.assertEqual([row[5] for row in rows], ["Reproductive System"] * 2)
        self.assertCountEqual([row[11] for row in rows], sources)
        manager.client.files.create.assert_not_called()


if __name__ == "__main__":
    unittest.main()
