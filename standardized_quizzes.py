"""Standardized quiz catalog, published versions, and legacy sample questions."""

import json

STANDARDIZED_PREFIX = "Standardized: "
STANDARDIZED_QUESTION_PREFIX = "std-"

# Replace these flow-testing samples with reviewed questions from the source
# document. Add questions to the appropriate category and give each a unique,
# stable id starting with "std-" (at most 64 characters). Keep existing ids
# stable for attempt history; use new ids when replacing sample content.
STANDARDIZED_QUIZZES = {
    "Emergency Medicine": [
        {
            "id": "std-emergency-sample-1",
            "organ_system": "Cardiovascular System",
            "task_area": "Clinical Intervention",
            "topic_area": "Resuscitation",
            "question_stem": "Sample question: An unresponsive adult has no pulse and is not breathing normally. What should be started immediately?",
            "answer_choice_A": "High-quality CPR",
            "answer_choice_B": "Oral hydration",
            "answer_choice_C": "Routine outpatient follow-up",
            "answer_choice_D": "Observation alone",
            "answer_choice_E": "An oral antibiotic",
            "correct_answer": "A",
            "explanation": "High-quality CPR should begin immediately in an adult with cardiac arrest.",
        },
    ],
    "Family Medicine": [
        {
            "id": "std-family-sample-1",
            "organ_system": "Cardiovascular System",
            "task_area": "Health Maintenance, Patient Education, and Preventive Measures",
            "topic_area": "Blood pressure screening",
            "question_stem": "Sample question: Which measurement is routinely used to screen adults for hypertension?",
            "answer_choice_A": "Blood pressure",
            "answer_choice_B": "Visual acuity",
            "answer_choice_C": "Body temperature",
            "answer_choice_D": "Hearing threshold",
            "answer_choice_E": "Respiratory rate alone",
            "correct_answer": "A",
            "explanation": "Blood pressure measurement is used to screen for hypertension.",
        },
    ],
    "Internal Medicine": [
        {
            "id": "std-internal-sample-1",
            "organ_system": "Endocrine System",
            "task_area": "Using Diagnostic and Laboratory Studies",
            "topic_area": "Diabetes mellitus",
            "question_stem": "Sample question: Which test reflects average blood glucose over approximately the preceding three months?",
            "answer_choice_A": "Hemoglobin A1c",
            "answer_choice_B": "Serum sodium",
            "answer_choice_C": "Troponin",
            "answer_choice_D": "D-dimer",
            "answer_choice_E": "Serum lipase",
            "correct_answer": "A",
            "explanation": "Hemoglobin A1c reflects average glycemia over approximately two to three months.",
        },
    ],
    "Pediatrics": [
        {
            "id": "std-pediatrics-sample-1",
            "organ_system": "Infectious Diseases",
            "task_area": "Health Maintenance, Patient Education, and Preventive Measures",
            "topic_area": "Immunization",
            "question_stem": "Sample question: What is the primary purpose of routine childhood immunization?",
            "answer_choice_A": "Prevent vaccine-preventable infections",
            "answer_choice_B": "Treat every bacterial infection",
            "answer_choice_C": "Replace all well-child visits",
            "answer_choice_D": "Diagnose food allergies",
            "answer_choice_E": "Treat dehydration",
            "correct_answer": "A",
            "explanation": "Routine immunization protects children against vaccine-preventable infections.",
        },
    ],
    "Behavioral Health": [
        {
            "id": "std-behavioral-sample-1",
            "organ_system": "Psychiatry/Behavioral Science",
            "task_area": "History Taking and Performing Physical Examination",
            "topic_area": "Depression screening",
            "question_stem": "Sample question: Which symptom is a core feature assessed when screening for major depression?",
            "answer_choice_A": "Loss of interest or pleasure",
            "answer_choice_B": "Isolated ankle swelling",
            "answer_choice_C": "Ear discharge",
            "answer_choice_D": "A localized skin rash",
            "answer_choice_E": "Nasal congestion alone",
            "correct_answer": "A",
            "explanation": "Loss of interest or pleasure (anhedonia) is a core symptom of major depression.",
        },
    ],
    "Women's Health": [
        {
            "id": "std-womens-sample-1",
            "organ_system": "Reproductive System",
            "task_area": "Health Maintenance, Patient Education, and Preventive Measures",
            "topic_area": "Preconception counseling",
            "question_stem": "Sample question: Which supplement is routinely recommended before conception to reduce neural tube defect risk?",
            "answer_choice_A": "Folic acid",
            "answer_choice_B": "High-dose vitamin A",
            "answer_choice_C": "Colloidal silver",
            "answer_choice_D": "Activated charcoal",
            "answer_choice_E": "Melatonin",
            "correct_answer": "A",
            "explanation": "Folic acid supplementation before conception and during early pregnancy reduces neural tube defect risk.",
        },
    ],
}


def get_standardized_questions(quiz_name, cursor=None):
    if quiz_name and quiz_name.startswith(STANDARDIZED_PREFIX):
        name = quiz_name[len(STANDARDIZED_PREFIX):]
        if name in STANDARDIZED_QUIZZES:
            return STANDARDIZED_QUIZZES[name]
        if cursor is not None:
            cursor.execute(
                "SELECT question_ids FROM standardized_quiz_versions WHERE quiz_name = %s",
                (name,),
            )
            row = cursor.fetchone()
            if row:
                question_ids = json.loads(row["question_ids"])
                if not question_ids:
                    return []
                placeholders = ','.join(['%s'] * len(question_ids))
                cursor.execute(f"SELECT * FROM question_bank WHERE id IN ({placeholders})", question_ids)
                questions = {question["id"]: question for question in cursor.fetchall()}
                if any(question_id not in questions for question_id in question_ids):
                    raise ValueError("Published standardized quiz is missing questions")
                return [questions[question_id] for question_id in question_ids]
    return None


def list_standardized_versions(cursor):
    cursor.execute(
        "SELECT quiz_name, category, question_ids FROM standardized_quiz_versions ORDER BY quiz_name"
    )
    return [
        {
            "name": row["quiz_name"], "category": row["category"],
            "quiz_name": STANDARDIZED_PREFIX + row["quiz_name"],
            "count": len(json.loads(row["question_ids"])),
        }
        for row in cursor.fetchall()
    ]


def store_standardized_questions(cursor, questions):
    """Persist once so the shared grader, statistics, and review can use them."""
    if not questions:
        return
    placeholders = ','.join(['%s'] * len(questions))
    cursor.execute(
        f"SELECT id FROM question_bank WHERE id IN ({placeholders})",
        [question["id"] for question in questions],
    )
    stored = {row["id"] for row in cursor.fetchall()}
    questions = [question for question in questions if question["id"] not in stored]
    if not questions:
        return
    cursor.executemany("""
        INSERT IGNORE INTO question_bank
        (id, organ_system, task_area, topic_area, question_stem,
         answer_choice_A, answer_choice_B, answer_choice_C, answer_choice_D,
         answer_choice_E, correct_answer, explanation, difficulty_rating, created_date)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
    """, [
        tuple(question[field] for field in (
            "id", "organ_system", "task_area", "topic_area", "question_stem",
            "answer_choice_A", "answer_choice_B", "answer_choice_C",
            "answer_choice_D", "answer_choice_E", "correct_answer", "explanation",
        )) + (question.get("difficulty_rating", 1),)
        for question in questions
    ])
