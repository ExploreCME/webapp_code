"""Numbered PANCE questions, private host controls, and public audience voting."""
from functools import wraps
import hashlib
import hmac
import json
import os
import random
import secrets
import time

import pymysql
from redis.exceptions import RedisError
from flask import (
    Blueprint, abort, current_app, g, jsonify, make_response, redirect,
    render_template, request, session, url_for,
)

from db_pool import get_db_connection
from standardizers import ORGAN_SYSTEMS

paxcel_game_bp = Blueprint('paxcel_game', __name__, url_prefix='/paxcel/the_game')
HOST_TTL = 12 * 60 * 60
LOGIN_WINDOW = 15 * 60
LOGIN_LIMIT = 5
QUESTION_FIELDS = (
    'id', 'organ_system', 'task_area', 'question_stem', 'correct_answer',
    'explanation', *(f'answer_choice_{letter}' for letter in 'ABCDE'),
)
_ORGAN_FILTER = f"BINARY organ_system IN ({', '.join(['%s'] * len(ORGAN_SYSTEMS))})"
_PLAYABLE_FILTER = (
    f"{_ORGAN_FILTER} AND TRIM(question_stem) <> '' "
    "AND UPPER(TRIM(correct_answer)) IN ('A', 'B', 'C', 'D', 'E') "
    "AND TRIM(answer_choice_A) <> '' AND TRIM(answer_choice_B) <> '' "
    "AND TRIM(CASE UPPER(TRIM(correct_answer)) "
    "WHEN 'A' THEN answer_choice_A WHEN 'B' THEN answer_choice_B "
    "WHEN 'C' THEN answer_choice_C WHEN 'D' THEN answer_choice_D "
    "WHEN 'E' THEN answer_choice_E END) NOT IN ('', 'None')"
)


def get_db():
    if 'paxcel_game_db' not in g:
        g.paxcel_game_db = get_db_connection()
    return g.paxcel_game_db


@paxcel_game_bp.teardown_request
def close_db(error):
    db = g.pop('paxcel_game_db', None)
    if db is not None:
        db.close()


def admin_code():
    code = os.getenv('PAXCEL_GAME_ADMIN_CODE', '')
    return code if len(code) >= 16 else None


def code_fingerprint(code):
    return hmac.new(current_app.secret_key.encode(), code.encode(), hashlib.sha256).hexdigest()


def is_host():
    code = admin_code()
    host = session.get('paxcel_game_host', {})
    return bool(
        code and host.get('expires', 0) > time.time()
        and hmac.compare_digest(host.get('fingerprint', ''), code_fingerprint(code))
    )


def host_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not is_host():
            abort(403, 'Administrator access is required.')
        return view(*args, **kwargs)
    return wrapped


def csrf_token():
    if 'paxcel_game_csrf' not in session:
        session['paxcel_game_csrf'] = secrets.token_urlsafe(32)
    return session['paxcel_game_csrf']


@paxcel_game_bp.before_request
def protect_forms():
    if request.method == 'POST':
        expected = session.get('paxcel_game_csrf', '')
        supplied = request.form.get('csrf_token', '')
        if not expected or not secrets.compare_digest(expected.encode(), supplied.encode()):
            abort(400, 'This form expired. Reload the page and try again.')


@paxcel_game_bp.context_processor
def game_context():
    return {'game_csrf': csrf_token(), 'game_host': is_host(), 'show_app_header': False}


@paxcel_game_bp.after_request
def private_response(response):
    response.headers['Cache-Control'] = 'no-store, private, max-age=0'
    response.headers['Referrer-Policy'] = 'same-origin'
    response.headers['X-Robots-Tag'] = 'noindex, nofollow'
    return response


@paxcel_game_bp.errorhandler(pymysql.MySQLError)
def database_error(error):
    current_app.logger.exception('PAXcel game database operation failed')
    return render_template(
        'paxcel_game_message.html',
        message='The game database is unavailable. Please try again shortly.',
    ), 503


@paxcel_game_bp.errorhandler(RedisError)
def login_storage_error(error):
    current_app.logger.exception('PAXcel game administrator login storage failed')
    return render_template(
        'paxcel_game_message.html',
        message='Administrator login is temporarily unavailable. Please try again shortly.',
    ), 503


def choices(question):
    return [
        (letter, question.get(f'answer_choice_{letter}'))
        for letter in 'ABCDE'
        if question.get(f'answer_choice_{letter}')
        and question[f'answer_choice_{letter}'].strip() not in ('', 'None')
    ]


def fetch_random_question():
    """Choose a uniformly random playable question from canonical PANCE organs."""
    with get_db().cursor() as cursor:
        cursor.execute(f"SELECT COUNT(*) AS c FROM question_bank WHERE {_PLAYABLE_FILTER}", ORGAN_SYSTEMS)
        row = cursor.fetchone()
        total = int(row['c']) if row else 0
        if not total:
            return None
        cursor.execute(
            f"SELECT * FROM question_bank WHERE {_PLAYABLE_FILTER} ORDER BY id LIMIT 1 OFFSET %s",
            (*ORGAN_SYSTEMS, random.randrange(total)),
        )
        return cursor.fetchone()


def load_question(number):
    if not 0 < number <= 9223372036854775807:
        abort(404, 'Question number not found.')
    with get_db().cursor() as cursor:
        cursor.execute('SELECT * FROM paxcel_game_questions WHERE id = %s', (number,))
        row = cursor.fetchone()
    if row is None:
        abort(404, 'Question number not found. Check the number shown by the host.')
    row['question'] = json.loads(row['question_snapshot'])
    return row


def public_question(question):
    """Never include the correct answer or explanation in audience HTML/context."""
    allowed = ('question_stem', 'organ_system', *(f'answer_choice_{letter}' for letter in 'ABCDE'))
    return {field: question.get(field) for field in allowed}


@paxcel_game_bp.route('', methods=['GET', 'POST'], strict_slashes=False)
def start():
    error = None
    if request.method == 'POST':
        number = request.form.get('question_number', '').strip()
        if (len(number) <= 19 and number.isascii() and number.isdigit()
                and 0 < int(number) <= 9223372036854775807):
            endpoint = '.results' if is_host() else '.question'
            return redirect(url_for(endpoint, number=int(number)), code=303)
        error = 'Enter a valid question number.'
    if is_host():
        return render_template('paxcel_game.html', error=error), 400 if error else 200
    return render_template('paxcel_game_join.html', error=error), 400 if error else 200


@paxcel_game_bp.route('/admin', methods=['GET', 'POST'])
def admin():
    if is_host():
        return redirect(url_for('.start'))
    if not admin_code():
        return render_template(
            'paxcel_game_message.html',
            message='Administrator access is not configured. Set PAXCEL_GAME_ADMIN_CODE '
                    'to a private code of at least 16 characters on the server.',
        ), 503
    error = None
    if request.method == 'POST':
        from display_database import cache
        # Use the immediate peer, not an untrusted forwarded header.
        key = 'game_login:' + code_fingerprint(request.remote_addr or 'unknown')
        cache.add(key, 0, timeout=LOGIN_WINDOW)
        attempts = cache.cache.inc(key)
        if attempts is None:
            current_app.logger.error('PAXcel game login rate-limit storage failed')
            abort(503, 'Administrator login is temporarily unavailable.')
        if attempts > LOGIN_LIMIT:
            response = make_response(render_template(
                'paxcel_game_login.html', error='Too many attempts. Try again in 15 minutes.',
            ), 429)
            response.headers['Retry-After'] = str(LOGIN_WINDOW)
            return response
        supplied = request.form.get('code', '')
        if secrets.compare_digest(supplied.encode(), admin_code().encode()):
            session['paxcel_game_host'] = {
                'expires': time.time() + HOST_TTL,
                'fingerprint': code_fingerprint(admin_code()),
            }
            session['paxcel_game_csrf'] = secrets.token_urlsafe(32)
            cache.delete(key)
            return redirect(url_for('.start'), code=303)
        error = 'Incorrect administrator code.'
    return render_template('paxcel_game_login.html', error=error), 403 if error else 200


@paxcel_game_bp.post('/admin/logout')
def logout():
    session.pop('paxcel_game_host', None)
    session.pop('paxcel_game_csrf', None)
    return redirect(url_for('.start'), code=303)


@paxcel_game_bp.post('/play')
@host_required
def play():
    question = fetch_random_question()
    if question is None:
        return render_template('paxcel_game_message.html', message='No questions are available right now.'), 503
    snapshot = {field: question.get(field) for field in QUESTION_FIELDS}
    snapshot['correct_answer'] = snapshot['correct_answer'].strip().upper()
    db = get_db()
    with db.cursor() as cursor:
        cursor.execute(
            'INSERT INTO paxcel_game_questions (question_id, question_snapshot) VALUES (%s, %s)',
            (question['id'], json.dumps(snapshot)),
        )
        number = cursor.lastrowid
    db.commit()
    return redirect(url_for('.draw', number=number), code=303)


@paxcel_game_bp.get('/draw/<int:number>')
@host_required
def draw(number):
    game = load_question(number)
    return render_template(
        'paxcel_game_draw.html', number=number,
        organ_system=game['question'].get('organ_system'),
        organ_systems=ORGAN_SYSTEMS,
    )


@paxcel_game_bp.route('/question/<int:number>', methods=['GET', 'POST'])
def question(number):
    game = load_question(number)
    safe_question = public_question(game['question'])
    available_choices = choices(safe_question)
    error = None
    if request.method == 'POST':
        choice = request.form.get('user_choice', '')
        if choice not in dict(available_choices):
            error = 'Select one of the available answer choices.'
        else:
            db = get_db()
            with db.cursor() as cursor:
                cursor.execute(
                    'INSERT INTO paxcel_game_responses (game_question_id, question_id, answer_choice) '
                    'VALUES (%s, %s, %s)',
                    (number, game['question_id'], choice),
                )
            db.commit()
            return redirect(url_for('.submitted', number=number), code=303)
    return render_template(
        'paxcel_game_play.html', number=number, question=safe_question,
        game_choices=available_choices, error=error,
    ), 400 if error else 200


@paxcel_game_bp.get('/question/<int:number>/submitted')
def submitted(number):
    load_question(number)
    return render_template('paxcel_game_submitted.html', number=number)


@paxcel_game_bp.get('/question/<int:number>/results')
@host_required
def results(number):
    game = load_question(number)
    return render_template(
        'paxcel_game_results.html', number=number, question=game['question'],
        game_choices=choices(game['question']),
    )


@paxcel_game_bp.get('/question/<int:number>/results.json')
@host_required
def results_data(number):
    game = load_question(number)
    with get_db().cursor() as cursor:
        cursor.execute(
            'SELECT answer_choice, COUNT(*) AS count FROM paxcel_game_responses '
            'WHERE game_question_id = %s GROUP BY answer_choice', (number,),
        )
        counts = {row['answer_choice']: int(row['count']) for row in cursor.fetchall()}
    total = sum(counts.values())
    return jsonify(
        number=number, total=total,
        choices=[
            {'letter': letter, 'count': counts.get(letter, 0),
             'percent': round(100 * counts.get(letter, 0) / total, 1) if total else 0}
            for letter, _ in choices(game['question'])
        ],
    )
