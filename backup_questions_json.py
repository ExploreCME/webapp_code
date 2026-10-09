import os
import json
import pymysql
from dotenv import load_dotenv

load_dotenv('/home/ps51632/mysite/explorecme/.env')

conn = pymysql.connect(
    host=os.getenv('MYSQL_HOST'),
    user=os.getenv('MYSQL_USER'),
    password=os.getenv('MYSQL_PASSWORD'),
    database=os.getenv('MYSQL_DB'),
    cursorclass=pymysql.cursors.DictCursor
)

try:
    with conn.cursor() as cursor:
        cursor.execute("SELECT * FROM question_bank")
        questions = cursor.fetchall()
        
        # Convert datetime objects to string format for JSON serialization
        for q in questions:
            if 'created_date' in q and q['created_date']:
                q['created_date'] = str(q['created_date'])
                
        backup_filename = "question_bank_backup.json"
        with open(backup_filename, 'w') as f:
            json.dump(questions, f, indent=4)
            
        print(f"Successfully backed up {len(questions)} questions to {backup_filename}")
finally:
    conn.close()