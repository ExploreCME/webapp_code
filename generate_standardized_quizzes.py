#!/usr/bin/env python3
"""Generate and atomically publish three verified 120-question standardized exams."""

import argparse
import asyncio
from collections import Counter
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
import importlib
import json
import os
from pathlib import Path
import random
import sys
import uuid

from fill_database_topics import (
    QUESTION_FIELDS, ROOT, format_reasons, load_topics, positive_int,
    validate_question,
)
from standardized_quizzes import STANDARDIZED_PREFIX, STANDARDIZED_QUESTION_PREFIX, STANDARDIZED_QUIZZES
from standardizers import TASK_AREAS, storage_organ_system

QUESTIONS_PER_VERSION = 120
VERSION_COUNT = 3
BATCH_SIZE = 5


def normalized_stem(question):
    return " ".join(question["question_stem"].casefold().split())


def validate_name(value):
    name = value.strip()
    # The longest name is also stored with the internal standardized prefix.
    if not name or len(STANDARDIZED_PREFIX + name + " version 3") > 255:
        raise ValueError("Quiz name must be nonempty and fit within 255 characters including its version/prefix")
    if name.casefold().startswith(STANDARDIZED_PREFIX.strip().casefold()):
        raise ValueError("Enter the quiz name without the reserved Standardized: prefix")
    return next((category for category in STANDARDIZED_QUIZZES
                 if category.casefold() == name.casefold()), name)


def validate_percentages(labels, values):
    if set(values) != set(labels):
        raise ValueError("Percentages must include exactly the selected labels")
    result = {}
    for label in labels:
        try:
            value = Decimal(str(values[label]))
        except (InvalidOperation, ValueError):
            raise ValueError(f"Invalid percentage for {label}") from None
        if not value.is_finite() or not 0 <= value <= 100:
            raise ValueError(f"Percentage for {label} must be between 0 and 100")
        result[label] = value
    if sum(result.values()) != 100:
        raise ValueError("Percentages must total exactly 100")
    return result


def prompt_percentages(labels, heading):
    while True:
        print(f"\n{heading} (total must be 100; use 0 to exclude an area)")
        try:
            return validate_percentages(labels, {
                label: input(f"{label} (%): ").strip() for label in labels
            })
        except ValueError as error:
            print(f"{error}. Please re-enter this distribution.")


def allocate(percentages):
    """Largest-remainder rounding gives each version exactly 120 slots."""
    targets = {label: weight * QUESTIONS_PER_VERSION / 100
               for label, weight in percentages.items()}
    counts = {label: int(target.to_integral_value(rounding=ROUND_FLOOR))
              for label, target in targets.items()}
    ranked = sorted(targets, key=lambda label: targets[label] - counts[label], reverse=True)
    for label in ranked[:QUESTIONS_PER_VERSION - sum(counts.values())]:
        counts[label] += 1
    return counts


def build_blueprints(topics, organ_percentages, task_percentages, rng):
    organ_counts = allocate(validate_percentages(topics, organ_percentages))
    task_counts = allocate(validate_percentages(TASK_AREAS, task_percentages))
    blueprints = []
    for _ in range(VERSION_COUNT):
        organs = [organ for organ, count in organ_counts.items() for _ in range(count)]
        tasks = [task for task, count in task_counts.items() for _ in range(count)]
        rng.shuffle(organs)
        rng.shuffle(tasks)
        blueprints.append([
            {"organ_system": organ, "task_area": task, "topic_area": rng.choice(topics[organ])}
            for organ, task in zip(organs, tasks)
        ])
    return blueprints


class VersionPublisher:
    def __init__(self, connection_factory, quiz_name):
        self.connection_factory = connection_factory
        self.quiz_name = quiz_name

    def names(self):
        return [f"{self.quiz_name} version {version}" for version in range(1, VERSION_COUNT + 1)]

    def preflight(self):
        conn = self.connection_factory()
        try:
            with conn.cursor() as cursor:
                for table, columns in (
                    ("question_bank", ("id", *QUESTION_FIELDS, "source_organ_system", "created_date")),
                    ("standardized_quiz_versions", ("quiz_name", "category", "question_ids", "created_date")),
                ):
                    cursor.execute(f"SELECT {', '.join(columns)} FROM {table} LIMIT 0")
                    cursor.execute(
                        "SELECT ENGINE FROM information_schema.TABLES "
                        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s", (table,),
                    )
                    row = cursor.fetchone()
                    if not row or row["ENGINE"].upper() != "INNODB":
                        raise ValueError("Publication requires transactional InnoDB tables")
                names = self.names()
                placeholders = ','.join(['%s'] * len(names))
                cursor.execute(
                    f"SELECT quiz_name FROM standardized_quiz_versions WHERE quiz_name IN ({placeholders})",
                    names,
                )
                if cursor.fetchone():
                    raise ValueError("Versions already exist; choose a different quiz name. Published versions are immutable.")
        finally:
            conn.close()

    def publish(self, versions):
        if len(versions) != VERSION_COUNT or any(len(version) != QUESTIONS_PER_VERSION for version in versions):
            raise ValueError("Only three complete 120-question versions can be published")
        stems = [normalized_stem(question)
                 for version in versions for question in version]
        if len(set(stems)) != len(stems):
            raise ValueError("Questions must have distinct stems across all three versions")
        conn = self.connection_factory()
        transaction_started = False
        try:
            conn.begin()
            transaction_started = True
            with conn.cursor() as cursor:
                for name, questions in zip(self.names(), versions):
                    question_ids = [STANDARDIZED_QUESTION_PREFIX + uuid.uuid4().hex for _ in questions]
                    columns = ("id", *QUESTION_FIELDS, "source_organ_system", "created_date")
                    cursor.executemany(
                        f"INSERT INTO question_bank ({', '.join(columns)}) "
                        f"VALUES ({', '.join(['%s'] * len(columns))})",
                        [(question_id, *(storage_organ_system(question[field]) if field == "organ_system"
                                         else question[field] for field in QUESTION_FIELDS),
                          question.get("source_organ_system"), datetime.now())
                         for question_id, question in zip(question_ids, questions)],
                    )
                    cursor.execute(
                        "INSERT INTO standardized_quiz_versions (quiz_name, category, question_ids, created_date) "
                        "VALUES (%s, %s, %s, %s)",
                        (name, self.quiz_name, json.dumps(question_ids), datetime.now()),
                    )
            conn.commit()
        except BaseException:
            if transaction_started:
                try:
                    conn.rollback()
                except Exception:
                    pass
            raise
        finally:
            conn.close()


async def generate_versions(blueprints, engine, api_key, model, verify_model, concurrency, max_rounds):
    allowed_organ_systems = tuple(dict.fromkeys(
        item["organ_system"] for blueprint in blueprints for item in blueprint
    ))
    jobs = {}
    versions = [[None] * QUESTIONS_PER_VERSION for _ in blueprints]
    for version, blueprint in enumerate(blueprints):
        for slot, item in enumerate(blueprint):
            key = (item["organ_system"], item["task_area"], item["topic_area"])
            jobs.setdefault(key, []).append((version, slot))
    managers, workers = [], []
    reasons, stems = Counter(), set()
    pending = iter(jobs.items())

    async def worker():
        for (organ, task, topic), slots in pending:
            item = {"organ_system": organ, "task_area": task, "topic_area": topic}
            remaining = list(slots)
            while remaining:
                # Keep prompts within the shared engine's response-token budget.
                target = min(BATCH_SIZE, len(remaining))
                accepted = 0
                for _ in range(max_rounds):
                    batch_reasons = Counter()
                    try:
                        batch = await engine.process_topic_batch(
                            item_dict=dict(item), num_needed=target - accepted,
                            conv_manager=managers[0], conv_manager_verify=managers[1], stats=batch_reasons,
                            allowed_organ_systems=allowed_organ_systems,
                        )
                    finally:
                        reasons.update(batch_reasons)
                    if not isinstance(batch, list):
                        raise ValueError("Question engine returned an invalid batch")
                    for result in batch:
                        if accepted == target:
                            break
                        try:
                            question = validate_question(
                                result, item, engine.standardize_categories,
                                allowed_organ_systems=allowed_organ_systems,
                            )
                        except ValueError:
                            reasons["invalid_question"] += 1
                            continue
                        stem = normalized_stem(question)
                        if stem in stems:
                            reasons["duplicate_stem"] += 1
                            continue
                        # Source scope is enforced before broad storage classifications are applied.
                        question["source_organ_system"] = item["organ_system"]
                        question["organ_system"] = storage_organ_system(question["organ_system"])
                        stems.add(stem)
                        version, slot = remaining.pop(0)
                        versions[version][slot] = question
                        accepted += 1
                    if accepted == target:
                        break
                if accepted != target:
                    raise ValueError(f"Generation shortfall: {organ} / {topic} / {task}: {len(remaining)} missing")

    try:
        for chosen_model in (model, verify_model or model):
            managers.append(engine.ConversationManager(api_key=api_key, model=chosen_model))
        workers = [asyncio.create_task(worker()) for _ in range(min(concurrency, len(jobs)))]
        await asyncio.gather(*workers)
        return versions
    finally:
        for worker_task in workers:
            worker_task.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        await asyncio.gather(*(manager.client.close() for manager in managers), return_exceptions=True)
        if reasons:
            print("Generation diagnostics: " + format_reasons(reasons))


def parser():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--quiz-name", help="Prompted when omitted; matching category names are canonicalized")
    cli.add_argument("--topics-file", type=Path, help="Prompted when omitted")
    cli.add_argument("--env-file", type=Path, default=ROOT / ".env")
    cli.add_argument("--model", help="Account-accessible generation model")
    cli.add_argument("--verify-model", help="Defaults to the generation model")
    cli.add_argument("--concurrency", type=positive_int, default=5)
    cli.add_argument("--max-rounds", type=positive_int, default=3, help="Bounded refill rounds per topic/task")
    cli.add_argument("--seed", type=int, help="Reproducible topic and slot allocation (not AI output)")
    cli.add_argument("--dry-run", action="store_true", help="Show distributions without DB/API access")
    return cli


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        name = validate_name(args.quiz_name or input("Quiz name: "))
        path = args.topics_file or Path(input("Topics JSON file: ").strip()).expanduser()
        topics = load_topics(path, allow_custom_categories=True)
        organs = prompt_percentages(topics, "Organ system percentages")
        tasks = prompt_percentages(TASK_AREAS, "Task area percentages")
        blueprints = build_blueprints(topics, organs, tasks, random.Random(args.seed))
        for index, blueprint in enumerate(blueprints, 1):
            print(f"\n{name} version {index}: {len(blueprint)} questions")
            for label, count in Counter(item["organ_system"] for item in blueprint).items():
                print(f"  {label}: {count}")
            for task in TASK_AREAS:
                print(f"  {task}: {sum(item['task_area'] == task for item in blueprint)}")
            print("  Topics: " + json.dumps(dict(Counter(
                f"{item['organ_system']} / {item['topic_area']}" for item in blueprint
            )), ensure_ascii=False))
        if args.dry_run:
            print("Dry run: no API calls or database changes.")
            return 0
        if input("\nGenerate 360 verified questions (API costs) and publish to all signed-in users? [y/N]: ").strip().lower() != "y":
            print("Cancelled; nothing published.")
            return 0
        from dotenv import load_dotenv
        if not args.env_file.exists() and args.env_file != ROOT / ".env":
            raise ValueError("Specified --env-file does not exist")
        load_dotenv(args.env_file, override=False)
        api_key = os.getenv("OPENAI_API_KEY") or os.getenv("MY_API_KEY")
        if not api_key or any(not os.getenv(key) for key in (
            "MYSQL_HOST", "MYSQL_USER", "MYSQL_PASSWORD", "MYSQL_DB",
        )):
            raise ValueError("Configure OPENAI_API_KEY (or MY_API_KEY) and MYSQL_HOST/USER/PASSWORD/DB")
        from db_pool import get_db_connection
        publisher = VersionPublisher(get_db_connection, name)
        publisher.preflight()
        engine = importlib.import_module("Question_writing_and_verification_optomized")
        model = args.model or os.getenv("OPENAI_MODEL") or engine.DEFAULT_MODEL
        versions = asyncio.run(generate_versions(
            blueprints, engine, api_key, model, args.verify_model, args.concurrency, args.max_rounds,
        ))
        publisher.publish(versions)
        print("Published all three 120-question versions. Available under Standardized Quizzes immediately.")
        return 0
    except (ValueError, OSError, EOFError) as error:
        print(f"Error: {error}. Nothing published.", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted. No incomplete versions published.", file=sys.stderr)
        return 130
    except Exception as error:
        # Provider/driver exceptions may include credentials; print only their type.
        print(f"Generation/publication failed ({type(error).__name__}). No incomplete versions published. "
              "Check schema migration, configuration, and model access.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
