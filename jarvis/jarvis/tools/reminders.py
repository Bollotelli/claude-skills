"""Reminders and timers stored in ``~/.jarvis/erinnerungen.json``.

A background thread checks once per second and hands due reminders to a
callback (Jarvis speaks them). Reminders that came due while Jarvis was not
running are announced at the next start.
"""

from __future__ import annotations

import threading
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Callable

from .storage import JsonStore


def parse_time(value: str, tz: tzinfo) -> datetime:
    """Parse an ISO 8601 timestamp; naive values are taken as local time."""
    dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=tz)


class Reminders:
    def __init__(self, path: Path, tz: tzinfo):
        self.store = JsonStore(path)
        self.tz = tz
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def add(self, text: str, due: datetime) -> dict:
        with self.store.lock:
            items = self.store.load()
            item = {
                "id": JsonStore.next_id(items),
                "text": text.strip(),
                "due": due.isoformat(timespec="seconds"),
                "status": "offen",
                "created": datetime.now(self.tz).isoformat(timespec="seconds"),
            }
            items.append(item)
            self.store.save(items)
            return item

    def pending(self) -> list[dict]:
        items = [r for r in self.store.load() if r.get("status") == "offen"]
        return sorted(items, key=lambda r: parse_time(r["due"], self.tz))

    def cancel(self, reminder_id: int) -> dict | None:
        with self.store.lock:
            items = self.store.load()
            for item in items:
                if item.get("id") == reminder_id and item.get("status") == "offen":
                    item["status"] = "abgesagt"
                    self.store.save(items)
                    return item
            return None

    def pop_due(self, now: datetime) -> list[dict]:
        """Mark every reminder that is due as fired and return them."""
        with self.store.lock:
            items = self.store.load()
            fired = []
            for item in items:
                if item.get("status") != "offen":
                    continue
                try:
                    due = parse_time(item["due"], self.tz)
                except (KeyError, ValueError):
                    item["status"] = "ungueltig"
                    continue
                if due <= now:
                    item["status"] = "erledigt"
                    fired.append(item)
            if fired:
                self.store.save(items)
            return fired

    def start(self, on_due: Callable[[dict], None], interval: float = 1.0) -> None:
        if self._thread and self._thread.is_alive():
            return

        def loop() -> None:
            while not self._stop.is_set():
                for item in self.pop_due(datetime.now(self.tz)):
                    try:
                        on_due(item)
                    except Exception as exc:  # never let a callback kill the timer
                        print(f"[Erinnerung] Fehler: {exc}")
                self._stop.wait(interval)

        self._thread = threading.Thread(target=loop, name="jarvis-reminders", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
