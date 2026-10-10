-- migrations/20261010_add_norm_generated_columns.sql
-- Adds generated normalized lookup columns for empty/new datasets.
-- Safe to run before generating new questions, quizzes, attempts, and remediation assignments.

ALTER TABLE users
  ADD COLUMN clerk_id_norm VARCHAR(255)
    GENERATED ALWAYS AS (LOWER(TRIM(clerk_id))) STORED,
  ADD INDEX idx_users_clerk_id_norm (clerk_id_norm);

ALTER TABLE user_quizzes
  ADD COLUMN username_norm VARCHAR(255)
    GENERATED ALWAYS AS (LOWER(TRIM(username))) STORED,
  ADD COLUMN quiz_name_norm VARCHAR(255)
    GENERATED ALWAYS AS (LOWER(TRIM(quiz_name))) STORED,
  ADD INDEX idx_user_quizzes_username_norm (username_norm),
  ADD INDEX idx_user_quizzes_quiz_name_norm (quiz_name_norm);

ALTER TABLE quiz_attempts
  ADD COLUMN username_norm VARCHAR(255)
    GENERATED ALWAYS AS (LOWER(TRIM(username))) STORED,
  ADD COLUMN quiz_name_norm VARCHAR(255)
    GENERATED ALWAYS AS (LOWER(TRIM(quiz_name))) STORED,
  ADD INDEX idx_qa_username_norm (username_norm),
  ADD INDEX idx_qa_quiz_name_norm (quiz_name_norm);

ALTER TABLE remediation_assignments
  ADD COLUMN username_norm VARCHAR(255)
    GENERATED ALWAYS AS (LOWER(TRIM(username))) STORED,
  ADD COLUMN remediation_name_norm VARCHAR(255)
    GENERATED ALWAYS AS (LOWER(TRIM(remediation_name))) STORED,
  ADD COLUMN organ_system_norm VARCHAR(255)
    GENERATED ALWAYS AS (LOWER(TRIM(organ_system))) STORED,
  ADD COLUMN task_area_norm VARCHAR(255)
    GENERATED ALWAYS AS (LOWER(TRIM(task_area))) STORED,
  ADD COLUMN topic_area_norm VARCHAR(255)
    GENERATED ALWAYS AS (LOWER(TRIM(topic_area))) STORED,
  ADD INDEX idx_ra_username_norm (username_norm),
  ADD INDEX idx_ra_remediation_name_norm (remediation_name_norm),
  ADD INDEX idx_ra_organ_system_norm (organ_system_norm),
  ADD INDEX idx_ra_task_area_norm (task_area_norm),
  ADD INDEX idx_ra_topic_area_norm (topic_area_norm);

ALTER TABLE question_bank
  ADD COLUMN organ_system_norm VARCHAR(255)
    GENERATED ALWAYS AS (LOWER(TRIM(organ_system))) STORED,
  ADD COLUMN task_area_norm VARCHAR(255)
    GENERATED ALWAYS AS (LOWER(TRIM(task_area))) STORED,
  ADD COLUMN topic_area_norm VARCHAR(255)
    GENERATED ALWAYS AS (LOWER(TRIM(topic_area))) STORED,
  ADD INDEX idx_qb_organ_system_norm (organ_system_norm),
  ADD INDEX idx_qb_task_area_norm (task_area_norm),
  ADD INDEX idx_qb_topic_area_norm (topic_area_norm);
