import os
import pymysql
import pymysql.cursors
from dotenv import load_dotenv

# Load environment variables
load_dotenv('/home/ps51632/mysite/explorecme/.env')

# Database connection settings
MYSQL_HOST = os.getenv('MYSQL_HOST')
MYSQL_USER = os.getenv('MYSQL_USER')
MYSQL_PASSWORD = os.getenv('MYSQL_PASSWORD')
MYSQL_DB = os.getenv('MYSQL_DB')

def map_organ_to_canonical(raw_organ):
    if not raw_organ: return "Unknown"
    val = str(raw_organ).lower().strip()
    if "cardio" in val: return "Cardiovascular System"
    if "derm" in val: return "Dermatologic System"
    if "endo" in val: return "Endocrine System"
    if "eye" in val or "ear" in val or "nose" in val or "throat" in val or "ent" in val: return "Eyes, Ears, Nose, and Throat"
    if "gastro" in val or "gi" in val or "nutri" in val: return "Gastrointestinal System/Nutrition"
    if "renal" in val: return "Renal System"
    if "genito" in val or "uro" in val or val == "gu": return "Genitourinary System"
    if "hema" in val or "blood" in val: return "Hematologic System"
    if "infect" in val or "id" == val: return "Infectious Diseases"
    if "musculo" in val or "ortho" in val: return "Musculoskeletal System"
    if "neuro" in val: return "Neurologic System"
    if "psych" in val or "behav" in val: return "Psychiatry/Behavioral Science"
    if "pulm" in val or "resp" in val: return "Pulmonary System"
    if "repro" in val or "obgyn" in val or "ob/" in val: return "Reproductive System"
    if "prof" in val or "pract" in val: return "Professional Practice"
    
    # Fallback if it's completely unrecognized, just clean it up a bit
    return str(raw_organ).strip().title() 

def map_task_to_canonical(raw_task):
    if not raw_task: return "Unknown"
    val = str(raw_task).lower().strip()
    if "history" in val or "physical" in val or "hx" in val or "pe" in val: return "History Taking and Performing Physical Examination"
    if "lab" in val or "diagnos" in val or "dx" in val: return "Using Laboratory and Diagnostic Studies"
    if "most likely" in val or "formulat" in val: return "Formulating Most Likely Diagnosis"
    if "maint" in val or "prevent" in val or "educ" in val: return "Managing Patients - Health Maintenance, Patient Education, and Preventive Measures"
    if "interven" in val or "clin int" in val: return "Managing Patients - Clinical Intervention"
    if "pharm" in val or "therap" in val: return "Managing Patients - Pharmaceutical Therapeutics"
    if "sci" in val or "found" in val or "basic" in val: return "Applying Foundational Scientific Concepts"
    if "prof" in val or "pract" in val: return "Professional Practice"
    
    # Fallback if it's completely unrecognized
    return str(raw_task).strip().title()

def fix_remediations():
    print("Connecting to database...")
    conn = pymysql.connect(
        host=MYSQL_HOST,
        user=MYSQL_USER,
        password=MYSQL_PASSWORD,
        database=MYSQL_DB,
        cursorclass=pymysql.cursors.DictCursor
    )
    
    try:
        with conn.cursor() as cursor:
            print("Fetching existing remediation assignments...")
            cursor.execute("SELECT id, organ_system, task_area, topic_area FROM remediation_assignments")
            records = cursor.fetchall()
            
            updates = []
            for record in records:
                original_organ = record['organ_system']
                original_task = record['task_area']
                
                clean_organ = map_organ_to_canonical(original_organ)
                clean_task = map_task_to_canonical(original_task)
                
                # If either the organ or task doesn't perfectly match the canonical version, add it to updates
                if original_organ != clean_organ or original_task != clean_task:
                    updates.append((clean_organ, clean_task, record['id']))
            
            if not updates:
                print("All records are already standardized! No changes needed.")
                return
                
            print(f"Found {len(updates)} records that need fixing. Updating now...")
            
            # Update the records in batch
            update_query = """
                UPDATE remediation_assignments 
                SET organ_system = %s, task_area = %s 
                WHERE id = %s
            """
            cursor.executemany(update_query, updates)
            conn.commit()
            
            print(f"Successfully fixed {len(updates)} remediation assignments!")
            
    except Exception as e:
        print(f"An error occurred: {e}")
        conn.rollback()
    finally:
        conn.close()

if __name__ == "__main__":
    fix_remediations()