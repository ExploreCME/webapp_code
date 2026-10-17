from copy import deepcopy
import json
import unittest

import rnxcel_engine as engine


def authored(version_id="version-1", sata=False):
    return {
        "version_id": version_id, "exam_profile": "NCLEX_RN_2026",
        "type": "select-all-that-apply" if sata else "single-best-answer",
        "category_id": "MOC", "objective_id": "MOC-01",
        "stem": "Select the marked symbol.",
        "options": [
            {"id": "a", "text": "Circle", "private": "option-secret"},
            {"id": "b", "text": "Square"},
            {"id": "c", "text": "Triangle"},
        ],
        "correct_option_ids": ["a", "b"] if sata else ["a"],
        "rationale": "Private fixture explanation",
        "scoring": "plus-minus" if sata else "single",
        "official_activity_reviewed": True,
        "tags": {
            "population": ["adult"], "setting": ["acute-care"],
            "specialty": [], "organ_topics": [], "nursing_tasks": ["symbol-selection"],
            "clinical_judgment": ["recognize-cues"],
            "official_activity_refs": ["Fixture-only reference"],
            "private": ["tag-secret"],
        },
        "provenance": {"private": "provenance-secret"},
    }


def case_version():
    return {
        "version_id": "case-1", "exam_profile": "NCLEX_RN_2026",
        "type": "unfolding-case-study", "title": "Symbol sequence fixture",
        "stages": [
            {
                "id": f"stage-{index}",
                "exhibits": [{
                    "timestamp": f"09:{index:02d}", "text": f"exhibit-{index}",
                    "private": "exhibit-secret",
                }],
                "item": authored(f"stage-version-{index}", sata=index % 2 == 1),
            }
            for index in range(6)
        ],
    }


class EngineTests(unittest.TestCase):
    def test_validation_detaches_json_and_retains_private_snapshot(self):
        source = authored()
        result = engine.validate_version(source)
        self.assertEqual(source, result)
        result["options"][0]["text"] = "Different"
        self.assertNotEqual(source, result)
        self.assertIn("provenance", result)

    def test_required_fields_and_enum_validation(self):
        for name in (
            "version_id", "exam_profile", "type", "category_id", "objective_id",
            "stem", "options", "correct_option_ids", "rationale", "scoring", "tags",
        ):
            value = authored()
            del value[name]
            with self.subTest(field=name), self.assertRaises(ValueError):
                engine.validate_version(value)
        for name, invalid in (
            ("type", []), ("category_id", {}), ("objective_id", "MOC-09"),
            ("objective_id", []),
            ("objective_id", "SIC-01"), ("exam_profile", "NCLEX_PN_2026"),
            ("rationale", " "), ("scoring", "plus-minus"),
            ("version_id", 42), ("stem", None),
        ):
            value = authored()
            value[name] = invalid
            with self.subTest(field=name, invalid=invalid), self.assertRaises(ValueError):
                engine.validate_version(value)

    def test_all_category_objective_pairs(self):
        for category in engine.CATEGORIES:
            for number in range(1, 9):
                value = authored()
                value.update(category_id=category, objective_id=f"{category}-{number:02d}")
                engine.validate_version(value)

    def test_invalid_options_keys_and_tags(self):
        mutations = [
            lambda v: v.update(options=[]),
            lambda v: v["options"].append(deepcopy(v["options"][0])),
            lambda v: v["options"][0].update(id=1),
            lambda v: v["options"][0].update(text=""),
            lambda v: v.update(correct_option_ids=[]),
            lambda v: v.update(correct_option_ids=["a", "a"]),
            lambda v: v.update(correct_option_ids=["unknown"]),
            lambda v: v.update(correct_option_ids=["a", "b"]),
            lambda v: v["tags"].pop("population"),
            lambda v: v["tags"].update(population="adult"),
            lambda v: v["tags"].update(population=[3]),
            lambda v: v["tags"].update(official_activity_refs=[{}]),
            lambda v: v.update(provenance={"not-json": object()}),
            lambda v: v.update(provenance={"not-json": float("nan")}),
        ]
        for mutate in mutations:
            value = authored()
            mutate(value)
            with self.subTest(value=value), self.assertRaises(ValueError):
                engine.validate_version(value)

    def test_selectable_tag_dimensions_use_catalog_values(self):
        for dimension, allowed in (
            ("population", engine.POPULATIONS),
            ("setting", engine.SETTINGS),
            ("clinical_judgment", engine.CLINICAL_JUDGMENT),
        ):
            for tag in allowed:
                value = authored()
                value["tags"][dimension] = [tag]
                engine.validate_version(value)
            value["tags"][dimension] = ["unknown-editorial-value"]
            with self.subTest(dimension=dimension), self.assertRaises(ValueError):
                engine.validate_version(value)
        value = authored()
        value["tags"].update(
            specialty=["reviewed specialty"], organ_topics=["reviewed organ topic"],
            nursing_tasks=["reviewed nursing task"],
        )
        engine.validate_version(value)

    def test_official_activity_mappings_require_explicit_editorial_review(self):
        for review_flag in (None, False, 1, "true"):
            value = authored()
            value["official_activity_reviewed"] = review_flag
            with self.subTest(flag=review_flag), self.assertRaises(ValueError):
                engine.validate_version(value)
        value = authored()
        del value["official_activity_reviewed"]
        with self.assertRaises(ValueError):
            engine.validate_version(value)
        value["tags"]["official_activity_refs"] = []
        engine.validate_version(value)
        del value["tags"]["official_activity_refs"]
        engine.validate_version(value)
        value["official_activity_reviewed"] = False
        engine.validate_version(value)
        value = authored()
        del value["official_activity_reviewed"]
        value["tags"]["official_activity_reviewed"] = True
        with self.assertRaises(ValueError):
            engine.validate_version(value)

    def test_unhashable_identifiers_and_tag_values_raise_value_error(self):
        for field in ("objective_id", "category_id", "type"):
            for invalid in ([], {}, {"not": "an identifier"}):
                value = authored()
                value[field] = invalid
                with self.subTest(field=field, value=invalid), self.assertRaises(ValueError):
                    engine.validate_version(value)
        for dimension in engine.TAG_DIMENSIONS:
            value = authored()
            value["tags"][dimension] = [["nested"]]
            with self.subTest(dimension=dimension), self.assertRaises(ValueError):
                engine.validate_version(value)

    def test_provenance_preserves_editorial_metadata_but_stays_private(self):
        provenance = {
            "sources": [{"title": "Editorial reference", "locator": "Chapter 1"}],
            "editorial_review": {"reviewer": "Nurse editor", "date": "2026-10-17"},
        }
        for value in (authored(), case_version()):
            value["provenance"] = provenance
            validated = engine.validate_version(value)
            self.assertEqual(validated["provenance"], provenance)
            self.assertNotIn("provenance", engine.learner_item([validated], 0))
            for invalid in (None, [], "source", True, {1: "source"}):
                value["provenance"] = invalid
                with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                    engine.validate_version(value)
        value = case_version()
        value["stages"][0]["item"]["provenance"] = []
        with self.assertRaises(ValueError):
            engine.validate_version(value)

    def test_version_ids_match_ascii_database_column(self):
        for invalid in ("vérsion-1", "version-\u2603", "version-\n1", "version-1 ",
                        " version-1", "version-\x7f"):
            value = authored(invalid)
            with self.subTest(id=invalid), self.assertRaises(ValueError):
                engine.validate_version(value)
        engine.validate_version(authored("VERSION-1:v2_2026"))

    def test_scoring_rules(self):
        self.assertEqual(engine.score(authored(), ["a"]), (1, 1))
        self.assertEqual(engine.score(authored(), ["b"]), (0, 1))
        sata = authored(sata=True)
        for selection, expected in (
            (["a"], (1, 2)), (["a", "b"], (2, 2)), (["c"], (0, 2)),
            (["a", "c"], (0, 2)), (["a", "b", "c"], (1, 2)),
        ):
            self.assertEqual(engine.score(sata, selection), expected)

    def test_invalid_selections_never_score(self):
        for selection in (None, "", "a", [], {}, ["a", "a"], ["z"], [1], [True]):
            with self.subTest(selection=selection), self.assertRaises(ValueError):
                engine.score(authored(), selection)
        with self.assertRaises(ValueError):
            engine.score(authored(), ["a", "b"])

    def test_case_requires_six_unique_valid_stages(self):
        engine.validate_version(case_version())
        for mutate in (
            lambda v: v["stages"].pop(),
            lambda v: v["stages"][1].update(id="stage-0"),
            lambda v: v["stages"][0].update(exhibits="text"),
            lambda v: v["stages"][0]["exhibits"][0].update(timestamp=None),
            lambda v: v["stages"][0]["item"].update(correct_option_ids=["z"]),
        ):
            value = case_version()
            mutate(value)
            with self.assertRaises(ValueError):
                engine.validate_version(value)

    def test_flatten_preserves_sequence_and_releases_only_prior_exhibits(self):
        snapshot = [authored(), case_version(), authored("final")]
        items = engine.flatten(snapshot)
        self.assertEqual(len(items), 8)
        for index in range(6):
            item = engine.learner_item(snapshot, index + 1)
            self.assertEqual(item["version_id"], "case-1")
            self.assertEqual(item["stage_index"], index)
            self.assertEqual(item["stage_count"], 6)
            self.assertEqual(
                [entry["text"] for entry in item["exhibits"]],
                [f"exhibit-{prior}" for prior in range(index + 1)],
            )
        items[1]["exhibits"][0]["text"] = "mutated"
        self.assertEqual(engine.flatten(snapshot)[2]["exhibits"][0]["text"], "exhibit-0")

    def test_case_and_plain_tags_are_not_mutated_by_projection_or_review(self):
        snapshot = [authored(), case_version()]
        original = deepcopy(snapshot)
        items = engine.flatten(snapshot)
        responses = [
            {"index": index, "selections": ["a"]} for index in range(len(items))
        ]
        reviewed = engine.review(snapshot, responses)
        for index in range(len(items)):
            public = engine.learner_item(snapshot, index)
            public["tags"]["population"].append("modified-public-value")
            reviewed[index]["tags"]["population"].append("modified-review-value")
            items[index]["tags"]["population"].append("modified-flat-value")
        self.assertEqual(snapshot, original)

    def test_public_projection_uses_nested_allowlists(self):
        for snapshot in ([authored()], [case_version()]):
            encoded = json.dumps(engine.learner_item(snapshot, 0))
            for secret in (
                "correct_option_ids", "rationale", "provenance", "private",
                "scoring", "official_activity_reviewed",
                "option-secret", "tag-secret", "exhibit-secret",
            ):
                self.assertNotIn(secret, encoded)
        snapshot = [authored()]
        public = engine.learner_item(snapshot, 0)
        public["options"][0]["text"] = "Changed"
        self.assertNotEqual(snapshot[0]["options"][0]["text"], "Changed")

    def test_invalid_snapshot_and_index(self):
        for snapshot in ([], {}, None):
            with self.assertRaises(ValueError):
                engine.flatten(snapshot)
        for index in (-1, 1, True, 0.0, "0"):
            with self.assertRaises(ValueError):
                engine.learner_item([authored()], index)

    def test_review_requires_completion_and_recomputes_points(self):
        snapshot = [authored(), authored("sata", sata=True)]
        responses = [
            {"index": 0, "selections": ["a"], "earned": 999},
            {"index": 1, "selections": ["a", "c"]},
        ]
        for incomplete in ([], responses[:1], [responses[1], responses[0]]):
            with self.assertRaises(ValueError):
                engine.review(snapshot, incomplete)
        reviewed = engine.review(snapshot, responses)
        self.assertEqual((reviewed[0]["earned"], reviewed[1]["earned"]), (1, 0))
        self.assertEqual(reviewed[0]["rationale"], "Private fixture explanation")
        self.assertEqual(reviewed[1]["correct_option_ids"], ["a", "b"])
        self.assertNotIn("provenance", reviewed[0])


if __name__ == "__main__":
    unittest.main()
