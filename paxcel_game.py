# paxcel_game.py
"""Public "PANCE prep one random question at a time" marketing page."""
import random

from flask import Blueprint, g, make_response, render_template

from db_pool import get_db_connection
from standardizers import ORGAN_SYSTEMS

paxcel_game_bp = Blueprint('paxcel_game', __name__, url_prefix='/paxcel/the_game')

_ORGAN_FILTER = f"BINARY organ_system IN ({', '.join(['%s'] * len(ORGAN_SYSTEMS))})"
_PLAYABLE_FILTER = (
    f"{_ORGAN_FILTER} "
    "AND question_stem IS NOT NULL AND question_stem <> '' "
    "AND correct_answer IS NOT NULL AND correct_answer <> ''"
)


def get_db():
    if 'paxcel_game_db' not in g:
        g.paxcel_game_db = get_db_connection()
    return g.paxcel_game_db


@paxcel_game_bp.teardown_request
def close_db(error):
    db = g.pop('paxcel_game_db', None)
    if db is not None:
        try:
            db.close()
        except Exception:
            pass


def fetch_random_question():
    """Pick one playable question uniformly at random (avoids ORDER BY RAND())."""
    cursor = get_db().cursor()
    cursor.execute(f"SELECT COUNT(*) AS c FROM question_bank WHERE {_PLAYABLE_FILTER}", ORGAN_SYSTEMS)
    row = cursor.fetchone()
    total = int(row['c']) if row else 0
    if total <= 0:
        return None
    cursor.execute(
        f"SELECT * FROM question_bank WHERE {_PLAYABLE_FILTER} ORDER BY id LIMIT 1 OFFSET %s",
        (*ORGAN_SYSTEMS, random.randrange(total)),
    )
    return cursor.fetchone()


def _no_store(response):
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response


@paxcel_game_bp.route('', strict_slashes=False)
def start():
    return _no_store(make_response(render_template('paxcel_game.html')))


@paxcel_game_bp.route('/play')
def play():
    question = fetch_random_question()
    return _no_store(make_response(render_template(
        'paxcel_game_play.html',
        question=question,
        organ_systems=list(ORGAN_SYSTEMS),
        answered=False,
        previous_choice=None,
        mode='study_exp',
        is_public_preview=True,
    )))
