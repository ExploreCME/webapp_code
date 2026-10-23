"""Rename every published version of one standardized assessment.

Run from the project directory (for example, the Render Shell):

    python rename_standardized_assessment.py --old-name "Emergecy Medicine" --new-name "Emergency Medicine" --dry-run
    python rename_standardized_assessment.py --old-name "Emergecy Medicine" --new-name "Emergency Medicine"

Each `<old> version <N>` row becomes `<new> version <N>` with category `<new>`,
and exact `Standardized: <old> version <N>` attempt and assignment names are
updated in the same transaction. Questions, responses, scores, and dates are
not changed. Back up the database first and run without active assessments.
"""
import argparse
import re
import sys

from db_pool import get_db_connection
from standardized_quizzes import STANDARDIZED_PREFIX, STANDARDIZED_QUIZZES


MAX_NAME_LENGTH = 255
VERSION_SEPARATOR = " version "
REFERENCE_TABLES = {"quiz_attempts": "attempt_id", "assigned_quizzes": "id"}
TABLES = ("standardized_quiz_versions", *REFERENCE_TABLES)
VERSION_SUFFIX = re.compile(r"\sversion\s*\d+\s*$", re.IGNORECASE)


def _norm(value):
    """Mirror the app's LOWER(TRIM(...)) quiz-name normalization."""
    return (value or "").strip().lower()


def version_name(base, number):
    return f"{base}{VERSION_SEPARATOR}{number}"


def parse_version(quiz_name, base):
    """Return N when quiz_name is exactly `<base> version <N>`, else None."""
    prefix = base + VERSION_SEPARATOR
    if not quiz_name.startswith(prefix):
        return None
    suffix = quiz_name[len(prefix):]
    if suffix.isascii() and suffix.isdigit() and not suffix.startswith("0"):
        return int(suffix)
    return None


def validate_base_name(value, label):
    name = (value or "").strip()
    if not name:
        raise ValueError(f"The {label} must not be empty.")
    if not name.isprintable():
        raise ValueError(f"The {label} must not contain control characters.")
    if name.casefold().startswith(STANDARDIZED_PREFIX.strip().casefold()):
        raise ValueError(f"Enter the {label} without the reserved 'Standardized:' prefix.")
    if VERSION_SUFFIX.search(name):
        raise ValueError(f"Enter the {label} without a 'version <N>' suffix; all versions are renamed.")
    return name


def validate_names(old_name, new_name):
    old_name = validate_base_name(old_name, "old name")
    new_name = validate_base_name(new_name, "new name")
    # Match the generator: built-in categories keep their catalog capitalization.
    new_name = next((category for category in STANDARDIZED_QUIZZES
                     if category.casefold() == new_name.casefold()), new_name)
    if old_name == new_name:
        raise ValueError("The old and new names are identical; nothing to rename.")
    if len(STANDARDIZED_PREFIX + version_name(new_name, 1)) > MAX_NAME_LENGTH:
        raise ValueError(
            f"The new name must fit within {MAX_NAME_LENGTH} characters including "
            "the 'Standardized: ' prefix and version suffix."
        )
    return old_name, new_name


def inspect_schema(cursor):
    """Require transactional tables; report how each quiz_name_norm column is maintained."""
    placeholders = ", ".join(["%s"] * len(TABLES))
    cursor.execute(
        "SELECT TABLE_NAME AS table_name, ENGINE AS engine FROM information_schema.TABLES "
        f"WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME IN ({placeholders})",
        TABLES,
    )
    engines = {row["table_name"]: (row["engine"] or "") for row in cursor.fetchall()}
    invalid = [table for table in TABLES if engines.get(table, "").upper() != "INNODB"]
    if invalid:
        raise ValueError(
            "Renaming requires existing transactional InnoDB tables; check "
            f"{', '.join(invalid)}. Nothing was renamed."
        )
    placeholders = ", ".join(["%s"] * len(REFERENCE_TABLES))
    cursor.execute(
        "SELECT TABLE_NAME AS table_name, EXTRA AS extra FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA = DATABASE() AND COLUMN_NAME = 'quiz_name_norm' "
        f"AND TABLE_NAME IN ({placeholders})",
        tuple(REFERENCE_TABLES),
    )
    # Generated columns follow quiz_name automatically; plain columns are written by the app.
    return {
        row["table_name"]: "generated" if "GENERATED" in (row["extra"] or "").upper() else "plain"
        for row in cursor.fetchall()
    }


def build_plan(cursor, old_name, new_name, norm_columns, lock=False):
    """Return the exact rename plan, None when nothing matches, or raise on any conflict."""
    lock_clause = " FOR UPDATE" if lock else ""
    cursor.execute(
        "SELECT quiz_name, category FROM standardized_quiz_versions ORDER BY quiz_name"
        + lock_clause
    )
    rows = cursor.fetchall()

    versions, problems = [], []
    for row in rows:
        quiz_name, category = row["quiz_name"], row["category"]
        number = parse_version(quiz_name, old_name)
        if number is not None and category == old_name:
            versions.append((number, quiz_name, version_name(new_name, number)))
        elif number is not None or category == old_name:
            problems.append(f"{quiz_name!r} has category {category!r}")
        elif (_norm(category) == _norm(old_name)
              or parse_version(_norm(quiz_name), _norm(old_name)) is not None):
            problems.append(
                f"{quiz_name!r} (category {category!r}) differs from {old_name!r} "
                "only by capitalization or spacing"
            )
    if problems:
        raise ValueError(
            "Inconsistent published metadata; nothing was renamed. Each version must be "
            f"named '<name> version <N>' with category exactly '<name>'. Review: {'; '.join(problems)}"
        )
    if not versions:
        return None
    versions.sort()

    source_names = {old for _, old, _ in versions}
    new_names = [new for _, _, new in versions]
    new_norms = {_norm(name) for name in new_names}
    reserved = {_norm(category) for category in STANDARDIZED_QUIZZES}
    conflicts = []
    for name in new_names:
        if len(STANDARDIZED_PREFIX + name) > MAX_NAME_LENGTH:
            conflicts.append(f"{name!r} exceeds {MAX_NAME_LENGTH} characters with its prefix")
        if _norm(name) in reserved:
            conflicts.append(f"{name!r} is a reserved built-in sample name")
    for row in rows:
        if row["quiz_name"] in source_names:
            continue
        if _norm(row["quiz_name"]) in new_norms or _norm(row["category"]) == _norm(new_name):
            conflicts.append(
                f"published version {row['quiz_name']!r} (category {row['category']!r}) already exists"
            )

    old_refs = [STANDARDIZED_PREFIX + old for _, old, _ in versions]
    new_refs = [STANDARDIZED_PREFIX + new for new in new_names]
    old_ref_norms = {_norm(name) for name in old_refs}
    new_ref_norms = {_norm(name) for name in new_refs}
    references = {}
    for table, key in REFERENCE_TABLES.items():
        names = old_refs + new_refs
        placeholders = ", ".join(["%s"] * len(names))
        where, params = f"quiz_name IN ({placeholders})", list(names)
        if table in norm_columns:
            where += f" OR quiz_name_norm IN ({placeholders})"
            params.extend(_norm(name) for name in names)
        cursor.execute(
            f"SELECT {key} AS row_key, quiz_name FROM {table} WHERE {where}{lock_clause}",
            tuple(params),
        )
        matched = {name: [] for name in old_refs}
        for row in cursor.fetchall():
            quiz_name = row["quiz_name"]
            if quiz_name in matched:
                matched[quiz_name].append(row["row_key"])
            elif _norm(quiz_name) in old_ref_norms:
                conflicts.append(
                    f"{table} {row['row_key']!r} uses case/spacing variant {quiz_name!r}"
                )
            elif _norm(quiz_name) in new_ref_norms:
                conflicts.append(f"{table} {row['row_key']!r} already uses {quiz_name!r}")
            else:
                conflicts.append(
                    f"{table} {row['row_key']!r} has quiz_name_norm out of sync with {quiz_name!r}"
                )
        references[table] = {
            name: tuple(sorted(keys, key=str)) for name, keys in matched.items()
        }

    if conflicts:
        raise ValueError(
            "Destination or reference conflict; nothing was renamed (no overwrite or merge). "
            f"Resolve these first: {'; '.join(conflicts)}"
        )
    return {"versions": versions, "references": references}


def print_preview(plan, old_name, new_name, norm_columns, print_func):
    attempts = plan["references"]["quiz_attempts"]
    assignments = plan["references"]["assigned_quizzes"]
    print_func("\nProposed rename:")
    print_func(f"  Category: {old_name!r} -> {new_name!r}")
    for _, old, new in plan["versions"]:
        ref = STANDARDIZED_PREFIX + old
        print_func(
            f"  {old!r} -> {new!r} "
            f"(quiz_attempts: {len(attempts[ref])}, assigned_quizzes: {len(assignments[ref])})"
        )
    print_func(f"  Versions: {len(plan['versions'])}")
    print_func(
        f"  Attempt/assignment names keep the {STANDARDIZED_PREFIX.strip()!r} prefix; "
        "attempt IDs stay the same."
    )
    for table in REFERENCE_TABLES:
        if norm_columns.get(table) == "plain":
            print_func(f"  {table}.quiz_name_norm is updated to match.")
    print_func(
        "  Not changed: questions, question IDs/order, responses, scores, dates, "
        "and other assessments."
    )


def apply_plan(cursor, plan, new_name, norm_columns):
    for _, old, new in plan["versions"]:
        cursor.execute(
            "UPDATE standardized_quiz_versions SET quiz_name = %s, category = %s "
            "WHERE quiz_name = %s AND BINARY quiz_name = %s",
            (new, new_name, old, old),
        )
        if cursor.rowcount != 1:
            raise RuntimeError(
                f"Expected to rename 1 version row for {old!r}, got {cursor.rowcount}; rolling back."
            )
        old_ref, new_ref = STANDARDIZED_PREFIX + old, STANDARDIZED_PREFIX + new
        for table in REFERENCE_TABLES:
            expected = len(plan["references"][table][old_ref])
            if not expected:
                continue
            assignments, params = "quiz_name = %s", [new_ref]
            if norm_columns.get(table) == "plain":
                assignments += ", quiz_name_norm = %s"
                params.append(_norm(new_ref))
            cursor.execute(
                f"UPDATE {table} SET {assignments} WHERE quiz_name = %s AND BINARY quiz_name = %s",
                (*params, old_ref, old_ref),
            )
            if cursor.rowcount != expected:
                raise RuntimeError(
                    f"Expected to update {expected} {table} rows for {old_ref!r}, "
                    f"got {cursor.rowcount}; rolling back."
                )


def _rollback(connection):
    try:
        connection.rollback()
    except Exception:
        pass


def rename_assessment(connection, old_name, new_name, dry_run=False,
                      input_func=input, print_func=print):
    """Preview, confirm, then rename atomically. Returns True on success or dry run."""
    try:
        # Preview from a consistent snapshot without holding locks during confirmation.
        connection.begin()
        with connection.cursor() as cursor:
            norm_columns = inspect_schema(cursor)
            plan = build_plan(cursor, old_name, new_name, norm_columns)
        connection.rollback()

        if plan is None:
            print_func(f"\nNo published standardized assessment found named exactly {old_name!r}.")
            print_func("Check spelling and capitalization. Nothing was renamed.")
            return False

        print_preview(plan, old_name, new_name, norm_columns, print_func)
        if dry_run:
            print_func("\nDry run: no changes were made.")
            return True

        print_func("\nBack up the database first and make sure no one is taking these assessments.")
        required = f"RENAME {old_name}"
        if input_func(f"\nType exactly '{required}' to confirm: ") != required:
            print_func("Cancelled. Nothing was renamed.")
            return False

        connection.begin()
        with connection.cursor() as cursor:
            locked_plan = build_plan(cursor, old_name, new_name, norm_columns, lock=True)
            if locked_plan != plan:
                raise RuntimeError(
                    "The assessment data changed after the preview; nothing was renamed. "
                    "Rerun the script when no assessments are in progress."
                )
            apply_plan(cursor, plan, new_name, norm_columns)
        connection.commit()
        print_func(
            f"\nRenamed {len(plan['versions'])} published version(s) from {old_name!r} to {new_name!r}."
        )
        return True
    except BaseException:
        _rollback(connection)
        raise


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Rename all published versions of a standardized assessment.",
    )
    parser.add_argument("--old-name", help="Current base name, e.g. 'Emergecy Medicine'")
    parser.add_argument("--new-name", help="Corrected base name, e.g. 'Emergency Medicine'")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview the changes without writing to the database")
    return parser.parse_args(argv)


def main(argv=None, input_func=input, print_func=print, connect=get_db_connection):
    args = parse_args(argv)
    old_name = args.old_name
    if old_name is None:
        old_name = input_func("Current assessment name (example: Emergecy Medicine): ")
    new_name = args.new_name
    if new_name is None:
        new_name = input_func("New assessment name (example: Emergency Medicine): ")
    old_name, new_name = validate_names(old_name, new_name)

    connection = connect()
    try:
        return rename_assessment(
            connection, old_name, new_name, dry_run=args.dry_run,
            input_func=input_func, print_func=print_func,
        )
    finally:
        connection.close()


def run(argv=None):
    try:
        renamed = main(argv)
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled. Nothing was renamed.")
        return 1
    except Exception as exc:
        print(f"\nFailed: {exc}", file=sys.stderr)
        return 1
    return 0 if renamed else 1


if __name__ == "__main__":
    sys.exit(run())
