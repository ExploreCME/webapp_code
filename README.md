# ExploreCME Webapp

This repository contains the web application and supporting services for the ExploreCME platform.

## Uploaded documents on Render

Uploads use temporary OpenAI Files with `purpose="user_data"`, not an S3 bucket,
GitHub, or persistent Render storage. Configure both web and worker:

| Variable | Value |
| --- | --- |
| `OPENAI_API_KEY` | Required on both services as a Render secret; both keys must belong to the same OpenAI project so the worker can access uploaded file IDs |
| `MAX_DOCUMENT_BYTES` | Maximum PDF bytes; default `20971520` (20 MiB), identical on both services |
| `DOCUMENT_TTL_SECONDS` | OpenAI file lifetime; default `86400` (24 hours), valid range `3600`..`2592000` seconds, identical on both services |

Use HTTPS and project-scoped credentials with the file create/read/delete and
model permissions each service needs. Do not put credentials or user documents
in GitHub. Remove all former S3/AWS environment configuration from both services,
including manually configured Render secrets; no bucket is required.

Both upload forms accept only readable, nonempty, unencrypted PDFs. Files are
size-limited and validated in memory before upload. Each OpenAI
file is created with `purpose="user_data"` and
`expires_after={"anchor": "created_at", "seconds": DOCUMENT_TTL_SECONDS}`.
Expiration is attached at creation, not after queueing. The existing
`quiz_generation_tasks.payload` JSON records the owner, original sanitized
filename, OpenAI file ID, size, and upload time, not a local path or S3 object key.
The row's `id` is the associated task ID, so no schema migration is required.
Only successfully uploaded file IDs are queued. Quiz creation and task insertion
commit together. Uploads that cannot be queued expire automatically under the same TTL.

The worker conditionally claims pending tasks and uses the queued OpenAI file ID
for extraction and question generation. Generated questions are saved in MySQL
only; no extracted-topic or generated-question JSON files are retained remotely.
Temporary local files are removed, and the remote OpenAI file is deleted after
processing succeeds **or fails**. The document list comes from owner-filtered
task metadata, not retained source files or local directory scans.

### Retention and privacy (required before enabling uploads)

Set `DOCUMENT_TTL_SECONDS` to the institution-approved period within the valid
range and keep the published policy consistent. OpenAI's automatic file
expiration catches crash/orphan files, including uploads left by a process crash
before queueing or a failed cleanup request. It is a backstop for immediate
deletion after success or failure, not a retained retry window. File expiration
does not delete task metadata or questions stored in MySQL.

Allow for provider deletion delays and keep queue/processing deadlines shorter
than the configured TTL. Monitor backlog, failed tasks, and cleanup failures.
Pending task rows survive redeployments, but their files can expire. After a
worker restart, inspect interrupted `processing` tasks; do not blindly reset
them or regenerate questions already saved. There is no retained-document retry
button. After failure or expiration, users must re-upload with a **new quiz or
remediation assignment name** until conflicting existing names are resolved.

Uploaded PDFs are sent to OpenAI for temporary processing. Local validation is
not a malware scan or de-identification tool. Upload notices prohibit
unauthorized confidential or patient-identifying data. Before rollout, update
the published privacy policy and obtain institution approval for storage region,
the configured TTL, and OpenAI's current processing/retention terms; do not assume
remote deletion or expiration means zero provider retention. Existing static
privacy PDFs are not rewritten by this change.

### Migrating existing tasks

Legacy S3/object-key and `filepath` payloads are explicitly rejected with a
re-upload message; there is no bucket or PythonAnywhere path fallback and no
automatic migration. Have users re-upload pending or failed legacy documents
with new quiz/assignment names until conflicting old names are resolved.
Operators must clean up the old bucket and its retained PDFs/generated JSON
using the old bucket's lifecycle policy or explicit deletion. Include noncurrent
versions, delete markers, and incomplete multipart uploads where applicable;
removing application credentials does not delete old objects.

Before production rollout, exercise both upload forms with a safe PDF on the
web service, confirm file-ID access from the separate worker in the same OpenAI
project, and verify extraction and MySQL question persistence. Check remote
deletion after success and failure, re-upload with a new name, duplicate
filenames, rejected legacy payloads, and file expiration after a worker crash or
unqueued upload. Confirm `expires_after` is set on creation and monitor queue
age against the TTL. Automated storage tests mock OpenAI; they do not verify
real provider expiration or deletion:

```bash
python -m unittest discover -s tests -p 'test_document*.py' -v
python -m unittest discover -s tests -p 'test_quiz_worker.py' -v
```

## Quiz worker database configuration

The web app and quiz worker use the shared pool in `db_pool.py`.
Set `MYSQL_HOST`, `MYSQL_USER`, `MYSQL_PASSWORD`, and `MYSQL_DB` on both Render
services. The worker must use the database hostname, not `localhost`.
Set `MYSQL_PORT` to the database port (defaults to `3306`).
TLS is enabled by default; `MYSQL_SSL_CA` optionally specifies a CA certificate
file. Use `MYSQL_SSL_DISABLED=true` only for local/non-TLS databases.
For local runs, the pool loads `.env` from the repository root.

Run the focused worker tests from the repository root:

```bash
python -m unittest discover -s tests -p 'test_quiz_worker.py' -v
```

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
