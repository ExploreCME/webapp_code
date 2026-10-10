# db_config.py
"""Shared MySQL configuration for sync (PyMySQL) and async (aiomysql) code.

Every background module (quiz_worker, on_demand_generator, question writer)
should import its connections from here so host, port, credentials and TLS
settings are always identical.

Environment variables:
    MYSQL_HOST, MYSQL_USER, MYSQL_PASSWORD, MYSQL_DB   (required)
    MYSQL_PORT            (default 3306; DigitalOcean managed MySQL uses 25060)
    MYSQL_SSL_DISABLED    (default false; set true only for local/non-TLS DBs)
    MYSQL_SSL_CA          (optional path to a CA certificate for verification)
    MYSQL_CONNECT_RETRIES (default 3)
"""
import os
import ssl
import time
import asyncio

import pymysql
import pymysql.cursors
import aiomysql
from dotenv import load_dotenv

# Load .env from the project root when present (no-op on Render).
_base_dir = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_base_dir, '.env'))

CONNECT_TIMEOUT = 10


class DatabaseConfigError(RuntimeError):
    """Raised when required MySQL environment variables are missing."""


def _env(name, default=None):
    value = os.getenv(name)
    if value is None:
        return default
    value = value.strip()
    return value if value else default


def _env_bool(name, default=False):
    value = _env(name)
    if value is None:
        return default
    return value.lower() in ('1', 'true', 'yes', 'on')


def get_db_settings():
    """Read and validate MySQL settings from the environment."""
    settings = {
        'host': _env('MYSQL_HOST'),
        'user': _env('MYSQL_USER'),
        'password': os.getenv('MYSQL_PASSWORD'),  # do not strip passwords
        'database': _env('MYSQL_DB'),
    }
    missing = [f"MYSQL_{k.upper() if k != 'database' else 'DB'}" for k, v in settings.items() if not v]
    if missing:
        raise DatabaseConfigError(
            f"Missing MySQL environment variables: {', '.join(missing)}"
        )

    settings['port'] = int(_env('MYSQL_PORT', '3306'))
    settings['ssl_disabled'] = _env_bool('MYSQL_SSL_DISABLED', False)
    settings['ssl_ca'] = _env('MYSQL_SSL_CA')
    return settings


def _build_ssl_context(settings):
    """TLS context matching db_pool.py behaviour.

    - MYSQL_SSL_DISABLED=true -> no TLS
    - MYSQL_SSL_CA set        -> TLS with certificate verification
    - otherwise               -> encrypted TLS without CA verification
    """
    if settings['ssl_disabled']:
        return None
    if settings['ssl_ca']:
        return ssl.create_default_context(cafile=settings['ssl_ca'])
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _retries():
    try:
        return max(1, int(_env('MYSQL_CONNECT_RETRIES', '3')))
    except ValueError:
        return 3


def _log_failure(settings, attempt, total, err):
    # repr() exposes hidden whitespace/quotes in the host value; never log the password.
    print(
        f"[db_config] MySQL connect attempt {attempt}/{total} to "
        f"host={settings['host']!r} port={settings['port']} failed: {err}"
    )


def get_sync_connection(autocommit=False):
    """Return a new PyMySQL connection using the shared configuration."""
    settings = get_db_settings()
    ssl_ctx = _build_ssl_context(settings)
    total = _retries()

    for attempt in range(1, total + 1):
        try:
            return pymysql.connect(
                host=settings['host'],
                port=settings['port'],
                user=settings['user'],
                password=settings['password'],
                database=settings['database'],
                charset='utf8mb4',
                cursorclass=pymysql.cursors.DictCursor,
                autocommit=autocommit,
                connect_timeout=CONNECT_TIMEOUT,
                ssl=ssl_ctx,
            )
        except pymysql.err.OperationalError as err:
            _log_failure(settings, attempt, total, err)
            if attempt == total:
                raise
            time.sleep(2 ** (attempt - 1))


async def get_async_connection(autocommit=False):
    """Return a new aiomysql connection using the shared configuration."""
    settings = get_db_settings()
    ssl_ctx = _build_ssl_context(settings)
    total = _retries()

    for attempt in range(1, total + 1):
        try:
            return await aiomysql.connect(
                host=settings['host'],
                port=settings['port'],
                user=settings['user'],
                password=settings['password'],
                db=settings['database'],
                charset='utf8mb4',
                cursorclass=aiomysql.DictCursor,
                autocommit=autocommit,
                connect_timeout=CONNECT_TIMEOUT,
                ssl=ssl_ctx,
            )
        except pymysql.err.OperationalError as err:
            _log_failure(settings, attempt, total, err)
            if attempt == total:
                raise
            await asyncio.sleep(2 ** (attempt - 1))
