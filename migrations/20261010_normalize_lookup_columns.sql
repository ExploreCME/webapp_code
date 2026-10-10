    def generate_overall_usage(self, clerk_ids, include_practice=True):
        """Generates overall database usage statistics for a list of students."""
        format_strings = ','.join(['%s'] * len(clerk_ids))

        # Added an optional flag to match test-mode strictness if needed
        mode_filter = "" if include_practice else "AND qa.mode = 'test'"

        # 1. Fetch valid organ systems (Top 14) to establish our defined list
        self.m_cursor.execute("""
            SELECT organ_system_norm AS organ_system
            FROM question_bank
            WHERE organ_system_norm IS NOT NULL AND organ_system_norm != ''
            GROUP BY organ_system_norm
            ORDER BY COUNT(*) DESC
            LIMIT 14
        """)
        valid_organs = [row['organ_system'] for row in self.m_cursor.fetchall() if row['organ_system']]

        # 2. Fetch the raw responses
        self.m_cursor.execute(f'''
            SELECT ar.is_correct,
                   COALESCE(qb.organ_system_norm, ra.organ_system_norm) AS organ_system,
                   COALESCE(qb.task_area_norm, ra.task_area_norm) AS task_area,
                   ra.topic_area
            FROM attempt_responses ar
            JOIN quiz_attempts qa ON ar.attempt_id = qa.attempt_id
            LEFT JOIN question_bank qb ON ar.question_id = qb.id
            LEFT JOIN remediation_questions rq ON ar.question_id = rq.id
            LEFT JOIN remediation_assignments ra ON rq.assignment_id = ra.id
            WHERE qa.username IN ({format_strings}) {mode_filter}
        ''', tuple(clerk_ids))

        responses = self.m_cursor.fetchall()
