using System;
using System.Collections.Generic;
using System.IO;
using System.Text.RegularExpressions;

namespace MedicalNoteDesktop.Core
{
    public class InboxCapture
    {
        public string FilePath { get; set; } = string.Empty;
        public string FileName { get; set; } = string.Empty;
        public string Title { get; set; } = string.Empty;
        public string DateTimeStr { get; set; } = string.Empty;
        public string CleanContent { get; set; } = string.Empty;
        public bool IsOrganized { get; set; } = false;
    }

    public class LocalInboxRepository
    {
        private readonly LocalVaultRepository _vaultRepo;

        public LocalInboxRepository(LocalVaultRepository vaultRepo)
        {
            _vaultRepo = vaultRepo;
        }

        public List<InboxCapture> GetAllCaptures()
        {
            var list = new List<InboxCapture>();
            string inboxDir = _vaultRepo.InboxPath;
            if (!Directory.Exists(inboxDir)) return list;

            string[] files = Directory.GetFiles(inboxDir, "*.md");
            foreach (var file in files)
            {
                try
                {
                    string fileName = Path.GetFileName(file);
                    string rawText = File.ReadAllText(file);
                    var (clean, isOrganized) = MarkdownParser.ParseOrganizedTag(rawText);

                    string title = Path.GetFileNameWithoutExtension(file);
                    string dtStr = File.GetLastWriteTime(file).ToString("yyyy-MM-dd HH:mm:ss");

                    // タイトルのパース
                    var matchTitle = Regex.Match(clean, @"^#\s*(.*)", RegexOptions.Multiline);
                    if (matchTitle.Success)
                    {
                        title = matchTitle.Groups[1].Value.Trim();
                    }

                    // 日時のパース
                    var matchDt = Regex.Match(clean, @"作成日時:\s*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})");
                    if (matchDt.Success)
                    {
                        dtStr = matchDt.Groups[1].Value;
                    }

                    list.Add(new InboxCapture
                    {
                        FilePath = file,
                        FileName = fileName,
                        Title = title,
                        DateTimeStr = dtStr,
                        CleanContent = clean,
                        IsOrganized = isOrganized
                    });
                }
                catch { }
            }

            // 日時降順ソート
            list.Sort((a, b) => string.Compare(b.DateTimeStr, a.DateTimeStr, StringComparison.Ordinal));
            return list;
        }

        public InboxCapture? CreateCapture(string title, string text, List<string>? attachmentFileNames = null)
        {
            _vaultRepo.EnsureVaultDirectories();
            string nowStr = DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss");
            string stamp = DateTime.Now.ToString("yyyyMMdd_HHmmss");
            string safeHeading = Regex.Replace(title, @"[\\/:*?""<>|]", "_");
            string fileName = $"{stamp}_{safeHeading}.md";
            string filePath = Path.Combine(_vaultRepo.InboxPath, fileName);

            var lines = new List<string>
            {
                $"# {safeHeading}",
                "",
                $"作成日時: {nowStr}",
                "<!-- organized: 0 -->",
                ""
            };

            if (!string.IsNullOrWhiteSpace(text))
            {
                lines.Add(text.Trim());
                lines.Add("");
            }

            if (attachmentFileNames != null)
            {
                foreach (var att in attachmentFileNames)
                {
                    lines.Add($"![](attachment://{att})");
                }
                lines.Add("");
            }

            string fullText = string.Join("\n", lines);
            File.WriteAllText(filePath, fullText);

            var (clean, isOrg) = MarkdownParser.ParseOrganizedTag(fullText);
            return new InboxCapture
            {
                FilePath = filePath,
                FileName = fileName,
                Title = safeHeading,
                DateTimeStr = nowStr,
                CleanContent = clean,
                IsOrganized = isOrg
            };
        }

        public bool SetOrganized(string fileName, bool isOrganized)
        {
            string filePath = Path.Combine(_vaultRepo.InboxPath, fileName);
            if (!File.Exists(filePath)) return false;

            try
            {
                string rawText = File.ReadAllText(filePath);
                string cleanText = MarkdownParser.ParseOrganizedTag(rawText).cleanContent;
                string newRawText = MarkdownParser.InjectOrganizedTag(cleanText, isOrganized);
                File.WriteAllText(filePath, newRawText);
                return true;
            }
            catch
            {
                return false;
            }
        }

        public bool DeleteCapture(string fileName)
        {
            try
            {
                string filePath = Path.Combine(_vaultRepo.InboxPath, fileName);
                if (File.Exists(filePath))
                {
                    File.Delete(filePath);
                }
                return true;
            }
            catch
            {
                return false;
            }
        }
    }
}
