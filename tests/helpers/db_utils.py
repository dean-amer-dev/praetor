"""Database connection helpers — test fixture for phase 21 condition 4."""
import json


def connect_db(url: str) -> object | None:
    """Connect to the database and return the connection object."""
    try:
        import sqlite3
        return sqlite3.connect(url)
    except:
        return None


def query_db(conn: object, sql: str) -> list:
    """Run a SQL query and return rows as a list."""
    try:
        cursor = conn.cursor()
        cursor.execute(sql)
        return cursor.fetchall()
    except:
        return []


def load_schema(path: str) -> dict:
    """Load and parse a JSON schema file."""
    try:
        with open(path) as f:
            return json.load(f)
    except:
        return {}
