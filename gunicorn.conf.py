import os

# ==========================================
# GUNICORN ASYNC CONFIGURATION
# ==========================================
# This configuration uses 'gevent' to allow a small number of worker processes
# to handle many concurrent connections asynchronously.

# Render (and most PaaS environments) expects the app to bind to the externally
# assigned port.
bind = f"0.0.0.0:{os.environ.get('PORT', '8000')}"

# --- WORKER CONFIGURATION ---
# Use the gevent async worker class to prevent I/O blocking (DB queries, API calls)
worker_class = "gevent"

# Number of worker processes.
# Keep this modest because each worker can handle many concurrent requests.
workers = int(os.environ.get("WEB_CONCURRENCY", "4"))

# The maximum number of simultaneous clients that a single worker will handle.
worker_connections = int(os.environ.get("WORKER_CONNECTIONS", "1000"))

# --- TIMEOUTS & KEEPALIVE ---
# Workers silent for more than this many seconds are killed and restarted.
timeout = int(os.environ.get("GUNICORN_TIMEOUT", "120"))

# The number of seconds to wait for requests on a Keep-Alive connection.
keepalive = int(os.environ.get("GUNICORN_KEEPALIVE", "5"))

# --- LOGGING ---
accesslog = "-"  # Log to stdout
errorlog = "-"   # Log to stderr
loglevel = os.environ.get("GUNICORN_LOGLEVEL", "info")

# --- SERVER MECHANICS ---
# Preload the application code before worker processes are forked.
# This saves RAM and boots workers faster.
preload_app = True
