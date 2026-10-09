import os
import pymysql
from dotenv import load_dotenv

# Load your environment variables (using your specific PythonAnywhere path)
load_dotenv('/home/ps51632/mysite/explorecme/.env')

def list_remediations():
    try:
        # Connect to the database using your existing credentials
        connection = pymysql.connect(
            host=os.getenv('MYSQL_HOST'),
            user=os.getenv('MYSQL_USER'),
            password=os.getenv('MYSQL_PASSWORD'),
            database=os.getenv('MYSQL_DB'),
            cursorclass=pymysql.cursors.DictCursor
        )

        with connection.cursor() as cursor:
            # Fetch all distinct remediation names
            sql = "SELECT DISTINCT remediation_name, created_date FROM remediation_assignments ORDER BY created_date DESC"
            cursor.execute(sql)
            results = cursor.fetchall()

            print("\n" + "="*50)
            print(f" FOUND {len(results)} REMEDIATIONS IN DATABASE ")
            print("="*50)
            
            if not results:
                print("No remediations found in the 'remediation_assignments' table.")
            
            for index, row in enumerate(results, 1):
                # We put quotes around the name so you can spot any hidden trailing spaces!
                name = row.get('remediation_name', 'None')
                date = row.get('created_date', 'Unknown')
                print(f"{index}. Name: '{name}'  |  Created: {date}")
            
            print("="*50 + "\n")

    except Exception as e:
        print(f"An error occurred connecting to the database: {e}")
    finally:
        if 'connection' in locals() and connection.open:
            connection.close()

if __name__ == "__main__":
    list_remediations()