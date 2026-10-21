-- Follow-up to #44: connection settings alone cannot make utf8mb3 columns
-- accept four-byte Unicode (e.g. emoji in generated summaries/questions).
--
-- Operator instructions:
-- 1. Back up both tables and test this migration on a restored copy first.
--    Inspect SHOW CREATE TABLE for both tables and any additional foreign keys.
--    This targets the existing remediation_questions.assignment_id ->
--    remediation_assignments.id relationship. Review other referencing tables
--    before proceeding if the deployed schema differs.
--    CONVERT may widen TEXT to MEDIUMTEXT to preserve character capacity.
--    Check disk space, collation-dependent uniqueness, and index limits:
--    the three-VARCHAR(255) lookup index needs 3060 bytes under utf8mb4
--    (requires an InnoDB configuration supporting 3072-byte index keys).
-- 2. Pause ALL writers to these tables (web requests and background workers)
--    until both conversions and verification finish. ALTER may rebuild/lock
--    tables and commits implicitly; this is NOT a transactional migration.
-- 3. Run in a dedicated MySQL session with the target database selected:
--      mysql [connection options] --database=YOUR_DB < migrations/20261021_remediation_utf8mb4.sql
--    Use normal fail-fast batch mode, NOT --force. ALTER privileges are needed.
--    Checks are disabled ONLY in this session so both sides of the existing
--    foreign key can be converted without dropping the constraint.
-- 4. If any statement fails, disconnect the dedicated session (restoring its
--    session settings), keep writers paused, resolve the cause and rerun.
--    A partial conversion is not rolled back. Reapplying these conversions is
--    safe after completion or partial failure, but can still rebuild tables.
-- 5. Before resuming writers, verify the queries below: all character columns
--    and both table defaults must be utf8mb4_unicode_ci, and orphans must be 0.
--    Re-enabling FOREIGN_KEY_CHECKS does not validate existing rows.

SET @remediation_previous_fk_checks = @@SESSION.FOREIGN_KEY_CHECKS;
SET SESSION FOREIGN_KEY_CHECKS = 0;

ALTER TABLE remediation_assignments
    CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

ALTER TABLE remediation_questions
    CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

SET SESSION FOREIGN_KEY_CHECKS = @remediation_previous_fk_checks;

SELECT TABLE_NAME, TABLE_COLLATION
FROM information_schema.TABLES
WHERE TABLE_SCHEMA = DATABASE()
  AND TABLE_NAME IN ('remediation_assignments', 'remediation_questions');

SELECT TABLE_NAME, COLUMN_NAME, CHARACTER_SET_NAME, COLLATION_NAME
FROM information_schema.COLUMNS
WHERE TABLE_SCHEMA = DATABASE()
  AND TABLE_NAME IN ('remediation_assignments', 'remediation_questions')
  AND CHARACTER_SET_NAME IS NOT NULL;

SELECT COUNT(*) AS orphaned_questions
FROM remediation_questions AS q
LEFT JOIN remediation_assignments AS a ON a.id = q.assignment_id
WHERE q.assignment_id IS NOT NULL AND a.id IS NULL;
