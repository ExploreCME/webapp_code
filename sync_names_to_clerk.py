import sqlite3
import requests
import os
import time
from dotenv import load_dotenv

# Load environment variables
load_dotenv('/home/ps51632/mysite/explorecme/.env')

# Grab the secret key from your .env
CLERK_SECRET_KEY = os.getenv('CLERK_SECRET_KEY')

# Path to your SQLite database
DB_PATH = '/home/ps51632/mysite/explorecme/pance_db.sqlite' 

if not CLERK_SECRET_KEY:
    print("Error: CLERK_SECRET_KEY not found in .env file.")
    exit(1)

def pull_names_from_clerk():
    # Connect to the local SQLite database
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    try:
        # Fetch all users that have a clerk_id
        cursor.execute("SELECT clerk_id FROM users WHERE clerk_id IS NOT NULL")
        users = cursor.fetchall()
        
        print(f"Found {len(users)} users in the local database. Pulling names from Clerk...")

        headers = {
            "Authorization": f"Bearer {CLERK_SECRET_KEY}",
            "Content-Type": "application/json"
        }

        for user in users:
            clerk_id = user['clerk_id']
            
            # Prepare Clerk API Request to GET user data
            url = f"https://api.clerk.com/v1/users/{clerk_id}"
            
            # Make the GET request to fetch the user from Clerk
            response = requests.get(url, headers=headers)

            if response.status_code == 200:
                clerk_data = response.json()
                
                # Extract the names from the Clerk response
                fn = clerk_data.get('first_name') or ""
                ln = clerk_data.get('last_name') or ""
                
                # Update the local database with the names from Clerk
                cursor.execute("""
                    UPDATE users 
                    SET first_name = ?, last_name = ? 
                    WHERE clerk_id = ?
                """, (fn, ln, clerk_id))
                conn.commit()

                print(f"SUCCESS: Pulled Clerk user {clerk_id} -> First: '{fn}', Last: '{ln}'")
            else:
                print(f"FAILED: Could not fetch {clerk_id} from Clerk. Status: {response.status_code}")

            # Sleep for 250ms to respect Clerk API rate limits
            time.sleep(0.25)

        print("\nSync completed successfully.")

    except Exception as e:
        print(f"An error occurred: {str(e)}")
    finally:
        conn.close()

if __name__ == '__main__':
    pull_names_from_clerk()