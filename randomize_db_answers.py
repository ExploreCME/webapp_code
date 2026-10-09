#!/usr/bin/env python3
import os
import random
import pymysql
from dotenv import load_dotenv

# Load environment variables
load_dotenv('/home/ps51632/mysite/explorecme/.env')

def shuffle_answers_in_table(cursor, table_name):
    print(f"Fetching questions from {table_name}...")
    cursor.execute(f"""
        SELECT id, answer_choice_A, answer_choice_B, answer_choice_C,
               answer_choice_D, answer_choice_E, correct_answer
        FROM {table_name}
    """)
    questions = cursor.fetchall()

    updates = []
    letters = ['A', 'B', 'C', 'D', 'E']

    for q in questions:
        choices = {
            'A': q['answer_choice_A'],
            'B': q['answer_choice_B'],
            'C': q['answer_choice_C'],
            'D': q['answer_choice_D'],
            'E': q['answer_choice_E']
        }

        old_correct_letter = (q['correct_answer'] or '').strip().upper()

        # Guard clause for invalid or missing correct answer data
        if old_correct_letter not in choices:
            print(f"  [Warning] Skipping ID {q['id']} in {table_name} - Invalid correct_answer: '{old_correct_letter}'")
            continue

        correct_text = choices[old_correct_letter]

        # Scramble the values
        choice_texts = list(choices.values())
        random.shuffle(choice_texts)

        # Find the new letter for the correct text
        new_correct_letter = letters[choice_texts.index(correct_text)]

        # Append to our batch update list
        updates.append((
            choice_texts[0],  # new A
            choice_texts[1],  # new B
            choice_texts[2],  # new C
            choice_texts[3],  # new D
            choice_texts[4],  # new E
            new_correct_letter,
            q['id']
        ))

    if updates:
        print(f"Shuffling choices for {len(updates)} questions in {table_name}...")
        cursor.executemany(f"""
            UPDATE {table_name}
            SET answer_choice_A = %s,
                answer_choice_B = %s,
                answer_choice_C = %s,
                answer_choice_D = %s,
                answer_choice_E = %s,
                correct_answer = %s
            WHERE id = %s
        """, updates)
        print(f"Successfully updated {table_name}.")
    else:
        print(f"No valid questions to update in {table_name}.")

def main():
    # Connect to the database
    print("Connecting to database...")
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
            # Shuffle main question bank
            shuffle_answers_in_table(cursor, 'question_bank')

            # Shuffle remediation questions
            shuffle_answers_in_table(cursor, 'remediation_questions')

        # Commit the transaction
        conn.commit()
        print("\nAll database changes committed successfully! ✨")

    except Exception as e:
        print(f"\nAn error occurred: {e}")
        print("Rolling back database changes to prevent data corruption...")
        conn.rollback()
    finally:
        conn.close()
        print("Database connection closed.")

if __name__ == "__main__":
    main()