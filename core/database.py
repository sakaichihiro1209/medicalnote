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
from flask import session, has_request_context

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

CREATE TABLE IF NOT EXISTS user_config (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def get_db_path(user_id: str | None = None) -> Path:
    """キャッシュ用一時データベースファイルのパスを取得する。ユーザーIDごとにファイルを分割。"""
    # ユーザーIDの特定
    if not user_id and has_request_context():
        try:
            user_id = session.get("google_user_id")
        except Exception:
            pass

    # ファイル名用の安全なサフィックスを生成 (MD5ハッシュ)
    suffix = ""
    if user_id:
        import hashlib
        suffix = "_" + hashlib.md5(user_id.encode("utf-8")).hexdigest()

    db_filename = f"medical_cache{suffix}.db"

    if os.name == "nt":
        return Path(tempfile.gettempdir()) / db_filename
    return Path(f"/tmp/{db_filename}")


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def connect(user_id: str | None = None) -> sqlite3.Connection:
    db_path = get_db_path(user_id=user_id)
    # 親ディレクトリの存在を保証
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL (Write-Ahead Logging) モードを有効化して、読込と書込の並行競合を解消
    try:
        conn.execute("PRAGMA journal_mode = WAL")
    except sqlite3.Error:
        pass
    return conn


def init_db(user_id: str | None = None) -> None:
    """データベースファイルが無い/空の場合にスキーマを作成する。
    スキーマが古い場合は、ファイルを物理削除して再構築する。
    """
    db_path = get_db_path(user_id=user_id)
    if db_path.exists():
        try:
            conn = sqlite3.connect(str(db_path))
            cur = conn.execute("PRAGMA table_info(knowledge)")
            cols = [row[1] for row in cur.fetchall()]
            cur = conn.execute("PRAGMA table_info(section_master)")
            sec_cols = [row[1] for row in cur.fetchall()]
            # 新しいテーブル user_config が存在するかチェック
            cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='user_config'")
            has_user_config = cur.fetchone() is not None
            conn.close()

            # 古い構造なら削除
            if (cols and "content" not in cols) or (cols and "dirty" not in cols) or (sec_cols and "color" not in sec_cols) or not has_user_config:
                print(f"Old schema detected for user {user_id}. Rebuilding SQLite cache DB...")
                db_path.unlink()
        except Exception as e:
            print(f"Failed to check schema, unlinking old database: {e}")
            try:
                db_path.unlink()
            except:
                pass

    conn = connect(user_id=user_id)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()
