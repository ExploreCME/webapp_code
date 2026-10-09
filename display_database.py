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
