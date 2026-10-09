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
    print("=" * 50)
    print("      FLASK USER ROLE & PROGRAM CODE MANAGER      ")
    print("=" * 50)

    # 1. Input User Email
    email_input = input("\nEnter User Email: ").strip()
    if not email_input:
        print("[Error] Email cannot be empty.")
        return

    # Connect to database to fetch the clerk_id first
    try:
        conn = get_mysql_connection()
        cursor = conn.cursor()

        # 2. Look up the clerk_id associated with the provided email
        # NOTE: MySQL uses %s for placeholders instead of ?
        cursor.execute("SELECT clerk_id, email FROM users WHERE email = %s", (email_input,))
        user = cursor.fetchone()

        if not user:
            print("\n" + "=" * 50)
            print(f" ERROR: User with email '{email_input}' not found in database!")
            print(" Users must log into the app at least once before they can be updated.")
            print("=" * 50 + "\n")
            return

        clerk_id = user[0]
        found_email = user[1]
        
        print(f"\n[User Found] Email: {found_email} | Clerk ID: {clerk_id}")

        # 3. Input Faculty Status
        print("\n[Faculty Switch]")
        faculty_input = input("Is this user Faculty? (y/n / true/false / 1/0): ").strip().lower()
        
        # Evaluates identically to flask_app.py's internal logic
        is_faculty_bool = faculty_input in ["y", "yes", "true", "1"]
        is_faculty_val = 1 if is_faculty_bool else 0

        # 4. Input School / Program Code
        print("\n[School Code]")
        school_code = input("Enter School / Program Code (e.g., HARVARD_PA): ").strip().upper()
        if not school_code:
            school_code = "NONE"

        # 5. Update the existing user safely using ONLY the clerk_id
        cursor.execute("""
            UPDATE users 
            SET is_faculty = %s, program_code = %s
            WHERE clerk_id = %s
        """, (is_faculty_val, school_code, clerk_id))
        conn.commit()
        
        print("\n" + "=" * 50)
        print(" SUCCESS: User record updated successfully!")
        print("=" * 50)
        print(f" User Email   : {found_email}")
        print(f" Clerk ID     : {clerk_id}")
        print(f" Is Faculty   : {is_faculty_bool} (DB Value: {is_faculty_val})")
        print(f" Program Code : {school_code}")
        print("=" * 50 + "\n")

    except Exception as e:
        print(f"\n[Database Error] Could not update user: {e}")
    finally:
        if "conn" in locals() and conn:
            conn.close()

if __name__ == "__main__":
    main()