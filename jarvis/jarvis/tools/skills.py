"""Search and read the skill library this app ships next to.

The repository around Jarvis holds hundreds of expert playbooks (SKILL.md
files) for strategy, finance, marketing, sales, operations and more. Jarvis
uses them as its business knowledge base: find the right playbook, read it,
apply its framework to the user's question.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

# Sync copies for other coding agents, the docs site and this app itself.
SKIP_DIRS = {
    ".git", ".codex", ".codex-plugin", ".gemini", ".hermes", ".vibe", ".claude",
    "docs", "audit", "node_modules", "__pycache__", "jarvis", "site",
}
MAX_CHARS = 60_000
STOPWORDS = {
    "the", "and", "for", "with", "how", "what", "when", "use", "using", "into", "from",
    "der", "die", "das", "und", "wie", "was", "fuer", "für", "mit", "ein", "eine", "ich",
}


def _frontmatter(text: str) -> dict[str, str]:
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    lines = text[3:end].splitlines()
    fields: dict[str, str] = {}
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        key, sep, value = line.partition(":")
        if not sep or line[:1].isspace() or key.strip() not in {"name", "description"}:
            continue
        value = value.strip()
        if value in {">", "|", ">-", "|-", ">+", "|+"}:  # folded / literal block
            block = []
            while i < len(lines) and (lines[i][:1].isspace() or not lines[i].strip()):
                block.append(lines[i].strip())
                i += 1
            value = " ".join(part for part in block if part)
        fields[key.strip()] = value.strip("\"'")
    return fields


def _stem(word: str) -> str:
    """Crude English stemming so 'pricing' finds 'price' and 'forecasts' finds 'forecast'."""
    for suffix in ("ing", "ed", "es", "s", "e"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)]
    return word


def _words(text: str) -> set[str]:
    return {
        _stem(w) for w in re.findall(r"[a-zäöüß0-9]+", text.lower())
        if len(w) > 2 and w not in STOPWORDS
    }


class SkillLibrary:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self._index: list[dict] | None = None

    def index(self) -> list[dict]:
        if self._index is None:
            entries = []
            for dirpath, dirnames, filenames in os.walk(self.root):
                dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
                if "SKILL.md" not in filenames:
                    continue
                path = Path(dirpath) / "SKILL.md"
                try:
                    with path.open(encoding="utf-8") as fh:
                        head = fh.read(4000)
                except OSError:
                    continue
                meta = _frontmatter(head)
                rel = path.relative_to(self.root).as_posix()
                name = meta.get("name") or path.parent.name
                description = meta.get("description", "").replace('\\"', '"')
                entries.append({
                    "path": rel,
                    "name": name,
                    "description": description,
                    "_name": _words(name.replace("-", " ")),
                    "_path": _words(rel.replace("/", " ").replace("-", " ")),
                    "_desc": _words(description),
                })
            self._index = entries
        return self._index

    def find(self, query: str, limit: int = 6) -> list[dict]:
        terms = _words(query)
        scored = []
        for entry in self.index():
            score = sum(
                4 * (term in entry["_name"]) + 2 * (term in entry["_path"]) + (term in entry["_desc"])
                for term in terms
            )
            if score:
                scored.append((score, entry))
        scored.sort(key=lambda pair: (-pair[0], pair[1]["path"]))
        return [
            {"path": entry["path"], "name": entry["name"], "description": entry["description"][:300]}
            for _, entry in scored[:limit]
        ]

    def read(self, rel_path: str) -> str:
        target = (self.root / rel_path).resolve()
        if not target.is_relative_to(self.root) or target.suffix.lower() != ".md":
            raise PermissionError("Nur .md-Dateien innerhalb der Skill-Bibliothek sind lesbar.")
        if not target.is_file():
            raise FileNotFoundError(f"Datei nicht gefunden: {rel_path}")
        text = target.read_text(encoding="utf-8", errors="replace")
        if len(text) > MAX_CHARS:
            text = text[:MAX_CHARS] + f"\n\n[... gekürzt: Datei hat {len(text)} Zeichen, gezeigt wurden {MAX_CHARS}]"
        return text
