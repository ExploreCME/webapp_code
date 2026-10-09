###fill_database_pa.py
import os
import json
import pymysql
import pymysql.cursors
import asyncio
import uuid
from datetime import datetime
from dotenv import load_dotenv

# Import the shared batch engine and helper logic from your optimized script
from Question_writing_and_verification_optomized import (
    ConversationManager,
    process_topic_batch,
    standardize_categories
)

# 1. Point exactly to where your .env file lives
load_dotenv('/home/ps51632/mysite/explorecme/.env')

# 2. Securely fetch the DB credentials from the .env file
MYSQL_HOST = os.getenv('MYSQL_HOST')
MYSQL_USER = os.getenv('MYSQL_USER')
MYSQL_PASSWORD = os.getenv('MYSQL_PASSWORD')
MYSQL_DB = os.getenv('MYSQL_DB')

# Standard NCCPA Task Areas (Canonical Standard)
PANCE_TASK_AREAS = [
    "History Taking and Performing Physical Examination",
    "Using Diagnostic and Laboratory Studies",
    "Formulating the Most Likely Diagnosis",
    "Health Maintenance, Patient Education, and Preventive Measures",
    "Clinical Intervention",
    "Pharmaceutical Therapeutics",
    "Applying Foundational Scientific Concepts"
]

async def generate_questions_by_organ_system(json_path, username, quiz_name):
    try:
        with open(json_path, 'r') as f:
            topics_data = json.load(f)
    except FileNotFoundError:
        print(f"Error: Could not find '{json_path}'.")
        return

    print("\n--- Available Organ Systems ---")
    systems_list = list(topics_data.keys())
    for idx, sys_name in enumerate(systems_list, 1):
        print(f"{idx}. {sys_name}")

    choice_str = input(f"\nEnter the NUMBER of the Organ System you want to process (1-{len(systems_list)}): ").strip()
    try:
        choice_idx = int(choice_str)
        if choice_idx < 1 or choice_idx > len(systems_list):
            raise ValueError
    except ValueError:
        print(f"Error: '{choice_str}' is not a valid selection.")
        return

    selected_system = systems_list[choice_idx - 1]
    topics_list = topics_data[selected_system]

    print(f"\nProcessing Organ System: '{selected_system}' with {len(topics_list)} topics across all 7 task areas.")
    print("Generating exactly 1 question per task area (7 total questions per topic)...")

    conv_manager = ConversationManager(model="gpt-6.1-sol")
    conv_manager_verify = ConversationManager(model="gpt-6.1-sol")

    successful_questions = []
    semaphore = asyncio.Semaphore(5)

    async def safe_process_topic(topic_name):
        # Create an item dict for each topic containing all 7 task areas so we can generate a batch of 7
        item_dict = {
            "organ_system": selected_system,
            "topic_area": topic_name,
            "task_area": None  # Will be requested per batch across the task areas
        }

        topic_questions = []
        # Loop through each of the 7 PANCE task areas to ensure 1 question per task area per topic
        for task_area in PANCE_TASK_AREAS:
            async with semaphore:
                task_item_dict = {
                    "organ_system": selected_system,
                    "topic_area": topic_name,
                    "task_area": task_area
                }
                # process_topic_batch handles generation + 3-round verification for num_needed=1
                res_list = await process_topic_batch(
                    item_dict=task_item_dict,
                    num_needed=1,
                    conv_manager=conv_manager,
                    conv_manager_verify=conv_manager_verify
                )
                for res in res_list:
                    clean_organ, clean_task = standardize_categories(
                        res.get("organ_system", selected_system),
                        res.get("task_area", task_area)
                    )
                    topic_questions.append({
                        "question_id": uuid.uuid4().hex,
                        "clean_organ": clean_organ,
                        "clean_task": clean_task,
                        "topic_area": res.get("topic_area", topic_name),
                        "question_stem": res.get("question_stem"),
                        "answer_choice_A": res.get("answer_choice_A"),
                        "answer_choice_B": res.get("answer_choice_B"),
                        "answer_choice_C": res.get("answer_choice_C"),
                        "answer_choice_D": res.get("answer_choice_D"),
                        "answer_choice_E": res.get("answer_choice_E"),
                        "correct_answer": res.get("correct_answer"),
                        "explanation": res.get("explanation"),
                        "difficulty_rating": res.get("difficulty_rating", 5),
                        "created_date": datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    })
        return topic_questions

    generation_tasks = [safe_process_topic(topic) for topic in topics_list]

    for future in asyncio.as_completed(generation_tasks):
        try:
            res_list = await future
            if res_list:
                successful_questions.extend(res_list)
        except Exception as e:
            print(f"DEBUG: Task error: {e}")

    # Close OpenAI client sessions cleanly
    await conv_manager.client.close()
    await conv_manager_verify.client.close()

    # ==========================================
    # MYSQL BATCH INSERTION & MAPPING
    # ==========================================
    if successful_questions:
        print(f"\nWriting {len(successful_questions)} verified questions to MySQL...")
        conn = None
        try:
            conn = pymysql.connect(
                host=MYSQL_HOST,
                user=MYSQL_USER,
                password=MYSQL_PASSWORD,
                database=MYSQL_DB,
                cursorclass=pymysql.cursors.DictCursor
            )
            cursor = conn.cursor()

            # 1. Get or Create Quiz
            cursor.execute('SELECT quiz_id FROM user_quizzes WHERE username = %s AND quiz_name = %s', (username, quiz_name))
            quiz_row = cursor.fetchone()
            if quiz_row:
                quiz_id = quiz_row['quiz_id']
            else:
                quiz_id = uuid.uuid4().hex
                cursor.execute('''
                    INSERT IGNORE INTO user_quizzes (quiz_id, username, quiz_name, created_date)
                    VALUES (%s, %s, %s, %s)
                ''', (quiz_id, username, quiz_name, datetime.now().strftime('%Y-%m-%d %H:%M:%S')))

            # 2. Iterate through and map new questions
            for q in successful_questions:
                cursor.execute('''
                    INSERT IGNORE INTO question_bank (
                        id, organ_system, task_area, topic_area, question_stem,
                        answer_choice_A, answer_choice_B, answer_choice_C,
                        answer_choice_D, answer_choice_E, correct_answer,
                        explanation, difficulty_rating, created_date,
                        global_thumbs_up, global_thumbs_down
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 0, 0)
                ''', (
                    q["question_id"], q["clean_organ"], q["clean_task"], q["topic_area"], q["question_stem"],
                    q["answer_choice_A"], q["answer_choice_B"], q["answer_choice_C"], q["answer_choice_D"], q["answer_choice_E"],
                    q["correct_answer"], q["explanation"], q["difficulty_rating"], q["created_date"]
                ))
                question_id_to_map = q["question_id"]

                # If IGNORED because stem was a perfect duplicate, grab the existing question's ID to map instead
                if cursor.rowcount == 0:
                    cursor.execute('SELECT id FROM question_bank WHERE question_stem = %s', (q["question_stem"],))
                    existing_row = cursor.fetchone()
                    if existing_row:
                        question_id_to_map = existing_row['id']

                # 3. Map to Quiz
                cursor.execute('''
                    INSERT IGNORE INTO quiz_questions_map (quiz_id, question_id)
                    VALUES (%s, %s)
                ''', (quiz_id, question_id_to_map))

            conn.commit()
            print(f"DEBUG: Successfully mapped {len(successful_questions)} questions to user quiz in MySQL.")
        except Exception as e:
            print(f"DEBUG: MySQL Error: {e}")
        finally:
            if conn:
                conn.close()
    else:
        print("DEBUG: No questions were successfully generated and validated.")
    print("All tasks completed successfully!")

if __name__ == "__main__":
    JSON_SOURCE = "topics_by_organ_system.json"
    USERNAME = "explorecme"
    QUIZ_NAME = "organ_system_batch_quiz"
    asyncio.run(generate_questions_by_organ_system(JSON_SOURCE, USERNAME, QUIZ_NAME))