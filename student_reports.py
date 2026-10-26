"""Student-owned usage reports and catalog-exact standardized comparisons."""

import io
import json
import os
from xml.sax.saxutils import escape

from flask import Blueprint, abort, render_template, request, send_file, session
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

import flask_app
from report_engine import ReportEngine
from shared_utils import SHARED_VALID_TASK_AREAS
from standardized_quizzes import STANDARDIZED_QUIZZES, list_standardized_versions
from standardized_display import standardized_label


student_reports_bp = Blueprint(
    "student_reports", __name__,
    template_folder=os.path.join(os.path.dirname(__file__), "templates", "template standardization"),
)

# Same display dimensions as faculty_routes.generate_report's Overall Usage.
ORGANS = [
    "Cardiovascular System", "Dermatologic System", "Endocrine System",
    "Eyes, Ears, Nose, and Throat", "Gastrointestinal System/Nutrition",
    "Genitourinary System", "Hematologic System", "Infectious Diseases",
    "Musculoskeletal System", "Neurologic System", "Psychiatry/Behavioral Science",
    "Pulmonary System", "Renal System", "Reproductive System", "Professional Practice",
    "Miscellaneous",
]


def metric(correct=0, eligible=0):
    return {
        "correct": int(correct), "eligible": int(eligible),
        "percentage": round(correct / eligible * 100, 1) if eligible else None,
    }


def percentage(value):
    return "N/A" if value is None else f"{value}%"


@student_reports_bp.before_request
def require_student():
    username = session.get("username")
    if not isinstance(username, str) or not username.strip() or username.lower().startswith("guest_"):
        abort(403)
    return flask_app.require_eula()


@student_reports_bp.teardown_request
def close_report_db(error):
    flask_app.close_db(error)


@student_reports_bp.after_request
def private_report(response):
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Pragma"] = "no-cache"
    return response


def overall_report(connection, username):
    engine = ReportEngine(connection)
    try:
        stats = engine.generate_overall_usage([username], write_debug=False)
    finally:
        engine.m_cursor.close()
    groups = []
    for title, labels, values in (
        ("Organ Systems", ORGANS, stats["organs"]),
        ("Task Areas", SHARED_VALID_TASK_AREAS, stats["tasks"]),
    ):
        groups.append({"title": title, "rows": [
            {"label": label, "student": metric(
                values.get(label, {}).get("cor", 0), values.get(label, {}).get("att", 0)
            )}
            for label in labels
        ]})
    return [{
        "title": "Overall Usage",
        "student": metric(stats["total_correct"], stats["total_attempted"]),
        "groups": groups,
    }]


def standardized_report(connection, username, category, attempt_filter):
    sections = []
    with connection.cursor() as cursor:
        versions = list_standardized_versions(cursor, category)
        if not versions:
            return sections
        version_names = tuple(version["name"] for version in versions)
        placeholders = ",".join(["%s"] * len(version_names))
        cursor.execute(
            f"SELECT quiz_name, question_ids FROM standardized_quiz_versions WHERE quiz_name IN ({placeholders})",
            version_names,
        )
        version_ids = {
            row["quiz_name"]: list(dict.fromkeys(json.loads(row["question_ids"])))
            for row in cursor.fetchall()
        }
        all_ids = tuple(dict.fromkeys(
            question_id for ids in version_ids.values() for question_id in ids
        ))
        questions = {}
        if all_ids:
            placeholders = ",".join(["%s"] * len(all_ids))
            cursor.execute(
                f"SELECT id, organ_system, source_organ_system, task_area FROM question_bank WHERE id IN ({placeholders})",
                all_ids,
            )
            questions = {row["id"]: row for row in cursor.fetchall()}
        for version in versions:
            ids = version_ids[version["name"]]
            if not ids:
                sections.append({
                    "title": version["name"], "question_count": 0, "attempts": [],
                    "available_completed_attempts": 0, "platform_attempt_count": 0,
                    "student": metric(), "platform": metric(),
                    "groups": [{"title": title, "rows": []}
                               for title in ("Organ Systems", "Task Areas")],
                })
                continue
            placeholders = ",".join(["%s"] * len(ids))
            if not set(ids).issubset(questions):
                raise ValueError("Published standardized quiz is missing questions")

            # Completion is inferred from recorded responses, as in the quiz flow,
            # but only exact catalog IDs may satisfy this version's denominator.
            completed = f"""
                SELECT qa.attempt_id, qa.start_time
                FROM quiz_attempts qa
                JOIN attempt_responses ar ON ar.attempt_id = qa.attempt_id
                WHERE qa.quiz_name_norm = LOWER(TRIM(%s))
                  AND ar.question_id IN ({placeholders})
                  {{owner}}
                GROUP BY qa.attempt_id, qa.start_time
                HAVING COUNT(DISTINCT ar.question_id) = %s
            """
            parameters = (version["quiz_name"], *ids)
            owner_completed = completed.format(owner="AND qa.username_norm = LOWER(TRIM(%s))")
            owner_parameters = (*parameters, username, len(ids))
            cursor.execute(
                f"SELECT COUNT(*) AS completed_count FROM ({owner_completed}) finished",
                owner_parameters,
            )
            available_completed_attempts = int(cursor.fetchone()["completed_count"])
            cursor.execute(
                owner_completed + " ORDER BY qa.start_time DESC, qa.attempt_id DESC"
                + (" LIMIT 1" if attempt_filter == "recent" else ""),
                owner_parameters,
            )
            attempts = cursor.fetchall()
            own_counts = {}
            if attempts:
                attempt_ids = tuple(row["attempt_id"] for row in attempts)
                selected = ",".join(["%s"] * len(attempt_ids))
                cursor.execute(f"""
                    SELECT question_id, COUNT(*) AS eligible,
                           SUM(CASE WHEN is_correct = 1 THEN 1 ELSE 0 END) AS correct
                    FROM attempt_responses
                    WHERE attempt_id IN ({selected}) AND question_id IN ({placeholders})
                    GROUP BY question_id
                """, (*attempt_ids, *ids))
                own_counts = {row["question_id"]: row for row in cursor.fetchall()}

            # Only anonymous counts leave SQL. Platform includes every completed
            # attempt for this version, independent of the student's filter.
            cursor.execute(f"""
                SELECT ar.question_id, COUNT(*) AS eligible,
                       COUNT(DISTINCT ar.attempt_id) AS attempt_count,
                       SUM(CASE WHEN ar.is_correct = 1 THEN 1 ELSE 0 END) AS correct
                FROM attempt_responses ar
                JOIN ({completed.format(owner="")}) finished
                  ON finished.attempt_id = ar.attempt_id
                WHERE ar.question_id IN ({placeholders})
                GROUP BY ar.question_id
            """, (*parameters, len(ids), *ids))
            platform_counts = {row["question_id"]: row for row in cursor.fetchall()}

            def summarize(question_ids, counts):
                return metric(
                    sum(counts.get(qid, {}).get("correct", 0) for qid in question_ids),
                    sum(counts.get(qid, {}).get("eligible", 0) for qid in question_ids),
                )

            groups = []
            for field, title in (
                ("organ_system", "Organ Systems"),
                ("task_area", "Task Areas"),
            ):
                labels = {}
                for qid in ids:
                    if field == "organ_system":
                        label = standardized_label(questions[qid])
                    else:
                        label = str(questions[qid][field] or "Unknown").strip() or "Unknown"
                    labels.setdefault(label, []).append(qid)
                groups.append({"title": title, "rows": [
                    {"label": label, "student": summarize(labels.get(label, []), own_counts),
                     "platform": summarize(labels.get(label, []), platform_counts)}
                    for label in sorted(labels)
                ]})
            sections.append({
                "title": version["name"], "question_count": len(ids),
                "available_completed_attempts": available_completed_attempts,
                "platform_attempt_count": max(
                    (row["attempt_count"] for row in platform_counts.values()), default=0
                ),
                "attempts": attempts, "student": summarize(ids, own_counts),
                "platform": summarize(ids, platform_counts), "groups": groups,
            })
    return sections


def report_pdf(report_type, sections, explanation, score_label="Your percentage correct"):
    """Use the faculty ReportLab document/table approach for both report types."""
    output = io.BytesIO()
    styles = getSampleStyleSheet()
    elements = []
    for index, section in enumerate(sections):
        if index:
            elements.append(PageBreak())
        elements.extend([
            Paragraph(escape(section["title"]), styles["Title"]),
            Paragraph(escape(explanation), styles["Normal"]), Spacer(1, 12),
            Paragraph(
                f"{score_label}: {percentage(section['student']['percentage'])} "
                f"({section['student']['correct']}/{section['student']['eligible']})",
                styles["Normal"],
            ),
        ])
        if "platform" in section:
            elements.append(Paragraph(
                f"Platform percentage correct: {percentage(section['platform']['percentage'])} "
                f"({section['platform']['correct']}/{section['platform']['eligible']}); "
                f"selected completed attempts: {len(section['attempts'])} / "
                f"{section['available_completed_attempts']} available; "
                f"platform completed attempts: {section['platform_attempt_count']}",
                styles["Normal"],
            ))
            if section["question_count"] == 0:
                elements.append(Paragraph("No questions in this published version.", styles["Normal"]))
        for group in section["groups"]:
            elements.extend([Spacer(1, 12), Paragraph(group["title"], styles["Heading3"])])
            comparison = "platform" in section
            headers = ["Dimension", "Your correct / eligible", "Your percentage correct"] + (
                ["Platform correct / eligible", "Platform percentage correct"] if comparison else []
            )
            rows = [[Paragraph(escape(cell), styles["Normal"]) for cell in headers]]
            for row in group["rows"]:
                own = row["student"]
                cells = [row["label"], f"{own['correct']} / {own['eligible']}",
                         percentage(own["percentage"])]
                if comparison:
                    platform = row["platform"]
                    cells += [f"{platform['correct']} / {platform['eligible']}",
                              percentage(platform["percentage"])]
                rows.append([Paragraph(escape(str(cell)), styles["Normal"]) for cell in cells])
            widths = [170, 95, 55, 100, 55] if comparison else [250, 130, 95]
            table = Table(rows, colWidths=widths, repeatRows=1)
            table.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e2e8f0")),
                ("GRID", (0, 0), (-1, -1), .5, colors.HexColor("#bdc3c7")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]))
            elements.append(table)
    if not sections:
        elements.append(Paragraph("No published versions available.", styles["Normal"]))
    SimpleDocTemplate(output, pagesize=letter).build(elements)
    output.seek(0)
    return send_file(output, mimetype="application/pdf", as_attachment=True,
                     download_name=f"student_{report_type}_report.pdf")


@student_reports_bp.route("/student_reports")
def reports():
    with flask_app.get_db().cursor() as cursor:
        versions = list_standardized_versions(cursor)
    categories = list(STANDARDIZED_QUIZZES)
    categories.extend(sorted({version["category"] for version in versions} - set(categories)))
    return render_template("student_reports.html", categories=categories)


@student_reports_bp.route("/student_reports/preview")
@student_reports_bp.route("/student_reports/download", defaults={"download": True})
def report(download=False):
    report_type = request.args.get("report_type", "usage")
    category = request.args.get("category", "")
    attempt_filter = request.args.get("attempt_filter", "recent")
    if report_type not in ("usage", "standardized") or attempt_filter not in ("recent", "all"):
        abort(400)
    if report_type == "standardized" and not category:
        abort(400)
    connection = flask_app.get_db()
    if category and category not in STANDARDIZED_QUIZZES:
        with connection.cursor() as cursor:
            if not list_standardized_versions(cursor, category):
                abort(400)
    username = session["username"]
    score_label = "Average score" if report_type == "standardized" and attempt_filter == "all" else "Your percentage correct"
    if report_type == "usage":
        sections = overall_report(connection, username)
        explanation = (
            "All modes and all recorded responses, including incomplete attempts. "
            "Same Overall Usage metrics as faculty reports. Scores are correct / recorded responses; "
            "unanswered questions are excluded. N/A means no observations."
        )
    else:
        sections = standardized_report(connection, username, category, attempt_filter)
        explanation = (
            f"{category}: {'Most recent attempt (completed only)' if attempt_filter == 'recent' else 'Aggregate of all completed attempts'} "
            "per published version, across all modes. Completion requires a recorded response to every catalog question. "
            "Scores are total correct / eligible catalog responses (weighted, not averages of percentages). "
            "For fixed version membership, the all-attempts ratio also equals the mean completed-attempt score. "
            "Incomplete attempts and non-catalog responses are excluded; unanswered questions cannot complete an attempt. "
            "Platform uses all completed attempts for the same version, including yours, independent of your filter. "
            "N/A means no observations. Samples are excluded."
        )
    if download:
        return report_pdf(report_type, sections, explanation, score_label)
    return render_template(
        "student_report.html", report_type=report_type, sections=sections,
        explanation=explanation, category=category, attempt_filter=attempt_filter,
        percentage=percentage, score_label=score_label,
    )
