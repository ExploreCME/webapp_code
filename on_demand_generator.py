###on_demand_generator.py
import os
import json
import aiomysql
import asyncio
import uuid
import random
from datetime import datetime
from dotenv import load_dotenv

# 👇 IMPORT THE NEW BATCH ENGINE
from Question_writing_and_verification_optomized import (
    ConversationManager,
    process_topic_batch,
    standardize_categories
)

# Load environment variables
load_dotenv('/home/ps51632/mysite/explorecme/.env')

MYSQL_HOST = os.getenv('MYSQL_HOST')
MYSQL_USER = os.getenv('MYSQL_USER')
MYSQL_PASSWORD = os.getenv('MYSQL_PASSWORD')
MYSQL_DB = os.getenv('MYSQL_DB')

# ==========================================
# MYSQL CONNECTION HELPER
# ==========================================
async def get_db_connection():
    return await aiomysql.connect(
        host=MYSQL_HOST,
        user=MYSQL_USER,
        password=MYSQL_PASSWORD,
        db=MYSQL_DB,
        cursorclass=aiomysql.DictCursor
    )

# ==========================================
# DATABASE ROUTING LOGIC (SINGLE QUESTIONS)
# ==========================================
async def save_generated_question_to_mysql(q_data, username, quiz_name):
    conn = await get_db_connection()
    question_id_to_map = None

    try:
        async with conn.cursor() as cursor:
            current_timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            question_id = uuid.uuid4().hex

            # 1. Insert into Central Question Bank
            await cursor.execute('''
                INSERT IGNORE INTO question_bank (
                    id, organ_system, task_area, topic_area, question_stem,
                    answer_choice_A, answer_choice_B, answer_choice_C,
                    answer_choice_D, answer_choice_E, correct_answer,
                    explanation, difficulty_rating, created_date,
                    global_thumbs_up, global_thumbs_down
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 0, 0)
            ''', (
                question_id, q_data["clean_organ"], q_data["clean_task"], q_data["topic_area"],
                q_data["question_stem"], q_data["answer_choice_A"], q_data["answer_choice_B"],
                q_data["answer_choice_C"], q_data["answer_choice_D"], q_data["answer_choice_E"],
                q_data["correct_answer"], q_data["explanation"], q_data["difficulty_rating"],
                current_timestamp
            ))

            question_id_to_map = question_id

            if cursor.rowcount == 0:
                await cursor.execute('SELECT id FROM question_bank WHERE question_stem = %s', (q_data["question_stem"],))
                existing_row = await cursor.fetchone()
                if existing_row:
                    question_id_to_map = existing_row['id']

            # 2. Get or Create the Quiz
            await cursor.execute('SELECT quiz_id FROM user_quizzes WHERE username = %s AND quiz_name = %s', (username, quiz_name))
            quiz_row = await cursor.fetchone()
            if quiz_row:
                quiz_id = quiz_row['quiz_id']
            else:
                quiz_id = uuid.uuid4().hex
                await cursor.execute('''
                    INSERT IGNORE INTO user_quizzes (quiz_id, username, quiz_name, created_date)
                    VALUES (%s, %s, %s, %s)
                ''', (quiz_id, username, quiz_name, current_timestamp))

            # 3. Map to Quiz
            await cursor.execute('''
                INSERT IGNORE INTO quiz_questions_map (quiz_id, question_id)
                VALUES (%s, %s)
            ''', (quiz_id, question_id_to_map))

        await conn.commit()
    except Exception as e:
        print(f"DEBUG: MySQL Error in on-demand generation: {e}")
    finally:
        conn.close()

    return question_id_to_map

# ==========================================
# ASYNC WRAPPERS
# ==========================================
async def async_generate_single(username, quiz_name, organ_system, task_area, topic_area):
    conv_manager = ConversationManager(model="gpt-6.1-sol")
    conv_manager_verify = ConversationManager(model="gpt-6.1-sol")

    item_dict = {
        "organ_system": organ_system,
        "task_area": task_area,
        "topic_area": topic_area
    }

    results = await process_topic_batch(item_dict, 1, conv_manager, conv_manager_verify)

    await conv_manager.client.close()
    await conv_manager_verify.client.close()

    if results:
        q_data = results[0]
        raw_organ = q_data.get("organ_system") or organ_system
        raw_task = q_data.get("task_area") or task_area
        clean_organ, clean_task = standardize_categories(raw_organ, raw_task)
        q_data["clean_organ"] = clean_organ
        q_data["clean_task"] = clean_task
        return await save_generated_question_to_mysql(q_data, username, quiz_name)

    return None

async def async_generate_batch(username, quiz_name, generation_requests, quiz_id=None):
    conv_manager = ConversationManager(model="gpt-6.1-sol")
    conv_manager_verify = ConversationManager(model="gpt-6.1-sol")

    semaphore = asyncio.Semaphore(5)
    successful_questions = []

    grouped_requests = {}
    for req in generation_requests:
        key = (
            str(req.get('organ_system', '')).strip(),
            str(req.get('task_area', '')).strip(),
            str(req.get('topic_area', '')).strip()
        )
        if key not in grouped_requests:
            grouped_requests[key] = {
                "item_dict": req,
                "count": 0
            }
        grouped_requests[key]["count"] += 1

    print(f"[{datetime.now()}] On-Demand Batch: Consolidated {len(generation_requests)} requests into {len(grouped_requests)} unique topic batches.")

    async def safe_batch_process(item_dict, count):
        async with semaphore:
            res_list = await process_topic_batch(item_dict, count, conv_manager, conv_manager_verify)
            cleaned_list = []
            for res in res_list:
                raw_organ = res.get("organ_system")
                if not raw_organ or str(raw_organ).lower() in ["str", "string", "none"]:
                    raw_organ = item_dict.get("organ_system")

                raw_task = res.get("task_area")
                if not raw_task or str(raw_task).lower() in ["str", "string", "none"]:
                    raw_task = item_dict.get("task_area")

                clean_organ, clean_task = standardize_categories(raw_organ, raw_task)
                res["clean_organ"] = clean_organ
                res["clean_task"] = clean_task
                cleaned_list.append(res)
            return cleaned_list

    tasks = []
    for group in grouped_requests.values():
        tasks.append(safe_batch_process(group["item_dict"], group["count"]))

    for future in asyncio.as_completed(tasks):
        try:
            res_list = await future
            if res_list:
                successful_questions.extend(res_list)
        except Exception as e:
            print(f"DEBUG: Error in batch task: {e}")

    await conv_manager.client.close()
    await conv_manager_verify.client.close()

    if successful_questions:
        conn = await get_db_connection()
        try:
            async with conn.cursor() as cursor:
                current_timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

                if not quiz_id:
                    await cursor.execute('SELECT quiz_id FROM user_quizzes WHERE username = %s AND quiz_name = %s', (username, quiz_name))
                    quiz_row = await cursor.fetchone()
                    if quiz_row:
                        quiz_id = quiz_row['quiz_id']
                    else:
                        quiz_id = uuid.uuid4().hex
                        await cursor.execute('''
                            INSERT IGNORE INTO user_quizzes (quiz_id, username, quiz_name, created_date)
                            VALUES (%s, %s, %s, %s)
                        ''', (quiz_id, username, quiz_name, current_timestamp))

                print(f"[{datetime.now()}] Bulk Inserting {len(successful_questions)} new AI questions to Database.")

                for q in successful_questions:
                    q['question_id'] = uuid.uuid4().hex
                    q['created_date'] = current_timestamp

                insert_data = [
                    (
                        q["question_id"], q["clean_organ"], q["clean_task"], q["topic_area"], q["question_stem"],
                        q["answer_choice_A"], q["answer_choice_B"], q["answer_choice_C"], q["answer_choice_D"], q["answer_choice_E"],
                        q["correct_answer"], q["explanation"], q["difficulty_rating"], q["created_date"]
                    ) for q in successful_questions
                ]

                await cursor.executemany('''
                    INSERT IGNORE INTO question_bank (
                        id, organ_system, task_area, topic_area, question_stem,
                        answer_choice_A, answer_choice_B, answer_choice_C,
                        answer_choice_D, answer_choice_E, correct_answer,
                        explanation, difficulty_rating, created_date,
                        global_thumbs_up, global_thumbs_down
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 0, 0)
                ''', insert_data)

                print(f"[{datetime.now()}] Bulk Mapping all questions to Quiz ID: {quiz_id}")
                mapping_data = []
                for q in successful_questions:
                    question_id_to_map = q["question_id"]

                    if cursor.rowcount < len(successful_questions):
                        await cursor.execute('SELECT id FROM question_bank WHERE question_stem = %s', (q["question_stem"],))
                        existing_row = await cursor.fetchone()
                        if existing_row:
                            question_id_to_map = existing_row['id']

                    mapping_data.append((quiz_id, question_id_to_map))

                if mapping_data:
                    await cursor.executemany('''
                        INSERT IGNORE INTO quiz_questions_map (quiz_id, question_id)
                        VALUES (%s, %s)
                    ''', mapping_data)

            await conn.commit()
        except Exception as e:
            print(f"DEBUG: MySQL Error in bulk on-demand generation: {e}")
        finally:
            conn.close()

# ==========================================
# SYNCHRONOUS ENTRY POINTS (CALLED BY FLASK)
# ==========================================
def generate_single_question(username, quiz_name, organ_system, task_area, topic_area):
    return asyncio.run(async_generate_single(username, quiz_name, organ_system, task_area, topic_area))

def generate_batch(username, quiz_name, generation_requests, quiz_id=None):
    asyncio.run(async_generate_batch(username, quiz_name, generation_requests, quiz_id))