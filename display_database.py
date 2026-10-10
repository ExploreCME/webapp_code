# display_database.py
from flask import Flask, render_template, request, redirect, url_for, flash, session, g, make_response
from functools import wraps
import pymysql
import os
import time
from dotenv import load_dotenv
import stripe
import jwt

# NEW: Performance & Security Imports
from flask_caching import Cache
from jwt import PyJWKClient

# --- PERFORMANCE IMPORTS ---
from db_pool import get_db_connection  # <-- Uses the new global pool

# 1. Use the repo root instead of a PythonAnywhere absolute path
basedir = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(basedir, '.env'))

app = Flask(__name__, template_folder=os.path.join(basedir, 'templates', 'template standardization'))
app.secret_key = os.getenv('FLASK_SECRET_KEY', 'super_secret_pance_key_2026')

# Session configuration to match your quiz app requirements
app.config['SESSION_COOKIE_SAMESITE'] = os.getenv('SESSION_COOKIE_SAMESITE', 'None')
app.config['SESSION_COOKIE_SECURE'] = os.getenv('SESSION_COOKIE_SECURE', 'True').lower() in ('1', 'true', 'yes', 'on')

# 2. Setup Redis-backed cache for Render / production deployment
redis_url = os.getenv('REDIS_URL')
cache_config = {
    'CACHE_TYPE': 'RedisCache' if redis_url else 'SimpleCache',
    'CACHE_DEFAULT_TIMEOUT': int(os.getenv('CACHE_DEFAULT_TIMEOUT', '300')),
}
if redis_url:
    cache_config['CACHE_REDIS_URL'] = redis_url

cache = Cache(app, config=cache_config)

# ==========================================
# CONFIGURATION & DB MANAGEMENT
# ==========================================
def get_mysql_db():
    """Pulls a connection from the global pool."""
    if 'mysql_db' not in g:
        g.mysql_db = get_db_connection()
    return g.mysql_db

@app.teardown_appcontext
def close_dbs(error):
    mysql_db = g.pop('mysql_db', None)
    if mysql_db is not None:
        try:
            mysql_db.close()  # Safely returns it to the pool
        except Exception:
            pass

# ==========================================
# MAINTENANCE MODE TOGGLE
# ==========================================
MAINTENANCE_MODE = os.getenv('MAINTENANCE_MODE', 'True').lower() in ('1', 'true', 'yes', 'on')
BYPASS_SECRET = os.getenv('BYPASS_SECRET', 'let_me_in_2026')

@app.before_request
def check_maintenance_mode():
    if not MAINTENANCE_MODE:
        return

    # 1. Always allow static files (CSS/Images) to load
    if request.endpoint and request.endpoint.startswith('static'):
        return

    # 2. Check if the user is using the secret backdoor URL
    if request.args.get('bypass') == BYPASS_SECRET:
        session['maintenance_bypass'] = True
        return redirect(request.path)

    # 3. If they don't have the bypass token in their session, show the maintenance page
    if not session.get('maintenance_bypass'):
        return render_template('maintenance.html'), 503

# ==========================================
# CACHE BUSTING DECORATOR
# ==========================================
def nocache(view):
    @wraps(view)
    def no_cache(*args, **kwargs):
        response = make_response(view(*args, **kwargs))
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, public, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response
    return no_cache

# ==========================================
# SECURITY BOUNCER & SESSION BRIDGE
# ==========================================
# Initialize JWKS Client for secure Clerk verification.
# Render allows unrestricted outbound access, so signature verification can be re-enabled.
jwks_client = PyJWKClient(os.getenv('CLERK_JWKS_URL', 'https://clerk.explorecme.com/.well-known/jwks.json'))


def decode_clerk_token(token):
    """Verify Clerk JWTs when possible, but fall back gracefully for older/dev setups."""
    issuer = os.getenv('CLERK_ISSUER', 'https://clerk.explorecme.com')
    audience = os.getenv('CLERK_APP_ID')

    try:
        signing_key = jwks_client.get_signing_key_from_jwt(token)
        return jwt.decode(
            token,
            key=signing_key.key,
            algorithms=['RS256'],
            audience=audience,
            issuer=issuer,
            options={"require": ["exp", "iat", "sub"]}
        )
    except Exception:
        # Graceful fallback for local or temporary insecure environments.
        return jwt.decode(token, options={"verify_signature": False})


def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        clerk_token = request.cookies.get('__session')

        if not clerk_token:
            flash("Please sign in to access your dashboard.", "error")
            return redirect(url_for('index'))

        # Fast path check against the encrypted session
        # ONLY bypasses DB if the permissions are already loaded
        if session.get('username') and 'is_premium' in session:
            return f(*args, **kwargs)

        try:
            decoded_token = decode_clerk_token(clerk_token)
            user_id = decoded_token.get('sub')

            if user_id:
                session['username'] = user_id

                # Fetch premium/faculty status ONCE and store in session (Swapped to MySQL)
                conn = get_mysql_db()
                cursor = conn.cursor()
                cursor.execute("SELECT has_paid, is_faculty FROM users WHERE clerk_id = %s", (user_id,))
                result = cursor.fetchone()

                if result:
                    # 👇 FIXED: Access via Dictionary Keys instead of Tuple Indices
                    session['is_premium'] = bool(result.get('has_paid') == 1)
                    session['is_faculty'] = bool(str(result.get('is_faculty')).strip().upper() in ['1', 'Y', 'YES', 'TRUE'])
                else:
                    session['is_premium'] = False
                    session['is_faculty'] = False

        except Exception as e:
            print(f"Auth error decoding token: {e}")
            session.clear()
            flash("Session expired or invalid. Please sign in again.", "error")
            return redirect(url_for('index'))

        return f(*args, **kwargs)
    return decorated_function

# ==========================================
# GLOBAL TEMPLATE VARIABLES
# ==========================================
@app.context_processor
def inject_premium_status():
    """Reads strictly from memory/session to prevent DB locking on renders."""
    user_agent = request.headers.get('User-Agent', '').lower()
    is_app = 'median' in user_agent

    is_premium = session.get('is_premium', False)
    is_faculty = session.get('is_faculty', False)
    
    # 👇 NEW: Pass Clerk publishable key to all templates
    clerk_publishable_key = os.getenv('CLERK_PUBLISHABLE_KEY', '')

    return dict(
        is_premium=is_premium, 
        is_app=is_app, 
        is_faculty=is_faculty,
        clerk_publishable_key=clerk_publishable_key
    )

# ==========================================
# DATABASE HELPER FUNCTIONS
# ==========================================
@cache.cached(timeout=300, key_prefix='dashboard_stats')
def fetch_data_for_dashboard():
    """Cached for 5 minutes to prevent MySQL overload."""
    try:
        conn = get_mysql_db()
        cursor = conn.cursor()

        # 👇 FIXED: DictCursor expects aliased queries to extract variables safely
        cursor.execute("SELECT COUNT(id) as c FROM question_bank")
        count_res = cursor.fetchone()
        total_questions = count_res['c'] if count_res else 0

        cursor.execute("""
            SELECT organ_system, COUNT(id) as c
            FROM question_bank
            WHERE organ_system IS NOT NULL AND organ_system != ''
            GROUP BY organ_system
            ORDER BY COUNT(id) DESC
        """)
        # Convert dictionary rows back to tuples so the charts don't break
        organ_data = [(row['organ_system'], row['c']) for row in cursor.fetchall()]

        cursor.execute("""
            SELECT task_area, COUNT(id) as c
            FROM question_bank
            WHERE task_area IS NOT NULL AND task_area != ''
            GROUP BY task_area
            ORDER BY COUNT(id) DESC
        """)
        # Convert dictionary rows back to tuples so the charts don't break
        task_data_raw = [(row['task_area'], row['c']) for row in cursor.fetchall()]

    except Exception as e:
        print(f"Error fetching MySQL dashboard data: {e}")
        return 0, [], []

    task_area_mapping = {
        "History Taking and Performing Physical Examination": "Hx & PE",
        "Using Diagnostic and Laboratory Studies": "Labs & Dx",
        "Formulating the Most Likely Diagnosis": "Most Likely Dx",
        "Health Maintenance, Patient Education, and Preventive Measures": "Health Maint & Prev",
        "Clinical Intervention": "Clinic Int.",
        "Pharmaceutical Therapeutics": "Pharm Therapeutics",
        "Applying Foundational Scientific Concepts": "Basic Science"
    }

    task_data_dict = {}
    for area, count in task_data_raw:
        if area:
            truncated_area = task_area_mapping.get(area, area)
            task_data_dict[truncated_area] = task_data_dict.get(truncated_area, 0) + count

    task_data = list(task_data_dict.items())
    task_data.sort(key=lambda x: x[1], reverse=True)

    task_data = task_data[:7]

    return total_questions, organ_data, task_data

# ==========================================
# ROUTES
# ==========================================
@app.route('/db_dashboard')
@login_required
@nocache
def db_dashboard():
    total_q, organs, tasks = fetch_data_for_dashboard()

    organ_labels = [row[0] for row in organs]
    organ_counts = [row[1] for row in organs]
    task_labels = [row[0] for row in tasks]
    task_counts = [row[1] for row in tasks]

    return render_template(
        'index_for_display.html',
        total_questions=total_q,
        organ_labels=organ_labels,
        organ_counts=organ_counts,
        task_labels=task_labels,
        task_counts=task_counts
    )

@app.route('/')
@nocache
def index():
    clerk_token = request.cookies.get('__session')
    user_agent = request.headers.get('User-Agent', '').lower()
    is_app = 'median' in user_agent

    is_logged_in = False

    if clerk_token:
        try:
            decoded_token = decode_clerk_token(clerk_token)
            user_id = decoded_token.get('sub')

            if user_id:
                session['username'] = user_id
                is_logged_in = True

                # OPTIMIZATION: Throttle DB sync on homepage load (once every 10 mins)
                current_time = time.time()
                last_sync = session.get('last_db_sync', 0)

                if current_time - last_sync > 600:
                    conn = get_mysql_db()
                    cursor = conn.cursor()
                    cursor.execute("SELECT has_paid, is_faculty FROM users WHERE clerk_id = %s", (user_id,))
                    result = cursor.fetchone()

                    if result:
                        # 👇 FIXED: Access via Dictionary Keys
                        session['is_premium'] = bool(result.get('has_paid') == 1)
                        session['is_faculty'] = bool(str(result.get('is_faculty')).strip().upper() in ['1', 'Y', 'YES', 'TRUE'])
                        session['last_db_sync'] = current_time
                    else:
                        session['is_premium'] = False
                        session['is_faculty'] = False

        except Exception as e:
            print(f"Error checking user status on index: {e}")

    if is_app and not is_logged_in:
        return render_template('app_login.html')

    total_q, organs, tasks = fetch_data_for_dashboard()

    organ_labels = [row[0] for row in organs]
    organ_counts = [row[1] for row in organs]
    task_labels = [row[0] for row in tasks]
    task_counts = [row[1] for row in tasks]

    return render_template(
        'index_for_display.html',
        total_questions=total_q,
        organ_labels=organ_labels,
        organ_counts=organ_counts,
        task_labels=task_labels,
        task_counts=task_counts
    )

@app.route('/webhook', methods=['POST'])
def webhook():
    payload = request.data
    sig_header = request.headers.get('Stripe-Signature')
    endpoint_secret = os.getenv('STRIPE_WEBHOOK_SECRET')

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, endpoint_secret
        )
    except ValueError as e:
        return 'Invalid payload', 400
    except stripe.error.SignatureVerificationError as e:
        return 'Invalid signature', 400

    if event['type'] == 'checkout.session.completed':
        print("🚨🚨🚨 WEBHOOK RECEIVED: SUBSCRIPTION COMPLETED! 🚨🚨🚨")
        session_event = event['data']['object']

        # 👇 FIXED: Use getattr instead of dict .get() for StripeObjects to prevent 500 errors
        clerk_user_id = getattr(session_event, 'client_reference_id', None)
        stripe_customer_id = getattr(session_event, 'customer', None)

        print(f"🚨🚨🚨 CLERK ID FROM STRIPE: {clerk_user_id} 🚨🚨🚨")

        if clerk_user_id and stripe_customer_id:
            try:
                conn = get_mysql_db()
                cursor = conn.cursor()

                cursor.execute("""
                    UPDATE users
                    SET has_paid = 1, stripe_customer_id = %s
                    WHERE clerk_id = %s
                """, (stripe_customer_id, clerk_user_id))

                if cursor.rowcount == 0:
                    cursor.execute("""
                        INSERT INTO users (clerk_id, has_paid, stripe_customer_id)
                        VALUES (%s, 1, %s)
                    """, (clerk_user_id, stripe_customer_id))
                    print(f"Inserted new user and granted premium access to: {clerk_user_id}")
                else:
                    print(f"Updated existing user and granted premium access to: {clerk_user_id}")

                conn.commit()

            except Exception as e:
                print(f"Database error during webhook: {e}")
        else:
            print("🚨 ERROR: clerk_user_id is still None.")
            print(f"RAW STRIPE PAYLOAD: {session_event}")

    elif event['type'] == 'customer.subscription.deleted':
        print("🚨🚨🚨 WEBHOOK RECEIVED: SUBSCRIPTION CANCELED! 🚨🚨🚨")
        subscription = event['data']['object']

        stripe_customer_id = getattr(subscription, 'customer', None)

        if stripe_customer_id:
            try:
                conn = get_mysql_db()
                cursor = conn.cursor()

                cursor.execute("""
                    UPDATE users
                    SET has_paid = 0
                    WHERE stripe_customer_id = %s
                """, (stripe_customer_id,))

                conn.commit()
                print(f"Revoked access for Stripe Customer: {stripe_customer_id}")
            except Exception as e:
                print(f"Database error during cancellation: {e}")

    return 'Success', 200

# ==========================================
# NEW CLERK WEBHOOK
# ==========================================
@app.route('/clerk-webhook', methods=['POST'])
def clerk_webhook():
    print("🚨🚨🚨 TRIPWIRE: CLERK WEBHOOK REACHED PYTHON! 🚨🚨🚨")

    # 1. Force Flask to parse the JSON regardless of Clerk's headers
    try:
        payload = request.get_json(force=True)
    except Exception as e:
        print(f"Webhook Payload Error: {e}")
        return 'Invalid JSON', 400

    if not payload:
        return 'No payload provided', 400

    event_type = payload.get('type')
    data = payload.get('data', {})

    print(f"🚨 CLERK WEBHOOK RECEIVED: {event_type}")

    # 2. We only care about user profile changes
    if event_type in ['user.created', 'user.updated']:
        clerk_id = data.get('id')
        first_name = data.get('first_name') or ''
        last_name = data.get('last_name') or ''

        # Extract the primary email address
        email = ''
        email_addresses = data.get('email_addresses', [])
        primary_email_id = data.get('primary_email_address_id')

        for ea in email_addresses:
            if ea.get('id') == primary_email_id:
                email = ea.get('email_address', '')
                break

        # Fallback if primary ID didn't match but emails exist
        if not email and email_addresses:
            email = email_addresses[0].get('email_address', '')

        if clerk_id:
            try:
                conn = get_mysql_db()
                cursor = conn.cursor()

                # Try to update the user first
                cursor.execute("""
                    UPDATE users
                    SET first_name = %s, last_name = %s, email = %s
                    WHERE clerk_id = %s
                """, (first_name, last_name, email, clerk_id))

                # If 0 rows were updated, they are brand new, so insert them
                if cursor.rowcount == 0:
                    cursor.execute("""
                        INSERT INTO users (clerk_id, first_name, last_name, email)
                        VALUES (%s, %s, %s, %s)
                    """, (clerk_id, first_name, last_name, email))
                    print(f"Stored new user profile: {email}")
                else:
                    print(f"Updated user profile: {email}")

                conn.commit()
            except Exception as e:
                print(f"Database error during Clerk webhook: {e}")
                return 'Database Error', 500

    return 'Success', 200

# ==========================================
# INSTITUTIONAL ACCESS ROUTE (STRIPE CHECKOUT)
# ==========================================
@app.route('/institutional-access', methods=['GET', 'POST'])
@login_required
def institutional_access():
    try:
        clerk_token = request.cookies.get('__session')
        decoded_token = decode_clerk_token(clerk_token)
        user_id = decoded_token.get('sub')

        base_stripe_url = "https://buy.stripe.com/28E00b3adbt58IR0hvdfG01"
        checkout_url = f"{base_stripe_url}?client_reference_id={user_id}"

        return redirect(checkout_url, code=303)
    except Exception as e:
        print(f"Error redirecting to institutional checkout: {e}")
        flash("An error occurred starting checkout. Please try again.", "error")
        return redirect(url_for('index'))


@app.route('/create-checkout-session', methods=['POST'])
@login_required
def create_checkout_session():
    try:
        clerk_token = request.cookies.get('__session')
        decoded_token = decode_clerk_token(clerk_token)
        user_id = decoded_token.get('sub')

        checkout_session = stripe.checkout.Session.create(
            client_reference_id=user_id,
            line_items=[
                {
                    'price': 'price_1U8XG31IrD5jMp4rgoj4dYGg',
                    'quantity': 1,
                },
            ],
            mode='subscription',
            allow_promotion_codes=True,
            # 👇 FIXED: Pass session ID back to beat the webhook race condition
            success_url=request.host_url + 'payment-success?session_id={CHECKOUT_SESSION_ID}',
            cancel_url=request.host_url,
        )
    except Exception as e:
        return str(e), 403

    return redirect(checkout_session.url, code=303)

# 👇 FIXED: Intercept the session ID and upgrade the user instantly
@app.route('/payment-success')
@login_required
def payment_success():
    session_id = request.args.get('session_id')

    if session_id:
        try:
            # Ask Stripe directly if this checkout was successfully paid
            checkout_session = stripe.checkout.Session.retrieve(session_id)

            if checkout_session.payment_status == 'paid':
                # Force the browser session to premium immediately
                session['is_premium'] = True
                session['last_db_sync'] = time.time()

                # Update the database instantly to beat the webhook race condition
                clerk_user_id = checkout_session.client_reference_id
                stripe_customer_id = checkout_session.customer

                if clerk_user_id and stripe_customer_id:
                    conn = get_mysql_db()
                    cursor = conn.cursor()

                    cursor.execute("""
                        UPDATE users SET has_paid = 1, stripe_customer_id = %s
                        WHERE clerk_id = %s
                    """, (stripe_customer_id, clerk_user_id))

                    if cursor.rowcount == 0:
                        cursor.execute("""
                            INSERT INTO users (clerk_id, has_paid, stripe_customer_id)
                            VALUES (%s, 1, %s)
                        """, (clerk_user_id, stripe_customer_id))

                    conn.commit()

        except Exception as e:
            print(f"Error validating stripe session: {e}")

    # Fallback to clear cache just in case
    session.pop('last_db_sync', None)

    flash("Payment successful! Your premium access is now active.", "success")
    return redirect(url_for('db_dashboard'))


@app.route('/create-customer-portal-session', methods=['POST'])
@login_required
def customer_portal():
    try:
        clerk_token = request.cookies.get('__session')
        decoded_token = decode_clerk_token(clerk_token)
        user_id = decoded_token.get('sub')

        conn = get_mysql_db()
        cursor = conn.cursor()
        cursor.execute("SELECT stripe_customer_id FROM users WHERE clerk_id = %s", (user_id,))
        result = cursor.fetchone()

        # 👇 FIXED: Access via Dictionary Key instead of tuple index
        if not result or not result.get('stripe_customer_id'):
            flash("No active subscription found.", "error")
            return redirect(url_for('index'))

        stripe_customer_id = result.get('stripe_customer_id')

        portalSession = stripe.billing_portal.Session.create(
            customer=stripe_customer_id,
            return_url=request.host_url,
        )
        return redirect(portalSession.url, code=303)

    except Exception as e:
        print(f"Portal error: {e}")
        return str(e), 403

@app.route('/logout')
def logout():
    session.clear()
    response = redirect(url_for('index'))
    response.set_cookie('__session', '', expires=0, path='/')
    flash("You have been successfully logged out.", "success")
    return response


# ==========================================
# IMPORT & REGISTER THE BLUEPRINTS
# ==========================================
from flask_app import quiz_bp
app.register_blueprint(quiz_bp)

from user_stats import stats_bp
app.register_blueprint(stats_bp)

from faculty_routes import faculty_bp
app.register_blueprint(faculty_bp)

from preview_app import preview_bp
app.register_blueprint(preview_bp)
