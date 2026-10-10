-- 20261010_normalize_lookup_columns.sql
-- RECORD of the normalization columns applied manually to the production MySQL DB on 2026-10-10.
-- The application code does NOT currently read these columns.
-- NOTE: MySQL commits ALTER TABLE / CREATE INDEX implicitly, so a transaction wrapper gives no rollback protection.
-- NOTE: The `users` table has no `username` column, so it is intentionally not included here.
-- Do not re-run against a database where these columns already exist.

-- question_bank
ALTER TABLE question_bank
  ADD COLUMN organ_system_norm VARCHAR(255) NULL AFTER organ_system,
  ADD COLUMN task_area_norm VARCHAR(255) NULL AFTER task_area,
  ADD COLUMN topic_area_norm VARCHAR(255) NULL AFTER topic_area;

UPDATE question_bank
SET organ_system_norm = LOWER(TRIM(organ_system)),
    task_area_norm = LOWER(TRIM(task_area)),
    topic_area_norm = LOWER(TRIM(topic_area));

CREATE INDEX idx_question_bank_organ_norm ON question_bank (organ_system_norm, task_area_norm);
CREATE INDEX idx_question_bank_task_norm ON question_bank (task_area_norm);
CREATE INDEX idx_question_bank_topic_norm ON question_bank (topic_area_norm);

-- remediation_assignments
ALTER TABLE remediation_assignments
  ADD COLUMN organ_system_norm VARCHAR(255) NULL AFTER organ_system,
  ADD COLUMN task_area_norm VARCHAR(255) NULL AFTER task_area,
  ADD COLUMN topic_area_norm VARCHAR(255) NULL AFTER topic_area,
  ADD COLUMN username_norm VARCHAR(255) NULL AFTER username,
  ADD COLUMN remediation_name_norm VARCHAR(255) NULL AFTER remediation_name;

UPDATE remediation_assignments
SET organ_system_norm = LOWER(TRIM(organ_system)),
    task_area_norm = LOWER(TRIM(task_area)),
    topic_area_norm = LOWER(TRIM(topic_area)),
    username_norm = LOWER(TRIM(username)),
    remediation_name_norm = LOWER(TRIM(remediation_name));

CREATE INDEX idx_remediation_assignments_lookup ON remediation_assignments (organ_system_norm, task_area_norm, topic_area_norm);
CREATE INDEX idx_remediation_assignments_user_name ON remediation_assignments (username_norm, remediation_name_norm);

-- quiz_attempts
ALTER TABLE quiz_attempts
  ADD COLUMN username_norm VARCHAR(255) NULL AFTER username,
  ADD COLUMN quiz_name_norm VARCHAR(255) NULL AFTER quiz_name;

UPDATE quiz_attempts
SET username_norm = LOWER(TRIM(username)),
    quiz_name_norm = LOWER(TRIM(quiz_name));

CREATE INDEX idx_quiz_attempts_user_quiz_time ON quiz_attempts (username_norm, quiz_name_norm, start_time);
CREATE INDEX idx_quiz_attempts_quiz_name ON quiz_attempts (quiz_name_norm);

-- flashcards
ALTER TABLE flashcards
  ADD COLUMN organ_system_norm VARCHAR(255) NULL AFTER organ_system,
  ADD COLUMN topic_norm VARCHAR(255) NULL AFTER topic;

UPDATE flashcards
SET organ_system_norm = LOWER(TRIM(organ_system)),
    topic_norm = LOWER(TRIM(topic));

CREATE INDEX idx_flashcards_organ_topic ON flashcards (organ_system_norm, topic_norm);

-- class_members
ALTER TABLE class_members
  ADD COLUMN clerk_id_norm VARCHAR(255) NULL AFTER clerk_id;

UPDATE class_members
SET clerk_id_norm = LOWER(TRIM(clerk_id));

CREATE INDEX idx_class_members_clerk_norm ON class_members (class_id, clerk_id_norm);
