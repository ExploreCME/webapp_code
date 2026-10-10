# ExploreCME Webapp

This repository contains the web application and supporting services for the ExploreCME platform.

## Database maintenance

The repository includes database migration scripts in `migrations/`.

### Run the hot-path index migration

Before launch, or when new traffic begins, run the performance migration manually against the production database:

```bash
mysql --host="$MYSQL_HOST" \
      --port="${MYSQL_PORT:-3306}" \
      --user="$MYSQL_USER" \
      --password \
      --database="$MYSQL_DB" \
      < migrations/20261010_add_hot_path_indexes.sql
```

This migration adds indexes for the highest-traffic query paths, including quiz attempts, question lookups, class membership, and assignment lookups.

### Notes

- The migration file does not run automatically.
- Run it during a controlled maintenance window or a safe deployment step.
- After running the migration, verify the query plans with `EXPLAIN` on the busiest routes.

### Flashcard progress schema prerequisite

The normalized `flashcard_progress` migration was already applied manually in
production after an empty collision preflight. Its `organ_system_norm` and
`topic_norm` columns are stored generated columns using `LOWER(TRIM(...))`, and
`idx_flashcard_progress_norm_unique` is unique on
`(username, organ_system_norm, topic_norm)`. The original raw primary key remains
in place. **Do not blindly rerun the migration or recreate these columns/index.**
Verify the target schema with `SHOW CREATE TABLE flashcard_progress` and
`SHOW INDEX FROM flashcard_progress` before deploying.

Flashcard reads require the normalized columns on both `flashcards` and
`flashcard_progress`. Progress saves still write raw organ/topic labels; the
generated columns and unique index handle case/spacing variants. Clerk usernames
are passed through unchanged. No migration DDL runs from the application.

Run the focused regression tests from the repository root:

```bash
python -m unittest discover -s tests -p 'test_flashcards.py' -v
```

These use an isolated SQLite fixture with generated columns and an upsert syntax
adapter; verify save/resume against a safe MySQL test database separately to cover
MySQL-specific schema and collation behavior.

Hot-path query batching (quiz mode attempt counts, remediation module stats,
question resume position, and user assignment scope) is covered by query-count
regression tests:

```bash
python -m unittest discover -s tests -p 'test_query_batching.py' -v
```
