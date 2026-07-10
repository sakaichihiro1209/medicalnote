"""
Medical Knowledge Manager - Flask Web アプリケーションのメインコントローラ。
Jinja2 テンプレートと HTMX 部分更新を駆使したレスポンシブWebインターフェース。
"""

import os
from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    session,
    jsonify,
    Response,
    send_file,
)
from google_auth_oauthlib.flow import Flow
import io
import threading

# 相対パスでの core モジュールのインポート
from core import database
from core import gdrive_client
from core import knowledge_repository
from core import inbox_repository
from core import markdown_parser
from core import settings

# ローカルデバッグ時の HTTP OAuth 許可
os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"

app = Flask(__name__)
# Render.com等での本番鍵の取得、無ければデフォルト
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "medical-secret-key-12345")

# ドライブ操作排他ロック & ステータス
DRIVE_LOCK = threading.Lock()
DRIVE_TASK_ACTIVE = False
DRIVE_TASK_NAME = None


def check_drive_lock_and_respond():
    """現在バックグラウンドでドライブ操作が走っている場合、競合回避用のダイアログHTMLを即座に返す。"""
    global DRIVE_TASK_ACTIVE, DRIVE_TASK_NAME
    if DRIVE_TASK_ACTIVE:
        task_desc = DRIVE_TASK_NAME or "同期処理"
        # OOBスワップで競合モーダルをポップアップさせ、現在の処理は何も行わずに終了する
        conflict_html = f"""
        <div id="conflict-modal" class="modal-overlay" style="display: flex; z-index: 9999;" hx-swap-oob="true">
            <div class="modal-content" style="max-width: 400px; text-align: center; position: relative;">
                <h3 class="modal-title" style="color: var(--color-warning); margin-bottom: 0.75rem;">⚠️ ドライブ処理の順番待ち</h3>
                <div style="margin: 1.5rem 0;">
                    <div class="material-symbols-outlined" style="animation: spin 1.5s linear infinite; font-size: 3rem; color: var(--color-warning); margin-bottom: 0.75rem;">sync</div>
                    <p style="font-size: 0.95rem; font-weight: 600; line-height: 1.5;">現在、Google ドライブへの「{task_desc}」が進行中です。</p>
                    <p style="font-size: 0.8rem; color: var(--color-text-gray); margin-top: 0.5rem;">完了するまでしばらくお待ちいただき、時間をおいて再度お試しください。</p>
                </div>
                <button type="button" class="btn btn-secondary" style="width: 100%;" onclick="document.getElementById('conflict-modal').style.display='none'">閉じる</button>
            </div>
        </div>
        """
        return conflict_html
    return None


@app.template_filter("section_color_class")
def section_color_class(name: str) -> str:
    """セクション名からパステルカラー用のCSSクラスを一意に割り当てる。"""
    return knowledge_repository.get_or_create_section_color(name)


@app.before_request
def initialize_app():
    """リクエスト処理の前にキャッシュDBの初期化と、初回自動スキャンを行う。"""
    # SQLite キャッシュDBの初期化 (テーブルが無ければ自動作成)
    database.init_db()

    # キャッシュが空かつ、Google ドライブ連携済みの場合は自動再構築を実行
    creds = gdrive_client.get_credentials()
    if creds and knowledge_repository.check_cache_empty():
        print("Cache is empty. Automatically scanning Google Drive...")
        knowledge_repository.rebuild_cache_from_gdrive()


def get_oauth_flow() -> Flow | None:
    """OAuth2 認証フローインスタンスを作成する。"""
    client_id = settings.get("GOOGLE_CLIENT_ID")
    client_secret = settings.get("GOOGLE_CLIENT_SECRET")
    if not client_id or not client_secret:
        return None

    client_config = {
        "web": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }
    # コールバックURLを自動決定
    redirect_uri = url_for("google_callback", _external=True)

    # ローカル開発環境以外は常にプロトコルを強制的に https:// へ統一する (OAuth仕様対策)
    if not ("localhost" in redirect_uri or "127.0.0.1" in redirect_uri):
        redirect_uri = redirect_uri.replace("http://", "https://")

    return Flow.from_client_config(
        client_config,
        scopes=["https://www.googleapis.com/auth/drive.file"],
        redirect_uri=redirect_uri,
    )


# =====================================================================
# Google ドライブ OAuth2 ログインルート
# =====================================================================


@app.route("/login/google")
def google_login():
    """Google ログイン画面 (OAuth同意) へリダイレクトする。"""
    settings.set_val("VAULT_SYNCHRONIZED", "false")
    try:
        flow = get_oauth_flow()
        if not flow:
            # クライアントIDとシークレットが未入力の場合は、設定画面モーダルをトリガー表示する
            return render_template(
                "index.html",
                google_connected=False,
                show_settings_modal=True,
                error_msg="クライアントIDとクライアントシークレットが設定されていません。下のフォームから設定してください。",
            )

        # リフレッシュトークンを常に取得するために offline & prompt=consent を指定
        authorization_url, state = flow.authorization_url(
            access_type="offline", prompt="consent"
        )
        session["oauth_state"] = state
        return redirect(authorization_url)
    except Exception as e:
        import traceback
        err_detail = traceback.format_exc()
        return render_template(
            "index.html",
            google_connected=False,
            show_settings_modal=True,
            error_msg=f"Googleへの遷移処理中にエラーが発生しました:\n{e}\n\n{err_detail}",
        )


@app.route("/login/google/callback")
def google_callback():
    """Google 認証成功時のコールバック。リフレッシュトークンを取得してセッション及び永続ファイルへ保存。"""
    try:
        flow = get_oauth_flow()
        if not flow:
            return "OAuth設定エラー", 500

        auth_resp = request.url
        if not ("localhost" in auth_resp or "127.0.0.1" in auth_resp):
            auth_resp = auth_resp.replace("http://", "https://")

        flow.fetch_token(authorization_response=auth_resp)
        credentials = flow.credentials

        if credentials.refresh_token:
            # リフレッシュトークンを暗号化セッションに保存
            session["google_refresh_token"] = credentials.refresh_token
            # 設定ファイル settings.json に永続保存
            settings.set_val("GOOGLE_REFRESH_TOKEN", credentials.refresh_token)
            # メモリ上のフォルダ構造キャッシュを破棄して再ロードさせる
            gdrive_client.clear_vault_cache()
            # ログインしたアカウントに合わせて古いキャッシュを全クリア (混在防止)
            db = database.connect()
            try:
                with db:
                    db.execute("DELETE FROM knowledge")
                    db.execute("DELETE FROM inbox_cache")
            finally:
                db.close()
            # 初回フォルダ構造の構築
            gdrive_client.ensure_vault_structure()
            # ナレッジ & Inbox キャッシュの再構築を実行
            knowledge_repository.rebuild_cache_from_gdrive()
            inbox_repository.rebuild_inbox_cache()
            # 整合性確認完了
            settings.set_val("VAULT_SYNCHRONIZED", "true")

        return redirect(url_for("index"))
    except Exception as e:
        import traceback
        err_detail = traceback.format_exc()
        return render_template(
            "index.html",
            google_connected=False,
            show_settings_modal=True,
            error_msg=f"Google認証コールバック処理中にエラーが発生しました:\n{e}\n\n{err_detail}",
        )


@app.route("/logout/google")
def google_logout():
    """セッションの認証情報をクリアして接続を切断する。"""
    session.pop("google_refresh_token", None)
    settings.clear_auth_settings()
    gdrive_client.clear_vault_cache()
    # キャッシュDBも一旦クリア
    db = database.connect()
    try:
        with db:
            db.execute("DELETE FROM knowledge")
            db.execute("DELETE FROM inbox_cache")
    finally:
        db.close()
    return redirect(url_for("index"))


@app.route("/settings/save", methods=["POST"])
def save_app_settings():
    """クライアントID、シークレットなどを永続保存し、そのままGoogle認証を開始する。"""
    client_id = request.form.get("client_id", "").strip()
    client_secret = request.form.get("client_secret", "").strip()
    vault_folder_id = request.form.get("vault_folder_id", "").strip()

    if client_id:
        settings.set_val("GOOGLE_CLIENT_ID", client_id)
    if client_secret:
        settings.set_val("GOOGLE_CLIENT_SECRET", client_secret)
    if vault_folder_id:
        settings.set_val("GDRIVE_VAULT_FOLDER_ID", vault_folder_id)

    return redirect(url_for("google_login"))


# =====================================================================
# メイン画面 (ダッシュボード) & カード検索
# =====================================================================


@app.route("/")
def index():
    """メイン画面のレンダリング。"""
    google_connected = gdrive_client.get_credentials() is not None
    vault_synchronized = settings.get("VAULT_SYNCHRONIZED") == "true"
    unorganized_count = 0
    cards = []

    if google_connected and vault_synchronized:
        unorganized_count = inbox_repository.get_unorganized_count()
        cards = knowledge_repository.list_cards()

    # 「Knowledgeに整理する」から遷移してきた場合、自動でサイドバーを展開するパラメータを渡す
    organize_inbox_id = request.args.get("organize_inbox_id")

    # 現在の永続設定値を取得してテンプレートに渡す
    current_client_id = settings.get("GOOGLE_CLIENT_ID") or ""
    current_client_secret = settings.get("GOOGLE_CLIENT_SECRET") or ""
    current_vault_folder_id = settings.get("GDRIVE_VAULT_FOLDER_ID") or ""

    return render_template(
        "index.html",
        google_connected=google_connected,
        vault_synchronized=vault_synchronized,
        unorganized_inbox_count=unorganized_count,
        cards=cards,
        organize_inbox_id=organize_inbox_id,
        client_id=current_client_id,
        client_secret=current_client_secret,
        vault_folder_id=current_vault_folder_id,
    )


@app.route("/search")
def search_cards():
    """HTMX 用の部分更新検索エンドポイント。"""
    query = request.args.get("query", "")
    cards = knowledge_repository.list_cards(query)
    return render_template("partials/knowledge_list.html", cards=cards)


# =====================================================================
# Knowledge Card 操作
# =====================================================================


@app.route("/knowledge/<drive_file_id>")
def get_card(drive_file_id: str):
    """指定されたカードの詳細 (各セクション) を HTML 断片で返す。"""
    doc, info = knowledge_repository.get_card_by_id(drive_file_id)
    if not doc or not info:
        return "<div style='padding: 2rem; color: var(--color-danger);'>カードの読み込みに失敗しました</div>"

    suggested = knowledge_repository.get_suggested_sections()
    return render_template(
        "partials/card_detail.html", doc=doc, info=info, suggested_sections=suggested
    )


@app.route("/knowledge/new", methods=["POST"])
def new_card():
    """新規カードの作成。同名がある場合はエラーを返す。"""
    conflict = check_drive_lock_and_respond()
    if conflict:
        return conflict

    title = request.form.get("title", "").strip()
    if not title:
        return "<div style='padding: 2rem; color: var(--color-danger);'>タイトルが空です</div>", 400

    file_id = knowledge_repository.create_card(title)
    if not file_id:
        # 同名エラーまたはAPIエラー
        return (
            f"<script>alert('カード「{title}」は既に存在するか、作成に失敗しました。');</script>"
            "<div style='padding: 2rem; color: var(--color-danger);'>作成失敗</div>",
            400,
        )

    # 作成されたカードの詳細画面へリダイレクト（HTMXのターゲットを差し替える）
    return redirect(url_for("get_card", drive_file_id=file_id))


@app.route("/knowledge/<drive_file_id>/delete", methods=["DELETE"])
def delete_card(drive_file_id: str):
    """カードの削除。成功時はウェルカムプレースホルダーを返す。"""
    success = knowledge_repository.delete_card(drive_file_id)
    if not success:
        return "<script>alert('削除に失敗しました。');</script>", 400

    # 削除後は一覧をリフレッシュさせつつウェルカム画面を返す
    # HTMXで親要素や一覧を自動更新するために、クライアントサイドへのイベント発行ヘッダーを付与
    response = Response(
        render_template("index.html")
    )  # ダミーの空レイアウトとしてウェルカム画面を返す
    response.headers["HX-Trigger"] = "search-input"  # 一覧検索トリガーを起動してリフレッシュ
    
    # シンプルなウェルカムメッセージ断片を返却
    return (
        "<div class='welcome-container'>"
        "<span class='material-symbols-outlined welcome-icon'>medical_services</span>"
        "<h2 class='welcome-title'>削除しました</h2>"
        "<p class='welcome-text'>カードの削除が完了しました。</p>"
        "</div>"
    )


# =====================================================================
# Section 操作 & インライン編集
# =====================================================================


@app.route("/knowledge/<drive_file_id>/sections/<section_name>/edit")
def edit_section_form(drive_file_id: str, section_name: str):
    """インライン編集用の textarea 入力フォームを返す。"""
    doc, info = knowledge_repository.get_card_by_id(drive_file_id)
    if not doc or not info:
        return "Error", 404

    # 該当セクションの探索
    section = doc.get_section(section_name)
    if not section:
        return "Section not found", 404

    # HTML 内のインデックスを維持するためにクエリパラメータから取得
    index = request.args.get("index", "1")

    return render_template(
        "partials/section_edit.html", sec=section, info=info, index=index
    )


@app.route("/knowledge/<drive_file_id>/sections/<section_name>", methods=["GET"])
def get_section_card(drive_file_id: str, section_name: str):
    """指定されたセクションの閲覧モードのカード HTML 断片を返す（キャンセル時用）。"""
    doc, info = knowledge_repository.get_card_by_id(drive_file_id)
    if not doc or not info:
        return "Error", 404

    section = doc.get_section(section_name)
    if not section:
        return "Section not found", 404

    index = request.args.get("index", "1")

    return render_template(
        "partials/section_card.html", sec=section, info=info, index=index
    )


@app.route("/knowledge/<drive_file_id>/sections/<section_name>", methods=["POST"])
def save_section(drive_file_id: str, section_name: str):
    """インライン編集の内容を保存し、Googleドライブに書き戻した上でカード表示に戻す。"""
    conflict = check_drive_lock_and_respond()
    if conflict:
        return conflict

    doc, info = knowledge_repository.get_card_by_id(drive_file_id)
    if not doc or not info:
        return "Error", 404

    new_content = request.form.get("content", "")
    index = request.form.get("index", "1")

    # ドキュメント内のセクション本文を更新
    section = doc.get_section(section_name)
    if section:
        section.content = new_content
    else:
        return "Section not found", 404

    # Google ドライブへの保存とキャッシュ同期
    success = knowledge_repository.save_card(drive_file_id, doc)
    if not success:
        return "<span style='color: var(--color-danger);'>保存に失敗しました</span>", 500

    # 使用頻度のカウントアップ
    knowledge_repository.increment_section_usage(section_name)

    return render_template(
        "partials/section_card.html", sec=section, info=info, index=index
    )


@app.route("/knowledge/<drive_file_id>/sections/add", methods=["POST"])
def add_section(drive_file_id: str):
    """セクションをカードへ追加し、上書き保存する。同名がある場合はエラー。"""
    conflict = check_drive_lock_and_respond()
    if conflict:
        return conflict

    doc, info = knowledge_repository.get_card_by_id(drive_file_id)
    if not doc or not info:
        return "Error", 404

    sec_name = request.form.get("section_name", "").strip()
    if not sec_name:
        return "<script>alert('セクション名が空です。');</script>", 400

    # 重複チェック
    if doc.get_section(sec_name):
        return f"<script>alert('セクション「{sec_name}」は既にこのカードに存在します。');</script>", 400

    # 新規セクションの追加
    doc.sections.append(markdown_parser.Section(name=sec_name, content=""))
    success = knowledge_repository.save_card(drive_file_id, doc)
    if not success:
        return "<script>alert('追加に失敗しました。');</script>", 500

    # 使用頻度のカウントアップ
    knowledge_repository.increment_section_usage(sec_name)

    # 追加後の全セクション詳細画面を再ロード
    return redirect(url_for("get_card", drive_file_id=drive_file_id))


# =====================================================================
# Inbox キャプチャ操作
# =====================================================================


@app.route("/inbox/panel")
def inbox_panel():
    """Inbox パネル全体 (Jinja2) を返す。"""
    captures = inbox_repository.list_captures()
    return render_template("partials/inbox_panel.html", captures=captures)


@app.route("/inbox/list")
def inbox_list():
    """Inbox のメモカードリスト部分のみを返す。"""
    captures = inbox_repository.list_captures()
    return render_template("partials/inbox_list.html", captures=captures)


@app.route("/inbox/upload-status", methods=["GET"])
def upload_status():
    """現在バックグラウンドでドライブ書き込み（アップロード等）が走っているかどうかを取得する。"""
    global DRIVE_TASK_ACTIVE
    return jsonify({"active": DRIVE_TASK_ACTIVE})


def make_inbox_list_response(captures):
    """リストHTMLとバッジ更新OOB用HTMLを結合して返却する。"""
    list_html = render_template("partials/inbox_list.html", captures=captures)
    unorganized_count = inbox_repository.get_unorganized_count()
    if unorganized_count > 0:
        badge_html = f'<span class="badge" id="inbox-badge" hx-swap-oob="true" style="display: inline-block;">{unorganized_count}</span>'
        drawer_badge_html = f'<span class="badge" id="drawer-inbox-badge" hx-swap-oob="true" style="display: inline-block;">{unorganized_count}</span>'
    else:
        badge_html = '<span class="badge" id="inbox-badge" hx-swap-oob="true" style="display: none;">0</span>'
        drawer_badge_html = '<span class="badge" id="drawer-inbox-badge" hx-swap-oob="true" style="display: none;">0</span>'
    return list_html + badge_html + drawer_badge_html


@app.route("/inbox/new", methods=["POST"])
def new_capture():
    """新規キャプチャの作成 (バックグラウンド非同期アップロード対応)。"""
    # 既に別のアクティブタスクがあれば順番待ち
    conflict = check_drive_lock_and_respond()
    if conflict:
        return conflict

    text = request.form.get("text", "")
    title_hint = request.form.get("title_hint", "").strip()
    title_hint = title_hint if title_hint else None

    # 画像ファイルの読み込み (フォルダ選択とカメラ撮影の別々のフィールドからマージしてメモリに保持)
    images = []
    for key in ["images_lib", "images_cam"]:
        if key in request.files:
            files = request.files.getlist(key)
            for f in files:
                if f.filename:
                    images.append((f.filename, f.read()))

    # バックグラウンドスレッドの処理ロジック
    def bg_upload():
        global DRIVE_TASK_ACTIVE, DRIVE_TASK_NAME
        with DRIVE_LOCK:
            DRIVE_TASK_ACTIVE = True
            DRIVE_TASK_NAME = "新規メモのアップロード"
            try:
                # ドライブへアップロード
                inbox_repository.create_capture(text, title_hint=title_hint, images=images)
                # 完了後にキャッシュ再スキャン
                inbox_repository.rebuild_inbox_cache()
            except Exception as e:
                print(f"Background upload task failed: {e}")
            finally:
                DRIVE_TASK_ACTIVE = False
                DRIVE_TASK_NAME = None

    # 非同期スレッドを起動
    threading.Thread(target=bg_upload).start()

    # 即座にレスポンスを返す
    captures = inbox_repository.list_captures()
    list_html = make_inbox_list_response(captures)

    return list_html


@app.route("/inbox/<drive_file_id>/amend", methods=["POST"])
def amend_capture(drive_file_id: str):
    """既存の未整理メモに追記。"""
    conflict = check_drive_lock_and_respond()
    if conflict:
        return conflict

    append_text = request.form.get("append_text", "")
    inbox_repository.append_capture(drive_file_id, append_text)

    # 追記後のリスト表示をリフレッシュ
    captures = inbox_repository.list_captures()
    return make_inbox_list_response(captures)


@app.route("/inbox/<drive_file_id>/toggle-organized", methods=["POST"])
def toggle_inbox_organized(drive_file_id: str):
    """整理状態のチェックボックストグル。"""
    conflict = check_drive_lock_and_respond()
    if conflict:
        return conflict

    db = database.connect()
    try:
        cur = db.execute(
            "SELECT organized FROM inbox_cache WHERE drive_file_id = ?",
            (drive_file_id,),
        )
        row = cur.fetchone()
        current = row["organized"] if row else 0
        new_val = 0 if current == 1 else 1
        inbox_repository.set_organized(drive_file_id, new_val == 1)
    finally:
        db.close()

    # 更新後のリスト表示をリフレッシュ
    captures = inbox_repository.list_captures()
    return make_inbox_list_response(captures)


@app.route("/inbox/<drive_file_id>/content")
def get_inbox_content(drive_file_id: str):
    """非同期取得用のキャプチャテキストコンテンツ返却エンドポイント (ローカルキャッシュからロード)。"""
    db = database.connect()
    content = ""
    try:
        cur = db.execute(
            "SELECT content FROM inbox_cache WHERE drive_file_id = ?",
            (drive_file_id,),
        )
        row = cur.fetchone()
        if row:
            content = row["content"]
        else:
            # なければドライブからロード
            content = gdrive_client.download_file_content(drive_file_id)
    finally:
        db.close()
    return jsonify({"content": content})


@app.route("/inbox/unorganized-count")
def get_unorganized_inbox_count():
    """未整理件数を返す (JSでのバッジ更新同期用)。"""
    count = inbox_repository.get_unorganized_count()
    return jsonify({"count": count})


# =====================================================================
# 添付画像解決 & 再スキャン
# =====================================================================


@app.route("/attachments/<file_id>")
def get_attachment(file_id: str):
    """attachment://独自URIスキームを解決する画像プロキシエンドポイント。"""
    byte_data = gdrive_client.download_file_bytes(file_id)
    if not byte_data:
        return "Image not found", 404

    # MIMEタイプの決定
    # デフォルトを image/png にし、データの先頭シグネチャをチェックして対応
    mime_type = "image/png"
    if byte_data.startswith(b"\xff\xd8"):
        mime_type = "image/jpeg"
    elif byte_data.startswith(b"GIF8"):
        mime_type = "image/gif"
    elif byte_data.startswith(b"RIFF") and b"WEBP" in byte_data[:15]:
        mime_type = "image/webp"

    return send_file(io.BytesIO(byte_data), mimetype=mime_type)


@app.route("/rebuild-cache", methods=["POST"])
def rebuild_cache():
    """Google ドライブの内容からキャッシュSQLite DBを完全同期・再構築する。"""
    conflict = check_drive_lock_and_respond()
    if conflict:
        return conflict

    res = knowledge_repository.rebuild_cache_from_gdrive()
    print(f"Rebuild knowledge cache completed: {res}")

    # Inbox も一緒にキャッシュ再構築
    inbox_res = inbox_repository.rebuild_inbox_cache()
    print(f"Rebuild inbox cache completed: {inbox_res} files cached.")

    # 整合性が確認されたので同期済みフラグをセット
    settings.set_val("VAULT_SYNCHRONIZED", "true")

    # HTMX のリフレッシュ完了のタイミングで画面全体を再読み込みさせるために
    # クライアントへリダイレクトを指示するヘッダーを設定
    response = Response("")
    response.headers["HX-Refresh"] = "true"
    return response


# =====================================================================
# サーバー起動 (ローカル検証用)
# =====================================================================

if __name__ == "__main__":
    # Render.com等での本番起動は gunicorn が Procfile から実行するため、
    # 本ブロックはローカルテスト用
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
