"""
Knowledge Card のビジネスロジックと Google Drive ＆ SQLite キャッシュ間のデータ同期を行うモジュール。
"""

import json
import sqlite3
import threading
from typing import List, Dict, Tuple, Optional
from . import database
from . import gdrive_client
from . import markdown_parser
from . import settings

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
    """Google ドライブ上の Knowledge/ フォルダ内の Markdown ファイルから SQLite キャッシュを再構築する。"""
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

        # トランザクション処理
        with db:
            # 1. 既存のナレッジキャッシュをクリア
            db.execute("DELETE FROM knowledge")

            # 2. 全ファイルを走査してDBへ登録 & セクション出現数をカウント
            for file_info in files:
                name = file_info.get("name", "")
                if not name.lower().endswith(".md"):
                    continue

                file_id = file_info["id"]
                text = gdrive_client.download_file_content(file_id)
                if not text:
                    skipped += 1
                    continue

                # Markdownパース
                doc = markdown_parser.parse_markdown(text)
                title = doc.title or name[:-3]  # タイトルが空なら拡張子を除いたファイル名

                if not title:
                    skipped += 1
                    continue

                # セクション出現カウントの加算
                for sec in doc.sections:
                    section_counts[sec.name] = section_counts.get(sec.name, 0) + 1

                try:
                    db.execute(
                        "INSERT OR REPLACE INTO knowledge (title, drive_file_id, content, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (title, file_id, text, ts, ts),
                    )
                    restored += 1
                except sqlite3.Error as e:
                    print(f"Error caching card {title}: {e}")
                    skipped += 1

            # 3. section_master の usage_count をリセットして更新
            db.execute("UPDATE section_master SET usage_count = 0")
            for sec_name, count in section_counts.items():
                # すでに存在するマスター名かチェック
                cur = db.execute(
                    "SELECT id, color FROM section_master WHERE section_name = ?", (sec_name,)
                )
                row = cur.fetchone()
                if row:
                    # すでに色があれば維持、無ければ新しく割り当てる
                    color = row["color"]
                    if not color:
                        color = _allocate_color_in_transaction(db, sec_name)
                    db.execute(
                        "UPDATE section_master SET usage_count = ?, color = ?, updated_at = ? WHERE id = ?",
                        (count, color, ts, row["id"]),
                    )
                else:
                    # 新規登録
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
    """指定されたタイトルで Google ドライブ上に空の Markdown を新規作成し、DBキャッシュに追加する。"""
    # 既存チェック
    db = database.connect()
    try:
        cur = db.execute("SELECT id FROM knowledge WHERE title = ?", (title,))
        if cur.fetchone():
            return None  # すでに同名のカードが存在する
    finally:
        db.close()

    structure = gdrive_client.ensure_vault_structure()
    if not structure:
        return None

    # 空のドキュメントを書き出し
    doc = markdown_parser.KnowledgeDocument(title=title)
    text = markdown_parser.render_markdown(doc)

    parent_id = structure["knowledge"]
    filename = f"{title}.md"
    file_id = gdrive_client.upload_file_content(parent_id, filename, text)

    if not file_id:
        return None

    # キャッシュの保存
    db = database.connect()
    ts = database.now_iso()
    try:
        with db:
            db.execute(
                "INSERT OR REPLACE INTO knowledge (title, drive_file_id, content, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (title, file_id, text, ts, ts),
            )
        return file_id
    except sqlite3.Error as e:
        print(f"Failed to cache new card {title}: {e}")
        return file_id
    finally:
        db.close()


def save_card(drive_file_id: str, doc: markdown_parser.KnowledgeDocument) -> bool:
    """カードの中身 (KnowledgeDocument) を SQLite キャッシュに即座に保存し、Google ドライブへはバックグラウンドで非同期アップロードする。"""
    structure = gdrive_client.ensure_vault_structure()
    if not structure:
        return False

    text = markdown_parser.render_markdown(doc)
    parent_id = structure["knowledge"]
    filename = f"{doc.title}.md"

    # 1. ローカルの SQLite キャッシュDBを即時（ゼロ遅延）更新
    db = database.connect()
    ts = database.now_iso()
    try:
        with db:
            db.execute(
                "UPDATE knowledge SET title = ?, content = ?, updated_at = ? WHERE drive_file_id = ?",
                (doc.title, text, ts, drive_file_id),
            )
    except sqlite3.Error as e:
        print(f"Failed to update cache on save for {doc.title}: {e}")
        # キャッシュの更新失敗時は続行するが警告
    finally:
        db.close()

    # 2. Google ドライブへのアップロード処理をバックグラウンドスレッドで非同期に実行
    # ユーザーへの応答速度を極限まで速くするため、アップロード完了を待たずに即時 True を返す
    def upload_worker():
        try:
            gdrive_client.upload_file_content(
                parent_id, filename, text, file_id=drive_file_id
            )
        except Exception as e:
            print(f"Background upload failed for {filename}: {e}")

    threading.Thread(target=upload_worker, daemon=True).start()
    return True


def delete_card(drive_file_id: str) -> bool:
    """Google ドライブおよび SQLite キャッシュからカードを削除する。"""
    # ドライブから削除
    drive_deleted = gdrive_client.delete_file(drive_file_id)
    if not drive_deleted:
        # すでに削除されている可能性もあるため、DB側も消去処理を続行
        pass

    db = database.connect()
    try:
        with db:
            db.execute("DELETE FROM knowledge WHERE drive_file_id = ?", (drive_file_id,))
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
