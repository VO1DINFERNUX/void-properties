"""Create the local SQLite database and apply the schema.

Usage:
    python scripts/init_db.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.db.database import DB_PATH, init_db

if __name__ == "__main__":
    init_db()
    print(f"Database ready at {DB_PATH}")
