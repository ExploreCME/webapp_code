import os
import sqlite3
import requests
import time
from dotenv import load_dotenv

# --- CONFIGURATION ---
BASE_DIR = '/home/ps51632/mysite/explorecme'
DB_PATH = os.path.join(BASE_DIR, 'pance_db.sqlite')
ENV_PATH = os.path.join(BASE_DIR, '.env')

# Load the secret key securely from your .env file
load_dotenv(ENV_PATH)
CLERK_SECRET_KEY = os.getenv('CLERK_SECRET_KEY')

def main():
    print("=" * 60)
    print("      CLERK API TO SQLITE EMAIL SYNC SCRIPT      ")
    print("=" * 60)

    if not CLERK_SECRET_KEY:
        print("❌ ERROR: CLERK_SECRET_KEY not found in your .env file.")
        return

    headers = {
        "Authorization": f"Bearer {CLERK_SECRET_KEY}",
        "Content-Type": "application/json"
    }

    try:
        # Connect to the SQLite database
        conn = sqlite3.connect(DB_PATH, timeout=15.0)
        cursor = conn.cursor()
        
        # Fetch ALL users from the database
        cursor.execute("SELECT clerk_id, email FROM users")
        users = cursor.fetchall()
        
        print(f"Found {len(users)} total users in the database. Syncing with Clerk...\n")
        
        updated_count = 0
        
        for row in users:
            clerk_id = row[0]
            current_email = row[1]
            
            # Skip invalid or placeholder IDs
            if not clerk_id or not str(clerk_id).startswith('user_'):
                continue
                
            # Call Clerk's Backend API for this specific user
            url = f"https://api.clerk.com/v1/users/{clerk_id}"
            response = requests.get(url, headers=headers)
            
            if response.status_code == 200:
                clerk_data = response.json()
                
                email_addresses = clerk_data.get('email_addresses', [])
                primary_email_id = clerk_data.get('primary_email_address_id')
                
                email_to_save = None
                
                # 1. Try to find their primary email
                for ea in email_addresses:
                    if ea.get('id') == primary_email_id:
                        email_to_save = ea.get('email_address')
                        break
                        
                # 2. Fallback to the first available email if primary isn't explicitly marked
                if not email_to_save and email_addresses:
                    email_to_save = email_addresses[0].get('email_address')
                    
                # 3. Update the database if we found an email
                if email_to_save:
                    cursor.execute("UPDATE users SET email = ? WHERE clerk_id = ?", (email_to_save, clerk_id))
                    
                    if not current_email:
                        print(f"✅ FIXED: {clerk_id} -> {email_to_save}")
                        updated_count += 1
                    elif current_email != email_to_save:
                        print(f"🔄 UPDATED: {clerk_id} -> {email_to_save} (Was: {current_email})")
                        updated_count += 1
                    else:
                        print(f"⚡ OK: {clerk_id} already has correct email ({email_to_save})")
                else:
                    print(f"⚠️ Warning: Clerk has no email address on file for {clerk_id}")
                    
            elif response.status_code == 404:
                print(f"❌ Not Found: User {clerk_id} does not exist in Clerk anymore.")
            else:
                print(f"❌ API Error for {clerk_id}: {response.status_code} - {response.text}")
            
            # A tiny pause to respect Clerk's API rate limits
            time.sleep(0.1)
                
        # Save all the newly found emails into the database permanently!
        conn.commit()
        
        print("\n" + "=" * 60)
        print(f"🎉 Sync Complete! Successfully updated {updated_count} records.")
        print("=" * 60 + "\n")
        
    except Exception as e:
        print(f"\n[Script Error] {e}")
    finally:
        if 'conn' in locals():
            conn.close()

if __name__ == "__main__":
    main()