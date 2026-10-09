import sqlite3, os, sys

if len(sys.argv) < 2:
    print("Error: You must provide a clerk_id.")
    sys.exit(1)

target_id = sys.argv[1]
basedir = os.path.dirname(os.path.abspath(__file__))
conn = sqlite3.connect(os.path.join(basedir, 'pance_db.sqlite'))
cursor = conn.cursor()

try:
    # 1. Remove them from all class rosters
    cursor.execute("DELETE FROM class_members WHERE clerk_id = ?", (target_id,))
    classes_removed = cursor.rowcount
    
    # 2. Remove them from the main users table
    cursor.execute("DELETE FROM users WHERE clerk_id = ?", (target_id,))
    users_removed = cursor.rowcount
    
    conn.commit()
    print(f"SUCCESS: Deleted {users_removed} user account(s) and {classes_removed} class roster entries for '{target_id}'.")
except Exception as e:
    print(f"Error deleting user: {e}")
finally:
    conn.close()
