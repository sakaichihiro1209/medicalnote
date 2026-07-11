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
import threading
from flask import session, has_request_context

# フォルダ構造のIDキャッシュ（メモリ上。ユーザーIDごとの二重構造）
# { "user_id": { "root": "...", "knowledge": "...", "inbox": "...", "attachments": "..." } }
_VAULT_STRUCTURE = {}

# スレッドローカルに refresh_token を格納して、非同期ワーカーでの credentials 取得をシームレスにする
_thread_local = threading.local()

def set_thread_refresh_token(token: str | None):
    _thread_local.refresh_token = token

def clear_thread_refresh_token():
    if hasattr(_thread_local, "refresh_token"):
        del _thread_local.refresh_token

def get_credentials(refresh_token: str | None = None) -> Credentials | None:
    """OAuth2 認証用の Credentials を取得する。"""
    client_id = settings.get("GOOGLE_CLIENT_ID")
    client_secret = settings.get("GOOGLE_CLIENT_SECRET")
    
    # 引数で渡されたトークンを最優先、次点にスレッドローカル、セッション、settings.json
    if not refresh_token:
        if hasattr(_thread_local, "refresh_token") and _thread_local.refresh_token:
            refresh_token = _thread_local.refresh_token
        elif has_request_context():
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


def get_gdrive_service(refresh_token: str | None = None):
    """Drive API サービスインスタンスを構築する。"""
    creds = get_credentials(refresh_token=refresh_token)
    if not creds:
        return None
    # google-auth ライブラリが期限切れトークンを自動で更新する
    return build("drive", "v3", credentials=creds)


def get_user_info(refresh_token: str | None = None) -> dict | None:
    """現在ログインしているユーザーの一意の情報を取得する。"""
    service = get_gdrive_service(refresh_token=refresh_token)
    if not service:
        return None
    try:
        about = service.about().get(fields="user").execute()
        return about.get("user")
    except Exception as e:
        print(f"Error fetching user info from drive: {e}")
        return None


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


def ensure_vault_structure(user_id: str | None = None) -> dict | None:
    """Google ドライブ上の Vault フォルダ構造 (My_Vault/{Knowledge,Inbox,Attachments}) を保証する。"""
    global _VAULT_STRUCTURE
    
    if not user_id and has_request_context():
        user_id = session.get("google_user_id")
        
    if not user_id:
        try:
            creds = get_credentials()
            if creds and creds.refresh_token:
                user_id = creds.refresh_token[:30]
        except Exception:
            pass
            
    if not user_id:
        user_id = "default"
        
    if user_id in _VAULT_STRUCTURE:
        return _VAULT_STRUCTURE[user_id]

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

        _VAULT_STRUCTURE[user_id] = {
            "root": vault_root_id,
            "knowledge": knowledge_id,
            "inbox": inbox_id,
            "attachments": attachments_id,
        }
        return _VAULT_STRUCTURE[user_id]
    except Exception as e:
        print(f"Failed to ensure vault structure for user {user_id}: {e}")
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


def clear_vault_cache(user_id: str | None = None):
    """フォルダ構造のキャッシュをリセットする。"""
    global _VAULT_STRUCTURE
    if not user_id and has_request_context():
        user_id = session.get("google_user_id")
    if user_id:
        _VAULT_STRUCTURE.pop(user_id, None)
    else:
        _VAULT_STRUCTURE.clear()
