#!/usr/bin/env python3
"""Fail CI when protected PAXcel files change without explicit approval.

Changed file paths are read from stdin (NUL- or newline-separated). Pull
request labels are read from the PR_LABELS environment variable (a JSON array
of label names) and from any --label arguments.

Exit codes: 0 = pass, 1 = protected PAXcel files changed without approval,
2 = invalid input.
"""

import argparse
import json
import os
import sys

APPROVAL_LABEL = "paxcel-change-approved"

# Centralized, reviewed list of protected PAXcel paths (exact repository paths).
# Keep RNXcel-only files (rnxcel_*.py, rnxcel*.html, tests/test_rnxcel*.py) out.
PROTECTED_PATHS = (
    # PAXcel landing/session surfaces and quiz flow endpoints
    "display_database.py",
    "flask_app.py",
    # Core PAXcel quiz templates
    "templates/template standardization/index.html",
    "templates/template standardization/quiz_mode.html",
    "templates/template standardization/quiz_results.html",
    "templates/template standardization/review_attempt.html",
    "templates/template standardization/student_dashboard.html",
    # PAXcel-specific regression tests
    "tests/test_entitlements.py",
    "tests/test_blueprint_quiz.py",
    "tests/test_standardized_quizzes.py",
)


def protected_changes(changed_files, protected_paths=PROTECTED_PATHS):
    protected = set(protected_paths)
    return sorted({path for path in changed_files if path in protected})


def parse_labels(raw):
    if not raw or not raw.strip():
        return []
    labels = json.loads(raw)
    if not isinstance(labels, list) or not all(isinstance(x, str) for x in labels):
        raise ValueError("PR_LABELS must be a JSON array of strings")
    return labels


def evaluate(changed_files, labels):
    """Return (exit_code, message) for the given changes and PR labels."""
    hits = protected_changes(changed_files)
    if not hits:
        return 0, "PAXcel guardrail: no protected PAXcel files changed. Pass."
    listing = "\n".join(f"  - {path}" for path in hits)
    if APPROVAL_LABEL in labels:
        return 0, (
            "PAXcel guardrail: protected PAXcel files changed and the PR has "
            f"label '{APPROVAL_LABEL}'. Pass (approved change).\n{listing}"
        )
    return 1, (
        "PAXcel guardrail: protected PAXcel files changed without approval.\n"
        f"{listing}\n"
        "If this PAXcel change is intentional, have a reviewer add the label "
        f"'{APPROVAL_LABEL}' to this pull request; the check re-runs "
        "automatically. Otherwise, revert the changes to these files."
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--label", action="append", default=[],
                        help="PR label (repeatable); added to PR_LABELS")
    args = parser.parse_args(argv)
    try:
        labels = parse_labels(os.environ.get("PR_LABELS", "")) + args.label
    except ValueError as exc:  # json.JSONDecodeError is a ValueError
        print(f"PAXcel guardrail: invalid PR_LABELS: {exc}", file=sys.stderr)
        return 2
    raw = sys.stdin.read()
    separator = "\0" if "\0" in raw else "\n"
    changed = [path.rstrip("\r") for path in raw.split(separator) if path.strip()]
    code, message = evaluate(changed, labels)
    if code:
        print(f"::error title=PAXcel guardrail::Protected PAXcel files changed; "
              f"add label '{APPROVAL_LABEL}' to approve.")
        print(message, file=sys.stderr)
    else:
        print(message)
    return code


if __name__ == "__main__":
    sys.exit(main())
