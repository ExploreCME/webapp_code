# shared_utils.py

SHARED_VALID_TASK_AREAS = [
    "Hx & PE",
    "Labs & Dx",
    "Most Likely Dx",
    "Health Maint & Prev",
    "Clinic Int.",
    "Pharm Therapeutics",
    "Basic Science"
]

SHARED_TASK_AREA_MAPPING = {
    "history taking and performing physical examination": "Hx & PE",
    "history taking & performing physical examination": "Hx & PE",
    "using laboratory and diagnostic studies": "Labs & Dx",
    "using diagnostic and laboratory studies": "Labs & Dx",
    "formulating most likely diagnosis": "Most Likely Dx",
    "formulating the most likely diagnosis": "Most Likely Dx",
    "health maintenance, patient education, and preventative measures": "Health Maint & Prev",
    "health maintenance, patient education, and preventive measures": "Health Maint & Prev",
    "health maintenance, patient education, & preventative measures": "Health Maint & Prev",
    "managing patients - health maintenance, patient education, and preventive measures": "Health Maint & Prev",
    "clinical intervention": "Clinic Int.",
    "clinical interventions": "Clinic Int.",
    "managing patients - clinical interventions": "Clinic Int.",
    "managing patients - clinical intervention": "Clinic Int.",
    "managing patients-clinical interventions": "Clinic Int.",
    "managing patients-clinical intervention": "Clinic Int.",
    "clin int": "Clinic Int.",
    "clin int.": "Clinic Int.",
    "pharmaceutical therapeutics": "Pharm Therapeutics",
    "managing patients - pharmaceutical therapeutics": "Pharm Therapeutics",
    "applying basic scientific concepts": "Basic Science",
    "applying foundational scientific concepts": "Basic Science"
}

def get_standardized_task_area(raw_task_area_from_db):
    if not raw_task_area_from_db:
        return "Unknown Task Area"
    
    clean_task_key = " ".join(raw_task_area_from_db.strip().lower().split())
    mapped_task = SHARED_TASK_AREA_MAPPING.get(clean_task_key, raw_task_area_from_db.strip().title())
    return mapped_task

def is_assignment_name_unique(conn, username, target_name, assignment_type='quiz'):
    """
    Checks if a quiz or remediation name already exists for a user.
    Returns True if the name is unique (safe to use), False if it already exists.
    """
    if not target_name:
        return False
        
    with conn.cursor() as cursor:
        if assignment_type == 'quiz':
            cursor.execute('''
                SELECT 1 FROM user_quizzes WHERE username = %s AND TRIM(LOWER(quiz_name)) = TRIM(LOWER(%s))
                UNION
                SELECT 1 FROM quiz_generation_tasks WHERE username = %s AND TRIM(LOWER(quiz_name)) = TRIM(LOWER(%s)) AND status IN ('pending', 'processing')
            ''', (username, target_name, username, target_name))
            
        elif assignment_type == 'remediation':
            cursor.execute('''
                SELECT 1 FROM remediation_assignments 
                WHERE TRIM(LOWER(username)) = TRIM(LOWER(%s)) AND TRIM(LOWER(remediation_name)) = TRIM(LOWER(%s))
            ''', (username, target_name))
            
        # If fetchone() returns None, the name is unique.
        return cursor.fetchone() is None
