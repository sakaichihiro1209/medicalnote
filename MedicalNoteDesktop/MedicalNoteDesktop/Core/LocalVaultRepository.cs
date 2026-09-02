using System;
using System.Collections.Generic;
using System.IO;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace MedicalNoteDesktop.Core
{
    public class LocalVaultRepository
    {
        public string VaultPath { get; private set; }
        public string KnowledgePath => Path.Combine(VaultPath, "Knowledge");
        public string InboxPath => Path.Combine(VaultPath, "Inbox");
        public string AttachmentsPath => Path.Combine(VaultPath, "Attachments");
        public string SettingsFilePath => Path.Combine(VaultPath, "vault_settings.json");

        public LocalVaultRepository(string? customVaultPath = null)
        {
            if (string.IsNullOrWhiteSpace(customVaultPath))
            {
                string docs = Environment.GetFolderPath(Environment.SpecialFolder.MyDocuments);
                VaultPath = Path.Combine(docs, "MedicalNote", "My_Vault");
            }
            else
            {
                VaultPath = customVaultPath;
            }

            EnsureVaultDirectories();
        }

        public void EnsureVaultDirectories()
        {
            Directory.CreateDirectory(VaultPath);
            Directory.CreateDirectory(KnowledgePath);
            Directory.CreateDirectory(InboxPath);
            Directory.CreateDirectory(AttachmentsPath);
        }

        // --- Settings Management ---
        public JsonObject LoadVaultSettings()
        {
            if (File.Exists(SettingsFilePath))
            {
                try
                {
                    string json = File.ReadAllText(SettingsFilePath);
                    var node = JsonNode.Parse(json);
                    if (node is JsonObject obj) return obj;
                }
                catch { }
            }
            return new JsonObject
            {
                ["user_role"] = "",
                ["font_size"] = "medium",
                ["section_colors"] = new JsonObject()
            };
        }

        public void SaveVaultSettings(JsonObject settings)
        {
            try
            {
                string json = settings.ToJsonString(new JsonSerializerOptions { WriteIndented = true });
                File.WriteAllText(SettingsFilePath, json);
            }
            catch (Exception ex)
            {
                Console.WriteLine($"Failed to save vault settings: {ex.Message}");
            }
        }

        // --- Knowledge Notes Management ---
        public List<KnowledgeDocument> GetAllCards()
        {
            var list = new List<KnowledgeDocument>();
            if (!Directory.Exists(KnowledgePath)) return list;

            string[] files = Directory.GetFiles(KnowledgePath, "*.md");
            foreach (var file in files)
            {
                try
                {
                    string text = File.ReadAllText(file);
                    var doc = MarkdownParser.ParseMarkdown(text);
                    if (string.IsNullOrEmpty(doc.Title))
                    {
                        doc.Title = Path.GetFileNameWithoutExtension(file);
                    }
                    list.Add(doc);
                }
                catch { }
            }
            return list;
        }

        public KnowledgeDocument? GetCardByTitle(string title)
        {
            string filePath = Path.Combine(KnowledgePath, $"{title}.md");
            if (!File.Exists(filePath)) return null;

            try
            {
                string text = File.ReadAllText(filePath);
                var doc = MarkdownParser.ParseMarkdown(text);
                if (string.IsNullOrEmpty(doc.Title)) doc.Title = title;
                return doc;
            }
            catch
            {
                return null;
            }
        }

        public bool SaveCard(KnowledgeDocument doc)
        {
            try
            {
                EnsureVaultDirectories();
                string filePath = Path.Combine(KnowledgePath, $"{doc.Title}.md");
                string content = MarkdownParser.RenderMarkdown(doc);
                File.WriteAllText(filePath, content);
                return true;
            }
            catch (Exception ex)
            {
                Console.WriteLine($"Failed to save card {doc.Title}: {ex.Message}");
                return false;
            }
        }

        public bool DeleteCard(string title)
        {
            try
            {
                string filePath = Path.Combine(KnowledgePath, $"{title}.md");
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

        public string SaveAttachment(string originalFileName, byte[] data)
        {
            EnsureVaultDirectories();
            string ext = Path.GetExtension(originalFileName);
            string stamp = DateTime.Now.ToString("yyyyMMdd_HHmmss");
            string fileName = $"{stamp}_{Guid.NewGuid().ToString("N").Substring(0, 6)}{ext}";
            string fullPath = Path.Combine(AttachmentsPath, fileName);
            File.WriteAllBytes(fullPath, data);
            return fileName;
        }
    }
}
