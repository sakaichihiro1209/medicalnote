using System;
using System.Collections.Generic;
using System.Text;
using System.Text.RegularExpressions;

namespace MedicalNoteDesktop.Core
{
    public class Section
    {
        public string Name { get; set; } = string.Empty;
        public string Content { get; set; } = string.Empty;
        public string UpdatedAt { get; set; } = string.Empty;
        public string UpdatedBy { get; set; } = string.Empty;
    }

    public class KnowledgeDocument
    {
        public string Title { get; set; } = string.Empty;
        public List<Section> Sections { get; set; } = new List<Section>();

        public Section? GetSection(string name)
        {
            return Sections.Find(s => s.Name.Equals(name, StringComparison.OrdinalIgnoreCase));
        }
    }

    public static class MarkdownParser
    {
        private static readonly Regex UpdatedRegex = new Regex(@"<!--\s*updated:\s*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}(?::\d{2})?)(?:\s*\((.*?)\))?\s*-->", RegexOptions.Compiled);
        private static readonly Regex OrganizedRegex = new Regex(@"<!--\s*organized:\s*(\d+)\s*-->\n?", RegexOptions.Compiled);

        public static KnowledgeDocument ParseMarkdown(string text)
        {
            var doc = new KnowledgeDocument();
            if (string.IsNullOrWhiteSpace(text)) return doc;

            string[] lines = text.Split(new[] { "\r\n", "\r", "\n" }, StringSplitOptions.None);
            Section? currentSec = null;
            var currentLines = new List<string>();

            foreach (var line in lines)
            {
                if (line.StartsWith("# "))
                {
                    if (string.IsNullOrEmpty(doc.Title))
                    {
                        doc.Title = line.Substring(2).Trim();
                    }
                }
                else if (line.StartsWith("## "))
                {
                    if (currentSec != null)
                    {
                        currentSec.Content = string.Join("\n", currentLines).Trim();
                        doc.Sections.Add(currentSec);
                        currentLines.Clear();
                    }

                    currentSec = new Section
                    {
                        Name = line.Substring(3).Trim()
                    };
                }
                else
                {
                    if (currentSec != null)
                    {
                        // updated コメントのチェック
                        var match = UpdatedRegex.Match(line);
                        if (match.Success)
                        {
                            currentSec.UpdatedAt = match.Groups[1].Value;
                            currentSec.UpdatedBy = match.Groups[2].Value;
                        }
                        else
                        {
                            currentLines.Add(line);
                        }
                    }
                }
            }

            if (currentSec != null)
            {
                currentSec.Content = string.Join("\n", currentLines).Trim();
                doc.Sections.Add(currentSec);
            }

            return doc;
        }

        public static string RenderMarkdown(KnowledgeDocument doc)
        {
            var sb = new StringBuilder();
            sb.AppendLine($"# {doc.Title}");
            sb.AppendLine();

            foreach (var sec in doc.Sections)
            {
                sb.AppendLine($"## {sec.Name}");
                if (!string.IsNullOrEmpty(sec.UpdatedAt))
                {
                    string by = !string.IsNullOrEmpty(sec.UpdatedBy) ? $" ({sec.UpdatedBy})" : "";
                    sb.AppendLine($"<!-- updated:{sec.UpdatedAt}{by} -->");
                }
                if (!string.IsNullOrWhiteSpace(sec.Content))
                {
                    sb.AppendLine(sec.Content.Trim());
                }
                sb.AppendLine();
            }

            return sb.ToString().TrimEnd();
        }

        public static (string cleanContent, bool isOrganized) ParseOrganizedTag(string rawText)
        {
            bool isOrganized = rawText.Contains("<!-- organized: 1 -->");
            string clean = OrganizedRegex.Replace(rawText, "");
            return (clean, isOrganized);
        }

        public static string InjectOrganizedTag(string cleanText, bool isOrganized)
        {
            string clean = OrganizedRegex.Replace(cleanText, "");
            int val = isOrganized ? 1 : 0;
            if (clean.Contains("作成日時:"))
            {
                var parts = clean.Split(new[] { "作成日時:" }, 2, StringSplitOptions.None);
                var subparts = parts[1].Split(new[] { "\n" }, 2, StringSplitOptions.None);
                string dateLine = "作成日時:" + subparts[0];
                string rest = subparts.Length > 1 ? subparts[1] : "";
                return $"{parts[0]}{dateLine}\n<!-- organized: {val} -->\n{rest}";
            }
            return $"<!-- organized: {val} -->\n{clean}";
        }
    }
}
