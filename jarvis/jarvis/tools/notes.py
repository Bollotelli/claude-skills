"""Notes as plain Markdown files in ``~/.jarvis/notizen/``.

Plain files on purpose: you can open, edit or sync them with any editor
(Obsidian, iCloud, VS Code) and Jarvis still finds them.
"""

from __future__ import annotations

import re
import threading
import unicodedata
from datetime import datetime
from pathlib import Path

_UMLAUTS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss", "Ä": "Ae", "Ö": "Oe", "Ü": "Ue"})


def slugify(title: str) -> str:
    text = unicodedata.normalize("NFKD", title.translate(_UMLAUTS))
    text = text.encode("ascii", "ignore").decode().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text[:60] or "notiz"


class Notes:
    def __init__(self, folder: Path):
        self.folder = Path(folder)
        self.lock = threading.Lock()

    def _files(self) -> list[Path]:
        if not self.folder.exists():
            return []
        return sorted(self.folder.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)

    @staticmethod
    def _title(path: Path) -> str:
        try:
            with path.open(encoding="utf-8") as fh:
                first = fh.readline().strip()
        except OSError:
            return path.stem
        return first.lstrip("#").strip() or path.stem

    def find_by_title(self, title: str) -> Path | None:
        wanted = title.strip().lower()
        for path in self._files():
            if self._title(path).lower() == wanted:
                return path
        return None

    def save(self, title: str, content: str) -> tuple[Path, bool]:
        """Create a note, or append to an existing note with the same title.

        Returns ``(path, appended)``.
        """
        stamp = datetime.now().strftime("%d.%m.%Y %H:%M")
        with self.lock:
            self.folder.mkdir(parents=True, exist_ok=True)
            existing = self.find_by_title(title)
            if existing:
                with existing.open("a", encoding="utf-8") as fh:
                    fh.write(f"\n\n---\n_{stamp}_\n\n{content.strip()}\n")
                return existing, True
            base = f"{datetime.now():%Y-%m-%d}_{slugify(title)}"
            path = self.folder / f"{base}.md"
            n = 2
            while path.exists():
                path = self.folder / f"{base}-{n}.md"
                n += 1
            path.write_text(f"# {title.strip()}\n\n_{stamp}_\n\n{content.strip()}\n", encoding="utf-8")
            return path, False

    def search(self, query: str = "", limit: int = 8) -> list[dict]:
        words = [w for w in re.findall(r"\w+", query.lower()) if len(w) > 1]
        results = []
        for path in self._files():
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            lower = text.lower()
            if words and not all(w in lower for w in words):
                continue
            body = text.split("\n", 1)[-1]
            body = re.sub(r"_\d{2}\.\d{2}\.\d{4} \d{2}:\d{2}_|^---$", " ", body, flags=re.M)
            body = re.sub(r"\s+", " ", body).strip()
            results.append({"id": path.stem, "title": self._title(path), "snippet": body[:160]})
            if len(results) >= limit:
                break
        return results

    def read(self, note_id: str) -> str | None:
        note_id = note_id.strip()
        candidate = self.folder / f"{Path(note_id).name}.md"
        path = candidate if candidate.exists() else self.find_by_title(note_id)
        if path is None:
            return None
        return path.read_text(encoding="utf-8")
