# display_database.py
from flask import Flask, render_template, request, redirect, url_for, flash, session, g, make_response, abort
from functools import wraps
import pymysql
import os
import secrets
from dotenv import load_dotenv
import stripe
import jwt

# NEW: Performance & Security Imports
from flask_caching import Cache
from jwt import PyJWKClient

# --- PERFORMANCE IMPORTS ---
from db_pool import get_db_connection  # <-- Uses the new global pool
from standardizers import ORGAN_SYSTEMS

# 1. Use the repo root instead of a PythonAnywhere absolute path
basedir = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(basedir, '.env'))
stripe.api_key = (os.getenv('STRIPE_SECRET_KEY') or '').strip() or None
# Recurring Price for PAXcel individual checkout; override with STRIPE_PRICE_ID.
DEFAULT_STRIPE_PRICE_ID = 'price_1UTsSC1IrD5jMp4rcIhmylIf'

COMING_SOON_PRODUCTS = {
    'rnxcel': {
        'product_name': 'RNXcel',
        'product_tagline': 'Nursing Question Bank',
        'product_description': 'Board-style practice questions for nursing students, built with the same generation and verification process as PAXcel.',
        'logo_file': 'images/rnxcel_logo.png',
        'launch_date': '01/01/2027',
    },
    'mdxcel': {
        'product_name': 'MDXcel',
        'product_tagline': 'Medical Doctor Question Bank',
        'product_description': 'Board-style practice questions for medical students and physicians, built with the same generation and verification process as PAXcel.',
        'logo_file': 'images/mdxcel_logo.png',
        'launch_date': '06/02/2027',
    },
}

app = Flask(__name__, template_folder=os.path.join(basedir, 'templates', 'template standardization'))
flask_secret_key = os.getenv('FLASK_SECRET_KEY')
if os.getenv('FLASK_ENV', '').lower() == 'production' and not flask_secret_key:
    raise RuntimeError("FLASK_SECRET_KEY must be set in production.")
app.secret_key = flask_secret_key or secrets.token_hex(32)
if not stripe.api_key:
    app.logger.warning("STRIPE_SECRET_KEY is not configured; individual checkout is unavailable.")

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
BYPASS_SECRET = os.getenv('BYPASS_SECRET', '')

@app.before_request
def check_maintenance_mode():
    if not MAINTENANCE_MODE:
        return

    # 1. Always allow static files (CSS/Images) to load
    if request.endpoint and request.endpoint.startswith('static'):
        return

    # 2. Check if the user is using the secret backdoor URL
    if BYPASS_SECRET and request.args.get('bypass') == BYPASS_SECRET:
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
    """Verify Clerk JWTs using the configured issuer's signing key."""
    issuer = os.getenv('CLERK_ISSUER', 'https://clerk.explorecme.com')
    audience = os.getenv('CLERK_APP_ID')

    signing_key = jwks_client.get_signing_key_from_jwt(token)
    return jwt.decode(
        token,
        key=signing_key.key,
        algorithms=['RS256'],
        audience=audience,
        issuer=issuer,
        options={"require": ["exp", "iat", "sub"]}
    )


def refresh_user_status():
    """Verify identity and load entitlements once per request, never from session."""
    if getattr(g, 'user_status_loaded', False):
        return g.user_id

    g.user_status_loaded = True
    g.user_id = None
    g.is_premium = False
    g.is_faculty = False
    session['is_premium'] = False
    session['is_faculty'] = False
    session.pop('last_db_sync', None)
    clerk_token = request.cookies.get('__session')

    if not clerk_token:
        if not session.get('username', '').startswith('guest_'):
            session.pop('username', None)
            session.pop('accepted_eula', None)
        return None

    try:
        decoded_token = decode_clerk_token(clerk_token)
        user_id = decoded_token.get('sub')
        if not user_id:
            raise ValueError("Missing Clerk user ID")
        if session.get('username') != user_id:
            session.pop('accepted_eula', None)
        session['username'] = user_id

        conn = get_db_connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute("SELECT has_paid, is_faculty FROM users WHERE clerk_id = %s", (user_id,))
                result = cursor.fetchone()
        finally:
            conn.close()

        g.user_id = user_id
        g.is_premium = bool(result and result.get('has_paid') == 1)
        g.is_faculty = bool(result and str(result.get('is_faculty')).strip().upper() in ['1', 'Y', 'YES', 'TRUE'])
        session['is_premium'] = g.is_premium
        session['is_faculty'] = g.is_faculty
        return user_id
    except Exception as e:
        print(f"Error refreshing user status: {e}")
        session.pop('username', None)
        session.pop('accepted_eula', None)
        return None


@app.before_request
def refresh_request_entitlements():
    if request.blueprint == 'rnxcel':
        return
    if request.endpoint and request.endpoint.startswith('static'):
        return
    if request.endpoint in ('webhook', 'clerk_webhook', 'logout'):
        return
    refresh_user_status()


def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not refresh_user_status():
            flash("Please sign in to access your dashboard.", "error")
            return redirect(url_for('paxcel_landing'))

        return f(*args, **kwargs)
    return decorated_function

# ==========================================
# GLOBAL TEMPLATE VARIABLES
# ==========================================
@app.context_processor
def inject_premium_status():
    """Use request-scoped database status, independent of dashboard stats caching."""
    if request.blueprint == 'rnxcel':
        return {}
    user_agent = request.headers.get('User-Agent', '').lower()
    is_app = 'median' in user_agent

    refresh_user_status()
    is_premium = g.is_premium
    is_faculty = g.is_faculty

    # Pass Clerk publishable key to all templates
    clerk_publishable_key = os.getenv('CLERK_PUBLISHABLE_KEY', '')

    return dict(
        is_premium=is_premium,
        is_app=is_app,
        is_faculty=is_faculty,
        is_public_preview=request.blueprint == 'preview' or bool(getattr(g, 'preview', None)),
        clerk_publishable_key=clerk_publishable_key
    )

# ==========================================
# DATABASE HELPER FUNCTIONS
# ==========================================
@cache.cached(timeout=300, key_prefix='dashboard_stats_canonical_v1')
def fetch_data_for_dashboard():
    """Count only exact stored canonical organs; cache for 5 minutes."""
    try:
        conn = get_mysql_db()
        cursor = conn.cursor()
        placeholders = ', '.join(['%s'] * len(ORGAN_SYSTEMS))
        organ_filter = f"BINARY organ_system IN ({placeholders})"

        # DictCursor expects aliased queries to extract variables safely
        cursor.execute(
            f"SELECT COUNT(id) as c FROM question_bank WHERE {organ_filter}",
            ORGAN_SYSTEMS,
        )
        count_res = cursor.fetchone()
        total_questions = count_res['c'] if count_res else 0

        cursor.execute(f"""
            SELECT organ_system, COUNT(id) as c
            FROM question_bank
            WHERE {organ_filter}
            GROUP BY organ_system
            ORDER BY COUNT(id) DESC
        """, ORGAN_SYSTEMS)
        # Convert dictionary rows back to tuples so the charts don't break
        organ_data = [(row['organ_system'], row['c']) for row in cursor.fetchall()]

        cursor.execute(f"""
            SELECT task_area, COUNT(id) as c
            FROM question_bank
            WHERE {organ_filter}
                AND task_area IS NOT NULL AND task_area != ''
            GROUP BY task_area
            ORDER BY COUNT(id) DESC
        """, ORGAN_SYSTEMS)
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
def explorecme_home():
    return render_template('explorecme_home.html')

@app.route('/coming-soon/<product>')
@nocache
def coming_soon(product):
    if product.lower() == 'rnxcel':
        return redirect(url_for('rnxcel.landing'))
    info = COMING_SOON_PRODUCTS.get(product.lower())
    if info is None:
        abort(404)
    return render_template('coming_soon.html', **info)

@app.route('/paxcel')
@nocache
def paxcel_landing():
    user_agent = request.headers.get('User-Agent', '').lower()
    is_app = 'median' in user_agent

    is_logged_in = bool(refresh_user_status())

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
        print("WEBHOOK RECEIVED: SUBSCRIPTION COMPLETED!")
        session_event = event['data']['object']

        # Use getattr instead of dict .get() for StripeObjects to prevent 500 errors
        clerk_user_id = getattr(session_event, 'client_reference_id', None)
        stripe_customer_id = getattr(session_event, 'customer', None)

        print(f"CLERK ID FROM STRIPE: {clerk_user_id}")

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
            print("ERROR: clerk_user_id is still None.")
            print(f"RAW STRIPE PAYLOAD: {session_event}")

    elif event['type'] == 'customer.subscription.deleted':
        print("WEBHOOK RECEIVED: SUBSCRIPTION CANCELED!")
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
    print("TRIPWIRE: CLERK WEBHOOK REACHED PYTHON!")

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

    print(f"CLERK WEBHOOK RECEIVED: {event_type}")

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
        return redirect(url_for('paxcel_landing'))


@app.route('/create-checkout-session', methods=['POST'])
@login_required
def create_checkout_session():
    if not stripe.api_key:
        app.logger.error(
            "Checkout blocked: STRIPE_SECRET_KEY missing/empty. host=%s path=%s",
            request.host,
            request.path,
        )
        flash("Checkout is temporarily unavailable. Please try again later.", "error")
        return redirect(url_for('paxcel_landing'))

    price_id = (os.getenv('STRIPE_PRICE_ID') or '').strip() or DEFAULT_STRIPE_PRICE_ID
    try:
        clerk_token = request.cookies.get('__session')
        if not clerk_token:
            app.logger.warning("Checkout blocked: missing __session cookie.")
            flash("Please sign in again and retry checkout.", "error")
            return redirect(url_for('paxcel_landing'))

        decoded_token = decode_clerk_token(clerk_token)
        user_id = decoded_token.get('sub')
        if not user_id:
            app.logger.warning("Checkout blocked: decoded Clerk token missing sub.")
            flash("Please sign in again and retry checkout.", "error")
            return redirect(url_for('paxcel_landing'))

        app.logger.info(
            "Checkout start: user_id=%s key_present=%s livemode_guess=%s price_id=%s",
            user_id,
            bool(stripe.api_key),
            stripe.api_key.startswith('sk_live_'),
            price_id,
        )

        checkout_session = stripe.checkout.Session.create(
            client_reference_id=user_id,
            line_items=[
                {
                    'price': price_id,
                    'quantity': 1,
                },
            ],
            mode='subscription',
            allow_promotion_codes=True,
            # Pass session ID back to beat the webhook race condition
            success_url=request.host_url + 'payment-success?session_id={CHECKOUT_SESSION_ID}',
            cancel_url=request.host_url,
        )
    except stripe.error.StripeError as e:
        # Stripe SDK errors (authentication, invalid request, permissions, etc.)
        app.logger.exception(
            "Stripe checkout error: type=%s code=%s param=%s user_message=%s",
            type(e).__name__,
            getattr(e, 'code', None),
            getattr(e, 'param', None),
            getattr(e, 'user_message', None),
        )
        flash("Checkout is temporarily unavailable. Please try again later.", "error")
        return redirect(url_for('paxcel_landing'))
    except Exception as e:
        # Non-Stripe errors (token decode, unexpected runtime issues)
        app.logger.exception("Checkout unexpected error: type=%s", type(e).__name__)
        flash("Checkout is temporarily unavailable. Please try again later.", "error")
        return redirect(url_for('paxcel_landing'))

    return redirect(checkout_session.url, code=303)

# Intercept the session ID and upgrade the user instantly
@app.route('/payment-success')
@login_required
def payment_success():
    session_id = request.args.get('session_id')

    if session_id:
        try:
            # Ask Stripe directly if this checkout was successfully paid
            checkout_session = stripe.checkout.Session.retrieve(session_id)

            if checkout_session.payment_status == 'paid':
                # Update the database instantly to beat the webhook race condition
                clerk_user_id = checkout_session.client_reference_id
                stripe_customer_id = checkout_session.customer

                if clerk_user_id == g.user_id and stripe_customer_id:
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

        if not result or not result.get('stripe_customer_id'):
            flash("No active subscription found.", "error")
            return redirect(url_for('paxcel_landing'))

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
    response = redirect(url_for('paxcel_landing'))
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

from student_reports import student_reports_bp
app.register_blueprint(student_reports_bp)

from preview_app import preview_bp
app.register_blueprint(preview_bp)

from rnxcel_routes import rnxcel_bp
app.register_blueprint(rnxcel_bp)

from paxcel_game import paxcel_game_bp
app.register_blueprint(paxcel_game_bp)
