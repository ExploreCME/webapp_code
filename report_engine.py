# report_engine.py
import csv
import io
import math
import re
import os

# --- IMPORT SHARED CLEANING LOGIC ---
from shared_utils import SHARED_VALID_TASK_AREAS, get_standardized_task_area


class ReportEngine:
    # Accept the second argument as a fallback to prevent breaking faculty_routes.py
    def __init__(self, mysql_conn, legacy_sqlite_conn=None):
        self.m_conn = mysql_conn
        self.m_cursor = self.m_conn.cursor()

    def _get_class_roster(self, class_id):
        # MIGRATED TO MYSQL: Using %s instead of ?
        self.m_cursor.execute("""
            SELECT u.clerk_id, u.first_name, u.last_name, u.email
            FROM class_members cm
            JOIN users u ON cm.clerk_id = u.clerk_id
            WHERE cm.class_id = %s
        """, (class_id,))
        return self.m_cursor.fetchall()

    def generate_individual_report(self, clerk_id, target_type, target_name, attempt_filter='highest'):
        """
        Returns percentage and response data for a single student.
        Supports filtering by 'highest' score, 'recent' attempt, or 'all' attempts.
        Returns a list of dictionaries.
        """
        mode_filter = "AND qa.mode = 'test'" if target_type != 'remediation' else ""

        # 1. Fetch all attempts for the user (Most recent first)
        self.m_cursor.execute(f'''
            SELECT qa.attempt_id, qa.start_time,
                   COUNT(ar.id) as answered_count,
                   COALESCE(SUM(ar.is_correct), 0) as correct_answers
            FROM quiz_attempts qa
            LEFT JOIN attempt_responses ar ON qa.attempt_id = ar.attempt_id
            WHERE qa.username = %s AND qa.quiz_name = %s {mode_filter}
            GROUP BY qa.attempt_id, qa.start_time
            ORDER BY qa.start_time DESC
        ''', (clerk_id, target_name))

        all_attempts = self.m_cursor.fetchall()

        if not all_attempts:
            return []

        # 2. Filter the attempts based on the requested logic (Synced with UI Route)
        clean_filter = str(attempt_filter).lower().strip() if attempt_filter else 'highest'

        if clean_filter == 'all':
            selected_attempts = all_attempts
        elif clean_filter == 'recent':
            selected_attempts = [all_attempts[0]]  # SQL already ordered by start_time DESC
        else:  # Default to 'highest'
            best_attempt = all_attempts[0]
            best_pct = -1

            for att in all_attempts:
                ans = att['answered_count'] or 0
                corr = att['correct_answers'] or 0
                # SYNC FIX: Using identical rounding logic as the route
                pct = round((corr / ans * 100), 1) if ans > 0 else 0

                # Because of DESC order, keeping strictly '>' means ties go to the newer attempt
                if pct > best_pct:
                    best_pct = pct
                    best_attempt = att

            selected_attempts = [best_attempt]

        # 3. Generate the detailed report for each selected attempt
        reports = []
        for att in selected_attempts:
            attempt_id = att['attempt_id']

            self.m_cursor.execute('''
                SELECT question_id, user_choice, correct_answer, is_correct
                FROM attempt_responses WHERE attempt_id = %s ORDER BY submitted_time ASC
            ''', (attempt_id,))
            answers = self.m_cursor.fetchall()

            total = len(answers)
            correct = sum(1 for a in answers if a['is_correct'])
            pct = round((correct / total * 100), 1) if total > 0 else 0

            module_breakdown = []
            if target_type == 'remediation':
                self.m_cursor.execute('''
                    SELECT ra.topic_area, COUNT(ar.id) as answered, COALESCE(SUM(ar.is_correct), 0) as correct
                    FROM attempt_responses ar
                    JOIN remediation_questions rq ON ar.question_id = rq.id
                    JOIN remediation_assignments ra ON rq.assignment_id = ra.id
                    WHERE ar.attempt_id = %s
                    GROUP BY ra.id, ra.topic_area
                ''', (attempt_id,))

                for row in self.m_cursor.fetchall():
                    m_ans = int(row['answered'] or 0)
                    m_cor = int(row['correct'] or 0)
                    module_breakdown.append({
                        'topic_area': row['topic_area'],
                        'answered': m_ans,
                        'correct': m_cor,
                        'percentage': round((m_cor / m_ans * 100), 1) if m_ans > 0 else 0
                    })

            reports.append({
                'attempt_id': attempt_id,
                'percentage': pct,
                'answers': answers,
                'module_breakdown': module_breakdown
            })

        return reports

    def generate_overall_usage(self, clerk_ids, include_practice=True):
        """Generates overall database usage statistics for a list of students."""
        format_strings = ','.join(['%s'] * len(clerk_ids))

        # Added an optional flag to match test-mode strictness if needed
        mode_filter = "" if include_practice else "AND qa.mode = 'test'"

        # 1. Fetch valid organ systems (Top 14) to establish our defined list
        self.m_cursor.execute("""
            SELECT TRIM(organ_system) as organ_system
            FROM question_bank
            WHERE organ_system IS NOT NULL AND TRIM(organ_system) != ''
            GROUP BY TRIM(organ_system)
            ORDER BY COUNT(*) DESC
            LIMIT 14
        """)
        valid_organs = [row['organ_system'] for row in self.m_cursor.fetchall() if row['organ_system']]

        # 2. Fetch the raw responses
        self.m_cursor.execute(f'''
            SELECT ar.is_correct,
                   COALESCE(qb.organ_system, ra.organ_system) AS organ_system,
                   COALESCE(qb.task_area, ra.task_area) AS task_area,
                   ra.topic_area
            FROM attempt_responses ar
            JOIN quiz_attempts qa ON ar.attempt_id = qa.attempt_id
            LEFT JOIN question_bank qb ON ar.question_id = qb.id
            LEFT JOIN remediation_questions rq ON ar.question_id = rq.id
            LEFT JOIN remediation_assignments ra ON rq.assignment_id = ra.id
            WHERE qa.username IN ({format_strings}) {mode_filter}
        ''', tuple(clerk_ids))

        responses = self.m_cursor.fetchall()

        # --- DEBUG FILE WRITING (UPDATED WITH ABSOLUTE PATH) ---
        current_dir = os.path.dirname(os.path.abspath(__file__))
        file_path = os.path.join(current_dir, 'debug_overall_usage.txt')

        try:
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write("Organ System | Task Area | Topic Area\n")
                f.write("-" * 80 + "\n")
                for r in responses:
                    org = str(r.get('organ_system') or "None").strip()
                    task = str(r.get('task_area') or "None").strip()
                    topic = str(r.get('topic_area') or "None").strip()
                    f.write(f"{org} | {task} | {topic}\n")
        except Exception as e:
            print(f"Failed to write debug file: {e}")
        # --------------------------

        # 3. Calculate Stats and Group Miscellaneous
        stats = {'total_attempted': len(responses), 'total_correct': 0, 'organs': {}, 'tasks': {}}
        for r in responses:
            if r['is_correct']:
                stats['total_correct'] += 1

            # Check if organ is in the defined valid list, else force to 'Miscellaneous'
            org = r['organ_system'] or 'Unknown'
            if org not in valid_organs:
                org = "Miscellaneous"

            mapped_task = get_standardized_task_area(r['task_area'])

            if org not in stats['organs']:
                stats['organs'][org] = {'att': 0, 'cor': 0}

            stats['organs'][org]['att'] += 1
            if r['is_correct']:
                stats['organs'][org]['cor'] += 1

            # Apply filtering based on SHARED_VALID_TASK_AREAS for the 'tasks' breakdown
            if mapped_task in SHARED_VALID_TASK_AREAS:
                if mapped_task not in stats['tasks']:
                    stats['tasks'][mapped_task] = {'att': 0, 'cor': 0}
                stats['tasks'][mapped_task]['att'] += 1
                if r['is_correct']:
                    stats['tasks'][mapped_task]['cor'] += 1

        stats['overall_pct'] = round((stats['total_correct'] / stats['total_attempted'] * 100), 1) if stats['total_attempted'] > 0 else 0
        return stats

    def generate_item_analysis(self, class_id, target_type, target_name, attempt_filter='highest'):
        """
        Performs ExamSoft-style psychometric analysis on a specific quiz/remediation.
        Supports filtering by 'highest' score, 'recent' attempt, or 'all' attempts.
        Calculates Item Difficulty (p-value), Discrimination Index, and Distractor Frequencies.
        """
        roster = self._get_class_roster(class_id)
        if not roster:
            return None

        clerk_ids = [student['clerk_id'] for student in roster]
        format_strings = ','.join(['%s'] * len(clerk_ids))
        mode_filter = "AND qa.mode = 'test'" if target_type != 'remediation' else ""

        # 1. Fetch ALL relevant attempts for the class
        self.m_cursor.execute(f'''
            SELECT qa.username, qa.attempt_id, qa.start_time,
                   COUNT(ar.id) as answered_count,
                   COALESCE(SUM(ar.is_correct), 0) as correct_answers
            FROM quiz_attempts qa
            LEFT JOIN attempt_responses ar ON qa.attempt_id = ar.attempt_id
            WHERE qa.quiz_name = %s AND qa.username IN ({format_strings}) {mode_filter}
            GROUP BY qa.attempt_id, qa.username, qa.start_time
            ORDER BY qa.start_time DESC
        ''', (target_name, *clerk_ids))

        all_class_attempts = self.m_cursor.fetchall()
        if not all_class_attempts:
            return None

        # 2. Filter the attempts based on the requested logic (Synced with UI Route)
        clean_filter = str(attempt_filter).lower().strip() if attempt_filter else 'highest'

        if clean_filter == 'all':
            attempt_ids = [att['attempt_id'] for att in all_class_attempts]

        elif clean_filter == 'recent':
            recent_map = {}
            for att in all_class_attempts:
                user = att['username']
                # SQL orders by start_time DESC, so the first one encountered is the most recent
                if user not in recent_map:
                    recent_map[user] = att['attempt_id']
            attempt_ids = list(recent_map.values())

        else:  # Default to 'highest'
            best_attempts_map = {}
            for att in all_class_attempts:
                user = att['username']
                ans = att['answered_count'] or 0
                corr = att['correct_answers'] or 0

                # SYNC FIX: Using identical rounding logic as the route
                pct = round((corr / ans * 100), 1) if ans > 0 else 0

                # Because of DESC order, keeping strictly '>' means ties go to the newer attempt
                if user not in best_attempts_map or pct > best_attempts_map[user]['pct']:
                    best_attempts_map[user] = {'attempt_id': att['attempt_id'], 'pct': pct}

            attempt_ids = [data['attempt_id'] for data in best_attempts_map.values()]

        if not attempt_ids:
            return None

        att_format = ','.join(['%s'] * len(attempt_ids))

        # 3. Fetch all responses for these filtered attempts
        self.m_cursor.execute(f'''
            SELECT attempt_id, question_id, user_choice, correct_answer, is_correct
            FROM attempt_responses
            WHERE attempt_id IN ({att_format})
        ''', tuple(attempt_ids))

        all_responses = self.m_cursor.fetchall()

        # 4. Calculate total scores to determine Upper / Lower 27% boundaries
        student_scores = {att_id: 0 for att_id in attempt_ids}
        question_data = {}

        for r in all_responses:
            q_id = r['question_id']
            att_id = r['attempt_id']

            if r['is_correct']:
                student_scores[att_id] += 1

            if q_id not in question_data:
                question_data[q_id] = {
                    'total_attempts': 0, 'correct_count': 0, 'correct_answer': r['correct_answer'],
                    'choices': {'A': 0, 'B': 0, 'C': 0, 'D': 0, 'E': 0},
                    'upper_correct': 0, 'lower_correct': 0
                }

            question_data[q_id]['total_attempts'] += 1
            if r['is_correct']:
                question_data[q_id]['correct_count'] += 1
            if r['user_choice'] in question_data[q_id]['choices']:
                question_data[q_id]['choices'][r['user_choice']] += 1

        # Sort attempts by score descending
        sorted_attempts = sorted(student_scores.items(), key=lambda item: item[1], reverse=True)
        total_students = len(sorted_attempts)

        # Upper/Lower 27% Rule for Discrimination Index
        group_size = max(1, math.ceil(total_students * 0.27))
        upper_group = set([x[0] for x in sorted_attempts[:group_size]])
        lower_group = set([x[0] for x in sorted_attempts[-group_size:]])

        # Recalculate metrics based on groups
        for r in all_responses:
            if r['is_correct']:
                q_id = r['question_id']
                att_id = r['attempt_id']
                if att_id in upper_group:
                    question_data[q_id]['upper_correct'] += 1
                if att_id in lower_group:
                    question_data[q_id]['lower_correct'] += 1

        # Final Analysis Compilation
        analysis_report = []
        for q_id, data in question_data.items():
            t_att = data['total_attempts']
            p_value = data['correct_count'] / t_att if t_att > 0 else 0

            # Discrimination Index: D = (Correct in Upper / Group Size) - (Correct in Lower / Group Size)
            discrim_index = (data['upper_correct'] / group_size) - (data['lower_correct'] / group_size)

            analysis_report.append({
                'question_id': q_id,
                'correct_answer': data['correct_answer'],
                'difficulty_p_value': round(p_value, 2),
                'discrimination_index': round(discrim_index, 2),
                'distractor_a': round(data['choices']['A'] / t_att * 100, 1) if t_att else 0,
                'distractor_b': round(data['choices']['B'] / t_att * 100, 1) if t_att else 0,
                'distractor_c': round(data['choices']['C'] / t_att * 100, 1) if t_att else 0,
                'distractor_d': round(data['choices']['D'] / t_att * 100, 1) if t_att else 0,
                'distractor_e': round(data['choices']['E'] / t_att * 100, 1) if t_att else 0
            })

        return analysis_report
