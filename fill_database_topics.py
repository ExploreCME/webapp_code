#!/usr/bin/env python3
"""Generate topic/task questions with the shared engine and save short batches."""

import argparse
import asyncio
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import importlib
import json
import os
from pathlib import Path
import sys
import uuid

from standardizers import TASK_AREAS

ROOT = Path(__file__).resolve().parent
ORGAN_SYSTEMS = (
    "Cardiovascular System", "Dermatologic System", "Endocrine System",
    "Eyes, Ears, Nose, and Throat", "Gastrointestinal System/Nutrition",
    "Genitourinary System", "Hematologic System", "Infectious Diseases",
    "Musculoskeletal System", "Neurologic System", "Psychiatry/Behavioral Science",
    "Pulmonary System", "Renal System", "Reproductive System",
)
QUESTION_FIELDS = (
    "organ_system", "task_area", "topic_area", "question_stem",
    "answer_choice_A", "answer_choice_B", "answer_choice_C",
    "answer_choice_D", "answer_choice_E", "correct_answer", "explanation",
    "difficulty_rating",
)
TABLE_COLUMNS = {
    "question_bank": ("id", *QUESTION_FIELDS, "created_date",
                      "global_thumbs_up", "global_thumbs_down"),
    "user_quizzes": ("quiz_id", "username", "quiz_name", "created_date"),
    "quiz_questions_map": ("quiz_id", "question_id"),
}


def positive_int(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def trimmed_string(value):
    return isinstance(value, str) and bool(value) and value == value.strip()


def parser():
    cli = argparse.ArgumentParser(description=(
        "Generate questions from topics. Regular runs incur API costs and modify "
        "the configured database; repeated runs generate additional questions."))
    cli.add_argument("--topics-file", type=Path, default=ROOT / "topics_by_organ_system.json")
    selection = cli.add_mutually_exclusive_group()
    selection.add_argument("--organ-system", choices=ORGAN_SYSTEMS)
    selection.add_argument("--all", action="store_true")
    cli.add_argument("--list-systems", action="store_true")
    cli.add_argument("--topic", action="append", default=[], help="Exact topic name; repeatable")
    cli.add_argument("--username", default="explorecme")
    cli.add_argument("--quiz-name", default="organ_system_batch_quiz")
    cli.add_argument("--questions-per-task", type=positive_int, default=1)
    cli.add_argument("--concurrency", type=positive_int, default=5)
    cli.add_argument("--env-file", type=Path, default=ROOT / ".env")
    cli.add_argument("--model", help="Account-accessible model compatible with the shared engine")
    cli.add_argument("--verify-model", help="Defaults to the generation model")
    cli.add_argument("--dry-run", action="store_true")
    return cli


def load_topics(path, *, allow_custom_categories=False):
    def unique_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"Duplicate JSON key: {key}")
            result[key] = value
        return result

    with path.open(encoding="utf-8") as source:
        data = json.load(source, object_pairs_hook=unique_keys)
    if not isinstance(data, dict) or not data:
        raise ValueError("Topics JSON must be a nonempty organ-system-to-topic-array mapping")
    result = {}
    for system, topics in data.items():
        if not trimmed_string(system) or len(system) > 255:
            raise ValueError("Organ systems must be nonempty trimmed strings of at most 255 characters")
        if not allow_custom_categories and system not in ORGAN_SYSTEMS:
            raise ValueError(f"Noncanonical organ system: {system!r}")
        if system.casefold() in {label.casefold() for label in result}:
            raise ValueError(f"Duplicate organ system ignoring case: {system!r}")
        if not isinstance(topics, list) or not topics:
            raise ValueError(f"{system}: expected a nonempty topic array")
        if any(not trimmed_string(topic) or len(topic) > 255 for topic in topics):
            raise ValueError(f"{system}: topics must be nonempty trimmed strings of at most 255 characters")
        result[system] = list(dict.fromkeys(topics))
    return result


def select_topics(args, data):
    if args.all:
        systems = list(data)
    elif args.organ_system:
        if args.organ_system not in data:
            raise ValueError("Selected organ system is absent from the topics file")
        systems = [args.organ_system]
    else:
        if not sys.stdin.isatty():
            raise ValueError("Noninteractive input requires --organ-system or --all")
        systems = list(data)
        for index, system in enumerate(systems, 1):
            print(f"{index}. {system}")
        try:
            index = int(input(f"Select organ system (1-{len(systems)}): "))
        except (ValueError, EOFError):
            raise ValueError("Invalid numbered selection") from None
        if not 1 <= index <= len(systems):
            raise ValueError("Invalid numbered selection")
        systems = [systems[index - 1]]
    requested = set(args.topic)
    unknown = requested - {topic for system in systems for topic in data[system]}
    if unknown:
        raise ValueError(f"Unknown selected topics: {', '.join(sorted(unknown))}")
    selected = [(system, topic) for system in systems for topic in data[system]
                if not requested or topic in requested]
    if not selected:
        raise ValueError("Selection is empty")
    return selected


def validate_question(question, item, standardize, *, allowed_organ_systems=None):
    if not isinstance(question, dict):
        raise ValueError("Question is not an object")
    cleaned = {}
    for field in QUESTION_FIELDS[:-1]:
        value = question.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"Missing or empty {field}")
        cleaned[field] = value.strip()
    # Never feed arbitrary labels to the engine's fuzzy/defaulting normalizer.
    organs = ORGAN_SYSTEMS if allowed_organ_systems is None else allowed_organ_systems
    for field, labels in (("organ_system", organs), ("task_area", TASK_AREAS)):
        labels_by_case = {label.casefold(): label for label in labels}
        value = labels_by_case.get(cleaned[field].casefold())
        if value != item[field]:
            raise ValueError(f"Wrong {field}")
        cleaned[field] = value
    context = {} if allowed_organ_systems is None else {"allowed_organ_systems": organs}
    organ, task = standardize(cleaned["organ_system"], cleaned["task_area"], **context)
    if (organ, task) != (item["organ_system"], item["task_area"]):
        raise ValueError("Category normalization changed the requested classification")
    if cleaned["topic_area"] != item["topic_area"]:
        raise ValueError("Wrong topic_area")
    cleaned["topic_area"] = item["topic_area"]
    if cleaned["correct_answer"] not in "ABCDE" or len(cleaned["correct_answer"]) != 1:
        raise ValueError("correct_answer must be A-E")
    difficulty = question.get("difficulty_rating")
    if type(difficulty) is not int or not 1 <= difficulty <= 10:
        raise ValueError("difficulty_rating must be an integer in 1..10")
    cleaned["difficulty_rating"] = difficulty
    return cleaned


class DatabaseWriter:
    def __init__(self, connection_factory, username, quiz_name):
        self.connection_factory = connection_factory
        self.username = username
        self.quiz_name = quiz_name

    def preflight(self):
        conn = self.connection_factory()
        try:
            with conn.cursor() as cursor:
                for table, columns in TABLE_COLUMNS.items():
                    cursor.execute(f"SELECT {', '.join(columns)} FROM {table} LIMIT 0")
                    cursor.execute(
                        "SELECT ENGINE FROM information_schema.TABLES "
                        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s", (table,))
                    row = cursor.fetchone()
                    if not row or row["ENGINE"].upper() != "INNODB":
                        raise RuntimeError("Required tables must use transactional InnoDB")
                for table, required in (
                    ("user_quizzes", {"username", "quiz_name"}),
                    ("quiz_questions_map", {"quiz_id", "question_id"}),
                ):
                    cursor.execute(f"SHOW INDEX FROM {table}")
                    indexes = {}
                    for row in cursor.fetchall():
                        if row["Non_unique"] == 0:
                            indexes.setdefault(row["Key_name"], set()).add(row["Column_name"])
                    if required not in indexes.values():
                        raise RuntimeError(f"Missing required unique key on {table}")
        finally:
            conn.close()

    def save(self, questions):
        conn = self.connection_factory()
        counts = {"inserted": 0, "reused": 0, "mapped": 0, "persisted": 0}
        try:
            # DBUtils begin() pins the connection and disables transaction reconnects.
            conn.begin()
            with conn.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO user_quizzes (quiz_id, username, quiz_name, created_date) "
                    "VALUES (%s, %s, %s, %s) ON DUPLICATE KEY UPDATE quiz_id = quiz_id",
                    (uuid.uuid4().hex, self.username, self.quiz_name, datetime.now()))
                cursor.execute(
                    "SELECT quiz_id FROM user_quizzes WHERE username = %s AND quiz_name = %s",
                    (self.username, self.quiz_name))
                row = cursor.fetchone()
                if not row:
                    raise RuntimeError("Quiz upsert did not resolve a quiz")
                quiz_id = row["quiz_id"]
                for question in questions:
                    # Explicit stem lookup respects MySQL collation; the schema need
                    # not have a unique stem index. Reuse only matching classifications.
                    cursor.execute(
                        "SELECT id FROM question_bank WHERE question_stem = %s "
                        "AND organ_system = %s AND task_area = %s AND topic_area = %s "
                        "ORDER BY id LIMIT 1",
                        tuple(question[field] for field in (
                            "question_stem", "organ_system", "task_area", "topic_area")))
                    existing = cursor.fetchone()
                    if existing:
                        question_id = existing["id"]
                        counts["reused"] += 1
                    else:
                        question_id = uuid.uuid4().hex
                        columns = ("id", *QUESTION_FIELDS, "created_date",
                                   "global_thumbs_up", "global_thumbs_down")
                        cursor.execute(
                            f"INSERT INTO question_bank ({', '.join(columns)}) "
                            f"VALUES ({', '.join(['%s'] * len(columns))})",
                            (question_id, *(question[field] for field in QUESTION_FIELDS),
                             datetime.now(), 0, 0))
                        counts["inserted"] += 1
                    cursor.execute(
                        "SELECT question_id FROM quiz_questions_map "
                        "WHERE quiz_id = %s AND question_id = %s", (quiz_id, question_id))
                    if not cursor.fetchone():
                        # The quiz upsert locks its row, serializing writers to this quiz.
                        cursor.execute(
                            "INSERT INTO quiz_questions_map (quiz_id, question_id) VALUES (%s, %s)",
                            (quiz_id, question_id))
                        counts["mapped"] += 1
                    counts["persisted"] += 1
            conn.commit()
            return counts
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()


def format_reasons(reasons):
    # Engine keys starting with "retried_" count recovered retries, not discards.
    discards = ", ".join(f"{key}={value}" for key, value in sorted(reasons.items())
                         if value and not key.startswith("retried_"))
    retries = ", ".join(f"{key}={value}" for key, value in sorted(reasons.items())
                        if value and key.startswith("retried_"))
    return "; ".join(part for part in (discards, retries and f"diagnostics: {retries}") if part)


async def thread_call(executor, function, *args):
    future = asyncio.get_running_loop().run_in_executor(executor, function, *args)
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError:
        # A synchronous transaction cannot be cancelled; drain it before releasing resources.
        try:
            await future
        finally:
            raise asyncio.CancelledError


async def generate(args, selected, engine, writer, api_key, model):
    counts = dict.fromkeys(
        ("requested", "generated", "rejected", "inserted", "reused", "mapped",
         "persisted", "failed", "shortfall"), 0)
    counts["requested"] = len(selected) * len(TASK_AREAS) * args.questions_per_task
    jobs = iter({"organ_system": system, "topic_area": topic, "task_area": task}
                for system, topic in selected for task in TASK_AREAS)
    reasons = Counter()
    managers, workers = [], []
    executor = ThreadPoolExecutor(max_workers=1)

    async def worker():
        for item in jobs:
            label = f"{item['organ_system']} / {item['topic_area']} / {item['task_area']}"
            # Per-job reasons keep diagnostics attributed to this topic/task only.
            job_reasons = Counter()
            try:
                results = await engine.process_topic_batch(
                    item_dict=dict(item), num_needed=args.questions_per_task,
                    conv_manager=managers[0], conv_manager_verify=managers[1],
                    stats=job_reasons)
                if not isinstance(results, list):
                    raise ValueError("Batch result is not a list")
            except Exception as error:
                reasons.update(job_reasons)
                counts["failed"] += 1
                counts["shortfall"] += args.questions_per_task
                print(f"Generation failed: {label} ({type(error).__name__})", file=sys.stderr)
                continue
            counts["generated"] += len(results)
            valid, stems = [], set()
            for result in results:
                try:
                    question = validate_question(result, item, engine.standardize_categories)
                    if question["question_stem"] in stems or len(valid) >= args.questions_per_task:
                        raise ValueError("Duplicate stem or excess question")
                    stems.add(question["question_stem"])
                    valid.append(question)
                except ValueError as error:
                    counts["rejected"] += 1
                    job_reasons[f"rejected: {error}"] += 1
                    print(f"Rejected: {label}: {error}")
            reasons.update(job_reasons)
            deficit = max(0, args.questions_per_task - len(valid))
            counts["shortfall"] += deficit
            if deficit:
                details = format_reasons(job_reasons)
                print(f"Shortfall: {label}: accepted {len(valid)}/{args.questions_per_task}"
                      + (f" (discard reasons: {details})" if details else ""))
            if valid:
                try:
                    saved = await thread_call(executor, writer.save, valid)
                    for key, value in saved.items():
                        counts[key] += value
                except Exception as error:
                    counts["failed"] += 1
                    print(f"Persistence failed: {label} ({type(error).__name__}); batch rolled back",
                          file=sys.stderr)

    async def close_manager(manager):
        await manager.client.close()

    try:
        for chosen_model in (model, args.verify_model or model):
            managers.append(engine.ConversationManager(api_key=api_key, model=chosen_model))
        workers = [asyncio.create_task(worker()) for _ in range(
            min(args.concurrency, len(selected) * len(TASK_AREAS)))]
        await asyncio.gather(*workers)
    finally:
        for task in workers:
            task.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        try:
            closed = await asyncio.gather(
                *(close_manager(manager) for manager in managers), return_exceptions=True)
            if any(isinstance(result, BaseException) for result in closed):
                counts["failed"] += 1
                print("Client cleanup failed", file=sys.stderr)
        finally:
            executor.shutdown(wait=True)
    print("Summary: " + ", ".join(f"{key}={value}" for key, value in counts.items()))
    if reasons:
        print("Discard reasons: " + format_reasons(reasons))
    complete = (counts["persisted"] == counts["requested"]
                and not any(counts[key] for key in ("failed", "rejected", "shortfall")))
    print("Target satisfied and persisted." if complete else
          "Incomplete run; already committed batches remain in the database.")
    return 0 if complete else 1


def main(argv=None):
    cli = parser()
    args = cli.parse_args(argv)
    try:
        data = load_topics(args.topics_file)
        if args.list_systems:
            print("\n".join(data))
            return 0
        for name, value in (("username", args.username), ("quiz-name", args.quiz_name)):
            if not trimmed_string(value) or len(value) > 255:
                raise ValueError(f"{name} must be a nonempty trimmed string of at most 255 characters")
        selected = select_topics(args, data)
    except (OSError, ValueError) as error:
        cli.error(str(error))
    for system, topic in selected:
        print(f"{system}: {topic}")
    print(f"Target: {len(selected) * len(TASK_AREAS) * args.questions_per_task} questions "
          f"({args.questions_per_task} per topic/task, {len(TASK_AREAS)} task areas); output is not guaranteed.")
    if args.dry_run:
        print("Dry run: no API calls or database connections.")
        return 0
    print("Generation incurs API costs and modifies the configured DB. "
          "Repeated runs generate additional questions.")
    try:
        from dotenv import load_dotenv
        if args.env_file.exists():
            load_dotenv(args.env_file, override=False)
        elif args.env_file != ROOT / ".env":
            raise ValueError("Specified --env-file does not exist")
        api_key = os.getenv("OPENAI_API_KEY") or os.getenv("MY_API_KEY")
        required = ("MYSQL_HOST", "MYSQL_USER", "MYSQL_PASSWORD", "MYSQL_DB")
        missing = [name for name in required if not os.getenv(name)]
        if not api_key:
            missing.append("OPENAI_API_KEY or MY_API_KEY")
        if missing:
            raise ValueError("Missing configuration: " + ", ".join(missing))
        from db_pool import get_db_connection
        writer = DatabaseWriter(get_db_connection, args.username, args.quiz_name)
        writer.preflight()
        engine = importlib.import_module("Question_writing_and_verification_optomized")
        model = args.model or os.getenv("OPENAI_MODEL") or engine.DEFAULT_MODEL
        return asyncio.run(generate(args, selected, engine, writer, api_key, model))
    except KeyboardInterrupt:
        print("Interrupted; committed batches remain saved.", file=sys.stderr)
        return 130
    except ValueError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    except Exception as error:
        # Provider/driver exceptions can contain credentials or connection URLs.
        print(f"Runtime/preflight failed ({type(error).__name__}); check dependencies, "
              "configuration, DB schema/permissions, and model access.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
