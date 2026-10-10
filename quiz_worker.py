##quiz_worker.py
##quiz_worker.py
import os
import sys
import time
import json
from db_pool import get_db_connection
from document_storage import validated_file_id, delete_document
from tempfile import TemporaryDirectory
from datetime import datetime

# 1. Force unbuffered output so logs show up in PythonAnywhere immediately
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

# Import your LLM generation scripts
import EOR_Extraction_optimized as eor_extractor
import Question_writing_and_verification_optomized as question_generator
import on_demand_generator
from create_remediation_assignment import start_remediation_job

def process_tasks():
    while True:
        conn = None
        task = None
        document_claimed = False
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
                conn = None
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
            cursor.execute("UPDATE quiz_generation_tasks SET status = 'processing', updated_at = NOW() WHERE id = %s AND status = 'pending'", (task_id,))
            claimed = cursor.rowcount
            conn.commit()
            if claimed == 0:
                task = None
                continue
            document_claimed = task_type in ('document', 'remediation_pdf')

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
                quiz_id = payload.get('quiz_id')
                num_per_topic = payload.get('num_per_topic')
                use_existing = payload.get('use_existing')

                file_id = validated_file_id(payload, username)
                with TemporaryDirectory(prefix='quiz-topics-') as directory:
                    extracted_topics_path = os.path.join(directory, 'extracted_topics.json')
                    eor_extractor.extract_topics_from_openai_file(file_id, quiz_name, extracted_topics_path)
                    question_generator.generate_questions_from_eor(
                        extracted_topics_path, quiz_name, None,
                        username, num_per_topic, use_existing, quiz_id
                    )

            elif task_type == 'remediation_pdf':
                # Sourced from faculty_routes.py -> create_remediation()
                remediation_name = payload.get('remediation_name')

                file_id = validated_file_id(payload, username)
                results = start_remediation_job(None, username, remediation_name, uploaded_file_id=file_id)
                if not results:
                    raise RuntimeError("Document generation produced no remediation modules")

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
                if conn is not None:
                    try:
                        conn.close()
                    except Exception as db_e:
                        print(f"[{datetime.now()}] Failed to close database connection: {db_e}")
                    conn = None
                if task:
                    conn = get_db_connection()
                    cursor = conn.cursor()
                    cursor.execute("UPDATE quiz_generation_tasks SET status = 'failed', updated_at = NOW() WHERE id = %s", (task['id'],))
                    conn.commit()
            except Exception as db_e:
                print(f"[{datetime.now()}] Failed to update status to 'failed': {db_e}")
            time.sleep(5)
        finally:
            if document_claimed:
                try:
                    delete_document(payload, username)
                except Exception as cleanup_error:
                    print(f"Task #{task_id}: OpenAI file cleanup failed; expiration remains the fallback: {cleanup_error}")
            # Ensure the connection is always closed at the end of every loop
            if conn is not None:
                try:
                    conn.close()
                except Exception as db_e:
                    print(f"[{datetime.now()}] Failed to close database connection: {db_e}")

if __name__ == "__main__":
    print("Starting Background Quiz Worker...")
    process_tasks()