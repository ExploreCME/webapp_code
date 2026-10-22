import hashlib
import json
from pathlib import Path
import random
import unittest

from fill_database_topics import ORGAN_SYSTEMS
import generate_standardized_quizzes as generator


ROOT = Path(__file__).resolve().parents[1]
TOPIC_FILES = (
    "family_medicine_topic_list.json",
    "internal_medicine_topic_list.json",
    "pediatrics_topic_list.json",
    "psychiatry_topic_list.json",
    "womens_health_topic_list.json",
    "surgery_topic_list.json",
)
SOURCE_TOPIC_DIGESTS = {
    "family_medicine_topic_list.json": "7dae8d304c39a3862b89c56540045cdf21d5a78bfac5903f00df8bbb3acb3125",
    "internal_medicine_topic_list.json": "9e06a90fb0071212fdeb786157e39628f5766be02e20d051c93a943126ac0c2c",
    "pediatrics_topic_list.json": "eff90d08ddbaca4a0df8a2a57ce701e517a41fd509914c7d163ad2a16abaa155",
    "psychiatry_topic_list.json": "8f539d2e23fb0bb230f0b350b001fb7c9974cad070b5bf7d29372828b5278f79",
    "womens_health_topic_list.json": "370362f37a9798bcf26536423d47b395f00c04ebdeb488b4149929f796e33d0a",
    "surgery_topic_list.json": "192ecf6ad420f70532558a7a4d4668d341daadab8cf3941fd98af7a788c01fe3",
}
SURGERY_PERIOPERATIVE_TOPICS = {
    f"Perioperative risk assessment and complications for {section}"
    for section in (
        "gastrointestinal", "cardiovascular", "pulmonary/thoracic surgery", "breast surgery",
        "dermatologic", "renal/genitourinary", "trauma/acute care",
        "neurologic/neurosurgery", "pain medicine/anesthesia", "endocrine",
    )
}


def topic_digest(topics):
    serialized = json.dumps(sorted(topics), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


class TopicListCompatibilityTests(unittest.TestCase):
    def test_lists_load_and_build_three_complete_blueprints(self):
        for filename in TOPIC_FILES:
            with self.subTest(filename=filename):
                path = ROOT / filename
                raw = json.loads(path.read_text(encoding="utf-8"))
                topics = generator.load_topics(path)
                self.assertEqual(set(raw), set(topics))
                self.assertTrue(set(raw).issubset(ORGAN_SYSTEMS))
                self.assertTrue(all(topics.values()))
                self.assertFalse({"title", "organization", "publication_year", "organ_systems"} & set(raw))

                organ_percentages = dict.fromkeys(topics, 0)
                organ_percentages[next(iter(topics))] = 100
                task_percentages = dict.fromkeys(generator.TASK_AREAS, 0)
                task_percentages[generator.TASK_AREAS[0]] = 100
                blueprints = generator.build_blueprints(
                    topics, organ_percentages, task_percentages, random.Random(42),
                )
                self.assertEqual([len(blueprint) for blueprint in blueprints], [120, 120, 120])
                for blueprint in blueprints:
                    for item in blueprint:
                        self.assertIn(item["topic_area"], topics[item["organ_system"]])

    def test_conversions_preserve_source_topics_and_surgery_flags(self):
        for filename in TOPIC_FILES:
            with self.subTest(filename=filename):
                topics = generator.load_topics(ROOT / filename)
                actual_topics = {
                    topic for values in topics.values() for topic in values
                }
                if filename == "surgery_topic_list.json":
                    self.assertTrue(SURGERY_PERIOPERATIVE_TOPICS.issubset(actual_topics))
                    actual_topics -= SURGERY_PERIOPERATIVE_TOPICS
                self.assertEqual(topic_digest(actual_topics), SOURCE_TOPIC_DIGESTS[filename])


if __name__ == "__main__":
    unittest.main()
