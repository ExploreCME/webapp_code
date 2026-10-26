-- Apply before deploying source-heading readers/writers, with web/worker
-- remediation traffic paused. Back up and test against a restored DB first.
-- MySQL DDL commits implicitly and may lock/rebuild the table.
-- Rerunnable: leave existing source metadata untouched, including NULL legacy rows.
SET @source_heading_exists = (
    SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'remediation_assignments'
      AND COLUMN_NAME = 'source_organ_system'
);
SET @source_heading_ddl = IF(@source_heading_exists = 0,
    'ALTER TABLE remediation_assignments ADD COLUMN source_organ_system TEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL',
    'SELECT 1');
PREPARE source_heading_migration FROM @source_heading_ddl;
EXECUTE source_heading_migration;
DEALLOCATE PREPARE source_heading_migration;

SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLLATION_NAME
FROM information_schema.COLUMNS
WHERE TABLE_SCHEMA = DATABASE()
  AND TABLE_NAME = 'remediation_assignments'
  AND COLUMN_NAME = 'source_organ_system';
