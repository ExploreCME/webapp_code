import importlib.util
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "paxcel_guardrail", ROOT / "scripts" / "paxcel_guardrail.py")
guardrail = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guardrail)

LABEL = "paxcel-change-approved"


def run(stdin, labels=None, argv=()):
    env = {} if labels is None else {"PR_LABELS": json.dumps(labels)}
    out, err = io.StringIO(), io.StringIO()
    with patch.dict("os.environ", env, clear=True), \
            patch("sys.stdin", io.StringIO(stdin)), \
            patch("sys.stdout", out), patch("sys.stderr", err):
        code = guardrail.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class PaxcelGuardrailTests(unittest.TestCase):
    def test_label_constant_is_exact(self):
        self.assertEqual(guardrail.APPROVAL_LABEL, LABEL)

    def test_required_paths_are_protected(self):
        required = {
            "display_database.py",
            "flask_app.py",
            "templates/template standardization/index.html",
            "templates/template standardization/quiz_mode.html",
            "templates/template standardization/quiz_results.html",
            "templates/template standardization/review_attempt.html",
            "templates/template standardization/student_dashboard.html",
        }
        self.assertTrue(required <= set(guardrail.PROTECTED_PATHS))

    def test_crlf_and_trailing_newline_in_nul_mode(self):
        code, _, _ = run("README.md\r\nflask_app.py\r\n", labels=[])
        self.assertEqual(code, 1)
        code, _, _ = run("README.md\0flask_app.py\n", labels=[])
        self.assertEqual(code, 1)

    def test_rnxcel_only_paths_are_not_protected(self):
        for path in guardrail.PROTECTED_PATHS:
            self.assertNotIn("rnxcel", path.lower(), path)

    def test_rnxcel_only_change_passes(self):
        code, out, _ = run(
            "rnxcel_routes.py\ntemplates/template standardization/rnxcel_player.html\n",
            labels=[])
        self.assertEqual(code, 0)
        self.assertIn("no protected PAXcel files changed", out)

    def test_protected_change_without_label_fails(self):
        code, out, err = run("flask_app.py\nREADME.md\n", labels=["bug"])
        self.assertEqual(code, 1)
        self.assertIn("::error", out)
        self.assertIn(LABEL, out)
        self.assertIn("  - flask_app.py", err)
        self.assertNotIn("README.md", err)
        self.assertIn(f"'{LABEL}'", err)

    def test_protected_change_without_any_labels_env_fails(self):
        code, _, _ = run("display_database.py\n")
        self.assertEqual(code, 1)

    def test_protected_change_with_label_passes(self):
        code, out, _ = run(
            "templates/template standardization/quiz_mode.html\n",
            labels=["enhancement", LABEL])
        self.assertEqual(code, 0)
        self.assertIn("Pass (approved change)", out)
        self.assertIn("quiz_mode.html", out)

    def test_label_argument_approves(self):
        code, _, _ = run("flask_app.py\n", argv=["--label", LABEL])
        self.assertEqual(code, 0)

    def test_similar_label_does_not_approve(self):
        code, _, _ = run("flask_app.py\n", labels=["paxcel-change-approved "])
        self.assertEqual(code, 1)

    def test_nul_separated_paths_with_spaces(self):
        code, _, err = run(
            "README.md\0templates/template standardization/index.html\0",
            labels=[])
        self.assertEqual(code, 1)
        self.assertIn("templates/template standardization/index.html", err)

    def test_prefix_or_similar_names_are_not_protected(self):
        code, _, _ = run(
            "flask_app.py10011137\nold/flask_app.py\n"
            "templates/template standardization/index.html.before.wide\n",
            labels=[])
        self.assertEqual(code, 0)

    def test_invalid_labels_json_is_an_error(self):
        out, err = io.StringIO(), io.StringIO()
        with patch.dict("os.environ", {"PR_LABELS": "{"}, clear=True), \
                patch("sys.stdin", io.StringIO("flask_app.py\n")), \
                patch("sys.stdout", out), patch("sys.stderr", err):
            self.assertEqual(guardrail.main([]), 2)
        self.assertIn("invalid PR_LABELS", err.getvalue())


if __name__ == "__main__":
    unittest.main()
