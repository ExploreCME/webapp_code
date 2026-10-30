import copy
import json
import os
from pathlib import Path
import sqlite3
import time
import unittest
from unittest.mock import MagicMock, patch

import pymysql
from flask import template_rendered
from redis.exceptions import ConnectionError as RedisConnectionError

import display_database
import paxcel_game

QUESTION = {
    'id': 'q-123',
    'organ_system': 'Cardiovascular System',
    'task_area': 'Clinical Intervention',
    'question_stem': '<p>A 54-year-old man has blood pressure of 162/98 mmHg.</p>',
    'answer_choice_A': 'Lisinopril',
    'answer_choice_B': 'Metoprolol',
    'answer_choice_C': 'Furosemide',
    'answer_choice_D': 'Clonidine',
    'answer_choice_E': '',
    'correct_answer': 'a',
    'explanation': 'PRIVATE EXPLANATION: ACE inhibitors are first-line therapy.',
}
TEST_CODE = 'test-only-private-host-code'


class SQLiteCursor:
    def __init__(self, connection):
        self.cursor = connection.cursor()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.cursor.close()

    def execute(self, sql, params=()):
        return self.cursor.execute(sql.replace('%s', '?'), params)

    def fetchone(self):
        row = self.cursor.fetchone()
        return dict(row) if row is not None else None

    def fetchall(self):
        return [dict(row) for row in self.cursor.fetchall()]

    @property
    def lastrowid(self):
        return self.cursor.lastrowid


class SQLiteConnection:
    """Run storage queries against real relational state without MySQL credentials."""
    def __init__(self):
        self.connection = sqlite3.connect(':memory:')
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript('''
            PRAGMA foreign_keys=ON;
            CREATE TABLE paxcel_game_questions (
                id INTEGER PRIMARY KEY AUTOINCREMENT, question_id TEXT NOT NULL,
                question_snapshot TEXT NOT NULL
            );
            CREATE TABLE paxcel_game_responses (
                id INTEGER PRIMARY KEY, game_question_id INTEGER NOT NULL,
                question_id TEXT NOT NULL, answer_choice TEXT NOT NULL,
                FOREIGN KEY(game_question_id) REFERENCES paxcel_game_questions(id)
            );
            INSERT INTO sqlite_sequence (name, seq) VALUES ('paxcel_game_questions', 999);
        ''')

    def cursor(self):
        return SQLiteCursor(self.connection)

    def commit(self):
        self.connection.commit()

    def close(self):
        # A request closes a pooled handle, not this test's underlying database.
        pass


class PaxcelGameTests(unittest.TestCase):
    def setUp(self):
        self.app = display_database.app
        config = self.app.config.copy()
        self.addCleanup(lambda: self.app.config.update(config))
        self.app.config.update(TESTING=True, SECRET_KEY='game-test-session-secret')
        self.client = self.app.test_client()
        self.db = SQLiteConnection()
        self.addCleanup(self.db.connection.close)
        self.contexts = []

        def capture(sender, template, context, **extra):
            self.contexts.append(context)

        template_rendered.connect(capture, self.app, weak=False)
        self.addCleanup(template_rendered.disconnect, capture, self.app)
        for patcher in (
            patch.dict(os.environ, {'PAXCEL_GAME_ADMIN_CODE': TEST_CODE}),
            patch.object(display_database, 'MAINTENANCE_MODE', False),
            patch.object(paxcel_game, 'get_db_connection', return_value=self.db),
            patch.object(paxcel_game, 'fetch_random_question', return_value=copy.deepcopy(QUESTION)),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        with self.app.app_context():
            display_database.cache.clear()

    def token(self, client=None):
        with (client or self.client).session_transaction() as state:
            return state['paxcel_game_csrf']

    def login(self, client=None):
        client = client or self.client
        client.get('/paxcel/the_game/admin')
        return client.post('/paxcel/the_game/admin', data={
            'csrf_token': self.token(client), 'code': TEST_CODE,
        })

    def create_question(self):
        self.login()
        response = self.client.post('/paxcel/the_game/play', data={'csrf_token': self.token()})
        self.assertEqual(response.status_code, 303)
        return int(response.location.rsplit('/', 1)[1])

    def vote(self, client, number, choice='B'):
        client.get(f'/paxcel/the_game/question/{number}')
        return client.post(f'/paxcel/the_game/question/{number}', data={
            'csrf_token': self.token(client), 'user_choice': choice,
        })

    def test_public_entry_has_lookup_and_no_host_go(self):
        response = self.client.get('/paxcel/the_game')
        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn('name="question_number"', html)
        self.assertNotIn('id="goButton"', html)
        self.assertIn('no-store', response.headers['Cache-Control'])
        self.assertIn('noindex', response.headers['X-Robots-Tag'])

    def test_login_preserves_go_screen_and_logout_removes_access(self):
        self.assertEqual(self.login().status_code, 303)
        html = self.client.get('/paxcel/the_game').get_data(as_text=True)
        self.assertIn('PANCE prep', html)
        self.assertIn('id="goButton"', html)
        self.assertIn('method="post" action="/paxcel/the_game/play"', html)
        response = self.client.post('/paxcel/the_game/admin/logout', data={'csrf_token': self.token()})
        self.assertEqual(response.status_code, 303)
        self.assertEqual(self.client.get('/paxcel/the_game/draw/1000').status_code, 403)

    def test_login_unconfigured_or_short_code_is_disabled(self):
        for code in ('', '123456'):
            with self.subTest(code=code), patch.dict(os.environ, {'PAXCEL_GAME_ADMIN_CODE': code}):
                self.assertEqual(self.client.get('/paxcel/the_game/admin').status_code, 503)

    def test_failed_logins_are_throttled(self):
        self.client.get('/paxcel/the_game/admin')
        for _ in range(paxcel_game.LOGIN_LIMIT):
            response = self.client.post('/paxcel/the_game/admin', data={
                'csrf_token': self.token(), 'code': 'wrong',
            })
            self.assertEqual(response.status_code, 403)
        response = self.client.post('/paxcel/the_game/admin', data={
            'csrf_token': self.token(), 'code': TEST_CODE,
        })
        self.assertEqual(response.status_code, 429)
        self.assertIn('Retry-After', response.headers)
        self.assertNotIn('paxcel_game_host', self.client.get('/paxcel/the_game').get_data(as_text=True))

    def test_admin_code_never_accepted_in_url(self):
        self.client.get('/paxcel/the_game?code=' + TEST_CODE)
        self.assertEqual(self.client.get('/paxcel/the_game/draw/1000').status_code, 403)

    def test_login_storage_outage_does_not_grant_access(self):
        self.client.get('/paxcel/the_game/admin')
        with patch.object(display_database.cache, 'add', side_effect=RedisConnectionError('offline')):
            response = self.client.post('/paxcel/the_game/admin', data={
                'csrf_token': self.token(), 'code': TEST_CODE,
            })
        self.assertEqual(response.status_code, 503)
        with self.client.session_transaction() as state:
            self.assertNotIn('paxcel_game_host', state)

    def test_csrf_required_for_login_draw_logout_and_votes(self):
        self.client.get('/paxcel/the_game')
        for path in ('/admin', '/play', '/admin/logout', '/question/1000', ''):
            with self.subTest(path=path):
                self.assertEqual(self.client.post('/paxcel/the_game' + path).status_code, 400)
                self.assertEqual(self.client.post('/paxcel/the_game' + path, data={
                    'csrf_token': 'invalid-☃',
                }).status_code, 400)

    def test_nonhosts_cannot_draw_or_read_host_pages(self):
        number = self.create_question()
        audience = self.app.test_client()
        audience.get('/paxcel/the_game')
        self.assertEqual(audience.post('/paxcel/the_game/play', data={
            'csrf_token': self.token(audience),
        }).status_code, 403)
        for path in (f'/draw/{number}', f'/question/{number}/results',
                     f'/question/{number}/results.json'):
            with self.subTest(path=path):
                self.assertEqual(audience.get('/paxcel/the_game' + path).status_code, 403)

    def test_session_expiration_and_code_rotation_revoke_host(self):
        number = self.create_question()
        with patch.dict(os.environ, {'PAXCEL_GAME_ADMIN_CODE': TEST_CODE + '-rotated'}):
            self.assertEqual(self.client.get(f'/paxcel/the_game/draw/{number}').status_code, 403)
        with self.client.session_transaction() as state:
            host = dict(state['paxcel_game_host'])
            host['expires'] = time.time() - 1
            state['paxcel_game_host'] = host
        self.assertEqual(self.client.get(f'/paxcel/the_game/question/{number}/results.json').status_code, 403)

    def test_draw_has_real_stable_number_and_manual_question_button(self):
        number = self.create_question()
        self.assertEqual(number, 1000)
        for _ in range(2):
            response = self.client.get(f'/paxcel/the_game/draw/{number}')
            html = response.get_data(as_text=True)
            self.assertIn("Let's see the question", html)
            self.assertIn(f'href="/paxcel/the_game/question/{number}"', html)
            self.assertIn(f'textContent = {number}', html)
            self.assertNotIn(QUESTION['question_stem'], html)
        self.assertEqual(self.db.connection.execute('SELECT COUNT(*) FROM paxcel_game_questions').fetchone()[0], 1)

    def test_snapshot_is_unchanged_after_bank_updates(self):
        number = self.create_question()
        changed = dict(QUESTION, question_stem='Changed later')
        with patch.object(paxcel_game, 'fetch_random_question', return_value=changed):
            html = self.client.get(f'/paxcel/the_game/question/{number}').get_data(as_text=True)
            self.assertIn(QUESTION['question_stem'], html)
            self.assertNotIn('Changed later', html)

    def test_audience_lookup_then_vote_never_discloses_answers(self):
        number = self.create_question()
        audience = self.app.test_client()
        audience.get('/paxcel/the_game')
        response = audience.post('/paxcel/the_game', data={
            'csrf_token': self.token(audience), 'question_number': str(number),
        })
        self.assertEqual(response.status_code, 303)
        response = audience.get(response.location)
        html = response.get_data(as_text=True)
        self.assertIn(QUESTION['question_stem'], html)
        self.assertLess(html.index(QUESTION['question_stem']), html.index('value="A"'))
        self.assertIn('class="quiz-viewport game-viewport"', html)
        for private in ('correct_answer', QUESTION['explanation'], 'gameExplanation', 'Results',
                        'const correct', 'reveal(', 'gameTimer', 'rateQuestion('):
            self.assertNotIn(private, html)
        # No private values in template context, not merely hidden DOM elements.
        self.assertNotIn('correct_answer', self.contexts[-1]['question'])
        self.assertNotIn('explanation', self.contexts[-1]['question'])
        response = self.vote(audience, number, 'B')
        self.assertEqual(response.status_code, 303)
        confirmation = audience.get(response.location).get_data(as_text=True)
        self.assertIn('Response saved!', confirmation)
        self.assertNotIn(QUESTION['explanation'], confirmation)
        row = self.db.connection.execute('SELECT * FROM paxcel_game_responses').fetchone()
        self.assertEqual((row['game_question_id'], row['question_id'], row['answer_choice']), (number, QUESTION['id'], 'B'))

    def test_repeated_votes_allowed_and_distributions_isolated(self):
        first = self.create_question()
        second = self.create_question()
        audience = self.app.test_client()
        for number, choice in ((first, 'A'), (first, 'B'), (first, 'B'), (second, 'D')):
            self.assertEqual(self.vote(audience, number, choice).status_code, 303)
        data = self.client.get(f'/paxcel/the_game/question/{first}/results.json').get_json()
        self.assertEqual(data['total'], 3)
        self.assertEqual(data['choices'], [
            {'letter': 'A', 'count': 1, 'percent': 33.3},
            {'letter': 'B', 'count': 2, 'percent': 66.7},
            {'letter': 'C', 'count': 0, 'percent': 0.0},
            {'letter': 'D', 'count': 0, 'percent': 0.0},
        ])
        self.assertEqual(self.client.get(f'/paxcel/the_game/question/{second}/results.json').get_json()['total'], 1)

    def test_zero_votes_and_results_tabs_correct_marker_explanation_toggle(self):
        number = self.create_question()
        data = self.client.get(f'/paxcel/the_game/question/{number}/results.json').get_json()
        self.assertEqual(data['total'], 0)
        self.assertTrue(all(choice['percent'] == 0 for choice in data['choices']))
        html = self.client.get(f'/paxcel/the_game/question/{number}/results').get_data(as_text=True)
        for expected in ('Question stem', 'Answer choices', 'Live results', 'Correct answer',
                         'Show explanation', 'id="hostExplanation" hidden', QUESTION['explanation'],
                         'setTimeout(refresh, 3000)'):
            self.assertIn(expected, html)
        question_html = self.client.get(f'/paxcel/the_game/question/{number}').get_data(as_text=True)
        self.assertIn('>Results</a>', question_html)
        self.assertNotIn(QUESTION['explanation'], question_html)

    def test_invalid_and_missing_questions_and_choices_never_save(self):
        number = self.create_question()
        audience = self.app.test_client()
        audience.get('/paxcel/the_game')
        for value in ('0', '-1', 'abc', '1' * 100, '9223372036854775808'):
            response = audience.post('/paxcel/the_game', data={
                'csrf_token': self.token(audience), 'question_number': value,
            })
            self.assertEqual(response.status_code, 400)
        self.assertEqual(audience.get('/paxcel/the_game/question/999999').status_code, 404)
        for choice in ('', 'E', 'Z', 'B; DELETE FROM question_bank'):
            self.assertEqual(self.vote(audience, number, choice).status_code, 400)
        self.assertEqual(self.db.connection.execute('SELECT COUNT(*) FROM paxcel_game_responses').fetchone()[0], 0)

    def test_database_write_failure_never_confirms_success(self):
        number = self.create_question()
        audience = self.app.test_client()
        audience.get(f'/paxcel/the_game/question/{number}')
        real_execute = SQLiteCursor.execute

        def fail_insert(cursor, sql, params=()):
            if sql.startswith('INSERT INTO paxcel_game_responses'):
                raise pymysql.OperationalError(1146, 'Response table missing')
            return real_execute(cursor, sql, params)

        with patch.object(SQLiteCursor, 'execute', fail_insert):
            response = self.vote(audience, number, 'A')
        self.assertEqual(response.status_code, 503)
        self.assertNotIn('Response saved!', response.get_data(as_text=True))

    def test_empty_bank_reports_error_without_number(self):
        self.login()
        with patch.object(paxcel_game, 'fetch_random_question', return_value=None):
            response = self.client.post('/paxcel/the_game/play', data={'csrf_token': self.token()})
        self.assertEqual(response.status_code, 503)
        self.assertIn('No questions are available', response.get_data(as_text=True))


class GameRandomSelectionTests(unittest.TestCase):
    def test_random_offset_and_valid_choice_filters(self):
        db = MagicMock()
        cursor = db.cursor.return_value.__enter__.return_value
        cursor.fetchone.side_effect = [{'c': 50}, QUESTION]
        with patch.object(paxcel_game, 'get_db', return_value=db), patch.object(
            paxcel_game.random, 'randrange', return_value=7,
        ) as randrange:
            self.assertEqual(paxcel_game.fetch_random_question(), QUESTION)
        randrange.assert_called_once_with(50)
        sql, params = cursor.execute.call_args.args
        self.assertIn('LIMIT 1 OFFSET %s', sql)
        self.assertIn('CASE UPPER(TRIM(correct_answer))', sql)
        self.assertEqual(params[-1], 7)
        self.assertEqual(params[:-1], paxcel_game.ORGAN_SYSTEMS)

    def test_schema_included_in_initializer(self):
        root = Path(__file__).resolve().parents[1]
        migration = root / 'migrations' / '20261030_add_paxcel_game.sql'
        self.assertIn(migration.name, (root / 'init.db.py').read_text())
        sql = migration.read_text()
        self.assertIn('AUTO_INCREMENT=1000', sql)
        self.assertIn('FOREIGN KEY (game_question_id)', sql)
        self.assertIn('question_snapshot JSON NOT NULL', sql)


if __name__ == '__main__':
    unittest.main()
