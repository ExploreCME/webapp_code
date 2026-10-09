import os
import pymysql
from dotenv import load_dotenv

# 1. Load environment variables from the exact path used in your Flask app
load_dotenv('/home/ps51632/mysite/explorecme/.env')

MYSQL_HOST = os.getenv('MYSQL_HOST')
MYSQL_USER = os.getenv('MYSQL_USER')
MYSQL_PASSWORD = os.getenv('MYSQL_PASSWORD')
MYSQL_DB = os.getenv('MYSQL_DB')

def get_mysql_connection():
    """Establishes a direct connection to the MySQL database."""
    return pymysql.connect(
        host=MYSQL_HOST,
        user=MYSQL_USER,
        password=MYSQL_PASSWORD,
        database=MYSQL_DB
    )

def main():
    print("\n" + "=" * 80)
    print(f"{'CLERK ID (Copy this)':<35} | {'EMAIL':<30} | {'FACULTY'}")
    print("=" * 80)

    try:
        conn = get_mysql_connection()
        cursor = conn.cursor()
        
        # Sort alphabetically by email so duplicates are stacked right next to each other!
        cursor.execute("SELECT clerk_id, email, is_faculty FROM users ORDER BY email ASC")
        users = cursor.fetchall()

        for user in users:
            clerk_id = user[0] if user[0] else "MISSING ID"
            email = user[1] if user[1] else "(Blank - Needs Webhook Sync)"
            is_fac = user[2]
            
            print(f"{clerk_id:<35} | {email:<30} | {is_fac}")
            
    except Exception as e:
        print(f"\n[Database Error]: {e}")
    finally:
        # Check if conn exists in locals to safely close the connection
        if "conn" in locals() and conn:
            conn.close()
            
    print("=" * 80 + "\n")

if __name__ == "__main__":
    main()