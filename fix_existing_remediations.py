import os
import pymysql
import pymysql.cursors
from dotenv import load_dotenv
from standardizers import standardize_organ_system, standardize_task_area

# Load environment variables
load_dotenv('/home/ps51632/mysite/explorecme/.env')

# Database connection settings
MYSQL_HOST = os.getenv('MYSQL_HOST')
MYSQL_USER = os.getenv('MYSQL_USER')
MYSQL_PASSWORD = os.getenv('MYSQL_PASSWORD')
MYSQL_DB = os.getenv('MYSQL_DB')

def map_organ_to_canonical(raw_organ):
    return standardize_organ_system(raw_organ)

def map_task_to_canonical(raw_task):
    return standardize_task_area(raw_task)

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