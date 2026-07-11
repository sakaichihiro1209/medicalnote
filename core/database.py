"""
SQLite のキャッシュスキーマ定義と低レベル接続処理。
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path
from typing import List

SCHEMA = """
CREATE TABLE IF NOT EXISTS knowledge (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL UNIQUE,
    drive_file_id TEXT NOT NULL UNIQUE,
    content TEXT,
    created_at TEXT,
    updated_at TEXT,
    dirty INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS section_master (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    section_name TEXT NOT NULL UNIQUE,
    color TEXT,
    usage_count INTEGER DEFAULT 0,
    created_at TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS inbox_cache (
    drive_file_id TEXT PRIMARY KEY,
    file_name TEXT NOT NULL,
    title TEXT NOT NULL,
    date_time TEXT NOT NULL,
    content TEXT,
    organized INTEGER NOT NULL DEFAULT 0
);
"""


def get_db_path() -> Path:
    """キャッシュ用一時データベースファイルのパスを取得する。"""
    # Render.com や一般 Linux 環境では /tmp が書き込み可能で高速
    # Windows などのローカル検証では一時ディレクトリを使用
    if os.name == "nt":
        return Path(tempfile.gettempdir()) / "medical_knowledge_cache.db"
    return Path("/tmp/medical_knowledge_cache.db")


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    db_path = get_db_path()
    # 親ディレクトリの存在を保証
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    """データベースファイルが無い/空の場合にスキーマを作成する。
    スキーマが古い場合は、ファイルを物理削除して再構築する。
    """
    db_path = get_db_path()
    if db_path.exists():
        try:
            conn = sqlite3.connect(str(db_path))
            # knowledgeテーブルの確認
            cur = conn.execute("PRAGMA table_info(knowledge)")
            cols = [row[1] for row in cur.fetchall()]
            # section_masterの確認
            cur = conn.execute("PRAGMA table_info(section_master)")
            sec_cols = [row[1] for row in cur.fetchall()]
            conn.close()

            # 古い構造なら削除
            if (cols and "content" not in cols) or (cols and "dirty" not in cols) or (sec_cols and "color" not in sec_cols):
                print("Old schema detected. Rebuilding SQLite cache DB...")
                db_path.unlink()
        except Exception as e:
            print(f"Failed to check schema, unlinking old database: {e}")
            try:
                db_path.unlink()
            except:
                pass

    conn = connect()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()
