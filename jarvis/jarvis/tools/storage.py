"""Tiny thread-safe JSON list store with atomic writes."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path


class JsonStore:
    """A JSON file holding a list of dict records (one file per collection)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.lock = threading.RLock()

    def load(self) -> list[dict]:
        with self.lock:
            if not self.path.exists():
                return []
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except OSError:
                return []
            except json.JSONDecodeError:
                data = None
            if not isinstance(data, list):
                # Keep a damaged (e.g. hand-edited) file instead of overwriting it.
                backup = self.path.with_name(f"{self.path.stem}.defekt-{time.strftime('%Y%m%d-%H%M%S')}.json")
                os.replace(self.path, backup)
                print(f"[Jarvis] {self.path.name} war nicht lesbar und wurde nach {backup.name} verschoben.")
                return []
            return data

    def save(self, records: list[dict]) -> None:
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.path)

    @staticmethod
    def next_id(records: list[dict]) -> int:
        return max((int(r.get("id", 0)) for r in records), default=0) + 1
