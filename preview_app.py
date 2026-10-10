# preview_app.py
from flask import Blueprint, render_template, request, redirect, url_for, flash, g
import pymysql
import os

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
SAMPLE_QUIZ_NAME = "CVA Sample Questions for 2028s"
SAMPLE_REMEDIATION_NAME = "EOR 1 Remediation"

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
    """Allows user to select Study or Test mode."""
    if assignment_type not in ['quiz', 'remediation']:
        flash("Invalid preview type.", "error")
        return redirect(url_for('preview.preview_menu'))

    return render_template('preview_setup.html', assignment_type=assignment_type)

# ==========================================
# TAKE PREVIEW (STATELESS GATEWAY)
# ==========================================
@preview_bp.route('/preview/take/<assignment_type>', methods=['GET'])
def preview_take(assignment_type):
    """
    Routes the user into the main application's quiz engine.

    By deliberately omitting an 'attempt_id', the main app (flask_app.py)
    will render the live index.html and actively grade submitted answers,
    but it will bypass all database INSERT logic!
    """
    mode = request.args.get('mode', 'study_no_exp')

    if assignment_type == 'quiz':
        # Send directly to the main app's question page without an attempt_id
        return redirect(url_for('quiz.question_page',
                                quiz_name=SAMPLE_QUIZ_NAME,
                                mode=mode))

    elif assignment_type == 'remediation':
        # Remediations route requires the assignment UUID instead of the name
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM remediation_assignments WHERE remediation_name = %s LIMIT 1", (SAMPLE_REMEDIATION_NAME,))
        row = cursor.fetchone()

        if not row:
            flash("Sample remediation not found in the database.", "error")
            return redirect(url_for('preview.preview_menu'))

        return redirect(url_for('quiz.remediation_question_page',
                                assignment_id=row['id'],
                                mode=mode))
