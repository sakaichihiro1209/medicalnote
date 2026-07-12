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


def rebuild_inbox_cache(user_id: str | None = None) -> int:
    """Google ドライブ上の Inbox フォルダから全 Markdown 本文をスキャンし、SQLite キャッシュを再構築する。
    すでに整理済みのファイルは本文のダウンロードを完全にスキップして処理を極限まで高速化します。
    """
    from flask import has_request_context, session
    refresh_token = None
    if not user_id and has_request_context():
        user_id = session.get("google_user_id")
        refresh_token = session.get("google_refresh_token")

    if not refresh_token:
        try:
            refresh_token = gdrive_client._thread_local.refresh_token
        except AttributeError:
            pass

    service = gdrive_client.get_gdrive_service(refresh_token=refresh_token)
    if not service:
        return 0

    structure = gdrive_client.ensure_vault_structure(user_id=user_id)
    if not structure:
        return 0
    inbox_folder_id = structure["inbox"]

    files = gdrive_client.list_files_in_folder(inbox_folder_id)

    db = database.connect(user_id=user_id)
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
                    gdrive_client.set_thread_refresh_token(refresh_token)
                    text = gdrive_client.download_file_content(file_id) or ""
                    gdrive_client.clear_thread_refresh_token()

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


def list_captures(user_id: str | None = None) -> List[Dict]:
    """SQLite キャッシュから Inbox の全ファイルリストを取得する。未整理が上、整理済みが下に並びます。"""
    from flask import has_request_context, session
    is_sync = False
    if has_request_context():
        try:
            is_sync = session.get("vault_synchronized") == "true"
        except Exception:
            pass

    if not gdrive_client.get_credentials() or not is_sync:
        return []

    db = database.connect(user_id=user_id)
    try:
        cur = db.execute("SELECT COUNT(*) AS c FROM inbox_cache")
        count = cur.fetchone()["c"]
        if count == 0:
            db.close()
            rebuild_inbox_cache(user_id=user_id)
            db = database.connect(user_id=user_id)

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
    text: str, title_hint: str | None = None, images: List[Tuple[str, bytes]] | None = None, user_id: str | None = None
) -> str | None:
    """Google ドライブの Inbox/ フォルダに新規キャプチャを保存する。画像は Attachments/ フォルダへ。"""
    from flask import has_request_context, session
    refresh_token = None
    if not user_id and has_request_context():
        user_id = session.get("google_user_id")
        refresh_token = session.get("google_refresh_token")

    if not refresh_token:
        try:
            refresh_token = gdrive_client._thread_local.refresh_token
        except AttributeError:
            pass

    service = gdrive_client.get_gdrive_service(refresh_token=refresh_token)
    if not service:
        return None

    structure = gdrive_client.ensure_vault_structure(user_id=user_id)
    if not structure:
        return None
    inbox_folder_id = structure["inbox"]
    attachments_folder_id = structure["attachments"]

    now = datetime.now()
    stamp = now.strftime("%Y%m%d_%H%M%S")

    # 見出し語の決定
    first_line = text.strip().splitlines()[0] if text.strip() else "capture"
    heading = _safe_fragment(title_hint or first_line)
    filename = f"{stamp}_{heading}.md"

    # 1. 添付画像を Attachments フォルダへアップロードし、Markdown リンク (attachment://fileId) を生成
    image_refs = []
    if images:
        gdrive_client.set_thread_refresh_token(refresh_token)
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
        gdrive_client.clear_thread_refresh_token()

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
    markdown_content = "\n".join(content_lines)
    gdrive_client.set_thread_refresh_token(refresh_token)
    file_id = gdrive_client.upload_file_content(inbox_folder_id, filename, markdown_content)
    gdrive_client.clear_thread_refresh_token()

    if not file_id:
        return None

    # 4. SQLite キャッシュ DB の整理状態を「未整理 (0)」に初期登録し本文もキャッシュ
    db = database.connect(user_id=user_id)
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


def edit_capture(
    drive_file_id: str, new_title: str, new_text: str, new_images: List[Tuple[str, bytes]] | None = None, user_id: str | None = None
) -> bool:
    """既存のキャプチャを丸ごと上書き保存し、整理状態を「未整理」に戻す。"""
    from flask import has_request_context, session
    import re
    refresh_token = None
    if not user_id and has_request_context():
        user_id = session.get("google_user_id")
        refresh_token = session.get("google_refresh_token")

    if not refresh_token:
        try:
            refresh_token = gdrive_client._thread_local.refresh_token
        except AttributeError:
            pass

    # 1. まずローカルの SQLite キャッシュから既存の content とファイル名を取得
    db = database.connect(user_id=user_id)
    existing_content = ""
    filename = "edited_capture.md"
    try:
        cur = db.execute("SELECT content, file_name FROM inbox_cache WHERE drive_file_id = ?", (drive_file_id,))
        row = cur.fetchone()
        if row:
            existing_content = row["content"]
            filename = row["file_name"]
        else:
            # キャッシュに無い場合はドライブからロード
            gdrive_client.set_thread_refresh_token(refresh_token)
            existing_content = gdrive_client.download_file_content(drive_file_id) or ""
            gdrive_client.clear_thread_refresh_token()
    finally:
        db.close()

    # 作成日時のパース (existing_contentから抽出)
    created_at_line = ""
    created_match = re.search(r"^作成日時:\s*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})", existing_content, re.MULTILINE)
    if created_match:
        created_at_line = f"作成日時: {created_match.group(1)}"
    else:
        created_at_line = f"作成日時: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"

    # 既存の画像リンク ![](attachment://file_id) を抽出して退避
    existing_image_links = re.findall(r"\!\[\]\(attachment://[a-zA-Z0-9_-]+\)", existing_content)

    now = datetime.now()
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    stamp = now.strftime("%Y%m%d_%H%M%S")

    # 新しい見出し
    heading = _safe_fragment(new_title or "capture")
    
    # ファイル名も新しいタイトルに基づいて更新 (タイムスタンプ部分はそのまま維持)
    if "_" in filename:
        parts = filename.split("_", 1)
        filename = f"{parts[0]}_{heading}.md"
    else:
        filename = f"{stamp}_{heading}.md"

    # 新しい画像のアップロード
    new_image_refs = []
    if new_images:
        gdrive_client.set_thread_refresh_token(refresh_token)
        structure = gdrive_client.ensure_vault_structure(user_id=user_id)
        attachments_folder_id = structure["attachments"] if structure else None
        if attachments_folder_id:
            for i, (original_name, byte_data) in enumerate(new_images):
                ext = original_name.split(".")[-1].lower() if "." in original_name else "jpg"
                dest_name = f"{stamp}_{i}_{original_name}"
                mime_type = f"image/{ext}" if ext in ["png", "jpg", "jpeg", "gif", "webp"] else "application/octet-stream"
                file_id = gdrive_client.upload_file_bytes(
                    attachments_folder_id, dest_name, byte_data, mime_type
                )
                if file_id:
                    new_image_refs.append(f"![](attachment://{file_id})")
        gdrive_client.clear_thread_refresh_token()

    # Markdown 本文を再構成 (上書き)
    content_lines = [
        f"# {heading}",
        "",
        created_at_line,
        f"修正日時: {now_str}",
        "",
    ]
    if new_text.strip():
        content_lines.append(new_text.strip())
        content_lines.append("")

    # 既存の画像と、新しい画像をマージ
    all_images = existing_image_links + new_image_refs
    for img in all_images:
        content_lines.append(img)
    content_lines.append("")

    new_content = "\n".join(content_lines)

    # 2. ローカルキャッシュを即座に更新
    db = database.connect(user_id=user_id)
    try:
        with db:
            db.execute(
                "UPDATE inbox_cache SET content = ?, title = ?, file_name = ?, organized = 0 WHERE drive_file_id = ?",
                (new_content, heading, filename, drive_file_id),
            )
    except sqlite3.Error as e:
        print(f"Failed to update inbox cache on edit: {e}")
    finally:
        db.close()

    # 3. Google ドライブへの上書きはバックグラウンドスレッドで非同期に処理
    def upload_worker():
        try:
            gdrive_client.set_thread_refresh_token(refresh_token)
            service = gdrive_client.get_gdrive_service(refresh_token=refresh_token)
            if not service:
                return
            structure = gdrive_client.ensure_vault_structure(user_id=user_id)
            if not structure:
                return
            inbox_folder_id = structure["inbox"]
            
            gdrive_client.upload_file_content(
                inbox_folder_id, filename, new_content, file_id=drive_file_id
            )
            gdrive_client.clear_thread_refresh_token()
        except Exception as e:
            print(f"Background inbox upload failed: {e}")

    threading.Thread(target=upload_worker, daemon=True).start()
    return True


def set_organized(drive_file_id: str, organized: bool, user_id: str | None = None) -> None:
    """指定されたキャプチャの整理状態を SQLite DB 上で更新する。"""
    db = database.connect(user_id=user_id)
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


def get_unorganized_count(user_id: str | None = None) -> int:
    """未整理のキャプチャ件数をカウントする (SQLで一瞬で取得)。"""
    db = database.connect(user_id=user_id)
    try:
        cur = db.execute("SELECT COUNT(*) AS c FROM inbox_cache WHERE organized = 0")
        return cur.fetchone()["c"]
    except sqlite3.Error:
        return 0
    finally:
        db.close()
