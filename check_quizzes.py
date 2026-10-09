import os
import pymysql
import pymysql.cursors

# Configure your database connection details directly or via environment variables
#DB_HOST = os.getenv('MYSQL_HOST', 'your_mysql_host')
#DB_USER = os.getenv('MYSQL_USER', 'your_mysql_user')
#DB_PASSWORD = os.getenv('MYSQL_PASSWORD', 'your_mysql_password')
#DB_NAME = os.getenv('MYSQL_DB', 'your_mysql_db')
# Configure your database connection details directly
DB_HOST = 'ps51632.mysql.pythonanywhere-services.com'
DB_USER = 'ps51632'
DB_PASSWORD = 'Hayden3@3851'  # Replace with the password you set in the "Databases" tab
DB_NAME = 'ps51632$pa_questions' # Replace with your actual database name (e.g., ps51632$default)

def list_quizzes_for_user(user_identifier):
    """
    Connects to the MySQL database and lists all user-created quizzes
    as well as associated counts for a given username or email address.
    """
    connection = pymysql.connect(
        host=DB_HOST,
        user=DB_USER,
        password=DB_PASSWORD,
        database=DB_NAME,
        cursorclass=pymysql.cursors.DictCursor
    )

    try:
        with connection.cursor() as cursor:
            # Query matching the user across user_quizzes and mapping to question counts
            query = '''
                SELECT uq.quiz_id, uq.quiz_name, uq.created_date, COUNT(qqm.map_id) AS question_count
                FROM user_quizzes uq
                LEFT JOIN quiz_questions_map qqm ON uq.quiz_id = qqm.quiz_id
                WHERE TRIM(LOWER(uq.username)) = TRIM(LOWER(%s))
                GROUP BY uq.quiz_id, uq.quiz_name, uq.created_date
                ORDER BY uq.created_date DESC
            '''
            cursor.execute(query, (user_identifier,))
            quizzes = cursor.fetchall()

            if not quizzes:
                print(f"No quizzes found for user: {user_identifier}")
                return

            print(f"\n--- Quizzes for User: {user_identifier} ---")
            for idx, quiz in enumerate(quizzes, 1):
                print(f"{idx}. Quiz Name: {quiz['quiz_name']}")
                print(f"   - Quiz ID: {quiz['quiz_id']}")
                print(f"   - Questions: {quiz['question_count']}")
                print(f"   - Created: {quiz['created_date']}")
                print("-" * 40)

    finally:
        connection.close()

if __name__ == '__main__':
    target_user = input("Enter username or email address to query: ").strip()
    list_quizzes_for_user(target_user)