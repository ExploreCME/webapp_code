"""Source categories for standardized presentation, not shared storage taxonomy."""


def standardized_label(question):
    source = question.get("source_organ_system")
    if source and source.strip():
        return source
    return question.get("organ_system") or "Unknown"


def standardized_category_breakdown(cursor, attempt_id):
    cursor.execute("""
        SELECT qb.organ_system, qb.source_organ_system, ar.is_correct
        FROM attempt_responses ar
        LEFT JOIN question_bank qb ON qb.id = ar.question_id
        WHERE ar.attempt_id = %s
    """, (attempt_id,))
    groups = {}
    for question in cursor.fetchall():
        label = standardized_label(question)
        row = groups.setdefault(label, {
            "label": label, "answered": 0, "correct": 0,
        })
        row["answered"] += 1
        row["correct"] += int(bool(question["is_correct"]))
    for row in groups.values():
        row["percentage"] = round(row["correct"] / row["answered"] * 100, 1)
    return [groups[label] for label in sorted(groups)]
