"""Delete one published standardized assessment version.

Run from the project directory (for example, the Render Shell):

    python delete_standardized_assessment.py

Only the matching row in standardized_quiz_versions is removed. Questions in
question_bank and student attempt records are retained.
"""
import json
import sys

from db_pool import get_db_connection


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


def _question_count(question_ids):
    if isinstance(question_ids, (str, bytes, bytearray)):
        question_ids = json.loads(question_ids)
    return len(question_ids or [])


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

            print_func("\nAssessment selected:")
            print_func(f"  Name:      {row['quiz_name']}")
            print_func(f"  Category:  {row['category']}")
            print_func(f"  Questions: {_question_count(row['question_ids'])}")
            print_func("\nOnly the published listing will be deleted.")
            print_func("Questions and student attempt records will be retained.")
            print_func("This version will disappear from catalog-based reports.")

            required = f"DELETE {row['quiz_name']}"
            confirmation = input_func(f"\nType exactly '{required}' to confirm: ")
            if confirmation != required:
                connection.rollback()
                print_func("Cancelled. Nothing was deleted.")
                return False

            cursor.execute(DELETE_SQL, (row["quiz_name"],))
            if cursor.rowcount != 1:
                raise RuntimeError(
                    f"Expected one deleted row, got {cursor.rowcount}; rolling back."
                )

        connection.commit()
        print_func(f"\nDeleted published assessment: {row['quiz_name']}")
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
        main()
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled.")
        return 1
    except Exception as exc:
        print(f"\nFailed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(run())
