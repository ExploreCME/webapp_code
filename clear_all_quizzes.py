#!/usr/bin/env python3
import os
import pymysql
from dotenv import load_dotenv

# Load environment variables
load_dotenv('/home/ps51632/mysite/explorecme/.env')

def clear_all_quizzes():
    print("⚠️  WARNING: NUCLEAR WIPE INITIATED ⚠️")
    print("This will delete ALL user quizzes, attempt history, faculty assignments, and remediation modules.")
    print("Only your core question_bank and flashcards will remain intact.")
    confirm = input("Are you absolutely sure you want to proceed? (Type 'yes' to confirm): ")
    
    if confirm.lower() != 'yes':
        print("Operation cancelled.")
        return

    print("\nConnecting to database...")
    try:
        conn = pymysql.connect(
            host=os.getenv('MYSQL_HOST'),
            user=os.getenv('MYSQL_USER'),
            password=os.getenv('MYSQL_PASSWORD'),
            database=os.getenv('MYSQL_DB'),
            cursorclass=pymysql.cursors.DictCursor
        )
    except Exception as e:
        print(f"Failed to connect to database: {e}")
        return

    try:
        with conn.cursor() as cursor:
            # 1. Delete all attempt responses
            print("Clearing attempt responses...")
            cursor.execute("""
                DELETE ar FROM attempt_responses ar
                JOIN quiz_attempts qa ON ar.attempt_id = qa.attempt_id
            """)
            
            # 2. Delete the quiz attempts themselves
            print("Clearing quiz attempts...")
            cursor.execute("DELETE FROM quiz_attempts")
            
            # 3. Delete the mappings connecting questions to quizzes
            print("Clearing quiz question mappings...")
            cursor.execute("DELETE FROM quiz_questions_map")
            
            # 4. Delete any pending or completed background generation tasks
            print("Clearing generation tasks...")
            cursor.execute("DELETE FROM quiz_generation_tasks")
            
            # 5. Delete the parent user_quizzes
            print("Deleting user quizzes...")
            cursor.execute("DELETE FROM user_quizzes")

            # 6. Delete faculty assignment rules
            print("Clearing faculty assignments...")
            cursor.execute("DELETE FROM assigned_quizzes")
            cursor.execute("DELETE FROM assigned_remediations")
            
            # 7. Delete remediation modules and their questions
            print("Clearing remediations...")
            cursor.execute("DELETE FROM remediation_questions")
            cursor.execute("DELETE FROM remediation_assignments")
            
        # Commit the transaction
        conn.commit()
        print("\nNUCLEAR WIPE COMPLETE: All quizzes, assignments, and histories have been successfully deleted! ✨")
        
    except Exception as e:
        print(f"\nAn error occurred: {e}")
        print("Rolling back database changes to prevent data corruption...")
        conn.rollback()
    finally:
        conn.close()
        print("Database connection closed.")

if __name__ == "__main__":
    clear_all_quizzes()