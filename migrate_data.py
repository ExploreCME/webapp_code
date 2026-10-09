# migrate_data.py
# migrate_data.py
import sqlite3
import pymysql
import os
from dotenv import load_dotenv

# 1. Load Environment Variables
basedir = '/home/ps51632/mysite/explorecme'
load_dotenv(os.path.join(basedir, '.env'))

# 2. Database Connection Details
SQLITE_DB_PATH = os.path.join(basedir, 'pance_db.sqlite')
MYSQL_HOST = os.getenv('MYSQL_HOST')
MYSQL_USER = os.getenv('MYSQL_USER')
MYSQL_PASSWORD = os.getenv('MYSQL_PASSWORD')
MYSQL_DB = os.getenv('MYSQL_DB')

def run_migration():
    print("🚀 Starting Data Migration: SQLite -> MySQL...")

    # Connect to SQLite
    sqlite_conn = sqlite3.connect(SQLITE_DB_PATH)
    sqlite_conn.row_factory = sqlite3.Row
    sqlite_cursor = sqlite_conn.cursor()

    # Connect to MySQL
    mysql_conn = pymysql.connect(
        host=MYSQL_HOST,
        user=MYSQL_USER,
        password=MYSQL_PASSWORD,
        database=MYSQL_DB,
        cursorclass=pymysql.cursors.DictCursor
    )
    mysql_cursor = mysql_conn.cursor()

    try:
        # ==========================================
        # 1. MIGRATE USERS
        # ==========================================
        print("Migrating 'users' table...")
        sqlite_cursor.execute("SELECT * FROM users")
        users = sqlite_cursor.fetchall()

        user_data = []
        for row in users:
            u = dict(row) # Convert sqlite3.Row to standard dictionary
            user_data.append((
                u.get('clerk_id'), u.get('has_paid', 0), u.get('stripe_customer_id'),
                u.get('is_faculty', 'N'), u.get('program_code'), u.get('first_name'),
                u.get('last_name'), u.get('email'), u.get('accepted_eula', 0)
            ))

        if user_data:
            mysql_cursor.executemany("""
                INSERT IGNORE INTO users
                (clerk_id, has_paid, stripe_customer_id, is_faculty, program_code, first_name, last_name, email, accepted_eula)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, user_data)
            mysql_conn.commit()
            print(f"✅ Migrated {len(user_data)} users.")
        else:
            print("No users found to migrate.")

        # ==========================================
        # 2. MIGRATE CLASSES
        # ==========================================
        print("Migrating 'classes' table...")
        sqlite_cursor.execute("SELECT * FROM classes")
        classes = sqlite_cursor.fetchall()

        class_data = []
        for row in classes:
            c = dict(row)
            class_data.append((
                c.get('id'), c.get('class_name'), c.get('program_code'),
                c.get('created_by'), c.get('created_at')
            ))

        if class_data:
            # We explicitly insert the ID so relationships remain intact
            mysql_cursor.executemany("""
                INSERT IGNORE INTO classes
                (id, class_name, program_code, created_by, created_at)
                VALUES (%s, %s, %s, %s, %s)
            """, class_data)
            mysql_conn.commit()
            print(f"✅ Migrated {len(class_data)} classes.")
        else:
            print("No classes found to migrate.")

        # ==========================================
        # 3. MIGRATE CLASS MEMBERS
        # ==========================================
        print("Migrating 'class_members' table...")
        sqlite_cursor.execute("SELECT * FROM class_members")
        members = sqlite_cursor.fetchall()

        member_data = []
        for row in members:
            m = dict(row)
            member_data.append((m.get('class_id'), m.get('clerk_id')))

        if member_data:
            mysql_cursor.executemany("""
                INSERT IGNORE INTO class_members (class_id, clerk_id)
                VALUES (%s, %s)
            """, member_data)
            mysql_conn.commit()
            print(f"✅ Migrated {len(member_data)} class members.")
        else:
            print("No class members found to migrate.")

        print("🎉 Migration completed successfully!")

    except Exception as e:
        print(f"❌ Error during migration: {e}")
        mysql_conn.rollback()
    finally:
        sqlite_conn.close()
        mysql_conn.close()

if __name__ == "__main__":
    run_migration()