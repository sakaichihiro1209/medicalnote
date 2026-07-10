"""
Knowledge Card の Markdown 形式を扱うモジュール。

仕様（仕様書 6章）:
    # タイトル

    ## Section名
    本文

    ## Section名
    本文

制約:
    - 同一Knowledge Card内でSection名は一意
    - Section順序はMarkdownの記載順
    - テンプレート固定は禁止、Sectionは自由追加可能

このモジュールは Markdown を「唯一の正データ」として扱うための
読み書きロジックのみを担当する。SQLite には一切依存しない。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class Section:
    name: str
    content: str = ""


@dataclass
class KnowledgeDocument:
    title: str
    sections: List[Section] = field(default_factory=list)

    def section_names(self) -> List[str]:
        return [s.name for s in self.sections]

    def has_duplicate_section_names(self) -> bool:
        names = self.section_names()
        return len(names) != len(set(names))

    def get_section(self, name: str) -> Optional[Section]:
        for s in self.sections:
            if s.name == name:
                return s
        return None


_H1_RE = re.compile(r"^#\s+(.*?)\s*$")
_H2_RE = re.compile(r"^##\s+(.*?)\s*$")


def parse_markdown(text: str) -> KnowledgeDocument:
    """Markdown文字列を KnowledgeDocument にパースする。

    - 最初に見つかった "# " 見出しをタイトルとする
    - それ以降の "## " 見出しを Section の区切りとする
    - Section本文は次の "## " が出るまでの行を結合したもの
    """
    lines = text.replace("\r\n", "\n").split("\n")

    title = ""
    sections: List[Section] = []
    current_name: Optional[str] = None
    current_lines: List[str] = []
    title_found = False

    def flush():
        nonlocal current_name, current_lines
        if current_name is not None:
            content = "\n".join(current_lines).strip("\n")
            sections.append(Section(name=current_name, content=content))
        current_name = None
        current_lines = []

    for line in lines:
        h1_match = _H1_RE.match(line)
        h2_match = _H2_RE.match(line)

        if h1_match and not title_found:
            title = h1_match.group(1)
            title_found = True
            continue

        if h2_match:
            flush()
            current_name = h2_match.group(1)
            continue

        if current_name is not None:
            current_lines.append(line)
        # h1見出し発見前 & section開始前の行は無視（空行など）

    flush()
    return KnowledgeDocument(title=title, sections=sections)


def render_markdown(doc: KnowledgeDocument) -> str:
    """KnowledgeDocument を Markdown 文字列に書き出す。"""
    parts = [f"# {doc.title}\n"]
    for sec in doc.sections:
        content = sec.content.strip("\n")
        parts.append(f"\n## {sec.name}\n\n{content}\n")
    text = "\n".join(parts).strip("\n") + "\n"
    return text


def extract_title_only(text: str) -> str:
    """先頭の '# ' 行のみを高速に取り出す（一覧表示の再構築などで使用）。"""
    for line in text.replace("\r\n", "\n").split("\n"):
        m = _H1_RE.match(line)
        if m:
            return m.group(1)
    return ""
