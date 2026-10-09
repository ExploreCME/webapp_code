##quiz_worker.py
##quiz_worker.py
import os
import sys
import time
import json
import pymysql
import pymysql.cursors
from datetime import datetime
from dotenv import load_dotenv

# 1. Force unbuffered output so logs show up in PythonAnywhere immediately
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

# 2. Force load environment variables for the background worker
load_dotenv('/home/ps51632/mysite/explorecme/.env')

# Import your LLM generation scripts
import EOR_Extraction_optimized as eor_extractor
import Question_writing_and_verification_optomized as question_generator
import on_demand_generator
from create_remediation_assignment import start_remediation_job

def get_db_connection():
    return pymysql.connect(
        host=os.getenv('MYSQL_HOST'),
        user=os.getenv('MYSQL_USER'),
        password=os.getenv('MYSQL_PASSWORD'),
        database=os.getenv('MYSQL_DB'),
        cursorclass=pymysql.cursors.DictCursor
    )

def process_tasks():
    while True:
        conn = None
        task = None
        try:
            # 1. Connect to fetch one pending task
            conn = get_db_connection()
            cursor = conn.cursor()

            cursor.execute('''
                SELECT id, username, quiz_name, task_type, payload
                FROM quiz_generation_tasks
                WHERE status = 'pending'
                ORDER BY created_at ASC LIMIT 1
            ''')
            task = cursor.fetchone()

            if not task:
                conn.close()
                print(f"[{datetime.now()}] Queue empty. Sleeping for 5s...")
                time.sleep(5)
                continue

            task_id = task['id']
            username = task['username']
            quiz_name = task['quiz_name']
            task_type = task['task_type']
            payload = json.loads(task['payload'])

            print(f"[{datetime.now()}] Processing Task #{task_id}: {quiz_name} ({task_type})")

            # Mark as processing
            cursor.execute("UPDATE quiz_generation_tasks SET status = 'processing', updated_at = NOW() WHERE id = %s", (task_id,))
            conn.commit()

            # 👇 CRITICAL FIX: Close the database connection before starting the long LLM process!
            conn.close()
            conn = None

            # ==========================================
            # 2. GENERATION ROUTING
            # ==========================================

            if task_type == 'blueprint':
                # Sourced from flask_app.py -> build_quiz_batch()
                generation_requests = payload.get('generation_requests')
                quiz_id = payload.get('quiz_id')

                # Make sure your generate_batch function accepts quiz_id so it maps questions correctly!
                on_demand_generator.generate_batch(username, quiz_name, generation_requests, quiz_id=quiz_id)

            elif task_type == 'document':
                # Sourced from flask_app.py -> generate_from_document()
                filepath = payload.get('filepath')
                quiz_id = payload.get('quiz_id')
                extracted_topics_path = payload.get('extracted_topics_path')
                final_output_json_path = payload.get('final_output_json_path')
                num_per_topic = payload.get('num_per_topic')
                use_existing = payload.get('use_existing')

                try:
                    eor_extractor.extract_topics_from_eor_file(filepath, quiz_name, extracted_topics_path)
                    question_generator.generate_questions_from_eor(
                        extracted_topics_path, quiz_name, final_output_json_path,
                        username, num_per_topic, use_existing, quiz_id
                    )
                finally:
                    # Clean up the PDF to prevent disk quota exhaustion
                    if filepath and os.path.exists(filepath):
                        os.remove(filepath)

            elif task_type == 'remediation_pdf':
                # Sourced from faculty_routes.py -> create_remediation()
                filepath = payload.get('filepath')
                remediation_name = payload.get('remediation_name')

                try:
                    start_remediation_job(filepath, username, remediation_name)
                finally:
                    # Clean up the PDF to prevent disk quota exhaustion
                    if filepath and os.path.exists(filepath):
                        os.remove(filepath)

            # ==========================================
            # 3. COMPLETION
            # ==========================================

            # 👇 CRITICAL FIX: Reconnect to the database to mark the task as complete
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("UPDATE quiz_generation_tasks SET status = 'completed', updated_at = NOW() WHERE id = %s", (task_id,))
            conn.commit()
            print(f"[{datetime.now()}] Task #{task_id} Completed!")

        except Exception as e:
            print(f"[{datetime.now()}] Task FAILED: {e}")
            try:
                # Reconnect safely to record the failure
                if not conn or not conn.open:
                    conn = get_db_connection()
                cursor = conn.cursor()
                if task:
                    cursor.execute("UPDATE quiz_generation_tasks SET status = 'failed', updated_at = NOW() WHERE id = %s", (task['id'],))
                    conn.commit()
            except Exception as db_e:
                print(f"[{datetime.now()}] Failed to update status to 'failed': {db_e}")
        finally:
            # Ensure the connection is always closed at the end of every loop
            if conn and conn.open:
                conn.close()

if __name__ == "__main__":
    print("Starting Background Quiz Worker...")
    process_tasks()