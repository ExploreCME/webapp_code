###user_stats.py
####user_stats.py
import json
import os
import pymysql
import pymysql.cursors
import re
import time
from flask import Blueprint, render_template, session, redirect, url_for, g

# --- PERFORMANCE IMPORTS ---
from db_pool import get_db_connection

# --- IMPORT SHARED CLEANING LOGIC ---
from shared_utils import SHARED_VALID_TASK_AREAS, get_standardized_task_area

stats_bp = Blueprint('stats', __name__)

# ==========================================
# MYSQL CONNECTION POOL (SCALABLE)
# ==========================================
# Use the shared app pool instead of creating a second pool in this module.
def get_mysql_db():
    """Pulls a connection from the shared global pool."""
    if 'db' not in g:
        g.db = get_db_connection()
    return g.db

@stats_bp.teardown_request
def close_db(error):
    db = g.pop('db', None)
    if db is not None:
        try:
            db.close() # Returns connection to the pool rather than closing TCP socket
        except Exception:
            pass

# ==========================================
# IN-MEMORY CACHE FOR HEAVY GLOBAL QUERIES
# ==========================================
GLOBAL_TOP_ORGANS = None
GLOBAL_TOP_ORGANS_TIMESTAMP = 0

@stats_bp.route('/my_statistics')
def my_statistics():
    if 'username' not in session:
        return redirect(url_for('index'))
    user_identifier = session['username']  # clerk_id / username

    mysql_conn = get_mysql_db()
    cursor = mysql_conn.cursor()

    # ---------------------------------------------------------
    # 1. FETCH DISPLAY NAME (FAST PATH: SESSION CACHE)
    # ---------------------------------------------------------
    display_name = session.get('display_name')

    if not display_name:
        try:
            cursor.execute('SELECT first_name, last_name FROM users WHERE clerk_id = %s', (user_identifier,))
            user_record = cursor.fetchone()

            if user_record and user_record.get('first_name'):
                display_name = f"{user_record['first_name']} {user_record['last_name']}".strip()
            else:
                display_name = session.get('first_name', session.get('name', 'Student'))

            # Cache in session to prevent DB hit on next reload
            session['display_name'] = display_name
        except Exception as e:
            print(f"Error fetching display name from MySQL: {e}")
            display_name = session.get('first_name', session.get('name', 'Student'))

    # ---------------------------------------------------------
    # 2. MYSQL QUERIES FOR STATS & QUESTION DATA
    # ---------------------------------------------------------
    # --- CACHED TOP 14 ORGAN SYSTEMS GLOBALLY ---
    global GLOBAL_TOP_ORGANS, GLOBAL_TOP_ORGANS_TIMESTAMP

    # Cache the global organ query for 1 hour to prevent DB overload
    if not GLOBAL_TOP_ORGANS or (time.time() - GLOBAL_TOP_ORGANS_TIMESTAMP > 3600):
        cursor.execute("""
            SELECT organ_system_norm AS organ_system
            FROM question_bank
            WHERE organ_system_norm IS NOT NULL AND organ_system_norm != ''
            GROUP BY organ_system_norm
            ORDER BY COUNT(*) DESC
            LIMIT 14
        """)
        GLOBAL_TOP_ORGANS = [row['organ_system'] for row in cursor.fetchall() if row['organ_system']]
        GLOBAL_TOP_ORGANS_TIMESTAMP = time.time()

    global_top_14_organs = GLOBAL_TOP_ORGANS

    # --- TOTAL OVERALL USER STATS ---
    cursor.execute('''
        SELECT
            COUNT(ar.id) as total_attempted,
            SUM(CASE WHEN ar.is_correct = 1 THEN 1 ELSE 0 END) as total_correct
        FROM attempt_responses ar
        JOIN quiz_attempts qa ON ar.attempt_id = qa.attempt_id
        WHERE qa.username = %s
    ''', (user_identifier,))
    overall = cursor.fetchone() or {}
    total_attempted = int(overall.get('total_attempted') or 0)
    total_correct = int(overall.get('total_correct') or 0)

    # --- CROSS-TABULATED STATS (Organ System x Task Area) ---
    cursor.execute('''
        SELECT
            COALESCE(qb.organ_system_norm, ra.organ_system_norm) as organ_system,
            COALESCE(qb.task_area_norm, ra.task_area_norm) as task_area,
            COUNT(ar.id) as attempted,
            SUM(CASE WHEN ar.is_correct = 1 THEN 1 ELSE 0 END) as correct
        FROM attempt_responses ar
        JOIN quiz_attempts qa ON ar.attempt_id = qa.attempt_id
        LEFT JOIN question_bank qb ON ar.question_id = qb.id
        LEFT JOIN remediation_questions rq ON ar.question_id = rq.id
        LEFT JOIN remediation_assignments ra ON rq.assignment_id = ra.id
        WHERE qa.username = %s
        GROUP BY
            COALESCE(qb.organ_system_norm, ra.organ_system_norm),
            COALESCE(qb.task_area_norm, ra.task_area_norm)
    ''', (user_identifier,))
    raw_data = cursor.fetchall()

    # ---------------------------------------------------------
    # 3. BUILD NESTED STATS DICTIONARY
    # ---------------------------------------------------------
    stats_data = {
        "organ_systems": {},
        "task_areas": {}
    }

    for row in raw_data:
        organ = row['organ_system'] if row['organ_system'] else "Unknown Organ System"
        task = get_standardized_task_area(row['task_area'])
        attempted = int(row['attempted'] or 0)
        correct = int(row['correct'] or 0)

        # Force any organ not in the top 14 list into "Miscellaneous"
        if organ not in global_top_14_organs:
            organ = "Miscellaneous"

        # Plot Organ Systems
        if organ not in stats_data["organ_systems"]:
            stats_data["organ_systems"][organ] = {"attempted": 0, "correct": 0, "breakdown": {}}

        stats_data["organ_systems"][organ]["attempted"] += attempted
        stats_data["organ_systems"][organ]["correct"] += correct

        # Safely increment breakdown as multiple raw rows might now map to 'Miscellaneous'
        if task not in stats_data["organ_systems"][organ]["breakdown"]:
            stats_data["organ_systems"][organ]["breakdown"][task] = {"attempted": 0, "correct": 0}
        stats_data["organ_systems"][organ]["breakdown"][task]["attempted"] += attempted
        stats_data["organ_systems"][organ]["breakdown"][task]["correct"] += correct

        # Plot Task Areas if in 7 valid areas
        if task in SHARED_VALID_TASK_AREAS:
            if task not in stats_data["task_areas"]:
                stats_data["task_areas"][task] = {"attempted": 0, "correct": 0, "breakdown": {}}

            stats_data["task_areas"][task]["attempted"] += attempted
            stats_data["task_areas"][task]["correct"] += correct

            if organ not in stats_data["task_areas"][task]["breakdown"]:
                stats_data["task_areas"][task]["breakdown"][organ] = {"attempted": 0, "correct": 0}
            stats_data["task_areas"][task]["breakdown"][organ]["attempted"] += attempted
            stats_data["task_areas"][task]["breakdown"][organ]["correct"] += correct

    return render_template(
        'my_statistics.html',
        username=display_name,
        total_attempted=total_attempted,
        total_correct=total_correct,
        stats_data_json=json.dumps(stats_data)
    )
