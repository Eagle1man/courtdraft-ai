"""Показать режим журнала и таймаут блокировок SQLite."""

from __future__ import annotations

import sqlite3
from pathlib import Path

DB = Path("/home/ubuntuii/courtdraft-ai/data/cases/courtdraft.db")
connection = sqlite3.connect(DB, timeout=30)
try:
    print("journal_mode:", connection.execute("PRAGMA journal_mode").fetchone()[0])
    print("busy_timeout:", connection.execute("PRAGMA busy_timeout").fetchone()[0])
    print("foreign_keys:", connection.execute("PRAGMA foreign_keys").fetchone()[0])
finally:
    connection.close()
