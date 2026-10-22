#!/usr/bin/env python3
"""Export a published standardized quiz version to a PDF or TXT answer key (read-only)."""

import argparse
import json
from pathlib import Path
import re
import sys

from xml.sax.saxutils import escape

from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer

from db_pool import get_db_connection
from standardized_quizzes import STANDARDIZED_PREFIX

CHOICES = "ABCDE"


def para_text(value):
    """Escape DB text for ReportLab markup and preserve line breaks."""
    return escape(str(value or "")).replace("\n", "<br/>")


def list_versions(cursor):
    cursor.execute(
        "SELECT quiz_name, category, JSON_LENGTH(question_ids) AS n "
        "FROM standardized_quiz_versions ORDER BY category, quiz_name"
    )
    rows = cursor.fetchall()
    if not rows:
        print("No published standardized quiz versions found.")
    for row in rows:
        print(f"  {row['quiz_name']}  [{row['category']}, {row['n']} questions]")


def load_version(cursor, quiz_name, version):
    full_name = f"{quiz_name} version {version}"
    cursor.execute(
        "SELECT quiz_name, category, question_ids, created_date "
        "FROM standardized_quiz_versions WHERE quiz_name = %s",
        (full_name,),
    )
    row = cursor.fetchone()
    if not row:
        raise ValueError(f"No published version named {full_name!r}. Use --list to see available versions.")
    question_ids = json.loads(row["question_ids"])
    if not question_ids:
        raise ValueError(f"{full_name!r} has no questions")
    placeholders = ",".join(["%s"] * len(question_ids))
    cursor.execute(f"SELECT * FROM question_bank WHERE id IN ({placeholders})", question_ids)
    by_id = {q["id"]: q for q in cursor.fetchall()}
    missing = [qid for qid in question_ids if qid not in by_id]
    if missing:
        raise ValueError(f"{len(missing)} question(s) referenced by {full_name!r} are missing from question_bank")
    return row, [by_id[qid] for qid in question_ids]


def build_pdf(path, meta, questions):
    styles = getSampleStyleSheet()
    title = ParagraphStyle("T", parent=styles["Title"], alignment=TA_CENTER)
    meta_style = ParagraphStyle("M", parent=styles["Normal"], alignment=TA_CENTER, textColor="#555555")
    head = ParagraphStyle("H", parent=styles["Heading4"], spaceBefore=10, spaceAfter=2)
    tags = ParagraphStyle("Tags", parent=styles["Normal"], fontSize=8, textColor="#666666", spaceAfter=4)
    body = ParagraphStyle("B", parent=styles["Normal"], fontSize=10, leading=13, spaceAfter=4)
    choice = ParagraphStyle("C", parent=body, leftIndent=14, spaceAfter=1)
    answer = ParagraphStyle("A", parent=body, textColor="#0b6e2e", spaceBefore=4)

    story = [
        Paragraph(para_text(meta["quiz_name"]), title),
        Paragraph(
            f"Category: {para_text(meta['category'])} &nbsp;|&nbsp; Questions: {len(questions)}"
            f" &nbsp;|&nbsp; Published: {para_text(meta['created_date'])}",
            meta_style,
        ),
        Spacer(1, 0.25 * inch),
    ]
    for number, q in enumerate(questions, 1):
        correct = str(q.get("correct_answer") or "").strip().upper()
        block = [
            Paragraph(f"Question {number}", head),
            Paragraph(
                " / ".join(para_text(q.get(k)) for k in ("organ_system", "task_area", "topic_area")),
                tags,
            ),
            Paragraph(para_text(q.get("question_stem")), body),
        ]
        for letter_ in CHOICES:
            text = para_text(q.get(f"answer_choice_{letter_}"))
            line = f"{letter_}. {text}"
            block.append(Paragraph(f"<b>{line}</b>" if letter_ == correct else line, choice))
        correct_text = para_text(q.get(f"answer_choice_{correct}")) if correct in CHOICES else ""
        block.append(Paragraph(f"<b>Correct answer: {correct}.</b> {correct_text}", answer))
        block.append(Paragraph(f"<b>Explanation:</b> {para_text(q.get('explanation'))}", body))
        story.append(KeepTogether(block))

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.drawRightString(letter[0] - 0.75 * inch, 0.5 * inch, f"Page {doc.page}")
        canvas.drawString(0.75 * inch, 0.5 * inch, "CONFIDENTIAL - answer key")
        canvas.restoreState()

    doc = SimpleDocTemplate(
        str(path), pagesize=letter, title=meta["quiz_name"],
        leftMargin=0.75 * inch, rightMargin=0.75 * inch,
        topMargin=0.75 * inch, bottomMargin=0.75 * inch,
    )
    doc.build(story, onFirstPage=footer, onLaterPages=footer)


def build_txt(path, meta, questions):
    with path.open("w", encoding="utf-8") as output:
        output.write(f"{meta['quiz_name']}\n")
        output.write(
            f"Category: {meta['category']}\nQuestions: {len(questions)}\n"
            f"Published: {meta['created_date']}\nCONFIDENTIAL - answer key\n\n"
        )
        for number, q in enumerate(questions, 1):
            output.write(f"{'=' * 70}\nQuestion {number}\n\n")
            output.write(" / ".join(str(q.get(k) or "") for k in (
                "organ_system", "task_area", "topic_area"
            )) + "\n\n")
            output.write(f"{q.get('question_stem') or ''}\n\n")
            for choice in CHOICES:
                output.write(f"{choice}. {q.get(f'answer_choice_{choice}') or ''}\n")
            correct = str(q.get("correct_answer") or "").strip().upper()
            correct_text = (q.get(f"answer_choice_{correct}") or "") if correct in CHOICES else ""
            output.write(f"\nCorrect answer: {correct}. {correct_text}\n")
            output.write(f"Explanation: {q.get('explanation') or ''}\n\n")


def main(argv=None):
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--quiz-name", help='e.g. "Family Medicine" (prompted when omitted)')
    cli.add_argument("--version", type=int, help="Version number 1-3 (prompted when omitted)")
    cli.add_argument("--format", choices=("pdf", "txt"), default="pdf", help="Export format (default: pdf)")
    cli.add_argument("--output", type=Path, help="Output path (default: ~/Downloads/<quiz>_version_<n>.<format>)")
    cli.add_argument("--list", action="store_true", help="List published versions and exit")
    args = cli.parse_args(argv)

    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            if args.list:
                list_versions(cursor)
                return 0
            name = (args.quiz_name or input("Standardized quiz name: ")).strip()
            if name.startswith(STANDARDIZED_PREFIX):
                name = name[len(STANDARDIZED_PREFIX):].strip()
            name = re.sub(r"\s+version\s+\d+\s*$", "", name, flags=re.IGNORECASE)
            if not name:
                raise ValueError("Quiz name is required")
            version = args.version
            if version is None:
                version = int(input("Version number: ").strip())
            meta, questions = load_version(cursor, name, version)
        slug = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_")
        output = args.output or (Path.home() / "Downloads" / f"{slug}_version_{version}.{args.format}")
        output.parent.mkdir(parents=True, exist_ok=True)
        writer = build_txt if args.format == "txt" else build_pdf
        writer(output, meta, questions)
        print(f"Wrote {len(questions)} questions to {output.resolve()}")
        return 0
    except (ValueError, OSError, EOFError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    except Exception as error:
        # Driver errors may include connection details; print only the type.
        print(f"Export failed ({type(error).__name__}). Check database configuration.", file=sys.stderr)
        return 1
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    sys.exit(main())
