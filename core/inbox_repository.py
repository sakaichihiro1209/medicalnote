"""
未整理キャプチャ (Inbox) のロード、新規作成、追記（修正）、および整理状態のキャッシュ管理を行うモジュール。
"""

import re
import sqlite3
import threading
from datetime import datetime
from typing import List, Dict, Tuple, Optional
from . import database
from . import gdrive_client
from . import settings

_INVALID_FS_CHARS = re.compile(r'[\\/:*?"<>|]')


def _safe_fragment(text: str, max_len: int = 20) -> str:
    """見出し語からファイル名・パスに使用可能な安全な文字列を生成する。"""
    # 禁則文字を除去
    sanitized = _INVALID_FS_CHARS.sub("_", text).strip()
    # 連続する空白を単一の "_" に置換
    sanitized = re.sub(r"\s+", "_", sanitized)
    if not sanitized:
        return "capture"
    return sanitized[:max_len]


def rebuild_inbox_cache() -> int:
    """Google ドライブ上の Inbox フォルダから全 Markdown 本文をスキャンし、SQLite キャッシュを再構築する。
    すでに整理済みのファイルは本文のダウンロードを完全にスキップして処理を極限まで高速化します。
    """
    structure = gdrive_client.ensure_vault_structure()
    if not structure:
        return 0

    inbox_folder_id = structure["inbox"]
    files = gdrive_client.list_files_in_folder(inbox_folder_id)

    db = database.connect()
    restored = 0
    try:
        valid_files = [f for f in files if f.get("name", "").lower().endswith(".md")]
        valid_ids = [f["id"] for f in valid_files]

        # 既存の organized 状態をチェックするために一時取得
        existing_map = {}
        try:
            cur = db.execute("SELECT drive_file_id, organized FROM inbox_cache")
            existing_map = {row["drive_file_id"]: row["organized"] for row in cur.fetchall()}
        except sqlite3.Error:
            pass

        with db:
            # 1. Googleドライブから消去されたファイルをSQLiteからも削除
            if valid_ids:
                placeholders = ",".join("?" for _ in valid_ids)
                db.execute(
                    f"DELETE FROM inbox_cache WHERE drive_file_id NOT IN ({placeholders})",
                    valid_ids,
                )
            else:
                db.execute("DELETE FROM inbox_cache")

            # 2. 全ファイルを走査してキャッシュ登録
            for file_info in valid_files:
                file_id = file_info["id"]
                name = file_info.get("name", "")

                is_organized = existing_map.get(file_id, 0)

                # ファイル名から日付を解析し、タイトルはファイル名（拡張子除く）そのままとする
                dt = datetime.now()
                title = name[:-3] if name.lower().endswith(".md") else name

                if len(name) >= 16 and name[8] == "_" and name[15] == "_":
                    try:
                        year = int(name[0:4])
                        month = int(name[4:6])
                        day = int(name[6:8])
                        hour = int(name[9:11])
                        minute = int(name[11:13])
                        second = int(name[13:15])
                        dt = datetime(year, month, day, hour, minute, second)
                    except ValueError:
                        pass

                dt_str = dt.strftime("%Y-%m-%d %H:%M:%S")

                # 整理済みの場合は本文の事前ダウンロードをスキップ
                if is_organized == 1:
                    text = ""
                else:
                    text = gdrive_client.download_file_content(file_id) or ""

                db.execute(
                    "INSERT OR REPLACE INTO inbox_cache (drive_file_id, file_name, title, date_time, content, organized) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (file_id, name, title, dt_str, text, is_organized),
                )
                restored += 1
        return restored
    except Exception as e:
        print(f"Rebuild inbox cache failed: {e}")
        return restored
    finally:
        db.close()


def list_captures() -> List[Dict]:
    """SQLite キャッシュから Inbox の全ファイルリストを取得する。未整理が上、整理済みが下に並びます。"""
    # Google ログインしていない状態、または同期が完了するまではキャッシュを読み込まない
    if not gdrive_client.get_credentials() or settings.get("VAULT_SYNCHRONIZED") != "true":
        return []

    db = database.connect()
    try:
        cur = db.execute("SELECT COUNT(*) AS c FROM inbox_cache")
        count = cur.fetchone()["c"]
        if count == 0:
            db.close()
            rebuild_inbox_cache()
            db = database.connect()

        # organized ASC (未整理0 -> 整理済1), file_name DESC (日付最新順) でソート
        cur = db.execute("SELECT * FROM inbox_cache ORDER BY organized ASC, file_name DESC")
        entries = []
        for row in cur.fetchall():
            entries.append({
                "drive_file_id": row["drive_file_id"],
                "file_name": row["file_name"],
                "title": row["title"],
                "date_time": row["date_time"],
                "organized": row["organized"] == 1
            })
        return entries
    finally:
        db.close()


def create_capture(
    text: str, title_hint: str | None = None, images: List[Tuple[str, bytes]] | None = None
) -> str | None:
    """Google ドライブの Inbox/ フォルダに新規キャプチャを保存する。画像は Attachments/ フォルダへ。"""
    structure = gdrive_client.ensure_vault_structure()
    if not structure:
        return None

    now = datetime.now()
    stamp = now.strftime("%Y%m%d_%H%M%S")

    # 見出し語の決定
    first_line = text.strip().splitlines()[0] if text.strip() else "capture"
    heading = _safe_fragment(title_hint or first_line)
    filename = f"{stamp}_{heading}.md"

    # 1. 添付画像を Attachments フォルダへアップロードし、Markdown リンク (attachment://fileId) を生成
    image_refs = []
    if images:
        attachments_folder_id = structure["attachments"]
        for i, (original_name, byte_data) in enumerate(images):
            # 競合（スマホから同じファイル名で複数送られた場合など）を避けるためにインデックス付きタイムスタンプを付与
            ext = original_name.split(".")[-1].lower() if "." in original_name else "jpg"
            dest_name = f"{stamp}_{i}_{original_name}"
            mime_type = f"image/{ext}" if ext in ["png", "jpg", "jpeg", "gif", "webp"] else "application/octet-stream"

            file_id = gdrive_client.upload_file_bytes(
                attachments_folder_id, dest_name, byte_data, mime_type
            )
            if file_id:
                image_refs.append(f"![](attachment://{file_id})")

    # 2. Markdown 本文の生成 (Python版完全互換フォーマット)
    content_lines = [
        f"# {heading}",
        "",
        f"作成日時: {now.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
    ]
    if text.strip():
        content_lines.append(text.strip())
        content_lines.append("")

    for ref in image_refs:
        content_lines.append(ref)
    content_lines.append("")

    # 3. Google ドライブへアップロード
    inbox_folder_id = structure["inbox"]
    markdown_content = "\n".join(content_lines)
    file_id = gdrive_client.upload_file_content(inbox_folder_id, filename, markdown_content)

    if not file_id:
        return None

    # 4. SQLite キャッシュ DB の整理状態を「未整理 (0)」に初期登録し本文もキャッシュ
    db = database.connect()
    try:
        with db:
            dt_str = now.strftime("%Y-%m-%d %H:%M:%S")
            db.execute(
                "INSERT OR REPLACE INTO inbox_cache (drive_file_id, file_name, title, date_time, content, organized) "
                "VALUES (?, ?, ?, ?, ?, 0)",
                (file_id, filename, heading, dt_str, markdown_content),
            )
        return file_id
    except sqlite3.Error as e:
        print(f"Failed to cache inbox status for {filename}: {e}")
        return file_id
    finally:
        db.close()


def append_capture(drive_file_id: str, append_text: str) -> bool:
    """既存のキャプチャの末尾に修正内容を追記し、整理状態を「未整理」に戻す。"""
    # 1. まずローカルの SQLite キャッシュから既存の content を高速取得
    db = database.connect()
    existing_content = ""
    try:
        cur = db.execute("SELECT content, file_name FROM inbox_cache WHERE drive_file_id = ?", (drive_file_id,))
        row = cur.fetchone()
        if row:
            existing_content = row["content"]
            filename = row["file_name"]
        else:
            # キャッシュに無い場合はドライブからロード
            existing_content = gdrive_client.download_file_content(drive_file_id)
            filename = "amended_capture.md"
    finally:
        db.close()

    if not existing_content:
        return False

    now = datetime.now()
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")

    # 追記する文字列 (Python互換)
    payload = f"\n\n修正日時: {now_str}\n\n{append_text.strip()}\n"
    new_content = existing_content.rstrip() + payload

    # 2. ローカルキャッシュを即座に更新
    db = database.connect()
    try:
        with db:
            db.execute(
                "UPDATE inbox_cache SET content = ?, organized = 0 WHERE drive_file_id = ?",
                (new_content, drive_file_id),
            )
    except sqlite3.Error as e:
        print(f"Failed to update inbox cache on append: {e}")
    finally:
        db.close()

    # 3. Google ドライブへの上書きはバックグラウンドスレッドで非同期に処理
    structure = gdrive_client.ensure_vault_structure()
    if not structure:
        return False
    inbox_folder_id = structure["inbox"]

    def upload_worker():
        try:
            gdrive_client.upload_file_content(
                inbox_folder_id, filename, new_content, file_id=drive_file_id
            )
        except Exception as e:
            print(f"Background inbox upload failed: {e}")

    threading.Thread(target=upload_worker, daemon=True).start()
    return True


def set_organized(drive_file_id: str, organized: bool) -> None:
    """指定されたキャプチャの整理状態を SQLite DB 上で更新する。"""
    db = database.connect()
    val = 1 if organized else 0
    try:
        with db:
            db.execute(
                "UPDATE inbox_cache SET organized = ? WHERE drive_file_id = ?",
                (val, drive_file_id),
            )
    except sqlite3.Error as e:
        print(f"Failed to update organized cache for {drive_file_id}: {e}")
    finally:
        db.close()


def get_unorganized_count() -> int:
    """未整理のキャプチャ件数をカウントする (SQLで一瞬で取得)。"""
    db = database.connect()
    try:
        cur = db.execute("SELECT COUNT(*) AS c FROM inbox_cache WHERE organized = 0")
        return cur.fetchone()["c"]
    except sqlite3.Error:
        return 0
    finally:
        db.close()
