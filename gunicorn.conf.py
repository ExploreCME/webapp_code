import os

# ==========================================
# GUNICORN ASYNC CONFIGURATION
# ==========================================
# This configuration uses 'gevent' to allow a small number of worker processes
# to handle thousands of concurrent connections asynchronously.

# Bind to all interfaces on $PORT (set by Render), defaulting to 8000
bind = f"0.0.0.0:{os.environ.get('PORT', '8000')}"

# --- WORKER CONFIGURATION ---
# Use the gevent async worker class to prevent I/O blocking (DB queries, API calls)
worker_class = "gevent"

# Number of worker processes. 
# Formula: (2 x $NUM_CORES) + 1. 
# For a standard 2-core cloud server, 4 or 5 is ideal.
workers = 4

# The maximum number of simultaneous clients that a single worker will handle.
# 4 workers * 1000 connections = theoretical max of 4000 concurrent users.
worker_connections = 1000

# --- TIMEOUTS & KEEPALIVE ---
# Workers silent for more than this many seconds are killed and restarted.
# Set to 120s to allow time for heavy AI quiz generations or large PDF uploads.
timeout = 120

# The number of seconds to wait for requests on a Keep-Alive connection.
keepalive = 5

# --- LOGGING ---
accesslog = "-"  # Log to stdout
errorlog = "-"   # Log to stderr
loglevel = "info"

# --- SERVER MECHANICS ---
# Preload the application code before worker processes are forked.
# This saves RAM and boots workers faster.
preload_app = True