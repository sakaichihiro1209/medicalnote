"""
Google Drive API の接続と基本的な CRUD 操作をカプセル化するモジュール。
"""

import io
import os
from flask import session, has_request_context
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload
from . import settings

# フォルダ構造のIDキャッシュ（メモリ上。サーバー起動中にキャッシュし毎回のリスト検索を防ぐ）
_VAULT_STRUCTURE = {}


def get_credentials() -> Credentials | None:
    """OAuth2 認証用の Credentials を取得する。"""
    client_id = settings.get("GOOGLE_CLIENT_ID")
    client_secret = settings.get("GOOGLE_CLIENT_SECRET")
    
    # セッション内のリフレッシュトークンを優先（Web画面からの連携を動的にサポートするため）
    # リクエストコンテキストが存在する場合のみ Flask の session に安全にアクセスする
    refresh_token = None
    if has_request_context():
        try:
            refresh_token = session.get("google_refresh_token")
        except Exception:
            pass

    if not refresh_token:
        refresh_token = settings.get("GOOGLE_REFRESH_TOKEN")

    if not client_id or not client_secret or not refresh_token:
        return None

    return Credentials(
        token=None,
        refresh_token=refresh_token,
        client_id=client_id,
        client_secret=client_secret,
        token_uri="https://oauth2.googleapis.com/token",
        scopes=["https://www.googleapis.com/auth/drive.file"],
    )


def get_gdrive_service():
    """Drive API サービスインスタンスを構築する。"""
    creds = get_credentials()
    if not creds:
        return None
    # google-auth ライブラリが期限切れトークンを自動で更新する
    return build("drive", "v3", credentials=creds)


def get_or_create_folder(service, parent_id: str | None, name: str) -> str:
    """指定した親フォルダ配下で名前一致するフォルダを探す。無ければ自動作成する。"""
    query = f"mimeType = 'application/vnd.google-apps.folder' and name = '{name}' and trashed = false"
    if parent_id:
        query += f" and '{parent_id}' in parents"
    else:
        query += " and 'root' in parents"

    results = service.files().list(q=query, fields="files(id, name)").execute()
    files = results.get("files", [])
    if files:
        return files[0]["id"]

    # 新規作成
    folder_metadata = {
        "name": name,
        "mimeType": "application/vnd.google-apps.folder",
    }
    if parent_id:
        folder_metadata["parents"] = [parent_id]

    folder = service.files().create(body=folder_metadata, fields="id").execute()
    return folder["id"]


def ensure_vault_structure() -> dict | None:
    """Google ドライブ上の Vault フォルダ構造 (My_Vault/{Knowledge,Inbox,Attachments}) を保証する。"""
    global _VAULT_STRUCTURE
    if _VAULT_STRUCTURE:
        return _VAULT_STRUCTURE

    service = get_gdrive_service()
    if not service:
        return None

    try:
        # 1. ルートフォルダIDの確定
        vault_root_id = settings.get("GDRIVE_VAULT_FOLDER_ID")
        if not vault_root_id:
            # 環境変数指定がない場合は、マイドライブ直下に「My_Vault」を自動保証
            vault_root_id = get_or_create_folder(service, None, "My_Vault")

        # 2. 各サブフォルダの確定
        knowledge_id = get_or_create_folder(service, vault_root_id, "Knowledge")
        inbox_id = get_or_create_folder(service, vault_root_id, "Inbox")
        attachments_id = get_or_create_folder(service, vault_root_id, "Attachments")

        _VAULT_STRUCTURE = {
            "root": vault_root_id,
            "knowledge": knowledge_id,
            "inbox": inbox_id,
            "attachments": attachments_id,
        }
        return _VAULT_STRUCTURE
    except Exception as e:
        print(f"Failed to ensure vault structure: {e}")
        return None


def download_file_content(file_id: str) -> str:
    """指定された ID のテキストファイル (Markdown) をダウンロードして文字列として返す。"""
    service = get_gdrive_service()
    if not service:
        return ""
    try:
        request = service.files().get_media(fileId=file_id)
        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        return fh.getvalue().decode("utf-8")
    except Exception as e:
        print(f"Error downloading content of {file_id}: {e}")
        return ""


def download_file_bytes(file_id: str) -> bytes:
    """指定された ID のバイナリファイル (画像など) をダウンロードしてバイトデータを返す。"""
    service = get_gdrive_service()
    if not service:
        return b""
    try:
        request = service.files().get_media(fileId=file_id)
        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        return fh.getvalue()
    except Exception as e:
        print(f"Error downloading bytes of {file_id}: {e}")
        return b""


def upload_file_content(
    parent_id: str, name: str, content: str, file_id: str | None = None
) -> str | None:
    """テキストデータ (Markdown) を新規作成、または既存 ID に上書きアップロードする。"""
    service = get_gdrive_service()
    if not service:
        return None

    try:
        byte_data = content.encode("utf-8")
        fh = io.BytesIO(byte_data)
        media = MediaIoBaseUpload(fh, mimetype="text/markdown", resumable=True)

        if file_id:
            # 上書き更新
            file = (
                service.files()
                .update(fileId=file_id, media_body=media, fields="id")
                .execute()
            )
            return file["id"]
        else:
            # 新規作成
            file_metadata = {"name": name, "parents": [parent_id]}
            file = (
                service.files()
                .create(body=file_metadata, media_body=media, fields="id")
                .execute()
            )
            return file["id"]
    except Exception as e:
        print(f"Error uploading content for {name}: {e}")
        return None


def upload_file_bytes(
    parent_id: str,
    name: str,
    byte_data: bytes,
    mime_type: str,
    file_id: str | None = None,
) -> str | None:
    """バイナリデータ (画像など) を新規作成、または既存 ID に上書きアップロードする。"""
    service = get_gdrive_service()
    if not service:
        return None

    try:
        fh = io.BytesIO(byte_data)
        media = MediaIoBaseUpload(fh, mimetype=mime_type, resumable=True)

        if file_id:
            # 上書き更新
            file = (
                service.files()
                .update(fileId=file_id, media_body=media, fields="id")
                .execute()
            )
            return file["id"]
        else:
            # 新規作成
            file_metadata = {"name": name, "parents": [parent_id]}
            file = (
                service.files()
                .create(body=file_metadata, media_body=media, fields="id")
                .execute()
            )
            return file["id"]
    except Exception as e:
        print(f"Error uploading bytes for {name}: {e}")
        return None


def list_files_in_folder(folder_id: str) -> list[dict]:
    """指定したフォルダ配下のファイル一覧 (名前, ID) を取得する。"""
    service = get_gdrive_service()
    if not service:
        return []
    try:
        query = f"'{folder_id}' in parents and trashed = false"
        results = (
            service.files()
            .list(q=query, fields="files(id, name, mimeType, size, modifiedTime)")
            .execute()
        )
        return results.get("files", [])
    except Exception as e:
        print(f"Error listing files in folder {folder_id}: {e}")
        return []


def delete_file(file_id: str) -> bool:
    """指定された ID のファイルを Google ドライブから削除する。"""
    service = get_gdrive_service()
    if not service:
        return False
    try:
        service.files().delete(fileId=file_id).execute()
        return True
    except Exception as e:
        print(f"Error deleting file {file_id}: {e}")
        return False


def clear_vault_cache():
    """フォルダ構造のキャッシュをリセットする。"""
    global _VAULT_STRUCTURE
    _VAULT_STRUCTURE.clear()
