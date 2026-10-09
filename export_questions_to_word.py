import os
import pymysql
import pymysql.cursors
from dotenv import load_dotenv

# 1. Load DB credentials
load_dotenv('/home/ps51632/mysite/explorecme/.env')

MYSQL_HOST = os.getenv('MYSQL_HOST')
MYSQL_USER = os.getenv('MYSQL_USER')
MYSQL_PASSWORD = os.getenv('MYSQL_PASSWORD')
MYSQL_DB = os.getenv('MYSQL_DB')

def get_db_connection():
    """Establish and return a connection to the MySQL database."""
    return pymysql.connect(
        host=MYSQL_HOST,
        user=MYSQL_USER,
        password=MYSQL_PASSWORD,
        database=MYSQL_DB,
        cursorclass=pymysql.cursors.DictCursor
    )

def export_questions():
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # Fetch distinct organ systems available in the database
        cursor.execute("""
            SELECT DISTINCT organ_system 
            FROM question_bank 
            WHERE organ_system IS NOT NULL AND organ_system != '' 
            ORDER BY organ_system ASC
        """)
        systems = [row['organ_system'] for row in cursor.fetchall()]

        if not systems:
            print("No organ systems found in the database.")
            return

        # Prompt user to select an organ system
        print("\n--- Available Organ Systems ---")
        for idx, sys in enumerate(systems, 1):
            print(f"{idx}. {sys}")

        choice_str = input(f"\nEnter the NUMBER of the Organ System to export (1-{len(systems)}): ").strip()
        
        try:
            choice_idx = int(choice_str)
            if choice_idx < 1 or choice_idx > len(systems):
                raise ValueError
        except ValueError:
            print(f"Error: '{choice_str}' is not a valid selection.")
            return

        selected_system = systems[choice_idx - 1]
        print(f"\nFetching questions for: {selected_system}...")

        # Retrieve questions matching the selected organ system
        cursor.execute("""
            SELECT 
                id, question_stem, 
                answer_choice_A, answer_choice_B, answer_choice_C, answer_choice_D, answer_choice_E, 
                correct_answer, explanation
            FROM question_bank
            WHERE organ_system = %s
        """, (selected_system,))

        questions = cursor.fetchall()

        if not questions:
            print(f"No questions found for {selected_system}.")
            return

        # Format a safe filename (e.g., "Cardiovascular_System_Questions.txt")
        safe_filename = selected_system.replace(" ", "_").replace("/", "_") + "_Questions.txt"

        # Write to the text file
        with open(safe_filename, 'w', encoding='utf-8') as f:
            f.write(f"--- QUESTION BANK EXPORT ---\n")
            f.write(f"Organ System: {selected_system}\n")
            f.write(f"Total Questions: {len(questions)}\n")
            f.write("=" * 80 + "\n\n")

            for q_num, q in enumerate(questions, 1):
                f.write(f"Question {q_num}  [ID: {q['id']}]\n")
                f.write(f"{q['question_stem']}\n\n")
                
                f.write(f"A. {q['answer_choice_A']}\n")
                f.write(f"B. {q['answer_choice_B']}\n")
                f.write(f"C. {q['answer_choice_C']}\n")
                f.write(f"D. {q['answer_choice_D']}\n")
                
                if q.get('answer_choice_E'):
                    f.write(f"E. {q['answer_choice_E']}\n")
                    
                f.write(f"\nCorrect Answer: {q['correct_answer']}\n")
                
                if q.get('explanation'):
                    f.write(f"Explanation: {q['explanation']}\n")
                
                f.write("\n" + "-" * 80 + "\n\n")

        print(f"Success! {len(questions)} questions exported to '{safe_filename}'.")
        print("You can now open this text file and copy/paste it into Microsoft Word.")

    except Exception as e:
        print(f"An error occurred: {e}")
    finally:
        if conn:
            conn.close()

if __name__ == "__main__":
    export_questions()