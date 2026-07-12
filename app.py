"""
Medical Knowledge Manager - Flask Web アプリケーションのメインコントローラ。
Jinja2 テンプレートと HTMX 部分更新を駆使したレスポンシブWebインターフェース。
"""

import os
from datetime import datetime
import re
import html
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
    make_response,
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

# ドライブ操作排他ロック & ユーザー別アクティブタスク辞書
DRIVE_LOCK = threading.Lock()
DRIVE_TASKS = {}  # { "user_id": { "active": bool, "name": str } }

def is_drive_task_active(user_id: str | None = None) -> bool:
    """指定されたユーザーのバックグラウンド書き込みタスクが走っているかを取得。"""
    if not user_id:
        user_id = session.get("google_user_id")
    if not user_id:
        return False
    with DRIVE_LOCK:
        return DRIVE_TASKS.get(user_id, {}).get("active", False)

def get_drive_task_name(user_id: str | None = None) -> str:
    """指定されたユーザーのアクティブタスク名を取得。"""
    if not user_id:
        user_id = session.get("google_user_id")
    if not user_id:
        return "同期処理"
    with DRIVE_LOCK:
        return DRIVE_TASKS.get(user_id, {}).get("name", "同期処理") or "同期処理"

def set_drive_task_status(user_id: str | None, active: bool, name: str | None = None):
    """指定されたユーザーのタスク状態を更新。"""
    if not user_id:
        user_id = session.get("google_user_id")
    if not user_id:
        return
    with DRIVE_LOCK:
        DRIVE_TASKS[user_id] = {"active": active, "name": name}

def check_drive_lock_and_respond():
    """現在バックグラウンドでドライブ操作が走っている場合、競合回避用のダイアログHTMLを即座に返す。"""
    user_id = session.get("google_user_id")
    if is_drive_task_active(user_id):
        task_desc = get_drive_task_name(user_id)
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
    user_id = session.get("google_user_id")
    return knowledge_repository.get_or_create_section_color(name, user_id=user_id)


@app.template_filter("render_section_content")
def render_section_content_filter(content: str) -> str:
    """セクション本文内のwiki://ノートリンクを HTMX で遷移するアンカータグに置換する。"""
    if not content:
        return ""
    # HTML エスケープして安全にする
    escaped = html.escape(content)
    # wiki リンク [タイトル](wiki://ファイルID) ➔ HTMX SPA リンクへ置換
    pattern = r"\[(.*?)\]\(wiki://(.*?)\)"
    replacement = r'<a href="/knowledge/\2" hx-get="/knowledge/\2" hx-target="#detail-pane" hx-push-url="true" onclick="event.stopPropagation();" class="wiki-link" style="color: var(--color-primary); font-weight: bold; text-decoration: underline;">\1</a>'
    
    rendered = re.sub(pattern, replacement, escaped)
    return rendered


@app.errorhandler(Exception)
def handle_exception(e):
    """アプリ内のすべての未キャッチ例外を捉え、生のエラー情報を画面に表示する。"""
    import traceback
    err_detail = traceback.format_exc()
    return f"""
    <html>
    <body style="font-family: sans-serif; padding: 2rem; max-width: 800px; margin: 0 auto; line-height: 1.6;">
        <h2 style="color: #e53e3e; margin-bottom: 1rem;">⚠️ アプリケーションエラーが発生しました</h2>
        <p>処理中に以下の未キャッチ例外が発生しました：</p>
        <pre style="background: #f7fafc; padding: 1.25rem; border-radius: 6px; border: 1px solid #e2e8f0; overflow-x: auto; font-family: monospace; font-size: 0.9rem; line-height: 1.4; color: #2d3748;">{e}\n\n{err_detail}</pre>
        <br>
        <a href="/" style="display: inline-block; background: #3182ce; color: white; padding: 0.5rem 1.25rem; text-decoration: none; border-radius: 4px; font-weight: bold;">ホームに戻る</a>
    </body>
    </html>
    """, 500


@app.before_request
def initialize_app():
    """リクエスト処理の前に、ログインユーザー専用のキャッシュDBのテーブル初期化を行う。"""
    user_id = session.get("google_user_id")
    if user_id:
        database.init_db(user_id=user_id)
    else:
        database.init_db()


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
    session.pop("vault_synchronized", None)
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
        return f"""
        <html>
        <body style="font-family: sans-serif; padding: 2rem; max-width: 800px; margin: 0 auto; line-height: 1.6;">
            <h2 style="color: var(--color-danger, #e53e3e); margin-bottom: 1rem;">⚠️ 接続開始処理中にエラーが発生しました</h2>
            <p>Google ログイン開始処理中に以下の問題が発生しました：</p>
            <pre style="background: #f7fafc; padding: 1.25rem; border-radius: var(--radius-md, 6px); border: 1px solid #e2e8f0; overflow-x: auto; font-family: monospace; font-size: 0.9rem; line-height: 1.4; color: #2d3748;">{e}\n\n{err_detail}</pre>
            <br>
            <a href="/" style="display: inline-block; background: #3182ce; color: white; padding: 0.5rem 1.25rem; text-decoration: none; border-radius: 4px; font-weight: bold;">ホームに戻る</a>
        </body>
        </html>
        """, 500


@app.route("/login/google/callback")
def google_callback():
    """Google 認証成功時のコールバック。リフレッシュトークンを取得してセッションへ保存し、個別のDBキャッシュを構築。"""
    try:
        flow = get_oauth_flow()
        if not flow:
            return "OAuth設定エラー", 500

        code = request.args.get("code")
        if not code:
            return "認証コードが取得できませんでした", 400

        flow.fetch_token(code=code)
        credentials = flow.credentials

        refresh_token = credentials.refresh_token
        if not refresh_token:
            refresh_token = session.get("google_refresh_token")
            
        if not refresh_token:
            # トークンが無い場合は警告画面を出して失敗させる！
            return """
            <html>
            <body style="font-family: sans-serif; padding: 2rem; max-width: 800px; margin: 0 auto; line-height: 1.6;">
                <h2 style="color: #e53e3e; margin-bottom: 1rem;">⚠️ Google ドライブ連携に必要な認証が取得できませんでした</h2>
                <p>Google から接続の更新に必要な情報（リフレッシュトークン）が返されませんでした。</p>
                <p>これは、Googleアカウントにこのアプリのアクセス権がすでに残っている場合に発生します。以下の手順で解決してください：</p>
                <ol style="margin-left: 1.5rem; margin-bottom: 1.5rem;">
                    <li><a href="https://myaccount.google.com/connections" target="_blank" style="color: #3182ce; font-weight: bold;">Googleサードパーティ製アプリ管理</a>へアクセスします。</li>
                    <li>一覧から<strong>「新めも」または「新人めも」（このアプリ）</strong>を選択し、<strong>「接続の削除」</strong>をクリックして連携を一度解除します。</li>
                    <li>このアプリの画面に戻り、もう一度「Google 連携」を開始してください。</li>
                </ol>
                <a href="/logout/google" style="display: inline-block; background: #3182ce; color: white; padding: 0.5rem 1.25rem; text-decoration: none; border-radius: 4px; font-weight: bold;">設定をリセットしてやり直す</a>
            </body>
            </html>
            """, 400

        session["google_refresh_token"] = refresh_token
        
        # Google ユーザー情報からユニークIDを特定
        user_info = gdrive_client.get_user_info(refresh_token=refresh_token)
        if not user_info:
            return "Google ユーザー情報の取得に失敗しました", 500
            
        user_id = user_info.get("permissionId") or user_info.get("emailAddress")
        if not user_id:
            return "Google ユーザーの一意IDが特定できませんでした", 500
            
        session["google_user_id"] = user_id

        # ユーザーごとの SQLite キャッシュ DB を初期化・再構築
        database.init_db(user_id=user_id)
        
        # DBの user_config に refresh_token を永続保存（セッション切れ・スレッド用）
        db = database.connect(user_id=user_id)
        try:
            with db:
                db.execute(
                    "INSERT OR REPLACE INTO user_config (key, value) VALUES ('google_refresh_token', ?)",
                    (refresh_token,)
                )
        except Exception as e:
            settings.log_debug(f"Failed to save refresh_token to user_config: {e}")
        finally:
            db.close()
        
        # キャッシュの同期実行
        knowledge_repository.rebuild_cache_from_gdrive(user_id=user_id)
        inbox_repository.rebuild_inbox_cache(user_id=user_id)
        
        # セッションに同期完了フラグをセット
        session["vault_synchronized"] = "true"

        return redirect(url_for("index"))
    except Exception as e:
        import traceback
        err_detail = traceback.format_exc()
        return f"""
        <html>
        <body style="font-family: sans-serif; padding: 2rem; max-width: 800px; margin: 0 auto; line-height: 1.6;">
            <h2 style="color: var(--color-danger, #e53e3e); margin-bottom: 1rem;">⚠️ 認証処理中にエラーが発生しました</h2>
            <p>Google 認証完了後の初期セットアップ中に以下の問題が発生しました：</p>
            <pre style="background: #f7fafc; padding: 1.25rem; border-radius: var(--radius-md, 6px); border: 1px solid #e2e8f0; overflow-x: auto; font-family: monospace; font-size: 0.9rem; line-height: 1.4; color: #2d3748;">{e}\n\n{err_detail}</pre>
            <br>
            <a href="/" style="display: inline-block; background: #3182ce; color: white; padding: 0.5rem 1.25rem; text-decoration: none; border-radius: 4px; font-weight: bold;">ホームに戻る</a>
        </body>
        </html>
        """, 500


@app.route("/logout/google")
def google_logout():
    """セッションの認証情報をクリアして接続を切断する。"""
    user_id = session.pop("google_user_id", None)
    session.pop("google_refresh_token", None)
    session.pop("vault_synchronized", None)
    
    settings.clear_auth_settings()
    gdrive_client.clear_vault_cache()
    
    # ユーザー専用のキャッシュDBファイルを物理的に削除してクリーンアップ
    if user_id:
        try:
            db_path = database.get_db_path(user_id=user_id)
            if db_path.exists():
                db_path.unlink()
        except Exception as e:
            print(f"Failed to unlink database for user {user_id} on logout: {e}")
            
    return redirect(url_for("index"))


@app.route("/settings/save", methods=["POST"])
def save_app_settings():
    """クライアントID、シークレット、および身分設定を永続保存する。"""
    client_id = request.form.get("client_id", "").strip()
    client_secret = request.form.get("client_secret", "").strip()
    vault_folder_id = request.form.get("vault_folder_id", "").strip()
    user_role = request.form.get("user_role", "").strip()
    font_size = request.form.get("font_size", "medium").strip()

    if client_id:
        settings.set_val("GOOGLE_CLIENT_ID", client_id)
    if client_secret:
        settings.set_val("GOOGLE_CLIENT_SECRET", client_secret)
    if vault_folder_id:
        settings.set_val("GDRIVE_VAULT_FOLDER_ID", vault_folder_id)

    user_id = session.get("google_user_id")
    if user_id:
        # ローカル DB に保存
        db = database.connect(user_id=user_id)
        try:
            with db:
                db.execute(
                    "INSERT OR REPLACE INTO user_config (key, value) VALUES ('user_role', ?)",
                    (user_role,)
                )
                db.execute(
                    "INSERT OR REPLACE INTO user_config (key, value) VALUES ('font_size', ?)",
                    (font_size,)
                )
        except Exception as e:
            print(f"Failed to save user settings: {e}")
        finally:
            db.close()
        
        # Google ドライブへ vault_settings.json を即座に上書き保存
        try:
            knowledge_repository.save_vault_settings_to_gdrive(user_id=user_id)
        except Exception as e:
            print(f"Failed to sync settings to gdrive: {e}")

    google_connected = gdrive_client.get_credentials(user_id=user_id) is not None
    if google_connected:
        # すでに連携完了している場合は、ホームに戻るだけにする
        return redirect(url_for("index"))

    return redirect(url_for("google_login"))


# =====================================================================
# メイン画面 (ダッシュボード) & カード検索
# =====================================================================


@app.route("/")
def index():
    """メイン画面のレンダリング。"""
    google_connected = gdrive_client.get_credentials() is not None
    vault_synchronized = session.get("vault_synchronized") == "true"
    unorganized_count = 0
    cards = []
    
    user_id = session.get("google_user_id")
    needs_initial_sync = False
    if google_connected and user_id:
        needs_initial_sync = knowledge_repository.check_cache_empty(user_id=user_id)

    if google_connected and vault_synchronized:
        unorganized_count = inbox_repository.get_unorganized_count(user_id=user_id)
        cards = knowledge_repository.list_cards(user_id=user_id)

    # 「Knowledgeに整理する」から遷移してきた場合、自動でサイドバーを展開するパラメータを渡す
    organize_inbox_id = request.args.get("organize_inbox_id")

    # 現在の永続設定値を取得してテンプレートに渡す
    current_client_id = settings.get("GOOGLE_CLIENT_ID") or ""
    current_client_secret = settings.get("GOOGLE_CLIENT_SECRET") or ""
    current_vault_folder_id = settings.get("GDRIVE_VAULT_FOLDER_ID") or ""

    user_role = ""
    font_size = "medium"
    if google_connected and user_id:
        db = database.connect(user_id=user_id)
        try:
            cur = db.execute("SELECT key, value FROM user_config WHERE key IN ('user_role', 'font_size')")
            for row in cur.fetchall():
                if row["key"] == "user_role":
                    user_role = row["value"]
                elif row["key"] == "font_size":
                    font_size = row["value"]
        except Exception:
            pass
        finally:
            db.close()

    return render_template(
        "index.html",
        google_connected=google_connected,
        vault_synchronized=vault_synchronized,
        needs_initial_sync=needs_initial_sync,
        unorganized_inbox_count=unorganized_count,
        cards=cards,
        organize_inbox_id=organize_inbox_id,
        client_id=current_client_id,
        client_secret=current_client_secret,
        vault_folder_id=current_vault_folder_id,
        user_role=user_role,
        font_size=font_size,
    )


@app.route("/search")
def search_cards():
    """HTMX 用の部分更新検索エンドポイント。"""
    query = request.args.get("query", "")
    user_id = session.get("google_user_id")
    cards = knowledge_repository.list_cards(user_id=user_id, query=query)
    return render_template("partials/knowledge_list.html", cards=cards)


@app.route("/api/knowledge/search", methods=["GET"])
def search_knowledge_api():
    """他ノートリンク挿入用に、カードのタイトル部分一致検索を行い HTML フラグメントを返却する。"""
    query = request.args.get("q", "").strip()
    user_id = session.get("google_user_id")
    index = request.args.get("index", "1")
    
    cards = []
    if user_id:
        cards = knowledge_repository.list_cards(user_id=user_id, query=query)
        
    return render_template("partials/link_search_results.html", cards=cards, index=index)


# =====================================================================
# Knowledge Card 操作
# =====================================================================


@app.route("/knowledge/<drive_file_id>")
def get_card(drive_file_id: str):
    """指定されたカードの詳細 (各セクション) を HTML 断片で返す。"""
    user_id = session.get("google_user_id")
    doc, info = knowledge_repository.get_card_by_id(drive_file_id, user_id=user_id)
    if not doc or not info:
        return "<div style='padding: 2rem; color: var(--color-danger);'>カードの読み込みに失敗しました</div>"

    suggested = knowledge_repository.get_suggested_sections(user_id=user_id)
    edit_section = request.args.get("edit_section", "")
    return render_template(
        "partials/card_detail.html", 
        doc=doc, 
        info=info, 
        suggested_sections=suggested,
        edit_section=edit_section
    )


@app.route("/knowledge/new", methods=["POST"])
def new_card():
    """新規カードの作成。同名がある場合はエラーを返す。"""
    title = request.form.get("title", "").strip()
    if not title:
        return "<div style='padding: 2rem; color: var(--color-danger);'>タイトルが空です</div>", 400

    user_id = session.get("google_user_id")
    file_id = knowledge_repository.create_card(title, user_id=user_id)
    if not file_id:
        # 同名エラーまたはAPIエラー
        return (
            f"<script>alert('ノート「{title}」は既に存在するか、作成に失敗しました。');</script>"
            "<div style='padding: 2rem; color: var(--color-danger);'>作成失敗</div>",
            400,
        )

    # 作成されたノートの詳細画面へリダイレクト（HTMXのターゲットを差し替える）
    return redirect(url_for("get_card", drive_file_id=file_id))


@app.route("/knowledge/<drive_file_id>/delete", methods=["DELETE"])
def delete_card(drive_file_id: str):
    """カードの削除。成功時はウェルカムプレースホルダーを返す。"""
    user_id = session.get("google_user_id")
    success = knowledge_repository.delete_card(drive_file_id, user_id=user_id)
    if not success:
        return "<script>alert('削除に失敗しました。');</script>", 400

    # 削除後は一覧をリフレッシュさせつつウェルカム画面を返す
    # HTMXで親要素や一覧を自動更新するために、クライアントサイドへのイベント発行ヘッダーを付与
    response = make_response(
        "<div class='welcome-container'>"
        "<span class='material-symbols-outlined welcome-icon'>medical_services</span>"
        "<h2 class='welcome-title'>削除しました</h2>"
        "<p class='welcome-text'>カードの削除が完了しました。</p>"
        "</div>"
    )
    response.headers["HX-Trigger"] = "search-input"  # 一覧検索トリガーを起動してリフレッシュ
    return response


# =====================================================================
# Section 操作 & インライン編集
# =====================================================================


@app.route("/knowledge/<drive_file_id>/sections/<section_name>/edit")
def edit_section_form(drive_file_id: str, section_name: str):
    """インライン編集用の textarea 入力フォームを返す。"""
    user_id = session.get("google_user_id")
    doc, info = knowledge_repository.get_card_by_id(drive_file_id, user_id=user_id)
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
    user_id = session.get("google_user_id")
    doc, info = knowledge_repository.get_card_by_id(drive_file_id, user_id=user_id)
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
    user_id = session.get("google_user_id")
    doc, info = knowledge_repository.get_card_by_id(drive_file_id, user_id=user_id)
    if not doc or not info:
        return "Error", 404

    new_content = request.form.get("content", "")
    index = request.form.get("index", "1")

    # 現在の身分情報を取得
    user_role = ""
    db = database.connect(user_id=user_id)
    try:
        cur = db.execute("SELECT value FROM user_config WHERE key = 'user_role'")
        row = cur.fetchone()
        if row:
            user_role = row["value"]
    except Exception:
        pass
    finally:
        db.close()

    # ドキュメント内のセクション本文とメタデータを更新
    section = doc.get_section(section_name)
    if section:
        section.content = new_content
        section.updated_at = database.now_jst().strftime("%Y-%m-%d %H:%M")
        section.updated_by = user_role
    else:
        return "Section not found", 404

    # Google ドライブへの保存とキャッシュ同期
    success = knowledge_repository.save_card(drive_file_id, doc, user_id=user_id)
    if not success:
        return "<span style='color: var(--color-danger);'>保存に失敗しました</span>", 500

    # 使用頻度のカウントアップ
    knowledge_repository.increment_section_usage(section_name, user_id=user_id)

    return render_template(
        "partials/section_card.html", sec=section, info=info, index=index
    )


@app.route("/knowledge/<drive_file_id>/sections/reorder", methods=["POST"])
def reorder_sections(drive_file_id: str):
    """ドラッグ＆ドロップによるセクションの順番並び替え要求を処理し、非同期で Google ドライブへ保存する。"""
    user_id = session.get("google_user_id")
    doc, info = knowledge_repository.get_card_by_id(drive_file_id, user_id=user_id)
    if not doc or not info:
        return jsonify({"status": "error", "message": "Card not found"}), 404

    req_data = request.json or {}
    new_order = req_data.get("section_names", [])
    if not new_order:
        return jsonify({"status": "error", "message": "No section order provided"}), 400

    # doc.sections を新しい順番にソート
    name_to_sec = {sec.name: sec for sec in doc.sections}
    sorted_sections = []
    for name in new_order:
        if name in name_to_sec:
            sorted_sections.append(name_to_sec[name])
    
    # 漏れ防止 (新規追加直後などで一致しないものがあれば末尾に付加)
    for sec in doc.sections:
        if sec not in sorted_sections:
            sorted_sections.append(sec)

    doc.sections = sorted_sections

    # 非同期保存の実行 (内部で別スレッドが立ち上がり、トースト通知も自動連携される)
    success = knowledge_repository.save_card(drive_file_id, doc, user_id=user_id)
    if not success:
        return jsonify({"status": "error", "message": "Failed to initiate save"}), 500

    return jsonify({"status": "success"})


@app.route("/knowledge/<drive_file_id>/sections/add", methods=["POST"])
def add_section(drive_file_id: str):
    """セクションをカードへ追加し、上書き保存する。同名がある場合はエラー。"""
    user_id = session.get("google_user_id")
    doc, info = knowledge_repository.get_card_by_id(drive_file_id, user_id=user_id)
    if not doc or not info:
        return "Error", 404

    sec_name = request.form.get("section_name", "").strip()
    if not sec_name:
        return "<script>alert('セクション名が空です。');</script>", 400

    # 重複チェック
    if doc.get_section(sec_name):
        return f"<script>alert('セクション「{sec_name}」は既にこのノートに存在します。');</script>", 400

    # 現在の身分情報を取得
    user_role = ""
    db = database.connect(user_id=user_id)
    try:
        cur = db.execute("SELECT value FROM user_config WHERE key = 'user_role'")
        row = cur.fetchone()
        if row:
            user_role = row["value"]
    except Exception:
        pass
    finally:
        db.close()

    # 新規セクションの追加
    ts = database.now_jst().strftime("%Y-%m-%d %H:%M")
    doc.sections.append(markdown_parser.Section(
        name=sec_name,
        content="",
        updated_at=ts,
        updated_by=user_role
    ))
    success = knowledge_repository.save_card(drive_file_id, doc, user_id=user_id)
    if not success:
        return "<script>alert('追加に失敗しました。');</script>", 500

    # 使用頻度のカウントアップ
    knowledge_repository.increment_section_usage(sec_name, user_id=user_id)

    # 追加後のノート詳細画面を直接レンダリングして返却 (HTMXの相性バグを解消)
    suggested = knowledge_repository.get_suggested_sections(user_id=user_id)
    return render_template(
        "partials/card_detail.html",
        doc=doc,
        info=info,
        suggested_sections=suggested,
        edit_section=sec_name
    )


# =====================================================================
# Inbox キャプチャ操作
# =====================================================================


@app.route("/inbox/panel")
def inbox_panel():
    """Inbox パネル全体 (Jinja2) を返す。"""
    user_id = session.get("google_user_id")
    captures = inbox_repository.list_captures(user_id=user_id)
    return render_template("partials/inbox_panel.html", captures=captures)


@app.route("/inbox/list")
def inbox_list():
    """Inbox のメモカードリスト部分のみを返す。"""
    user_id = session.get("google_user_id")
    captures = inbox_repository.list_captures(user_id=user_id)
    return render_template("partials/inbox_list.html", captures=captures)


@app.route("/inbox/upload-status", methods=["GET"])
def upload_status():
    """現在バックグラウンドでドライブ書き込み（アップロード等）が走っているかどうかを取得する。"""
    user_id = session.get("google_user_id")
    return jsonify({"active": is_drive_task_active(user_id)})


def make_inbox_list_response(captures, user_id=None):
    """リストHTMLとバッジ更新OOB用HTMLを結合して返却する。"""
    list_html = render_template("partials/inbox_list.html", captures=captures)
    unorganized_count = inbox_repository.get_unorganized_count(user_id=user_id)
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
    user_id = session.get("google_user_id")
    refresh_token = session.get("google_refresh_token")

    def bg_upload():
        set_drive_task_status(user_id, True, "新規メモのアップロード")
        try:
            # バックグラウンドスレッド固有のトークンコンテキストをバインド
            gdrive_client.set_thread_refresh_token(refresh_token)
            # ドライブへアップロード
            inbox_repository.create_capture(text, title_hint=title_hint, images=images, user_id=user_id)
            # 完了後にキャッシュ再スキャン
            inbox_repository.rebuild_inbox_cache(user_id=user_id)
            gdrive_client.clear_thread_refresh_token()
        except Exception as e:
            print(f"Background upload task failed: {e}")
        finally:
            set_drive_task_status(user_id, False, None)

    # 非同期スレッドを起動
    threading.Thread(target=bg_upload).start()

    # 即座にレスポンスを返す
    captures = inbox_repository.list_captures(user_id=user_id)
    list_html = make_inbox_list_response(captures, user_id=user_id)

    return list_html


@app.route("/inbox/<drive_file_id>/edit", methods=["POST"])
def edit_capture(drive_file_id: str):
    """既存のメモを上書き編集保存する。"""
    conflict = check_drive_lock_and_respond()
    if conflict:
        return conflict

    title = request.form.get("title_hint", "").strip()
    text = request.form.get("text", "")

    # 新規追加の画像
    images = []
    for key in ["images_lib", "images_cam"]:
        if key in request.files:
            files = request.files.getlist(key)
            for f in files:
                if f.filename:
                    images.append((f.filename, f.read()))

    user_id = session.get("google_user_id")
    refresh_token = session.get("google_refresh_token")

    # 1. ローカル SQLite キャッシュを即時（同期）で更新！
    # これにより、返却する UI リストへ即座に変更が反映されます。
    inbox_repository.edit_capture(
        drive_file_id, title, text, new_images=images, user_id=user_id
    )

    # 2. 即座に最新のキャッシュリストをレンダリングして返却
    captures = inbox_repository.list_captures(user_id=user_id)
    return make_inbox_list_response(captures, user_id=user_id)


@app.route("/inbox/<drive_file_id>/details", methods=["GET"])
def get_inbox_details(drive_file_id: str):
    """メモ編集モーダル用に、既存メモのタイトル・生テキストをロードするための JSON レスポンス。"""
    user_id = session.get("google_user_id")
    db = database.connect(user_id=user_id)
    title = ""
    raw_text = ""
    try:
        cur = db.execute("SELECT title, content FROM inbox_cache WHERE drive_file_id = ?", (drive_file_id,))
        row = cur.fetchone()
        if row:
            title = row["title"]
            content = row["content"]
            
            # Markdown 本文から、見出し行とメタデータ（作成日時・修正日時）行を除去して「生テキスト」を復元する
            lines = content.splitlines()
            body_lines = []
            header_passed = 0
            
            for line in lines:
                if line.startswith("# ") and header_passed == 0:
                    header_passed = 1
                    continue
                if line.startswith("作成日時:") or line.startswith("修正日時:"):
                    continue
                if line.strip() == "" and header_passed < 3:
                    continue
                if line.startswith("![](attachment://"):
                    continue
                    
                body_lines.append(line)
                header_passed = 3
                
            raw_text = "\n".join(body_lines).strip()
    finally:
        db.close()

    return jsonify({"title": title, "text": raw_text})


@app.route("/inbox/<drive_file_id>/toggle-organized", methods=["POST"])
def toggle_inbox_organized(drive_file_id: str):
    """整理状態のチェックボックストグル。"""
    conflict = check_drive_lock_and_respond()
    if conflict:
        return conflict

    user_id = session.get("google_user_id")
    db = database.connect(user_id=user_id)
    try:
        cur = db.execute(
            "SELECT organized FROM inbox_cache WHERE drive_file_id = ?",
            (drive_file_id,),
        )
        row = cur.fetchone()
        current = row["organized"] if row else 0
        new_val = 0 if current == 1 else 1
        inbox_repository.set_organized(drive_file_id, new_val == 1, user_id=user_id)
    finally:
        db.close()

    # 更新後のリスト表示をリフレッシュ
    captures = inbox_repository.list_captures(user_id=user_id)
    return make_inbox_list_response(captures, user_id=user_id)


@app.route("/inbox/<drive_file_id>/content")
def get_inbox_content(drive_file_id: str):
    """非同期取得用のキャプチャテキストコンテンツ返却エンドポイント (ローカルキャッシュからロード)。"""
    user_id = session.get("google_user_id")
    db = database.connect(user_id=user_id)
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
            refresh_token = session.get("google_refresh_token")
            gdrive_client.set_thread_refresh_token(refresh_token)
            content = gdrive_client.download_file_content(drive_file_id) or ""
            gdrive_client.clear_thread_refresh_token()
    finally:
        db.close()
    return jsonify({"content": content})


@app.route("/inbox/unorganized-count")
def get_unorganized_inbox_count():
    """未整理件数を返す (JSでのバッジ更新同期用)。"""
    user_id = session.get("google_user_id")
    count = inbox_repository.get_unorganized_count(user_id=user_id)
    return jsonify({"count": count})


@app.route("/knowledge/sync-status", methods=["GET"])
def get_knowledge_sync_status():
    """現在バックグラウンドで未同期のノート (dirty=1) があるかどうかを返す。"""
    user_id = session.get("google_user_id")
    db = database.connect(user_id=user_id)
    try:
        cur = db.execute("SELECT COUNT(*) AS c FROM knowledge WHERE dirty = 1")
        count = cur.fetchone()["c"]
        return jsonify({"active": count > 0})
    except Exception as e:
        print(f"Error checking sync status: {e}")
        return jsonify({"active": False})
    finally:
        db.close()


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

    user_id = session.get("google_user_id")
    
    # 未完了のままスタックしているキャッシュの同期ロックフラグ (dirty=1) を強制リセット
    db = database.connect(user_id=user_id)
    try:
        with db:
            db.execute("UPDATE knowledge SET dirty = 0")
    except Exception as e:
        print(f"Failed to reset dirty flags on manual rebuild: {e}")
    finally:
        db.close()

    res = knowledge_repository.rebuild_cache_from_gdrive(user_id=user_id)
    print(f"Rebuild knowledge cache completed for user {user_id}: {res}")

    # Inbox も一緒にキャッシュ再構築
    inbox_res = inbox_repository.rebuild_inbox_cache(user_id=user_id)
    print(f"Rebuild inbox cache completed for user {user_id}: {inbox_res} files cached.")

    # 整合性の検証（安全ブレーキ）
    is_empty = knowledge_repository.check_cache_empty(user_id=user_id)
    restored = res.get("restored", 0) if isinstance(res, dict) else 0

    if restored == 0 and is_empty:
        # 同期が0件、かつキャッシュも空のままなら、無限リロードを防ぐためリフレッシュしない
        print(f"Initial sync failed or no notebooks found for user {user_id}. Refusing automatic refresh to prevent loops.")
        return """
        <div style="padding: 1.5rem; margin: 1rem; background-color: #fff5f5; border: 1.5px dashed var(--color-danger); border-radius: var(--radius-md); text-align: center; color: var(--color-danger);">
            <div class="material-symbols-outlined" style="font-size: 2.5rem; margin-bottom: 0.5rem;">warning</div>
            <div style="font-weight: bold; font-size: 0.95rem; margin-bottom: 0.25rem;">ノートが見つかりませんでした</div>
            <div style="font-size: 0.8rem; line-height: 1.5; opacity: 0.85;">
                Google ドライブの「My_Vault/Knowledge」フォルダ内に Markdown ファイルが1件も存在しないか、権限エラーが発生しています。<br>
                マイドライブに「My_Vault」フォルダが自動作成されているか、またその中にノートファイル（.md）があるかご確認ください。<br>
                接続をやり直す場合は、一度ログアウトして再試行してください。
            </div>
        </div>
        """, 200

    # 整合性が確認されたので同期済みフラグをセット
    session["vault_synchronized"] = "true"

    # HTMX のリフレッシュ完了のタイミングで画面全体を再読み込みさせるために
    # クライアントへリダイレクトを指示するヘッダーを設定
    response = Response("")
    response.headers["HX-Refresh"] = "true"
    return response


@app.route("/debug-logs")
def show_debug_logs():
    """バックグラウンド同期のデバッグログを表示する。"""
    user_id = session.get("google_user_id")
    db = database.connect(user_id=user_id)
    dirty_count = 0
    try:
        cur = db.execute("SELECT COUNT(*) AS c FROM knowledge WHERE dirty = 1")
        dirty_count = cur.fetchone()["c"]
    except Exception as e:
        dirty_count = f"Error: {e}"
    finally:
        db.close()

    logs_html = "<br>".join(settings.DEBUG_LOGS[::-1])  # 最新を上に
    return f"""
    <html>
    <head><title>新人めも - デバッグログ</title></head>
    <body style="font-family: monospace; padding: 2rem; background: #1a202c; color: #cbd5e0; line-height: 1.5;">
        <h2 style="color: #63b3ed; border-bottom: 2px solid #2d3748; padding-bottom: 0.5rem; margin-bottom: 1rem;">⚙️ 同期デバッグログ診断</h2>
        <div style="background: #2d3748; padding: 1rem; border-radius: 6px; margin-bottom: 1.5rem; border: 1px solid #4a5568;">
            <strong>ユーザー ID:</strong> {user_id}<br>
            <strong>未同期のデータ件数 (dirty=1):</strong> <span style="color: {'#fc8181' if str(dirty_count) != '0' else '#68d391'}; font-weight: bold;">{dirty_count}</span>
        </div>
        <div style="background: #2d3748; padding: 1.5rem; border-radius: 6px; border: 1px solid #4a5568; max-height: 500px; overflow-y: auto;">
            {logs_html if logs_html else "ログはまだ記録されていません。"}
        </div>
        <br>
        <button onclick="window.location.reload()" style="background: #3182ce; color: white; border: none; padding: 0.5rem 1rem; border-radius: 4px; font-weight: bold; cursor: pointer;">更新</button>
        <a href="/" style="color: #a0aec0; margin-left: 1rem; text-decoration: none;">← アプリに戻る</a>
    </body>
    </html>
    """


# =====================================================================
# サーバー起動 (ローカル検証用)
# =====================================================================

if __name__ == "__main__":
    # Render.com等での本番起動は gunicorn が Procfile から実行するため、
    # 本ブロックはローカルテスト用
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
