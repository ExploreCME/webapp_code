##flask_app.py
import json
import random
from datetime import datetime
import os
import threading
from urllib.parse import unquote, urlparse, parse_qs
from flask import Blueprint, render_template, request, redirect, url_for, flash, g, session, current_app
from werkzeug.utils import secure_filename
import jwt
import uuid

# --- PERFORMANCE IMPORTS ---
from db_pool import get_db_connection

# --- IMPORT SHARED UNIQUE CHECK ---
from shared_utils import is_assignment_name_unique

# ==========================================
# BLUEPRINT INITIALIZATION
# ==========================================
quiz_bp = Blueprint('quiz', __name__, template_folder='/home/ps51632/mysite/explorecme/templates/template standardization')

# ==========================================
# MYSQL CONNECTION POOL (SCALABLE)
# ==========================================
def get_db():
    """Pulls a connection from the global pool. DDL statements have been removed."""
    if 'db' not in g:
        g.db = get_db_connection()
    return g.db

def get_user_quizzes_db():
    return get_db()

def is_quiz_name_unique(cursor, username, quiz_name):
    """Checks if a quiz name is already in use by this user (including pending generation tasks)."""
    cursor.execute('''
        SELECT 1 FROM user_quizzes WHERE username = %s AND TRIM(LOWER(quiz_name)) = TRIM(LOWER(%s))
        UNION
        SELECT 1 FROM quiz_generation_tasks WHERE username = %s AND TRIM(LOWER(quiz_name)) = TRIM(LOWER(%s)) AND status IN ('pending', 'processing')
    ''', (username, quiz_name, username, quiz_name))
    return cursor.fetchone() is None

@quiz_bp.teardown_request
def close_db(error):
    db = g.pop('db', None)
    if db is not None:
        try:
            db.close() # Returns connection to the pool rather than closing TCP socket
        except Exception:
            pass

@quiz_bp.after_request
def remove_frame_restrictions(response):
    response.headers["Content-Security-Policy"] = "frame-ancestors * https: http: data: blob: 'self'"
    if "X-Frame-Options" in response.headers:
        del response.headers["X-Frame-Options"]
    return response

@quiz_bp.before_request
def require_eula():
    # Endpoints that are safe to access without EULA acceptance
    exempt_endpoints = ['quiz.view_eula', 'quiz.accept_eula', 'static']
    if request.endpoint in exempt_endpoints or (request.endpoint and request.endpoint.startswith('static')):
        return

    # FAST PATH: Check encrypted session memory first (Zero DB overhead)
    if session.get('accepted_eula'):
        return

    # 1. STRICTLY READ IDENTITY FROM CLERK
    clerk_token = request.cookies.get('__session')

    # Let normal routes kick unauthenticated users to the login screen
    if not clerk_token:
        return

    try:
        decoded_token = jwt.decode(clerk_token, options={"verify_signature": False})
        username = decoded_token.get('sub')
    except Exception:
        return

    if not username:
        return

    # 2. CHECK THE DATABASE (Only runs once per session login)
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT accepted_eula FROM users WHERE clerk_id = %s", (username,))
        result = cursor.fetchone()

        # 3. IF ACCEPTED, CACHE IN SESSION. OTHERWISE, FORCE REDIRECT.
        if result and result.get('accepted_eula') == 1:
            session['accepted_eula'] = True
            return
        else:
            session['accepted_eula'] = False  # Added negative state caching to prevent DB spam
            return redirect(url_for('quiz.view_eula'))

    except Exception as e:
        print(f"EULA Interceptor Error: {e}")
        # FAIL CLOSED: Force redirect if anything breaks
        return redirect(url_for('quiz.view_eula'))

@quiz_bp.before_request
def restrict_guest_users():
    """
    Acts as a bouncer for guest users. If they try to navigate away from
    the allowed quiz/remediation routes, their session is destroyed.
    """
    username = session.get('username', '')

    if username.startswith('guest_'):
        # List of routes the guest is allowed to use
        allowed_endpoints = [
            'quiz.start_quiz',
            'quiz.question_page',
            'quiz.check_answer',
            'quiz.quiz_mode',
            'quiz.remediation_question_page',
            'quiz.remediation_modules',
            'quiz.view_remediation_flashcard',
            'quiz.quiz_results',
            'quiz.review_attempt',
            'quiz.save_flashcard_progress',
            'quiz.guest_exit', # Allowed explicit exit route
            'preview.preview_menu',
            'preview.preview_take'
        ]

        # If they try to go anywhere else (like /dashboard), kill the session and kick them
        if request.endpoint and request.endpoint not in allowed_endpoints and not request.endpoint.startswith('static'):
            session.pop('username', None)
            flash("Sample session ended. Please sign in to access the full application.", "warning")
            return redirect(url_for('index'))

# ==========================================
# ROUTE: GUEST EXIT HANDLER
# ==========================================
@quiz_bp.route('/guest_exit')
def guest_exit():
    """Silently pops the guest session and returns them to the landing page."""
    session.pop('username', None)
    return redirect(url_for('index'))


UPLOAD_FOLDER = '/home/ps51632/mysite/explorecme/uploads'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# ==========================================
# ROUTE: EULA ACCEPTANCE
# ==========================================
@quiz_bp.route('/eula', methods=['GET'])
def view_eula():
    if not request.cookies.get('__session'):
        return redirect(url_for('index'))
    return render_template('eula.html')

@quiz_bp.route('/accept_eula', methods=['POST'])
def accept_eula():
    clerk_token = request.cookies.get('__session')
    if not clerk_token:
        return redirect(url_for('index'))
    try:
        decoded_token = jwt.decode(clerk_token, options={"verify_signature": False})
        username = decoded_token.get('sub')
    except Exception:
        return redirect(url_for('index'))
    if not username:
        return redirect(url_for('index'))

    try:
        conn = get_db()
        cursor = conn.cursor()

        cursor.execute("UPDATE users SET accepted_eula = 1 WHERE clerk_id = %s", (username,))

        if cursor.rowcount == 0:
            cursor.execute("INSERT INTO users (clerk_id, accepted_eula, has_paid) VALUES (%s, 1, 0)", (username,))
        conn.commit()

        # Update session immediately
        session['accepted_eula'] = True
    except Exception as e:
        print(f"Error updating EULA status: {e}")
    flash("Thank you for accepting the End User Agreement.", "success")
    return redirect(url_for('quiz.dashboard'))

# ==========================================
# ROUTE: STUDENT DASHBOARD
# ==========================================
@quiz_bp.route('/student_dashboard')
def student_dashboard():
    if 'username' not in session:
        return redirect(url_for('index'))

    username = session['username']
    conn = get_db()
    cursor = conn.cursor()

    # 1. Fetch user assignment criteria
    cursor.execute("SELECT program_code FROM users WHERE clerk_id = %s", (username,))
    p_res = cursor.fetchone()
    program_code = p_res.get('program_code') if p_res and p_res.get('program_code') else 'NONE'

    cursor.execute("SELECT class_id FROM class_members WHERE clerk_id = %s", (username,))
    student_class_ids = [int(row['class_id']) for row in cursor.fetchall() if row.get('class_id') is not None]

    upcoming_assignments = []
    now = datetime.now()

    # 2. Fetch Assignments
    try:
        assign_conds = [
            "assigned_by = %s",
            "clerk_id = %s",
            "(class_id IS NULL AND clerk_id IS NULL AND program_code IN (%s, 'ALL', 'NONE'))"
        ]
        assign_params = [username, username, program_code]

        if student_class_ids:
            placeholders = ','.join(['%s'] * len(student_class_ids))
            assign_conds.append(f"class_id IN ({placeholders})")
            assign_params.extend(student_class_ids)

        assign_where = " OR ".join(assign_conds)

        # Quizzes
        cursor.execute(f"SELECT quiz_name, due_date FROM assigned_quizzes WHERE ({assign_where}) AND due_date IS NOT NULL AND due_date >= NOW()", assign_params)
        for row in cursor.fetchall():
            days_away = (row['due_date'] - now).days
            upcoming_assignments.append({
                'name': row['quiz_name'],
                'type': 'Quiz',
                'due_date_str': row['due_date'].strftime("%b %d, %Y"),
                'days_away': days_away if days_away >= 0 else 0
            })

        # Remediations
        cursor.execute(f"SELECT remediation_name, due_date FROM assigned_remediations WHERE ({assign_where}) AND due_date IS NOT NULL AND due_date >= NOW()", assign_params)
        for row in cursor.fetchall():
            days_away = (row['due_date'] - now).days
            upcoming_assignments.append({
                'name': row['remediation_name'],
                'type': 'Remediation',
                'due_date_str': row['due_date'].strftime("%b %d, %Y"),
                'days_away': days_away if days_away >= 0 else 0
            })

    except Exception as e:
        print(f"Error fetching upcoming assignments: {e}")

    # Sort them so the most urgent assignments appear at the top
    upcoming_assignments.sort(key=lambda x: x['days_away'])

    return render_template('student_dashboard.html', username=username, upcoming_assignments=upcoming_assignments)

# ==========================================
# ROUTE: DASHBOARD
# ==========================================
@quiz_bp.route('/dashboard')
def dashboard():
    clerk_token = request.cookies.get('__session')
    if not clerk_token:
        return redirect(url_for('index'))

    username = session.get('username')

    # Fast path: check session
    if session.get('is_premium'):
        return render_template('dashboard.html', username=username, is_premium=True)

    try:
        decoded_token = jwt.decode(clerk_token, options={"verify_signature": False})
        username = decoded_token.get('sub')
        if not username:
            return redirect(url_for('index'))

        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT has_paid FROM users WHERE clerk_id = %s", (username,))
        result = cursor.fetchone()

        is_premium = bool(result and result.get('has_paid') == 1)
        session['is_premium'] = is_premium

    except Exception:
        return redirect(url_for('index'))

    if not is_premium:
        return redirect(url_for('db_dashboard'))

    return render_template('dashboard.html', username=username, is_premium=is_premium)

# ==========================================
# ROUTE: BACKGROUND CLERK USER SYNC
# ==========================================
@quiz_bp.route('/sync_clerk_user', methods=['POST'])
def sync_clerk_user():
    data = request.json
    clerk_id = data.get('clerk_id')
    first_name = data.get('first_name', '')
    last_name = data.get('last_name', '')
    email = data.get('email', '')

    if not clerk_id:
        return current_app.response_class(response=json.dumps({"status": "error"}), status=400, mimetype='application/json')

    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM users WHERE clerk_id = %s", (clerk_id,))
        if cursor.fetchone():
            cursor.execute("""
                UPDATE users
                SET first_name = %s, last_name = %s, email = %s
                WHERE clerk_id = %s
            """, (first_name, last_name, email, clerk_id))
        else:
            cursor.execute("""
                INSERT INTO users (clerk_id, first_name, last_name, email, accepted_eula, has_paid)
                VALUES (%s, %s, %s, %s, 0, 0)
            """, (clerk_id, first_name, last_name, email))
        conn.commit()
        return current_app.response_class(response=json.dumps({"status": "success"}), mimetype='application/json')
    except Exception as e:
        print(f"Clerk Sync Error: {e}")
        return current_app.response_class(response=json.dumps({"status": "error"}), status=500, mimetype='application/json')

# ==========================================
# ROUTE: CONSOLIDATED UPLOAD & GENERATE (TASK QUEUE)
# ==========================================
@quiz_bp.route('/generate_from_document', methods=['POST'])
def generate_from_document():
    if 'username' not in session:
        return redirect(url_for('index'))
    username = session.get('username')
    secured_username = secure_filename(username)

    if 'file' not in request.files:
        flash('No file was uploaded.', 'error')
        return redirect(url_for('quiz.menu'))
    file = request.files['file']
    if file.filename == '':
        flash('No file selected.', 'error')
        return redirect(url_for('quiz.menu'))

    quiz_name = request.form.get('quiz_name')
    if not quiz_name:
        flash('Please provide a quiz name.', 'error')
        return redirect(url_for('quiz.menu'))

    try:
        num_per_topic = max(1, min(int(request.form.get('num_per_topic', 1)), 5))
    except ValueError:
        num_per_topic = 1

    use_existing = True
    conn = get_db()
    cursor = conn.cursor()

    # --- UNIQUE NAME CHECK ---
    if not is_quiz_name_unique(cursor, username, quiz_name):
        flash(f'The quiz name "{quiz_name}" is already in use. Please choose a unique name.', 'error')
        return redirect(url_for('quiz.menu'))
    # -------------------------

    quiz_id = uuid.uuid4().hex
    cursor.execute('INSERT IGNORE INTO user_quizzes (quiz_id, username, quiz_name, created_date) VALUES (%s, %s, %s, %s)',
                   (quiz_id, username, quiz_name, datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
    conn.commit()

    filename = f"{secured_username}_{secure_filename(file.filename)}"
    filepath = os.path.join(UPLOAD_FOLDER, filename)
    file.save(filepath)

    extracted_topics_filename = f"{secured_username}_{secure_filename(quiz_name)}_extracted_topics.json"
    output_base_name = f"{secured_username}_{secure_filename(quiz_name)}"

    task_payload = json.dumps({
        "filepath": filepath,
        "quiz_id": quiz_id,
        "extracted_topics_path": os.path.join(UPLOAD_FOLDER, extracted_topics_filename),
        "final_output_json_path": os.path.join(UPLOAD_FOLDER, f"{output_base_name}_output.json"),
        "num_per_topic": num_per_topic,
        "use_existing": use_existing
    })

    cursor.execute('''
        INSERT INTO quiz_generation_tasks (username, quiz_name, task_type, payload, status, created_at, updated_at)
        VALUES (%s, %s, %s, %s, 'pending', NOW(), NOW())
    ''', (username, quiz_name, 'document', task_payload))
    conn.commit()

    flash(f'Document uploaded! Your quiz "{quiz_name}" is now queued for background generation.', 'success')
    return redirect(url_for('quiz.home'))

# ==========================================
# ROUTE: MENU
# ==========================================
@quiz_bp.route('/menu')
def menu():
    if 'username' not in session:
        return redirect(url_for('index'))

    username = session['username']

    # 1. Check session first
    is_faculty = session.get('is_faculty', False)

    # 2. If not in session, check the database
    if not is_faculty:
        try:
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute("SELECT is_faculty FROM users WHERE clerk_id = %s", (username,))
            user_row = cursor.fetchone()

            if user_row and user_row.get('is_faculty') is not None:
                val = str(user_row['is_faculty']).strip().lower()
                if val in ['1', 'y', 'yes', 'true']:
                    is_faculty = True
                    session['is_faculty'] = True # Save to session for next time
        except Exception as e:
            print(f"Error checking MySQL faculty status: {e}")

    # 3. Pass faculty_route=is_faculty to the template
    return render_template('menu.html', username=username, faculty_route=is_faculty)

@quiz_bp.route('/home', methods=['GET'])
def home():
    if 'username' not in session:
        return redirect(url_for('index'))
    username = session.get('username')
    json_files, user_raw_files, generating_quizzes = [], [], []

    if username:
        secured_username = secure_filename(username)
        if os.path.exists(UPLOAD_FOLDER):
            all_files = os.listdir(UPLOAD_FOLDER)
            json_files = sorted([f for f in all_files if f.startswith(secured_username + '_') and f.endswith('_output.json')])
            user_raw_files = sorted([f for f in all_files if f.startswith(secured_username + '_') and not f.endswith('_output.json') and not f.endswith('_extracted_topics.json')])
        try:
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute('''
                SELECT quiz_name FROM quiz_generation_tasks
                WHERE username = %s AND status IN ('pending', 'processing')
            ''', (username,))
            generating_quizzes = [row['quiz_name'] for row in cursor.fetchall()]
        except Exception:
            pass

    return render_template('home.html', json_files=json_files, username=username, user_raw_files=user_raw_files, generating_quizzes=generating_quizzes)

# ==========================================
# ROUTE: API QUEUE POLLING (STATUS UI)
# ==========================================
@quiz_bp.route('/api/generation_status')
def generation_status():
    if 'username' not in session:
        return current_app.response_class(response=json.dumps({"status": "error", "message": "Unauthorized"}), status=401, mimetype='application/json')
    username = session.get('username')
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT quiz_name, status, created_at, updated_at
            FROM quiz_generation_tasks
            WHERE username = %s AND status IN ('pending', 'processing')
        ''', (username,))
        tasks = cursor.fetchall()
        for t in tasks:
            if t.get('created_at'):
                t['created_at'] = str(t['created_at'])
            if t.get('updated_at'):
                t['updated_at'] = str(t['updated_at'])

        return current_app.response_class(response=json.dumps({"status": "success", "tasks": tasks}), mimetype='application/json')
    except Exception as e:
        return current_app.response_class(response=json.dumps({"status": "error", "message": str(e)}), status=500, mimetype='application/json')

# ==========================================
# ROUTE: SELECT QUIZ PAGE (SCALABLE MYSQL)
# ==========================================
@quiz_bp.route('/select_quiz')
def select_quiz():
    if 'username' not in session:
        return redirect(url_for('index'))
    username = session.get('username')

    conn = get_db()
    cursor = conn.cursor()

    # 1. Fetch User Data & Cast Classes to Integers
    cursor.execute("SELECT has_paid, program_code FROM users WHERE clerk_id = %s", (username,))
    user_res = cursor.fetchone()

    if not user_res or user_res.get('has_paid') != 1:
        flash("You need premium access to view assignments.", "error")
        return redirect(url_for('quiz.dashboard'))

    program_code = (user_res.get('program_code') or 'NONE').strip().upper()
    cursor.execute("SELECT class_id FROM class_members WHERE clerk_id = %s", (username,))

    # Strictly cast to int so MySQL IN() queries use indexes efficiently
    student_class_ids = [int(row['class_id']) for row in cursor.fetchall() if row.get('class_id') is not None]

    quiz_data = []
    now = datetime.now()

    try:
        # =========================================================
        # PHASE 1: SCALABLE ASSIGNMENT LOOKUP
        # =========================================================
        assign_conds = [
            "assigned_by = %s",
            "clerk_id = %s",
            "(class_id IS NULL AND clerk_id IS NULL AND program_code IN (%s, 'ALL', 'NONE'))"
        ]
        assign_params = [username, username, program_code]

        if student_class_ids:
            placeholders = ','.join(['%s'] * len(student_class_ids))
            assign_conds.append(f"class_id IN ({placeholders})")
            assign_params.extend(student_class_ids)

        assign_where = " OR ".join(assign_conds)

        cursor.execute(f"SELECT quiz_name, due_date FROM assigned_quizzes WHERE {assign_where}", assign_params)
        assigned_q_map = {str(row['quiz_name']).strip().lower(): row['due_date'] for row in cursor.fetchall() if row['quiz_name']}

        cursor.execute(f"SELECT remediation_name, due_date FROM assigned_remediations WHERE {assign_where}", assign_params)
        assigned_r_map = {}
        for row in cursor.fetchall():
            rn = row['remediation_name']
            rn_clean = str(rn).strip() if rn and str(rn).strip() not in ['', 'None'] else "Unnamed Assignment"
            assigned_r_map[rn_clean.lower()] = row['due_date']

        # =========================================================
        # PHASE 2: SCALABLE CONTENT FETCHING
        # =========================================================
        q_names = list(assigned_q_map.keys())
        q_conds = ["uq.username = %s"]
        q_params = [username]

        if q_names:
            q_conds.append(f"LOWER(TRIM(uq.quiz_name)) IN ({','.join(['%s'] * len(q_names))})")
            q_params.extend(q_names)

        cursor.execute(f'''
            SELECT uq.quiz_name, COUNT(qqm.map_id) as q_count
            FROM user_quizzes uq
            LEFT JOIN quiz_questions_map qqm ON uq.quiz_id = qqm.quiz_id
            WHERE {" OR ".join(q_conds)}
            GROUP BY uq.quiz_id, uq.quiz_name
        ''', q_params)
        quizzes_db = cursor.fetchall()

        # Fetch Remediations with full total question counts
        r_names = list(assigned_r_map.keys())
        r_conds = ["ra.username = %s"]
        r_params = [username]

        if r_names:
            r_conds.append(f"LOWER(IFNULL(NULLIF(TRIM(ra.remediation_name), ''), 'Unnamed Assignment')) IN ({','.join(['%s'] * len(r_names))})")
            r_params.extend(r_names)

        cursor.execute(f'''
            SELECT IFNULL(NULLIF(TRIM(ra.remediation_name), ''), 'Unnamed Assignment') AS safe_r_name,
                   COUNT(DISTINCT ra.id) as mod_count,
                   COUNT(rq.id) as total_q
            FROM remediation_assignments ra
            LEFT JOIN remediation_questions rq ON ra.id = rq.assignment_id
            WHERE {" OR ".join(r_conds)}
            GROUP BY safe_r_name
        ''', r_params)
        rems_db = cursor.fetchall()

        # =========================================================
        # PHASE 3: SCALABLE ATTEMPT CACHING
        # =========================================================
        cursor.execute('''
            SELECT qa.quiz_name, qa.attempt_id, qa.mode, qa.start_time,
                   COUNT(ar.id) as answered_count, COALESCE(SUM(ar.is_correct), 0) as correct_answers
            FROM quiz_attempts qa
            LEFT JOIN attempt_responses ar ON qa.attempt_id = ar.attempt_id
            WHERE qa.username = %s
            GROUP BY qa.attempt_id, qa.quiz_name, qa.mode, qa.start_time
            ORDER BY qa.start_time DESC
        ''', (username,))
        attempts_dict = {}
        for att in cursor.fetchall():
            qn = str(att['quiz_name']).strip().lower() if att['quiz_name'] else ''
            if qn not in attempts_dict: attempts_dict[qn] = []
            attempts_dict[qn].append(att)

        # =========================================================
        # PHASE 4: O(1) PYTHON ASSEMBLY
        # =========================================================
        seen_items = set()

        for row in quizzes_db:
            qname = row['quiz_name']
            if not qname: continue
            safe_key = qname.strip().lower()

            if f"q_{safe_key}" in seen_items: continue
            seen_items.add(f"q_{safe_key}")

            total_q = row['q_count']
            is_assigned = safe_key in assigned_q_map
            due_date = assigned_q_map.get(safe_key)

            my_atts = attempts_dict.get(safe_key, [])
            att_list, is_inc, inc_count = [], False, 0

            for a in my_atts:
                ans, cor = a['answered_count'], a['correct_answers']
                status = 'completed' if ans >= total_q else 'incomplete'
                if status == 'incomplete':
                    is_inc = True
                    inc_count += 1
                score = f"{round((cor/ans)*100, 1)}%" if ans > 0 else "0%"
                st_time = str(a['start_time']) if a.get('start_time') else ''

                att_list.append({
                    'attempt_id': a['attempt_id'], 'mode': a['mode'], 'start_time': st_time,
                    'answered_count': ans, 'total_questions': total_q, 'status': status, 'score_display': score
                })

            quiz_data.append({
                'name': qname, 'count': f"{total_q} Qs", 'source': 'faculty' if is_assigned else 'user',
                'is_incomplete': is_inc, 'incomplete_count': inc_count, 'has_attempts': len(att_list) > 0, 'attempts': att_list,
                'due_date': due_date,
                'formatted_due_date': due_date.strftime("%B %d, %Y at %I:%M %p") if isinstance(due_date, datetime) else None,
                'is_expired': True if (isinstance(due_date, datetime) and now > due_date) else False,
                'is_remediation': False
            })

        for row in rems_db:
            rname = row['safe_r_name']
            safe_key = rname.strip().lower()

            if f"r_{safe_key}" in seen_items: continue

            mod_count = row['mod_count']
            total_q = row['total_q']
            if mod_count <= 0: continue

            seen_items.add(f"r_{safe_key}")
            is_assigned = safe_key in assigned_r_map
            due_date = assigned_r_map.get(safe_key)

            my_atts = attempts_dict.get(safe_key, [])
            att_list, is_inc, inc_count = [], False, 0

            for a in my_atts:
                ans, cor = a['answered_count'], a['correct_answers']
                status = 'completed' if ans >= total_q else 'incomplete'
                if status == 'incomplete':
                    is_inc = True
                    inc_count += 1
                score = f"{round((cor/ans)*100, 1)}%" if ans > 0 else "0%"
                st_time = str(a['start_time']) if a.get('start_time') else ''

                att_list.append({
                    'attempt_id': a['attempt_id'], 'mode': a['mode'], 'start_time': str(a['start_time']) if a.get('start_time') else '',
                    'answered_count': ans, 'total_questions': total_q, 'status': status, 'score_display': score
                })

            quiz_data.append({
                'name': rname, 'count': f"{mod_count} Modules ({total_q} Qs)", 'source': 'faculty' if is_assigned else 'user',
                'is_incomplete': is_inc, 'incomplete_count': inc_count, 'has_attempts': len(att_list) > 0, 'attempts': att_list,
                'due_date': due_date,
                'formatted_due_date': due_date.strftime("%B %d, %Y at %I:%M %p") if isinstance(due_date, datetime) else None,
                'is_expired': True if (isinstance(due_date, datetime) and now > due_date) else False,
                'is_remediation': True
            })

    except Exception as e:
        print(f"Error fetching assignment data: {e}")
        flash("An error occurred while loading your assignments.", "error")

    quiz_data.sort(key=lambda x: x['name'].lower())
    return render_template('select_quiz.html', username=username, quizzes=quiz_data)

# ==========================================
# ROUTE: DELETE QUIZ (MYSQL)
# ==========================================
@quiz_bp.route('/delete_quiz', methods=['POST'])
def delete_quiz():
    if 'username' not in session:
        return redirect(url_for('index'))
    quiz_name = request.form.get('quiz_name')
    username = session.get('username')

    if not quiz_name:
        flash("Quiz name is required for deletion.", "error")
        return redirect(url_for('quiz.select_quiz'))

    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT quiz_id FROM user_quizzes WHERE username = %s AND quiz_name = %s", (username, quiz_name))
        q_row = cursor.fetchone()

        if q_row:
            quiz_id = q_row['quiz_id']
            cursor.execute("DELETE FROM quiz_questions_map WHERE quiz_id = %s", (quiz_id,))
            cursor.execute("""
                DELETE ar FROM attempt_responses ar
                JOIN quiz_attempts qa ON ar.attempt_id = qa.attempt_id
                WHERE qa.username = %s AND qa.quiz_name = %s
            """, (username, quiz_name))
            cursor.execute("DELETE FROM quiz_attempts WHERE username = %s AND quiz_name = %s", (username, quiz_name))
            cursor.execute("DELETE FROM user_quizzes WHERE quiz_id = %s", (quiz_id,))
            cursor.execute("DELETE FROM quiz_generation_tasks WHERE username = %s AND quiz_name = %s", (username, quiz_name))
            conn.commit()
            flash(f'Quiz "{quiz_name}" was successfully removed.', 'success')
        else:
            flash("Quiz not found or you do not have permission to delete it.", "error")
    except Exception as e:
        print(f"Error deleting quiz: {e}")
        flash("An error occurred while deleting the quiz.", "error")
    return redirect(url_for('quiz.select_quiz'))

# ==========================================
# ROUTE: USER DEFINED QUIZ (BLUEPRINT GENERATOR)
# ==========================================
@quiz_bp.route('/custom_quiz', methods=['GET', 'POST'])
def custom_quiz():
    if 'username' not in session:
        return redirect(url_for('index'))
    username = session.get('username')
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

    is_faculty = False
    if session.get('is_faculty'):
        is_faculty = True
    else:
        try:
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute("SELECT is_faculty FROM users WHERE clerk_id = %s", (username,))
            user_row = cursor.fetchone()
            if user_row and user_row.get('is_faculty') is not None:
                val = str(user_row['is_faculty']).strip().lower()
                if val in ['1', 'y', 'yes', 'true']:
                    is_faculty = True
                    session['is_faculty'] = True
        except Exception as e:
            print(f"Error checking MySQL faculty status: {e}")

    if not is_faculty:
        faculty_flag = request.args.get('faculty') or request.form.get('faculty') or ''
        if not faculty_flag and request.referrer:
            parsed_ref = urlparse(request.referrer)
            ref_args = parse_qs(parsed_ref.query)
            faculty_flag = ref_args.get('faculty', [''])[0]
        if faculty_flag.strip().lower() in ['y', 'yes', 'true', '1']:
            is_faculty = True
            session['is_faculty'] = True

    if request.method == 'POST':
        num_questions = min(int(request.form.get('num_questions', 10)), 120)
        quiz_name = request.form.get('quiz_name')

        # --- UNIQUE NAME CHECK (Early UI rejection) ---
        conn = get_db()
        cursor = conn.cursor()
        if not is_quiz_name_unique(cursor, username, quiz_name):
            return current_app.response_class(
                response=json.dumps({
                    "status": "error",
                    "message": f"The quiz name '{quiz_name}' is already in use. Please choose a unique name."
                }),
                mimetype='application/json'
            )
        # ----------------------------------------------

        if is_faculty:
            use_existing_val = request.form.get('use_existing', 'yes')
            use_existing = str(use_existing_val).lower() in ['yes', 'y', 'true', '1']
        else:
            use_existing = True

        organ_percentages, organ_selected_topics, task_percentages = {}, {}, {}
        for organ in organ_data.keys():
            safe_name = organ.replace(" ", "_").replace("/", "_")
            pct_val = request.form.get(f'organ_pct_{safe_name}')
            topics_val = request.form.getlist(f'organ_topics_{safe_name}')
            if pct_val and pct_val.isdigit() and int(pct_val) > 0:
                organ_percentages[organ] = int(pct_val)
                if topics_val:
                    organ_selected_topics[organ] = topics_val

        for task in task_areas:
            safe_name = task.replace(" ", "_").replace(",", "")
            pct_val = request.form.get(f'task_pct_{safe_name}')
            if pct_val and pct_val.isdigit() and int(pct_val) > 0:
                task_percentages[task] = int(pct_val)

        def get_exact_counts(percentages_dict, total_q, fallback_keys):
            counts = {}
            if not percentages_dict:
                return {k: 0 for k in fallback_keys}, True
            allocated = 0
            for k, v in percentages_dict.items():
                exact = round(total_q * (v / 100.0))
                counts[k] = exact
                allocated += exact
            if (diff := total_q - allocated) != 0 and counts:
                counts[max(counts, key=counts.get)] += diff
            return counts, False

        organ_counts, random_organs = get_exact_counts(organ_percentages, num_questions, list(organ_data.keys()))
        task_counts, random_tasks = get_exact_counts(task_percentages, num_questions, task_areas)

        organ_pool, task_pool = [], []

        # Build Organ Pool
        if not random_organs:
            for o, count in organ_counts.items():
                organ_pool.extend([o] * count)
            random.shuffle(organ_pool)
        else:
            organ_pool = ["any"] * num_questions

        # Build Task Pool
        if not random_tasks:
            for t, count in task_counts.items():
                task_pool.extend([t] * count)
            random.shuffle(task_pool)
        else:
            task_pool = ["any"] * num_questions

        # Pad with "any" if rounding left the pools short
        while len(organ_pool) < num_questions:
            organ_pool.append("any")
        while len(task_pool) < num_questions:
            task_pool.append("any")

        organ_pool, task_pool = organ_pool[:num_questions], task_pool[:num_questions]

        # --- FIXED TOPIC LOGIC ---
        quiz_blueprint = []
        for i in range(num_questions):
            org = organ_pool[i]

            # If the user selected specific topics, pick one randomly.
            # Otherwise, use 'any' so the database just filters by organ system and task area.
            if org in organ_selected_topics and organ_selected_topics[org]:
                chosen_topic = random.choice(organ_selected_topics[org])
            else:
                chosen_topic = "any"

            quiz_blueprint.append({
                "organ_system": org,
                "task_area": task_pool[i],
                "topic_area": chosen_topic
            })
        # -------------------------

        return current_app.response_class(
            response=json.dumps({
                "status": "success",
                "blueprint": quiz_blueprint,
                "quiz_name": quiz_name,
                "use_existing": use_existing
            }),
            mimetype='application/json'
        )

    return render_template('custom_quiz.html', organ_data=organ_data, task_areas=task_areas, is_faculty=is_faculty)

# ==========================================
# BATCH GENERATION ROUTE (MYSQL - TASK QUEUE + HYBRID FALLBACK)
# ==========================================
@quiz_bp.route('/build_quiz_batch', methods=['POST'])
def build_quiz_batch():
    if 'username' not in session:
        return current_app.response_class(response=json.dumps({"status": "error"}), status=401, mimetype='application/json')

    data = request.json
    quiz_name = data.get('quiz_name')
    blueprint = data.get('blueprint', [])
    use_existing = data.get('use_existing', True)
    username = session['username']

    conn = get_db()
    cursor = conn.cursor()

    # --- UNIQUE NAME CHECK ---
    if not is_quiz_name_unique(cursor, username, quiz_name):
        return current_app.response_class(
            response=json.dumps({
                "status": "error",
                "message": f"The quiz name '{quiz_name}' is already in use. Please choose a unique name."
            }),
            mimetype='application/json'
        )
    # -------------------------

    quiz_id = uuid.uuid4().hex
    cursor.execute(
        'INSERT IGNORE INTO user_quizzes (quiz_id, username, quiz_name, created_date) VALUES (%s, %s, %s, %s)',
        (quiz_id, username, quiz_name, datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    )
    conn.commit()

    cursor.execute('SELECT question_id FROM quiz_questions_map WHERE quiz_id = %s', (quiz_id,))
    used_ids = {row['question_id'] for row in cursor.fetchall()}

    candidate_pool = {}
    if use_existing and blueprint:
        cursor.execute('SELECT id, TRIM(organ_system) as os, TRIM(task_area) as ta, TRIM(topic_area) as to_area FROM question_bank')
        all_qs = cursor.fetchall()
        for q in all_qs:
            key = ((q['os'] or '').lower(), (q['ta'] or '').lower(), (q['to_area'] or '').lower())
            if key not in candidate_pool:
                candidate_pool[key] = []
            candidate_pool[key].append(q['id'])

    PANCE_TASK_AREAS = [
        "History Taking and Performing Physical Examination",
        "Using Laboratory and Diagnostic Studies",
        "Formulating Most Likely Diagnosis",
        "Health Maintenance, Patient Education, and Preventative Measures",
        "Clinical Intervention",
        "Pharmaceutical Therapeutics",
        "Applying Basic Scientific Concepts"
    ]

    generation_requests = []
    mappings_to_insert = []

    for item in blueprint:
        o_sys = (item.get('organ_system') or '').strip().lower()
        ta_area = (item.get('task_area') or '').strip().lower()
        to_area = (item.get('topic_area') or '').strip().lower()

        # Strict wildcard filtering
        candidates = []
        for key, q_ids in candidate_pool.items():
            k_os, k_ta, k_to = key

            # 1. Match Organ System (Unless 'any')
            if o_sys and o_sys != 'any' and k_os != o_sys:
                continue

            # 2. Match Task Area (Unless 'any')
            if ta_area and ta_area != 'any' and k_ta != ta_area:
                continue

            # 3. Match Topic Area (STRICT match unless 'any')
            if to_area and to_area != 'any' and k_to != to_area:
                continue

            # If all strict filters pass, add the available questions
            candidates.extend([q_id for q_id in q_ids if q_id not in used_ids])

        if use_existing and candidates:
            selected_id = random.choice(candidates)
            used_ids.add(selected_id)
            mappings_to_insert.append((quiz_id, selected_id))
        else:
            # If we run out of exact topic matches, send it to AI Generation!
            original_ta = (item.get('task_area') or '').strip()
            gen_ta = original_ta if (original_ta and original_ta.lower() != 'any') else random.choice(PANCE_TASK_AREAS)

            generation_requests.append({
                "organ_system": item.get('organ_system'),
                "task_area": gen_ta,
                "topic_area": item.get('topic_area')
            })

    if mappings_to_insert:
        cursor.executemany('INSERT IGNORE INTO quiz_questions_map (quiz_id, question_id) VALUES (%s, %s)', mappings_to_insert)
        conn.commit()

    background_started = False
    if generation_requests:
        task_payload = json.dumps({
            "generation_requests": generation_requests,
            "quiz_id": quiz_id
        })
        cursor.execute('''
            INSERT INTO quiz_generation_tasks (username, quiz_name, task_type, payload, status, created_at, updated_at)
            VALUES (%s, %s, %s, %s, 'pending', NOW(), NOW())
        ''', (username, quiz_name, 'blueprint', task_payload))
        conn.commit()
        background_started = True

    return current_app.response_class(response=json.dumps({
        "status": "success",
        "duplicated": len(mappings_to_insert),
        "generated": len(generation_requests),
        "background_started": background_started
    }), mimetype='application/json')

# ==========================================
# SUBMIT ANSWER (MYSQL UPSERT)
# ==========================================
@quiz_bp.route('/submit-answer', methods=['POST'])
def check_answer():
    raw_question_id = request.form.get('question_id')
    user_choice = request.form.get('user_choice')
    selected_quiz_name = request.form.get('quiz_name')
    mode = request.form.get('mode', 'study_no_exp')
    attempt_id = request.form.get('attempt_id')
    next_q_id = request.form.get('next_q_id')

    if not raw_question_id or not user_choice:
        flash('Invalid submission. Please select an answer.', 'error')
        return redirect(url_for('quiz.question_page', q=raw_question_id, quiz_name=selected_quiz_name, mode=mode, attempt_id=attempt_id))

    conn = get_db()
    cursor = conn.cursor()

    if selected_quiz_name and selected_quiz_name.startswith('REMEDIATION_'):
        cursor.execute('SELECT correct_answer FROM remediation_questions WHERE id = %s', (raw_question_id,))
    else:
        cursor.execute('SELECT correct_answer FROM question_bank WHERE id = %s', (raw_question_id,))

    question = cursor.fetchone()

    if question:
        # --- BULLETPROOF BACKEND GRADER ---
        raw_db_ans = str(question['correct_answer']).upper().replace("OPTION", "").strip()
        clean_correct_letter = None

        for letter in ['A', 'B', 'C', 'D', 'E']:
            if raw_db_ans.startswith(f"{letter})") or raw_db_ans.startswith(f"{letter}.") or raw_db_ans == letter:
                clean_correct_letter = letter
                break

        if not clean_correct_letter:
            scrubbed = raw_db_ans.replace(")", "").replace(".", "").replace(" ", "")
            if len(scrubbed) > 0 and scrubbed[0] in ['A', 'B', 'C', 'D', 'E']:
                clean_correct_letter = scrubbed[0]

        if clean_correct_letter:
            is_correct = 1 if user_choice.strip().upper() == clean_correct_letter else 0
            correct_display = clean_correct_letter
        else:
            is_correct = 1 if user_choice.strip().upper() in raw_db_ans else 0
            correct_display = raw_db_ans

        if attempt_id:
            submitted_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cursor.execute('''
                INSERT INTO attempt_responses (attempt_id, question_id, user_choice, correct_answer, is_correct, submitted_time)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    user_choice = VALUES(user_choice),
                    correct_answer = VALUES(correct_answer),
                    is_correct = VALUES(is_correct),
                    submitted_time = VALUES(submitted_time)
            ''', (attempt_id, raw_question_id, user_choice.strip().upper(), correct_display, is_correct, submitted_time))
            conn.commit()

        if mode == 'test':
            if next_q_id and next_q_id != 'None':
                return redirect(url_for('quiz.question_page', q=next_q_id, quiz_name=selected_quiz_name, mode=mode, attempt_id=attempt_id))
            flash('Test Complete! Great job.', 'success')
            return redirect(url_for('quiz.quiz_results', attempt_id=attempt_id, quiz_name=selected_quiz_name))
        else:
            if is_correct:
                flash(f'Correct! ✨ The answer was {correct_display}.', 'success')
            else:
                flash(f'Incorrect. ❌ You chose {user_choice.upper()}, but the correct answer was {correct_display}.', 'error')
            return redirect(url_for('quiz.question_page', q=raw_question_id, answered='True', quiz_name=selected_quiz_name, mode=mode, attempt_id=attempt_id))
    else:
        flash('Question not found in the database.', 'error')
        return redirect(url_for('quiz.question_page', q=raw_question_id, answered='True', quiz_name=selected_quiz_name, mode=mode, attempt_id=attempt_id))

# ==========================================
# QUIZ MODE, ATTEMPTS, RESULTS (MYSQL)
# ==========================================
@quiz_bp.route('/question_page')
def question_page():
    raw_q = request.args.get('q')
    q = int(raw_q) if raw_q and raw_q.isdigit() else raw_q

    answered = request.args.get('answered') == 'True'
    selected_quiz_name = request.args.get('quiz_name', None)
    mode = request.args.get('mode', 'study_no_exp')
    attempt_id = request.args.get('attempt_id')
    go_to_num = request.args.get('go_to_num')

    if selected_quiz_name and selected_quiz_name.startswith('REMEDIATION_'):
        assignment_id = selected_quiz_name.replace('REMEDIATION_', '')
        return redirect(url_for('quiz.remediation_question_page', assignment_id=assignment_id, q=raw_q, answered=answered, attempt_id=attempt_id, mode=mode))

    conn = get_db()
    cursor = conn.cursor()
    question_ids = []

    if selected_quiz_name:
        cursor.execute('''
            SELECT qb.id
            FROM question_bank qb
            JOIN quiz_questions_map qqm ON qb.id = qqm.question_id
            JOIN user_quizzes uq ON qqm.quiz_id = uq.quiz_id
            WHERE uq.quiz_name = %s
            ORDER BY qqm.map_id ASC
        ''', (selected_quiz_name,))
        question_ids = [row['id'] for row in cursor.fetchall()]

        if mode == 'study_missed' and question_ids:
            cursor.execute('''
                SELECT DISTINCT ar.question_id
                FROM attempt_responses ar
                JOIN quiz_attempts qa ON ar.attempt_id = qa.attempt_id
                WHERE qa.username = %s AND qa.quiz_name = %s AND ar.is_correct = 0
            ''', (session.get('username'), selected_quiz_name))
            missed_set = {row['question_id'] for row in cursor.fetchall()}
            question_ids = [question for question in question_ids if question in missed_set]
            if not question_ids:
                flash("Great job! You have no previously missed questions for this quiz.", "success")
                return redirect(url_for('quiz.quiz_attempts', quiz_name=selected_quiz_name))

    if go_to_num and question_ids:
        try:
            target_index = int(go_to_num) - 1
            if 0 <= target_index < len(question_ids):
                q = question_ids[target_index]
            else:
                flash(f"Question number {go_to_num} is out of range.", "error")
        except ValueError:
            pass

    if not q and selected_quiz_name and question_ids:
        if attempt_id:
            cursor.execute('''
                SELECT qb.id
                FROM question_bank qb
                JOIN quiz_questions_map qqm ON qb.id = qqm.question_id
                JOIN user_quizzes uq ON qqm.quiz_id = uq.quiz_id
                LEFT JOIN attempt_responses ar ON qb.id = ar.question_id AND ar.attempt_id = %s
                WHERE uq.quiz_name = %s AND ar.id IS NULL
                ORDER BY qqm.map_id ASC LIMIT 1
            ''', (attempt_id, selected_quiz_name))
            result = cursor.fetchone()
            q = result['id'] if result else question_ids[0]
        else:
            q = question_ids[0]

    if not q:
        flash("No questions found for this quiz.", "error")
        return redirect(url_for('quiz.select_quiz'))

    current_question_number, next_q_id, prev_q_id = 0, None, None

    if q in question_ids:
        current_question_index = question_ids.index(q)
        current_question_number = current_question_index + 1
        if current_question_index + 1 < len(question_ids):
            next_q_id = question_ids[current_question_index + 1]
        if current_question_index > 0:
            prev_q_id = question_ids[current_question_index - 1]

    # --- FIXED: STR() CASTING TO PREVENT TYPE MISMATCH ---
    responses_dict = {}
    previous_choice = None

    if attempt_id:
        cursor.execute('SELECT question_id, is_correct, user_choice FROM attempt_responses WHERE attempt_id = %s', (attempt_id,))
        for row in cursor.fetchall():
            responses_dict[str(row['question_id'])] = row['is_correct']
            if str(row['question_id']) == str(q):
                previous_choice = row['user_choice']

    grid_data = []
    for idx, q_id in enumerate(question_ids):
        status = 'unanswered'
        if str(q_id) in responses_dict:
            if mode == 'test':
                status = 'answered'
            else:
                status = 'correct' if responses_dict[str(q_id)] == 1 else 'incorrect'
        grid_data.append({
            'question_number': idx + 1,
            'question_id': q_id,
            'status': status,
            'is_current': str(q_id) == str(q)
        })

    cursor.execute('SELECT * FROM question_bank WHERE id = %s', (q,))
    question_data = cursor.fetchone()

    if question_data:
        cursor.execute('''
            SELECT
                COUNT(*) as total_attempts,
                COALESCE(SUM(is_correct), 0) as total_correct,
                COALESCE(SUM(CASE WHEN user_choice = 'A' THEN 1 ELSE 0 END), 0) as a_count,
                COALESCE(SUM(CASE WHEN user_choice = 'B' THEN 1 ELSE 0 END), 0) as b_count,
                COALESCE(SUM(CASE WHEN user_choice = 'C' THEN 1 ELSE 0 END), 0) as c_count,
                COALESCE(SUM(CASE WHEN user_choice = 'D' THEN 1 ELSE 0 END), 0) as d_count,
                COALESCE(SUM(CASE WHEN user_choice = 'E' THEN 1 ELSE 0 END), 0) as e_count
            FROM attempt_responses
            WHERE question_id = %s
        ''', (q,))
        stats = cursor.fetchone()

        total = int(stats['total_attempts'] or 0)
        correct = int(stats['total_correct'] or 0)

        question_data['correct'] = correct
        question_data['incorrect'] = total - correct
        question_data['choice_a_count'] = int(stats['a_count'] or 0)
        question_data['choice_b_count'] = int(stats['b_count'] or 0)
        question_data['choice_c_count'] = int(stats['c_count'] or 0)
        question_data['choice_d_count'] = int(stats['d_count'] or 0)
        question_data['choice_e_count'] = int(stats['e_count'] or 0)

    return render_template('index.html', question=question_data, next_q_id=next_q_id, prev_q_id=prev_q_id, answered=answered, previous_choice=previous_choice,
                           quiz_name=selected_quiz_name, total_questions=len(question_ids), current_question_number=current_question_number,
                           mode=mode, attempt_id=attempt_id, grid_data=grid_data)

@quiz_bp.route('/quiz_mode')
def quiz_mode():
    # --- FREE PREVIEW GUEST BYPASS ---
    if request.args.get('is_preview') == 'true' and 'username' not in session:
        session['username'] = 'guest_' + uuid.uuid4().hex[:8]

    if 'username' not in session:
        return redirect(url_for('index'))

    quiz_name = request.args.get('quiz_name')

    # --- HARDCODED FIX FOR SAMPLE CONTENT ---
    if request.args.get('is_preview') == 'true':
        quiz_name = 'EOR 1 Remediation'

    if not quiz_name:
        return redirect(url_for('quiz.select_quiz'))

    username = session['username']
    conn = get_db()
    cursor = conn.cursor()

    is_remediation = False

    cursor.execute("SELECT 1 FROM remediation_assignments WHERE TRIM(LOWER(remediation_name)) = TRIM(LOWER(%s))", (quiz_name,))
    if cursor.fetchone():
        is_remediation = True

    cursor.execute("SELECT program_code FROM users WHERE clerk_id = %s", (username,))
    p_res = cursor.fetchone()
    program_code = p_res.get('program_code') if p_res and p_res.get('program_code') else 'NONE'

    cursor.execute("SELECT class_id FROM class_members WHERE clerk_id = %s", (username,))
    student_class_ids = [str(row['class_id']) for row in cursor.fetchall() if row.get('class_id') is not None]

    table_name = "assigned_remediations" if is_remediation else "assigned_quizzes"
    name_col = "remediation_name" if is_remediation else "quiz_name"

    # Incorporating explicit clerk_id lookup for exact student assignments
    assign_conds = [
        "clerk_id = %s",
        "(class_id IS NULL AND clerk_id IS NULL AND program_code = %s)"
    ]
    assign_params = [quiz_name, username, program_code]

    if student_class_ids:
        placeholders = ','.join(['%s'] * len(student_class_ids))
        assign_conds.append(f"class_id IN ({placeholders})")
        assign_params.extend(student_class_ids)

    assign_where = " OR ".join(assign_conds)

    cursor.execute(f'''
        SELECT due_date, required_mode FROM {table_name}
        WHERE {name_col} = %s AND ({assign_where})
        ORDER BY assigned_date DESC LIMIT 1
    ''', assign_params)
    assign_row = cursor.fetchone()

    if assign_row and assign_row.get('due_date'):
        if datetime.now() > assign_row['due_date']:
            flash(f"Access denied: The deadline for '{quiz_name}' has passed.", "error")
            return redirect(url_for('quiz.select_quiz'))

    if is_remediation:
        cursor.execute('''
            SELECT COUNT(rq.id) as total FROM remediation_questions rq
            JOIN remediation_assignments ra ON rq.assignment_id = ra.id
            WHERE TRIM(LOWER(ra.remediation_name)) = TRIM(LOWER(%s))
        ''', (quiz_name,))
    else:
        cursor.execute('''
            SELECT COUNT(qqm.map_id) as total FROM quiz_questions_map qqm
            JOIN user_quizzes uq ON qqm.quiz_id = uq.quiz_id
            WHERE uq.quiz_name = %s
        ''', (quiz_name,))

    total_q_res = cursor.fetchone()
    total_questions = total_q_res['total'] if total_q_res else 0

    forced_mode = None
    if assign_row and assign_row.get('required_mode') and assign_row['required_mode'] != 'any':
        req_mode = assign_row['required_mode']
        if req_mode == 'test':
            cursor.execute('''
                SELECT qa.attempt_id, COUNT(ar.id) as answered
                FROM quiz_attempts qa
                LEFT JOIN attempt_responses ar ON qa.attempt_id = ar.attempt_id
                WHERE qa.username = %s AND qa.quiz_name = %s AND qa.mode = 'test'
                GROUP BY qa.attempt_id
            ''', (username, quiz_name))
            if not any(att['answered'] >= total_questions for att in cursor.fetchall()):
                forced_mode = 'test'
        else:
            forced_mode = req_mode

    # Fetch ALL incomplete attempts to pass to the template
    cursor.execute('SELECT attempt_id, mode, start_time FROM quiz_attempts WHERE username = %s AND quiz_name = %s ORDER BY start_time DESC', (username, quiz_name))
    all_attempts = cursor.fetchall()

    incomplete_attempts = []
    for att in all_attempts:
        cursor.execute('SELECT COUNT(id) as c FROM attempt_responses WHERE attempt_id = %s', (att['attempt_id'],))
        answered_count = cursor.fetchone()['c']

        if answered_count < total_questions:
            # Safely format the date depending on if it's a datetime object or a string from MySQL
            st_str = att['start_time'].strftime('%b %d, %Y') if isinstance(att['start_time'], datetime) else str(att['start_time']).split(' ')[0]

            incomplete_attempts.append({
                'attempt_id': att['attempt_id'],
                'mode': att['mode'],
                'start_time': st_str,
                'answered': answered_count,
                'total': total_questions
            })

    return render_template('quiz_mode.html', quiz_name=quiz_name, incomplete_attempts=incomplete_attempts, forced_mode=forced_mode)

# Helper function to determine where a user left off in a remediation
def _get_remediation_resume_url(cursor, attempt_id, quiz_name):
    cursor.execute('''
        SELECT rq.assignment_id
        FROM remediation_questions rq
        JOIN remediation_assignments ra ON rq.assignment_id = ra.id
        LEFT JOIN attempt_responses ar ON rq.id = ar.question_id AND ar.attempt_id = %s
        WHERE TRIM(LOWER(ra.remediation_name)) = TRIM(LOWER(%s)) AND ar.id IS NULL
        ORDER BY ra.created_date ASC, ra.id ASC, rq.id ASC LIMIT 1
    ''', (attempt_id, quiz_name))
    unanswered = cursor.fetchone()

    if unanswered:
        assign_id = unanswered['assignment_id']
        cursor.execute("SELECT 1 FROM attempt_responses ar JOIN remediation_questions rq ON ar.question_id = rq.id WHERE rq.assignment_id = %s AND ar.attempt_id = %s", (assign_id, attempt_id))
        has_started = cursor.fetchone()

        if has_started:
            return url_for('quiz.remediation_question_page', assignment_id=assign_id, attempt_id=attempt_id)
        else:
            return url_for('quiz.view_remediation_flashcard', assignment_id=assign_id, attempt_id=attempt_id)
    else:
        return url_for('quiz.remediation_modules', remediation_name=quiz_name, attempt_id=attempt_id)

@quiz_bp.route('/start_quiz', methods=['POST'])
def start_quiz():
    # --- FREE PREVIEW GUEST BYPASS ---
    if request.form.get('is_preview') == 'true' and 'username' not in session:
        session['username'] = 'guest_' + uuid.uuid4().hex[:8]

    if 'username' not in session:
        return redirect(url_for('index'))

    username = session['username']
    quiz_name = request.form.get('quiz_name')
    requested_mode = request.form.get('mode')
    resume_attempt_id = request.form.get('resume_attempt_id')

    # --- HARDCODED FIX FOR SAMPLE CONTENT ---
    if request.form.get('is_preview') == 'true':
        quiz_name = 'EOR 1 Remediation'

    if not quiz_name or not requested_mode:
        return redirect(url_for('quiz.select_quiz'))

    conn = get_db()
    cursor = conn.cursor()

    is_remediation = False

    cursor.execute("SELECT 1 FROM remediation_assignments WHERE TRIM(LOWER(remediation_name)) = TRIM(LOWER(%s))", (quiz_name,))
    if cursor.fetchone():
        is_remediation = True

    cursor.execute("SELECT program_code FROM users WHERE clerk_id = %s", (username,))
    p_res = cursor.fetchone()
    program_code = p_res.get('program_code') if p_res and p_res.get('program_code') else 'NONE'

    cursor.execute("SELECT class_id FROM class_members WHERE clerk_id = %s", (username,))
    student_class_ids = [str(row['class_id']) for row in cursor.fetchall() if row.get('class_id') is not None]

    table_name = "assigned_remediations" if is_remediation else "assigned_quizzes"
    name_col = "remediation_name" if is_remediation else "quiz_name"

    # Incorporating explicit clerk_id lookup for exact student assignments
    assign_conds = [
        "clerk_id = %s",
        "(class_id IS NULL AND clerk_id IS NULL AND program_code = %s)"
    ]
    assign_params = [quiz_name, username, program_code]

    if student_class_ids:
        placeholders = ','.join(['%s'] * len(student_class_ids))
        assign_conds.append(f"class_id IN ({placeholders})")
        assign_params.extend(student_class_ids)

    assign_where = " OR ".join(assign_conds)

    cursor.execute(f'''
        SELECT required_mode FROM {table_name}
        WHERE {name_col} = %s AND ({assign_where})
        ORDER BY assigned_date DESC LIMIT 1
    ''', assign_params)
    assign_req = cursor.fetchone()

    # ENFORCEMENT LOGIC: Validate against assignment mode
    if assign_req and assign_req.get('required_mode') and assign_req['required_mode'] != 'any':
        req_mode = assign_req['required_mode']
        if req_mode == 'test':
            if is_remediation:
                cursor.execute('''
                    SELECT COUNT(rq.id) as total FROM remediation_questions rq
                    JOIN remediation_assignments ra ON rq.assignment_id = ra.id
                    WHERE TRIM(LOWER(ra.remediation_name)) = TRIM(LOWER(%s))
                ''', (quiz_name,))
            else:
                cursor.execute('''
                    SELECT COUNT(qqm.map_id) as total FROM quiz_questions_map qqm
                    JOIN user_quizzes uq ON qqm.quiz_id = uq.quiz_id
                    WHERE uq.quiz_name = %s
                ''', (quiz_name,))

            total_q_res = cursor.fetchone()
            total_questions = total_q_res['total'] if total_q_res else 0

            cursor.execute('''
                SELECT qa.attempt_id, COUNT(ar.id) as answered
                FROM quiz_attempts qa
                LEFT JOIN attempt_responses ar ON qa.attempt_id = ar.attempt_id
                WHERE qa.username = %s AND qa.quiz_name = %s AND qa.mode = 'test'
                GROUP BY qa.attempt_id
                ORDER BY qa.start_time DESC
            ''', (username, quiz_name))
            test_attempts = cursor.fetchall()

            # They must have at least one test attempt where all questions are answered
            has_completed_test = any(att['answered'] >= total_questions for att in test_attempts)

            # Find any incomplete test attempt (most recent one)
            incomplete_test = next((att for att in test_attempts if att['answered'] < total_questions), None)

            # Block study mode if test mode hasn't been completed yet
            if requested_mode != 'test' and not has_completed_test:
                flash("This assignment requires you to complete a Test mode attempt first.", "warning")
                return redirect(url_for('quiz.quiz_mode', quiz_name=quiz_name))

            # Force them to finish an ongoing test attempt before starting a brand new one
            if requested_mode == 'test' and incomplete_test and not resume_attempt_id:
                flash("You must complete your ongoing test attempt before starting a new one.", "warning")
                if is_remediation:
                    url = _get_remediation_resume_url(cursor, incomplete_test['attempt_id'], quiz_name)
                    return redirect(url)
                else:
                    return redirect(url_for('quiz.question_page', quiz_name=quiz_name, mode='test', attempt_id=incomplete_test['attempt_id']))

        elif req_mode == 'study' and requested_mode == 'test':
            flash("This assignment is restricted to Study mode only.", "warning")
            return redirect(url_for('quiz.quiz_mode', quiz_name=quiz_name))

    # All validations passed. Now build or resume the attempt.
    if is_remediation:
        if resume_attempt_id:
            url = _get_remediation_resume_url(cursor, resume_attempt_id, quiz_name)
            return redirect(url)
        else:
            attempt_id = uuid.uuid4().hex
            cursor.execute('INSERT INTO quiz_attempts (attempt_id, username, username_norm, quiz_name, quiz_name_norm, mode, start_time) VALUES (%s, %s, %s, %s, %s, %s, %s)',
                           (attempt_id, username, username.strip().lower(), quiz_name, quiz_name.strip().lower(), requested_mode, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
            conn.commit()
            return redirect(url_for('quiz.remediation_modules', remediation_name=quiz_name, attempt_id=attempt_id))
    else:
        if resume_attempt_id:
            return redirect(url_for('quiz.question_page', quiz_name=quiz_name, mode=requested_mode, attempt_id=resume_attempt_id))

        attempt_id = uuid.uuid4().hex
        cursor.execute('INSERT INTO quiz_attempts (attempt_id, username, username_norm, quiz_name, quiz_name_norm, mode, start_time) VALUES (%s, %s, %s, %s, %s, %s, %s)',
                       (attempt_id, username, username.strip().lower(), quiz_name, quiz_name.strip().lower(), requested_mode, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()
        return redirect(url_for('quiz.question_page', quiz_name=quiz_name, mode=requested_mode, attempt_id=attempt_id))

@quiz_bp.route('/resume_quiz/<quiz_name>')
def resume_quiz(quiz_name):
    if 'username' not in session:
        return redirect(url_for('index'))

    conn = get_db()
    cursor = conn.cursor()

    is_remediation = False
    cursor.execute("SELECT 1 FROM remediation_assignments WHERE TRIM(LOWER(remediation_name)) = TRIM(LOWER(%s))", (quiz_name,))
    if cursor.fetchone():
        is_remediation = True

    cursor.execute('SELECT attempt_id, mode FROM quiz_attempts WHERE username = %s AND quiz_name = %s ORDER BY start_time DESC LIMIT 1', (session['username'], quiz_name))
    last_attempt = cursor.fetchone()

    if last_attempt:
        if is_remediation:
            url = _get_remediation_resume_url(cursor, last_attempt['attempt_id'], quiz_name)
            return redirect(url)
        else:
            return redirect(url_for('quiz.question_page', quiz_name=quiz_name, attempt_id=last_attempt['attempt_id'], mode=last_attempt['mode']))

    flash("No previous attempt found for this assignment.")
    return redirect(url_for('quiz.select_quiz'))

@quiz_bp.route('/quiz_attempts')
def quiz_attempts():
    if 'username' not in session:
        return redirect(url_for('index'))
    username, quiz_name = session.get('username'), request.args.get('quiz_name')
    if not quiz_name:
        return redirect(url_for('quiz.select_quiz'))

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT qa.attempt_id, qa.quiz_name, qa.mode, qa.start_time, COUNT(ar.id) as questions_answered, COALESCE(SUM(ar.is_correct), 0) as correct_answers
        FROM quiz_attempts qa LEFT JOIN attempt_responses ar ON qa.attempt_id = ar.attempt_id
        WHERE qa.username = %s AND qa.quiz_name = %s GROUP BY qa.attempt_id ORDER BY qa.start_time DESC
    ''', (username, quiz_name))
    attempts = []
    for row in cursor.fetchall():
        attempt = dict(row)
        if attempt.get('start_time'):
            attempt['start_time'] = str(attempt['start_time'])
        answered, correct = int(attempt.get('questions_answered') or 0), int(attempt.get('correct_answers') or 0)
        attempt['score_display'] = f"{round((correct / answered) * 100, 1)}% ({correct} Correct, {answered - correct} Incorrect)" if answered > 0 else "No answers recorded"
        attempts.append(attempt)

    return render_template('quiz_attempts.html', quiz_name=quiz_name, attempts=attempts)

@quiz_bp.route('/quiz_results')
def quiz_results():
    if 'username' not in session:
        return redirect(url_for('index'))
    attempt_id, quiz_name = request.args.get('attempt_id'), request.args.get('quiz_name')
    if not attempt_id:
        return redirect(url_for('quiz.select_quiz'))

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT qa.attempt_id, qa.quiz_name, qa.mode, qa.start_time, COUNT(ar.id) as questions_answered, COALESCE(SUM(ar.is_correct), 0) as correct_answers
        FROM quiz_attempts qa LEFT JOIN attempt_responses ar ON qa.attempt_id = ar.attempt_id
        WHERE qa.attempt_id = %s GROUP BY qa.attempt_id
    ''', (attempt_id,))
    attempt_data = cursor.fetchone()
    if not attempt_data:
        return redirect(url_for('quiz.select_quiz'))

    attempt = dict(attempt_data)
    if attempt.get('start_time'):
        attempt['start_time'] = str(attempt['start_time'])

    ans, cor = int(attempt.get('questions_answered') or 0), int(attempt.get('correct_answers') or 0)
    attempt['percentage'] = round((cor / ans) * 100, 1) if ans > 0 else 0
    attempt['incorrect'], attempt['correct'], attempt['total_answered'] = ans - cor, cor, ans

    is_remediation = False
    module_breakdown = []

    cursor.execute("SELECT 1 FROM remediation_assignments WHERE TRIM(LOWER(remediation_name)) = TRIM(LOWER(%s))", (quiz_name,))
    if cursor.fetchone():
        is_remediation = True

        # --- FIXED LOGIC: Anchoring query to assignments and LEFT JOINing responses ---
        cursor.execute('''
            SELECT ra.topic_area,
                   COUNT(ar.id) as answered,
                   COALESCE(SUM(ar.is_correct), 0) as correct
            FROM remediation_assignments ra
            JOIN remediation_questions rq ON ra.id = rq.assignment_id
            LEFT JOIN attempt_responses ar ON rq.id = ar.question_id AND ar.attempt_id = %s
            WHERE TRIM(LOWER(ra.remediation_name)) = TRIM(LOWER(%s))
            GROUP BY ra.id, ra.topic_area
        ''', (attempt_id, quiz_name))

        for row in cursor.fetchall():
            m_ans = int(row['answered'] or 0)
            m_cor = int(row['correct'] or 0)
            module_breakdown.append({
                'topic_area': row['topic_area'],
                'answered': m_ans,
                'correct': m_cor,
                'percentage': round((m_cor / m_ans) * 100, 1) if m_ans > 0 else 0
            })

    return render_template('quiz_results.html', attempt=attempt, quiz_name=quiz_name, is_remediation=is_remediation, module_breakdown=module_breakdown)

@quiz_bp.route('/review_attempt')
def review_attempt():
    if 'username' not in session:
        return redirect(url_for('index'))
    attempt_id = request.args.get('attempt_id')
    faculty_return_url = request.args.get('faculty_return_url')
    if not attempt_id:
        return redirect(url_for('quiz.select_quiz'))

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM quiz_attempts WHERE attempt_id = %s', (attempt_id,))
    attempt_row = cursor.fetchone()
    if attempt_row and attempt_row.get('start_time'):
        attempt_row['start_time'] = str(attempt_row['start_time'])

    cursor.execute('''
        SELECT ar.*,
               COALESCE(qb.question_stem, rq.question_stem) as question_stem,
               COALESCE(qb.answer_choice_A, rq.answer_choice_A) as answer_choice_A,
               COALESCE(qb.answer_choice_B, rq.answer_choice_B) as answer_choice_B,
               COALESCE(qb.answer_choice_C, rq.answer_choice_C) as answer_choice_C,
               COALESCE(qb.answer_choice_D, rq.answer_choice_D) as answer_choice_D,
               COALESCE(qb.answer_choice_E, rq.answer_choice_E) as answer_choice_E,
               COALESCE(qb.explanation, rq.explanation) as explanation
        FROM attempt_responses ar
        LEFT JOIN question_bank qb ON ar.question_id = qb.id
        LEFT JOIN remediation_questions rq ON ar.question_id = rq.id
        WHERE ar.attempt_id = %s ORDER BY ar.submitted_time ASC
    ''', (attempt_id,))
    responses = cursor.fetchall()

    for resp in responses:
        if resp.get('submitted_time'):
            resp['submitted_time'] = str(resp['submitted_time'])

    return render_template('review_attempt.html', attempt=attempt_row if attempt_row else {}, responses=responses, faculty_return_url=faculty_return_url)

@quiz_bp.route('/database-stats')
def database_stats():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) as c FROM question_bank")
    total_questions = cursor.fetchone()['c']
    cursor.execute("SELECT organ_system, COUNT(*) as c FROM question_bank GROUP BY organ_system ORDER BY COUNT(*) DESC")
    organ_data = cursor.fetchall()
    cursor.execute("SELECT task_area, COUNT(*) as c FROM question_bank GROUP BY task_area ORDER BY COUNT(*) DESC")
    task_data = cursor.fetchall()

    return render_template('db_stats.html', total_questions=total_questions,
                           organ_labels=[row['organ_system'] for row in organ_data], organ_counts=[row['c'] for row in organ_data],
                           task_labels=[row['task_area'] for row in task_data], task_counts=[row['c'] for row in task_data])

@quiz_bp.route('/rate_question', methods=['POST'])
def rate_question():
    data = request.json
    question_id, rating_type = data.get('question_id'), data.get('rating_type')
    if not question_id or rating_type not in ['up', 'down']:
        return current_app.response_class(response=json.dumps({"status": "error"}), status=400, mimetype='application/json')

    conn = get_db()
    cursor = conn.cursor()
    if rating_type == 'up':
        cursor.execute('UPDATE question_bank SET global_thumbs_up = COALESCE(global_thumbs_up, 0) + 1 WHERE id = %s', (question_id,))
    elif rating_type == 'down':
        cursor.execute('UPDATE question_bank SET global_thumbs_down = COALESCE(global_thumbs_down, 0) + 1 WHERE id = %s', (question_id,))
    conn.commit()

    return current_app.response_class(response=json.dumps({"status": "success"}), mimetype='application/json')

# ==========================================
# ROUTE: FLASHCARDS SELECT ORGAN
# ==========================================
@quiz_bp.route('/flashcards')
def flashcards_select_organ():
    if 'username' not in session:
        return redirect(url_for('index'))
    return render_template('flashcards_organ_select.html', organ_systems=[
        "Cardiovascular System", "Dermatologic System", "Endocrine System",
        "Eyes, Ears, Nose, and Throat", "Gastrointestinal System/Nutrition",
        "Hematologic System", "Infectious Diseases", "Musculoskeletal System",
        "Neurological System", "Psychiatry/Behavioral Science", "Pulmonary System",
        "Renal System", "Reproductive System", "Genitourinary System"
    ])

# ==========================================
# ROUTE: FLASHCARDS TOPIC SELECTOR
# ==========================================
@quiz_bp.route('/flashcards/<path:organ_system>')
def flashcards_select_topic(organ_system):
    if 'username' not in session:
        return redirect(url_for('index'))
    organ_system = unquote(organ_system)
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT DISTINCT topic
        FROM flashcards
        WHERE TRIM(organ_system) = TRIM(%s)
          AND topic IS NOT NULL
          AND TRIM(topic) != ''
        ORDER BY topic ASC
    ''', (organ_system,))
    topics = [row['topic'] for row in cursor.fetchall()]
    return render_template('flashcards_topic_select.html', organ_system=organ_system, topics=topics)

# ==========================================
# ROUTE: FLASHCARDS VIEWER (By Topic)
# ==========================================
@quiz_bp.route('/flashcards/<path:organ_system>/<path:topic>')
def flashcards_viewer(organ_system, topic):
    if 'username' not in session:
        return redirect(url_for('index'))
    username = session['username']
    organ_system = unquote(organ_system)
    topic = unquote(topic)

    conn = get_db()
    cursor = conn.cursor()
    if topic.lower() == 'all':
        cursor.execute('''
            SELECT id, organ_system, topic, task_area, prompt, answer
            FROM flashcards
            WHERE TRIM(organ_system) = TRIM(%s)
            ORDER BY id ASC
        ''', (organ_system,))
    else:
        cursor.execute('''
            SELECT id, organ_system, topic, task_area, prompt, answer
            FROM flashcards
            WHERE TRIM(organ_system) = TRIM(%s) AND TRIM(topic) = TRIM(%s)
            ORDER BY id ASC
        ''', (organ_system, topic))
    cards = cursor.fetchall()

    if not cards:
        cards = [{
            "id": 0,
            "organ_system": organ_system,
            "topic": topic,
            "task_area": "N/A",
            "prompt": f"No flashcards found in database for {organ_system} - {topic}.",
            "answer": "Placeholder"
        }]

    cursor.execute('''
        SELECT current_index FROM flashcard_progress
        WHERE username = %s AND organ_system = %s AND topic = %s
    ''', (username, organ_system, topic))
    saved_progress = cursor.fetchone()
    saved_index = saved_progress['current_index'] if saved_progress else 0

    return render_template('flashcards_viewer.html',
                           organ_system=organ_system,
                           topic=topic,
                           cards=cards,
                           saved_index=max(0, min(saved_index, len(cards) - 1)))

# ==========================================
# ROUTE: SAVE FLASHCARD PROGRESS (Database)
# ==========================================
@quiz_bp.route('/save_flashcard_progress', methods=['POST'])
def save_flashcard_progress():
    if 'username' not in session:
        return current_app.response_class(response=json.dumps({"status": "error"}), status=401, mimetype='application/json')
    username = session['username']
    data = request.json
    organ_system = data.get('organ_system')
    topic = data.get('topic', 'All')
    current_index = data.get('current_index')

    if organ_system and current_index is not None:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO flashcard_progress (username, organ_system, topic, current_index)
            VALUES (%s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE current_index = VALUES(current_index)
        ''', (username, organ_system, topic, int(current_index)))
        conn.commit()
        return current_app.response_class(response=json.dumps({"status": "success"}), mimetype='application/json')
    return current_app.response_class(response=json.dumps({"status": "error"}), status=400, mimetype='application/json')

# ==========================================
# ROUTE: REMEDIATION ASSIGNMENTS
# ==========================================
@quiz_bp.route('/remediation/modules/<path:remediation_name>')
def remediation_modules(remediation_name):
    if 'username' not in session:
        return redirect(url_for('index'))
    attempt_id = request.args.get('attempt_id')
    if not attempt_id:
        return redirect(url_for('quiz.select_quiz'))

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT id, topic_area, task_area
        FROM remediation_assignments
        WHERE TRIM(LOWER(remediation_name)) = TRIM(LOWER(%s))
        ORDER BY created_date ASC, id ASC
    ''', (remediation_name,))
    modules = cursor.fetchall()

    module_stats = []
    total_questions = 0
    total_answered = 0

    for mod in modules:
        cursor.execute("SELECT COUNT(id) as c FROM remediation_questions WHERE assignment_id = %s", (mod['id'],))
        q_count = cursor.fetchone()['c'] or 0

        cursor.execute('''
            SELECT COUNT(ar.id) as a, SUM(ar.is_correct) as cor
            FROM attempt_responses ar
            JOIN remediation_questions rq ON ar.question_id = rq.id
            WHERE ar.attempt_id = %s AND rq.assignment_id = %s
        ''', (attempt_id, mod['id']))
        ans_data = cursor.fetchone()

        answered = int(ans_data['a'] or 0)
        correct = int(ans_data['cor'] or 0)

        total_questions += q_count
        total_answered += answered

        module_stats.append({
            'id': mod['id'],
            'topic_area': mod['topic_area'],
            'task_area': mod['task_area'],
            'total': q_count,
            'answered': answered,
            'correct': correct,
            'is_complete': (answered >= q_count) if q_count > 0 else True
        })

    all_complete = (total_questions > 0 and total_answered >= total_questions)

    return render_template('remediation_module_list.html',
                           remediation_name=remediation_name,
                           modules=module_stats,
                           attempt_id=attempt_id,
                           all_complete=all_complete)

@quiz_bp.route('/remediation/review/<assignment_id>')
def view_remediation_flashcard(assignment_id):
    if 'username' not in session:
        return redirect(url_for('index'))
    attempt_id = request.args.get('attempt_id')

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM remediation_assignments WHERE id = %s", (assignment_id,))
    assignment = cursor.fetchone()

    if not assignment:
        flash("Remediation assignment not found.", "error")
        return redirect(url_for('quiz.menu'))

    return render_template('remediation_flashcard.html', assignment=assignment, attempt_id=attempt_id)

@quiz_bp.route('/remediation/questions/<assignment_id>')
def remediation_question_page(assignment_id):
    if 'username' not in session:
        return redirect(url_for('index'))

    conn = get_db()
    cursor = conn.cursor()

    raw_q = request.args.get('q')
    current_q_id = int(raw_q) if raw_q and raw_q.isdigit() else raw_q

    answered = request.args.get('answered') == 'True'
    attempt_id = request.args.get('attempt_id')

    mode = 'test'
    if attempt_id:
        cursor.execute("SELECT mode FROM quiz_attempts WHERE attempt_id = %s", (attempt_id,))
        att_row = cursor.fetchone()
        if att_row:
            mode = att_row['mode']

    cursor.execute("SELECT remediation_name FROM remediation_assignments WHERE id = %s", (assignment_id,))
    rem_row = cursor.fetchone()
    rem_name = rem_row['remediation_name'] if rem_row else "Unknown Remediation"

    if current_q_id == 'NEXT_MODULE':
        flash("Module complete! Select your next module.", "success")
        return redirect(url_for('quiz.remediation_modules', remediation_name=rem_name, attempt_id=attempt_id))

    cursor.execute("SELECT * FROM remediation_questions WHERE assignment_id = %s ORDER BY id ASC", (assignment_id,))
    questions = cursor.fetchall()

    if not questions:
        flash("No questions found for this module.", "error")
        return redirect(url_for('quiz.remediation_modules', remediation_name=rem_name, attempt_id=attempt_id))

    question_ids = [q['id'] for q in questions]

    if mode == 'study_missed' and question_ids:
        cursor.execute('''
            SELECT DISTINCT ar.question_id
            FROM attempt_responses ar
            JOIN quiz_attempts qa ON ar.attempt_id = qa.attempt_id
            WHERE qa.username = %s AND qa.quiz_name = %s AND ar.is_correct = 0
        ''', (session.get('username'), rem_name))
        missed_set = {row['question_id'] for row in cursor.fetchall()}
        question_ids = [q for q in question_ids if q in missed_set]
        if not question_ids:
            flash("Great job! You have no missed questions to review for this module.", "success")
            return redirect(url_for('quiz.remediation_modules', remediation_name=rem_name, attempt_id=attempt_id))

    if not current_q_id and attempt_id:
        cursor.execute('''
            SELECT q.id FROM remediation_questions q
            LEFT JOIN attempt_responses ar ON q.id = ar.question_id AND ar.attempt_id = %s
            WHERE q.assignment_id = %s AND ar.id IS NULL
            ORDER BY q.id ASC LIMIT 1
        ''', (attempt_id, assignment_id))
        unanswered = cursor.fetchone()
        current_q_id = unanswered['id'] if unanswered else question_ids[0]
    elif not current_q_id:
        current_q_id = question_ids[0]

    current_question_index = question_ids.index(current_q_id) if current_q_id in question_ids else 0
    current_question_data = next((q for q in questions if q['id'] == current_q_id), questions[0])

    next_q_id = question_ids[current_question_index + 1] if current_question_index + 1 < len(question_ids) else 'NEXT_MODULE'
    prev_q_id = question_ids[current_question_index - 1] if current_question_index > 0 else None

    # --- FIXED: STR() CASTING TO PREVENT TYPE MISMATCH ---
    previous_choice = None
    responses_dict = {}

    if attempt_id:
        cursor.execute('SELECT question_id, is_correct, user_choice FROM attempt_responses WHERE attempt_id = %s', (attempt_id,))
        for row in cursor.fetchall():
            responses_dict[str(row['question_id'])] = row['is_correct']
            if str(row['question_id']) == str(current_q_id):
                previous_choice = row['user_choice']

    grid_data = []
    for idx, q_id in enumerate(question_ids):
        status = 'unanswered'
        if str(q_id) in responses_dict:
            status = 'answered' if mode == 'test' else ('correct' if responses_dict[str(q_id)] == 1 else 'incorrect')
        grid_data.append({
            'question_number': idx + 1,
            'question_id': q_id,
            'status': status,
            'is_current': str(q_id) == str(current_q_id)
        })

    for key in ['correct', 'incorrect', 'choice_a_count', 'choice_b_count', 'choice_c_count', 'choice_d_count', 'choice_e_count']:
        current_question_data[key] = 0

    # --- SMART EXIT ROUTING ---
    # If the user is a guest, clicking exit routes them directly back to the index page.
    if session.get('username', '').startswith('guest_'):
        exit_url = url_for('quiz.guest_exit')
    else:
        exit_url = url_for('quiz.remediation_modules', remediation_name=rem_name, attempt_id=attempt_id)

    return render_template('index.html',
                           question=current_question_data,
                           total_questions=len(question_ids),
                           current_question_number=current_question_index + 1,
                           next_q_id=next_q_id,
                           prev_q_id=prev_q_id,
                           grid_data=grid_data,
                           mode=mode,
                           quiz_name=f"REMEDIATION_{assignment_id}",
                           attempt_id=attempt_id,
                           answered=answered,
                           previous_choice=previous_choice,
                           exit_url=exit_url)