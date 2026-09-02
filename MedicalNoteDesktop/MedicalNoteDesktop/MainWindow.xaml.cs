using System;
using System.IO;
using System.Net;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Threading.Tasks;
using System.Web;
using System.Windows;
using MedicalNoteDesktop.Core;

namespace MedicalNoteDesktop
{
    public partial class MainWindow : Window
    {
        private HttpListener? _listener;
        private readonly LocalVaultRepository _vaultRepo;
        private readonly LocalInboxRepository _inboxRepo;
        private int _port = 5050;

        public MainWindow()
        {
            InitializeComponent();
            _vaultRepo = new LocalVaultRepository();
            _inboxRepo = new LocalInboxRepository(_vaultRepo);

            StartLocalServer();
            InitializeWebView();
        }

        private async void InitializeWebView()
        {
            try
            {
                await webView.EnsureCoreWebView2Async();
                webView.Source = new Uri($"http://localhost:{_port}/");
            }
            catch (Exception ex)
            {
                MessageBox.Show($"WebView2 初期化エラー: {ex.Message}\nWebView2 Runtime がインストールされているか確認してください。", "エラー", MessageBoxButton.OK, MessageBoxImage.Error);
            }
        }

        private void StartLocalServer()
        {
            Task.Run(() =>
            {
                while (true)
                {
                    try
                    {
                        _listener = new HttpListener();
                        _listener.Prefixes.Add($"http://localhost:{_port}/");
                        _listener.Start();
                        break;
                    }
                    catch
                    {
                        _port++;
                        if (_port > 5100) break;
                    }
                }

                while (_listener != null && _listener.IsListening)
                {
                    try
                    {
                        var context = _listener.GetContext();
                        Task.Run(() => HandleRequest(context));
                    }
                    catch { }
                }
            });
        }

        private void HandleRequest(HttpListenerContext context)
        {
            var request = context.Request;
            var response = context.Response;
            string rawUrl = request.Url?.AbsolutePath ?? "/";

            try
            {
                // 静的アセット (wwwroot)
                if (rawUrl.StartsWith("/static/"))
                {
                    string relativePath = rawUrl.Substring(8);
                    string filePath = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "wwwroot", "static", relativePath.Replace('/', '\\'));
                    if (File.Exists(filePath))
                    {
                        byte[] bytes = File.ReadAllBytes(filePath);
                        string mime = GetMimeType(filePath);
                        response.ContentType = mime;
                        response.OutputStream.Write(bytes, 0, bytes.Length);
                        response.Close();
                        return;
                    }
                }

                // API エンドポイント ＆ 画面レスポンス (C# 処理)
                string method = request.HttpMethod.ToUpper();
                
                // メインダッシュボード
                if (rawUrl == "/" && method == "GET")
                {
                    string html = GetIndexHtml();
                    SendHtml(response, html);
                    return;
                }

                // 設定保存
                if (rawUrl == "/settings/save" && method == "POST")
                {
                    var form = ParseForm(request);
                    var settings = _vaultRepo.LoadVaultSettings();
                    if (form.ContainsKey("user_role")) settings["user_role"] = form["user_role"];
                    if (form.ContainsKey("font_size")) settings["font_size"] = form["font_size"];
                    _vaultRepo.SaveVaultSettings(settings);

                    response.Redirect("/");
                    response.Close();
                    return;
                }

                // ノート検索 API
                if (rawUrl == "/knowledge/search" || rawUrl == "/api/knowledge/search")
                {
                    string query = request.QueryString["q"] ?? request.QueryString["query"] ?? "";
                    string html = GetKnowledgeListHtml(query);
                    SendHtml(response, html);
                    return;
                }

                // 新規ノート作成
                if (rawUrl == "/knowledge/new" && method == "POST")
                {
                    var form = ParseForm(request);
                    string title = form.GetValueOrDefault("title", "").Trim();
                    if (!string.IsNullOrEmpty(title))
                    {
                        var doc = new KnowledgeDocument { Title = title };
                        _vaultRepo.SaveCard(doc);
                        response.Redirect($"/knowledge/{HttpUtility.UrlEncode(title)}");
                        response.Close();
                        return;
                    }
                }

                // ノート詳細表示
                if (rawUrl.StartsWith("/knowledge/"))
                {
                    string title = HttpUtility.UrlDecode(rawUrl.Substring(11));
                    
                    // セクション並び替え API
                    if (title.EndsWith("/sections/reorder") && method == "POST")
                    {
                        string cardTitle = title.Replace("/sections/reorder", "");
                        string jsonBody = ReadBody(request);
                        var doc = _vaultRepo.GetCardByTitle(cardTitle);
                        if (doc != null)
                        {
                            var json = JsonNode.Parse(jsonBody);
                            var names = json?["section_names"]?.AsArray();
                            if (names != null)
                            {
                                var newSecs = new List<Section>();
                                foreach (var n in names)
                                {
                                    string sName = n?.ToString() ?? "";
                                    var sec = doc.GetSection(sName);
                                    if (sec != null) newSecs.Add(sec);
                                }
                                doc.Sections = newSecs;
                                _vaultRepo.SaveCard(doc);
                            }
                        }
                        SendJson(response, new { status = "success" });
                        return;
                    }

                    // ノート削除 API
                    if (method == "DELETE" && !title.Contains("/sections/"))
                    {
                        _vaultRepo.DeleteCard(title);
                        response.Headers.Add("HX-Trigger", "refresh-list");
                        SendHtml(response, "<div class='welcome-container'><h2>削除しました</h2></div>");
                        return;
                    }

                    // セクション削除 API
                    if (method == "DELETE" && title.Contains("/sections/") && title.EndsWith("/delete"))
                    {
                        string[] parts = title.Split("/sections/");
                        string cardTitle = parts[0];
                        string secName = parts[1].Replace("/delete", "");

                        var doc = _vaultRepo.GetCardByTitle(cardTitle);
                        if (doc != null)
                        {
                            doc.Sections.RemoveAll(s => s.Name.Equals(secName, StringComparison.OrdinalIgnoreCase));
                            _vaultRepo.SaveCard(doc);
                        }
                        SendHtml(response, "");
                        return;
                    }

                    // セクション保存 API
                    if (method == "POST" && title.Contains("/sections/"))
                    {
                        string[] parts = title.Split("/sections/");
                        string cardTitle = parts[0];
                        string secName = parts[1];

                        var form = ParseForm(request);
                        string content = form.GetValueOrDefault("content", "");
                        var settings = _vaultRepo.LoadVaultSettings();
                        string userRole = settings["user_role"]?.ToString() ?? "";

                        var doc = _vaultRepo.GetCardByTitle(cardTitle);
                        if (doc != null)
                        {
                            var sec = doc.GetSection(secName);
                            if (sec == null)
                            {
                                sec = new Section { Name = secName };
                                doc.Sections.Add(sec);
                            }
                            sec.Content = content;
                            sec.UpdatedAt = DateTime.Now.ToString("yyyy-MM-dd HH:mm");
                            sec.UpdatedBy = userRole;

                            _vaultRepo.SaveCard(doc);

                            string cardHtml = GetSectionCardHtml(sec, cardTitle);
                            SendHtml(response, cardHtml);
                            return;
                        }
                    }

                    // ノート詳細カードレンダリング
                    var kDoc = _vaultRepo.GetCardByTitle(title);
                    if (kDoc != null)
                    {
                        string detailHtml = GetCardDetailHtml(kDoc);
                        SendHtml(response, detailHtml);
                        return;
                    }
                }

                // Inbox メモ作成
                if (rawUrl == "/inbox/capture" && method == "POST")
                {
                    var form = ParseForm(request);
                    string title = form.GetValueOrDefault("title", "無題");
                    string text = form.GetValueOrDefault("text", "");
                    _inboxRepo.CreateCapture(title, text);

                    var captures = _inboxRepo.GetAllCaptures();
                    string listHtml = GetInboxListHtml(captures);
                    SendHtml(response, listHtml);
                    return;
                }

                // 404
                response.StatusCode = 404;
                response.Close();
            }
            catch (Exception ex)
            {
                response.StatusCode = 500;
                SendHtml(response, $"<html><body><h2>500 Error: {ex.Message}</h2></body></html>");
            }
        }

        private void SendHtml(HttpListenerResponse response, string html)
        {
            byte[] bytes = Encoding.UTF8.GetBytes(html);
            response.ContentType = "text/html; charset=utf-8";
            response.OutputStream.Write(bytes, 0, bytes.Length);
            response.Close();
        }

        private void SendJson(HttpListenerResponse response, object obj)
        {
            string json = JsonSerializer.Serialize(obj);
            byte[] bytes = Encoding.UTF8.GetBytes(json);
            response.ContentType = "application/json; charset=utf-8";
            response.OutputStream.Write(bytes, 0, bytes.Length);
            response.Close();
        }

        private string ReadBody(HttpListenerRequest request)
        {
            using var reader = new StreamReader(request.InputStream, request.ContentEncoding);
            return reader.ReadToEnd();
        }

        private System.Collections.Generic.Dictionary<string, string> ParseForm(HttpListenerRequest request)
        {
            string body = ReadBody(request);
            var dict = new System.Collections.Generic.Dictionary<string, string>();
            var parsed = HttpUtility.ParseQueryString(body);
            foreach (string? key in parsed.AllKeys)
            {
                if (key != null) dict[key] = parsed[key] ?? "";
            }
            return dict;
        }

        private string GetMimeType(string path)
        {
            string ext = Path.GetExtension(path).ToLower();
            return ext switch
            {
                ".css" => "text/css",
                ".js" => "text/javascript",
                ".png" => "image/png",
                ".jpg" or ".jpeg" => "image/jpeg",
                ".svg" => "image/svg+xml",
                _ => "application/octet-stream"
            };
        }

        // --- HTML Generation Helpers ---
        private string GetIndexHtml()
        {
            var settings = _vaultRepo.LoadVaultSettings();
            string fontSize = settings["font_size"]?.ToString() ?? "medium";

            return $@"<!DOCTYPE html>
<html lang=""ja"">
<head>
    <meta charset=""UTF-8"">
    <title>新人めも (Medical Knowledge Manager Desktop)</title>
    <link rel=""stylesheet"" href=""/static/style.css"">
    <link rel=""stylesheet"" href=""https://fonts.googleapis.com/css2?family=Material+Symbols+Outlined:opsz,wght,FILL,GRAD@20..48,100..700,0..1,-50..200"" />
    <script src=""https://unpkg.com/htmx.org@1.9.10""></script>
    <script src=""https://cdn.jsdelivr.net/npm/marked/marked.min.js""></script>
    <script src=""https://cdn.jsdelivr.net/npm/sortablejs@1.15.0/Sortable.min.js""></script>
</head>
<body class=""size-{fontSize}"">
    <div class=""app-header"">
        <div class=""header-title"">
            <span class=""material-symbols-outlined"" style=""font-size: 1.8rem;"">medical_services</span>
            新人めも <span style=""font-size: 0.75rem; opacity: 0.75; font-weight: normal;"">(Desktop版 - ローカル保存)</span>
        </div>
        <div>
            <button class=""btn btn-secondary"" onclick=""document.getElementById('settings-modal').style.display='flex'"">
                <span class=""material-symbols-outlined"">settings</span> 設定
            </button>
        </div>
    </div>
    
    <div class=""app-container"">
        <div class=""pane-sidebar"">
            <div class=""sidebar-header"">
                <div class=""search-box"">
                    <input type=""text"" name=""q"" class=""input-text"" placeholder=""ノートを検索...""
                           hx-get=""/knowledge/search"" hx-trigger=""keyup changed delay:200ms, refresh-list from:body""
                           hx-target=""#cards-list-container"" id=""search-input"">
                </div>
                <button class=""btn btn-primary"" onclick=""document.getElementById('new-card-modal').style.display='flex'"">
                    <span class=""material-symbols-outlined"">add</span> 新規ノート
                </button>
            </div>
            <div class=""list-scrollable"" id=""cards-list-container"">
                {GetKnowledgeListHtml("")}
            </div>
        </div>

        <div class=""pane-main active"" id=""detail-pane"">
            <div class=""welcome-container"">
                <span class=""material-symbols-outlined welcome-icon"">medical_services</span>
                <h2 class=""welcome-title"">Medical Knowledge Manager</h2>
                <p class=""welcome-text"">左側のリストからノートを選択するか、新規ノートを作成してください。</p>
            </div>
        </div>
    </div>

    <!-- 設定モーダル -->
    <div class=""modal-overlay"" id=""settings-modal"" style=""display: none;"">
        <div class=""modal-content"" style=""max-width: 450px;"">
            <h3 class=""modal-title"">アプリ設定</h3>
            <form action=""/settings/save"" method=""POST"">
                <div style=""margin-bottom: 1rem;"">
                    <label style=""font-size: 0.85rem; font-weight: 600; color: var(--color-text-gray); display: block; margin-bottom: 0.35rem;"">あなたの身分（例: 医師、看護師、学生）</label>
                    <input type=""text"" name=""user_role"" class=""input-text"" value=""{settings["user_role"]}"">
                </div>
                <div style=""margin-bottom: 1.5rem;"">
                    <label style=""font-size: 0.85rem; font-weight: 600; color: var(--color-text-gray); display: block; margin-bottom: 0.35rem;"">文字サイズ</label>
                    <select name=""font_size"" class=""input-text"">
                        <option value=""small"" {(fontSize == "small" ? "selected" : "")}>小</option>
                        <option value=""medium"" {(fontSize == "medium" ? "selected" : "")}>中 (標準)</option>
                        <option value=""large"" {(fontSize == "large" ? "selected" : "")}>大 (大きめ)</option>
                    </select>
                </div>
                <div style=""display: flex; justify-content: flex-end; gap: 0.5rem;"">
                    <button type=""button"" class=""btn btn-secondary"" onclick=""document.getElementById('settings-modal').style.display='none'"">キャンセル</button>
                    <button type=""submit"" class=""btn btn-primary"">保存</button>
                </div>
            </form>
        </div>
    </div>

    <!-- 新規ノートモーダル -->
    <div class=""modal-overlay"" id=""new-card-modal"" style=""display: none;"">
        <div class=""modal-content"" style=""max-width: 400px;"">
            <h3 class=""modal-title"">新規ノートの作成</h3>
            <form action=""/knowledge/new"" method=""POST"">
                <div style=""margin-bottom: 1.25rem;"">
                    <input type=""text"" name=""title"" class=""input-text"" placeholder=""ノートタイトルを入力..."" required>
                </div>
                <div style=""display: flex; justify-content: flex-end; gap: 0.5rem;"">
                    <button type=""button"" class=""btn btn-secondary"" onclick=""document.getElementById('new-card-modal').style.display='none'"">キャンセル</button>
                    <button type=""submit"" class=""btn btn-primary"">作成</button>
                </div>
            </form>
        </div>
    </div>

    <script>
        function renderMarkdownSections() {{
            if (typeof marked === 'undefined') return;
            const contentElements = document.querySelectorAll('.section-card-content');
            contentElements.forEach(el => {{
                if (el.getAttribute('data-markdown-rendered') === 'true') return;
                const raw = el.getAttribute('data-raw-content') || el.innerText;
                const html = marked.parse(raw);
                el.innerHTML = html;
                el.setAttribute('data-markdown-rendered', 'true');
                
                el.querySelectorAll('a').forEach(a => {{
                    const href = a.getAttribute('href');
                    if (href && href.startsWith('wiki://')) {{
                        const title = href.replace('wiki://', '');
                        a.setAttribute('href', `/knowledge/${{encodeURIComponent(title)}}`);
                        a.setAttribute('hx-get', `/knowledge/${{encodeURIComponent(title)}}`);
                        a.setAttribute('hx-target', '#detail-pane');
                        a.setAttribute('onclick', 'event.stopPropagation();');
                    }}
                }});
                if (typeof htmx !== 'undefined') htmx.process(el);
            }});
        }}

        document.addEventListener('DOMContentLoaded', renderMarkdownSections);
        document.addEventListener('htmx:afterOnLoad', renderMarkdownSections);
    </script>
</body>
</html>";
        }

        private string GetKnowledgeListHtml(string query)
        {
            var docs = _vaultRepo.GetAllCards();
            if (!string.IsNullOrWhiteSpace(query))
            {
                docs = docs.FindAll(d => d.Title.Contains(query, StringComparison.OrdinalIgnoreCase));
            }

            var sb = new StringBuilder();
            sb.AppendLine("<div class='knowledge-list'>");
            foreach (var doc in docs)
            {
                string encTitle = HttpUtility.UrlEncode(doc.Title);
                sb.AppendLine($@"
                <div class='knowledge-item' hx-get='/knowledge/{encTitle}' hx-target='#detail-pane'>
                    <div class='knowledge-item-title'>{HttpUtility.HtmlEncode(doc.Title)}</div>
                    <div class='knowledge-item-meta'>セクション: {doc.Sections.Count} 件</div>
                </div>");
            }
            sb.AppendLine("</div>");
            return sb.ToString();
        }

        private string GetCardDetailHtml(KnowledgeDocument doc)
        {
            string encTitle = HttpUtility.UrlEncode(doc.Title);
            var sb = new StringBuilder();
            sb.AppendLine($@"
            <div class='detail-header-wrapper'>
                <div>
                    <h1 class='detail-title'>{HttpUtility.HtmlEncode(doc.Title)}</h1>
                </div>
                <div class='detail-toolbar'>
                    <button class='btn btn-danger' hx-delete='/knowledge/{encTitle}' hx-target='#detail-pane' hx-confirm='ノートを削除しますか？'>
                        <span class='material-symbols-outlined'>delete</span> 削除
                    </button>
                </div>
            </div>

            <div class='card-grid' id='sections-container'>");

            for (int i = 0; i < doc.Sections.Count; i++)
            {
                var sec = doc.Sections[i];
                sb.AppendLine($"<div id='sec-wrapper-{i + 1}' data-section-name='{HttpUtility.HtmlEncode(sec.Name)}'>");
                sb.AppendLine(GetSectionCardHtml(sec, doc.Title, i + 1));
                sb.AppendLine("</div>");
            }

            sb.AppendLine("</div>");

            sb.AppendLine($@"
            <script>
                (function() {{
                    const container = document.getElementById('sections-container');
                    if (container && typeof Sortable !== 'undefined') {{
                        Sortable.create(container, {{
                            animation: 250,
                            handle: '.drag-handle',
                            onEnd: function (evt) {{
                                if (evt.oldIndex === evt.newIndex) return;
                                const sortedNames = Array.from(container.querySelectorAll('[data-section-name]')).map(el => el.getAttribute('data-section-name'));
                                fetch('/knowledge/{encTitle}/sections/reorder', {{
                                    method: 'POST',
                                    headers: {{ 'Content-Type': 'application/json' }},
                                    body: JSON.stringify({{ section_names: sortedNames }})
                                }});
                            }}
                        }});
                    }}
                }})();
            </script>");

            return sb.ToString();
        }

        private string GetSectionCardHtml(Section sec, string cardTitle, int index = 1)
        {
            string encTitle = HttpUtility.UrlEncode(cardTitle);
            string encSecName = HttpUtility.UrlEncode(sec.Name);
            string meta = !string.IsNullOrEmpty(sec.UpdatedAt) ? $"<div class='section-card-meta' style='text-align: right; font-size: 0.75rem; color: var(--color-text-gray); margin-top: 0.5rem;'>最終更新: {sec.UpdatedAt} {sec.UpdatedBy}</div>" : "";

            return $@"
            <div class='section-card' hx-get='/knowledge/{encTitle}/sections/{encSecName}/edit?index={index}' hx-target='#sec-wrapper-{index}'>
                <span class='material-symbols-outlined drag-handle' onclick='event.stopPropagation();'>drag_handle</span>
                <h3 class='section-card-title'>{HttpUtility.HtmlEncode(sec.Name)}</h3>
                <div class='section-card-content' data-raw-content='{HttpUtility.HtmlEncode(sec.Content)}'>{HttpUtility.HtmlEncode(sec.Content)}</div>
                {meta}
            </div>";
        }

        private string GetInboxListHtml(System.Collections.Generic.List<InboxCapture> captures)
        {
            var sb = new StringBuilder();
            foreach (var cap in captures)
            {
                string orgClass = cap.IsOrganized ? "organized" : "unorganized";
                string statusText = cap.IsOrganized ? "整理済み" : "未整理";
                sb.AppendLine($@"
                <div class='inbox-card {orgClass}'>
                    <div class='inbox-card-meta'>
                        <span>{HttpUtility.HtmlEncode(cap.DateTimeStr)}</span>
                        <span style='font-size: 0.75rem; padding: 0.15rem 0.4rem; border-radius: 4px; background: #edf2f7;'>{statusText}</span>
                    </div>
                    <div style='font-weight: 600; margin-bottom: 0.35rem;'>{HttpUtility.HtmlEncode(cap.Title)}</div>
                    <div class='inbox-card-body'>{HttpUtility.HtmlEncode(cap.CleanContent)}</div>
                </div>");
            }
            return sb.ToString();
        }
    }
}
