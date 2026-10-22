"""Delete one published standardized assessment version.

Run from the project directory (for example, the Render Shell):

    python delete_standardized_assessment.py

Permanently removes the published version, its questions, attempts, responses,
assignments, and question mappings. Back up the database before running.
"""
import json
import sys

from db_pool import get_db_connection
from standardized_quizzes import STANDARDIZED_PREFIX, STANDARDIZED_QUESTION_PREFIX


SELECT_SQL = """
    SELECT quiz_name, category, question_ids
    FROM standardized_quiz_versions
    WHERE BINARY quiz_name = %s
    FOR UPDATE
"""

DELETE_SQL = """
    DELETE FROM standardized_quiz_versions
    WHERE BINARY quiz_name = %s
"""


def build_quiz_name(name, version):
    """Return the published version name, e.g. 'Emergency Medicine version 1'."""
    name = (name or "").strip()
    version = (version or "").strip()
    if not name:
        raise ValueError("Enter an assessment name.")
    if not version.isascii() or not version.isdigit() or int(version) < 1:
        raise ValueError("Version must be a positive whole number.")
    return f"{name} version {int(version)}"


def _question_ids(question_ids):
    if isinstance(question_ids, (str, bytes, bytearray)):
        question_ids = json.loads(question_ids)
    if not isinstance(question_ids, list) or any(
        not isinstance(question_id, str)
        or not question_id.startswith(STANDARDIZED_QUESTION_PREFIX)
        for question_id in question_ids
    ):
        raise ValueError("Invalid published standardized question IDs; nothing deleted.")
    return list(dict.fromkeys(question_ids))


def delete_version(connection, quiz_name, input_func=input, print_func=print):
    """Delete quiz_name after confirmation. Returns True only if a row was deleted."""
    try:
        connection.begin()
        with connection.cursor() as cursor:
            cursor.execute(SELECT_SQL, (quiz_name,))
            row = cursor.fetchone()

            if row is None:
                connection.rollback()
                print_func(f"\nNo exact match found: {quiz_name!r}")
                print_func("Check spelling, capitalization, and version number.")
                return False

            question_ids = _question_ids(row["question_ids"])
            attempt_name = STANDARDIZED_PREFIX + row["quiz_name"]
            cursor.execute(
                "SELECT quiz_name, question_ids FROM standardized_quiz_versions "
                "WHERE BINARY quiz_name <> %s FOR UPDATE",
                (row["quiz_name"],),
            )
            for other in cursor.fetchall():
                if set(question_ids).intersection(_question_ids(other["question_ids"])):
                    raise ValueError(
                        f"Questions are shared with {other['quiz_name']!r}; nothing deleted."
                    )

            attempts_where = "BINARY quiz_name = %s"
            responses_where = (
                "attempt_id IN (SELECT attempt_id FROM quiz_attempts "
                "WHERE BINARY quiz_name = %s)"
            )
            deletions = [
                ("attempt_responses", responses_where, (attempt_name,)),
                ("quiz_attempts", attempts_where, (attempt_name,)),
                ("assigned_quizzes", attempts_where, (attempt_name,)),
            ]
            if question_ids:
                placeholders = ", ".join(["%s"] * len(question_ids))
                deletions.extend([
                    ("quiz_questions_map", f"BINARY question_id IN ({placeholders})",
                     tuple(question_ids)),
                    ("question_bank", f"BINARY id IN ({placeholders})",
                     tuple(question_ids)),
                ])

            # Lock attempts before their children while collecting preview counts.
            counts = {}
            for table, where, params in [deletions[1], deletions[0], *deletions[2:]]:
                cursor.execute(f"SELECT 1 FROM {table} WHERE {where} FOR UPDATE", params)
                counts[table] = len(cursor.fetchall())

            print_func("\nAssessment selected:")
            print_func(f"  Name:      {row['quiz_name']}")
            print_func(f"  Category:  {row['category']}")
            print_func(f"  Questions: {counts.get('question_bank', 0)}")
            print_func(f"  Attempts:  {counts['quiz_attempts']}")
            for table in ("attempt_responses", "assigned_quizzes", "quiz_questions_map"):
                print_func(f"  {table}: {counts.get(table, 0)}")
            print_func("\nWARNING: All listed data will be permanently deleted.")
            print_func("Back up the database first. Student attempt history will be lost.")

            required = f"DELETE {row['quiz_name']}"
            confirmation = input_func(f"\nType exactly '{required}' to confirm: ")
            if confirmation != required:
                connection.rollback()
                print_func("Cancelled. Nothing was deleted.")
                return False

            for table, where, params in deletions:
                cursor.execute(f"DELETE FROM {table} WHERE {where}", params)
            cursor.execute(DELETE_SQL, (row["quiz_name"],))
            if cursor.rowcount != 1:
                raise RuntimeError(
                    f"Expected one deleted row, got {cursor.rowcount}; rolling back."
                )

        connection.commit()
        print_func(f"\nDeleted published assessment and all associated data: {row['quiz_name']}")
        return True
    except BaseException:
        connection.rollback()
        raise


def main(input_func=input, print_func=print, connect=get_db_connection):
    name = input_func("Assessment name (example: Emergency Medicine): ")
    version = input_func("Version number (example: 1): ")
    quiz_name = build_quiz_name(name, version)

    connection = connect()
    try:
        return delete_version(connection, quiz_name, input_func, print_func)
    finally:
        connection.close()


def run():
    try:
        deleted = main()
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled.")
        return 1
    except Exception as exc:
        print(f"\nFailed: {exc}", file=sys.stderr)
        return 1
    return 0 if deleted else 1


if __name__ == "__main__":
    sys.exit(run())
