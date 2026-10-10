# ExploreCME Webapp

This repository contains the web application and supporting services for the ExploreCME platform.

## Uploaded documents on Render

Uploads use a private Amazon S3 or S3-compatible bucket (including Cloudflare R2),
not GitHub or a Render service's local disk. Configure both web and worker:

| Variable | Value |
| --- | --- |
| `S3_BUCKET` | The same private bucket on both services |
| `S3_ENDPOINT_URL` | Empty for AWS S3; for R2, `https://<account-id>.r2.cloudflarestorage.com` |
| `AWS_DEFAULT_REGION` | Your AWS bucket region; `auto` for R2 |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | Separate least-privilege service credentials, configured as Render secrets |
| `MAX_DOCUMENT_BYTES` | Maximum PDF bytes; default `20971520` (20 MiB), identical on both services |
| `OPENAI_API_KEY` | Required on the worker for PDF processing |

For S3, enable Block Public Access, bucket-owner-enforced ownership, and default
server-side encryption. For R2, disable public bucket access and public custom
domains. The application never sets public ACLs or exposes object URLs.
Use HTTPS endpoints only. Restrict the web credential to `PutObject` under
`uploads/*`; the worker needs `GetObject` there and `PutObject` for generated
JSON. Neither service needs bucket listing, lifecycle administration, or deletion.
Scope R2 tokens to the selected bucket using the provider's available permissions.
Do not put credentials or user documents in GitHub.

Both upload forms accept only readable, nonempty, unencrypted PDFs. Files are
size-limited and parsed before storage. Objects receive unique UUID identifiers;
user identifiers are hashed in object keys. The existing
`quiz_generation_tasks.payload` JSON records the owner, original sanitized
filename, object key, size, and upload time. The row's `id` is the associated task
ID, so no schema migration is required. Only successfully stored objects are
queued. Quiz creation and task insertion commit together.

The worker conditionally claims pending tasks, downloads PDFs into an isolated
temporary directory, and removes temporary files even when processing fails.
It saves extracted topics and question JSON alongside the source object;
MySQL remains authoritative for quizzes and questions. The document list comes
from owner-filtered task metadata, not local directory scans. Failed documents
can be retried from that list while their source objects remain retained.

### Retention and privacy (required before enabling uploads)

Configure a bucket lifecycle rule to expire **all objects under `uploads/` after
7 days**, including generated JSON and unqueued uploads left by a database
failure. This is an operator-managed policy, not an application timer or an
HTTP `Expires` header. Select a different documented period if your institution
requires it, and keep the published policy consistent. If versioning is enabled,
also expire noncurrent versions and delete markers; configure incomplete
multipart upload cleanup where supported. Services deliberately cannot delete
originals, so failures do not destroy retry inputs. Metadata and database
questions are not removed by this bucket rule.

Lifecycle expiration is asynchronous: allow for provider deletion delays and
keep queue/retry deadlines shorter than the chosen retention period. After
expiration, users must upload again. Monitor backlog and failed tasks. After a
worker restart, inspect interrupted `processing` tasks and reset them to
`pending` only after confirming the original worker is no longer running and
the source is retained; pending tasks already survive service redeployments.

Uploaded PDFs are sent to OpenAI, and remote files are deleted in the existing
processing functions' cleanup paths. Local validation is not a malware scan or
de-identification tool. Upload notices prohibit unauthorized confidential or
patient-identifying data. Before rollout, update the published privacy policy
and obtain institution approval for storage region, retention, and OpenAI's
current processing/retention terms; do not assume remote deletion means zero
provider retention. Existing static privacy PDFs are not rewritten by this change.

### Migrating existing tasks

Old payloads containing only `filepath` are explicitly rejected with a re-upload
message; there is no fallback to a PythonAnywhere path. Have users re-upload
pending or failed legacy documents, resolving conflicting old quiz/assignment
names first. If preserving a legacy task, an administrator must securely move
its PDF into the private bucket and replace its payload with validated owner,
object-key and upload metadata while retaining task-specific generation fields.
Do not reset tasks to pending until the source object is available.

Before production rollout, exercise both upload forms with a safe PDF on the
web service, confirm processing on the separate worker, then test duplicate
filenames, failed-task retries, a worker redeployment, and lifecycle expiration.
Automated storage tests mock S3; they do not provision or verify a real bucket:

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
