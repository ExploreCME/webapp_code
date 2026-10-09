##faculty_routes.py
##faculty_routes.py
from flask import Blueprint, render_template, request, redirect, url_for, flash, session, current_app, send_file, Response
from display_database import login_required
from werkzeug.utils import secure_filename
import pymysql
import pymysql.cursors
import os
import json
import jwt
import traceback
from dotenv import load_dotenv
import io
import csv
import asyncio
import random
import urllib.parse
from reportlab.lib.pagesizes import letter, landscape
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib import colors

# --- PERFORMANCE IMPORTS ---
from db_pool import get_db_connection  # <-- Uses the new global pool

# Import the new statistical report engine (Fail gracefully if not created yet)
try:
    from report_engine import ReportEngine
except ImportError:
    ReportEngine = None

# --- IMPORT SHARED CLEANING LOGIC & UNIQUE CHECK ---
from shared_utils import SHARED_VALID_TASK_AREAS, is_assignment_name_unique

load_dotenv('/home/ps51632/mysite/explorecme/.env')

faculty_bp = Blueprint('faculty', __name__)

# ==========================================
# MYSQL DATABASE CONNECTION POOL (SCALABLE)
# ==========================================
def get_mysql_connection():
    """Pulls a connection directly from the global pool. Calling conn.close() returns it to the pool."""
    return get_db_connection()

def is_user_faculty(username):
    """Helper function to verify if the current user has faculty privileges. Cached in session."""
    # Fast path: Check session memory
    if session.get('is_faculty'):
        return True

    # Fallback: Query MySQL DB and cache in session
    conn = get_mysql_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT is_faculty FROM users WHERE clerk_id = %s", (username,))
            result = cursor.fetchone()

            if result and result.get('is_faculty') is not None:
                val = str(result.get('is_faculty')).strip().upper()
                is_fac = val in ['1', 'Y', 'YES', 'TRUE']
                session['is_faculty'] = is_fac
                return is_fac
    finally:
        conn.close()

    return False

def get_display_name(first_name, last_name, email):
    """Smart parser to always return a clean name, even if DB fields are missing."""
    fn = str(first_name).strip() if first_name and str(first_name).lower() != 'none' else ''
    ln = str(last_name).strip() if last_name and str(last_name).lower() != 'none' else ''

    if ln and fn:
        full_name = f"{ln}, {fn}"
    elif ln:
        full_name = ln
    elif fn:
        full_name = fn
    else:
        full_name = ""

    if full_name:
        return full_name

    if email and '@' in email:
        parts = email.split('@')[0].replace('.', ' ').replace('_', ' ').title().split()
        if len(parts) > 1:
            return f"{parts[-1]}, {' '.join(parts[:-1])}"
        return " ".join(parts)

    return email or "Unknown Student"

@faculty_bp.route('/faculty_dashboard')
@login_required
def dashboard():
    username = session.get('username')
    if not is_user_faculty(username):
        flash("Unauthorized access. Faculty privileges required.", "error")
        return redirect(url_for('index'))

    return render_template('faculty_dashboard.html')

# ==========================================
# FACULTY HUB ROUTES
# ==========================================
@faculty_bp.route('/faculty/generate_hub')
@login_required
def generate_hub():
    username = session.get('username')
    if not is_user_faculty(username):
        return redirect(url_for('index'))
    return render_template('faculty_generate_hub.html')

@faculty_bp.route('/faculty/reports_hub')
@login_required
def reports_hub():
    try:
        username = session.get('username')
        if not is_user_faculty(username):
            return redirect(url_for('index'))
        return render_template('faculty_reports_hub.html')
    except Exception as e:
        import traceback
        return f"<div style='padding: 40px; color: red;'><h3>Crash in reports_hub:</h3><pre>{traceback.format_exc()}</pre></div>"

# ==========================================
# REMEDIATION MANAGEMENT ROUTES
# ==========================================
@faculty_bp.route('/faculty/remediation_menu')
@login_required
def remediation_menu():
    username = session.get('username')
    if not is_user_faculty(username):
        flash("Unauthorized access. Faculty privileges required.", "error")
        return redirect(url_for('index'))
    return render_template('faculty_remediation_menu.html')

@faculty_bp.route('/faculty/create_remediation', methods=['GET', 'POST'])
@login_required
def create_remediation():
    username = session.get('username')
    if not is_user_faculty(username):
        flash("Unauthorized access. Faculty privileges required.", "error")
        return redirect(url_for('index'))

    if request.method == 'POST':
        if 'eor_pdf' not in request.files:
            flash('No file uploaded.', 'error')
            return redirect(request.url)

        file = request.files['eor_pdf']
        remediation_name = request.form.get('remediation_name')

        if not file or file.filename == '' or not remediation_name:
            flash('Please select a file and enter a remediation name.', 'error')
            return redirect(request.url)

        conn = get_mysql_connection()
        try:
            with conn.cursor() as cursor:
                # --- UNIQUE NAME CHECK ---
                if not is_assignment_name_unique(conn, username, remediation_name, assignment_type='remediation'):
                    flash(f'A remediation named "{remediation_name}" already exists. Please choose a unique name.', 'error')
                    return redirect(request.url)

                if file and file.filename.endswith('.pdf'):
                    # Secure the filename and append identifiers to prevent overwriting
                    filename = secure_filename(f"{username}_{remediation_name}_{file.filename}")
                    upload_folder = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'uploads')
                    os.makedirs(upload_folder, exist_ok=True)
                    filepath = os.path.join(upload_folder, filename)

                    # Save the file to disk for the background daemon to pick up
                    file.save(filepath)

                    # --- QUEUE TASK IN BACKGROUND (Non-Blocking) ---
                    task_payload = json.dumps({
                        "filepath": filepath,
                        "remediation_name": remediation_name
                    })

                    cursor.execute('''
                        INSERT INTO quiz_generation_tasks (username, quiz_name, task_type, payload, status, created_at, updated_at)
                        VALUES (%s, %s, 'remediation_pdf', %s, 'pending', NOW(), NOW())
                    ''', (username, remediation_name, task_payload))
                    conn.commit()

                    flash(f'PDF uploaded! Remediation "{remediation_name}" is generating in the background.', 'success')
                    return redirect(url_for('faculty.review_remediation'))
                else:
                    flash('Invalid file type. Please upload a PDF.', 'error')
                    return redirect(request.url)

        except Exception as e:
            flash(f'Error queuing task: {str(e)}', 'error')
            return redirect(url_for('faculty.remediation_menu'))
        finally:
            conn.close()

    return render_template('faculty_create_remediation.html')

@faculty_bp.route('/faculty/review_remediation')
@login_required
def review_remediation():
    username = session.get('username')
    if not is_user_faculty(username):
        return redirect(url_for('index'))

    conn = get_mysql_connection()
    remediations_dict = {}

    try:
        with conn.cursor() as cursor:
            sql = """
                SELECT id, remediation_name, organ_system, task_area, topic_area, created_date
                FROM remediation_assignments
                WHERE TRIM(LOWER(username)) = TRIM(LOWER(%s))
                ORDER BY remediation_name ASC
            """
            cursor.execute(sql, (username,))
            all_modules = cursor.fetchall()

            for mod in all_modules:
                raw_name = mod.get('remediation_name')
                if not raw_name or str(raw_name).strip() == "" or str(raw_name) == "None":
                    name = "Unnamed Assignment"
                else:
                    name = str(raw_name).strip()

                if name not in remediations_dict:
                    remediations_dict[name] = {
                        'remediation_name': name,
                        'latest_date': mod.get('created_date'),
                        'module_count': 0
                    }
                remediations_dict[name]['module_count'] += 1

    except Exception as e:
        flash(f"Database error loading remediations: {str(e)}", "error")
    finally:
        conn.close()

    remediations = list(remediations_dict.values())
    return render_template('faculty_review_remediation.html', remediations=remediations)

@faculty_bp.route('/faculty/review_remediation/<path:remediation_name>')
@login_required
def review_remediation_details(remediation_name):
    username = session.get('username')
    if not is_user_faculty(username):
        return redirect(url_for('index'))

    conn = get_mysql_connection()
    modules = []
    clean_name = urllib.parse.unquote(remediation_name).strip()

    try:
        with conn.cursor() as cursor:
            sql = """
                SELECT id, remediation_name, organ_system, task_area, topic_area, created_date
                FROM remediation_assignments
                WHERE TRIM(LOWER(username)) = TRIM(LOWER(%s))
                ORDER BY remediation_name ASC
            """
            cursor.execute(sql, (username,))
            all_modules = cursor.fetchall()

            for mod in all_modules:
                raw_name = mod.get('remediation_name')
                if not raw_name or str(raw_name).strip() == "" or str(raw_name) == "None":
                    db_name = "Unnamed Assignment"
                else:
                    db_name = str(raw_name).strip()

                if db_name == clean_name:
                    modules.append(mod)

    except Exception as e:
        flash(f"Database error loading modules: {str(e)}", "error")
    finally:
        conn.close()

    return render_template('faculty_review_remediation_details.html', modules=modules, remediation_name=clean_name)

@faculty_bp.route('/faculty/delete_remediation_module', methods=['POST'])
@login_required
def delete_remediation_module():
    username = session.get('username')
    if not is_user_faculty(username):
        return redirect(url_for('index'))

    module_id = request.form.get('module_id')
    remediation_name = request.form.get('remediation_name')

    if not module_id:
        flash("Invalid module ID.", "error")
        return redirect(url_for('faculty.review_remediation'))

    conn = get_mysql_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT id, topic_area, remediation_name FROM remediation_assignments WHERE id = %s AND TRIM(LOWER(username)) = TRIM(LOWER(%s))", (module_id, username))
            module = cursor.fetchone()

            if module:
                rem_name = remediation_name or module['remediation_name']
                if not rem_name or str(rem_name).strip() == "" or str(rem_name) == "None":
                    rem_name = "Unnamed Assignment"

                cursor.execute("DELETE FROM remediation_questions WHERE assignment_id = %s", (module_id,))
                cursor.execute("DELETE FROM remediation_assignments WHERE id = %s", (module_id,))
                conn.commit()
                flash(f'Module "{module["topic_area"]}" was successfully deleted.', 'success')
                return redirect(url_for('faculty.review_remediation_details', remediation_name=rem_name))
            else:
                flash("Module not found or unauthorized.", "error")
    except Exception as e:
        flash(f'Error deleting module: {str(e)}', 'error')
    finally:
        conn.close()

    return redirect(url_for('faculty.review_remediation'))

@faculty_bp.route('/faculty/delete_entire_remediation', methods=['POST'])
@login_required
def delete_entire_remediation():
    username = session.get('username')
    if not is_user_faculty(username):
        return redirect(url_for('index'))

    remediation_name = request.form.get('remediation_name')
    if not remediation_name:
        flash("Invalid remediation name.", "error")
        return redirect(url_for('faculty.review_remediation'))

    conn = get_mysql_connection()
    try:
        with conn.cursor() as cursor:
            if remediation_name == "Unnamed Assignment":
                cursor.execute("""
                    DELETE q FROM remediation_questions q
                    INNER JOIN remediation_assignments a ON q.assignment_id = a.id
                    WHERE TRIM(LOWER(a.username)) = TRIM(LOWER(%s)) AND (a.remediation_name IS NULL OR TRIM(a.remediation_name) = '')
                """, (username,))
                cursor.execute("""
                    DELETE FROM remediation_assignments
                    WHERE TRIM(LOWER(username)) = TRIM(LOWER(%s)) AND (remediation_name IS NULL OR TRIM(remediation_name) = '')
                """, (username,))
            else:
                cursor.execute("""
                    DELETE q FROM remediation_questions q
                    INNER JOIN remediation_assignments a ON q.assignment_id = a.id
                    WHERE TRIM(LOWER(a.username)) = TRIM(LOWER(%s)) AND TRIM(LOWER(a.remediation_name)) = TRIM(LOWER(%s))
                """, (username, remediation_name))
                cursor.execute("""
                    DELETE FROM remediation_assignments
                    WHERE TRIM(LOWER(username)) = TRIM(LOWER(%s)) AND TRIM(LOWER(remediation_name)) = TRIM(LOWER(%s))
                """, (username, remediation_name))

            conn.commit()
            flash(f'The assignment "{remediation_name}" and all its modules were successfully deleted.', 'success')
    except Exception as e:
        flash(f'Error deleting remediation assignment: {str(e)}', 'error')
    finally:
        conn.close()

    return redirect(url_for('faculty.review_remediation'))

@faculty_bp.route('/faculty/review_remediation_module/<string:module_id>')
@login_required
def review_remediation_questions(module_id):
    username = session.get('username')
    if not is_user_faculty(username):
        return redirect(url_for('index'))

    conn = get_mysql_connection()
    questions = []
    module_info = None

    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT id, remediation_name, organ_system, task_area, topic_area
                FROM remediation_assignments
                WHERE id = %s AND TRIM(LOWER(username)) = TRIM(LOWER(%s))
            """, (module_id, username))
            module_info = cursor.fetchone()

            if not module_info:
                flash("Module not found or unauthorized.", "error")
                return redirect(url_for('faculty.review_remediation'))

            cursor.execute("""
                SELECT *
                FROM remediation_questions
                WHERE assignment_id = %s
                ORDER BY id ASC
            """, (module_id,))
            questions = cursor.fetchall()
    except Exception as e:
        flash(f"Database error loading questions: {str(e)}", "error")
    finally:
        conn.close()

    return render_template('faculty_review_remediation_questions.html', questions=questions, module=module_info)

@faculty_bp.route('/faculty/remove_remediation_question', methods=['POST'])
@login_required
def remove_remediation_question():
    username = session.get('username')
    if not is_user_faculty(username):
        return redirect(url_for('index'))

    question_id = request.form.get('question_id')
    module_id = request.form.get('module_id')

    conn = get_mysql_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT id FROM remediation_assignments
                WHERE id = %s AND TRIM(LOWER(username)) = TRIM(LOWER(%s))
            """, (module_id, username))

            if cursor.fetchone():
                cursor.execute("""
                    DELETE FROM remediation_questions
                    WHERE id = %s AND assignment_id = %s
                """, (question_id, module_id))
                conn.commit()
                flash("Question successfully removed from the remediation module.", "success")
            else:
                flash("Unauthorized to modify this module.", "error")
    except Exception as e:
        flash(f"Error removing question: {str(e)}", "error")
    finally:
        conn.close()

    return redirect(url_for('faculty.review_remediation_questions', module_id=module_id))


# ==========================================
# REVIEW LIST ROUTE - Standard Quizzes
# ==========================================
@faculty_bp.route('/faculty/review')
@login_required
def review_list():
    username = session.get('username')
    if not is_user_faculty(username):
        return redirect(url_for('index'))

    conn = get_mysql_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute('''
                SELECT uq.quiz_name, COUNT(qqm.map_id) as question_count
                FROM user_quizzes uq
                LEFT JOIN quiz_questions_map qqm ON uq.quiz_id = qqm.quiz_id
                WHERE TRIM(LOWER(uq.username)) = TRIM(LOWER(%s))
                GROUP BY uq.quiz_id, uq.quiz_name
                ORDER BY uq.quiz_name ASC
            ''', (username,))
            quizzes = cursor.fetchall()
    finally:
        conn.close()

    return render_template('faculty_review_list.html', quizzes=quizzes)

@faculty_bp.route('/faculty/review/<quiz_name>')
@login_required
def review_single(quiz_name):
    username = session.get('username')
    if not is_user_faculty(username):
        return redirect(url_for('index'))

    conn = get_mysql_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute('''
                SELECT qb.*
                FROM question_bank qb
                JOIN quiz_questions_map qqm ON qb.id = qqm.question_id
                JOIN user_quizzes uq ON qqm.quiz_id = uq.quiz_id
                WHERE uq.quiz_name = %s AND TRIM(LOWER(uq.username)) = TRIM(LOWER(%s))
                ORDER BY qqm.map_id ASC
            ''', (quiz_name, username))
            questions = cursor.fetchall()
    finally:
        conn.close()

    return render_template('faculty_review_single.html', questions=questions, quiz_name=quiz_name)

@faculty_bp.route('/faculty/remove_question', methods=['POST'])
@login_required
def remove_question():
    username = session.get('username')
    if not is_user_faculty(username):
        return redirect(url_for('index'))

    question_id = request.form.get('question_id')
    quiz_name = request.form.get('quiz_name')

    conn = get_mysql_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute('SELECT quiz_id FROM user_quizzes WHERE quiz_name = %s AND TRIM(LOWER(username)) = TRIM(LOWER(%s))', (quiz_name, username))
            quiz_row = cursor.fetchone()

            if quiz_row:
                cursor.execute('DELETE FROM quiz_questions_map WHERE question_id = %s AND quiz_id = %s',
                               (question_id, quiz_row['quiz_id']))
                conn.commit()
    finally:
        conn.close()

    flash("Question successfully removed from the quiz.", "success")
    return redirect(url_for('faculty.review_single', quiz_name=quiz_name))

# ==========================================
# SEARCH & ADD ADDITIONAL QUESTIONS
# ==========================================
@faculty_bp.route('/faculty/review/<quiz_name>/add_questions', methods=['GET', 'POST'])
@login_required
def search_add_questions(quiz_name):
    username = session.get('username')
    if not is_user_faculty(username):
        return redirect(url_for('index'))

    base_dir = os.path.dirname(os.path.abspath(__file__))
    try:
        with open(os.path.join(base_dir, 'topics_by_organ_system.json'), 'r') as f:
            organ_data = json.load(f)
    except Exception:
        organ_data = {}

    task_areas = [
        "History Taking and Performing Physical Examination",
        "Using Laboratory and Diagnostic Studies",
        "Formulating Most Likely Diagnosis",
        "Health Maintenance, Patient Education, and Preventative Measures",
        "Clinical Intervention",
        "Pharmaceutical Therapeutics",
        "Applying Basic Scientific Concepts"
    ]

    # --- DEFINED EXPLICIT FALLBACK ---
    DEFAULT_ORGAN_SYSTEMS = [
        "Cardiovascular System", "Dermatologic System", "Endocrine System",
        "Eyes, Ears, Nose, and Throat", "Gastrointestinal System/Nutrition",
        "Genitourinary System", "Hematologic System", "Infectious Diseases",
        "Musculoskeletal System", "Neurologic System", "Psychiatry/Behavioral Science",
        "Pulmonary System", "Renal System", "Reproductive System"
    ]

    if request.method == 'POST':
        organ_system = request.form.get('organ_system', '').strip()
        task_area = request.form.get('task_area', '').strip()
        topic_area = request.form.get('topic_area', '').strip()

        try:
            num_questions = max(1, min(int(request.form.get('num_questions', 5)), 20))
        except ValueError:
            num_questions = 5

        conn = get_mysql_connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute("SELECT quiz_id FROM user_quizzes WHERE quiz_name = %s AND TRIM(LOWER(username)) = TRIM(LOWER(%s))", (quiz_name, username))
                quiz_row = cursor.fetchone()
                if not quiz_row:
                    flash("Quiz not found.", "error")
                    return redirect(url_for('faculty.review_list'))

                quiz_id = quiz_row['quiz_id']

                cursor.execute("SELECT question_id FROM quiz_questions_map WHERE quiz_id = %s", (quiz_id,))
                mapped_ids = {r['question_id'] for r in cursor.fetchall()}

                def fetch_qs(o, ta, to):
                    conds, params = [], []
                    if o and o.lower() != 'any':
                        conds.append("TRIM(organ_system) = TRIM(%s)")
                        params.append(o)
                    if ta and ta.lower() != 'any':
                        conds.append("TRIM(task_area) = TRIM(%s)")
                        params.append(ta)
                    if to and to.lower() != 'any':
                        conds.append("TRIM(topic_area) = TRIM(%s)")
                        params.append(to)

                    query = "SELECT * FROM question_bank" + ((" WHERE " + " AND ".join(conds)) if conds else "")
                    cursor.execute(query, params)
                    return [q for q in cursor.fetchall() if q['id'] not in mapped_ids]

                found_questions = fetch_qs(organ_system, task_area, topic_area)

                if len(found_questions) < num_questions and topic_area and topic_area.lower() != 'any':
                    found_questions += fetch_qs(organ_system, task_area, 'any')
                if len(found_questions) < num_questions and task_area and task_area.lower() != 'any':
                    found_questions += fetch_qs(organ_system, 'any', topic_area)

                seen = set()
                unique_found = []
                for q in found_questions:
                    if q['id'] not in seen:
                        seen.add(q['id'])
                        unique_found.append(q)

                found_questions = unique_found[:num_questions]

                missing_count = num_questions - len(found_questions)
                if missing_count > 0:
                    generation_requests = []

                    # --- SAFE FALLBACK SELECTION ---
                    available_organs = list(organ_data.keys()) if organ_data else DEFAULT_ORGAN_SYSTEMS

                    # FIX: Explicitly check that values are truthy (not just empty strings "") so they correctly drop into the fallback randomizers
                    for _ in range(missing_count):
                        generation_requests.append({
                            "organ_system": organ_system if (organ_system and organ_system.lower() != 'any') else random.choice(available_organs),
                            "task_area": task_area if (task_area and task_area.lower() != 'any') else random.choice(task_areas),
                            "topic_area": topic_area if (topic_area and topic_area.lower() != 'any') else ""
                        })

                    task_payload = json.dumps({
                        "generation_requests": generation_requests,
                        "quiz_id": quiz_id
                    })

                    cursor.execute('''
                        INSERT INTO quiz_generation_tasks (username, quiz_name, task_type, payload, status, created_at, updated_at)
                        VALUES (%s, %s, %s, %s, 'pending', NOW(), NOW())
                    ''', (username, quiz_name, 'blueprint', task_payload))
                    conn.commit()

                    if len(found_questions) > 0:
                        flash(f"Found {len(found_questions)} existing questions. The remaining {missing_count} are being generated by AI in the background!", "info")
                    else:
                        flash(f"No existing questions found. All {missing_count} questions are being generated by AI in the background!", "info")

        finally:
            conn.close()

        return render_template(
            'faculty_add_questions_review.html',
            quiz_name=quiz_name,
            found_questions=found_questions
        )

    return render_template(
        'faculty_search_questions.html',
        quiz_name=quiz_name,
        organ_data=organ_data,
        task_areas=task_areas
    )

@faculty_bp.route('/faculty/review/<quiz_name>/attach_questions', methods=['POST'])
@login_required
def attach_questions_to_quiz(quiz_name):
    username = session.get('username')
    if not is_user_faculty(username):
        return redirect(url_for('index'))

    selected_question_ids = request.form.getlist('selected_questions')
    if not selected_question_ids:
        flash("No questions were selected to add.", "warning")
        return redirect(url_for('faculty.review_single', quiz_name=quiz_name))

    conn = get_mysql_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT quiz_id FROM user_quizzes WHERE quiz_name = %s AND TRIM(LOWER(username)) = TRIM(LOWER(%s))", (quiz_name, username))
            quiz_row = cursor.fetchone()

            if quiz_row:
                quiz_id = quiz_row['quiz_id']
                mappings = [(quiz_id, q_id) for q_id in selected_question_ids]
                cursor.executemany("INSERT IGNORE INTO quiz_questions_map (quiz_id, question_id) VALUES (%s, %s)", mappings)
                conn.commit()
                flash(f"Successfully added {len(selected_question_ids)} questions to '{quiz_name}'.", "success")
    finally:
        conn.close()

    return redirect(url_for('faculty.review_single', quiz_name=quiz_name))

# ==========================================
# ASSIGN QUIZ ROUTE
# ==========================================
@faculty_bp.route('/faculty/assign_menu')
@login_required
def assign_menu():
    username = session.get('username')
    if not is_user_faculty(username):
        flash("Unauthorized access.", "error")
        return redirect(url_for('index'))
    return render_template('faculty_assign_menu.html')

@faculty_bp.route('/faculty/assign', methods=['GET', 'POST'])
@login_required
def assign_quiz():
    username = session.get('username')
    if not is_user_faculty(username):
        return redirect(url_for('index'))

    conn = get_mysql_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT program_code FROM users WHERE clerk_id = %s", (username,))
            u_res = cursor.fetchone()
            program_code = u_res.get('program_code') if u_res else None

            if not program_code:
                flash("You do not have a program code assigned. Please contact an admin.", "error")
                return redirect(url_for('faculty.dashboard'))

            cursor.execute("SELECT id, class_name FROM classes WHERE program_code = %s ORDER BY class_name ASC", (program_code,))
            classes = cursor.fetchall()

            cursor.execute("SELECT clerk_id, first_name, last_name, email FROM users WHERE program_code = %s ORDER BY last_name ASC, first_name ASC", (program_code,))
            raw_students = cursor.fetchall()
            students = []
            for s in raw_students:
                s_dict = dict(s)
                s_dict['display_name'] = get_display_name(s['first_name'], s['last_name'], s['email'])
                students.append(s_dict)

            if request.method == 'POST':
                assignment_type = request.form.get('assignment_type')
                target_type = request.form.get('target_type')
                class_id = request.form.get('class_id') if target_type == 'class' else None
                clerk_id = request.form.get('clerk_id') if target_type == 'student' else None
                due_date_str = request.form.get('due_date')
                due_date = due_date_str if due_date_str else None

                if target_type == 'class' and not class_id:
                    flash("Please select a class.", "error")
                    return redirect(url_for('faculty.assign_quiz'))
                elif target_type == 'student' and not clerk_id:
                    flash("Please select a student.", "error")
                    return redirect(url_for('faculty.assign_quiz'))

                target_name = ""
                if class_id:
                    cursor.execute("SELECT class_name FROM classes WHERE id = %s", (class_id,))
                    c_res = cursor.fetchone()
                    target_name = c_res['class_name'] if c_res else "the selected class"
                elif clerk_id:
                    cursor.execute("SELECT first_name, last_name, email FROM users WHERE clerk_id = %s", (clerk_id,))
                    s_res = cursor.fetchone()
                    if s_res:
                        target_name = get_display_name(s_res['first_name'], s_res['last_name'], s_res['email'])
                    else:
                        target_name = "the selected student"

                due_msg = f" due by {due_date}" if due_date else ""

                if assignment_type == 'quiz':
                    quiz_name = request.form.get('quiz_name')
                    required_mode = request.form.get('required_mode', 'any')

                    if not quiz_name:
                        flash("Please select a quiz to assign.", "error")
                    else:
                        cursor.execute('''
                            INSERT INTO assigned_quizzes (quiz_name, program_code, class_id, clerk_id, assigned_by, required_mode, due_date)
                            VALUES (%s, %s, %s, %s, %s, %s, %s)
                        ''', (quiz_name, program_code, class_id, clerk_id, username, required_mode, due_date))
                        conn.commit()
                        flash(f"Quiz '{quiz_name}' successfully assigned to {target_name}{due_msg}.", "success")

                elif assignment_type == 'remediation':
                    remediation_name = request.form.get('remediation_name')
                    required_mode = request.form.get('required_mode', 'any')

                    if not remediation_name:
                        flash("Please select a remediation to assign.", "error")
                    else:
                        cursor.execute('''
                            INSERT INTO assigned_remediations (remediation_name, program_code, class_id, clerk_id, assigned_by, required_mode, due_date)
                            VALUES (%s, %s, %s, %s, %s, %s, %s)
                        ''', (remediation_name, program_code, class_id, clerk_id, username, required_mode, due_date))
                        conn.commit()
                        flash(f"Remediation '{remediation_name}' successfully assigned to {target_name}{due_msg}.", "success")

                return redirect(url_for('faculty.dashboard'))

            cursor.execute('SELECT quiz_name FROM user_quizzes WHERE TRIM(LOWER(username)) = TRIM(LOWER(%s)) ORDER BY quiz_name ASC', (username,))
            quizzes = cursor.fetchall()

            cursor.execute('''
                SELECT id, remediation_name, organ_system, task_area, topic_area, created_date
                FROM remediation_assignments
                WHERE TRIM(LOWER(username)) = TRIM(LOWER(%s))
            ''', (username,))
            all_rems = cursor.fetchall()

            unique_rems = set()
            for r in all_rems:
                raw_name = r.get('remediation_name')
                if not raw_name or str(raw_name).strip() == "" or str(raw_name) == "None":
                    unique_rems.add("Unnamed Assignment")
                else:
                    unique_rems.add(str(raw_name).strip())

            remediations = [{'remediation_name': name} for name in sorted(list(unique_rems))]

    finally:
        conn.close()

    return render_template('faculty_assign.html', quizzes=quizzes, remediations=remediations, classes=classes, students=students)

# ==========================================
# CLASS MANAGEMENT ROUTES
# ==========================================
@faculty_bp.route('/faculty/manage_classes')
@login_required
def manage_classes():
    username = session.get('username')
    if not is_user_faculty(username):
        return redirect(url_for('index'))

    conn = get_mysql_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute('''
                SELECT c.id, c.class_name, COUNT(cm.clerk_id) as student_count
                FROM classes c
                LEFT JOIN class_members cm ON c.id = cm.class_id
                WHERE c.created_by = %s
                GROUP BY c.id
                ORDER BY c.class_name ASC
            ''', (username,))
            classes = cursor.fetchall()
    finally:
        conn.close()

    return render_template('faculty_manage_classes.html', classes=classes)

@faculty_bp.route('/faculty/create_class', methods=['GET', 'POST'])
@login_required
def create_class():
    try:
        username = session.get('username')
        if not is_user_faculty(username):
            return redirect(url_for('index'))

        conn = get_mysql_connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute("SELECT program_code FROM users WHERE clerk_id = %s", (username,))
                result = cursor.fetchone()
                faculty_program = result['program_code'] if result else None

                if not faculty_program:
                    flash("You do not have a program code assigned. Please contact an admin.", "error")
                    return redirect(url_for('faculty.dashboard'))

                if request.method == 'POST':
                    class_name = request.form.get('class_name')
                    selected_users = request.form.getlist('selected_users')

                    if not class_name:
                        flash("Class name is required.", "error")
                    else:
                        cursor.execute('INSERT INTO classes (class_name, program_code, created_by) VALUES (%s, %s, %s)',
                                       (class_name, faculty_program, username))
                        class_id = cursor.lastrowid

                        if selected_users:
                            for user_id in selected_users:
                                cursor.execute('INSERT INTO class_members (class_id, clerk_id) VALUES (%s, %s)', (class_id, user_id))

                        conn.commit()
                        flash(f"Class '{class_name}' created with {len(selected_users)} students.", "success")
                        return redirect(url_for('faculty.manage_classes'))

                cursor.execute("""
                    SELECT clerk_id, first_name, last_name, email
                    FROM users
                    WHERE program_code = %s AND clerk_id != %s
                    ORDER BY last_name ASC
                """, (faculty_program, username))

                students = []
                for row in cursor.fetchall():
                    student = dict(row)
                    display_name = get_display_name(student.get('first_name'), student.get('last_name'), student.get('email'))

                    student['display_name'] = display_name
                    student['email'] = display_name
                    student['first_name'] = display_name
                    student['last_name'] = ""
                    students.append(student)

        finally:
            conn.close()

        return render_template('faculty_create_class.html', students=students, faculty_program=faculty_program)
    except Exception as e:
        import traceback
        return f"<div style='padding: 40px; color: red;'><h3>Crash in create_class:</h3><pre>{traceback.format_exc()}</pre></div>"

@faculty_bp.route('/faculty/edit_class/<int:class_id>', methods=['GET', 'POST'])
@login_required
def edit_class(class_id):
    username = session.get('username')
    if not is_user_faculty(username):
        return redirect(url_for('index'))

    conn = get_mysql_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT class_name, program_code FROM classes WHERE id = %s AND created_by = %s", (class_id, username))
            class_info = cursor.fetchone()

            if not class_info:
                flash("Class not found or access denied.", "error")
                return redirect(url_for('faculty.manage_classes'))

            class_name = class_info['class_name']
            program_code = class_info['program_code']

            if request.method == 'POST':
                action = request.form.get('action')
                student_id = request.form.get('student_id')

                if action == 'add':
                    cursor.execute("SELECT 1 FROM class_members WHERE class_id = %s AND clerk_id = %s", (class_id, student_id))
                    if not cursor.fetchone():
                        cursor.execute("INSERT INTO class_members (class_id, clerk_id) VALUES (%s, %s)", (class_id, student_id))
                        conn.commit()
                        flash(f"Student added to {class_name}.", "success")
                    else:
                        flash("Student is already in the class.", "warning")

                elif action == 'remove':
                    cursor.execute("DELETE FROM class_members WHERE class_id = %s AND clerk_id = %s", (class_id, student_id))
                    conn.commit()
                    flash("Student removed from the class.", "success")

                elif action == 'delete_class':
                    cursor.execute("DELETE FROM class_members WHERE class_id = %s", (class_id,))
                    cursor.execute("DELETE FROM classes WHERE id = %s", (class_id,))
                    conn.commit()
                    flash(f"Class '{class_name}' deleted.", "success")
                    return redirect(url_for('faculty.manage_classes'))

                return redirect(url_for('faculty.edit_class', class_id=class_id))

            cursor.execute("""
                SELECT cm.clerk_id, u.first_name, u.last_name, u.email
                FROM class_members cm
                JOIN users u ON cm.clerk_id = u.clerk_id
                WHERE cm.class_id = %s
                ORDER BY u.last_name ASC
            """, (class_id,))

            current_members = []
            for row in cursor.fetchall():
                member = dict(row)
                display_name = get_display_name(member.get('first_name'), member.get('last_name'), member.get('email'))

                member['display_name'] = display_name
                member['email'] = display_name
                member['first_name'] = display_name
                member['last_name'] = ""
                current_members.append(member)

            current_member_ids = [row['clerk_id'] for row in current_members]

            cursor.execute("""
                SELECT clerk_id, first_name, last_name, email
                FROM users
                WHERE program_code = %s AND clerk_id != %s
                ORDER BY last_name ASC
            """, (program_code, username))

            all_program_students = []
            for row in cursor.fetchall():
                student = dict(row)
                display_name = get_display_name(student.get('first_name'), student.get('last_name'), student.get('email'))

                student['display_name'] = display_name
                student['email'] = display_name
                student['first_name'] = display_name
                student['last_name'] = ""
                all_program_students.append(student)

            available_students = [s for s in all_program_students if s['clerk_id'] not in current_member_ids]

    finally:
        conn.close()

    return render_template('faculty_edit_class.html',
                           class_name=class_name,
                           class_id=class_id,
                           current_members=current_members,
                           available_students=available_students)

# ==========================================
# CLASS RESULTS ROUTE
# ==========================================
@faculty_bp.route('/faculty/class_results', methods=['GET', 'POST'])
@login_required
def class_results():
    username = session.get('username')
    if not is_user_faculty(username):
        return redirect(url_for('index'))

    conn = get_mysql_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT id, class_name FROM classes WHERE created_by = %s ORDER BY class_name ASC", (username,))
            classes = cursor.fetchall()

            cursor.execute('''
                SELECT DISTINCT u.clerk_id, u.first_name, u.last_name, u.email
                FROM users u
                JOIN class_members cm ON u.clerk_id = cm.clerk_id
                JOIN classes c ON cm.class_id = c.id
                WHERE c.created_by = %s
                ORDER BY u.last_name ASC
            ''', (username,))

            all_students = []
            for row in cursor.fetchall():
                s = dict(row)
                s['display_name'] = get_display_name(s.get('first_name'), s.get('last_name'), s.get('email'))
                all_students.append(s)

            cursor.execute('SELECT quiz_name FROM user_quizzes WHERE TRIM(LOWER(username)) = TRIM(LOWER(%s)) ORDER BY quiz_name ASC', (username,))
            quizzes = [r['quiz_name'] for r in cursor.fetchall() if r['quiz_name']]

            cursor.execute("SELECT DISTINCT remediation_name FROM remediation_assignments WHERE TRIM(LOWER(username)) = TRIM(LOWER(%s)) ORDER BY remediation_name ASC", (username,))
            remediations = [r['remediation_name'] for r in cursor.fetchall() if r['remediation_name'] and str(r['remediation_name']).strip() not in ['', 'None']]

            target_type = request.form.get('target_type') or request.args.get('target_type') or 'class'
            selected_class_id = request.form.get('class_id') or request.args.get('class_id')
            selected_student_id = request.form.get('student_id') or request.args.get('student_id')
            attempt_filter = request.form.get('attempt_filter') or request.args.get('attempt_filter') or 'all'
            clean_filter = attempt_filter.lower().strip()

            selected_selector = request.form.get('assignment_selector')
            assignment_type = request.args.get('assignment_type')
            selected_assignment = request.args.get('assignment_name')

            if selected_selector:
                parts = selected_selector.split('|', 1)
                if len(parts) == 2:
                    assignment_type = parts[0]
                    selected_assignment = parts[1]

            results = None
            selected_target_name = ""

            is_ready_to_query = False
            if target_type == 'class' and selected_class_id and selected_assignment and assignment_type:
                is_ready_to_query = True
            elif target_type == 'student' and selected_student_id and selected_assignment and assignment_type:
                is_ready_to_query = True

            if is_ready_to_query:
                if target_type == 'class':
                    for c in classes:
                        if str(c['id']) == str(selected_class_id):
                            selected_target_name = f"Class: {c['class_name']}"
                            break
                else:
                    for s in all_students:
                        if s['clerk_id'] == selected_student_id:
                            selected_target_name = f"Student: {s['display_name']}"
                            break

                total_q = 0
                if assignment_type == 'quiz':
                    cursor.execute('''
                        SELECT COUNT(qqm.map_id) as count
                        FROM quiz_questions_map qqm
                        JOIN user_quizzes uq ON qqm.quiz_id = uq.quiz_id
                        WHERE uq.quiz_name = %s AND TRIM(LOWER(uq.username)) = TRIM(LOWER(%s))
                    ''', (selected_assignment, username))
                    q_count_res = cursor.fetchone()
                    total_q = q_count_res['count'] if q_count_res else 0
                elif assignment_type == 'remediation':
                    cursor.execute('''
                        SELECT COUNT(rq.id) as count
                        FROM remediation_questions rq
                        JOIN remediation_assignments ra ON rq.assignment_id = ra.id
                        WHERE TRIM(LOWER(ra.remediation_name)) = TRIM(LOWER(%s)) AND TRIM(LOWER(ra.username)) = TRIM(LOWER(%s))
                    ''', (selected_assignment, username))
                    q_count_res = cursor.fetchone()
                    total_q = q_count_res['count'] if q_count_res else 0

                if target_type == 'class':
                    cursor.execute("""
                        SELECT u.clerk_id, u.first_name, u.last_name, u.email
                        FROM class_members cm
                        JOIN users u ON cm.clerk_id = u.clerk_id
                        WHERE cm.class_id = %s
                    """, (selected_class_id,))
                    target_students = cursor.fetchall()
                else:
                    cursor.execute("""
                        SELECT clerk_id, first_name, last_name, email
                        FROM users WHERE clerk_id = %s
                    """, (selected_student_id,))
                    target_students = cursor.fetchall()

                results = []
                attempts_by_user = {}

                # --- OPTIMIZATION: N+1 BULK QUERY ---
                if target_students:
                    student_ids = [s['clerk_id'] for s in target_students]
                    format_strings = ','.join(['%s'] * len(student_ids))

                    bulk_query = f'''
                        SELECT qa.username, qa.attempt_id, qa.mode, qa.start_time,
                               COUNT(ar.id) as answered_count,
                               COALESCE(SUM(ar.is_correct), 0) as correct_answers
                        FROM quiz_attempts qa
                        LEFT JOIN attempt_responses ar ON qa.attempt_id = ar.attempt_id
                        WHERE qa.quiz_name = %s AND qa.username IN ({format_strings})
                        GROUP BY qa.attempt_id, qa.username, qa.mode, qa.start_time
                        ORDER BY qa.start_time DESC
                    '''
                    params = [selected_assignment] + student_ids
                    cursor.execute(bulk_query, params)
                    all_raw_attempts = cursor.fetchall()

                    for att in all_raw_attempts:
                        uname = att['username']
                        if uname not in attempts_by_user:
                            attempts_by_user[uname] = []
                        attempts_by_user[uname].append(att)

                for student in target_students:
                    student_id = student['clerk_id']
                    display_name = get_display_name(student['first_name'], student['last_name'], student['email'])

                    raw_attempts = attempts_by_user.get(student_id, [])

                    highest_test_score = None
                    most_recent_test_score = None

                    for att in raw_attempts:
                        ans = att['answered_count'] or 0
                        corr = att['correct_answers'] or 0
                        pct = round((corr / ans * 100), 1) if ans > 0 else 0

                        if att['mode'] == 'test':
                            if highest_test_score is None or pct > highest_test_score:
                                highest_test_score = pct
                            if most_recent_test_score is None:
                                most_recent_test_score = pct

                    filtered_attempts = raw_attempts
                    if clean_filter == 'recent' and raw_attempts:
                        filtered_attempts = [raw_attempts[0]]
                    elif clean_filter == 'highest' and raw_attempts:
                        best_att = raw_attempts[0]
                        best_pct = -1
                        for att in raw_attempts:
                            ans = att['answered_count'] or 0
                            corr = att['correct_answers'] or 0
                            pct = round((corr / ans * 100), 1) if ans > 0 else 0
                            if pct > best_pct:
                                best_pct = pct
                                best_att = att
                        filtered_attempts = [best_att]

                    attempts = []
                    for att in filtered_attempts:
                        ans = att['answered_count'] or 0
                        corr = att['correct_answers'] or 0
                        pct = round((corr / ans * 100), 1) if ans > 0 else 0
                        safe_time_str = str(att['start_time']) if att.get('start_time') else ''

                        attempts.append({
                            'attempt_id': att['attempt_id'],
                            'mode': att['mode'],
                            'start_time': safe_time_str,
                            'answered_count': ans,
                            'total_questions': total_q,
                            'correct_answers': corr,
                            'score_pct': pct
                        })

                    results.append({
                        'clerk_id': student_id,
                        'display_name': display_name,
                        'email': display_name,
                        'first_name': display_name,
                        'highest_test_score': highest_test_score,
                        'most_recent_test_score': most_recent_test_score,
                        'attempts': attempts
                    })
    finally:
        conn.close()

    return render_template('faculty_class_results.html',
                           classes=classes,
                           all_students=all_students,
                           quizzes=quizzes,
                           remediations=remediations,
                           target_type=target_type,
                           selected_class_id=selected_class_id,
                           selected_student_id=selected_student_id,
                           selected_assignment=selected_assignment,
                           assignment_type=assignment_type,
                           selected_target_name=selected_target_name,
                           attempt_filter=clean_filter,
                           results=results)

# ==========================================
# GENERATE PDF & CSV REPORTS ROUTE
# ==========================================
@faculty_bp.route('/faculty/reports', methods=['GET', 'POST'])
@login_required
def generate_report():
    try:
        username = session.get('username')
        if not is_user_faculty(username):
            return redirect(url_for('index'))

        conn = get_mysql_connection()
        try:
            with conn.cursor() as cursor:
                PREDEFINED_ORGANS = [
                    "Cardiovascular System", "Dermatologic System", "Endocrine System",
                    "Eyes, Ears, Nose, and Throat", "Gastrointestinal System/Nutrition",
                    "Genitourinary System", "Hematologic System", "Infectious Diseases",
                    "Musculoskeletal System", "Neurologic System", "Psychiatry/Behavioral Science",
                    "Pulmonary System", "Renal System", "Reproductive System", "Professional Practice",
                    "Miscellaneous"
                ]
                PREDEFINED_TASKS = SHARED_VALID_TASK_AREAS

                # --- GET REQUEST: Load Dropdowns ---
                if request.method == 'GET':
                    cursor.execute("SELECT id, class_name FROM classes WHERE created_by = %s ORDER BY class_name ASC", (username,))
                    classes = cursor.fetchall()

                    cursor.execute('''
                        SELECT DISTINCT u.clerk_id, u.first_name, u.last_name, u.email
                        FROM users u
                        JOIN class_members cm ON u.clerk_id = cm.clerk_id
                        JOIN classes c ON cm.class_id = c.id
                        WHERE c.created_by = %s
                        ORDER BY u.last_name ASC
                    ''', (username,))

                    students = []
                    for row in cursor.fetchall():
                        s = dict(row)
                        s['display_name'] = get_display_name(s.get('first_name'), s.get('last_name'), s.get('email'))
                        students.append(s)

                    cursor.execute("SELECT quiz_name FROM user_quizzes WHERE TRIM(LOWER(username)) = TRIM(LOWER(%s)) ORDER BY quiz_name ASC", (username,))
                    quizzes = [r['quiz_name'] for r in cursor.fetchall()]

                    cursor.execute("SELECT DISTINCT remediation_name FROM remediation_assignments WHERE TRIM(LOWER(username)) = TRIM(LOWER(%s)) ORDER BY remediation_name ASC", (username,))
                    remediations = [r['remediation_name'] for r in cursor.fetchall() if r['remediation_name'] and str(r['remediation_name']).strip() not in ['', 'None']]

                    return render_template('faculty_report.html', classes=classes, students=students, quizzes=quizzes, remediations=remediations)

                # --- POST REQUEST: Generate Report ---
                report_target = request.form.get('report_target')
                report_type = request.form.get('report_type')
                attempt_filter = request.form.get('attempt_filter', 'highest')

                if report_target == 'class' and report_type == 'item_analysis':
                    if not ReportEngine:
                        flash("Report engine module missing. Please create report_engine.py in the main directory.", "error")
                        return redirect(url_for('faculty.reports_hub'))

                    class_id = request.form.get('target_class_id')
                    cursor.execute("SELECT class_name FROM classes WHERE id = %s", (class_id,))
                    c_res = cursor.fetchone()
                    class_name = c_res['class_name'] if c_res else "Class"

                    assignment_name = request.form.get('quiz_name') if request.form.get('quiz_name') else request.form.get('remediation_name')
                    assignment_type = 'remediation' if request.form.get('remediation_name') else 'quiz'

                    engine = ReportEngine(conn, conn)
                    analysis = engine.generate_item_analysis(class_id, assignment_type, assignment_name, attempt_filter)

                    if not analysis:
                        flash(f"Not enough data to generate an item analysis for '{assignment_name}'.", "error")
                        return redirect(url_for('faculty.reports_hub'))

                    si = io.StringIO()
                    cw = csv.writer(si)
                    cw.writerow(['Question ID', 'Correct Answer', 'Difficulty (p-value)', 'Discrimination Index', '% Chose A', '% Chose B', '% Chose C', '% Chose D', '% Chose E'])

                    for item in analysis:
                        cw.writerow([
                            item['question_id'], item['correct_answer'], item['difficulty_p_value'],
                            item['discrimination_index'], f"{item['distractor_a']}%", f"{item['distractor_b']}%",
                            f"{item['distractor_c']}%", f"{item['distractor_d']}%", f"{item['distractor_e']}%"
                        ])

                    output = si.getvalue()
                    return Response(output, mimetype='text/csv', headers={"Content-Disposition": f"attachment;filename=ItemAnalysis_{class_name.replace(' ', '_')}_{assignment_name.replace(' ', '_')}.csv"})

                def get_assignment_data(clerk_id, assignment_name, is_remediation, filter_type):
                    mode_filter = ""
                    cursor.execute(f'''
                        SELECT qa.attempt_id, qa.start_time,
                               COUNT(ar.id) as answered_count,
                               COALESCE(SUM(ar.is_correct), 0) as correct_answers
                        FROM quiz_attempts qa
                        LEFT JOIN attempt_responses ar ON qa.attempt_id = ar.attempt_id
                        WHERE qa.username = %s AND qa.quiz_name = %s {mode_filter}
                        GROUP BY qa.attempt_id, qa.start_time
                        ORDER BY qa.start_time DESC
                    ''', (clerk_id, assignment_name))

                    attempts = cursor.fetchall()
                    if not attempts:
                        return []

                    selected_attempts = []
                    if filter_type == 'highest':
                        best_att = max(attempts, key=lambda x: (x['correct_answers'] / x['answered_count']) if x['answered_count'] else 0)
                        selected_attempts = [best_att]
                    elif filter_type == 'recent':
                        selected_attempts = [attempts[0]]
                    else:
                        selected_attempts = attempts

                    results = []
                    for att in selected_attempts:
                        ans = att['answered_count'] or 0
                        corr = att['correct_answers'] or 0
                        pct = round((corr / ans * 100), 1) if ans > 0 else 0

                        cursor.execute('''
                            SELECT user_choice, correct_answer, is_correct
                            FROM attempt_responses
                            WHERE attempt_id = %s
                            ORDER BY submitted_time ASC
                        ''', (att['attempt_id'],))
                        answers = cursor.fetchall()

                        st_time = att['start_time'].strftime("%Y-%m-%d %H:%M") if hasattr(att['start_time'], 'strftime') else str(att['start_time'])
                        results.append({'pct': pct, 'answers': answers, 'date': st_time})

                    return results

                def get_bulk_assignment_data(student_ids, assignment_name, is_remediation, filter_type):
                    if not student_ids:
                        return {}

                    format_strings = ','.join(['%s'] * len(student_ids))
                    cursor.execute(f'''
                        SELECT qa.username, qa.attempt_id, qa.start_time,
                               COUNT(ar.id) as answered_count,
                               COALESCE(SUM(ar.is_correct), 0) as correct_answers
                        FROM quiz_attempts qa
                        LEFT JOIN attempt_responses ar ON qa.attempt_id = ar.attempt_id
                        WHERE qa.quiz_name = %s AND qa.username IN ({format_strings})
                        GROUP BY qa.attempt_id, qa.username, qa.start_time
                        ORDER BY qa.start_time DESC
                    ''', [assignment_name] + student_ids)

                    all_attempts = cursor.fetchall()

                    attempts_by_user = {}
                    for att in all_attempts:
                        uname = att['username']
                        if uname not in attempts_by_user:
                            attempts_by_user[uname] = []
                        attempts_by_user[uname].append(att)

                    final_data_by_user = {}
                    target_attempt_ids = []
                    selected_attempts_flat = []

                    for uname, user_attempts in attempts_by_user.items():
                        if filter_type == 'highest':
                            best_att = max(user_attempts, key=lambda x: (x['correct_answers'] / x['answered_count']) if x['answered_count'] else 0)
                            selected_atts = [best_att]
                        elif filter_type == 'recent':
                            selected_atts = [user_attempts[0]]
                        else:
                            selected_atts = user_attempts

                        selected_attempts_flat.extend(selected_atts)
                        target_attempt_ids.extend([a['attempt_id'] for a in selected_atts])

                    if not target_attempt_ids:
                        return {}

                    fmt = ','.join(['%s'] * len(target_attempt_ids))
                    cursor.execute(f'''
                        SELECT attempt_id, user_choice, correct_answer, is_correct
                        FROM attempt_responses
                        WHERE attempt_id IN ({fmt})
                        ORDER BY submitted_time ASC
                    ''', target_attempt_ids)

                    all_responses = cursor.fetchall()

                    responses_by_attempt = {}
                    for r in all_responses:
                        aid = r['attempt_id']
                        if aid not in responses_by_attempt:
                            responses_by_attempt[aid] = []
                        responses_by_attempt[aid].append(r)

                    for att in selected_attempts_flat:
                        uname = att['username']
                        ans = att['answered_count'] or 0
                        corr = att['correct_answers'] or 0
                        pct = round((corr / ans * 100), 1) if ans > 0 else 0
                        st_time = att['start_time'].strftime("%Y-%m-%d %H:%M") if hasattr(att['start_time'], 'strftime') else str(att['start_time'])

                        if uname not in final_data_by_user:
                            final_data_by_user[uname] = []

                        final_data_by_user[uname].append({
                            'pct': pct,
                            'answers': responses_by_attempt.get(att['attempt_id'], []),
                            'date': st_time
                        })

                    return final_data_by_user

                if report_target == 'class' and report_type in ['quiz', 'remediation']:
                    class_id = request.form.get('target_class_id')
                    cursor.execute("SELECT class_name FROM classes WHERE id = %s", (class_id,))
                    class_name = cursor.fetchone()['class_name']

                    cursor.execute("""
                        SELECT u.clerk_id, u.first_name, u.last_name, u.email
                        FROM class_members cm
                        JOIN users u ON cm.clerk_id = u.clerk_id
                        WHERE cm.class_id = %s ORDER BY u.last_name ASC
                    """, (class_id,))
                    students = cursor.fetchall()

                    assignment_name = request.form.get('quiz_name') if report_type == 'quiz' else request.form.get('remediation_name')
                    is_rem = (report_type == 'remediation')

                    si = io.StringIO()
                    cw = csv.writer(si)

                    student_ids = [s['clerk_id'] for s in students]
                    bulk_class_data = get_bulk_assignment_data(student_ids, assignment_name, is_rem, attempt_filter)

                    all_data = []
                    max_q = 0
                    for student in students:
                        s_name = get_display_name(student['first_name'], student['last_name'], student['email'])
                        attempts_data = bulk_class_data.get(student['clerk_id'], [])

                        if not attempts_data:
                            all_data.append((s_name, "N/A", "No Attempts", []))
                            continue

                        for att in attempts_data:
                            if len(att['answers']) > max_q: max_q = len(att['answers'])
                            all_data.append((s_name, f"{att['pct']}%", att['date'], att['answers']))

                    headers = ['Student Name', 'Attempt Date', 'Overall Score']
                    for i in range(1, max_q + 1):
                        headers.extend([f"Q{i} User Answer", f"Q{i} Correct Answer"])
                    cw.writerow(headers)

                    for name, pct, date_str, answers in all_data:
                        row = [name, date_str, pct]
                        for a in answers:
                            row.extend([a['user_choice'], a['correct_answer']])
                        cw.writerow(row)

                    output = si.getvalue()
                    return Response(output, mimetype='text/csv', headers={"Content-Disposition": f"attachment;filename=Report_{class_name.replace(' ', '_')}.csv"})

                elif report_target == 'student' and report_type in ['quiz', 'remediation']:
                    student_id = request.form.get('target_student_id')
                    cursor.execute("SELECT first_name, last_name, email FROM users WHERE clerk_id = %s", (student_id,))
                    s_row = cursor.fetchone()
                    student_name = get_display_name(s_row['first_name'], s_row['last_name'], s_row['email'])

                    assignment_name = request.form.get('quiz_name') if report_type == 'quiz' else request.form.get('remediation_name')
                    is_rem = (report_type == 'remediation')
                    attempts_data = get_assignment_data(student_id, assignment_name, is_rem, attempt_filter)

                    buffer = io.BytesIO()
                    doc = SimpleDocTemplate(buffer, pagesize=letter)
                    elements = []
                    styles = getSampleStyleSheet()

                    elements.append(Paragraph(f"<b>Assignment Report:</b> {assignment_name}", styles['Title']))
                    elements.append(Paragraph(f"<b>Student:</b> {student_name}", styles['Normal']))
                    elements.append(Spacer(1, 20))

                    if not attempts_data:
                        elements.append(Paragraph("No completed attempts found.", styles['Normal']))
                    else:
                        for idx, att in enumerate(attempts_data):
                            elements.append(Paragraph(f"<b>Attempt Date:</b> {att['date']} | <b>Score:</b> {att['pct']}%", styles['Heading3']))
                            elements.append(Spacer(1, 10))

                            table_data = [['Q#', 'Student Answer', 'Correct Answer', 'Result']]
                            for q_idx, a in enumerate(att['answers']):
                                res = "Correct" if a['is_correct'] else "Incorrect"
                                table_data.append([str(q_idx+1), a['user_choice'], a['correct_answer'], res])

                            t = Table(table_data, colWidths=[50, 150, 150, 100])
                            t.setStyle(TableStyle([
                                ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#2c3e50')),
                                ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
                                ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
                                ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                                ('GRID', (0, 0), (-1, -1), 1, colors.HexColor('#bdc3c7')),
                            ]))
                            elements.append(t)
                            elements.append(Spacer(1, 20))

                    doc.build(elements)
                    buffer.seek(0)
                    return send_file(buffer, as_attachment=True, download_name=f"Report_{student_name.replace(' ','_')}.pdf", mimetype='application/pdf')

                if report_type == 'usage':
                    engine = ReportEngine(conn, conn)

                    if report_target == 'class':
                        class_id = request.form.get('target_class_id')
                        cursor.execute("SELECT class_name FROM classes WHERE id = %s", (class_id,))
                        class_name = cursor.fetchone()['class_name']

                        cursor.execute("""
                            SELECT u.clerk_id, u.first_name, u.last_name, u.email
                            FROM class_members cm
                            JOIN users u ON cm.clerk_id = u.clerk_id
                            WHERE cm.class_id = %s ORDER BY u.last_name ASC
                        """, (class_id,))
                        students = cursor.fetchall()

                        si = io.StringIO()
                        cw = csv.writer(si)
                        headers = ['Student Name', 'Overall %', 'Total Attempted']
                        for o in PREDEFINED_ORGANS: headers.extend([f"{o} Att", f"{o} %"])
                        for t in PREDEFINED_TASKS: headers.extend([f"{t} Att", f"{t} %"])
                        cw.writerow(headers)

                        for student in students:
                            s_name = get_display_name(student['first_name'], student['last_name'], student['email'])

                            stats = engine.generate_overall_usage([student['clerk_id']])
                            pct = stats['overall_pct']
                            total_att = stats['total_attempted']
                            org_data = stats['organs']
                            tsk_data = stats['tasks']

                            row = [s_name, f"{pct}%", total_att]
                            for o in PREDEFINED_ORGANS:
                                att = org_data.get(o, {}).get('att', 0)
                                corr = org_data.get(o, {}).get('cor', 0)
                                p = round((corr/att*100),1) if att > 0 else 0
                                row.extend([att, f"{p}%"])
                            for t in PREDEFINED_TASKS:
                                att = tsk_data.get(t, {}).get('att', 0)
                                corr = tsk_data.get(t, {}).get('cor', 0)
                                p = round((corr/att*100),1) if att > 0 else 0
                                row.extend([att, f"{p}%"])
                            cw.writerow(row)

                        output = si.getvalue()
                        return Response(output, mimetype='text/csv', headers={"Content-Disposition": f"attachment;filename=Usage_Report_{class_name.replace(' ', '_')}.csv"})

                    elif report_target == 'student':
                        student_id = request.form.get('target_student_id')
                        cursor.execute("SELECT first_name, last_name, email FROM users WHERE clerk_id = %s", (student_id,))
                        s_row = cursor.fetchone()
                        student_name = get_display_name(s_row['first_name'], s_row['last_name'], s_row['email'])

                        stats = engine.generate_overall_usage([student_id])
                        pct = stats['overall_pct']
                        total_att = stats['total_attempted']
                        org_data = stats['organs']
                        tsk_data = stats['tasks']

                        buffer = io.BytesIO()
                        doc = SimpleDocTemplate(buffer, pagesize=letter)
                        elements = []
                        styles = getSampleStyleSheet()

                        elements.append(Paragraph(f"<b>Student Usage Statistics</b>", styles['Title']))
                        elements.append(Spacer(1, 12))
                        elements.append(Paragraph(f"<b>Student:</b> {student_name}", styles['Normal']))
                        elements.append(Paragraph(f"<b>Total Questions Attempted:</b> {total_att}", styles['Normal']))
                        elements.append(Paragraph(f"<b>Overall Average:</b> {pct}%", styles['Normal']))
                        elements.append(Spacer(1, 20))

                        elements.append(Paragraph("<b>Organ Systems Breakdown</b>", styles['Heading3']))
                        elements.append(Spacer(1, 10))
                        org_table = [['Organ System', 'Attempted', 'Score %']]
                        for o in PREDEFINED_ORGANS:
                            att = org_data.get(o, {}).get('att', 0)
                            c_pct = round((org_data.get(o, {}).get('cor', 0) / att * 100), 1) if att > 0 else 0
                            org_table.append([o, str(att), f"{c_pct}%"])

                        t1 = Table(org_table, colWidths=[250, 100, 100])
                        t1.setStyle(TableStyle([
                            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#34495E')),
                            ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
                            ('GRID', (0, 0), (-1, -1), 1, colors.HexColor('#bdc3c7')),
                        ]))
                        elements.append(t1)
                        elements.append(Spacer(1, 20))

                        elements.append(Paragraph("<b>Task Area Breakdown</b>", styles['Heading3']))
                        elements.append(Spacer(1, 10))
                        tsk_table = [['Task Area', 'Attempted', 'Score %']]
                        for tsk in PREDEFINED_TASKS:
                            att = tsk_data.get(tsk, {}).get('att', 0)
                            c_pct = round((tsk_data.get(tsk, {}).get('cor', 0) / att * 100), 1) if att > 0 else 0
                            tsk_table.append([tsk, str(att), f"{c_pct}%"])

                        t2 = Table(tsk_table, colWidths=[250, 100, 100])
                        t2.setStyle(TableStyle([
                            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#34495E')),
                            ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
                            ('GRID', (0, 0), (-1, -1), 1, colors.HexColor('#bdc3c7')),
                        ]))
                        elements.append(t2)

                        doc.build(elements)
                        buffer.seek(0)
                        return send_file(buffer, as_attachment=True, download_name=f"Usage_{student_name.replace(' ','_')}.pdf", mimetype='application/pdf')

        finally:
            conn.close()

        return redirect(url_for('faculty.reports_hub'))

    except Exception as e:
        import traceback
        return f"<div style='padding: 40px; color: red;'><h3>Crash in Report Generator:</h3><pre>{traceback.format_exc()}</pre></div>"

# ==========================================
# REVIEW ASSIGNED CONTENT
# ==========================================
@faculty_bp.route('/faculty/review_assigned', methods=['GET'])
@login_required
def review_assigned():
    username = session.get('username')
    if not is_user_faculty(username):
        return redirect(url_for('index'))

    conn = get_mysql_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT program_code FROM users WHERE clerk_id = %s", (username,))
            p_row = cursor.fetchone()
            program_code = p_row['program_code'] if p_row else None

            if not program_code:
                flash("You do not have a program code assigned. Please contact an admin.", "error")
                return redirect(url_for('faculty.dashboard'))

            cursor.execute("SELECT id, class_name FROM classes WHERE program_code = %s ORDER BY class_name ASC", (program_code,))
            classes = cursor.fetchall()
            class_map = {str(c['id']): c['class_name'] for c in classes}

            cursor.execute("SELECT clerk_id, first_name, last_name, email FROM users WHERE program_code = %s ORDER BY last_name ASC", (program_code,))
            students = cursor.fetchall()
            student_map = {s['clerk_id']: get_display_name(s['first_name'], s['last_name'], s['email']) for s in students}

            cursor.execute("SELECT DISTINCT quiz_name FROM assigned_quizzes WHERE program_code = %s ORDER BY quiz_name ASC", (program_code,))
            assigned_quizzes = [r['quiz_name'] for r in cursor.fetchall() if r['quiz_name']]

            cursor.execute("SELECT DISTINCT remediation_name FROM assigned_remediations WHERE program_code = %s ORDER BY remediation_name ASC", (program_code,))
            assigned_remediations = [r['remediation_name'] for r in cursor.fetchall() if r['remediation_name']]

            view_class_id = request.args.get('class_id')
            view_clerk_id = request.args.get('clerk_id')
            view_type = request.args.get('type')
            view_name = request.args.get('name')

            results = []
            view_title = ""

            if view_class_id:
                view_title = f"Assignments for: {class_map.get(view_class_id, 'Selected Class')}"
                cursor.execute("SELECT id, quiz_name as name, due_date, 'quiz' as type FROM assigned_quizzes WHERE program_code = %s AND class_id = %s ORDER BY name ASC", (program_code, view_class_id))
                results.extend(cursor.fetchall())
                cursor.execute("SELECT id, remediation_name as name, due_date, 'remediation' as type FROM assigned_remediations WHERE program_code = %s AND class_id = %s ORDER BY name ASC", (program_code, view_class_id))
                results.extend(cursor.fetchall())

            elif view_clerk_id:
                view_title = f"Assignments for: {student_map.get(view_clerk_id, 'Selected Student')}"
                cursor.execute("SELECT id, quiz_name as name, due_date, 'quiz' as type FROM assigned_quizzes WHERE program_code = %s AND clerk_id = %s ORDER BY name ASC", (program_code, view_clerk_id))
                results.extend(cursor.fetchall())
                cursor.execute("SELECT id, remediation_name as name, due_date, 'remediation' as type FROM assigned_remediations WHERE program_code = %s AND clerk_id = %s ORDER BY name ASC", (program_code, view_clerk_id))
                results.extend(cursor.fetchall())

            elif view_type and view_name:
                view_title = f"Assigned to: {view_name}"
                if view_type == 'quiz':
                    cursor.execute("SELECT id, class_id, clerk_id, due_date FROM assigned_quizzes WHERE program_code = %s AND quiz_name = %s ORDER BY assigned_date DESC", (program_code, view_name))
                    records = cursor.fetchall()
                    for r in records:
                        if r['class_id']:
                            r['target_name'] = f"Class: {class_map.get(str(r['class_id']), 'Unknown Class')}"
                        elif r['clerk_id']:
                            r['target_name'] = f"Student: {student_map.get(str(r['clerk_id']), 'Unknown Student')}"
                        else:
                            r['target_name'] = 'Entire Program'
                        r['type'] = 'quiz'
                        results.append(r)
                elif view_type == 'remediation':
                    cursor.execute("SELECT id, class_id, clerk_id, due_date FROM assigned_remediations WHERE program_code = %s AND remediation_name = %s ORDER BY assigned_date DESC", (program_code, view_name))
                    records = cursor.fetchall()
                    for r in records:
                        if r['class_id']:
                            r['target_name'] = f"Class: {class_map.get(str(r['class_id']), 'Unknown Class')}"
                        elif r['clerk_id']:
                            r['target_name'] = f"Student: {student_map.get(str(r['clerk_id']), 'Unknown Student')}"
                        else:
                            r['target_name'] = 'Entire Program'
                        r['type'] = 'remediation'
                        results.append(r)
    finally:
        conn.close()

    return render_template('faculty_review_assigned.html',
                           classes=classes, students=students,
                           assigned_quizzes=assigned_quizzes,
                           assigned_remediations=assigned_remediations,
                           results=results, view_title=view_title)

@faculty_bp.route('/faculty/delete_assigned_record', methods=['POST'])
@login_required
def delete_assigned_record():
    username = session.get('username')
    if not is_user_faculty(username):
        return redirect(url_for('index'))

    record_id = request.form.get('record_id')
    assignment_type = request.form.get('assignment_type')

    return_class_id = request.form.get('return_class_id')
    return_type = request.form.get('return_type')
    return_name = request.form.get('return_name')

    conn = get_mysql_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT program_code FROM users WHERE clerk_id = %s", (username,))
            p_row = cursor.fetchone()
            program_code = p_row['program_code'] if p_row else None

            if not program_code:
                flash("You do not have a program code assigned.", "error")
                return redirect(url_for('faculty.review_assigned'))

            if assignment_type == 'quiz':
                cursor.execute("DELETE FROM assigned_quizzes WHERE id = %s AND program_code = %s", (record_id, program_code))
            elif assignment_type == 'remediation':
                cursor.execute("DELETE FROM assigned_remediations WHERE id = %s AND program_code = %s", (record_id, program_code))

            conn.commit()
            flash("Assignment successfully unlinked.", "success")
    except Exception as e:
        flash(f"Error removing assignment: {e}", "error")
    finally:
        conn.close()

    if return_class_id:
        return redirect(url_for('faculty.review_assigned', class_id=return_class_id))
    elif return_type and return_name:
        return redirect(url_for('faculty.review_assigned', type=return_type, name=return_name))

    return redirect(url_for('faculty.review_assigned'))