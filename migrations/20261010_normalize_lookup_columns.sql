# db_pool.py
import os
import pymysql
from dbutils.pooled_db import PooledDB
from dotenv import load_dotenv

# Load .env from the project root when present.
base_dir = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(base_dir, '.env'))

mysql_pool = None


def _build_pool():
    """Create the MySQL pool only when the environment is complete."""
    global mysql_pool

    required = [
        os.getenv('MYSQL_HOST'),
        os.getenv('MYSQL_USER'),
        os.getenv('MYSQL_PASSWORD'),
        os.getenv('MYSQL_DB'),
    ]

    if not all(required):
        return None

    ssl_disabled = os.getenv('MYSQL_SSL_DISABLED', 'false').lower() in ('1', 'true', 'yes', 'on')
    ssl_ca = os.getenv('MYSQL_SSL_CA')

    if ssl_disabled:
        ssl_config = None
    elif ssl_ca:
        ssl_config = {'ca': ssl_ca}
    else:
        ssl_config = {'ssl': {}}

    mysql_pool = PooledDB(
        creator=pymysql,
        maxconnections=12,
        mincached=2,
        blocking=True,
        host=os.getenv('MYSQL_HOST'),
        port=int(os.getenv('MYSQL_PORT', '3306')),
        user=os.getenv('MYSQL_USER'),
        password=os.getenv('MYSQL_PASSWORD'),
        database=os.getenv('MYSQL_DB'),
        charset='utf8mb4',
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True,
        connect_timeout=10,
        ping=1,
        ssl=ssl_config,
    )
    return mysql_pool


def get_db_connection():
    """Returns a connection from the global pool or raises a clear config error."""
    global mysql_pool

    if mysql_pool is None:
        mysql_pool = _build_pool()

    if mysql_pool is None:
        raise RuntimeError(
            "MySQL environment values are not set. Set MYSQL_HOST, MYSQL_USER, "
            "MYSQL_PASSWORD, and MYSQL_DB before starting the app."
        )

    return mysql_pool.connection()
