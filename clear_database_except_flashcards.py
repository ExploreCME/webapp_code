#!/usr/bin/env python3
import os
import argparse
import pymysql
from dotenv import load_dotenv

# Load environment variables from the local project root when present,
# but do not depend on a machine-specific absolute path.
load_dotenv()

MYSQL_HOST = os.getenv('MYSQL_HOST')
MYSQL_USER = os.getenv('MYSQL_USER')
MYSQL_PASSWORD = os.getenv('MYSQL_PASSWORD')
MYSQL_DB = os.getenv('MYSQL_DB')
MYSQL_PORT = int(os.getenv('MYSQL_PORT', '25060'))


def _connect_mysql():
    """Create a MySQL connection using Render/DigitalOcean-friendly settings."""
    missing = [
        name for name, value in {
            'MYSQL_HOST': MYSQL_HOST,
            'MYSQL_USER': MYSQL_USER,
            'MYSQL_PASSWORD': MYSQL_PASSWORD,
            'MYSQL_DB': MYSQL_DB,
        }.items() if not value
    ]
    if missing:
        raise RuntimeError(
            "Missing MySQL environment variables: " + ", ".join(missing)
        )

    ssl_disabled = os.getenv('MYSQL_SSL_DISABLED', 'false').lower() in (
        '1', 'true', 'yes', 'on'
    )
    ssl_ca = os.getenv('MYSQL_SSL_CA')

    connection_kwargs = {
        'host': MYSQL_HOST,
        'port': MYSQL_PORT,
        'user': MYSQL_USER,
        'password': MYSQL_PASSWORD,
        'database': MYSQL_DB,
        'cursorclass': pymysql.cursors.DictCursor,
        'autocommit': False,
        'connect_timeout': 10,
        'charset': 'utf8mb4',
    }

    if not ssl_disabled:
        if ssl_ca:
            connection_kwargs['ssl'] = {'ca': ssl_ca}
        else:
            # DigitalOcean managed MySQL generally requires TLS.
            # This matches the repo's safer encrypted-connection pattern.
            connection_kwargs['ssl'] = {'ssl': {}}

    return pymysql.connect(**connection_kwargs)


def clear_database_except_flashcards(assume_yes: bool = False, keep_users: bool = True):
    """
    Clears quiz/attempt/task/assignment/remediation/question data while preserving:
      - flashcards
      - flashcard_progress
      - users (optional; preserved by default)

    Intended for testing resets before generating new content.
    """

    conn = _connect_mysql()

    # Child -> parent order to avoid FK violations.
    # NOTE: We do not disable FK checks; ordered DELETE is safer with evolving schemas.
    tables_to_delete = [
        "attempt_responses",
        "quiz_questions_map",
        "remediation_questions",
        "quiz_attempts",
        "quiz_generation_tasks",
        "assigned_quizzes",
        "assigned_remediations",
        "user_quizzes",
        "question_bank",
        "remediation_assignments",
    ]

    if not keep_users:
        tables_to_delete.append("users")

    confirmation_text = (
        "This will DELETE all non-flashcard testing data, including remediations, quizzes, "
        "attempt history, generation tasks, and mappings."
    )
    if keep_users:
        confirmation_text += " Users will be preserved."
    else:
        confirmation_text += " Users will ALSO be deleted."

    if not assume_yes:
        print("⚠️  DATABASE RESET WARNING")
        print(confirmation_text)
        confirm = input("Type 'yes' to proceed: ").strip().lower()
        if confirm != "yes":
            print("Operation cancelled.")
            conn.close()
            return

    deleted_counts = {}

    try:
        with conn.cursor() as cursor:
            print("\nStarting database cleanup...")

            for table in tables_to_delete:
                try:
                    cursor.execute(f"DELETE FROM `{table}`")
                    deleted_counts[table] = cursor.rowcount
                    print(f"-> Cleared {table}: {cursor.rowcount} rows deleted")
                except pymysql.err.ProgrammingError as e:
                    # Table doesn't exist in this environment/schema variant
                    deleted_counts[table] = None
                    print(f"-> Skipped {table}: {e}")
                except pymysql.err.OperationalError as e:
                    deleted_counts[table] = None
                    print(f"-> Note on {table}: {e}")

            conn.commit()
            print("\n✅ Database cleanup committed.")

            # Post-wipe verification report
            verify_tables = [
                "attempt_responses",
                "quiz_attempts",
                "quiz_questions_map",
                "user_quizzes",
                "question_bank",
                "quiz_generation_tasks",
                "assigned_quizzes",
                "assigned_remediations",
                "remediation_questions",
                "remediation_assignments",
                "flashcards",
                "flashcard_progress",
            ]
            if not keep_users:
                verify_tables.append("users")

            print("\nPost-wipe verification (row counts):")
            for t in verify_tables:
                try:
                    cursor.execute(f"SELECT COUNT(*) AS c FROM `{t}`")
                    c = cursor.fetchone()["c"]
                    print(f"-> {t}: {c}")
                except Exception as e:
                    print(f"-> {t}: skipped ({e})")

            print("\nSummary:")
            print("Preserved:")
            print("-> flashcards")
            print("-> flashcard_progress")
            if keep_users:
                print("-> users")
            print("Cleared:")
            print("-> questions, quizzes, attempts, tasks, assignments, remediations")

    except Exception as e:
        conn.rollback()
        print(f"\n❌ Error during cleanup. Rolled back. Details: {e}")
        raise
    finally:
        conn.close()
        print("Database connection closed.")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Reset ExploreCME DB for testing while preserving flashcards."
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Run non-interactively without confirmation prompt."
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--keep-users",
        dest="keep_users",
        action="store_true",
        help="Preserve users table (default)."
    )
    group.add_argument(
        "--delete-users",
        dest="keep_users",
        action="store_false",
        help="Also delete users table."
    )
    parser.set_defaults(keep_users=True)
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    clear_database_except_flashcards(assume_yes=args.yes, keep_users=args.keep_users)
