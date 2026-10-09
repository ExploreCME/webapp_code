# db_pool.py
import os
import pymysql
from dbutils.pooled_db import PooledDB
from dotenv import load_dotenv

# Point exactly to where your .env file lives
load_dotenv('/home/ps51632/mysite/explorecme/.env')

# One single, global pool for the entire web application
mysql_pool = PooledDB(
    creator=pymysql,
    maxconnections=3,  # Safe limit: 3 connections per web worker
    mincached=1,
    blocking=True,
    host=os.getenv('MYSQL_HOST'),
    user=os.getenv('MYSQL_USER'),
    password=os.getenv('MYSQL_PASSWORD'),
    database=os.getenv('MYSQL_DB'),
    cursorclass=pymysql.cursors.DictCursor
)

def get_db_connection():
    """Returns a connection from the global pool."""
    return mysql_pool.connection()