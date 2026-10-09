import os
import pymysql
from dotenv import load_dotenv

# Hardcoded absolute path for PythonAnywhere
basedir = '/home/ps51632/mysite/explorecme'
load_dotenv(os.path.join(basedir, '.env'))

def init_mysql():
    print("Initializing MySQL Database (Full Migration)...")
    
    MYSQL_HOST = os.getenv('MYSQL_HOST')
    MYSQL_USER = os.getenv('MYSQL_USER')
    MYSQL_PASSWORD = os.getenv('MYSQL_PASSWORD')
    MYSQL_DB = os.getenv('MYSQL_DB')

    if not all([MYSQL_HOST, MYSQL_USER, MYSQL_PASSWORD, MYSQL_DB]):
        print("❌ ERROR: Missing MySQL credentials in .env file.")
        return

    conn = pymysql.connect(
        host=MYSQL_HOST,
        user=MYSQL_USER,
        password=MYSQL_PASSWORD,
        database=MYSQL_DB
    )
    cursor = conn.cursor()

    tables = [
        # ==========================================
        # 1. NEWLY MIGRATED USER & CLASS TABLES
        # ==========================================
        '''
        CREATE TABLE IF NOT EXISTS users (
            clerk_id VARCHAR(255) PRIMARY KEY,
            has_paid INT DEFAULT 0,
            stripe_customer_id VARCHAR(255),
            is_faculty VARCHAR(10) DEFAULT 'N',
            program_code VARCHAR(255),
            first_name VARCHAR(255),
            last_name VARCHAR(255),
            email VARCHAR(255),
            accepted_eula INT DEFAULT 0
        )
        ''',
        '''
        CREATE TABLE IF NOT EXISTS classes (
            id INT AUTO_INCREMENT PRIMARY KEY,
            class_name VARCHAR(255),
            program_code VARCHAR(255),
            created_by VARCHAR(255),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        ''',
        '''
        CREATE TABLE IF NOT EXISTS class_members (
            class_id INT,
            clerk_id VARCHAR(255),
            FOREIGN KEY(class_id) REFERENCES classes(id) ON DELETE CASCADE
        )
        ''',
        # ==========================================
        # 2. EXISTING QUIZ & ASSIGNMENT TABLES
        # ==========================================
        '''
        CREATE TABLE IF NOT EXISTS question_bank (
            id VARCHAR(64) PRIMARY KEY,
            organ_system VARCHAR(255),
            task_area VARCHAR(255),
            topic_area VARCHAR(255),
            question_stem TEXT,
            answer_choice_A TEXT,
            answer_choice_B TEXT,
            answer_choice_C TEXT,
            answer_choice_D TEXT,
            answer_choice_E TEXT,
            correct_answer VARCHAR(10),
            explanation TEXT,
            difficulty_rating INT,
            created_date DATETIME,
            global_thumbs_up INT DEFAULT 0,
            global_thumbs_down INT DEFAULT 0
        )
        ''',
        '''
        CREATE TABLE IF NOT EXISTS user_quizzes (
            quiz_id VARCHAR(64) PRIMARY KEY,
            username VARCHAR(255),
            quiz_name VARCHAR(255),
            created_date DATETIME,
            UNIQUE KEY unique_user_quiz (username, quiz_name)
        )
        ''',
        '''
        CREATE TABLE IF NOT EXISTS quiz_questions_map (
            map_id INT AUTO_INCREMENT PRIMARY KEY,
            quiz_id VARCHAR(64),
            question_id VARCHAR(64),
            FOREIGN KEY(quiz_id) REFERENCES user_quizzes(quiz_id),
            FOREIGN KEY(question_id) REFERENCES question_bank(id),
            UNIQUE KEY unique_quiz_question (quiz_id, question_id)
        )
        ''',
        '''
        CREATE TABLE IF NOT EXISTS quiz_attempts (
            attempt_id VARCHAR(64) PRIMARY KEY,
            username VARCHAR(255),
            quiz_name VARCHAR(255),
            mode VARCHAR(50),
            start_time DATETIME
        )
        ''',
        '''
        CREATE TABLE IF NOT EXISTS attempt_responses (
            id INT AUTO_INCREMENT PRIMARY KEY,
            attempt_id VARCHAR(64),
            question_id VARCHAR(64),
            user_choice VARCHAR(10),
            correct_answer VARCHAR(10),
            is_correct INT,
            submitted_time DATETIME,
            UNIQUE KEY unique_attempt_question (attempt_id, question_id)
        )
        ''',
        '''
        CREATE TABLE IF NOT EXISTS assigned_quizzes (
            id INT AUTO_INCREMENT PRIMARY KEY,
            quiz_name VARCHAR(255),
            program_code VARCHAR(255),
            class_id INT,
            clerk_id VARCHAR(255) DEFAULT NULL,
            assigned_by VARCHAR(255),
            required_mode VARCHAR(50) DEFAULT 'any',
            due_date DATETIME DEFAULT NULL,
            assigned_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        ''',
        '''
        CREATE TABLE IF NOT EXISTS assigned_remediations (
            id INT AUTO_INCREMENT PRIMARY KEY,
            remediation_name VARCHAR(255),
            program_code VARCHAR(255),
            class_id INT,
            clerk_id VARCHAR(255) DEFAULT NULL,
            assigned_by VARCHAR(255),
            required_mode VARCHAR(50) DEFAULT 'any',
            due_date DATETIME DEFAULT NULL,
            assigned_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        ''',
        '''
        CREATE TABLE IF NOT EXISTS flashcards (
            id INT AUTO_INCREMENT PRIMARY KEY,
            organ_system VARCHAR(255),
            topic VARCHAR(255),
            task_area VARCHAR(255),
            prompt TEXT,
            answer TEXT
        )
        ''',
        '''
        CREATE TABLE IF NOT EXISTS flashcard_progress (
            username VARCHAR(255),
            organ_system VARCHAR(255),
            topic VARCHAR(255),
            current_index INT,
            PRIMARY KEY (username, organ_system, topic)
        )
        ''',
        '''
        CREATE TABLE IF NOT EXISTS quiz_generation_tasks (
            id INT AUTO_INCREMENT PRIMARY KEY,
            username VARCHAR(255),
            quiz_name VARCHAR(255),
            task_type VARCHAR(50),
            payload LONGTEXT,
            status VARCHAR(50) DEFAULT 'pending',
            created_at DATETIME,
            updated_at DATETIME
        )
        ''',
        '''
        CREATE TABLE IF NOT EXISTS remediation_assignments (
            id VARCHAR(64) PRIMARY KEY,
            username VARCHAR(255),
            organ_system VARCHAR(255),
            task_area VARCHAR(255),
            topic_area VARCHAR(255),
            summary TEXT,
            diagram_code TEXT,
            created_date DATETIME
        )
        ''',
        '''
        CREATE TABLE IF NOT EXISTS remediation_questions (
            id VARCHAR(64) PRIMARY KEY,
            assignment_id VARCHAR(64),
            question_stem TEXT,
            answer_choice_A TEXT,
            answer_choice_B TEXT,
            answer_choice_C TEXT,
            answer_choice_D TEXT,
            answer_choice_E TEXT,
            correct_answer VARCHAR(10),
            explanation TEXT,
            difficulty_rating INT,
            FOREIGN KEY(assignment_id) REFERENCES remediation_assignments(id)
        )
        '''
    ]

    for sql in tables:
        cursor.execute(sql)

    # MySQL Migrations (Self-Healing existing databases)
    mysql_migrations = [
        "ALTER TABLE assigned_remediations ADD COLUMN required_mode VARCHAR(50) DEFAULT 'any'",
        "ALTER TABLE assigned_quizzes ADD COLUMN clerk_id VARCHAR(255) DEFAULT NULL",
        "ALTER TABLE assigned_remediations ADD COLUMN clerk_id VARCHAR(255) DEFAULT NULL"
    ]

    for migration in mysql_migrations:
        try:
            cursor.execute(migration)
        except pymysql.MySQLError:
            # Column already exists
            pass

    conn.commit()
    conn.close()
    print("✅ MySQL Initialization Complete.")

if __name__ == "__main__":
    print("========================================")
    print("   EXPLORECME DATABASE INITIALIZER")
    print("========================================\n")
    
    try:
        init_mysql()
        print("\n🎉 Database initialized successfully!")
    except Exception as e:
        print(f"\n❌ FATAL ERROR: {e}")