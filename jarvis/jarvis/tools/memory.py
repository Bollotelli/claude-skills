"""Long-term memory: facts about the user, stored in ``~/.jarvis/gedaechtnis.json``.

The facts are written into the system prompt when a conversation starts, so
Jarvis remembers names, preferences and business details across sessions.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from .storage import JsonStore


class Memory:
    def __init__(self, path: Path):
        self.store = JsonStore(path)

    def all(self) -> list[dict]:
        return self.store.load()

    def remember(self, fact: str) -> dict:
        fact = fact.strip()
        with self.store.lock:
            facts = self.store.load()
            for existing in facts:
                if existing.get("fact", "").lower() == fact.lower():
                    return existing
            entry = {
                "id": JsonStore.next_id(facts),
                "fact": fact,
                "created": datetime.now().isoformat(timespec="seconds"),
            }
            facts.append(entry)
            self.store.save(facts)
            return entry

    def forget(self, memory_id: int) -> dict | None:
        with self.store.lock:
            facts = self.store.load()
            for entry in facts:
                if entry.get("id") == memory_id:
                    facts.remove(entry)
                    self.store.save(facts)
                    return entry
            return None
