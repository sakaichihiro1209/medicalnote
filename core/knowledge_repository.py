"""
Knowledge Card のビジネスロジックと Google Drive ＆ SQLite キャッシュ間のデータ同期を行うモジュール。
"""

import json
import sqlite3
import threading
import queue
from datetime import datetime, timezone
from typing import List, Dict, Tuple, Optional
from . import database
from . import gdrive_client
from . import markdown_parser
from . import settings

# --- バックグラウンド同期キューとシリアルワーカーの実装 ---
_sync_queue: queue.Queue = queue.Queue()

def _parse_iso(s: str) -> datetime:
    s_norm = s.replace("Z", "+00:00")
    return datetime.fromisoformat(s_norm)

def _is_drive_newer(drive_ts: str, local_ts: str) -> bool:
    try:
        d_dt = _parse_iso(drive_ts)
        l_dt = _parse_iso(local_ts)
        if d_dt.tzinfo is not None:
            d_dt = d_dt.astimezone(timezone.utc).replace(tzinfo=None)
        if l_dt.tzinfo is not None:
            l_dt = l_dt.astimezone(timezone.utc).replace(tzinfo=None)
        return d_dt > l_dt
    except Exception as e:
        print(f"Timestamp compare error ({drive_ts} vs {local_ts}): {e}")
        return True

def _process_async_save(payload):
    drive_file_id = payload["drive_file_id"]
    content = payload["content"]
    local_updated_at = payload["local_updated_at"]
    
    # 競合判定チェック (ドライブ側が新しければダウンロードして上書き、アップロードはスキップ)
    service = gdrive_client.get_gdrive_service()
    if not service:
        return
    try:
        file_meta = service.files().get(fileId=drive_file_id, fields="modifiedTime").execute()
        drive_modified = file_meta.get("modifiedTime")
        if drive_modified and _is_drive_newer(drive_modified, local_updated_at):
            print(f"[Sync Worker] Conflict detected for {drive_file_id}. Overwriting local cache with drive changes.")
            drive_text = gdrive_client.download_file_content(drive_file_id)
            if drive_text:
                doc = markdown_parser.parse_markdown(drive_text)
                db = database.connect()
                try:
                    with db:
                        db.execute(
                            "UPDATE knowledge SET title = ?, content = ?, updated_at = ?, dirty = 0 WHERE drive_file_id = ?",
                            (doc.title, drive_text, drive_modified, drive_file_id)
                        )
                except Exception as ex:
                    print(f"[Sync Worker] Failed to resolve conflict: {ex}")
                finally:
                    db.close()
            return
    except Exception as e:
        print(f"[Sync Worker] Failed conflict check: {e}")

    structure = gdrive_client.ensure_vault_structure()
    if not structure:
        return
    parent_id = structure["knowledge"]
    
    db = database.connect()
    try:
        cur = db.execute("SELECT title FROM knowledge WHERE drive_file_id = ?", (drive_file_id,))
        row = cur.fetchone()
        filename = f"{row['title']}.md" if row else "untitled.md"
    finally:
        db.close()

    uploaded_id = gdrive_client.upload_file_content(parent_id, filename, content, file_id=drive_file_id)
    if uploaded_id:
        try:
            file_meta = service.files().get(fileId=drive_file_id, fields="modifiedTime").execute()
            new_drive_modified = file_meta.get("modifiedTime")
            if new_drive_modified:
                db = database.connect()
                try:
                    with db:
                        db.execute(
                            "UPDATE knowledge SET updated_at = ?, dirty = 0 WHERE drive_file_id = ?",
                            (new_drive_modified, drive_file_id)
                        )
                finally:
                    db.close()
        except Exception as e:
            print(f"[Sync Worker] Post-save metadata sync failed: {e}")

def _process_async_create(payload):
    temp_file_id = payload["temp_file_id"]
    title = payload["title"]
    content = payload["content"]
    
    structure = gdrive_client.ensure_vault_structure()
    if not structure:
        return
    parent_id = structure["knowledge"]
    filename = f"{title}.md"
    
    real_file_id = gdrive_client.upload_file_content(parent_id, filename, content)
    if real_file_id:
        service = gdrive_client.get_gdrive_service()
        drive_modified = None
        if service:
            try:
                file_meta = service.files().get(fileId=real_file_id, fields="modifiedTime").execute()
                drive_modified = file_meta.get("modifiedTime")
            except Exception as e:
                print(f"[Sync Worker] Failed metadata check on create: {e}")
        
        ts = drive_modified if drive_modified else database.now_iso()
        db = database.connect()
        try:
            with db:
                db.execute(
                    "UPDATE knowledge SET drive_file_id = ?, updated_at = ?, dirty = 0 WHERE drive_file_id = ?",
                    (real_file_id, ts, temp_file_id)
                )
        except sqlite3.Error as e:
            print(f"[Sync Worker] Failed to update temporary card cache: {e}")
        finally:
            db.close()

def _process_async_delete(payload):
    drive_file_id = payload["drive_file_id"]
    gdrive_client.delete_file(drive_file_id)

def _sync_worker():
    while True:
        try:
            task = _sync_queue.get()
            if task is None:
                break
            action = task.get("action")
            payload = task.get("payload")
            if action == "save":
                _process_async_save(payload)
            elif action == "create":
                _process_async_create(payload)
            elif action == "delete":
                _process_async_delete(payload)
            _sync_queue.task_done()
        except Exception as e:
            print(f"[Sync Worker] Sync worker error: {e}")

# シリアル同期ワーカーのスレッドを常時起動
threading.Thread(target=_sync_worker, daemon=True).start()
# ----------------------------------------------------

PASTEL_COLORS = [
    "sec-col-gaiyou",   # 概要
    "sec-col-shindan",  # 診断
    "sec-col-chiryou",  # 治療
    "sec-col-shoujou",  # 症状
    "sec-col-shofou",   # 処方
    "sec-col-kensa",    # 検査
    "sec-col-0", "sec-col-1", "sec-col-2", "sec-col-3", "sec-col-4", "sec-col-5",
    "sec-col-6", "sec-col-7", "sec-col-8", "sec-col-9", "sec-col-10", "sec-col-11"
]


def rebuild_cache_from_gdrive() -> Dict[str, int]:
    """Google ドライブ上の Knowledge/ フォルダ内の Markdown ファイルから SQLite キャッシュを差分同期する。"""
    structure = gdrive_client.ensure_vault_structure()
    if not structure:
        return {"restored": 0, "skipped": 0}

    knowledge_folder_id = structure["knowledge"]
    files = gdrive_client.list_files_in_folder(knowledge_folder_id)

    db = database.connect()
    ts = database.now_iso()
    restored = 0
    skipped = 0
    section_counts: Dict[str, int] = {}

    try:
        # トランザクション外でドライブ上のセクション色情報を先にローカルDBへ同期
        sync_section_colors_from_gdrive(db)

        # 現在の SQLite キャッシュ上のファイル情報を取得
        cur = db.execute("SELECT drive_file_id, updated_at, dirty FROM knowledge")
        local_cache = {row["drive_file_id"]: {"updated_at": row["updated_at"], "dirty": row["dirty"]} for row in cur.fetchall()}

        drive_file_ids = set()

        # トランザクション処理
        with db:
            for file_info in files:
                name = file_info.get("name", "")
                if not name.lower().endswith(".md"):
                    continue

                file_id = file_info["id"]
                drive_file_ids.add(file_id)
                drive_modified = file_info.get("modifiedTime")

                # 差分チェック
                is_changed = True
                if file_id in local_cache:
                    cache_info = local_cache[file_id]
                    # 未同期のローカル変更がある (dirty == 1) 場合は、ドライブからの上書きを防ぐ
                    if cache_info["dirty"] == 1:
                        is_changed = False
                        # ただし、バッティング時にドライブの方が新しければ上書きする
                        if drive_modified and _is_drive_newer(drive_modified, cache_info["updated_at"]):
                            is_changed = True
                    elif drive_modified:
                        # ドライブ側の modifiedTime がローカルの updated_at より新しくなければ、スキップ
                        if not _is_drive_newer(drive_modified, cache_info["updated_at"]):
                            is_changed = False

                if not is_changed:
                    skipped += 1
                    # スキップした場合でも、既存キャッシュからセクションのカウント集計を行う
                    cur_card = db.execute("SELECT content, title FROM knowledge WHERE drive_file_id = ?", (file_id,))
                    card_row = cur_card.fetchone()
                    if card_row and card_row["content"]:
                        doc = markdown_parser.parse_markdown(card_row["content"])
                        for sec in doc.sections:
                            section_counts[sec.name] = section_counts.get(sec.name, 0) + 1
                    continue

                text = gdrive_client.download_file_content(file_id)
                if not text:
                    skipped += 1
                    continue

                # Markdownパース
                doc = markdown_parser.parse_markdown(text)
                title = doc.title or name[:-3]

                if not title:
                    skipped += 1
                    continue

                # セクション出現カウントの加算
                for sec in doc.sections:
                    section_counts[sec.name] = section_counts.get(sec.name, 0) + 1

                try:
                    db.execute(
                        "INSERT OR REPLACE INTO knowledge (title, drive_file_id, content, created_at, updated_at, dirty) "
                        "VALUES (?, ?, ?, ?, ?, 0)",
                        (title, file_id, text, ts, drive_modified if drive_modified else ts),
                    )
                    restored += 1
                except sqlite3.Error as e:
                    print(f"Error caching card {title}: {e}")
                    skipped += 1

            # ドライブ上で削除された（drive_file_ids に含まれない）ファイルを SQLite から削除
            for cached_id in local_cache.keys():
                if cached_id not in drive_file_ids:
                    # 仮 ID (temp_ で始まる) は削除しない（まだアップロードされていない新規作成ノート）
                    if not cached_id.startswith("temp_"):
                        db.execute("DELETE FROM knowledge WHERE drive_file_id = ?", (cached_id,))

            # section_master の usage_count をリセットして更新
            db.execute("UPDATE section_master SET usage_count = 0")
            for sec_name, count in section_counts.items():
                cur = db.execute(
                    "SELECT id, color FROM section_master WHERE section_name = ?", (sec_name,)
                )
                row = cur.fetchone()
                if row:
                    color = row["color"]
                    if not color:
                        color = _allocate_color_in_transaction(db, sec_name)
                    db.execute(
                        "UPDATE section_master SET usage_count = ?, color = ?, updated_at = ? WHERE id = ?",
                        (count, color, ts, row["id"]),
                    )
                else:
                    color = _allocate_color_in_transaction(db, sec_name)
                    db.execute(
                        "INSERT INTO section_master (section_name, color, usage_count, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (sec_name, color, count, ts, ts),
                    )

        # 割り当てた新しい色情報も含めてドライブ側へ上書き保存
        save_section_colors_to_gdrive()

        return {"restored": restored, "skipped": skipped}
    except Exception as e:
        print(f"Rebuild cache failed: {e}")
        return {"restored": restored, "skipped": skipped}
    finally:
        db.close()


def list_cards(query: str = "") -> List[Dict]:
    """SQLite キャッシュからカード一覧を取得する。検索文字列 (query) による部分一致に対応。"""
    # Google ログインしていない状態、または同期が完了するまではキャッシュを読み込まない
    if not gdrive_client.get_credentials() or settings.get("VAULT_SYNCHRONIZED") != "true":
        return []

    db = database.connect()
    try:
        if query.strip():
            # タイトルによる部分一致検索
            cur = db.execute(
                "SELECT * FROM knowledge WHERE title LIKE ? ORDER BY title COLLATE NOCASE ASC",
                (f"%{query.strip()}%",),
            )
        else:
            cur = db.execute("SELECT * FROM knowledge ORDER BY title COLLATE NOCASE ASC")
        return [dict(row) for row in cur.fetchall()]
    finally:
        db.close()


def get_card_by_id(drive_file_id: str) -> Tuple[Optional[markdown_parser.KnowledgeDocument], Optional[Dict]]:
    """指定された ID のカードをキャッシュDB（フォールバックでGoogleドライブ）から読み込んでパースしたオブジェクトとDB情報を取得する。"""
    # Google ログインしていない状態、または同期が完了するまではキャッシュを読み込まない
    if not gdrive_client.get_credentials() or settings.get("VAULT_SYNCHRONIZED") != "true":
        return None, None

    db = database.connect()
    try:
        cur = db.execute(
            "SELECT * FROM knowledge WHERE drive_file_id = ?", (drive_file_id,)
        )
        row = cur.fetchone()
        info = dict(row) if row else None

        if info and info.get("content"):
            # キャッシュDBから超高速読み込み
            text = info["content"]
        else:
            # キャッシュに本文が無い場合はGoogleドライブからダウンロード (フォールバック)
            text = gdrive_client.download_file_content(drive_file_id)
            if text and info:
                # 今後のためにキャッシュDBへ本文を保存
                ts = database.now_iso()
                db.execute(
                    "UPDATE knowledge SET content = ?, updated_at = ? WHERE drive_file_id = ?",
                    (text, ts, drive_file_id),
                )
                db.commit()

        if not text:
            return None, info

        doc = markdown_parser.parse_markdown(text)
        return doc, info
    finally:
        db.close()


def create_card(title: str) -> str | None:
    """指定されたタイトルで SQLite キャッシュに即時仮登録し、Google ドライブへは非同期で新規作成する。"""
    # 既存チェック
    db = database.connect()
    try:
        cur = db.execute("SELECT id FROM knowledge WHERE title = ?", (title,))
        if cur.fetchone():
            return None  # すでに同名のカードが存在する
    finally:
        db.close()

    # 一意の仮IDを生成
    import uuid
    temp_id = f"temp_{uuid.uuid4().hex}"

    # 空のドキュメントを作成
    doc = markdown_parser.KnowledgeDocument(title=title)
    text = markdown_parser.render_markdown(doc)

    db = database.connect()
    ts = database.now_iso()
    try:
        with db:
            db.execute(
                "INSERT OR REPLACE INTO knowledge (title, drive_file_id, content, created_at, updated_at, dirty) "
                "VALUES (?, ?, ?, ?, ?, 1)",
                (title, temp_id, text, ts, ts),
            )
        
        # ドライブ作成ジョブをキューに積む
        _sync_queue.put({
            "action": "create",
            "payload": {
                "temp_file_id": temp_id,
                "title": title,
                "content": text
            }
        })
        return temp_id
    except sqlite3.Error as e:
        print(f"Failed to cache new card {title}: {e}")
        return None
    finally:
        db.close()


def save_card(drive_file_id: str, doc: markdown_parser.KnowledgeDocument) -> bool:
    """カードの中身 (KnowledgeDocument) を SQLite キャッシュに即座に保存し、Google ドライブへは非同期アップロードキューに入れる。"""
    text = markdown_parser.render_markdown(doc)

    # 1. ローカルの SQLite キャッシュDBを即時（ゼロ遅延）更新
    db = database.connect()
    ts = database.now_iso()
    try:
        with db:
            db.execute(
                "UPDATE knowledge SET title = ?, content = ?, updated_at = ?, dirty = 1 WHERE drive_file_id = ?",
                (doc.title, text, ts, drive_file_id),
            )
    except sqlite3.Error as e:
        print(f"Failed to update cache on save for {doc.title}: {e}")
        return False
    finally:
        db.close()

    # 2. ドライブ同期ジョブをキューに追加
    _sync_queue.put({
        "action": "save",
        "payload": {
            "drive_file_id": drive_file_id,
            "content": text,
            "local_updated_at": ts
        }
    })
    return True


def delete_card(drive_file_id: str) -> bool:
    """SQLite キャッシュから即時削除し、Google ドライブからは非同期で物理削除する。"""
    db = database.connect()
    try:
        with db:
            db.execute("DELETE FROM knowledge WHERE drive_file_id = ?", (drive_file_id,))
        
        # ドライブ削除ジョブをキューに追加
        _sync_queue.put({
            "action": "delete",
            "payload": {
                "drive_file_id": drive_file_id
            }
        })
        return True
    except sqlite3.Error as e:
        print(f"Failed to delete card cache {drive_file_id}: {e}")
        return False
    finally:
        db.close()


def get_suggested_sections() -> List[str]:
    """使用頻度の高い順 (usage_count 降順) でセクションの候補リストを取得する。"""
    db = database.connect()
    try:
        cur = db.execute(
            "SELECT section_name FROM section_master ORDER BY usage_count DESC, section_name ASC"
        )
        return [row["section_name"] for row in cur.fetchall()]
    finally:
        db.close()


def increment_section_usage(section_name: str) -> None:
    """指定されたセクションの使用頻度 (usage_count) を +1 する。"""
    db = database.connect()
    ts = database.now_iso()
    try:
        with db:
            # 存在チェック
            cur = db.execute(
                "SELECT id, usage_count FROM section_master WHERE section_name = ?",
                (section_name,),
            )
            row = cur.fetchone()
            if row:
                db.execute(
                    "UPDATE section_master SET usage_count = ?, updated_at = ? WHERE id = ?",
                    (row["usage_count"] + 1, ts, row["id"]),
                )
            else:
                db.execute(
                    "INSERT INTO section_master (section_name, usage_count, created_at, updated_at) "
                    "VALUES (?, 1, ?, ?)",
                    (section_name, ts, ts),
                )
    except sqlite3.Error as e:
        print(f"Error incrementing section usage for {section_name}: {e}")
    finally:
        db.close()


def check_cache_empty() -> bool:
    """SQLiteのキャッシュDBが空かどうかを確認する（自動再構築判定用）。"""
    db = database.connect()
    try:
        cur = db.execute("SELECT COUNT(*) AS c FROM knowledge")
        return cur.fetchone()["c"] == 0
    except sqlite3.Error:
        return True
    finally:
        db.close()


def sync_section_colors_from_gdrive(db=None) -> None:
    """Googleドライブ上の system/section_colors.json からセクション名とカラーのマッピングを同期する。"""
    structure = gdrive_client.ensure_vault_structure()
    if not structure or "system" not in structure:
        return

    system_folder_id = structure["system"]
    files = gdrive_client.list_files_in_folder(system_folder_id)

    # section_colors.jsonを探す
    json_file_id = None
    for f in files:
        if f["name"] == "section_colors.json":
            json_file_id = f["id"]
            break

    if not json_file_id:
        return

    text = gdrive_client.download_file_content(json_file_id)
    if not text:
        return

    try:
        data = json.loads(text)
        colors = data.get("section_colors", {})

        # SQLite側へ反映
        local_db = db or database.connect()
        ts = database.now_iso()
        try:
            # トランザクション処理
            with local_db:
                for name, color in colors.items():
                    # 既に存在するかチェック
                    cur = local_db.execute("SELECT id FROM section_master WHERE section_name = ?", (name,))
                    row = cur.fetchone()
                    if row:
                        local_db.execute(
                            "UPDATE section_master SET color = ?, updated_at = ? WHERE id = ?",
                            (color, ts, row["id"])
                        )
                    else:
                        local_db.execute(
                            "INSERT INTO section_master (section_name, color, usage_count, created_at, updated_at) "
                            "VALUES (?, ?, 0, ?, ?)",
                            (name, color, ts, ts)
                        )
        finally:
            if not db:
                local_db.close()
    except Exception as e:
        print(f"Error syncing section colors from gdrive: {e}")


def save_section_colors_to_gdrive() -> None:
    """現在の SQLite 内のセクション色マッピングを Google ドライブ上の system/section_colors.json へアップロード保存する。"""
    structure = gdrive_client.ensure_vault_structure()
    if not structure or "system" not in structure:
        return

    # 現在のマッピングをDBから取得
    db = database.connect()
    try:
        cur = db.execute("SELECT section_name, color FROM section_master WHERE color IS NOT NULL")
        colors = {row["section_name"]: row["color"] for row in cur.fetchall()}
    finally:
        db.close()

    if not colors:
        return

    system_folder_id = structure["system"]
    files = gdrive_client.list_files_in_folder(system_folder_id)

    # 既存のファイルを探す
    json_file_id = None
    for f in files:
        if f["name"] == "section_colors.json":
            json_file_id = f["id"]
            break

    content_data = {"section_colors": colors}
    content_str = json.dumps(content_data, ensure_ascii=False, indent=4)

    # アップロード
    gdrive_client.upload_file_content(system_folder_id, "section_colors.json", content_str, file_id=json_file_id)


def get_or_create_section_color(section_name: str) -> str:
    """指定されたセクションの色を取得する。未定義の場合は新しく一意な色を割り当てて保存する。"""
    db = database.connect()
    try:
        # DBから色を探す
        cur = db.execute("SELECT color FROM section_master WHERE section_name = ?", (section_name,))
        row = cur.fetchone()
        if row and row["color"]:
            return row["color"]

        # 未定義なので新しく割り当てる
        # 現在使用中の色をリストアップ
        cur = db.execute("SELECT color FROM section_master WHERE color IS NOT NULL")
        used_colors = {r["color"] for r in cur.fetchall()}

        # 18色パレットから現在使われていないものを選択
        allocated_color = None
        for color in PASTEL_COLORS:
            if color not in used_colors:
                allocated_color = color
                break

        # もし全ての色が使われていれば、ハッシュ計算で決定的に割り当てる (重複許容)
        if not allocated_color:
            h = sum(ord(c) for c in section_name) % 12
            allocated_color = f"sec-col-{h}"

        # DBへ保存
        ts = database.now_iso()
        with db:
            cur = db.execute("SELECT id, usage_count FROM section_master WHERE section_name = ?", (section_name,))
            row = cur.fetchone()
            if row:
                db.execute(
                    "UPDATE section_master SET color = ?, updated_at = ? WHERE id = ?",
                    (allocated_color, ts, row["id"])
                )
            else:
                db.execute(
                    "INSERT INTO section_master (section_name, color, usage_count, created_at, updated_at) "
                    "VALUES (?, ?, 0, ?, ?)",
                    (section_name, allocated_color, ts, ts)
                )

        # ドライブに保存
        save_section_colors_to_gdrive()

        return allocated_color
    except sqlite3.Error as e:
        print(f"Error getting/creating section color for {section_name}: {e}")
        # フォールバック
        h = sum(ord(c) for c in section_name) % 12
        return f"sec-col-{h}"
    finally:
        db.close()


def _allocate_color_in_transaction(db, sec_name: str) -> str:
    """同一DBトランザクション内で新しく一意な色を割り当てる内部用関数。"""
    cur = db.execute("SELECT color FROM section_master WHERE color IS NOT NULL")
    used_colors = {r["color"] for r in cur.fetchall()}
    allocated_color = None
    for color in PASTEL_COLORS:
        if color not in used_colors:
            allocated_color = color
            break
    if not allocated_color:
        h = sum(ord(c) for c in sec_name) % 12
        allocated_color = f"sec-col-{h}"
    return allocated_color
