-- Apply before deploying standardized source-heading readers/writers.
-- Pause publication/web traffic, back up, and test against a restored DB first.
-- MySQL DDL commits implicitly and may lock/rebuild question_bank.
-- Rerunnable; do not backfill or infer lost headings for historical questions.
SET @standardized_source_exists = (
    SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'question_bank'
      AND COLUMN_NAME = 'source_organ_system'
);
SET @standardized_source_ddl = IF(@standardized_source_exists = 0,
    'ALTER TABLE question_bank ADD COLUMN source_organ_system TEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL',
    'SELECT 1');
PREPARE standardized_source_migration FROM @standardized_source_ddl;
EXECUTE standardized_source_migration;
DEALLOCATE PREPARE standardized_source_migration;

SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLLATION_NAME
FROM information_schema.COLUMNS
WHERE TABLE_SCHEMA = DATABASE()
  AND TABLE_NAME = 'question_bank'
  AND COLUMN_NAME = 'source_organ_system';
