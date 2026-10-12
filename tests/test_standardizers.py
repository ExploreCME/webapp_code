import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from standardizers import standardize_organ_system, standardize_task_area


ROOT = Path(__file__).resolve().parents[1]
ORGANS = (
    "Cardiovascular System", "Dermatologic System", "Endocrine System",
    "Eyes, Ears, Nose, and Throat", "Gastrointestinal System/Nutrition",
    "Genitourinary System", "Hematologic System", "Infectious Diseases",
    "Musculoskeletal System", "Neurologic System", "Psychiatry/Behavioral Science",
    "Pulmonary System", "Renal System", "Reproductive System",
)
TASKS = (
    "History Taking and Performing Physical Examination",
    "Using Diagnostic and Laboratory Studies",
    "Formulating the Most Likely Diagnosis",
    "Health Maintenance, Patient Education, and Preventive Measures",
    "Clinical Intervention", "Pharmaceutical Therapeutics",
    "Applying Foundational Scientific Concepts",
)


class StandardizerTests(unittest.TestCase):
    def test_all_canonical_labels_are_preserved(self):
        for normalize, labels in (
            (standardize_organ_system, ORGANS + ("Professional Practice", "Misc")),
            (standardize_task_area, TASKS + ("Professional Practice",)),
        ):
            for label in labels:
                for raw in (label, label.upper(), f"  {label.lower()}  "):
                    with self.subTest(raw=raw):
                        self.assertEqual(normalize(raw), label)
                        self.assertEqual(normalize(normalize(raw)), label)

    def test_organ_aliases_and_prefixes(self):
        aliases = (
            ("Cardiology", ORGANS[0]), ("Dermatologic", ORGANS[1]),
            ("Dermatology", ORGANS[1]), ("derm", ORGANS[1]), ("skin", ORGANS[1]),
            ("Dermatologic System: Acanthosis Nigricans", ORGANS[1]),
            ("Endocrinology", ORGANS[2]), ("ENT", ORGANS[3]),
            ("EENT", ORGANS[3]), ("GI", ORGANS[4]), ("GI/nutrition", ORGANS[4]),
            ("(gi)", ORGANS[4]), ("gastroenterology", ORGANS[4]),
            ("GU", ORGANS[5]), ("Urology", ORGANS[5]), ("Hematology", ORGANS[6]),
            ("Hematology: Diagnostic Studies", ORGANS[6]), ("B12", ORGANS[6]),
            ("ID", ORGANS[7]), ("infectious disease", ORGANS[7]),
            ("Orthopedics", ORGANS[8]), ("Neurology", ORGANS[9]),
            ("Behavioral health", ORGANS[10]), ("respiratory", ORGANS[11]),
            ("kidneys", ORGANS[12]), ("renal", ORGANS[12]),
            ("renal/genitourinary", ORGANS[5]), ("OB/GYN", ORGANS[13]),
            ("obgyn", ORGANS[13]), ("professional practice", "Professional Practice"),
        )
        for raw, expected in aliases:
            with self.subTest(raw=raw):
                self.assertEqual(standardize_organ_system(raw), expected)

    def test_organ_substring_traps_and_fallback(self):
        for raw in ("original", "digital", "general", "argument", "research",
                    "scheme", "unknown category", "", "   ", None, 123):
            with self.subTest(raw=raw):
                self.assertEqual(standardize_organ_system(raw), "Misc")

    def test_organ_fallback_can_preserve_unknown_labels_for_strict_selection(self):
        self.assertEqual(standardize_organ_system("unknown category", default="unknown category"),
                         "unknown category")
        self.assertEqual(standardize_organ_system("Hematology", default="Hematology"),
                         "Hematologic System")

    def test_task_aliases(self):
        aliases = (
            ("lab", TASKS[1]), ("labs", TASKS[1]), ("laboratory", TASKS[1]),
            ("diagnostic studies", TASKS[1]), ("study", TASKS[1]),
            ("lab-based evaluation", TASKS[1]), ("physical exam", TASKS[0]),
            ("PE", TASKS[0]), ("Hx & PE", TASKS[0]), ("history taking", TASKS[0]),
            ("rx", TASKS[5]), ("medications", TASKS[5]), ("treatment", TASKS[5]),
            ("prevention", TASKS[3]), ("patient education", TASKS[3]),
            ("procedures", TASKS[4]), ("surgery", TASKS[4]), ("clin int", TASKS[4]),
            ("sci", TASKS[6]), ("science", TASKS[6]), ("scientific", TASKS[6]),
            ("basic science", TASKS[6]), ("foundational concepts", TASKS[6]),
            ("pathophysiology", TASKS[6]), ("most likely diagnosis", TASKS[2]),
            ("diagnosis", TASKS[2]), ("dx", TASKS[2]),
            ("Using Laboratory and Diagnostic Studies", TASKS[1]),
            ("Formulating Most Likely Diagnosis", TASKS[2]),
            *[(f"Managing Patients - {label}", label) for label in TASKS[3:6]],
        )
        for raw, expected in aliases:
            with self.subTest(raw=raw):
                self.assertEqual(standardize_task_area(raw), expected)

    def test_task_substring_traps_and_fallback(self):
        for raw in ("syllable", "collaborative", "labeling", "specimen", "sphere",
                    "expenditure", "scissors", "rescind", "founder", "basically",
                    "basketball", "example", "unknown task", "  custom task  ", 123):
            with self.subTest(raw=raw):
                self.assertEqual(standardize_task_area(raw), str(raw).strip().title())
        for raw in (None, "", "   "):
            with self.subTest(raw=raw):
                self.assertEqual(standardize_task_area(raw), TASKS[2])


class StandardizerIntegrationTests(unittest.TestCase):
    def load(self, name):
        dependencies = {key: MagicMock() for key in
                        ("openai", "db_pool", "dotenv", "pymysql", "pymysql.cursors")}
        with patch.dict(sys.modules, dependencies):
            spec = importlib.util.spec_from_file_location(name, ROOT / (name + ".py"))
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        return module

    def test_existing_entry_points_agree(self):
        generator = self.load("Question_writing_and_verification_optomized")
        extractor = self.load("EOR_Extraction_optimized")
        remediation = self.load("fix_existing_remediations")
        for raw in ORGANS + ("Dermatology", "GI", "general", None):
            with self.subTest(organ=raw):
                expected = standardize_organ_system(raw)
                self.assertEqual(generator.standardize_categories(raw, None)[0], expected)
                self.assertEqual(extractor.standardize_extracted_organ(raw), expected)
                self.assertEqual(remediation.map_organ_to_canonical(raw), expected)
        for raw in TASKS + ("lab", "PE", "scissors", "specimen", None):
            with self.subTest(task=raw):
                expected = standardize_task_area(raw)
                self.assertEqual(generator.standardize_categories(None, raw)[1], expected)
                self.assertEqual(remediation.map_task_to_canonical(raw), expected)

    def test_extraction_normalizes_both_categories_before_saving(self):
        extractor = self.load("EOR_Extraction_optimized")
        manager = MagicMock()
        manager.structured_completion.return_value.model_dump.return_value = {
            "extracted_topics": [{
                "organ_system": "Dermatologic System",
                "task_area": "Formulating Most Likely Diagnosis",
                "topic_area": "Acanthosis Nigricans",
            }]
        }
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "extraction.json"
            extractor.eor_extract_information(manager, "file-test", str(output))
            item = json.loads(output.read_text())["extracted_topics"][0]
        self.assertEqual(item["organ_system"], ORGANS[1])
        self.assertEqual(item["task_area"], TASKS[2])
        self.assertEqual(item["topic_area"], "Acanthosis nigricans")
        generator = self.load("Question_writing_and_verification_optomized")
        self.assertEqual(generator.standardize_categories(
            item["organ_system"], item["task_area"]), (ORGANS[1], TASKS[2]))


if __name__ == "__main__":
    unittest.main()
