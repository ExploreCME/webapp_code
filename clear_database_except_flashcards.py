# wipe_database.py
import os
import pymysql
from dotenv import load_dotenv

# Point to your .env file
load_dotenv('/home/ps51632/mysite/explorecme/.env')

MYSQL_HOST = os.getenv('MYSQL_HOST')
MYSQL_USER = os.getenv('MYSQL_USER')
MYSQL_PASSWORD = os.getenv('MYSQL_PASSWORD')
MYSQL_DB = os.getenv('MYSQL_DB')

def wipe_questions_and_quizzes_only():
    conn = pymysql.connect(
        host=MYSQL_HOST,
        user=MYSQL_USER,
        password=MYSQL_PASSWORD,
        database=MYSQL_DB,
        cursorclass=pymysql.cursors.DictCursor
    )

    try:
        with conn.cursor() as cursor:
            print("Starting targeted database cleanup...")

            # Disable foreign key checks temporarily to safely clear relational mapping tables
            cursor.execute("SET FOREIGN_KEY_CHECKS = 0;")

            # Tables to wipe completely (Leaving `users` and `flashcards` untouched)
            tables_to_truncate = [
                "attempt_responses",
                "quiz_attempts",
                "quiz_questions_map",
                "user_quizzes",
                "question_bank",
                "quiz_generation_tasks",
                "remediation_assignments",
                "remediation_questions"
            ]

            for table in tables_to_truncate:
                try:
                    cursor.execute(f"TRUNCATE TABLE {table};")
                    print(f"-> Cleared table: {table}")
                except pymysql.err.OperationalError as e:
                    print(f"-> Note on {table}: {e}")

            # Re-enable foreign key checks
            cursor.execute("SET FOREIGN_KEY_CHECKS = 1;")

            conn.commit()
            print("\nDatabase cleanup complete!")
            print("-> User accounts (`users` table) remain intact.")
            print("-> Flashcards (`flashcards` table) remain intact.")
            print("-> All questions, attempt logs, quiz history, tasks, and remediations have been cleared.")

    except Exception as e:
        conn.rollback()
        print(f"Error during database wipe: {e}")
    finally:
        conn.close()

if __name__ == "__main__":
    confirm = input("Are you sure you want to delete all questions, attempt history, quiz records, and remediations? (yes/no): ").strip().lower()
    if confirm == 'yes':
        wipe_questions_and_quizzes_only()
    else:
        print("Operation cancelled.")