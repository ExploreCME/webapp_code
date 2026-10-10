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
