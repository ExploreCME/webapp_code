##quiz_worker.py
import os
import sys
import time
import json
import traceback
from datetime import datetime

# 1. Force unbuffered output so logs show up immediately
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

# Import your LLM generation scripts
import EOR_Extraction_optimized as eor_extractor
import Question_writing_and_verification_optomized as question_generator
import on_demand_generator
from create_remediation_assignment import start_remediation_job

# Shared MySQL configuration (host, port, TLS, retries)
from db_config import get_sync_connection as get_db_connection


def _safe_close(conn):
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass


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
                _safe_close(conn)
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
            cursor.execute("UPDATE quiz_generation_tasks SET status = 'processing', updated_at = NOW() WHERE id = %s", (task_id,))
            conn.commit()

            # Close the database connection before starting the long LLM process
            _safe_close(conn)
            conn = None

            # ==========================================
            # 2. GENERATION ROUTING
            # ==========================================

            if task_type == 'blueprint':
                generation_requests = payload.get('generation_requests')
                quiz_id = payload.get('quiz_id')
                on_demand_generator.generate_batch(username, quiz_name, generation_requests, quiz_id=quiz_id)

            elif task_type == 'document':
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
                    if filepath and os.path.exists(filepath):
                        os.remove(filepath)

            elif task_type == 'remediation_pdf':
                filepath = payload.get('filepath')
                remediation_name = payload.get('remediation_name')

                try:
                    start_remediation_job(filepath, username, remediation_name)
                finally:
                    if filepath and os.path.exists(filepath):
                        os.remove(filepath)

            # ==========================================
            # 3. COMPLETION
            # ==========================================
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("UPDATE quiz_generation_tasks SET status = 'completed', updated_at = NOW() WHERE id = %s", (task_id,))
            conn.commit()
            print(f"[{datetime.now()}] Task #{task_id} Completed!")

        except Exception as e:
            print(f"[{datetime.now()}] Task FAILED: {e}")
            traceback.print_exc()
            try:
                if task:
                    _safe_close(conn)
                    conn = get_db_connection()
                    cursor = conn.cursor()
                    cursor.execute("UPDATE quiz_generation_tasks SET status = 'failed', updated_at = NOW() WHERE id = %s", (task['id'],))
                    conn.commit()
            except Exception as db_e:
                print(f"[{datetime.now()}] Failed to update status to 'failed': {db_e}")
            if not task:
                # Connection/poll failure: back off instead of hammering the DB
                time.sleep(5)
        finally:
            _safe_close(conn)


if __name__ == "__main__":
    print("Starting Background Quiz Worker...")
    process_tasks()
