-- Add latency-focused indexes for the highest-traffic query paths.
-- Run after the app is stable enough to allow an online index build.
-- These indexes are intended to reduce full scans on the quiz and stats routes.

CREATE INDEX idx_attempt_responses_question
    ON attempt_responses (question_id, user_choice, is_correct);

CREATE INDEX idx_quiz_attempts_user_quiz_time
    ON quiz_attempts (username, quiz_name, start_time);

CREATE INDEX idx_quiz_attempts_quiz_user
    ON quiz_attempts (quiz_name, username);

CREATE INDEX idx_user_quizzes_quiz_name
    ON user_quizzes (quiz_name);

CREATE INDEX idx_class_members_clerk_id
    ON class_members (clerk_id);

CREATE INDEX idx_class_members_class_clerk
    ON class_members (class_id, clerk_id);

CREATE INDEX idx_users_program_code
    ON users (program_code);

CREATE INDEX idx_question_bank_filters
    ON question_bank (organ_system, task_area, topic_area);

CREATE INDEX idx_assigned_quizzes_name
    ON assigned_quizzes (quiz_name);

CREATE INDEX idx_assigned_quizzes_class
    ON assigned_quizzes (class_id);

CREATE INDEX idx_assigned_quizzes_clerk
    ON assigned_quizzes (clerk_id);

CREATE INDEX idx_assigned_quizzes_by
    ON assigned_quizzes (assigned_by);

CREATE INDEX idx_assigned_quizzes_program
    ON assigned_quizzes (program_code);

CREATE INDEX idx_assigned_remediations_name
    ON assigned_remediations (remediation_name);

CREATE INDEX idx_assigned_remediations_class
    ON assigned_remediations (class_id);

CREATE INDEX idx_assigned_remediations_clerk
    ON assigned_remediations (clerk_id);

CREATE INDEX idx_assigned_remediations_by
    ON assigned_remediations (assigned_by);

CREATE INDEX idx_assigned_remediations_program
    ON assigned_remediations (program_code);

CREATE INDEX idx_remediation_assignments_name
    ON remediation_assignments (remediation_name);

CREATE INDEX idx_remediation_assignments_user
    ON remediation_assignments (username);

CREATE INDEX idx_classes_created_by
    ON classes (created_by);

CREATE INDEX idx_classes_program_code
    ON classes (program_code);
