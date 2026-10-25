"""Shared category normalization for generation, extraction, and remediation."""

import re


ORGAN_SYSTEMS = (
    "Cardiovascular System", "Dermatologic System", "Endocrine System",
    "Eyes, Ears, Nose, and Throat", "Gastrointestinal System/Nutrition",
    "Genitourinary System", "Hematologic System", "Infectious Diseases",
    "Musculoskeletal System", "Neurologic System", "Psychiatry/Behavioral Science",
    "Pulmonary System", "Renal System", "Reproductive System",
    "Professional Practice", "Misc",
)

TASK_AREAS = (
    "History Taking and Performing Physical Examination",
    "Using Diagnostic and Laboratory Studies",
    "Formulating the Most Likely Diagnosis",
    "Health Maintenance, Patient Education, and Preventive Measures",
    "Clinical Intervention",
    "Pharmaceutical Therapeutics",
    "Applying Foundational Scientific Concepts",
)

_CANONICAL_ORGANS = {label.lower(): label for label in ORGAN_SYSTEMS}

SOURCE_ORGAN_ALIASES = {
    "Neurology/Developmental": "Neurologic System",
    "Anxiety disorders; Trauma- and stress-related disorders": "Psychiatry/Behavioral Science",
    "Depressive disorders; Bipolar and related disorders": "Psychiatry/Behavioral Science",
    "Substance-related disorders": "Psychiatry/Behavioral Science",
    "Schizophrenia spectrum and other psychotic disorders": "Psychiatry/Behavioral Science",
    "Disruptive, impulse-control, and conduct disorders; Neurodevelopmental disorders": "Psychiatry/Behavioral Science",
    "Feeding or eating disorders": "Psychiatry/Behavioral Science",
    "Personality disorders; Obsessive-compulsive and related disorders": "Psychiatry/Behavioral Science",
    "Somatic symptom and related disorders; Nonadherence to medical treatment": "Psychiatry/Behavioral Science",
    "Paraphilic disorders; Sexual dysfunctions": "Psychiatry/Behavioral Science",
    "OB - Prenatal care/normal pregnancy": "Reproductive System",
    "GYN – Menstruation": "Reproductive System",
    "OB - Pregnancy complications": "Reproductive System",
    "GYN – Infections": "Reproductive System",
    "GYN – Neoplasms": "Reproductive System",
    "GYN - Disorders of the breast": "Reproductive System",
    "OB - Labor and delivery complications": "Reproductive System",
    "OB - Postpartum care": "Reproductive System",
    "GYN – Other": "Reproductive System",
    "GYN - Structural abnormalities": "Reproductive System",
    "Pulmonary/Thoracic Surgery": "Pulmonary System",
    "Breast Surgery": "Reproductive System",
    "Trauma/Acute Care": "Misc",
    "Neurologic/Neurosurgery": "Neurologic System",
    "Pain Medicine/Anesthesia": "Misc",
}


def category_key(value):
    """Compare source categories without confusing them with disease/topic text."""
    return re.sub(r"\s*[-–]\s*", "-", " ".join(str(value or "").split())).casefold()


_SOURCE_ORGANS = {category_key(label): label for label in SOURCE_ORGAN_ALIASES}


def selected_category_key(value):
    """Normalize known source aliases only; leave other selected labels distinct."""
    key = category_key(value)
    return key if key in _SOURCE_ORGANS else str(value or "").strip().casefold()


def source_organ_system(value):
    """Retain explicitly supported document scope; otherwise use canonical behavior."""
    return _SOURCE_ORGANS.get(category_key(value)) or standardize_organ_system(value)


def storage_organ_system(value):
    """Map known source labels only, preserving unrelated selected JSON categories."""
    source = _SOURCE_ORGANS.get(category_key(value))
    return SOURCE_ORGAN_ALIASES[source] if source else value


DOCUMENT_CATEGORY_INSTRUCTION = (
    "Preserve these source content-category labels when present in the document, "
    "instead of replacing them with a broad canonical organ system:\n"
    + "\n".join(f"* {label}" for label in SOURCE_ORGAN_ALIASES)
    + "\nKeep each category's educational scope and every specific disease/keyword "
    "in topic_area distinct. Never use a category label as a substitute for its topics. "
    "For other categories, use the canonical organ systems listed below.\n"
)

_CANONICAL_TASKS = {label.lower(): label for label in TASK_AREAS}
TASK_ALIASES = {
    "Using Laboratory and Diagnostic Studies": TASK_AREAS[1],
    "Formulating Most Likely Diagnosis": TASK_AREAS[2],
    "Health Maintenance, Patient Education, and Preventative Measures": TASK_AREAS[3],
    "Applying Basic Scientific Concepts": TASK_AREAS[6],
    **{f"Managing Patients - {label}": label for label in TASK_AREAS[3:6]},
}
_CANONICAL_TASKS.update({alias.lower(): label for alias, label in TASK_ALIASES.items()})
_CANONICAL_TASKS.update({
    "professional practice": "Professional Practice",
})

# Stems match only at the start of a word; abbreviations match whole words.
_ORGAN_RULES = (
    ("Dermatologic System", r"derm\w*|skin"),
    ("Hematologic System", r"hemat\w*|heme|blood|myeloma|anemia|b12"),
    ("Neurologic System", r"neuro\w*|brain|parkinson\w*|seizure\w*|dementia"),
    ("Reproductive System", r"gynecolog\w*|repro\w*|obstetric\w*|ob/gyn|obgyn|ob/|women|female|pregnan\w*"),
    ("Eyes, Ears, Nose, and Throat", r"eenot|eent|ent|ears?|eyes?|throat|nose|oral cavity|glaucoma"),
    ("Gastrointestinal System/Nutrition", r"gi|gastro\w*|nutri\w*|stomach|bowel|esophag\w*|ulcerative"),
    ("Cardiovascular System", r"cardio\w*|heart|vascular|tamponade|tachycardia"),
    ("Psychiatry/Behavioral Science", r"psych\w*|behav\w*|mental"),
    ("Musculoskeletal System", r"rheumatolog\w*|musculo\w*|ortho\w*|bones?|joints?|lupus|stenosis"),
    ("Infectious Diseases", r"infec\w*|id|salmonella"),
    ("Endocrine System", r"endocrin\w*|endo|thyroid|diabetes"),
    ("Pulmonary System", r"pulm\w*|resp\w*|asthma|pneumonia|sarcoidosis"),
    ("Genitourinary System", r"genito\w*|uro\w*|gu"),
    ("Renal System", r"renal|kidney\w*"),
    ("Professional Practice", r"prof\w*|pract\w*"),
)

_TASK_RULES = (
    (TASK_AREAS[1], r"labs?|laborator\w*|diagnostic\w*|stud(?:y|ies)"),
    (TASK_AREAS[5], r"pharm\w*|medication\w*|drugs?|therap\w*|treat\w*|rx"),
    (TASK_AREAS[0], r"hx|history|physical|pe|exam(?:ination)?"),
    (TASK_AREAS[3], r"prevent\w*|educ\w*|maint\w*"),
    (TASK_AREAS[4], r"interven\w*|procedur\w*|surg\w*|clin int"),
    (TASK_AREAS[6], r"sci|science|scientific|concepts?|pathophysiolog\w*|foundation(?:al)?|basic"),
    (TASK_AREAS[2], r"formulat\w*|diagnos(?:is|es)|most likely|dx"),
    ("Professional Practice", r"prof\w*|pract\w*"),
)


def _match_rules(value, rules):
    for label, pattern in rules:
        if re.search(r"\b(?:" + pattern + r")\b", value):
            return label
    return None


def standardize_organ_system(raw_organ, *, default="Misc"):
    """Return a canonical organ label, or the given default for unrecognized input."""
    value = str(raw_organ).strip() if raw_organ else ""
    source = _SOURCE_ORGANS.get(category_key(value))
    if source:
        return SOURCE_ORGAN_ALIASES[source]
    value = value.split(":", 1)[0].strip().lower()
    if value in _CANONICAL_ORGANS:
        return _CANONICAL_ORGANS[value]
    return _match_rules(value, _ORGAN_RULES) or default


def standardize_task_area(raw_task):
    """Normalize tasks; retain unknown text in title case, default empty to diagnosis."""
    raw = str(raw_task).strip() if raw_task else ""
    value = raw.lower()
    if value in _CANONICAL_TASKS:
        return _CANONICAL_TASKS[value]
    return (_match_rules(value, _TASK_RULES) or raw.title()
            or "Formulating the Most Likely Diagnosis")
