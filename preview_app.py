# preview_app.py
from flask import Blueprint, render_template, request, redirect, url_for, g, session, abort
import secrets
import time

# --- PERFORMANCE IMPORTS ---
from db_pool import get_db_connection

preview_bp = Blueprint('preview', __name__)

# ==========================================
# MYSQL CONNECTION POOL (SCALABLE)
# ==========================================
# Use the shared app pool instead of creating a second pool in this module.
def get_db():
    """Pulls a connection directly from the shared pool."""
    if 'db' not in g:
        g.db = get_db_connection()
    return g.db

@preview_bp.teardown_request
def close_db(error):
    """Safely returns the connection back to the MySQL pool."""
    db = g.pop('db', None)
    if db is not None:
        try:
            db.close()
        except Exception:
            pass

# ==========================================
# HARDCODED PREVIEW SETTINGS
# ==========================================
SAMPLE_QUIZ_NAME = "EOR 1 Quiz"
SAMPLE_REMEDIATION_NAME = "EOR 1 Remediation"
PREVIEW_TTL = 3600
PREVIEW_MODES = ('study_no_exp', 'study_exp', 'test')


def preview_cache():
    from display_database import cache
    return cache


def save_preview():
    state = g.preview
    remaining = int(state['expires'] - time.time())
    if remaining <= 0:
        abort(410, "Sample session expired. Please start again.")
    preview_cache().set(state['attempt_id'], state, timeout=remaining)


def load_preview():
    """Only a session-bound opaque capability can authorize sample requests."""
    g.preview = None
    values = request.form if request.method == 'POST' else request.args
    if (request.form.get('attempt_id') and request.args.get('attempt_id')
            and request.form['attempt_id'] != request.args['attempt_id']):
        abort(400)
    attempt_id = values.get('attempt_id') or request.args.get('attempt_id', '')
    if not attempt_id.startswith('preview_'):
        return
    if not attempt_id.isascii() or not secrets.compare_digest(session.get('paxcel_preview', ''), attempt_id):
        abort(404)
    state = preview_cache().get(attempt_id)
    if not state or state['expires'] <= time.time():
        abort(410, "Sample session expired. Please start again.")
    allowed = {
        'quiz.question_page', 'quiz.check_answer', 'quiz.quiz_results',
        'quiz.review_attempt', 'quiz.remediation_question_page',
        'quiz.remediation_modules', 'quiz.view_remediation_flashcard',
    }
    if request.endpoint not in allowed:
        abort(400)
    assignment_id = (request.view_args or {}).get('assignment_id')
    module_ids = {str(m['id']) for m in state['modules']}
    if assignment_id is not None and str(assignment_id) not in module_ids:
        abort(404)
    name = values.get('quiz_name')
    valid_names = {state['quiz_name']}
    valid_names.update('REMEDIATION_' + mid for mid in module_ids)
    if name is not None and name not in valid_names:
        abort(404)
    if request.endpoint in ('quiz.question_page', 'quiz.check_answer'):
        if state['kind'] == 'quiz' and name != state['quiz_name']:
            abort(404)
        if state['kind'] == 'remediation' and not (name or '').startswith('REMEDIATION_'):
            abort(404)
    remediation_name = (request.view_args or {}).get('remediation_name')
    if remediation_name is not None and remediation_name != state['quiz_name']:
        abort(404)
    if request.endpoint.startswith('quiz.remediation_') or request.endpoint == 'quiz.view_remediation_flashcard':
        if state['kind'] != 'remediation':
            abort(404)
    if values.get('mode', state['mode']) != state['mode']:
        abort(400, "Invalid sample mode.")
    if values.get('resume_attempt_id'):
        abort(400)
    questions = state['questions']
    if assignment_id:
        questions = [q for q in questions if str(q['assignment_id']) == str(assignment_id)]
    if name and name.startswith('REMEDIATION_'):
        module_id = name[len('REMEDIATION_'):]
        questions = [q for q in questions if str(q['assignment_id']) == module_id]
    for field in ('q', 'question_id', 'next_q_id'):
        qid = values.get(field)
        if not qid or (field == 'next_q_id' and qid == 'None'):
            continue
        if qid == 'NEXT_MODULE' and state['kind'] == 'remediation' and field != 'question_id':
            continue
        if qid not in {str(q['id']) for q in questions}:
            abort(404)
    if request.endpoint == 'quiz.check_answer':
        if values.get('user_choice', '').strip().upper() not in ('A', 'B', 'C', 'D', 'E'):
            abort(400)
        if state['kind'] == 'remediation' and not (name or '').startswith('REMEDIATION_'):
            abort(400)
    g.preview = state


def preview_responses():
    return list(g.preview['responses'].values())


def protect_preview_response(response):
    if request.blueprint == 'preview' or getattr(g, 'preview', None):
        response.headers['Cache-Control'] = 'no-store'
        response.headers['Referrer-Policy'] = 'same-origin'
    return response


preview_bp.after_request(protect_preview_response)

# ==========================================
# PREVIEW MENU & SETUP
# ==========================================
@preview_bp.route('/preview')
def preview_menu():
    """Public menu allowing users to pick Quiz or Remediation."""
    return render_template('preview_menu.html',
                            quiz_name=SAMPLE_QUIZ_NAME,
                            remediation_name=SAMPLE_REMEDIATION_NAME)

@preview_bp.route('/preview/setup/<assignment_type>')
def preview_setup(assignment_type):
    """Keep old public links working without the extra setup screen."""
    return redirect(url_for('preview.preview_take', assignment_type=assignment_type,
                            mode=request.args.get('mode', 'study_no_exp')))

# ==========================================
# TAKE PREVIEW (STATELESS GATEWAY)
# ==========================================
@preview_bp.route('/preview/take/<assignment_type>', methods=['GET'])
def preview_take(assignment_type):
    """Start an isolated, expiring sample; never create a learning record."""
    if assignment_type not in ('quiz', 'remediation'):
        abort(404)
    mode = request.args.get('mode', 'study_no_exp')
    if mode not in PREVIEW_MODES:
        abort(400, "Invalid sample mode.")
    cursor = get_db().cursor()
    modules = []
    if assignment_type == 'quiz':
        name = SAMPLE_QUIZ_NAME
        cursor.execute(
            "SELECT quiz_id FROM user_quizzes WHERE BINARY quiz_name = %s "
            "ORDER BY created_date ASC, quiz_id ASC LIMIT 1", (name,))
        row = cursor.fetchone()
        if not row:
            abort(404, "Sample quiz unavailable.")
        cursor.execute(
            "SELECT qb.* FROM question_bank qb JOIN quiz_questions_map qm ON qb.id = qm.question_id "
            "WHERE qm.quiz_id = %s ORDER BY qm.map_id ASC", (row['quiz_id'],))
        questions = cursor.fetchall()
    else:
        name = SAMPLE_REMEDIATION_NAME
        cursor.execute(
            "SELECT * FROM remediation_assignments WHERE BINARY remediation_name = %s "
            "ORDER BY created_date ASC, id ASC", (name,))
        modules = cursor.fetchall()
        questions = []
        for module in modules:
            cursor.execute("SELECT * FROM remediation_questions WHERE assignment_id = %s ORDER BY id ASC",
                           (module['id'],))
            questions.extend(cursor.fetchall())
        modules = [m for m in modules if any(str(q['assignment_id']) == str(m['id']) for q in questions)]
    if not questions:
        abort(404, "Sample questions unavailable.")
    old_id = session.get('paxcel_preview')
    if old_id:
        preview_cache().delete(old_id)
    attempt_id = 'preview_' + secrets.token_urlsafe(32)
    g.preview = {
        'attempt_id': attempt_id, 'kind': assignment_type, 'quiz_name': name,
        'mode': mode, 'questions': questions, 'modules': modules,
        'responses': {}, 'expires': time.time() + PREVIEW_TTL,
    }
    session['paxcel_preview'] = attempt_id
    save_preview()
    if assignment_type == 'remediation':
        return redirect(url_for('quiz.remediation_modules', remediation_name=name,
                                mode=mode, attempt_id=attempt_id))
    return redirect(url_for('quiz.question_page', quiz_name=name, mode=mode, attempt_id=attempt_id))
