# ExploreCME Webapp

This repository contains the web application and supporting services for the ExploreCME platform.

## Public landing pages and product logos

The ExploreCME home page (`/`) and RNXcel/MDXcel coming-soon pages omit the
application navbar and Clerk browser scripts. `/paxcel` and application pages
retain their existing navigation and authentication. Marketing templates opt out
with `show_app_header = false`; the shared base defaults to showing the header.

Product images use these case-sensitive paths:

- `static/images/paxcel_logo.png` — home card and PAXcel landing page
- `static/images/rnxcel_logo.png` — home card and RNXcel coming-soon page
- `static/images/mdxcel_logo.png` — home card and MDXcel coming-soon page

The current product files are placeholders, not newly uploaded user logos.
Replace them with the real PNG logos at these exact paths, or supply the actual
upload paths and branch so the references can be updated. Uploading files only
to the `static/` root (or a directory named `ststic`) does not change references
to `static/images/`. The main ExploreCME brand uses `static/FullLogo.png`.

Run the focused regressions from the repository root:

```bash
python -m unittest discover -s tests -p 'test_landing_pages.py' -v
python -m unittest discover -s tests -p 'test_entitlements.py' -v
```

## RNXcel: content-ready NCLEX-RN practice

The homepage opens the public `/rnxcel` landing. `/coming-soon/rnxcel` redirects
there; the three existing `/rnxcel/practice/<item_type>` slugs remain compatible.
The profile is **NCLEX_RN_2026**, blueprint **2026-2029**, RN only.
Global maintenance mode still applies.

Signed-in nursing journeys remain under `/rnxcel`: `/dashboard`, `/practice`
(setup), `/cases`, `/attempts` (history/resume), attempt results/review and
`/progress`. Clerk identity is signature-verified on every protected request
using the existing verifier, independently of PAXcel session identity.
Nursing consent is stored separately and uses the shared ExploreCME terms.
Owner-bound nursing CSRF tokens protect mutations; answer/results/review
queries are owner-scoped. No nursing subscription or payment flow is implemented.
**PAXcel `has_paid` is not a nursing entitlement.** All verified users who accept
the terms can use nursing practice when reviewed content becomes available.

### Empty production content and taxonomy

**There are no nursing questions or cases in this repository or migration.**
Empty installed storage shows zero available items, disabled starts, no attempts
and no performance metrics. Forged starts with no eligible content are rejected.
RNXcel never falls back to PA questions, sample banks, generators, or AI calls.
Synthetic nonclinical fixtures exist only in tests; SQLite/injected fixture
storage is allowed only with Flask `TESTING=True`, never via a public route.

`rnxcel_catalog.py` is the editable editorial configuration: eight category IDs
and proposed bank allocation weights totaling 100, plus 64 stable draft
objective IDs (`MOC-01` through `PAD-08`). These weights are **not exact official
exam percentages or compulsory per-quiz quotas**. Objective clusters are draft
editorial groupings, **not official exhaustive NCSBN objectives**. Maternal-newborn,
pediatric and multisystem content uses secondary population cross-tags.
Each scored item has exactly one primary category and one primary objective.
Population, specialty, setting, organ/topic tags, nursing tasks, clinical-judgment
skills, response format and case delivery are separate dimensions; PA
standardizers are never used. Optional official activity references must be
reviewed mappings, not inferred/fabricated references.

Setup validates category/objective, experience, population, setting, clinical
judgment and 1–50 scored items. Only untimed practice with feedback after
completion is supported. Counts reflect published eligible nursing versions.
Cases are indivisible six-item units; every stage must satisfy selected filters.
Case-library links preserve the selected published case through setup and start.
If the exact requested item count cannot be filled, start is disabled/rejected.
Selection is deterministic, without category quotas or adaptive difficulty.

### Engine and private version contract

`rnxcel_engine.validate_version()` validates JSON-authored versions before use;
`rnxcel_storage.RNXcelStore` reads only published `NCLEX_RN_2026` records.
There is deliberately no public authoring/import/seed endpoint. Future reviewed
content must be published through a restricted editorial process, not this app.
The legacy frozen dataclasses remain available for compatibility; production
attempts use validated JSON versions, not the empty `RNXCEL_ITEMS` tuple.

For a standalone version the private object contains:

- `version_id`, `exam_profile`, `type` (`single-best-answer` or
  `select-all-that-apply`), `category_id`, `objective_id`, `stem`;
- `options`: variable-length list of `{id, text}` with unique stable IDs;
- `correct_option_ids`, `rationale`, explicit `scoring` (`single` or `plus-minus`);
- `tags`: separate string lists `population`, `setting`, `specialty`,
  `organ_topics`, `nursing_tasks`, `clinical_judgment`; optional reviewed
  `official_activity_refs` (requiring `tags.official_activity_reviewed: true`);
- optional private version-level `provenance` object for editorial source/review metadata.

A case version has `version_id`, `exam_profile`,
`type: unfolding-case-study`, `title` and exactly six ordered `stages`.
Each stage has a unique `id`, `exhibits` (`{timestamp, text}` records) and an
`item` using the scored-item contract above. The trajectory is predetermined,
not response-driven branching. The server releases only the current item and
cumulative exhibits; future items/exhibits, keys and rationales never reach the
player. Earlier answers cannot be changed. Keys/rationales are available only
after the whole attempt completes, including all case stages.

Scoring is deterministic: SBA earns 1 for the keyed ID, otherwise 0 (possible 1).
SATA earns `max(0, selected_correct - selected_incorrect)`, with possible points
equal to the number of keyed options. At least one selection is required.
Duplicate/unknown IDs and multiple selections for SBA are rejected. Reordering
options does not alter keys. This is a documented practice scoring rule,
**not complete official NCLEX scoring, CAT, psychometric equivalence or pass
prediction**. Reports aggregate actual completed nursing responses by category,
objective, response format and clinical judgment; cross-tags can contribute to
more than one clinical-judgment row.

### Migration, rollout and rollback (existing Render hosting)

Deployment remains `gunicorn -c gunicorn.conf.py display_database:app` as configured
in `render.yaml`. No hosting, DNS, billing, worker or deployment changes are
required by this PR. Do not deploy without operator approval.

1. Back up the database. Apply existing PA migrations in their documented order
   if not already installed; RNXcel has no dependency on their tables.
2. Apply `migrations/20261017_add_rnxcel.sql` once to the existing MySQL database
   **before enabling nursing practice**. It creates only `rnxcel_item_versions`,
   `rnxcel_attempts` and `rnxcel_consents`, with no seeded rows, PA updates,
   triggers on PA tables, or runtime DDL.
3. Use the existing `MYSQL_*` pool/TLS configuration and `CLERK_*` issuer,
   JWKS and publishable key settings. Confirm a strong `FLASK_SECRET_KEY` and
   HTTPS secure session cookies. The web runtime needs nursing SELECT/INSERT/
   UPDATE rights, not CREATE/DROP. Protect authored JSON/private snapshots and
   backups as answer-key-bearing data.
4. Smoke-test landing, verified sign-in, nursing consent, zero-count setup,
   history and progress. With no published content, starts must remain disabled.
   No AI jobs, payment calls or clinical fixtures should be introduced.
5. Later, an authorized editorial process must validate and review each version,
   store its source/review provenance, and explicitly publish it. New content
   revisions receive **new version IDs**; never overwrite published authored
   objects. Retire a version to prevent new starts. Starts recheck publication
   under a transaction; existing attempts retain their immutable private
   snapshots and can resume/review after retirement or removal of source rows.

An installed empty bank is a normal HTTP 200 state. Missing migrations,
unreadable/corrupt content or operational DB failures fail closed with HTTP 503,
not fabricated zero counts. Logs distinguish `missing_migration`,
`content_unavailable` and `database_error`; fix the schema/configuration rather
than attempting runtime table creation. Invalid published versions are excluded;
if none of the published versions are usable, storage reports unavailable.
Writes use transactions and row locks; retries of an identical saved answer are
idempotent, and conflicting retries cannot double-grade an item.

Rollback: revert the application release first. Leave nursing tables intact to
preserve history; PAXcel code never queries them. If explicitly decommissioning
RNXcel, export nursing data and then drop only the three `rnxcel_*` tables.
Never truncate, rename, or rewrite PA banks, attempts, counters or user records.
No migration down-step touches PAXcel data.

### Verification

From the repository root, with existing `requirements.txt` installed:

```bash
python -m unittest discover -s tests -p 'test_rnxcel*.py' -v
python -m unittest discover -s tests -p 'test_landing_pages.py' -v
python -m unittest discover -s tests -p 'test_entitlements.py' -v
python -m unittest discover -s tests -v
```

Tests exercise nonclinical isolated fixtures for both response formats and all
six case stages, snapshots/resume, invalid and repeated submissions, ownership,
key hiding, release/skip protection, filters, empty/unavailable storage,
authentication/consent/CSRF and PAXcel isolation. No clinical-content generation,
live payment, production DB mutation or deployment is part of verification.
An editorial publishing UI, timed/adaptive modes, additional NGN formats,
subscription enforcement and official exam calibration are not implemented.

## Standardized student quizzes

Discover Standardized Quizzes from the student dashboard card, which links to
`/select_standardized_quiz`; it is not listed in the global header/navigation.
Each category opens a list of published versions with setup and attempt-history
links. **Every signed-in account, including unpaid users and faculty, can take
these quizzes** without assignments or a subscription. Guests cannot access them.
Setup, four modes, question screens, scoring, results, resume, and review reuse
the existing quiz engine. Ordinary generated/assigned quizzes retain their existing
access rules.

### Generate and publish three versions

Before deploying the version-selection routes or running the generator, apply
`migrations/20261016_add_standardized_quiz_versions.sql` to the application's MySQL
database. Fresh databases created with `init.db.py` include the same table.
The generator checks the schema and transactional InnoDB storage before API calls.
If the migration is missing, category selection remains available with the legacy
sample fallback, and the server logs migration guidance. Generated versions require
applying this migration to the same database used by the deployed application.
Selection requests never create or alter tables; the migration does not generate
or populate quiz versions.

Run from the repository root:

```bash
python generate_standardized_quizzes.py
```

The script prompts for:

1. Quiz name (for example, `Emergency Medicine`).
2. Topics JSON filename, using the same canonical organ-system-to-topic-array
   structure as `topics_by_organ_system.json`.
3. A percentage for **every organ system in that file**, totaling exactly 100.
4. A percentage for **each of the seven canonical task areas**, totaling exactly 100.
5. Confirmation before incurring API costs and publishing shared quizzes.

Use zero to exclude an organ or task. Largest-remainder rounding converts each
distribution to exactly 120 questions per version, so rounding can differ slightly
from the entered percentages. Tasks are shuffled independently against organ
slots; a topic is chosen uniformly at random from that organ's topic list for each
slot. Each version therefore preserves the organ and task totals while varying
topic selection and question order.

Generation calls the existing `process_topic_batch` engine in batches of up to five
questions (within its response-token budget), including its answer,
confidence, and difficulty verification. Output is validated against each requested
organ/task/topic, and duplicate stems are rejected across all 360 questions.
Bounded refill rounds request any missing slots without relaxing their categories.
If any slot cannot be filled, **no versions are published**. On success, a single
transaction stores all questions and publishes:

- `Emergency Medicine version 1`
- `Emergency Medicine version 2`
- `Emergency Medicine version 3`

Matching category names are case-insensitive and normalized to the six dashboard
labels. Other names appear as additional categories. Published versions are
immediately visible to all signed-in users with no assignment or app restart.
Rerunning the same name is rejected before generation; versions are immutable to
preserve attempt history. Choose a new quiz name for an additional set.

Useful options:

```bash
python generate_standardized_quizzes.py --quiz-name "Emergency Medicine" --topics-file /absolute/path/topics.json --dry-run --seed 42
python generate_standardized_quizzes.py --env-file /absolute/path/quiz.env --model ACCOUNT_MODEL --verify-model ACCOUNT_MODEL --concurrency 5 --max-rounds 3
```

Dry runs still prompt for percentages but need no credentials, database, or API
access. `--seed` reproduces blueprint allocation, not model-generated questions.
Normal runs use `OPENAI_API_KEY` (or `MY_API_KEY`) and `MYSQL_HOST`, `MYSQL_USER`,
`MYSQL_PASSWORD`, `MYSQL_DB`, plus the shared pool's port/TLS environment settings.
Existing environment variables override the selected `.env` file. Model selection
uses `--model`, then `OPENAI_MODEL`, then the shared engine's default; use a model
available to your account. No additional dependencies are required.

Published membership is an ordered list of `std-` question IDs in
`standardized_quiz_versions`; content lives in `question_bank`. Neither user quiz
ownership nor `quiz_questions_map` is used. IDs and version names are immutable,
and ordinary blueprint generation excludes standardized question IDs.
`standardized_quizzes.py` retains the original sample sets for legacy attempts;
categories with no generated versions show a clearly labeled sample fallback or
an empty-state message. The `Standardized:` internal quiz-name prefix is reserved.

### Student reports

Student Dashboard → **Reports** offers **Overall Usage** and **Standardized
Assessment**, with an on-screen preview and PDF download. Reports always use the
signed-in student's identity; there is no student or class selector.

Overall Usage reuses the faculty report engine's response counts, overall
percentage, organ-system grouping, and standardized task-area metrics. As in
the faculty student usage report, this includes practice activity and recorded
responses from incomplete attempts; unanswered questions are not observations.

Standardized Assessment reports use published catalog versions in the selected
category, not quiz-name prefix matching or personal quiz maps. Each version has
its own screen section and starts on a new PDF page. Legacy sample practice is
not a published assessment version and is excluded from these comparisons.
Surgery is the seventh built-in category; it has no sample questions and remains
empty until reviewed versions are published.

There is no persisted completion flag. A completed attempt must contain a
response for every question in that version's immutable membership. Unanswered
questions therefore leave an attempt incomplete and exclude it from these
reports, rather than being counted as incorrect. Stored response correctness is
used, matching the shared grader; repeated submissions update the unique
attempt/question response rather than adding observations.

Most recent selects a completed attempt independently for each version, ordered
by attempt start time descending, then attempt ID descending for ties.
Aggregate includes all completed attempts
and shows their average percentage score. Organ Systems and Task Areas include
every dimension in the version, even when the student has no observations.
Platform comparisons include all completed attempts for that exact version
across all users, independently of the student's selected mode. Dimensional
percentages use summed correct responses divided by eligible responses, not
averages of user percentages; counts accompany the percentages. Missing data is
labeled explicitly, not presented as 0%. Platform data is anonymous aggregates
only, never other students' identities or individual response histories.

Deploy the existing
`migrations/20261016_add_standardized_quiz_versions.sql` before publishing or
reporting on versions, along with the application's existing normalized lookup
columns and unique attempt/question response constraint. No new migration is
required for reports. Without the catalog table, reports show an empty state
and the catalog helper logs the migration prerequisite; report and selection
GET requests never create or modify schema.

Run the generator and SQLite-backed flow regressions without MySQL/API access:

```bash
python -m unittest discover -s tests -p 'test_standardized_quizzes.py' -v
python -m unittest discover -s tests -p 'test_generate_standardized_quizzes.py' -v
```

## Blueprint quiz database-first selection

Blueprint forms, fallback generation, and the topic-filling pipeline share the
canonical task definitions in `standardizers.py`. Previously, raw-label matching
treated legacy names such as “Applying Basic Scientific Concepts” as different
from the stored “Applying Foundational Scientific Concepts”, incorrectly queuing
generation despite sufficient matching questions.

Database-first selection now normalizes both request and stored organ/task
aliases. It selects unique question IDs per quiz, reassigning overlapping wildcard
matches when needed to exhaust compatible rows before queuing only the deficit.
Questions may still be reused across different quizzes. Selected organs, tasks,
and topics are never relaxed; any/blank/absent topics allow all topics within the
selected organ/task. Wildcards remain independent of category defaults, and
unrecognized organ labels do not fall back to unrelated `Misc` rows.

Legacy task percentage field names remain accepted, with canonical fields taking
precedence when both are submitted. Faculty create-new mode bypasses the existing
pool. The UI forwards this preference and distinguishes database reuse from
queued generation. No bank questions are rewritten, and clinical verification
criteria are unchanged.

Run the mocked regressions (no database or live OpenAI calls):

```bash
python -m unittest discover -s tests -p 'test_blueprint_quiz.py' -v
python -m unittest discover -s tests -p 'test_standardizers.py' -v
python -m unittest discover -s tests -p 'test_fill_database_topics.py' -v
```

## Fill a quiz from selected topics (Bash)

`fill_database_topics.py` is a standalone CLI; it does not replace
`fill_database_pa.py`. Run with Python from the repository directory:

```bash
python fill_database_topics.py --help
python fill_database_topics.py --list-systems
python fill_database_topics.py --organ-system "Pulmonary System" --topic "Asthma" --dry-run
python fill_database_topics.py --organ-system "Pulmonary System"
python fill_database_topics.py --all
python fill_database_topics.py --topics-file /absolute/path/topics.json --organ-system "Pulmonary System"
python fill_database_topics.py --organ-system "Pulmonary System" --env-file /absolute/path/quiz.env --model ACCOUNT_MODEL
```

The default topics file is repository-root `topics_by_organ_system.json`.
Custom files must be nonempty JSON mappings using exact canonical organ labels
(see `--help`) and nonempty arrays of trimmed topic names. For example:

```json
{
  "Pulmonary System": ["Asthma", "Chronic obstructive pulmonary disease"]
}
```

Repeated topics within a system are deduplicated in file order. Repeat `--topic`
to select exact names within the chosen system(s); unknown names are errors.
Without `--organ-system` or `--all`, an interactive terminal displays a numbered
menu; noninteractive input fails instead of waiting for input.

Defaults are username `explorecme`, quiz name `organ_system_batch_quiz`, one
question per each of the seven task labels retained from `fill_database_pa.py`,
and concurrency five. Override with `--username`, `--quiz-name`,
`--questions-per-task`, and `--concurrency`. The sample two-topic file targets
14 questions, **not a guaranteed output**.

Help, listing, and dry runs need no credentials or optional runtime dependencies.
Regular runs reuse installed repository dependencies from `requirements.txt`.
Set `OPENAI_API_KEY` (or `MY_API_KEY`), `MYSQL_HOST`, `MYSQL_USER`,
`MYSQL_PASSWORD`, and `MYSQL_DB` in the environment or the selected env file
(default repository-root `.env`). Existing environment variables take precedence.
The shared DB helper retains `MYSQL_PORT`, `MYSQL_SSL_CA`, and
`MYSQL_SSL_DISABLED` settings; use TLS for managed databases. Never commit
credentials. The shared modules also load root `.env` as a fallback; use a complete
environment/selected file to avoid unintentionally mixing configurations.

`--model` overrides `OPENAI_MODEL`, otherwise the shared engine's `DEFAULT_MODEL`
is used. Its current repository value, `gpt-6.1-sol`, is configuration, **not a
claim of public provider availability**. Select a model available to your account
and compatible with the shared engine's JSON response/chat-completion parameters.
`--verify-model` optionally selects another account-accessible verification model.
The shared engine requires three agreeing verifier answers with confidence of at
least 8 on a 1-10 scale, then a 1-10 difficulty rating. Confidence and difficulty
values such as `0.9`, `"9/10"`, or `"90%"` are normalized to that scale.
Unusable verifier responses, such as API errors, empty or malformed JSON, or answers
and confidence values that can't be parsed, are retried up to twice per question.
A verifier disagreement or low confidence discards the question immediately.
Every debug line includes the question's `organ / topic / task #attempt.index`
label. Saved questions always keep the requested organ, task, and topic. The
generator's own labels are logged when they differ. Shortfall lines and a final
`Discard reasons:` line count why questions were dropped (for example,
`verifier_disagreed`, `verifier_low_confidence`, `verifier_malformed_json`,
`verifier_confidence_malformed`, `generator_api_error`, or `rejected: ...`).
Retries that recovered are shown separately under `diagnostics:`.
API errors during a batch no longer drop questions that were already verified.
Model verification does **not** assure clinical correctness; review
questions before educational or clinical use.

**Regular generation incurs API costs and modifies the configured database.**
Connectivity, required columns, transactional InnoDB tables, and quiz/mapping
unique keys are checked before paid generation. This is not a guarantee of
write permissions or continued connectivity. Accepted topic/task batches are
saved incrementally in short explicit transactions; a failed batch rolls back,
while previously committed batches remain saved. Questions use strict inserts.
An exact-stem lookup using the database's collation can reuse an existing question
with matching organ/task/topic metadata and always maps its actual ID. There is
no assumed unique stem index or cross-process stem deduplication guarantee.

Repeated runs generate additional questions, not resumable/idempotent generation.
There is no local checkpoint: uncommitted results can be lost on failure.
Inspect the summary and existing quiz before retrying; a retry can incur new
charges and add more questions. The summary distinguishes requested/generated,
rejected, newly inserted/reused, newly mapped, persisted, failed batches, and
generation shortfalls. Persisted counts include reused/already-mapped results;
newly mapped counts do not. Exit status is 0 only when the target is accepted and
persisted with no reported failures/rejections, 1 for runtime/partial failures,
2 for invalid arguments/selections, and 130 for an interrupted run.

Isolated tests mock generation, clients, and DB operations; no real OpenAI
generation or database writes are performed:

```bash
python -m unittest discover -s tests -p 'test_fill_database_topics.py' -v
```

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
