import os
import pymysql
import pymysql.cursors
from dotenv import load_dotenv

# Load your environment variables exactly as you do in your web app
load_dotenv('/home/ps51632/mysite/explorecme/.env')

def export_question_data():
    try:
        # Connect to the database
        connection = pymysql.connect(
            host=os.getenv('MYSQL_HOST'),
            user=os.getenv('MYSQL_USER'),
            password=os.getenv('MYSQL_PASSWORD'),
            database=os.getenv('MYSQL_DB'),
            cursorclass=pymysql.cursors.DictCursor
        )
        
        with connection.cursor() as cursor:
            # Fetch all questions
            # Note: If your actual column name is 'topic' instead of 'topic_area', change it below
            sql = "SELECT id, organ_system, task_area, topic_area FROM question_bank"
            cursor.execute(sql)
            questions = cursor.fetchall()

        # Define the output file path (saves in the exact same directory as this script)
        current_dir = os.path.dirname(os.path.abspath(__file__))
        output_file = os.path.join(current_dir, 'question_db_export.txt')
        
        # Write to the text file
        with open(output_file, 'w', encoding='utf-8') as f:
            f.write("ID | Organ System | Task Area | Topic Area\n")
            f.write("-" * 100 + "\n")
            
            for q in questions:
                q_id = str(q.get('id') or 'N/A')
                org = str(q.get('organ_system') or 'None').strip()
                task = str(q.get('task_area') or 'None').strip()
                topic = str(q.get('topic_area') or 'None').strip()
                
                f.write(f"{q_id} | {org} | {task} | {topic}\n")
                
        print(f"✅ Successfully exported {len(questions)} questions!")
        print(f"📄 File saved at: {output_file}")

    except Exception as e:
        print(f"❌ An error occurred: {e}")
    finally:
        if 'connection' in locals() and connection.open:
            connection.close()

if __name__ == "__main__":
    export_question_data()